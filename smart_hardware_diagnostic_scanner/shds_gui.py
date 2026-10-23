"""Smart Hardware Diagnostic Scanner — Dark Neon Edition (single-file GUI).

Run directly:  python shds_gui.py

Everything needed by the desktop application lives in this one file:
theme, utilities, logging, GPU detection, optional charts, hardware
collection, monitoring, diagnostics, reports, and the Tkinter GUI.

Hard constraints preserved:
- No invented sensor data: missing sensors/drivers stay UNAVAILABLE/UNSUPPORTED.
- All Tkinter widget updates happen on the main thread.
- subprocess uses shell=False + explicit argv + timeout=8.
- psutil and matplotlib are optional; the app degrades gracefully.
"""
from __future__ import annotations

import argparse
import base64
import csv
import html
import io
import json
import logging
import logging.handlers
import math
import os
import platform
import queue
import re
import shutil
import shlex
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# ---------------------------------------------------------------------------
# Optional dependencies
# ---------------------------------------------------------------------------
try:
    import psutil
    HAS_PSUTIL = True
except ImportError:  # optional dependency
    psutil = None  # type: ignore
    HAS_PSUTIL = False

try:
    import matplotlib

    matplotlib.use("TkAgg")
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    from matplotlib.figure import Figure

    HAS_MPL = True
except Exception:
    # ImportError (not installed) is the common case, but a broken backend
    # (e.g. TkAgg missing) raises other exceptions at import time. Either way
    # the app must degrade to the placeholder instead of crashing import.
    HAS_MPL = False
    Figure = None  # type: ignore
    FigureCanvasTkAgg = None  # type: ignore

try:
    # Optional: native desktop toasts for threshold alerts. Absent everywhere
    # except a machine with `plyer` installed; the app falls back to an in-app
    # banner, so this is never required.
    from plyer import notification as _plyer_notification

    HAS_PLYER = True
except Exception:
    _plyer_notification = None  # type: ignore
    HAS_PLYER = False


# ---------------------------------------------------------------------------
# Theme — dark default with a live light mode (see set_mode / get_mode).
# Every palette defines exactly the keys in PALETTE_KEYS (enforced by tests);
# the apps re-configure plain widgets through the swap map built from these
# two dicts, so both stay in sync with scanner/theme.py.
# ---------------------------------------------------------------------------
PALETTE_KEYS = (
    "BG_MAIN", "BG_PANEL", "BG_CARD", "BG_SIDEBAR", "BG_OUTPUT",
    "BG_STATUSBAR", "FG_PRIMARY", "FG_MUTED", "FG_DIM",
    "NEON_CYAN", "NEON_PURPLE", "NEON_GREEN", "NEON_YELLOW", "NEON_RED",
    "NEON_ORANGE", "BORDER", "ENTRY_BG", "PROGRESS_BG", "OUTPUT_FG",
)

_DARK_PALETTE = {
    # Backgrounds — all valid 6-digit hex.
    "BG_MAIN": "#12121A",
    "BG_PANEL": "#1C1C28",
    "BG_CARD": "#232333",
    "BG_SIDEBAR": "#16161F",
    "BG_OUTPUT": "#0E0E14",
    "BG_STATUSBAR": "#16161F",
    # Foreground / text.
    "FG_PRIMARY": "#E8E8F0",
    "FG_MUTED": "#9A9AB0",
    "FG_DIM": "#6B6B85",
    # Neon accents.
    "NEON_CYAN": "#00E5FF",
    "NEON_PURPLE": "#B266FF",
    "NEON_GREEN": "#39FF88",
    "NEON_YELLOW": "#FFD60A",
    "NEON_RED": "#FF4D6D",
    "NEON_ORANGE": "#FF9F1C",
    # Widget specifics.
    "BORDER": "#34344A",
    "ENTRY_BG": "#101018",
    "PROGRESS_BG": "#2A2A3D",
    "OUTPUT_FG": "#CFFAFE",
}

_LIGHT_PALETTE = {
    # Backgrounds.
    "BG_MAIN": "#F4F4F9",
    "BG_PANEL": "#FFFFFF",
    "BG_CARD": "#FFFFFF",
    "BG_SIDEBAR": "#E9E9F1",
    "BG_OUTPUT": "#F7F7FC",
    "BG_STATUSBAR": "#E9E9F1",
    # Foreground / text (darker accents keep contrast on white).
    "FG_PRIMARY": "#1A1A28",
    "FG_MUTED": "#5A5A70",
    "FG_DIM": "#8A8AA0",
    # Accents (readable on light backgrounds).
    "NEON_CYAN": "#0077B8",
    "NEON_PURPLE": "#7B3FE4",
    "NEON_GREEN": "#0E8A4A",
    "NEON_YELLOW": "#B58900",
    "NEON_RED": "#C81E4A",
    "NEON_ORANGE": "#D97A0F",
    # Widget specifics.
    "BORDER": "#C8C8D8",
    "ENTRY_BG": "#FFFFFF",
    "PROGRESS_BG": "#DDDDE8",
    "OUTPUT_FG": "#1A1A28",
}


def _apply_palette(palette: dict[str, str]) -> None:
    """Point the module-level colour constants at the given palette."""
    for key in PALETTE_KEYS:
        globals()[key] = palette[key]
    globals()["STATUS_COLOURS"] = {
        "PASS": palette["NEON_GREEN"],
        "WARNING": palette["NEON_YELLOW"],
        "FAIL": palette["NEON_RED"],
        "UNAVAILABLE": palette["FG_MUTED"],
        "INFO": palette["NEON_CYAN"],
    }


def set_theme_mode(mode: str) -> None:
    """Switch the whole palette ("dark" default, "light" supported)."""
    _apply_palette(_LIGHT_PALETTE if str(mode) == "light" else _DARK_PALETTE)


def get_theme_mode() -> str:
    """Return the currently active mode name."""
    return "light" if BG_MAIN == _LIGHT_PALETTE["BG_MAIN"] else "dark"


def theme_palettes() -> tuple[dict[str, str], dict[str, str]]:
    """Return (dark, light) palette dicts; used for live widget re-theming."""
    return dict(_DARK_PALETTE), dict(_LIGHT_PALETTE)


# Start in the dark-neon default.
_apply_palette(_DARK_PALETTE)

def _default_font_family() -> str:
    """Pick a UI font that exists on the current OS (Tk silently falls back)."""
    system = platform.system().lower()
    if system == "windows":
        return "Segoe UI"
    if system == "darwin":
        return "Helvetica Neue"
    return "DejaVu Sans"  # Linux/other


def _default_mono_family() -> str:
    """Pick a monospace font that exists on the current OS."""
    system = platform.system().lower()
    if system == "windows":
        return "Consolas"
    if system == "darwin":
        return "Menlo"
    return "DejaVu Sans Mono"  # Linux/other


FONT_FAMILY = _default_font_family()
FONT_TITLE = (FONT_FAMILY, 16, "bold")
FONT_HEADING = (FONT_FAMILY, 11, "bold")
FONT_BODY = (FONT_FAMILY, 10)
FONT_SMALL = (FONT_FAMILY, 9)
FONT_MONO = (_default_mono_family(), 9)

STATUS_COLOURS = {
    "PASS": NEON_GREEN,
    "WARNING": NEON_YELLOW,
    "FAIL": NEON_RED,
    "UNAVAILABLE": FG_MUTED,
    "INFO": NEON_CYAN,
}

# Dashboard cards. The dashboard computes its per-card width from the window
# size at runtime; these constants are only fallbacks used before the first
# <Configure> event arrives. CARD_MIN_WIDTH must fit five cards plus padding
# inside the main column at the minimum window size (960 - 210 sidebar).
CARD_MIN_WIDTH = 136
CARD_HEIGHT = 150

# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------
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


def clamp(value: float, low: float, high: float) -> float:
    """Clamp a number into [low, high]; NaN becomes `low`."""
    if not math.isfinite(value):
        return low
    return max(low, min(high, value))


def format_uptime(seconds: float | None) -> str:
    """Format an uptime as "3d 4h 12m" / "5h 3m" / "42s".

    `None` means "unknown" and is reported as such rather than as 0s.
    """
    if seconds is None:
        return "Unknown"
    try:
        seconds_f = float(seconds)
    except (TypeError, ValueError):
        return "Unknown"
    if not math.isfinite(seconds_f):
        return "Unknown"
    total = int(max(0, seconds_f))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def format_rate_bytes(bytes_per_s: float | None) -> str:
    """Format a byte/second rate using the shared byte formatter."""
    if bytes_per_s is None:
        return "n/a"
    try:
        value = float(bytes_per_s)
    except (TypeError, ValueError):
        return "n/a"
    if not math.isfinite(value) or value < 0:
        return "n/a"
    return f"{format_bytes(value)}/s"


def health_label(score: int) -> str:
    """Human word for a 0-100 health score."""
    if score >= 90:
        return "Excellent"
    if score >= 75:
        return "Good"
    if score >= 55:
        return "Fair"
    if score >= 35:
        return "Degraded"
    return "Critical"


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
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


log = configure_logging()


# ---------------------------------------------------------------------------
# GPU detection: NVIDIA, AMD (rocm-smi), Intel (intel_gpu_top)
# ---------------------------------------------------------------------------
NVIDIA_SMI_TIMEOUT = 8  # seconds


@dataclass
class GpuInfo:
    """Single GPU reading; unavailable metrics stay as None."""

    index: int = 0
    name: str = "Unavailable"
    utilization_pct: float | None = None
    temperature_c: float | None = None
    memory_used_mb: float | None = None
    memory_total_mb: float | None = None
    fan_pct: float | None = None
    power_w: float | None = None
    available: bool = False
    source: str = "nvidia-smi"
    error: str = ""


@dataclass
class GpuStatus:
    """Overall GPU provider state."""

    supported: bool = False
    tool_found: bool = False
    gpus: list[GpuInfo] = field(default_factory=list)
    message: str = "Unavailable"


