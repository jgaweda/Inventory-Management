@echo off
REM Build Windows executable for the Inventory Management System.
REM Run from the project root: scripts\build_exe.bat
REM
REM IMPORTANT - Windows 7 requires Python 3.8.x
REM   Download from: https://www.python.org/downloads/release/python-3819/
REM   Use the "Windows x86-64 executable installer"

echo === Building Inventory System (Windows) ===

cd /d "%~dp0\.."

REM Create venv if needed
if not exist "build_venv" (
    python -m venv build_venv
)
call build_venv\Scripts\activate.bat

REM Install deps + pyinstaller
pip install --upgrade pip
pip install -r requirements_win7.txt pyinstaller
pip install pyinstaller

REM Build
pyinstaller --clean scripts\inventory.spec

echo.
echo === Build complete ===
echo Output: dist\InventorySystem\
echo Run:    dist\InventorySystem\InventorySystem.exe
echo.
echo To distribute: zip the dist\InventorySystem\ folder.
pause
