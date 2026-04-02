@echo off
title HP Inventory System (DEV)
echo.
echo  Starting in DEVELOPMENT mode...
echo.

if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
)

python app.py --dev --host 127.0.0.1 --port 5000

pause
