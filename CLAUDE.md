# CLAUDE.md

## Project Overview

Inventory Management System — a Flask-based web application for tracking IT devices, product references, and associated documentation. Uses SQLite (WAL mode) with direct SQL (no ORM), Jinja2 templates, and vanilla JS with Tailwind CSS on the frontend. Distributed as single-file executables via PyInstaller for Windows and macOS.

## Quick Start

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
./scripts/start-dev.sh          # Dev server on 127.0.0.1:8080
```

Default login: `admin` / `admin`

## Key Commands

| Task | Command |
|------|---------|
| Dev server | `./scripts/start-dev.sh` or `python app.py --dev` |
| Production server | `./scripts/start.sh` or `python app.py` |
| Run all tests | `python3 -m pytest tests/ -v` |
| Run single test file | `python3 -m pytest tests/test_devices.py -v` |
| Build executable | `./scripts/build_exe.sh` |
| Reset admin password | `python app.py --reset-admin NEW_PASSWORD` |
| Export DB to SQL | `python app.py --export-sql dump.sql` |
| Emergency backup | `python app.py --emergency-backup [PATH]` |
| Convert PNG→JPG | `python app.py --convert-png-to-jpg` |

## Architecture

```
app.py                      — Flask routes, auth, scheduler (~3100 lines, monolithic)
database.py                 — All SQLite CRUD operations, migrations, schema (~3100 lines)
barcode_utils.py            — Barcode/QR code & label image generation (~370 lines)
runtime_dirs.py             — Path resolution (bundled PyInstaller vs. source)
import_product_reference.py — CSV/XLSX product reference importer
templates/                  — Jinja2 HTML templates (14 files)
static/                     — Runtime-generated label images
tests/                      — pytest test suite (unittest style with unittest.mock)
seed_data/                  — Default CSV + images for product references
scripts/                    — Build and startup scripts (sh + bat)
.github/workflows/          — CI/CD pipeline for multi-platform builds
```

There is no separate models layer — `database.py` handles schema, migrations, and all data access. Routes in `app.py` are organized by feature area.

### Runtime Directories (runtime_dirs.py)

| Variable | PyInstaller (.exe) | Running from source |
|----------|-------------------|---------------------|
| `BUNDLE_DIR` | `sys._MEIPASS` (read-only assets) | Project root |
| `DATA_DIR` | Directory containing .exe (writable) | Project root |

- Read-only assets (templates, static, seed_data) → `BUNDLE_DIR`
- Writable user data (database, backups, logs, labels) → `DATA_DIR`

## Code Conventions

- **Python style**: snake_case for functions/variables, UPPER_CASE for constants, `_prefix` for private helpers
- **Indentation**: 4 spaces
- **Database access**: Always use the `db_transaction()` context manager for writes
- **Auth**: `@permission_required('permission_name')` decorator on protected routes
- **Error handling**: try/except with logging; Flask `flash()` for user-facing messages
- **No linter config**: No .flake8, pylintrc, or pyproject.toml — follow existing code patterns
- **Tests**: unittest + unittest.mock, one test file per feature area
- **No ORM**: Raw SQL queries with `sqlite3.Row` for dict-like row access

## Route Map

### Authentication
| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET/POST | `/login` | Public | Login with rate limiting (10 attempts/5 min per IP) |
| GET | `/logout` | Public | Session clear |

### Dashboard & Public
| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/` | Public | Redirects to `/scan` |
| GET | `/health` | Public | JSON health check |
| GET | `/docs` | Login | Help documentation |
| GET | `/scan` | Public | Barcode scanner page |
| GET | `/api/lookup` | Public | JSON barcode lookup |
| GET | `/favicon.ico` | Public | Favicon |

