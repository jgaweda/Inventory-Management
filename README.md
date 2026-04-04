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
3. If macOS blocks the app, open Terminal and run:
   ```bash
   xattr -cr ~/Downloads/InventorySystem-macOS/
   ```
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
./start.sh
```

Open **http://localhost:8080** in your browser. The database is created automatically on first run.

**Default login:** username `admin`, password `admin` (change after first login).

## Features

- **Device tracking** -- Add, edit, search, and retire hardware devices
- **Full-text search** -- Search across all device fields (name, category, serial number, location, etc.)
- **Barcode labels** -- Auto-generated QR codes and Code 128 barcodes for each device
- **Label printing** -- Print individual labels sized for DYMO LabelWriter 450 (3.5" x 1.5")
- **Barcode scanning** -- USB barcode scanner support with auto-detection + fullscreen camera QR scanning
- **Check-out/in** -- Track who has which device with full audit trail
- **CSV import/export** -- Bulk import from CSV; export with current search filters applied
- **Database backups** -- Configurable auto-backup with skip-if-unchanged, smart retention policy
- **Git backup push** -- Zip and push backups to a dedicated git branch with Personal Access Token auth
- **Restore** -- Restore from local backups or directly from git with one click
- **Dashboard** -- At-a-glance stats, category breakdowns, recent activity
- **Application log** -- Filterable log viewer with category chips
- **User management** -- Role-based access (admin/viewer); public read-only routes for the network
- **Dark / light theme** -- Toggle in the sidebar; preference saved per browser

## Tech Stack

- **Backend**: Python 3.10+ / Flask 3.x
- **WSGI Server**: Waitress (production, cross-platform)
- **Database**: SQLite3 (WAL mode, single file)
- **Labels**: qrcode + python-barcode + Pillow
- **Frontend**: HTML + Vanilla JS + Tailwind CSS (CDN)

## Running from Source (Development)

If you prefer to run from source instead of the executable:

```bash
python3 -m venv venv
source venv/bin/activate          # macOS/Linux
# venv\Scripts\activate           # Windows
pip install -r requirements.txt
./start-dev.sh                    # or start-dev.bat on Windows
```

Runs on `127.0.0.1:8080` with Flask debug mode and auto-reload.

## Building Executables

### Using the build scripts

**macOS / Linux** (requires Python 3.10+):
```bash
chmod +x build_exe.sh
./build_exe.sh
# Output: dist/InventorySystem/InventorySystem
```

**Windows** (requires [Python 3.8](https://www.python.org/downloads/release/python-3819/) for Win7 compatibility):
```cmd
build_exe.bat
:: Output: dist\InventorySystem\InventorySystem.exe
```

Zip the `dist/InventorySystem/` folder to distribute.

### Automated builds (GitHub Actions)

The repository automatically builds macOS and Windows executables when a version tag is pushed:

```bash
git tag v1.0.0
git push origin v1.0.0
```

This creates a **GitHub Release** with downloadable zip files for both platforms. Builds can also be triggered manually from the Actions tab.

## Cross-Platform Support

The application runs on **macOS**, **Windows**, and **Linux** with no platform-specific code. Default port is 8080 (avoids macOS AirPlay Receiver conflict on port 5000).
