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
- **Device attachments** -- Upload files (images, PDFs, docs) to any device; inline image preview, download, and delete with permission control
- **CSV import/export** -- Bulk import from CSV/XLSX; export with current search filters applied

### Labels & Scanning
- **Barcode labels** -- Auto-generated QR codes and Code 128 barcodes with CNX- prefix
- **Scrambled IDs** -- 6-character IDs from a safe 30-character alphabet (no ambiguous characters like O/0, I/1/L) using a linear congruential permutation for non-sequential, collision-free codes
- **Scanner-optimized layout** -- 3.5" x 1.5" labels at 300 DPI; barcode rendered at 2x then downscaled with nearest-neighbor for crisp, whole-pixel bar edges
- **Label printing** -- PDF and PNG output sized for DYMO LabelWriter 450
- **Barcode scanning** -- USB barcode scanner support with auto-detection + fullscreen camera QR/barcode scanning

### Product Reference & Wiki
- **Product catalog** -- Inline-editable spreadsheet of printer/device specs (codename, model, chipset, Wi-Fi gen, cartridge/toner, etc.)
- **Seed data import** -- One-click import of default product reference data from CSV
- **Product wiki** -- Community notepad per product for testing nuances, known issues, and configuration tips
- **Wiki attachments** -- Admin file upload (images, PDFs, docs, spreadsheets, logs); all users can download
- **Image preview** -- Uploaded images display as inline thumbnails on the wiki page
- **Wiki integrity repair** -- Automatic cleanup of orphaned attachment records when files are missing, plus manual repair endpoint
- **Import/Export** -- Bulk import product references from CSV or XLSX; export to CSV

### Backups & Recovery
- **Automatic backups** -- Configurable interval with skip-if-unchanged detection (compares DB hash)
- **Smart retention** -- Keeps at least one backup per day for 7 days, then applies max backup limit
- **Cloud backup** -- Zip and push backups + wiki uploads to a dedicated git branch with PAT auth
- **AES-256 encryption** -- Optional encryption password for cloud backups; data is unreadable on GitHub without the password
- **Encryption key export** -- Download your encryption password as a text file for safekeeping, with manual decryption instructions
- **Restore** -- Restore from local backups or directly from cloud with one click
- **Post-restore validation** -- Full database compatibility check after restore; automatic rollback to safety backup on failure
- **Admin password required** -- Cloud restore requires admin password re-entry for safety
- **Backup health monitoring** -- Dashboard alerts when backups are overdue (only when the database has actually changed)
- **Emergency backup** -- SQL export for disaster recovery scenarios
- **Self-healing scheduler** -- Backup scheduler automatically recovers from crashes; health checked every 60 seconds
- **Thread-safe operations** -- File locks prevent concurrent backup verify/prune/delete races; scheduler lock prevents duplicate threads
- **Microsecond-precision filenames** -- Prevents naming collisions when multiple backups run concurrently
- **Git token sanitization** -- Access tokens are stripped from all log output to prevent credential leaks
- **Config corruption recovery** -- Corrupt backup config JSON is logged and falls back to defaults instead of crashing

### System
- **Dashboard** -- At-a-glance stats, category breakdowns, recent activity
- **Application log** -- Paginated log viewer with category filters (Inventory, Users, Data, System) and export to .log file
- **User management** -- Role-based access control:
  - **Admin** -- Full access to all features
  - **Custom** -- Granular permissions: devices, references, wiki, wiki admin, backups, logs, retire, notes delete
  - **Public** -- Read-only routes accessible without login
- **Password hints** -- Optional per-user hints displayed on the login page after a failed attempt
- **In-app updates** -- Admin can check for and apply code updates from the git repository
- **Dark / light theme** -- Toggle in the sidebar; preference saved per browser
- **Health endpoint** -- `GET /health` returns JSON with database integrity status
- **Rate limiting** -- Login brute-force protection (10 attempts per 5 minutes per IP)

## Tech Stack

- **Backend**: Python 3.10+ / Flask 3.x
- **WSGI Server**: Waitress (production, cross-platform)
- **Database**: SQLite3 (WAL mode, single file)
- **Labels**: qrcode + python-barcode + Pillow
- **Encryption**: pyzipper (AES-256 for cloud backups)
- **Spreadsheets**: openpyxl (XLSX import/export)
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
python3 -m pytest tests/ -v              # all tests
python3 -m pytest tests/test_devices.py  # just device tests
python3 -m pytest tests/test_auth.py     # just auth tests
python3 -m pytest tests/test_references.py  # just product reference tests
python3 -m pytest tests/test_wiki.py     # just wiki tests
python3 -m pytest tests/test_backup.py   # just backup tests
python3 -m pytest tests/test_general.py  # labels, export, search, etc.
```

363 tests organized by feature: devices, auth, references, wiki, backups, and general.

## Project Structure

```
app.py                  # Flask application (routes, middleware, scheduler)
database.py             # Database layer (schema, CRUD, backups, cloud push)
barcode_utils.py        # Barcode/QR code image generation and label layout
import_product_reference.py  # CSV/XLSX product reference importer
templates/              # Jinja2 HTML templates (15 templates)
static/                 # CSS, JS, images, generated labels
seed_data/              # Default product reference CSV + images
scripts/                # Start scripts, build scripts, PyInstaller spec
tests/                  # Test suite (6 modules, 363 tests)
  __init__.py           #   Shared BaseTestCase and test infrastructure
  test_devices.py       #   Device CRUD, checkout, notes, attachments
  test_auth.py          #   Login, roles, permissions, users, password hints
  test_references.py    #   Product references, seed import, cartridge/toner
  test_wiki.py          #   Wiki pages, attachments, markdown, integrity
  test_backup.py        #   Backups, scheduler, encryption, cloud restore, robustness
  test_general.py       #   Barcodes, labels, export, search, dashboard
```

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