### Devices
| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/devices` | Public | List/search with filters (category, status, connectivity, location, codename) |
| GET/POST | `/devices/add` | `devices` | Create new device |
| GET | `/devices/<id>` | Public | Detail view with audit log |
| GET/POST | `/devices/<id>/edit` | `devices` | Edit device |
| POST | `/devices/<id>/retire` | `retire` | Retire with reason |
| POST | `/devices/<id>/checkout` | `devices` | Assign to user |
| POST | `/devices/<id>/checkin` | `devices` | Return to available |
| POST | `/devices/<id>/notes` | Public | Add note (tracks author) |
| POST | `/devices/<id>/notes/<note_id>/delete` | `devices` | Delete note |
| POST | `/devices/<id>/upload` | `devices` | Upload attachment |
| GET | `/device/attachment/<id>` | Public | Download attachment |
| GET | `/device/attachment/<id>/preview` | Public | Inline preview |
| POST | `/device/attachment/<id>/delete` | `devices` | Delete attachment |

### Labels & Export
| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/labels/<id>.png` | Public | PNG barcode label (1050x450 px, 300 DPI) |
| GET | `/labels/<id>.pdf` | Public | PDF barcode label |
| POST | `/labels/sheet` | `devices` | Multi-label PDF sheet (3x6 grid) |
| GET | `/export` | Public | CSV export with filters |
| GET | `/export/xlsx` | Public | Excel export |

### Product References
| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/reference` | Public | List/search references |
| GET/POST | `/reference/add` | `references` | Create reference |
| GET/POST | `/reference/<id>/edit` | `references` | Edit reference |
| PATCH | `/api/reference/<id>` | `references` | Inline field update |
| POST | `/reference/<id>/delete` | `references` | Delete reference + wiki |
| POST | `/reference/seed` | `references` | Load default CSV data |
| POST | `/reference/import` | `references` | Import CSV/XLSX/TSV |
| GET | `/reference/export` | Public | CSV export |
| GET | `/reference/export/xlsx` | Public | Excel export |
| GET | `/reference/export/zip` | Public | ZIP with wiki attachments |

### Product Wiki
| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/wiki/<ref_id>` | Public | View wiki page (markdown) |
| POST | `/wiki/<ref_id>/save` | `wiki` | Save content |
| POST | `/wiki/<ref_id>/upload` | `wiki` | Upload attachment |
| GET | `/wiki/attachment/<id>` | Public | Download attachment |
| GET | `/wiki/attachment/<id>/preview` | Public | Inline preview |
| POST | `/wiki/attachment/<id>/delete` | `wiki` | Delete attachment |
| POST | `/wiki/repair` | `wiki` | Fix orphaned attachments |

### Users
| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/users` | `users` | List users |
| GET/POST | `/users/add` | `users` | Create user |
| GET/POST | `/users/<id>/edit` | `users` | Edit user |
| POST | `/users/<id>/delete` | `users` | Delete user (blocks last admin) |
| GET/POST | `/account` | Login | Change own password |

### Backups
| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/backups` | `backups` | Backup dashboard |
| POST | `/backups/create` | `backups` | Manual backup |
| POST | `/backups/upload` | `backups` | Upload backup file |
| POST | `/backups/config` | `backups` | Update all backup settings |
| GET | `/backups/export-encryption-key` | `backups` | Download encryption key |
| POST | `/backups/config/reset` | `backups` | Reset to defaults |
| GET | `/backups/browse-directory` | `backups` | AJAX directory browser |
| POST | `/backups/push` | `backups` | Trigger immediate push |
| GET | `/backups/local/list` | `backups` | List local backups |
| GET | `/backups/git/list` | `backups` | List git backups |
| POST | `/backups/git/restore` | `backups` | Restore from git (re-auth required) |
| POST | `/backups/filepath/push` | `backups` | Push to file path |
| GET | `/backups/filepath/list` | `backups` | List file path backups |
| POST | `/backups/filepath/restore` | `backups` | Restore from file path (re-auth required) |
| POST | `/backups/<filename>/delete` | `backups` | Delete backup |
| GET | `/backups/<filename>/download` | `backups` | Download backup |
| POST | `/backups/<filename>/restore` | `backups` | Restore backup |

