# HP Connectivity Team Inventory Management System

A self-contained inventory management system for tracking printers, routers, laptops, phones/tablets, and other hardware devices. Built for long-term use by the HP Connectivity Team.

## Download

Pre-built executables are available on the [**Releases page**](../../releases/latest). No Python installation required.

| Platform | Download | Requirements |
|----------|----------|--------------|
| **macOS** | `InventorySystem-macOS.zip` | macOS 12+ (Apple Silicon) |
| **Windows** | `InventorySystem-Windows.zip` | Windows 7+ |

## Installation & Running

### macOS

1. Download `InventorySystem-macOS.zip` from [Releases](../../releases/latest)
2. Unzip the file
3. **Required:** Open Terminal and run this to remove the macOS quarantine flag:
   ```bash
   xattr -cr ~/Downloads/InventorySystem-macOS/
   ```
   *(macOS blocks all apps downloaded outside the App Store — this is normal)*
4. Double-click `InventorySystem` or run from Terminal:
   ```bash
   ./InventorySystem
   ```
5. Open **http://localhost:8080** in your browser

### Windows

1. Download `InventorySystem-Windows.zip` from [Releases](../../releases/latest)
2. Unzip the folder
3. Double-click `InventorySystem.exe`
4. Open **http://localhost:8080** in your browser

### Linux (from source)

```bash
pip3 install -r requirements.txt
./scripts/start.sh
```

Open **http://localhost:8080** in your browser. The database is created automatically on first run.

**Default login:** username `admin`, password `admin` (change after first login).

## Features

### Inventory Management
- **Device tracking** -- Add, edit, search, and retire hardware devices
- **Full-text search** -- Search across all device fields (name, category, serial, location, etc.)
- **Check-out/in** -- Track who has which device with full audit trail
- **Duplicate detection** -- Serial number uniqueness enforced across active devices
- **Ownership tracking** -- Mark devices as HP Owned or Vendor Supplied
- **CSV import/export** -- Bulk import from CSV/XLSX; export with current search filters applied

### Labels & Scanning
- **Barcode labels** -- Auto-generated QR codes and Code 128 barcodes with CNX- prefix (base-36 sequential)
- **Professional labels** -- 3.5" x 1.5" labels at 300 DPI with barcode-dominant layout, device name, and ID
- **Label printing** -- PDF and PNG output sized for DYMO LabelWriter 450
- **Barcode scanning** -- USB barcode scanner support with auto-detection + fullscreen camera QR/barcode scanning

### Product Reference & Wiki
- **Product catalog** -- Inline-editable spreadsheet of printer/device specs (codename, model, chipset, Wi-Fi gen, etc.)
- **Product wiki** -- Community notepad per product for testing nuances, known issues, and configuration tips
- **Wiki attachments** -- Admin file upload (images, PDFs, docs, spreadsheets, logs); all users can download
- **Image preview** -- Uploaded images display as inline thumbnails on the wiki page
- **Import/Export** -- Bulk import product references from CSV or XLSX; export to CSV

### System
- **Dashboard** -- At-a-glance stats, category breakdowns, recent activity
- **Database backups** -- Configurable auto-backup with skip-if-unchanged, smart retention policy
- **Git backup push** -- Zip and push backups to a dedicated git branch with Personal Access Token auth
- **Restore** -- Restore from local backups or directly from git with one click
- **Application log** -- Paginated, filterable log viewer
- **User management** -- Role-based access (admin/viewer); public read-only routes for the network
- **Dark / light theme** -- Toggle in the sidebar; preference saved per browser
- **Health endpoint** -- `GET /health` returns JSON with database integrity status
- **Rate limiting** -- Login brute-force protection (10 attempts per 5 minutes per IP)

## Tech Stack

- **Backend**: Python 3.10+ / Flask 3.x
- **WSGI Server**: Waitress (production, cross-platform)
- **Database**: SQLite3 (WAL mode, single file)
- **Labels**: qrcode + python-barcode + Pillow
- **Frontend**: HTML + Vanilla JS + CSS custom properties (dark/light theme)

## Running from Source (Development)

If you prefer to run from source instead of the executable:

```bash
python3 -m venv venv
source venv/bin/activate          # macOS/Linux
# venv\Scripts\activate           # Windows
pip install -r requirements.txt
./scripts/start-dev.sh            # or scripts\start-dev.bat on Windows
```

Runs on `127.0.0.1:8080` with Flask debug mode and auto-reload.

## Running Tests

```bash
python3 -m unittest discover -s tests -v    # all tests
python3 -m unittest tests.test_devices -v   # just device tests
python3 -m unittest tests.test_backup -v    # just backup tests
```

318 tests organized by feature: devices, auth, references, wiki, backups, and general.

## Building Executables

### Using the build scripts

**macOS / Linux** (requires Python 3.10+):
```bash
chmod +x scripts/build_exe.sh
./scripts/build_exe.sh
# Output: dist/InventorySystem/InventorySystem
```

**Windows** (requires [Python 3.8](https://www.python.org/downloads/release/python-3819/) for Win7 compatibility):
```cmd
scripts\build_exe.bat
:: Output: dist\InventorySystem\InventorySystem.exe
```

Zip the `dist/InventorySystem/` folder to distribute.

### Automated builds (GitHub Actions)

Nightly builds run automatically and create versioned GitHub Releases with downloadable zip files for both platforms. The version number auto-increments on each build. Builds can also be triggered manually from the Actions tab.

## Cross-Platform Support

The application runs on **macOS**, **Windows**, and **Linux** with no platform-specific code. Default port is 8080 (avoids macOS AirPlay Receiver conflict on port 5000).
