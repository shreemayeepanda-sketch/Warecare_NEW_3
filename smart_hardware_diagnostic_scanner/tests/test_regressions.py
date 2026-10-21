"""Regression tests for bugs found during the 2026-10 audit.

Each test pins one specific defect so it cannot come back silently.
"""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import shds_gui as g
from scanner import diagnostics as d
from scanner import monitoring as mon
from scanner import reports as r
from scanner import scheduler as s
from scanner import smart as sm
from scanner import updater as u


# ---------------------------------------------------------------------------
# Monitoring: a failed thread start must not wedge monitoring forever
# ---------------------------------------------------------------------------
class TestMonitorStartFailure(unittest.TestCase):
    def test_start_failure_leaves_running_false(self):
        worker = mon.MonitorWorker(interval_s=1.0)
        with patch.object(threading.Thread, "start",
                          side_effect=RuntimeError("can't start new thread")):
            self.assertFalse(worker.start())
        # The bug: `running` stayed True, so every later start() returned
        # False and the app said "Monitoring is already running." forever.
        self.assertFalse(worker.running)
        # A real start must work afterwards.
        self.assertTrue(worker.start())
        worker.stop(timeout=0.1)


class TestMonitorStopDoesNotOrphan(unittest.TestCase):
    def test_restart_does_not_revive_the_previous_thread(self):
        worker = mon.MonitorWorker(interval_s=0.5)
        self.assertTrue(worker.start())
        first = worker._thread
        worker.stop(timeout=0.1)
        # A fresh start must not revive the previous thread: with a shared
        # stop event, start() cleared the flag and the orphan ran forever.
        self.assertTrue(worker.start())
        worker.stop(timeout=0.1)
        # The first thread must terminate on its own.
        deadline = time.monotonic() + 10.0
        while first.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(first.is_alive(),
                         "the stopped monitor thread was revived by a later start()")


# ---------------------------------------------------------------------------
# Diagnostics: a hot second GPU must not be masked by a cool first one
# ---------------------------------------------------------------------------
class TestRecommendationWorstStatus(unittest.TestCase):
    def test_hot_gpu_recommendation_survives_a_cool_gpu(self):
        findings = [
            d.Finding(component="GPU", status="FAIL",
                      message="GPU0 hot", key="GPU:0"),
            d.Finding(component="GPU", status="PASS",
                      message="GPU1 cool", key="GPU:1"),
        ]
        recs = d.build_recommendations(findings)
        self.assertTrue(any("GPU running hot" in r for r in recs), recs)

    def test_unavailable_gpu_recommendation_survives(self):
        findings = [
            d.Finding(component="GPU", status="UNAVAILABLE",
                      message="no tool", key="GPU:0"),
            d.Finding(component="GPU", status="PASS",
                      message="GPU1 fine", key="GPU:1"),
        ]
        recs = d.build_recommendations(findings)
        self.assertTrue(any("GPU metrics unavailable" in r for r in recs), recs)


# ---------------------------------------------------------------------------
# Scheduler: cron paths must be quoted and may not inject a second line
# ---------------------------------------------------------------------------
class TestCronQuoting(unittest.TestCase):
    def test_path_with_spaces_is_quoted(self):
        line = s.build_cron_line("/usr/bin/python3", "/home/dev/My Tools/main.py")
        self.assertIn("'/home/dev/My Tools/main.py'", line)

    def test_plain_paths_are_not_quoted(self):
        line = s.build_cron_line("python3", "main.py", "MON", "09:30")
        self.assertEqual(
            line,
            "30 9 * * 1 python3 main.py --scan --quiet --no-history "
            + s.SCHEDULE_MARKER)

    def test_newline_in_path_is_rejected(self):
        with self.assertRaises(ValueError):
            s.build_cron_line("/usr/bin/python3", "/tmp/x\n* * * * * evil\nmain.py")

    def test_percent_is_escaped(self):
        line = s.build_cron_line("/usr/bin/python3", "/home/100%/main.py")
        self.assertIn(r"\%", line)


# ---------------------------------------------------------------------------
# Updater: a malformed URL must degrade, never raise
# ---------------------------------------------------------------------------
class TestUpdaterMalformedUrl(unittest.TestCase):
    def test_bogus_url_is_unavailable_not_an_exception(self):
        result = u.check_for_update("not-a-url")
        self.assertEqual(result.status, "unavailable")

    def test_url_with_control_characters_is_unavailable(self):
        result = u.check_for_update("http://exa mple.com/x")
        self.assertEqual(result.status, "unavailable")

    def test_non_http_scheme_is_unavailable(self):
        result = u.check_for_update("file:///etc/passwd")
        self.assertEqual(result.status, "unavailable")