### Logs & Settings
| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/logs` | `logs` | View application logs |
| POST | `/logs/clear` | `logs` | Clear logs |
| GET | `/logs/export` | `logs` | Export logs CSV |
| POST | `/logs/config` | `logs` | Configure max log size |
| POST | `/settings/server` | `settings` | Update server port |
| POST | `/settings/guest-permissions` | `settings` | Configure public access |
| POST | `/admin/update/check` | `settings` | Check for app updates |
| POST | `/admin/update/apply` | `settings` | Apply update + restart |

### API Endpoints
| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/devices/distinct/<field>` | Public | Distinct values for filters |
| GET | `/api/reference/search` | Public | Search references by query |

## Database

### Connection & Configuration

SQLite with WAL journal mode, stored as `inventory.db` in the data directory.

**PRAGMAs applied on every connection:**
- `journal_mode=WAL` — concurrent reads, non-blocking writes
- `foreign_keys=ON` — referential integrity
- `busy_timeout=30000` — 30s timeout on lock contention
- `synchronous=NORMAL` — balance durability/speed
- `cache_size=-8000` — 8 MB page cache
- `temp_store=MEMORY` — temp tables in RAM

### Transaction Pattern

```python
@contextmanager
def db_transaction():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
```

All writes must use `with db_transaction() as conn:`.

### Schema (10 tables)

| Table | Purpose | Key Columns |
|-------|---------|-------------|
| `devices` | Core inventory items | device_id (PK), barcode_value (UNIQUE), name, category, device_type, is_mesh, manufacturer, model_number, serial_number, connectivity, status, location, assigned_to, codename, variant |
| `audit_log` | Append-only change history | log_id, device_id (FK), action, performed_by, timestamp, details |
| `categories` | Device categories | category_id, name (UNIQUE), description, sort_order |
| `users` | Authentication & roles | user_id, username (UNIQUE), password_hash, salt, role, permissions (JSON), display_name, password_hint |
| `product_reference` | Product spec catalog | ref_id, codename, model_name, wifi_gen, year, chip_manufacturer, chip_codename, fw_codebase, print_technology, cartridge_toner, variant, predecessor |
| `product_wiki` | Community notes per product | wiki_id, ref_id (FK, UNIQUE), content, updated_by, updated_at |
| `wiki_attachments` | Wiki file uploads | attachment_id, ref_id (FK), filename, original_name, content_type, size_bytes |
| `device_attachments` | Device file uploads | attachment_id, device_id (FK), filename, original_name, content_type, size_bytes |
| `device_notes` | Public device notes | note_id, device_id (FK), author, content, created_at |
| `schema_info` | Metadata (version, config) | key (PK), value — stores schema_version, app_version, guest_permissions |
| `barcode_seq` | Barcode sequence counter | id (CHECK id=1), next_val |

### Schema Migrations

- `SCHEMA_VERSION = 2` in database.py
- Tracked in `schema_info` table; migrations run automatically on startup via `init_db()`
- Safe to call repeatedly — uses `ALTER TABLE ... ADD COLUMN` with error handling for existing columns
- Legacy role migration: editor/power_user/viewer → custom role with per-user JSON permissions

### Barcode Generation

Uses a linear congruential generator (LCG) permutation to scramble sequential IDs:
- Prefix: `CNX-`
- Alphabet: 30 chars (`23456789ABCDEFGHJKMNPQRSTVWXYZ`) — excludes ambiguous O, I, L, U, 0, 1
- Space: 30^6 = 729M unique barcodes
- Bijective mapping — no collisions, hides creation order

### Key Database Functions by Area

**Devices:** `add_device()`, `update_device()`, `get_device()`, `get_device_by_barcode()`, `get_device_by_serial()`, `search_devices()`, `retire_device()`, `checkout_device()`, `checkin_device()`

**Users:** `authenticate_user()`, `create_user()`, `update_user()`, `delete_user()`, `get_guest_permissions()`, `save_guest_permissions()`

**Backups:** `backup_database()`, `restore_database()`, `push_backups_to_git()`, `push_backups_to_filepath()`, `restore_from_git()`, `restore_from_filepath()`, `verify_backup()`, `emergency_backup()`, `get_backup_health()`

**References:** `get_all_product_references()`, `add_product_reference()`, `upsert_product_reference()`, `update_product_reference()`, `delete_product_reference()`

**Wiki:** `get_wiki_by_ref_id()`, `save_wiki()`, `get_wiki_attachments()`, `add_wiki_attachment()`, `check_attachment_integrity()`

