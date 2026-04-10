#!/usr/bin/env bash
# HP Connectivity Team Inventory System — Production Start (Linux / macOS)

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

echo ""
echo "  Starting HP Connectivity Team Inventory System..."
echo "  ================================================"
echo ""

# Prefer a project-local venv if present, otherwise use system python
VENV_DIR="$PROJECT_DIR/venv"
if [ -d "$VENV_DIR" ] && [ -f "$VENV_DIR/bin/python" ]; then
    PYTHON="$VENV_DIR/bin/python"
    echo "  Using venv: $VENV_DIR"
else
    PYTHON=$(command -v python3 2>/dev/null || command -v python 2>/dev/null)
    if [ -z "$PYTHON" ]; then
        echo "ERROR: Python not found. Please install Python 3.10+."
        exit 1
    fi

    # If Flask is missing, offer to create a venv and install dependencies
    if ! "$PYTHON" -c 'import flask' >/dev/null 2>&1; then
        echo "  Flask is not installed for $PYTHON."
        echo "  Creating a virtual environment at $VENV_DIR and installing dependencies..."
        "$PYTHON" -m venv "$VENV_DIR"
        PYTHON="$VENV_DIR/bin/python"
        "$PYTHON" -m pip install --upgrade pip --quiet
        "$PYTHON" -m pip install -r "$PROJECT_DIR/requirements.txt" --quiet
        echo "  Dependencies installed."
    fi
fi

# Generate a secret key if not set (persists for the session)
if [ -z "$SECRET_KEY" ]; then
    export SECRET_KEY="$($PYTHON -c 'import secrets; print(secrets.token_hex(32))')"
fi

# Start the production server
exec "$PYTHON" app.py --host 0.0.0.0 --port 8080
