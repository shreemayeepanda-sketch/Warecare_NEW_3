"""Logging configuration with size-limited rotating logs.

Logs go to a per-user, always-writable location instead of beside the
application package, so a protected install directory cannot block startup.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import tempfile
from pathlib import Path

APP_NAME = "SmartHardwareDiagnosticScanner"


def _user_data_root() -> Path:
    """Return the per-user application-data root for the current platform."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / APP_NAME
        # LOCALAPPDATA missing (rare): %APPDATA% is the next best per-user root.
        base = os.environ.get("APPDATA")
        if base:
            return Path(base) / APP_NAME
        return Path.home() / "AppData" / "Local" / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    # Linux/other: XDG spec
    base = os.environ.get("XDG_DATA_HOME")
    if base:
        return Path(base) / APP_NAME
    return Path.home() / ".local" / "share" / APP_NAME


def get_log_dir() -> Path:
    """Return a writable per-user log directory; never raise."""
    try:
        primary = _user_data_root() / "logs"
    except Exception:
        primary = Path.home() / APP_NAME / "logs"
    try:
        fallbacks = (primary,
                     Path(tempfile.gettempdir()) / APP_NAME / "logs",
                     Path.cwd() / "logs")
    except Exception:
        fallbacks = (Path("logs"),)
    for candidate in fallbacks:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".write_test"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
            return candidate
        except (PermissionError, OSError):
            continue
    return primary  # nothing writable verified; caller handles failure


def configure_logging(level: int = logging.INFO) -> logging.Logger:
    """Configure the "shds" logger with rotating file + console handlers.

    Idempotent. A failing log file (unwritable dir, disk full, etc.) must not
    prevent the application from starting: the logger still works with the
    console handler and logs a warning about the missing file sink.
    """
    logger = logging.getLogger("shds")
    if logger.handlers:
        return logger
    logger.setLevel(level)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    file_handler: logging.Handler | None = None
    try:
        log_dir = get_log_dir()
        log_file = log_dir / "scanner.log"
        file_handler = logging.handlers.RotatingFileHandler(
            str(log_file), maxBytes=512_000, backupCount=3, encoding="utf-8")
        file_handler.setFormatter(fmt)
        file_handler.setLevel(level)
        logger.addHandler(file_handler)
    except (PermissionError, OSError) as exc:
        file_handler = None
        # File sink unavailable; console still works.
        logger.warning("File logging unavailable (%s); logging to console only.", exc)

    console = logging.StreamHandler()
    console.setLevel(logging.WARNING)
    console.setFormatter(fmt)
    logger.addHandler(console)
    logger.propagate = False

    if file_handler is None:
        logger.warning("File logging disabled — log file could not be created.")
    return logger