**Audit:** `log_action()`, `get_audit_log()`, `get_stats()`

## Authentication & Authorization

### Roles & Permissions

| Role | Permissions |
|------|-------------|
| `admin` | All: devices, references, wiki, users, backups, logs, settings, retire |
| `custom` | Per-user from JSON permissions field |
| Guest (not logged in) | Defaults to `references` and `wiki`; configurable via settings |

**Admin-only permissions** (not assignable to custom): `users`, `settings`

### Security Features

- Rate limiting: 10 login attempts per IP per 5-minute window (in-memory)
- Password hashing: SHA-256 with per-user salt
- Open redirect protection on login `next` parameter
- Session-based auth with Flask `secret_key`
- Re-authentication required for cloud/filepath backup restore
- Minimum 4-character passwords

### Request Lifecycle

1. `@app.before_request load_user()` — loads `g.user` from session
2. `@app.before_request _check_scheduler_health()` — self-healing scheduler check (every 60s)
3. `@app.context_processor inject_globals()` — injects `categories`, `current_user`, `has_permission`, `app_version`, `backup_health` into all templates

## Scheduler & Background Tasks

Single persistent daemon thread (`backup-scheduler`) that wakes every 5 seconds to check for due tasks. Self-healing: restarted via `before_request` hook if thread dies.

| Task | Default Interval | Config Key | Action |
|------|-----------------|------------|--------|
| Backup | 24h | `backup_interval_hours` | `db.backup_database()` |
| Git push | 24h | `git_push_interval_hours` | `db.push_to_git()` |
| File path push | 24h | `filepath_push_interval_hours` | `db.push_to_filepath()` |
| Prune | 24h | `prune_interval_hours` | `db._smart_prune_backups()` |
| Verify | 24h (fixed) | N/A | Rotates through backups checking integrity |

All tasks retry on failure with exponential backoff: [2, 5, 15] minutes. Overdue tasks run within 5 seconds of startup.

### Deferred Startup Thread

Runs `startup_integrity_check()` and `check_attachment_integrity()` in a background daemon thread on app start.

## Backup System

- **Local backups**: SQLite online backup API → `.zip` bundles (db + wiki_uploads + device_uploads), smart pruning (keep 1/day for 7 days + max N). `include_uploads` config toggle controls upload bundling.
- **Git backups**: Clone repo, push encrypted ZIP (AES-256 via pyzipper), incremental commits, no force-push
- **File path backups**: Same as git but to local/network path (SharePoint, USB, etc.)
- **Change detection**: SHA-256 hash of DB file; skips automated backup if unchanged
- **Safety**: Creates safety backup before restore, rolls back on failure
- **Verification**: Rotates through backup files checking SQLite integrity; catches silent corruption

## Barcode & Label Generation (barcode_utils.py)

| Function | Description |
|----------|-------------|
| `generate_label()` | 1050x450 px (3.5"x1.5" @ 300 DPI) with QR code, device name, Code 128 barcode. DYMO printer margins. |
| `generate_label_sheet()` | US Letter page (2550x3300 px), 3x6 grid = 18 labels per page |
| `generate_qr_code()` | QR with H-level error correction, "JG" monogram in center |
| `generate_barcode_image()` | Code 128, rendered at 2x DPI then downscaled for crisp bars |

Multi-platform font resolution (DejaVu, Liberation, Helvetica, Arial) with caching and fallback.

## Testing

### Test Files

| File | Lines | Coverage |
|------|-------|----------|
| `test_auth.py` | 682 | Login, rate limiting, RBAC, guest permissions, user CRUD |
| `test_general.py` | 820 | Barcode generation/encoding, scrambling, safe alphabet, migration |
| `test_backup.py` | 1054 | Backup/restore, skip-if-unchanged, integrity, concurrency |
| `test_devices.py` | 551 | Device CRUD, barcode lookup, checkout/checkin, export, labels |
| `test_references.py` | 552 | Reference CRUD, inline PATCH, import/export |
| `test_wiki.py` | 242 | Wiki pages, attachments, integrity, extension filtering |

