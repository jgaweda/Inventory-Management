"""
HP Connectivity Team Inventory Management System — Flask Application

All routes are defined here. Run with: python app.py [--host HOST] [--port PORT]

Authentication: Admins (scanner terminal) can add/edit/checkout/retire/import devices.
Anyone on the network can view the inventory without logging in.
"""

import argparse
import csv
import io
import json
import logging
import os
import traceback
import uuid
from functools import wraps
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler

from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, send_file, jsonify, Response, session, g,
)
from runtime_dirs import BUNDLE_DIR, DATA_DIR

import database as db
import barcode_utils

app = Flask(__name__,
            static_folder=os.path.join(BUNDLE_DIR, 'static'),
            template_folder=os.path.join(BUNDLE_DIR, 'templates'))
app.secret_key = os.environ.get('SECRET_KEY', 'hp-connectivity-inventory-system-change-me')

# Application version (read from VERSION file)
_version_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'VERSION')
try:
    with open(_version_path) as _vf:
        _app_version = _vf.read().strip()
except FileNotFoundError:
    _app_version = 'dev'

# ---------------------------------------------------------------------------
# Application logging (rotating file, single file that overwrites at limit)
# ---------------------------------------------------------------------------

LOG_DIR = os.path.join(DATA_DIR, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, 'app.log')
LOG_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'log_config.json')
SERVER_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'server_config.json')


def _load_log_config():
    try:
        with open(LOG_CONFIG_FILE, 'r') as f:
            import json as _j
            return _j.load(f)
    except (FileNotFoundError, ValueError):
        return {'max_size_mb': 2}


def _save_log_config(config):
    import json as _j
    with open(LOG_CONFIG_FILE, 'w') as f:
        _j.dump(config, f)


