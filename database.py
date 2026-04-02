"""
Database module for the HP Connectivity Team Inventory Management System.

Handles all SQLite operations: schema creation, CRUD for devices,
audit logging, search, and statistics. Uses WAL mode for better
concurrency and a context manager for safe transactions.
"""

import sqlite3
import uuid
import json
import logging
import os
import shutil
import hashlib
import secrets
import subprocess
from contextlib import contextmanager
from datetime import datetime
from logging.handlers import RotatingFileHandler

# Path to the SQLite database file (same directory as this script)
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'inventory.db')

# Shared application logger (same file as app.py uses)
_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
os.makedirs(_LOG_DIR, exist_ok=True)
_audit_logger = logging.getLogger('inventory')
# Only add handler if the logger doesn't already have one (app.py may have set it up)
if not _audit_logger.handlers:
    _audit_logger.setLevel(logging.DEBUG)
    _h = RotatingFileHandler(os.path.join(_LOG_DIR, 'app.log'), maxBytes=2*1024*1024, backupCount=5)
    _h.setFormatter(logging.Formatter('%(asctime)s | %(levelname)-7s | %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
    _audit_logger.addHandler(_h)

# Default categories seeded on first run (sort_order determines dropdown order)
DEFAULT_CATEGORIES = [
    ('Printer', 'Printers and multifunction devices', 1),
    ('Router/AP', 'Routers and wireless access points', 2),
    ('Laptop/Phone/Tablet', 'Laptops, phones, and tablets', 3),
    ('Other', 'Uncategorized items', 4),
]

# Fields that can be updated via update_device()
UPDATABLE_FIELDS = [
    'name', 'category', 'manufacturer', 'model_number', 'serial_number',
    'connectivity', 'vendor_supplied', 'status', 'location',
    'assigned_to', 'notes',
]


def get_connection():
    """Create a new SQLite connection with WAL mode and foreign keys enabled."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


@contextmanager
def db_transaction():
    """Context manager that auto-commits on success, rolls back on error."""
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Create all tables, indexes, and seed default data. Safe to call multiple times."""
    with db_transaction() as conn:
        # Devices table
        conn.execute('''
            CREATE TABLE IF NOT EXISTS devices (
                device_id TEXT PRIMARY KEY,
                barcode_value TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                category TEXT DEFAULT '',
                manufacturer TEXT DEFAULT '',
                model_number TEXT DEFAULT '',
                serial_number TEXT DEFAULT '',
                connectivity TEXT DEFAULT '',
                vendor_supplied INTEGER DEFAULT 0,
                status TEXT DEFAULT 'available'
                    CHECK(status IN ('available','checked_out','retired','lost')),
                location TEXT DEFAULT '',
                assigned_to TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Audit log table (append-only)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS audit_log (
                log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                action TEXT NOT NULL,
                performed_by TEXT DEFAULT '',
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                details TEXT DEFAULT '',
                FOREIGN KEY (device_id) REFERENCES devices(device_id)
            )
        ''')

        # Categories table
        conn.execute('''
            CREATE TABLE IF NOT EXISTS categories (
                category_id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                description TEXT DEFAULT '',
                sort_order INTEGER DEFAULT 99
            )
        ''')

        # Users table for authentication
        conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'viewer'
                    CHECK(role IN ('admin','viewer')),
                display_name TEXT DEFAULT '',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_login DATETIME
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)')

        # Indexes for common queries
        conn.execute('CREATE INDEX IF NOT EXISTS idx_devices_status ON devices(status)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_devices_category ON devices(category)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_devices_barcode ON devices(barcode_value)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_audit_device ON audit_log(device_id)')

        # Seed default categories
        for name, desc, sort_ord in DEFAULT_CATEGORIES:
            conn.execute(
                'INSERT OR IGNORE INTO categories (name, description, sort_order) VALUES (?, ?, ?)',
                (name, desc, sort_ord)
            )
        # Ensure sort_order is up to date for existing databases
        for name, desc, sort_ord in DEFAULT_CATEGORIES:
            conn.execute(
                'UPDATE categories SET sort_order = ? WHERE name = ?',
                (sort_ord, name)
            )

        # Seed default admin user if no users exist (password: admin)
        user_count = conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]
        if user_count == 0:
            salt = secrets.token_hex(16)
            pw_hash = hashlib.sha256((salt + 'admin').encode()).hexdigest()
            conn.execute(
                'INSERT INTO users (username, password_hash, salt, role, display_name) VALUES (?, ?, ?, ?, ?)',
                ('admin', pw_hash, salt, 'admin', 'Administrator')
            )


