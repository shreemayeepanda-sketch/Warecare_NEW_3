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
from __future__ import annotations

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
