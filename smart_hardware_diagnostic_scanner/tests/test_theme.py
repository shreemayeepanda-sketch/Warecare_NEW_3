"""Tests for theme constants and the per-OS font selection."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from scanner import theme as T


class TestFontsPerOs(unittest.TestCase):
    """Fonts are chosen per OS; Tk falls back when a family is missing."""

    def test_windows_fonts(self):
        with patch("platform.system", return_value="Windows"):
            self.assertEqual(T._default_font_family(), "Segoe UI")
            self.assertEqual(T._default_mono_family(), "Consolas")

    def test_macos_fonts(self):
        with patch("platform.system", return_value="Darwin"):
            self.assertEqual(T._default_font_family(), "Helvetica Neue")
            self.assertEqual(T._default_mono_family(), "Menlo")

    def test_linux_fonts(self):
        with patch("platform.system", return_value="Linux"):
            self.assertEqual(T._default_font_family(), "DejaVu Sans")
            self.assertEqual(T._default_mono_family(), "DejaVu Sans Mono")

    def test_font_tuples_use_selected_family(self):
        with patch("platform.system", return_value="Darwin"):
            import importlib
            importlib.reload(T)
            self.assertEqual(T.FONT_TITLE[0], "Helvetica Neue")
            self.assertEqual(T.FONT_MONO[0], "Menlo")
        # Restore the module to the platform's real values for later classes.
        importlib.reload(T)


class TestThemeConstants(unittest.TestCase):
    REQUIRED_KEYS = ("BG_MAIN", "BG_PANEL", "BG_CARD", "BG_SIDEBAR",
                     "BG_OUTPUT", "BG_STATUSBAR", "FG_PRIMARY", "FG_MUTED",
                     "FG_DIM", "NEON_CYAN", "NEON_PURPLE", "NEON_GREEN",
                     "NEON_YELLOW", "NEON_RED", "NEON_ORANGE", "BORDER",
                     "ENTRY_BG", "PROGRESS_BG", "OUTPUT_FG")

    def test_colours_are_valid_hex_both_modes(self):
        dark, light = T.palettes()
        for palette in (dark, light):
            for name in self.REQUIRED_KEYS:
                self.assertRegex(palette[name], r"^#[0-9A-Fa-f]{6}$", name)
            for name in self.REQUIRED_KEYS:
                # Every constant is also readable as a module attribute.
                self.assertRegex(getattr(T, name), r"^#[0-9A-Fa-f]{6}$", name)

    def test_palettes_have_identical_key_sets(self):
        dark, light = T.palettes()
        self.assertEqual(set(dark), set(T.PALETTE_KEYS))
        self.assertEqual(set(light), set(T.PALETTE_KEYS))
        # They must actually differ, or a "toggle" would be a no-op.
        differing = [k for k in T.PALETTE_KEYS if dark[k] != light[k]]
        self.assertGreaterEqual(len(differing), 10)

    def test_mode_round_trip(self):
        original = T.get_mode()
        try:
            T.set_mode("light")
            self.assertEqual(T.get_mode(), "light")
            self.assertEqual(T.BG_MAIN, T.palettes()[1]["BG_MAIN"])
            T.set_mode("dark")
            self.assertEqual(T.get_mode(), "dark")
        finally:
            T.set_mode(original)

    def test_set_mode_normalises_unknown_values_to_dark(self):
        original = T.get_mode()
        try:
            T.set_mode("bogus")
            self.assertEqual(T.get_mode(), "dark")
        finally:
            T.set_mode(original)

    def test_status_colours_cover_all_statuses(self):
        for status in ("PASS", "WARNING", "FAIL", "UNAVAILABLE", "INFO"):
            self.assertIn(status, T.STATUS_COLOURS)

    def test_status_colours_follow_the_active_palette(self):
        original = T.get_mode()
        try:
            T.set_mode("light")
            self.assertEqual(T.STATUS_COLOURS["PASS"], T.NEON_GREEN)
            self.assertEqual(T.STATUS_COLOURS["UNAVAILABLE"], T.FG_MUTED)
        finally:
            T.set_mode(original)


if __name__ == "__main__":
    unittest.main()
