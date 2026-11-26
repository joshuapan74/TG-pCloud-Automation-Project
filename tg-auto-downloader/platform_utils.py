"""Cross-platform utilities for Windows and macOS, including Apple-style icon generation."""

import json
import os
import subprocess
import sys
import tempfile
from typing import Any, Dict, Optional, Tuple

APP_NAME = "TGAutoDownloader"
APP_DISPLAY_NAME = "TG Auto Downloader"
MACOS_BUNDLE_ID = "com.tgautodownloader.app"
WINDOWS_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
WINDOWS_RUN_VALUE = "TGAutoDownloader"


def get_app_data_dir() -> str:
    """Return per-user OS application data directory and ensure it exists."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        path = os.path.join(base, APP_NAME)
    elif sys.platform == "darwin":
        path = os.path.expanduser(f"~/Library/Application Support/{APP_NAME}")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
        path = os.path.join(base, APP_NAME)

    os.makedirs(path, exist_ok=True)
    return path


def get_bundle_resource_path(relative_path: str) -> str:
    """Resolve bundled asset path for both source mode and PyInstaller/py2app frozen mode."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        base_dir = sys._MEIPASS
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_dir, relative_path)


def get_memory_usage_mb() -> float:
    """Return current process RSS memory usage in MB across platforms."""
    try:
        import psutil
        return psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)
    except Exception:
        pass

    if sys.platform != "win32":
        try:
            import resource
            rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            divisor = (1024 * 1024) if sys.platform == "darwin" else 1024
            return rss / divisor
        except Exception:
            pass
    return 0.0


def open_path_in_os(path: str) -> bool:
    """Open a file or folder in the OS default file manager / viewer."""
    if not path or not os.path.exists(path):
        return False
    try:
        if sys.platform == "win32":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return True
    except Exception:
        return False


def get_launch_command_args(extra_args: Optional[list] = None) -> list:
    """Build command list to re-invoke this app (supports frozen executables and scripts)."""
    extra_args = extra_args or []
    if getattr(sys, "frozen", False):
        return [sys.executable] + extra_args
    main_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")
    return [sys.executable, main_script] + extra_args


def _get_windows_autostart_cmd() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    python_exe = sys.executable
    pythonw_exe = os.path.join(os.path.dirname(python_exe), "pythonw.exe")
    runner = pythonw_exe if os.path.exists(pythonw_exe) else python_exe
    main_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")
    return f'"{runner}" "{main_script}"'


def _get_macos_plist_path() -> str:
    return os.path.expanduser(f"~/Library/LaunchAgents/{MACOS_BUNDLE_ID}.plist")


def is_autostart_enabled() -> bool:
    """Check whether OS login autostart is currently enabled."""
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WINDOWS_RUN_KEY, 0, winreg.KEY_READ) as key:
                winreg.QueryValueEx(key, WINDOWS_RUN_VALUE)
                return True
        except FileNotFoundError:
            return False
        except Exception:
            return False
    elif sys.platform == "darwin":
        return os.path.exists(_get_macos_plist_path())
    return False


def enable_autostart() -> Tuple[bool, str]:
    """Enable OS login autostart for the current user."""
    if sys.platform == "win32":
        try:
            import winreg
            cmd = _get_windows_autostart_cmd()
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, WINDOWS_RUN_KEY) as key:
                winreg.SetValueEx(key, WINDOWS_RUN_VALUE, 0, winreg.REG_SZ, cmd)
            return True, "已啟用 Windows 開機自動啟動"
        except Exception as e:
            return False, f"設定 Windows 開機啟動失敗: {e}"

    if sys.platform == "darwin":
        plist_path = _get_macos_plist_path()
        app_data = get_app_data_dir()
        os.makedirs(os.path.dirname(plist_path), exist_ok=True)
        args = get_launch_command_args()
        working_dir = os.path.dirname(args[-1]) if len(args) > 1 else os.path.dirname(args[0])
        arg_xml = "\n        ".join(f"<string>{a}</string>" for a in args)
        plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{MACOS_BUNDLE_ID}</string>
    <key>ProgramArguments</key>
    <array>
        {arg_xml}
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>WorkingDirectory</key>
    <string>{working_dir}</string>
    <key>StandardOutPath</key>
    <string>{os.path.join(app_data, "launchagent_stdout.log")}</string>
    <key>StandardErrorPath</key>
    <string>{os.path.join(app_data, "launchagent_stderr.log")}</string>
