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
import traceback
from contextlib import contextmanager
from datetime import datetime, timedelta
from runtime_dirs import DATA_DIR

# Path to the SQLite database file (writable data directory)
DB_PATH = os.path.join(DATA_DIR, 'inventory.db')

# Shared application logger — handler is configured by app.py
_audit_logger = logging.getLogger('inventory')

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
    'assigned_to', 'notes', 'codename', 'variant',
]


def get_connection():
    """Create a new SQLite connection with WAL mode and foreign keys enabled."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('PRAGMA busy_timeout=30000')
    conn.execute('PRAGMA synchronous=NORMAL')
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
    # Run integrity check on existing database
    if os.path.exists(DB_PATH):
        integrity = check_database_integrity()
        if integrity['ok']:
            _audit_logger.info('Database integrity check passed on startup')
        else:
            _audit_logger.error('DATABASE INTEGRITY CHECK FAILED: %s', integrity['result'])

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

        # Product reference table for printer specs
        conn.execute('''
            CREATE TABLE IF NOT EXISTS product_reference (
                ref_id INTEGER PRIMARY KEY AUTOINCREMENT,
                codename TEXT NOT NULL,
                model_name TEXT DEFAULT '',
                wifi_gen TEXT DEFAULT '',
                year TEXT DEFAULT '',
                chip_manufacturer TEXT DEFAULT '',
                chip_codename TEXT DEFAULT '',
                fw_codebase TEXT DEFAULT '',
                print_technology TEXT DEFAULT '',
                variant TEXT DEFAULT '',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_prodref_codename ON product_reference(codename)')

        # Migrate: add new columns if upgrading from old schema
        pr_cols = [row[1] for row in conn.execute('PRAGMA table_info(product_reference)').fetchall()]
        for col, default in [('model_name', ''), ('wifi_gen', ''), ('chip_manufacturer', ''),
                             ('chip_codename', ''), ('fw_codebase', ''), ('print_technology', ''),
                             ('variant', '')]:
            if col not in pr_cols:
                conn.execute(f"ALTER TABLE product_reference ADD COLUMN {col} TEXT DEFAULT ''")

        # Indexes for common queries
        conn.execute('CREATE INDEX IF NOT EXISTS idx_devices_status ON devices(status)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_devices_category ON devices(category)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_devices_barcode ON devices(barcode_value)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_audit_device ON audit_log(device_id)')

        # Migrate: add sort_order column if missing (existing databases)
        cols = [row[1] for row in conn.execute('PRAGMA table_info(categories)').fetchall()]
        if 'sort_order' not in cols:
            conn.execute('ALTER TABLE categories ADD COLUMN sort_order INTEGER DEFAULT 99')

        # Migrate: add codename column to devices if missing
        device_cols = [row[1] for row in conn.execute('PRAGMA table_info(devices)').fetchall()]
        if 'codename' not in device_cols:
            conn.execute("ALTER TABLE devices ADD COLUMN codename TEXT DEFAULT ''")
        if 'variant' not in device_cols:
            conn.execute("ALTER TABLE devices ADD COLUMN variant TEXT DEFAULT ''")

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
            location, assigned_to, notes, codename, variant)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        data.get('codename', ''),
        data.get('variant', ''),
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


def search_devices(query='', category='', status='', connectivity='', location='', codename=''):
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

        if codename:
            conditions.append("codename = ?")
            params.append(codename)

        if query:
            conditions.append("""
                (name LIKE ? OR category LIKE ? OR manufacturer LIKE ?
                 OR model_number LIKE ? OR serial_number LIKE ?
                 OR connectivity LIKE ? OR barcode_value LIKE ?
                 OR status LIKE ? OR location LIKE ?
                 OR assigned_to LIKE ? OR notes LIKE ?
                 OR codename LIKE ?)
            """)
            like_q = f"%{query}%"
            params.extend([like_q] * 12)

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


def get_distinct_values(column):
    """Return sorted list of distinct non-empty values for a device column."""
    allowed = {'connectivity', 'manufacturer', 'location', 'assigned_to'}
    if column not in allowed:
        return []
    with db_transaction() as conn:
        rows = conn.execute(
            f"SELECT DISTINCT {column} FROM devices WHERE {column} != '' AND {column} IS NOT NULL AND status != 'retired' ORDER BY {column}"
        ).fetchall()
        return [r[0] for r in rows]


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

REPO_DIR = DATA_DIR
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
        'prune_enabled': bool(saved.get('prune_enabled', False)),
        'prune_interval_hours': saved.get('prune_interval_hours', 24),
        'backup_enabled': bool(saved.get('backup_enabled', False)),
        'git_enabled': bool(saved.get('git_enabled', False)),
        'git_repo': saved.get('git_repo', ''),
        'git_branch': saved.get('git_branch', 'backups'),
        'git_token': saved.get('git_token', ''),
        'git_push_interval_hours': saved.get('git_push_interval_hours', 24),
        'last_git_push': saved.get('last_git_push', ''),
        'last_backup': saved.get('last_backup', ''),
        'last_backup_hash': saved.get('last_backup_hash', ''),
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


def _compute_db_hash(skip_checkpoint=False):
    """Compute a SHA-256 hash of the database content for change detection."""
    if not skip_checkpoint:
        checkpoint_wal()
    h = hashlib.sha256()
    try:
        with open(DB_PATH, 'rb') as f:
            for chunk in iter(lambda: f.read(65536), b''):
                h.update(chunk)
        return h.hexdigest()
    except FileNotFoundError:
        return ''
    except OSError as e:
        _audit_logger.error('Failed to compute database hash: %s', e)
        return ''


def backup_database(performed_by='system', manual=False):
    """
    Create a safe backup of the database using SQLite's online backup API.
    Automated backups are prefixed 'auto_backup_' and pruned to max_backups.
    Manual backups are prefixed 'manual_backup_' and never auto-pruned.
    Skips automated backups if the database hasn't changed since the last one.
    Returns dict with backup metadata (includes 'skipped' key).
    """
    import time as _time
    start_time = _time.monotonic()

    backup_dir = _get_backup_dir()
    config = _get_backup_config()

    # Skip-if-unchanged for automated backups (manual backups always proceed)
    if not manual:
        current_hash = _compute_db_hash()
        last_hash = config.get('last_backup_hash', '')
        if current_hash and current_hash == last_hash:
            _audit_logger.info('Scheduled backup skipped — database unchanged since last backup')
            config['last_backup'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            save_backup_config(config)
            return {
                'filename': None,
                'path': None,
                'size': 0,
                'timestamp': datetime.now().strftime('%Y%m%d_%H%M%S'),
                'pruned': 0,
                'skipped': True,
            }

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    if manual:
        backup_filename = f'manual_backup_{timestamp}.db'
    else:
        backup_filename = f'auto_backup_{timestamp}.db'
    backup_path = os.path.join(backup_dir, backup_filename)

    # Checkpoint WAL before backup to ensure all data is in the main file
    wal_result = checkpoint_wal()
    if not wal_result['success']:
        _audit_logger.warning('WAL checkpoint before backup returned error: %s', wal_result.get('error'))

    # Use SQLite online backup API for a consistent snapshot
    src = None
    dst = None
    try:
        src = sqlite3.connect(DB_PATH)
        dst = sqlite3.connect(backup_path)
        src.backup(dst)
    except Exception as e:
        # Clean up partial backup file on failure
        _audit_logger.error('Backup API failed for %s: %s\n%s', backup_filename, e, traceback.format_exc())
        if dst:
            dst.close()
            dst = None
        if src:
            src.close()
            src = None
        try:
            if os.path.exists(backup_path):
                os.remove(backup_path)
                _audit_logger.info('Cleaned up partial backup file: %s', backup_filename)
        except OSError:
            pass
        raise
    finally:
        if dst:
            dst.close()
        if src:
            src.close()

    # Verify the backup is a valid SQLite database
    backup_valid = False
    try:
        verify_conn = sqlite3.connect(backup_path)
        result = verify_conn.execute('PRAGMA integrity_check').fetchone()[0]
        row_count = verify_conn.execute('SELECT COUNT(*) FROM devices').fetchone()[0]
        verify_conn.close()
        if result != 'ok':
            _audit_logger.error('Backup integrity check FAILED: %s — %s', backup_filename, result)
        else:
            backup_valid = True
    except Exception as e:
        _audit_logger.error('Backup post-write verification error: %s — %s\n%s',
                            backup_filename, e, traceback.format_exc())

    if not backup_valid:
        # Remove invalid backup file
        try:
            os.remove(backup_path)
            _audit_logger.warning('Removed invalid backup file: %s', backup_filename)
        except OSError:
            pass
        raise RuntimeError(f'Backup verification failed for {backup_filename}')

    file_size = os.path.getsize(backup_path)

    # Smart prune: keep at least 1 backup per day for 7 days, then apply max_backups
    pruned = _smart_prune_backups(config['max_backups'])

    # Record last successful backup time and hash for skip-if-unchanged
    # skip_checkpoint=True since we already checkpointed above
    config['last_backup'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    config['last_backup_hash'] = _compute_db_hash(skip_checkpoint=True)
    save_backup_config(config)

    elapsed_ms = round((_time.monotonic() - start_time) * 1000)
    _audit_logger.info('Backup completed: %s (%d bytes, %d devices, pruned=%d, %dms) by=%s',
                       backup_filename, file_size, row_count, pruned, elapsed_ms, performed_by)

    return {
        'filename': backup_filename,
        'path': backup_path,
        'size': file_size,
        'timestamp': timestamp,
        'pruned': pruned,
        'skipped': False,
    }


def get_backup_health():
    """Check if backups and git pushes are on schedule. Returns health status."""
    config = _get_backup_config()
    now = datetime.now()
    issues = []

    # Check backup schedule
    if config['backup_enabled']:
        last = config.get('last_backup', '')
        if not last:
            issues.append('Auto-backup is enabled but no backup has been completed yet')
        else:
            last_dt = datetime.strptime(last, '%Y-%m-%d %H:%M:%S')
            overdue_hours = config['backup_interval_hours'] * 2
            if (now - last_dt).total_seconds() > overdue_hours * 3600:
                hours_ago = round((now - last_dt).total_seconds() / 3600, 1)
                issues.append(f'Backup overdue: last backup was {hours_ago} hours ago (interval: {config["backup_interval_hours"]}h)')

    # Check cloud backup (git push) schedule
    if config['git_enabled'] and config.get('git_repo'):
        last = config.get('last_git_push', '')
        if not last:
            issues.append('Cloud backup is enabled but no cloud backup has been completed yet')
        else:
            last_dt = datetime.strptime(last, '%Y-%m-%d %H:%M:%S')
            overdue_hours = config['git_push_interval_hours'] * 2
            if (now - last_dt).total_seconds() > overdue_hours * 3600:
                hours_ago = round((now - last_dt).total_seconds() / 3600, 1)
                issues.append(f'Cloud backup overdue: last cloud backup was {hours_ago} hours ago (interval: {config["git_push_interval_hours"]}h)')

    # Check backup directory has files
    backup_dir = _get_backup_dir()
    backup_files = [f for f in os.listdir(backup_dir) if _is_backup_file(f)]

    return {
        'healthy': len(issues) == 0,
        'issues': issues,
        'last_backup': config.get('last_backup', ''),
        'last_git_push': config.get('last_git_push', ''),
        'backup_enabled': config['backup_enabled'],
        'git_enabled': config['git_enabled'],
        'backup_count': len(backup_files),
        'db_size': os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0,
    }


def check_database_integrity():
    """Run SQLite integrity check and return results."""
    try:
        conn = get_connection()
        result = conn.execute('PRAGMA integrity_check').fetchone()[0]
        conn.close()
        return {'ok': result == 'ok', 'result': result}
    except Exception as e:
        return {'ok': False, 'result': str(e)}


def checkpoint_wal():
    """Force a WAL checkpoint to ensure all data is written to the main database file.
    Should be called before backups for maximum data consistency."""
    try:
        conn = get_connection()
        # TRUNCATE mode: checkpoint and truncate WAL file to zero size
        result = conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        conn.close()
        # result is (busy, log, checkpointed)
        return {'success': True, 'busy': result[0], 'log_pages': result[1], 'checkpointed': result[2]}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def verify_latest_backup():
    """
    Verify the most recent backup file is still a valid, intact SQLite database.
    Returns dict with verification results.
    """
    backup_dir = _get_backup_dir()
    all_backups = sorted(
        [f for f in os.listdir(backup_dir) if _is_backup_file(f)],
        reverse=True,
    )
    if not all_backups:
        return {'ok': False, 'result': 'No backup files found', 'filename': None}

    latest = all_backups[0]
    latest_path = os.path.join(backup_dir, latest)
    try:
        conn = sqlite3.connect(latest_path)
        result = conn.execute('PRAGMA integrity_check').fetchone()[0]
        conn.execute('SELECT COUNT(*) FROM devices')
        conn.close()
        ok = result == 'ok'
        if not ok:
            _audit_logger.warning('Backup verification failed: %s — %s', latest, result)
        return {'ok': ok, 'result': result, 'filename': latest}
    except Exception as e:
        _audit_logger.error('Backup verification error: %s — %s', latest, e)
        return {'ok': False, 'result': str(e), 'filename': latest}


def startup_integrity_check():
    """
    Run on application startup to verify database health.
    Returns dict with check results, logs warnings if issues found.
    """
    result = check_database_integrity()
    if result['ok']:
        _audit_logger.info('Startup integrity check: database OK')
    else:
        _audit_logger.error('STARTUP INTEGRITY CHECK FAILED: %s — '
                            'database may be corrupt, consider restoring from backup',
                            result['result'])
    return result


def get_database_status():
    """Get comprehensive database status for monitoring."""
    status = {
        'exists': os.path.exists(DB_PATH),
        'size_bytes': 0,
        'wal_size_bytes': 0,
        'integrity': 'unknown',
        'table_counts': {},
    }
    if not status['exists']:
        return status

    status['size_bytes'] = os.path.getsize(DB_PATH)

    wal_path = DB_PATH + '-wal'
    if os.path.exists(wal_path):
        status['wal_size_bytes'] = os.path.getsize(wal_path)

    # Integrity check
    integrity = check_database_integrity()
    status['integrity'] = 'ok' if integrity['ok'] else integrity['result']

    # Row counts
    try:
        conn = get_connection()
        for table in ['devices', 'audit_log', 'users', 'categories']:
            count = conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
            status['table_counts'][table] = count
        conn.close()
    except Exception as e:
        status['table_counts'] = {'error': str(e)}

    return status


def _prune_old_backups(max_backups):
    """Simple prune: remove oldest automated backups beyond max count."""
    backup_dir = _get_backup_dir()
    auto_backups = sorted(
        [f for f in os.listdir(backup_dir) if f.startswith('auto_backup_') and f.endswith('.db')],
        reverse=True,
    )
    pruned = 0
    for old_file in auto_backups[max_backups:]:
        try:
            os.remove(os.path.join(backup_dir, old_file))
            pruned += 1
        except OSError:
            pass
    return pruned


def _smart_prune_backups(max_backups):
    """
    Smart retention: keep at least 1 backup per day for the last 7 days,
    then apply max_backups to the remainder. Manual backups are never pruned.
    """
    backup_dir = _get_backup_dir()
    auto_backups = sorted(
        [f for f in os.listdir(backup_dir) if f.startswith('auto_backup_') and f.endswith('.db')],
        reverse=True,  # newest first
    )
    if len(auto_backups) <= max_backups:
        return 0

    now = datetime.now()
    cutoff = now - timedelta(days=7)
    protected = set()  # filenames to keep (one per day for 7 days)
    days_seen = set()

    for f in auto_backups:
        try:
            ts_part = f.replace('auto_backup_', '').replace('.db', '').split('_uploaded')[0]
            dt = datetime.strptime(ts_part, '%Y%m%d_%H%M%S')
        except ValueError:
            continue
        if dt >= cutoff:
            day_key = dt.strftime('%Y%m%d')
            if day_key not in days_seen:
                days_seen.add(day_key)
                protected.add(f)

    # Always protect the newest max_backups as well
    for f in auto_backups[:max_backups]:
        protected.add(f)

    # Prune anything not protected
    pruned = 0
    for f in auto_backups:
        if f not in protected:
            try:
                os.remove(os.path.join(backup_dir, f))
                pruned += 1
            except OSError as e:
                _audit_logger.warning('Failed to prune backup %s: %s', f, e)
    if pruned:
        _audit_logger.info('Smart prune: removed %d auto-backups, kept %d (protected %d daily + %d newest)',
                           pruned, len(auto_backups) - pruned, len(days_seen), min(max_backups, len(auto_backups)))
    return pruned


def push_backups_to_git():
    """
    Zip all local .db backup files into a single archive and push to a
    dedicated git branch. Uses incremental commits (not force-push) so
    git history preserves multiple recovery points.
    Returns dict with push metadata.
    """
    import tempfile
    import time as _time
    import zipfile

    start_time = _time.monotonic()
    config = _get_backup_config()
    backup_dir = _get_backup_dir()
    git_branch = config.get('git_branch', 'backups').strip() or 'backups'

    # Collect all backup files (auto + manual)
    backup_files = sorted(
        [f for f in os.listdir(backup_dir) if _is_backup_file(f)],
        reverse=True,
    )
    _audit_logger.info('Git push: backup_dir=%s, found %d .db files to zip',
                       backup_dir, len(backup_files))
    if not backup_files:
        raise ValueError('No backup files to push')

    git_env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0'}
    remote_url = _get_git_push_url()

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            # Try to clone existing branch to preserve history
            clone_result = subprocess.run(
                ['git', 'clone', '--depth', '10', '--branch', git_branch,
                 '--single-branch', remote_url, tmpdir],
                capture_output=True, timeout=60, env=git_env,
            )
            if clone_result.returncode != 0:
                # Branch doesn't exist yet — init fresh
                subprocess.run(['git', 'init'], cwd=tmpdir, capture_output=True,
                               check=True, timeout=15, env=git_env)
                subprocess.run(['git', 'checkout', '--orphan', git_branch],
                               cwd=tmpdir, capture_output=True, check=True,
                               timeout=15, env=git_env)

            # Set commit identity
            subprocess.run(['git', 'config', 'user.email', 'inventory@local'],
                           cwd=tmpdir, capture_output=True, check=True, timeout=5, env=git_env)
            subprocess.run(['git', 'config', 'user.name', 'Inventory System'],
                           cwd=tmpdir, capture_output=True, check=True, timeout=5, env=git_env)

            # Create/update zip archive
            zip_name = 'inventory_backups.zip'
            zip_path = os.path.join(tmpdir, zip_name)
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                for bf in backup_files:
                    zf.write(os.path.join(backup_dir, bf), bf)
            zip_size = os.path.getsize(zip_path)

            subprocess.run(['git', 'add', zip_name],
                           cwd=tmpdir, capture_output=True, check=True, timeout=30, env=git_env)

            # Check if there are actual changes to commit
            diff_result = subprocess.run(
                ['git', 'diff', '--cached', '--quiet'],
                cwd=tmpdir, capture_output=True, timeout=15, env=git_env,
            )
            if diff_result.returncode == 0:
                _audit_logger.info('Git push skipped — backup zip unchanged')
                config['last_git_push'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                save_backup_config(config)
                push_target = git_repo or 'origin'
                return {
                    'files_pushed': len(backup_files),
                    'zip_size': zip_size,
                    'pushed_to': f'{push_target} ({git_branch})',
                    'skipped': True,
                }

            commit_msg = (f'Backup {datetime.now().strftime("%Y-%m-%d %H:%M")} '
                          f'({len(backup_files)} files, {zip_size // 1024}KB)')
            subprocess.run(
                ['git', 'commit', '-m', commit_msg],
                cwd=tmpdir, capture_output=True, check=True, timeout=30, env=git_env,
            )

            # Regular push (not --force) to preserve commit history
            subprocess.run(
                ['git', 'push', remote_url, f'{git_branch}:{git_branch}'],
                cwd=tmpdir, capture_output=True, check=True, timeout=120, env=git_env,
            )

        except subprocess.CalledProcessError as e:
            stderr = e.stderr.decode() if e.stderr else str(e)
            _audit_logger.error('Git push subprocess failed: %s', stderr)
            raise RuntimeError(f'Git push failed: {stderr}')
        except subprocess.TimeoutExpired:
            _audit_logger.error('Git push timed out after 120s')
            raise RuntimeError('Git push timed out')

    config['last_git_push'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    save_backup_config(config)

    elapsed_ms = round((_time.monotonic() - start_time) * 1000)
    git_repo = config.get('git_repo', '').strip()
    push_target = git_repo or 'origin'
    _audit_logger.info('Git push completed: %d files (%dKB zip) to %s (%s) in %dms',
                       len(backup_files), zip_size // 1024, push_target, git_branch, elapsed_ms)
    return {
        'files_pushed': len(backup_files),
        'zip_size': zip_size,
        'pushed_to': f'{push_target} ({git_branch})',
        'skipped': False,
    }


def _is_backup_file(filename):
    """Check if a filename is a recognized backup file."""
    return (filename.endswith('.db') and
            (filename.startswith('auto_backup_') or
             filename.startswith('manual_backup_') or
             filename.startswith('inventory_backup_')))  # legacy support


def _parse_backup_timestamp(filename):
    """Extract display timestamp from a backup filename."""
    ts_part = filename.replace('.db', '')
    for prefix in ('auto_backup_', 'manual_backup_', 'inventory_backup_'):
        ts_part = ts_part.replace(prefix, '')
    # Strip _uploaded suffix from uploaded files
    ts_part = ts_part.split('_uploaded')[0]
    try:
        dt = datetime.strptime(ts_part, '%Y%m%d_%H%M%S')
        return dt.strftime('%Y-%m-%d %H:%M:%S')
    except ValueError:
        return ts_part


def list_backups():
    """List existing backup files, most recent first."""
    backup_dir = _get_backup_dir()
    backups = []
    for f in sorted(os.listdir(backup_dir), reverse=True):
        if _is_backup_file(f):
            path = os.path.join(backup_dir, f)
            stat = os.stat(path)
            backup_type = 'manual' if f.startswith('manual_backup_') else 'auto'
            backups.append({
                'filename': f,
                'size': stat.st_size,
                'timestamp': _parse_backup_timestamp(f),
                'type': backup_type,
            })
    return backups


def restore_database(filename):
    """
    Restore the database from a backup file using SQLite online backup API.
    Checkpoints WAL first, creates a safety backup, validates, then restores.
    Rolls back to safety backup if restore fails.
    Returns dict with restore metadata.
    """
    import time as _time
    start_time = _time.monotonic()

    backup_dir = _get_backup_dir()
    if not _is_backup_file(filename) or '..' in filename:
        raise ValueError('Invalid backup filename')
    backup_path = os.path.join(backup_dir, filename)
    if not os.path.isfile(backup_path):
        raise FileNotFoundError(f'Backup file not found: {filename}')

    # Validate the backup file is a valid SQLite database with full integrity check
    test_conn = sqlite3.connect(backup_path)
    try:
        integrity = test_conn.execute('PRAGMA integrity_check').fetchone()[0]
        if integrity != 'ok':
            raise ValueError(f'Backup file failed integrity check: {integrity}')
        device_count = test_conn.execute('SELECT COUNT(*) FROM devices').fetchone()[0]
        user_count = test_conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]
        _audit_logger.info('Restore source validated: %s (integrity=ok, %d devices, %d users)',
                           filename, device_count, user_count)
    except sqlite3.DatabaseError as e:
        raise ValueError(f'Backup file is not a valid database: {e}')
    finally:
        test_conn.close()

    # Checkpoint WAL before restore to flush any pending writes
    checkpoint_wal()

    # Create a safety backup of the current DB before overwriting
    safety_backup = backup_database(performed_by='pre-restore-safety', manual=True)

    # Restore: copy backup over the live database using the backup API
    try:
        src = sqlite3.connect(backup_path)
        dst = sqlite3.connect(DB_PATH)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()

        # Verify restored database integrity
        verify_conn = sqlite3.connect(DB_PATH)
        try:
            post_integrity = verify_conn.execute('PRAGMA integrity_check').fetchone()[0]
            if post_integrity != 'ok':
                raise RuntimeError(f'Post-restore integrity check failed: {post_integrity}')
        finally:
            verify_conn.close()

    except Exception as e:
        # Rollback: restore from safety backup
        _audit_logger.error('Restore from %s failed, rolling back to safety backup %s: %s\n%s',
                            filename, safety_backup['filename'], e, traceback.format_exc())
        try:
            safety_path = os.path.join(backup_dir, safety_backup['filename'])
            rollback_src = sqlite3.connect(safety_path)
            rollback_dst = sqlite3.connect(DB_PATH)
            try:
                rollback_src.backup(rollback_dst)
            finally:
                rollback_dst.close()
                rollback_src.close()
            _audit_logger.info('Rollback to safety backup %s succeeded', safety_backup['filename'])
        except Exception as rollback_err:
            _audit_logger.critical('ROLLBACK FAILED after restore failure: %s — database may be corrupt',
                                   rollback_err)
        raise

    # Re-run init_db to apply any migrations the restored DB may be missing
    init_db()

    # Update hash so next scheduled backup detects the restored content
    config = _get_backup_config()
    config['last_backup_hash'] = _compute_db_hash()
    save_backup_config(config)

    elapsed_ms = round((_time.monotonic() - start_time) * 1000)
    _audit_logger.info('Database restored from %s (safety=%s, %dms)',
                       filename, safety_backup['filename'], elapsed_ms)

    return {
        'restored_from': filename,
        'safety_backup': safety_backup['filename'],
    }


def delete_backup(filename):
    """Delete a backup file. Returns True if deleted."""
    backup_dir = _get_backup_dir()
    if not _is_backup_file(filename) or '..' in filename:
        raise ValueError('Invalid backup filename')
    path = os.path.join(backup_dir, filename)
    if os.path.isfile(path):
        os.remove(path)
        return True
    return False


def _get_git_push_url():
    """Build the authenticated URL for git operations."""
    config = _get_backup_config()
    git_repo = config.get('git_repo', '').strip()
    git_token = config.get('git_token', '').strip()
    git_env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0'}

    if git_repo:
        remote_url = git_repo
    else:
        result = subprocess.run(
            ['git', 'remote', 'get-url', 'origin'],
            cwd=REPO_DIR, capture_output=True, check=True, timeout=15, env=git_env,
        )
        remote_url = result.stdout.decode().strip()

    if git_token and remote_url.startswith('git@github.com:'):
        path = remote_url.replace('git@github.com:', '')
        remote_url = f'https://github.com/{path}'

    if git_token and remote_url.startswith('https://'):
        remote_url = remote_url.replace('https://', f'https://{git_token}@', 1)

    return remote_url


def list_git_backups():
    """
    Fetch the backup zip from git and list the .db files inside it.
    Returns list of dicts with filename and size info.
    """
    import tempfile
    import zipfile

    config = _get_backup_config()
    git_branch = config.get('git_branch', 'backups').strip() or 'backups'
    remote_url = _get_git_push_url()
    git_env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0'}

    with tempfile.TemporaryDirectory() as tmpdir:
        # Shallow clone just the backup branch
        result = subprocess.run(
            ['git', 'clone', '--depth', '1', '--branch', git_branch,
             '--single-branch', remote_url, tmpdir],
            capture_output=True, timeout=60, env=git_env,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode() if result.stderr else ''
            if 'not found' in stderr.lower() or 'could not find' in stderr.lower():
                raise ValueError(f'Branch "{git_branch}" not found on remote. Push backups first.')
            raise RuntimeError(f'Git clone failed: {stderr}')

        zip_path = os.path.join(tmpdir, 'inventory_backups.zip')
        if not os.path.isfile(zip_path):
            raise ValueError('No inventory_backups.zip found on the git branch.')

        entries = []
        with zipfile.ZipFile(zip_path, 'r') as zf:
            for info in zf.infolist():
                if _is_backup_file(info.filename):
                    backup_type = 'manual' if info.filename.startswith('manual_backup_') else 'auto'
                    entries.append({
                        'filename': info.filename,
                        'size': info.file_size,
                        'timestamp': _parse_backup_timestamp(info.filename),
                        'type': backup_type,
                    })

        # Sort most recent first
        entries.sort(key=lambda e: e['filename'], reverse=True)
        return entries


def restore_from_git(filename):
    """
    Extract a specific .db file from the git backup zip and restore it.
    Creates a safety backup first. Returns restore metadata.
    """
    import tempfile
    import zipfile

    if not _is_backup_file(filename) or '..' in filename:
        raise ValueError('Invalid backup filename')

    config = _get_backup_config()
    git_branch = config.get('git_branch', 'backups').strip() or 'backups'
    remote_url = _get_git_push_url()
    git_env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0'}

    with tempfile.TemporaryDirectory() as tmpdir:
        # Clone the backup branch
        result = subprocess.run(
            ['git', 'clone', '--depth', '1', '--branch', git_branch,
             '--single-branch', remote_url, tmpdir],
            capture_output=True, timeout=60, env=git_env,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode() if result.stderr else ''
            raise RuntimeError(f'Git clone failed: {stderr}')

        zip_path = os.path.join(tmpdir, 'inventory_backups.zip')
        if not os.path.isfile(zip_path):
            raise ValueError('No inventory_backups.zip found on the git branch.')

        # Extract the requested file
        with zipfile.ZipFile(zip_path, 'r') as zf:
            if filename not in zf.namelist():
                raise ValueError(f'File "{filename}" not found in backup zip.')
            zf.extract(filename, tmpdir)

        extracted_path = os.path.join(tmpdir, filename)

        # Validate it's a real SQLite database with full integrity check
        test_conn = sqlite3.connect(extracted_path)
        try:
            integrity = test_conn.execute('PRAGMA integrity_check').fetchone()[0]
            if integrity != 'ok':
                raise ValueError(f'Git backup file failed integrity check: {integrity}')
            device_count = test_conn.execute('SELECT COUNT(*) FROM devices').fetchone()[0]
            user_count = test_conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]
            _audit_logger.info('Git restore source validated: %s (integrity=ok, %d devices, %d users)',
                               filename, device_count, user_count)
        except sqlite3.DatabaseError as e:
            raise ValueError(f'File is not a valid database: {e}')
        finally:
            test_conn.close()

        # Checkpoint WAL before restore to flush pending writes
        checkpoint_wal()

        # Safety backup before restore
        safety = backup_database(performed_by='pre-git-restore-safety', manual=True)

        # Restore using backup API with rollback on failure
        try:
            src = sqlite3.connect(extracted_path)
            dst = sqlite3.connect(DB_PATH)
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()

            # Verify restored database integrity
            verify_conn = sqlite3.connect(DB_PATH)
            try:
                post_integrity = verify_conn.execute('PRAGMA integrity_check').fetchone()[0]
                if post_integrity != 'ok':
                    raise RuntimeError(f'Post-restore integrity check failed: {post_integrity}')
            finally:
                verify_conn.close()

        except Exception as e:
            # Rollback to safety backup
            _audit_logger.error('Git restore from %s failed, rolling back: %s\n%s',
                                filename, e, traceback.format_exc())
            try:
                safety_path = os.path.join(_get_backup_dir(), safety['filename'])
                rb_src = sqlite3.connect(safety_path)
                rb_dst = sqlite3.connect(DB_PATH)
                try:
                    rb_src.backup(rb_dst)
                finally:
                    rb_dst.close()
                    rb_src.close()
                _audit_logger.info('Rollback to safety backup %s succeeded', safety['filename'])
            except Exception as rb_err:
                _audit_logger.critical('ROLLBACK FAILED after git restore failure: %s', rb_err)
            raise

        init_db()

        # Update hash so next scheduled backup detects the restored content
        cfg = _get_backup_config()
        cfg['last_backup_hash'] = _compute_db_hash()
        save_backup_config(cfg)

        _audit_logger.info('Database restored from git:%s (safety=%s)', filename, safety['filename'])

        return {
            'restored_from': f'git:{filename}',
            'safety_backup': safety['filename'],
        }


# ---------------------------------------------------------------------------
# Product Reference (printer spec catalog)
# ---------------------------------------------------------------------------


def get_all_product_references(search=''):
    """Return all product reference entries, optionally filtered."""
    conn = get_connection()
    try:
        if search:
            like = f'%{search}%'
            rows = conn.execute('''
                SELECT * FROM product_reference
                WHERE codename LIKE ? OR model_name LIKE ? OR year LIKE ?
                    OR chip_manufacturer LIKE ? OR chip_codename LIKE ? OR wifi_gen LIKE ?
                ORDER BY year DESC, codename ASC
            ''', (like, like, like, like, like, like)).fetchall()
        else:
            rows = conn.execute(
                'SELECT * FROM product_reference ORDER BY year DESC, codename ASC'
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_inventory_counts_by_codename():
    """Return dict of codename -> {total, available, checked_out} from devices table."""
    conn = get_connection()
    try:
        rows = conn.execute('''
            SELECT codename, status, COUNT(*) as cnt
            FROM devices
            WHERE codename != '' AND codename IS NOT NULL AND status != 'retired'
            GROUP BY codename, status
        ''').fetchall()
        counts = {}
        for r in rows:
            cn = r['codename']
            if cn not in counts:
                counts[cn] = {'total': 0, 'available': 0, 'checked_out': 0}
            counts[cn]['total'] += r['cnt']
            if r['status'] == 'available':
                counts[cn]['available'] = r['cnt']
            elif r['status'] == 'checked_out':
                counts[cn]['checked_out'] = r['cnt']
        return counts
    finally:
        conn.close()


def get_product_reference(ref_id):
    """Return a single product reference by ID."""
    conn = get_connection()
    try:
        row = conn.execute('SELECT * FROM product_reference WHERE ref_id = ?', (ref_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_product_reference_by_codename(codename):
    """Return product reference(s) matching a codename."""
    conn = get_connection()
    try:
        rows = conn.execute(
            'SELECT * FROM product_reference WHERE codename = ? ORDER BY year DESC',
            (codename,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def add_product_reference(codename, model_name='', wifi_gen='', year='',
                          chip_manufacturer='', chip_codename='', fw_codebase='',
                          print_technology='', variant=''):
    """Add a single product reference entry."""
    with db_transaction() as conn:
        conn.execute('''
            INSERT INTO product_reference
                (codename, model_name, wifi_gen, year, chip_manufacturer, chip_codename, fw_codebase, print_technology, variant)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (codename, model_name, wifi_gen, year, chip_manufacturer, chip_codename, fw_codebase, print_technology, variant))


def update_product_reference(ref_id, codename, model_name='', wifi_gen='', year='',
                             chip_manufacturer='', chip_codename='', fw_codebase='',
                             print_technology='', variant=''):
    """Update an existing product reference entry."""
    with db_transaction() as conn:
        conn.execute('''
            UPDATE product_reference
            SET codename = ?, model_name = ?, wifi_gen = ?, year = ?,
                chip_manufacturer = ?, chip_codename = ?, fw_codebase = ?,
                print_technology = ?, variant = ?, updated_at = CURRENT_TIMESTAMP
            WHERE ref_id = ?
        ''', (codename, model_name, wifi_gen, year, chip_manufacturer, chip_codename, fw_codebase, print_technology, variant, ref_id))


def delete_product_reference(ref_id):
    """Delete a product reference entry."""
    with db_transaction() as conn:
        conn.execute('DELETE FROM product_reference WHERE ref_id = ?', (ref_id,))


def clear_all_product_references():
    """Delete all product reference entries."""
    with db_transaction() as conn:
        conn.execute('DELETE FROM product_reference')
