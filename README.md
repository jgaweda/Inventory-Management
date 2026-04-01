# HP Connectivity Team Inventory Management System

A simple, self-contained inventory management system for tracking Wi-Fi modules, Bluetooth dongles, dev boards, antennas, and test equipment.

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Run the application
python app.py

# 3. Open in browser
# http://127.0.0.1:5000
```

The database (`inventory.db`) is created automatically on first run with sample data.

## Features

- **Device tracking** — Add, edit, search, and retire hardware devices
- **Barcode labels** — Auto-generated QR codes and Code 128 barcodes for each device
- **Label printing** — Print individual labels or full sheets (US Letter, 3x5 grid)
- **Barcode scanning** — USB barcode scanner support with auto-detection + camera QR scanning
- **Check-out/in** — Track who has which device with full audit trail
- **CSV import/export** — Bulk import devices from CSV, export full inventory
- **Audit log** — Append-only log of all actions (add, edit, checkout, return, retire)
- **Dashboard** — At-a-glance stats, category breakdowns, recent activity

## Common Tasks

### Adding a Device
1. Click **Add Device** in the sidebar
2. Fill in at least the device name
3. A barcode label is generated automatically

### Checking Out a Device
1. Go to the device detail page
2. Enter the person's name in the "Check Out" form
3. Click **Check Out**

### Scanning Barcodes
1. Click **Scan** in the sidebar
2. **USB scanner**: Just scan — the barcode input auto-detects rapid input
3. **Camera**: Click "Enable Camera" and point at a QR code

### Printing Labels
1. Go to **Devices** list
2. Check the devices you want labels for
3. Click **Print Labels** to download a printable sheet

### Importing from CSV
1. Click **Import CSV** in the sidebar
2. Upload a CSV file with headers: `name,category,manufacturer,model_number,serial_number,firmware_version,connectivity,location,notes`
3. Only `name` is required; other fields are optional

### Backing Up
```bash
./backup.sh
```
This copies `inventory.db` to `backups/` with a timestamp. Keeps the last 30 copies.

## Tech Stack

- **Backend**: Python 3.10+ / Flask
- **Database**: SQLite3 (WAL mode, single file)
- **Labels**: qrcode + python-barcode + Pillow
- **Frontend**: HTML + Vanilla JS + Tailwind CSS (CDN)
- **No build step, no Node.js, no React**

## Project Structure

```
├── app.py              # Flask routes
├── database.py         # SQLite CRUD, schema, audit log
├── barcode_utils.py    # QR/barcode/label generation
├── requirements.txt    # Python dependencies
├── backup.sh           # Database backup script
├── inventory.db        # SQLite database (auto-created)
├── static/labels/      # Generated label PNGs
├── templates/          # HTML templates
└── backups/            # Database backups
```

## Troubleshooting

**"Module not found" errors**: Run `pip install -r requirements.txt`

**Labels not generating**: Ensure `static/labels/` directory exists (created automatically on startup)

**Database locked**: The system uses WAL mode for better concurrency. If the database is locked, ensure no other process has an exclusive lock on `inventory.db`.

**Port already in use**: Run with a different port: `python app.py --port 5001`

**Reset everything**: Delete `inventory.db` and restart. The database and sample data will be recreated.