def _to_float(raw: str) -> float | None:
    text = (raw or "").strip().lower().replace("w", "").replace("%", "").replace("c", "")
    if text in ("", "n/a", "unknown", "[n/a]", "n/a.", "none"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def query_nvidia_gpus(timeout: int = NVIDIA_SMI_TIMEOUT) -> GpuStatus:
    """Query NVIDIA GPUs safely with explicit argv and shell=False.

    Never raises: all failures are reported as unsupported/unavailable.
    """
    status = GpuStatus()
    exe = which("nvidia-smi")
    if not exe:
        status.message = "nvidia-smi not found — GPU metrics unsupported on this machine."
        log.info("nvidia-smi not found")
        return status
    status.tool_found = True
    argv = [
        exe,
        "--query-gpu=index,name,utilization.gpu,temperature.gpu,memory.used,memory.total,fan.speed,power.draw",
        "--format=csv,noheader,nounits",
    ]
    try:
        proc = subprocess.run(argv, shell=False, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        status.message = "nvidia-smi executable disappeared."
        return status
    except subprocess.TimeoutExpired:
        status.message = f"nvidia-smi timed out after {timeout}s."
        log.warning("nvidia-smi timeout")
        return status
    except OSError as exc:
        status.message = f"Could not run nvidia-smi: {exc}"
        log.warning("nvidia-smi OSError: %s", exc)
        return status
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()[:300]
        status.message = f"nvidia-smi failed (exit {proc.returncode}): {err or 'no details'}"
        log.warning("nvidia-smi exit %s: %s", proc.returncode, err)
        return status
    lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
    if not lines:
        status.message = "nvidia-smi returned no GPU rows."
        return status
    gpus: list[GpuInfo] = []
    for line in lines:
        parts = [p.strip() for p in line.split(",")]
        # pad to 8 columns
        while len(parts) < 8:
            parts.append("")
        try:
            idx = int(float(parts[0])) if parts[0] not in ("", "N/A") else len(gpus)
        except ValueError:
            idx = len(gpus)
        gpu = GpuInfo(
            index=idx,
            name=parts[1] or "Unknown NVIDIA GPU",
            utilization_pct=_to_float(parts[2]),
            temperature_c=_to_float(parts[3]),
            memory_used_mb=_to_float(parts[4]),
            memory_total_mb=_to_float(parts[5]),
            fan_pct=_to_float(parts[6]),
            power_w=_to_float(parts[7]),
            available=True,
        )
        gpus.append(gpu)
    status.supported = True
    status.gpus = gpus
    status.message = f"Found {len(gpus)} NVIDIA GPU(s)."
    return status


def _loose_float(value) -> float | None:
    """Parse a possibly string-shaped number ("43.0", 12) or return None."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None  # NaN check


def query_rocm_gpus(timeout: int = NVIDIA_SMI_TIMEOUT) -> GpuStatus:
    """Query AMD GPUs via rocm-smi --json. Never raises; degrades honestly."""
    status = GpuStatus()
    exe = which("rocm-smi")
    if not exe:
        status.message = "rocm-smi not found — AMD GPU metrics unavailable."
        log.info("rocm-smi not found")
        return status
    status.tool_found = True
    argv = [exe, "--showtemp", "--showuse", "--showmemuse", "--json"]
    try:
        proc = subprocess.run(argv, shell=False, capture_output=True, text=True,
                              timeout=timeout)
    except FileNotFoundError:
        status.message = "rocm-smi executable disappeared."
        return status
    except subprocess.TimeoutExpired:
        status.message = f"rocm-smi timed out after {timeout}s."
        log.warning("rocm-smi timeout")
        return status
    except OSError as exc:
        status.message = f"Could not run rocm-smi: {exc}"
        log.warning("rocm-smi OSError: %s", exc)
        return status
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()[:300]
        status.message = f"rocm-smi failed (exit {proc.returncode}): {err or 'no details'}"
        log.warning("rocm-smi exit %s: %s", proc.returncode, err)
        return status
    try:
        data = json.loads(proc.stdout or "")
    except (ValueError, TypeError):
        status.message = "rocm-smi returned non-JSON output."
        return status
    if not isinstance(data, dict):
        status.message = "rocm-smi JSON was not an object."
        return status
    gpus: list[GpuInfo] = []
    for card, info in data.items():
        if not isinstance(info, dict):
            continue
        util = temp = mem_used = None
        for key, value in info.items():
            low = str(key).lower()
            if "gpu use" in low and util is None:
                util = _loose_float(value)
            elif "temperature" in low and temp is None:
                temp = _loose_float(value)
            elif "vram total used" in low and mem_used is None:
                # Reported in bytes; surface as MiB for consistency with nvidia.
                raw = _loose_float(value)
                if raw is not None:
                    mem_used = raw / (1024 * 1024)
        card_index = 0
        digits = "".join(ch for ch in str(card) if ch.isdigit())
        if digits:
            card_index = int(digits)
        gpus.append(GpuInfo(
            index=card_index,
            name=f"AMD {card}",
            utilization_pct=util,
            temperature_c=temp,
            memory_used_mb=mem_used,
            available=True,
        ))
    if not gpus:
        status.message = "rocm-smi returned no GPU cards."
        return status
    status.supported = True
    status.gpus = gpus
    status.message = f"Found {len(gpus)} AMD GPU(s) via rocm-smi."
    return status


def query_intel_gpus(timeout: int = 3) -> GpuStatus:
    """Query Intel GPUs via intel_gpu_top -J (JSON per line).

    intel_gpu_top streams continuously, so the run is deliberately short:
    a TimeoutExpired is EXPECTED and its partial stdout is parsed. Without
    a usable sample the metrics stay unavailable rather than invented.
    """
    status = GpuStatus()
    exe = which("intel_gpu_top")
    if not exe:
        status.message = "intel_gpu_top not found — Intel GPU metrics unavailable."
        log.info("intel_gpu_top not found")
        return status
    status.tool_found = True
    argv = [exe, "-J", "-s", "500"]
    stdout = ""
    timed_out = False
    try:
        proc = subprocess.run(argv, shell=False, capture_output=True, text=True,
                              timeout=timeout)
        stdout = proc.stdout or ""
        if proc.returncode != 0:
            err = (proc.stderr or "").strip()[:300]
            status.message = (f"intel_gpu_top failed (exit {proc.returncode}): "
                              f"{err or 'no details'}")
            log.warning("intel_gpu_top exit %s: %s", proc.returncode, err)
            return status
    except FileNotFoundError:
        status.message = "intel_gpu_top executable disappeared."
        return status
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = exc.stdout or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
    except OSError as exc:
        status.message = f"Could not run intel_gpu_top: {exc}"
        log.warning("intel_gpu_top OSError: %s", exc)
        return status

    util = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        gpu = data.get("gpu") or {}
        if isinstance(gpu, dict):
            util = _loose_float(gpu.get("busy"))
            if util is not None:
                break
    if util is None:
        status.message = ("intel_gpu_top produced no usable sample."
                          if timed_out else "intel_gpu_top returned no GPU data.")
        return status
    status.supported = True
    status.gpus = [GpuInfo(index=0, name="Intel GPU", utilization_pct=util,
                           available=True)]
    status.message = "Found 1 Intel GPU via intel_gpu_top."
    return status


def query_gpus(timeout: int = NVIDIA_SMI_TIMEOUT) -> GpuStatus:
    """Vendor chain: NVIDIA first, then AMD (rocm-smi), then Intel.

    Returns the first supported provider; when none is available the
    message lists what was tried so the user can see the whole story.
    """
    tried: list[str] = []
    for name, probe in (("nvidia-smi", query_nvidia_gpus),
                        ("rocm-smi", query_rocm_gpus),
                        ("intel_gpu_top", query_intel_gpus)):
        try:
            status = probe(timeout=timeout)  # type: ignore[operator]
        except Exception as exc:  # a buggy probe must never break the snapshot
            log.warning("gpu probe %s crashed: %s", name, exc)
            tried.append(f"{name} (error: {exc})")
            continue
        if status.supported and status.gpus:
            return status
        tried.append(f"{name} ({status.message[:80]})")
    return GpuStatus(message="No supported GPU metrics available. Tried: "
                     + "; ".join(tried))


# ---------------------------------------------------------------------------
# Optional charts
# ---------------------------------------------------------------------------
class ChartPanel:
    """Two-line live chart (CPU + RAM). No-op placeholder when Matplotlib is absent."""

    def __init__(self, parent, bg: str = "#232333", fg: str = "#E8E8F0") -> None:
        self.parent = parent
        self.bg = bg
        self.fg = fg
        self.canvas = None
        self.ax = None
        self.fig = None
        self.line_cpu = None
        self.line_mem = None
        if HAS_MPL:
            self.fig = Figure(figsize=(5.5, 2.6), dpi=100, facecolor=bg)
            self.ax = self.fig.add_subplot(111, facecolor=bg)
            self.ax.set_ylim(0, 100)
            self.ax.set_xlabel("samples", color=fg, fontsize=8)
            self.ax.set_ylabel("%", color=fg, fontsize=8)
            self.ax.tick_params(colors=fg, labelsize=7)
            for spine in self.ax.spines.values():
                spine.set_color("#34344A")
            self.ax.grid(True, color="#34344A", linewidth=0.4, alpha=0.7)
            (self.line_cpu,) = self.ax.plot([], [], color="#00E5FF", linewidth=1.6, label="CPU %")
            (self.line_mem,) = self.ax.plot([], [], color="#B266FF", linewidth=1.6, label="RAM %")
            leg = self.ax.legend(facecolor=bg, edgecolor="#34344A", fontsize=7)
            for text in leg.get_texts():
                text.set_color(fg)
            self.fig.tight_layout(pad=0.8)
            self.canvas = FigureCanvasTkAgg(self.fig, master=parent)
            widget = self.canvas.get_tk_widget()
            widget.configure(highlightthickness=0)
            widget.pack(fill="both", expand=True)

    def update(self, cpu_hist: list, mem_hist: list) -> None:
        """Redraw from bounded histories. Safe to call without Matplotlib."""
        if not HAS_MPL or self.canvas is None or self.ax is None:
            return
        try:
            xs = list(range(len(cpu_hist)))
            self.line_cpu.set_data(xs, list(cpu_hist))
            self.line_mem.set_data(xs, list(mem_hist))
            self.ax.set_xlim(0, max(10, len(xs)))
            self.ax.set_ylim(0, 100)
            self.canvas.draw_idle()
        except Exception as exc:
            log.debug("chart update failed: %s", exc)

    def pack_widget(self) -> None:
        if self.canvas is None:
            return
        self.canvas.get_tk_widget().pack(fill="both", expand=True)

    def set_palette(self, bg: str, fg: str, border: str,
                    cpu_colour: str, mem_colour: str) -> None:
        """Re-colour the chart for a theme switch (no-op without Matplotlib)."""
        if not HAS_MPL or self.fig is None or self.ax is None:
            self.bg, self.fg = bg, fg
            return
        self.bg, self.fg = bg, fg
        try:
            self.fig.set_facecolor(bg)
            self.ax.set_facecolor(bg)
            self.ax.tick_params(colors=fg)
            for text in (self.ax.xaxis.label, self.ax.yaxis.label):
                text.set_color(fg)
            for spine in self.ax.spines.values():
                spine.set_color(border)
            for line, colour in ((self.line_cpu, cpu_colour),
                                 (self.line_mem, mem_colour)):
                if line is not None:
                    line.set_color(colour)
            self.ax.grid(True, color=border, linewidth=0.4, alpha=0.7)
            leg = self.ax.get_legend()
            if leg is not None:
                leg.set_facecolor(bg)
                leg.set_edgecolor(border)
                for text in leg.get_texts():
                    text.set_color(fg)
            self.canvas.draw_idle()
        except Exception as exc:
            log.debug("chart re-theme failed: %s", exc)


class TrendChart:
    """Health-score / CPU / RAM trend across past scans.

    Like ChartPanel this is an optional widget: without matplotlib it degrades
    to nothing and the History page still lists the scans as text.
    """

    def __init__(self, parent, bg: str = BG_CARD, fg: str = FG_PRIMARY) -> None:
        self.bg = bg
        self.fg = fg
        self.canvas = None
        self.fig = None
        self.ax = None
        self.lines: dict = {}
        if not HAS_MPL:
            return
        self.fig = Figure(figsize=(6.0, 2.6), dpi=100, facecolor=bg)
        self.ax = self.fig.add_subplot(111, facecolor=bg)
        self.ax.set_ylim(0, 100)
        self.ax.set_xlabel("scan", color=fg, fontsize=8)
        self.ax.set_ylabel("score / %", color=fg, fontsize=8)
        self.ax.tick_params(colors=fg, labelsize=7)
        for spine in self.ax.spines.values():
            spine.set_color(BORDER)
        self.ax.grid(True, color=BORDER, linewidth=0.4, alpha=0.7)
        (self.lines["score"],) = self.ax.plot([], [], color=NEON_CYAN, linewidth=1.8,
                                              label="Health score")
        (self.lines["cpu"],) = self.ax.plot([], [], color=NEON_ORANGE, linewidth=1.1,
                                            label="CPU %")
        (self.lines["mem"],) = self.ax.plot([], [], color=NEON_PURPLE, linewidth=1.1,
                                            label="RAM %")
        leg = self.ax.legend(facecolor=bg, edgecolor=BORDER, fontsize=7)
        for text in leg.get_texts():
            text.set_color(fg)
        self.fig.tight_layout(pad=0.8)
        self.canvas = FigureCanvasTkAgg(self.fig, master=parent)
        widget = self.canvas.get_tk_widget()
        widget.configure(highlightthickness=0)
        widget.pack(fill="both", expand=True)

    def update(self, entries: list) -> None:
        """Redraw from HistoryEntry objects (score, cpu, mem)."""
        if not HAS_MPL or self.canvas is None or self.ax is None:
            return
        try:
            xs = list(range(len(entries)))
            self.lines["score"].set_data(xs, [e.score for e in entries])
            self.lines["cpu"].set_data(xs, [e.cpu if e.cpu is not None else float("nan")
                                            for e in entries])
            self.lines["mem"].set_data(xs, [e.mem if e.mem is not None else float("nan")
                                            for e in entries])
            self.ax.set_xlim(0, max(10, len(xs)))
            self.ax.set_ylim(0, 100)
            self.canvas.draw_idle()
        except Exception as exc:
            log.debug("trend chart update failed: %s", exc)

    def set_palette(self, bg: str, fg: str, border: str) -> None:
        """Re-colour the trend chart for a theme switch (no-op without MPL)."""
        if not HAS_MPL or self.fig is None or self.ax is None:
            self.bg, self.fg = bg, fg
            return
        self.bg, self.fg = bg, fg
        try:
            self.fig.set_facecolor(bg)
            self.ax.set_facecolor(bg)
            self.ax.tick_params(colors=fg)
            for text in (self.ax.xaxis.label, self.ax.yaxis.label):
                text.set_color(fg)
            for spine in self.ax.spines.values():
                spine.set_color(border)
            self.ax.grid(True, color=border, linewidth=0.4, alpha=0.7)
            leg = self.ax.get_legend()
            if leg is not None:
                leg.set_facecolor(bg)
                leg.set_edgecolor(border)
                for text in leg.get_texts():
                    text.set_color(fg)
            self.canvas.draw_idle()
        except Exception as exc:
            log.debug("trend chart re-theme failed: %s", exc)


# ---------------------------------------------------------------------------
# Hardware collection
# ---------------------------------------------------------------------------
@dataclass
class CpuSnapshot:
    available: bool = False
    percent: float | None = None
    per_core: list[float] = field(default_factory=list)
    physical_cores: int | None = None
    logical_cores: int | None = None
    freq_current_mhz: float | None = None
    freq_min_mhz: float | None = None
    freq_max_mhz: float | None = None
    load_avg: tuple | None = None
    error: str = ""


@dataclass
class MemSnapshot:
    available: bool = False
    total: int = 0
    available_bytes: int = 0
    used: int = 0
    percent: float | None = None
    error: str = ""


@dataclass
class DiskEntry:
    device: str = ""
    mountpoint: str = ""
    fstype: str = ""
    total: int = 0
    used: int = 0
    free: int = 0
    percent: float | None = None
    accessible: bool = False
    error: str = ""


@dataclass
class NetSnapshot:
    available: bool = False
    interfaces: list[str] = field(default_factory=list)
    bytes_sent: int = 0
    bytes_recv: int = 0
    up_bps: float | None = None
    down_bps: float | None = None
    error: str = ""


@dataclass
class BatterySnapshot:
    """Battery/power state. Desktops (no battery) stay explicitly unavailable."""

    available: bool = False
    percent: float | None = None
    plugged: bool | None = None
    secs_left: float | None = None
    error: str = ""


@dataclass
class DiskIoSnapshot:
    """Aggregate disk throughput between successive samples."""

    available: bool = False
    read_bps: float | None = None
    write_bps: float | None = None
    read_bytes: int = 0
    write_bytes: int = 0
    error: str = ""


@dataclass
class TempReading:
    label: str = ""
    current: float | None = None
    source: str = ""
    status: str = "unavailable"  # available | unavailable | unsupported


@dataclass
class ProcEntry:
    pid: int = 0
    name: str = ""
    cpu: float = 0.0
    mem_mb: float = 0.0


@dataclass
class HardwareSnapshot:
    cpu: CpuSnapshot = field(default_factory=CpuSnapshot)
    mem: MemSnapshot = field(default_factory=MemSnapshot)
    disks: list[DiskEntry] = field(default_factory=list)
    net: NetSnapshot = field(default_factory=NetSnapshot)
    temps: list[TempReading] = field(default_factory=list)
    gpu: GpuStatus = field(default_factory=GpuStatus)
    battery: BatterySnapshot = field(default_factory=BatterySnapshot)
    disk_io: DiskIoSnapshot = field(default_factory=DiskIoSnapshot)
    hostname: str = ""
    os_info: str = ""
    uptime_s: float | None = None
    timestamp: float = 0.0


_prev_net: dict[str, float] = {"sent": 0, "recv": 0, "t": 0.0}
_prev_disk_io: dict[str, float] = {"read": 0, "write": 0, "t": 0.0}
_net_lock = threading.Lock()
_disk_io_lock = threading.Lock()
_proc_primed = False


def _rate_bps(current: int | None, previous: int | None, dt: float) -> float | None:
    """Rate in bits/s, or None for the first sample or a counter reset.

    A reset (previous > current, e.g. interface restart or reboot) would
    produce a negative delta, so the rate is reported as unavailable instead.
    """
    if current is None or previous is None:
        return None
    if dt <= 0:
        return None
    if current < previous:
        log.info("network counter reset detected (previous=%s current=%s); "
                 "rate unavailable for this sample", previous, current)
        return None
    return (current - previous) * 8.0 / dt


def _rate_bytes(current: int | None, previous: int | None, dt: float) -> float | None:
    """Byte/s rate, or None for a first sample / counter reset (see _rate_bps)."""
    if current is None or previous is None:
        return None
    if dt <= 0:
        return None
    if current < previous:
        return None
    return (current - previous) / dt


def collect_cpu() -> CpuSnapshot:
    snap = CpuSnapshot()
    if not HAS_PSUTIL:
        snap.error = "psutil not installed"
        return snap
    try:
        snap.percent = float(psutil.cpu_percent(interval=None))
        try:
            snap.per_core = [float(v) for v in psutil.cpu_percent(interval=None, percpu=True)]
        except Exception as exc:  # per-core optional
            log.debug("per-core cpu failed: %s", exc)
            snap.per_core = []
        try:
            snap.physical_cores = psutil.cpu_count(logical=False)
        except Exception:
            snap.physical_cores = None
        try:
            snap.logical_cores = psutil.cpu_count(logical=True)
        except Exception:
            snap.logical_cores = None
        try:
            freq = psutil.cpu_freq()
            if freq:
                snap.freq_current_mhz = freq.current
                snap.freq_min_mhz = freq.min
                snap.freq_max_mhz = freq.max
        except Exception:
            pass
        try:
            if hasattr(psutil, "getloadavg"):
                snap.load_avg = tuple(psutil.getloadavg())
        except Exception:
            snap.load_avg = None
        snap.available = True
    except Exception as exc:
        snap.error = str(exc)
        log.warning("cpu collect failed: %s", exc)
    return snap


def collect_memory() -> MemSnapshot:
    snap = MemSnapshot()
    if not HAS_PSUTIL:
        snap.error = "psutil not installed"
        return snap
    try:
        vm = psutil.virtual_memory()
        snap.total = int(vm.total)
        snap.available_bytes = int(vm.available)
        snap.used = int(vm.used)
        snap.percent = float(vm.percent)
        snap.available = True
    except Exception as exc:
        snap.error = str(exc)
        log.warning("memory collect failed: %s", exc)
    return snap


def collect_disks() -> list[DiskEntry]:
    entries: list[DiskEntry] = []
    if not HAS_PSUTIL:
        return entries
    try:
        parts = psutil.disk_partitions(all=False)
    except Exception as exc:
        log.warning("disk_partitions failed: %s", exc)
        return entries
    seen: set[str] = set()
    for part in parts:
        key = (part.mountpoint or part.device).lower()
        if key in seen:
            continue
        seen.add(key)
        entry = DiskEntry(device=part.device, mountpoint=part.mountpoint, fstype=part.fstype)
        try:
            usage = psutil.disk_usage(part.mountpoint)
            entry.total = int(usage.total)
            entry.used = int(usage.used)
            entry.free = int(usage.free)
            entry.percent = float(usage.percent)
            entry.accessible = True
        except PermissionError as exc:
            entry.error = f"Permission denied: {exc}"
            log.debug("disk %s permission denied", part.mountpoint)
        except (FileNotFoundError, OSError) as exc:
            entry.error = f"Inaccessible: {exc}"
            log.debug("disk %s inaccessible: %s", part.mountpoint, exc)
        except Exception as exc:
            entry.error = str(exc)
            log.warning("disk %s failed: %s", part.mountpoint, exc)
        entries.append(entry)
    return entries


def collect_network() -> NetSnapshot:
    snap = NetSnapshot()
    if not HAS_PSUTIL:
        snap.error = "psutil not installed"
        return snap
    try:
        addrs = psutil.net_if_addrs()
        snap.interfaces = sorted(addrs.keys())
        counters = psutil.net_io_counters()
        if counters is not None:
            snap.bytes_sent = int(counters.bytes_sent)
            snap.bytes_recv = int(counters.bytes_recv)
            now = time.monotonic()
            with _net_lock:
                prev_sent = _prev_net.get("sent", 0)
                prev_recv = _prev_net.get("recv", 0)
                prev_t = _prev_net.get("t", 0.0)
                if prev_t and now > prev_t:
                    dt = now - prev_t
                    # Sent and received counters reset independently (interface
                    # restart, reboot, adapter swap); handle each separately.
                    snap.up_bps = _rate_bps(snap.bytes_sent, prev_sent, dt)
                    snap.down_bps = _rate_bps(snap.bytes_recv, prev_recv, dt)
                else:
                    snap.up_bps = None  # first sample
                    snap.down_bps = None
                _prev_net.update({"sent": snap.bytes_sent, "recv": snap.bytes_recv, "t": now})
        snap.available = True
    except Exception as exc:
        snap.error = str(exc)
        log.warning("network collect failed: %s", exc)
    return snap


def collect_battery() -> BatterySnapshot:
    """Read battery state; desktops report unavailable (never invented)."""
    snap = BatterySnapshot()
    if not HAS_PSUTIL:
        snap.error = "psutil not installed"
        return snap
    getter = getattr(psutil, "sensors_battery", None)
    if getter is None:
        snap.error = "battery API unsupported on this platform"
        return snap
    try:
        batt = getter()
    except Exception as exc:
        snap.error = str(exc)
        log.debug("battery read failed: %s", exc)
        return snap
    if batt is None:
        snap.error = "No battery detected (desktop or unsupported platform)."
        return snap
    try:
        snap.percent = float(batt.percent)
        snap.plugged = bool(batt.power_plugged)
        secs = getattr(batt, "secsleft", None)
        # psutil uses sentinel constants for "unknown"/"charging" states.
        if isinstance(secs, (int, float)) and 0 <= secs < 86400:
            snap.secs_left = float(secs)
        snap.available = True
    except Exception as exc:
        snap.error = str(exc)
        log.debug("battery parse failed: %s", exc)
    return snap


def collect_disk_io() -> DiskIoSnapshot:
    """Aggregate disk throughput; first sample has no rate (honest None)."""
    snap = DiskIoSnapshot()
    if not HAS_PSUTIL:
        snap.error = "psutil not installed"
        return snap
    getter = getattr(psutil, "disk_io_counters", None)
    if getter is None:
        snap.error = "disk I/O counters unsupported"
        return snap
    try:
        counters = getter()
    except Exception as exc:
        snap.error = str(exc)
        log.debug("disk_io_counters failed: %s", exc)
        return snap
    if counters is None:
        snap.error = "No disk I/O counters reported."
        return snap
    try:
        snap.read_bytes = int(counters.read_bytes)
        snap.write_bytes = int(counters.write_bytes)
        now = time.monotonic()
        with _disk_io_lock:
            prev_read = _prev_disk_io.get("read", 0)
            prev_write = _prev_disk_io.get("write", 0)
            prev_t = _prev_disk_io.get("t", 0.0)
            if prev_t and now > prev_t:
                dt = now - prev_t
                snap.read_bps = _rate_bytes(snap.read_bytes, prev_read, dt)
                snap.write_bps = _rate_bytes(snap.write_bytes, prev_write, dt)
            _prev_disk_io.update({"read": snap.read_bytes, "write": snap.write_bytes,
                                  "t": now})
        snap.available = True
    except Exception as exc:
        snap.error = str(exc)
        log.debug("disk io parse failed: %s", exc)
    return snap


def collect_uptime() -> float | None:
    """Seconds since boot, or None when unavailable.

    None (not 0.0) so the UI can say "Unknown" instead of claiming the machine
    just booted — the app never invents a reading.
    """
    if not HAS_PSUTIL:
        return None
    try:
        boot = float(psutil.boot_time())
        return max(0.0, time.time() - boot) if boot > 0 else None
    except Exception as exc:
        log.debug("boot_time failed: %s", exc)
        return None


def collect_temps() -> list[TempReading]:
    """Query sensors; never invent readings. RAM temp reported unsupported."""
    readings: list[TempReading] = []
    temps: dict = {}
    if not HAS_PSUTIL:
        readings.append(TempReading(label="sensors", current=None, source="psutil",
                                    status="unsupported"))
    else:
        try:
            temps = psutil.sensors_temperatures()
        except Exception as exc:
            log.debug("sensors_temperatures failed: %s", exc)
            temps = {}
    if not temps:
        readings.append(TempReading(label="system", current=None,
                                    source="psutil.sensors_temperatures",
                                    status="unsupported"))
    else:
        for chip, items in temps.items():
            for item in items:
                label = f"{chip}:{getattr(item, 'label', '') or 'sensor'}".strip(":")
                cur = getattr(item, "current", None)
                try:
                    cur_f = float(cur) if cur not in (None, "") else None
                except (TypeError, ValueError):
                    cur_f = None
                readings.append(TempReading(
                    label=label,
                    current=cur_f,
                    source="psutil.sensors_temperatures",
                    status="available" if cur_f is not None else "unavailable",
                ))
    # Explicit honesty: standard psutil cannot read physical RAM temperature.
    readings.append(TempReading(label="RAM", current=None,
                                source="no reliable sensor interface",
                                status="unsupported"))
    return readings


def collect_processes(limit: int = 12, sort_by: str = "cpu") -> list[ProcEntry]:
    """Return top processes; never terminates anything."""
    global _proc_primed
    out: list[ProcEntry] = []
    if not HAS_PSUTIL:
        return out
    try:
        if not _proc_primed:
            # One-time ~100 ms cost (worker thread only): prime psutil's
            # per-process CPU counters so the first real pass is not all 0.0.
            for _p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info"]):
                pass
            _proc_primed = True
            time.sleep(0.1)
        procs = []
        for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info"]):
            try:
                info = p.info
                mem_mb = 0.0
                if info.get("memory_info"):
                    mem_mb = float(info["memory_info"].rss) / (1024 * 1024)
                procs.append(ProcEntry(
                    pid=int(info.get("pid", 0)),
                    name=str(info.get("name", "?"))[:60],
                    cpu=float(info.get("cpu_percent") or 0.0),
                    mem_mb=round(mem_mb, 1),
                ))
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            except Exception as exc:
                log.debug("process row failed: %s", exc)
                continue
    except Exception as exc:
        log.warning("process_iter failed: %s", exc)
        return out
    key = (lambda e: e.mem_mb) if sort_by == "mem" else (lambda e: e.cpu)
    procs.sort(key=key, reverse=True)
    return procs[: max(1, limit)]


def collect_process_detail(pid: int, max_children: int = 40) -> dict:
    """Read-only detail for one process. Never terminates or signals anything.

    Every field is best-effort: AccessDenied / NoSuchProcess degrade to a
    readable string instead of raising, because that is normal on Windows.
    """
    detail: dict = {"pid": pid, "available": False, "error": "", "fields": {},
                    "children": [], "connections": [], "open_files": [],
                    "notes": []}
    if not HAS_PSUTIL:
        detail["error"] = "psutil not installed"
        return detail
    try:
        proc = psutil.Process(pid)
    except Exception as exc:
        detail["error"] = f"Process no longer exists or is inaccessible: {exc}"
        return detail

    detail["available"] = True
    fields = detail["fields"]

    def _safe(label: str, fn) -> None:
        try:
            fields[label] = fn()
        except psutil.AccessDenied:
            fields[label] = "Access denied"
            detail["notes"].append(label)
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            fields[label] = "Process ended"
        except Exception as exc:  # pragma: no cover - platform specific
            fields[label] = f"Unavailable ({exc})"

    with proc.oneshot():
        _safe("Name", lambda: proc.name())
        _safe("Status", lambda: proc.status())
        _safe("Executable", lambda: proc.exe())
        _safe("Command line", lambda: " ".join(proc.cmdline()) or "(empty)")
        _safe("Working directory", lambda: proc.cwd())
        _safe("User", lambda: proc.username())
        _safe("Parent PID", lambda: proc.ppid())
        _safe("Threads", lambda: proc.num_threads())
        _safe("Started", lambda: datetime.fromtimestamp(proc.create_time()).isoformat(
            timespec="seconds"))
        _safe("CPU (cumulative s)", lambda: round(sum(proc.cpu_times()[:2]), 2))
        _safe("CPU % (since app start)", lambda: proc.cpu_percent(interval=None))
        _safe("Memory RSS", lambda: format_bytes(proc.memory_info().rss))
        _safe("Memory VMS", lambda: format_bytes(proc.memory_info().vms))
        _safe("Memory %", lambda: round(proc.memory_percent(), 2))
        _safe("Nice", lambda: proc.nice())
        _safe("I/O read", lambda: format_bytes(proc.io_counters().read_bytes))
        _safe("I/O written", lambda: format_bytes(proc.io_counters().write_bytes))

    # Children: bounded, read-only listing.
    try:
        kids = proc.children()
        for child in kids[:max_children]:
            try:
                detail["children"].append((child.pid, child.name()))
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                detail["children"].append((child.pid, "?"))
        if len(kids) > max_children:
            detail["notes"].append(f"children truncated to {max_children}")
    except Exception as exc:
        detail["notes"].append(f"children unavailable ({exc})")

    # Connections and open files are the slowest / most privileged calls.
    try:
        conns = proc.net_connections(kind="inet") if hasattr(proc, "net_connections") \
            else proc.connections(kind="inet")
        for c in conns[:25]:
            laddr = getattr(c, "laddr", None)
            raddr = getattr(c, "raddr", None)
            detail["connections"].append(
                f"{c.type.name if hasattr(c.type, 'name') else c.type} "
                f"{getattr(laddr, 'ip', '?')}:{getattr(laddr, 'port', '?')} -> "
                f"{getattr(raddr, 'ip', '-')}:{getattr(raddr, 'port', '-')} "
                f"[{c.status}]")
        if len(conns) > 25:
            detail["notes"].append("connections truncated to 25")
    except psutil.AccessDenied:
        detail["notes"].append("connections: access denied")
    except Exception as exc:
        detail["notes"].append(f"connections unavailable ({exc})")

    try:
        files = proc.open_files()
        detail["open_files"] = [f.path for f in files[:25]]
        if len(files) > 25:
            detail["notes"].append("open files truncated to 25")
    except psutil.AccessDenied:
        detail["notes"].append("open files: access denied")
    except Exception as exc:
        detail["notes"].append(f"open files unavailable ({exc})")

    return detail


def collect_snapshot(include_gpu: bool = True) -> HardwareSnapshot:
    """Collect a full snapshot; each section degrades independently."""
    try:
        hostname = socket.gethostname()
    except Exception:
        hostname = "unknown"
    snap = HardwareSnapshot(
        cpu=collect_cpu(),
        mem=collect_memory(),
        disks=collect_disks(),
        net=collect_network(),
        temps=collect_temps(),
        battery=collect_battery(),
        disk_io=collect_disk_io(),
        hostname=hostname,
        os_info=f"{platform.system()} {platform.release()} ({platform.version()})",
        uptime_s=collect_uptime(),
        timestamp=time.time(),
    )
    if include_gpu:
        try:
            snap.gpu = query_gpus()
        except Exception as exc:  # never let GPU break the snapshot
            log.warning("gpu query failed: %s", exc)
            snap.gpu = GpuStatus(message=f"GPU query failed: {exc}")
    return snap


# ---------------------------------------------------------------------------
# Monitoring worker
# ---------------------------------------------------------------------------
MAX_HISTORY = 120  # bounded chart history


@dataclass
class Sample:
    t: float
    cpu: float | None
    mem: float | None
    gpu: float | None = None
    up_bps: float | None = None
    down_bps: float | None = None


class MonitorWorker:
    """Single cancellable sampler thread. Tkinter polls `poll()` on the main thread."""

    def __init__(self, interval_s: float = 1.0, include_gpu: bool = False) -> None:
        self.interval_s = max(0.5, float(interval_s))
        self.include_gpu = include_gpu
        self.samples: queue.Queue[Sample | HardwareSnapshot] = queue.Queue()
        self.history: list[Sample] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.running = False

    def start(self) -> bool:
        """Start sampling. Returns False if already running (no overlapping loops)."""
        with self._lock:
            if self.running:
                return False
            # A fresh stop event per run: a later start() must never re-arm the
            # stop flag of a thread that is still finishing its last sample.
            self._stop = threading.Event()
            thread = threading.Thread(target=self._loop, name="shds-monitor", daemon=True)
            try:
                thread.start()
            except (RuntimeError, OSError) as exc:
                # Thread creation failed: `running` must stay False, otherwise
                # monitoring can never be started again in this session.
                log.warning("monitor thread failed to start: %s", exc)
                return False
            self._thread = thread
            self.running = True
            return True

    def request_stop(self) -> None:
        """Signal the worker to stop without joining (main-thread safe)."""
        self._stop.set()

    def stop(self, timeout: float = 3.0) -> None:
        """Ask the sampler to stop. Never blocks the GUI thread for long.

        The worker may be inside a slow sample (e.g. an 8s nvidia-smi call),
        so the join happens on a helper thread instead of freezing the UI.
        The per-run stop event guarantees the old thread exits on its own
        even if it is still busy.
        """
        with self._lock:
            if not self.running:
                return
            self._stop.set()
            thread = self._thread
            self.running = False
            self._thread = None
        if thread is not None and thread.is_alive():
            threading.Thread(target=thread.join, args=(timeout,),
                             name="shds-monitor-reap", daemon=True).start()

    def set_interval(self, interval_s: float) -> None:
        self.interval_s = max(0.5, float(interval_s))

    def _loop(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                snap = collect_snapshot(include_gpu=self.include_gpu)
                sample = Sample(
                    t=time.time(),
                    cpu=snap.cpu.percent,
                    mem=snap.mem.percent,
                    gpu=(snap.gpu.gpus[0].utilization_pct if snap.gpu.gpus else None),
                    up_bps=snap.net.up_bps,
                    down_bps=snap.net.down_bps,
                )
                self.samples.put(sample)
                self.samples.put(snap)
                self.history.append(sample)
                if len(self.history) > MAX_HISTORY:
                    del self.history[: len(self.history) - MAX_HISTORY]
            except Exception as exc:  # worker must never die silently-crashing the GUI
                log.warning("monitor sample failed: %s", exc)
            wait = self.interval_s - (time.monotonic() - started)
            if wait > 0:
                self._stop.wait(wait)

    def poll(self) -> list:
        """Drain the queue (call from the GUI thread only)."""
        items: list = []
        try:
            while True:
                items.append(self.samples.get_nowait())
        except queue.Empty:
            pass
        return items

    def clear_history(self) -> None:
        self.history.clear()


@dataclass
class ScanWorker:
    """One-shot diagnostic scan on a background thread with result queue."""

    result: queue.Queue = field(default_factory=queue.Queue)
    _stop: threading.Event = field(default_factory=threading.Event)
    running: bool = False

    def start(self, snapshot: HardwareSnapshot, thresholds: dict | None = None) -> bool:
        if self.running:
            return False

        self._stop.clear()
        self.running = True

        def _job() -> None:
            try:
                # Simulate staged progress so the GUI progress bar animates.
                for _ in range(4):
                    if self._stop.is_set():
                        break
                    time.sleep(0.15)
                if not self._stop.is_set():
                    self.result.put(run_diagnostics(snapshot, thresholds))
            except Exception as exc:
                log.exception("scan failed: %s", exc)
                self.result.put(exc)
            finally:
                self.running = False

        try:
            threading.Thread(target=_job, name="shds-scan", daemon=True).start()
        except (RuntimeError, OSError) as exc:
            # Never leave `running` stuck True: the scan button would stay
            # disabled for the rest of the session.
            log.warning("scan thread failed to start: %s", exc)
            self.running = False
            return False
        return True

    def cancel(self) -> None:
        self._stop.set()


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------
# Documented defaults: high usage => WARNING (not "damaged hardware").
DEFAULT_THRESHOLDS = {
    "cpu_warn": 85.0,      # % sustained utilisation -> WARNING
    "cpu_fail": 97.0,      # % extreme utilisation -> FAIL (pressure, not damage)
    "mem_warn": 85.0,      # % used -> WARNING
    "mem_fail": 96.0,      # % used -> FAIL (pressure)
    "disk_warn": 85.0,     # % used per partition -> WARNING
    "disk_fail": 95.0,     # % used per partition -> FAIL (capacity risk)
    "temp_warn": 85.0,     # degC -> WARNING (if a real sensor reports it)
    "temp_fail": 100.0,    # degC -> FAIL
    "gpu_temp_warn": 83.0,
    "gpu_temp_fail": 95.0,
    # Battery: LOWER is worse, so warn > fail here. These two are intentionally
    # excluded from THRESHOLD_PAIRS (which enforces warn < fail) and validated
    # by diagnose_battery via _status_for_low instead.
    "battery_warn": 25.0,  # % remaining -> WARNING
    "battery_fail": 8.0,   # % remaining -> FAIL (about to hibernate)
}


@dataclass
class Finding:
    component: str
    status: str  # PASS | WARNING | FAIL | UNAVAILABLE
    message: str
    detail: str = ""
    key: str = ""  # stable identity for cross-scan comparison (see compare_reports)


@dataclass
class DiagnosticReport:
    started_at: float = 0.0
    finished_at: float = 0.0
    findings: list[Finding] = field(default_factory=list)
    summary: dict[str, int] = field(default_factory=dict)
    recommendations: list[str] = field(default_factory=list)
    health_score: int = 100

    @property
    def duration_s(self) -> float:
        return max(0.0, self.finished_at - self.started_at)


# Worse-than ordering used by compare_reports. UNAVAILABLE sits above PASS so a
# sensor dropping out shows up as a change, without claiming it failed.
SEVERITY_RANK = {"PASS": 0, "UNAVAILABLE": 1, "WARNING": 2, "FAIL": 3}


def finding_key(finding: Finding) -> str:
    """Stable key for a finding; falls back to the component name."""
    return finding.key or finding.component


def compute_health_score(findings: list[Finding]) -> int:
    """0-100 health score from findings. Deterministic and documented.

    Starts at 100 and subtracts a fixed penalty per finding:
      FAIL -20, WARNING -7, UNAVAILABLE -2, PASS 0.
    Missing data costs less than real pressure, so "unsupported sensor" never
    looks like a broken machine. Clamped to 0..100.
    """
    penalties = {"FAIL": 20, "WARNING": 7, "UNAVAILABLE": 2, "PASS": 0}
    score = 100
    for f in findings:
        score -= penalties.get(f.status, 0)
    return int(clamp(score, 0, 100))


def compare_reports(previous: DiagnosticReport | None,
                    current: DiagnosticReport) -> dict:
    """Diff two scans by stable finding key.

    Returns {"regressions": [...], "improvements": [...], "added": [...],
             "removed": [...]} where each entry is
    (key, previous_status, current_status, message). "removed" entries carry
    the previous message because the current scan has nothing to show.
    Ordering within each list follows the current (or previous) findings order,
    so output is stable for tests.
    """
    result: dict[str, list] = {"regressions": [], "improvements": [],
                               "added": [], "removed": []}
    if previous is None:
        return result
    prev_by_key = {finding_key(f): f for f in previous.findings}
    cur_by_key = {finding_key(f): f for f in current.findings}

    for key, cur in cur_by_key.items():
        if key not in prev_by_key:
            result["added"].append((key, None, cur.status, cur.message))
            continue
        prev = prev_by_key[key]
        before = SEVERITY_RANK.get(prev.status, 1)
        after = SEVERITY_RANK.get(cur.status, 1)
        if after > before:
            result["regressions"].append((key, prev.status, cur.status, cur.message))
        elif after < before:
            result["improvements"].append((key, prev.status, cur.status, cur.message))

    for key, prev in prev_by_key.items():
        if key not in cur_by_key:
            result["removed"].append((key, prev.status, None, prev.message))
    return result


def _status_for(value: float | None, warn: float, fail: float) -> str:
    if value is None:
        return "UNAVAILABLE"
    if value >= fail:
        return "FAIL"
    if value >= warn:
        return "WARNING"
    return "PASS"


def _status_for_low(value: float | None, warn: float, fail: float) -> str:
    """Inverse of _status_for: used where a LOW value is the bad one (battery)."""
    if value is None:
        return "UNAVAILABLE"
    if value <= fail:
        return "FAIL"
    if value <= warn:
        return "WARNING"
    return "PASS"


def diagnose_cpu(snap: HardwareSnapshot, th: dict) -> list[Finding]:
    cpu = snap.cpu
    if not cpu.available or cpu.percent is None:
        return [Finding("CPU", "UNAVAILABLE",
                        "CPU metrics unavailable.",
                        cpu.error or "psutil missing or access denied.",
                        key="CPU")]
    st = _status_for(cpu.percent, th["cpu_warn"], th["cpu_fail"])
    cores = f"{cpu.physical_cores}P/{cpu.logical_cores}L" if cpu.logical_cores else "n/a"
    msg = f"CPU utilisation {cpu.percent:.1f}% (cores {cores})"
    if st == "PASS":
        msg += " — healthy."
    elif st == "WARNING":
        msg += " — high load; close heavy apps if sustained."
    else:
        msg += " — extreme load; check runaway processes (no damage claimed)."
    return [Finding("CPU", st, msg, key="CPU")]


def diagnose_memory(snap: HardwareSnapshot, th: dict) -> list[Finding]:
    mem = snap.mem
    if not mem.available or mem.percent is None:
        return [Finding("Memory", "UNAVAILABLE",
                        "Memory metrics unavailable.",
                        mem.error or "psutil missing.", key="Memory")]
    st = _status_for(mem.percent, th["mem_warn"], th["mem_fail"])
    msg = (f"Memory {mem.percent:.1f}% used "
           f"({format_bytes(mem.used)} / {format_bytes(mem.total)})")
    if st == "WARNING":
        msg += " — memory pressure; consider closing tabs/apps."
    elif st == "FAIL":
        msg += " — critically low free memory; save work, close apps."
    else:
        msg += " — healthy."
    return [Finding("Memory", st, msg, key="Memory")]


def diagnose_disks(snap: HardwareSnapshot, th: dict) -> list[Finding]:
    if not snap.disks:
        return [Finding("Storage", "UNAVAILABLE", "No disk partitions detected.",
                        "psutil missing or access denied.", key="Storage")]
    out: list[Finding] = []
    for d in snap.disks:
        if not d.accessible or d.percent is None:
            out.append(Finding("Storage", "UNAVAILABLE",
                               f"{d.mountpoint or d.device}: inaccessible.",
                               d.error or "permission denied",
                               key=f"Storage:{d.mountpoint or d.device}"))
            continue
        st = _status_for(d.percent, th["disk_warn"], th["disk_fail"])
        out.append(Finding("Storage", st,
                           f"{d.mountpoint} {d.percent:.1f}% used ({d.fstype or 'fs'})",
                           f"total={d.total} used={d.used}",
                           key=f"Storage:{d.mountpoint}"))
    return out


def diagnose_gpu(snap: HardwareSnapshot, th: dict) -> list[Finding]:
    g = snap.gpu
    if not g.supported or not g.gpus:
        return [Finding("GPU", "UNAVAILABLE",
                        "No supported GPU metrics available.",
                        g.message, key="GPU")]
    out: list[Finding] = []
    for gpu in g.gpus:
        if gpu.temperature_c is not None:
            st = _status_for(gpu.temperature_c, th["gpu_temp_warn"], th["gpu_temp_fail"])
            out.append(Finding("GPU", st if st != "PASS" else "PASS",
                               f"GPU{gpu.index} {gpu.name}: {gpu.temperature_c:.0f}C" +
                               (f", util {gpu.utilization_pct:.0f}%" if gpu.utilization_pct is not None else ""),
                               key=f"GPU:{gpu.index}"))
        elif gpu.utilization_pct is not None:
            out.append(Finding("GPU", "PASS",
                               f"GPU{gpu.index} {gpu.name}: util {gpu.utilization_pct:.0f}% (temp unavailable)",
                               key=f"GPU:{gpu.index}"))
        else:
            out.append(Finding("GPU", "UNAVAILABLE",
                               f"GPU{gpu.index} {gpu.name}: metrics unavailable.",
                               key=f"GPU:{gpu.index}"))
    return out


def diagnose_network(snap: HardwareSnapshot, th: dict) -> list[Finding]:  # noqa: ARG001
    n = snap.net
    if not n.available:
        return [Finding("Network", "UNAVAILABLE", "Network counters unavailable.",
                        n.error or "psutil missing.", key="Network")]
    if not n.interfaces:
        return [Finding("Network", "WARNING", "No network interfaces reported.",
                        key="Network")]
    return [Finding("Network", "PASS",
                    f"{len(n.interfaces)} interface(s): {', '.join(n.interfaces[:6])}",
                    key="Network")]


def diagnose_battery(snap: HardwareSnapshot, th: dict) -> list[Finding]:
    """Battery rule. Desktops report UNAVAILABLE — never a fabricated level."""
    b = snap.battery
    if not b.available or b.percent is None:
        return [Finding("Battery", "UNAVAILABLE",
                        "No battery detected or battery data unavailable.",
                        b.error or "psutil sensors_battery returned nothing.",
                        key="Battery")]
    if b.plugged:
        return [Finding("Battery", "PASS",
                        f"Battery {b.percent:.0f}% — on AC power.",
                        key="Battery")]
    st = _status_for_low(b.percent, th["battery_warn"], th["battery_fail"])
    msg = f"Battery {b.percent:.0f}% — on battery"
    if b.secs_left is not None:
        msg += f", about {format_uptime(b.secs_left)} remaining"
    if st == "WARNING":
        msg += ". Consider plugging in."
    elif st == "FAIL":
        msg += ". Critically low — plug in now to avoid an abrupt shutdown."
    else:
        msg += "."
    return [Finding("Battery", st, msg, key="Battery")]


def diagnose_temps(snap: HardwareSnapshot, th: dict) -> list[Finding]:
    real = [t for t in snap.temps if t.status == "available" and t.current is not None]
    if not real:
        return [Finding("Sensors", "UNAVAILABLE",
                        "No reliable temperature sensors exposed on this machine.",
                        "Unsupported sensors are reported, never invented.",
                        key="Sensors")]
    out: list[Finding] = []
    for t in real[:8]:
        assert t.current is not None
        st = _status_for(t.current, th["temp_warn"], th["temp_fail"])
        out.append(Finding("Sensors", st, f"{t.label}: {t.current:.1f}C (source: {t.source})",
                           key=f"Sensors:{t.label}"))
    return out


def _disk_advice() -> str:
    """Platform-appropriate 'free up disk space' advice (no tool is run)."""
    system = platform.system().lower()
    if system == "windows":
        return ("Disk nearly full: free space via Windows Disk Clean-up; "
                "empty the Recycle Bin.")
    if system == "darwin":
        return ("Disk nearly full: free space via System Settings > General > "
                "Storage (Optimize Storage).")
    return ("Disk nearly full: free space with your distribution's disk-usage "
            "tool (e.g. baobab); clear package caches and old journal logs.")


def _battery_advice() -> str:
    """Platform-appropriate 'battery low' advice (no tool is run)."""
    system = platform.system().lower()
    if system == "windows":
        return ("Battery low: plug in the charger. If the level drops faster "
                "than usual, check Settings > Power & battery for the battery "
                "report.")
    if system == "darwin":
        return ("Battery low: plug in the charger. If the level drops faster "
                "than usual, check System Settings > Battery for usage "
                "details.")
    return ("Battery low: plug in the charger. If the level drops faster than "
            "usual, check your desktop's power settings or `upower -i` "
            "output.")


def build_recommendations(findings: list[Finding]) -> list[str]:
    recs: list[str] = []
    comps = {f.component: f.status for f in findings}
    if comps.get("CPU") in ("WARNING", "FAIL"):
        recs.append("High CPU: identify the top process in the Processes tab; close or restart it. No settings were changed.")
    if comps.get("Memory") in ("WARNING", "FAIL"):
        recs.append("High memory: close unused browser tabs/apps and save work; consider adding RAM only if pressure is chronic.")
    if any(f.component == "Storage" and f.status in ("WARNING", "FAIL") for f in findings):
        recs.append(_disk_advice() + " This app deletes nothing by itself.")
    if any(f.component == "Sensors" and f.status in ("WARNING", "FAIL") for f in findings):
        recs.append("High temperature: ensure vents are clear and fans run; avoid blocking airflow. Seek service if it persists.")
    if comps.get("GPU") in ("WARNING", "FAIL"):
        recs.append("GPU running hot or heavily loaded: close GPU-heavy apps and "
                    "check airflow/cooling; no settings were changed.")
    if comps.get("GPU") == "UNAVAILABLE":
        recs.append("GPU metrics unavailable: install vendor drivers / nvidia-smi for NVIDIA, or use vendor tools for AMD/Intel.")
    if comps.get("Sensors") == "UNAVAILABLE":
        recs.append("Temperature sensors unsupported here: readings are honestly reported as unavailable, not estimated.")
    if comps.get("Battery") in ("WARNING", "FAIL"):
        recs.append(_battery_advice())
    if not recs:
        if any(f.status in ("WARNING", "FAIL") for f in findings):
            # Unhandled component-specific problem (e.g. a missing NIC): never
            # claim "all healthy" when something was actually flagged.
            recs.append("Some checks need attention — review the findings above.")
        else:
            recs.append("All checks look healthy. Re-run the scan after heavy workloads to compare.")
    void = [f for f in findings if f.status == "UNAVAILABLE"]
    if void:
        recs.append(f"{len(void)} check(s) unavailable — see findings for the exact reason (missing sensor, driver, or permission).")
    return recs


def run_diagnostics(snap: HardwareSnapshot, thresholds: dict | None = None) -> DiagnosticReport:
    """Run all diagnostic rules against a snapshot."""
    th = dict(DEFAULT_THRESHOLDS)
    if thresholds:
        th.update(thresholds)
    started = time.time()
    findings: list[Finding] = []
    findings += diagnose_cpu(snap, th)
    findings += diagnose_memory(snap, th)
    findings += diagnose_disks(snap, th)
    findings += diagnose_gpu(snap, th)
    findings += diagnose_network(snap, th)
    findings += diagnose_temps(snap, th)
    findings += diagnose_battery(snap, th)
    summary = {"PASS": 0, "WARNING": 0, "FAIL": 0, "UNAVAILABLE": 0}
    for f in findings:
        summary[f.status] = summary.get(f.status, 0) + 1
    report = DiagnosticReport(
        started_at=started,
        finished_at=time.time(),
        findings=findings,
        summary=summary,
        recommendations=build_recommendations(findings),
        health_score=compute_health_score(findings),
    )
    return report


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------
def build_report_dict(snap: HardwareSnapshot, diag: DiagnosticReport,
                      thresholds: dict) -> dict:
    """Serialisable report payload."""
    ts = datetime.fromtimestamp(diag.finished_at or time.time()).isoformat(timespec="seconds")
    disks = [
        {"device": d.device, "mountpoint": d.mountpoint, "fstype": d.fstype,
         "total": d.total, "used": d.used, "free": d.free, "percent": d.percent,
         "accessible": d.accessible, "error": d.error}
        for d in snap.disks
    ]
    gpu = {
        "supported": snap.gpu.supported,
        "message": snap.gpu.message,
        "gpus": [asdict(g) for g in snap.gpu.gpus],
    }
    temps = [asdict(t) for t in snap.temps]
    return {
        "tool": "Smart Hardware Diagnostic Scanner — Dark Neon Edition",
        "author": "Devansh",
        "timestamp": ts,
        "os": snap.os_info or f"{platform.system()} {platform.release()}",
        "python": python_info(),
        "hostname": snap.hostname,
        "scan_duration_s": round(diag.duration_s, 2),
        "health_score": diag.health_score,
        "uptime_s": (round(snap.uptime_s, 1)
                     if snap.uptime_s is not None else None),
        "uptime_human": format_uptime(snap.uptime_s),
        "thresholds": dict(thresholds),
        "hardware": {
            "cpu": asdict(snap.cpu),
            "memory": asdict(snap.mem),
            "disks": disks,
            "network": asdict(snap.net),
            "temperatures": temps,
            "gpu": gpu,
            "battery": asdict(snap.battery),
            "disk_io": asdict(snap.disk_io),
        },
        "findings": [asdict(f) for f in diag.findings],
        "summary": dict(diag.summary),
        "recommendations": list(diag.recommendations),
    }


def render_txt(payload: dict) -> str:
    """Human-readable text rendering of the payload."""
    score_raw = payload.get("health_score")
    try:
        score_value = int(score_raw)
    except (TypeError, ValueError):
        score_text = "n/a" if score_raw is None else f"n/a ({score_raw})"
    else:
        score_text = f"{score_value}/100 ({health_label(score_value)})"
    lines = [
        "SMART HARDWARE DIAGNOSTIC SCANNER — DARK NEON EDITION",
        f"Author: {payload.get('author', 'Devansh')}",
        f"Timestamp: {payload.get('timestamp', '')}",
        f"OS: {payload.get('os', '')}",
        f"Python: {payload.get('python', '')}",
        f"Host: {payload.get('hostname', '')}",
        f"Scan duration: {payload.get('scan_duration_s', 0)}s",
        f"Uptime: {payload.get('uptime_human', 'Unknown')}",
        f"Health score: {score_text}",
        "",
        "== SUMMARY ==",
    ]
    for key, val in payload.get("summary", {}).items():
        lines.append(f"  {key}: {val}")
    lines.append("")
    lines.append("== HARDWARE ==")
    hw = payload.get("hardware", {})
    cpu = hw.get("cpu", {})
    mem = hw.get("memory", {})
    cpu_pct = cpu.get("percent")
    mem_pct = mem.get("percent")
    lines.append(f"CPU: {cpu_pct if cpu_pct is not None else 'n/a'}% logical={cpu.get('logical_cores')} "
                 f"physical={cpu.get('physical_cores')} freq={cpu.get('freq_current_mhz')} MHz")
    lines.append(f"Memory: {mem_pct if mem_pct is not None else 'n/a'}% used "
                 f"({format_bytes(mem.get('used', 0))} / {format_bytes(mem.get('total', 0))})")
    for d in hw.get("disks", []):
        if d.get("accessible"):
            lines.append(f"Disk {d.get('mountpoint')}: {d.get('percent')}% used "
                         f"({format_bytes(d.get('used', 0))} / {format_bytes(d.get('total', 0))})")
        else:
            lines.append(f"Disk {d.get('mountpoint') or d.get('device')}: UNAVAILABLE ({d.get('error', '')})")
    batt = hw.get("battery", {})
    if batt.get("available"):
        plug = "on AC power" if batt.get("plugged") else "on battery"
        lines.append(f"Battery: {batt.get('percent')}% ({plug})")
    else:
        lines.append(f"Battery: UNAVAILABLE ({batt.get('error', 'no battery')})")
    dio = hw.get("disk_io", {})
    if dio.get("available"):
        lines.append(f"Disk I/O: read {format_rate_bytes(dio.get('read_bps'))} · "
                     f"write {format_rate_bytes(dio.get('write_bps'))}")
    else:
        lines.append(f"Disk I/O: UNAVAILABLE ({dio.get('error', 'no counters')})")
    net = hw.get("network", {})
    lines.append(f"Network: interfaces={', '.join(net.get('interfaces', [])) or 'none'}")
    gpu = hw.get("gpu", {})
    lines.append(f"GPU: {gpu.get('message', 'Unavailable')}")
    for g in gpu.get("gpus", []):
        lines.append(f"  GPU{g.get('index')} {g.get('name')}: util={g.get('utilization_pct')} "
                     f"temp={g.get('temperature_c')} mem={g.get('memory_used_mb')}/{g.get('memory_total_mb')} MB")
    lines.append("Temperatures:")
    for t in hw.get("temperatures", []):
        if t.get("status") == "available":
            lines.append(f"  {t.get('label')}: {t.get('current')} C (source: {t.get('source')})")
        else:
            lines.append(f"  {t.get('label')}: {str(t.get('status') or 'unknown').upper()} "
                         f"(source: {t.get('source')})")
    lines.append("")
    lines.append("== FINDINGS ==")
    for f in payload.get("findings", []):
        lines.append(f"[{f.get('status')}] {f.get('component')}: {f.get('message')}")
        if f.get("detail"):
            lines.append(f"    detail: {f.get('detail')}")
    lines.append("")
    lines.append("== RECOMMENDATIONS ==")
    for r in payload.get("recommendations", []):
        lines.append(f" - {r}")
    lines.append("")
    return "\n".join(lines)


def _html_page(title: str, body: str) -> str:
    """Wrap body markup in a self-contained dark-neon HTML document."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
  :root {{
    --bg:#12121A; --panel:#1C1C28; --card:#232333; --border:#34344A;
    --fg:#E8E8F0; --muted:#9A9AB0; --cyan:#00E5FF; --purple:#B266FF;
    --green:#39FF88; --yellow:#FFD60A; --red:#FF4D6D; --orange:#FF9F1C;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; padding:28px; background:var(--bg); color:var(--fg);
         font:14px/1.55 "Segoe UI",system-ui,sans-serif; }}
  h1 {{ font-size:22px; margin:0 0 4px; color:var(--cyan); }}
  h2 {{ font-size:15px; margin:26px 0 10px; color:var(--cyan);
        border-bottom:1px solid var(--border); padding-bottom:6px; }}
  .sub {{ color:var(--muted); font-size:12px; margin-bottom:18px; }}
  table {{ border-collapse:collapse; width:100%; background:var(--panel);
           border:1px solid var(--border); }}
  th,td {{ text-align:left; padding:7px 10px; border-bottom:1px solid var(--border);
           font-size:13px; vertical-align:top; }}
  th {{ background:var(--card); color:var(--cyan); font-weight:600; }}
  tr:last-child td {{ border-bottom:none; }}
  .grid {{ display:flex; flex-wrap:wrap; gap:10px; }}
  .kv {{ background:var(--card); border:1px solid var(--border); border-radius:8px;
         padding:10px 12px; min-width:170px; flex:1 1 170px; }}
  .kv .k {{ color:var(--muted); font-size:11px; text-transform:uppercase;
            letter-spacing:.5px; }}
  .kv .v {{ font-size:16px; margin-top:3px; }}
  .b-PASS {{ color:var(--green); }} .b-WARNING {{ color:var(--yellow); }}
  .b-FAIL {{ color:var(--red); }} .b-UNAVAILABLE {{ color:var(--muted); }}
  .b-INFO {{ color:var(--cyan); }}
  .score {{ font-size:44px; font-weight:700; line-height:1; }}
  .bar {{ height:8px; background:var(--card); border-radius:4px; overflow:hidden;
          margin-top:8px; }}
  .bar > span {{ display:block; height:100%; background:var(--cyan); }}
  ol,ul {{ padding-left:20px; }} li {{ margin:4px 0; }}
  img {{ max-width:100%; border:1px solid var(--border); border-radius:8px;
         background:var(--panel); }}
  footer {{ margin-top:26px; color:var(--muted); font-size:11px; }}
  code {{ background:var(--card); padding:1px 5px; border-radius:4px; }}
</style>
</head>
<body>
{body}
<footer>Generated by Smart Hardware Diagnostic Scanner — Dark Neon Edition · by Devansh</footer>
</body>
</html>
"""


def _score_colour_class(score: int) -> str:
    if score >= 90:
        return "b-PASS"
    if score >= 55:
        return "b-WARNING"
    return "b-FAIL"


def render_html(payload: dict, chart_png_b64: str | None = None) -> str:
    """Render a self-contained HTML report (no external assets)."""
    esc = html.escape
    score = int(payload.get("health_score", 100) or 0)
    summary = payload.get("summary", {}) or {}
    hw = payload.get("hardware", {}) or {}

    cards = [
        ("Health score", f'<span class="score {_score_colour_class(score)}">{score}</span>'
                         f'<span class="b-INFO">/100 &nbsp;{esc(health_label(score))}</span>'
                         f'<div class="bar"><span style="width:{score}%;'
                         f'background:{"var(--green)" if score >= 90 else "var(--yellow)" if score >= 55 else "var(--red)"}">'
                         f'</span></div>'),
        ("Host", esc(str(payload.get("hostname", "")))),
        ("OS", esc(str(payload.get("os", "")))),
        ("Uptime", esc(str(payload.get("uptime_human", "Unknown")))),
        ("Scan time", esc(str(payload.get("timestamp", "")))),
        ("Duration", f'{esc(str(payload.get("scan_duration_s", 0)))} s'),
    ]
    cards_html = "".join(
        f'<div class="kv"><div class="k">{esc(k)}</div><div class="v">{v}</div></div>'
        for k, v in cards)

    summary_html = "".join(
        f'<div class="kv"><div class="k">{esc(k)}</div>'
        f'<div class="v b-{esc(k)}">{int(summary.get(k, 0))}</div></div>'
        for k in ("PASS", "WARNING", "FAIL", "UNAVAILABLE"))

    rows = []
    for f in payload.get("findings", []):
        status = str(f.get("status", ""))
        rows.append(
            f'<tr><td class="b-{esc(status)}">{esc(status)}</td>'
            f'<td>{esc(str(f.get("component", "")))}</td>'
            f'<td>{esc(str(f.get("message", "")))}</td>'
            f'<td class="sub">{esc(str(f.get("detail", "")))}</td></tr>')
    findings_html = ("<table><tr><th>Status</th><th>Component</th><th>Finding</th>"
                     "<th>Detail</th></tr>" + "".join(rows) + "</table>") if rows \
        else "<p class='sub'>No findings.</p>"

    def _fmt_pct(v) -> str:
        return "n/a" if v is None else f"{float(v):.1f} %"

    cpu = hw.get("cpu", {}) or {}
    mem = hw.get("memory", {}) or {}
    hw_rows = [
        ("CPU", f"{_fmt_pct(cpu.get('percent'))} · {cpu.get('logical_cores', '?')} logical cores"
                f" · {cpu.get('freq_current_mhz', '—')} MHz"),
        ("Memory", f"{_fmt_pct(mem.get('percent'))} used · "
                   f"{format_bytes(mem.get('used', 0))} / {format_bytes(mem.get('total', 0))}"),
    ]
    batt = hw.get("battery", {}) or {}
    hw_rows.append(("Battery", (f"{batt.get('percent')}% · "
                                f"{'on AC power' if batt.get('plugged') else 'on battery'}")
                   if batt.get("available") else f"Unavailable — {batt.get('error', 'no battery')}"))
    dio = hw.get("disk_io", {}) or {}
    hw_rows.append(("Disk I/O", (f"read {format_rate_bytes(dio.get('read_bps'))} · "
                                 f"write {format_rate_bytes(dio.get('write_bps'))}")
                   if dio.get("available") else f"Unavailable — {dio.get('error', 'no counters')}"))
    net = hw.get("network", {}) or {}
    hw_rows.append(("Network", ", ".join(net.get("interfaces", [])) or "none"))
    gpu = hw.get("gpu", {}) or {}
    hw_rows.append(("GPU", str(gpu.get("message", "Unavailable"))))
    for d in hw.get("disks", []):
        if d.get("accessible"):
            hw_rows.append((f"Disk {d.get('mountpoint')}",
                            f"{_fmt_pct(d.get('percent'))} used · "
                            f"{format_bytes(d.get('used', 0))} / {format_bytes(d.get('total', 0))}"))
        else:
            hw_rows.append((f"Disk {d.get('mountpoint') or d.get('device')}",
                            f"Unavailable — {d.get('error', '')}"))
    for t in hw.get("temperatures", []):
        if t.get("status") == "available":
            hw_rows.append((f"Temp {t.get('label')}", f"{t.get('current')} °C"))
    hw_html = "".join(
        f'<div class="kv"><div class="k">{esc(k)}</div>'
        f'<div class="v">{esc(str(v))}</div></div>'
        for k, v in hw_rows)

    recs_html = "".join(f"<li>{esc(str(r))}</li>"
                        for r in payload.get("recommendations", [])) or "<li>None.</li>"
    chart_html = (f'<img alt="Trend chart" src="data:image/png;base64,{chart_png_b64}">'
                  if chart_png_b64 else
                  '<p class="sub">Chart unavailable (matplotlib not installed).</p>')
    th = payload.get("thresholds", {}) or {}
    th_html = "".join(f'<div class="kv"><div class="k">{esc(str(k))}</div>'
                      f'<div class="v">{esc(str(v))}</div></div>' for k, v in th.items())

    body = f"""
<h1>{esc(payload.get("tool", "Hardware Report"))}</h1>
<div class="sub">Author: {esc(str(payload.get("author", "Devansh")))} ·
Python {esc(str(payload.get("python", "")))}</div>
<div class="grid">{cards_html}</div>

<h2>Outcome</h2>
<div class="grid">{summary_html}</div>

<h2>Trend</h2>
{chart_html}

<h2>Hardware</h2>
<div class="grid">{hw_html}</div>

<h2>Findings</h2>
{findings_html}

<h2>Recommendations</h2>
<ol>{recs_html}</ol>

<h2>Thresholds in force</h2>
<div class="grid">{th_html}</div>
"""
    return _html_page(f"SHDS report {payload.get('timestamp', '')}", body)


def render_trend_png_b64(points: list[dict]) -> str | None:
    """Small health/history chart as base64 PNG, or None when matplotlib is absent.

    Uses an Agg canvas attached to a plain Figure so it works without Tk.
    """
    if not HAS_MPL or not points:
        return None
    try:
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure as MplFigure

        labels = [str(p.get("ts", ""))[-8:] for p in points]
        scores = [p.get("score") or 0 for p in points]
        fig = MplFigure(figsize=(7.2, 2.4), dpi=100, facecolor="#1C1C28")
        FigureCanvasAgg(fig)
        ax = fig.add_subplot(111, facecolor="#1C1C28")
        xs = list(range(len(labels)))
        ax.plot(xs, scores, color="#00E5FF", linewidth=1.8, marker="o",
                markersize=3, label="Health score")
        ax.set_ylim(0, 100)
        ax.set_xticks(xs[:: max(1, len(xs) // 8)])
        ax.set_xticklabels(labels[:: max(1, len(labels) // 8)], fontsize=7,
                           color="#9A9AB0")
        ax.tick_params(colors="#9A9AB0", labelsize=7)
        for spine in ax.spines.values():
            spine.set_color("#34344A")
        ax.grid(True, color="#34344A", linewidth=0.4, alpha=0.6)
        ax.set_title("Health score over time", color="#E8E8F0", fontsize=9)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", facecolor="#1C1C28")
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as exc:  # chart is a nice-to-have, never fatal
        log.debug("trend png failed: %s", exc)
        return None


def save_report(payload: dict, dest: str | Path,
                chart_png_b64: str | None = None) -> Path:
    """Write JSON/TXT/CSV/HTML based on suffix. Raises with friendly message."""
    path = safe_path_for_report(str(dest))
    try:
        if path.suffix.lower() == ".json":
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        elif path.suffix.lower() == ".txt":
            path.write_text(render_txt(payload), encoding="utf-8")
        elif path.suffix.lower() == ".html":
            path.write_text(render_html(payload, chart_png_b64), encoding="utf-8")
        elif path.suffix.lower() == ".csv":
            with path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(["component", "status", "message", "detail"])
                for f in payload.get("findings", []):
                    writer.writerow([f.get("component"), f.get("status"),
                                     f.get("message"), f.get("detail", "")])
        else:
            raise ValueError("Unsupported report format.")
    except (PermissionError, OSError, ValueError) as exc:
        log.warning("report save failed: %s", exc)
        raise
    return path


# ---------------------------------------------------------------------------
# Configuration persistence
# ---------------------------------------------------------------------------
CONFIG_FILENAME = "config.json"


def render_comparison_html(comparison: dict,
                           previous_payload: dict | None,
                           current_payload: dict) -> str:
    """Render a self-contained HTML document diffing two scans.

    Uses the same dark-neon shell as regular reports; nothing is invented —
    only statuses that actually changed (or appeared/disappeared) are listed.
    """
    esc = html.escape
    cur_ts = str(current_payload.get("timestamp", ""))
    prev_ts = str((previous_payload or {}).get("timestamp", "") or "n/a")
    host = str(current_payload.get("hostname", ""))
    cur_score = int(current_payload.get("health_score", 100) or 0)
    prev_score = (previous_payload or {}).get("health_score")

    buckets = [("regressions", "Worse", "b-FAIL"),
               ("improvements", "Better", "b-PASS"),
               ("added", "New since last scan", "b-INFO"),
               ("removed", "No longer reported", "b-UNAVAILABLE")]
    sections: list[str] = []
    total_changes = 0
    for bucket, heading, _colour_class in buckets:
        rows = comparison.get(bucket, []) or []
        total_changes += len(rows)
        if not rows:
            continue
        trs = []
        for entry in rows:
            key, before, after, message = entry
            if before is None:
                before_cell = "—"
            else:
                before_status = esc(str(before))
                before_cell = f'<span class="b-{before_status}">{before_status}</span>'
            trs.append(
                "<tr>"
                f"<td><code>{esc(str(key))}</code></td>"
                f"<td>{before_cell}</td>"
                f"<td>{esc(str(after)) if after is not None else '—'}</td>"
                f"<td>{esc(str(message))}</td>"
                "</tr>")
        sections.append(
            f"<h2>{esc(heading)} ({len(rows)})</h2>"
            "<table><tr><th>Check</th><th>Before</th><th>After</th><th>Message</th></tr>"
            + "".join(trs) + "</table>")
    if not sections:
        sections.append("<h2>No changes</h2><p>Every check kept the same "
                        "status in both scans.</p>")
    prev_score_html = "—" if prev_score is None else str(int(prev_score))
    body = f"""
<h1>Scan comparison (diff)</h1>
<div class="sub">Host {esc(host)} · previous scan {esc(prev_ts)} · current scan {esc(cur_ts)}</div>
<div class="grid">
  <div class="kv"><div class="k">Health score before</div><div class="v">{esc(prev_score_html)}</div></div>
  <div class="kv"><div class="k">Health score now</div><div class="v"><span class="{_score_colour_class(cur_score)}">{cur_score}</span></div></div>
  <div class="kv"><div class="k">Changed checks</div><div class="v">{total_changes}</div></div>
</div>
{"".join(sections)}
"""
    return _html_page("SHDS scan comparison", body)


def config_path() -> Path:
    """Per-user config file location."""
    return _user_data_root() / CONFIG_FILENAME


def load_config(path: Path | None = None) -> dict:
    """Read the config dict; any problem yields {} (defaults)."""
    target = path or config_path()
    try:
        if target.exists():
            # utf-8-sig: Windows Notepad and PowerShell write a BOM by default,
            # and a BOM would make json.loads fail and silently discard every
            # saved setting (thresholds, theme, geometry, alert toggle).
            data = json.loads(target.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict):
                return data
            log.warning("config file is not a JSON object; ignoring it")
    except (OSError, ValueError) as exc:
        log.warning("config load failed (%s); using defaults", exc)
    return {}


def save_config(data: dict, path: Path | None = None) -> bool:
    """Write the config atomically. Returns False (never raises) on failure."""
    target = path or config_path()
    tmp = target.with_name(target.name + ".tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, target)  # avoids a half-written config on crash
        return True
    except (OSError, ValueError, TypeError) as exc:
        # TypeError: json.dumps rejects non-serialisable values.
        log.warning("config save failed: %s", exc)
        return False
    finally:
        # Never leave a stray .tmp file behind when the write failed.
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def sanitise_thresholds(raw: object) -> dict:
    """Merge stored thresholds over the defaults, discarding junk values.

    Only known keys with a numeric value in 0..200 survive, so a hand-edited or
    corrupt config can never inject an unknown key or a nonsense threshold.
    """
    merged = dict(DEFAULT_THRESHOLDS)
    if isinstance(raw, dict):
        for key, value in raw.items():
            if key not in merged:
                continue
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(numeric) and 0 <= numeric <= 200:
                merged[key] = numeric
    return merged


_GEOMETRY_RE = re.compile(r"^\d+x\d+([+-]\d+[+-]\d+)?$")
DEFAULT_GEOMETRY = "1180x760"


def safe_geometry(raw: object) -> str:
    """Return a window geometry Tk will accept, or the default.

    A hand-edited or corrupt config.json must never brick the application:
    `Tk.geometry("garbage")` raises TclError, which would abort startup.
    """
    text = str(raw or "").strip()
    if _GEOMETRY_RE.match(text):
        return text
    return DEFAULT_GEOMETRY


# ---------------------------------------------------------------------------
# Startup entries (read-only) — Windows Run keys + Startup folder, Linux
# ~/.config/autostart and /etc/xdg/autostart, macOS LaunchAgents. Nothing here
# disables, edits or removes an entry; the list is purely informational.
# ---------------------------------------------------------------------------
@dataclass
class StartupEntry:
    name: str = ""      # human label
    command: str = ""   # what is launched (path or Exec value)
    source: str = ""    # where it was found: registry key path or file path
    kind: str = ""      # "registry" | "startup-folder" | "autostart" | "launchagent"
    note: str = ""


def _win_registry_entries() -> tuple[list[StartupEntry], str]:
    """HKCU/HKLM Run + RunOnce values. Windows only; degrades on failure."""
    entries: list[StartupEntry] = []
    errors: list[str] = []
    try:
        import winreg
    except ImportError:
        return [], "winreg unavailable (not Windows?)."
    roots = [
        (winreg.HKEY_CURRENT_USER, "HKCU"),
        (winreg.HKEY_LOCAL_MACHINE, "HKLM"),
    ]
    for root, label in roots:
        for sub in (r"Software\Microsoft\Windows\CurrentVersion\Run",
                    r"Software\Microsoft\Windows\CurrentVersion\RunOnce"):
            source = rf"{label}\{sub}"
            try:
                key = winreg.OpenKey(root, sub)
            except OSError as exc:
                # RunOnce keys legitimately may not exist.
                log.debug("registry open failed for %s: %s", source, exc)
                continue
            try:
                index = 0
                while True:
                    try:
                        name, value, _regtype = winreg.EnumValue(key, index)
                    except OSError:
                        break  # end of values
                    index += 1
                    if isinstance(value, bytes):
                        value = value.decode("utf-8", errors="replace")
                    entries.append(StartupEntry(
                        name=name or "(unnamed)",
                        command=str(value), source=source, kind="registry"))
            except OSError as exc:
                errors.append(f"{source}: {exc}")
            finally:
                try:
                    key.Close()
                except (OSError, AttributeError):
                    pass
    return entries, "; ".join(errors)


def _win_startup_folder_entries() -> tuple[list[StartupEntry], str]:
    """Files in the user's Start Menu Startup folder (name only, no parsing)."""
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        return [], "APPDATA environment variable is not set."
    folder = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / \
        "Programs" / "Startup"
    if not folder.is_dir():
        return [], f"Startup folder not found: {folder}"
    entries: list[StartupEntry] = []
    for path in sorted(folder.iterdir()):
        try:
            if not path.is_file():
                continue
            entries.append(StartupEntry(
                name=path.stem, command=path.name, source=str(folder),
                kind="startup-folder",
                note="shortcut/loader file — inspect manually"))
        except OSError:
            continue
    return entries, ""


def user_startup_dirs() -> list[Path]:
    """Platform startup-entry directories that exist (never created)."""
    home = os.path.expanduser("~")
    system = platform.system().lower()
    if system == "darwin":
        candidates = [Path(home) / "Library" / "LaunchAgents",
                      Path("/Library/LaunchAgents")]
    else:
        candidates = [Path(home) / ".config" / "autostart",
                      Path("/etc/xdg/autostart")]
    return [p for p in candidates if p.is_dir()]


def _parse_desktop_file(path: Path) -> StartupEntry | None:
    """Forgiving parse of a freedesktop .desktop autostart entry."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log.debug("autostart read failed for %s: %s", path, exc)
        return None
    if text.startswith("\ufeff"):  # BOM
        text = text.lstrip("\ufeff")
    values: dict[str, str] = {}
    in_entry_section = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("["):
            in_entry_section = stripped.lower() == "[desktop entry]"
            continue
        if not in_entry_section or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip().lower()] = value.strip()
    if not values:
        return None
    if values.get("type", "").lower() != "application":
        return None  # e.g. Link entries are not startup programs
    note = ""
    if values.get("hidden", "").lower() == "true":
        note = "hidden"
    elif values.get("nodisplay", "").lower() == "true":
        note = "not shown"
    return StartupEntry(
        name=values.get("name") or path.stem,
        command=values.get("exec", ""),
        source=str(path), kind="autostart", note=note)


def _parse_plist(path: Path) -> StartupEntry | None:
    """Forgiving parse of a macOS LaunchAgent .plist."""
    try:
        import plistlib
        with open(path, "rb") as fh:
            data = plistlib.load(fh)
    except (OSError, ValueError, ImportError) as exc:
        log.debug("plist read failed for %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        return None
    label = str(data.get("Label") or path.stem)
    args = data.get("ProgramArguments")
    if isinstance(args, list) and args:
        command = " ".join(str(a) for a in args)
    else:
        command = str(data.get("Program", ""))
    disabled = "disabled" if data.get("Disabled") is True else ""
    return StartupEntry(name=label, command=command, source=str(path),
                        kind="launchagent", note=disabled)


def _autostart_entries(directories: list[Path]) -> list[StartupEntry]:
    entries: list[StartupEntry] = []
    for directory in directories:
        for path in sorted(directory.iterdir()):
            try:
                if not path.is_file():
                    continue
            except OSError:
                continue
            if path.suffix.lower() == ".desktop":
                parsed = _parse_desktop_file(path)
                if parsed is not None:
                    entries.append(parsed)
            else:
                log.debug("skipping non-.desktop startup file %s", path)
    return entries


def _launchagent_entries(directories: list[Path]) -> list[StartupEntry]:
    entries: list[StartupEntry] = []
    for directory in directories:
        for path in sorted(directory.iterdir()):
            try:
                if not path.is_file() or path.suffix.lower() != ".plist":
                    continue
            except OSError:
                continue
            parsed = _parse_plist(path)
            if parsed is not None:
                entries.append(parsed)
    return entries


def collect_startup_entries() -> tuple[list[StartupEntry], str]:
    """Enumerate OS startup entries. Read-only; never raises."""
    system = platform.system().lower()
    errors: list[str] = []
    entries: list[StartupEntry] = []
    counts: dict[str, int] = {}
    try:
        if system == "windows":
            reg, err = _win_registry_entries()
            entries.extend(reg)
            counts["registry"] = len(reg)
            if err:
                errors.append(err)
            folder, err = _win_startup_folder_entries()
            entries.extend(folder)
            counts["startup-folder"] = len(folder)
            if err:
                errors.append(err)
        else:
            dirs = user_startup_dirs()
            if system == "darwin":
                launch = _launchagent_entries(dirs)
                entries.extend(launch)
                counts["launchagent"] = len(launch)
            else:
                auto = _autostart_entries(dirs)
                entries.extend(auto)
                counts["autostart"] = len(auto)
    except Exception as exc:  # noqa: BLE001 - listing must never crash the GUI
        log.debug("startup enumeration failed: %s", exc)
        errors.append(f"enumeration failed: {exc}")
    parts = [f"{count} {kind}" for kind, count in counts.items() if count]
    note = f"{len(entries)} startup entries found ({', '.join(parts)})." \
        if entries else "No startup entries found."
    if errors:
        note += "; source errors: " + "; ".join(errors)
    return entries, note


# ---------------------------------------------------------------------------
# Largest-files finder (read-only): top-N regular files under a folder with a
# time budget. Never deletes or writes anything.
# ---------------------------------------------------------------------------
@dataclass
class BigFile:
    path: str = ""
    size_bytes: int = 0


def largest_files(root: "str | os.PathLike[str]", limit: int = 25,
                  time_budget_s: float = 20.0,
                  max_depth: int = 8) -> tuple[list[BigFile], str]:
    """Read-only top-N largest regular files under root. Never raises."""
    import heapq
    base = Path(root)
    try:
        if not base.exists():
            return [], f"Path does not exist: {base}"
        if base.is_file():
            try:
                return ([BigFile(str(base), base.stat().st_size)],
                        "Single file provided.")
            except OSError as exc:
                return [], f"Cannot stat file: {exc}"
        if not base.is_dir():
            return [], f"Not a directory: {base}"
    except OSError as exc:
        return [], f"Cannot access path: {exc}"

    heap: list[tuple[int, str]] = []  # (size, path) min-heap of size `limit`
    files_seen = 0
    dirs_skipped = 0
    deadline = time.monotonic() + max(0.0, float(time_budget_s))
    partial = False

    def _walk(directory: Path, depth: int) -> None:
        nonlocal files_seen, dirs_skipped, partial
        if depth > max_depth:
            return
        if time.monotonic() > deadline:
            partial = True
            return
        try:
            with os.scandir(directory) as it:
                count = 0
                for entry in it:
                    count += 1
                    if count % 256 == 0 and time.monotonic() > deadline:
                        partial = True
                        return
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            _walk(Path(entry.path), depth + 1)
                            if partial:
                                return
                        elif entry.is_file(follow_symlinks=False):
                            files_seen += 1
                            size = entry.stat(follow_symlinks=False).st_size
                            if len(heap) < limit:
                                heapq.heappush(heap, (size, entry.path))
                            elif size > heap[0][0]:
                                heapq.heapreplace(heap, (size, entry.path))
                    except OSError as exc:
                        if entry.is_dir(follow_symlinks=False):
                            dirs_skipped += 1
                        else:
                            log.debug("skip unreadable %s: %s", entry.path, exc)
        except (PermissionError, OSError) as exc:
            dirs_skipped += 1
            log.debug("cannot enter %s: %s", directory, exc)

    _walk(base, 0)
    best = sorted(heap, reverse=True)
    out = [BigFile(path=p, size_bytes=s) for s, p in best]
    note = (f"Seen {files_seen} file(s), top {len(out)} shown"
            + (f", {dirs_skipped} dir(s) skipped (access denied)." if dirs_skipped
               else "."))
    if partial:
        note += (" Partial scan — time budget reached; results may be "
                 "incomplete.")
    return out, note


# ---------------------------------------------------------------------------
# SMART disk health via smartctl (smartmontools). Optional external tool;
# administrative privileges are typically required and answers degrade to
# UNAVAILABLE rather than being invented.
# ---------------------------------------------------------------------------
SMARTCTL_TIMEOUT = 8


@dataclass
class SmartDevice:
    device: str = ""
    model: str = ""
    status: str = "unavailable"   # PASS | FAIL | UNAVAILABLE
    detail: str = ""


@dataclass
class SmartStatus:
    supported: bool = False
    tool_found: bool = False
    devices: list = field(default_factory=list)
    message: str = "Unavailable"


def smart_scan_argv(exe: str) -> list[str]:
    return [exe, "--scan"]


def smart_health_argv(exe: str, device: str) -> list[str]:
    return [exe, "--health", device]


def classify_smart_exit(code: int) -> str:
    """Map a smartctl exit bitfield to PASS/FAIL/UNAVAILABLE (documented).

    smartctl uses a bitmask: bit 3 (value 8) means "SMART health FAILED";
    combinations mean e.g. "device open failed" (bit 1) or "some smart
    command failed" (bit 2) — those must NOT be reported as a failing disk,
    only as an unavailable verdict.
    """
    try:
        code = int(code)
    except (TypeError, ValueError):
        return "UNAVAILABLE"
    if code & 8:
        return "FAIL"
    if code == 0:
        return "PASS"
    return "UNAVAILABLE"


def query_smart_devices(timeout: int = SMARTCTL_TIMEOUT,
                        max_devices: int = 8) -> SmartStatus:
    """Ask smartctl for per-device health. Never raises; honest degradation."""
    status = SmartStatus()
    exe = which("smartctl")
    if not exe:
        status.message = ("smartctl not found — install smartmontools for "
                          "disk-health checks (optional).")
        log.info("smartctl not found")
        return status
    status.tool_found = True
    try:
        proc = subprocess.run(smart_scan_argv(exe), shell=False,
                              capture_output=True, text=True,
                              timeout=timeout)
    except FileNotFoundError:
        status.message = "smartctl executable disappeared."
        return status
    except subprocess.TimeoutExpired:
        status.message = f"smartctl --scan timed out after {timeout}s."
        log.warning("smartctl scan timeout")
        return status
    except OSError as exc:
        status.message = f"Could not run smartctl: {exc}"
        log.warning("smartctl OSError: %s", exc)
        return status
    if proc.returncode != 0 and not (proc.stdout or "").strip():
        err = (proc.stderr or "").strip()[:300]
        status.message = f"smartctl --scan failed (exit {proc.returncode}): {err or 'no details'}"
        log.warning("smartctl scan exit %s: %s", proc.returncode, err)
        return status

    devices: list[str] = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        token = next((tok for tok in line.split()
                      if tok.startswith("/dev/")), None)
        if token is None:
            parts = line.split()
            if len(parts) >= 3 and parts[0].lstrip("#").isdigit():
                token = parts[2]
        if token and token not in devices:
            devices.append(token)
        if len(devices) >= max_devices:
            break
    if not devices:
        status.message = "smartctl reported no devices."
        return status

    verdicts: list[SmartDevice] = []
    for device in devices[:max_devices]:
        entry = SmartDevice(device=device)
        try:
            health = subprocess.run(smart_health_argv(exe, device),
                                    shell=False, capture_output=True,
                                    text=True, timeout=timeout)
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            entry.status = "UNAVAILABLE"
            entry.detail = f"health query timed out: {exc}" \
                if isinstance(exc, subprocess.TimeoutExpired) \
                else "health query failed: smartctl disappeared"
        except OSError as exc:
            entry.status = "UNAVAILABLE"
            entry.detail = f"health query failed: {exc}"
        except Exception as exc:  # noqa: BLE001 - one disk must never stop others
            entry.status = "UNAVAILABLE"
            entry.detail = f"health query failed: {exc}"
        else:
            entry.status = classify_smart_exit(health.returncode)
            text = (health.stdout or "")
            for line in text.splitlines():
                if "Model" in line and ":" in line:
                    entry.model = line.split(":", 1)[1].strip()[:80]
                    break
            detail = (health.stderr or "").strip() or text.strip()
            if detail:
                entry.detail = detail.splitlines()[0][:200]
        verdicts.append(entry)
    status.devices = verdicts
    passes = sum(1 for d in verdicts if d.status == "PASS")
    fails = sum(1 for d in verdicts if d.status == "FAIL")
    status.supported = True
    status.message = (f"{len(verdicts)} device(s) checked: "
                      f"{passes} PASS / {fails} FAIL.")
    return status


# ---------------------------------------------------------------------------
# Per-process network connections (read-only snapshot via psutil).
# ---------------------------------------------------------------------------
@dataclass
class ConnRow:
    kind: str = ""      # TCP | UDP | literal socket-type fallback
    local: str = ""     # ip:port
    remote: str = ""    # ip:port or "-"
    status: str = ""
    pid: int = 0
    process: str = ""


def _format_conn_addr(addr) -> str:
    """Format a psutil address tuple like ip:port; IPv6 gets brackets."""
    if not addr or len(addr) < 2:
        return "-"
    ip, port = addr[0], addr[1]
    if ip is None or port is None:
        return "-"
    ip = str(ip)
    port = int(port) if str(port).isdigit() else port
    if ":" in ip:  # IPv6 literal
        return f"[{ip}]:{port}"
    return f"{ip}:{port}"


def collect_net_connections(limit: int = 200) -> tuple[list[ConnRow], str]:
    """List INET connections with owning process names. Read-only.

    Access-denied (tight privileges) and psutil-missing degrade to a message.
    """
    if not HAS_PSUTIL:
        return [], "psutil not installed — connection list unavailable."
    try:
        import psutil
    except ImportError:
        return [], "psutil not installed — connection list unavailable."
    try:
        conns = psutil.net_connections(kind="inet")
    except psutil.AccessDenied:
        return [], "The connection table requires elevated privileges on this system."
    except Exception as exc:  # noqa: BLE001 - degrade, never raise
        return [], f"Connection query failed: {exc}"

    names: dict[int, str] = {}

    def _process_name(pid: int) -> str:
        if pid in names:
            return names[pid]
        name = ""
        try:
            name = psutil.Process(pid).name()
        except Exception:  # noqa: BLE001 - gone or private; leave blank
            pass
        names[pid] = name
        return name

    import socket
    type_names = {socket.SOCK_STREAM: "TCP", socket.SOCK_DGRAM: "UDP"}

    def _order(row: ConnRow) -> tuple:
        if row.kind == "TCP" and row.status == "ESTABLISHED":
            return (0, row.pid)
        if row.kind == "TCP":
            return (1, row.pid)
        return (2, row.pid)

    rows: list[ConnRow] = []
    for c in conns:
        conn_kind = type_names.get(getattr(c, "type", None), str(getattr(c, "type", "")))
        rows.append(ConnRow(
            kind=conn_kind,
            local=_format_conn_addr(getattr(c, "laddr", None)),
            remote=_format_conn_addr(getattr(c, "raddr", None)),
            status=str(getattr(c, "status", "") or "-"),
            pid=int(getattr(c, "pid", 0) or 0),
        ))
    for row in rows:
        if row.pid > 0:
            row.process = _process_name(row.pid)
    rows.sort(key=_order)
    total = len(rows)
    if total == 0:
        return [], "No connections reported."
    capped = rows[:max(1, limit)]
    note = (f"{total} connection(s); showing first {len(capped)}."
            if total > len(capped) else f"{total} connection(s).")
    return capped, note


# ---------------------------------------------------------------------------
# Version check (manual, metadata-only). Never downloads or installs anything.
# ---------------------------------------------------------------------------
APP_VERSION = "1.0.0"
DEFAULT_RELEASES_URL = ("https://api.github.com/repos/your-github-user/"
                        "smart-hardware-diagnostic-scanner/releases/latest")


@dataclass
class UpdateCheck:
    status: str = "unavailable"   # up_to_date | update_available | unavailable
    latest: str = ""
    current: str = APP_VERSION
    detail: str = ""


def parse_tag(tag: object) -> str:
    """Normalise a release tag ("v1.2.3" -> "1.2.3"); junk -> "". """
    if not isinstance(tag, str):
        return ""
    return tag.strip().lstrip("vV").strip()


def compare_versions(current: str, latest: str) -> int:
    """Compare dotted numeric versions; 0 when undecidable (non-numeric)."""
    try:
        cur_parts = [p for p in str(current).strip().split(".")]
        lat_parts = [p for p in str(latest).strip().split(".")]
        width = max(len(cur_parts), len(lat_parts))
        cur_parts += ["0"] * (width - len(cur_parts))
        lat_parts += ["0"] * (width - len(lat_parts))
        for c, l in zip(cur_parts, lat_parts):
            try:
                ci, li = int(c), int(l)
            except ValueError:
                return 0  # undecidable; never claim an update on junk
            if ci != li:
                return -1 if ci < li else 1
        return 0
    except Exception:  # noqa: BLE001 - comparison must never raise
        return 0


def check_for_update(url: str | None = None,
                     timeout: float = 4.0) -> UpdateCheck:
    """Fetch the latest release tag from GitHub. Never raises; no downloads."""
    import urllib.error
    import urllib.request
    target = (url or DEFAULT_RELEASES_URL).strip()
    result = UpdateCheck()
    if not target.lower().startswith(("http://", "https://")):
        result.detail = "Update URL must start with http(s)."
        return result
    request = urllib.request.Request(
        target, headers={"User-Agent": f"SmartHardwareDiagnosticScanner/{APP_VERSION}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(65536)  # a tag payload is tiny; cap the read
    except urllib.error.HTTPError as exc:
        result.detail = f"Update check failed: HTTP {exc.code}."
        return result
    except (urllib.error.URLError, OSError, ValueError) as exc:
        result.detail = f"No internet connection or GitHub unreachable ({exc})."
        return result
    except Exception as exc:  # noqa: BLE001
        result.detail = f"Update check failed: {exc}"
        return result
    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except (ValueError, AttributeError):
        result.detail = "Update response was not valid JSON."
        return result
    if not isinstance(payload, dict):
        result.detail = "Update response was not an object."
        return result
    latest = parse_tag(payload.get("tag_name"))
    if not latest:
        result.detail = "Release payload had no usable tag_name."
        return result
    result.latest = latest
    order = compare_versions(result.current, latest)
    if order < 0:
        result.status = "update_available"
        result.detail = f"Version {latest} is available upstream."
    else:
        result.status = "up_to_date"
        result.detail = ("Local version is newer (pre-release?)"
                         if order > 0 else f"You are running the latest release ({latest}).")
    return result


# ---------------------------------------------------------------------------
# Scheduled weekly scan (user-confirmed registration via schtasks/cron).
# These builders are pure; the caller owns the subprocess and confirmation.
# ---------------------------------------------------------------------------
SCHEDULE_MARKER = "# SHDS weekly scan (added by Smart Hardware Diagnostic Scanner)"
VALID_DAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
SCHTASKS_TASK_NAME = "SHDS weekly scan"


def validate_time(value: str) -> bool:
    """Strict "HH:MM" 24-hour validation (ASCII digits only).

    `str.isdigit()` is not enough: it accepts full-width and Arabic-Indic
    digits, and `int()` on a superscript like "²" raises ValueError. Both
    would break the schedule registration instead of being rejected.
    """
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        return False
    hh, mm = value[:2], value[3:]
    if not all(ch in "0123456789" for ch in hh + mm):
        return False
    return int(hh) <= 23 and int(mm) <= 59


def day_to_cron_dow(day: str) -> int:
    """MON..SUN -> cron day-of-week 1..6 and SUN=0. Raises for bad days."""
    mapping = {"MON": 1, "TUE": 2, "WED": 3, "THU": 4, "FRI": 5, "SAT": 6,
               "SUN": 0}
    key = str(day).strip().upper()
    if key not in mapping:
        # ValueError (not KeyError) so the callers' `except (OSError,
        # ValueError)` handlers actually catch it.
        raise ValueError(f"Day must be one of {', '.join(VALID_DAYS)}.")
    return mapping[key]


def scan_schedule_args() -> list[str]:
    """Shared headless-only flags for the scheduled command."""
    return ["--scan", "--quiet", "--no-history"]


def build_schtasks_argv(python_exe: str, script: str, day: str = "MON",
                        when: str = "09:00") -> list[str]:
    """schtasks argv for a weekly headless scan (run with shell=False)."""
    if day not in VALID_DAYS:
        raise ValueError(f"Day must be one of {', '.join(VALID_DAYS)}.")
    if not validate_time(when):
        raise ValueError("Time must be HH:MM (24-hour), e.g. 09:30.")
    quoted = f'"{python_exe}" "{script}"' + " " + " ".join(scan_schedule_args())
    return ["schtasks", "/Create", "/F", "/TN", SCHTASKS_TASK_NAME,
            "/SC", "WEEKLY", "/D", day, "/ST", when, "/TR", quoted]


def remove_schtasks_argv() -> list[str]:
    return ["schtasks", "/Delete", "/TN", SCHTASKS_TASK_NAME, "/F"]


def _cron_token(value: str) -> str:
    """One shell- and cron-safe token for an executable/script path.

    cron splits the command on whitespace, so a path containing a space must
    be quoted or the schedule silently breaks. Newlines are rejected: they
    would inject a second, unmarked crontab line that the removal logic could
    never clean up. '%' is escaped because cron turns it into a newline.
    """
    text = str(value)
    if "\n" in text or "\r" in text:
        raise ValueError("Path must not contain newlines.")
    return shlex.quote(text.replace("%", r"\%"))


def build_cron_line(python_exe: str, script: str, day: str = "MON",
                    when: str = "09:00") -> str:
    """Cron line for a weekly headless scan; marker at the end for removal."""
    if day not in VALID_DAYS:
        raise ValueError(f"Day must be one of {', '.join(VALID_DAYS)}.")
    if not validate_time(when):
        raise ValueError("Time must be HH:MM (24-hour), e.g. 09:30.")
    hh, mm = str(when).split(":")
    dow = day_to_cron_dow(day)
    return (f"{int(mm)} {int(hh)} * * {dow} {_cron_token(python_exe)} "
            + f"{_cron_token(script)} "
            + " ".join(scan_schedule_args()) + f" {SCHEDULE_MARKER}")


def strip_scheduled_entries(text: str) -> tuple[str, int]:
    """Remove marker-bearing lines; keep everything else and line endings."""
    if not text:
        return "", 0
    kept: list[str] = []
    removed = 0
    # Split on "\n" (not splitlines) so CRLF line endings of unrelated user
    # jobs are preserved byte-for-byte when the crontab is rewritten.
    for line in text.split("\n"):
        if SCHEDULE_MARKER in line:
            removed += 1
            continue
        kept.append(line)
    if removed:
        while kept and not kept[-1].strip():
            kept.pop()
    return ("\n".join(kept), removed)


def merge_crontab(existing: "str | None") -> str:
    """Existing crontab -> text minus old SHDS lines (caller appends new ones)."""
    if not existing:
        return ""
    return strip_scheduled_entries(existing)[0]


# ---------------------------------------------------------------------------
# Scan history (SQLite)
# ---------------------------------------------------------------------------
HISTORY_FILENAME = "history.db"


@dataclass
class HistoryEntry:
    ts: str
    score: int
    cpu: float | None = None
    mem: float | None = None
    disk: float | None = None
    fails: int = 0
    warnings: int = 0


def worst_disk_percent(payload: dict) -> float | None:
    """Highest used % across accessible volumes, or None."""
    values = []
    for d in (payload.get("hardware", {}) or {}).get("disks", []) or []:
        if d.get("accessible") and d.get("percent") is not None:
            try:
                values.append(float(d["percent"]))
            except (TypeError, ValueError):
                continue
    return max(values) if values else None


class HistoryStore:
    """Tiny SQLite store of scan summaries for the trend view.

    Only touched from the Tk main thread (right after a scan finishes), so no
    cross-thread sharing. Every method is failure tolerant: a read-only or
    corrupt database degrades to unavailable rather than breaking the app.
    """

    def __init__(self, path: Path | None = None, max_rows: int = 500) -> None:
        self.path = Path(path) if path else (_user_data_root() / HISTORY_FILENAME)
        self.max_rows = max(20, int(max_rows))
        self.available = False
        self.error = ""
        self._conn: sqlite3.Connection | None = None
        # Open eagerly so callers can inspect `available`/`error` immediately.
        self.open()

    def open(self) -> bool:
        if self._conn is not None:
            return self.available
        conn: sqlite3.Connection | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path))
            conn.execute(
                "CREATE TABLE IF NOT EXISTS scans ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,"
                " score INTEGER, cpu REAL, mem REAL, disk REAL,"
                " fails INTEGER, warnings INTEGER)")
            conn.commit()
            self._conn = conn
            self.available = True
        except (sqlite3.Error, OSError) as exc:
            self.error = str(exc)
            # sqlite3.connect may have succeeded before the schema step failed
            # (e.g. a corrupt or foreign file); close it or it leaks.
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            self._conn = None
            self.available = False
            log.warning("history store unavailable: %s", exc)
        return self.available

    def record(self, payload: dict) -> bool:
        """Append one scan summary; trims the oldest rows beyond max_rows."""
        if not self.open() or self._conn is None:
            return False
        summary = payload.get("summary", {}) or {}
        try:
            self._conn.execute(
                "INSERT INTO scans (ts, score, cpu, mem, disk, fails, warnings)"
                " VALUES (?,?,?,?,?,?,?)",
                (
                    str(payload.get("timestamp", "")),
                    int(payload.get("health_score", 0) or 0),
                    _num_or_none((payload.get("hardware", {}) or {}).get("cpu", {}).get("percent")),
                    _num_or_none((payload.get("hardware", {}) or {}).get("memory", {}).get("percent")),
                    worst_disk_percent(payload),
                    int(summary.get("FAIL", 0) or 0),
                    int(summary.get("WARNING", 0) or 0),
                ))
            self._conn.commit()
            self._conn.execute(
                "DELETE FROM scans WHERE id NOT IN ("
                " SELECT id FROM scans ORDER BY id DESC LIMIT ?)", (self.max_rows,))
            self._conn.commit()
            return True
        except (sqlite3.Error, OSError, TypeError, ValueError) as exc:
            self.error = str(exc)
            log.warning("history record failed: %s", exc)
            return False

    def recent(self, limit: int = 60) -> list[HistoryEntry]:
        """Most recent `limit` entries, oldest first (chart-ready order)."""
        if not self.open() or self._conn is None:
            return []
        try:
            rows = self._conn.execute(
                "SELECT ts, score, cpu, mem, disk, fails, warnings FROM scans"
                " ORDER BY id DESC LIMIT ?", (max(1, int(limit)),)).fetchall()
        except (sqlite3.Error, OSError) as exc:
            log.warning("history read failed: %s", exc)
            return []
        entries = [HistoryEntry(ts=r[0], score=int(r[1] or 0), cpu=r[2], mem=r[3],
                                disk=r[4], fails=int(r[5] or 0), warnings=int(r[6] or 0))
                   for r in rows]
        entries.reverse()
        return entries

    def count(self) -> int:
        if not self.open() or self._conn is None:
            return 0
        try:
            return int(self._conn.execute("SELECT COUNT(*) FROM scans").fetchone()[0])
        except (sqlite3.Error, OSError, TypeError):
            return 0

    def clear(self) -> bool:
        if not self.open() or self._conn is None:
            return False
        try:
            self._conn.execute("DELETE FROM scans")
            self._conn.commit()
            return True
        except (sqlite3.Error, OSError) as exc:
            log.warning("history clear failed: %s", exc)
            return False

    def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        self.available = False


def _num_or_none(value) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


# ---------------------------------------------------------------------------
# Desktop notifications (optional)
# ---------------------------------------------------------------------------
def send_toast(title: str, message: str) -> bool:
    """Best-effort native toast. Returns True only if one was actually shown.

    Absence of plyer (or a platform that refuses toasts) returns False and the
    caller falls back to the in-app banner, so alerts are never lost silently.
    """
    if not HAS_PLYER or _plyer_notification is None:
        return False
    try:
        _plyer_notification.notify(title=title, message=message,
                                   app_name=APP_NAME, timeout=8)
        return True
    except Exception as exc:
        log.debug("toast failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
NAV_ITEMS = ["Dashboard", "Processes", "Connections", "Startup", "Diagnostics",
             "History", "Reports & Tools"]

# Seconds between repeat alerts for the same component, so a sustained problem
# notifies once rather than every refresh tick.
ALERT_COOLDOWN_S = 120.0

SAFE_TEMP_SUFFIXES = {".tmp", ".log", ".bak", ".cache", ".dmp", ".old"}


def _status_of_pct(value: float | None, warn: float, fail: float) -> str:
    if value is None:
        return "UNAVAILABLE"
    if value >= fail:
        return "FAIL"
    if value >= warn:
        return "WARNING"
    return "PASS"


def approved_temp_root() -> Path:
    return Path(tempfile.gettempdir()).resolve()


def preview_temp_candidates(limit: int = 200) -> tuple[list[Path], str]:
    """List safe top-level temp files (non-recursive, allow-listed suffixes only)."""
    root = approved_temp_root()
    try:
        entries = list(root.iterdir())
    except (PermissionError, OSError) as exc:
        return [], f"Cannot read temp dir: {exc}"
    out: list[Path] = []
    for p in entries:
        if len(out) >= limit:
            break
        try:
            if p.is_symlink() or not p.is_file():
                continue
            if p.suffix.lower() not in SAFE_TEMP_SUFFIXES:
                continue
            # validate containment
            p.resolve().relative_to(root)
            out.append(p)
        except (OSError, ValueError):
            continue
    return out, f"Scanned {root} (top level only, safe suffixes)."


class ScannerApp(tk.Tk):
    """Main application window."""

    def __init__(self) -> None:
        super().__init__()
        self.title("Smart Hardware Diagnostic Scanner — Dark Neon Edition  |  by Devansh")
        self.minsize(960, 620)
        self.configure(bg=BG_MAIN)

        # --- persisted settings (config.json in the per-user data dir) ---
        # NOTE: stored as `self.settings`, not `self.config`, because `config`
        # is a Tkinter widget method (alias of `configure`) and assigning to it
        # would shadow that API on the whole window.
        self.settings: dict = load_config()
        stored = sanitise_thresholds(self.settings.get("thresholds"))
        if self.validate_thresholds(stored) is not None:
            log.warning("stored thresholds were inconsistent; using defaults")
            stored = dict(DEFAULT_THRESHOLDS)
        self.thresholds: dict = stored
        geometry = safe_geometry(self.settings.get("geometry"))
        try:
            self.geometry(geometry)
        except tk.TclError:
            # Never let a bad stored geometry stop the app from starting.
            log.warning("stored geometry %r was rejected; using the default",
                        self.settings.get("geometry"))
            self.geometry("1180x760")

        # Restore the saved theme before any widget is built, so the UI is
        # never constructed twice. Only "light"/"dark" are accepted.
        self._theme_mode = ("light" if str(self.settings.get("theme", "dark"))
                            == "light" else "dark")
        set_theme_mode(self._theme_mode)
        self.configure(bg=BG_MAIN)

        self.monitor = MonitorWorker(interval_s=1.0, include_gpu=False)
        self.latest_snap: HardwareSnapshot | None = None
        self.latest_diag: DiagnosticReport | None = None
        self.previous_diag: DiagnosticReport | None = None
        self.previous_payload: dict | None = None
        self.latest_payload: dict | None = None
        self.last_comparison: dict | None = None
        self.cpu_hist: list[float] = []
        self.mem_hist: list[float] = []
        self.refresh_interval = tk.DoubleVar(value=1.0)
        self.proc_sort = tk.StringVar(value="cpu")
        self.alerts_enabled = tk.BooleanVar(value=bool(self.settings.get("alerts", True)))
        self.monitor_running = False

        self.history = HistoryStore()
        self.history_page_entries: list[HistoryEntry] = []
        self._alert_last: dict[str, float] = {}
        self._detail_windows: dict[int, tk.Toplevel] = {}
        self._detail_texts: dict[int, tk.Text] = {}

        self._bg_queue: queue.Queue = queue.Queue()
        self._scan_active = False
        self._refresh_active = False
        self._procs_active = False
        self._monitor_polling = False
        self._after_ids: set[str] = set()

        self._build_style()
        self._build_layout()
        self._bind_shortcuts()
        self.show_page("Dashboard")
        self._log("Application started. Optional deps: "
                  f"psutil={'yes' if HAS_PSUTIL else 'NO — metrics unavailable'} / "
                  f"matplotlib={'yes' if HAS_MPL else 'NO — charts disabled'} / "
                  f"notifications={'plyer' if HAS_PLYER else 'in-app only'}.", "INFO")
        if self.history.available:
            count = self.history.count()
            self._log(f"Scan history ready ({count} record(s)) at {self.history.path}", "INFO")
        else:
            self._log(f"Scan history unavailable: {self.history.error}", "WARNING")
        if not HAS_PSUTIL:
            messagebox.showwarning(
                "Optional dependency missing",
                "psutil is not installed.\nThe app will launch, but live metrics "
                "are unavailable.\nInstall with: pip install psutil")
        self._reschedule(250, self._poll_bg_queue)
        self._reschedule(300, self.refresh_once)
        self.protocol("WM_DELETE_WINDOW", self.on_closing)

    def _bind_shortcuts(self) -> None:
        """Keyboard shortcuts: F5 refresh, Ctrl+R scan, Ctrl+H history, Ctrl+S save."""
        self.bind("<F5>", lambda _e: self.refresh_once())
        self.bind("<Control-r>", lambda _e: self.run_scan())
        self.bind("<Control-h>", lambda _e: self.show_page("History"))
        self.bind("<Control-s>", lambda _e: self.export_report("json"))
        self.bind("<Control-d>", lambda _e: self.show_page("Dashboard"))

    # ---------- styling / layout ----------
    def _build_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background=BG_MAIN)
        style.configure("Card.TFrame", background=BG_CARD, relief="flat")
        style.configure("Side.TFrame", background=BG_SIDEBAR)
        style.configure("TLabel", background=BG_MAIN, foreground=FG_PRIMARY,
                        font=FONT_BODY)
        style.configure("Card.TLabel", background=BG_CARD, foreground=FG_PRIMARY)
        style.configure("Muted.TLabel", background=BG_CARD, foreground=FG_MUTED,
                        font=FONT_SMALL)
        style.configure("Title.TLabel", background=BG_MAIN, foreground=FG_PRIMARY,
                        font=FONT_TITLE)
        style.configure("Head.TLabel", background=BG_MAIN, foreground=NEON_CYAN,
                        font=FONT_HEADING)
        style.configure("Side.TButton", background=BG_SIDEBAR, foreground=FG_PRIMARY,
                        font=FONT_BODY, borderwidth=0, padding=(14, 10), anchor="w")
        style.map("Side.TButton", background=[("active", BG_CARD)],
                  foreground=[("active", NEON_CYAN)])
        style.configure("Accent.TButton", background=NEON_CYAN, foreground="#0A0A12",
                        font=FONT_BODY, borderwidth=0, padding=(12, 8))
        style.map("Accent.TButton", background=[("active", "#4DF0FF")])
        style.configure("Ghost.TButton", background=BG_PANEL, foreground=FG_PRIMARY,
                        borderwidth=1, padding=(10, 7))
        style.configure("TProgressbar", background=NEON_CYAN, troughcolor=PROGRESS_BG,
                        bordercolor=BORDER, thickness=10)
        style.configure("Treeview", background=BG_PANEL, fieldbackground=BG_PANEL,
                        foreground=FG_PRIMARY, rowheight=24, font=FONT_BODY)
        style.configure("Treeview.Heading", background=BG_SIDEBAR,
                        foreground=NEON_CYAN, font=FONT_SMALL)
        style.configure("TSpinbox", fieldbackground=ENTRY_BG, background=ENTRY_BG,
                        foreground=FG_PRIMARY)
        style.configure("TEntry", fieldbackground=ENTRY_BG, foreground=FG_PRIMARY)

    def _build_layout(self) -> None:
        # Sidebar
        self.sidebar = ttk.Frame(self, style="Side.TFrame", width=210)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)
        brand = tk.Label(self.sidebar, text="◈  SHDS", bg=BG_SIDEBAR,
                         fg=NEON_CYAN, font=(FONT_FAMILY, 15, "bold"))
        brand.pack(pady=(18, 2), padx=12, anchor="w")
        sub = tk.Label(self.sidebar, text="Dark Neon Edition\nby Devansh",
                       bg=BG_SIDEBAR, fg=FG_MUTED, font=FONT_SMALL, justify="left")
        sub.pack(padx=12, anchor="w", pady=(0, 16))
        self.nav_buttons: dict[str, ttk.Button] = {}
        for item in NAV_ITEMS:
            btn = ttk.Button(self.sidebar, text=f"  {item}", style="Side.TButton",
                             command=lambda n=item: self.show_page(n))
            btn.pack(fill="x", padx=8, pady=2)
            self.nav_buttons[item] = btn
        tk.Label(self.sidebar, text="Refresh interval (s):", bg=BG_SIDEBAR,
                 fg=FG_MUTED, font=FONT_SMALL).pack(padx=12, pady=(18, 2), anchor="w")
        intervals = ttk.Combobox(self.sidebar, textvariable=self.refresh_interval,
                                 values=["0.5", "1.0", "2.0", "5.0"], width=8,
                                 state="readonly")
        intervals.pack(padx=12, anchor="w")
        intervals.bind("<<ComboboxSelected>>", lambda _e: self._apply_interval())
        self.mon_btn = ttk.Button(self.sidebar, text="▶  Start monitoring",
                                  style="Accent.TButton", command=self.toggle_monitoring)
        self.mon_btn.pack(fill="x", padx=12, pady=(16, 6))
        ttk.Checkbutton(self.sidebar, text="Alert me on problems",
                        variable=self.alerts_enabled,
                        command=self._on_alerts_toggled).pack(padx=12, anchor="w")
        self.theme_btn = ttk.Button(
            self.sidebar,
            text="◐  Switch to light" if get_theme_mode() == "dark"
                 else "◐  Switch to dark",
            style="Ghost.TButton", command=self._toggle_theme)
        self.theme_btn.pack(fill="x", padx=12, pady=(8, 0))
        self._update_btn = ttk.Button(
            self.sidebar, text="⇩  Check for updates", style="Ghost.TButton",
            command=self.check_for_updates)
        self._update_btn.pack(fill="x", padx=12, pady=(6, 0))
        tk.Label(self.sidebar, text="F5 refresh · Ctrl+R scan\nCtrl+H history · Ctrl+S save",
                 bg=BG_SIDEBAR, fg=FG_DIM, font=FONT_SMALL,
                 justify="left").pack(padx=12, pady=(14, 0), anchor="w")

        # Main column
        main_col = ttk.Frame(self)
        main_col.pack(side="left", fill="both", expand=True)
        header = ttk.Frame(main_col)
        header.pack(fill="x", padx=18, pady=(14, 6))
        self.page_title = ttk.Label(header, text="Hardware Dashboard",
                                    style="Title.TLabel")
        self.page_title.pack(side="left")
        self.head_status = tk.Label(header, text="● idle", bg=BG_MAIN,
                                    fg=FG_MUTED, font=FONT_SMALL)
        self.head_status.pack(side="right")
        self.health_chip = tk.Label(header, text="Health —", bg=BG_MAIN,
                                    fg=FG_MUTED, font=FONT_SMALL)
        self.health_chip.pack(side="right", padx=(0, 14))
        # Transient in-app banner: the guaranteed fallback when a native toast
        # is unavailable or disabled.
        self.alert_banner = tk.Label(header, text="", bg=BG_MAIN, fg=NEON_RED,
                                    font=FONT_SMALL)
        self.alert_banner.pack(side="right", padx=(0, 12))

        self.pages_frame = ttk.Frame(main_col)
        self.pages_frame.pack(fill="both", expand=True, padx=18, pady=6)
        self.pages: dict[str, ttk.Frame] = {}
        for name in NAV_ITEMS:
            frame = ttk.Frame(self.pages_frame)
            frame.place(relx=0, rely=0, relwidth=1, relheight=1)
            self.pages[name] = frame
        self._build_dashboard(self.pages["Dashboard"])
        self._build_processes(self.pages["Processes"])
        self._build_connections(self.pages["Connections"])
        self._build_startup(self.pages["Startup"])
        self._build_diagnostics(self.pages["Diagnostics"])
        self._build_history(self.pages["History"])
        self._build_reports(self.pages["Reports & Tools"])

        # Status bar
        self.status_var = tk.StringVar(value="Ready.")
        self.scan_var = tk.StringVar(value="Last scan: never")
        self.health_var = tk.StringVar(value="Health: —")
        self.uptime_var = tk.StringVar(value="Uptime: —")
        self.battery_var = tk.StringVar(value="Battery: —")
        bar = tk.Frame(self, bg=BG_STATUSBAR, height=28)
        bar.pack(side="bottom", fill="x")
        tk.Label(bar, textvariable=self.status_var, bg=BG_STATUSBAR,
                 fg=FG_MUTED, font=FONT_SMALL).pack(side="left", padx=12)
        for var, colour in ((self.scan_var, NEON_PURPLE),
                            (self.health_var, NEON_CYAN),
                            (self.battery_var, NEON_GREEN),
                            (self.uptime_var, FG_MUTED)):
            tk.Label(bar, textvariable=var, bg=BG_STATUSBAR,
                     fg=colour, font=FONT_SMALL).pack(side="right", padx=12)

    # ---------- pages ----------
    def _card(self, parent, title: str, accent: str, bar_kind: str = "progress"):
        card = ttk.Frame(parent, style="Card.TFrame", width=CARD_MIN_WIDTH,
                         height=CARD_HEIGHT)
        card.pack_propagate(False)
        card.rowconfigure(2, weight=1)
        card.columnconfigure(0, weight=1)
        top = tk.Frame(card, bg=BG_CARD)
        top.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 0))
        tk.Label(top, text=title, bg=BG_CARD, fg=accent,
                 font=FONT_HEADING).pack(side="left")
        dot = tk.Label(top, text="●", bg=BG_CARD, fg=FG_DIM, font=FONT_BODY)
        dot.pack(side="right")
        val = tk.Label(card, text="—", bg=BG_CARD, fg=FG_PRIMARY,
                       font=(FONT_FAMILY, 15, "bold"), anchor="w")
        val.grid(row=1, column=0, sticky="ew", padx=12, pady=(4, 0))
        if bar_kind == "cores":
            # Per-core mini bars (CPU card only), drawn on demand.
            bar = tk.Canvas(card, height=18, bg=BG_CARD, highlightthickness=0)
        else:
            bar = ttk.Progressbar(card, mode="determinate", maximum=100)
        bar.grid(row=2, column=0, sticky="ew", padx=12, pady=(6, 0))
        detail = tk.Label(card, text="", bg=BG_CARD, fg=FG_MUTED,
                          font=FONT_SMALL, wraplength=CARD_MIN_WIDTH - 36,
                          justify="left", anchor="nw")
        detail.grid(row=3, column=0, sticky="ew", padx=12, pady=(4, 8))
        return card, val, bar, detail, dot

    def _draw_cores(self, canvas: tk.Canvas, per_core: list[float]) -> None:
        """Draw one thin vertical bar per logical core (0-100 %)."""
        try:
            canvas.delete("all")
            width = max(1, canvas.winfo_width())
            height = int(canvas.winfo_height() or 18)
            cores = [c for c in per_core if isinstance(c, (int, float))]
            if not cores:
                canvas.create_text(2, height // 2, anchor="w", text="per-core n/a",
                                   fill=FG_DIM, font=FONT_SMALL)
                return
            gap = 2
            bar_w = max(2, (width - gap * (len(cores) - 1)) / len(cores))
            for i, value in enumerate(cores):
                pct_val = clamp(float(value), 0.0, 100.0)
                x0 = i * (bar_w + gap)
                x1 = x0 + bar_w
                h = max(2.0, height * pct_val / 100.0)
                colour = (NEON_RED if pct_val >= 90 else
                          NEON_YELLOW if pct_val >= 70 else NEON_CYAN)
                canvas.create_rectangle(x0, height - h, x1, height,
                                        fill=colour, outline="")
        except tk.TclError:
            pass

    def _build_dashboard(self, page) -> None:
        # Cards sit in a single-row grid whose column widths are recomputed
        # from the row's actual pixel width on every resize, so all five
        # cards stay visible at any window size (default 1180 and min 960,
        # and narrower as well). This replaces the fixed-width pack layout
        # that overflowed the window.
        self.cards_row = ttk.Frame(page)
        self.cards_row.pack(fill="x", pady=(0, 8))
        self._card_titles = [("CPU", NEON_CYAN), ("RAM", NEON_PURPLE),
                             ("GPU", NEON_GREEN), ("Disk", NEON_YELLOW),
                             ("Network", NEON_ORANGE)]
        self.cards: dict[str, tuple] = {}
        self._card_slots: list[tuple[ttk.Frame, int]] = []
        for col, (title, accent) in enumerate(self._card_titles):
            kind = "cores" if title == "CPU" else "progress"
            card, val, bar, detail, dot = self._card(self.cards_row, title, accent, kind)
            card.grid(row=0, column=col, sticky="nsew", padx=(0, 8))
            self.cards_row.columnconfigure(col, weight=1, minsize=0)
            self.cards[title] = (val, bar, detail, dot)
            self._card_slots.append((card, col))
        self.cores_canvas = self.cards["CPU"][1]
        # Rows must not grow vertically: cards keep CARD_HEIGHT, the chart
        # below absorbs the rest.
        self.cards_row.rowconfigure(0, weight=0)
        self.cards_row.bind("<Configure>", self._on_cards_resize)
        self._last_card_width = 0

        btn_row = ttk.Frame(page)
        btn_row.pack(fill="x", pady=(0, 8))
        ttk.Button(btn_row, text="⟳  Refresh now", style="Ghost.TButton",
                   command=self.refresh_once).pack(side="left", padx=(0, 8))
        ttk.Button(btn_row, text="♥  Run diagnostic scan", style="Accent.TButton",
                   command=self.run_scan).pack(side="left")
        ttk.Label(btn_row, text="Charts: CPU + RAM (live)" if HAS_MPL else
                  "Charts unavailable — matplotlib not installed.",
                  font=FONT_SMALL, foreground=FG_MUTED,
                  background=BG_MAIN).pack(side="right")
        chart_box = tk.Frame(page, bg=BG_CARD, highlightbackground=BORDER,
                             highlightthickness=1)
        chart_box.pack(fill="both", expand=True, pady=(0, 4))
        if HAS_MPL:
            self.chart = ChartPanel(chart_box, bg=BG_CARD, fg=FG_PRIMARY)
        else:
            tk.Label(chart_box, text="Install matplotlib for live charts:\n"
                     "pip install matplotlib", bg=BG_CARD, fg=FG_MUTED,
                     font=FONT_BODY, justify="center").pack(expand=True, pady=30)
            self.chart = None

    def _on_cards_resize(self, event) -> None:
        # The grid distributes the row width across the five equal-weight
        # columns; sync each card's requested width to its actual column width
        # (minus the inter-card padding) so details wrap correctly and cards
        # never force the row wider than the window.
        row_width = event.width
        n = len(self._card_slots)
        if n == 0 or row_width < CARD_MIN_WIDTH:
            return
        col_width = row_width // n
        target = max(60, col_width - 8)  # minus padx=(0, 8)
        if abs(target - self._last_card_width) < 1:
            return
        self._last_card_width = target
        wrap = max(48, target - 36)
        for card, _col in self._card_slots:
            try:
                card.configure(width=target)
            except tk.TclError:
                pass
            for child in card.winfo_children():
                if isinstance(child, tk.Label):
                    try:
                        child.configure(wraplength=wrap)
                    except tk.TclError:
                        pass

    def _build_processes(self, page) -> None:
        ttk.Label(page, text="Top processes (read-only — nothing is terminated)",
                  style="Head.TLabel").pack(anchor="w", pady=(0, 6))
        row = ttk.Frame(page)
        row.pack(fill="x", pady=(0, 6))
        ttk.Radiobutton(row, text="Sort by CPU", variable=self.proc_sort,
                        value="cpu").pack(side="left", padx=(0, 10))
        ttk.Radiobutton(row, text="Sort by memory", variable=self.proc_sort,
                        value="mem").pack(side="left")
        ttk.Button(row, text="View details", style="Ghost.TButton",
                   command=self.open_selected_process).pack(side="right", padx=(8, 0))
        ttk.Button(row, text="Refresh", style="Ghost.TButton",
                   command=self.refresh_processes).pack(side="right")
        ttk.Label(page, text="Double-click a row (or press Enter) for read-only details — "
                  "the app never terminates or modifies processes.",
                  style="Muted.TLabel").pack(anchor="w", pady=(0, 6))
        cols = ("pid", "name", "cpu", "mem")
        self.proc_tree = ttk.Treeview(page, columns=cols, show="headings", height=16)
        for c, w, label in [("pid", 90, "PID"), ("name", 420, "Name"),
                            ("cpu", 110, "CPU %"), ("mem", 130, "Mem MB")]:
            self.proc_tree.heading(c, text=label)
            self.proc_tree.column(c, width=w, anchor="w" if c == "name" else "center")
        vsb = ttk.Scrollbar(page, orient="vertical", command=self.proc_tree.yview)
        self.proc_tree.configure(yscrollcommand=vsb.set)
        self.proc_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")
        self.proc_tree.bind("<Double-1>", lambda _e: self.open_selected_process())
        self.proc_tree.bind("<Return>", lambda _e: self.open_selected_process())

    def _build_connections(self, page) -> None:
        ttk.Label(page, text="Active network connections (read-only)",
                  style="Head.TLabel").pack(anchor="w", pady=(0, 6))
        ttk.Label(page, text="A snapshot of INET connections with the owning "
                  "process where visible. On macOS elevated privileges may be "
                  "required; nothing is listed silently as blank.",
                  style="Muted.TLabel").pack(anchor="w", pady=(0, 6))
        row = ttk.Frame(page)
        row.pack(fill="x", pady=(0, 6))
        self.conn_note = tk.StringVar(value="Not loaded yet.")
        ttk.Label(row, textvariable=self.conn_note,
                  style="Muted.TLabel").pack(side="left", padx=(0, 8))
        ttk.Button(row, text="Refresh", style="Ghost.TButton",
                   command=self.refresh_connections).pack(side="right")
        cols = ("kind", "local", "remote", "status", "pid", "process")
        self.conn_tree = ttk.Treeview(page, columns=cols, show="headings", height=16)
        for c, w, label in [("kind", 70, "Type"), ("local", 220, "Local"),
                            ("remote", 220, "Remote"), ("status", 130, "Status"),
                            ("pid", 80, "PID"), ("process", 240, "Process")]:
            self.conn_tree.heading(c, text=label)
            self.conn_tree.column(c, width=w, anchor="w" if c in ("process", "status")
                                  else "center")
        vsb = ttk.Scrollbar(page, orient="vertical", command=self.conn_tree.yview)
        self.conn_tree.configure(yscrollcommand=vsb.set)
        self.conn_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")

    def refresh_connections(self) -> None:
        """Collect connections off-thread; rows land via the background queue."""
        threading.Thread(target=self._connections_job, name="shds-conn",
                         daemon=True).start()
        self.set_status("Collecting connections…")

    def _connections_job(self) -> None:
        try:
            rows, note = collect_net_connections(limit=200)
        except Exception as exc:  # noqa: BLE001 - degrade, never crash GUI
            log.warning("connection collect failed: %s", exc)
            self._bg_queue.put(("connections", ([], f"Connection query failed: {exc}")))
            return
        self._bg_queue.put(("connections", (rows, note)))

    def _render_connections(self, data) -> None:
        rows, note = data
        try:
            self.conn_tree.delete(*self.conn_tree.get_children())
            for row in rows:
                self.conn_tree.insert("", "end", values=(
                    row.kind, row.local, row.remote, row.status,
                    row.pid if row.pid else "—", row.process or "—"))
            self.conn_note.set(note)
        except tk.TclError:
            pass
        self.set_status(f"Connections: {note}")

    def _build_startup(self, page) -> None:
        ttk.Label(page, text="Startup entries (read-only — nothing is disabled)",
                  style="Head.TLabel").pack(anchor="w", pady=(0, 6))
        ttk.Label(page, text="Programs registered to start with your session: "
                  "Windows Run keys + Startup folder; Linux autostart files; "
                  "macOS LaunchAgents. Sorting is informational only.",
                  style="Muted.TLabel").pack(anchor="w", pady=(0, 6))
        row = ttk.Frame(page)
        row.pack(fill="x", pady=(0, 6))
        self.startup_note = tk.StringVar(value="Not loaded yet.")
        ttk.Label(row, textvariable=self.startup_note,
                  style="Muted.TLabel").pack(side="left", padx=(0, 8))
        ttk.Button(row, text="Refresh", style="Ghost.TButton",
                   command=self.refresh_startup).pack(side="right")
        cols = ("name", "kind", "source", "command", "note")
        self.startup_tree = ttk.Treeview(page, columns=cols, show="headings",
                                         height=16)
        for c, w, label in [("name", 180, "Name"), ("kind", 120, "Type"),
                            ("source", 260, "Source"), ("command", 320, "Command"),
                            ("note", 110, "Note")]:
            self.startup_tree.heading(c, text=label)
            self.startup_tree.column(c, width=w, anchor="w")
        vsb = ttk.Scrollbar(page, orient="vertical",
                            command=self.startup_tree.yview)
        self.startup_tree.configure(yscrollcommand=vsb.set)
        self.startup_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")

    def refresh_startup(self) -> None:
        threading.Thread(target=self._startup_job, name="shds-startup",
                         daemon=True).start()
        self.set_status("Collecting startup entries…")

    def _startup_job(self) -> None:
        try:
            entries, note = collect_startup_entries()
        except Exception as exc:  # noqa: BLE001 - degrade, never crash GUI
            log.warning("startup collect failed: %s", exc)
            self._bg_queue.put(("startup", ([], f"Startup enumeration failed: {exc}")))
            return
        self._bg_queue.put(("startup", (entries, note)))

    def _render_startup(self, data) -> None:
        entries, note = data
        try:
            self.startup_tree.delete(*self.startup_tree.get_children())
            for entry in entries:
                self.startup_tree.insert("", "end", values=(
                    entry.name or "—", entry.kind, entry.source,
                    entry.command or "—", entry.note))
            self.startup_note.set(note)
        except tk.TclError:
            pass
        self.set_status(f"Startup: {note}")

    def _selected_proc_row(self) -> tuple[int, str] | None:
        """PID + name of the selected process row, or None."""
        try:
            selection = self.proc_tree.selection()
            if not selection:
                return None
            values = self.proc_tree.item(selection[0], "values")
            if not values:
                return None
            return int(values[0]), str(values[1])
        except (tk.TclError, ValueError, IndexError):
            return None

    def open_selected_process(self) -> None:
        selected = self._selected_proc_row()
        if selected is None:
            messagebox.showinfo("No selection", "Select a process row first.")
            return
        pid, name = selected
        self.open_process_detail(pid, name)

    def open_process_detail(self, pid: int, name: str) -> None:
        """Open (or focus) a read-only detail window for one process."""
        existing = self._detail_windows.get(pid)
        if existing is not None:
            try:
                if existing.winfo_exists():
                    existing.lift()
                    return
            except tk.TclError:
                pass
            self._detail_windows.pop(pid, None)

        win = tk.Toplevel(self)
        win.title(f"Process details — {name} (PID {pid})")
        win.geometry("640x520")
        win.configure(bg=BG_MAIN)
        win.transient(self)
        tk.Label(win, text=f"{name}  ·  PID {pid}", bg=BG_MAIN, fg=NEON_CYAN,
                 font=FONT_HEADING).pack(anchor="w", padx=12, pady=(10, 0))
        tk.Label(win, text="Read-only. Metrics refresh once; nothing is changed.",
                 bg=BG_MAIN, fg=FG_MUTED, font=FONT_SMALL).pack(anchor="w", padx=12)
        box = tk.Frame(win, bg=BG_OUTPUT, highlightbackground=BORDER,
                       highlightthickness=1)
        box.pack(fill="both", expand=True, padx=12, pady=8)
        text = tk.Text(box, bg=BG_OUTPUT, fg=FG_PRIMARY, font=FONT_MONO,
                       wrap="word", state="disabled")
        sb = ttk.Scrollbar(box, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=sb.set)
        text.pack(side="left", fill="both", expand=True, padx=2, pady=2)
        sb.pack(side="left", fill="y")
        text.configure(state="normal")
        text.insert("end", "Loading…\n")
        text.configure(state="disabled")
        self._detail_windows[pid] = win
        self._detail_texts[pid] = text

        def _close() -> None:
            self._detail_windows.pop(pid, None)
            self._detail_texts.pop(pid, None)
            try:
                win.destroy()
            except tk.TclError:
                pass

        win.protocol("WM_DELETE_WINDOW", _close)
        ttk.Button(win, text="Close", style="Ghost.TButton",
                   command=_close).pack(anchor="e", padx=12, pady=(0, 10))
        # Collection runs off-thread (open_files/connections can be slow).
        threading.Thread(target=self._proc_detail_job, args=(pid, win),
                         name="shds-proc-detail", daemon=True).start()

    def _proc_detail_job(self, pid: int, win: tk.Toplevel) -> None:
        try:
            detail = collect_process_detail(pid)
        except Exception as exc:  # never leave the window stuck on "Loading…"
            log.warning("process detail failed: %s", exc)
            detail = {"pid": pid, "available": False,
                      "error": f"detail collection failed: {exc}",
                      "fields": {}, "children": [], "connections": [],
                      "open_files": [], "notes": []}
        self._bg_queue.put(("proc_detail", (pid, win, detail)))

    def _render_process_detail(self, pid: int, win: tk.Toplevel, detail: dict) -> None:
        """Fill the detail window (main thread). Widget may be gone — ignore then."""
        text = self._detail_texts.get(pid)
        if text is None:
            return
        try:
            if not win.winfo_exists():
                self._detail_windows.pop(pid, None)
                self._detail_texts.pop(pid, None)
                return
        except tk.TclError:
            return
        lines = []
        if not detail.get("available"):
            lines.append(f"Unavailable: {detail.get('error', 'unknown reason')}")
            lines.append("")
            lines.append("The process may have exited between the list refresh and "
                         "this lookup, or access was denied.")
        else:
            for label, value in (detail.get("fields") or {}).items():
                lines.append(f"{label:>22}: {value}")
            notes = detail.get("notes") or []
            if notes:
                lines.append("")
                lines.append(f"Restricted/limited: {', '.join(notes)}")
            children = detail.get("children") or []
            lines.append("")
            lines.append(f"Child processes ({len(children)}):")
            for child_pid, child_name in children or [("—", "none")]:
                lines.append(f"  {child_pid:>8}  {child_name}")
            conns = detail.get("connections") or []
            lines.append("")
            lines.append(f"Network connections ({len(conns)}):")
            lines.extend(f"  {c}" for c in conns) if conns else lines.append("  none")
            files = detail.get("open_files") or []
            lines.append("")
            lines.append(f"Open files ({len(files)}):")
            lines.extend(f"  {p}" for p in files) if files else lines.append("  none")
        try:
            text.configure(state="normal")
            text.delete("1.0", "end")
            text.insert("end", "\n".join(lines) + "\n")
            text.configure(state="disabled")
        except tk.TclError:
            pass

    def _build_diagnostics(self, page) -> None:
        ttk.Label(page, text="Diagnostic scan output", style="Head.TLabel").pack(anchor="w")
        row = ttk.Frame(page)
        row.pack(fill="x", pady=6)
        ttk.Button(row, text="♥  Run diagnostic scan", style="Accent.TButton",
                   command=self.run_scan).pack(side="left", padx=(0, 8))
        ttk.Button(row, text="Compare with last", style="Ghost.TButton",
                   command=self.show_comparison).pack(side="left", padx=(0, 8))
        ttk.Button(row, text="Save diff HTML", style="Ghost.TButton",
                   command=self.export_diff_html).pack(side="left", padx=(0, 8))
        ttk.Button(row, text="♥  Check SMART (smartctl)", style="Ghost.TButton",
                   command=self.check_smart).pack(side="left", padx=(0, 8))
        ttk.Button(row, text="Copy summary", style="Ghost.TButton",
                   command=self.copy_summary).pack(side="left", padx=(0, 8))
        ttk.Button(row, text="Clear", style="Ghost.TButton",
                   command=lambda: self.diag_text.configure(state="normal") or
                   self.diag_text.delete("1.0", "end") or
                   self.diag_text.configure(state="disabled")).pack(side="left")
        self.scan_bar = ttk.Progressbar(row, mode="indeterminate", length=200)
        self.scan_bar.pack(side="right")

        body = ttk.Frame(page)
        body.pack(fill="both", expand=True)
        gauge_box = tk.Frame(body, bg=BG_CARD, highlightbackground=BORDER,
                             highlightthickness=1, width=214, height=140)
        gauge_box.pack(side="left", fill="y", padx=(0, 8))
        gauge_box.pack_propagate(False)
        self.gauge = tk.Canvas(gauge_box, width=214, height=140, bg=BG_CARD,
                               highlightthickness=0)
        self.gauge.pack(fill="both", expand=True)
        self._draw_gauge(None)

        out_box = tk.Frame(body, bg=BG_OUTPUT, highlightbackground=BORDER,
                           highlightthickness=1)
        out_box.pack(side="left", fill="both", expand=True)
        self.diag_text = tk.Text(out_box, bg=BG_OUTPUT, fg=OUTPUT_FG,
                                 insertbackground=NEON_CYAN, font=FONT_MONO,
                                 wrap="word", state="disabled")
        sb = ttk.Scrollbar(out_box, orient="vertical", command=self.diag_text.yview)
        self.diag_text.configure(yscrollcommand=sb.set)
        self.diag_text.pack(side="left", fill="both", expand=True, padx=2, pady=2)
        sb.pack(side="left", fill="y")
        for tag, colour in STATUS_COLOURS.items():
            self.diag_text.tag_configure(tag, foreground=colour)

    def _draw_gauge(self, score: int | None) -> None:
        """Semicircular health gauge. `None` draws the empty/idle state."""
        try:
            c = self.gauge
            c.delete("all")
            w, h = int(c.winfo_reqwidth()), int(c.winfo_reqheight())
            pad = 18
            x0, y0, x1, y1 = pad, pad, w - pad, (h - pad) + 34
            c.create_arc(x0, y0, x1, y1, start=200, extent=-220, style="arc",
                         outline=PROGRESS_BG, width=10)
            if score is None:
                colour = FG_DIM
                text = "—"
                label = "no scan yet"
            else:
                colour = (NEON_GREEN if score >= 90 else
                          NEON_YELLOW if score >= 55 else NEON_RED)
                text = str(score)
                label = health_label(score)
                # Arc length is proportional to the score (negative = clockwise).
                c.create_arc(x0, y0, x1, y1, start=200, extent=-2.2 * score,
                             style="arc", outline=colour, width=10)
            c.create_text(w // 2, h // 2 - 6, text=text, fill=colour,
                          font=(FONT_FAMILY, 26, "bold"))
            c.create_text(w // 2, h // 2 + 22, text=label, fill=FG_MUTED,
                          font=FONT_SMALL)
            c.create_text(w // 2, 12, text="HEALTH", fill=NEON_CYAN,
                          font=FONT_SMALL)
        except tk.TclError:
            pass

    def copy_summary(self) -> None:
        """Copy a plain-text digest of the latest scan to the clipboard."""
        if not self.latest_diag:
            messagebox.showinfo("Nothing to copy", "Run a diagnostic scan first.")
            return
        lines = [self._summary_text()]
        text = "\n".join(lines)
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
            self.set_status("Summary copied to clipboard.")
            self._log("Scan summary copied to clipboard.", "INFO")
        except tk.TclError:
            messagebox.showerror("Clipboard", "Could not access the clipboard.")

    def _summary_text(self) -> str:
        diag = self.latest_diag
        assert diag is not None
        s = diag.summary
        when = datetime.fromtimestamp(diag.finished_at or time.time()).strftime(
            "%Y-%m-%d %H:%M:%S")
        lines = [
            "Smart Hardware Diagnostic Scanner — summary",
            f"When: {when}",
            f"Health score: {diag.health_score}/100 ({health_label(diag.health_score)})",
            f"PASS={s.get('PASS', 0)} WARNING={s.get('WARNING', 0)} "
            f"FAIL={s.get('FAIL', 0)} UNAVAILABLE={s.get('UNAVAILABLE', 0)}",
            "",
        ]
        for f in diag.findings:
            lines.append(f"[{f.status}] {f.component}: {f.message}")
        if diag.recommendations:
            lines.append("")
            lines.append("Recommendations:")
            lines += [f" - {r}" for r in diag.recommendations]
        if self.latest_snap is not None:
            lines.append("")
            lines.append(f"Uptime: {format_uptime(self.latest_snap.uptime_s)}")
        return "\n".join(lines)

    # ---------- diff HTML export ----------
    def export_diff_html(self) -> None:
        """Write a self-contained HTML diff of the last two scans."""
        if self.latest_payload is None:
            messagebox.showinfo("Nothing to diff", "Run a diagnostic scan first.")
            return
        if self.last_comparison is None or self.previous_diag is None:
            messagebox.showinfo(
                "Nothing to diff",
                "A diff needs two scans — run another scan, then try again.")
            return
        dest = filedialog.asksaveasfilename(
            defaultextension=".html", filetypes=[("HTML", "*.html")],
            initialfile=f"shds-diff-{datetime.now().strftime('%Y%m%d-%H%M%S')}.html")
        if not dest:
            return
        try:
            path = safe_path_for_report(dest)
            path.write_text(render_comparison_html(
                self.last_comparison, self.previous_payload,
                self.latest_payload), encoding="utf-8")
        except (ValueError, PermissionError, OSError) as exc:
            messagebox.showerror("Export failed", str(exc))
            return
        self.set_status(f"Diff report saved: {path}")
        self._log(f"Diff report saved: {path}", "PASS")
        messagebox.showinfo("Diff saved", f"Saved diff report to:\n{path}")

    # ---------- SMART disk health ----------
    def check_smart(self) -> None:
        """Query smartctl (optional tool) off-thread; results are logged."""
        self.set_status("Querying SMART health (smartctl)…")
        self._log("Checking SMART disk health (requires the optional "
                  "smartmontools tool; admin rights may be needed)…", "INFO")
        threading.Thread(target=self._smart_job, name="shds-smart",
                         daemon=True).start()

    def _smart_job(self) -> None:
        try:
            status = query_smart_devices()
        except Exception as exc:  # noqa: BLE001 - never crash the GUI
            log.warning("smart query failed: %s", exc)
            status = SmartStatus(message=f"SMART query failed: {exc}")
        self._bg_queue.put(("smart", status))

    def _render_smart(self, status: SmartStatus) -> None:
        self._log(f"[SMART] {status.message}", "INFO")
        for device in status.devices:
            tag = {"PASS": "PASS", "FAIL": "FAIL"}.get(device.status, "INFO")
            line = f"[SMART] {device.device}: {device.status}"
            if device.model:
                line += f" ({device.model})"
            self._log(line, tag)
            if device.detail:
                self._log(f"    detail: {device.detail}", "INFO")
        if not status.tool_found:
            self._log(status.message, "WARNING")
        self.set_status(f"SMART: {status.message}")

    # ---------- largest files (read-only) ----------
    def choose_big_files_folder(self) -> None:
        chosen = filedialog.askdirectory(title="Choose a folder to analyse")
        if chosen:
            self.bigfiles_var.set(chosen)

    def find_largest_files(self) -> None:
        folder = str(self.bigfiles_var.get()).strip()
        if not folder:
            messagebox.showinfo("No folder", "Choose a folder first.")
            return
        self.bigfiles_btn.state(["disabled"])
        try:
            threading.Thread(target=self._bigfiles_job, args=(folder,),
                             name="shds-bigfiles", daemon=True).start()
        except (RuntimeError, OSError) as exc:
            # Otherwise the button would stay disabled for the whole session.
            log.warning("largest-files thread failed to start: %s", exc)
            self.bigfiles_btn.state(["!disabled"])
            self.set_status("Largest-files scan unavailable: "
                            "a background thread could not be started.")
            return
        self.set_status("Scanning for largest files…")

    def _bigfiles_job(self, folder: str) -> None:
        try:
            files, note = largest_files(folder, limit=25, time_budget_s=20.0)
        except Exception as exc:  # noqa: BLE001 - degrade, never crash GUI
            log.warning("largest-files scan failed: %s", exc)
            self._bg_queue.put(("bigfiles", ([], f"Scan failed: {exc}", folder)))
            return
        self._bg_queue.put(("bigfiles", (files, note, folder)))

    def _render_bigfiles(self, data) -> None:
        files, note, folder = data
        self.bigfiles_btn.state(["!disabled"])
        try:
            self.bigfiles_list.configure(state="normal")
            self.bigfiles_list.delete("1.0", "end")
            self.bigfiles_list.insert("end", f"Folder: {folder}\n{note}\n\n"
                                      if note else "")
            if not files:
                self.bigfiles_list.insert("end", "No files found.\n")
            for item in files:
                self.bigfiles_list.insert(
                    "end", f"{format_bytes(item.size_bytes):>12}  {item.path}\n")
            self.bigfiles_list.configure(state="disabled")
        except tk.TclError:
            pass
        self.set_status(f"Largest files: {note}")

    # ---------- scheduled scans ----------
    def register_schedule(self) -> None:
        """Confirm, then register a weekly headless scan (schtasks/cron)."""
        day = str(self.schedule_day_var.get()).strip().upper()[:3]
        when = str(self.schedule_time_var.get()).strip()
        python_exe = sys.executable or "python"
        script = str(Path(__file__).resolve())
        if day not in VALID_DAYS:
            messagebox.showerror("Invalid day",
                                 f"Choose one of: {', '.join(VALID_DAYS)}.")
            return
        if not validate_time(when):
            messagebox.showerror("Invalid time",
                                 "Time must be HH:MM (24-hour), e.g. 09:30.")
            return
        if platform.system().lower() == "windows":
            argv = build_schtasks_argv(python_exe, script, day, when)
            shown = subprocess.list2cmdline(argv)
            summary = (f"Create Windows scheduled task \"{SCHTASKS_TASK_NAME}\" "
                       f"({day} {when}):\n{shown}\n\n"
                       f"It runs this scanner headlessly: "
                       f"{' '.join(scan_schedule_args())}")
        else:
            line = build_cron_line(python_exe, script, day, when)
            summary = (f"Add this line to your crontab (runs on {day}, {when}):\n"
                       f"\n    {line}\n\nOther cron jobs are preserved; "
                       "re-registering replaces the previous SHDS entry.")
        if not messagebox.askyesno("Confirm scheduled scan", summary
                                   + "\n\nRegister it now?"):
            return
        threading.Thread(target=self._schedule_job, args=(day, when),
                         name="shds-schedule", daemon=True).start()

    def _schedule_job(self, day: str, when: str) -> None:
        """Runs off-thread; communicates the outcome via the background queue."""
        python_exe = sys.executable
        script = str(Path(__file__).resolve())
        is_windows = platform.system().lower() == "windows"
        try:
            if is_windows:
                argv = build_schtasks_argv(python_exe, script, day, when)
                proc = subprocess.run(argv, shell=False, capture_output=True,
                                      text=True, timeout=8)
                if proc.returncode == 0:
                    self._bg_queue.put(("schedule", (True,
                        f"Windows task registered ({day} {when}).")))
                else:
                    err = (proc.stderr or proc.stdout or "").strip()[:200]
                    self._bg_queue.put(("schedule", (False,
                        f"schtasks failed (exit {proc.returncode}): {err}")))
            else:
                current = None
                try:
                    proc = subprocess.run(["crontab", "-l"], shell=False,
                                          capture_output=True, text=True,
                                          timeout=8)
                    if proc.returncode == 0:
                        current = proc.stdout
                    elif "no crontab" in (proc.stdout + proc.stderr).lower():
                        current = None  # normal for first-time users
                    else:
                        err = (proc.stderr or "").strip()[:200]
                        self._bg_queue.put(("schedule", (False,
                            f"Cannot read crontab: {err or 'access denied'}")))
                        return
                except FileNotFoundError:
                    self._bg_queue.put(("schedule", (False,
                        "crontab command not found on this system.")))
                    return
                crontab_text = merge_crontab(current)
                crontab_text = (crontab_text.rstrip("\n") + "\n"
                                if crontab_text else "")
                crontab_text += (build_cron_line(python_exe, script, day, when)
                                 + "\n")
                proc = subprocess.run(["crontab", "-"], shell=False,
                                      input=crontab_text, capture_output=True,
                                      text=True, timeout=8)
                if proc.returncode == 0:
                    self._bg_queue.put(("schedule", (True,
                        f"Cron entry registered ({day} {when}).")))
                else:
                    err = (proc.stderr or "").strip()[:200]
                    self._bg_queue.put(("schedule", (False,
                        f"crontab update failed (exit {proc.returncode}): {err}")))
        except subprocess.TimeoutExpired:
            self._bg_queue.put(("schedule", (False,
                "Scheduler command timed out after 8s.")))
        except (OSError, ValueError) as exc:
            self._bg_queue.put(("schedule", (False, f"Scheduler error: {exc}")))

    def remove_schedule(self) -> None:
        """Confirm, then remove the SHDS scheduled scan (never other jobs)."""
        is_windows = platform.system().lower() == "windows"
        if is_windows:
            summary = (f"Delete the Windows scheduled task "
                       f"\"{SCHTASKS_TASK_NAME}\"?\n\n"
                       "No other scheduled tasks are touched.")
        else:
            summary = ("Remove the marked SHDS line from your crontab?\n\n"
                       "Only lines tagged with the SHDS marker are removed;\n"
                       "every other cron job is preserved.")
        if not messagebox.askyesno("Remove scheduled scan", summary):
            return
        threading.Thread(target=self._remove_schedule_job,
                         name="shds-unschedule", daemon=True).start()

    def _remove_schedule_job(self) -> None:
        try:
            if platform.system().lower() == "windows":
                proc = subprocess.run(remove_schtasks_argv(), shell=False,
                                      capture_output=True, text=True, timeout=8)
                if proc.returncode == 0:
                    self._bg_queue.put(("schedule", (True,
                        "Windows scheduled task removed.")))
                else:
                    err = (proc.stderr or proc.stdout or "").strip()[:200]
                    self._bg_queue.put(("schedule", (False,
                        f"schtasks delete failed (exit {proc.returncode}): {err}"
                        " (task may not exist)")))
            else:
                proc = subprocess.run(["crontab", "-l"], shell=False,
                                      capture_output=True, text=True, timeout=8)
                if proc.returncode != 0:
                    self._bg_queue.put(("schedule", (False,
                        "No crontab exists — nothing to remove.")))
                    return
                new_text, removed = strip_scheduled_entries(proc.stdout)
                if removed == 0:
                    self._bg_queue.put(("schedule", (True,
                        "No SHDS cron line found — nothing removed.")))
                    return
                proc = subprocess.run(["crontab", "-"], shell=False,
                                      input=new_text, capture_output=True,
                                      text=True, timeout=8)
                if proc.returncode == 0:
                    self._bg_queue.put(("schedule", (True,
                        f"Removed {removed} SHDS cron line(s).")))
                else:
                    err = (proc.stderr or "").strip()[:200]
                    self._bg_queue.put(("schedule", (False,
                        f"crontab update failed (exit {proc.returncode}): {err}")))
        except subprocess.TimeoutExpired:
            self._bg_queue.put(("schedule", (False,
                "Scheduler command timed out after 8s.")))
        except (OSError, ValueError) as exc:
            self._bg_queue.put(("schedule", (False, f"Scheduler error: {exc}")))

    # ---------- version check ----------
    def check_for_updates(self) -> None:
        """Manual, metadata-only version check (never downloads anything)."""
        self._update_btn.state(["disabled"])
        self.set_status("Checking for updates…")
        threading.Thread(target=self._update_job, name="shds-update",
                         daemon=True).start()

    def _update_job(self) -> None:
        url = str(self.settings.get("update_url") or "").strip()
        try:
            result = check_for_update(url or None)
        except Exception as exc:  # noqa: BLE001 - degrade, never crash GUI
            log.warning("update check failed: %s", exc)
            result = UpdateCheck(detail=f"Update check failed: {exc}")
        self._bg_queue.put(("update", result))

    def _render_update_check(self, result: UpdateCheck) -> None:
        self._update_btn.state(["!disabled"])
        if result.status == "update_available":
            self._log(f"Update available: version {result.latest} "
                      f"(you have {result.current}). Download it manually from "
                      "the project's GitHub releases — nothing is auto-installed.",
                      "WARNING")
        elif result.status == "up_to_date":
            self._log(f"Update check: {result.detail}", "PASS")
        else:
            self._log(f"Update check unavailable: {result.detail}", "WARNING")
        self.set_status(f"Update check: {result.status.replace('_', ' ')}.")

    def _build_history(self, page) -> None:
        ttk.Label(page, text="Scan history & trends (stored locally)",
                  style="Head.TLabel").pack(anchor="w")
        row = ttk.Frame(page)
        row.pack(fill="x", pady=6)
        ttk.Button(row, text="⟳  Refresh", style="Ghost.TButton",
                   command=self.refresh_history).pack(side="left", padx=(0, 8))
        ttk.Button(row, text="Clear history", style="Ghost.TButton",
                   command=self.clear_history_action).pack(side="left", padx=(0, 8))
        ttk.Button(row, text="Save trend HTML", style="Ghost.TButton",
                   command=self.export_history_html).pack(side="left")
        self.history_note = tk.Label(row, text="", bg=BG_MAIN, fg=FG_MUTED,
                                     font=FONT_SMALL)
        self.history_note.pack(side="right")

        chart_box = tk.Frame(page, bg=BG_CARD, highlightbackground=BORDER,
                             highlightthickness=1)
        chart_box.pack(fill="x", pady=(0, 6))
        if HAS_MPL:
            self.trend = TrendChart(chart_box, bg=BG_CARD, fg=FG_PRIMARY)
        else:
            tk.Label(chart_box, text="Install matplotlib for the trend chart:\n"
                     "pip install matplotlib", bg=BG_CARD, fg=FG_MUTED,
                     font=FONT_BODY, justify="center").pack(expand=True, pady=22)
            self.trend = None

        cols = ("when", "score", "cpu", "mem", "disk", "warn", "fail")
        self.hist_tree = ttk.Treeview(page, columns=cols, show="headings", height=10)
        for c, w, label in [("when", 200, "When"), ("score", 70, "Score"),
                            ("cpu", 70, "CPU %"), ("mem", 70, "RAM %"),
                            ("disk", 70, "Disk %"), ("warn", 80, "Warnings"),
                            ("fail", 70, "Fails")]:
            self.hist_tree.heading(c, text=label)
            self.hist_tree.column(c, width=w,
                                  anchor="w" if c == "when" else "center")
        vsb = ttk.Scrollbar(page, orient="vertical", command=self.hist_tree.yview)
        self.hist_tree.configure(yscrollcommand=vsb.set)
        self.hist_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")

    def refresh_history(self) -> None:
        """Reload the history table + trend chart from SQLite."""
        entries = self.history.recent(limit=80)
        self.history_page_entries = entries
        if self.trend is not None:
            self.trend.update(entries)
        try:
            self.hist_tree.delete(*self.hist_tree.get_children())
            for e in reversed(entries):  # newest at the top
                self.hist_tree.insert("", "end", values=(
                    e.ts, e.score,
                    "n/a" if e.cpu is None else f"{e.cpu:.0f}",
                    "n/a" if e.mem is None else f"{e.mem:.0f}",
                    "n/a" if e.disk is None else f"{e.disk:.0f}",
                    e.warnings, e.fails))
        except tk.TclError:
            pass
        if not self.history.available:
            note = f"History unavailable: {self.history.error}"
        else:
            note = (f"{len(entries)} of {self.history.count()} record(s) · "
                    f"{self.history.path}")
        try:
            self.history_note.configure(text=note)
        except tk.TclError:
            pass

    def clear_history_action(self) -> None:
        if not self.history.available:
            messagebox.showinfo("History", f"History is unavailable:\n{self.history.error}")
            return
        if not messagebox.askyesno("Clear history",
                                   "Delete all stored scan history?\n"
                                   "This does not touch any files outside the app."):
            return
        self.history.clear()
        self.refresh_history()
        self._log("Scan history cleared.", "WARNING")
        self.set_status("Scan history cleared.")

    def export_history_html(self) -> None:
        entries = self.history_page_entries or self.history.recent(limit=80)
        if not entries:
            messagebox.showinfo("No history", "Run a scan first — there is no history yet.")
            return
        points = [{"ts": e.ts, "score": e.score} for e in entries]
        latest = entries[-1]
        hostname = self.latest_snap.hostname if self.latest_snap is not None else ""
        if not hostname:
            try:
                hostname = socket.gethostname()
            except OSError:
                hostname = "unknown"
        payload = {
            "tool": "Smart Hardware Diagnostic Scanner — History trend",
            "author": "Devansh",
            "timestamp": latest.ts or datetime.now().isoformat(timespec="seconds"),
            "python": python_info(),
            "hostname": hostname,
            "health_score": latest.score,
            "uptime_human": "n/a",
            "os": platform.system(),
            "scan_duration_s": 0,
            "summary": {"PASS": 0,
                        "WARNING": sum(e.warnings for e in entries),
                        "FAIL": sum(e.fails for e in entries),
                        "UNAVAILABLE": 0},
            "findings": [],
            "recommendations": ["This report summarises scan history, not one scan."],
            "hardware": {"cpu": {"percent": latest.cpu, "logical_cores": None,
                                 "freq_current_mhz": None},
                         "memory": {"percent": latest.mem, "used": 0, "total": 0},
                         "disks": [], "network": {"interfaces": []},
                         "temperatures": [], "gpu": {"message": "see scan reports"},
                         "battery": {}, "disk_io": {}},
            "thresholds": dict(self.thresholds),
        }
        dest = filedialog.asksaveasfilename(
            defaultextension=".html", filetypes=[("HTML", "*.html")],
            initialfile=f"shds-history-{datetime.now().strftime('%Y%m%d-%H%M%S')}.html")
        if not dest:
            return
        try:
            path = save_report(payload, dest, render_trend_png_b64(points))
        except (ValueError, PermissionError, OSError) as exc:
            messagebox.showerror("Export failed", str(exc))
            return
        self._log(f"History trend saved: {path}", "PASS")
        messagebox.showinfo("Trend saved", f"Saved trend report to:\n{path}")

    def _build_reports(self, page) -> None:
        ttk.Label(page, text="Reports, thresholds & safe tools", style="Head.TLabel").pack(anchor="w")
        row = ttk.Frame(page)
        row.pack(fill="x", pady=8)
        ttk.Button(row, text="Save JSON", style="Ghost.TButton",
                   command=lambda: self.export_report("json")).pack(side="left", padx=(0, 8))
        ttk.Button(row, text="Save TXT", style="Ghost.TButton",
                   command=lambda: self.export_report("txt")).pack(side="left", padx=(0, 8))
        ttk.Button(row, text="Save CSV", style="Ghost.TButton",
                   command=lambda: self.export_report("csv")).pack(side="left", padx=(0, 8))
        ttk.Button(row, text="Save HTML", style="Accent.TButton",
                   command=lambda: self.export_report("html")).pack(side="left")
        th_box = tk.LabelFrame(page, text="Thresholds (% / °C) — documented, configurable",
                               bg=BG_PANEL, fg=NEON_CYAN, font=FONT_SMALL,
                               padx=10, pady=8)
        th_box.pack(fill="x", pady=8)
        self.th_vars: dict[str, tk.StringVar] = {}
        for i, (key, val) in enumerate(self.thresholds.items()):
            tk.Label(th_box, text=key, bg=BG_PANEL, fg=FG_MUTED,
                     font=FONT_SMALL).grid(row=i // 4, column=(i % 4) * 2,
                                           sticky="w", padx=4, pady=2)
            var = tk.StringVar(value=str(val))
            ent = ttk.Entry(th_box, textvariable=var, width=8)
            ent.grid(row=i // 4, column=(i % 4) * 2 + 1, padx=4, pady=2, sticky="w")
            self.th_vars[key] = var
        ttk.Button(th_box, text="Apply thresholds", style="Ghost.TButton",
                   command=self.apply_thresholds).grid(row=3, column=6, padx=8)
        clean_box = tk.LabelFrame(page, text="Safe temp-file cleanup (optional, preview first)",
                                  bg=BG_PANEL, fg=NEON_CYAN, font=FONT_SMALL,
                                  padx=10, pady=8)
        clean_box.pack(fill="both", expand=True)
        ttk.Label(clean_box, text=f"Approved location only: {approved_temp_root()} "
                  "(top-level files, safe suffixes, never recursive).",
                  font=FONT_SMALL, foreground=FG_MUTED,
                  background=BG_PANEL).pack(anchor="w")
        brow = ttk.Frame(clean_box)
        brow.pack(fill="x", pady=6)
        ttk.Button(brow, text="Preview files", style="Ghost.TButton",
                   command=self.preview_cleanup).pack(side="left", padx=(0, 8))
        ttk.Button(brow, text="Delete previewed files", style="Ghost.TButton",
                   command=self.clean_previewed).pack(side="left")
        self.clean_list = tk.Text(clean_box, height=8, bg=BG_OUTPUT,
                                  fg=FG_PRIMARY, font=FONT_MONO, wrap="word",
                                  state="disabled")
        self.clean_list.pack(fill="both", expand=True)
        self._previewed: list[Path] = []

        # Largest-files finder + scheduled scans share the bottom half.
        tools_row = ttk.Frame(page)
        tools_row.pack(fill="both", expand=True, pady=(8, 0))
        big_box = tk.LabelFrame(
            tools_row, text="Largest files (read-only — nothing is deleted)",
            bg=BG_PANEL, fg=NEON_CYAN, font=FONT_SMALL, padx=10, pady=8)
        big_box.pack(side="left", fill="both", expand=True, padx=(0, 8))
        self.bigfiles_var = tk.StringVar(value="")
        brow = ttk.Frame(big_box)
        brow.pack(fill="x", pady=(0, 6))
        tk.Label(brow, textvariable=self.bigfiles_var, bg=BG_PANEL,
                 fg=FG_MUTED, font=FONT_SMALL).pack(side="left", padx=(0, 8))
        ttk.Button(brow, text="Choose folder…", style="Ghost.TButton",
                   command=self.choose_big_files_folder).pack(side="right",
                                                             padx=(8, 0))
        self.bigfiles_btn = ttk.Button(brow, text="Find largest files",
                                       style="Ghost.TButton",
                                       command=self.find_largest_files)
        self.bigfiles_btn.pack(side="right")
        self.bigfiles_list = tk.Text(big_box, height=10, bg=BG_OUTPUT,
                                     fg=FG_PRIMARY, font=FONT_MONO, wrap="word",
                                     state="disabled")
        self.bigfiles_list.pack(fill="both", expand=True)

        sched_box = tk.LabelFrame(
            tools_row, text="Scheduled weekly scan (runs headless, your machine)",
            bg=BG_PANEL, fg=NEON_CYAN, font=FONT_SMALL, padx=10, pady=8)
        sched_box.pack(side="left", fill="both", expand=True)
        ttk.Label(sched_box, text="Registers this scanner in Windows Task "
                  "Scheduler (schtasks) or on a cron line (Linux/macOS).\nThe "
                  "scan runs with the documented flags: --scan --quiet "
                  "--no-history (writes nothing).",
                  font=FONT_SMALL, foreground=FG_MUTED,
                  background=BG_PANEL).pack(anchor="w", pady=(0, 6))
        srow = ttk.Frame(sched_box)
        srow.pack(fill="x", pady=(0, 6))
        tk.Label(srow, text="Day:", bg=BG_PANEL, fg=FG_MUTED,
                 font=FONT_SMALL).pack(side="left")
        self.schedule_day_var = tk.StringVar(value="MON")
        ttk.Combobox(srow, textvariable=self.schedule_day_var,
                     values=list(VALID_DAYS), width=6,
                     state="readonly").pack(side="left", padx=(4, 12))
        tk.Label(srow, text="Time (HH:MM):", bg=BG_PANEL, fg=FG_MUTED,
                 font=FONT_SMALL).pack(side="left")
        self.schedule_time_var = tk.StringVar(value="09:00")
        ttk.Entry(srow, textvariable=self.schedule_time_var,
                  width=7).pack(side="left", padx=(4, 12))
        ttk.Button(srow, text="Register weekly scan", style="Ghost.TButton",
                   command=self.register_schedule).pack(side="left",
                                                        padx=(0, 8))
        ttk.Button(srow, text="Remove schedule", style="Ghost.TButton",
                   command=self.remove_schedule).pack(side="left")

    # ---------- navigation / status ----------
    def show_page(self, name: str) -> None:
        self.pages[name].lift()
        self.page_title.configure(text=name)
        for key, btn in self.nav_buttons.items():
            btn.state(["pressed"] if key == name else ["!pressed"])
        if name == "History":
            self.refresh_history()
        self.set_status(f"Viewing {name}.")

    def _on_alerts_toggled(self) -> None:
        enabled = bool(self.alerts_enabled.get())
        self.settings["alerts"] = enabled
        save_config(self.settings)
        self._log(f"Desktop alerts {'enabled' if enabled else 'disabled'}.", "INFO")

    def _alert(self, component: str, title: str, message: str) -> None:
        """Raise an alert: in-app banner always, native toast when available.

        Repeat alerts for the same component are suppressed for
        ALERT_COOLDOWN_S so a sustained problem notifies once, not every tick.
        """
        now = time.monotonic()
        if not bool(self.alerts_enabled.get()):
            return
        last = self._alert_last.get(component, 0.0)
        if now - last < ALERT_COOLDOWN_S:
            return
        self._alert_last[component] = now
        toast = send_toast(title, message)
        self._log(f"ALERT ({component}): {message}"
                  f"{'' if toast else ' [in-app banner only]'}", "WARNING")
        try:
            self.alert_banner.configure(text=f"⚠ {component}: {message}")
            self._reschedule(15000, self._clear_alert_banner)
        except tk.TclError:
            pass

    def _clear_alert_banner(self) -> None:
        try:
            self.alert_banner.configure(text="")
        except tk.TclError:
            pass

    def set_status(self, msg: str) -> None:
        self.status_var.set(msg)

    # ---------- theme (dark default, live light toggle, persisted) ----------
    # Widget roles whose colours both palettes define; the swap map pairs each
    # value with its counterpart via the shared palette key.
    _THEME_SWAP_CLASSES = ("Frame", "Label", "Text", "Canvas",
                           "LabelFrame", "Toplevel")

    def _toggle_theme(self) -> None:
        self.apply_theme("dark" if get_theme_mode() == "light" else "light")

    def apply_theme(self, mode: str) -> None:
        """Switch palette live: ttk styles rebuild, plain widgets walk.

        ttk widgets pick the new colours up from the rebuilt styles; plain
        tk widgets keep construction-time colours, so every descendant is
        re-configured through the palette swap map. Dynamic colours (status
        dots, output tags, charts, detail windows) are re-rendered from the
        new palette. The choice is persisted to config.json.
        """
        mode = "light" if str(mode).lower() == "light" else "dark"
        self._theme_mode = mode
        set_theme_mode(mode)
        self.configure(bg=BG_MAIN)
        self._build_style()
        self._retheme_plain_widgets()
        self._retheme_text_tags()
        if self.chart is not None:
            self.chart.set_palette(BG_CARD, FG_PRIMARY, BORDER,
                                   NEON_CYAN, NEON_PURPLE)
        if self.trend is not None:
            self.trend.set_palette(BG_CARD, FG_PRIMARY, BORDER)
        self.theme_btn.configure(
            text="◐  Switch to dark" if mode == "light"
                 else "◐  Switch to light")
        self.settings["theme"] = mode
        save_config(self.settings)
        self.set_status(f"Theme: {mode} (saved).")
        if self.latest_snap is not None:
            try:
                self._apply_snapshot(self.latest_snap)  # re-render dot colours
            except Exception:  # noqa: BLE001 - re-render must never break toggling
                log.debug("snapshot re-render after theme change failed",
                          exc_info=True)

    def _retheme_plain_widgets(self) -> None:
        """Re-colour every plain (non-ttk) widget via the palette swap map."""
        dark, light = theme_palettes()
        swap: dict[str, str] = {}
        for key in PALETTE_KEYS:
            swap[dark[key]] = light[key]
            swap[light[key]] = dark[key]

        def _descendants(widget) -> "tuple[tk.Misc, ...]":
            for child in widget.winfo_children():
                yield child
                yield from _descendants(child)

        for widget in _descendants(self):
            try:
                cls = widget.winfo_class()
            except tk.TclError:
                continue
            if cls not in self._THEME_SWAP_CLASSES:
                continue  # ttk widgets follow the rebuilt styles
            for option in ("bg", "foreground", "highlightbackground"):
                try:
                    current = str(widget.cget(option))
                    new_value = swap.get(current)
                    if new_value:
                        widget.configure({option: new_value})
                except (tk.TclError, KeyError):
                    pass  # unsupported option on this widget — skip

    def _retheme_text_tags(self) -> None:
        try:
            for tag, colour in STATUS_COLOURS.items():
                self.diag_text.tag_configure(tag, foreground=colour)
        except (tk.TclError, AttributeError):
            pass

    def _log(self, msg: str, tag: str = "INFO") -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{stamp}] {msg}\n"
        try:
            self.diag_text.configure(state="normal")
            self.diag_text.insert("end", line, tag if tag in STATUS_COLOURS else None)
            self.diag_text.see("end")
            self.diag_text.configure(state="disabled")
        except (tk.TclError, AttributeError):
            pass
        if tag == "FAIL":
            log.error(msg)
        elif tag == "WARNING":
            log.warning(msg)
        else:
            log.info(msg)

    # ---------- data flows (thread-safe) ----------
    def refresh_once(self) -> None:
        if self._scan_active or self._refresh_active:
            return
        self._refresh_active = True
        self.set_status("Refreshing metrics…")
        try:
            threading.Thread(target=self._collect_job, name="shds-refresh",
                             daemon=True).start()
        except (RuntimeError, OSError) as exc:  # can't start new thread / shutdown
            log.warning("refresh thread failed to start: %s", exc)
            self._refresh_active = False
            self.set_status("Refresh unavailable: background thread could not start.")
            return

    def _collect_job(self) -> None:
        try:
            snap = collect_snapshot(include_gpu=True)
            item = ("snapshot", snap)
        except Exception as exc:  # never crash GUI
            log.exception("collect failed")
            item = ("refresh_error", f"Refresh failed: {exc}")
        finally:
            self._refresh_active = False
        self._bg_queue.put(item)

    def run_scan(self) -> None:
        if self._scan_active:
            messagebox.showinfo("Scan running", "A diagnostic scan is already running.")
            return
        self._scan_active = True
        try:
            self.scan_bar.start(12)
        except tk.TclError:
            pass
        self.head_status.configure(text="● scanning…", fg=NEON_YELLOW)
        self.set_status("Running diagnostic scan…")
        self._log("Starting diagnostic scan…", "INFO")
        try:
            threading.Thread(target=self._scan_job, name="shds-scan", daemon=True).start()
        except (RuntimeError, OSError) as exc:
            log.warning("scan thread failed to start: %s", exc)
            self._scan_active = False
            try:
                self.scan_bar.stop()
            except tk.TclError:
                pass
            self.head_status.configure(text="● idle", fg=FG_MUTED)
            self.set_status("Scan unavailable: background thread could not start.")

    def _scan_job(self) -> None:
        try:
            snap = collect_snapshot(include_gpu=True)
            diag = run_diagnostics(snap, self.thresholds)
            payload = build_report_dict(snap, diag, self.thresholds)
            self._bg_queue.put(("scan", (snap, diag, payload)))
        except Exception as exc:
            log.exception("scan failed")
            self._bg_queue.put(("error", f"Scan failed: {exc}"))
        finally:
            self._bg_queue.put(("scan_done", None))

    def _reschedule(self, ms: int, fn) -> None:
        """Schedule `fn`, remembering the id so shutdown can cancel it."""
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        holder: dict[str, str] = {}

        def _run() -> None:
            self._after_ids.discard(holder.get("id", ""))
            fn()

        try:
            holder["id"] = self.after(ms, _run)
            self._after_ids.add(holder["id"])
        except tk.TclError:
            pass

    def _poll_bg_queue(self) -> None:
        try:
            try:
                while True:
                    kind, data = self._bg_queue.get_nowait()
                    if kind == "snapshot":
                        self._apply_snapshot(data)
                    elif kind == "scan":
                        snap, diag, payload = data
                        self._apply_snapshot(snap)
                        self._apply_diag(diag, payload)
                    elif kind == "scan_done":
                        self._scan_active = False
                        try:
                            self.scan_bar.stop()
                        except tk.TclError:
                            pass
                        self.head_status.configure(text="● idle", fg=FG_MUTED)
                    elif kind == "error":
                        self._scan_active = False
                        try:
                            self.scan_bar.stop()
                        except tk.TclError:
                            pass
                        self._log(str(data), "FAIL")
                        self.set_status(str(data))
                    elif kind == "refresh_error":
                        # A failed manual refresh must not disturb an in-flight scan.
                        self._log(str(data), "FAIL")
                        self.set_status(str(data))
                    elif kind == "procs":
                        self._render_procs(data)
                    elif kind == "proc_detail":
                        pid, win, detail = data
                        self._render_process_detail(pid, win, detail)
                    elif kind == "connections":
                        self._render_connections(data)
                    elif kind == "startup":
                        self._render_startup(data)
                    elif kind == "smart":
                        self._render_smart(data)
                    elif kind == "bigfiles":
                        self._render_bigfiles(data)
                    elif kind == "update":
                        self._render_update_check(data)
                    elif kind == "schedule":
                        success, message = data
                        self._log(("Registered: " if success else "Failed: ")
                                  + message, "PASS" if success else "FAIL")
                        self.set_status(message)
            except queue.Empty:
                pass
            except Exception:  # noqa: BLE001 - one poisoned item must not kill the loop
                # A handler that raised must not permanently stop every
                # background update (snapshots, scans, process rows) while the
                # window stays alive — previously the loop simply died here,
                # leaving the scan button wedged on "Scan running".
                log.exception("background queue handler failed; polling continues")
        finally:
            # Runs even when a handler failed, so the loop always reschedules.
            self._reschedule(250, self._poll_bg_queue)

    def _apply_snapshot(self, snap: HardwareSnapshot) -> None:
        self.latest_snap = snap
        val_cpu, _bar_cpu, det_cpu, dot_cpu = self.cards["CPU"]
        self._draw_cores(self.cores_canvas, snap.cpu.per_core)
        if snap.cpu.available and snap.cpu.percent is not None:
            val_cpu.configure(text=f"{snap.cpu.percent:.1f} %")
            st = _status_of_pct(snap.cpu.percent, self.thresholds["cpu_warn"],
                                self.thresholds["cpu_fail"])
            dot_cpu.configure(fg=STATUS_COLOURS.get(st, FG_DIM))
            det_cpu.configure(text=f"{snap.cpu.logical_cores or '?'} logical cores · "
                              f"{snap.cpu.freq_current_mhz or '—'} MHz")
        else:
            val_cpu.configure(text="Unavailable")
            dot_cpu.configure(fg=FG_DIM)
            det_cpu.configure(text=snap.cpu.error or "psutil unavailable")
        val_m, bar_m, det_m, dot_m = self.cards["RAM"]
        if snap.mem.available and snap.mem.percent is not None:
            val_m.configure(text=f"{snap.mem.percent:.1f} %")
            bar_m.configure(value=snap.mem.percent)
            st = _status_of_pct(snap.mem.percent, self.thresholds["mem_warn"],
                                self.thresholds["mem_fail"])
            dot_m.configure(fg=STATUS_COLOURS.get(st, FG_DIM))
            det_m.configure(text=f"{format_bytes(snap.mem.used)} / {format_bytes(snap.mem.total)}")
        else:
            val_m.configure(text="Unavailable")
            bar_m.configure(value=0)
            dot_m.configure(fg=FG_DIM)
            det_m.configure(text="psutil unavailable")
        val_g, bar_g, det_g, dot_g = self.cards["GPU"]
        g: GpuStatus = snap.gpu
        if g.supported and g.gpus:
            first = g.gpus[0]
            if first.utilization_pct is not None:
                val_g.configure(text=f"{first.utilization_pct:.0f} %")
                bar_g.configure(value=first.utilization_pct)
            else:
                val_g.configure(text="No util")
                bar_g.configure(value=0)
            dot_g.configure(fg=NEON_GREEN)
            temp = f"{first.temperature_c:.0f}C" if first.temperature_c is not None else "temp n/a"
            det_g.configure(text=f"{first.name[:22]} · {temp}")
        else:
            val_g.configure(text="Unavailable")
            bar_g.configure(value=0)
            dot_g.configure(fg=FG_DIM)
            det_g.configure(text=(g.message[:42] if g.message else "unsupported"))
        val_d, bar_d, det_d, dot_d = self.cards["Disk"]
        dio = snap.disk_io
        io_text = (f" · R {format_rate_bytes(dio.read_bps)} "
                   f"W {format_rate_bytes(dio.write_bps)}") if dio.available else ""
        acc = [d for d in snap.disks if d.accessible and d.percent is not None]
        if acc:
            worst = max(acc, key=lambda d: d.percent or 0)
            assert worst.percent is not None
            val_d.configure(text=f"{worst.percent:.0f} %")
            bar_d.configure(value=worst.percent)
            st = _status_of_pct(worst.percent, self.thresholds["disk_warn"],
                                self.thresholds["disk_fail"])
            dot_d.configure(fg=STATUS_COLOURS.get(st, FG_DIM))
            det_d.configure(text=f"{worst.mountpoint} · {len(acc)} volume(s){io_text}")
        else:
            val_d.configure(text="Unavailable")
            bar_d.configure(value=0)
            dot_d.configure(fg=FG_DIM)
            det_d.configure(text=f"no accessible volumes{io_text}")
        val_n, bar_n, det_n, dot_n = self.cards["Network"]
        if snap.net.available:
            val_n.configure(text=f"{len(snap.net.interfaces)} ifaces")
            bar_n.configure(value=0)
            dot_n.configure(fg=NEON_GREEN)
            if snap.net.down_bps is not None and snap.net.up_bps is not None:
                det_n.configure(text=f"↓ {snap.net.down_bps/1e6:.2f} Mbps · "
                                f"↑ {snap.net.up_bps/1e6:.2f} Mbps")
            else:
                det_n.configure(text=", ".join(snap.net.interfaces[:3]) or "no traffic yet")
        else:
            val_n.configure(text="Unavailable")
            bar_n.configure(value=0)
            dot_n.configure(fg=FG_DIM)
            det_n.configure(text="counters unavailable")
        # Status-bar chips (uptime + battery are cheap and always available).
        self.uptime_var.set(f"Uptime: {format_uptime(snap.uptime_s)}")
        b = snap.battery
        if b.available and b.percent is not None:
            self.battery_var.set(f"Battery: {b.percent:.0f}%"
                                 + (" ⚡" if b.plugged else ""))
        else:
            self.battery_var.set("Battery: n/a")

        # Live threshold alerts (cooldown-limited; the banner always shows, the
        # toast only when plyer is present and alerts are enabled).
        if snap.cpu.available and snap.cpu.percent is not None \
                and snap.cpu.percent >= self.thresholds["cpu_warn"]:
            self._alert("CPU", "CPU load is high",
                        f"{snap.cpu.percent:.0f}% sustained utilisation")
        if snap.mem.available and snap.mem.percent is not None \
                and snap.mem.percent >= self.thresholds["mem_warn"]:
            self._alert("Memory", "Memory pressure",
                        f"{snap.mem.percent:.0f}% of RAM in use")
        worst_disk = None
        for d in snap.disks:
            if d.accessible and d.percent is not None:
                if worst_disk is None or d.percent > worst_disk.percent:
                    worst_disk = d
        if worst_disk is not None and worst_disk.percent is not None \
                and worst_disk.percent >= self.thresholds["disk_warn"]:
            self._alert("Storage", "Disk nearly full",
                        f"{worst_disk.mountpoint} at {worst_disk.percent:.0f}%")
        if b.available and b.percent is not None and not b.plugged \
                and b.percent <= self.thresholds["battery_warn"]:
            self._alert("Battery", "Battery low", f"{b.percent:.0f}% remaining")

        self.set_status(f"Updated {datetime.now().strftime('%H:%M:%S')} — host {snap.hostname}")
        self.refresh_processes()

    def _apply_diag(self, diag: DiagnosticReport, payload: dict) -> None:
        # Compare against the previous scan *before* overwriting it.
        self.last_comparison = compare_reports(self.previous_diag, diag)
        self.previous_diag = diag
        self.previous_payload = self.latest_payload
        self.latest_diag = diag
        self.latest_payload = payload
        self.scan_var.set(f"Last scan: {payload.get('timestamp', '')} "
                          f"({diag.duration_s:.1f}s)")
        self._draw_gauge(diag.health_score)
        self.health_var.set(f"Health: {diag.health_score}/100 "
                            f"({health_label(diag.health_score)})")
        try:
            self.health_chip.configure(
                text=f"Health {diag.health_score}/100",
                fg=(NEON_GREEN if diag.health_score >= 90 else
                    NEON_YELLOW if diag.health_score >= 55 else NEON_RED))
        except tk.TclError:
            pass
        self._log(f"Health score: {diag.health_score}/100 "
                  f"({health_label(diag.health_score)})", "INFO")
        for f in diag.findings:
            self._log(f"[{f.status}] {f.component}: {f.message}", f.status)
            if f.detail:
                self._log(f"    detail: {f.detail}", "INFO")
        self._log_comparison(self.last_comparison)
        self._log("— Recommendations —", "INFO")
        for r in diag.recommendations:
            self._log(f" • {r}", "INFO")
        s = diag.summary
        self._log(f"Summary: PASS={s.get('PASS',0)} WARNING={s.get('WARNING',0)} "
                  f"FAIL={s.get('FAIL',0)} UNAVAILABLE={s.get('UNAVAILABLE',0)}", "INFO")
        # Persist the summary for the History page (failure is non-fatal).
        if not self.history.record(payload):
            log.debug("history record skipped: %s", self.history.error or "unavailable")
        if self.pages.get("History") is not None and self.pages["History"].winfo_ismapped():
            self.refresh_history()
        # Only genuine FAILs raise an alert, so warnings don't become noise.
        for f in diag.findings:
            if f.status == "FAIL":
                self._alert(f"scan:{f.component}", f"{f.component} check failed", f.message)
        self.set_status("Scan complete.")
        self.show_page("Diagnostics")

    def _log_comparison(self, comparison: dict | None) -> None:
        """Print the regression/improvement diff from the previous scan."""
        if not comparison:
            return
        regressions = comparison.get("regressions", [])
        improvements = comparison.get("improvements", [])
        added = comparison.get("added", [])
        removed = comparison.get("removed", [])
        if not (regressions or improvements or added or removed):
            self._log("— Changes since last scan —", "INFO")
            self._log(" • No change: every check kept the same status.", "INFO")
            return
        self._log("— Changes since last scan —", "INFO")
        for key, before, after, _msg in regressions:
            self._log(f" • WORSE   {key}: {before} → {after}", "WARNING")
        for key, before, after, _msg in improvements:
            self._log(f" • BETTER  {key}: {before} → {after}", "PASS")
        for key, _before, after, _msg in added:
            self._log(f" • NEW     {key}: {after}", "INFO")
        for key, before, _after, _msg in removed:
            self._log(f" • GONE    {key}: was {before}", "INFO")

    def show_comparison(self) -> None:
        """Re-print the last comparison (button on the Diagnostics page)."""
        if self.latest_diag is None:
            messagebox.showinfo("Nothing to compare", "Run a scan first.")
            return
        if self.previous_diag is None or self.last_comparison is None:
            self._log("No previous scan to compare with yet — this was the first scan.",
                      "INFO")
            return
        self._log_comparison(self.last_comparison)
        regressions = len(self.last_comparison.get("regressions", []))
        improvements = len(self.last_comparison.get("improvements", []))
        self._log(f"Compared with the previous scan: {regressions} regression(s), "
                  f"{improvements} improvement(s).", "INFO")

    # ---------- monitoring ----------
    def _apply_interval(self) -> None:
        try:
            self.monitor.set_interval(float(self.refresh_interval.get()))
        except (tk.TclError, ValueError):
            pass

    def _poll_interval_ms(self) -> int:
        try:
            return int(max(0.5, float(self.refresh_interval.get())) * 1000)
        except (tk.TclError, ValueError):
            return 1000

    def toggle_monitoring(self) -> None:
        if self.monitor_running:
            self.monitor.stop()
            self.monitor_running = False
            self.mon_btn.configure(text="▶  Start monitoring")
            self.head_status.configure(text="● idle", fg=FG_MUTED)
            self.set_status("Monitoring stopped.")
            self._log("Live monitoring stopped.", "INFO")
            return
        self._apply_interval()
        if self.monitor.start():
            self.monitor_running = True
            self.mon_btn.configure(text="■  Stop monitoring")
            self.head_status.configure(text="● live", fg=NEON_GREEN)
            self.set_status("Live monitoring running…")
            self._log("Live monitoring started.", "PASS")
            if not self._monitor_polling:
                self._monitor_polling = True
                self._reschedule(500, self._poll_monitor)
        else:
            messagebox.showinfo("Monitoring", "Monitoring is already running.")

    def _poll_monitor(self) -> None:
        if not self.monitor_running:
            self._monitor_polling = False
            return
        try:
            items = self.monitor.poll()
            snaps = [i for i in items if isinstance(i, HardwareSnapshot)]
            samples = [i for i in items if isinstance(i, Sample)]
            for s in samples:
                if s.cpu is not None:
                    self.cpu_hist.append(s.cpu)
                if s.mem is not None:
                    self.mem_hist.append(s.mem)
                for hist in (self.cpu_hist, self.mem_hist):
                    if len(hist) > 120:
                        del hist[: len(hist) - 120]
            if self.chart is not None:
                self.chart.update(self.cpu_hist, self.mem_hist)
            if snaps:
                self._apply_snapshot(snaps[-1])  # main thread — safe
        except Exception:  # noqa: BLE001 - one bad tick must not kill the loop
            # Without this, an exception during _apply_snapshot silently
            # stopped live monitoring while the header kept saying "live".
            log.exception("monitor poll failed; polling continues")
        self._reschedule(self._poll_interval_ms(), self._poll_monitor)

    # ---------- processes ----------
    def refresh_processes(self) -> None:
        # Read the Tkinter variable here on the main thread; the worker only
        # receives a plain string and never touches Tkinter.
        # `_apply_snapshot` calls this on every monitoring tick, and
        # process_iter() can take longer than a 0.5 s interval, so without this
        # guard the daemon threads would pile up on a busy machine.
        if self._procs_active:
            return
        try:
            sort_by = str(self.proc_sort.get())
        except tk.TclError:
            sort_by = "cpu"
        self._procs_active = True
        try:
            threading.Thread(target=self._proc_job, args=(sort_by,),
                             name="shds-procs", daemon=True).start()
        except (RuntimeError, OSError) as exc:
            log.warning("process thread failed to start: %s", exc)
            self._procs_active = False

    def _proc_job(self, sort_by: str) -> None:
        """Worker: collect top processes. Takes plain values only — no Tkinter."""
        try:
            rows = collect_processes(limit=14, sort_by=sort_by)
            self._bg_queue.put(("procs", rows))
        except Exception as exc:
            log.warning("process refresh failed: %s", exc)
        finally:
            self._procs_active = False

    def _render_procs(self, rows) -> None:
        try:
            self.proc_tree.delete(*self.proc_tree.get_children())
            for r in rows:
                self.proc_tree.insert("", "end",
                                      values=(r.pid, r.name, f"{r.cpu:.1f}", f"{r.mem_mb:.1f}"))
        except tk.TclError:
            pass

    # ---------- reports / tools ----------
    def export_report(self, fmt: str) -> None:
        if not self.latest_payload:
            messagebox.showinfo("No report yet",
                                "Run a diagnostic scan first, then export.")
            return
        ext = {"json": ".json", "txt": ".txt", "csv": ".csv", "html": ".html"}[fmt]
        types = {"json": [("JSON", "*.json")], "txt": [("Text", "*.txt")],
                 "csv": [("CSV", "*.csv")], "html": [("HTML", "*.html")]}[fmt]
        dest = filedialog.asksaveasfilename(
            defaultextension=ext, filetypes=types,
            initialfile=f"shds-report-{datetime.now().strftime('%Y%m%d-%H%M%S')}{ext}")
        if not dest:
            return
        chart_b64 = None
        if fmt == "html":
            points = [{"ts": e.ts, "score": e.score}
                      for e in self.history.recent(limit=80)]
            chart_b64 = render_trend_png_b64(points)
        try:
            path = save_report(self.latest_payload, dest, chart_b64)
        except (ValueError, PermissionError, OSError) as exc:
            messagebox.showerror("Export failed", str(exc))
            return
        messagebox.showinfo("Report saved", f"Saved report to:\n{path}")
        self.set_status(f"Report saved: {path}")
        self._log(f"Report saved: {path}", "PASS")

    # Pairs of (warning key, failure key) used to validate threshold ordering.
    THRESHOLD_PAIRS = [("cpu_warn", "cpu_fail"), ("mem_warn", "mem_fail"),
                       ("disk_warn", "disk_fail"), ("temp_warn", "temp_fail"),
                       ("gpu_temp_warn", "gpu_temp_fail")]

    @staticmethod
    def validate_thresholds(values: dict[str, float]) -> str | None:
        """Validate candidate thresholds without side effects.

        Returns an error message, or None when the values are consistent.
        Every entered value is checked before any of them is applied.
        """
        for key, val in values.items():
            if not 0 <= val <= 200:
                return f"'{key}' must be a number between 0 and 200."
        for warn_key, fail_key in ScannerApp.THRESHOLD_PAIRS:
            warn, fail = values.get(warn_key), values.get(fail_key)
            if warn is None or fail is None:
                continue
            if warn >= fail:
                return (f"'{warn_key}' ({warn:g}) must be less than "
                        f"'{fail_key}' ({fail:g}).")
        # Battery is the inverse rule (a low value is the bad one), so warning
        # must sit ABOVE failure (default warn 25% / fail 8%). A reversed pair
        # would mark a healthy charge as FAIL, so reject it explicitly.
        batt_warn = values.get("battery_warn")
        batt_fail = values.get("battery_fail")
        if batt_warn is not None and batt_fail is not None and batt_warn <= batt_fail:
            return (f"'battery_warn' ({batt_warn:g}) must be greater than "
                    f"'battery_fail' ({batt_fail:g}).")
        return None

    def apply_thresholds(self) -> None:
        # Parse everything first; only mutate self.thresholds when all values
        # are valid, so the active thresholds never end up half-applied.
        values: dict[str, float] = {}
        for key, var in self.th_vars.items():
            try:
                values[key] = float(var.get())
            except (ValueError, tk.TclError):
                messagebox.showerror("Invalid threshold", f"'{key}' must be a number.")
                return
        error = self.validate_thresholds(values)
        if error is not None:
            messagebox.showerror("Invalid threshold", error)
            self.set_status("Thresholds not applied — invalid values.")
            self._log(f"Thresholds rejected: {error}", "WARNING")
            return
        self.thresholds = values
        self.settings["thresholds"] = dict(values)
        saved = save_config(self.settings)
        self._log(f"Thresholds updated: {self.thresholds}"
                  f"{'' if saved else ' (could not write config.json)'}", "INFO")
        self.set_status("Thresholds applied.")

    def preview_cleanup(self) -> None:
        files, note = preview_temp_candidates()
        self._previewed = files
        try:
            self.clean_list.configure(state="normal")
            self.clean_list.delete("1.0", "end")
            self.clean_list.insert("end", note + "\n")
            if not files:
                self.clean_list.insert("end", "Nothing safe to delete.\n")
            for p in files:
                try:
                    size = p.stat().st_size
                except OSError:
                    size = 0
                self.clean_list.insert("end", f"{p}  ({format_bytes(size)})\n")
            self.clean_list.configure(state="disabled")
        except tk.TclError:
            pass
        self._log(f"Cleanup preview: {len(files)} file(s). {note}", "INFO")

    def clean_previewed(self) -> None:
        if not self._previewed:
            messagebox.showinfo("Cleanup", "Preview first — nothing selected.")
            return
        if not messagebox.askyesno(
                "Confirm deletion",
                f"Delete {len(self._previewed)} file(s) inside\n{approved_temp_root()}\n"
                "only? Locked files are skipped. This cannot be undone."):
            return
        root = approved_temp_root()
        removed, skipped, freed = 0, 0, 0
        for p in list(self._previewed):
            try:
                rp = p.resolve()
                rp.relative_to(root)  # containment check
                if rp.suffix.lower() not in SAFE_TEMP_SUFFIXES or not rp.is_file():
                    skipped += 1
                    continue
                size = rp.stat().st_size
                rp.unlink()
                freed += size
                removed += 1
            except (OSError, ValueError):
                skipped += 1
        self._previewed = []
        msg = (f"Cleanup done: removed {removed}, skipped {skipped}, "
               f"freed {format_bytes(freed)}.")
        self._log(msg, "PASS" if removed else "WARNING")
        messagebox.showinfo("Cleanup", msg)
        self.preview_cleanup()

    # ---------- shutdown ----------
    def on_closing(self) -> None:
        try:
            if self.monitor_running:
                self.monitor.request_stop()
        except Exception:
            pass
        self._scan_active = False
        # Persist window size + settings; never let this block the shutdown.
        try:
            self.settings["geometry"] = self.winfo_geometry()
            self.settings["thresholds"] = dict(self.thresholds)
            self.settings["alerts"] = bool(self.alerts_enabled.get())
            save_config(self.settings)
        except (tk.TclError, ValueError) as exc:
            log.debug("could not persist settings on exit: %s", exc)
        try:
            self.history.close()
        except Exception as exc:
            log.debug("history close failed: %s", exc)
        for pid, win in list(self._detail_windows.items()):
            try:
                win.destroy()
            except tk.TclError:
                pass
            self._detail_windows.pop(pid, None)
            self._detail_texts.pop(pid, None)
        # Cancel pending callbacks first: a callback that fires after destroy()
        # prints Tcl's "invalid command name" and is never useful.
        for after_id in list(self._after_ids):
            try:
                self.after_cancel(after_id)
            except tk.TclError:
                pass
        self._after_ids.clear()
        try:
            self.destroy()
        except tk.TclError:
            pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def _log_hint() -> str:
    """Best-effort real log-file location."""
    try:
        return str(get_log_dir() / "scanner.log")
    except Exception:
        system = platform.system().lower()
        if system == "windows":
            return r"%LOCALAPPDATA%\SmartHardwareDiagnosticScanner\logs\scanner.log"
        if system == "darwin":
            return ("~/Library/Application Support/"
                    "SmartHardwareDiagnosticScanner/logs/scanner.log")
        return "~/.local/share/SmartHardwareDiagnosticScanner/logs/scanner.log"


def build_parser() -> argparse.ArgumentParser:
    """Command-line interface (headless scanning for Task Scheduler / CI)."""
    parser = argparse.ArgumentParser(
        prog="shds_gui.py",
        description="Smart Hardware Diagnostic Scanner — Dark Neon Edition. "
                    "Run without arguments to open the GUI, or use --scan for a "
                    "headless scan (usable from Task Scheduler).",
        epilog="Exit codes: 0 = no FAIL findings, 1 = at least one FAIL, "
               "2 = the scan could not run.")
    parser.add_argument("--scan", action="store_true",
                        help="run one headless scan and exit (no GUI)")
    parser.add_argument("--json", metavar="PATH", help="write a JSON report to PATH (with --scan)")
    parser.add_argument("--txt", metavar="PATH", help="write a TXT report to PATH (with --scan)")
    parser.add_argument("--csv", metavar="PATH", help="write a CSV report to PATH (with --scan)")
    parser.add_argument("--html", metavar="PATH",
                        help="write a self-contained HTML report to PATH (with --scan)")
    parser.add_argument("--no-gpu", action="store_true",
                        help="skip the nvidia-smi query (faster)")
    parser.add_argument("--no-history", action="store_true",
                        help="do not record this scan in the local history database")
    parser.add_argument("--thresholds", metavar="PATH",
                        help="load thresholds from a config JSON (default: saved config)")
    parser.add_argument("--quiet", action="store_true",
                        help="print only the summary (health score and counts), "
                             "not the per-finding list")
    return parser


def run_cli(argv: list[str]) -> int:
    """Headless scan used by `--scan`. Returns a process exit code.

    Never opens a Tk window, so it is safe in a service/scheduled context.
    """
    args = build_parser().parse_args(argv)

    # A cp1252 console (common on Windows) cannot encode characters such as the
    # em dash or emoji used in findings; replace rather than crash the scan.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, OSError, ValueError):
            pass

    if args.thresholds:
        # A thresholds file that cannot be read must NOT silently fall back to
        # the defaults: that would evaluate the machine against thresholds the
        # caller never asked for and return a meaningless exit code.
        tpath = Path(args.thresholds)
        if not tpath.is_file():
            print(f"Thresholds file not found: {tpath}", file=sys.stderr)
            return 2
        try:
            supplied = json.loads(tpath.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            print(f"Could not read thresholds file {tpath}: {exc}", file=sys.stderr)
            return 2
        if not isinstance(supplied, dict):
            print(f"Thresholds file {tpath} is not a JSON object.", file=sys.stderr)
            return 2
        # Accept both {"thresholds": {...}} (the app's own config shape) and a
        # flat {"cpu_warn": ...} map, so a hand-written file is never silently
        # ignored.
        raw = supplied.get("thresholds", supplied)
        if not isinstance(raw, dict):
            print(f"Thresholds file {tpath} has no 'thresholds' object.",
                  file=sys.stderr)
            return 2
        thresholds = sanitise_thresholds(raw)
    else:
        thresholds = sanitise_thresholds(load_config().get("thresholds"))

    if ScannerApp.validate_thresholds(thresholds) is not None:
        print("Invalid thresholds in the supplied config; using defaults.",
              file=sys.stderr)
        thresholds = dict(DEFAULT_THRESHOLDS)

    def emit(message: str = "") -> None:
        if not args.quiet:
            print(message)

    def emit_always(message: str = "") -> None:
        # The summary lines are the whole point of --quiet: a scheduled run
        # must still report the health score and the finding counts.
        print(message)

    emit("Scanning hardware…")
    try:
        snap = collect_snapshot(include_gpu=not args.no_gpu)
        diag = run_diagnostics(snap, thresholds)
        payload = build_report_dict(snap, diag, thresholds)
    except Exception as exc:
        log.exception("headless scan failed")
        print(f"Scan failed: {exc}", file=sys.stderr)
        return 2

    summary = diag.summary
    emit_always(f"Health score: {diag.health_score}/100 ({health_label(diag.health_score)})")
    emit_always(f"PASS={summary.get('PASS', 0)} WARNING={summary.get('WARNING', 0)} "
                f"FAIL={summary.get('FAIL', 0)} UNAVAILABLE={summary.get('UNAVAILABLE', 0)}")
    if not args.quiet:
        emit("")
        for f in diag.findings:
            line = f"[{f.status}] {f.component}: {f.message}"
            if f.detail:
                line += f"  ({f.detail})"
            emit(line)
        if diag.recommendations:
            emit("")
            emit("Recommendations:")
            for r in diag.recommendations:
                emit(f" - {r}")

    chart_b64 = None
    if args.html:
        store = HistoryStore()
        try:
            points = [{"ts": e.ts, "score": e.score} for e in store.recent(limit=80)]
        finally:
            store.close()
        chart_b64 = render_trend_png_b64(points)

    for fmt, dest in (("json", args.json), ("txt", args.txt),
                      ("csv", args.csv), ("html", args.html)):
        if not dest:
            continue
        try:
            path = save_report(payload, dest, chart_b64 if fmt == "html" else None)
            emit_always(f"{fmt.upper()} report written: {path}")
        except (ValueError, PermissionError, OSError) as exc:
            print(f"Could not write {fmt.upper()} report: {exc}", file=sys.stderr)
            return 2

    if not args.no_history:
        store = HistoryStore()
        written = store.record(payload)
        store.close()
        if not written:
            log.info("history not recorded (store unavailable)")

    emit_always(f"Done: health {diag.health_score}/100, "
                f"FAIL={summary.get('FAIL', 0)}, WARNING={summary.get('WARNING', 0)}")
    return 1 if summary.get("FAIL", 0) else 0


def main(argv: list[str] | None = None) -> int:
    """GUI by default; `--scan` switches to the headless CLI path."""
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if any(a == "--scan" for a in argv):
            return run_cli(argv)
        if argv:
            # Unknown args without --scan: report usage rather than silently
            # opening the GUI and ignoring what the user asked for.
            build_parser().parse_args(argv)
            print("Nothing to do: pass --scan for a headless run, "
                  "or run without arguments to open the GUI.")
            return 0
        app = ScannerApp()
        try:
            app.mainloop()
            return 0
        finally:
            # on_closing() (WM_DELETE_WINDOW) normally closes the SQLite
            # history store; if mainloop() raised, it never ran, and the
            # connection would keep history.db locked.
            try:
                app.history.close()
            except Exception:
                pass
    except SystemExit as exc:  # argparse usage errors
        return int(exc.code or 0)
    except Exception as exc:
        logging.getLogger("shds").exception("Fatal error: %s", exc)
        traceback.print_exc()
        try:
            # GUI path (pythonw has no console): a dialog is the only way the
            # user sees the failure, so always try.
            messagebox.showerror("Fatal error", f"Unexpected error:\n{exc}")
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
