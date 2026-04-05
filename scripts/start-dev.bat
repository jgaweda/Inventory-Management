@echo off
title HP Inventory System (DEV)
echo.
echo  Starting in DEVELOPMENT mode...
echo.

:: Navigate to project root (parent of scripts\)
cd /d "%~dp0\.."

python app.py --dev --host 127.0.0.1 --port 8080

pause