</dict>
</plist>
"""
        try:
            with open(plist_path, "w", encoding="utf-8") as f:
                f.write(plist_content)
            subprocess.run(["launchctl", "load", plist_path], capture_output=True)
            return True, "已啟用 macOS 開機自動啟動"
        except Exception as e:
            return False, f"設定 macOS LaunchAgent 失敗: {e}"

    return False, "目前作業系統不支援自動啟動設定"


def disable_autostart() -> Tuple[bool, str]:
    """Disable OS login autostart for the current user."""
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WINDOWS_RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                try:
                    winreg.DeleteValue(key, WINDOWS_RUN_VALUE)
                except FileNotFoundError:
                    pass
            return True, "已關閉 Windows 開機自動啟動"
        except Exception as e:
            return False, f"移除 Windows 開機啟動失敗: {e}"

    if sys.platform == "darwin":
        plist_path = _get_macos_plist_path()
        try:
            if os.path.exists(plist_path):
                subprocess.run(["launchctl", "unload", plist_path], capture_output=True)
                os.remove(plist_path)
            return True, "已關閉 macOS 開機自動啟動"
        except Exception as e:
            return False, f"移除 macOS LaunchAgent 失敗: {e}"

    return False, "目前作業系統不支援自動啟動設定"


def _hex_to_rgb(h: str) -> Tuple[int, int, int]:
    h = h.lstrip("#")
    return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))


def create_apple_squircle_icon(state: str = "idle", size: int = 256):
    """Render an anti-aliased Apple-style gradient squircle icon with cloud/download arrow."""
    from PIL import Image, ImageDraw

    palettes = {
        "idle": ("#0A84FF", "#3655E6", "#FFFFFF"),          # Apple Blue -> Indigo
        "downloading": ("#30D158", "#0A7E56", "#FFFFFF"),   # Apple Green -> Emerald
        "warning": ("#FF453A", "#C92A2A", "#FFFFFF"),       # Apple Red -> Crimson
        "unconfigured": ("#8E8E93", "#58585E", "#F2F2F7"),  # Apple System Gray
    }
    top_hex, bot_hex, fg_hex = palettes.get(state, palettes["idle"])
    c_top = _hex_to_rgb(top_hex)
    c_bot = _hex_to_rgb(bot_hex)
    fg = _hex_to_rgb(fg_hex) + (255,)

    scale = 2
    hi = size * scale

    # Vertical gradient canvas
    grad = Image.new("RGBA", (hi, hi), (0, 0, 0, 0))
    grad_draw = ImageDraw.Draw(grad)
    for y in range(hi):
        t = y / max(1, hi - 1)
        r = int(c_top[0] * (1 - t) + c_bot[0] * t)
        g = int(c_top[1] * (1 - t) + c_bot[1] * t)
        b = int(c_top[2] * (1 - t) + c_bot[2] * t)
        grad_draw.line([(0, y), (hi, y)], fill=(r, g, b, 255))

    # Rounded squircle mask
    margin = int(hi * 0.06)
    radius = int(hi * 0.22)
    mask = Image.new("L", (hi, hi), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle(
        (margin, margin, hi - margin, hi - margin),
        radius=radius,
        fill=255,
    )

    icon = Image.new("RGBA", (hi, hi), (0, 0, 0, 0))
    icon.paste(grad, (0, 0), mask)

    draw = ImageDraw.Draw(icon)

    # Subtle top inner highlight arc
    draw.rounded_rectangle(
        (margin + 2, margin + 2, hi - margin - 2, hi - margin - 2),
        radius=max(4, radius - 2),
        outline=(255, 255, 255, 55),
        width=max(2, hi // 64),
    )

    # Crisp SF-inspired download arrow + tray bar
    cx = hi // 2
    shaft_w = max(4, int(hi * 0.11))
    shaft_top = int(hi * 0.23)
    shaft_bot = int(hi * 0.54)
    draw.rounded_rectangle(
        (cx - shaft_w // 2, shaft_top, cx + shaft_w // 2, shaft_bot),
        radius=shaft_w // 2,
        fill=fg,
    )

    # Arrow head
    head_half_w = int(hi * 0.21)
    head_top = int(hi * 0.45)
    head_tip = int(hi * 0.67)
    draw.polygon(
        [(cx - head_half_w, head_top), (cx + head_half_w, head_top), (cx, head_tip)],
        fill=fg,
    )

    # Rounded tray dock bar at bottom
    tray_w = int(hi * 0.46)
    tray_h = max(4, int(hi * 0.075))
    tray_y = int(hi * 0.72)
    draw.rounded_rectangle(
        (cx - tray_w // 2, tray_y, cx + tray_w // 2, tray_y + tray_h),
        radius=tray_h // 2,
        fill=fg,
    )

    return icon.resize((size, size), Image.Resampling.LANCZOS)


def create_tray_icon_image(state: str = "idle"):
    """Create 64x64 Apple-style system tray icon."""
    return create_apple_squircle_icon(state=state, size=64)


def ensure_app_icon_files(target_dir: str) -> Tuple[str, str]:
    """Ensure app_icon.png and multi-size app_icon.ico exist in target_dir."""
    os.makedirs(target_dir, exist_ok=True)
    png_path = os.path.join(target_dir, "app_icon.png")
    ico_path = os.path.join(target_dir, "app_icon.ico")
    if not os.path.exists(png_path) or not os.path.exists(ico_path):
        img = create_apple_squircle_icon(state="idle", size=256)
        img.save(png_path, format="PNG")
        img.save(
            ico_path,
            format="ICO",
            sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
        )
    return png_path, ico_path


def atomic_write_json(path: str, data: Dict[str, Any]):
    """Atomically write JSON to disk to prevent partial reads across processes."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        raise


def read_json_safe(path: str, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Read a JSON file safely, returning a default dict if missing or invalid."""
    if default is None:
        default = {}
    if not os.path.exists(path):
        return dict(default)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else dict(default)
    except Exception:
        return dict(default)
