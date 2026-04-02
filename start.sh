#!/usr/bin/env bash
# HP Connectivity Team Inventory System — Production Start (Linux / macOS)

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo ""
echo "  Starting HP Connectivity Team Inventory System..."
echo "  ================================================"
echo ""

# Use python3 if available, otherwise python
PYTHON=$(command -v python3 2>/dev/null || command -v python 2>/dev/null)
if [ -z "$PYTHON" ]; then
    echo "ERROR: Python not found. Please install Python 3.10+."
    exit 1
fi

# Generate a secret key if not set (persists for the session)
if [ -z "$SECRET_KEY" ]; then
    export SECRET_KEY="$($PYTHON -c 'import secrets; print(secrets.token_hex(32))')"
fi

# Start the production server
$PYTHON app.py --host 0.0.0.0 --port 8080
