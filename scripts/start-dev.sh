#!/usr/bin/env bash
# HP Connectivity Team Inventory System — Development Mode (Linux / macOS)

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

echo ""
echo "  Starting in DEVELOPMENT mode..."
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

    # If Flask is missing, create a venv and install dependencies
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

exec "$PYTHON" app.py --dev --host 127.0.0.1 --port 8080
