"""UI audit tests for the standalone single-file build ``ewarecare.py``.

These tests exercise ONLY the standalone build (never the original package):
the vector icon renderer (``get_icon``), the live dark/light theme toggle of
``ScannerApp`` (every tab, plain tk widgets, ttk styles, tagged CustomTkinter
widgets, canvas/chart colours, text tags and text carets), and object-growth
behaviour of repeated theme toggles.

Ground rules honoured here:
* the real ``ScannerApp`` is driven with ``update()`` pumps — never mainloop();
* no dialogs (``messagebox`` is patched for the constructor path, and no test
  invokes a method that pops one);
* no network, no files written; one Tk root (the ScannerApp itself) per module;
* tests assert CORRECT post-fix behaviour; known pre-fix failures map to the
  numbered bugs in the historical UI audit (Agent 2):
  - ``test_automation_labelframe_follows_theme``   -> Bug 1 (CRITICAL)
  - ``test_reports_labelframes_follow_theme``      -> Bug 1 (CRITICAL)
  - ``test_text_widget_caret_follows_theme``       -> Bug 2 (MEDIUM)
  - ``test_combobox_uses_theme_palette``           -> Bug 3 (MEDIUM)
  - ``test_apply_theme_same_mode_is_a_no_op``      -> Bug 6 (MEDIUM)
"""

import gc
import importlib.util
import sys
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "ewarecare.py"

# ---------------------------------------------------------------------------
# Bootstrap: import the standalone module from its file path. It must be
# registered in sys.modules *before* exec_module because the module builds
# @dataclass classes whose field resolution looks up sys.modules[cls.__module__].
# The `if __name__ == "__main__":` guard (asserted below) keeps importing
# side-effect free — no GUI launches.
# ---------------------------------------------------------------------------
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_spec = importlib.util.spec_from_file_location("ewarecare", MODULE_PATH)
ew = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ew
_spec.loader.exec_module(ew)

# Facts captured immediately after import (before any test creates a root).
_MAIN_GUARD_PRESENT = 'if __name__ == "__main__":' in MODULE_PATH.read_text(
    encoding="utf-8")
_NO_ROOT_AT_IMPORT = not any(isinstance(o, tk.Tk) for o in gc.get_objects())
_NO_APP_AT_IMPORT = not any(isinstance(o, ew.ScannerApp)
                            for o in gc.get_objects())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def pump(app, ms):
    """Pump the Tk event loop for ~ms milliseconds (never mainloop)."""
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        app.update()


def walk(widget):
    """All descendants of `widget` (same recursion the app's re-theme uses)."""
    out = []
    for child in widget.winfo_children():
        out.append(child)
        out.extend(walk(child))
    return out


def same_colour(app, a, b):
    """Compare two Tk colour strings via winfo_rgb (case/format agnostic)."""
    if a == b:
        return True
    try:
        return tuple(app.winfo_rgb(a)) == tuple(app.winfo_rgb(b))
    except tk.TclError:
        return False


def to_light(app):
    if ew.T.get_mode() != "light":
        app.apply_theme("light")
    pump(app, 80)


def to_dark(app):
    if ew.T.get_mode() != "dark":
        app.apply_theme("dark")
    pump(app, 80)


def find_labelframes(parent):
    """tk.LabelFrame group boxes report winfo_class() == "Labelframe"."""
    return [w for w in walk(parent) if w.winfo_class() == "Labelframe"]


def labelframe_by_prefix(parent, prefix):
    for box in find_labelframes(parent):
        if str(box.cget("text")).startswith(prefix):
            return box
    raise AssertionError(f"no Labelframe starting with {prefix!r}")


