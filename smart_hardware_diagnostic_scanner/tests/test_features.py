"""Headless tests for the new feature helpers inside the single-file app."""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import shds_gui as g


class TestSmartHelpers(unittest.TestCase):
    def test_scan_and_health_argv(self):
        self.assertEqual(g.smart_scan_argv("smartctl"), ["smartctl", "--scan"])
        self.assertEqual(g.smart_health_argv("smartctl", "/dev/sda"),
                         ["smartctl", "--health", "/dev/sda"])

    def test_classify_smart_exit(self):
        self.assertEqual(g.classify_smart_exit(0), "PASS")
        self.assertEqual(g.classify_smart_exit(8), "FAIL")
        self.assertEqual(g.classify_smart_exit(9), "FAIL")   # fail + minor bits
        self.assertEqual(g.classify_smart_exit(1), "UNAVAILABLE")
        self.assertEqual(g.classify_smart_exit(4), "UNAVAILABLE")
        self.assertEqual(g.classify_smart_exit("x"), "UNAVAILABLE")

    def test_query_without_tool_degrades(self):
        with patch.object(g, "which", return_value=None):
            status = g.query_smart_devices()
        self.assertFalse(status.tool_found)
        self.assertIn("smartmontools", status.message)

    def test_query_with_tool_mocked(self):
        fake = subprocess.CompletedProcess(args=["smartctl", "--scan"],
                                           returncode=0,
                                           stdout="# 0  /dev/sda -d ata # ...",
                                           stderr="")
        health = subprocess.CompletedProcess(args=["smartctl", "--health",
                                                   "/dev/sda"],
                                             returncode=0,
                                             stdout="Model: TestDisk",
                                             stderr="")

        def _run(argv, **kwargs):
            return fake if "--scan" in argv else health

        with patch.object(g, "which", return_value="smartctl"), \
             patch.object(g.subprocess, "run", side_effect=_run):
            status = g.query_smart_devices()
        self.assertTrue(status.supported)
        self.assertEqual(status.devices[0].status, "PASS")
        self.assertEqual(status.devices[0].model, "TestDisk")


class TestNetviewHelpers(unittest.TestCase):
    def test_format_conn_addr(self):
        self.assertEqual(g._format_conn_addr(("1.2.3.4", 80)), "1.2.3.4:80")
        self.assertEqual(g._format_conn_addr(("::1", 8080)), "[::1]:8080")
        self.assertEqual(g._format_conn_addr(()), "-")
        self.assertEqual(g._format_conn_addr(None), "-")


class TestVersionHelpers(unittest.TestCase):
    def test_parse_tag(self):
        self.assertEqual(g.parse_tag("v1.2.3"), "1.2.3")
        self.assertEqual(g.parse_tag("V2.0"), "2.0")
        self.assertEqual(g.parse_tag(None), "")

    def test_compare_versions(self):
        self.assertEqual(g.compare_versions("1.0.0", "1.0.0"), 0)
        self.assertEqual(g.compare_versions("1.0.0", "1.1.0"), -1)
        self.assertEqual(g.compare_versions("1.2.10", "1.2.9"), 1)
        self.assertEqual(g.compare_versions("1.2", "1.2.0"), 0)
        self.assertEqual(g.compare_versions("abc", "1.0"), 0)

    def test_check_for_update_mocked(self):
        import urllib.error

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, *a):
                return b'{"tag_name": "v1.1.0"}'

        with patch("urllib.request.urlopen", return_value=_Resp()):
            result = g.check_for_update()
        self.assertEqual(result.status, "update_available")
        self.assertEqual(result.latest, "1.1.0")

    def test_check_for_update_http_error(self):
        import urllib.error
        with patch("urllib.request.urlopen",
                   side_effect=urllib.error.HTTPError("u", 404, "nf", None, None)):
            result = g.check_for_update()
        self.assertEqual(result.status, "unavailable")


