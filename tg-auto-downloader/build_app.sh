#!/usr/bin/env bash
# Build macOS .app bundle via PyInstaller (no personal secrets or sessions bundled)
set -e
cd "$(dirname "$0")"

if [ ! -d "venv" ]; then
    python3 -m venv venv
fi
source venv/bin/activate
pip install -r requirements.txt pyinstaller

rm -rf build dist
pyinstaller \
    --windowed \
    --onedir \
    --name "TG Auto Downloader" \
    --icon "assets/app_icon.png" \
    --add-data "assets:assets" \
    --osx-bundle-identifier "com.tgautodownloader.app" \
    --hidden-import pystray._darwin \
    main.py

echo "Build complete: dist/TG Auto Downloader.app"
