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
from PIL import Image
from functools import wraps
from datetime import datetime, timezone
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

# ---------------------------------------------------------------------------
# Application logging (rotating file, single file that overwrites at limit)
# ---------------------------------------------------------------------------

LOG_DIR = os.path.join(DATA_DIR, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, 'app.log')
LOG_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'log_config.json')


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


def login_required(f):
    """Decorator: redirect to login if not authenticated."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not g.user:
            flash('Please log in to continue.', 'warning')
            return redirect(url_for('login', next=request.path))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    """Decorator: require admin role. Viewers get an error flash."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not g.user:
            flash('Please log in to continue.', 'warning')
            return redirect(url_for('login', next=request.path))
        if g.user['role'] != 'admin':
            flash('You do not have permission to perform this action.', 'error')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated


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
    }

# ---------------------------------------------------------------------------
# Login / Logout
# ---------------------------------------------------------------------------

@app.route('/login', methods=['GET', 'POST'])
def login():
    if g.user:
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        user = db.authenticate_user(username, password)
        if user:
            session['user_id'] = user['user_id']
            session['role'] = user['role']
            app_logger.info('Login successful: user=%s role=%s ip=%s', username, user['role'], request.remote_addr)
            next_url = request.form.get('next') or url_for('dashboard')
            return redirect(next_url)
        else:
            app_logger.warning('Login failed: user=%s ip=%s', username, request.remote_addr)
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
    devices = db.search_devices(
        query=q,
        category=request.args.get('category', ''),
        status=request.args.get('status', ''),
        connectivity=request.args.get('connectivity', ''),
        location=request.args.get('location', ''),
        codename=codename,
    )
    if q:
        app_logger.info('Device search: query="%s" results=%d ip=%s', q, len(devices), request.remote_addr)
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
@admin_required
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

        data = {
            'name': name,
            'category': category,
            'manufacturer': request.form.get('manufacturer', ''),
            'model_number': request.form.get('model_number', ''),
            'serial_number': request.form.get('serial_number', ''),
            'connectivity': request.form.get('connectivity', ''),
            'vendor_supplied': 1 if request.form.get('vendor_supplied') else 0,
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

    return render_template('device_detail.html', device=device, audit=audit, prod_ref=prod_ref)

# ---------------------------------------------------------------------------
# Edit device (admin only)
# ---------------------------------------------------------------------------

@app.route('/devices/<device_id>/edit', methods=['GET', 'POST'])
@admin_required
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
            'vendor_supplied': 1 if request.form.get('vendor_supplied') else 0,
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
@admin_required
def device_retire(device_id):
    db.retire_device(device_id, performed_by=current_username())
    app_logger.info('Device retired: id=%s by=%s', device_id, current_username())
    flash('Device retired successfully.', 'success')
    return redirect(url_for('device_list'))

# ---------------------------------------------------------------------------
# Check out / Check in (admin only)
# ---------------------------------------------------------------------------

@app.route('/devices/<device_id>/checkout', methods=['POST'])
@admin_required
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
@admin_required
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
    """Serve a label as a PDF matching 1.5x3.5 inch portrait label stock."""
    device = db.get_device(device_id)
    if not device:
        return 'Device not found', 404
    # Always regenerate to ensure PDF matches current label
    barcode_utils.generate_label(device_id, device['barcode_value'], _label_name(device))
    path = barcode_utils.get_label_path(device_id)

    # Landscape PNG (1050x450 = 3.5x1.5")
    img = Image.open(path)
    img_buffer = io.BytesIO()
    img.save(img_buffer, 'JPEG', quality=95)
    img_data = img_buffer.getvalue()
    img_w, img_h = img.size  # 1050 x 450

    # Portrait page matching label stock: 1.5" wide x 3.5" tall
    page_w = 108   # 1.5 * 72
    page_h = 252   # 3.5 * 72

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
    pdf.write(f'4 0 obj\n<< /Type /XObject /Subtype /Image /Width {img_w} /Height {img_h} /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length {len(img_data)} >>\nstream\n'.encode())
    pdf.write(img_data)
    pdf.write(b'\nendstream\nendobj\n')

    # Rotate landscape image 90° CCW via transformation matrix to fit portrait page.
    # Matrix: [0 -page_w page_h 0 0 page_w] rotates and scales the unit square
    # so the landscape image fills the portrait page.
    # The image left edge maps to the top of the page.
    content = f'q 0 -{page_w} {page_h} 0 0 {page_w} cm /Img Do Q'.encode()
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
@admin_required
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
# CSV Export (public)
# ---------------------------------------------------------------------------

@app.route('/export')
def export_csv():
    """Export devices to CSV with optional filters."""
    # Read filter params
    category = request.args.get('category', '')
    status = request.args.get('status', '')
    connectivity = request.args.get('connectivity', '')
    location = request.args.get('location', '')
    q = request.args.get('q', '')
    include_retired = request.args.get('include_retired') == '1'

    if category or status or connectivity or location or q:
        # Use search with filters
        if not status and include_retired:
            status = ''  # search_devices excludes retired by default
        devices = db.search_devices(
            query=q,
            category=category,
            status=status if status else ('retired' if include_retired else ''),
            connectivity=connectivity,
            location=location,
        )
        # If include_retired and no specific status, we need all devices
        if include_retired and not status:
            non_retired = db.search_devices(query=q, category=category, connectivity=connectivity, location=location)
            retired = db.search_devices(query=q, category=category, status='retired', connectivity=connectivity, location=location)
            # Merge without duplicates
            seen = set()
            devices = []
            for d in non_retired + retired:
                if d['device_id'] not in seen:
                    seen.add(d['device_id'])
                    devices.append(d)
    else:
        devices = db.get_all_devices(include_retired=include_retired)

    app_logger.info('CSV export: %d devices (filters: cat=%s status=%s q=%s) ip=%s',
                    len(devices), category or 'all', status or 'all', q or 'none', request.remote_addr)

    output = io.StringIO()
    fields = ['device_id', 'barcode_value', 'name', 'category', 'manufacturer',
              'model_number', 'serial_number', 'connectivity', 'vendor_supplied',
              'status', 'location', 'assigned_to', 'notes', 'created_at', 'updated_at']
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction='ignore')
    writer.writeheader()
    for d in devices:
        writer.writerow(d)

    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=inventory_export.csv'}
    )

