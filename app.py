"""
HP Connectivity Team Inventory Management System — Flask Application

All routes are defined here. Run with: python app.py [--host HOST] [--port PORT]

Authentication: Admins (scanner terminal) can add/edit/checkout/retire/import devices.
Anyone on the network can view the inventory without logging in.
"""

import argparse
import csv
import io
import logging
import os
from functools import wraps
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, send_file, jsonify, Response, session, g,
)

import database as db
import barcode_utils

app = Flask(__name__, static_folder='static', template_folder='templates')
app.secret_key = 'hp-connectivity-inventory-system-secret-key'

# ---------------------------------------------------------------------------
# Application logging (rotating file, single file that overwrites at limit)
# ---------------------------------------------------------------------------

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
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
_log_handler = RotatingFileHandler(LOG_FILE, maxBytes=_log_max_bytes, backupCount=0)
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
    return render_template('dashboard.html', stats=stats)

# ---------------------------------------------------------------------------
# Device list (public)
# ---------------------------------------------------------------------------

@app.route('/devices')
def device_list():
    q = request.args.get('q', '')
    devices = db.search_devices(
        query=q,
        category=request.args.get('category', ''),
        status=request.args.get('status', ''),
        connectivity=request.args.get('connectivity', ''),
        location=request.args.get('location', ''),
    )
    if q:
        app_logger.info('Device search: query="%s" results=%d ip=%s', q, len(devices), request.remote_addr)
    return render_template('devices.html', devices=devices,
                           q=q,
                           selected_category=request.args.get('category', ''),
                           selected_status=request.args.get('status', ''),
                           selected_connectivity=request.args.get('connectivity', ''),
                           selected_location=request.args.get('location', ''))

# ---------------------------------------------------------------------------
# Add device (admin only)
# ---------------------------------------------------------------------------

@app.route('/devices/add', methods=['GET', 'POST'])
@admin_required
def device_add():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        if not name:
            flash('Device name is required.', 'error')
            return render_template('device_form.html', device=request.form, is_edit=False)

        data = {
            'name': name,
            'category': request.form.get('category', ''),
            'manufacturer': request.form.get('manufacturer', ''),
            'model_number': request.form.get('model_number', ''),
            'serial_number': request.form.get('serial_number', ''),
            'connectivity': request.form.get('connectivity', ''),
            'vendor_supplied': 1 if request.form.get('vendor_supplied') else 0,
            'location': request.form.get('location', ''),
            'notes': request.form.get('notes', ''),
        }
        device_id = db.add_device(data, performed_by=current_username())

        # Generate label
        device = db.get_device(device_id)
        barcode_utils.generate_label(device_id, device['barcode_value'], device['name'])

        app_logger.info('Device added: id=%s name="%s" by=%s', device_id, name, current_username())
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
        barcode_utils.generate_label(device_id, device['barcode_value'], device['name'])
        app_logger.debug('Label generated on-the-fly: id=%s', device_id)

    app_logger.info('Device viewed: id=%s name="%s" ip=%s', device_id, device['name'], request.remote_addr)
    audit = db.get_audit_log(device_id=device_id, limit=50)
    return render_template('device_detail.html', device=device, audit=audit)

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
        name = request.form.get('name', '').strip()
        if not name:
            flash('Device name is required.', 'error')
            return render_template('device_form.html', device=request.form, is_edit=True, device_id=device_id)

        data = {
            'name': name,
            'category': request.form.get('category', ''),
            'manufacturer': request.form.get('manufacturer', ''),
            'model_number': request.form.get('model_number', ''),
            'serial_number': request.form.get('serial_number', ''),
            'connectivity': request.form.get('connectivity', ''),
            'vendor_supplied': 1 if request.form.get('vendor_supplied') else 0,
            'status': request.form.get('status', device['status']),
            'location': request.form.get('location', ''),
            'assigned_to': request.form.get('assigned_to', ''),
            'notes': request.form.get('notes', ''),
        }
        db.update_device(device_id, data, performed_by=current_username())

        barcode_utils.generate_label(device_id, device['barcode_value'], name)

        app_logger.info('Device updated: id=%s name="%s" by=%s', device_id, name, current_username())
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

@app.route('/labels/<device_id>.png')
def serve_label(device_id):
    """Serve a label PNG, generating it on the fly if it doesn't exist."""
    if not barcode_utils.label_exists(device_id):
        device = db.get_device(device_id)
        if not device:
            app_logger.warning('Label requested for unknown device: id=%s', device_id)
            return 'Device not found', 404
        barcode_utils.generate_label(device_id, device['barcode_value'], device['name'])
        app_logger.info('Label generated: id=%s name="%s"', device_id, device['name'])

    path = barcode_utils.get_label_path(device_id)
    return send_file(path, mimetype='image/png')


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
    """Export all devices (including retired) to CSV."""
    devices = db.get_all_devices(include_retired=True)
    app_logger.info('CSV export: %d devices ip=%s', len(devices), request.remote_addr)

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
        role = request.form.get('role', 'viewer')
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
    try:
        with open(LOG_FILE, 'r') as f:
            lines = f.readlines()
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
    log_file_size = os.path.getsize(LOG_FILE) if os.path.exists(LOG_FILE) else 0
    return render_template('app_log.html', entries=entries, log_config=log_config, log_file_size=log_file_size)


@app.route('/logs/clear', methods=['POST'])
@admin_required
def clear_logs():
    """Clear the application log file."""
    try:
        with open(LOG_FILE, 'w') as f:
            f.write('')
        app_logger.info('Application log cleared by %s', current_username())
        flash('Application log cleared.', 'success')
    except Exception as e:
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

    return render_template('account.html')