# ---------------------------------------------------------------------------
# shds_gui: config handling
# ---------------------------------------------------------------------------
class TestConfigRobustness(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_utf8_bom_config_is_read(self):
        target = self.tmp / "config.json"
        # Notepad and PowerShell write a BOM by default.
        target.write_bytes('﻿{"thresholds": {"cpu_warn": 10}}'.encode("utf-8"))
        data = g.load_config(target)
        self.assertEqual(data.get("thresholds", {}).get("cpu_warn"), 10)

    def test_save_config_never_raises_on_unserialisable_data(self):
        target = self.tmp / "config.json"
        self.assertFalse(g.save_config({"bad": object()}, target))
        # No stray .tmp file may be left behind.
        self.assertFalse((self.tmp / "config.json.tmp").exists())

    def test_bad_geometry_falls_back_to_the_default(self):
        self.assertEqual(g.safe_geometry("abc"), g.DEFAULT_GEOMETRY)
        self.assertEqual(g.safe_geometry(None), g.DEFAULT_GEOMETRY)
        self.assertEqual(g.safe_geometry(""), g.DEFAULT_GEOMETRY)
        self.assertEqual(g.safe_geometry("1180x760"), "1180x760")
        self.assertEqual(g.safe_geometry("900x600+10+20"), "900x600+10+20")


class TestValidateTimeAsciiOnly(unittest.TestCase):
    def test_unicode_digits_are_rejected(self):
        # "０９:００" (full-width) and "٠٩:٠٠" (Arabic-Indic) are digits to
        # str.isdigit() but must not be accepted as a schedule time.
        self.assertFalse(g.validate_time("０９:００"))
        self.assertFalse(g.validate_time("٠٩:٠٠"))

    def test_superscripts_do_not_raise(self):
        # int("²") raises ValueError, which used to abort the Tk callback.
        self.assertFalse(g.validate_time("²²:¹²"))


class TestDayToCronDowRaisesValueError(unittest.TestCase):
    def test_bad_day_raises_value_error_not_key_error(self):
        with self.assertRaises(ValueError):
            g.day_to_cron_dow("FUNDAY")


# ---------------------------------------------------------------------------
# shds_gui: the CLI must not silently ignore a bad --thresholds file
# ---------------------------------------------------------------------------
class TestCliThresholdsFile(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _run(self, args):
        from contextlib import redirect_stdout
        import io
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = g.run_cli(args)
        return code, buf.getvalue()

    def test_missing_thresholds_file_is_an_error(self):
        missing = self.tmp / "nope.json"
        code, _out = self._run(["--scan", "--no-gpu", "--no-history",
                                 "--thresholds", str(missing)])
        self.assertEqual(code, 2)

    def test_corrupt_thresholds_file_is_an_error(self):
        bad = self.tmp / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        code, _out = self._run(["--scan", "--no-gpu", "--no-history",
                                 "--thresholds", str(bad)])
        self.assertEqual(code, 2)

    def test_non_object_thresholds_file_is_an_error(self):
        bad = self.tmp / "list.json"
        bad.write_text("[1, 2, 3]", encoding="utf-8")
        code, _out = self._run(["--scan", "--no-gpu", "--no-history",
                                 "--thresholds", str(bad)])
        self.assertEqual(code, 2)

    def test_valid_thresholds_file_is_applied(self):
        good = self.tmp / "good.json"
        good.write_text(json.dumps({"thresholds": {"cpu_warn": 1, "cpu_fail": 2}}),
                        encoding="utf-8")
        code, _out = self._run(["--scan", "--no-gpu", "--no-history",
                                 "--thresholds", str(good)])
        self.assertIn(code, (0, 1))


# ---------------------------------------------------------------------------
# scanner/reports: the path validator allows .html, so the writer must too
# ---------------------------------------------------------------------------
class TestPackageHtmlReport(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _payload(self):
        return {
            "tool": "SHDS", "timestamp": "2026-10-20T10:00:00",
            "os": "Windows", "python": "3.14", "hostname": "box",
            "scan_duration_s": 1.2, "summary": {"PASS": 1, "WARNING": 0,
                                                "FAIL": 0, "UNAVAILABLE": 0},
            "findings": [{"component": "CPU", "status": "PASS",
                           "message": "ok", "detail": ""}],
            "recommendations": ["All good."],
        }

    def test_html_destination_is_written(self):
        dest = self.tmp / "report.html"
        path = r.save_report(self._payload(), dest)
        self.assertTrue(path.exists())
        text = path.read_text(encoding="utf-8")
        self.assertIn("<!DOCTYPE html>", text)
        self.assertIn("CPU", text)

    def test_html_escapes_untrusted_values(self):
        payload = self._payload()
        payload["hostname"] = '<img src=x onerror="alert(1)">'
        payload["findings"][0]["message"] = "<script>alert(1)</script>"
        dest = self.tmp / "evil.html"
        r.save_report(payload, dest)
        text = dest.read_text(encoding="utf-8")
        # No live tag or live attribute may survive; the payload must appear
        # as inert, escaped text.
        self.assertNotIn("<script>", text)
        self.assertNotIn("<img", text)
        self.assertNotIn('onerror="', text)
        self.assertIn("&lt;script&gt;", text)
        self.assertIn("&lt;img", text)


# ---------------------------------------------------------------------------
# SMART: the "-d <type>" hint and the real device model
# ---------------------------------------------------------------------------
class TestSmartDeviceType(unittest.TestCase):
    def test_health_argv_carries_the_device_type(self):
        self.assertEqual(sm.health_argv("smartctl", "/dev/sdb", "sat"),
                         ["smartctl", "-d", "sat", "--health", "/dev/sdb"])

    def test_health_argv_without_a_type_is_unchanged(self):
        self.assertEqual(sm.health_argv("smartctl", "/dev/sda"),
                         ["smartctl", "--health", "/dev/sda"])

    def test_device_type_is_parsed_from_a_scan_row(self):
        self.assertEqual(sm._device_type_of("/dev/sdb -d sat # /dev/sdb [SAT]"), "sat")
        self.assertEqual(sm._device_type_of("/dev/nvme0 -d nvme # /dev/nvme0"), "nvme")
        self.assertEqual(sm._device_type_of("/dev/sda # no type here"), "")

    def test_model_prefers_device_model_over_model_family(self):
        text = ("Model Family:     Samsung based SSDs\n"
                "Device Model:     Samsung SSD 860 EVO 500GB\n")
        self.assertEqual(sm._model_of(text), "Samsung SSD 860 EVO 500GB")

    def test_model_falls_back_to_the_family(self):
        text = "Model Family:     Samsung based SSDs\n"
        self.assertEqual(sm._model_of(text), "Samsung based SSDs")


if __name__ == "__main__":
    unittest.main()
