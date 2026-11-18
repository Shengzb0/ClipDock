@echo off
cd /d "%~dp0"
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw main.py
    exit /b 0
)
where pyw >nul 2>nul
if %errorlevel%==0 (
    start "" pyw main.py
    exit /b 0
)
echo Python not found. Install Python 3.9+ from https://www.python.org and tick "Add python.exe to PATH".
pause