def _load_server_config():
    try:
        with open(SERVER_CONFIG_FILE, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {'port': 8080, 'host': '0.0.0.0'}


def _save_server_config(config):
    with open(SERVER_CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=2)


_log_config = _load_log_config()
_log_max_bytes = int(_log_config.get('max_size_mb', 2) * 1024 * 1024)

app_logger = logging.getLogger('inventory')
app_logger.setLevel(logging.DEBUG)
_log_handler = RotatingFileHandler(LOG_FILE, maxBytes=_log_max_bytes, backupCount=1)
_log_handler.setFormatter(logging.Formatter(
    '%(asctime)s | %(levelname)-7s | %(message)s', datefmt='%Y-%m-%d %H:%M:%S'
))
app_logger.addHandler(_log_handler)


def _reconfigure_log_handler(max_size_mb):
    """Update the log handler's max size at runtime."""
    _log_handler.maxBytes = int(max_size_mb * 1024 * 1024)

# ---------------------------------------------------------------------------
# Startup: initialize the database
# ---------------------------------------------------------------------------

with app.app_context():
    db.init_db()
    os.makedirs(os.path.join(app.static_folder, 'labels'), exist_ok=True)
    os.makedirs(db._get_backup_dir(), exist_ok=True)
    os.makedirs(os.path.join(DATA_DIR, 'wiki_uploads'), exist_ok=True)
    # Startup integrity check — log warning if database is corrupt
    _integrity = db.startup_integrity_check()
    if not _integrity['ok']:
        app_logger.error('DATABASE INTEGRITY ISSUE ON STARTUP: %s', _integrity['result'])
    # Check wiki attachment integrity — remove orphaned DB records for missing files
    _att_check = db.check_attachment_integrity(os.path.join(DATA_DIR, 'wiki_uploads'))
    if _att_check['orphaned_removed'] > 0:
        app_logger.warning('Startup: removed %d orphaned wiki attachment records', _att_check['orphaned_removed'])
    app_logger.info('Application started — database initialized')

# ---------------------------------------------------------------------------
# Authentication helpers
# ---------------------------------------------------------------------------

@app.before_request
def load_user():
    """Load the current user from session before each request."""
    g.user = None
    user_id = session.get('user_id')
    if user_id:
        g.user = db.get_user(user_id)
        if not g.user:
            session.clear()


# Periodically check if the scheduler thread is alive (every ~60 seconds)
_last_scheduler_check = datetime.now()


@app.before_request
def _check_scheduler_health():
    """Self-heal: restart scheduler thread if it died, checked at most once per minute."""
    global _last_scheduler_check
    now = datetime.now()
    if (now - _last_scheduler_check).total_seconds() < 60:
        return
    _last_scheduler_check = now
    if _scheduler_thread is not None and not _scheduler_thread.is_alive():
        app_logger.warning('Scheduler thread found dead — restarting')
        _ensure_scheduler_running()


def login_required(f):
    """Decorator: redirect to login if not authenticated."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not g.user:
            flash('Please log in to continue.', 'warning')
            return redirect(url_for('login', next=request.path))
        return f(*args, **kwargs)
    return decorated


# Centralized permission model — single source of truth for all role access.
# To change what a role can do, edit this dict. To add a role, add a line.
ROLE_PERMISSIONS = {
    'admin':  {'devices', 'references', 'wiki', 'wiki_admin', 'users', 'backups', 'logs', 'settings', 'notes_delete', 'retire'},
    'custom': set(),  # custom users get permissions from their user record
}

# Assignable permissions shown as checkboxes when creating/editing custom users.
# Admin-only permissions (users, backups, logs, settings) are not assignable.
ASSIGNABLE_PERMISSIONS = [
    ('devices',      'Devices — Add, edit, checkout/checkin devices'),
    ('references',   'References — Manage product reference catalog'),
    ('wiki',         'Wiki — View and edit product wiki pages'),
    ('wiki_admin',   'Wiki Admin — Upload/delete wiki attachments'),
    ('retire',       'Retire — Retire and unretire devices'),
    ('notes_delete', 'Notes — Delete device notes'),
]


def get_user_permissions(user):
    """Return the effective permission set for a user dict."""
    if not user:
        return set()
    if user['role'] == 'admin':
        return ROLE_PERMISSIONS['admin']
    # Custom users: permissions is a pre-parsed list from _parse_user_row
    perms = user.get('permissions')
    if isinstance(perms, list):
        return set(perms)
    if isinstance(perms, str):
        try:
            return set(json.loads(perms))
        except (json.JSONDecodeError, TypeError):
            pass
    return set()


def has_permission(permission):
    """Check if the current user has a specific permission."""
    if not g.user:
        return False
    return permission in get_user_permissions(g.user)


def permission_required(permission):
    """Decorator: require a specific permission. Redirects to login or dashboard."""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not g.user:
                flash('Please log in to continue.', 'warning')
                return redirect(url_for('login', next=request.path))
            if not has_permission(permission):
                flash('You do not have permission to perform this action.', 'error')
                return redirect(url_for('dashboard'))
            return f(*args, **kwargs)
        return decorated
    return decorator


def current_username():
    """Return display name of logged-in user, or 'system'."""
    if g.user:
        return g.user['display_name'] or g.user['username']
    return 'system'

# ---------------------------------------------------------------------------
# Context processor: inject categories, user, and current time into templates
# ---------------------------------------------------------------------------

@app.context_processor
def inject_globals():
    return {
        'categories': db.get_categories(),
        'now': datetime.now(timezone.utc),
        'current_user': g.user,
        'has_permission': has_permission,
        'app_version': _app_version,
    }

# ---------------------------------------------------------------------------
# Login / Logout
# ---------------------------------------------------------------------------

# Simple in-memory rate limiter for login
_login_attempts = {}  # ip -> [timestamp, ...]
_LOGIN_WINDOW = 300   # 5 minutes
_LOGIN_MAX = 10       # max attempts per window


@app.route('/login', methods=['GET', 'POST'])
def login():
    if g.user:
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        ip = request.remote_addr
        now = datetime.now().timestamp()
        # Clean old attempts and check rate
        attempts = [t for t in _login_attempts.get(ip, []) if now - t < _LOGIN_WINDOW]
        if len(attempts) >= _LOGIN_MAX:
            app_logger.warning('Login rate limited: ip=%s attempts=%d', ip, len(attempts))
            flash('Too many login attempts. Please wait a few minutes.', 'error')
            return render_template('login.html', next=request.args.get('next', ''))

        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        user = db.authenticate_user(username, password)
        if user:
            _login_attempts.pop(ip, None)  # Clear on success
            session['user_id'] = user['user_id']
            session['role'] = user['role']
            app_logger.info('Login successful: user=%s role=%s ip=%s', username, user['role'], request.remote_addr)
            next_url = request.form.get('next', '')
            if not next_url or next_url.startswith('//') or '://' in next_url:
                next_url = url_for('dashboard')
            return redirect(next_url)
        else:
            attempts.append(now)
            _login_attempts[ip] = attempts
            app_logger.warning('Login failed: user=%s ip=%s attempt=%d/%d', username, ip, len(attempts), _LOGIN_MAX)
            flash('Invalid username or password.', 'error')

    return render_template('login.html', next=request.args.get('next', ''))


@app.route('/logout')
def logout():
    username = current_username()
    session.clear()
    app_logger.info('Logout: user=%s ip=%s', username, request.remote_addr)
    flash('You have been logged out.', 'success')
    return redirect(url_for('dashboard'))

# ---------------------------------------------------------------------------
# Dashboard (public)
# ---------------------------------------------------------------------------

@app.route('/health')
def health():
    """Health check endpoint for monitoring and CI smoke tests."""
    try:
        integrity = db.check_database_integrity()
        return jsonify({'status': 'ok', 'db': integrity['result']})
    except Exception as e:
        return jsonify({'status': 'error', 'error': str(e)}), 500


@app.route('/')
def dashboard():
    stats = db.get_stats()
    health = db.get_backup_health()
    return render_template('dashboard.html', stats=stats, health=health)

# ---------------------------------------------------------------------------
# Device list (public)
# ---------------------------------------------------------------------------

@app.route('/devices')
def device_list():
    q = request.args.get('q', '')
    codename = request.args.get('codename', '')
    # When linked from product reference with a codename filter, use server-side filter
    # Otherwise load all devices for instant client-side filtering
    if codename:
        devices = db.search_devices(codename=codename)
    else:
        devices = db.search_devices()
    return render_template('devices.html', devices=devices,
                           q=q,
                           selected_category=request.args.get('category', ''),
                           selected_status=request.args.get('status', ''),
                           selected_connectivity=request.args.get('connectivity', ''),
                           selected_location=request.args.get('location', ''),
                           selected_codename=codename)

# ---------------------------------------------------------------------------
# Add device (admin only)
# ---------------------------------------------------------------------------

@app.route('/devices/add', methods=['GET', 'POST'])
@permission_required('devices')
def device_add():
    if request.method == 'POST':
        manufacturer = request.form.get('manufacturer', '').strip()
        model_number = request.form.get('model_number', '').strip()
        category = request.form.get('category', '').strip()
        codename = request.form.get('codename', '').strip()

        # "Other" with custom detail becomes "Other - <detail>"
        if category == 'Other':
            other_detail = request.form.get('other_detail', '').strip()
            if other_detail:
                category = f'Other - {other_detail}'

        if category == 'Printer':
            if not codename:
                flash('Codename is required for printers.', 'error')
                return render_template('device_form.html', device=request.form, is_edit=False)
            variant = request.form.get('variant', '').strip()
            codename_display = f'{codename} {variant}'.strip() if variant else codename
            mfg_model = f'{manufacturer} {model_number}'.strip()
            name = f'{codename_display} ({mfg_model})' if mfg_model else codename_display
        else:
            if not manufacturer:
                flash('Manufacturer is required.', 'error')
                return render_template('device_form.html', device=request.form, is_edit=False)
            name = f'{manufacturer} {model_number}'.strip()

        serial_number = request.form.get('serial_number', '').strip()
        if serial_number:
            existing = db.get_device_by_serial(serial_number)
            if existing:
                flash(f'A device with serial number "{serial_number}" already exists: {existing["name"]}', 'error')
                return render_template('device_form.html', device=request.form, is_edit=False)

        data = {
            'name': name,
            'category': category,
            'manufacturer': request.form.get('manufacturer', ''),
            'model_number': request.form.get('model_number', ''),
            'serial_number': serial_number,
            'connectivity': request.form.get('connectivity', ''),
            'vendor_supplied': 1 if request.form.get('vendor_supplied') == '1' else 0,
            'location': request.form.get('location', ''),
            'notes': request.form.get('notes', ''),
            'codename': codename,
            'variant': request.form.get('variant', '').strip(),
        }
        device_id = db.add_device(data, performed_by=current_username())

        # Generate label
        device = db.get_device(device_id)
        barcode_utils.generate_label(device_id, device['barcode_value'], _label_name(device))

        app_logger.info('Device added: id=%s name="%s" cat="%s" by=%s', device_id, name, category, current_username())
        flash(f'Device "{name}" added successfully.', 'success')
        return redirect(url_for('device_detail', device_id=device_id))

    return render_template('device_form.html', device={}, is_edit=False)

# ---------------------------------------------------------------------------
# Device detail (public)
# ---------------------------------------------------------------------------

@app.route('/devices/<device_id>')
def device_detail(device_id):
    device = db.get_device(device_id)
    if not device:
        app_logger.warning('Device not found: id=%s ip=%s', device_id, request.remote_addr)
        flash('Device not found.', 'error')
        return redirect(url_for('device_list'))

    if not barcode_utils.label_exists(device_id):
        barcode_utils.generate_label(device_id, device['barcode_value'], _label_name(device))
        app_logger.debug('Label generated on-the-fly: id=%s', device_id)

    app_logger.info('Device viewed: id=%s name="%s" ip=%s', device_id, device['name'], request.remote_addr)
    audit = db.get_audit_log(device_id=device_id, limit=50)

    # Look up product reference data if this is a printer with a codename
    prod_ref = None
    if device.get('category') == 'Printer':
        codename = device.get('codename', '')
        if codename:
            refs = db.get_product_reference_by_codename(codename)
            if refs:
                prod_ref = refs[0]

    device_notes = db.get_device_notes(device_id)
    return render_template('device_detail.html', device=device, audit=audit,
                           prod_ref=prod_ref, device_notes=device_notes)

# ---------------------------------------------------------------------------
# Device notes (public — anyone can add)
# ---------------------------------------------------------------------------

@app.route('/devices/<device_id>/notes', methods=['POST'])
def add_device_note(device_id):
    """Add a note to a device. Anyone can add notes."""
    device = db.get_device(device_id)
    if not device:
        flash('Device not found.', 'error')
        return redirect(url_for('device_list'))

    content = request.form.get('note_content', '').strip()
    if not content:
        flash('Note cannot be empty.', 'error')
        return redirect(url_for('device_detail', device_id=device_id))

    if len(content) > 2000:
        flash('Note is too long (max 2000 characters).', 'error')
        return redirect(url_for('device_detail', device_id=device_id))

    author = current_username() if g.user else request.form.get('author_name', '').strip()
    if not author:
        author = 'Anonymous'

    db.add_device_note(device_id, author, content)
    app_logger.info('Note added to device %s by %s', device_id, author)
    flash('Note added.', 'success')
    return redirect(url_for('device_detail', device_id=device_id))


@app.route('/devices/<device_id>/notes/<int:note_id>/delete', methods=['POST'])
@permission_required('notes_delete')
def delete_device_note_route(device_id, note_id):
    """Delete a device note (admin only)."""
    db.delete_device_note(note_id)
    flash('Note deleted.', 'success')
    return redirect(url_for('device_detail', device_id=device_id))

# ---------------------------------------------------------------------------
# Edit device (admin only)
# ---------------------------------------------------------------------------

@app.route('/devices/<device_id>/edit', methods=['GET', 'POST'])
@permission_required('devices')
def device_edit(device_id):
    device = db.get_device(device_id)
    if not device:
        flash('Device not found.', 'error')
        return redirect(url_for('device_list'))

    if request.method == 'POST':
        manufacturer = request.form.get('manufacturer', '').strip()
        model_number = request.form.get('model_number', '').strip()
        category = request.form.get('category', '').strip()
        codename = request.form.get('codename', '').strip()

        # "Other" with custom detail becomes "Other - <detail>"
        if category == 'Other':
            other_detail = request.form.get('other_detail', '').strip()
            if other_detail:
                category = f'Other - {other_detail}'

        if category == 'Printer':
            if not codename:
                flash('Codename is required for printers.', 'error')
                return render_template('device_form.html', device=request.form, is_edit=True, device_id=device_id)
            variant = request.form.get('variant', '').strip()
            codename_display = f'{codename} {variant}'.strip() if variant else codename
            mfg_model = f'{manufacturer} {model_number}'.strip()
            name = f'{codename_display} ({mfg_model})' if mfg_model else codename_display
        else:
            if not manufacturer:
                flash('Manufacturer is required.', 'error')
                return render_template('device_form.html', device=request.form, is_edit=True, device_id=device_id)
            name = f'{manufacturer} {model_number}'.strip()

        data = {
            'name': name,
            'category': category,
            'manufacturer': manufacturer,
            'model_number': model_number,
            'serial_number': request.form.get('serial_number', ''),
            'connectivity': request.form.get('connectivity', ''),
            'vendor_supplied': 1 if request.form.get('vendor_supplied') == '1' else 0,
            'status': request.form.get('status', device['status']),
            'location': request.form.get('location', ''),
            'assigned_to': request.form.get('assigned_to', ''),
            'notes': request.form.get('notes', ''),
            'codename': codename,
            'variant': request.form.get('variant', '').strip(),
        }
        db.update_device(device_id, data, performed_by=current_username())

        updated_device = db.get_device(device_id)
        barcode_utils.generate_label(device_id, device['barcode_value'], _label_name(updated_device))

        app_logger.info('Device updated: id=%s name="%s" cat="%s" by=%s', device_id, name, category, current_username())
        flash(f'Device "{name}" updated successfully.', 'success')
        return redirect(url_for('device_detail', device_id=device_id))

    return render_template('device_form.html', device=device, is_edit=True, device_id=device_id)

# ---------------------------------------------------------------------------
# Retire device (admin only)
# ---------------------------------------------------------------------------

@app.route('/devices/<device_id>/retire', methods=['POST'])
@permission_required('retire')
def device_retire(device_id):
    db.retire_device(device_id, performed_by=current_username())
    app_logger.info('Device retired: id=%s by=%s', device_id, current_username())
    flash('Device retired successfully.', 'success')
    return redirect(url_for('device_list'))

# ---------------------------------------------------------------------------
# Check out / Check in (admin only)
# ---------------------------------------------------------------------------

@app.route('/devices/<device_id>/checkout', methods=['POST'])
@permission_required('devices')
def device_checkout(device_id):
    assigned_to = request.form.get('assigned_to', '').strip()
    if not assigned_to:
        flash('Please enter who is checking out this device.', 'error')
        return redirect(url_for('device_detail', device_id=device_id))

    db.checkout_device(device_id, assigned_to, performed_by=current_username())
    app_logger.info('Device checked out: id=%s to=%s by=%s', device_id, assigned_to, current_username())
    flash(f'Device checked out to {assigned_to}.', 'success')
    return redirect(url_for('device_detail', device_id=device_id))


@app.route('/devices/<device_id>/checkin', methods=['POST'])
@permission_required('devices')
def device_checkin(device_id):
    db.checkin_device(device_id, performed_by=current_username())
    app_logger.info('Device checked in: id=%s by=%s', device_id, current_username())
    flash('Device checked in successfully.', 'success')
    return redirect(url_for('device_detail', device_id=device_id))

# ---------------------------------------------------------------------------
# Label serving and label sheet generation
# ---------------------------------------------------------------------------

def _label_name(device):
    """Return the name to display on a device label. Printers use codename + variant only."""
    if device.get('category') == 'Printer' and device.get('codename'):
        variant = device.get('variant', '')
        return f"{device['codename']} {variant}".strip() if variant else device['codename']
    return device['name']


@app.route('/labels/<device_id>.png')
def serve_label(device_id):
    """Serve a label PNG, always regenerating to ensure it's current."""
    device = db.get_device(device_id)
    if not device:
        app_logger.warning('Label requested for unknown device: id=%s', device_id)
        return 'Device not found', 404
    barcode_utils.generate_label(device_id, device['barcode_value'], _label_name(device))
    path = barcode_utils.get_label_path(device_id)
    return send_file(path, mimetype='image/png')


@app.route('/labels/<device_id>.pdf')
def serve_label_pdf(device_id):
    """Serve a label as a PDF matching 3.5x1.5 inch landscape label stock."""
    device = db.get_device(device_id)
    if not device:
        return 'Device not found', 404
    # Regenerate only if label is missing or stale
    path = barcode_utils.get_label_path(device_id)
    if not os.path.isfile(path) or os.path.getmtime(path) < datetime.fromisoformat(device['updated_at']).timestamp():
        barcode_utils.generate_label(device_id, device['barcode_value'], _label_name(device))

    # Landscape PNG (1050x450 = 3.5x1.5" at 300 DPI)
    import zlib
    from PIL import Image
    img = Image.open(path).convert('RGB')
    img_w, img_h = img.size
    # Use FlateDecode (lossless) instead of JPEG to preserve crisp barcode edges
    raw_data = img.tobytes()
    img_data = zlib.compress(raw_data, 9)

    # Landscape page matching label stock: 3.5" wide x 1.5" tall
    page_w = 252   # 3.5 * 72
    page_h = 108   # 1.5 * 72

    xref_offsets = []
    pdf = io.BytesIO()

    pdf.write(b'%PDF-1.4\n')

    xref_offsets.append(pdf.tell())
    pdf.write(b'1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n')

    xref_offsets.append(pdf.tell())
    pdf.write(b'2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n')

    xref_offsets.append(pdf.tell())
    pdf.write(f'3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page_w} {page_h}] /Contents 5 0 R /Resources << /XObject << /Img 4 0 R >> >> >>\nendobj\n'.encode())

    xref_offsets.append(pdf.tell())
    pdf.write(f'4 0 obj\n<< /Type /XObject /Subtype /Image /Width {img_w} /Height {img_h} /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode /Length {len(img_data)} >>\nstream\n'.encode())
    pdf.write(img_data)
    pdf.write(b'\nendstream\nendobj\n')

    # Simple scale — landscape image on landscape page, no rotation needed
    content = f'q {page_w} 0 0 {page_h} 0 0 cm /Img Do Q'.encode()
    xref_offsets.append(pdf.tell())
    pdf.write(f'5 0 obj\n<< /Length {len(content)} >>\nstream\n'.encode())
    pdf.write(content)
    pdf.write(b'\nendstream\nendobj\n')

    # Xref table
    xref_start = pdf.tell()
    pdf.write(b'xref\n')
    pdf.write(f'0 {len(xref_offsets) + 1}\n'.encode())
    pdf.write(b'0000000000 65535 f \n')
    for offset in xref_offsets:
        pdf.write(f'{offset:010d} 00000 n \n'.encode())

    # Trailer
    pdf.write(f'trailer\n<< /Size {len(xref_offsets) + 1} /Root 1 0 R >>\n'.encode())
    pdf.write(b'startxref\n')
    pdf.write(f'{xref_start}\n'.encode())
    pdf.write(b'%%EOF\n')

    pdf.seek(0)
    return send_file(pdf, mimetype='application/pdf',
                     download_name=f'{device_id}_label.pdf')


@app.route('/labels/sheet', methods=['POST'])
@permission_required('devices')
def label_sheet():
    """Generate and download a printable sheet of labels for selected devices."""
    device_ids = request.form.getlist('device_ids')
    if not device_ids:
        flash('No devices selected for label printing.', 'error')
        return redirect(url_for('device_list'))

    devices = []
    for did in device_ids:
        d = db.get_device(did)
        if d:
            devices.append(d)

    if not devices:
        flash('No valid devices found.', 'error')
        return redirect(url_for('device_list'))

    sheet = barcode_utils.generate_label_sheet(devices)
    buffer = io.BytesIO()
    sheet.save(buffer, format='PNG')
    buffer.seek(0)

    app_logger.info('Label sheet generated: %d devices by=%s', len(devices), current_username())
    return send_file(buffer, mimetype='image/png', as_attachment=True,
                     download_name='label_sheet.png')

# ---------------------------------------------------------------------------
# Scanner page and API
# ---------------------------------------------------------------------------

@app.route('/scan')
def scan_page():
    app_logger.debug('Scan page accessed: ip=%s', request.remote_addr)
    return render_template('scan.html')


@app.route('/api/lookup')
def api_lookup():
    """JSON API for barcode scanner lookup. Case-insensitive."""
    barcode = request.args.get('barcode', '').strip()
    if not barcode:
        app_logger.warning('Barcode lookup: empty barcode ip=%s', request.remote_addr)
        return jsonify({'found': False, 'error': 'No barcode provided'}), 400

    device = db.get_device_by_barcode(barcode)
    if device:
        app_logger.info('Barcode scan: barcode=%s found="%s" (id=%s) ip=%s', barcode, device['name'], device['device_id'], request.remote_addr)
        return jsonify({
            'found': True,
            'device_id': device['device_id'],
            'name': device['name'],
            'status': device['status'],
            'assigned_to': device['assigned_to'],
            'location': device['location'],
        })
    else:
        app_logger.info('Barcode scan: barcode=%s not_found ip=%s', barcode, request.remote_addr)
        return jsonify({'found': False}), 404

# ---------------------------------------------------------------------------
# Export (public) — CSV and Excel
# ---------------------------------------------------------------------------

def _get_export_devices():
    """Gather devices based on export filter query params."""
    category = request.args.get('category', '')
    status = request.args.get('status', '')
    connectivity = request.args.get('connectivity', '')
    location = request.args.get('location', '')
    q = request.args.get('q', '')
    include_retired = request.args.get('include_retired') == '1'

    if category or status or connectivity or location or q:
        if include_retired and not status:
            non_retired = db.search_devices(query=q, category=category, connectivity=connectivity, location=location)
            retired = db.search_devices(query=q, category=category, status='retired', connectivity=connectivity, location=location)
            seen = set()
            devices = []
            for d in non_retired + retired:
                if d['device_id'] not in seen:
                    seen.add(d['device_id'])
                    devices.append(d)
        else:
            devices = db.search_devices(
                query=q, category=category,
                status=status if status else '',
                connectivity=connectivity, location=location,
            )
    else:
        devices = db.get_all_devices(include_retired=include_retired)
    return devices

EXPORT_FIELDS = ['device_id', 'barcode_value', 'name', 'category', 'manufacturer',
                 'model_number', 'serial_number', 'connectivity', 'vendor_supplied',
                 'status', 'location', 'assigned_to', 'notes', 'codename', 'variant',
                 'created_at', 'updated_at']

EXPORT_HEADERS = {
    'device_id': 'Device ID',
    'barcode_value': 'Barcode',
    'name': 'Name',
    'category': 'Category',
    'manufacturer': 'Manufacturer',
    'model_number': 'Model Number',
    'serial_number': 'Serial Number',
    'connectivity': 'Connectivity Type/Version',
    'vendor_supplied': 'Source',
    'status': 'Status',
    'location': 'Location',
    'assigned_to': 'Assigned To',
    'notes': 'Notes',
    'codename': 'Codename',
    'variant': 'Variant',
    'created_at': 'Created',
    'updated_at': 'Updated',
}

@app.route('/export')
def export_csv():
    """Export devices to CSV."""
    devices = _get_export_devices()
    app_logger.info('CSV export: %d devices ip=%s', len(devices), request.remote_addr)

    output = io.StringIO()
    headers = [EXPORT_HEADERS.get(f, f) for f in EXPORT_FIELDS]
    writer = csv.writer(output)
    writer.writerow(headers)
    for d in devices:
        row = []
        for f in EXPORT_FIELDS:
            val = d.get(f, '')
            if f == 'vendor_supplied':
                val = 'Vendor Supplied' if val else 'HP Owned'
            row.append(val if val is not None else '')
        writer.writerow(row)

    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=inventory_export.csv'}
    )


