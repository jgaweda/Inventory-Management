@echo off
title HP Connectivity Team Inventory System
echo.
echo  Starting HP Connectivity Team Inventory System...
echo  ================================================
echo.

:: Activate virtual environment if it exists
if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
)

:: Generate a secret key if not set
if "%SECRET_KEY%"=="" (
    for /f %%i in ('python -c "import secrets; print(secrets.token_hex(32))"') do set SECRET_KEY=%%i
)

:: Start the production server
python app.py --host 0.0.0.0 --port 5000

pause
