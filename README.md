# Connectivity Team Inventory Management System

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

## Running from Source (Development)

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
## Project Structure

```
app.py                  # Flask application (routes, middleware, scheduler)
database.py             # Database layer (schema, CRUD, backups, cloud push)
barcode_utils.py        # Barcode/QR code image generation and label layout
import_product_reference.py  # CSV/XLSX product reference importer
templates/              # Jinja2 HTML templates
static/                 # CSS, JS, images, generated labels
seed_data/              # Default product reference CSV + images
scripts/                # Start scripts, build scripts, PyInstaller spec
tests/                  # Test suite
  __init__.py           #   Shared BaseTestCase and test infrastructure
  test_devices.py       #   Device CRUD, checkout, notes, attachments
  test_auth.py          #   Login, roles, permissions, users, password hints
  test_references.py    #   Product references, seed import, cartridge/toner
  test_wiki.py          #   Wiki pages, attachments, markdown, integrity
  test_backup.py        #   Backups, scheduler, encryption, cloud restore
  test_general.py       #   Barcodes, labels, export, search, dashboard
```

## Tech Stack

- **Backend**: Python 3.10+ / Flask 3.x
- **WSGI Server**: Waitress (production, cross-platform)
- **Database**: SQLite3 (WAL mode, single file)
- **Frontend**: HTML + Vanilla JS + CSS custom properties
