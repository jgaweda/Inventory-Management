"""
Database module for the HP Connectivity Team Inventory Management System.

Handles all SQLite operations: schema creation, CRUD for devices,
audit logging, search, and statistics. Uses WAL mode for better
concurrency and a context manager for safe transactions.
"""

import sqlite3
import uuid
import json
import os
from contextlib import contextmanager
from datetime import datetime

# Path to the SQLite database file (same directory as this script)
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'inventory.db')

# Default categories seeded on first run
DEFAULT_CATEGORIES = [
    ('Wi-Fi Module', 'Wireless networking modules'),
    ('Bluetooth Dongle', 'Bluetooth USB dongles and adapters'),
    ('Dev Board', 'Development and evaluation boards'),
    ('Antenna', 'Antennas and antenna assemblies'),
    ('Access Point', 'Wireless access points'),
    ('Cable/Adapter', 'Cables, adapters, and connectors'),
    ('Test Equipment', 'Test and measurement equipment'),
    ('Reference Design', 'Reference design hardware'),
    ('Other', 'Uncategorized items'),
]

# Fields that can be updated via update_device()
UPDATABLE_FIELDS = [
    'name', 'category', 'manufacturer', 'model_number', 'serial_number',
    'firmware_version', 'connectivity', 'status', 'location',
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
                firmware_version TEXT DEFAULT '',
                connectivity TEXT DEFAULT '',
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
                description TEXT DEFAULT ''
            )
        ''')

        # Indexes for common queries
        conn.execute('CREATE INDEX IF NOT EXISTS idx_devices_status ON devices(status)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_devices_category ON devices(category)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_devices_barcode ON devices(barcode_value)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_audit_device ON audit_log(device_id)')

        # Seed default categories
        for name, desc in DEFAULT_CATEGORIES:
            conn.execute(
                'INSERT OR IGNORE INTO categories (name, description) VALUES (?, ?)',
                (name, desc)
            )

        # Seed test data if the devices table is empty
        count = conn.execute('SELECT COUNT(*) FROM devices').fetchone()[0]
        if count == 0:
            _seed_test_data(conn)


def _seed_test_data(conn):
    """Insert test devices for development/demo. Only called when DB is empty."""
    test_devices = [
        {
            'name': 'Intel AX211 Wi-Fi Module',
            'category': 'Wi-Fi Module',
            'manufacturer': 'Intel',
            'model_number': 'AX211NGW',
            'serial_number': 'SN-AX211-001',
            'firmware_version': '22.240.0.4',
            'connectivity': 'Wi-Fi 6E',
            'location': 'Lab A Shelf 2',
        },
        {
            'name': 'Qualcomm QCA6696 Module',
            'category': 'Wi-Fi Module',
            'manufacturer': 'Qualcomm',
            'model_number': 'QCA6696',
            'serial_number': 'SN-QCA-002',
            'firmware_version': '3.2.1',
            'connectivity': 'Wi-Fi 7',
            'location': 'Lab A Shelf 3',
        },
        {
            'name': 'Nordic nRF52840 Dongle',
            'category': 'Bluetooth Dongle',
            'manufacturer': 'Nordic Semiconductor',
            'model_number': 'nRF52840',
            'serial_number': 'SN-NRF-003',
            'firmware_version': '1.4.2',
            'connectivity': 'BT 5.3 / BLE',
            'location': 'Lab B',
        },
        {
            'name': 'Raspberry Pi 4 Dev Board',
            'category': 'Dev Board',
            'manufacturer': 'Raspberry Pi Foundation',
            'model_number': 'RPi4-8GB',
            'serial_number': 'SN-RPI-004',
            'firmware_version': '-',
            'connectivity': 'Wi-Fi 5 / BT 5.0',
            'location': 'Lab A Bench 1',
        },
        {
            'name': 'Taoglas FXP840 Antenna',
            'category': 'Antenna',
            'manufacturer': 'Taoglas',
            'model_number': 'FXP840.07.0100A',
            'serial_number': 'SN-TAG-005',
            'firmware_version': '-',
            'connectivity': 'Wi-Fi 6E',
            'location': 'Storage Cabinet',
        },
        {
            'name': 'Broadcom BCM4389 Eval Board',
            'category': 'Dev Board',
            'manufacturer': 'Broadcom',
            'model_number': 'BCM4389-EVB',
            'serial_number': 'SN-BCM-006',
            'firmware_version': '101.10.591',
            'connectivity': 'Wi-Fi 6E / BT 5.2',
            'location': 'Lab B Bench 2',
            'notes': 'Primary Wi-Fi 6E test platform',
        },
    ]

    device_ids = []
    for data in test_devices:
        device_id = _insert_device(conn, data, performed_by='system')
        device_ids.append(device_id)

    # Check out device #1 to Sarah Chen
    if device_ids:
        conn.execute(
            "UPDATE devices SET status='checked_out', assigned_to=?, updated_at=CURRENT_TIMESTAMP WHERE device_id=?",
            ('Sarah Chen', device_ids[0])
        )
        log_action(conn, device_ids[0], 'checked_out', 'system', 'Assigned to Sarah Chen')


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
            model_number, serial_number, firmware_version, connectivity, status,
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
        data.get('firmware_version', ''),
        data.get('connectivity', ''),
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
    """Append an entry to the audit log. Uses an existing connection (caller manages transaction)."""
    conn.execute(
        'INSERT INTO audit_log (device_id, action, performed_by, details) VALUES (?, ?, ?, ?)',
        (device_id, action, performed_by, details)
    )


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
    """Get all categories ordered by name."""
    with db_transaction() as conn:
        rows = conn.execute('SELECT * FROM categories ORDER BY name').fetchall()
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
