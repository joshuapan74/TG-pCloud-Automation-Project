"""
TG Auto Downloader — macOS py2app packaging script (generic, zero bundled secrets).
Usage:
  python3 setup.py py2app
"""

from setuptools import setup

PY_MODULES = [
    "platform_utils",
    "utils",
    "file_service",
    "queue_service",
    "tg_service",
    "gui_app",
]

OPTIONS = {
    "py2app": {
        "argv_emulation": False,
        "emulate_shell_environment": True,
        "iconfile": None,
        "packages": [
            "telethon",
            "pystray",
            "PIL",
            "psutil",
            "dotenv",
            "aiofiles",
        ],
        "includes": [
            "asyncio",
            "threading",
            "subprocess",
            "zipfile",
            "shutil",
            "sqlite3",
            "json",
            "tkinter",
            "tkinter.filedialog",
            "tkinter.messagebox",
            "tkinter.ttk",
        ],
        "excludes": [
            "matplotlib",
            "numpy",
            "scipy",
            "PyInstaller",
            "test",
            "unittest",
            "doctest",
        ],
        "plist": {
            "CFBundleName": "TG Auto Downloader",
            "CFBundleDisplayName": "TG Auto Downloader",
            "CFBundleIdentifier": "com.tgautodownloader.app",
            "CFBundleVersion": "3.0.0",
            "CFBundleShortVersionString": "3.0.0",
            "LSUIElement": True,
            "NSAppTransportSecurity": {
                "NSAllowsArbitraryLoads": True,
            },
        },
    }
}

setup(
    name="TG_Auto_Downloader",
    version="3.0.0",
    description="Cross-platform Telegram Saved Messages auto downloader & sync folder mover",
    py_modules=PY_MODULES,
    app=["main.py"],
    options=OPTIONS,
    setup_requires=["py2app"],
)