@app.route('/export/xlsx')
def export_xlsx():
    """Export devices to Excel (.xlsx)."""
    devices = _get_export_devices()
    app_logger.info('Excel export: %d devices ip=%s', len(devices), request.remote_addr)

    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment
    except ImportError:
        flash('openpyxl is required for Excel export. Install with: pip install openpyxl', 'error')
        return redirect(url_for('device_list'))

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Inventory'

    # Header row
    headers = [EXPORT_HEADERS.get(f, f) for f in EXPORT_FIELDS]
    ws.append(headers)
    header_font = Font(bold=True, size=11)
    header_fill = PatternFill(start_color='E2EFDA', end_color='E2EFDA', fill_type='solid')
    for cell in ws[1]:
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal='center')

    # Data rows
    for d in devices:
        row = []
        for f in EXPORT_FIELDS:
            val = d.get(f, '')
            if f == 'vendor_supplied':
                val = 'Vendor Supplied' if val else 'HP Owned'
            row.append(val if val is not None else '')
        ws.append(row)

    # Auto-width columns
    for col in ws.columns:
        max_len = max((len(str(cell.value or '')) for cell in col), default=10)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 2, 40)

    # Freeze header row
    ws.freeze_panes = 'A2'

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={'Content-Disposition': 'attachment; filename=inventory_export.xlsx'}
    )


# ---------------------------------------------------------------------------
# User management (admin only)
# ---------------------------------------------------------------------------

@app.route('/users')
@permission_required('users')
def user_list():
    users = db.get_all_users()
    return render_template('users.html', users=users)


@app.route('/users/add', methods=['GET', 'POST'])
@permission_required('users')
def user_add():
    if request.method == 'POST':
        username = request.form.get('username', '').strip().lower()
        password = request.form.get('password', '')
        role = request.form.get('role', 'custom')
        display_name = request.form.get('display_name', '').strip()

        # Collect permissions from checkboxes (only for custom role)
        permissions = None
        if role == 'custom':
            permissions = request.form.getlist('permissions')

        if not username or not password:
            flash('Username and password are required.', 'error')
            return render_template('user_form.html', user={}, is_edit=False,
                                   assignable_permissions=ASSIGNABLE_PERMISSIONS)

        if len(password) < 4:
            flash('Password must be at least 4 characters.', 'error')
            return render_template('user_form.html', user=request.form, is_edit=False,
                                   assignable_permissions=ASSIGNABLE_PERMISSIONS)

        try:
            db.create_user(username, password, role, display_name, permissions=permissions)
            app_logger.info('User created: username=%s role=%s permissions=%s by=%s',
                            username, role, permissions, current_username())
            flash(f'User "{username}" created successfully.', 'success')
            return redirect(url_for('user_list'))
        except ValueError as e:
            flash(str(e), 'error')
            return render_template('user_form.html', user=request.form, is_edit=False,
                                   assignable_permissions=ASSIGNABLE_PERMISSIONS)

    return render_template('user_form.html', user={}, is_edit=False,
                           assignable_permissions=ASSIGNABLE_PERMISSIONS)


@app.route('/users/<int:user_id>/edit', methods=['GET', 'POST'])
@permission_required('users')
def user_edit(user_id):
    user = db.get_user(user_id)
    if not user:
        flash('User not found.', 'error')
        return redirect(url_for('user_list'))

    if request.method == 'POST':
        role = request.form.get('role', user['role'])
        data = {
            'display_name': request.form.get('display_name', '').strip(),
            'role': role,
        }
        if role == 'custom':
            data['permissions'] = request.form.getlist('permissions')
        else:
            data['permissions'] = None  # admin uses role defaults

        password = request.form.get('password', '').strip()
        if password:
            if len(password) < 4:
                flash('Password must be at least 4 characters.', 'error')
                return render_template('user_form.html', user=user, is_edit=True,
                                       assignable_permissions=ASSIGNABLE_PERMISSIONS)
            data['password'] = password

        db.update_user(user_id, data)
        app_logger.info('User updated: username=%s role=%s by=%s',
                        user['username'], role, current_username())
        flash(f'User "{user["username"]}" updated.', 'success')
        return redirect(url_for('user_list'))

    return render_template('user_form.html', user=user, is_edit=True,
                           assignable_permissions=ASSIGNABLE_PERMISSIONS)


@app.route('/users/<int:user_id>/delete', methods=['POST'])
@permission_required('users')
def user_delete(user_id):
    try:
        db.delete_user(user_id)
        app_logger.info('User deleted: user_id=%s by=%s', user_id, current_username())
        flash('User deleted.', 'success')
    except ValueError as e:
        app_logger.warning('User delete failed: user_id=%s error=%s', user_id, e)
        flash(str(e), 'error')
    return redirect(url_for('user_list'))

# ---------------------------------------------------------------------------
# Application Log viewer (admin only)
# ---------------------------------------------------------------------------

