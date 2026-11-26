import os
import re
import shutil


def strip_ansi(text: str) -> str:
    """Strip ANSI escape sequences from a string."""
    ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
    return ansi_escape.sub("", text)


def format_size(size_bytes: int) -> str:
    """Format file size into human-readable representation."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.2f} KB"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.2f} MB"
    return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


def get_free_disk_space(path: str = ".") -> float:
    """Get free disk space of the target path in GB."""
    try:
        _, _, free = shutil.disk_usage(path)
        return free / (1024 * 1024 * 1024)
    except Exception:
        try:
            _, _, free = shutil.disk_usage(".")
            return free / (1024 * 1024 * 1024)
        except Exception:
            return 0.0


def make_progress_bar(percentage: float, width: int = 15) -> str:
    """Create a text-based progress bar."""
    clamped = max(0.0, min(100.0, percentage))
    filled_len = int(round(width * clamped / 100))
    bar = "█" * filled_len + "░" * (width - filled_len)
    return f"[{bar}] {clamped:.1f}%"
