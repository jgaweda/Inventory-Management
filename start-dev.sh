#!/usr/bin/env bash
# HP Connectivity Team Inventory System — Development Mode (Linux / macOS)

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo ""
echo "  Starting in DEVELOPMENT mode..."
echo ""

# Activate virtual environment if it exists
if [ -f "venv/bin/activate" ]; then
    source venv/bin/activate
fi

# Use python3 if available, otherwise python
PYTHON=$(command -v python3 2>/dev/null || command -v python 2>/dev/null)
if [ -z "$PYTHON" ]; then
    echo "ERROR: Python not found. Please install Python 3.10+."
    exit 1
fi

$PYTHON app.py --dev --host 127.0.0.1 --port 5000