@app.route('/logs')
@permission_required('logs')
def app_logs():
    """View application log entries with pagination. Most recent first."""
    per_page = 200
    page = max(1, request.args.get('page', 1, type=int))

    lines = []
    # Read rotated backup first (older), then current log (newer)
    for log_path in [LOG_FILE + '.1', LOG_FILE]:
        try:
            with open(log_path, 'r') as f:
                lines.extend(f.readlines())
        except FileNotFoundError:
            pass

    # Parse into structured entries, most recent first
    all_entries = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        parts = line.split(' | ', 2)
        if len(parts) == 3:
            all_entries.append({
                'timestamp': parts[0],
                'level': parts[1].strip(),
                'message': parts[2],
            })
        else:
            all_entries.append({
                'timestamp': '',
                'level': '',
                'message': line,
            })

    total = len(all_entries)
    total_pages = max(1, (total + per_page - 1) // per_page)
    start = (page - 1) * per_page
    entries = all_entries[start:start + per_page]

    log_config = _load_log_config()
    log_file_size = sum(os.path.getsize(p) for p in [LOG_FILE, LOG_FILE + '.1'] if os.path.exists(p))
    return render_template('app_log.html', entries=entries, log_config=log_config,
                           log_file_size=log_file_size, page=page, total_pages=total_pages)


@app.route('/logs/clear', methods=['POST'])
@permission_required('logs')
def clear_logs():
    """Clear the application log file."""
    try:
        with open(LOG_FILE, 'w') as f:
            f.write('')
        # Remove rotated backup file too
        backup_log = LOG_FILE + '.1'
        if os.path.exists(backup_log):
            os.remove(backup_log)
        app_logger.info('Application log cleared by %s', current_username())
        flash('Application log cleared.', 'success')
    except Exception as e:
        app_logger.error('Log clear failed: %s by=%s\nTraceback:\n%s', e, current_username(), traceback.format_exc())
        flash(f'Error clearing log: {e}', 'error')
    return redirect(url_for('app_logs'))


@app.route('/logs/export')
@permission_required('logs')
def export_logs():
    """Export the application log as a downloadable .log file."""
    lines = []
    for log_path in [LOG_FILE + '.1', LOG_FILE]:
        try:
            with open(log_path, 'r') as f:
                lines.extend(f.readlines())
        except FileNotFoundError:
            pass

    content = ''.join(lines)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    return Response(
        content,
        mimetype='text/plain',
        headers={'Content-Disposition': f'attachment; filename=app_log_{timestamp}.log'}
    )


@app.route('/logs/config', methods=['POST'])
@permission_required('logs')
def update_log_config():
    """Update application log max size."""
    try:
        max_size_mb = float(request.form.get('max_size_mb', 2))
        if max_size_mb < 0.1:
            max_size_mb = 0.1
        if max_size_mb > 100:
            max_size_mb = 100
    except (ValueError, TypeError):
        max_size_mb = 2

    config = {'max_size_mb': max_size_mb}
    _save_log_config(config)
    _reconfigure_log_handler(max_size_mb)
    app_logger.info('Log max size changed to %.1f MB by %s', max_size_mb, current_username())
    flash(f'Log max size set to {max_size_mb} MB. Log will overwrite oldest entries when this limit is reached.', 'success')
    return redirect(url_for('app_logs'))

# ---------------------------------------------------------------------------
# Change own password (any logged-in user)
# ---------------------------------------------------------------------------

@app.route('/account', methods=['GET', 'POST'])
@login_required
def account():
    if request.method == 'POST':
        current_pw = request.form.get('current_password', '')
        new_pw = request.form.get('new_password', '')
        confirm_pw = request.form.get('confirm_password', '')

        users = db.get_all_users() if has_permission('users') else []
        server_config = _load_server_config()

        # Verify current password
        user = db.authenticate_user(g.user['username'], current_pw)
        if not user:
            flash('Current password is incorrect.', 'error')
            return render_template('account.html', users=users, server_config=server_config)

        if len(new_pw) < 4:
            flash('New password must be at least 4 characters.', 'error')
            return render_template('account.html', users=users, server_config=server_config)

        if new_pw != confirm_pw:
            flash('New passwords do not match.', 'error')
            return render_template('account.html', users=users, server_config=server_config)

        db.update_user(g.user['user_id'], {'password': new_pw})
        app_logger.info('Password changed: user=%s', g.user['username'])
        flash('Password changed successfully.', 'success')
        return redirect(url_for('account'))

    users = db.get_all_users() if has_permission('users') else []
    server_config = _load_server_config()
    return render_template('account.html', users=users, server_config=server_config)


@app.route('/settings/server', methods=['POST'])
@permission_required('settings')
def save_server_config():
    try:
        port = int(request.form.get('port', 8080))
        if port < 1 or port > 65535:
            flash('Port must be between 1 and 65535.', 'error')
            return redirect(url_for('account'))
    except (ValueError, TypeError):
        flash('Invalid port number.', 'error')
        return redirect(url_for('account'))

    config = _load_server_config()
    config['port'] = port
    _save_server_config(config)
    app_logger.info('Server config updated: port=%d by user=%s', port, g.user['username'])
    flash('Server settings saved. Restart the application for changes to take effect.', 'success')
    return redirect(url_for('account'))


# ---------------------------------------------------------------------------
# Database backup (admin only)
# ---------------------------------------------------------------------------

import threading

# ---------------------------------------------------------------------------
# Persistent backup scheduler — single thread that wakes every 60 seconds
# and checks what tasks are due. Replaces fragile threading.Timer chains that
# silently died when a daemon thread was killed or an exception escaped.
# ---------------------------------------------------------------------------

_scheduler_thread = None
_scheduler_stop = threading.Event()

# Next-run timestamps (None = disabled). Protected by _scheduler_lock.
_scheduler_lock = threading.Lock()
_next_backup_time = None
_next_git_push_time = None
_next_prune_time = None
_next_verify_time = None

# Consecutive failure counters for retry backoff (max 3 retries then normal interval)
_RETRY_DELAYS_MIN = [2, 5, 15]  # minutes to wait before retry 1, 2, 3
_fail_count = {'backup': 0, 'git_push': 0, 'prune': 0}


def _scheduler_loop():
    """Persistent loop: wake every 60s, run any overdue tasks."""
    while not _scheduler_stop.is_set():
        try:
            now = datetime.now()

            with _scheduler_lock:
                run_backup = _next_backup_time is not None and now >= _next_backup_time
                run_git = _next_git_push_time is not None and now >= _next_git_push_time
                run_prune = _next_prune_time is not None and now >= _next_prune_time
                run_verify = _next_verify_time is not None and now >= _next_verify_time

            if run_backup:
                _exec_scheduled_backup()
            if run_git:
                _exec_scheduled_git_push()
            if run_prune:
                _exec_scheduled_prune()
            if run_verify:
                _exec_scheduled_verify()
        except Exception:
            app_logger.error('Scheduler loop error (will continue):\n%s', traceback.format_exc())

        # Sleep in 5-second chunks so stop events are responsive
        for _ in range(12):
            if _scheduler_stop.is_set():
                break
            _scheduler_stop.wait(5)


def _retry_or_reschedule(task_name, start_func, stop_func, config_enabled_key, config_interval_key):
    """Handle retry backoff on failure or normal reschedule on success."""
    config = db._get_backup_config()
    if not config.get(config_enabled_key):
        stop_func()
        _fail_count[task_name] = 0
        return
    fails = _fail_count[task_name]
    if fails > 0 and fails <= len(_RETRY_DELAYS_MIN):
        retry_minutes = _RETRY_DELAYS_MIN[fails - 1]
        app_logger.warning('Scheduled %s: retry %d/%d in %d minutes',
                           task_name, fails, len(_RETRY_DELAYS_MIN), retry_minutes)
        start_func(retry_minutes / 60.0)
    else:
        # Normal interval (either success or retries exhausted)
        if fails > len(_RETRY_DELAYS_MIN):
            app_logger.error('Scheduled %s: all %d retries exhausted, resuming normal interval',
                             task_name, len(_RETRY_DELAYS_MIN))
            _fail_count[task_name] = 0
        start_func(config[config_interval_key])


def _exec_scheduled_backup():
    """Run backup and reschedule from latest config, with retry on failure."""
    try:
        result = db.backup_database(performed_by='scheduled')
        if result.get('skipped'):
            app_logger.info('Scheduled backup skipped — database unchanged')
        else:
            app_logger.info('Scheduled backup completed: %s (%d bytes, pruned=%d)',
                            result['filename'], result['size'], result['pruned'])
        _fail_count['backup'] = 0
    except Exception as e:
        app_logger.error('Scheduled backup failed: %s\nTraceback:\n%s', e, traceback.format_exc())
        _fail_count['backup'] += 1
    _retry_or_reschedule('backup', _start_backup_timer, _stop_backup_timer,
                         'backup_enabled', 'backup_interval_hours')


def _exec_scheduled_git_push():
    """Run git push and reschedule from latest config, with retry on failure."""
    try:
        result = db.push_backups_to_git()
        if result.get('skipped'):
            app_logger.info('Scheduled git push skipped — backup zip unchanged')
        else:
            app_logger.info('Scheduled git push completed: %d files to %s',
                            result['files_pushed'], result['pushed_to'])
        _fail_count['git_push'] = 0
    except Exception as e:
        app_logger.error('Scheduled git push failed: %s\nTraceback:\n%s', e, traceback.format_exc())
        _fail_count['git_push'] += 1
    _retry_or_reschedule('git_push', _start_git_push_timer, _stop_git_push_timer,
                         'git_enabled', 'git_push_interval_hours')


def _exec_scheduled_prune():
    """Run prune and reschedule from latest config, with retry on failure."""
    try:
        config = db._get_backup_config()
        pruned = db._smart_prune_backups(config['max_backups'])
        if pruned:
            app_logger.info('Scheduled prune completed: removed %d old auto-backups', pruned)
        _fail_count['prune'] = 0
    except Exception as e:
        app_logger.error('Scheduled prune failed: %s\nTraceback:\n%s', e, traceback.format_exc())
        _fail_count['prune'] += 1
    _retry_or_reschedule('prune', _start_prune_timer, _stop_prune_timer,
                         'prune_enabled', 'prune_interval_hours')


def _exec_scheduled_verify():
    """Run backup verification and reschedule."""
    try:
        result = db.verify_backup(rotate=True)
        if result['ok']:
            app_logger.info('Backup verification passed: %s', result['filename'])
        else:
            app_logger.error('BACKUP VERIFICATION FAILED: %s — %s',
                             result['filename'], result['result'])
    except Exception as e:
        app_logger.error('Backup verification error: %s\nTraceback:\n%s', e, traceback.format_exc())
    # Re-arm: verify every 24 hours
    with _scheduler_lock:
        global _next_verify_time
        _next_verify_time = datetime.now() + timedelta(hours=24)


def _start_backup_timer(interval_hours):
    """Schedule the next backup after interval_hours from now."""
    global _next_backup_time
    seconds = max(interval_hours * 3600, 300)  # Minimum 5 minutes
    with _scheduler_lock:
        _next_backup_time = datetime.now() + timedelta(seconds=seconds)
    app_logger.info('Backup scheduler armed: next backup in %s hours', interval_hours)


def _stop_backup_timer():
    """Disable scheduled backups."""
    global _next_backup_time
    with _scheduler_lock:
        _next_backup_time = None


def _start_git_push_timer(interval_hours):
    """Schedule the next git push after interval_hours from now."""
    global _next_git_push_time
    seconds = max(interval_hours * 3600, 300)
    with _scheduler_lock:
        _next_git_push_time = datetime.now() + timedelta(seconds=seconds)
    app_logger.info('Git push scheduler armed: next push in %s hours', interval_hours)


def _stop_git_push_timer():
    """Disable scheduled git pushes."""
    global _next_git_push_time
    with _scheduler_lock:
        _next_git_push_time = None


def _start_prune_timer(interval_hours):
    """Schedule the next prune after interval_hours from now."""
    global _next_prune_time
    seconds = max(interval_hours * 3600, 300)
    with _scheduler_lock:
        _next_prune_time = datetime.now() + timedelta(seconds=seconds)
    app_logger.info('Prune scheduler armed: next prune in %s hours', interval_hours)


def _stop_prune_timer():
    """Disable scheduled prunes."""
    global _next_prune_time
    with _scheduler_lock:
        _next_prune_time = None


def _ensure_scheduler_running():
    """Start the scheduler thread if it isn't already alive."""
    global _scheduler_thread
    if _scheduler_thread is not None and _scheduler_thread.is_alive():
        return
    _scheduler_stop.clear()
    _scheduler_thread = threading.Thread(target=_scheduler_loop, name='backup-scheduler', daemon=True)
    _scheduler_thread.start()
    app_logger.info('Backup scheduler thread started')


# Restore schedules on startup — if a task is overdue, run it soon instead of
# waiting a full interval (prevents persistent "overdue" after restart).
_startup_config = db._get_backup_config()
if _startup_config.get('backup_enabled'):
    _last_bk = _startup_config.get('last_backup', '')
    _bk_interval = _startup_config['backup_interval_hours']
    if _last_bk:
        _bk_age_hours = (datetime.now() - datetime.strptime(_last_bk, '%Y-%m-%d %H:%M:%S')).total_seconds() / 3600
        if _bk_age_hours > _bk_interval:
            # Only treat as truly overdue if the database has changed since last backup
            _current_hash = db._compute_db_hash()
            _last_hash = _startup_config.get('last_backup_hash', '')
            if _current_hash and _current_hash == _last_hash:
                app_logger.info('Backup age (%.1f hours) exceeds interval but database unchanged — scheduling at normal interval', _bk_age_hours)
                _start_backup_timer(_bk_interval)
            else:
                app_logger.info('Backup overdue on startup (%.1f hours old, database changed), scheduling in 30 seconds', _bk_age_hours)
                _start_backup_timer(30 / 3600)  # ~30 seconds
        else:
            _start_backup_timer(_bk_interval - _bk_age_hours)
    else:
        _start_backup_timer(_bk_interval)
if _startup_config.get('git_enabled') and _startup_config.get('git_repo'):
    _last_gp = _startup_config.get('last_git_push', '')
    _gp_interval = _startup_config['git_push_interval_hours']
    if _last_gp:
        _gp_age_hours = (datetime.now() - datetime.strptime(_last_gp, '%Y-%m-%d %H:%M:%S')).total_seconds() / 3600
        if _gp_age_hours > _gp_interval:
            _start_git_push_timer(30 / 3600)
        else:
            _start_git_push_timer(_gp_interval - _gp_age_hours)
    else:
        _start_git_push_timer(_gp_interval)
if _startup_config.get('prune_enabled'):
    _start_prune_timer(_startup_config['prune_interval_hours'])

# Verification runs every 24 hours regardless of config
_next_verify_time = datetime.now() + timedelta(hours=24)

# Start the single persistent scheduler thread
_ensure_scheduler_running()


@app.route('/backups')
@permission_required('backups')
def backup_list():
    """View backup management page."""
    _ensure_scheduler_running()  # Self-heal if scheduler died
    backups = db.list_backups()
    config = db._get_backup_config()
    with _scheduler_lock:
        next_backup = _next_backup_time.strftime('%Y-%m-%d %H:%M:%S') if _next_backup_time else None
        next_push = _next_git_push_time.strftime('%Y-%m-%d %H:%M:%S') if _next_git_push_time else None
        next_prune = _next_prune_time.strftime('%Y-%m-%d %H:%M:%S') if _next_prune_time else None
    scheduler_alive = _scheduler_thread is not None and _scheduler_thread.is_alive()
    health = db.get_backup_health()
    # Show verification failure as an error flash (only when failed)
    if config.get('last_verify_time') and not config.get('last_verify_ok'):
        verify_msg = f'Backup verification FAILED: {config.get("last_verify_file", "unknown")}'
        if config.get('last_verify_result'):
            verify_msg += f' — {config["last_verify_result"]}'
        flash(verify_msg, 'error')
    return render_template('backups.html', backups=backups, config=config,
                           next_backup_time=next_backup, next_git_push_time=next_push,
                           next_prune_time=next_prune, scheduler_alive=scheduler_alive,
                           backup_health=health)


@app.route('/backups/create', methods=['POST'])
@permission_required('backups')
def backup_create():
    """Trigger a manual database backup."""
    try:
        result = db.backup_database(performed_by=current_username(), manual=True)
        app_logger.info('Manual backup created: %s (%d bytes, pruned=%d) by=%s',
                        result['filename'], result['size'], result['pruned'],
                        current_username())
        flash(f'Backup created: {result["filename"]}', 'success')
    except Exception as e:
        app_logger.error('Manual backup failed: %s by=%s\nTraceback:\n%s', e, current_username(), traceback.format_exc())
        flash(f'Backup failed: {e}', 'error')
    return redirect(url_for('backup_list'))


@app.route('/backups/upload', methods=['POST'])
@permission_required('backups')
def backup_upload():
    """Restore database from an uploaded .db file."""
    MAX_UPLOAD_MB = 500
    file = request.files.get('backup_file')
    if not file or not file.filename:
        flash('No file selected.', 'error')
        return redirect(url_for('backup_list'))
    if not file.filename.endswith('.db'):
        flash('Invalid file type. Please upload a .db file.', 'error')
        return redirect(url_for('backup_list'))
    # Validate SQLite magic bytes before saving to disk
    header = file.read(16)
    file.seek(0)
    if header[:16] != b'SQLite format 3\x00':
        flash('Invalid file: not a valid SQLite database.', 'error')
        return redirect(url_for('backup_list'))
    # Check file size (read content length or measure stream)
    file.seek(0, 2)  # seek to end
    file_size = file.tell()
    file.seek(0)
    if file_size > MAX_UPLOAD_MB * 1024 * 1024:
        flash(f'File too large ({file_size // (1024*1024)} MB). Maximum is {MAX_UPLOAD_MB} MB.', 'error')
        return redirect(url_for('backup_list'))
    try:
        # Save uploaded file to backup dir
        backup_dir = db._get_backup_dir()
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        dest_filename = f'manual_backup_{timestamp}_uploaded.db'
        dest_path = os.path.join(backup_dir, dest_filename)
        file.save(dest_path)

        # Run compatibility check before restore
        compat = db.validate_backup_compatibility(dest_path)
        if not compat['compatible']:
            error_detail = '; '.join(compat['errors'])
            flash(f'Backup is not compatible: {error_detail}', 'error')
            try:
                os.remove(dest_path)
            except OSError:
                pass
            return redirect(url_for('backup_list'))

        # Restore from the uploaded file
        result = db.restore_database(dest_filename)
        app_logger.info('Database restored from upload: %s (safety: %s) by=%s',
                        dest_filename, result['safety_backup'], current_username())
        msg = f'Database restored from uploaded file. Safety backup: {result["safety_backup"]}'
        if result.get('warnings'):
            msg += f' ({len(result["warnings"])} compatibility warning{"s" if len(result["warnings"]) != 1 else ""})'
        flash(msg, 'success')
        for w in result.get('warnings', []):
            flash(w, 'warning')
    except ValueError as e:
        app_logger.error('Upload restore failed: %s by=%s\nTraceback:\n%s', e, current_username(), traceback.format_exc())
        flash(f'Restore failed: {e}', 'error')
    except Exception as e:
        app_logger.error('Upload restore failed: %s by=%s\nTraceback:\n%s', e, current_username(), traceback.format_exc())
        flash(f'Restore failed: {e}', 'error')
    return redirect(url_for('backup_list'))



@app.route('/backups/config', methods=['POST'])
@permission_required('backups')
def backup_config():
    """Update all backup configuration settings."""
    config = db._get_backup_config()

    # Local backup settings
    backup_dir = request.form.get('backup_dir', '').strip()
    if backup_dir:
        if not os.path.isabs(backup_dir):
            flash('Backup directory must be an absolute path.', 'error')
            return redirect(url_for('backup_list'))
        os.makedirs(backup_dir, exist_ok=True)
        if not os.access(backup_dir, os.W_OK):
            flash(f'Backup directory is not writable: {backup_dir}', 'error')
            return redirect(url_for('backup_list'))
        config['backup_dir'] = backup_dir

    try:
        config['max_backups'] = max(1, int(request.form.get('max_backups', 5)))
    except (ValueError, TypeError):
        config['max_backups'] = 5

    config['backup_enabled'] = '1' in request.form.getlist('backup_enabled')
    try:
        config['backup_interval_hours'] = max(0.1, float(request.form.get('backup_interval_hours', 24)))
    except (ValueError, TypeError):
        config['backup_interval_hours'] = 24

    # Prune settings
    config['prune_enabled'] = '1' in request.form.getlist('prune_enabled')
    try:
        config['prune_interval_hours'] = max(0.1, float(request.form.get('prune_interval_hours', 24)))
    except (ValueError, TypeError):
        config['prune_interval_hours'] = 24

    # Git push settings
    config['git_enabled'] = '1' in request.form.getlist('git_enabled')
    config['git_repo'] = request.form.get('git_repo', '').strip()
    config['git_branch'] = request.form.get('git_branch', 'backups').strip() or 'backups'
    config['git_token'] = request.form.get('git_token', '').strip()
    try:
        config['git_push_interval_hours'] = max(0.1, float(request.form.get('git_push_interval_hours', 24)))
    except (ValueError, TypeError):
        config['git_push_interval_hours'] = 24

    db.save_backup_config(config)

    # Manage backup timer
    if config['backup_enabled']:
        _start_backup_timer(config['backup_interval_hours'])
        app_logger.info('Backup schedule enabled: every %s hours by=%s',
                        config['backup_interval_hours'], current_username())
    else:
        _stop_backup_timer()

    # Manage git push timer
    if config['git_enabled'] and config['git_repo']:
        _start_git_push_timer(config['git_push_interval_hours'])
        app_logger.info('Git push schedule enabled: every %s hours to %s by=%s',
                        config['git_push_interval_hours'], config['git_repo'], current_username())
    else:
        _stop_git_push_timer()

    # Manage prune timer
    if config['prune_enabled']:
        _start_prune_timer(config['prune_interval_hours'])
        app_logger.info('Prune schedule enabled: every %s hours by=%s',
                        config['prune_interval_hours'], current_username())
    else:
        _stop_prune_timer()

    # Ensure the scheduler thread is alive (recovers if it died)
    _ensure_scheduler_running()

    app_logger.info('Backup config updated by=%s', current_username())
    flash('Backup configuration saved.', 'success')
    return redirect(url_for('backup_list'))


@app.route('/backups/config/reset', methods=['POST'])
@permission_required('backups')
def backup_config_reset():
    """Reset backup configuration to factory defaults (preserves git credentials)."""
    current = db._get_backup_config()
    defaults = db.get_default_backup_config()
    # Preserve git credentials and repo settings — user shouldn't have to re-enter these
    defaults['git_repo'] = current.get('git_repo', '')
    defaults['git_branch'] = current.get('git_branch', 'backups')
    defaults['git_token'] = current.get('git_token', '')
    # Preserve timestamps
    defaults['last_backup'] = current.get('last_backup', '')
    defaults['last_git_push'] = current.get('last_git_push', '')
    defaults['last_backup_hash'] = current.get('last_backup_hash', '')
    db.save_backup_config(defaults)
    _stop_backup_timer()
    _stop_git_push_timer()
    _stop_prune_timer()
    app_logger.info('Backup config reset to defaults by=%s', current_username())
    flash('Backup configuration reset to defaults.', 'success')
    return redirect(url_for('backup_list'))


@app.route('/backups/push', methods=['POST'])
@permission_required('backups')
def backup_push_git():
    """Manually trigger a git push of backup bundle."""
    try:
        result = db.push_backups_to_git()
        app_logger.info('Manual git push: %d files to %s by=%s',
                        result['files_pushed'], result['pushed_to'], current_username())
        flash(f'Backups pushed to git: {result["files_pushed"]} .db files pushed to {result["pushed_to"]}', 'success')
    except Exception as e:
        app_logger.error('Git push failed: %s by=%s\nTraceback:\n%s', e, current_username(), traceback.format_exc())
        flash(f'Git push failed: {e}', 'error')
    return redirect(url_for('backup_list'))


@app.route('/backups/local/list')
@permission_required('backups')
def backup_local_list():
    """API: list .db files in the local backup directory."""
    try:
        backups = db.list_backups()
        return jsonify({'ok': True, 'backups': backups})
    except Exception as e:
        app_logger.error('Local backup list failed: %s\nTraceback:\n%s', e, traceback.format_exc())
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/backups/git/list')
@permission_required('backups')
def backup_git_list():
    """API: list .db files available in the git backup zip."""
    try:
        entries = db.list_git_backups()
        return jsonify({'ok': True, 'backups': entries})
    except Exception as e:
        app_logger.error('Git backup list failed: %s\nTraceback:\n%s', e, traceback.format_exc())
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/backups/git/restore', methods=['POST'])
@permission_required('backups')
def backup_git_restore():
    """Restore database from a file in the git backup zip."""
    filename = request.form.get('filename', '').strip()
    if not filename:
        flash('No file selected.', 'error')
        return redirect(url_for('backup_list'))
    try:
        result = db.restore_from_git(filename)
        app_logger.info('Database restored from git: %s (safety: %s) by=%s',
                        result['restored_from'], result['safety_backup'], current_username())
        flash(f'Database restored from git backup: {filename}. Safety backup: {result["safety_backup"]}', 'success')
    except Exception as e:
        app_logger.error('Git restore failed: %s by=%s\nTraceback:\n%s', e, current_username(), traceback.format_exc())
        flash(f'Restore from git failed: {e}', 'error')
    return redirect(url_for('backup_list'))


@app.route('/backups/<filename>/delete', methods=['POST'])
@permission_required('backups')
def backup_delete(filename):
    """Delete a backup file."""
    try:
        db.delete_backup(filename)
        app_logger.info('Backup deleted: %s by=%s', filename, current_username())
        flash(f'Backup deleted: {filename}', 'success')
    except Exception as e:
        app_logger.error('Backup delete failed: file=%s error=%s\nTraceback:\n%s', filename, e, traceback.format_exc())
        flash(f'Error deleting backup: {e}', 'error')
    return redirect(url_for('backup_list'))


@app.route('/backups/<filename>/download')
@permission_required('backups')
def backup_download(filename):
    """Download a backup file."""
    if not db._is_backup_file(filename) or '..' in filename:
        flash('Invalid backup file.', 'error')
        return redirect(url_for('backup_list'))
    backup_dir = db._get_backup_dir()
    path = os.path.join(backup_dir, filename)
    if not os.path.isfile(path):
        flash('Backup file not found.', 'error')
        return redirect(url_for('backup_list'))
    app_logger.info('Backup downloaded: %s by=%s', filename, current_username())
    return send_file(path, as_attachment=True, download_name=filename)


@app.route('/backups/<filename>/restore', methods=['POST'])
@permission_required('backups')
def backup_restore(filename):
    """Restore the database from a backup file."""
    try:
        result = db.restore_database(filename)
        app_logger.info('Database restored from %s (safety backup: %s) by=%s',
                        result['restored_from'], result['safety_backup'], current_username())
        msg = f'Database restored from {filename}. A safety backup was created: {result["safety_backup"]}'
        if result.get('warnings'):
            msg += f' ({len(result["warnings"])} compatibility warning{"s" if len(result["warnings"]) != 1 else ""})'
        flash(msg, 'success')
        for w in result.get('warnings', []):
            flash(w, 'warning')
    except FileNotFoundError:
        flash('Backup file not found.', 'error')
    except ValueError as e:
        app_logger.error('Restore failed: %s by=%s\nTraceback:\n%s', e, current_username(), traceback.format_exc())
        flash(f'Restore failed: {e}', 'error')
    except Exception as e:
        app_logger.error('Restore failed: %s by=%s\nTraceback:\n%s', e, current_username(), traceback.format_exc())
        flash(f'Restore failed: {e}', 'error')
    return redirect(url_for('backup_list'))

# ---------------------------------------------------------------------------
# Product Reference (printer/device spec catalog)
# ---------------------------------------------------------------------------

@app.route('/reference')
def product_reference_list():
    search = request.args.get('q', '')
    refs = db.get_all_product_references(search)
    inv_counts = db.get_inventory_counts_by_codename()
    return render_template('product_reference.html', refs=refs, search=search, inv_counts=inv_counts)


@app.route('/reference/add', methods=['GET', 'POST'])
@login_required
def product_reference_add():
    if not has_permission('references'):
        flash('You do not have permission to perform this action.', 'error')
        return redirect(url_for('product_reference_list'))

    if request.method == 'POST':
        codename = request.form.get('codename', '').strip()
        if not codename:
            flash('Codename is required.', 'error')
            return render_template('product_reference_form.html', ref=None, current_year=str(datetime.now().year))

        db.add_product_reference(
            codename=codename,
            model_name=request.form.get('model_name', '').strip(),
            wifi_gen=request.form.get('wifi_gen', '').strip(),
            year=request.form.get('year', '').strip(),
            chip_manufacturer=request.form.get('chip_manufacturer', '').strip(),
            chip_codename=request.form.get('chip_codename', '').strip(),
            fw_codebase=request.form.get('fw_codebase', '').strip(),
            print_technology=request.form.get('print_technology', '').strip(),
            cartridge_toner=request.form.get('cartridge_toner', '').strip(),
        )
        flash(f'Product reference "{codename}" added.', 'success')
        return redirect(url_for('product_reference_list'))

    return render_template('product_reference_form.html', ref=None, current_year=str(datetime.now().year))


@app.route('/reference/<int:ref_id>/edit', methods=['GET', 'POST'])
@login_required
def product_reference_edit(ref_id):
    if not has_permission('references'):
        flash('You do not have permission to perform this action.', 'error')
        return redirect(url_for('product_reference_list'))

    ref = db.get_product_reference(ref_id)
    if not ref:
        flash('Product reference not found.', 'error')
        return redirect(url_for('product_reference_list'))

    if request.method == 'POST':
        codename = request.form.get('codename', '').strip()
        if not codename:
            flash('Codename is required.', 'error')
            return render_template('product_reference_form.html', ref=ref)

        db.update_product_reference(
            ref_id=ref_id,
            codename=codename,
            model_name=request.form.get('model_name', '').strip(),
            wifi_gen=request.form.get('wifi_gen', '').strip(),
            year=request.form.get('year', '').strip(),
            chip_manufacturer=request.form.get('chip_manufacturer', '').strip(),
            chip_codename=request.form.get('chip_codename', '').strip(),
            fw_codebase=request.form.get('fw_codebase', '').strip(),
            print_technology=request.form.get('print_technology', '').strip(),
            cartridge_toner=request.form.get('cartridge_toner', '').strip(),
        )
        flash(f'Product reference "{codename}" updated.', 'success')
        return redirect(url_for('product_reference_list'))

    return render_template('product_reference_form.html', ref=ref)


@app.route('/api/reference/<int:ref_id>', methods=['PATCH'])
@login_required
def api_reference_update(ref_id):
    """Inline edit API — update a single field on a product reference."""
    if not has_permission('references'):
        return jsonify({'error': 'Permission denied'}), 403
    ref = db.get_product_reference(ref_id)
    if not ref:
        return jsonify({'error': 'Not found'}), 404
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data'}), 400
    allowed = {'codename', 'model_name', 'wifi_gen', 'year', 'chip_manufacturer',
               'chip_codename', 'fw_codebase', 'print_technology', 'cartridge_toner'}
    updates = {k: v.strip() for k, v in data.items() if k in allowed}
    if not updates:
        return jsonify({'error': 'No valid fields'}), 400
    # Merge with existing values
    merged = {k: ref[k] for k in allowed}
    merged.update(updates)
    if not merged.get('codename'):
        return jsonify({'error': 'Codename is required'}), 400
    db.update_product_reference(ref_id=ref_id, **merged)
    app_logger.info('Product reference inline edit: ref_id=%d fields=%s by=%s', ref_id, list(updates.keys()), current_username())
    return jsonify({'ok': True})


@app.route('/reference/<int:ref_id>/delete', methods=['POST'])
@login_required
def product_reference_delete(ref_id):
    if not has_permission('references'):
        flash('You do not have permission to perform this action.', 'error')
        return redirect(url_for('product_reference_list'))
    db.delete_product_reference(ref_id)
    flash('Product reference deleted.', 'success')
    return redirect(url_for('product_reference_list'))


HEADER_MAP = {
    # Codename (required field)
    'codename': 'codename',
    'code name': 'codename',
    'product codename': 'codename',
    'product': 'codename',
    # Model name
    'model name': 'model_name',
    'model_name': 'model_name',
    'model': 'model_name',
    # Wi-Fi generation
    'wi-fi gen': 'wifi_gen',
    'wifi gen': 'wifi_gen',
    'wifi_gen': 'wifi_gen',
    'wi-fi generation': 'wifi_gen',
    'wifi generation': 'wifi_gen',
    'wireless gen': 'wifi_gen',
    # Year
    'year': 'year',
    'release year': 'year',
    # Chip manufacturer
    'wireless chip set manufacturer': 'chip_manufacturer',
    'wireless chipset manufacturer': 'chip_manufacturer',
    'chip manufacturer': 'chip_manufacturer',
    'chip_manufacturer': 'chip_manufacturer',
    'chip vendor': 'chip_manufacturer',
    'wireless chip vendor': 'chip_manufacturer',
    # Chip codename
    'wireless chipset codename': 'chip_codename',
    'chip codename': 'chip_codename',
    'chip_codename': 'chip_codename',
    # Firmware codebase
    'fw codebase': 'fw_codebase',
    'fw_codebase': 'fw_codebase',
    'firmware codebase': 'fw_codebase',
    'codebase': 'fw_codebase',
    # Print technology
    'print technology': 'print_technology',
    'print_technology': 'print_technology',
    'technology': 'print_technology',
    # Cartridge/Toner
    'cartridge/toner': 'cartridge_toner',
    'cartridge_toner': 'cartridge_toner',
    'cartridge': 'cartridge_toner',
    'toner': 'cartridge_toner',
    'cartridge / toner': 'cartridge_toner',
    # Variant
    'variant': 'variant',
}


def _import_seed_data():
    """Import seed CSV (upsert) and attach seed images to wiki pages."""
    import csv as _csv
    seed_dir = os.path.join(BUNDLE_DIR, 'seed_data')
    csv_path = os.path.join(seed_dir, 'product_reference.csv')

    if not os.path.isfile(csv_path):
        flash('Seed data not found. No seed_data/product_reference.csv in the application bundle.', 'error')
        return redirect(url_for('product_reference_list'))

    added = 0
    updated = 0
    skipped = 0
    try:
        with open(csv_path, 'r', encoding='utf-8-sig') as f:
            reader = _csv.DictReader(f)
            if reader.fieldnames is None:
                flash('Seed CSV has no headers.', 'error')
                return redirect(url_for('product_reference_list'))
            for row in reader:
                norm = {k.strip().lower(): v.strip() for k, v in row.items() if k}
                codename = norm.get('codename', '').strip()
                if not codename:
                    skipped += 1
                    continue
                _ref_id, action = db.upsert_product_reference(
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
                    cartridge_toner=norm.get('cartridge/toner', norm.get('cartridge_toner', '')),
                    variant=norm.get('variant', ''),
                )
                if action == 'added':
                    added += 1
                else:
                    updated += 1
    except Exception as e:
        app_logger.error('Seed CSV import failed: %s\n%s', e, traceback.format_exc())
        flash(f'Seed import failed: {e}', 'error')
        return redirect(url_for('product_reference_list'))

    # Phase 2: attach seed images to wiki pages
    images_attached = 0
    zip_path = os.path.join(seed_dir, 'printer_images.zip')
    if os.path.isfile(zip_path):
        try:
            from database import _seed_wiki_images
            images_attached = _seed_wiki_images(zip_path)
        except Exception as e:
            app_logger.error('Seed image attachment failed: %s\n%s', e, traceback.format_exc())
            flash(f'Image attachment partially failed: {e}', 'warning')

    parts = []
    if added:
        parts.append(f'{added} added')
    if updated:
        parts.append(f'{updated} updated')
    if skipped:
        parts.append(f'{skipped} skipped')
    msg = f'Seed import: {", ".join(parts)}.'
    if images_attached:
        msg += f' {images_attached} images attached to wiki pages.'
    flash(msg, 'success')
    return redirect(url_for('product_reference_list'))


@app.route('/reference/seed', methods=['POST'])
@login_required
def product_reference_seed():
    """Import seed data from the application bundle."""
    if not has_permission('references'):
        flash('You do not have permission to perform this action.', 'error')
        return redirect(url_for('product_reference_list'))
    return _import_seed_data()


@app.route('/reference/import', methods=['POST'])
@login_required
def product_reference_import():
    """Import product references from an uploaded .xlsx or .csv file."""
    if not has_permission('references'):
        flash('You do not have permission to perform this action.', 'error')
        return redirect(url_for('product_reference_list'))

    file = request.files.get('import_file')
    if not file or not file.filename:
        flash('No file selected.', 'error')
        return redirect(url_for('product_reference_list'))

    filename = file.filename.lower()
    if not filename.endswith(('.xlsx', '.csv')):
        flash('Unsupported file type. Use .xlsx or .csv', 'error')
        return redirect(url_for('product_reference_list'))

    try:
        import_mode = request.form.get('import_mode', 'add')
        if import_mode == 'overwrite':
            db.clear_all_product_references()

        imported = 0
        skipped = 0

        def _map_headers(raw):
            """Map raw header names to DB columns, warn about unrecognized ones."""
            mapped = [HEADER_MAP.get(str(h).strip().lower()) for h in raw]
            if not any(m == 'codename' for m in mapped):
                flash('Warning: No "Codename" column found. All rows will be skipped.', 'warning')
            unrecognized = [str(h).strip() for h, m in zip(raw, mapped)
                            if m is None and str(h).strip()]
            if unrecognized:
                flash(f'Unrecognized columns ignored: {", ".join(unrecognized)}', 'warning')
            return mapped

        if filename.endswith('.xlsx'):
            import openpyxl
            wb = openpyxl.load_workbook(file, read_only=True, data_only=True)
            ws = wb.active
            rows = ws.iter_rows()
            raw_headers = [cell.value or '' for cell in next(rows)]
            headers = _map_headers(raw_headers)

            for row in rows:
                values = [cell.value for cell in row]
                if not any(v is not None and str(v).strip() for v in values):
                    continue
                record = {}
                for i, val in enumerate(values):
                    if i < len(headers) and headers[i]:
                        record[headers[i]] = str(val).strip() if val is not None else ''
                codename = record.get('codename', '').strip()
                if not codename:
                    skipped += 1
                    continue
                db.add_product_reference(**record)
                imported += 1
            wb.close()
        else:
            import csv, io
            raw = file.read()
            text = raw.decode('utf-8-sig')
            # Auto-detect delimiter
            delimiter = '\t' if '\t' in text[:2048] else ','
            reader = csv.reader(io.StringIO(text), delimiter=delimiter)
            raw_headers = next(reader)
            headers = _map_headers(raw_headers)

            for row in reader:
                if not any(cell.strip() for cell in row):
                    continue
                record = {}
                for i, val in enumerate(row):
                    if i < len(headers) and headers[i]:
                        record[headers[i]] = val.strip()
                codename = record.get('codename', '').strip()
                if not codename:
                    skipped += 1
                    continue
                db.add_product_reference(**record)
                imported += 1

        flash(f'Imported {imported} product{"s" if imported != 1 else ""}.'
              + (f' {skipped} rows skipped (no codename).' if skipped else ''), 'success')
    except Exception as e:
        app_logger.error('Product reference import failed: %s\nTraceback:\n%s', e, traceback.format_exc())
        flash(f'Import failed: {e}', 'error')

    return redirect(url_for('product_reference_list'))


@app.route('/reference/export')
@login_required
def product_reference_export():
    """Export all product references as a .csv download."""
    if not has_permission('references'):
        flash('You do not have permission to perform this action.', 'error')
        return redirect(url_for('product_reference_list'))
    import csv, io
    refs = db.get_all_product_references()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Codename', 'Model Name', 'Print Technology', 'Cartridge/Toner', 'Wi-Fi Gen', 'Year',
                     'Wireless Chip Set Manufacturer', 'Wireless Chipset Codename', 'FW Codebase',
                     'Variant'])
    for r in refs:
        writer.writerow([r['codename'], r['model_name'], r['print_technology'],
                         r.get('cartridge_toner', ''), r['wifi_gen'], r['year'],
                         r['chip_manufacturer'], r['chip_codename'], r['fw_codebase'],
                         r.get('variant', '')])
    csv_bytes = output.getvalue().encode('utf-8-sig')
    return Response(csv_bytes, mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename=product_reference.csv'})


@app.route('/reference/export/xlsx')
@login_required
def product_reference_export_xlsx():
    """Export all product references as an .xlsx download."""
    if not has_permission('references'):
        flash('You do not have permission to perform this action.', 'error')
        return redirect(url_for('product_reference_list'))

    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment
    except ImportError:
        flash('openpyxl is required for Excel export. Install with: pip install openpyxl', 'error')
        return redirect(url_for('product_reference_list'))

    refs = db.get_all_product_references()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Product Reference'

    headers = ['Codename', 'Model Name', 'Print Technology', 'Cartridge/Toner', 'Wi-Fi Gen', 'Year',
               'Wireless Chip Set Manufacturer', 'Wireless Chipset Codename', 'FW Codebase',
               'Variant']
    ws.append(headers)
    header_font = Font(bold=True, size=11)
    header_fill = PatternFill(start_color='E2EFDA', end_color='E2EFDA', fill_type='solid')
    for cell in ws[1]:
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal='center')

    for r in refs:
        ws.append([r['codename'], r['model_name'], r['print_technology'],
                   r.get('cartridge_toner', ''), r['wifi_gen'], r['year'],
                   r['chip_manufacturer'], r['chip_codename'], r['fw_codebase'],
                   r.get('variant', '')])

    for col in ws.columns:
        max_len = max((len(str(cell.value or '')) for cell in col), default=10)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 2, 40)
    ws.freeze_panes = 'A2'

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={'Content-Disposition': 'attachment; filename=product_reference.xlsx'}
    )


