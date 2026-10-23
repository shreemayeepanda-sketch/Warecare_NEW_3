"""Tests for fix 5: per-user log directory and failure-tolerant logging setup."""
from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scanner import logging_config as lc


class TestLogDirSelection(unittest.TestCase):
    def setUp(self):
        # configure_logging is idempotent per logger; clear handlers between
        # tests so each test configures from scratch.
        logger = logging.getLogger("shds")
        for h in list(logger.handlers):
            logger.removeHandler(h)
            h.close()
        self.addCleanup(self._reset_handlers)

    def _reset_handlers(self):
        logger = logging.getLogger("shds")
        for h in list(logger.handlers):
            logger.removeHandler(h)
            h.close()

    def test_windows_uses_localappdata(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.dict("os.environ", {"LOCALAPPDATA": td}), \
                 patch.object(lc.sys, "platform", "win32"):
                log_dir = lc.get_log_dir()
                self.assertEqual(log_dir,
                                 Path(td) / lc.APP_NAME / "logs")

    def test_windows_falls_back_to_appdata(self):
        env = {"LOCALAPPDATA": "", "APPDATA": r"D:\fake\roaming"}
        with patch.dict("os.environ", env, clear=True), \
             patch.object(lc.sys, "platform", "win32"):
            log_dir = lc.get_log_dir()
            self.assertIn("SmartHardwareDiagnosticScanner", str(log_dir))
            self.assertIn("fake", str(log_dir))

    def test_linux_uses_xdg_or_home(self):
        # Provide a valid HOME: Path.home() on Windows reads USERPROFILE, and
        # expanduser can raise RuntimeError when no home is determinable.
        env = {"XDG_DATA_HOME": ""}
        with patch.dict("os.environ", env, clear=True), \
             patch.object(lc.sys, "platform", "linux"), \
             patch.object(Path, "home", return_value=Path("/home/test")):
            log_dir = lc.get_log_dir()
            self.assertIn(".local", str(log_dir))
            self.assertIn(lc.APP_NAME, str(log_dir))

    def test_macos_uses_application_support(self):
        # Assert the pure path selection, not get_log_dir(): the latter
        # verifies writability and would fall back to the temp directory on
        # Windows, where the faked "/Users/test" path is not creatable.
        with patch.object(Path, "home", return_value=Path("/Users/test")), \
             patch.object(lc.sys, "platform", "darwin"):
            root = lc._user_data_root()
            self.assertIn("Application Support", str(root))

    def test_unwritable_primary_falls_back_to_temp(self):
        # Primary path cannot be created; temp dir is the documented fallback.
        unwritable = Path(tempfile.gettempdir()) / "shds_unwritable_probe" / "x" / "y"
        with patch.object(lc, "_user_data_root", return_value=unwritable), \
             patch.object(lc, "tempfile") as fake_tf:
            fake_tf.gettempdir.return_value = tempfile.gettempdir()
            log_dir = lc.get_log_dir()
            self.assertNotEqual(log_dir.parent.parent, unwritable)
            self.assertTrue(log_dir.exists())

    def test_get_log_dir_never_raises(self):
        # Even with a completely hostile environment, get_log_dir returns a path.
        with patch.object(lc, "_user_data_root",
                          side_effect=OSError("no idea where to write")):
            try:
                result = lc.get_log_dir()
            except OSError:
                self.fail("get_log_dir raised OSError")
            else:
                self.assertIsInstance(result, Path)


class TestConfigureLogging(unittest.TestCase):
    def _fresh_logger(self):
        logger = logging.getLogger("shds")
        for h in list(logger.handlers):
            logger.removeHandler(h)
            h.close()
        return logger

    def setUp(self):
        self.addCleanup(self._fresh_logger)

    def test_configure_returns_working_logger(self):
        logger = lc.configure_logging()
        self.assertEqual(logger.name, "shds")
        self.assertTrue(logger.handlers)
        self.assertFalse(logger.propagate)

    def test_configure_is_idempotent(self):
        logger = lc.configure_logging()
        n = len(logger.handlers)
        again = lc.configure_logging()
        self.assertIs(again, logger)
        self.assertEqual(len(again.handlers), n)

    def test_file_handler_error_does_not_raise(self):
        # Simulate a hostile environment: get_log_dir itself fails. Logging
        # setup must still succeed with a console-only logger.
        self._fresh_logger()
        with patch.object(lc, "get_log_dir", side_effect=PermissionError("denied")):
            logger = lc.configure_logging()  # must not raise
        self.assertTrue(logger.handlers)
        kinds = [type(h).__name__ for h in logger.handlers]
        self.assertNotIn("RotatingFileHandler", kinds)
        self.assertIn("StreamHandler", kinds)

    def test_log_dir_contains_log_file_name(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.dict("os.environ", {"LOCALAPPDATA": td}), \
                 patch.object(lc.sys, "platform", "win32"):
                log_dir = lc.get_log_dir()
                self.assertEqual(log_dir.name, "logs")
                (log_dir / "scanner.log").write_text("probe", encoding="utf-8")
                self.assertTrue((log_dir / "scanner.log").exists())


if __name__ == "__main__":
    unittest.main()