# ---------------------------------------------------------------------------
# User management (admin only)
# ---------------------------------------------------------------------------

@app.route('/users')
@admin_required
def user_list():
    users = db.get_all_users()
    return render_template('users.html', users=users)


@app.route('/users/add', methods=['GET', 'POST'])
@admin_required
def user_add():
    if request.method == 'POST':
        username = request.form.get('username', '').strip().lower()
        password = request.form.get('password', '')
        role = request.form.get('role', 'admin')
        display_name = request.form.get('display_name', '').strip()

        if not username or not password:
            flash('Username and password are required.', 'error')
            return render_template('user_form.html', user={}, is_edit=False)

        if len(password) < 4:
            flash('Password must be at least 4 characters.', 'error')
            return render_template('user_form.html', user=request.form, is_edit=False)

        try:
            db.create_user(username, password, role, display_name)
            app_logger.info('User created: username=%s role=%s by=%s', username, role, current_username())
            flash(f'User "{username}" created successfully.', 'success')
            return redirect(url_for('user_list'))
        except ValueError as e:
            flash(str(e), 'error')
            return render_template('user_form.html', user=request.form, is_edit=False)

    return render_template('user_form.html', user={}, is_edit=False)


@app.route('/users/<int:user_id>/edit', methods=['GET', 'POST'])
@admin_required
def user_edit(user_id):
    user = db.get_user(user_id)
    if not user:
        flash('User not found.', 'error')
        return redirect(url_for('user_list'))

    if request.method == 'POST':
        data = {
            'display_name': request.form.get('display_name', '').strip(),
            'role': request.form.get('role', user['role']),
        }
        password = request.form.get('password', '').strip()
        if password:
            if len(password) < 4:
                flash('Password must be at least 4 characters.', 'error')
                return render_template('user_form.html', user=user, is_edit=True)
            data['password'] = password

        db.update_user(user_id, data)
        app_logger.info('User updated: username=%s by=%s', user['username'], current_username())
        flash(f'User "{user["username"]}" updated.', 'success')
        return redirect(url_for('user_list'))

    return render_template('user_form.html', user=user, is_edit=True)


