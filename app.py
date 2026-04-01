"""
HP Connectivity Team Inventory Management System — Flask Application

All routes are defined here. Run with: python app.py [--host HOST] [--port PORT]
"""

import argparse
import csv
import io
import os
from datetime import datetime

from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, send_file, jsonify, Response,
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
    # Ensure directories exist
    os.makedirs(os.path.join(app.static_folder, 'labels'), exist_ok=True)
    os.makedirs(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'backups'), exist_ok=True)

# ---------------------------------------------------------------------------
# Context processor: inject categories and current time into all templates
# ---------------------------------------------------------------------------

@app.context_processor
def inject_globals():
    return {
        'categories': db.get_categories(),
        'now': datetime.utcnow(),
    }

# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@app.route('/')
def dashboard():
    stats = db.get_stats()
    return render_template('dashboard.html', stats=stats)

# ---------------------------------------------------------------------------
# Device list (search & filter)
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
# Add device
# ---------------------------------------------------------------------------

@app.route('/devices/add', methods=['GET', 'POST'])
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
            'firmware_version': request.form.get('firmware_version', ''),
            'connectivity': request.form.get('connectivity', ''),
            'location': request.form.get('location', ''),
            'notes': request.form.get('notes', ''),
        }
        performed_by = request.form.get('performed_by', 'system')
        device_id = db.add_device(data, performed_by=performed_by)

        # Generate label
        device = db.get_device(device_id)
        barcode_utils.generate_label(device_id, device['barcode_value'], device['name'])

        flash(f'Device "{name}" added successfully.', 'success')
        return redirect(url_for('device_detail', device_id=device_id))

    return render_template('device_form.html', device={}, is_edit=False)

# ---------------------------------------------------------------------------
# Device detail
# ---------------------------------------------------------------------------

@app.route('/devices/<device_id>')
def device_detail(device_id):
    device = db.get_device(device_id)
    if not device:
        flash('Device not found.', 'error')
        return redirect(url_for('device_list'))

    # Ensure label exists
    if not barcode_utils.label_exists(device_id):
        barcode_utils.generate_label(device_id, device['barcode_value'], device['name'])

    audit = db.get_audit_log(device_id=device_id, limit=50)
    return render_template('device_detail.html', device=device, audit=audit)

# ---------------------------------------------------------------------------
# Edit device
# ---------------------------------------------------------------------------

@app.route('/devices/<device_id>/edit', methods=['GET', 'POST'])
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
            'firmware_version': request.form.get('firmware_version', ''),
            'connectivity': request.form.get('connectivity', ''),
            'status': request.form.get('status', device['status']),
            'location': request.form.get('location', ''),
            'assigned_to': request.form.get('assigned_to', ''),
            'notes': request.form.get('notes', ''),
        }
        performed_by = request.form.get('performed_by', 'system')
        db.update_device(device_id, data, performed_by=performed_by)

        # Regenerate label (name may have changed)
        barcode_utils.generate_label(device_id, device['barcode_value'], name)

        flash(f'Device "{name}" updated successfully.', 'success')
        return redirect(url_for('device_detail', device_id=device_id))

    return render_template('device_form.html', device=device, is_edit=True, device_id=device_id)

# ---------------------------------------------------------------------------
# Retire device (soft delete)
# ---------------------------------------------------------------------------

@app.route('/devices/<device_id>/retire', methods=['POST'])
def device_retire(device_id):
    performed_by = request.form.get('performed_by', 'system')
    db.retire_device(device_id, performed_by=performed_by)
    flash('Device retired successfully.', 'success')
    return redirect(url_for('device_list'))

# ---------------------------------------------------------------------------
# Check out / Check in
# ---------------------------------------------------------------------------

@app.route('/devices/<device_id>/checkout', methods=['POST'])
def device_checkout(device_id):
    assigned_to = request.form.get('assigned_to', '').strip()
    if not assigned_to:
        flash('Please enter who is checking out this device.', 'error')
        return redirect(url_for('device_detail', device_id=device_id))

    performed_by = request.form.get('performed_by', 'system')
    db.checkout_device(device_id, assigned_to, performed_by=performed_by)
    flash(f'Device checked out to {assigned_to}.', 'success')
    return redirect(url_for('device_detail', device_id=device_id))


@app.route('/devices/<device_id>/checkin', methods=['POST'])
def device_checkin(device_id):
    performed_by = request.form.get('performed_by', 'system')
    db.checkin_device(device_id, performed_by=performed_by)
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
# CSV Export
# ---------------------------------------------------------------------------

@app.route('/export')
def export_csv():
    """Export all devices (including retired) to CSV."""
    devices = db.get_all_devices(include_retired=True)

    output = io.StringIO()
    fields = ['device_id', 'barcode_value', 'name', 'category', 'manufacturer',
              'model_number', 'serial_number', 'firmware_version', 'connectivity',
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
# CSV Import
# ---------------------------------------------------------------------------

@app.route('/import', methods=['GET', 'POST'])
def import_csv():
    if request.method == 'POST':
        file = request.files.get('file')
        if not file or not file.filename:
            flash('Please select a CSV file.', 'error')
            return render_template('import.html')

        performed_by = request.form.get('performed_by', 'system')
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
                    data = {
                        'name': name,
                        'category': row.get('category', ''),
                        'manufacturer': row.get('manufacturer', ''),
                        'model_number': row.get('model_number', ''),
                        'serial_number': row.get('serial_number', ''),
                        'firmware_version': row.get('firmware_version', ''),
                        'connectivity': row.get('connectivity', ''),
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
# Audit log
# ---------------------------------------------------------------------------

@app.route('/audit')
def audit_log():
    entries = db.get_audit_log(limit=200)
    return render_template('audit.html', entries=entries)

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
    ║   Press Ctrl+C to stop                           ║
    ╚══════════════════════════════════════════════════╝
    """)

    app.run(host=args.host, port=args.port, debug=True)
