@echo off
cd /d "%~dp0"
set PY=python
where python >nul 2>nul || set PY=py
%PY% -m pip install -r requirements.txt pyinstaller || goto :fail
%PY% -m PyInstaller --noconfirm --clean --windowed --onefile --name ClipDock --icon assets\clipdock.ico main.py || goto :fail
echo.
echo Done: dist\ClipDock.exe
pause
exit /b 0
:fail
echo.
echo Build failed, see the messages above.
pause
exit /b 1