@app.route('/users/<int:user_id>/delete', methods=['POST'])
@admin_required
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
@admin_required
def app_logs():
    """View application log entries. Most recent first."""
    lines = []
    # Read rotated backup first (older), then current log (newer)
    for log_path in [LOG_FILE + '.1', LOG_FILE]:
        try:
            with open(log_path, 'r') as f:
                lines.extend(f.readlines())
        except FileNotFoundError:
            pass

    # Parse into structured entries, most recent first
    entries = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        # Format: "2026-04-02 12:00:00 | INFO    | message"
        parts = line.split(' | ', 2)
        if len(parts) == 3:
            entries.append({
                'timestamp': parts[0],
                'level': parts[1].strip(),
                'message': parts[2],
            })
        else:
            entries.append({
                'timestamp': '',
                'level': '',
                'message': line,
            })

    # Limit to 500 most recent entries
    entries = entries[:500]
    log_config = _load_log_config()
    log_file_size = sum(os.path.getsize(p) for p in [LOG_FILE, LOG_FILE + '.1'] if os.path.exists(p))
    return render_template('app_log.html', entries=entries, log_config=log_config, log_file_size=log_file_size)


@app.route('/logs/clear', methods=['POST'])
@admin_required
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


@app.route('/logs/config', methods=['POST'])
@admin_required
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

        # Verify current password
        user = db.authenticate_user(g.user['username'], current_pw)
        if not user:
            flash('Current password is incorrect.', 'error')
            return render_template('account.html')

        if len(new_pw) < 4:
            flash('New password must be at least 4 characters.', 'error')
            return render_template('account.html')

        if new_pw != confirm_pw:
            flash('New passwords do not match.', 'error')
            return render_template('account.html')

        db.update_user(g.user['user_id'], {'password': new_pw})
        app_logger.info('Password changed: user=%s', g.user['username'])
        flash('Password changed successfully.', 'success')
        return redirect(url_for('account'))

    users = db.get_all_users() if g.user['role'] == 'admin' else []
    return render_template('account.html', users=users)

# ---------------------------------------------------------------------------
# Database backup (admin only)
# ---------------------------------------------------------------------------

import threading

_backup_timer = None      # Timer for recurring local backups
_git_push_timer = None    # Timer for recurring git pushes
_prune_timer = None       # Timer for recurring backup pruning
_next_backup_time = None  # datetime of next scheduled backup
_next_git_push_time = None  # datetime of next scheduled git push
_next_prune_time = None   # datetime of next scheduled prune


def _run_scheduled_backup():
    """Execute a scheduled backup and re-arm the timer."""
    try:
        result = db.backup_database(performed_by='scheduled')
        app_logger.info('Scheduled backup completed: %s (%d bytes)',
                        result['filename'], result['size'])
    except Exception as e:
        app_logger.error('Scheduled backup failed: %s\nTraceback:\n%s', e, traceback.format_exc())
    # Re-arm from latest config
    config = db._get_backup_config()
    if config.get('backup_enabled'):
        _start_backup_timer(config['backup_interval_hours'])


def _run_scheduled_git_push():
    """Execute a scheduled git push and re-arm the timer."""
    try:
        result = db.push_backups_to_git()
        app_logger.info('Scheduled git push completed: %d files to %s',
                        result['files_pushed'], result['pushed_to'])
    except Exception as e:
        app_logger.error('Scheduled git push failed: %s\nTraceback:\n%s', e, traceback.format_exc())
    # Re-arm from latest config
    config = db._get_backup_config()
    if config.get('git_enabled'):
        _start_git_push_timer(config['git_push_interval_hours'])


