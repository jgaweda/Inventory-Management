"""
HP Connectivity Team Inventory Management System — Flask Application

All routes are defined here. Run with: python app.py [--host HOST] [--port PORT]

Authentication: Admins (scanner terminal) can add/edit/checkout/retire/import devices.
Anyone on the network can view the inventory without logging in.
"""

import argparse
import csv
import io
import os
from functools import wraps
from datetime import datetime

from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, send_file, jsonify, Response, session, g,
)

import database as db
import barcode_utils

app = Flask(__name__, static_folder='static', template_folder='templates')
app.secret_key = 'hp-connectivity-inventory-system-secret-key'

# ---------------------------------------------------------------------------
# Startup: initialize the database
# ---------------------------------------------------------------------------

with app.app_context():
    db.init_db()
    os.makedirs(os.path.join(app.static_folder, 'labels'), exist_ok=True)
    os.makedirs(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'backups'), exist_ok=True)

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
        'now': datetime.utcnow(),
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
            next_url = request.form.get('next') or url_for('dashboard')
            return redirect(next_url)
        else:
            flash('Invalid username or password.', 'error')

    return render_template('login.html', next=request.args.get('next', ''))


@app.route('/logout')
def logout():
    session.clear()
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
    devices = db.search_devices(
        query=request.args.get('q', ''),
        category=request.args.get('category', ''),
        status=request.args.get('status', ''),
        connectivity=request.args.get('connectivity', ''),
        location=request.args.get('location', ''),
    )
    return render_template('devices.html', devices=devices,
                           q=request.args.get('q', ''),
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
        flash('Device not found.', 'error')
        return redirect(url_for('device_list'))

    if not barcode_utils.label_exists(device_id):
        barcode_utils.generate_label(device_id, device['barcode_value'], device['name'])

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
    flash(f'Device checked out to {assigned_to}.', 'success')
    return redirect(url_for('device_detail', device_id=device_id))


@app.route('/devices/<device_id>/checkin', methods=['POST'])
@admin_required
def device_checkin(device_id):
    db.checkin_device(device_id, performed_by=current_username())
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
            return 'Device not found', 404
        barcode_utils.generate_label(device_id, device['barcode_value'], device['name'])

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

    return send_file(buffer, mimetype='image/png', as_attachment=True,
                     download_name='label_sheet.png')

# ---------------------------------------------------------------------------
# Scanner page and API
# ---------------------------------------------------------------------------

@app.route('/scan')
def scan_page():
    return render_template('scan.html')


@app.route('/api/lookup')
def api_lookup():
    """JSON API for barcode scanner lookup. Case-insensitive."""
    barcode = request.args.get('barcode', '').strip()
    if not barcode:
        return jsonify({'found': False, 'error': 'No barcode provided'}), 400

    device = db.get_device_by_barcode(barcode)
    if device:
        return jsonify({
            'found': True,
            'device_id': device['device_id'],
            'name': device['name'],
            'status': device['status'],
            'assigned_to': device['assigned_to'],
            'location': device['location'],
        })
    else:
        return jsonify({'found': False}), 404

# ---------------------------------------------------------------------------
# CSV Export (public)
# ---------------------------------------------------------------------------

@app.route('/export')
def export_csv():
    """Export all devices (including retired) to CSV."""
    devices = db.get_all_devices(include_retired=True)

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
# CSV Import (admin only)
# ---------------------------------------------------------------------------

@app.route('/import', methods=['GET', 'POST'])
@admin_required
def import_csv():
    if request.method == 'POST':
        file = request.files.get('file')
        if not file or not file.filename:
            flash('Please select a CSV file.', 'error')
            return render_template('import.html')

        performed_by = current_username()
        imported = 0
        errors = 0

        try:
            stream = io.TextIOWrapper(file.stream, encoding='utf-8-sig')
            reader = csv.DictReader(stream)
            for row in reader:
                name = row.get('name', '').strip()
                if not name:
                    errors += 1
                    continue
                try:
                    vendor_val = row.get('vendor_supplied', '0').strip().lower()
                    data = {
                        'name': name,
                        'category': row.get('category', ''),
                        'manufacturer': row.get('manufacturer', ''),
                        'model_number': row.get('model_number', ''),
                        'serial_number': row.get('serial_number', ''),
                        'connectivity': row.get('connectivity', ''),
                        'vendor_supplied': 1 if vendor_val in ('1', 'yes', 'true') else 0,
                        'location': row.get('location', ''),
                        'notes': row.get('notes', ''),
                    }
                    device_id = db.add_device(data, performed_by=performed_by)
                    device = db.get_device(device_id)
                    barcode_utils.generate_label(device_id, device['barcode_value'], device['name'])
                    imported += 1
                except Exception:
                    errors += 1
        except Exception as e:
            flash(f'Error reading CSV file: {e}', 'error')
            return render_template('import.html')

        flash(f'Import complete: {imported} devices imported, {errors} errors.', 'success')
        return redirect(url_for('device_list'))

    return render_template('import.html')

# ---------------------------------------------------------------------------
# Audit log (public)
# ---------------------------------------------------------------------------

@app.route('/audit')
def audit_log():
    entries = db.get_audit_log(limit=200)
    return render_template('audit.html', entries=entries)

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
        flash(f'User "{user["username"]}" updated.', 'success')
        return redirect(url_for('user_list'))

    return render_template('user_form.html', user=user, is_edit=True)


@app.route('/users/<int:user_id>/delete', methods=['POST'])
@admin_required
def user_delete(user_id):
    try:
        db.delete_user(user_id)
        flash('User deleted.', 'success')
    except ValueError as e:
        flash(str(e), 'error')
    return redirect(url_for('user_list'))

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
        flash('Password changed successfully.', 'success')
        return redirect(url_for('account'))

    return render_template('account.html')

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