class TestScheduleHelpers(unittest.TestCase):
    def test_validate_time(self):
        self.assertTrue(g.validate_time("09:30"))
        self.assertFalse(g.validate_time("9:30"))
        self.assertFalse(g.validate_time("24:00"))
        self.assertFalse(g.validate_time("12:60"))

    def test_day_to_cron_dow(self):
        self.assertEqual(g.day_to_cron_dow("MON"), 1)
        self.assertEqual(g.day_to_cron_dow("sun"), 0)

    def test_build_schtasks_argv(self):
        argv = g.build_schtasks_argv("pythonw", r"C:\path app\shds_gui.py",
                                     "THU", "07:15")
        self.assertEqual(argv[0], "schtasks")
        self.assertEqual(argv[8], "THU")
        self.assertEqual(argv[10], "07:15")
        self.assertEqual(argv[11], "/TR")
        self.assertIn('"pythonw"', argv[12])
        self.assertIn("--scan", argv[12])
        self.assertIn("--quiet", argv[12])

    def test_build_schtasks_argv_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            g.build_schtasks_argv("python", "x.py", "FRIDAY")
        with self.assertRaises(ValueError):
            g.build_schtasks_argv("python", "x.py", "MON", "9:00")

    def test_build_cron_line(self):
        line = g.build_cron_line("/usr/bin/python3", "/opt/shds_gui.py",
                                 "SAT", "22:05")
        self.assertTrue(line.startswith("5 22 * * 6 "))
        self.assertIn("--no-history", line)
        self.assertTrue(line.rstrip().endswith(g.SCHEDULE_MARKER))

    def test_strip_and_merge_crontab(self):
        text = ("0 3 * * * backup\n"
                f"15 9 * * 1 py shds_gui.py --scan {g.SCHEDULE_MARKER}\n"
                "0 * * * * hourly\n")
        cleaned, removed = g.strip_scheduled_entries(text)
        self.assertEqual(removed, 1)
        self.assertIn("backup", cleaned)
        self.assertNotIn("SCHEDULE", cleaned)
        merged = g.merge_crontab(text)
        self.assertNotIn("shds_gui.py", merged)
        self.assertEqual(g.merge_crontab(None), "")
        self.assertEqual(g.merge_crontab(""), "")


class TestLargestFiles(unittest.TestCase):
    def _tree(self) -> Path:
        base = Path(tempfile.mkdtemp(prefix="shds_files_"))
        self.addCleanup(self._cleanup, base)
        (base / "sub" / "deep").mkdir(parents=True)
        (base / "big.bin").write_bytes(b"x" * 5000)
        (base / "sub" / "small.txt").write_bytes(b"y" * 10)
        (base / "sub" / "deep" / "deep.bin").write_bytes(b"z" * 2000)
        return base

    @staticmethod
    def _cleanup(base: Path) -> None:
        import shutil
        shutil.rmtree(base, ignore_errors=True)

    def test_top_files_sorted_desc(self):
        base = self._tree()
        files, note = g.largest_files(base)
        sizes = [f.size_bytes for f in files]
        self.assertEqual(sizes, sorted(sizes, reverse=True))
        self.assertEqual(sizes[0], 5000)
        self.assertIn("file(s)", note)

    def test_depth_cap(self):
        base = self._tree()
        files, _note = g.largest_files(base, max_depth=1)
        self.assertTrue(all("deep.bin" not in f.path for f in files))

    def test_missing_path(self):
        files, note = g.largest_files("Z:/definitely/not/here/xyz")
        self.assertEqual(files, [])
        self.assertIn("does not exist", note)

    def test_single_file(self):
        base = self._tree()
        files, _note = g.largest_files(base / "big.bin")
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].size_bytes, 5000)

    def test_budget_zero_is_partial_not_crash(self):
        base = self._tree()
        files, note = g.largest_files(base, time_budget_s=0.0)
        self.assertIsInstance(files, list)
        self.assertIn("budget", note)


