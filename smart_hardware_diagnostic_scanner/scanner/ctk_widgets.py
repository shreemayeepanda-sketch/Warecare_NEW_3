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
from __future__ import annotations

import customtkinter as ctk

from . import theme as T

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
