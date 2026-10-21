"""Standalone single-file build of the Warecare hardware diagnostic scanner.

The theme, utilities, hardware collectors, diagnostic engine, and
CustomTkinter interface are merged here into one self-contained module.
"""

from __future__ import annotations

import csv
import heapq
import html
import http.client
import itertools
import json
import logging
import logging.handlers
import math
import os
import platform
import plistlib
import queue
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Iterable

import customtkinter as ctk
from PIL import Image, ImageDraw

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:  # optional dependency
    psutil = None  # type: ignore
    HAS_PSUTIL = False

try:
    import winreg
except ImportError:  # Windows-only stdlib module
    winreg = None  # type: ignore


class _ModuleNamespace:
    """Attribute proxy over this module's globals for merged submodules."""

    def __getattr__(self, name: str):
        try:
            return globals()[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


T = _ModuleNamespace()
icons = _ModuleNamespace()
ctkw = _ModuleNamespace()

"""Centralised dark-neon theme with a light mode (valid Tkinter colours only).

`set_mode("dark"|"light")` re-points the module-level colour constants, so UI
code can keep reading `T.BG_MAIN`-style names. Plain (non-ttk) widgets keep
their construction colours, so the apps re-configure them during a live
toggle — every palette below has the SAME key set (enforced by tests).

The palette also carries the colours CustomTkinter needs (buttons, entries and
progress bars). CustomTkinter does not use ttk styles: each widget takes its
colours as constructor options (`fg_color`, `hover_color`, `text_color`, ...),
so `ctk_button_kwargs()` / `ctk_entry_kwargs()` / `ctk_progress_kwargs()` build
the option dicts straight from the palette and are the single place where the
CTk look is defined.
"""
__version__ = "1.0.0"
APP_VERSION = __version__


import platform

import customtkinter as ctk

# Font selection: pick families that exist on the current OS; Tk silently
# falls back to its default when a family is missing, so these are hints,
# never requirements.
def _default_font_family() -> str:
    system = platform.system().lower()
    if system == "windows":
        return "Segoe UI"
    if system == "darwin":
        return "Helvetica Neue"
    return "DejaVu Sans"  # Linux/other


def _default_mono_family() -> str:
    system = platform.system().lower()
    if system == "windows":
        return "Consolas"
    if system == "darwin":
        return "Menlo"
    return "DejaVu Sans Mono"  # Linux/other


# Every palette defines exactly these keys (verified by tests).
PALETTE_KEYS = (
    "BG_MAIN", "BG_PANEL", "BG_CARD", "BG_SIDEBAR", "BG_OUTPUT",
    "BG_STATUSBAR", "FG_PRIMARY", "FG_MUTED", "FG_DIM",
    "NEON_CYAN", "NEON_PURPLE", "NEON_GREEN", "NEON_YELLOW", "NEON_RED",
    "NEON_ORANGE", "AMBER", "BORDER", "ENTRY_BG", "PROGRESS_BG", "OUTPUT_FG",
    # CustomTkinter widget colours.
    "BTN_GHOST_BG", "BTN_GHOST_HOVER", "BTN_GHOST_FG", "BTN_GHOST_BORDER",
    "BTN_ACCENT_BG", "BTN_ACCENT_HOVER", "BTN_ACCENT_FG",
    "BTN_SIDE_BG", "BTN_SIDE_HOVER", "BTN_SIDE_FG",
    "BTN_SIDE_ACTIVE_BG", "BTN_SIDE_ACTIVE_FG",
    "ENTRY_FG", "ENTRY_BORDER", "PROGRESS_FG",
)

# Sidebar buttons sit on the sidebar with `fg_color="transparent"`; only the
# active item gets a solid highlight. "transparent" is a valid CustomTkinter
# colour (the widget then paints its parent's colour) but not a Tk colour, so
# it lives only in the BTN_* widget keys.
_SIDE_TRANSPARENT = "transparent"

_DARK_PALETTE = {
    # Backgrounds. BG_CARD (#1C1C28) is shared by the widgets' ghost styling —
    # keep every value below UNIQUE across the palette, or the dark<->light
    # retheme swap (which is keyed by colour values) resolves collisions
    # against the wrong key. (BG_PANEL == BG_CARD and BG_SIDEBAR ==
    # BG_STATUSBAR are safe: their light counterparts are equal too.)
    "BG_MAIN": "#12121A",
    "BG_PANEL": "#1C1C28",
    "BG_CARD": "#1C1C28",
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
    "AMBER": "#FFB545",
    # Widget specifics.
    "BORDER": "#34344A",
    "ENTRY_BG": "#101018",
    "PROGRESS_BG": "#2A2A3D",
    "OUTPUT_FG": "#CFFAFE",
    # CustomTkinter buttons.
    "BTN_GHOST_BG": "#1C1C28",
    "BTN_GHOST_HOVER": "#262636",
    "BTN_GHOST_FG": "#E8E8F0",
    "BTN_GHOST_BORDER": "#34344A",
    "BTN_ACCENT_BG": "#00E5FF",
    "BTN_ACCENT_HOVER": "#4DF0FF",
    "BTN_ACCENT_FG": "#0A0A12",
    "BTN_SIDE_BG": "transparent",
    "BTN_SIDE_HOVER": "#232333",
    "BTN_SIDE_FG": "#E8E8F0",
    "BTN_SIDE_ACTIVE_BG": "#1E1E2E",
    "BTN_SIDE_ACTIVE_FG": "#00E5FF",
    # CustomTkinter entries / progress bars.
    "ENTRY_FG": "#E8E8F0",
    "ENTRY_BORDER": "#34344A",
    "PROGRESS_FG": "#00E5FF",
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
    "AMBER": "#B87A1E",
    # Widget specifics.
    "BORDER": "#C8C8D8",
    "ENTRY_BG": "#FFFFFF",
    "PROGRESS_BG": "#DDDDE8",
    "OUTPUT_FG": "#1A1A28",
    # CustomTkinter buttons.
    "BTN_GHOST_BG": "#FFFFFF",
    "BTN_GHOST_HOVER": "#ECECF4",
    "BTN_GHOST_FG": "#1A1A28",
    "BTN_GHOST_BORDER": "#C8C8D8",
    "BTN_ACCENT_BG": "#0077B8",
    "BTN_ACCENT_HOVER": "#1A8FD1",
    "BTN_ACCENT_FG": "#FFFFFF",
    "BTN_SIDE_BG": "transparent",
    "BTN_SIDE_HOVER": "#DCDCE8",
    "BTN_SIDE_FG": "#1A1A28",
    "BTN_SIDE_ACTIVE_BG": "#DCDCE8",
    "BTN_SIDE_ACTIVE_FG": "#0077B8",
    # CustomTkinter entries / progress bars.
    "ENTRY_FG": "#1A1A28",
    "ENTRY_BORDER": "#C8C8D8",
    "PROGRESS_FG": "#0077B8",
}


def _apply_palette(palette: dict[str, str]) -> None:
    """Point the module-level constants at the given palette."""
    for key in PALETTE_KEYS:
        globals()[key] = palette[key]
    globals()["STATUS_COLOURS"] = {
        "PASS": palette["NEON_GREEN"],
        "WARNING": palette["NEON_YELLOW"],
        "FAIL": palette["NEON_RED"],
        "UNAVAILABLE": palette["FG_MUTED"],
        "INFO": palette["NEON_CYAN"],
    }


def set_mode(mode: str) -> None:
    """Switch the whole palette ("dark" default, "light" supported)."""
    _apply_palette(_LIGHT_PALETTE if str(mode) == "light" else _DARK_PALETTE)
    # Keep CustomTkinter's own appearance mode in step, so the parts of a
    # widget CTk paints itself (default text colours, entry placeholders)
    # follow the app's theme as well.
    try:
        ctk.set_appearance_mode("light" if str(mode) == "light" else "dark")
    except Exception:  # noqa: BLE001 - appearance mode must never break a theme switch
        pass


def get_mode() -> str:
    """Return the currently active mode name."""
    return "light" if BG_MAIN == _LIGHT_PALETTE["BG_MAIN"] else "dark"


def palettes() -> tuple[dict[str, str], dict[str, str]]:
    """Return (dark, light) palette dicts; used for live widget re-theming."""
    return dict(_DARK_PALETTE), dict(_LIGHT_PALETTE)


# ---------------------------------------------------------------------------
# CustomTkinter option builders
#
# CustomTkinter has no ttk styles: every widget takes its colours as
# constructor options. These helpers are the single place where the CTk look
# is defined, so the live theme toggle can re-apply exactly the same colours
# (see the apps' *_retheme_ctk_widgets()).
# ---------------------------------------------------------------------------

# Button roles and the height (px) that matches the old ttk padding.
BUTTON_ROLES = ("ghost", "accent", "side")

BUTTON_HEIGHTS = {"ghost": 30, "accent": 32, "side": 36}
BUTTON_CORNER_RADIUS = 4


def ctk_button_kwargs(role: str = "ghost", bg_color: str | None = None) -> dict:
    """Constructor options for a CTkButton in the given role.

    `bg_color` is the colour of the widget's parent: CustomTkinter paints a
    rectangular background behind every widget, and without this the widget
    shows a mismatched patch of the default appearance colour.
    """
    if role not in BUTTON_ROLES:
        raise ValueError(f"unknown button role: {role!r}")
    if role == "accent":
        options = {
            "fg_color": BTN_ACCENT_BG,
            "hover_color": BTN_ACCENT_HOVER,
            "text_color": BTN_ACCENT_FG,
            "text_color_disabled": BTN_ACCENT_FG,
            "border_width": 0,
        }
    elif role == "side":
        options = {
            "fg_color": BTN_SIDE_BG,
            "hover_color": BTN_SIDE_HOVER,
            "text_color": BTN_SIDE_FG,
            "text_color_disabled": BTN_SIDE_FG,
            "border_width": 0,
        }
    else:
        options = {
            "fg_color": BTN_GHOST_BG,
            "hover_color": BTN_GHOST_HOVER,
            "text_color": BTN_GHOST_FG,
            "text_color_disabled": FG_DIM,
            "border_width": 1,
            "border_color": BTN_GHOST_BORDER,
        }
    options["height"] = BUTTON_HEIGHTS[role]
    options["corner_radius"] = BUTTON_CORNER_RADIUS
    options["font"] = FONT_BODY
    if bg_color:
        options["bg_color"] = bg_color
    return options


def ctk_side_button_kwargs(bg_color: str | None = None) -> dict:
    """CTkButton options for a sidebar navigation button (left aligned).

    Nav buttons are pill-shaped, transparent while inactive and left
    aligned; `show_page()` highlights the active item with a solid
    background and a neon-cyan icon + label.
    """
    options = ctk_button_kwargs("side", bg_color)
    options["anchor"] = "w"
    options["corner_radius"] = 8
    return options


def ctk_card_kwargs(bg_color: str | None = None) -> dict:
    """Constructor options for a rounded dashboard card (CTkFrame).

    Cards are the visual unit of the new dashboard: slightly lighter than
    the main background, a 12 px corner radius and a hairline border from
    the theme.
    """
    options = {
        "fg_color": BG_CARD,
        "border_color": BORDER,
        "border_width": 1,
        "corner_radius": 12,
    }
    if bg_color:
        options["bg_color"] = bg_color
    return options


def make_card(master, bg_color: str | None = None, **kwargs):
    """Create a themed rounded card (CTkFrame) that can be re-coloured on a
    theme switch."""
    options = ctk_card_kwargs(bg_color)
    options.update(kwargs)
    card = ctk.CTkFrame(master, **options)
    setattr(card, _THEME_TAG, ("card", None, bg_color))
    return card


def ctk_entry_kwargs(bg_color: str | None = None) -> dict:
    """Constructor options for a CTkEntry."""
    options = {
        "fg_color": ENTRY_BG,
        "border_color": ENTRY_BORDER,
        "text_color": ENTRY_FG,
        "corner_radius": BUTTON_CORNER_RADIUS,
        "border_width": 1,
        "height": 28,
        "font": FONT_BODY,
    }
    if bg_color:
        options["bg_color"] = bg_color
    return options


def ctk_progress_kwargs(bg_color: str | None = None) -> dict:
    """Constructor options for a CTkProgressBar (trough + fill colours)."""
    options = {
        "fg_color": PROGRESS_BG,
        "progress_color": PROGRESS_FG,
        "border_color": PROGRESS_BG,
        "border_width": 0,
        "height": 10,
        "corner_radius": 4,
    }
    if bg_color:
        options["bg_color"] = bg_color
    return options


# ---------------------------------------------------------------------------
# Widget factories
#
# The created widget remembers its role in `_shds_theme`, so the live theme
# toggle can re-apply exactly the same colours (see `retheme_widget`).
# ---------------------------------------------------------------------------

_THEME_TAG = "_shds_theme"


def make_button(master, role: str = "ghost", bg_color: str | None = None, **kwargs):
    """Create a themed CTkButton that can be re-coloured on a theme switch."""
    options = ctk_button_kwargs(role, bg_color)
    options.update(kwargs)
    button = ctk.CTkButton(master, **options)
    setattr(button, _THEME_TAG, ("button", role, bg_color))
    return button


def make_side_button(master, bg_color: str | None = None, **kwargs):
    """Create a themed sidebar navigation button (left aligned)."""
    options = ctk_side_button_kwargs(bg_color)
    options.update(kwargs)
    button = ctk.CTkButton(master, **options)
    setattr(button, _THEME_TAG, ("side_button", "side", bg_color))
    return button


def make_entry(master, bg_color: str | None = None, **kwargs):
    """Create a themed CTkEntry that can be re-coloured on a theme switch."""
    options = ctk_entry_kwargs(bg_color)
    options.update(kwargs)
    entry = ctk.CTkEntry(master, **options)
    setattr(entry, _THEME_TAG, ("entry", None, bg_color))
    return entry


def ctk_switch_kwargs(bg_color: str | None = None) -> dict:
    """Constructor options for a CTkSwitch (cyan when on, muted when off)."""
    options = {
        "fg_color": PROGRESS_BG,            # off track
        "progress_color": NEON_CYAN,        # on fill
        "button_color": FG_PRIMARY,
        "button_hover_color": NEON_CYAN,
        "text_color": FG_PRIMARY,
        "text_color_disabled": FG_DIM,
        "font": FONT_BODY,
    }
    if bg_color:
        options["bg_color"] = bg_color
    return options


def ctk_menu_kwargs(bg_color: str | None = None) -> dict:
    """Constructor options for a CTkOptionMenu (dropdown)."""
    options = {
        "fg_color": BTN_GHOST_BG,               # field
        "button_color": BTN_GHOST_BORDER,       # arrow side
        "button_hover_color": BTN_GHOST_HOVER,
        "text_color": FG_PRIMARY,
        "text_color_disabled": FG_DIM,
        "dropdown_fg_color": BG_CARD,
        "dropdown_hover_color": BTN_SIDE_ACTIVE_BG,
        "dropdown_text_color": FG_PRIMARY,
        "corner_radius": BUTTON_CORNER_RADIUS,
        "height": 28,
        "font": FONT_BODY,
    }
    if bg_color:
        options["bg_color"] = bg_color
    return options


def make_switch(master, bg_color: str | None = None, **kwargs):
    """Create a themed CTkSwitch that can be re-coloured on a theme switch."""
    options = ctk_switch_kwargs(bg_color)
    options.update(kwargs)
    switch = ctk.CTkSwitch(master, **options)
    setattr(switch, _THEME_TAG, ("switch", None, bg_color))
    return switch


def make_menu(master, bg_color: str | None = None, **kwargs):
    """Create a themed CTkOptionMenu that can be re-coloured on a theme switch."""
    options = ctk_menu_kwargs(bg_color)
    options.update(kwargs)
    menu = ctk.CTkOptionMenu(master, **options)
    setattr(menu, _THEME_TAG, ("menu", None, bg_color))
    return menu


def retheme_widget(widget) -> bool:
    """Re-apply the current palette to one widget created by the factories.

    Returns True when the widget was re-coloured. Widgets that were not
    created by the factories are left untouched.
    """
    info = getattr(widget, _THEME_TAG, None)
    if not info:
        return False
    kind, role, bg_color = info
    if kind == "button":
        options = ctk_button_kwargs(role, bg_color)
    elif kind == "side_button":
        options = ctk_side_button_kwargs(bg_color)
    elif kind == "card":
        options = ctk_card_kwargs(bg_color)
    elif kind == "entry":
        options = ctk_entry_kwargs(bg_color)
    elif kind == "switch":
        options = ctk_switch_kwargs(bg_color)
    elif kind == "menu":
        options = ctk_menu_kwargs(bg_color)
    elif kind == "progress":
        options = ctk_progress_kwargs(bg_color)
    else:  # pragma: no cover - defensive
        return False
    # The stored `bg_color` is the literal colour recorded at construction
    # (the mode active back then). CustomTkinter widgets keep no live
    # reference to the palette, so that literal would pin the *old* mode's
    # patch colour behind every rounded corner after a toggle. Resolve it
    # through the dark<->light mapping so it always tracks the current mode.
    # The map is FIRST-WINS (setdefault): two palette keys sharing a colour
    # must agree on the mapping — e.g. light dark-mode card/ghost backgrounds
    # are both #1C1C28 -> #FFFFFF. Keep palette values unambiguous, or this
    # resolution picks whichever key comes first.
    if bg_color:
        dark, light = palettes()
        swap: dict[str, str] = {}
        for key in PALETTE_KEYS:
            swap.setdefault(dark[key], light[key])
            swap.setdefault(light[key], dark[key])
        current_bg = swap.get(bg_color, bg_color)
        options["bg_color"] = current_bg
    else:
        current_bg = bg_color
    # Only re-apply the colour options; geometry stays as constructed.
    colour_keys = ("fg_color", "hover_color", "text_color",
                   "text_color_disabled", "border_color", "border_width",
                   "button_color", "button_hover_color",
                   "dropdown_fg_color", "dropdown_hover_color",
                   "dropdown_text_color", "progress_color", "bg_color")
    changes = {key: value for key, value in options.items() if key in colour_keys}
    try:
        widget.configure(**changes)
    except Exception:  # noqa: BLE001 - a re-theme must never break a toggle
        return False
    # Remember the resolved colours, so the next toggle maps them again.
    setattr(widget, _THEME_TAG, (kind, role, current_bg))
    return True


# Start in the dark-neon default.
_apply_palette(_DARK_PALETTE)

FONT_FAMILY = _default_font_family()
FONT_TITLE = (FONT_FAMILY, 16, "bold")
FONT_HEADING = (FONT_FAMILY, 11, "bold")
FONT_BODY = (FONT_FAMILY, 10)
FONT_SMALL = (FONT_FAMILY, 9)
FONT_MONO = (_default_mono_family(), 9)

# Dashboard cards. The dashboard computes its per-card width from the window
# size at runtime; these constants are only fallbacks used before the first
# <Configure> event arrives. CARD_MIN_WIDTH must fit five cards plus padding
# inside the main column at the minimum window size (960 - 210 sidebar).
CARD_MIN_WIDTH = 136
CARD_HEIGHT = 150


"""Small shared utilities: formatting, validation, platform helpers."""

import math
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


"""GPU detection: NVIDIA (nvidia-smi), AMD (rocm-smi), Intel (intel_gpu_top).

Optional tools, never fabricated. All queries use explicit argv + shell=False
+ a timeout and never raise: failures degrade to unavailable/unsupported.
"""

import json
import logging
import subprocess
from dataclasses import dataclass, field


log_gpu = logging.getLogger("shds.gpu")

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
        log_gpu.info("nvidia-smi not found")
        return status
    status.tool_found = True
    argv = [
        exe,
        "--query-gpu=index,name,utilization.gpu,temperature.gpu,memory.used,memory.total,fan.speed,power.draw",
        "--format=csv,noheader,nounits",
    ]
    try:
        proc = subprocess.run(argv, shell=False, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except FileNotFoundError:
        status.message = "nvidia-smi executable disappeared."
        return status
    except subprocess.TimeoutExpired:
        status.message = f"nvidia-smi timed out after {timeout}s."
        log_gpu.warning("nvidia-smi timeout")
        return status
    except OSError as exc:
        status.message = f"Could not run nvidia-smi: {exc}"
        log_gpu.warning("nvidia-smi OSError: %s", exc)
        return status
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()[:300]
        status.message = f"nvidia-smi failed (exit {proc.returncode}): {err or 'no details'}"
        log_gpu.warning("nvidia-smi exit %s: %s", proc.returncode, err)
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
        log_gpu.info("rocm-smi not found")
        return status
    status.tool_found = True
    argv = [exe, "--showtemp", "--showuse", "--showmemuse", "--json"]
    try:
        proc = subprocess.run(argv, shell=False, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except FileNotFoundError:
        status.message = "rocm-smi executable disappeared."
        return status
    except subprocess.TimeoutExpired:
        status.message = f"rocm-smi timed out after {timeout}s."
        log_gpu.warning("rocm-smi timeout")
        return status
    except OSError as exc:
        status.message = f"Could not run rocm-smi: {exc}"
        log_gpu.warning("rocm-smi OSError: %s", exc)
        return status
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()[:300]
        status.message = f"rocm-smi failed (exit {proc.returncode}): {err or 'no details'}"
        log_gpu.warning("rocm-smi exit %s: %s", proc.returncode, err)
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
    a full sample the metrics stay unavailable rather than invented.
    """
    status = GpuStatus()
    exe = which("intel_gpu_top")
    if not exe:
        status.message = "intel_gpu_top not found — Intel GPU metrics unavailable."
        log_gpu.info("intel_gpu_top not found")
        return status
    status.tool_found = True
    argv = [exe, "-J", "-s", "500"]
    stdout = ""
    timed_out = False
    try:
        proc = subprocess.run(argv, shell=False, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        stdout = proc.stdout or ""
        if proc.returncode != 0:
            err = (proc.stderr or "").strip()[:300]
            status.message = (f"intel_gpu_top failed (exit {proc.returncode}): "
                              f"{err or 'no details'}")
            log_gpu.warning("intel_gpu_top exit %s: %s", proc.returncode, err)
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
        log_gpu.warning("intel_gpu_top OSError: %s", exc)
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
            log_gpu.warning("gpu probe %s crashed: %s", name, exc)
            tried.append(f"{name} (error: {exc})")
            continue
        if status.supported and status.gpus:
            return status
        tried.append(f"{name} ({status.message[:80]})")
    return GpuStatus(message="No supported GPU metrics available. Tried: "
                     + "; ".join(tried))


"""Hardware snapshot collection (psutil-based, fully defensive)."""

import socket
import threading
import time

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:  # optional dependency
    psutil = None  # type: ignore
    HAS_PSUTIL = False


log_hardware = logging.getLogger("shds.hardware")


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
    hostname: str = ""
    os_info: str = ""
    timestamp: float = 0.0


_prev_net: dict[str, float] = {"sent": 0, "recv": 0, "t": 0.0}
_net_lock = threading.Lock()
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
        log_hardware.info("network counter reset detected (previous=%s current=%s); "
                 "rate unavailable for this sample", previous, current)
        return None
    return (current - previous) * 8.0 / dt


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
            log_hardware.debug("per-core cpu failed: %s", exc)
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
        log_hardware.warning("cpu collect failed: %s", exc)
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
        log_hardware.warning("memory collect failed: %s", exc)
    return snap


def collect_disks() -> list[DiskEntry]:
    entries: list[DiskEntry] = []
    if not HAS_PSUTIL:
        return entries
    try:
        parts = psutil.disk_partitions(all=False)
    except Exception as exc:
        log_hardware.warning("disk_partitions failed: %s", exc)
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
            log_hardware.debug("disk %s permission denied", part.mountpoint)
        except (FileNotFoundError, OSError) as exc:
            entry.error = f"Inaccessible: {exc}"
            log_hardware.debug("disk %s inaccessible: %s", part.mountpoint, exc)
        except Exception as exc:
            entry.error = str(exc)
            log_hardware.warning("disk %s failed: %s", part.mountpoint, exc)
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
        log_hardware.warning("network collect failed: %s", exc)
    return snap


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
            log_hardware.debug("sensors_temperatures failed: %s", exc)
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
                log_hardware.debug("process row failed: %s", exc)
                continue
    except Exception as exc:
        log_hardware.warning("process_iter failed: %s", exc)
        return out
    key = (lambda e: e.mem_mb) if sort_by == "mem" else (lambda e: e.cpu)
    procs.sort(key=key, reverse=True)
    return procs[: max(1, limit)]


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
        hostname=hostname,
        os_info=f"{platform.system()} {platform.release()} ({platform.version()})",
        timestamp=time.time(),
    )
    if include_gpu:
        try:
            snap.gpu = query_gpus()
        except Exception as exc:  # never let GPU break the snapshot
            log_hardware.warning("gpu query failed: %s", exc)
            snap.gpu = GpuStatus(message=f"GPU query failed: {exc}")
    return snap


"""SMART disk health via smartctl (optional tool, honest degradation)."""



log_smart = logging.getLogger("shds.smart")

SMARTCTL_TIMEOUT = 8  # seconds


@dataclass
class SmartDevice:
    """Single disk health reading; missing details stay empty."""

    device: str = ""             # device path as smartctl reported it
    model: str = ""              # best-effort model name (may be "")
    status: str = "unavailable"  # "PASS" | "FAIL" | "UNAVAILABLE"
    detail: str = ""


@dataclass
class SmartStatus:
    """Overall SMART provider state."""

    supported: bool = False
    tool_found: bool = False
    devices: list[SmartDevice] = field(default_factory=list)
    message: str = "Unavailable"


def scan_argv(exe: str) -> list[str]:
    """argv for a device enumeration run (pure; exported for tests)."""
    return [exe, "--scan"]


def health_argv(exe: str, device: str, dev_type: str | None = None) -> list[str]:
    """argv for one device health query (pure).

    `dev_type` is the "-d <type>" hint smartctl --scan prints for USB
    bridges, NVMe and other devices that need an explicit driver
    ("/dev/sdb -d sat"). Without it the health query fails and the disk is
    reported UNAVAILABLE even though it is perfectly healthy.
    """
    argv = [exe]
    if dev_type:
        argv += ["-d", dev_type]
    return argv + ["--health", device]


def classify_smart_exit(code: int) -> str:
    """Map a smartctl exit code to PASS/FAIL/UNAVAILABLE.

    smartctl exits with a bitfield, and this mapping is deliberately coarse:

    - bit 3 (value 8) means "SMART health of the device failed" -> "FAIL"
      (other set bits alongside 8 are checksum/attribute details, not
      stronger verdicts, so 8|1 -> "FAIL" too);
    - exit code 0 means the device passed -> "PASS";
    - anything else (command line errors, permission problems, unsupported
      device, firmware quirks) means no health verdict was obtained and is
      reported honestly as "UNAVAILABLE" instead of guessed.
    """
    if code & 8:
        return "FAIL"
    if code == 0:
        return "PASS"
    return "UNAVAILABLE"


def _first_line(text: str | None) -> str:
    """First non-empty line of text, trimmed ("" for empty text)."""
    for line in (text or "").splitlines():
        clean = line.strip()
        if clean:
            return clean
    return ""


def _device_of(line: str) -> str:
    """Device path token from one smartctl --scan row ("" for junk lines).

    smartctl always reports POSIX-style device paths, so the token containing
    "/dev/" wins; when uncertain, old-style indexed rows ("# 0  <dev> ...")
    fall back to their third whitespace token. Every other line is skipped.
    """
    tokens = (line or "").split()
    for token in tokens:
        if "/dev/" in token:
            return token.rstrip(",;")
    if len(tokens) >= 3 and tokens[0] == "#" and tokens[1].isdigit():
        candidate = tokens[2].rstrip(",;")
        if candidate and not candidate.startswith("#"):
            return candidate
    return ""


def _device_type_of(line: str) -> str:
    """The "-d <type>" driver hint from one smartctl --scan row ("" if absent).

    smartctl --scan prints rows such as
    "/dev/sdb -d sat # /dev/sdb [SAT], ATA device" — the type is required
    for USB bridges and NVMe devices.
    """
    tokens = (line or "").split()
    for index, token in enumerate(tokens):
        if token == "-d" and index + 1 < len(tokens):
            candidate = tokens[index + 1].rstrip(",;")
            if candidate and not candidate.startswith("#"):
                return candidate
    return ""


def _model_of(text: str | None) -> str:
    """Device model name from smartctl output.

    smartctl prints "Model Family" *before* "Device Model", so the first line
    mentioning "model" is usually the generic family string. Prefer the real
    device model and fall back to the family only when it is all we have.
    """
    family = ""
    for line in (text or "").splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key = key.strip().lower()
        value = value.strip()
        if not value:
            continue
        if key == "device model":
            return value[:80]
        if key == "model family" and not family:
            family = value[:80]
    return family


def _health_check(exe: str, device: str, timeout: int,
                  dev_type: str | None = None) -> SmartDevice:
    """Run one health query; a failure degrades that device only."""
    row = SmartDevice(device=device, status="UNAVAILABLE")
    try:
        proc = subprocess.run(health_argv(exe, device, dev_type), shell=False,
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except FileNotFoundError:
        row.detail = "smartctl executable disappeared."
    except subprocess.TimeoutExpired:
        row.detail = f"smartctl timed out after {timeout}s."
    except OSError as exc:
        row.detail = f"Could not run smartctl: {exc}"
    else:
        row.status = classify_smart_exit(proc.returncode)
        row.model = _model_of(proc.stdout)
        detail = _first_line(proc.stderr) or _first_line(proc.stdout)
        row.detail = detail[:200]
    return row


def query_smart_devices(timeout: int = SMARTCTL_TIMEOUT,
                        max_devices: int = 8) -> SmartStatus:
    """Enumerate disks and run one health query each; never raises.

    smartctl (from the smartmontools package) is optional: when it is not
    installed the status honestly reports the tool as missing instead of
    inventing health data.
    """
    status = SmartStatus()
    exe = which("smartctl")
    if not exe:
        status.message = ("smartctl not found — install smartmontools to enable "
                          "disk SMART checks.")
        log_smart.info("smartctl not found")
        return status
    status.tool_found = True
    try:
        proc = subprocess.run(scan_argv(exe), shell=False, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except FileNotFoundError:
        status.message = "smartctl executable disappeared."
        return status
    except subprocess.TimeoutExpired:
        status.message = f"smartctl --scan timed out after {timeout}s."
        log_smart.warning("smartctl --scan timeout")
        return status
    except OSError as exc:
        status.message = f"Could not run smartctl: {exc}"
        log_smart.warning("smartctl OSError: %s", exc)
        return status
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()[:300]
        status.message = f"smartctl --scan failed (exit {proc.returncode}): {err or 'no details'}"
        log_smart.warning("smartctl --scan exit %s: %s", proc.returncode, err)
        return status
    devices: list[tuple[str, str]] = []
    for line in (proc.stdout or "").splitlines():
        device = _device_of(line)
        if device:
            devices.append((device, _device_type_of(line)))
    if not devices:
        status.message = "smartctl reported no usable devices."
        return status
    for device, dev_type in devices[: max(0, max_devices)]:
        try:
            row = _health_check(exe, device, timeout, dev_type)
        except Exception as exc:  # belt and braces: the loop never aborts
            log_smart.warning("smartctl health check crashed for %s: %s", device, exc)
            row = SmartDevice(device=device, status="UNAVAILABLE", detail=str(exc))
        status.devices.append(row)
    passed = sum(1 for d in status.devices if d.status == "PASS")
    failed = sum(1 for d in status.devices if d.status == "FAIL")
    status.supported = bool(status.devices)
    status.message = (f"{len(status.devices)} device(s) checked: "
                      f"{passed} PASS / {failed} FAIL.")
    return status


"""Per-process network connections via psutil (read-only)."""

from typing import Iterable

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:  # optional dependency
    psutil = None  # type: ignore
    HAS_PSUTIL = False

log_netview = logging.getLogger("shds.netview")

_SOCKET_KINDS = {
    socket.SOCK_STREAM: "TCP",
    socket.SOCK_DGRAM: "UDP",
}


@dataclass
class ConnRow:
    """One socket row; pid 0 / empty process name when the owner is unknown."""

    kind: str = ""      # "TCP" | "UDP" | other socket type name
    local: str = ""     # "ip:port" formatted
    remote: str = ""    # "ip:port" or "-" for listening sockets
    status: str = ""    # e.g. "LISTEN", "ESTABLISHED", "NONE"
    pid: int = 0
    process: str = ""   # process name or "" when unknown


def _format_addr(addr) -> str:
    """Format a psutil address tuple as "ip:port" ("-" when unknown).

    Dual-stack tuples (ip, port, flowinfo, scopeid) use their first two
    elements. An IPv6 ip (detected via ":" in the ip) is wrapped in brackets
    so the port stays readable, e.g. "[::1]:8080". Port 0 is shown as 0.
    """
    if not addr or len(addr) < 2:
        return "-"
    ip_text = str(addr[0] or "")
    if not ip_text:
        return "-"
    try:
        port = int(addr[1])
    except (TypeError, ValueError):
        return "-"
    if ":" in ip_text:
        return f"[{ip_text}]:{port}"
    return f"{ip_text}:{port}"


def _socket_kind(value) -> str:
    """Human readable socket type ("TCP"/"UDP" or the raw type as text)."""
    try:
        return _SOCKET_KINDS.get(int(value), str(value))
    except (TypeError, ValueError):
        return str(value)


def _addr_port(addr) -> int:
    """The port part of a psutil address tuple (-1 when unusable)."""
    if not addr or len(addr) < 2:
        return -1
    try:
        return int(addr[1])
    except (TypeError, ValueError):
        return -1


def _conn_row(c, names: dict[int, str]) -> ConnRow:
    """One ConnRow from a psutil connection entry (never signals)."""
    pid = int(getattr(c, "pid", 0) or 0)
    return ConnRow(
        kind=_socket_kind(getattr(c, "type", 0)),
        local=_format_addr(getattr(c, "laddr", None)),
        remote=_format_addr(getattr(c, "raddr", None)),
        status=str(getattr(c, "status", "") or ""),
        pid=pid,
        process=names.get(pid, ""),
    )


def _sort_key(row: ConnRow) -> tuple[int, int, str]:
    """TCP ESTABLISHED first, then other TCP, then UDP; then by pid."""
    if row.kind == "TCP" and row.status == "ESTABLISHED":
        group = 0
    elif row.kind == "TCP":
        group = 1
    else:
        group = 2
    return (group, row.pid, row.local)


def _name_for(pid: int) -> str:
    """Name of one process or "" — best-effort, never signals anything."""
    try:
        return str(psutil.Process(pid).name() or "")[:60]
    except Exception as exc:  # NoSuchProcess, AccessDenied, anything else
        log_netview.debug("pid %s name lookup failed: %s", pid, exc)
        return ""


def _pid_names(pids: Iterable[int]) -> dict[int, str]:
    """pid -> process name map; only pids above 0 are candidates."""
    names: dict[int, str] = {}
    for pid in sorted({p for p in pids if p > 0}):
        names[pid] = _name_for(pid)
    return names


def collect_connections(limit: int = 200) -> tuple[list[ConnRow], str]:
    """Snapshot the connection table with owning process names.

    Read-only and never raises: rows are built from psutil's connection
    table plus process command names; nothing is terminated, signalled, or
    otherwise touched. All failures degrade to an empty list and a message.
    """
    if not HAS_PSUTIL:
        return [], "psutil not installed"
    try:
        conns = psutil.net_connections(kind="inet")
    except psutil.AccessDenied:
        return [], "connection table requires elevated privileges on this system"
    except Exception as exc:
        log_netview.warning("net_connections failed: %s", exc)
        return [], f"could not read connection table: {exc}"
    names = _pid_names(getattr(c, "pid", 0) or 0 for c in conns)
    rows: list[ConnRow] = []
    for c in conns:
        try:
            rows.append(_conn_row(c, names))
        except Exception as exc:
            log_netview.debug("connection row skipped: %s", exc)
    rows.sort(key=_sort_key)
    try:
        cap = int(limit)
    except (TypeError, ValueError):
        cap = 200
    shown = rows[: max(0, cap)]
    if not shown:
        return [], "no connections reported."
    if len(shown) < len(rows):
        return shown, f"showing first {len(shown)} of {len(rows)} connections"
    return shown, f"{len(shown)} connection(s) listed."


"""OS startup-entry enumeration (read-only, cross-OS, fully defensive).

Nothing here disables, deletes, or modifies any startup item — the scanner
only reports what it finds. Every public function is safe to call from a
background thread and never raises: failures degrade into notes.
"""

import os
import plistlib
from collections.abc import Callable

try:
    import winreg
    HAS_WINREG = True
except ImportError:  # Windows-only stdlib module
    winreg = None  # type: ignore
    HAS_WINREG = False

log_startup = logging.getLogger("shds.startup")

_RUN_SUBKEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUNONCE_SUBKEY = r"Software\Microsoft\Windows\CurrentVersion\RunOnce"
_DESKTOP_ENTRY_SECTION = "Desktop Entry"

# Display order and labels for the per-kind counts in the summary note.
_KIND_ORDER = ("registry", "autostart", "launchagent", "startup-folder")
_KIND_LABELS = {
    "registry": "registry",
    "autostart": "autostart",
    "launchagent": "launchagent",
    "startup-folder": "folder",
}


@dataclass
class StartupEntry:
    """One startup location found on the OS (reported, never modified)."""

    name: str = ""      # human label
    command: str = ""   # what is launched (path or Exec value)
    source: str = ""    # where it was found: registry key path or file path
    kind: str = ""      # "registry" | "startup-folder" | "autostart" | "launchagent"
    note: str = ""      # extra info (e.g. "hidden" from a desktop file)


def _win_registry_entries() -> tuple[list[StartupEntry], str]:
    """Read Run/RunOnce values under HKCU and HKLM (strictly read-only).

    Never raises: unreadable keys degrade to a note, missing keys are normal.
    """
    if not HAS_WINREG:
        return [], "winreg unavailable (not Windows) — registry entries skipped."
    entries: list[StartupEntry] = []
    errors: list[str] = []
    hives = (
        ("HKCU", winreg.HKEY_CURRENT_USER),
        ("HKLM", winreg.HKEY_LOCAL_MACHINE),
    )
    for hive_name, hive in hives:
        for subkey in (_RUN_SUBKEY, _RUNONCE_SUBKEY):
            full = f"{hive_name}\\{subkey}"
            # Read BOTH registry views: on 64-bit Python the default view is
            # the 64-bit one, so autoruns installed by 32-bit applications
            # (Wow6432Node) would otherwise never be listed.
            views = [(0, "")]
            for flag, label in ((getattr(winreg, "KEY_WOW64_64KEY", 0), " (64-bit)"),
                                (getattr(winreg, "KEY_WOW64_32KEY", 0), " (32-bit)")):
                if flag:
                    views.append((flag, label))
            for view_flag, view_label in views:
                try:
                    with winreg.OpenKey(hive, subkey, 0,
                                        winreg.KEY_READ | view_flag) as key:
                        index = 0
                        while True:
                            try:
                                name, command, _vtype = winreg.EnumValue(key, index)
                            except OSError:
                                break
                            index += 1
                            entries.append(StartupEntry(
                                name=str(name or ""),
                                command=str(command or "").strip(),
                                source=full + view_label,
                                kind="registry",
                            ))
                except FileNotFoundError:
                    continue  # Run/RunOnce absent for this hive — normal
                except OSError as exc:
                    errors.append(f"{full}{view_label}: {exc}")
                    log_startup.info("cannot read registry key %s: %s", full, exc)
    return entries, "; ".join(errors)


def _win_startup_folder_entries() -> tuple[list[StartupEntry], str]:
    """List files in the per-user Start Menu Startup folder (read-only).

    Never raises: a missing APPDATA variable or an unreadable folder degrades
    to a note; a folder that simply does not exist is not an error.
    """
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return [], "APPDATA environment variable not set — startup folder skipped."
    folder = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    try:
        paths = sorted(folder.iterdir())
    except FileNotFoundError:
        return [], ""  # folder does not exist yet — nothing to report
    except OSError as exc:
        return [], f"Cannot read startup folder {folder}: {exc}"
    entries: list[StartupEntry] = []
    for path in paths:
        try:
            if path.is_symlink() or not path.is_file():
                continue
        except OSError:
            continue
        entries.append(StartupEntry(
            name=path.stem,
            command=path.name,
            source=str(path),
            kind="startup-folder",
        ))
    return entries, ""


def _parse_desktop_file(path: Path) -> dict[str, str] | None:
    """Minimal forgiving parse of a .desktop file's [Desktop Entry] section.

    Handles a UTF-8 BOM, skips comments/blank lines and any other section,
    and keeps the first value per key. Returns None for unreadable or
    malformed files (no [Desktop Entry] header at all).
    """
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        log_startup.debug("cannot read desktop file %s: %s", path, exc)
        return None
    keys: dict[str, str] = {}
    section: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        if section != _DESKTOP_ENTRY_SECTION or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key:
            keys.setdefault(key, value.strip())
    if not keys:
        return None
    return keys


def _autostart_entries(directories: list[Path]) -> list[StartupEntry]:
    """Parse *.desktop files in the given XDG autostart dirs (read-only).

    Missing or unreadable directories simply contribute no entries; malformed
    desktop files are skipped with a debug log. Never raises.
    """
    entries: list[StartupEntry] = []
    for directory in directories:
        try:
            paths = sorted(directory.glob("*.desktop"))
        except OSError as exc:
            log_startup.debug("cannot list autostart dir %s: %s", directory, exc)
            continue
        for path in paths:
            try:
                if not path.is_file():
                    continue
            except OSError:
                continue
            keys = _parse_desktop_file(path)
            if keys is None:
                log_startup.debug("ignoring malformed desktop file: %s", path)
                continue
            note = ""
            if str(keys.get("Hidden", "")).strip().lower() == "true":
                note = "hidden"
            elif str(keys.get("NoDisplay", "")).strip().lower() == "true":
                note = "not shown"
            entries.append(StartupEntry(
                name=keys.get("Name") or path.stem,
                command=keys.get("Exec", ""),
                source=str(path),
                kind="autostart",
                note=note,
            ))
    return entries


def _launchagent_entries(directories: list[Path]) -> list[StartupEntry]:
    """Parse *.plist LaunchAgents in the given directories (read-only).

    Missing directories contribute nothing; malformed or non-dict plists are
    skipped with a debug log. Never raises.
    """
    entries: list[StartupEntry] = []
    for directory in directories:
        try:
            paths = sorted(directory.glob("*.plist"))
        except OSError as exc:
            log_startup.debug("cannot list LaunchAgents dir %s: %s", directory, exc)
            continue
        for path in paths:
            try:
                if not path.is_file():
                    continue
                with path.open("rb") as handle:
                    data = plistlib.load(handle)
            except Exception as exc:  # forgiving: one bad plist must not stop the scan
                log_startup.debug("ignoring malformed plist %s: %s", path, exc)
                continue
            if not isinstance(data, dict):
                log_startup.debug("ignoring non-dict plist: %s", path)
                continue
            label = data.get("Label")
            if isinstance(label, str) and label.strip():
                name = label.strip()
            else:
                name = path.stem
            program_args = data.get("ProgramArguments")
            if isinstance(program_args, list) and program_args:
                command = " ".join(str(part) for part in program_args)
            else:
                program = data.get("Program")
                command = program if isinstance(program, str) else ""
            entries.append(StartupEntry(
                name=name,
                command=command,
                source=str(path),
                kind="launchagent",
            ))
    return entries


def user_startup_dirs() -> list[Path]:
    """Platform-appropriate autostart directories (pure-ish, creates nothing).

    Candidate paths are returned regardless of existence — enumeration skips
    missing/unreadable dirs on its own, and this function never touches the
    filesystem beyond expanding "~". On Windows the startup sources live in
    the registry and the Start Menu folder, so no directories apply.
    """
    try:
        home = os.path.expanduser("~")
    except (OSError, RuntimeError) as exc:
        log_startup.warning("cannot expand home directory: %s", exc)
        return []
    if sys.platform == "win32":
        return []
    if sys.platform == "darwin":
        candidates = [
            Path(home) / "Library" / "LaunchAgents",
            Path("/Library/LaunchAgents"),
        ]
    else:
        candidates = [
            Path(home) / ".config" / "autostart",
            Path("/etc/xdg/autostart"),
        ]
    return candidates


def _guard_source(
    fn: Callable[[], tuple[list[StartupEntry], str]],
    label: str,
) -> tuple[list[StartupEntry], str]:
    """Run one platform source, converting a surprise failure into a note."""
    try:
        return fn()
    except Exception as exc:
        log_startup.warning("%s enumeration failed unexpectedly: %s", label, exc)
        return [], f"{label} enumeration failed: {exc}"


def _guard_listing(
    fn: Callable[[], list[StartupEntry]],
    label: str,
) -> tuple[list[StartupEntry], str]:
    """Run one directory-based source, converting a failure into a note."""
    try:
        return fn(), ""
    except Exception as exc:
        log_startup.warning("%s enumeration failed unexpectedly: %s", label, exc)
        return [], f"{label} enumeration failed: {exc}"


def collect_startup_entries() -> tuple[list[StartupEntry], str]:
    """Enumerate OS startup entries across all sources for this platform.

    Strictly read-only — nothing is disabled, deleted, or modified. Sources
    degrade independently, so one failing source never hides the others.
    Never raises; the note summarises per-kind counts and, when applicable,
    any source-level errors.
    """
    sources: list[tuple[list[StartupEntry], str]] = []
    try:
        if sys.platform == "win32":
            sources.append(_guard_source(_win_registry_entries, "Windows registry"))
            sources.append(_guard_source(_win_startup_folder_entries, "startup folder"))
        else:
            directories = user_startup_dirs()
            if sys.platform == "darwin":
                sources.append(_guard_listing(
                    lambda: _launchagent_entries(directories), "LaunchAgents"))
            else:
                sources.append(_guard_listing(
                    lambda: _autostart_entries(directories), "autostart"))
    except Exception as exc:  # absolute safety net around the dispatch itself
        log_startup.warning("startup enumeration failed unexpectedly: %s", exc)
        return [], f"Startup enumeration failed: {exc}"

    entries: list[StartupEntry] = []
    errors: list[str] = []
    for source_entries, source_error in sources:
        entries.extend(source_entries)
        if source_error:
            errors.append(source_error)

    if entries:
        counts: dict[str, int] = {}
        for entry in entries:
            counts[entry.kind] = counts.get(entry.kind, 0) + 1
        breakdown = ", ".join(
            f"{counts[kind]} {_KIND_LABELS.get(kind, kind)}"
            for kind in _KIND_ORDER
            if counts.get(kind)
        )
        note = f"{len(entries)} startup entries found ({breakdown})."
    else:
        note = "No startup entries found."
    if errors:
        note += "; source errors: " + "; ".join(errors)
    return entries, note


"""Largest-files finder (read-only, bounded by depth cap and time budget).

The scan never deletes, moves, or writes anything. Directories are only
listed; symlinks (and Windows junctions) are never followed, so symlink
bombs cannot loop the walk. The function is safe to call from a background
thread and never raises: failures degrade into the note string.
"""

import heapq
import itertools

log_filesutil = logging.getLogger("shds.filesutil")

_CHECK_EVERY = 256  # scandir entries between time-budget checks


@dataclass
class BigFile:
    """One large regular file hit: path as given and size in bytes."""

    path: str = ""
    size_bytes: int = 0


def largest_files(
    root: str | Path,
    limit: int = 25,
    time_budget_s: float = 20.0,
    max_depth: int = 8,
) -> tuple[list[BigFile], str]:
    """Return the top-N largest regular files under root, largest first.

    Strictly read-only. Permission/OSError on entering a subdirectory skips
    just that directory (counted in the note). The time budget is checked
    every 256 entries via time.monotonic(); on exhaustion the scan stops and
    the note says it is partial. A file passed as root returns just that
    file. Never raises.
    """
    try:
        limit = max(1, int(limit))
        max_depth = max(0, int(max_depth))
        time_budget_s = float(time_budget_s)
        root_str = os.fspath(root)
    except (TypeError, ValueError) as exc:
        return [], f"Invalid largest_files parameters: {exc}"

    if not root_str:
        return [], "path does not exist (empty path given)"
    try:
        if not os.path.exists(root_str):
            return [], f"path does not exist: {root_str}"
    except OSError as exc:
        return [], f"Cannot inspect path {root_str}: {exc}"

    if os.path.isfile(root_str):
        try:
            size = os.path.getsize(root_str)
        except OSError as exc:
            return [], f"Cannot read file size of {root_str}: {exc}"
        note = f"Root is a single file: {size} byte(s)."
        return [BigFile(path=root_str, size_bytes=size)], note
    if not os.path.isdir(root_str):
        return [], f"Root is neither a directory nor a regular file: {root_str}"

    deadline = time.monotonic() + max(0.0, time_budget_s)
    heap: list[tuple[int, int, str]] = []  # min-heap of (size, seq, path)
    counter = itertools.count()
    entries_seen = 0
    files_seen = 0
    dirs_skipped = 0
    partial = False

    stack: list[tuple[str, int]] = [(root_str, 0)]
    while stack and not partial:
        current, depth = stack.pop()
        try:
            listing = os.scandir(current)
        except OSError as exc:
            if current == root_str:
                return [], f"Cannot read root directory {root_str}: {exc}"
            dirs_skipped += 1
            log_filesutil.debug("skipping directory %s: %s", current, exc)
            continue
        try:
            with listing as it:
                for entry in it:
                    # Budget check every 256 entries (and once at the start).
                    if entries_seen % _CHECK_EVERY == 0 and time.monotonic() >= deadline:
                        partial = True
                        break
                    entries_seen += 1
                    try:
                        if entry.is_symlink():
                            continue
                        is_junction = getattr(entry, "is_junction", None)
                        if callable(is_junction) and is_junction():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if depth < max_depth:
                                stack.append((entry.path, depth + 1))
                            continue
                        if entry.is_file(follow_symlinks=False):
                            try:
                                size = entry.stat(follow_symlinks=False).st_size
                            except OSError:
                                continue
                            files_seen += 1
                            if len(heap) < limit:
                                heapq.heappush(heap, (size, next(counter), entry.path))
                            elif size > heap[0][0]:
                                heapq.heapreplace(heap, (size, next(counter), entry.path))
                    except OSError:
                        continue  # one unreadable entry never stops the scan
        except OSError as exc:
            if current == root_str:
                return [], f"Cannot read root directory {root_str}: {exc}"
            dirs_skipped += 1
            log_filesutil.debug("error while listing %s: %s", current, exc)

    results = [BigFile(path=path_str, size_bytes=size)
               for size, _seq, path_str in heapq.nlargest(len(heap), heap)]

    summary = [f"{files_seen} file(s) seen"]
    if dirs_skipped:
        summary.append(f"{dirs_skipped} dir(s) skipped (access errors)")
    if partial:
        summary.append(f"partial scan — time budget reached after {files_seen} file(s) seen")
    note = f"Largest-file scan of {root_str}: " + "; ".join(summary) + "."
    return results, note


"""Logging configuration with size-limited rotating logs.

Logs go to a per-user, always-writable location instead of beside the
application package, so a protected install directory cannot block startup.
"""

import logging.handlers
import tempfile

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


"""Modular diagnostic engine with documented, configurable thresholds."""

import platform
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime


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
}


@dataclass
class Finding:
    component: str
    status: str  # PASS | WARNING | FAIL | UNAVAILABLE
    message: str
    detail: str = ""
    key: str = ""  # stable identity for cross-scan comparison (see compare_reports)
    code: str = ""  # machine-readable cause, used by scanner.explainer


@dataclass
class DiagnosticReport:
    started_at: float = 0.0
    finished_at: float = 0.0
    findings: list[Finding] = field(default_factory=list)
    summary: dict[str, int] = field(default_factory=dict)
    recommendations: list[str] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return max(0.0, self.finished_at - self.started_at)


def _status_for(value: float | None, warn: float, fail: float) -> str:
    if value is None:
        return "UNAVAILABLE"
    if value >= fail:
        return "FAIL"
    if value >= warn:
        return "WARNING"
    return "PASS"


# Worse-than ordering used by compare_reports. UNAVAILABLE sits above PASS so a
# sensor dropping out shows up as a change, without claiming it failed.
SEVERITY_RANK = {"PASS": 0, "UNAVAILABLE": 1, "WARNING": 2, "FAIL": 3}


def finding_key(finding: Finding) -> str:
    """Stable key for a finding; falls back to the component name."""
    return finding.key or finding.component


def compare_reports(previous: list[Finding] | None,
                    current: list[Finding]) -> dict:
    """Diff two finding lists by stable key.

    Returns {"regressions": [...], "improvements": [...], "added": [...],
    "removed": [...]} where each entry is
    (key, previous_status, current_status, message). Pass the findings lists
    (not the full reports) so callers can use any report object.
    """
    result: dict[str, list] = {"regressions": [], "improvements": [],
                               "added": [], "removed": []}
    if not previous:
        return result
    prev_by_key = {finding_key(f): f for f in previous}
    cur_by_key = {finding_key(f): f for f in current}

    for key, cur in cur_by_key.items():
        if key not in prev_by_key:
            result["added"].append((key, None, cur.status, cur.message))
            continue
        before = SEVERITY_RANK.get(prev_by_key[key].status, 1)
        after = SEVERITY_RANK.get(cur.status, 1)
        if after > before:
            result["regressions"].append((key, prev_by_key[key].status,
                                          cur.status, cur.message))
        elif after < before:
            result["improvements"].append((key, prev_by_key[key].status,
                                           cur.status, cur.message))

    for key, prev in prev_by_key.items():
        if key not in cur_by_key:
            result["removed"].append((key, prev.status, None, prev.message))
    return result


def diagnose_cpu(snap: HardwareSnapshot, th: dict) -> list[Finding]:
    cpu = snap.cpu
    if not cpu.available or cpu.percent is None:
        return [Finding("CPU", "UNAVAILABLE",
                        "CPU metrics unavailable.",
                        cpu.error or "psutil missing or access denied.",
                        key="CPU", code="CPU_UNAVAILABLE")]
    st = _status_for(cpu.percent, th["cpu_warn"], th["cpu_fail"])
    cores = f"{cpu.physical_cores}P/{cpu.logical_cores}L" if cpu.logical_cores else "n/a"
    msg = f"CPU utilisation {cpu.percent:.1f}% (cores {cores})"
    if st == "PASS":
        msg += " — healthy."
    elif st == "WARNING":
        msg += " — high load; close heavy apps if sustained."
    else:
        msg += " — extreme load; check runaway processes (no damage claimed)."
    return [Finding("CPU", st, msg, key="CPU",
                    code="CPU_HIGH" if st in ("WARNING", "FAIL") else "")]


def diagnose_memory(snap: HardwareSnapshot, th: dict) -> list[Finding]:
    mem = snap.mem
    if not mem.available or mem.percent is None:
        return [Finding("Memory", "UNAVAILABLE",
                        "Memory metrics unavailable.",
                        mem.error or "psutil missing.", key="Memory",
                        code="MEM_UNAVAILABLE")]
    st = _status_for(mem.percent, th["mem_warn"], th["mem_fail"])
    msg = (f"Memory {mem.percent:.1f}% used "
           f"({format_bytes(mem.used)} / {format_bytes(mem.total)})")
    if st == "WARNING":
        msg += " — memory pressure; consider closing tabs/apps."
    elif st == "FAIL":
        msg += " — critically low free memory; save work, close apps."
    else:
        msg += " — healthy."
    return [Finding("Memory", st, msg, key="Memory",
                    code="MEM_HIGH" if st in ("WARNING", "FAIL") else "")]


def diagnose_disks(snap: HardwareSnapshot, th: dict) -> list[Finding]:
    if not snap.disks:
        return [Finding("Storage", "UNAVAILABLE", "No disk partitions detected.",
                        "psutil missing or access denied.", key="Storage",
                        code="STORAGE_UNAVAILABLE")]
    out: list[Finding] = []
    for d in snap.disks:
        if not d.accessible or d.percent is None:
            out.append(Finding("Storage", "UNAVAILABLE",
                               f"{d.mountpoint or d.device}: inaccessible.",
                               d.error or "permission denied",
                               key=f"Storage:{d.mountpoint or d.device}",
                               code="STORAGE_UNAVAILABLE"))
            continue
        st = _status_for(d.percent, th["disk_warn"], th["disk_fail"])
        code = "DISK_FULL" if st == "FAIL" else ("DISK_LOW" if st == "WARNING" else "")
        out.append(Finding("Storage", st,
                           f"{d.mountpoint} {d.percent:.1f}% used ({d.fstype or 'fs'})",
                           f"total={d.total} used={d.used}",
                           key=f"Storage:{d.mountpoint}", code=code))
    return out


def diagnose_gpu(snap: HardwareSnapshot, th: dict) -> list[Finding]:
    g = snap.gpu
    if not g.supported or not g.gpus:
        return [Finding("GPU", "UNAVAILABLE",
                        "No supported GPU metrics available.",
                        g.message, key="GPU")]
    out: list[Finding] = []
    for gpu in g.gpus:
        key = f"GPU:{gpu.index}"
        if gpu.temperature_c is not None:
            st = _status_for(gpu.temperature_c, th["gpu_temp_warn"], th["gpu_temp_fail"])
            out.append(Finding("GPU", st,
                               f"GPU{gpu.index} {gpu.name}: {gpu.temperature_c:.0f}C" +
                               (f", util {gpu.utilization_pct:.0f}%" if gpu.utilization_pct is not None else ""),
                               key=key,
                               code="GPU_HOT" if st in ("WARNING", "FAIL") else ""))
        elif gpu.utilization_pct is not None:
            out.append(Finding("GPU", "PASS",
                               f"GPU{gpu.index} {gpu.name}: util {gpu.utilization_pct:.0f}% (temp unavailable)",
                               key=key))
        else:
            out.append(Finding("GPU", "UNAVAILABLE",
                               f"GPU{gpu.index} {gpu.name}: metrics unavailable.",
                               key=key))
    return out


def diagnose_network(snap: HardwareSnapshot, th: dict) -> list[Finding]:  # noqa: ARG001
    n = snap.net
    if not n.available:
        return [Finding("Network", "UNAVAILABLE", "Network counters unavailable.",
                        n.error or "psutil missing.", key="Network")]
    if not n.interfaces:
        return [Finding("Network", "WARNING", "No network interfaces reported.",
                        key="Network", code="NETWORK_NO_INTERFACES")]
    return [Finding("Network", "PASS",
                    f"{len(n.interfaces)} interface(s): {', '.join(n.interfaces[:6])}",
                    key="Network")]


def diagnose_temps(snap: HardwareSnapshot, th: dict) -> list[Finding]:
    real = [t for t in snap.temps if t.status == "available" and t.current is not None]
    if not real:
        return [Finding("Sensors", "UNAVAILABLE",
                        "No reliable temperature sensors exposed on this machine.",
                        "Unsupported sensors are reported, never invented.")]
    out: list[Finding] = []
    for t in real[:8]:
        st = _status_for(t.current, th["temp_warn"], th["temp_fail"])
        out.append(Finding("Sensors", st, f"{t.label}: {t.current:.1f}C (source: {t.source})",
                           key=f"Sensors:{t.label}"))
    return out


# ---------------------------------------------------------------------------
# Cross-platform OS health checks.
#
# Every check is strictly read-only: subprocess.run() gets an explicit argv,
# shell=False and an 8-second timeout, and nothing is started, stopped or
# modified. Missing/permission-denied/slow commands degrade to UNAVAILABLE
# instead of raising, so a scan always completes.
# ---------------------------------------------------------------------------
OS_CHECK_TIMEOUT_S = 8
OS_INODE_WARN_PCT = 90
OS_INODE_FAIL_PCT = 95
OS_KERNEL_ERRORS_FAIL = 5
OS_SYSTEMD_FAILED_FAIL = 5
OS_LOG_LINES_FAIL = 50
OS_BACKUP_WARN_DAYS = 7
OS_BACKUP_FAIL_DAYS = 30

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_TM_BACKUP_RE = re.compile(r"(\d{4}-\d{2}-\d{2}-\d{6})")


def _run_readonly(argv: list[str],
                  timeout: float = OS_CHECK_TIMEOUT_S) -> tuple[bool, str, str]:
    """Run one read-only command; return ``(ok, stdout, error_text)``.

    Never raises: a missing binary, a permission error or a timeout is
    reported through the error text so callers can degrade to UNAVAILABLE.
    """
    try:
        proc = subprocess.run(
            argv,
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=_CREATE_NO_WINDOW,
        )
    except FileNotFoundError:
        return False, "", f"command not found: {argv[0]}"
    except subprocess.TimeoutExpired:
        return False, "", f"{argv[0]} timed out after {timeout:g}s"
    except UnicodeError as exc:  # defensive: bytes outside the declared codec
        return False, "", f"{argv[0]} produced undecodable output: {exc}"
    except (PermissionError, OSError) as exc:
        return False, "", f"{argv[0]} could not run: {exc}"
    out = proc.stdout or ""
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        suffix = f": {err[:300]}" if err else ""
        return False, out, f"{argv[0]} exited with code {proc.returncode}{suffix}"
    return True, out, ""


def _powershell(script: str) -> tuple[bool, str, str]:
    """Run a read-only PowerShell snippet (explicit argv, never a shell)."""
    return _run_readonly(["powershell", "-NoProfile", "-NonInteractive",
                          "-Command", script])


def _diag_first_line(text: str) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def diagnose_windows_os() -> list[Finding]:
    """Read-only Windows checks: Update service, Defender, System Restore."""
    out: list[Finding] = []

    ok, text, err = _powershell(
        "(Get-Service -Name wuauserv -ErrorAction Stop).Status")
    if not ok:
        out.append(Finding(
            "OS: Windows Update", "UNAVAILABLE",
            "Could not read the Windows Update service status.", err,
            key="OS:WindowsUpdate", code="OS_CHECK_UNAVAILABLE"))
    else:
        status = _diag_first_line(text).lower()
        if status == "running":
            out.append(Finding(
                "OS: Windows Update", "PASS",
                "Windows Update service (wuauserv) is running.",
                key="OS:WindowsUpdate"))
        elif status in ("stopped", "stop pending", "start pending"):
            out.append(Finding(
                "OS: Windows Update", "WARNING",
                "Windows Update service (wuauserv) is stopped — updates may not "
                "install until it starts.",
                "Windows can start this service on demand; open Windows Update "
                "and choose 'Check for updates'.",
                key="OS:WindowsUpdate", code="WINDOWS_UPDATE_STOPPED"))
        else:
            out.append(Finding(
                "OS: Windows Update", "UNAVAILABLE",
                f"Unrecognised Windows Update service status '{status or 'empty'}'.",
                key="OS:WindowsUpdate", code="OS_CHECK_UNKNOWN"))

    ok, text, err = _powershell(
        "(Get-MpComputerStatus -ErrorAction Stop).RealTimeProtectionEnabled")
    value = _diag_first_line(text).lower()
    if not ok or value not in ("true", "false"):
        out.append(Finding(
            "OS: Defender", "UNAVAILABLE",
            "Windows Defender status could not be read.",
            err or f"unexpected output: {value or 'empty'}",
            key="OS:Defender", code="OS_CHECK_UNAVAILABLE"))
    elif value == "false":
        out.append(Finding(
            "OS: Defender", "WARNING",
            "Windows Defender real-time protection is disabled.",
            "A third-party antivirus can also switch this off; confirm in "
            "Windows Security before changing anything.",
            key="OS:Defender", code="DEFENDER_DISABLED"))
    else:
        out.append(Finding(
            "OS: Defender", "PASS",
            "Windows Defender real-time protection is enabled.",
            key="OS:Defender"))

    # DisableSR=1 is the documented policy marker for "System Restore off".
    # A missing value means protection was not disabled this way.
    ok, text, err = _powershell(
        r"(Get-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\Windows NT"
        r"\CurrentVersion\SystemRestore' -Name DisableSR "
        r"-ErrorAction SilentlyContinue).DisableSR")
    if not ok:
        out.append(Finding(
            "OS: System Restore", "UNAVAILABLE",
            "System Restore status could not be read.", err,
            key="OS:SystemRestore", code="OS_CHECK_UNAVAILABLE"))
    elif _diag_first_line(text) == "1":
        out.append(Finding(
            "OS: System Restore", "WARNING",
            "System Restore is turned off (DisableSR=1).",
            "Without restore points there is no quick rollback after a bad "
            "driver or update.",
            key="OS:SystemRestore", code="SYSTEM_RESTORE_OFF"))
    else:
        out.append(Finding(
            "OS: System Restore", "PASS",
            "System Restore is not disabled by policy (DisableSR not set).",
            "Existing restore points are not inspected; this is a policy check.",
            key="OS:SystemRestore"))
    return out


def _kernel_error_lines(text: str) -> list[str]:
    """Pick kernel-log lines that describe real hardware/software errors."""
    hits: list[str] = []
    for raw in (text or "").splitlines():
        low = raw.lower()
        if ("i/o error" in low or "hardware error" in low
                or "machine check" in low or "segfault" in low
                or "ext4-fs error" in low
                or ("xfs (" in low and "error" in low)
                or ("btrfs" in low and ("error" in low or "corrupt" in low))):
            line = raw.strip()
            if line:
                hits.append(line[:300])
    return hits


def _inode_usage(text: str) -> tuple[int, str] | None:
    """Worst inode percentage from ``df -i -P`` output: (percent, mount)."""
    worst: tuple[int, str] | None = None
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) < 6:
            continue
        pct = parts[4].rstrip("%")
        if not pct.isdigit():
            continue  # header row ("IUse%") or non-numeric entry
        value = int(pct)
        mount = " ".join(parts[5:])
        if worst is None or value > worst[0]:
            worst = (value, mount)
    return worst


def _failed_units(text: str) -> list[str]:
    """Unit names from ``systemctl --failed --no-legend`` (read-only)."""
    units: list[str] = []
    for line in (text or "").splitlines():
        line = line.strip().lstrip("●").strip()
        if line:
            units.append(line.split()[0])
    return units


def diagnose_linux_os() -> list[Finding]:
    """Read-only Linux checks: kernel log, inodes, failed systemd units."""
    out: list[Finding] = []

    ok, text, err = _run_readonly(["dmesg"])
    if not ok:
        low = err.lower()
        denied = ("permission" in low or "not permitted" in low)
        out.append(Finding(
            "OS: Kernel Log", "UNAVAILABLE",
            "Kernel log (dmesg) could not be read.",
            err + (" — dmesg is often restricted to root; elevated rights are "
                   "only needed to read it, not to fix it." if denied else ""),
            key="OS:KernelLog",
            code="ACCESS_DENIED" if denied else "OS_CHECK_UNAVAILABLE"))
    else:
        hits = _kernel_error_lines(text)
        if not hits:
            out.append(Finding(
                "OS: Kernel Log", "PASS",
                "No hardware or storage errors found in the kernel log.",
                key="OS:KernelLog"))
        else:
            st = "FAIL" if len(hits) >= OS_KERNEL_ERRORS_FAIL else "WARNING"
            out.append(Finding(
                "OS: Kernel Log", st,
                f"Kernel log contains {len(hits)} hardware/software "
                "error line(s).",
                " | ".join(hits[:5])[:500],
                key="OS:KernelLog", code="KERNEL_HARDWARE_ERROR"))

    ok, text, err = _run_readonly(["df", "-i", "-P"])
    worst = _inode_usage(text) if ok else None
    if not ok or worst is None:
        out.append(Finding(
            "OS: Inodes", "UNAVAILABLE",
            "Inode usage could not be read.", err,
            key="OS:Inodes", code="OS_CHECK_UNAVAILABLE"))
    else:
        pct, mount = worst
        if pct >= OS_INODE_FAIL_PCT:
            st, code = "FAIL", "INODES_CRITICAL"
        elif pct >= OS_INODE_WARN_PCT:
            st, code = "WARNING", "INODES_LOW"
        else:
            st, code = "PASS", ""
        out.append(Finding(
            "OS: Inodes", st,
            f"Highest inode usage {pct}% ({mount}).",
            "Inodes are file metadata records: a volume can have free space "
            "and still be unable to create files when inodes run out.",
            key="OS:Inodes", code=code))

    ok, text, err = _run_readonly(
        ["systemctl", "--failed", "--no-legend", "--no-pager"])
    if not ok:
        out.append(Finding(
            "OS: systemd", "UNAVAILABLE",
            "Failed systemd services could not be listed.", err,
            key="OS:Systemd", code="OS_CHECK_UNAVAILABLE"))
    else:
        units = _failed_units(text)
        if not units:
            out.append(Finding(
                "OS: systemd", "PASS",
                "No failed systemd services.",
                key="OS:Systemd"))
        else:
            st = "FAIL" if len(units) >= OS_SYSTEMD_FAILED_FAIL else "WARNING"
            out.append(Finding(
                "OS: systemd", st,
                f"{len(units)} systemd service(s) have failed.",
                ", ".join(units[:5])[:500],
                key="OS:Systemd", code="SYSTEMD_FAILED"))
    return out


def _macos_log_errors(text: str, sample_limit: int = 3) -> tuple[int, list[str]]:
    """Count non-header lines from ``log show``; keep a few samples."""
    count = 0
    samples: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("Timestamp"):
            continue
        count += 1
        if len(samples) < sample_limit:
            samples.append(line[:240])
    return count, samples


def _diagnose_time_machine() -> Finding:
    """Read-only Time Machine check based on ``tmutil latestbackup``."""
    ok, text, err = _run_readonly(["tmutil", "latestbackup"])
    combined = f"{text}\n{err}".lower()
    if "command not found" in combined:
        return Finding(
            "OS: Time Machine", "UNAVAILABLE",
            "Time Machine's tmutil command is not available.",
            err, key="OS:TimeMachine", code="OS_CHECK_UNAVAILABLE")
    if "no backups" in combined:
        return Finding(
            "OS: Time Machine", "WARNING",
            "Time Machine reports no completed backups.",
            "Backups are not running; open Time Machine settings and choose a "
            "backup disk.",
            key="OS:TimeMachine", code="TIMEMACHINE_NO_BACKUP")
    if not ok:
        return Finding(
            "OS: Time Machine", "UNAVAILABLE",
            "Time Machine backup status could not be read.", err,
            key="OS:TimeMachine", code="OS_CHECK_UNAVAILABLE")
    path = _diag_first_line(text)
    match = _TM_BACKUP_RE.search(path)
    if match is None:
        return Finding(
            "OS: Time Machine", "UNAVAILABLE",
            "Time Machine returned an unrecognised backup path.",
            path[:300], key="OS:TimeMachine", code="OS_CHECK_UNKNOWN")
    try:
        when = datetime.strptime(match.group(1), "%Y-%m-%d-%H%M%S")
    except ValueError:
        return Finding(
            "OS: Time Machine", "UNAVAILABLE",
            "Time Machine backup timestamp could not be parsed.",
            path[:300], key="OS:TimeMachine", code="OS_CHECK_UNKNOWN")
    age_days = max(0.0, (datetime.now() - when).total_seconds() / 86400.0)
    if age_days <= OS_BACKUP_WARN_DAYS:
        return Finding(
            "OS: Time Machine", "PASS",
            f"Last Time Machine backup: {age_days:.1f} day(s) ago.",
            key="OS:TimeMachine")
    st = "FAIL" if age_days > OS_BACKUP_FAIL_DAYS else "WARNING"
    return Finding(
        "OS: Time Machine", st,
        f"Last Time Machine backup is {age_days:.0f} days old.",
        "Backups older than a week are a data-loss risk; check the Time "
        "Machine destination and run a backup.",
        key="OS:TimeMachine", code="TIMEMACHINE_STALE")


def diagnose_macos_os() -> list[Finding]:
    """Read-only macOS checks: recent unified-log errors and Time Machine."""
    out: list[Finding] = []
    ok, text, err = _run_readonly(
        ["log", "show", "--predicate", 'eventMessage contains "error"',
         "--last", "1h"])
    if not ok:
        out.append(Finding(
            "OS: System Log", "UNAVAILABLE",
            "Recent macOS system log errors could not be read.", err,
            key="OS:MacLog", code="OS_CHECK_UNAVAILABLE"))
    else:
        count, samples = _macos_log_errors(text)
        if count == 0:
            out.append(Finding(
                "OS: System Log", "PASS",
                "No 'error' messages in the last hour of the unified log.",
                key="OS:MacLog"))
        else:
            st = "FAIL" if count >= OS_LOG_LINES_FAIL else "WARNING"
            out.append(Finding(
                "OS: System Log", st,
                f"Unified log has {count} line(s) mentioning 'error' in the "
                "last hour.",
                " | ".join(samples)[:500],
                key="OS:MacLog", code="MACOS_LOG_ERRORS"))
    out.append(_diagnose_time_machine())
    return out


def diagnose_os(system: str | None = None) -> list[Finding]:
    """Run the read-only checks for the current (or given) operating system."""
    system = system or platform.system()
    try:
        if system == "Windows":
            return diagnose_windows_os()
        if system == "Linux":
            return diagnose_linux_os()
        if system == "Darwin":
            return diagnose_macos_os()
    except Exception as exc:  # noqa: BLE001 - an OS check must never break a scan
        return [Finding("OS", "UNAVAILABLE", "OS-level health checks failed.",
                        str(exc), key="OS", code="OS_CHECK_ERROR")]
    return [Finding(
        "OS", "UNAVAILABLE",
        f"No OS-level checks are implemented for {system or 'this system'}.",
        "Hardware checks still run normally.", key="OS",
        code="OS_UNSUPPORTED")]


# Weights for the combined System Health score (see system_health_score).
HEALTH_WEIGHTS = {"cpu": 0.30, "mem": 0.25, "disk": 0.25, "os": 0.20}


def _health_metric_score(percent: float) -> float:
    """Turn a usage percentage into a 0..100 'headroom' score."""
    return max(0.0, min(100.0, 100.0 - float(percent)))


def system_health_score(snap: HardwareSnapshot | None,
                        findings: list[Finding] | None = None) -> dict:
    """Combine CPU, RAM, worst disk and OS-level findings into 0..100.

    Weights: CPU 30%, memory 25%, disk 25%, OS-level 20%. Metrics that are
    unavailable are left out and the remaining weights renormalise, so a
    missing sensor never pretends the machine is broken. Pass ``findings=None``
    when no diagnostic scan has run yet: the OS component is then excluded
    (unknown is not unhealthy) and renormalised away as well.

    Only findings whose stable key/component starts with ``OS`` feed the OS
    part: WARNING costs 4 points, FAIL costs 10, capped at 60. UNAVAILABLE
    never lowers the score because a check that could not run proves nothing.
    """
    components: dict[str, float] = {}
    weights: dict[str, float] = {}
    if snap is not None:
        if snap.cpu.available and snap.cpu.percent is not None:
            components["cpu"] = _health_metric_score(snap.cpu.percent)
            weights["cpu"] = HEALTH_WEIGHTS["cpu"]
        if snap.mem.available and snap.mem.percent is not None:
            components["mem"] = _health_metric_score(snap.mem.percent)
            weights["mem"] = HEALTH_WEIGHTS["mem"]
        accessible = [d for d in snap.disks
                      if d.accessible and d.percent is not None]
        if accessible:
            components["disk"] = _health_metric_score(
                max(d.percent for d in accessible if d.percent is not None))
            weights["disk"] = HEALTH_WEIGHTS["disk"]

    # Without a single live metric a "health score" would be invented; report
    # no score instead (the UI shows a dash).
    if not weights:
        return {"total": None, "components": {}, "os_fail": 0, "os_warning": 0}

    os_fail = 0
    os_warning = 0
    if findings is not None:
        os_findings = [f for f in findings
                       if (f.key or f.component).startswith("OS")]
        os_fail = sum(1 for f in os_findings if f.status == "FAIL")
        os_warning = sum(1 for f in os_findings if f.status == "WARNING")
        penalty = min(60.0, os_fail * 10.0 + os_warning * 4.0)
        components["os"] = 100.0 - penalty
        weights["os"] = HEALTH_WEIGHTS["os"]

    result = {
        "total": None,
        "components": {k: round(v, 1) for k, v in components.items()},
        "os_fail": os_fail,
        "os_warning": os_warning,
    }
    if not weights:
        return result
    total = (sum(components[k] * weights[k] for k in weights)
             / sum(weights.values()))
    result["total"] = int(round(max(0.0, min(100.0, total))))
    return result


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


def build_recommendations(findings: list[Finding]) -> list[str]:
    recs: list[str] = []
    # A component can emit several findings (one per GPU, one per volume), so
    # keep the WORST status per component. A plain "last one wins" mapping
    # would let a cool second GPU hide a first GPU that is overheating.
    comps: dict[str, str] = {}
    for f in findings:
        current = comps.get(f.component)
        if current is None or SEVERITY_RANK.get(f.status, 0) > SEVERITY_RANK.get(current, 0):
            comps[f.component] = f.status
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
    if any(f.component.startswith("OS") and f.status in ("WARNING", "FAIL")
           for f in findings):
        recs.append("OS-level check flagged an issue: use the 'How to fix' buttons "
                    "in the Diagnostics tab for a plain-English explanation and the "
                    "exact settings page (or a safe read-only command). This app "
                    "changes nothing by itself.")
    if comps.get("Sensors") == "UNAVAILABLE":
        recs.append("Temperature sensors unsupported here: readings are honestly reported as unavailable, not estimated.")
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


def run_diagnostics(snap: HardwareSnapshot, thresholds: dict | None = None,
                    include_os: bool = True) -> DiagnosticReport:
    """Run all diagnostic rules against a snapshot.

    ``include_os`` controls the read-only OS checks (Windows/Linux/macOS);
    tests that exercise threshold logic can turn them off to stay hermetic.
    """
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
    if include_os:
        findings += diagnose_os()
    summary = {"PASS": 0, "WARNING": 0, "FAIL": 0, "UNAVAILABLE": 0}
    for f in findings:
        summary[f.status] = summary.get(f.status, 0) + 1
    report = DiagnosticReport(
        started_at=started,
        finished_at=time.time(),
        findings=findings,
        summary=summary,
        recommendations=build_recommendations(findings),
    )
    return report


"""Plain-English explanations and safe fixes for diagnostic findings.

The explainer never runs anything. It turns a FAIL/WARNING ``Finding`` into an
:class:`Explanation` whose ``kind`` tells the UI what the "How to fix" button
may do:

* ``uri``     - open a known operating-system settings page (Windows
                ``ms-settings:`` URIs). Opening a page is read-only.
* ``command`` - copy a safe, read-only command to the clipboard so the user
                can inspect the problem and run it manually if they choose.
* ``info``    - show an explanation only; there is no safe automated action.

Matching is code-first (``Finding.code`` from the diagnostics engine) and then
pattern-based, so older or third-party findings still get a useful answer.
"""

from typing import Callable

EXPLAINED_STATUSES = ("FAIL", "WARNING")

_STORAGE_SETTINGS_MAC = 'open "x-apple.systempreferences:com.apple.settings.Storage"'
_MACOS_LOG_COMMAND = ('log show --predicate \'eventMessage contains "error"\' '
                      "--last 1h")


@dataclass(frozen=True)
class Explanation:
    """A single plain-English explanation plus one safe, user-run action."""

    title: str
    explanation: str
    fix: str
    kind: str = "info"  # "uri" | "command" | "info"
    target: str = ""    # settings URI or command to copy


def _platform_name(system: str | None) -> str:
    """Normalise a platform name to "Windows", "Linux" or "Darwin"."""
    text = str(system or "").strip().lower()
    if text.startswith("win"):
        return "Windows"
    if text.startswith("mac") or text in ("darwin", "osx"):
        return "Darwin"
    if text.startswith("linux"):
        return "Linux"
    return str(system or "")


# ---------------------------------------------------------------------------
# Builders. They are intentionally small and pure so the UI can call them on
# the main thread without doing any I/O.
# ---------------------------------------------------------------------------
def _windows_update(_system: str = "") -> Explanation:
    return Explanation(
        "Windows Update service is stopped",
        "Windows Update uses the 'wuauserv' service to download and install "
        "fixes. When it is stopped, security updates can be delayed. Windows "
        "also starts this service on demand, so 'stopped' alone is a warning "
        "worth checking — not proof that updates are broken.",
        "Open Windows Update settings and choose 'Check for updates'. The "
        "scanner did not start or stop any service.",
        "uri", "ms-settings:windowsupdate")


def _defender(_system: str = "") -> Explanation:
    return Explanation(
        "Windows Defender real-time protection is off",
        "Real-time protection scans files as they are opened and blocks known "
        "malware. It can be off deliberately (another antivirus is installed) "
        "or accidentally after a policy change.",
        "Open Windows Security > Virus & threat protection and review the "
        "setting. The scanner changes nothing.",
        "uri", "ms-settings:windowsdefender")


def _system_restore(_system: str = "") -> Explanation:
    return Explanation(
        "System Restore is turned off",
        "Restore points let Windows undo a bad driver or update without "
        "deleting personal files. With System Restore off, that rollback "
        "option is missing when something breaks.",
        "The button copies a safe command to your clipboard. Run "
        "'SystemPropertiesProtection.exe' yourself (or search 'Create a "
        "restore point') and switch protection on for the system drive. The "
        "scanner cannot change this setting.",
        "command", "SystemPropertiesProtection.exe")


def _kernel_errors(system: str = "") -> Explanation:
    target = (_MACOS_LOG_COMMAND if _platform_name(system) == "Darwin"
              else "dmesg --level=err,warn")
    return Explanation(
        "Kernel log reports hardware or storage errors",
        "The kernel log contains I/O errors, a segmentation fault or a "
        "hardware machine-check message. Repeated 'I/O error' lines often "
        "mean a failing cable, SSD/HDD or file system; 'segfault' lines are "
        "usually a crashing application rather than broken RAM.",
        "The button copies a read-only log command. Back up important files "
        "first if disk errors repeat, and check the drive's SMART data. The "
        "scanner only reads the log.",
        "command", target)


def _inodes(_system: str = "") -> Explanation:
    return Explanation(
        "Running out of inodes (file records)",
        "Every file needs an inode — a small metadata record. A volume can "
        "show plenty of free space and still refuse to create files when it "
        "holds millions of tiny files: caches, logs, mail or build folders "
        "are the usual culprits.",
        "The button copies 'df -i', which lists inode usage per volume. Move "
        "or delete old caches/build folders once you know what is safe. The "
        "scanner deletes nothing.",
        "command", "df -i")


def _systemd(_system: str = "") -> Explanation:
    return Explanation(
        "A systemd service failed",
        "One or more services did not start or crashed. The failed unit is "
        "often a driver helper, VPN or update service, so the machine can "
        "look fine while that feature is silently broken.",
        "The button copies 'systemctl --failed' (read-only) so you can see "
        "the unit names; then use 'journalctl -u <unit>' for the reason. The "
        "scanner starts and stops nothing.",
        "command", "systemctl --failed")


def _macos_log(_system: str = "") -> Explanation:
    return Explanation(
        "macOS log contains recent errors",
        "The unified log recorded messages containing 'error' in the last "
        "hour. Many are harmless app-level complaints, but repeated disk, "
        "network or graphics errors can explain crashes and slow-downs.",
        "The button copies the same read-only query so you can inspect the "
        "lines yourself. Look for repeated patterns rather than single "
        "messages. The scanner changes nothing.",
        "command", _MACOS_LOG_COMMAND)


def _time_machine(_system: str = "") -> Explanation:
    return Explanation(
        "Time Machine backups are stale or missing",
        "Time Machine is macOS's built-in backup. If the last successful "
        "backup is more than a week old (or missing), a disk failure or an "
        "accidental deletion could lose recent work. Common causes are an "
        "unplugged backup disk, a full destination or a failed scheduled run.",
        "The button copies 'tmutil startbackup', which starts a backup when "
        "you run it. Check the destination in Time Machine settings first if "
        "it keeps failing. The scanner never starts a backup by itself.",
        "command", "tmutil startbackup")


def _explain_disk(system: str = "") -> Explanation:
    explanation = (
        "A nearly full disk slows the whole machine: the operating system "
        "needs free space for its page/swap file, temporary files, update "
        "staging and file-system bookkeeping. When space runs low, apps wait "
        "on the disk and saves can fail. This is a capacity problem, not "
        "proof that the drive is damaged.")
    name = _platform_name(system)
    if name == "Windows":
        return Explanation(
            "Disk is nearly full", explanation,
            "Open Windows Storage Sense to review what is using space and "
            "remove temporary files. Nothing is deleted until you confirm.",
            "uri", "ms-settings:storagesense")
    if name == "Darwin":
        return Explanation(
            "Disk is nearly full", explanation,
            "The button copies a command that opens Storage settings, where "
            "macOS suggests large files to review. Nothing is deleted "
            "automatically.",
            "command", _STORAGE_SETTINGS_MAC)
    return Explanation(
        "Disk is nearly full", explanation,
        "The button copies 'baobab', a graphical disk-usage analyser "
        "(install gnome-disk-utility if it is missing). Review large folders "
        "before deleting anything. The scanner deletes nothing.",
        "command", "baobab")


def _explain_cpu(_system: str = "") -> Explanation:
    return Explanation(
        "High CPU load",
        "Sustained high CPU means programs are competing for the processor. "
        "The machine gets hot, fans speed up and everything lags; it does not "
        "mean the CPU is damaged. A single runaway process is the most common "
        "cause.",
        "Open Task Manager (Ctrl+Shift+Esc on Windows), Activity Monitor "
        "(macOS) or top (Linux), sort by CPU and close what you recognise. "
        "The scanner does not terminate processes.",
        "info")


def _memory(_system: str = "") -> Explanation:
    return Explanation(
        "High memory use",
        "When RAM fills up, the operating system swaps data to disk, which is "
        "much slower — the machine stutters and apps may be killed. Browser "
        "tabs and Electron apps are common causes, and a leak grows over "
        "time.",
        "Close unused tabs/apps and save your work; restart the app that keeps "
        "growing. Consider more RAM only if this is chronic. The scanner "
        "changes nothing.",
        "info")


def _gpu_hot(_system: str = "") -> Explanation:
    return Explanation(
        "GPU temperature is high",
        "A hot GPU throttles its clock to protect itself, so games and "
        "rendering slow down and the fans get loud. Dust, blocked vents or a "
        "heavy workload are common causes.",
        "Close GPU-heavy apps, clear the vents and check airflow. Persistent "
        "overheating may need service. The scanner changes nothing.",
        "info")


def _access_denied(system: str = "") -> Explanation:
    admin = "Administrator" if _platform_name(system) == "Windows" else "elevated rights"
    return Explanation(
        "Administrator rights needed",
        "The operating system protects this reading (sensor, system setting or "
        "service list) behind elevated rights. The scanner reports it as "
        "unavailable instead of inventing a value — 'Access Denied' is a "
        "permission wall, not a hardware fault.",
        f"If you trust this app, re-run it with {admin} ('Run as "
        "administrator' on Windows, sudo on Linux/macOS) to read that sensor. "
        "Otherwise treat the value as unknown. The scanner never elevates "
        "itself.",
        "info")


def _generic(finding) -> Explanation:
    component = str(getattr(finding, "component", "Check") or "Check")
    message = str(getattr(finding, "message", "") or "did not pass.")
    return Explanation(
        f"{component}: {message}",
        "This check did not pass. The scanner only reads data, so nothing has "
        "been changed on your machine. A single failed check is a hint to "
        "investigate, not a diagnosis.",
        "Review the detail in the scan output; if the problem persists, use "
        "the operating system's own settings or the hardware vendor's tool. "
        "No automatic fix is available for this item.",
        "info")


_CODE_BUILDERS: dict[str, Callable[[str], Explanation]] = {
    "WINDOWS_UPDATE_STOPPED": _windows_update,
    "DEFENDER_DISABLED": _defender,
    "SYSTEM_RESTORE_OFF": _system_restore,
    "KERNEL_HARDWARE_ERROR": _kernel_errors,
    "INODES_LOW": _inodes,
    "INODES_CRITICAL": _inodes,
    "SYSTEMD_FAILED": _systemd,
    "MACOS_LOG_ERRORS": _macos_log,
    "TIMEMACHINE_STALE": _time_machine,
    "TIMEMACHINE_NO_BACKUP": _time_machine,
    "DISK_FULL": _explain_disk,
    "DISK_LOW": _explain_disk,
    "CPU_HIGH": _explain_cpu,
    "MEM_HIGH": _memory,
    "GPU_HOT": _gpu_hot,
    "ACCESS_DENIED": _access_denied,
}


def known_codes() -> frozenset[str]:
    """Codes that have a dedicated explanation (used by tests and docs)."""
    return frozenset(_CODE_BUILDERS)


def _from_patterns(text: str, system: str) -> Explanation | None:
    """Fallback for findings without a known code (or from older exports)."""
    if any(marker in text for marker in (
            "access denied", "access is denied", "permission denied",
            "permissionerror", "operation not permitted", "privilege")):
        return _access_denied(system)
    if any(marker in text for marker in (
            "i/o error", "hardware error", "machine check", "segfault",
            "ext4-fs error", "xfs (", "btrfs")):
        return _kernel_errors(system)
    if "inode" in text or "df -i" in text:
        return _inodes(system)
    if "wuauserv" in text or "windows update" in text:
        return _windows_update(system)
    if "defender" in text:
        return _defender(system)
    if "system restore" in text or "disablesr" in text:
        return _system_restore(system)
    if "systemd" in text or "failed unit" in text or "systemctl" in text:
        return _systemd(system)
    if "time machine" in text or "timemachine" in text or "tmutil" in text:
        return _time_machine(system)
    if ("storage" in text or "disk" in text or "volume" in text
            or "partition" in text):
        return _explain_disk(system)
    if "cpu" in text:
        return _explain_cpu(system)
    if "memory" in text:
        return _memory(system)
    if "gpu" in text:
        return _gpu_hot(system)
    return None


def explain_finding(finding, system: str | None = None) -> Explanation | None:
    """Explain a FAIL/WARNING finding; return None for anything else.

    ``system`` defaults to ``platform.system()`` so the disk/access-denied
    fixes name the right settings. The result is deterministic and performs
    no I/O; opening a URI or copying a command is the caller's job.
    """
    if finding is None:
        return None
    status = str(getattr(finding, "status", "") or "").upper()
    if status not in EXPLAINED_STATUSES:
        return None
    name = _platform_name(system if system is not None else platform.system())

    code = str(getattr(finding, "code", "") or "").strip().upper()
    builder = _CODE_BUILDERS.get(code)
    if builder is not None:
        return builder(name)

    text = " ".join(
        str(getattr(finding, attr, "") or "")
        for attr in ("component", "message", "detail", "key")).lower()
    matched = _from_patterns(text, name)
    if matched is not None:
        return matched
    return _generic(finding)


"""Auto-Action Rules ("The Kill Switch").

This module lets the user pre-authorise automatic responses to sustained
hardware pressure, e.g. "kill the top CPU process when CPU is above 95% for
two minutes". It is deliberately powerful, and just as deliberately fenced:

* Rules never fire on a single sample: the metric must stay above the
  threshold for the whole `duration_s`, and the timer resets the moment it
  drops back below.
* The Master Switch (off by default) must be enabled before anything runs.
* `kill_process` refuses a hard-coded list of critical system processes and
  the app's own process, and only ever terminates the single most demanding
  *eligible* process.
* Actions always execute on a dedicated worker thread, never on the GUI or
  monitoring thread.
* Every action is reported back through the `on_log` callback so the
  Diagnostics output shows exactly what happened.
"""

import logging
import os
import threading

log_actions = logging.getLogger("shds.actions")

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:  # pragma: no cover - psutil is a core dependency in practice
    psutil = None  # type: ignore
    HAS_PSUTIL = False

# Metrics a rule can watch. All are percentages (0..100).
METRIC_LABELS = {"cpu": "CPU", "mem": "RAM", "disk": "Disk"}

# Actions a rule can take.
ACTION_LABELS = {"kill_process": "Kill Top Process",
                 "clean_temp": "Safe Temp Cleanup",
                 "alert_only": "Alert Only"}

# Critical processes that must NEVER be terminated, even in an emergency.
# Names are compared case-insensitively without a ".exe" suffix.
PROTECTED_PROCESS_NAMES = frozenset({
    # Kernel / session - killing these loses the whole machine.
    "system", "system idle process", "idle", "registry", "memory compression",
    "smss", "csrss", "wininit", "winlogon", "services", "lsass", "svchost",
    "fontdrvhost", "dwm", "winmgmt", "wudfhost", "audiodg",
    # The desktop shell - killing it destroys the user's session.
    "explorer", "taskmgr", "sihost", "shellexperiencehost",
    "searchhost", "startmenuexperiencehost",
    # Anti-malware / security (protected by design, not by permission).
    "securityhealthservice", "msmpeng", "smartscreen",
    # This application itself.
    "python", "pythonw", "pylauncher",
})

# Irreducible safety floor: even if PROTECTED_PROCESS_NAMES is ever emptied,
# reset or user-cleared at runtime, these names are still refused outright.
_HARD_PROTECTED_FLOOR = frozenset({
    # Windows kernel / session
    "system", "system idle process", "idle", "registry",
    "memory compression", "smss", "csrss", "wininit", "winlogon",
    "services", "lsass", "svchost", "dwm", "explorer",
    # macOS / Linux
    "kernel_task", "launchd", "init", "systemd",
    # This application itself.
    "python", "pythonw",
})

# Lowest PID value that may be terminated (0/4 are kernel on Windows).
_MIN_KILLABLE_PID = 5

METRIC_ATTRS = {"cpu": "cpu", "mem": "mem", "disk": "disks"}


def is_protected_process(name: str | None) -> bool:
    """True when a process must never be terminated."""
    if not name:
        return True
    base = str(name).strip().lower()
    base = base[:-4] if base.endswith(".exe") else base
    base = base.strip()
    return ((not base) or base in _HARD_PROTECTED_FLOOR
            or base in PROTECTED_PROCESS_NAMES)


class ActionRule:
    """One automatic action triggered by a sustained threshold breach."""

    def __init__(self, metric: str, threshold: float, duration_s: float,
                 action_type: str, label: str = "") -> None:
        if metric not in METRIC_LABELS:
            raise ValueError(f"unknown metric: {metric!r}")
        if action_type not in ACTION_LABELS:
            raise ValueError(f"unknown action_type: {action_type!r}")
        self.metric = metric
        self.threshold = float(threshold)
        self.duration_s = float(duration_s)
        if self.duration_s <= 0:
            raise ValueError(
                f"duration_s must be > 0 (no sustained-breach window = "
                f"instant fire): got {self.duration_s!r}")
        self.action_type = action_type
        self.label = label
        # Live state: consecutive seconds spent above the threshold, and
        # how many times the rule has actually fired.
        self.breach_s = 0.0
        self.times_fired = 0
        # Double-fire guard: set on dispatch, cleared when the action thread
        # finishes (see RuleEngine._dispatch / _run_action).
        self.in_flight = threading.Event()

    # ---------- evaluation ----------
    def value(self, snapshot) -> float | None:
        """The metric the rule watches, or None when it is unavailable."""
        if snapshot is None:
            return None
        # A broken/partial snapshot degrades to "no value" (never raises):
        # RuleEngine.tick promises to never raise on unusable input.
        if self.metric == "cpu":
            value = getattr(getattr(snapshot, "cpu", None), "percent", None)
        elif self.metric == "mem":
            value = getattr(getattr(snapshot, "mem", None), "percent", None)
        else:  # disk: the most-used accessible partition
            candidates = [getattr(d, "percent", None) for d in
                          getattr(snapshot, "disks", []) or []
                          if getattr(d, "accessible", False)
                          and getattr(d, "percent", None) is not None]
            value = max(candidates) if candidates else None
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def exceeded(self, snapshot) -> bool:
        """True when the metric is at or above the threshold right now."""
        value = self.value(snapshot)
        return value is not None and value >= self.threshold

    def check(self, snapshot, time_elapsed: float) -> bool:
        """Should the rule fire? True only when the WHOLE duration elapsed.

        Pure predicate: `time_elapsed` is how long the metric has been
        continuously over the threshold. The caller owns the timer (see
        `RuleEngine.tick`); this method never mutates state.
        """
        if not self.exceeded(snapshot):
            return False
        return time_elapsed >= self.duration_s

    # ---------- bookkeeping / display ----------
    def description(self) -> str:
        return (f"{METRIC_LABELS[self.metric]} > {self.threshold:g}% "
                f"for {self.duration_s:g}s → {ACTION_LABELS[self.action_type]}")

    def __repr__(self) -> str:
        return (f"ActionRule({self.metric}, {self.threshold}, "
                f"{self.duration_s}, {self.action_type})")


class RuleEngine:
    """Checks the rules on every monitoring tick and runs the actions.

    `tick(snapshot, dt_s)` is called on every monitoring tick (dt_s = the
    sampling interval). If a rule's threshold has been exceeded once, its
    breach timer accumulates; the moment a sample comes back under the
    threshold the timer resets to zero. When a rule fires:

    * the breach timer resets (so another fire needs another full window);
    * the action runs on a worker thread created by `executor`
      (`threading.Thread(..., daemon=True)` by default — never the caller's
      thread, so the GUI can never freeze on a terminate() call);
    * the outcome is reported via `on_log(message, tag)`.

    Note that `on_log` may be called from a worker thread — it exist
    precisely so UI code can funnel the message back to the main thread.
    """

    def __init__(self, on_log=None, executor=None, self_pid: int | None = None,
                 clean_action=None, proc_provider=None) -> None:
        self.rules: list[ActionRule] = []
        self.enabled = False  # Master Switch — OFF until the user grants it.
        self._on_log = on_log
        self._executor = executor
        self.self_pid = self_pid if self_pid is not None else os.getpid()
        # clean_temp action: a callable returning a summary string. None means
        # "not wired up" and the action degrades to a log line (never deletes).
        self._clean_action = clean_action
        # kill_process victim source: callable(sort_by) -> rows with
        # .pid/.name/.cpu/.mem_mb. None falls back to psutil via collect_processes.
        self._proc_provider = proc_provider

    # ---------- logging helpers ----------
    def _log(self, message: str, tag: str = "AUTO") -> None:
        if self._on_log:
            try:
                self._on_log(message, tag)
            except Exception:  # noqa: BLE001 - logging must never break actions
                log_actions.exception("actions on_log callback failed")

    # ---------- the tick ----------
    def tick(self, snapshot, dt_s: float) -> list[ActionRule]:
        """Evaluate every rule against one monitoring sample.

        Returns the rules that fired this tick (already dispatched to the
        executor). Never raises: a broken snapshot deactivates the rules for
        that tick only.
        """
        if not self.enabled or not self.rules:
            return []
        fired: list[ActionRule] = []
        for rule in self.rules:
            if rule.exceeded(snapshot):
                if dt_s > 0:
                    rule.breach_s += dt_s
                if rule.check(snapshot, rule.breach_s):
                    # Reset first: a second fire needs another full window,
                    # even if the action thread starts slowly.
                    rule.breach_s = 0.0
                    if rule.in_flight.is_set():
                        # The previous action for this rule is still running
                        # (a slow victim lookup or slow sample path): never
                        # dispatch a second concurrent one, or the same
                        # process can be terminated twice.
                        self._log(f"Auto-action '{rule.action_type}' skipped "
                                  "— previous action is still running.",
                                  "WARNING")
                        continue
                    rule.times_fired += 1
                    fired.append(rule)
                    self._dispatch(rule, snapshot)
            else:
                # Threshold dropped below the limit → the timer resets.
                rule.breach_s = 0.0
        return fired

    def _dispatch(self, rule: ActionRule, snapshot) -> None:
        """Start the action on its own thread; never block the caller."""
        rule.in_flight.set()
        try:
            if self._executor:
                self._executor(lambda: self._run_action(rule, snapshot))
            else:
                threading.Thread(target=self._run_action, name="shds-action",
                                 args=(rule, snapshot), daemon=True).start()
        except (RuntimeError, OSError) as exc:
            rule.in_flight.clear()
            log_actions.warning("action thread failed to start: %s", exc)
            self._log(f"Auto-action could not start its worker thread: {exc}",
                      "WARNING")

    def _run_action(self, rule: ActionRule, snapshot) -> None:
        """Execute one action (runs on a worker thread)."""
        try:
            if rule.action_type == "kill_process":
                message, tag = self._kill_top_process(rule, snapshot)
            elif rule.action_type == "clean_temp":
                message, tag = self._clean_temp(rule, snapshot)
            else:  # alert_only
                message, tag = self._alert(rule, snapshot)
            self._log(message, tag)
        except Exception as exc:  # noqa: BLE001 - one bad action must not kill the engine
            log_actions.exception("auto-action failed: %s", exc)
            self._log(f"Auto-action '{rule.action_type}' failed: {exc}", "WARNING")
        finally:
            # The action is done: after a fresh full breach window the rule
            # may fire again. Runs even when the action raised.
            rule.in_flight.clear()

    # ---------- actions ----------
    def _kill_top_process(self, rule: ActionRule, snapshot) -> tuple[str, str]:
        """Terminate ONE safe victim: the most demanding eligible process.

        Sorting follows the watched metric (CPU breach → sort by CPU,
        RAM breach → sort by memory; a Disk breach has no sensible victim and
        falls back to CPU). Protected and system processes are skipped.
        """
        if not HAS_PSUTIL:
            return ("psutil is unavailable, so the kill_process action ran "
                    "with no one to safely terminate.", "WARNING")
        sort_by = "mem" if rule.metric == "mem" else "cpu"
        rows = self._victims(sort_by)
        for row in rows:
            pid = getattr(row, "pid", 0)
            name = getattr(row, "name", "")
            if pid < _MIN_KILLABLE_PID or pid == self.self_pid:
                continue
            if is_protected_process(name):
                continue
            try:
                psutil.Process(int(pid)).terminate()
            except Exception as exc:  # noqa: BLE001 - a refused kill is logged
                return (f"Auto-action tried to kill {name} (PID {pid}) but the "
                        f"system refused: {exc}", "WARNING")
            metric_text = METRIC_LABELS[rule.metric].upper()
            return (f"Killed {name} (PID {pid}) because {metric_text} was "
                    f">{rule.threshold:g}% for {rule.duration_s:g}s.", "FAIL")
        return ("Auto-action (kill top process) fired, but every candidate was "
                "protected or unkillable — nothing was terminated.", "WARNING")

    def _victims(self, sort_by: str = "cpu") -> list:
        """Top candidate processes for the kill action."""
        if self._proc_provider is not None:
            return list(self._proc_provider(sort_by))
        if not HAS_PSUTIL:
            return []
        try:
            return collect_processes(limit=8, sort_by=sort_by)
        except Exception as exc:  # noqa: BLE001 - defensive
            log_actions.warning("collect_processes failed inside kill action: %s", exc)
            return []

    def _clean_temp(self, rule: ActionRule, snapshot) -> tuple[str, str]:  # noqa: ARG002
        """Run the app-provided safe temp cleanup (or explain its absence)."""
        if self._clean_action is None:
            return ("Auto-action (safe temp cleanup) fired, but no cleanup "
                    "handler is configured — nothing was deleted.", "WARNING")
        summary = str(self._clean_action())
        return (f"Auto-action (safe temp cleanup) fired: {summary}", "PASS")

    def _alert(self, rule: ActionRule, snapshot) -> tuple[str, str]:  # noqa: ARG002
        """Report the sustained breach without taking any action."""
        metric_text = METRIC_LABELS[rule.metric].upper()
        return (f"WARNING: {metric_text} stayed above {rule.threshold:g}% "
                f"for {rule.duration_s:g}s (alert-only rule).", "WARNING")


"""Diagnostic report building + JSON/TXT/CSV export with safe file handling."""

import csv
import html
import json
from dataclasses import asdict
from pathlib import Path


log_reports = logging.getLogger("shds.reports")


def _safe_int(value: object) -> int | None:
    """Best-effort int conversion; None for anything unusable (never raises)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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
        "thresholds": dict(thresholds),
        "hardware": {
            "cpu": asdict(snap.cpu),
            "memory": asdict(snap.mem),
            "disks": disks,
            "network": asdict(snap.net),
            "temperatures": temps,
            "gpu": gpu,
        },
        "findings": [asdict(f) for f in diag.findings],
        "summary": dict(diag.summary),
        "recommendations": list(diag.recommendations),
    }


def render_txt(payload: dict) -> str:
    """Human-readable text rendering of the payload."""
    lines = [
        "SMART HARDWARE DIAGNOSTIC SCANNER — DARK NEON EDITION",
        f"Author: {payload.get('author', 'Devansh')}",
        f"Timestamp: {payload.get('timestamp', '')}",
        f"OS: {payload.get('os', '')}",
        f"Python: {payload.get('python', '')}",
        f"Host: {payload.get('hostname', '')}",
        f"Scan duration: {payload.get('scan_duration_s', 0)}s",
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


def render_html(payload: dict) -> str:
    """Render a self-contained HTML report of one scan.

    Every hardware-derived value is escaped before it reaches the document,
    so a hostile device name can never inject markup.
    """
    esc = html.escape
    ts = str(payload.get("timestamp", ""))
    host = str(payload.get("hostname", ""))
    summary = payload.get("summary", {}) or {}
    hw = payload.get("hardware", {}) or {}

    cards: list[tuple[str, str]] = [
        ("Host", host),
        ("Timestamp", ts),
        ("OS", str(payload.get("os", ""))),
        ("Python", str(payload.get("python", ""))),
        ("Scan duration", f"{payload.get('scan_duration_s', '—')} s"),
    ]
    for label, key in (("PASS", "PASS"), ("WARNING", "WARNING"),
                       ("FAIL", "FAIL"), ("UNAVAILABLE", "UNAVAILABLE")):
        cards.append((label, str(summary.get(key, 0))))

    kv = "".join(
        f'<div class="kv"><div class="k">{esc(label)}</div>'
        f'<div class="v">{esc(value)}</div></div>'
        for label, value in cards)

    rows: list[str] = []
    for finding in payload.get("findings", []) or []:
        status = str(finding.get("status", ""))
        detail = str(finding.get("detail", "") or "")
        rows.append(
            "<tr>"
            f'<td><span class="b-{esc(status)}">{esc(status)}</span></td>'
            f"<td>{esc(str(finding.get('component', '')))}</td>"
            f"<td>{esc(str(finding.get('message', '')))}</td>"
            f"<td>{esc(detail)}</td>"
            "</tr>")

    recs = "".join(f"<li>{esc(str(r))}</li>"
                   for r in (payload.get("recommendations", []) or []))
    rec_html = (f"<h2>Recommendations</h2><ul>{recs}</ul>" if recs else "")

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SHDS report {esc(ts)}</title>
<style>{_COMPARISON_CSS}</style></head>
<body>
<h1>Smart Hardware Diagnostic Scanner</h1>
<div class="sub">Scan report · {esc(ts)} · {esc(host)}</div>
<div class="grid">{kv}</div>
<h2>Findings</h2>
<table><tr><th>Status</th><th>Component</th><th>Message</th><th>Detail</th></tr>
{''.join(rows)}</table>
{rec_html}
<footer>Generated by the Smart Hardware Diagnostic Scanner. Findings describe
this application's own checks, not a vendor diagnostic.</footer>
</body></html>
"""


def save_report(payload: dict, dest: str | Path) -> Path:
    """Write JSON/TXT/CSV/HTML based on suffix. Raises with friendly message on failure."""
    path = safe_path_for_report(str(dest))
    try:
        if path.suffix.lower() == ".json":
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        elif path.suffix.lower() == ".txt":
            path.write_text(render_txt(payload), encoding="utf-8")
        elif path.suffix.lower() == ".csv":
            with path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(["component", "status", "message", "detail"])
                for f in payload.get("findings", []):
                    writer.writerow([f.get("component"), f.get("status"),
                                     f.get("message"), f.get("detail", "")])
        elif path.suffix.lower() == ".html":
            path.write_text(render_html(payload), encoding="utf-8")
        else:
            raise ValueError("Unsupported report format.")
    except (PermissionError, OSError, ValueError) as exc:
        log_reports.warning("report save failed: %s", exc)
        raise
    return path


_COMPARISON_CSS = """
  body { margin:0; padding:28px; background:#12121A; color:#E8E8F0;
         font:14px/1.55 "Segoe UI",system-ui,sans-serif; }
  h1 { font-size:22px; margin:0 0 4px; color:#00E5FF; }
  h2 { font-size:15px; margin:26px 0 10px; color:#00E5FF;
       border-bottom:1px solid #34344A; padding-bottom:6px; }
  .sub { color:#9A9AB0; font-size:12px; margin-bottom:18px; }
  table { border-collapse:collapse; width:100%; background:#1C1C28;
          border:1px solid #34344A; }
  th,td { text-align:left; padding:7px 10px; border-bottom:1px solid #34344A;
          font-size:13px; vertical-align:top; }
  th { background:#232333; color:#00E5FF; font-weight:600; }
  .grid { display:flex; flex-wrap:wrap; gap:10px; }
  .kv { background:#232333; border:1px solid #34344A; border-radius:8px;
        padding:10px 12px; min-width:170px; flex:1 1 170px; }
  .kv .k { color:#9A9AB0; font-size:11px; text-transform:uppercase; }
  .kv .v { font-size:16px; margin-top:3px; }
  .b-PASS { color:#39FF88; } .b-WARNING { color:#FFD60A; }
  .b-FAIL { color:#FF4D6D; } .b-UNAVAILABLE { color:#9A9AB0; }
  .b-INFO { color:#00E5FF; }
  code { background:#232333; padding:1px 5px; border-radius:4px; }
  footer { margin-top:26px; color:#9A9AB0; font-size:11px; }
"""


def render_comparison_html(comparison: dict,
                           previous_payload: dict | None,
                           current_payload: dict) -> str:
    """Render a self-contained HTML diff of two scans. Nothing is invented:
    only statuses that changed (or appeared/disappeared) are listed.
    """
    esc = html.escape
    cur_ts = str(current_payload.get("timestamp", ""))
    prev_ts = str((previous_payload or {}).get("timestamp", "") or "n/a")
    host = str(current_payload.get("hostname", ""))
    # The scanner/ package does not compute a health score, so the key is
    # usually absent. Never invent one: render "n/a" instead of a fake 100.
    cur_score = _safe_int(current_payload.get("health_score"))
    prev_score = _safe_int((previous_payload or {}).get("health_score"))

    buckets = [("regressions", "Worse", "b-FAIL"),
               ("improvements", "Better", "b-PASS"),
               ("added", "New since last scan", "b-INFO"),
               ("removed", "No longer reported", "b-UNAVAILABLE")]
    sections: list[str] = []
    total_changes = 0
    for bucket, heading, _colour in buckets:
        rows = comparison.get(bucket, []) or []
        total_changes += len(rows)
        if not rows:
            continue
        trs: list[str] = []
        for entry in rows:
            key, before, after, message = entry
            if before is None:
                before_cell = "—"
            else:
                before_status = esc(str(before))
                before_cell = f'<span class="b-{before_status}">{before_status}</span>'
            after_cell = (esc(str(after)) if after is not None else "—")
            trs.append(
                "<tr>"
                f"<td><code>{esc(str(key))}</code></td>"
                f"<td>{before_cell}</td>"
                f"<td>{after_cell}</td>"
                f"<td>{esc(str(message))}</td>"
                "</tr>")
        sections.append(
            f"<h2>{esc(heading)} ({len(rows)})</h2>"
            "<table><tr><th>Check</th><th>Before</th><th>After</th>"
            "<th>Message</th></tr>" + "".join(trs) + "</table>")
    if not sections:
        sections.append("<h2>No changes</h2><p>Every check kept the same "
                        "status in both scans.</p>")
    prev_score_html = "—" if prev_score is None else str(prev_score)
    cur_score_html = "—" if cur_score is None else str(cur_score)
    body = f"""
<h1>Scan comparison (diff)</h1>
<div class="sub">Host {esc(host)} · previous scan {esc(prev_ts)} · current scan {esc(cur_ts)}</div>
<div class="grid">
  <div class="kv"><div class="k">Health score before</div><div class="v">{esc(prev_score_html)}</div></div>
  <div class="kv"><div class="k">Health score now</div><div class="v"><span class="b-INFO">{cur_score_html}</span></div></div>
  <div class="kv"><div class="k">Changed checks</div><div class="v">{total_changes}</div></div>
</div>
{"".join(sections)}
<footer>Generated by Smart Hardware Diagnostic Scanner</footer>
"""
    return (f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SHDS scan comparison</title>
<style>{_COMPARISON_CSS}</style>
</head>
<body>
{body}
</body>
</html>
""")


"""Optional update check against GitHub releases (metadata only, never auto-installs)."""

import http.client
import socket
import urllib.error
import urllib.parse
import urllib.request

log_updater = logging.getLogger("shds.updater")

DEFAULT_RELEASES_URL = ("https://api.github.com/repos/your-github-user/"
                        "smart-hardware-diagnostic-scanner/releases/latest")  # placeholder; documented in README

USER_AGENT = f"SmartHardwareDiagnosticScanner/{APP_VERSION}"


@dataclass
class UpdateCheck:
    """Result of one release check; failures degrade to status='unavailable'."""

    status: str = "unavailable"   # "up_to_date" | "update_available" | "unavailable"
    latest: str = ""              # latest version string ("1.2.0")
    current: str = APP_VERSION
    detail: str = ""              # human explanation (error reason etc.)


def _unavailable(detail: str) -> UpdateCheck:
    """Build the failure result; checking never raises and never touches files."""
    return UpdateCheck(status="unavailable", latest="", current=APP_VERSION, detail=detail)


def parse_tag(tag: str | None) -> str:
    """Strip a leading "v"/"V" and whitespace; non-string input yields ""."""
    if not isinstance(tag, str):
        return ""
    text = tag.strip()
    if text[:1] in ("v", "V"):
        text = text[1:]
    return text


def compare_versions(a: str, b: str) -> int:
    """Compare dotted numeric versions: -1 (a<b), 0 (equal or undecidable), 1 (a>b).

    Missing components are padded with 0 ("1.2" == "1.2.0"). Any non-numeric
    component makes the outcome undecidable, so 0 is returned.
    """
    if not isinstance(a, str) or not isinstance(b, str):
        return 0
    parts_a = a.strip().split(".")
    parts_b = b.strip().split(".")
    width = max(len(parts_a), len(parts_b))
    parts_a += ["0"] * (width - len(parts_a))
    parts_b += ["0"] * (width - len(parts_b))
    result = 0
    for text_a, text_b in zip(parts_a, parts_b):
        try:
            num_a = int(text_a.strip())
            num_b = int(text_b.strip())
        except ValueError:
            return 0  # non-numeric component: cannot decide, treat as equal
        if result == 0 and num_a != num_b:
            result = -1 if num_a < num_b else 1
    return result


def check_for_update(url: str | None = None, timeout: float = 4.0) -> UpdateCheck:
    """Check the latest GitHub release. Metadata only: never raises, never installs.

    Any failure (offline, HTTP error, timeout, bad JSON, missing tag) degrades to
    status="unavailable" with a human explanation in `detail`.
    """
    target = (url or "").strip() or DEFAULT_RELEASES_URL
    parsed = urllib.parse.urlparse(target)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        # A malformed URL must degrade like any other failure, not raise out
        # of a function that promises it never does.
        return _unavailable(f"unsupported update URL: {target!r}")
    try:
        request = urllib.request.Request(target, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw = resp.read(1_000_000)
    except urllib.error.HTTPError as exc:
        log_updater.info("update check: HTTP %s from GitHub", exc.code)
        return _unavailable(f"GitHub returned HTTP {exc.code} ({exc.reason}).")
    except urllib.error.URLError as exc:
        log_updater.info("update check: URLError %s", exc.reason)
        return _unavailable(f"no internet connection or GitHub unreachable (URLError: {exc.reason}).")
    except socket.timeout as exc:  # alias of TimeoutError since Python 3.10
        log_updater.info("update check timed out after %ss", timeout)
        return _unavailable(f"update check timed out after {timeout}s ({exc}).")
    except (OSError, ValueError, http.client.HTTPException) as exc:
        # ValueError covers a bad URL/port, http.client.HTTPException covers
        # InvalidURL and friends — none of them are OSError subclasses.
        log_updater.info("update check: request failed %s", exc)
        return _unavailable(f"update check failed: {exc}.")
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError as exc:  # JSONDecodeError and UnicodeDecodeError are ValueError
        return _unavailable(f"could not parse GitHub response as JSON ({exc}).")
    if not isinstance(payload, dict):
        return _unavailable("GitHub response was not a JSON object.")
    latest = parse_tag(payload.get("tag_name"))
    if not latest:
        return _unavailable("GitHub response contained no usable tag_name.")
    result = UpdateCheck(latest=latest)
    order = compare_versions(APP_VERSION, latest)
    if order < 0:
        result.status = "update_available"
        result.detail = f"update available: {latest} (current {APP_VERSION})."
        return result
    result.status = "up_to_date"
    if order > 0:
        result.detail = "local version is newer (pre-release?)"
    else:
        result.detail = f"running the latest release ({APP_VERSION})."
    return result


"""Pure builders for the optional weekly headless scan (schtasks on Windows, cron elsewhere).

No I/O happens here and no subprocess is ever started by this module: the GUI
composes the argv/lines with these helpers and owns running (and removing) them.
"""

import shlex

log_scheduler = logging.getLogger("shds.scheduler")

SCHEDULE_MARKER = "# SHDS weekly scan (added by Smart Hardware Diagnostic Scanner)"
VALID_DAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")

SCHTASKS_TASK_NAME = "SHDS weekly scan"  # /TN value used by the Windows builder


def validate_time(value: str) -> bool:
    """Strict "HH:MM" 24h check (00:00-23:59); no leniency for "9:00" or "24:00"."""
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        return False
    hour_text, minute_text = value[:2], value[3:]
    if not all(ch in "0123456789" for ch in hour_text + minute_text):
        return False
    return int(hour_text) <= 23 and int(minute_text) <= 59


def _check_day(day: str) -> str:
    """Return the day normalized to uppercase; raises ValueError when invalid."""
    if not isinstance(day, str) or day.strip().upper() not in VALID_DAYS:
        log_scheduler.warning("invalid schedule day: %r", day)
        raise ValueError(f"day must be one of: {', '.join(VALID_DAYS)} (got {day!r})")
    return day.strip().upper()


def day_to_cron_dow(day: str) -> int:
    """Map a day name to cron day-of-week (MON=1 ... SAT=6, SUN=0). Raises ValueError."""
    key = _check_day(day)
    return 0 if key == "SUN" else VALID_DAYS.index(key) + 1


def scan_args() -> list[str]:
    """Arguments for a headless scan run, shared by every scheduler backend."""
    return ["--scan", "--quiet", "--no-history"]


def _check_day_and_time(day: str, when: str) -> str:
    """Validate both schedule inputs for the builders; returns the normalized day."""
    key = _check_day(day)
    if not validate_time(when):
        log_scheduler.warning("invalid schedule time: %r", when)
        raise ValueError(f"when must be strict 24h HH:MM between 00:00 and 23:59 (got {when!r})")
    return key


def build_schtasks_argv(python_exe: str, script: str, day: str = "MON", when: str = "09:00") -> list[str]:
    """Build the "schtasks /Create" argv for a weekly scan (caller runs it with shell=False).

    The /TR command keeps embedded quotes on purpose so paths with spaces
    survive the single /TR argument. Raises ValueError for a bad day or time.
    """
    key = _check_day_and_time(day, when)
    quoted_command = f'"{python_exe}" "{script}" ' + " ".join(scan_args())
    return [
        "schtasks", "/Create", "/F",
        "/TN", SCHTASKS_TASK_NAME,
        "/SC", "WEEKLY",
        "/D", key,
        "/ST", when,
        "/TR", quoted_command,
    ]


def _cron_token(value: str) -> str:
    """One shell- and cron-safe token for an executable/script path.

    cron splits the command on whitespace, so a path containing a space must
    be quoted or the schedule silently breaks. Newlines are rejected outright:
    they would inject a second, unmarked crontab line that the removal logic
    could never clean up. '%' is escaped because cron itself turns it into a
    newline.
    """
    text = str(value)
    if "\n" in text or "\r" in text:
        log_scheduler.warning("rejecting path containing a newline: %r", value)
        raise ValueError(f"path must not contain newlines (got {value!r})")
    return shlex.quote(text.replace("%", r"\%"))


def build_cron_line(python_exe: str, script: str, day: str = "MON", when: str = "09:00") -> str:
    """Build one crontab line for the weekly scan (marker at the end so removal finds it).

    Raises ValueError for a bad day or time, or for a path that cannot be
    expressed safely in a crontab line.
    """
    key = _check_day_and_time(day, when)
    hour_text, minute_text = when.split(":", 1)
    hour, minute = int(hour_text), int(minute_text)
    dow = day_to_cron_dow(key)
    body = " ".join(scan_args())
    return (f"{minute} {hour} * * {dow} {_cron_token(python_exe)} "
            f"{_cron_token(script)} {body} {SCHEDULE_MARKER}")


def strip_scheduled_entries(text: str) -> tuple[str, int]:
    """Remove every line containing SCHEDULE_MARKER; return (cleaned_text, removed_count).

    All other lines are preserved as-is (line ending style included); trailing
    blank lines left behind by removals are collapsed.
    """
    if not isinstance(text, str):
        return "", 0
    kept: list[str] = []
    removed = 0
    for line in text.split("\n"):
        if SCHEDULE_MARKER in line:
            removed += 1
            continue
        kept.append(line)
    if removed:
        while kept and not kept[-1].strip():
            kept.pop()
    return "\n".join(kept), removed


def merge_crontab(existing: str | None) -> str:
    """Normalize an existing crontab so the caller can append a fresh scan line.

    None/empty means no crontab existed and yields "". Otherwise old scheduled
    scan entries are stripped and the remaining jobs are kept intact.
    """
    if not isinstance(existing, str) or not existing.strip():
        return ""
    cleaned, _removed = strip_scheduled_entries(existing)
    return cleaned


"""Programmatic vector icons for the UI, drawn with Pillow (no binary assets).

Every icon is a small line drawing painted on a transparent canvas with
`PIL.ImageDraw`, supersampled (`_SUPERSAMPLE`x) and downscaled with a Lanczos
filter so the strokes stay smooth at any size — the same look as a vector
icon pack, but generated in code.

`get_icon(name, size, color)` returns a `customtkinter.CTkImage` that can be
passed straight to `CTkButton(..., image=...)` / `CTkLabel(..., image=...)`.

Caching: the (slow) bitmap rendering is cached per (name, size, colour);
each call wraps the cached bitmap in a *fresh* `CTkImage`, because a
`CTkImage` binds its internal `PhotoImage`s to the Tk interpreter that first
displays it — a shared `CTkImage` would break as soon as a second root (or a
re-created window, as in the tests) displays the icon.

Design grid: every painter works on a 24x24 grid (`u` = one grid unit in
real pixels); strokes are `lw` = `STROKE_UNITS` grid units wide, matching
the look of standard 2px icon strokes.
"""

import math

from PIL import Image, ImageDraw

import customtkinter as ctk


__all__ = ["ICON_NAMES", "get_icon", "clear_cache"]

#: Draw at 4x and downscale — smooth, anti-aliased edges at any display size.
_SUPERSAMPLE = 4

#: Stroke width in design units (24-unit grid). ~2 units reads well at 20 px.
STROKE_UNITS = 2.2

#: Rendered bitmaps, shared across every consumer (interpreter-independent).
_BITMAP_CACHE: dict[tuple[str, int, str], "Image.Image"] = {}


# ---------------------------------------------------------------------------
# Painters. Each draws one icon on a 24x24 design grid:
#   d     — PIL ImageDraw on the supersampled canvas
#   u     — one design unit, in supersampled pixels
#   lw    — stroke width, in supersampled pixels (STROKE_UNITS * u)
#   color — stroke / fill colour as a hex string
# ---------------------------------------------------------------------------
def _monitor(d, u, lw, color):
    """A display screen (the requested 'monitor' circle, drawn as a dial)."""
    d.ellipse([4 * u, 4 * u, 20 * u, 20 * u], outline=color, width=lw)
    r = 1.6 * u
    d.ellipse([12 * u - r, 12 * u - r, 12 * u + r, 12 * u + r], fill=color)


def _dashboard(d, u, lw, color):
    """Speedometer: open dial with a needle — the dashboard gauge."""
    d.arc([4 * u, 4 * u, 20 * u, 20 * u], start=135, end=405, fill=color, width=lw)
    d.line([12 * u, 12 * u, 17 * u, 7.5 * u], fill=color, width=lw)
    r = 1.8 * u
    d.ellipse([12 * u - r, 12 * u - r, 12 * u + r, 12 * u + r], fill=color)


def _security(d, u, lw, color):
    """Shield outline (security / protection)."""
    pts = [(12 * u, 3 * u), (19.5 * u, 6 * u), (18.5 * u, 13 * u),
           (12 * u, 21 * u), (5.5 * u, 13 * u), (4.5 * u, 6 * u)]
    d.line(pts + [pts[0]], fill=color, width=lw, joint="curve")


def _speed(d, u, lw, color):
    """Lightning bolt (speed / performance), filled."""
    pts = [(13.5 * u, 2 * u), (5.5 * u, 13.5 * u), (10.5 * u, 13.5 * u),
           (9.5 * u, 22 * u), (18.5 * u, 9.5 * u), (13 * u, 9.5 * u)]
    d.polygon(pts, fill=color)


def _settings(d, u, lw, color):
    """Gear: toothed ring with a hub hole."""
    d.ellipse([5.5 * u, 5.5 * u, 18.5 * u, 18.5 * u], outline=color, width=lw)
    for k in range(8):
        ang = math.pi / 4 * k
        x1 = 12 + 6.5 * math.cos(ang)
        y1 = 12 + 6.5 * math.sin(ang)
        x2 = 12 + 9.5 * math.cos(ang)
        y2 = 12 + 9.5 * math.sin(ang)
        d.line([x1 * u, y1 * u, x2 * u, y2 * u], fill=color, width=lw)
    r = 2.2 * u
    d.ellipse([12 * u - r, 12 * u - r, 12 * u + r, 12 * u + r], outline=color, width=lw)


def _processes(d, u, lw, color):
    """Stacked task rows (process list)."""
    for i, y in enumerate((6, 12, 18)):
        x_end = 20 if i != 1 else 15
        d.line([4 * u, y * u, x_end * u, y * u], fill=color, width=lw)


def _connections(d, u, lw, color):
    """Two linked nodes (network connections)."""
    r = 3.2 * u
    d.ellipse([3 * u, 12.5 * u, 3 * u + 2 * r, 12.5 * u + 2 * r],
              outline=color, width=lw)
    d.ellipse([15 * u, 3.5 * u, 15 * u + 2 * r, 3.5 * u + 2 * r],
              outline=color, width=lw)
    d.line([3 * u + 2 * r + 0.8 * u, 12.5 * u + r,
            15 * u - 0.8 * u, 3.5 * u + r], fill=color, width=lw)


def _startup(d, u, lw, color):
    """Launch arrow (startup entries)."""
    d.line([12 * u, 20 * u, 12 * u, 5.5 * u], fill=color, width=lw)
    d.line([12 * u, 5.5 * u, 7 * u, 10.5 * u], fill=color, width=lw)
    d.line([12 * u, 5.5 * u, 17 * u, 10.5 * u], fill=color, width=lw)
    d.line([6 * u, 20 * u, 18 * u, 20 * u], fill=color, width=lw)


def _automation(d, u, lw, color):
    """Circular repeat arrow (automated rules)."""
    d.arc([4.5 * u, 4.5 * u, 19.5 * u, 19.5 * u], start=320, end=190,
          fill=color, width=lw)
    # Arrowhead at the arc's end (upper-left), pointing clockwise.
    pts = [(7.2 * u, 2.2 * u), (10.4 * u, 5.8 * u), (5.2 * u, 7.4 * u)]
    d.polygon(pts, fill=color)


def _diagnostics(d, u, lw, color):
    """Magnifying glass (diagnostics scan)."""
    d.ellipse([4 * u, 4 * u, 15 * u, 15 * u], outline=color, width=lw)
    d.line([14.2 * u, 14.2 * u, 20 * u, 20 * u], fill=color, width=int(lw * 1.4))


def _reports(d, u, lw, color):
    """Document with text lines (reports & tools)."""
    d.rounded_rectangle([5 * u, 3 * u, 19 * u, 21 * u], radius=2 * u,
                        outline=color, width=lw)
    for y in (8.5, 12.5, 16.5):
        x_end = 16 if y < 15 else 12.5
        d.line([8 * u, y * u, x_end * u, y * u], fill=color, width=lw)


def _cpu(d, u, lw, color):
    """Processor: die with pins on all four sides."""
    d.rounded_rectangle([6 * u, 6 * u, 18 * u, 18 * u], radius=1.5 * u,
                        outline=color, width=lw)
    for v in (9, 15):
        d.line([v * u, 3 * u, v * u, 6 * u], fill=color, width=lw)
        d.line([v * u, 18 * u, v * u, 21 * u], fill=color, width=lw)
        d.line([3 * u, v * u, 6 * u, v * u], fill=color, width=lw)
        d.line([18 * u, v * u, 21 * u, v * u], fill=color, width=lw)


def _ram(d, u, lw, color):
    """Memory module: stick with chip dividers."""
    d.rounded_rectangle([3 * u, 7 * u, 21 * u, 17 * u], radius=1.5 * u,
                        outline=color, width=lw)
    for x in (7.5, 12, 16.5):
        d.line([x * u, 7 * u, x * u, 17 * u], fill=color, width=lw)


def _gpu(d, u, lw, color):
    """Graphics card: board with a cooling fan."""
    d.rounded_rectangle([3 * u, 6 * u, 21 * u, 18 * u], radius=1.5 * u,
                        outline=color, width=lw)
    d.ellipse([7.5 * u, 8.5 * u, 16.5 * u, 17.5 * u], outline=color, width=lw)
    r = 1.4 * u
    d.ellipse([12 * u - r, 13 * u - r, 12 * u + r, 13 * u + r], fill=color)


def _disk(d, u, lw, color):
    """Drive platter: disc with a hub."""
    d.ellipse([4 * u, 4 * u, 20 * u, 20 * u], outline=color, width=lw)
    r = 2 * u
    d.ellipse([12 * u - r, 12 * u - r, 12 * u + r, 12 * u + r], fill=color)


def _network(d, u, lw, color):
    """Wi-Fi waves (network activity)."""
    d.arc([6 * u, 11 * u, 18 * u, 23 * u], start=205, end=335, fill=color,
          width=lw)
    d.arc([2.5 * u, 7 * u, 21.5 * u, 26 * u], start=215, end=325, fill=color,
          width=lw)
    r = 1.7 * u
    d.ellipse([12 * u - r, 18 * u - r, 12 * u + r, 18 * u + r], fill=color)


def _health(d, u, lw, color):
    """Heart (system health)."""
    pts = [(12 * u, 20.5 * u), (4.5 * u, 13 * u), (4.5 * u, 8 * u),
           (8.2 * u, 4.5 * u), (12 * u, 7.5 * u), (15.8 * u, 4.5 * u),
           (19.5 * u, 8 * u), (19.5 * u, 13 * u)]
    d.polygon(pts, fill=color)


def _refresh(d, u, lw, color):
    """Sync arrows (refresh / reload)."""
    d.arc([4.5 * u, 4.5 * u, 19.5 * u, 19.5 * u], start=30, end=270,
          fill=color, width=lw)
    pts = [(17.6 * u, 2.6 * u), (17.4 * u, 8.2 * u), (12.2 * u, 5.2 * u)]
    d.polygon(pts, fill=color)


_PAINTERS = {
    "monitor": _monitor,
    "dashboard": _dashboard,
    "security": _security,
    "speed": _speed,
    "settings": _settings,
    "processes": _processes,
    "connections": _connections,
    "startup": _startup,
    "automation": _automation,
    "diagnostics": _diagnostics,
    "reports": _reports,
    "cpu": _cpu,
    "ram": _ram,
    "gpu": _gpu,
    "disk": _disk,
    "network": _network,
    "health": _health,
    "refresh": _refresh,
}

ICON_NAMES = tuple(sorted(_PAINTERS))

# Sidebar navigation: page name -> icon name.
NAV_ICONS = {
    "Dashboard": "dashboard",
    "Processes": "processes",
    "Connections": "connections",
    "Startup": "startup",
    "Automation": "automation",
    "Diagnostics": "diagnostics",
    "Reports & Tools": "reports",
}

# Dashboard metric cards: card title -> icon name.
CARD_ICONS = {
    "CPU": "cpu",
    "RAM": "ram",
    "GPU": "gpu",
    "Disk": "disk",
    "Network": "network",
}


def get_icon(name: str, size: int = 24, color: str | None = None) -> "ctk.CTkImage":
    """Render one vector icon as a `customtkinter.CTkImage`.

    `name` must be one of `ICON_NAMES`. `size` is the display size in
    pixels (the bitmap is drawn at size * _SUPERSAMPLE and downscaled).
    `color` is any Tk/PIL hex colour; it defaults to the active palette's
    neon cyan accent. The rendered bitmap is cached per (name, size,
    colour); the returned `CTkImage` is always fresh, so every Tk root can
    bind its own `PhotoImage`s to it.
    """
    try:
        painter = _PAINTERS[name]
    except KeyError:
        raise KeyError(f"unknown icon name: {name!r} — "
                       f"known icons: {', '.join(ICON_NAMES)}") from None
    color = T.NEON_CYAN if color is None else str(color)
    size = max(8, int(size))
    key = (name, size, color)
    bitmap = _BITMAP_CACHE.get(key)
    if bitmap is None:
        px = size * _SUPERSAMPLE
        img = Image.new("RGBA", (px, px), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        unit = px / 24.0  # one design unit in supersampled pixels
        lw = max(1, round(STROKE_UNITS * unit))  # Pillow needs int widths
        painter(draw, unit, lw, color)
        bitmap = img.resize((size, size), Image.Resampling.LANCZOS)
        _BITMAP_CACHE[key] = bitmap
    return ctk.CTkImage(light_image=bitmap, dark_image=bitmap, size=(size, size))


def clear_cache() -> None:
    """Drop every cached bitmap (mainly for tests)."""
    _BITMAP_CACHE.clear()
"""Optional Matplotlib live charts, embedded in Tkinter (degrades gracefully)."""

import logging

log_charts = logging.getLogger("shds.charts")

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
            log_charts.debug("chart update failed: %s", exc)

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
            log_charts.debug("chart re-theme failed: %s", exc)
"""CustomTkinter widget helpers.

CustomTkinter replaces the ttk widgets that carry most of the visual weight
(buttons, entries, progress bars) with their own rounded, fully themeable
versions. The layout of the apps is unchanged — only the widget classes
change.

This module holds the small compatibility layer that the migration needs:

* `ProgressBar` keeps ttk's 0..100 value range. `CTkProgressBar` works with
  a 0.0..1.0 fraction and has no `maximum` option, so the apps' existing
  `bar.configure(value=42)` calls would otherwise break or silently show the
  wrong fill.
* `start(interval)` / `stop()` accept the ttk `Progressbar.start(interval)`
  signature (CTk ignores the interval; speed is fixed by CTk).
* `apply_button_state` / `apply_entry_state` replace the ttk
  `widget.state(["disabled"])` idiom, which CustomTkinter does not provide.
"""



__all__ = ["ProgressBar", "make_button", "make_side_button", "make_entry",
           "make_switch", "make_menu", "make_progress", "make_card",
           "apply_button_state", "apply_entry_state"]

# The factories live in theme.py (they read the palette); re-exported here so
# UI code builds every themed CustomTkinter widget through one façade.
make_button = T.make_button
make_side_button = T.make_side_button
make_entry = T.make_entry
make_switch = T.make_switch
make_menu = T.make_menu
make_card = T.make_card


class ProgressBar(ctk.CTkProgressBar):
    """A `CTkProgressBar` that speaks ttk's 0..100 value range.

    * `maximum` is accepted again (default 100).
    * `set(value)` and `get()` use 0..maximum, not 0..1.
    * `configure(value=...)` works like it did with ttk.
    * `start(interval)` keeps ttk's signature; the interval is ignored
      because CustomTkinter's indeterminate animation has a fixed speed.
    """

    def __init__(self, master, maximum: float = 100, **kwargs) -> None:
        self._maximum = float(maximum) if maximum else 100.0
        kwargs.pop("maximum", None)
        super().__init__(master, **kwargs)

    @property
    def maximum(self) -> float:
        """The upper bound of the value range (100 by default)."""
        return self._maximum

    def set(self, value, from_variable_callback: bool = False) -> None:
        """Set the value (0..maximum)."""
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            numeric = 0.0
        # CTkProgressBar.set() expects a 0.0..1.0 fraction.
        super().set(numeric / self._maximum, from_variable_callback)

    def get(self) -> float:
        """Return the value in 0..maximum."""
        return super().get() * self._maximum

    def start(self, interval: float | None = None) -> None:
        """Begin the indeterminate animation (ttk-compatible signature)."""
        # CustomTkinter has no per-call interval: drop it.
        super().start()

    def configure(self, cnf=None, **kwargs):
        """Accept ttk's `value=` and `maximum=` options."""
        if cnf:
            for key, value in dict(cnf).items():
                kwargs.setdefault(key, value)
        if "value" in kwargs:
            value = kwargs.pop("value")
            self.set(value)
        if "maximum" in kwargs:
            self._maximum = float(kwargs.pop("maximum") or 100.0)
        require_redraw = kwargs.pop("require_redraw", False)
        super().configure(require_redraw=require_redraw, **kwargs)

    def __setitem__(self, key, value) -> None:
        if key == "value":
            self.set(value)
            return
        if key == "maximum":
            self._maximum = float(value or 100.0)
            return
        super().__setitem__(key, value)

    def __getitem__(self, key):
        if key == "value":
            return self.get()
        if key == "maximum":
            return self._maximum
        return super().__getitem__(key)


def apply_button_state(widget, state: str) -> None:
    """Enable/disable a CTkButton.

    Replaces the ttk `widget.state(["disabled"])` / `["!disabled"]` idiom,
    which CustomTkinter does not implement.
    """
    widget.configure(state="disabled" if state == "disabled" else "normal")


def apply_entry_state(widget, state: str) -> None:
    """Enable/disable a CTkEntry (same replacement as `apply_button_state`)."""
    widget.configure(state="disabled" if state == "disabled" else "normal")


def make_progress(master, bg_color: str | None = None, **kwargs):
    """Create a themed progress bar that can be re-coloured on a theme switch.

    Returns the ttk-compatible `ProgressBar` (0..100 values) styled from the
    palette, tagged with its role so `theme.retheme_widget` picks it up on a
    live theme toggle.
    """
    options = T.ctk_progress_kwargs(bg_color)
    options.update(kwargs)
    bar = ProgressBar(master, **options)
    setattr(bar, T._THEME_TAG, ("progress", None, bg_color))
    return bar
"""Background monitoring worker: samples hardware on a thread, GUI polls a queue."""

import queue
import threading
import time
from dataclasses import dataclass, field


log_monitoring = logging.getLogger("shds.monitoring")

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
                # Thread creation failed (thread limit, interpreter shutdown...).
                # `running` must stay False, otherwise monitoring can never be
                # started again for the lifetime of the process.
                log_monitoring.warning("monitor thread failed to start: %s", exc)
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
        so the join is done on a helper thread instead of freezing the UI.
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
        # Bind the stop event ONCE: start() swaps self._stop between runs, so
        # re-reading the attribute inside the loop would keep an old sampler
        # thread alive forever when a new run arms a fresh event.
        stop = self._stop
        while not stop.is_set():
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
                log_monitoring.warning("monitor sample failed: %s", exc)
            wait = self.interval_s - (time.monotonic() - started)
            if wait > 0:
                stop.wait(wait)

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
                log_monitoring.exception("scan failed: %s", exc)
                self.result.put(exc)
            finally:
                self.running = False

        try:
            threading.Thread(target=_job, name="shds-scan", daemon=True).start()
        except (RuntimeError, OSError) as exc:
            # Never leave `running` stuck True: the scan button would stay
            # disabled for the rest of the session.
            log_monitoring.warning("scan thread failed to start: %s", exc)
            self.running = False
            return False
        return True

    def cancel(self) -> None:
        self._stop.set()
"""Dark Neon desktop GUI (CustomTkinter). All widget updates run on the main thread."""

import os
import platform
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk



log_app = configure_logging()

NAV_ITEMS = ["Dashboard", "Processes", "Connections", "Startup", "Automation",
             "Diagnostics", "Reports & Tools"]

#: Display size (px) of the sidebar's vector icons.
ICON_SIZE = 20

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


class ScannerApp(ctk.CTk):
    """Main application window."""

    def __init__(self) -> None:
        super().__init__()
        self.title("Smart Hardware Diagnostic Scanner — Dark Neon Edition  |  by Devansh")
        self.geometry("1180x760")
        self.minsize(960, 620)
        self.configure(bg=T.BG_MAIN)

        self.thresholds: dict = dict(DEFAULT_THRESHOLDS)
        self.monitor = MonitorWorker(interval_s=1.0, include_gpu=False)
        self.latest_snap: HardwareSnapshot | None = None
        self.latest_diag: DiagnosticReport | None = None
        self.previous_diag: DiagnosticReport | None = None
        self.previous_payload: dict | None = None
        self.last_comparison: dict | None = None
        self.latest_payload: dict | None = None
        self.cpu_hist: list[float] = []
        self.mem_hist: list[float] = []
        self.refresh_interval = tk.DoubleVar(value=1.0)
        self.proc_sort = tk.StringVar(value="cpu")
        self.monitor_running = False

        self._bg_queue: queue.Queue = queue.Queue()
        self._scan_active = False
        self._refresh_active = False
        self._procs_active = False
        self._monitor_polling = False
        self._after_ids: set[str] = set()

        # Auto-Action Rules ("The Kill Switch"). The Master Switch is OFF by
        # default — nothing automatic happens until the user flips it here.
        self.rules_enabled = tk.BooleanVar(value=False)
        self.actions_engine = RuleEngine(
            on_log=self._automation_log, self_pid=os.getpid(),
            clean_action=self._automation_clean_temp)

        self._build_style()
        self._build_layout()
        self.show_page("Dashboard")
        self._log("Application started. Optional deps: "
                  f"psutil={'yes' if HAS_PSUTIL else 'NO — metrics unavailable'} / "
                  f"matplotlib={'yes' if HAS_MPL else 'NO — charts disabled'}.", "INFO")
        if not HAS_PSUTIL:
            messagebox.showwarning(
                "Optional dependency missing",
                "psutil is not installed.\nThe app will launch, but live metrics "
                "are unavailable.\nInstall with: pip install psutil")
        self._reschedule(250, self._poll_bg_queue)
        self._reschedule(300, self.refresh_once)
        self.protocol("WM_DELETE_WINDOW", self.on_closing)

    # ---------- styling / layout ----------
    def _build_style(self) -> None:
        """Style the widgets that stay ttk (labels, frames, trees, comboboxes).

        Buttons, entries and progress bars are CustomTkinter widgets and take
        their colours from the palette via `T.ctk_*_kwargs()` instead, so they
        are not configured here.
        """
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background=T.BG_MAIN)
        style.configure("Card.TFrame", background=T.BG_CARD, relief="flat")
        style.configure("Side.TFrame", background=T.BG_SIDEBAR)
        style.configure("TLabel", background=T.BG_MAIN, foreground=T.FG_PRIMARY,
                        font=T.FONT_BODY)
        style.configure("Card.TLabel", background=T.BG_CARD, foreground=T.FG_PRIMARY)
        style.configure("Muted.TLabel", background=T.BG_CARD, foreground=T.FG_MUTED,
                        font=T.FONT_SMALL)
        style.configure("Title.TLabel", background=T.BG_MAIN, foreground=T.FG_PRIMARY,
                        font=T.FONT_TITLE)
        style.configure("Head.TLabel", background=T.BG_MAIN, foreground=T.NEON_CYAN,
                        font=T.FONT_HEADING)
        style.configure("Treeview", background=T.BG_PANEL, fieldbackground=T.BG_PANEL,
                        foreground=T.FG_PRIMARY, rowheight=24, font=T.FONT_BODY)
        style.configure("Treeview.Heading", background=T.BG_SIDEBAR,
                        foreground=T.NEON_CYAN, font=T.FONT_SMALL)
        style.configure("TRadiobutton", background=T.BG_MAIN,
                        foreground=T.FG_PRIMARY)
        style.configure("TScrollbar", background=T.BG_PANEL,
                        troughcolor=T.BG_OUTPUT, bordercolor=T.BORDER)
        style.configure("TSpinbox", fieldbackground=T.ENTRY_BG, background=T.ENTRY_BG,
                        foreground=T.FG_PRIMARY)
        style.configure("TEntry", fieldbackground=T.ENTRY_BG, foreground=T.FG_PRIMARY)
        style.configure("TCombobox", fieldbackground=T.ENTRY_BG,
                        background=T.BG_CARD, foreground=T.FG_PRIMARY,
                        arrowcolor=T.FG_PRIMARY)
        # The combobox popdown is a classic tk.Listbox below the style system.
        self.option_add("*TCombobox*Listbox.background", T.BG_OUTPUT)
        self.option_add("*TCombobox*Listbox.foreground", T.FG_PRIMARY)

    def _build_layout(self) -> None:
        # Sidebar (240 px): brand, icon navigation, live controls.
        self.sidebar = tk.Frame(self, bg=T.BG_SIDEBAR, width=240)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)
        brand = tk.Label(self.sidebar, text="◈  SHDS", bg=T.BG_SIDEBAR,
                         fg=T.NEON_CYAN, font=(T.FONT_FAMILY, 17, "bold"))
        brand.pack(pady=(18, 2), padx=16, anchor="w")
        sub = tk.Label(self.sidebar, text="Hardware Diagnostic Scanner\nby Devansh",
                       bg=T.BG_SIDEBAR, fg=T.FG_MUTED, font=T.FONT_SMALL,
                       justify="left")
        sub.pack(padx=16, anchor="w", pady=(0, 14))
        self.nav_buttons: dict[str, ctk.CTkButton] = {}
        for item in NAV_ITEMS:
            btn = T.make_side_button(self.sidebar, T.BG_SIDEBAR,
                                     text=f" {item}",
                                     image=self._nav_icon(item, active=False),
                                     command=lambda n=item: self.show_page(n))
            btn.pack(fill="x", padx=10, pady=3)
            self.nav_buttons[item] = btn
        tk.Frame(self.sidebar, bg=T.BORDER, height=1).pack(
            fill="x", padx=16, pady=(14, 0))
        tk.Label(self.sidebar, text="Refresh interval (s):", bg=T.BG_SIDEBAR,
                 fg=T.FG_MUTED, font=T.FONT_SMALL).pack(padx=16, pady=(12, 2),
                                                        anchor="w")
        intervals = ttk.Combobox(self.sidebar, textvariable=self.refresh_interval,
                                 values=["0.5", "1.0", "2.0", "5.0"], width=8,
                                 state="readonly")
        intervals.pack(padx=16, anchor="w")
        intervals.bind("<<ComboboxSelected>>", lambda _e: self._apply_interval())
        self.mon_btn = T.make_button(self.sidebar, "accent", T.BG_SIDEBAR,
                                     text="▶  Start monitoring",
                                     command=self.toggle_monitoring)
        self.mon_btn.pack(fill="x", padx=16, pady=(16, 6))
        self.theme_btn = T.make_button(
            self.sidebar, "ghost", T.BG_SIDEBAR,
            text="◐  Switch to light" if T.get_mode() == "dark" else "◐  Switch to dark",
            command=self._toggle_theme)
        self.theme_btn.pack(fill="x", padx=16)
        self._update_btn = T.make_button(
            self.sidebar, "ghost", T.BG_SIDEBAR,
            text="⇩  Check for updates",
            command=self.check_for_updates)
        self._update_btn.pack(fill="x", padx=16, pady=(6, 0))

        # Main column
        main_col = ttk.Frame(self)
        main_col.pack(side="left", fill="both", expand=True)
        header = ttk.Frame(main_col)
        header.pack(fill="x", padx=18, pady=(14, 6))
        self.page_title = ttk.Label(header, text="Hardware Dashboard",
                                    style="Title.TLabel")
        self.page_title.pack(side="left")
        self.head_status = tk.Label(header, text="● idle", bg=T.BG_MAIN,
                                    fg=T.FG_MUTED, font=T.FONT_SMALL)
        self.head_status.pack(side="right")

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
        self._build_automation(self.pages["Automation"])
        self._build_diagnostics(self.pages["Diagnostics"])
        self._build_reports(self.pages["Reports & Tools"])

        # Status bar
        self.status_var = tk.StringVar(value="Ready.")
        self.scan_var = tk.StringVar(value="Last scan: never")
        bar = tk.Frame(self, bg=T.BG_STATUSBAR, height=28)
        bar.pack(side="bottom", fill="x")
        tk.Label(bar, textvariable=self.status_var, bg=T.BG_STATUSBAR,
                 fg=T.FG_MUTED, font=T.FONT_SMALL).pack(side="left", padx=12)
        tk.Label(bar, textvariable=self.scan_var, bg=T.BG_STATUSBAR,
                 fg=T.NEON_PURPLE, font=T.FONT_SMALL).pack(side="right", padx=12)

    # ---------- pages ----------
    def _nav_icon(self, item: str, active: bool):
        """Vector icon for one sidebar entry (cyan when active, muted else)."""
        return icons.get_icon(icons.NAV_ICONS[item], ICON_SIZE,
                              T.NEON_CYAN if active else T.FG_MUTED)

    def _card(self, parent, title: str, icon_name: str, accent_key: str):
        """Build one rounded dashboard card.

        Layout: vector icon top-left with the title beside it, the live
        value in large bold text below, a progress bar and a detail line.
        Returns (card, value_label, bar, detail_label, status_dot, icon_label);
        `self.cards[title]` keeps the (value, bar, detail, dot) tuple the
        snapshot renderer updates.
        """
        accent = getattr(T, accent_key)
        card = T.make_card(parent, T.BG_MAIN)
        card.configure(width=T.CARD_MIN_WIDTH, height=T.CARD_HEIGHT)
        card.grid_propagate(False)
        card.columnconfigure(0, weight=1)
        card.rowconfigure(3, weight=1)
        top = tk.Frame(card, bg=T.BG_CARD)
        top.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 0))
        icon = ctk.CTkLabel(top, image=icons.get_icon(icon_name, 22, accent),
                            text="", bg_color=T.BG_CARD, width=26)
        icon.pack(side="left")
        tk.Label(top, text=title, bg=T.BG_CARD, fg=accent,
                 font=T.FONT_HEADING).pack(side="left", padx=(8, 0))
        dot = tk.Label(top, text="●", bg=T.BG_CARD, fg=T.FG_DIM, font=T.FONT_BODY)
        dot.pack(side="right")
        val = tk.Label(card, text="—", bg=T.BG_CARD, fg=T.FG_PRIMARY,
                       font=(T.FONT_FAMILY, 16, "bold"), anchor="w")
        val.grid(row=1, column=0, sticky="ew", padx=12, pady=(4, 0))
        bar = ctkw.make_progress(card, T.BG_CARD, mode="determinate", maximum=100)
        bar.grid(row=2, column=0, sticky="ew", padx=12, pady=(6, 0))
        detail = tk.Label(card, text="", bg=T.BG_CARD, fg=T.FG_MUTED,
                          font=T.FONT_SMALL, wraplength=T.CARD_MIN_WIDTH - 36,
                          justify="left", anchor="nw")
        detail.grid(row=3, column=0, sticky="ew", padx=12, pady=(4, 8))
        return card, val, bar, detail, dot, icon

    def _build_dashboard(self, page) -> None:
        # "System Health": one combined 0-100 score across CPU, RAM, disk and
        # the OS-level checks, presented as a wide hero card above the five
        # metric cards (whose single-row grid layout is covered by tests).
        health = T.make_card(page, T.BG_MAIN, height=92)
        health.pack(fill="x", pady=(0, 8))
        health.grid_propagate(False)
        health.columnconfigure(2, weight=1)
        health.rowconfigure(0, weight=1)
        self.health_icon = ctk.CTkLabel(
            health, image=icons.get_icon("security", 28, T.NEON_CYAN),
            text="", bg_color=T.BG_CARD, width=34)
        self.health_icon.grid(row=0, column=0, rowspan=2, padx=(14, 6),
                              pady=10, sticky="ns")
        left = tk.Frame(health, bg=T.BG_CARD)
        left.grid(row=0, column=1, sticky="nsw", padx=(0, 10), pady=8)
        tk.Label(left, text="SYSTEM HEALTH", bg=T.BG_CARD, fg=T.FG_MUTED,
                 font=T.FONT_SMALL).pack(anchor="w")
        score_row = tk.Frame(left, bg=T.BG_CARD)
        score_row.pack(anchor="w")
        self.health_var = tk.StringVar(value="—")
        self.health_label = tk.Label(score_row, textvariable=self.health_var,
                                     bg=T.BG_CARD, fg=T.FG_DIM,
                                     font=(T.FONT_FAMILY, 24, "bold"))
        self.health_label.pack(side="left")
        tk.Label(score_row, text="/ 100", bg=T.BG_CARD, fg=T.FG_MUTED,
                 font=T.FONT_SMALL).pack(side="left", anchor="s",
                                         padx=(4, 0), pady=(0, 3))
        right = tk.Frame(health, bg=T.BG_CARD)
        right.grid(row=0, column=2, sticky="ew", padx=(0, 14), pady=8)
        right.columnconfigure(0, weight=1)
        self.health_bar = ctkw.make_progress(right, T.BG_CARD, height=10)
        self.health_bar.grid(row=0, column=0, sticky="ew", pady=(6, 2))
        self.health_bar.set(0)
        self.health_detail = tk.Label(
            right, text="Waiting for the first metrics sample…", bg=T.BG_CARD,
            fg=T.FG_MUTED, font=T.FONT_SMALL, justify="left", anchor="w",
            wraplength=420)
        self.health_detail.grid(row=1, column=0, sticky="ew")

        # Cards sit in a single-row grid whose column widths are recomputed
        # from the row's actual pixel width on every resize, so all five
        # cards stay visible at any window size (default 1180 and min 960,
        # and narrower as well). This replaces the fixed-width pack layout
        # that overflowed the window.
        self.cards_row = ttk.Frame(page)
        self.cards_row.pack(fill="x", pady=(0, 8))
        # (title, icon name, palette accent key) per card.
        self._card_titles = [("CPU", "cpu", "NEON_CYAN"),
                             ("RAM", "ram", "NEON_PURPLE"),
                             ("GPU", "gpu", "NEON_GREEN"),
                             ("Disk", "disk", "AMBER"),
                             ("Network", "network", "NEON_YELLOW")]
        self.cards: dict[str, tuple] = {}
        self._card_icons: dict[str, tuple] = {}
        self._card_slots: list[tuple[object, int]] = []
        for col, (title, icon_name, accent_key) in enumerate(self._card_titles):
            card, val, bar, detail, dot, icon = self._card(
                self.cards_row, title, icon_name, accent_key)
            card.grid(row=0, column=col, sticky="nsew", padx=(0, 8))
            self.cards_row.columnconfigure(col, weight=1, minsize=0)
            self.cards[title] = (val, bar, detail, dot)
            self._card_slots.append((card, col))
            self._card_icons[title] = (icon, icon_name, accent_key)
        # Rows must not grow vertically: cards keep CARD_HEIGHT, the chart
        # below absorbs the rest.
        self.cards_row.rowconfigure(0, weight=0)
        self.cards_row.bind("<Configure>", self._on_cards_resize)
        self._last_card_width = 0

        btn_row = ttk.Frame(page)
        btn_row.pack(fill="x", pady=(0, 8))
        T.make_button(btn_row, "ghost", T.BG_MAIN, text="⟳  Refresh now",
                      command=self.refresh_once).pack(side="left", padx=(0, 8))
        T.make_button(btn_row, "accent", T.BG_MAIN, text="♥  Run diagnostic scan",
                      command=self.run_scan).pack(side="left")
        ttk.Label(btn_row, text="Charts: CPU + RAM (live)" if HAS_MPL else
                  "Charts unavailable — matplotlib not installed.",
                  font=T.FONT_SMALL, foreground=T.FG_MUTED,
                  background=T.BG_MAIN).pack(side="right")
        chart_box = tk.Frame(page, bg=T.BG_CARD, highlightbackground=T.BORDER,
                             highlightthickness=1)
        chart_box.pack(fill="both", expand=True, pady=(0, 4))
        if HAS_MPL:
            self.chart = ChartPanel(chart_box, bg=T.BG_CARD, fg=T.FG_PRIMARY)
        else:
            tk.Label(chart_box, text="Install matplotlib for live charts:\n"
                     "pip install matplotlib", bg=T.BG_CARD, fg=T.FG_MUTED,
                     font=T.FONT_BODY, justify="center").pack(expand=True, pady=30)
            self.chart = None

    def _on_cards_resize(self, event) -> None:
        # The grid distributes the row width across the five equal-weight
        # columns; sync each card's requested width to its actual column width
        # (minus the inter-card padding) so details wrap correctly and cards
        # never force the row wider than the window.
        row_width = event.width
        n = len(self._card_slots)
        if n == 0 or row_width < T.CARD_MIN_WIDTH:
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
        ctkw.make_button(row, "ghost", T.BG_MAIN, text="Refresh",
                         command=self.refresh_processes).pack(side="right")
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

    def _build_connections(self, page) -> None:
        ttk.Label(page, text="Active network connections (read-only)",
                  style="Head.TLabel").pack(anchor="w", pady=(0, 6))
        ttk.Label(page, text="A snapshot of INET connections with the owning "
                  "process where visible. Nothing is listed silently as blank.",
                  style="Muted.TLabel").pack(anchor="w", pady=(0, 6))
        row = ttk.Frame(page)
        row.pack(fill="x", pady=(0, 6))
        self.conn_note = tk.StringVar(value="Not loaded yet.")
        ttk.Label(row, textvariable=self.conn_note,
                  style="Muted.TLabel").pack(side="left", padx=(0, 8))
        ctkw.make_button(row, "ghost", T.BG_MAIN, text="Refresh",
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
            rows, note = collect_connections(limit=200)
        except Exception as exc:  # noqa: BLE001 - degrade, never crash GUI
            log_app.warning("connection collect failed: %s", exc)
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
        ctkw.make_button(row, "ghost", T.BG_MAIN, text="Refresh",
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
            log_app.warning("startup collect failed: %s", exc)
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

    # ---------- automation (Auto-Action Rules / Kill Switch) ----------
    def _build_automation(self, page) -> None:
        ttk.Label(page, text="Auto-Action Rules (The Kill Switch)",
                  style="Head.TLabel").pack(anchor="w", pady=(0, 4))
        ttk.Label(page, text=(
            "⚠️ Very powerful: when a rule's threshold stays exceeded for the "
            "whole duration, its action runs automatically. Kill rules "
            "terminate the single worst eligible process (critical system "
            "processes are never touched). Rules are only evaluated while "
            "monitoring is running, and only while the Master Switch is on."),
            style="Muted.TLabel", wraplength=680, justify="left").pack(
            anchor="w", pady=(0, 8))

        # Master Switch
        master_row = ttk.Frame(page)
        master_row.pack(fill="x", pady=(0, 8))
        self.rules_master = ctkw.make_switch(
            master_row, T.BG_MAIN, text="Enable Auto-Action Rules",
            variable=self.rules_enabled, command=self._toggle_rules_master)
        self.rules_master.pack(side="left")

        # Rule builder
        add_box = tk.LabelFrame(page, text="Add rule", bg=T.BG_PANEL,
                                fg=T.NEON_CYAN, font=T.FONT_SMALL,
                                padx=10, pady=8)
        add_box.pack(fill="x", pady=(8, 8))
        self.rule_metric_var = tk.StringVar(value=METRIC_LABELS["cpu"])
        self.rule_threshold_var = tk.StringVar(value="95")
        self.rule_duration_var = tk.StringVar(value="120")
        self.rule_action_var = tk.StringVar(value=ACTION_LABELS["kill_process"])

        fields = [
            ("Metric (watched)", ctkw.make_menu(
                add_box, T.BG_PANEL, values=list(METRIC_LABELS.values()),
                variable=self.rule_metric_var, width=90)),
            ("Threshold (%)", ctkw.make_entry(add_box, T.BG_PANEL,
                                              textvariable=self.rule_threshold_var,
                                              width=10)),
            ("Duration (s)", ctkw.make_entry(add_box, T.BG_PANEL,
                                             textvariable=self.rule_duration_var,
                                             width=10)),
            ("Action (when fired)", ctkw.make_menu(
                add_box, T.BG_PANEL, values=list(ACTION_LABELS.values()),
                variable=self.rule_action_var, width=170)),
        ]
        brow = ttk.Frame(add_box)
        brow.pack(fill="x")
        for label_text, widget in fields:
            col = ttk.Frame(brow)
            col.pack(side="left", padx=(0, 10))
            tk.Label(col, text=label_text, bg=T.BG_PANEL, fg=T.FG_MUTED,
                     font=T.FONT_SMALL).pack(anchor="w")
            widget.pack(anchor="w", pady=(2, 0))
        ctkw.make_button(add_box, "accent", T.BG_PANEL, text="Add Rule",
                         command=self._add_rule).pack(anchor="w", pady=(8, 0))

        # Active rules list
        ttk.Label(page, text="Active rules", style="Head.TLabel").pack(
            anchor="w", pady=(8, 2))
        cols = ("metric", "threshold", "duration", "action", "fired")
        self.rules_tree = ttk.Treeview(page, columns=cols, show="headings",
                                       height=7)
        for c, w, label in [("metric", 90, "Metric"), ("threshold", 110, "Threshold"),
                            ("duration", 110, "Duration"), ("action", 170, "Action"),
                            ("fired", 80, "Fired")]:
            self.rules_tree.heading(c, text=label)
            self.rules_tree.column(c, width=w, anchor="w", stretch=False)
        self.rules_tree.column("duration", stretch=True)
        self.rules_tree.pack(fill="x", pady=(0, 6))
        rrow = ttk.Frame(page)
        rrow.pack(fill="x")
        ctkw.make_button(rrow, "ghost", T.BG_MAIN, text="Remove Selected",
                         command=self._remove_selected_rule).pack(side="left",
                                                                  padx=(0, 8))
        ttk.Label(rrow, text="A rule fires only when its metric stays over the "
                  "threshold for the whole duration; the timer resets when it "
                  "drops back below.", style="Muted.TLabel").pack(side="left")

    def _refresh_rules_tree(self) -> None:
        """Render the engine's rule list (values used by the Remove button)."""
        try:
            self.rules_tree.delete(*self.rules_tree.get_children())
            for i, rule in enumerate(self.actions_engine.rules):
                self.rules_tree.insert(
                    "", "end", iid=str(i),
                    values=(METRIC_LABELS[rule.metric],
                            f">{rule.threshold:g}%",
                            f"{rule.duration_s:g}s",
                            ACTION_LABELS[rule.action_type],
                            rule.times_fired),
                    tags=(f"{rule.metric}|{rule.threshold:g}|"
                          f"{rule.duration_s:g}|{rule.action_type}",))
        except (AttributeError, tk.TclError):
            pass

    def _add_rule(self) -> None:
        try:
            threshold = float(self.rule_threshold_var.get())
            duration = float(self.rule_duration_var.get())
        except (ValueError, tk.TclError):
            messagebox.showerror("Invalid rule",
                                 "Threshold and duration must be numbers.")
            return
        if not 0 < threshold <= 100:
            messagebox.showerror("Invalid rule",
                                 "Threshold must be a percentage between 0 and 100.")
            return
        if not 5 <= duration <= 86400:
            messagebox.showerror("Invalid rule",
                                 "Duration must be between 5 and 86400 seconds.")
            return
        reverse_metric = {label: key for key, label in METRIC_LABELS.items()}
        reverse_action = {label: key for key, label in ACTION_LABELS.items()}
        rule = ActionRule(reverse_metric[self.rule_metric_var.get()],
                          threshold, duration,
                          reverse_action[self.rule_action_var.get()])
        self.actions_engine.rules.append(rule)
        self._refresh_rules_tree()
        armed = "armed" if self.rules_enabled.get() else "NOT armed (Master Switch off)"
        self._log(f"Auto-action rule added: {rule.description()} — {armed}.", "INFO")
        self.set_status(f"Rule added ({rule.description()}).")

    def _remove_selected_rule(self) -> None:
        selection = self.rules_tree.selection()
        if not selection:
            messagebox.showinfo("Remove rule", "Select a rule row first.")
            return
        try:
            index = int(selection[0])
            rule = self.actions_engine.rules.pop(index)
        except (ValueError, IndexError, tk.TclError):
            self.set_status("Rule list changed — try again.")
            return
        self._refresh_rules_tree()
        self._log(f"Auto-action rule removed: {rule.description()}", "INFO")

    def _toggle_rules_master(self) -> None:
        try:
            enabled = bool(int(self.rules_master.get()))
        except (ValueError, tk.TclError, AttributeError):
            enabled = False
        self.actions_engine.enabled = enabled
        if enabled:
            for rule in self.actions_engine.rules:
                rule.breach_s = 0.0
            self._log("Master Switch ENABLED — auto-action rules are armed. "
                      "They run only while monitoring is active.", "WARNING")
        else:
            self._log("Master Switch disabled — auto-action rules are inert.", "INFO")

    def _automation_log(self, message: str, tag: str = "AUTO") -> None:
        """Thread-safe funnel for auto-action events (called from workers)."""
        self._bg_queue.put(("auto_log", (message, tag)))

    def _automation_clean_temp(self) -> str:
        """Safe temp cleanup for the clean_temp rule. Runs on a worker thread.

        Same policy as the Reports & Tools cleanup: only the OS temp root,
        only top-level files, only allow-listed suffixes, containment checked.
        No interactive prompts — automation implies prior consent.
        """
        files, note = preview_temp_candidates(limit=200)
        root = approved_temp_root()
        removed = skipped = 0
        freed = 0
        for p in files:
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
        return (f"removed {removed}, skipped {skipped}, "
                f"freed {format_bytes(freed)}. {note}")

    def _build_diagnostics(self, page) -> None:
        ttk.Label(page, text="Diagnostic scan output", style="Head.TLabel").pack(anchor="w")
        row = ttk.Frame(page)
        row.pack(fill="x", pady=6)
        ctkw.make_button(row, "accent", T.BG_MAIN, text="♥  Run diagnostic scan",
                         command=self.run_scan).pack(side="left", padx=(0, 8))
        ctkw.make_button(row, "ghost", T.BG_MAIN, text="Save diff HTML",
                         command=self.export_diff_html).pack(side="left", padx=(0, 8))
        ctkw.make_button(row, "ghost", T.BG_MAIN, text="♥  Check SMART (smartctl)",
                         command=self.check_smart).pack(side="left", padx=(0, 8))
        ctkw.make_button(row, "ghost", T.BG_MAIN, text="Clear",
                         command=self._clear_diagnostics_view).pack(side="left")
        self.scan_bar = ctkw.make_progress(row, T.BG_MAIN,
                                           mode="indeterminate", width=220)
        self.scan_bar.pack(side="right")
        # "How to fix" rows for FAIL/WARNING findings; rebuilt after every scan.
        self.fix_frame = tk.Frame(page, bg=T.BG_MAIN)
        self.fix_frame.pack(fill="x", pady=(0, 6))
        tk.Label(self.fix_frame,
                 text="FAIL and WARNING findings will offer a 'How to fix' "
                      "button after a scan.",
                 bg=T.BG_MAIN, fg=T.FG_MUTED, font=T.FONT_SMALL,
                 anchor="w").pack(fill="x")
        out_box = tk.Frame(page, bg=T.BG_OUTPUT, highlightbackground=T.BORDER,
                           highlightthickness=1)
        out_box.pack(fill="both", expand=True)
        self.diag_text = tk.Text(out_box, bg=T.BG_OUTPUT, fg=T.OUTPUT_FG,
                                 insertbackground=T.NEON_CYAN, font=T.FONT_MONO,
                                 wrap="word", state="disabled")
        sb = ttk.Scrollbar(out_box, orient="vertical", command=self.diag_text.yview)
        self.diag_text.configure(yscrollcommand=sb.set)
        self.diag_text.pack(side="left", fill="both", expand=True, padx=2, pady=2)
        sb.pack(side="left", fill="y")
        for tag, colour in T.STATUS_COLOURS.items():
            self.diag_text.tag_configure(tag, foreground=colour)

    def _clear_diagnostics_view(self) -> None:
        try:
            self.diag_text.configure(state="normal")
            self.diag_text.delete("1.0", "end")
            self.diag_text.configure(state="disabled")
        except tk.TclError:
            pass
        for child in self.fix_frame.winfo_children():
            child.destroy()

    def _render_fix_buttons(self, findings) -> None:
        """Rebuild the FAIL/WARNING rows with one 'How to fix' button each."""
        frame = getattr(self, "fix_frame", None)
        if frame is None:
            return
        for child in frame.winfo_children():
            child.destroy()
        actionable = [f for f in findings if f.status in ("FAIL", "WARNING")]
        # FAIL rows first: they are the ones that need attention now.
        actionable.sort(key=lambda f: 0 if f.status == "FAIL" else 1)
        if not actionable:
            tk.Label(frame, text="No FAIL or WARNING findings — nothing to fix.",
                     bg=T.BG_MAIN, fg=T.FG_MUTED, font=T.FONT_SMALL,
                     anchor="w").pack(fill="x")
            return
        header_colour = (T.STATUS_COLOURS.get("FAIL", T.NEON_RED)
                         if any(f.status == "FAIL" for f in actionable)
                         else T.STATUS_COLOURS.get("WARNING", T.NEON_YELLOW))
        tk.Label(
            frame,
            text="FAIL and WARNING findings need attention. 'How to fix' "
                 "explains the problem and opens the right settings or copies a "
                 "safe command — nothing is changed for you.",
            bg=T.BG_MAIN, fg=header_colour, font=T.FONT_SMALL, anchor="w",
            justify="left").pack(fill="x", pady=(0, 2))
        for finding in actionable:
            row = tk.Frame(frame, bg=T.BG_MAIN)
            row.pack(fill="x", pady=1)
            tk.Label(row,
                     text=f"[{finding.status}] {finding.component}: "
                          f"{finding.message}",
                     bg=T.BG_MAIN,
                     fg=T.STATUS_COLOURS.get(finding.status, T.FG_PRIMARY),
                     font=T.FONT_SMALL, anchor="w", justify="left").pack(
                         side="left", fill="x", expand=True)
            ctkw.make_button(row, "ghost", T.BG_MAIN, text="How to fix",
                             width=96,
                             command=lambda f=finding: self._show_fix(f)).pack(
                                 side="right", padx=(8, 0))

    def _copy_to_clipboard(self, text: str) -> bool:
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
            self.update_idletasks()
            return True
        except tk.TclError:
            return False

    def _open_settings_uri(self, uri: str) -> bool:
        """Open an OS settings page; never a shell, never a state change."""
        system = platform.system()
        try:
            if system == "Windows" and hasattr(os, "startfile"):
                os.startfile(uri)  # noqa: S606 - user-initiated settings page
            else:
                argv = (["open", uri] if system == "Darwin"
                        else ["xdg-open", uri])
                proc = subprocess.run(argv, shell=False, check=False,
                                      capture_output=True, timeout=8)
                if proc.returncode != 0:
                    raise OSError(
                        f"{argv[0]} exited with code {proc.returncode}")
            return True
        except (OSError, subprocess.SubprocessError) as exc:
            log_app.warning("could not open settings URI %s: %s", uri, exc)
            self._copy_to_clipboard(uri)
            return False

    def _show_fix(self, finding) -> None:
        """Explain one FAIL/WARNING finding and perform its safe fix."""
        explanation = explain_finding(finding)
        if explanation is None:
            self.set_status("No fix guidance needed for this finding.")
            return
        self._log(f"Fix guidance — {explanation.title}: "
                  f"{explanation.explanation}", "INFO")
        self._log(f"    {explanation.fix}", "INFO")
        if explanation.kind == "uri" and explanation.target:
            if self._open_settings_uri(explanation.target):
                self.set_status(f"Opened settings: {explanation.title}")
                messagebox.showinfo(
                    explanation.title,
                    f"{explanation.explanation}\n\n{explanation.fix}")
            else:
                self.set_status("Settings could not be opened — the address "
                                "was copied to the clipboard instead.")
            return
        if explanation.kind == "command" and explanation.target:
            copied = self._copy_to_clipboard(explanation.target)
            self.set_status(
                "Safe command copied to the clipboard — run it yourself if you "
                "choose." if copied else "Could not copy the command.")
            intro = ("Copied to your clipboard (nothing was run):"
                     if copied else "Run this yourself if you choose:")
            messagebox.showinfo(
                explanation.title,
                f"{explanation.explanation}\n\n{intro}\n\n"
                f"{explanation.target}\n\n{explanation.fix}")
            return
        messagebox.showinfo(explanation.title,
                            f"{explanation.explanation}\n\n{explanation.fix}")

    def _build_reports(self, page) -> None:
        ttk.Label(page, text="Reports, thresholds & safe tools", style="Head.TLabel").pack(anchor="w")
        row = ttk.Frame(page)
        row.pack(fill="x", pady=8)
        ctkw.make_button(row, "ghost", T.BG_MAIN, text="Save JSON",
                         command=lambda: self.export_report("json")).pack(side="left", padx=(0, 8))
        ctkw.make_button(row, "ghost", T.BG_MAIN, text="Save TXT",
                         command=lambda: self.export_report("txt")).pack(side="left", padx=(0, 8))
        ctkw.make_button(row, "ghost", T.BG_MAIN, text="Save CSV",
                         command=lambda: self.export_report("csv")).pack(side="left")
        th_box = tk.LabelFrame(page, text="Thresholds (% / °C) — documented, configurable",
                               bg=T.BG_PANEL, fg=T.NEON_CYAN, font=T.FONT_SMALL,
                               padx=10, pady=8)
        th_box.pack(fill="x", pady=8)
        self.th_vars: dict[str, tk.StringVar] = {}
        for i, (key, val) in enumerate(self.thresholds.items()):
            tk.Label(th_box, text=key, bg=T.BG_PANEL, fg=T.FG_MUTED,
                     font=T.FONT_SMALL).grid(row=i // 4, column=(i % 4) * 2,
                                             sticky="w", padx=4, pady=2)
            var = tk.StringVar(value=str(val))
            ent = ctkw.make_entry(th_box, T.BG_PANEL, textvariable=var, width=8)
            ent.grid(row=i // 4, column=(i % 4) * 2 + 1, padx=4, pady=2, sticky="w")
            self.th_vars[key] = var
        ctkw.make_button(th_box, "ghost", T.BG_PANEL, text="Apply thresholds",
                         command=self.apply_thresholds).grid(row=3, column=6, padx=8)
        clean_box = tk.LabelFrame(page, text="Safe temp-file cleanup (optional, preview first)",
                                  bg=T.BG_PANEL, fg=T.NEON_CYAN, font=T.FONT_SMALL,
                                  padx=10, pady=8)
        clean_box.pack(fill="both", expand=True)
        ttk.Label(clean_box, text=f"Approved location only: {approved_temp_root()} "
                  "(top-level files, safe suffixes, never recursive).",
                  font=T.FONT_SMALL, foreground=T.FG_MUTED,
                  background=T.BG_PANEL).pack(anchor="w")
        brow = ttk.Frame(clean_box)
        brow.pack(fill="x", pady=6)
        ctkw.make_button(brow, "ghost", T.BG_PANEL, text="Preview files",
                         command=self.preview_cleanup).pack(side="left", padx=(0, 8))
        ctkw.make_button(brow, "ghost", T.BG_PANEL, text="Delete previewed files",
                         command=self.clean_previewed).pack(side="left")
        self.clean_list = tk.Text(clean_box, height=8, bg=T.BG_OUTPUT,
                                  fg=T.FG_PRIMARY, font=T.FONT_MONO, wrap="word",
                                  insertbackground=T.NEON_CYAN,
                                  state="disabled")
        self.clean_list.pack(fill="both", expand=True)
        self._previewed: list[Path] = []

        tools_row = ttk.Frame(page)
        tools_row.pack(fill="both", expand=True, pady=(8, 0))
        big_box = tk.LabelFrame(
            tools_row, text="Largest files (read-only — nothing is deleted)",
            bg=T.BG_PANEL, fg=T.NEON_CYAN, font=T.FONT_SMALL, padx=10, pady=8)
        big_box.pack(side="left", fill="both", expand=True, padx=(0, 8))
        self.bigfiles_var = tk.StringVar(value="")
        brow = ttk.Frame(big_box)
        brow.pack(fill="x", pady=(0, 6))
        tk.Label(brow, textvariable=self.bigfiles_var, bg=T.BG_PANEL,
                 fg=T.FG_MUTED, font=T.FONT_SMALL).pack(side="left", padx=(0, 8))
        ctkw.make_button(brow, "ghost", T.BG_PANEL, text="Choose folder…",
                         command=self.choose_big_files_folder).pack(side="right",
                                                                    padx=(8, 0))
        self.bigfiles_btn = ctkw.make_button(brow, "ghost", T.BG_PANEL,
                                             text="Find largest files",
                                             command=self.find_largest_files)
        self.bigfiles_btn.pack(side="right")
        self.bigfiles_list = tk.Text(big_box, height=10, bg=T.BG_OUTPUT,
                                     fg=T.FG_PRIMARY, font=T.FONT_MONO,
                                     insertbackground=T.NEON_CYAN,
                                     wrap="word", state="disabled")
        self.bigfiles_list.pack(fill="both", expand=True)

        sched_box = tk.LabelFrame(
            tools_row, text="Scheduled weekly scan (runs headless, your machine)",
            bg=T.BG_PANEL, fg=T.NEON_CYAN, font=T.FONT_SMALL, padx=10, pady=8)
        sched_box.pack(side="left", fill="both", expand=True)
        ttk.Label(sched_box, text="Registers this scanner in Windows Task "
                  "Scheduler (schtasks) or on a cron line (Linux/macOS).\nThe "
                  "scan runs with the documented flags: --scan --quiet "
                  "--no-history (writes nothing).",
                  font=T.FONT_SMALL, foreground=T.FG_MUTED,
                  background=T.BG_PANEL).pack(anchor="w", pady=(0, 6))
        srow = ttk.Frame(sched_box)
        srow.pack(fill="x", pady=(0, 6))
        tk.Label(srow, text="Day:", bg=T.BG_PANEL, fg=T.FG_MUTED,
                 font=T.FONT_SMALL).pack(side="left")
        self.schedule_day_var = tk.StringVar(value="MON")
        ttk.Combobox(srow, textvariable=self.schedule_day_var,
                     values=list(VALID_DAYS), width=6,
                     state="readonly").pack(side="left", padx=(4, 12))
        tk.Label(srow, text="Time (HH:MM):", bg=T.BG_PANEL, fg=T.FG_MUTED,
                 font=T.FONT_SMALL).pack(side="left")
        self.schedule_time_var = tk.StringVar(value="09:00")
        ctkw.make_entry(srow, T.BG_PANEL, textvariable=self.schedule_time_var,
                        width=7).pack(side="left", padx=(4, 12))
        ctkw.make_button(srow, "ghost", T.BG_PANEL, text="Register weekly scan",
                         command=self.register_schedule).pack(side="left",
                                                              padx=(0, 8))
        ctkw.make_button(srow, "ghost", T.BG_PANEL, text="Remove schedule",
                         command=self.remove_schedule).pack(side="left")

    # ---------- navigation / status ----------
    def show_page(self, name: str) -> None:
        self.pages[name].lift()
        self.current_page = name
        self.page_title.configure(text=name)
        # CustomTkinter buttons have no ttk "pressed" state: the active nav
        # item is highlighted by its colours and icon instead (solid
        # background + neon-cyan icon and label).
        for key, btn in self.nav_buttons.items():
            active = key == name
            btn.configure(
                fg_color=T.BTN_SIDE_ACTIVE_BG if active else T.BTN_SIDE_BG,
                text_color=T.BTN_SIDE_ACTIVE_FG if active else T.BTN_SIDE_FG,
                hover_color=T.BTN_SIDE_ACTIVE_BG if active else T.BTN_SIDE_HOVER,
                bg_color=T.BG_SIDEBAR,
                image=self._nav_icon(key, active))
        self.set_status(f"Viewing {name}.")

    def set_status(self, msg: str) -> None:
        self.status_var.set(msg)

    # ---------- theme (dark default, live light toggle) ----------
    # Widget roles whose colours both palettes define; the swap map pairs each
    # value with its counterpart via the shared palette key.
    _THEME_SWAP_CLASSES = ("Frame", "Label", "Text", "Canvas",
                           "Labelframe", "Toplevel")

    def _toggle_theme(self) -> None:
        self.apply_theme("dark" if T.get_mode() == "light" else "light")

    def apply_theme(self, mode: str) -> None:
        """Switch palette live: ttk styles rebuild, plain widgets walk.

        ttk widgets pick the new colours up from the rebuilt styles; plain
        tk widgets keep construction-time colours, so every descendant is
        re-configured through the palette swap map. Dynamic colours (status
        dots, output tags, chart) are re-rendered from the new palette.
        """
        mode = "light" if str(mode).lower() == "light" else "dark"
        if T.get_mode() == mode:
            self.set_status(f"Theme: {mode}.")   # same mode = pure no-op
            return
        T.set_mode(mode)
        self._build_style()
        self._retheme_plain_widgets()
        self._retheme_text_tags()
        # Navigation highlight reads the freshly swapped palette.
        try:
            self.show_page(getattr(self, "current_page", "Dashboard"))
        except (KeyError, tk.TclError):
            pass
        if self.chart is not None:
            self.chart.set_palette(T.BG_CARD, T.FG_PRIMARY, T.BORDER,
                                   T.NEON_CYAN, T.NEON_PURPLE)
        # Vector icons carry colours baked into their bitmaps: redraw them
        # from the freshly swapped palette.
        self._refresh_card_icons()
        self.theme_btn.configure(
            text="◐  Switch to dark" if mode == "light" else "◐  Switch to light")
        self.set_status(f"Theme: {mode}.")
        if self.latest_snap is not None:
            try:
                self._apply_snapshot(self.latest_snap)  # re-render dot colours
            except Exception:  # noqa: BLE001 - re-render must never break toggling
                log_app.debug("snapshot re-render after theme change failed",
                          exc_info=True)

    def _refresh_card_icons(self) -> None:
        """Re-render the dashboard's vector icons after a palette swap.

        Icons are colour-bearing bitmaps, so a live theme toggle must draw
        them again from the newly active palette (the nav buttons get the
        same treatment inside `show_page`).
        """
        try:
            for icon_label, icon_name, accent_key in self._card_icons.values():
                icon_label.configure(
                    image=icons.get_icon(icon_name, 22, getattr(T, accent_key)),
                    bg_color=T.BG_CARD)
            health_icon = getattr(self, "health_icon", None)
            if health_icon is not None:
                health_icon.configure(
                    image=icons.get_icon("security", 28, T.NEON_CYAN),
                    bg_color=T.BG_CARD)
        except (tk.TclError, KeyError, AttributeError) as exc:
            log_app.debug("card icon re-render skipped: %s", exc)

    def _retheme_plain_widgets(self) -> None:
        """Re-colour every plain (non-ttk) widget via the palette swap map."""
        dark, light = T.palettes()
        swap: dict[str, str] = {}
        for key in T.PALETTE_KEYS:
            # First-wins: two palette keys may share a colour, but must agree
            # on their counterpart (see the note in theme.retheme_widget).
            swap.setdefault(dark[key], light[key])
            swap.setdefault(light[key], dark[key])

        def _descendants(widget) -> "tuple[tk.Misc, ...]":
            for child in widget.winfo_children():
                yield child
                yield from _descendants(child)

        for widget in _descendants(self):
            # CustomTkinter widgets know their role from construction
            # (theme.make_* factories) and re-colour straight from the palette.
            # Un-tagged CTk children (internal widgets, plain CTkLabel images)
            # are skipped as well: their colours are owned by the parent CTk
            # widget's redraw, and re-colouring them here would corrupt it.
            if T.retheme_widget(widget) or isinstance(widget, ctk.CTkBaseClass):
                continue
            try:
                cls = widget.winfo_class()
            except tk.TclError:
                continue
            if cls not in self._THEME_SWAP_CLASSES:
                continue  # ttk widgets follow the rebuilt styles
            for option in ("bg", "foreground", "highlightbackground",
                           "insertbackground"):
                try:
                    current = str(widget.cget(option))
                    new_value = swap.get(current)
                    if new_value:
                        widget.configure({option: new_value})
                except Exception:  # noqa: BLE001 - option not supported here, skip
                    pass

    def _retheme_text_tags(self) -> None:
        try:
            for tag, colour in T.STATUS_COLOURS.items():
                self.diag_text.tag_configure(tag, foreground=colour)
        except (tk.TclError, AttributeError):
            pass

    def _log(self, msg: str, tag: str = "INFO") -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{stamp}] {msg}\n"
        try:
            self.diag_text.configure(state="normal")
            self.diag_text.insert("end", line, tag if tag in T.STATUS_COLOURS else None)
            self.diag_text.see("end")
            self.diag_text.configure(state="disabled")
        except (tk.TclError, AttributeError):
            pass
        if tag == "FAIL":
            log_app.error(msg)
        elif tag == "WARNING":
            log_app.warning(msg)
        else:
            log_app.info(msg)

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
            log_app.warning("refresh thread failed to start: %s", exc)
            self._refresh_active = False
            self.set_status("Refresh unavailable: background thread could not start.")
            return

    def _collect_job(self) -> None:
        try:
            snap = collect_snapshot(include_gpu=True)
            item = ("snapshot", snap)
        except Exception as exc:  # never crash GUI
            log_app.exception("collect failed")
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
        self.head_status.configure(text="● scanning…", fg=T.NEON_YELLOW)
        self.set_status("Running diagnostic scan…")
        self._log("Starting diagnostic scan…", "INFO")
        try:
            threading.Thread(target=self._scan_job, name="shds-scan", daemon=True).start()
        except (RuntimeError, OSError) as exc:
            log_app.warning("scan thread failed to start: %s", exc)
            self._scan_active = False
            try:
                self.scan_bar.stop()
            except tk.TclError:
                pass
            self.head_status.configure(text="● idle", fg=T.FG_MUTED)
            self.set_status("Scan unavailable: background thread could not start.")

    def _scan_job(self) -> None:
        try:
            snap = collect_snapshot(include_gpu=True)
            diag = run_diagnostics(snap, self.thresholds)
            payload = build_report_dict(snap, diag, self.thresholds)
            self._bg_queue.put(("scan", (snap, diag, payload)))
        except Exception as exc:
            log_app.exception("scan failed")
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
            try:
                fn()
            except tk.TclError:
                # The window died between scheduling and firing (a destroy
                # path that bypassed on_closing): never leak a callback
                # traceback through report_callback_exception.
                pass

        try:
            holder["id"] = self.after(ms, _run)
            self._after_ids.add(holder["id"])
        except tk.TclError:
            pass

    def _poll_bg_queue(self) -> None:
        try:
            while True:
                try:
                    kind, data = self._bg_queue.get_nowait()
                except queue.Empty:
                    break
                # Each item is handled in isolation: a handler that raises must
                # not abort the rest of the drain, otherwise the items queued
                # behind it (e.g. the "scan_done" that clears the scan state)
                # are delayed by a full poll interval.
                try:
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
                        self.head_status.configure(
                            text="● live" if self.monitor_running else "● idle",
                            fg=T.NEON_GREEN if self.monitor_running else T.FG_MUTED)
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
                    elif kind == "auto_log":
                        # Auto-action events arrive from worker threads with
                        # no tkinter access; log them here on the main thread.
                        message, tag = data
                        self._log(message, tag)
                        if tag in ("FAIL", "WARNING"):
                            self.set_status(message)
                except Exception:  # noqa: BLE001
                    # One poisoned item must not kill the loop.
                    log_app.exception("background queue handler failed; polling continues")
        finally:
            # Runs even when a handler failed, so the loop always reschedules.
            self._reschedule(250, self._poll_bg_queue)

    def _apply_snapshot(self, snap: HardwareSnapshot) -> None:
        self.latest_snap = snap
        val_cpu, bar_cpu, det_cpu, dot_cpu = self.cards["CPU"]
        if snap.cpu.available and snap.cpu.percent is not None:
            val_cpu.configure(text=f"{snap.cpu.percent:.1f} %")
            bar_cpu.configure(value=snap.cpu.percent)
            st = _status_of_pct(snap.cpu.percent, self.thresholds["cpu_warn"],
                                self.thresholds["cpu_fail"])
            dot_cpu.configure(fg=T.STATUS_COLOURS.get(st, T.FG_DIM))
            det_cpu.configure(text=f"{snap.cpu.logical_cores or '?'} logical cores · "
                              f"{snap.cpu.freq_current_mhz or '—'} MHz")
        else:
            val_cpu.configure(text="Unavailable")
            bar_cpu.configure(value=0)
            dot_cpu.configure(fg=T.FG_DIM)
            det_cpu.configure(text=snap.cpu.error or "psutil unavailable")
        val_m, bar_m, det_m, dot_m = self.cards["RAM"]
        if snap.mem.available and snap.mem.percent is not None:
            val_m.configure(text=f"{snap.mem.percent:.1f} %")
            bar_m.configure(value=snap.mem.percent)
            st = _status_of_pct(snap.mem.percent, self.thresholds["mem_warn"],
                                self.thresholds["mem_fail"])
            dot_m.configure(fg=T.STATUS_COLOURS.get(st, T.FG_DIM))
            det_m.configure(text=f"{format_bytes(snap.mem.used)} / {format_bytes(snap.mem.total)}")
        else:
            val_m.configure(text="Unavailable")
            bar_m.configure(value=0)
            dot_m.configure(fg=T.FG_DIM)
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
            # Colour the dot by the same thresholds the diagnostics use, so the
            # dashboard cannot show a green "healthy" dot for a GPU that the
            # scan rates as FAIL (e.g. overheating).
            if first.temperature_c is not None:
                gpu_st = _status_of_pct(first.temperature_c,
                                        self.thresholds.get("gpu_temp_warn", 83.0),
                                        self.thresholds.get("gpu_temp_fail", 95.0))
            elif first.utilization_pct is not None:
                # No temperature reported: utilisation alone is not a failure
                # condition, so the dot stays neutral-green (PASS).
                gpu_st = "PASS"
            else:
                gpu_st = "UNAVAILABLE"
            dot_g.configure(fg=T.STATUS_COLOURS.get(gpu_st, T.FG_DIM))
            temp = f"{first.temperature_c:.0f}C" if first.temperature_c is not None else "temp n/a"
            det_g.configure(text=f"{first.name[:22]} · {temp}")
        else:
            val_g.configure(text="Unavailable")
            bar_g.configure(value=0)
            dot_g.configure(fg=T.FG_DIM)
            det_g.configure(text=(g.message[:42] if g.message else "unsupported"))
        val_d, bar_d, det_d, dot_d = self.cards["Disk"]
        acc = [d for d in snap.disks if d.accessible and d.percent is not None]
        if acc:
            worst = max(acc, key=lambda d: d.percent or 0)
            assert worst.percent is not None
            val_d.configure(text=f"{worst.percent:.0f} %")
            bar_d.configure(value=worst.percent)
            st = _status_of_pct(worst.percent, self.thresholds["disk_warn"],
                                self.thresholds["disk_fail"])
            dot_d.configure(fg=T.STATUS_COLOURS.get(st, T.FG_DIM))
            det_d.configure(text=f"{worst.mountpoint} · {len(acc)} volume(s)")
        else:
            val_d.configure(text="Unavailable")
            bar_d.configure(value=0)
            dot_d.configure(fg=T.FG_DIM)
            det_d.configure(text="no accessible volumes")
        val_n, bar_n, det_n, dot_n = self.cards["Network"]
        if snap.net.available:
            val_n.configure(text=f"{len(snap.net.interfaces)} ifaces")
            bar_n.configure(value=0)
            dot_n.configure(fg=T.NEON_GREEN)
            if snap.net.down_bps is not None and snap.net.up_bps is not None:
                det_n.configure(text=f"↓ {snap.net.down_bps/1e6:.2f} Mbps · "
                                f"↑ {snap.net.up_bps/1e6:.2f} Mbps")
            else:
                det_n.configure(text=", ".join(snap.net.interfaces[:3]) or "no traffic yet")
        else:
            val_n.configure(text="Unavailable")
            bar_n.configure(value=0)
            dot_n.configure(fg=T.FG_DIM)
            det_n.configure(text="counters unavailable")
        self.set_status(f"Updated {datetime.now().strftime('%H:%M:%S')} — host {snap.hostname}")
        self._update_system_health()
        self.refresh_processes()

    def _update_system_health(self) -> None:
        """Recompute the dashboard System Health score (never raises)."""
        score_var = getattr(self, "health_var", None)
        if score_var is None:
            return
        findings = (self.latest_diag.findings
                    if self.latest_diag is not None else None)
        try:
            health = system_health_score(self.latest_snap, findings)
        except Exception:  # noqa: BLE001 - cosmetic; never break a refresh
            log_app.debug("system health score failed", exc_info=True)
            return
        total = health.get("total")
        if total is None:
            score_var.set("—")
            self.health_label.configure(fg=T.FG_DIM)
            self.health_bar.set(0)
            self.health_detail.configure(
                text="Waiting for the first metrics sample…")
            return
        score_var.set(str(total))
        if total >= 80:
            colour = T.STATUS_COLOURS.get("PASS", T.NEON_GREEN)
        elif total >= 60:
            colour = T.STATUS_COLOURS.get("WARNING", T.NEON_YELLOW)
        else:
            colour = T.STATUS_COLOURS.get("FAIL", T.NEON_RED)
        self.health_label.configure(fg=colour)
        self.health_bar.configure(progress_color=colour)
        self.health_bar.set(total)
        parts = health.get("components", {})
        bits = [f"{label} {parts[key]:.0f}"
                for key, label in (("cpu", "CPU"), ("mem", "RAM"),
                                   ("disk", "Disk")) if key in parts]
        if "os" in parts:
            bits.append(f"OS {health.get('os_fail', 0)} fail / "
                        f"{health.get('os_warning', 0)} warn")
            note = ""
        else:
            note = " · run a scan for OS checks"
        self.health_detail.configure(text=" · ".join(bits) + note)

    def _apply_diag(self, diag: DiagnosticReport, payload: dict) -> None:
        # Compare against the previous scan *before* overwriting it.
        had_previous = self.previous_diag is not None
        self.last_comparison = compare_reports(self.previous_diag, diag)
        self.previous_diag = diag
        self.previous_payload = self.latest_payload
        self.latest_diag = diag
        self.latest_payload = payload
        self.scan_var.set(f"Last scan: {payload.get('timestamp', '')} "
                          f"({diag.duration_s:.1f}s)")
        for f in diag.findings:
            self._log(f"[{f.status}] {f.component}: {f.message}", f.status)
            if f.detail:
                self._log(f"    detail: {f.detail}", "INFO")
        if had_previous:
            self._log_comparison(self.last_comparison)
        else:
            # The very first scan has nothing to compare against: saying
            # "no changes since last scan" would be a false claim.
            self._log("— First scan: no previous results to compare against —", "INFO")
        self._log("— Recommendations —", "INFO")
        for r in diag.recommendations:
            self._log(f" • {r}", "INFO")
        s = diag.summary
        self._log(f"Summary: PASS={s.get('PASS',0)} WARNING={s.get('WARNING',0)} "
                  f"FAIL={s.get('FAIL',0)} UNAVAILABLE={s.get('UNAVAILABLE',0)}", "INFO")
        self._render_fix_buttons(diag.findings)
        self._update_system_health()
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
            self._log("— Changes since last scan: none (same statuses) —", "INFO")
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

    # ---------- diff HTML export ----------
    def export_diff_html(self) -> None:
        """Write a self-contained HTML diff of the last two scans."""
        if self.latest_payload is None:
            messagebox.showinfo("Nothing to diff", "Run a diagnostic scan first.")
            return
        if self.last_comparison is None or self.previous_payload is None:
            # `previous_payload` is the honest "was there really a scan before
            # this one?" marker: `previous_diag` is already set after the
            # first scan, so checking it would let a one-scan diff through.
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
            log_app.warning("smart query failed: %s", exc)
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
        ctkw.apply_button_state(self.bigfiles_btn, "disabled")
        threading.Thread(target=self._bigfiles_job, args=(folder,),
                         name="shds-bigfiles", daemon=True).start()
        self.set_status("Scanning for largest files…")

    def _bigfiles_job(self, folder: str) -> None:
        try:
            files, note = largest_files(folder, limit=25, time_budget_s=20.0)
        except Exception as exc:  # noqa: BLE001 - degrade, never crash GUI
            log_app.warning("largest-files scan failed: %s", exc)
            self._bg_queue.put(("bigfiles", ([], f"Scan failed: {exc}", folder)))
            return
        self._bg_queue.put(("bigfiles", (files, note, folder)))

    def _render_bigfiles(self, data) -> None:
        files, note, folder = data
        ctkw.apply_button_state(self.bigfiles_btn, "normal")
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
        script = self._package_entry_script()
        if day not in VALID_DAYS:
            messagebox.showerror("Invalid day",
                                 f"Choose one of: {', '.join(VALID_DAYS)}.")
            return
        if not validate_time(when):
            messagebox.showerror("Invalid time",
                                 "Time must be HH:MM (24-hour), e.g. 09:30.")
            return
        if platform.system().lower() == "windows":
            try:
                argv = build_schtasks_argv(python_exe, script, day, when)
            except ValueError as exc:
                messagebox.showerror("Invalid schedule", str(exc))
                return
            shown = subprocess.list2cmdline(argv)
            summary = (f"Create Windows scheduled task \"{SCHTASKS_TASK_NAME}\" "
                       f"({day} {when}):\n{shown}")
        else:
            try:
                line = build_cron_line(python_exe, script, day, when)
            except ValueError as exc:
                messagebox.showerror("Invalid schedule", str(exc))
                return
            summary = (f"Add this line to your crontab (runs on {day}, {when}):\n"
                       f"\n    {line}\n\nOther cron jobs are preserved; "
                       "re-registering replaces the previous SHDS entry.")
        if not messagebox.askyesno("Confirm scheduled scan", summary
                                   + "\n\nRegister it now?"):
            return
        threading.Thread(target=self._schedule_job, args=(day, when),
                         name="shds-schedule", daemon=True).start()

    def _package_entry_script(self) -> str:
        """Runnable entry point for the scheduled scan: <project>/main.py."""
        package_dir = Path(__file__).resolve().parent.parent
        return str(package_dir / "main.py")

    def _schedule_job(self, day: str, when: str) -> None:
        """Runs off-thread; communicates the outcome via the background queue."""
        python_exe = sys.executable
        script = self._package_entry_script()
        is_windows = platform.system().lower() == "windows"
        try:
            if is_windows:
                argv = build_schtasks_argv(python_exe, script, day, when)
                proc = subprocess.run(argv, shell=False, capture_output=True,
                                      text=True, encoding="utf-8",
                                      errors="replace", timeout=8)
                if proc.returncode == 0:
                    self._bg_queue.put(("schedule", (True,
                        f"Windows task registered ({day} {when}).")))
                else:
                    err = (proc.stderr or proc.stdout or "").strip()[:200]
                    self._bg_queue.put(("schedule", (False,
                        f"schtasks failed (exit {proc.returncode}): {err}")))
            else:
                try:
                    proc = subprocess.run(["crontab", "-l"], shell=False,
                                          capture_output=True, text=True,
                                          encoding="utf-8", errors="replace",
                                          timeout=8)
                except FileNotFoundError:
                    self._bg_queue.put(("schedule", (False,
                        "crontab command not found on this system.")))
                    return
                if proc.returncode == 0:
                    current = proc.stdout
                elif "no crontab" in (proc.stdout + proc.stderr).lower():
                    current = None  # normal for first-time users
                else:
                    err = (proc.stderr or "").strip()[:200]
                    self._bg_queue.put(("schedule", (False,
                        f"Cannot read crontab: {err or 'access denied'}")))
                    return
                crontab_text = merge_crontab(current)
                crontab_text = (crontab_text.rstrip("\n") + "\n"
                                if crontab_text else "")
                crontab_text += (build_cron_line(python_exe, script, day, when)
                                 + "\n")
                proc = subprocess.run(["crontab", "-"], shell=False,
                                      input=crontab_text, capture_output=True,
                                      text=True, encoding="utf-8",
                                      errors="replace", timeout=8)
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
                argv = ["schtasks", "/Delete", "/TN", SCHTASKS_TASK_NAME, "/F"]
                proc = subprocess.run(argv, shell=False, capture_output=True,
                                      text=True, encoding="utf-8",
                                      errors="replace", timeout=8)
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
                                      capture_output=True, text=True,
                                      encoding="utf-8", errors="replace", timeout=8)
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
                                      text=True, encoding="utf-8",
                                      errors="replace", timeout=8)
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
        ctkw.apply_button_state(self._update_btn, "disabled")
        self.set_status("Checking for updates…")
        threading.Thread(target=self._update_job, name="shds-update",
                         daemon=True).start()

    def _update_job(self) -> None:
        try:
            result = check_for_update()
        except Exception as exc:  # noqa: BLE001 - degrade, never crash GUI
            log_app.warning("update check failed: %s", exc)
            result = UpdateCheck(detail=f"Update check failed: {exc}")
        self._bg_queue.put(("update", result))

    def _render_update_check(self, result: UpdateCheck) -> None:
        ctkw.apply_button_state(self._update_btn, "normal")
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
            self.head_status.configure(text="● idle", fg=T.FG_MUTED)
            self.set_status("Monitoring stopped.")
            self._log("Live monitoring stopped.", "INFO")
            return
        self._apply_interval()
        if self.monitor.start():
            self.monitor_running = True
            self.mon_btn.configure(text="■  Stop monitoring")
            self.head_status.configure(text="● live", fg=T.NEON_GREEN)
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
                snap = snaps[-1]
                # Monitor snapshots are collected with include_gpu=False, so
                # their GPU status is a default "unsupported" stub. Applying it
                # verbatim would wipe the real GPU reading from the card on
                # every single tick. Keep the last known GPU data instead.
                if (not snap.gpu.supported and not snap.gpu.gpus
                        and self.latest_snap is not None
                        and self.latest_snap.gpu.supported):
                    snap.gpu = self.latest_snap.gpu
                self._apply_snapshot(snap)  # main thread — safe
                # Auto-Action Rules: evaluate on every monitoring tick. The
                # checks are cheap and main-thread safe; actual actions are
                # dispatched to worker threads inside the engine.
                try:
                    self.actions_engine.tick(snap, self.monitor.interval_s)
                except Exception:  # noqa: BLE001 - rules must not break monitoring
                    log_app.exception("auto-action tick failed; monitoring continues")
        except Exception:  # noqa: BLE001 - one bad tick must not kill the loop
            # Without this, an exception during _apply_snapshot silently
            # stopped live monitoring while the header kept saying "live".
            log_app.exception("monitor poll failed; polling continues")
        self._reschedule(self._poll_interval_ms(), self._poll_monitor)

    # ---------- processes ----------
    def refresh_processes(self) -> None:
        # Read the Tkinter variable here on the main thread; the worker only
        # receives a plain string and never touches Tkinter.
        # `_apply_snapshot` calls this on every monitoring tick, so guard
        # against overlapping collectors (a slow process_iter must not pile up
        # daemon threads).
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
        except Exception as exc:  # noqa: BLE001 - ANY start failure must
            # clear the guard flag: refresh_processes only runs while it is
            # False, so a wedged True flag freezes the process table forever.
            log_app.warning("process thread failed to start: %s", exc)
            self._procs_active = False

    def _proc_job(self, sort_by: str) -> None:
        """Worker: collect top processes. Takes plain values only — no Tkinter."""
        try:
            rows = collect_processes(limit=14, sort_by=sort_by)
            self._bg_queue.put(("procs", rows))
        except Exception as exc:
            log_app.warning("process refresh failed: %s", exc)
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
        ext = {"json": ".json", "txt": ".txt", "csv": ".csv"}[fmt]
        types = {"json": [("JSON", "*.json")], "txt": [("Text", "*.txt")],
                 "csv": [("CSV", "*.csv")]}[fmt]
        dest = filedialog.asksaveasfilename(
            defaultextension=ext, filetypes=types,
            initialfile=f"shds-report-{datetime.now().strftime('%Y%m%d-%H%M%S')}{ext}")
        if not dest:
            return
        try:
            path = save_report(self.latest_payload, dest)
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
        self._log(f"Thresholds updated: {self.thresholds}", "INFO")
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
        self.monitor_running = False
        self._monitor_polling = False
        try:
            # stop() (not just request_stop) also arms a helper thread that
            # joins the sampler and clears `monitor.running`: no monitoring
            # loop or stale "live" state may outlive the window.
            self.monitor.stop()
        except Exception:
            pass
        try:
            # Disarm the kill switch: no further auto-action may fire after
            # the window is gone (an in-flight action still finishes on its
            # thread, but nothing new is dispatched).
            self.actions_engine.enabled = False
        except Exception:
            pass
        try:
            # Cancel CustomTkinter's internal animation `after` loop: nothing
            # else stops it on shutdown and it keeps ticking after destroy.
            self.scan_bar.stop()
        except Exception:
            pass
        self._scan_active = False
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

def main() -> int:
    app = ScannerApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
