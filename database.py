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
from runtime_dirs import BUNDLE_DIR, DATA_DIR

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

# Current schema version — increment when making breaking schema changes
SCHEMA_VERSION = 2

# Tables required for a valid inventory database (used during restore validation)
REQUIRED_TABLES = {'devices', 'users'}
EXPECTED_TABLES = {'devices', 'audit_log', 'categories', 'users', 'product_reference',
                   'product_wiki', 'wiki_attachments', 'device_notes', 'schema_info'}

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
                role TEXT NOT NULL DEFAULT 'custom'
                    CHECK(role IN ('admin','custom')),
                permissions TEXT DEFAULT NULL,
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

        # Product wiki table — community notes per product
        conn.execute('''
            CREATE TABLE IF NOT EXISTS product_wiki (
                wiki_id INTEGER PRIMARY KEY AUTOINCREMENT,
                ref_id INTEGER NOT NULL,
                content TEXT DEFAULT '',
                updated_by TEXT DEFAULT '',
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (ref_id) REFERENCES product_reference(ref_id)
            )
        ''')
        conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_wiki_ref ON product_wiki(ref_id)')

        # Wiki attachments table
        conn.execute('''
            CREATE TABLE IF NOT EXISTS wiki_attachments (
                attachment_id INTEGER PRIMARY KEY AUTOINCREMENT,
                ref_id INTEGER NOT NULL,
                filename TEXT NOT NULL,
                original_name TEXT NOT NULL,
                content_type TEXT DEFAULT '',
                size_bytes INTEGER DEFAULT 0,
                uploaded_by TEXT DEFAULT '',
                uploaded_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (ref_id) REFERENCES product_reference(ref_id)
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_wiki_attach_ref ON wiki_attachments(ref_id)')

        # Device notes table — anyone can add notes to a device
        conn.execute('''
            CREATE TABLE IF NOT EXISTS device_notes (
                note_id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                author TEXT NOT NULL DEFAULT 'Anonymous',
                content TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (device_id) REFERENCES devices(device_id)
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_device_notes_device ON device_notes(device_id)')

        # Schema version tracking table
        conn.execute('''
            CREATE TABLE IF NOT EXISTS schema_info (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')

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

        # Migrate: expand user role CHECK constraint to include 'editor'
        # SQLite can't ALTER CHECK constraints, so rebuild the table
        role_check = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
        ).fetchone()

        # Migration: consolidate all non-admin roles into 'custom' with per-user permissions
        needs_custom_migration = role_check and 'custom' not in role_check[0]
        if needs_custom_migration:
            # Remember old roles before rebuilding
            old_users = conn.execute(
                'SELECT user_id, role FROM users WHERE role != ?', ('admin',)
            ).fetchall()
            conn.execute('ALTER TABLE users RENAME TO _users_old')
            conn.execute('''
                CREATE TABLE users (
                    user_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    salt TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'custom'
                        CHECK(role IN ('admin','custom')),
                    permissions TEXT DEFAULT NULL,
                    display_name TEXT DEFAULT '',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    last_login DATETIME
                )
            ''')
            conn.execute('''
                INSERT INTO users (user_id, username, password_hash, salt, role, display_name, created_at, last_login)
                SELECT user_id, username, password_hash, salt,
                       CASE WHEN role = 'admin' THEN 'admin' ELSE 'custom' END,
                       display_name, created_at, last_login
                FROM _users_old
            ''')
            # Migrate old role permissions to per-user permissions
            _legacy_perms = {
                'editor':     '["devices", "wiki"]',
                'power_user': '["references", "wiki"]',
                'viewer':     '["wiki"]',
            }
            for uid, old_role in old_users:
                perms_json = _legacy_perms.get(old_role, '["wiki"]')
                conn.execute('UPDATE users SET permissions = ? WHERE user_id = ?',
                             (perms_json, uid))
            conn.execute('DROP TABLE _users_old')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)')

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

        # Stamp current schema version after all migrations complete
        conn.execute('''
            INSERT OR REPLACE INTO schema_info (key, value, updated_at)
            VALUES ('schema_version', ?, CURRENT_TIMESTAMP)
        ''', (str(SCHEMA_VERSION),))
        # Also record the app version that last touched this database
        try:
            _ver_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'VERSION')
            with open(_ver_path) as _vf:
                _app_ver = _vf.read().strip()
        except Exception:
            _app_ver = 'unknown'
        conn.execute('''
            INSERT OR REPLACE INTO schema_info (key, value, updated_at)
            VALUES ('app_version', ?, CURRENT_TIMESTAMP)
        ''', (_app_ver,))

    # Seed product references from CSV + images on first startup
    _seed_product_references()


def _seed_product_references():
    """
    Seed product references and wiki images from seed_data/ on first startup.

    Expected files in seed_data/:
      - product_reference.csv  (CSV with product reference columns)
      - printer_images.zip     (zip of printer images, filenames match model names)

    Only runs when the product_reference table is empty.
    """
    import csv as _csv
    import zipfile
    import mimetypes

    conn = get_connection()
    try:
        ref_count = conn.execute('SELECT COUNT(*) FROM product_reference').fetchone()[0]
    finally:
        conn.close()

    if ref_count > 0:
        return  # Already seeded or user has added their own data

    seed_dir = os.path.join(BUNDLE_DIR, 'seed_data')
    csv_path = os.path.join(seed_dir, 'product_reference.csv')

    if not os.path.isfile(csv_path):
        return  # No seed CSV present

    _audit_logger.info('Seeding product references from %s', csv_path)

    # --- Phase 1: Import CSV into product_reference ---
    try:
        with open(csv_path, 'r', encoding='utf-8-sig') as f:
            reader = _csv.DictReader(f)
            # Normalize header names to lowercase for flexible matching
            if reader.fieldnames is None:
                _audit_logger.warning('Seed CSV has no headers, skipping')
                return

            imported = 0
            for row in reader:
                # Normalize keys to lowercase
                norm = {k.strip().lower(): v.strip() for k, v in row.items() if k}
                codename = norm.get('codename', '').strip()
                if not codename:
                    continue
                add_product_reference(
                    codename=codename,
                    model_name=norm.get('model name', norm.get('model_name', '')),
                    wifi_gen=norm.get('wi-fi gen', norm.get('wifi gen', norm.get('wifi_gen', ''))),
                    year=norm.get('year', ''),
                    chip_manufacturer=norm.get('wireless chip set manufacturer',
                                     norm.get('chip manufacturer', norm.get('chip_manufacturer', ''))),
                    chip_codename=norm.get('wireless chipset codename',
                                  norm.get('chip codename', norm.get('chip_codename', ''))),
                    fw_codebase=norm.get('fw codebase', norm.get('fw_codebase', '')),
                    print_technology=norm.get('print technology', norm.get('print_technology', '')),
                    variant=norm.get('variant', ''),
                )
                imported += 1

            _audit_logger.info('Seeded %d product references from CSV', imported)
    except Exception as e:
        _audit_logger.error('Failed to seed product references from CSV: %s\n%s', e, traceback.format_exc())
        return

    # --- Phase 2: Seed wiki images from zip ---
    zip_path = os.path.join(seed_dir, 'printer_images.zip')
    if not os.path.isfile(zip_path):
        _audit_logger.info('No printer_images.zip found, skipping image seeding')
        return

    wiki_uploads_dir = os.path.join(DATA_DIR, 'wiki_uploads')

    # Build lookup: model_name (lowercase) -> ref_id
    conn = get_connection()
    try:
        refs = conn.execute('SELECT ref_id, codename, model_name FROM product_reference').fetchall()
    finally:
        conn.close()

    model_to_ref = {}
    codename_to_ref = {}
    for r in refs:
        if r['model_name']:
            model_to_ref[r['model_name'].lower().strip()] = r['ref_id']
        if r['codename']:
            codename_to_ref[r['codename'].lower().strip()] = r['ref_id']

    try:
        images_seeded = 0
        with zipfile.ZipFile(zip_path, 'r') as zf:
            for entry in zf.namelist():
                # Skip directories and hidden files
                if entry.endswith('/') or '/.' in entry or entry.startswith('.'):
                    continue

                # Get just the filename without path and extension
                basename = os.path.basename(entry)
                name_without_ext, ext = os.path.splitext(basename)
                ext = ext.lower()
                if ext not in ('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.svg', '.webp'):
                    continue

                # Match to product reference by model name or codename
                lookup_key = name_without_ext.lower().strip()
                ref_id = model_to_ref.get(lookup_key) or codename_to_ref.get(lookup_key)

                if not ref_id:
                    _audit_logger.debug('Seed image "%s" did not match any product reference', basename)
                    continue

                # Save the image to wiki_uploads/{ref_id}/
                ref_upload_dir = os.path.join(wiki_uploads_dir, str(ref_id))
                os.makedirs(ref_upload_dir, exist_ok=True)

                safe_filename = uuid.uuid4().hex + ext
                dest_path = os.path.join(ref_upload_dir, safe_filename)

                img_data = zf.read(entry)
                with open(dest_path, 'wb') as out:
                    out.write(img_data)

                content_type = mimetypes.guess_type(basename)[0] or 'image/png'
                add_wiki_attachment(
                    ref_id=ref_id,
                    filename=safe_filename,
                    original_name=basename,
                    content_type=content_type,
                    size_bytes=len(img_data),
                    uploaded_by='system',
                )
                images_seeded += 1

        _audit_logger.info('Seeded %d wiki images from printer_images.zip', images_seeded)
    except Exception as e:
        _audit_logger.error('Failed to seed wiki images: %s\n%s', e, traceback.format_exc())


def generate_device_id():
    """Generate a short unique device ID (10 hex chars)."""
    return uuid.uuid4().hex[:10]


_B36_CHARS = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'


def _int_to_base36(n):
    """Convert a positive integer to an uppercase base-36 string."""
    if n == 0:
        return '0'
    result = []
    while n:
        n, rem = divmod(n, 36)
        result.append(_B36_CHARS[rem])
    return ''.join(reversed(result))


def _base36_to_int(s):
    """Convert a base-36 string back to an integer."""
    return int(s, 36)


_BARCODE_PREFIX = 'CNX-'


def _next_barcode_value(conn):
    """Generate the next sequential barcode like CNX-1, CNX-2, ..., CNX-A, CNX-10.

    Uses a barcode_seq table as a monotonic counter to avoid race conditions
    and full table scans. Falls back to scanning devices if the sequence
    table doesn't exist yet (first run / migration).
    """
    # Ensure sequence table exists
    conn.execute('''
        CREATE TABLE IF NOT EXISTS barcode_seq (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            next_val INTEGER NOT NULL DEFAULT 1
        )
    ''')
    row = conn.execute('SELECT next_val FROM barcode_seq WHERE id = 1').fetchone()
    if row is None:
        # Initialize from existing devices (migration from old scheme)
        max_num = 0
        for r in conn.execute("SELECT barcode_value FROM devices").fetchall():
            val = r[0]
            # Strip known prefixes
            stripped = val
            for prefix in (_BARCODE_PREFIX, 'INV-'):
                if val.startswith(prefix):
                    stripped = val[len(prefix):]
                    break
            try:
                max_num = max(max_num, _base36_to_int(stripped))
            except (ValueError, TypeError):
                continue
        next_val = max_num + 1
        conn.execute('INSERT INTO barcode_seq (id, next_val) VALUES (1, ?)', (next_val + 1,))
    else:
        next_val = row[0]
        conn.execute('UPDATE barcode_seq SET next_val = ? WHERE id = 1', (next_val + 1,))
    return f'{_BARCODE_PREFIX}{_int_to_base36(next_val)}'


def _insert_device(conn, data, performed_by='system'):
    """Internal helper: insert a device and log it. Returns device_id. Takes existing conn."""
    device_id = generate_device_id()
    barcode_value = _next_barcode_value(conn)

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


def get_device_by_serial(serial_number):
    """Look up a device by serial number (case-insensitive). Returns dict or None."""
    with db_transaction() as conn:
        row = conn.execute(
            'SELECT * FROM devices WHERE UPPER(serial_number) = UPPER(?) AND status != ?',
            (serial_number, 'retired')
        ).fetchone()
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


def _parse_user_row(row):
    """Convert a user row to a dict, parsing the permissions JSON field."""
    if not row:
        return None
    user = dict(row)
    perms_raw = user.get('permissions')
    if perms_raw and isinstance(perms_raw, str):
        try:
            user['permissions'] = json.loads(perms_raw)
        except (json.JSONDecodeError, TypeError):
            user['permissions'] = []
    elif not perms_raw:
        user['permissions'] = []
    return user


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
        return _parse_user_row(row)


def get_user(user_id):
    """Get a user by ID."""
    with db_transaction() as conn:
        row = conn.execute('SELECT * FROM users WHERE user_id = ?', (user_id,)).fetchone()
        return _parse_user_row(row)


def get_user_by_username(username):
    """Get a user by username."""
    with db_transaction() as conn:
        row = conn.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        return _parse_user_row(row)


def get_all_users():
    """Get all users ordered by username."""
    with db_transaction() as conn:
        rows = conn.execute(
            'SELECT user_id, username, role, permissions, display_name, created_at, last_login FROM users ORDER BY username'
        ).fetchall()
        return [_parse_user_row(r) for r in rows]


def create_user(username, password, role='custom', display_name='', permissions=None):
    """Create a new user. Returns user_id. Raises ValueError if username taken.
    permissions: optional list of permission strings for custom role."""
    pw_hash, salt = _hash_password(password)
    perms_json = json.dumps(sorted(permissions)) if permissions else None
    with db_transaction() as conn:
        try:
            conn.execute(
                'INSERT INTO users (username, password_hash, salt, role, permissions, display_name) VALUES (?, ?, ?, ?, ?, ?)',
                (username, pw_hash, salt, role, perms_json, display_name or username)
            )
            return conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        except sqlite3.IntegrityError:
            raise ValueError(f'Username "{username}" already exists')


def update_user(user_id, data):
    """Update user fields (display_name, role, permissions). Optionally update password."""
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
        if 'permissions' in data:
            perms = data['permissions']
            perms_json = json.dumps(sorted(perms)) if perms else None
            conn.execute(
                'UPDATE users SET permissions = ? WHERE user_id = ?',
                (perms_json, user_id)
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


def reset_admin_password(new_password='admin'):
    """Emergency admin password reset. Resets the first admin user's password.
    If no admin user exists, creates one with username 'admin'.
    Returns (username, was_created) tuple."""
    with db_transaction() as conn:
        admin = conn.execute(
            "SELECT user_id, username FROM users WHERE role = 'admin' ORDER BY user_id LIMIT 1"
        ).fetchone()
        if admin:
            pw_hash, salt = _hash_password(new_password)
            conn.execute(
                'UPDATE users SET password_hash = ?, salt = ? WHERE user_id = ?',
                (pw_hash, salt, admin['user_id'])
            )
            _audit_logger.warning('Admin password reset via CLI for user: %s', admin['username'])
            return (admin['username'], False)
        else:
            pw_hash, salt = _hash_password(new_password)
            conn.execute(
                "INSERT INTO users (username, password_hash, salt, role, display_name) "
                "VALUES (?, ?, ?, 'admin', 'Administrator')",
                ('admin', pw_hash, salt)
            )
            _audit_logger.warning('Emergency admin user created via CLI')
            return ('admin', True)


def export_database_to_sql(output_path):
    """Export entire database to a SQL dump file for emergency recovery."""
    conn = get_connection()
    try:
        with open(output_path, 'w') as f:
            for line in conn.iterdump():
                f.write(line + '\n')
        return True
    except Exception as e:
        _audit_logger.error('Database SQL export failed: %s', e)
        return False
    finally:
        conn.close()


def emergency_backup(dest_path=None):
    """Create an emergency backup copy of the database file.
    Returns the path of the backup file."""
    if dest_path is None:
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_dir = _get_backup_dir()
        os.makedirs(backup_dir, exist_ok=True)
        dest_path = os.path.join(backup_dir, f'emergency_{timestamp}.db')

    checkpoint_wal()
    shutil.copy2(DB_PATH, dest_path)
    _audit_logger.info('Emergency backup created: %s', dest_path)
    return dest_path


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
        'max_backups': saved.get('max_backups', 10),
        'backup_interval_hours': saved.get('backup_interval_hours', 4),
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
        'last_verify_time': saved.get('last_verify_time', ''),
        'last_verify_ok': saved.get('last_verify_ok', False),
        'last_verify_file': saved.get('last_verify_file', ''),
        'last_verify_result': saved.get('last_verify_result', ''),
        'last_verified_file': saved.get('last_verified_file', ''),
    }


def get_default_backup_config():
    """Return factory-default backup configuration values."""
    return {
        'backup_dir': _DEFAULT_BACKUP_DIR,
        'max_backups': 10,
        'backup_interval_hours': 4,
        'prune_enabled': False,
        'prune_interval_hours': 24,
        'backup_enabled': False,
        'git_enabled': False,
        'git_repo': '',
        'git_branch': 'backups',
        'git_token': '',
        'git_push_interval_hours': 24,
        'last_git_push': '',
        'last_backup': '',
        'last_backup_hash': '',
        'last_verify_time': '',
        'last_verify_ok': False,
        'last_verify_file': '',
        'last_verify_result': '',
        'last_verified_file': '',
    }


def save_backup_config(config):
    """Persist backup configuration to disk atomically (write-then-rename)."""
    tmp_path = BACKUP_CONFIG_FILE + '.tmp'
    with open(tmp_path, 'w') as f:
        json.dump(config, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, BACKUP_CONFIG_FILE)


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

    # Validate backup directory is writable before proceeding
    if not os.access(backup_dir, os.W_OK):
        raise RuntimeError(f'Backup directory is not writable: {backup_dir}')

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
    return verify_backup(rotate=False)


def verify_backup(rotate=False):
    """
    Verify a backup file is still a valid, intact SQLite database.
    When rotate=True, cycles through backups (different one each call)
    to catch silent corruption in older files.
    Stores results in backup config for UI display.
    """
    backup_dir = _get_backup_dir()
    all_backups = sorted(
        [f for f in os.listdir(backup_dir) if _is_backup_file(f)],
        reverse=True,
    )
    if not all_backups:
        result = {'ok': False, 'result': 'No backup files found', 'filename': None}
        _save_verify_result(result)
        return result

    if rotate and len(all_backups) > 1:
        config = _get_backup_config()
        last_verified = config.get('last_verified_file', '')
        try:
            idx = all_backups.index(last_verified)
            target_idx = (idx + 1) % len(all_backups)
        except ValueError:
            target_idx = 0
        target = all_backups[target_idx]
    else:
        target = all_backups[0]

    target_path = os.path.join(backup_dir, target)
    try:
        conn = sqlite3.connect(target_path)
        integrity = conn.execute('PRAGMA integrity_check').fetchone()[0]
        device_count = conn.execute('SELECT COUNT(*) FROM devices').fetchone()[0]
        user_count = conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]
        conn.close()
        ok = integrity == 'ok'
        if not ok:
            _audit_logger.warning('Backup verification failed: %s — %s', target, integrity)
        result = {
            'ok': ok,
            'result': integrity,
            'filename': target,
            'device_count': device_count,
            'user_count': user_count,
        }
    except Exception as e:
        _audit_logger.error('Backup verification error: %s — %s', target, e)
        result = {'ok': False, 'result': str(e), 'filename': target}

    _save_verify_result(result)
    return result


def _save_verify_result(result):
    """Store verification result in config for UI display."""
    config = _get_backup_config()
    config['last_verify_time'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    config['last_verify_ok'] = result.get('ok', False)
    config['last_verify_file'] = result.get('filename', '')
    config['last_verify_result'] = result.get('result', '')
    if result.get('filename'):
        config['last_verified_file'] = result['filename']
    save_backup_config(config)


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

            # Create/update zip archive — stable name so git tracks diffs
            zip_name = 'hp_connectivity_inventory_backup.zip'
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
                push_target = config.get('git_repo', '').strip() or 'origin'
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


def get_schema_version(db_path=None):
    """
    Read the schema version from a database file.
    Returns (version: int, app_version: str) tuple.
    Returns (0, 'unknown') for databases created before version tracking.
    """
    path = db_path or DB_PATH
    try:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        if 'schema_info' not in tables:
            conn.close()
            return (0, 'unknown')
        row = conn.execute(
            "SELECT value FROM schema_info WHERE key='schema_version'"
        ).fetchone()
        schema_ver = int(row[0]) if row else 0
        row2 = conn.execute(
            "SELECT value FROM schema_info WHERE key='app_version'"
        ).fetchone()
        app_ver = row2[0] if row2 else 'unknown'
        conn.close()
        return (schema_ver, app_ver)
    except Exception:
        return (0, 'unknown')


def validate_backup_compatibility(backup_path):
    """
    Validate that a backup file is compatible with the current application.
    Returns dict with 'compatible' (bool), 'warnings' (list), 'errors' (list),
    and metadata about the backup ('tables', 'schema_version', 'app_version',
    'device_count', 'user_count').
    """
    result = {
        'compatible': True,
        'warnings': [],
        'errors': [],
        'tables': set(),
        'schema_version': 0,
        'app_version': 'unknown',
        'device_count': 0,
        'user_count': 0,
    }

    try:
        conn = sqlite3.connect(backup_path)
    except Exception as e:
        result['compatible'] = False
        result['errors'].append(f'Cannot open database file: {e}')
        return result

    try:
        # Check integrity
        integrity = conn.execute('PRAGMA integrity_check').fetchone()[0]
        if integrity != 'ok':
            result['compatible'] = False
            result['errors'].append(f'Integrity check failed: {integrity}')
            return result

        # Enumerate tables
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        result['tables'] = tables

        # Check required tables
        missing_required = REQUIRED_TABLES - tables
        if missing_required:
            result['compatible'] = False
            result['errors'].append(
                f'Missing required tables: {", ".join(sorted(missing_required))}'
            )
            return result

        # Check expected (non-required) tables — warn if missing
        missing_expected = EXPECTED_TABLES - tables - {'schema_info'}
        if missing_expected:
            result['warnings'].append(
                f'Missing tables (will be created on restore): {", ".join(sorted(missing_expected))}'
            )

        # Read schema version from backup
        schema_ver, app_ver = get_schema_version(backup_path)
        result['schema_version'] = schema_ver
        result['app_version'] = app_ver

        if schema_ver > SCHEMA_VERSION:
            result['warnings'].append(
                f'Backup schema version ({schema_ver}) is newer than current app '
                f'schema ({SCHEMA_VERSION}). Some features may not work correctly.'
            )

        if schema_ver == 0:
            result['warnings'].append(
                'Backup was created before schema version tracking was added. '
                'Automatic migrations will be applied on restore.'
            )

        # Check device columns for compatibility
        device_cols = {r[1] for r in conn.execute('PRAGMA table_info(devices)').fetchall()}
        expected_device_cols = {'device_id', 'barcode_value', 'name', 'category',
                                'manufacturer', 'model_number', 'serial_number',
                                'connectivity', 'vendor_supplied', 'status',
                                'location', 'assigned_to', 'notes',
                                'created_at', 'updated_at'}
        missing_device_cols = expected_device_cols - device_cols
        if missing_device_cols:
            result['warnings'].append(
                f'Devices table missing columns (may indicate older backup): '
                f'{", ".join(sorted(missing_device_cols))}'
            )

        extra_device_cols = device_cols - expected_device_cols - {'codename', 'variant'}
        if extra_device_cols:
            result['warnings'].append(
                f'Devices table has unexpected columns (may indicate newer backup): '
                f'{", ".join(sorted(extra_device_cols))}'
            )

        # Check user role values for old role system
        if 'users' in tables:
            user_cols = {r[1] for r in conn.execute('PRAGMA table_info(users)').fetchall()}
            result['user_count'] = conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]

            if 'permissions' not in user_cols:
                result['warnings'].append(
                    'Users table is missing "permissions" column (pre-v2 schema). '
                    'Old roles will be migrated automatically on restore.'
                )

            # Check for legacy roles that need migration
            try:
                legacy_roles = conn.execute(
                    "SELECT DISTINCT role FROM users WHERE role NOT IN ('admin', 'custom')"
                ).fetchall()
                if legacy_roles:
                    role_names = [r[0] for r in legacy_roles]
                    result['warnings'].append(
                        f'Backup contains legacy user roles: {", ".join(role_names)}. '
                        f'These will be migrated to "custom" with appropriate permissions.'
                    )
            except Exception:
                pass

        # Count devices
        result['device_count'] = conn.execute('SELECT COUNT(*) FROM devices').fetchone()[0]

    except sqlite3.DatabaseError as e:
        result['compatible'] = False
        result['errors'].append(f'Database error during validation: {e}')
    finally:
        conn.close()

    return result


def restore_database(filename):
    """
    Restore the database from a backup file using SQLite online backup API.
    Checkpoints WAL first, creates a safety backup, validates, then restores.
    Rolls back to safety backup if restore fails.
    Returns dict with restore metadata including compatibility warnings.
    """
    import time as _time
    start_time = _time.monotonic()

    backup_dir = _get_backup_dir()
    if not _is_backup_file(filename) or '..' in filename:
        raise ValueError('Invalid backup filename')
    backup_path = os.path.join(backup_dir, filename)
    if not os.path.isfile(backup_path):
        raise FileNotFoundError(f'Backup file not found: {filename}')

    # Run backwards-compatibility validation on the backup file
    compat = validate_backup_compatibility(backup_path)
    if not compat['compatible']:
        error_detail = '; '.join(compat['errors'])
        raise ValueError(f'Backup is not compatible: {error_detail}')

    if compat['warnings']:
        for w in compat['warnings']:
            _audit_logger.warning('Restore compatibility warning for %s: %s', filename, w)

    _audit_logger.info(
        'Restore source validated: %s (integrity=ok, schema_v%d, app=%s, %d devices, %d users%s)',
        filename, compat['schema_version'], compat['app_version'],
        compat['device_count'], compat['user_count'],
        f', {len(compat["warnings"])} warnings' if compat['warnings'] else ''
    )

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
        'warnings': compat.get('warnings', []),
        'schema_version': compat.get('schema_version', 0),
        'app_version': compat.get('app_version', 'unknown'),
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
    git_token = os.environ.get('GIT_BACKUP_TOKEN', '').strip() or config.get('git_token', '').strip()
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

        # Find the backup zip (support both old and new naming)
        zip_path = os.path.join(tmpdir, 'hp_connectivity_inventory_backup.zip')
        if not os.path.isfile(zip_path):
            zip_path = os.path.join(tmpdir, 'inventory_backups.zip')
        if not os.path.isfile(zip_path):
            raise ValueError('No backup zip found on the git branch.')

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

        # Find the backup zip (support both old and new naming)
        zip_path = os.path.join(tmpdir, 'hp_connectivity_inventory_backup.zip')
        if not os.path.isfile(zip_path):
            zip_path = os.path.join(tmpdir, 'inventory_backups.zip')
        if not os.path.isfile(zip_path):
            raise ValueError('No backup zip found on the git branch.')

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
    """Add a single product reference entry. Returns the new ref_id."""
    with db_transaction() as conn:
        cursor = conn.execute('''
            INSERT INTO product_reference
                (codename, model_name, wifi_gen, year, chip_manufacturer, chip_codename, fw_codebase, print_technology, variant)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (codename, model_name, wifi_gen, year, chip_manufacturer, chip_codename, fw_codebase, print_technology, variant))
        ref_id = cursor.lastrowid
        # Auto-create a wiki page for the new product
        conn.execute('''
            INSERT OR IGNORE INTO product_wiki (ref_id, content, updated_by)
            VALUES (?, '', '')
        ''', (ref_id,))
        return ref_id


def update_product_reference(ref_id, codename, model_name='', wifi_gen='', year='',
                             chip_manufacturer='', chip_codename='', fw_codebase='',
                             print_technology=''):
    """Update an existing product reference entry."""
    with db_transaction() as conn:
        conn.execute('''
            UPDATE product_reference
            SET codename = ?, model_name = ?, wifi_gen = ?, year = ?,
                chip_manufacturer = ?, chip_codename = ?, fw_codebase = ?,
                print_technology = ?, updated_at = CURRENT_TIMESTAMP
            WHERE ref_id = ?
        ''', (codename, model_name, wifi_gen, year, chip_manufacturer, chip_codename, fw_codebase, print_technology, ref_id))


def delete_product_reference(ref_id):
    """Delete a product reference entry and its associated wiki/attachments."""
    with db_transaction() as conn:
        conn.execute('DELETE FROM wiki_attachments WHERE ref_id = ?', (ref_id,))
        conn.execute('DELETE FROM product_wiki WHERE ref_id = ?', (ref_id,))
        conn.execute('DELETE FROM product_reference WHERE ref_id = ?', (ref_id,))


def clear_all_product_references():
    """Delete all product reference entries and associated wiki data."""
    with db_transaction() as conn:
        conn.execute('DELETE FROM wiki_attachments')
        conn.execute('DELETE FROM product_wiki')
        conn.execute('DELETE FROM product_reference')


# ---------------------------------------------------------------------------
# Product Wiki
# ---------------------------------------------------------------------------

def get_wiki_by_ref_id(ref_id):
    """Return wiki content for a product reference, or None."""
    conn = get_connection()
    try:
        row = conn.execute(
            'SELECT * FROM product_wiki WHERE ref_id = ?', (ref_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def save_wiki(ref_id, content, updated_by=''):
    """Create or update wiki content for a product reference."""
    with db_transaction() as conn:
        existing = conn.execute(
            'SELECT wiki_id FROM product_wiki WHERE ref_id = ?', (ref_id,)
        ).fetchone()
        if existing:
            conn.execute('''
                UPDATE product_wiki
                SET content = ?, updated_by = ?, updated_at = CURRENT_TIMESTAMP
                WHERE ref_id = ?
            ''', (content, updated_by, ref_id))
        else:
            conn.execute('''
                INSERT INTO product_wiki (ref_id, content, updated_by)
                VALUES (?, ?, ?)
            ''', (ref_id, content, updated_by))


def get_wiki_attachments(ref_id):
    """Return all attachments for a product wiki."""
    conn = get_connection()
    try:
        rows = conn.execute(
            'SELECT * FROM wiki_attachments WHERE ref_id = ? ORDER BY uploaded_at DESC',
            (ref_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def add_wiki_attachment(ref_id, filename, original_name, content_type, size_bytes, uploaded_by):
    """Record a new wiki attachment."""
    with db_transaction() as conn:
        conn.execute('''
            INSERT INTO wiki_attachments
                (ref_id, filename, original_name, content_type, size_bytes, uploaded_by)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (ref_id, filename, original_name, content_type, size_bytes, uploaded_by))


def get_wiki_attachment(attachment_id):
    """Return a single attachment by ID."""
    conn = get_connection()
    try:
        row = conn.execute(
            'SELECT * FROM wiki_attachments WHERE attachment_id = ?', (attachment_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def delete_wiki_attachment(attachment_id):
    """Delete an attachment record."""
    with db_transaction() as conn:
        conn.execute('DELETE FROM wiki_attachments WHERE attachment_id = ?', (attachment_id,))


# ---------------------------------------------------------------------------
# Device Notes — anyone can add notes to a device
# ---------------------------------------------------------------------------

def get_device_notes(device_id):
    """Return all notes for a device, newest first."""
    conn = get_connection()
    try:
        rows = conn.execute(
            'SELECT * FROM device_notes WHERE device_id = ? ORDER BY created_at DESC',
            (device_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def add_device_note(device_id, author, content):
    """Add a note to a device. Returns the note_id."""
    with db_transaction() as conn:
        cursor = conn.execute(
            'INSERT INTO device_notes (device_id, author, content) VALUES (?, ?, ?)',
            (device_id, author, content)
        )
        return cursor.lastrowid


def delete_device_note(note_id):
    """Delete a device note by ID."""
    with db_transaction() as conn:
        conn.execute('DELETE FROM device_notes WHERE note_id = ?', (note_id,))
