@echo off
REM Build Windows .exe via PyInstaller (no personal secrets or sessions bundled)
setlocal
cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
    python -m venv venv
)
call venv\Scripts\activate.bat
pip install -r requirements.txt pyinstaller

if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

pyinstaller ^
    --noconsole ^
    --onefile ^
    --name "TG Auto Downloader" ^
    --icon "assets\app_icon.ico" ^
    --add-data "assets;assets" ^
    --hidden-import pystray._win32 ^
    --hidden-import PIL._tkinter_finder ^
    main.py

echo.
echo Build complete: dist\TG Auto Downloader.exe
endlocal