class TestStartupEntries(unittest.TestCase):
    def test_desktop_parse(self):
        base = Path(tempfile.mkdtemp(prefix="shds_start_"))
        self.addCleanup(g._rmtree_like(base) if hasattr(g, "_rmtree_like")
                        else self._cleanup, base)
        good = base / "app.desktop"
        good.write_text("[Desktop Entry]\nType=Application\nName=My App\n"
                        "Exec=run --now\n", encoding="utf-8")
        hidden = base / "hid.desktop"
        hidden.write_text("[Desktop Entry]\nType=Application\nName=Cloaked\n"
                          "Exec=nope\nHidden=true\n", encoding="utf-8")
        junk = base / "broken.desktop"
        junk.write_text("garbage with no sections", encoding="utf-8")
        empty = base / "empty.desktop"
        empty.write_text("", encoding="utf-8")
        entries = g._autostart_entries([base])
        names = {e.name: e for e in entries}
        self.assertIn("My App", names)
        self.assertEqual(names["My App"].command, "run --now")
        self.assertIn("Cloaked", names)
        self.assertEqual(names["Cloaked"].note, "hidden")
        self.assertNotIn("broken", names)
        self.assertNotIn("empty", names)

    @staticmethod
    def _cleanup(base: Path) -> None:
        import shutil
        shutil.rmtree(base, ignore_errors=True)

    def test_plist_parse(self):
        try:
            import plistlib
        except ImportError:  # pragma: no cover
            self.skipTest("plistlib unavailable")
        base = Path(tempfile.mkdtemp(prefix="shds_plist_"))
        self.addCleanup(self._cleanup, base)
        good = base / "agent.plist"
        good.write_bytes(plistlib.dumps(
            {"Label": "com.test.agent", "ProgramArguments": ["/usr/bin/py"]}))
        bad = base / "broken.plist"
        bad.write_bytes(b"not a plist")
        entries = g._launchagent_entries([base])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].name, "com.test.agent")
        self.assertEqual(entries[0].command, "/usr/bin/py")

    def test_collect_never_raises(self):
        entries, note = g.collect_startup_entries()
        self.assertIsInstance(entries, list)
        self.assertLessEqual(len(entries), 0 + len(entries))
        self.assertTrue(note)


class TestConnectionRows(unittest.TestCase):
    def test_collect_degrades_without_psutil(self):
        with patch.object(g, "HAS_PSUTIL", False):
            rows, note = g.collect_net_connections()
        self.assertEqual(rows, [])
        self.assertIn("psutil", note)


class TestComparisonHtml(unittest.TestCase):
    def test_renders_sections_and_escapes(self):
        comparison = {
            "regressions": [("CPU", "PASS", "FAIL",
                             "<script>alert('x')</script>")],
            "improvements": [],
            "added": [("Sensors:New", None, "WARNING", "new sensor")],
            "removed": [],
        }
        prev = {"timestamp": "t1", "health_score": 90}
        cur = {"timestamp": "t2", "hostname": "h<ost>", "health_score": 55}
        html_text = g.render_comparison_html(comparison, prev, cur)
        self.assertIn("Worse (1)", html_text)
        self.assertIn("New since last scan (1)", html_text)
        self.assertIn("&lt;script&gt;", html_text)
        self.assertIn("h&lt;ost&gt;", html_text)

    def test_no_changes(self):
        html_text = g.render_comparison_html(
            {"regressions": [], "improvements": [], "added": [], "removed": []},
            None, {"timestamp": "t", "hostname": "h", "health_score": 90})
        self.assertIn("No changes", html_text)


class TestShdsThemePalettes(unittest.TestCase):
    def test_parity_and_round_trip(self):
        dark, light = g.theme_palettes()
        self.assertEqual(set(dark), set(light))
        self.assertEqual(set(dark), set(g.PALETTE_KEYS))
        original = g.get_theme_mode()
        try:
            g.set_theme_mode("light")
            self.assertEqual(g.get_theme_mode(), "light")
            g.set_theme_mode("dark")
            self.assertEqual(g.get_theme_mode(), "dark")
        finally:
            g.set_theme_mode(original)


if __name__ == "__main__":
    unittest.main()
