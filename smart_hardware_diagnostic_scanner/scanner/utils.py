"""Small shared utilities: formatting, validation, platform helpers."""
from __future__ import annotations

import math
import platform
import shutil
import sys
from pathlib import Path


def format_bytes(num: float) -> str:
    """Format a byte count as a human readable string."""
    try:
        value = float(num)
    except (TypeError, ValueError):
        return "Unavailable"
    if not math.isfinite(value) or value < 0:
        return "Unavailable"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    idx = 0
    while value >= 1024.0 and idx < len(units) - 1:
        value /= 1024.0
        idx += 1
    if idx == 0:
        return f"{int(value)} {units[idx]}"
    return f"{value:.2f} {units[idx]}"


def pct(used: float, total: float) -> float | None:
    """Safely compute a percentage or return None."""
    try:
        used_f = float(used)
        total_f = float(total)
        if not math.isfinite(used_f) or not math.isfinite(total_f) or total_f <= 0:
            return None
        return round((used_f / total_f) * 100.0, 1)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def is_windows() -> bool:
    return platform.system().lower() == "windows"


def python_info() -> str:
    return sys.version.replace("\n", " ")


def os_info() -> str:
    return f"{platform.system()} {platform.release()} ({platform.version()}) / {platform.machine()}"


def which(cmd: str) -> str | None:
    """Return full path of a command or None (no shell involved)."""
    try:
        return shutil.which(cmd)
    except Exception:
        return None


def safe_path_for_report(path_str: str, allowed_dir: Path | None = None) -> Path:
    """Validate a user-selected report path.

    Raises ValueError for empty paths, directories, or disallowed suffixes.
    """
    if not path_str or not path_str.strip():
        raise ValueError("Empty file path.")
    path = Path(path_str).expanduser()
    if path.suffix.lower() not in {".json", ".txt", ".csv", ".html"}:
        raise ValueError("Report path must end with .json, .txt, .csv or .html")
    if allowed_dir is not None:
        try:
            path.resolve().relative_to(allowed_dir.resolve())
        except ValueError as exc:
            raise ValueError("Path is outside the allowed directory.") from exc
    parent = path.parent
    if str(parent) and not parent.exists():
        parent.mkdir(parents=True, exist_ok=True)
    return path
