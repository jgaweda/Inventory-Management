#!/usr/bin/env bash
# Legacy backup script — backups are now managed in-app via the Backups page.
# This script is kept for optional cron-based backup as a safety net.
#
# Usage: ./backup.sh
# Recommended: run via cron, e.g. daily at 2am:
#   0 2 * * * /path/to/inventory-system/backup.sh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DB_PATH="$SCRIPT_DIR/inventory.db"
BACKUP_DIR="$SCRIPT_DIR/backups"
MAX_BACKUPS=30

# Create backup directory if needed
mkdir -p "$BACKUP_DIR"

# Check that the database exists
if [ ! -f "$DB_PATH" ]; then
    echo "ERROR: Database not found at $DB_PATH"
    exit 1
fi

# Create backup with timestamp
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
BACKUP_FILE="$BACKUP_DIR/auto_backup_${TIMESTAMP}.db"
cp "$DB_PATH" "$BACKUP_FILE"

if [ $? -eq 0 ]; then
    echo "Backup created: $BACKUP_FILE"
else
    echo "ERROR: Backup failed"
    exit 1
fi

# Keep only the last N auto backups (works on macOS and Linux)
cd "$BACKUP_DIR"
ls -t auto_backup_*.db 2>/dev/null | tail -n +$((MAX_BACKUPS + 1)) | while read -r f; do
    rm -f "$f"
done
echo "Backup cleanup complete. $(ls auto_backup_*.db 2>/dev/null | wc -l | tr -d ' ') auto backups retained."