def ctk_widgets(page, class_name):
    return [w for w in walk(page) if type(w).__name__ == class_name]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def app():
    """The one scanner window for the whole module (single Tk root)."""
    # The constructor only pops a warning dialog when psutil is missing;
    # patch it anyway so no dialog can ever appear from this path.
    with patch.object(ew.messagebox, "showwarning"), \
         patch.object(ew.messagebox, "showinfo"), \
         patch.object(ew.messagebox, "showerror"):
        instance = ew.ScannerApp()
    pump(instance, 700)          # drain the first metrics snapshot + procs
    yield instance
    instance.on_closing()        # cancels after-loops, destroys the window
    pump(instance, 30)


# ---------------------------------------------------------------------------
# 1. Import behaves as a library (main guard, no GUI side effects)
# ---------------------------------------------------------------------------
def test_import_is_side_effect_free():
    assert _MAIN_GUARD_PRESENT, (
        "ewarecare.py lost its `if __name__ == \"__main__\":` guard — "
        "importing it would launch the GUI")
    assert _NO_ROOT_AT_IMPORT
    assert _NO_APP_AT_IMPORT
    assert callable(ew.main)
    assert hasattr(ew, "ScannerApp")


# ---------------------------------------------------------------------------
# 2. The icon renderer: logical-size contract + HiDPI display-resolution bitmaps
# ---------------------------------------------------------------------------
def test_get_icon_renders_logical_size_exactly(app):
    display_scale = ew.icons._HI_DPI_SCALE
    sizes = (8, 20, 22, 28, 37)
    for size in sizes:
        img = ew.icons.get_icon("dashboard", size, "#123456")
        # logical (display) size is exactly the requested size
        assert tuple(img.cget("size")) == (size, size)
        # the cached bitmap is rendered at the physical resolution CTk shows
        # it at (round(size * widget scaling); Windows defaults to 125%): a
        # 1x bitmap would be upscaled on screen and blur the strokes
        display_px = round(size * display_scale)
        bitmap = img.cget("light_image")
        assert bitmap.size == (display_px, display_px)
        assert bitmap.mode == "RGBA"
        assert img.cget("dark_image") is bitmap
        # aspect ratio preserved: square canvas -> square icon
        assert bitmap.width == bitmap.height
        # painting stays inside the supersampled design canvas (no overshoot)
        bbox = bitmap.getbbox()
        assert bbox is not None, "icon rendered empty"
        left, top, right, bottom = bbox
        assert 0 <= left <= right <= display_px
        assert 0 <= top <= bottom <= display_px
        # transparent canvas corners (stroke kept off the corners)
        assert bitmap.getpixel((0, 0))[3] == 0


def test_get_icon_display_size_matches_ctk_scaling(app):
    """CTk displays at round(size * widget scaling); the PhotoImage must
    report exactly that (no hardcoded pixel size overriding the parameter)."""
    scaling = app.mon_btn._get_widget_scaling()
    for size in (12, 20):
        img = ew.icons.get_icon("settings", size, "#445566")
        photo = img.create_scaled_photo_image(scaling, "dark")
        expected = round(size * scaling)
        assert (photo.width(), photo.height()) == (expected, expected)


def test_get_icon_uses_the_colour_parameter(app):
    # the monitor icon's solid hub sits at the centre of the 24x24 design
    # grid, so the cached bitmap's centre pixel is the place to sample (the
    # bitmap renders at round(size * _HI_DPI_SCALE), not the logical size).
    # Everything is Lanczos-downscaled with anti-aliasing (strokes are ~2px
    # at display size), so the centre pixel is compared with a tolerance.
    img = ew.icons.get_icon("monitor", 40, "#FF8800")
    bitmap = img.cget("light_image")
    c = bitmap.width // 2
    r, g, b, a = bitmap.getpixel((c, c))
    assert a >= 200, "icon hub is not opaque"
    assert max(abs(r - 255), abs(g - 136), abs(b - 0)) <= 4


