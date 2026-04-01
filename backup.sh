#!/bin/bash
# Backup script for the HP Connectivity Team Inventory System
# Copies inventory.db to backups/ with a timestamp. Keeps the last 30 backups.
#
# Usage: ./backup.sh
# Recommended: run via cron, e.g. daily at 2am:
#   0 2 * * * /path/to/inventory-system/backup.sh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DB_PATH="$SCRIPT_DIR/inventory.db"
BACKUP_DIR="$SCRIPT_DIR/backups"

# Create backup directory if needed
mkdir -p "$BACKUP_DIR"

# Check that the database exists
if [ ! -f "$DB_PATH" ]; then
    echo "ERROR: Database not found at $DB_PATH"
    exit 1
fi

# Create backup with timestamp
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
BACKUP_FILE="$BACKUP_DIR/inventory_${TIMESTAMP}.db"
cp "$DB_PATH" "$BACKUP_FILE"

if [ $? -eq 0 ]; then
    echo "Backup created: $BACKUP_FILE"
else
    echo "ERROR: Backup failed"
    exit 1
fi

# Keep only the last 30 backups (delete oldest)
cd "$BACKUP_DIR"
ls -t inventory_*.db 2>/dev/null | tail -n +31 | xargs -r rm --
echo "Backup cleanup complete. $(ls inventory_*.db 2>/dev/null | wc -l) backups retained."
