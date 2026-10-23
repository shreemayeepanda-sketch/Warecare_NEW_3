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
from __future__ import annotations

from PIL import Image, ImageDraw

import customtkinter as ctk

from . import theme as T

__all__ = ["ICON_NAMES", "get_icon", "clear_cache"]

#: Draw at 4x and downscale — smooth, anti-aliased edges at any display size.
_SUPERSAMPLE = 4

#: Cached bitmaps are rendered at `_HI_DPI_SCALE`x the logical size — the
#: physical resolution CTk displays them at (Windows' default widget scaling
#: is 125%). Tk would otherwise upscale the 1x bitmap on screen and blur the
#: anti-aliased strokes; on unscaled (100%) displays the slightly larger
#: bitmap is gently downscaled instead, which stays crisp.
_HI_DPI_SCALE = 1.25

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


def _refresh(d, u, lw, color):
    """Sync arrows (refresh / reload)."""
    d.arc([4.5 * u, 4.5 * u, 19.5 * u, 19.5 * u], start=30, end=270,
          fill=color, width=lw)
    pts = [(17.6 * u, 2.6 * u), (17.4 * u, 8.2 * u), (12.2 * u, 5.2 * u)]
    d.polygon(pts, fill=color)


_PAINTERS = {
    "dashboard": _dashboard,
    "security": _security,
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

    `name` must be one of `ICON_NAMES`. `size` is the *logical* display
    size in pixels: the bitmap is drawn at size * _SUPERSAMPLE and
    downscaled to round(size * _HI_DPI_SCALE) — the physical resolution
    CTk displays it at on scaled screens. `color` is any Tk/PIL hex
    colour; it defaults to the active palette's
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
        bitmap_px = round(size * _HI_DPI_SCALE)
        bitmap = img.resize((bitmap_px, bitmap_px), Image.Resampling.LANCZOS)
        _BITMAP_CACHE[key] = bitmap
    return ctk.CTkImage(light_image=bitmap, dark_image=bitmap, size=(size, size))


def clear_cache() -> None:
    """Drop every cached bitmap (mainly for tests)."""
    _BITMAP_CACHE.clear()