def _start_backup_timer(interval_hours):
    """Start (or restart) the recurring backup timer."""
    global _backup_timer, _next_backup_time
    _stop_backup_timer()
    seconds = max(interval_hours * 3600, 300)  # Minimum 5 minutes
    from datetime import timedelta
    _next_backup_time = datetime.now() + timedelta(seconds=seconds)
    _backup_timer = threading.Timer(seconds, _run_scheduled_backup)
    _backup_timer.daemon = True
    _backup_timer.start()
    app_logger.info('Backup scheduler armed: next backup in %s hours', interval_hours)


def _stop_backup_timer():
    """Cancel any pending scheduled backup."""
    global _backup_timer, _next_backup_time
    if _backup_timer is not None:
        _backup_timer.cancel()
        _backup_timer = None
    _next_backup_time = None


def _start_git_push_timer(interval_hours):
    """Start (or restart) the recurring git push timer."""
    global _git_push_timer, _next_git_push_time
    _stop_git_push_timer()
    seconds = max(interval_hours * 3600, 300)  # Minimum 5 minutes
    from datetime import timedelta
    _next_git_push_time = datetime.now() + timedelta(seconds=seconds)
    _git_push_timer = threading.Timer(seconds, _run_scheduled_git_push)
    _git_push_timer.daemon = True
    _git_push_timer.start()
    app_logger.info('Git push scheduler armed: next push in %s hours', interval_hours)


def _stop_git_push_timer():
    """Cancel any pending scheduled git push."""
    global _git_push_timer, _next_git_push_time
    if _git_push_timer is not None:
        _git_push_timer.cancel()
        _git_push_timer = None
    _next_git_push_time = None


def _run_scheduled_prune():
    """Execute a scheduled prune and re-arm the timer."""
    try:
        config = db._get_backup_config()
        pruned = db._prune_old_backups(config['max_backups'])
        if pruned:
            app_logger.info('Scheduled prune completed: removed %d old auto-backups', pruned)
    except Exception as e:
        app_logger.error('Scheduled prune failed: %s\nTraceback:\n%s', e, traceback.format_exc())
    config = db._get_backup_config()
    if config.get('prune_enabled'):
        _start_prune_timer(config['prune_interval_hours'])


def _start_prune_timer(interval_hours):
    """Start (or restart) the recurring prune timer."""
    global _prune_timer, _next_prune_time
    _stop_prune_timer()
    seconds = max(interval_hours * 3600, 300)
    from datetime import timedelta
    _next_prune_time = datetime.now() + timedelta(seconds=seconds)
    _prune_timer = threading.Timer(seconds, _run_scheduled_prune)
    _prune_timer.daemon = True
    _prune_timer.start()
    app_logger.info('Prune scheduler armed: next prune in %s hours', interval_hours)


def _stop_prune_timer():
    """Cancel any pending scheduled prune."""
    global _prune_timer, _next_prune_time
    if _prune_timer is not None:
        _prune_timer.cancel()
        _prune_timer = None
    _next_prune_time = None


# Restore timers on startup
_startup_config = db._get_backup_config()
if _startup_config.get('backup_enabled'):
    _start_backup_timer(_startup_config['backup_interval_hours'])
if _startup_config.get('git_enabled') and _startup_config.get('git_repo'):
    _start_git_push_timer(_startup_config['git_push_interval_hours'])
if _startup_config.get('prune_enabled'):
    _start_prune_timer(_startup_config['prune_interval_hours'])


@app.route('/backups')
@admin_required
def backup_list():
    """View backup management page."""
    backups = db.list_backups()
    config = db._get_backup_config()
    next_backup = _next_backup_time.strftime('%Y-%m-%d %H:%M:%S') if _next_backup_time else None
    next_push = _next_git_push_time.strftime('%Y-%m-%d %H:%M:%S') if _next_git_push_time else None
    next_prune = _next_prune_time.strftime('%Y-%m-%d %H:%M:%S') if _next_prune_time else None
    return render_template('backups.html', backups=backups, config=config,
                           next_backup_time=next_backup, next_git_push_time=next_push,
                           next_prune_time=next_prune)


