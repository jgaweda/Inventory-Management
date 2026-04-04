# HP Connectivity Team Inventory Management System

A self-contained inventory management system for tracking printers, routers, laptops, phones/tablets, and other hardware devices. Built for long-term use by the HP Connectivity Team.

## Quick Start

### Linux / macOS

```bash
pip3 install -r requirements.txt
./start.sh
```

### Windows

```cmd
pip install -r requirements.txt
start.bat
```

Open **http://localhost:8080** in your browser. The database is created automatically on first run.

### Development Mode

```bash
./start-dev.sh        # Linux / macOS
start-dev.bat         # Windows
```

Runs on `127.0.0.1:8080` with Flask debug mode and auto-reload.

## Features

- **Device tracking** — Add, edit, search, and retire hardware devices
- **Full-text search** — Search across all device fields (name, category, serial number, location, etc.)
- **Barcode labels** — Auto-generated QR codes and Code 128 barcodes for each device
- **Label printing** — Print individual labels sized for DYMO LabelWriter 450 (3.5" x 1.125")
- **Barcode scanning** — USB barcode scanner support with auto-detection + camera QR scanning
- **Check-out/in** — Track who has which device with full audit trail
- **CSV import/export** — Bulk import from CSV; export with current search filters applied
- **Database backups** — Configurable auto-backup with retention policy; manual backups kept indefinitely
- **Git backup push** — Zip and push backups to a dedicated git branch with Personal Access Token auth
- **Restore** — Restore from local backups or directly from git with one click
- **Dashboard** — At-a-glance stats, category breakdowns, recent activity
- **Application log** — Filterable log viewer with category chips (Device Updates, Scans, Auth, Import/Export, User Mgmt)
- **User management** — Role-based access (admin/viewer); public read-only routes for the network
- **Dark / light theme** — Toggle in the sidebar; preference saved per browser

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

### Exporting to CSV

1. Go to **Devices** and apply any search/filter
2. Click **Export CSV** — the export matches your current filters

### Configuring Backups

1. Go to **Backups** in the sidebar
2. Set backup directory, max backups to keep, and auto-backup interval
3. Optionally configure git push with a GitHub repo URL, branch, and Personal Access Token
4. Click **Save Configuration**

Auto-backups are pruned to the configured maximum. Manual backups (created via "Backup Now") are never pruned.

## Tech Stack

- **Backend**: Python 3.10+ / Flask 3.x
- **WSGI Server**: Waitress (production, cross-platform)
- **Database**: SQLite3 (WAL mode, single file)
- **Labels**: qrcode + python-barcode + Pillow
- **Frontend**: HTML + Vanilla JS + Tailwind CSS (CDN)

## Building Standalone Executables

The project includes build scripts that use PyInstaller to create self-contained executables (no Python installation needed on the target machine).

### macOS / Linux

**Prerequisites:** Python 3.10+

```bash
chmod +x build_exe.sh
./build_exe.sh
```

The script creates a virtual environment (`build_venv/`), installs all dependencies plus PyInstaller, and builds into `dist/InventorySystem/`. To run the built executable:

```bash
./dist/InventorySystem/InventorySystem
```

### Windows

**Prerequisites:** Python 3.8.x (required for Windows 7 compatibility — [download here](https://www.python.org/downloads/release/python-3819/), use the "Windows x86-64 executable installer")

```cmd
build_exe.bat
```

The script creates a virtual environment, installs Windows 7–compatible dependency versions from `requirements_win7.txt`, and builds into `dist\InventorySystem\`. To run:

```cmd
dist\InventorySystem\InventorySystem.exe
```

### Distribution

Zip the entire `dist/InventorySystem/` folder and share it. The recipient just unzips and runs the executable — no Python or pip required.

### Automated Builds (GitHub Actions)

The repository includes a GitHub Actions workflow (`.github/workflows/build-executables.yml`) that automatically builds executables for both macOS and Windows when a version tag is pushed or via manual dispatch.

## Cross-Platform Support

The application runs on **macOS**, **Windows**, and **Linux** with no platform-specific code. Default port is 8080 (avoids macOS AirPlay Receiver conflict on port 5000).