# ---------------------------------------------------------------------------
# Product Wiki — community notes per product
# ---------------------------------------------------------------------------

WIKI_UPLOADS_DIR = os.path.join(DATA_DIR, 'wiki_uploads')
ALLOWED_EXTENSIONS = {
    'png', 'jpg', 'jpeg', 'gif', 'bmp', 'svg', 'webp',
    'pdf', 'doc', 'docx', 'xls', 'xlsx', 'csv', 'txt',
    'zip', 'tar', 'gz', 'pptx', 'log',
}
MAX_UPLOAD_SIZE = 25 * 1024 * 1024  # 25 MB


@app.route('/wiki/<int:ref_id>')
def product_wiki(ref_id):
    """View/edit the wiki page for a product."""
    refs = db.get_all_product_references()
    ref = None
    for r in refs:
        if r['ref_id'] == ref_id:
            ref = r
            break
    if not ref:
        flash('Product not found.', 'error')
        return redirect(url_for('product_reference_list'))
    wiki = db.get_wiki_by_ref_id(ref_id)
    content = wiki['content'] if wiki else ''
    updated_by = wiki['updated_by'] if wiki else ''
    updated_at = wiki['updated_at'] if wiki else ''
    attachments = db.get_wiki_attachments(ref_id)
    return render_template('product_wiki.html', ref=ref, content=content,
                           updated_by=updated_by, updated_at=updated_at,
                           attachments=attachments)