@app.route('/backups/create', methods=['POST'])
@admin_required
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
@admin_required
def backup_upload():
    """Restore database from an uploaded .db file."""
    file = request.files.get('backup_file')
    if not file or not file.filename:
        flash('No file selected.', 'error')
        return redirect(url_for('backup_list'))
    if not file.filename.endswith('.db'):
        flash('Invalid file type. Please upload a .db file.', 'error')
        return redirect(url_for('backup_list'))
    try:
        # Save uploaded file to backup dir
        backup_dir = db._get_backup_dir()
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        dest_filename = f'manual_backup_{timestamp}_uploaded.db'
        dest_path = os.path.join(backup_dir, dest_filename)
        file.save(dest_path)

        # Restore from the uploaded file
        result = db.restore_database(dest_filename)
        app_logger.info('Database restored from upload: %s (safety: %s) by=%s',
                        dest_filename, result['safety_backup'], current_username())
        flash(f'Database restored from uploaded file. Safety backup: {result["safety_backup"]}', 'success')
    except ValueError as e:
        app_logger.error('Upload restore failed: %s by=%s\nTraceback:\n%s', e, current_username(), traceback.format_exc())
        flash(f'Restore failed: {e}', 'error')
    except Exception as e:
        app_logger.error('Upload restore failed: %s by=%s\nTraceback:\n%s', e, current_username(), traceback.format_exc())
        flash(f'Restore failed: {e}', 'error')
    return redirect(url_for('backup_list'))


@app.route('/backups/config', methods=['POST'])
@admin_required
def backup_config():
    """Update all backup configuration settings."""
    config = db._get_backup_config()

    # Local backup settings
    backup_dir = request.form.get('backup_dir', '').strip()
    if backup_dir:
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

    app_logger.info('Backup config updated by=%s', current_username())
    flash('Backup configuration saved.', 'success')
    return redirect(url_for('backup_list'))


@app.route('/backups/push', methods=['POST'])
@admin_required
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
@admin_required
def backup_local_list():
    """API: list .db files in the local backup directory."""
    try:
        backups = db.list_backups()
        return jsonify({'ok': True, 'backups': backups})
    except Exception as e:
        app_logger.error('Local backup list failed: %s\nTraceback:\n%s', e, traceback.format_exc())
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/backups/git/list')
@admin_required
def backup_git_list():
    """API: list .db files available in the git backup zip."""
    try:
        entries = db.list_git_backups()
        return jsonify({'ok': True, 'backups': entries})
    except Exception as e:
        app_logger.error('Git backup list failed: %s\nTraceback:\n%s', e, traceback.format_exc())
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/backups/git/restore', methods=['POST'])
@admin_required
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
@admin_required
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
@admin_required
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
@admin_required
def backup_restore(filename):
    """Restore the database from a backup file."""
    try:
        result = db.restore_database(filename)
        app_logger.info('Database restored from %s (safety backup: %s) by=%s',
                        result['restored_from'], result['safety_backup'], current_username())
        flash(f'Database restored from {filename}. A safety backup was created: {result["safety_backup"]}', 'success')
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
    if g.user['role'] != 'admin':
        flash('Admin access required.', 'error')
        return redirect(url_for('product_reference_list'))

    if request.method == 'POST':
        codename = request.form.get('codename', '').strip()
        if not codename:
            flash('Codename is required.', 'error')
            return render_template('product_reference_form.html', ref=None)

        db.add_product_reference(
            codename=codename,
            model_name=request.form.get('model_name', '').strip(),
            wifi_gen=request.form.get('wifi_gen', '').strip(),
            year=request.form.get('year', '').strip(),
            chip_manufacturer=request.form.get('chip_manufacturer', '').strip(),
            chip_codename=request.form.get('chip_codename', '').strip(),
            fw_codebase=request.form.get('fw_codebase', '').strip(),
            print_technology=request.form.get('print_technology', '').strip(),
            variant=request.form.get('variant', '').strip(),
        )
        flash(f'Product reference "{codename}" added.', 'success')
        return redirect(url_for('product_reference_list'))

    return render_template('product_reference_form.html', ref=None)


