@echo off
cd /d "%~dp0"
set PY=python
where python >nul 2>nul || set PY=py
%PY% main.py
echo.
echo ClipDock exited. Log file: %APPDATA%\ClipDock\clipdock.log
pause