@app.route('/wiki/<int:ref_id>/save', methods=['POST'])
@login_required
def product_wiki_save(ref_id):
    """Save wiki content (any logged-in user)."""
    content = request.form.get('content', '')
    username = g.user['username']
    db.save_wiki(ref_id, content, updated_by=username)
    flash('Wiki saved.', 'success')
    return redirect(url_for('product_wiki', ref_id=ref_id))


@app.route('/wiki/<int:ref_id>/upload', methods=['POST'])
@login_required
def wiki_upload(ref_id):
    """Upload an attachment to a product wiki."""
    if not has_permission('wiki_admin'):
        flash('You do not have permission to perform this action.', 'error')
        return redirect(url_for('product_wiki', ref_id=ref_id))

    file = request.files.get('attachment')
    if not file or not file.filename:
        flash('No file selected.', 'error')
        return redirect(url_for('product_wiki', ref_id=ref_id))

    original_name = file.filename
    ext = original_name.rsplit('.', 1)[-1].lower() if '.' in original_name else ''
    if ext not in ALLOWED_EXTENSIONS:
        flash(f'File type .{ext} is not allowed.', 'error')
        return redirect(url_for('product_wiki', ref_id=ref_id))

    # Read file and check size
    data = file.read()
    if len(data) > MAX_UPLOAD_SIZE:
        flash('File exceeds 25 MB limit.', 'error')
        return redirect(url_for('product_wiki', ref_id=ref_id))

    # Save to disk with unique filename
    upload_dir = os.path.join(WIKI_UPLOADS_DIR, str(ref_id))
    os.makedirs(upload_dir, exist_ok=True)
    safe_name = f'{uuid.uuid4().hex}.{ext}'
    filepath = os.path.join(upload_dir, safe_name)
    with open(filepath, 'wb') as f:
        f.write(data)

    db.add_wiki_attachment(
        ref_id=ref_id,
        filename=safe_name,
        original_name=original_name,
        content_type=file.content_type or '',
        size_bytes=len(data),
        uploaded_by=g.user['username'],
    )
    flash(f'Uploaded {original_name}.', 'success')
    return redirect(url_for('product_wiki', ref_id=ref_id))