# ---------------------------------------------------------------------------
# Database backup (admin only)
# ---------------------------------------------------------------------------

import threading

_backup_timer = None      # Timer for recurring local backups
_git_push_timer = None    # Timer for recurring git pushes


def _run_scheduled_backup():
    """Execute a scheduled backup and re-arm the timer."""
    try:
        result = db.backup_database(performed_by='scheduled')
        app_logger.info('Scheduled backup completed: %s (%d bytes)',
                        result['filename'], result['size'])
    except Exception as e:
        app_logger.error('Scheduled backup failed: %s', e)
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
        app_logger.error('Scheduled git push failed: %s', e)
    # Re-arm from latest config
    config = db._get_backup_config()
    if config.get('git_enabled'):
        _start_git_push_timer(config['git_push_interval_hours'])


def _start_backup_timer(interval_hours):
    """Start (or restart) the recurring backup timer."""
    global _backup_timer
    _stop_backup_timer()
    seconds = max(interval_hours * 3600, 300)  # Minimum 5 minutes
    _backup_timer = threading.Timer(seconds, _run_scheduled_backup)
    _backup_timer.daemon = True
    _backup_timer.start()
    app_logger.info('Backup scheduler armed: next backup in %s hours', interval_hours)


def _stop_backup_timer():
    """Cancel any pending scheduled backup."""
    global _backup_timer
    if _backup_timer is not None:
        _backup_timer.cancel()
        _backup_timer = None


def _start_git_push_timer(interval_hours):
    """Start (or restart) the recurring git push timer."""
    global _git_push_timer
    _stop_git_push_timer()
    seconds = max(interval_hours * 3600, 300)  # Minimum 5 minutes
    _git_push_timer = threading.Timer(seconds, _run_scheduled_git_push)
    _git_push_timer.daemon = True
    _git_push_timer.start()
    app_logger.info('Git push scheduler armed: next push in %s hours', interval_hours)


def _stop_git_push_timer():
    """Cancel any pending scheduled git push."""
    global _git_push_timer
    if _git_push_timer is not None:
        _git_push_timer.cancel()
        _git_push_timer = None


# Restore timers on startup
_startup_config = db._get_backup_config()
if _startup_config.get('backup_enabled'):
    _start_backup_timer(_startup_config['backup_interval_hours'])
if _startup_config.get('git_enabled') and _startup_config.get('git_repo'):
    _start_git_push_timer(_startup_config['git_push_interval_hours'])


@app.route('/backups')
@admin_required
def backup_list():
    """View backup management page."""
    backups = db.list_backups()
    config = db._get_backup_config()
    return render_template('backups.html', backups=backups, config=config)


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
        app_logger.error('Manual backup failed: %s by=%s', e, current_username())
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
        app_logger.error('Upload restore failed: %s by=%s', e, current_username())
        flash(f'Restore failed: {e}', 'error')
    except Exception as e:
        app_logger.error('Upload restore failed: %s by=%s', e, current_username())
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

    config['backup_enabled'] = request.form.get('backup_enabled') == '1'
    try:
        config['backup_interval_hours'] = max(0.1, float(request.form.get('backup_interval_hours', 24)))
    except (ValueError, TypeError):
        config['backup_interval_hours'] = 24

    # Git push settings
    config['git_enabled'] = request.form.get('git_enabled') == '1'
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
        app_logger.error('Git push failed: %s by=%s', e, current_username())
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
        app_logger.error('Local backup list failed: %s', e)
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/backups/git/list')
@admin_required
def backup_git_list():
    """API: list .db files available in the git backup zip."""
    try:
        entries = db.list_git_backups()
        return jsonify({'ok': True, 'backups': entries})
    except Exception as e:
        app_logger.error('Git backup list failed: %s', e)
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
        app_logger.error('Git restore failed: %s by=%s', e, current_username())
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
        app_logger.error('Backup delete failed: %s error=%s', filename, e)
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
        app_logger.error('Restore failed: %s by=%s', e, current_username())
        flash(f'Restore failed: {e}', 'error')
    except Exception as e:
        app_logger.error('Restore failed: %s by=%s', e, current_username())
        flash(f'Restore failed: {e}', 'error')
    return redirect(url_for('backup_list'))

# ---------------------------------------------------------------------------
# Global error handlers
# ---------------------------------------------------------------------------

@app.errorhandler(404)
def not_found(e):
    app_logger.warning('404 Not Found: %s ip=%s', request.path, request.remote_addr)
    flash('Page not found.', 'error')
    return redirect(url_for('dashboard'))


@app.errorhandler(500)
def internal_error(e):
    app_logger.error('500 Internal Server Error: %s — %s', request.path, e)
    flash('An unexpected error occurred.', 'error')
    return redirect(url_for('dashboard'))

# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='HP Connectivity Team Inventory System')
    parser.add_argument('--host', default='127.0.0.1', help='Host to bind to (default: 127.0.0.1)')
    parser.add_argument('--port', type=int, default=5000, help='Port to listen on (default: 5000)')
    args = parser.parse_args()

    print(f"""
    ╔══════════════════════════════════════════════════╗
    ║   HP Connectivity Team Inventory System          ║
    ║   Running at: http://{args.host}:{args.port}            ║
    ║   Database: {db.DB_PATH:<36s} ║
    ║   Default login: admin / admin                   ║
    ║   Press Ctrl+C to stop                           ║
    ╚══════════════════════════════════════════════════╝
    """)

    app.run(host=args.host, port=args.port, debug=True)
