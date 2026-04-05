#!/usr/bin/env bash
# Build macOS executable for the Inventory Management System.
# Run from the project root: ./scripts/build_exe.sh
set -e

echo "=== Building Inventory System (macOS) ==="

# Resolve project root (parent of scripts/)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

# Create/activate venv if needed
if [ ! -d "build_venv" ]; then
    python3 -m venv build_venv
fi
source build_venv/bin/activate

# Install deps + pyinstaller
pip install --upgrade pip
pip install -r requirements.txt pyinstaller

# Build
pyinstaller --clean scripts/inventory.spec

echo ""
echo "=== Build complete ==="
echo "Output: dist/InventorySystem/"
echo "Run:    ./dist/InventorySystem/InventorySystem"
echo ""
echo "To distribute: zip the dist/InventorySystem/ folder."