@app.route('/wiki/attachment/<int:attachment_id>')
def wiki_download(attachment_id):
    """Download a wiki attachment (public)."""
    att = db.get_wiki_attachment(attachment_id)
    if not att:
        return 'Attachment not found', 404
    filepath = os.path.join(WIKI_UPLOADS_DIR, str(att['ref_id']), att['filename'])
    if not os.path.isfile(filepath):
        return 'File not found on disk', 404
    return send_file(filepath, download_name=att['original_name'], as_attachment=True)


@app.route('/wiki/attachment/<int:attachment_id>/preview')
def wiki_attachment_preview(attachment_id):
    """Serve an attachment inline for image preview (public)."""
    att = db.get_wiki_attachment(attachment_id)
    if not att:
        return 'Attachment not found', 404
    filepath = os.path.join(WIKI_UPLOADS_DIR, str(att['ref_id']), att['filename'])
    if not os.path.isfile(filepath):
        return 'File not found on disk', 404
    return send_file(filepath, mimetype=att['content_type'])


@app.route('/wiki/attachment/<int:attachment_id>/delete', methods=['POST'])
@login_required
def wiki_delete_attachment(attachment_id):
    """Delete a wiki attachment."""
    if not has_permission('wiki_admin'):
        flash('You do not have permission to perform this action.', 'error')
        return redirect(url_for('product_reference_list'))
    att = db.get_wiki_attachment(attachment_id)
    if not att:
        flash('Attachment not found.', 'error')
        return redirect(url_for('product_reference_list'))
    # Delete file from disk
    filepath = os.path.join(WIKI_UPLOADS_DIR, str(att['ref_id']), att['filename'])
    if os.path.isfile(filepath):
        os.remove(filepath)
    db.delete_wiki_attachment(attachment_id)
    flash(f'Deleted {att["original_name"]}.', 'success')
    return redirect(url_for('product_wiki', ref_id=att['ref_id']))