def test_get_icon_default_colour_is_the_active_palette(app):
    to_dark(app)
    img = ew.icons.get_icon("monitor", 40)      # default = active palette
    bitmap = img.cget("light_image")            # centre = the monitor hub
    c = bitmap.width // 2
    r, g, b, a = bitmap.getpixel((c, c))
    hex_color = ew.T.NEON_CYAN                  # dark palette is the default
    expected = tuple(int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    assert a >= 200
    assert max(abs(r - expected[0]), abs(g - expected[1]),
               abs(b - expected[2])) <= 4


def test_get_icon_validates_and_caches(app):
    with pytest.raises(KeyError):
        ew.icons.get_icon("no-such-icon", 20)
    # sizes clamp to >= 8 and truncate floats
    assert tuple(ew.icons.get_icon("cpu", 3).cget("size")) == (8, 8)
    assert tuple(ew.icons.get_icon("cpu", 22.7).cget("size")) == (22, 22)
    # clean slate for cache counting
    ew.icons.clear_cache()
    assert ew.icons._BITMAP_CACHE == {}
    first = ew.icons.get_icon("gpu", 24, "#ABC123")
    second = ew.icons.get_icon("gpu", 24, "#ABC123")
    assert len(ew.icons._BITMAP_CACHE) == 1
    assert first.cget("light_image") is second.cget("light_image")
    assert first is not second          # fresh CTkImage wrapper per call
    # a different colour is a different cache entry (no cross-colour bleed)
    other = ew.icons.get_icon("gpu", 24, "#ABC124")
    assert other.cget("light_image") is not first.cget("light_image")
    assert len(ew.icons._BITMAP_CACHE) == 2
    # clearing the cache works and does not break rendering
    ew.icons.clear_cache()
    assert ew.icons._BITMAP_CACHE == {}
    img = ew.icons.get_icon("gpu", 24, "#ABC123")
    bitmap_px = round(24 * ew.icons._HI_DPI_SCALE)
    assert img.cget("light_image").size == (bitmap_px, bitmap_px)


def test_every_painter_renders_small_and_large(app):
    display_scale = ew.icons._HI_DPI_SCALE
    for name in ew.icons.ICON_NAMES:
        small = ew.icons.get_icon(name, 8, "#102030")
        large = ew.icons.get_icon(name, 64, "#102030")
        small_px = round(8 * display_scale)
        large_px = round(64 * display_scale)
        assert small.cget("light_image").size == (small_px, small_px)
        assert large.cget("light_image").size == (large_px, large_px)
        assert large.cget("light_image").getbbox() is not None


# ---------------------------------------------------------------------------
# 3. Theme toggle: module palette, appearance mode, button label
# ---------------------------------------------------------------------------
def test_theme_toggle_flips_palette_and_ctk_mode(app):
    to_light(app)
    _dark_pal, light_pal = ew.T.palettes()
    assert ew.T.BG_MAIN == light_pal["BG_MAIN"]
    assert ew.T.get_mode() == "light"
    assert ew.ctk.get_appearance_mode().lower() == "light"
    assert app.theme_btn.cget("text") == "◐  Switch to dark"
    to_dark(app)
    dark_pal, _light_pal = ew.T.palettes()
    assert ew.T.BG_MAIN == dark_pal["BG_MAIN"]
    assert app.theme_btn.cget("text") == "◐  Switch to light"


# ---------------------------------------------------------------------------
# 4. Theme toggle: tagged CustomTkinter widgets (factory re-theme path)
# ---------------------------------------------------------------------------
def test_theme_toggle_repaints_tagged_ctk_widgets(app):
    calls = []
    original = ew.T.retheme_widget

    def spy(widget):
        result = original(widget)
        calls.append((widget, result))
        return result

    ew.T.retheme_widget = spy            # instance attr shadows the global
    try:
        to_light(app)
    finally:
        del ew.T.retheme_widget          # fall back to the merged global

    # The factory-tagged population is large: 7 nav buttons, monitor/theme/
    # update buttons, dashboard/diagnostics/reports buttons, card progress
    # bars, menus, the master switch, ~14 entries... The walk must reach a
    # substantial share of them, on every tab.
    assert len(calls) >= 25, f"retheme_widget reached only {len(calls)} widgets"
    themed_ids = {id(w) for w, ok in calls if ok}

    def assert_themed(widget, option, expected_light, expected_dark=None):
        assert id(widget) in themed_ids, f"widget not re-themed: {widget!r}"
        assert same_colour(app, widget.cget(option), expected_light)

    _dark_pal, light_pal = ew.T.palettes()

    def light(key):
        return light_pal[key]

    # Automation tab (the newer tab) must be wired into the re-apply path:
    switch = app.rules_master
    assert id(switch) in themed_ids
    assert same_colour(app, switch.cget("progress_color"),
                       light("NEON_CYAN"))
    menus = ctk_widgets(app.pages["Automation"], "CTkOptionMenu")
    assert len(menus) >= 2
    assert same_colour(app, menus[0].cget("dropdown_fg_color"),
                       light("BG_CARD"))
    entries = ctk_widgets(app.pages["Automation"], "CTkEntry")
    assert len(entries) >= 2
    assert same_colour(app, entries[0].cget("fg_color"), light("ENTRY_BG"))

    # Sidebar + active navigation
    assert_themed(app.mon_btn, "fg_color", light("BTN_ACCENT_BG"))
    assert_themed(app.theme_btn, "fg_color", light("BTN_GHOST_BG"))
    active = app.nav_buttons[getattr(app, "current_page", "Dashboard")]
    assert_themed(active, "hover_color", light("BTN_SIDE_ACTIVE_BG"))
    assert_themed(active, "text_color", light("BTN_SIDE_ACTIVE_FG"))
    inactive = app.nav_buttons["Startup"]
    assert id(inactive) in themed_ids
    assert same_colour(app, inactive.cget("hover_color"),
                       light("BTN_SIDE_HOVER"))

    # Dashboard cards: card frame + card progress bar + health bar
    cpu_card, _col = app._card_slots[0]
    assert id(cpu_card) in themed_ids
    assert same_colour(app, cpu_card.cget("fg_color"), light("BG_CARD"))
    _val, bar, _det, _dot = app.cards["CPU"]
    assert id(bar) in themed_ids
    assert same_colour(app, bar.cget("fg_color"), light("PROGRESS_BG"))
    assert same_colour(app, bar.cget("progress_color"), light("PROGRESS_FG"))
    assert id(app.health_bar) in themed_ids

    # Back to dark: the same widgets map back (first-wins swap is symmetric)
    to_dark(app)
    dark_pal, _light_pal = ew.T.palettes()
    assert same_colour(app, switch.cget("progress_color"),
                       dark_pal["NEON_CYAN"])
    assert same_colour(app, menus[0].cget("dropdown_fg_color"),
                       dark_pal["BG_CARD"])
    assert same_colour(app, app.health_bar.cget("fg_color"),
                       dark_pal["PROGRESS_BG"])
    assert same_colour(app, active.cget("hover_color"),
                       dark_pal["BTN_SIDE_ACTIVE_BG"])


# ---------------------------------------------------------------------------
# 5. Theme toggle: plain tk widgets + rebuilt ttk styles on every tab
# ---------------------------------------------------------------------------
def test_theme_toggle_repaints_plain_widgets_on_every_tab(app):
    to_light(app)
    _dark_pal, light = ew.T.palettes()

    # -- Dashboard: card value label / card title / health detail ----------
    cpu_card, _col = app._card_slots[0]
    val, _bar, _det, _dot = app.cards["CPU"]
    assert same_colour(app, val.cget("bg"), light["BG_CARD"])
    assert same_colour(app, val.cget("fg"), light["FG_PRIMARY"])
    title = next(w for w in walk(cpu_card)
                 if isinstance(w, tk.Label) and str(w.cget("text")) == "CPU")
    assert same_colour(app, title.cget("bg"), light["BG_CARD"])
    assert same_colour(app, title.cget("fg"), light["NEON_CYAN"])
    assert same_colour(app, app.health_detail.cget("fg"), light["FG_MUTED"])

    # -- Diagnostics: the output Text widget --------------------------------
    assert same_colour(app, app.diag_text.cget("bg"), light["BG_OUTPUT"])
    assert same_colour(app, app.diag_text.cget("fg"), light["OUTPUT_FG"])

    # -- Reports & Tools: the two other Text widgets ------------------------
    assert same_colour(app, app.clean_list.cget("bg"), light["BG_OUTPUT"])
    assert same_colour(app, app.bigfiles_list.cget("bg"), light["BG_OUTPUT"])
    assert same_colour(app, app.clean_list.cget("fg"), light["FG_PRIMARY"])

    # -- every tab hosts widgets (naive census, incl. Automation) ----------
    for name in ew.NAV_ITEMS:
        assert len(walk(app.pages[name])) > 0, f"page {name!r} has no widgets"

    # -- ttk widgets follow the rebuilt styles -----------------------------
    style = ttk.Style(app)
    assert same_colour(app, style.lookup("Treeview", "background"),
                       light["BG_PANEL"])
    assert same_colour(app, style.lookup("Treeview.Heading", "background"),
                       light["BG_SIDEBAR"])
    assert same_colour(app, style.lookup("Head.TLabel", "foreground"),
                       light["NEON_CYAN"])
    assert same_colour(app, style.lookup("Muted.TLabel", "foreground"),
                       light["FG_MUTED"])

    to_dark(app)
    dark, _light_pal = ew.T.palettes()
    assert same_colour(app, style.lookup("Treeview", "background"),
                       dark["BG_PANEL"])
    assert same_colour(app, app.diag_text.cget("bg"), dark["BG_OUTPUT"])
    assert same_colour(app, app.clean_list.cget("bg"), dark["BG_OUTPUT"])
    assert same_colour(app, val.cget("bg"), dark["BG_CARD"])
    assert same_colour(app, title.cget("fg"), dark["NEON_CYAN"])


# ---------------------------------------------------------------------------
# 6. Bug 1 (CRITICAL): the tk.LabelFrame group boxes must re-theme.
# Pre-fix: _THEME_SWAP_CLASSES lists "LabelFrame", but
# tk.LabelFrame.winfo_class() returns "Labelframe" -> never matched -> the
# boxes keep the dark palette (#1C1C28 bg / #00E5FF fg) after toggling light.
# ---------------------------------------------------------------------------
def test_automation_labelframe_follows_theme(app):
    to_light(app)
    _dark_pal, light = ew.T.palettes()
    page = app.pages["Automation"]
    assert find_labelframes(page), "Automation lost its 'Add rule' group box"
    add_box = labelframe_by_prefix(page, "Add rule")
    assert same_colour(app, add_box.cget("bg"), light["BG_PANEL"]), (
        "Automation 'Add rule' box keeps the dark background in light mode")
    assert same_colour(app, add_box.cget("fg"), light["NEON_CYAN"]), (
        "Automation 'Add rule' label keeps the dark accent in light mode")
    to_dark(app)
    dark, _light_pal = ew.T.palettes()
    assert same_colour(app, add_box.cget("bg"), dark["BG_PANEL"])
    assert same_colour(app, add_box.cget("fg"), dark["NEON_CYAN"])


def test_reports_labelframes_follow_theme(app):
    to_light(app)
    _dark_pal, light = ew.T.palettes()
    page = app.pages["Reports & Tools"]
    th_box = labelframe_by_prefix(page, "Thresholds")
    assert same_colour(app, th_box.cget("bg"), light["BG_PANEL"]), (
        "Reports 'Thresholds' box keeps the dark background in light mode")
    assert same_colour(app, th_box.cget("fg"), light["NEON_CYAN"])
    clean_box = labelframe_by_prefix(page, "Safe temp-file cleanup")
    assert same_colour(app, clean_box.cget("bg"), light["BG_PANEL"])
    big_box = labelframe_by_prefix(page, "Largest files")
    assert same_colour(app, big_box.cget("bg"), light["BG_PANEL"])
    sched_box = labelframe_by_prefix(page, "Scheduled weekly scan")
    assert same_colour(app, sched_box.cget("bg"), light["BG_PANEL"])
    to_dark(app)
    dark, _light_pal = ew.T.palettes()
    assert same_colour(app, th_box.cget("bg"), dark["BG_PANEL"])
    assert same_colour(app, sched_box.cget("bg"), dark["BG_PANEL"])


# ---------------------------------------------------------------------------
# 7. Bug 2 (MEDIUM): text carets must follow the palette.
# Pre-fix: `insertbackground` is missing from the re-theme option tuple, and
# clean_list / bigfiles_list never set a themed caret at all (they report the
# system default, black — invisible on the dark output background).
# ---------------------------------------------------------------------------
def test_text_widget_caret_follows_theme(app):
    to_light(app)
    _dark_pal, light = ew.T.palettes()
    assert same_colour(app, app.diag_text.cget("insertbackground"),
                       light["NEON_CYAN"]), (
        "the diagnostics output caret keeps the dark-mode accent in light "
        "mode (bright #00E5FF on near-white)")
    assert same_colour(app, app.clean_list.cget("insertbackground"),
                       light["NEON_CYAN"]), (
        "the cleanup list caret is the un-themed system default")
    assert same_colour(app, app.bigfiles_list.cget("insertbackground"),
                       light["NEON_CYAN"]), (
        "the largest-files list caret is the un-themed system default")
    to_dark(app)
    dark, _light_pal = ew.T.palettes()
    assert same_colour(app, app.diag_text.cget("insertbackground"),
                       dark["NEON_CYAN"])
    assert same_colour(app, app.clean_list.cget("insertbackground"),
                       dark["NEON_CYAN"]), (
        "the cleanup list caret is invisible on the dark output background")
    assert same_colour(app, app.bigfiles_list.cget("insertbackground"),
                       dark["NEON_CYAN"])


# ---------------------------------------------------------------------------
# 8. Bug 3 (MEDIUM): ttk TCombobox must follow the palette.
# Pre-fix: _build_style configures TSpinbox/TEntry but never TCombobox, so
# both comboboxes (sidebar refresh interval, report scheduling day) keep the
# clam default light-grey #dcdad5 in BOTH modes.
# ---------------------------------------------------------------------------
def test_combobox_uses_theme_palette(app):
    style = ttk.Style(app)
    to_light(app)
    _dark_pal, light = ew.T.palettes()
    assert same_colour(app, style.lookup("TCombobox", "background"),
                       light["BG_CARD"]), (
        "TCombobox ignores the light palette (stays clam default #dcdad5)")
    assert same_colour(app, style.lookup("TCombobox", "fieldbackground"),
                       light["ENTRY_BG"])
    assert same_colour(app, style.lookup("TCombobox", "foreground"),
                       light["FG_PRIMARY"])
    to_dark(app)
    dark, _light_pal = ew.T.palettes()
    assert same_colour(app, style.lookup("TCombobox", "background"),
                       dark["BG_CARD"]), (
        "TCombobox ignores the dark palette (stays clam default #dcdad5)")
    assert same_colour(app, style.lookup("TCombobox", "fieldbackground"),
                       dark["ENTRY_BG"])
    assert same_colour(app, style.lookup("TCombobox", "foreground"),
                       dark["FG_PRIMARY"])


# ---------------------------------------------------------------------------
# 9. Theme toggle: diag_text colour tags follow the palette
# ---------------------------------------------------------------------------
def test_text_tags_follow_theme(app):
    to_light(app)
    _dark_pal, light = ew.T.palettes()
    for tag, key in (("PASS", "NEON_GREEN"), ("WARNING", "NEON_YELLOW"),
                     ("FAIL", "NEON_RED"), ("INFO", "NEON_CYAN"),
                     ("UNAVAILABLE", "FG_MUTED")):
        assert same_colour(app, app.diag_text.tag_cget(tag, "foreground"),
                           light[key]), f"text tag {tag} kept the dark colour"
    to_dark(app)
    dark, _light_pal = ew.T.palettes()
    for tag, key in (("PASS", "NEON_GREEN"), ("WARNING", "NEON_YELLOW"),
                     ("FAIL", "NEON_RED"), ("INFO", "NEON_CYAN")):
        assert same_colour(app, app.diag_text.tag_cget(tag, "foreground"),
                           dark[key]), f"text tag {tag} kept the light colour"


# ---------------------------------------------------------------------------
# 10. Theme toggle: the matplotlib canvas re-colours (chart set_palette)
# ---------------------------------------------------------------------------
def test_chart_canvas_follows_theme(app):
    if not getattr(ew, "HAS_MPL", False) or getattr(app, "chart", None) is None:
        pytest.skip("matplotlib not available")
    from matplotlib import colors as mpl_colors

    def face_hex(fig_or_ax):
        return mpl_colors.to_hex(fig_or_ax.get_facecolor()).lower()

    to_light(app)
    _dark_pal, light = ew.T.palettes()
    assert face_hex(app.chart.fig) == light["BG_CARD"].lower()
    assert face_hex(app.chart.ax) == light["BG_CARD"].lower()
    assert same_colour(app, app.chart.line_cpu.get_color(), light["NEON_CYAN"])
    assert same_colour(app, app.chart.line_mem.get_color(),
                       light["NEON_PURPLE"])
    to_dark(app)
    dark, _light_pal = ew.T.palettes()
    assert face_hex(app.chart.fig) == dark["BG_CARD"].lower()
    assert same_colour(app, app.chart.line_cpu.get_color(), dark["NEON_CYAN"])


# ---------------------------------------------------------------------------
# 11. Repeated toggles must not leak
# ---------------------------------------------------------------------------
LEAK_THRESHOLD_OBJECTS = 600
"""Max object-graph growth over 5 theme toggles.

Measurement hygiene: the app's own ``refresh_processes`` re-entry guard
(``_procs_active``) is set while measuring, so background psutil collectors
add no noise between toggles — the tracked delta is the theme-toggle path
itself. Measured on the audit machine: growth of -80 (garbage) to about
+80 objects across 5 toggles once collectors are quiet. The bound allows
generous first-toggle warm-up (CTk fonts, ttk style tables, appearance-mode
caches) while still catching real leaks, which accumulate on the order of
hundreds of objects per toggle (retained widget/image wrappers,
ever-growing lists, etc.). Unqualified raw runs (with collector traffic)
measured up to ~1318 objects — hygiene documented in the report.
"""

CTKIMAGE_CENSUS_LIMIT = 300
"""Live CTkImage instances must stay bounded after 5 toggles.

Each toggle re-renders ~13 icons (7 nav + 6 card/health); replaced CTkImages
must be garbage-collected (CTk 6.0.0 removes its configure callback when the
image is replaced). A census far above the live population would mean
replaced images are retained somewhere -> real leak. The census therefore
counts only CTkImages created after the test's baseline gc sweep: in a
whole-suite pytest process earlier files (the package's GUI tests) leave
their own CTkImages behind, and a raw absolute count would report that
pre-existing inventory as this test's leak.
"""


def test_theme_toggle_does_not_leak(app):
    import customtkinter as _ctk

    pump(app, 300)
    app._procs_active = True          # measurement hygiene (see threshold)
    try:
        gc.collect()
        # Census baseline: images alive *before* the toggles. Earlier test
        # files in a whole-suite run keep their own CTkImages alive, so the
        # census below is scoped to images created after this snapshot.
        pre_toggle_ids = {id(o) for o in gc.get_objects()
                          if isinstance(o, _ctk.CTkImage)}
        base_objects = len(gc.get_objects())
        for _ in range(5):
            app.apply_theme("light" if ew.T.get_mode() == "dark" else "dark")
            pump(app, 100)
        gc.collect()
        growth = len(gc.get_objects()) - base_objects
        assert growth <= LEAK_THRESHOLD_OBJECTS, (
            f"5 theme toggles grew the object graph by {growth} objects "
            f"(threshold {LEAK_THRESHOLD_OBJECTS}) — something is retained "
            f"per toggle")
        census = sum(1 for o in gc.get_objects()
                     if isinstance(o, _ctk.CTkImage)
                     and id(o) not in pre_toggle_ids)
        assert census <= CTKIMAGE_CENSUS_LIMIT, (
            f"{census} CTkImage instances created during the toggles are "
            f"still alive after 5 toggles — replaced icon images are not "
            f"being released")
    finally:
        app._procs_active = False
    to_dark(app)


# ---------------------------------------------------------------------------
# 12. The after-loops must survive any number of toggles: no stale timers,
#     no dead poll loop — the background-queue after-id keeps rotating.
# ---------------------------------------------------------------------------
def test_after_poll_loops_survive_repeated_toggles(app):
    to_light(app)
    ids_before = set(app._after_ids)
    assert len(ids_before) >= 1, "background queue poll lost its after-id"
    pump(app, 350)                    # poll interval is 250 ms -> id rotates
    ids_after = set(app._after_ids)
    assert len(ids_after) >= 1, "background queue poll stopped rescheduling"
    assert ids_after != ids_before, (
        "after-id never rotated — the poll loop is stuck or was replaced by "
        "a stale callback")
    for _ in range(3):
        app.apply_theme("light" if ew.T.get_mode() == "dark" else "dark")
        pump(app, 60)
    assert len(app._after_ids) >= 1
    assert app.winfo_exists()
    to_dark(app)


# ---------------------------------------------------------------------------
# 13. Bug 6 (MEDIUM): apply_theme() with the already-active mode is a no-op.
# Pre-fix: the plain-widget walk runs an unconditional dark<->light VALUE
# swap, so re-invoking it with the active mode inverts every plain widget's
# colours. Placed LAST deliberately: pre-fix it fails mid-body and would
# leave the (symmetric) swap inverted, corrupting later exact-colour tests.
# ---------------------------------------------------------------------------
def test_apply_theme_same_mode_is_a_no_op(app):
    to_dark(app)
    val = app.cards["CPU"][0]
    dark, _light = ew.T.palettes()
    assert same_colour(app, val.cget("bg"), dark["BG_CARD"])

    app.apply_theme("dark")               # request the already-active mode
    pump(app, 80)
    assert same_colour(app, val.cget("bg"), dark["BG_CARD"]), (
        "apply_theme('dark') while already dark inverted the widget colours")
    assert ew.T.get_mode() == "dark"

    app.apply_theme("DARK")               # normalised form: still a no-op
    pump(app, 80)
    assert same_colour(app, val.cget("bg"), dark["BG_CARD"])
    assert same_colour(app, app.diag_text.cget("bg"), dark["BG_OUTPUT"])

    # the real toggles still work after the no-op requests
    to_light(app)
    _dark, light = ew.T.palettes()
    assert same_colour(app, val.cget("bg"), light["BG_CARD"])
    assert same_colour(app, app.diag_text.cget("bg"), light["BG_OUTPUT"])
    to_dark(app)
    assert same_colour(app, val.cget("bg"), dark["BG_CARD"])