@app.route('/reference/<int:ref_id>/edit', methods=['GET', 'POST'])
@login_required
def product_reference_edit(ref_id):
    if g.user['role'] != 'admin':
        flash('Admin access required.', 'error')
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
            variant=request.form.get('variant', '').strip(),
        )
        flash(f'Product reference "{codename}" updated.', 'success')
        return redirect(url_for('product_reference_list'))

    return render_template('product_reference_form.html', ref=ref)


@app.route('/reference/<int:ref_id>/delete', methods=['POST'])
@login_required
def product_reference_delete(ref_id):
    if g.user['role'] != 'admin':
        flash('Admin access required.', 'error')
        return redirect(url_for('product_reference_list'))
    db.delete_product_reference(ref_id)
    flash('Product reference deleted.', 'success')
    return redirect(url_for('product_reference_list'))


HEADER_MAP = {
    'codename': 'codename',
    'model name': 'model_name',
    'wi-fi gen': 'wifi_gen',
    'wifi gen': 'wifi_gen',
    'year': 'year',
    'wireless chip set manufacturer': 'chip_manufacturer',
    'wireless chipset manufacturer': 'chip_manufacturer',
    'chip manufacturer': 'chip_manufacturer',
    'wireless chipset codename': 'chip_codename',
    'chip codename': 'chip_codename',
    'fw codebase': 'fw_codebase',
    'print technology': 'print_technology',
    'variant': 'variant',
}


@app.route('/reference/import', methods=['POST'])
@login_required
def product_reference_import():
    """Import product references from an uploaded .xlsx or .csv file."""
    if g.user['role'] != 'admin':
        flash('Admin access required.', 'error')
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

        if filename.endswith('.xlsx'):
            import openpyxl
            wb = openpyxl.load_workbook(file, read_only=True, data_only=True)
            ws = wb.active
            rows = ws.iter_rows()
            raw_headers = [cell.value or '' for cell in next(rows)]
            headers = [HEADER_MAP.get(str(h).strip().lower()) for h in raw_headers]

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
            stream = io.TextIOWrapper(file.stream, encoding='utf-8-sig')
            # Auto-detect delimiter
            sample = stream.read(2048)
            stream.seek(0)
            delimiter = '\t' if '\t' in sample else ','
            reader = csv.reader(stream, delimiter=delimiter)
            raw_headers = next(reader)
            headers = [HEADER_MAP.get(h.strip().lower()) for h in raw_headers]

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
    if g.user['role'] != 'admin':
        flash('Admin access required.', 'error')
        return redirect(url_for('product_reference_list'))
    import csv, io
    refs = db.get_all_product_references()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Codename', 'Variant', 'Model Name', 'Print Technology', 'Wi-Fi Gen', 'Year',
                     'Wireless Chip Set Manufacturer', 'Wireless Chipset Codename', 'FW Codebase'])
    for r in refs:
        writer.writerow([r['codename'], r['variant'], r['model_name'], r['print_technology'],
                         r['wifi_gen'], r['year'], r['chip_manufacturer'],
                         r['chip_codename'], r['fw_codebase']])
    csv_bytes = output.getvalue().encode('utf-8-sig')
    return Response(csv_bytes, mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename=product_reference.csv'})


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
        'variant': r['variant'],
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
    parser = argparse.ArgumentParser(description='HP Connectivity Team Inventory System')
    parser.add_argument('--host', default='0.0.0.0', help='Host to bind to (default: 0.0.0.0)')
    parser.add_argument('--port', type=int, default=8080, help='Port to listen on (default: 8080)')
    parser.add_argument('--dev', action='store_true', help='Run in development mode with debug enabled')
    args = parser.parse_args()

    url = f'http://{args.host}:{args.port}'
    mode = 'DEVELOPMENT' if args.dev else 'PRODUCTION'
    w = 48  # inner width between ║ chars
    print(f"""
    ╔{'═' * w}╗
    ║{'HP Connectivity Team Inventory System':^{w}}║
    ║{'':^{w}}║
    ║{f'  Running at: {url}':<{w}}║
    ║{f'  Mode: {mode}':<{w}}║
    ║{'':^{w}}║
    ║{'  Press Ctrl+C to stop':<{w}}║
    ╚{'═' * w}╝
    """)

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