@app.route('/wiki/repair', methods=['POST'])
@login_required
def wiki_repair_attachments():
    """Manually run attachment integrity check — removes orphaned DB records."""
    if not has_permission('wiki_admin'):
        flash('You do not have permission to perform this action.', 'error')
        return redirect(url_for('product_reference_list'))
    result = db.check_attachment_integrity(WIKI_UPLOADS_DIR)
    if result['orphaned_removed'] > 0:
        app_logger.info('Manual attachment repair: removed %d orphaned records by=%s',
                        result['orphaned_removed'], current_username())
        flash(f'Repair complete: removed {result["orphaned_removed"]} broken attachment references.', 'success')
    else:
        flash(f'All {result["total_checked"]} attachments are intact. No repairs needed.', 'success')
    return redirect(request.referrer or url_for('product_reference_list'))


@app.route('/api/devices/distinct/<field>')
@login_required
def api_distinct_values(field):
    """Return distinct values for a device field, for autocomplete."""
    values = db.get_distinct_values(field)
    return jsonify(values)


@app.route('/api/reference/search')
def api_reference_search():
    """JSON API for printer dropdown in the device form."""
    q = request.args.get('q', '').strip()
    refs = db.get_all_product_references(q)
    return jsonify([{
        'ref_id': r['ref_id'],
        'codename': r['codename'],
        'model_name': r['model_name'],
        'wifi_gen': r['wifi_gen'],
        'year': r['year'],
        'chip_manufacturer': r['chip_manufacturer'],
        'chip_codename': r['chip_codename'],
        'fw_codebase': r['fw_codebase'],
        'print_technology': r['print_technology'],
        'cartridge_toner': r.get('cartridge_toner', ''),
    } for r in refs])


# ---------------------------------------------------------------------------
# Health check endpoint (public, no auth required)
# ---------------------------------------------------------------------------

@app.route('/health')
def health_check():
    """Return backup and database health status as JSON for external monitoring."""
    health = db.get_backup_health()
    db_status = db.get_database_status()
    health['database'] = {
        'integrity': db_status['integrity'],
        'size_bytes': db_status['size_bytes'],
        'wal_size_bytes': db_status['wal_size_bytes'],
        'table_counts': db_status['table_counts'],
    }
    if db_status['integrity'] != 'ok':
        health['healthy'] = False
        health['issues'].append(f'Database integrity check failed: {db_status["integrity"]}')
    status_code = 200 if health['healthy'] else 503
    return jsonify(health), status_code


# ---------------------------------------------------------------------------
# Favicon / Apple Touch Icon (generated in-memory to suppress browser 404s)
# ---------------------------------------------------------------------------

_FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" rx="6" fill="#3b82f6"/>'
    '<text x="16" y="23" text-anchor="middle" fill="white" '
    'font-size="20" font-family="sans-serif" font-weight="bold">I</text></svg>'
)


@app.route('/favicon.ico')
def favicon():
    return Response(_FAVICON_SVG, mimetype='image/svg+xml',
                    headers={'Cache-Control': 'public, max-age=86400'})


@app.route('/apple-touch-icon.png')
@app.route('/apple-touch-icon-precomposed.png')
@app.route('/apple-touch-icon-<dimensions>.png')
@app.route('/apple-touch-icon-<dimensions>-precomposed.png')
def apple_touch_icon(**kwargs):
    return Response(_FAVICON_SVG, mimetype='image/svg+xml',
                    headers={'Cache-Control': 'public, max-age=86400'})


# ---------------------------------------------------------------------------
# Global error handlers
# ---------------------------------------------------------------------------

@app.errorhandler(404)
def not_found(e):
    app_logger.warning('404 Not Found: path=%s method=%s ip=%s user=%s user_agent=%s',
                       request.path, request.method, request.remote_addr,
                       current_username(), request.user_agent.string[:120])
    flash('Page not found.', 'error')
    return redirect(url_for('dashboard'))


@app.errorhandler(500)
def internal_error(e):
    tb = traceback.format_exc()
    app_logger.error('500 Internal Server Error: path=%s method=%s ip=%s user=%s\n'
                     'Exception: %s\nTraceback:\n%s',
                     request.path, request.method, request.remote_addr,
                     current_username(), e, tb)
    flash('An unexpected error occurred.', 'error')
    return redirect(url_for('dashboard'))


@app.errorhandler(Exception)
def unhandled_exception(e):
    tb = traceback.format_exc()
    app_logger.error('Unhandled exception: path=%s method=%s ip=%s user=%s\n'
                     'Exception type: %s — %s\nTraceback:\n%s',
                     request.path, request.method, request.remote_addr,
                     current_username(), type(e).__name__, e, tb)
    flash('An unexpected error occurred.', 'error')
    return redirect(url_for('dashboard'))

# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    server_cfg = _load_server_config()
    default_port = server_cfg.get('port', 8080)
    default_host = server_cfg.get('host', '0.0.0.0')

    parser = argparse.ArgumentParser(description='HP Connectivity Team Inventory System')
    parser.add_argument('--host', default=default_host, help=f'Host to bind to (default: {default_host})')
    parser.add_argument('--port', type=int, default=default_port, help=f'Port to listen on (default: {default_port})')
    parser.add_argument('--dev', action='store_true', help='Run in development mode with debug enabled')
    parser.add_argument('--reset-admin', metavar='PASSWORD',
                        help='Reset admin password to PASSWORD and exit. Requires server access. Creates admin if none exists.')
    parser.add_argument('--export-sql', metavar='FILE', help='Export database to SQL dump file and exit')
    parser.add_argument('--emergency-backup', nargs='?', const=True, metavar='PATH',
                        help='Create an emergency database backup and exit')
    args = parser.parse_args()

    # --- Recovery CLI commands (run and exit) ---
    if args.reset_admin:
        new_pw = args.reset_admin
        if len(new_pw) < 4:
            print('  ERROR: Password must be at least 4 characters.')
            exit(1)
        db.init_db()
        username, created = db.reset_admin_password(new_pw)
        if created:
            print(f'  Admin user created: {username}')
        else:
            print(f'  Password reset for admin user: {username}')
        print('  You can now log in with the new credentials.')
        exit(0)

    if args.export_sql:
        db.init_db()
        success = db.export_database_to_sql(args.export_sql)
        if success:
            print(f'  Database exported to: {args.export_sql}')
        else:
            print('  Export failed. Check logs for details.')
            exit(1)
        exit(0)

    if args.emergency_backup:
        db.init_db()
        dest = args.emergency_backup if args.emergency_backup is not True else None
        path = db.emergency_backup(dest)
        print(f'  Emergency backup created: {path}')
        exit(0)

    url = f'http://{args.host}:{args.port}'
    mode = 'DEVELOPMENT' if args.dev else 'PRODUCTION'
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'VERSION')) as _vf:
            _version = _vf.read().strip()
    except Exception:
        _version = 'unknown'
    w = 49  # inner width between | chars
    print()
    print(f'  +{"-" * w}+')
    print(f'  |{"HP Connectivity Team Inventory System":^{w}}|')
    print(f'  |{("v" + _version):^{w}}|')
    print(f'  |{"":^{w}}|')
    print(f'  |{"  Running at: " + url:<{w}}|')
    print(f'  |{"  Mode: " + mode:<{w}}|')
    print(f'  |{"":^{w}}|')
    print(f'  |{"  Press Ctrl+C to stop":<{w}}|')
    print(f'  +{"-" * w}+')
    print()

    if args.dev:
        app.run(host=args.host, port=args.port, debug=True)
    else:
        try:
            from waitress import serve
            app_logger.info('Starting production server (waitress) on %s:%s', args.host, args.port)
            serve(app, host=args.host, port=args.port, threads=4)
        except ImportError:
            print("  WARNING: waitress not installed. Install it for production:")
            print("    pip install waitress")
            print("  Falling back to Flask development server.\n")
            app.run(host=args.host, port=args.port, debug=False)