def generate_device_id():
    """Generate a short unique device ID (10 hex chars)."""
    return uuid.uuid4().hex[:10]


def generate_barcode_value(device_id):
    """Generate the barcode string for a device (e.g. 'INV-A3F8C91B0E')."""
    return f"INV-{device_id.upper()}"


def _insert_device(conn, data, performed_by='system'):
    """Internal helper: insert a device and log it. Returns device_id. Takes existing conn."""
    device_id = generate_device_id()
    barcode_value = generate_barcode_value(device_id)

    conn.execute('''
        INSERT INTO devices (device_id, barcode_value, name, category, manufacturer,
            model_number, serial_number, connectivity, vendor_supplied, status,
            location, assigned_to, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        device_id,
        barcode_value,
        data.get('name', ''),
        data.get('category', ''),
        data.get('manufacturer', ''),
        data.get('model_number', ''),
        data.get('serial_number', ''),
        data.get('connectivity', ''),
        int(data.get('vendor_supplied', 0)),
        data.get('status', 'available'),
        data.get('location', ''),
        data.get('assigned_to', ''),
        data.get('notes', ''),
    ))

    log_action(conn, device_id, 'added', performed_by, f'Device "{data.get("name", "")}" added')
    return device_id


def add_device(data, performed_by='system'):
    """Add a new device to the inventory. Returns the new device_id."""
    with db_transaction() as conn:
        return _insert_device(conn, data, performed_by)


def update_device(device_id, data, performed_by='system'):
    """Update a device's fields. Logs a diff of what changed."""
    with db_transaction() as conn:
        # Get current values
        row = conn.execute('SELECT * FROM devices WHERE device_id = ?', (device_id,)).fetchone()
        if not row:
            raise ValueError(f"Device {device_id} not found")

        # Build diff of changes
        changes = {}
        for field in UPDATABLE_FIELDS:
            if field in data and str(data[field]) != str(row[field]):
                changes[field] = {'old': row[field], 'new': data[field]}

        if not changes:
            return  # Nothing changed

        # Build UPDATE statement for changed fields only
        set_parts = []
        values = []
        for field in changes:
            set_parts.append(f"{field} = ?")
            values.append(data[field])
        set_parts.append("updated_at = CURRENT_TIMESTAMP")
        values.append(device_id)

        conn.execute(
            f"UPDATE devices SET {', '.join(set_parts)} WHERE device_id = ?",
            values
        )

        log_action(conn, device_id, 'updated', performed_by, json.dumps(changes))


def get_device(device_id):
    """Get a single device by ID. Returns dict or None."""
    with db_transaction() as conn:
        row = conn.execute('SELECT * FROM devices WHERE device_id = ?', (device_id,)).fetchone()
        return dict(row) if row else None


def get_device_by_barcode(barcode_value):
    """Look up a device by its barcode value (case-insensitive)."""
    with db_transaction() as conn:
        row = conn.execute(
            'SELECT * FROM devices WHERE UPPER(barcode_value) = UPPER(?)',
            (barcode_value,)
        ).fetchone()
        return dict(row) if row else None


def search_devices(query='', category='', status='', connectivity='', location=''):
    """
    Search devices with optional filters. All filters combined with AND.
    The query param searches across multiple text fields with LIKE.
    Excludes retired devices by default (unless status='retired' is explicitly requested).
    """
    with db_transaction() as conn:
        conditions = []
        params = []

        # Exclude retired unless explicitly filtering for them
        if status and status != 'retired':
            conditions.append("status = ?")
            params.append(status)
        elif status == 'retired':
            conditions.append("status = 'retired'")
        else:
            conditions.append("status != 'retired'")

        if category:
            conditions.append("category = ?")
            params.append(category)

        if connectivity:
            conditions.append("connectivity LIKE ?")
            params.append(f"%{connectivity}%")

        if location:
            conditions.append("location LIKE ?")
            params.append(f"%{location}%")

        if query:
            conditions.append("""
                (name LIKE ? OR manufacturer LIKE ? OR model_number LIKE ?
                 OR serial_number LIKE ? OR barcode_value LIKE ?
                 OR assigned_to LIKE ? OR notes LIKE ?)
            """)
            like_q = f"%{query}%"
            params.extend([like_q] * 7)

        where = " AND ".join(conditions) if conditions else "1=1"
        sql = f"SELECT * FROM devices WHERE {where} ORDER BY updated_at DESC"

        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def get_all_devices(include_retired=False):
    """Get all devices, optionally including retired ones."""
    with db_transaction() as conn:
        if include_retired:
            rows = conn.execute('SELECT * FROM devices ORDER BY updated_at DESC').fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM devices WHERE status != 'retired' ORDER BY updated_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]


def retire_device(device_id, performed_by='system'):
    """Soft-delete a device by setting its status to 'retired'."""
    with db_transaction() as conn:
        conn.execute(
            "UPDATE devices SET status='retired', updated_at=CURRENT_TIMESTAMP WHERE device_id=?",
            (device_id,)
        )
        log_action(conn, device_id, 'retired', performed_by, 'Device retired')


def checkout_device(device_id, assigned_to, performed_by='system'):
    """Check out a device to a person."""
    with db_transaction() as conn:
        conn.execute(
            "UPDATE devices SET status='checked_out', assigned_to=?, updated_at=CURRENT_TIMESTAMP WHERE device_id=?",
            (assigned_to, device_id)
        )
        log_action(conn, device_id, 'checked_out', performed_by, f'Assigned to {assigned_to}')


def checkin_device(device_id, performed_by='system'):
    """Return a device (check it back in)."""
    with db_transaction() as conn:
        # Get who had it for the log message
        row = conn.execute('SELECT assigned_to FROM devices WHERE device_id=?', (device_id,)).fetchone()
        prev_assignee = row['assigned_to'] if row else ''

        conn.execute(
            "UPDATE devices SET status='available', assigned_to='', updated_at=CURRENT_TIMESTAMP WHERE device_id=?",
            (device_id,)
        )
        log_action(conn, device_id, 'returned', performed_by,
                    f'Returned by {prev_assignee}' if prev_assignee else 'Device returned')


def log_action(conn, device_id, action, performed_by='', details=''):
    """Append an entry to the audit log and application log."""
    conn.execute(
        'INSERT INTO audit_log (device_id, action, performed_by, details) VALUES (?, ?, ?, ?)',
        (device_id, action, performed_by, details)
    )
    # Also write to the application log so audit events appear in the unified log viewer
    detail_str = f' — {details}' if details else ''
    _audit_logger.info('AUDIT device_id=%s action=%s by=%s%s', device_id, action, performed_by or 'system', detail_str)


def get_audit_log(device_id=None, limit=100):
    """Get audit log entries, optionally filtered by device. Most recent first."""
    with db_transaction() as conn:
        if device_id:
            rows = conn.execute('''
                SELECT a.*, d.name as device_name
                FROM audit_log a
                LEFT JOIN devices d ON a.device_id = d.device_id
                WHERE a.device_id = ?
                ORDER BY a.timestamp DESC LIMIT ?
            ''', (device_id, limit)).fetchall()
        else:
            rows = conn.execute('''
                SELECT a.*, d.name as device_name
                FROM audit_log a
                LEFT JOIN devices d ON a.device_id = d.device_id
                ORDER BY a.timestamp DESC LIMIT ?
            ''', (limit,)).fetchall()
        return [dict(r) for r in rows]


def get_categories():
    """Get all categories ordered by sort_order."""
    with db_transaction() as conn:
        rows = conn.execute('SELECT * FROM categories ORDER BY sort_order, name').fetchall()
        return [dict(r) for r in rows]


def get_stats():
    """
    Get dashboard statistics:
    - Device counts by status
    - Breakdown by category
    - Breakdown by connectivity type
    - Recent activity (last 15 entries)
    """
    with db_transaction() as conn:
        # Overall counts
        total = conn.execute("SELECT COUNT(*) FROM devices WHERE status != 'retired'").fetchone()[0]
        available = conn.execute("SELECT COUNT(*) FROM devices WHERE status = 'available'").fetchone()[0]
        checked_out = conn.execute("SELECT COUNT(*) FROM devices WHERE status = 'checked_out'").fetchone()[0]
        lost = conn.execute("SELECT COUNT(*) FROM devices WHERE status = 'lost'").fetchone()[0]
        retired = conn.execute("SELECT COUNT(*) FROM devices WHERE status = 'retired'").fetchone()[0]

        # By category (exclude retired)
        by_category = conn.execute('''
            SELECT category, COUNT(*) as count
            FROM devices WHERE status != 'retired' AND category != ''
            GROUP BY category ORDER BY count DESC
        ''').fetchall()

        # By connectivity (exclude retired and empty)
        by_connectivity = conn.execute('''
            SELECT connectivity, COUNT(*) as count
            FROM devices WHERE status != 'retired' AND connectivity != ''
            GROUP BY connectivity ORDER BY count DESC
        ''').fetchall()

        # Recent activity (last 15)
        recent = conn.execute('''
            SELECT a.*, d.name as device_name
            FROM audit_log a
            LEFT JOIN devices d ON a.device_id = d.device_id
            ORDER BY a.timestamp DESC LIMIT 15
        ''').fetchall()

        return {
            'total': total,
            'available': available,
            'checked_out': checked_out,
            'lost': lost,
            'retired': retired,
            'by_category': [dict(r) for r in by_category],
            'by_connectivity': [dict(r) for r in by_connectivity],
            'recent_activity': [dict(r) for r in recent],
        }


# ---------------------------------------------------------------------------
# User authentication and management
# ---------------------------------------------------------------------------

def _hash_password(password, salt=None):
    """Hash a password with a salt. Returns (hash, salt) tuple."""
    if salt is None:
        salt = secrets.token_hex(16)
    pw_hash = hashlib.sha256((salt + password).encode()).hexdigest()
    return pw_hash, salt


def authenticate_user(username, password):
    """Verify username/password. Returns user dict on success, None on failure."""
    with db_transaction() as conn:
        row = conn.execute(
            'SELECT * FROM users WHERE username = ?', (username,)
        ).fetchone()
        if not row:
            return None
        expected_hash = hashlib.sha256((row['salt'] + password).encode()).hexdigest()
        if expected_hash != row['password_hash']:
            return None
        # Update last_login timestamp
        conn.execute(
            'UPDATE users SET last_login = CURRENT_TIMESTAMP WHERE user_id = ?',
            (row['user_id'],)
        )
        return dict(row)


def get_user(user_id):
    """Get a user by ID."""
    with db_transaction() as conn:
        row = conn.execute('SELECT * FROM users WHERE user_id = ?', (user_id,)).fetchone()
        return dict(row) if row else None


def get_user_by_username(username):
    """Get a user by username."""
    with db_transaction() as conn:
        row = conn.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        return dict(row) if row else None


def get_all_users():
    """Get all users ordered by username."""
    with db_transaction() as conn:
        rows = conn.execute(
            'SELECT user_id, username, role, display_name, created_at, last_login FROM users ORDER BY username'
        ).fetchall()
        return [dict(r) for r in rows]


def create_user(username, password, role='viewer', display_name=''):
    """Create a new user. Returns user_id. Raises ValueError if username taken."""
    pw_hash, salt = _hash_password(password)
    with db_transaction() as conn:
        try:
            conn.execute(
                'INSERT INTO users (username, password_hash, salt, role, display_name) VALUES (?, ?, ?, ?, ?)',
                (username, pw_hash, salt, role, display_name or username)
            )
            return conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        except sqlite3.IntegrityError:
            raise ValueError(f'Username "{username}" already exists')


def update_user(user_id, data):
    """Update user fields (display_name, role). Optionally update password."""
    with db_transaction() as conn:
        if 'password' in data and data['password']:
            pw_hash, salt = _hash_password(data['password'])
            conn.execute(
                'UPDATE users SET password_hash = ?, salt = ? WHERE user_id = ?',
                (pw_hash, salt, user_id)
            )
        if 'display_name' in data:
            conn.execute(
                'UPDATE users SET display_name = ? WHERE user_id = ?',
                (data['display_name'], user_id)
            )
        if 'role' in data:
            conn.execute(
                'UPDATE users SET role = ? WHERE user_id = ?',
                (data['role'], user_id)
            )


def delete_user(user_id):
    """Delete a user. Cannot delete the last admin."""
    with db_transaction() as conn:
        user = conn.execute('SELECT role FROM users WHERE user_id = ?', (user_id,)).fetchone()
        if not user:
            raise ValueError('User not found')
        if user['role'] == 'admin':
            admin_count = conn.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'").fetchone()[0]
            if admin_count <= 1:
                raise ValueError('Cannot delete the last admin user')
        conn.execute('DELETE FROM users WHERE user_id = ?', (user_id,))


# ---------------------------------------------------------------------------
# Database backup
# ---------------------------------------------------------------------------

import gzip as _gzip

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
BACKUP_CONFIG_FILE = os.path.join(REPO_DIR, 'backup_config.json')

# Default backup directory (used when no config exists)
_DEFAULT_BACKUP_DIR = os.path.join(REPO_DIR, 'backups')


def _load_backup_config():
    """Load backup configuration from disk."""
    try:
        with open(BACKUP_CONFIG_FILE, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _get_backup_config():
    """Return full backup config with defaults applied."""
    saved = _load_backup_config()
    return {
        'backup_dir': saved.get('backup_dir', _DEFAULT_BACKUP_DIR),
        'max_backups': saved.get('max_backups', 5),
        'backup_interval_hours': saved.get('backup_interval_hours', 24),
        'backup_enabled': saved.get('backup_enabled', False),
        'git_enabled': saved.get('git_enabled', False),
        'git_repo': saved.get('git_repo', ''),
        'git_branch': saved.get('git_branch', 'main'),
        'git_push_interval_hours': saved.get('git_push_interval_hours', 24),
        'last_git_push': saved.get('last_git_push', ''),
    }


def save_backup_config(config):
    """Persist backup configuration to disk."""
    with open(BACKUP_CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=2)


def _get_backup_dir():
    """Get the configured backup directory, creating it if needed."""
    config = _get_backup_config()
    backup_dir = config['backup_dir'] or _DEFAULT_BACKUP_DIR
    os.makedirs(backup_dir, exist_ok=True)
    return backup_dir


def backup_database(performed_by='system'):
    """
    Create a safe backup of the database using SQLite's online backup API.
    Prunes old backups beyond max_backups.
    Returns dict with backup metadata.
    """
    backup_dir = _get_backup_dir()
    config = _get_backup_config()

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_filename = f'inventory_backup_{timestamp}.db'
    backup_path = os.path.join(backup_dir, backup_filename)

    # Use SQLite online backup API for a consistent snapshot
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(backup_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()

    file_size = os.path.getsize(backup_path)

    # Prune old backups beyond max_backups
    pruned = _prune_old_backups(config['max_backups'])

    return {
        'filename': backup_filename,
        'path': backup_path,
        'size': file_size,
        'timestamp': timestamp,
        'pruned': pruned,
    }


def _prune_old_backups(max_backups):
    """Remove oldest backups beyond the max count. Returns number pruned."""
    backup_dir = _get_backup_dir()
    all_backups = sorted(
        [f for f in os.listdir(backup_dir) if f.startswith('inventory_backup_') and f.endswith('.db')],
        reverse=True,
    )
    pruned = 0
    for old_file in all_backups[max_backups:]:
        try:
            os.remove(os.path.join(backup_dir, old_file))
            pruned += 1
        except OSError:
            pass
    return pruned


def push_backups_to_git():
    """
    Compress all local backups into a single .tar.gz bundle and push to git.
    The bundle overwrites the previous one in the repo.
    Returns dict with push metadata.
    """
    import tarfile

    config = _get_backup_config()
    backup_dir = _get_backup_dir()
    git_repo = config.get('git_repo', '').strip()
    git_branch = config.get('git_branch', 'main').strip() or 'main'

    if not git_repo:
        raise ValueError('No git repository configured')

    # Create compressed tar bundle
    bundle_name = 'inventory_backups.tar.gz'
    bundle_path = os.path.join(backup_dir, bundle_name)

    backup_files = sorted(
        [f for f in os.listdir(backup_dir) if f.startswith('inventory_backup_') and f.endswith('.db')]
    )
    if not backup_files:
        raise ValueError('No backup files to push')

    with tarfile.open(bundle_path, 'w:gz') as tar:
        for bf in backup_files:
            tar.add(os.path.join(backup_dir, bf), arcname=bf)

    bundle_size = os.path.getsize(bundle_path)

    # Git operations: add, commit, push
    try:
        # Ensure the remote exists or add it
        subprocess.run(
            ['git', 'remote', 'set-url', 'backup-remote', git_repo],
            cwd=REPO_DIR, capture_output=True, timeout=15,
        )
        # If set-url fails because remote doesn't exist, add it
        result = subprocess.run(
            ['git', 'remote', 'get-url', 'backup-remote'],
            cwd=REPO_DIR, capture_output=True, timeout=15,
        )
        if result.returncode != 0:
            subprocess.run(
                ['git', 'remote', 'add', 'backup-remote', git_repo],
                cwd=REPO_DIR, capture_output=True, check=True, timeout=15,
            )

        subprocess.run(
            ['git', 'add', '-f', bundle_path],
            cwd=REPO_DIR, capture_output=True, check=True, timeout=30,
        )
        subprocess.run(
            ['git', 'commit', '-m', f'Database backup bundle {datetime.now().strftime("%Y-%m-%d %H:%M")}',
             '--', bundle_path],
            cwd=REPO_DIR, capture_output=True, check=True, timeout=30,
        )
        subprocess.run(
            ['git', 'push', '-u', 'backup-remote', f'HEAD:{git_branch}', '--force'],
            cwd=REPO_DIR, capture_output=True, check=True, timeout=120,
        )
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode() if e.stderr else str(e)
        raise RuntimeError(f'Git push failed: {stderr}')
    except subprocess.TimeoutExpired:
        raise RuntimeError('Git push timed out')

    # Update last push timestamp
    config['last_git_push'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    save_backup_config(config)

    return {
        'bundle_name': bundle_name,
        'bundle_size': bundle_size,
        'files_included': len(backup_files),
        'pushed_to': f'{git_repo} ({git_branch})',
    }


def list_backups():
    """List existing backup files, most recent first."""
    backup_dir = _get_backup_dir()
    backups = []
    for f in sorted(os.listdir(backup_dir), reverse=True):
        if f.startswith('inventory_backup_') and f.endswith('.db'):
            path = os.path.join(backup_dir, f)
            stat = os.stat(path)
            ts_part = f.replace('inventory_backup_', '').replace('.db', '')
            try:
                dt = datetime.strptime(ts_part, '%Y%m%d_%H%M%S')
                display_time = dt.strftime('%Y-%m-%d %H:%M:%S')
            except ValueError:
                display_time = ts_part
            backups.append({
                'filename': f,
                'size': stat.st_size,
                'timestamp': display_time,
            })
    return backups


def restore_database(filename):
    """
    Restore the database from a backup file using SQLite online backup API.
    Creates a safety backup of the current DB first.
    Returns dict with restore metadata.
    """
    backup_dir = _get_backup_dir()
    if not filename.startswith('inventory_backup_') or '..' in filename:
        raise ValueError('Invalid backup filename')
    backup_path = os.path.join(backup_dir, filename)
    if not os.path.isfile(backup_path):
        raise FileNotFoundError(f'Backup file not found: {filename}')

    # Validate the backup file is a valid SQLite database
    test_conn = sqlite3.connect(backup_path)
    try:
        test_conn.execute('SELECT COUNT(*) FROM devices')
        test_conn.execute('SELECT COUNT(*) FROM users')
    except sqlite3.DatabaseError as e:
        test_conn.close()
        raise ValueError(f'Backup file is not a valid database: {e}')
    finally:
        test_conn.close()

    # Create a safety backup of the current DB before overwriting
    safety_backup = backup_database(performed_by='pre-restore-safety')

    # Restore: copy backup over the live database using the backup API
    src = sqlite3.connect(backup_path)
    dst = sqlite3.connect(DB_PATH)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()

    return {
        'restored_from': filename,
        'safety_backup': safety_backup['filename'],
    }


def delete_backup(filename):
    """Delete a backup file. Returns True if deleted."""
    backup_dir = _get_backup_dir()
    if not filename.startswith('inventory_backup_') or '..' in filename:
        raise ValueError('Invalid backup filename')
    path = os.path.join(backup_dir, filename)
    if os.path.isfile(path):
        os.remove(path)
        return True
    return False