### Test Patterns

- **Base class**: `BaseTestCase` in `tests/__init__.py` — fresh SQLite DB per test, test client, admin login helper
- **Isolation**: Each test gets its own temp directory and database via `INVENTORY_DATA_DIR`
- **Mocking**: `unittest.mock.patch` for `BUNDLE_DIR`, app config, filesystem operations
- **Fixtures**: Pre-populated admin user (admin/admin)

## Templates

| Template | Page |
|----------|------|
| `base.html` | Master layout — nav, flash messages, CSS/JS includes |
| `login.html` | Standalone login (no navbar) |
| `dashboard.html` | Home — stats, health, recent activity |
| `devices.html` | Inventory list with filters |
| `device_form.html` | Add/edit device form |
| `device_detail.html` | Device view with audit log, QR, barcode |
| `scan.html` | Barcode scanner interface |
| `product_reference.html` | Reference list with inline edit |
| `product_reference_form.html` | Add/edit reference form |
| `product_wiki.html` | Wiki page with markdown, attachments, toolbar |
| `user_form.html` | Add/edit user with permission checkboxes |
| `users.html` | User management list |
| `account.html` | Account settings, password change |
| `backups.html` | Backup management dashboard |
| `app_log.html` | Log viewer |
| `docs.html` | Help documentation |

All templates extend `base.html` and use `{% if has_permission(...) %}` for conditional UI.

## Dependencies

```
Flask>=3.0,<4.0          # Web framework
waitress>=3.0,<4.0       # Production WSGI server (8 threads)
qrcode[pil]>=8.0,<9.0    # QR code generation
python-barcode>=0.15,<1.0 # Code 128 barcode generation
Pillow>=11.0,<12.0       # Image processing for labels
openpyxl>=3.1,<4.0       # Excel file handling
pyzipper>=0.3,<1.0       # AES-256 encrypted ZIP for backups
```

Frontend: Tailwind CSS 3.4.1 (CDN), Google Fonts (DM Sans, JetBrains Mono), vanilla JS.

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `SECRET_KEY` | Flask session secret (auto-generated if unset) |
| `INVENTORY_DATA_DIR` | Override data directory path (used in tests) |

## Runtime Configuration Files

| File | Purpose |
|------|---------|
| `log_config.json` | Log rotation max size (default 2 MB) |
| `server_config.json` | Server host/port (default 0.0.0.0:8080) |
| `backup_config.json` | Full backup settings (intervals, git/filepath config, encryption, prune settings) |

These are created in the app directory at runtime, not checked into git.

## Git & CI

- **Main branch**: `ims-app`
- **Version**: Tracked in `VERSION` file (currently 1.1.31), auto-bumped by CI
- **Triggers**: Push to `ims-app` (ignoring VERSION), daily at 5 UTC, manual dispatch

### CI Pipeline (.github/workflows/build-executables.yml)

1. **check-changes** — Skip if only VERSION changed
2. **bump-version** — Increment patch version, commit with `[skip ci]`
3. **build-windows** — Python 3.8 on windows-latest (Windows 7 compatible), PyInstaller
4. **build-macos** — Python 3.12 on macos-14, PyInstaller + ad-hoc code signing
5. **release** — Create GitHub release with zipped executables

## Error Handling

- `404` — Logs path/method/IP/user-agent, redirects to dashboard
- `500` — Logs full traceback, redirects to dashboard
- Unhandled exceptions — Catch-all with full traceback logging, redirects to dashboard
- Logging: `RotatingFileHandler` at `DATA_DIR/logs/app.log`, format: `%(asctime)s | %(levelname)-7s | %(message)s`

## File Storage

| Directory | Content |
|-----------|---------|
| `DATA_DIR/` | `inventory.db`, config JSON files |
| `DATA_DIR/logs/` | `app.log` (rotating) |
| `DATA_DIR/backups/` | Backup `.zip` bundles (db + uploads) |
| `DATA_DIR/wiki_uploads/` | Wiki attachment files |
| `DATA_DIR/device_uploads/` | Device attachment files |
| `DATA_DIR/static/labels/` | Generated label PNGs (cache) |
