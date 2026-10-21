"""Tests for the single-file app's new features.

All tests here are headless: they import `shds_gui` (which never creates a Tk
root at import time) and exercise the pure logic, the SQLite history store, the
report renderers and the CLI. Hardware access is mocked, so nothing reads or
deletes real hardware state.

Run from the project root:  python -m unittest discover -s tests
"""
from __future__ import annotations

import contextlib
import base64
import io
import json
import os
import queue
import sqlite3
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import shds_gui as g


def _finding(status="PASS", component="CPU", key="CPU", message="msg"):
    return g.Finding(component=component, status=status, message=message, key=key)


def _snapshot(**overrides):
    snap = g.HardwareSnapshot(hostname="test-host", os_info="Test OS")
    for name, value in overrides.items():
        setattr(snap, name, value)
    return snap


def _battery(percent=50.0, plugged=False, secs_left=None):
    return g.BatterySnapshot(available=True, percent=percent, plugged=plugged,
                             secs_left=secs_left)


# ---------------------------------------------------------------------------
# Health score
# ---------------------------------------------------------------------------
class TestHealthScore(unittest.TestCase):
    def test_all_pass_is_perfect(self):
        self.assertEqual(g.compute_health_score([_finding()] * 5), 100)

    def test_empty_findings_is_perfect(self):
        self.assertEqual(g.compute_health_score([]), 100)

    def test_penalties_are_documented_values(self):
        self.assertEqual(g.compute_health_score([_finding("FAIL")]), 80)
        self.assertEqual(g.compute_health_score([_finding("WARNING")]), 93)
        self.assertEqual(g.compute_health_score([_finding("UNAVAILABLE")]), 98)

    def test_missing_data_costs_less_than_real_pressure(self):
        unavailable = g.compute_health_score([_finding("UNAVAILABLE")])
        warning = g.compute_health_score([_finding("WARNING")])
        self.assertGreater(unavailable, warning)

    def test_score_is_clamped_at_zero(self):
        self.assertEqual(g.compute_health_score([_finding("FAIL")] * 20), 0)

    def test_score_is_an_int(self):
        self.assertIsInstance(g.compute_health_score([_finding("WARNING")]), int)

    def test_run_diagnostics_sets_health_score(self):
        report = g.run_diagnostics(_snapshot())
        self.assertIsInstance(report.health_score, int)
        self.assertEqual(report.health_score, g.compute_health_score(report.findings))

    def test_health_labels(self):
        self.assertEqual(g.health_label(100), "Excellent")
        self.assertEqual(g.health_label(90), "Excellent")
        self.assertEqual(g.health_label(75), "Good")
        self.assertEqual(g.health_label(55), "Fair")
        self.assertEqual(g.health_label(35), "Degraded")
        self.assertEqual(g.health_label(0), "Critical")


# ---------------------------------------------------------------------------
# Comparison against the previous scan
# ---------------------------------------------------------------------------
class TestCompareReports(unittest.TestCase):
    def _report(self, findings):
        return g.DiagnosticReport(started_at=0.0, finished_at=1.0, findings=findings)

    def test_no_previous_scan_yields_nothing(self):
        result = g.compare_reports(None, self._report([_finding()]))
        self.assertEqual(result, {"regressions": [], "improvements": [],
                                  "added": [], "removed": []})

    def test_regression_detected(self):
        prev = self._report([_finding("PASS", key="CPU")])
        cur = self._report([_finding("WARNING", key="CPU")])
        result = g.compare_reports(prev, cur)
        self.assertEqual(len(result["regressions"]), 1)
        self.assertEqual(result["regressions"][0][:3], ("CPU", "PASS", "WARNING"))
        self.assertEqual(result["improvements"], [])

    def test_improvement_detected(self):
        prev = self._report([_finding("FAIL", key="Memory", component="Memory")])
        cur = self._report([_finding("PASS", key="Memory", component="Memory")])
        result = g.compare_reports(prev, cur)
        self.assertEqual(len(result["improvements"]), 1)
        self.assertEqual(result["regressions"], [])

    def test_fail_to_unavailable_is_an_improvement(self):
        prev = self._report([_finding("FAIL", key="Sensors")])
        cur = self._report([_finding("UNAVAILABLE", key="Sensors")])
        result = g.compare_reports(prev, cur)
        self.assertEqual(len(result["improvements"]), 1)

    def test_unchanged_is_reported_in_no_bucket(self):
        prev = self._report([_finding("PASS", key="CPU")])
        cur = self._report([_finding("PASS", key="CPU")])
        result = g.compare_reports(prev, cur)
        self.assertTrue(all(not v for v in result.values()))

    def test_added_and_removed_keys(self):
        prev = self._report([_finding("PASS", key="Storage:C:\\")])
        cur = self._report([_finding("PASS", key="Storage:D:\\")])
        result = g.compare_reports(prev, cur)
        self.assertEqual(result["added"][0][0], "Storage:D:\\")
        self.assertEqual(result["removed"][0][0], "Storage:C:\\")

    def test_missing_key_falls_back_to_component(self):
        self.assertEqual(g.finding_key(g.Finding("CPU", "PASS", "m")), "CPU")

    def test_per_volume_storage_findings_have_distinct_keys(self):
        snap = _snapshot(disks=[
            g.DiskEntry(mountpoint="C:\\", fstype="NTFS", total=100, used=50,
                        free=50, percent=50.0, accessible=True),
            g.DiskEntry(mountpoint="D:\\", fstype="NTFS", total=100, used=90,
                        free=10, percent=90.0, accessible=True),
        ])
        keys = [f.key for f in g.diagnose_disks(snap, g.DEFAULT_THRESHOLDS)]
        self.assertEqual(len(keys), 2)
        self.assertEqual(len(set(keys)), 2)

    def test_gpu_findings_are_keyed_per_device(self):
        status = g.GpuStatus(supported=True, tool_found=True, message="ok", gpus=[
            g.GpuInfo(index=0, name="A", temperature_c=50.0, available=True),
            g.GpuInfo(index=1, name="B", temperature_c=99.0, available=True),
        ])
        findings = g.diagnose_gpu(_snapshot(gpu=status), g.DEFAULT_THRESHOLDS)
        self.assertEqual([f.key for f in findings], ["GPU:0", "GPU:1"])
        self.assertEqual([f.status for f in findings], ["PASS", "FAIL"])


# ---------------------------------------------------------------------------
# Battery rule (new diagnostic)
# ---------------------------------------------------------------------------
class TestBatteryDiagnosis(unittest.TestCase):
    def test_unavailable_when_no_battery(self):
        snap = _snapshot(battery=g.BatterySnapshot(available=False, error="none"))
        finding = g.diagnose_battery(snap, g.DEFAULT_THRESHOLDS)[0]
        self.assertEqual(finding.status, "UNAVAILABLE")
        self.assertEqual(finding.key, "Battery")

    def test_on_ac_power_is_pass_even_when_full(self):
        snap = _snapshot(battery=_battery(percent=100.0, plugged=True))
        finding = g.diagnose_battery(snap, g.DEFAULT_THRESHOLDS)[0]
        self.assertEqual(finding.status, "PASS")
        self.assertIn("AC", finding.message)

    def test_low_battery_warns(self):
        snap = _snapshot(battery=_battery(percent=20.0))
        self.assertEqual(g.diagnose_battery(snap, g.DEFAULT_THRESHOLDS)[0].status,
                         "WARNING")

    def test_critical_battery_fails(self):
        snap = _snapshot(battery=_battery(percent=3.0))
        self.assertEqual(g.diagnose_battery(snap, g.DEFAULT_THRESHOLDS)[0].status,
                         "FAIL")

    def test_healthy_battery_passes(self):
        snap = _snapshot(battery=_battery(percent=80.0))
        self.assertEqual(g.diagnose_battery(snap, g.DEFAULT_THRESHOLDS)[0].status,
                         "PASS")

    def test_remaining_time_is_included_when_known(self):
        snap = _snapshot(battery=_battery(percent=60.0, secs_left=5400.0))
        self.assertIn("1h 30m", g.diagnose_battery(snap, g.DEFAULT_THRESHOLDS)[0].message)

    def test_run_diagnostics_includes_battery(self):
        report = g.run_diagnostics(_snapshot(battery=_battery(percent=10.0)))
        self.assertIn("Battery", {f.component for f in report.findings})

    def test_low_battery_adds_a_recommendation(self):
        report = g.run_diagnostics(_snapshot(battery=_battery(percent=4.0)))
        self.assertTrue(any("Battery low" in r for r in report.recommendations))

    def test_threshold_boundaries(self):
        for percent, expected in ((8.0, "FAIL"), (8.1, "WARNING"),
                                  (25.0, "WARNING"), (25.1, "PASS")):
            snap = _snapshot(battery=_battery(percent=percent))
            self.assertEqual(
                g.diagnose_battery(snap, g.DEFAULT_THRESHOLDS)[0].status, expected,
                f"percent={percent}")


class TestRecommendations(unittest.TestCase):
    def test_gpu_overheat_gets_advice_and_never_all_healthy(self):
        snap = _snapshot(
            cpu=g.CpuSnapshot(available=True, percent=5.0, logical_cores=8,
                              physical_cores=4),
            mem=g.MemSnapshot(available=True, percent=10.0, total=1000, used=100),
            disks=[g.DiskEntry(device="C:", mountpoint="C:", total=100, used=10,
                               free=90, percent=10.0, accessible=True)],
            net=g.NetSnapshot(available=True, interfaces=["Ethernet"]),
            temps=[g.TempReading(label="cpu", current=50.0, source="s",
                                 status="available")],
            gpu=g.GpuStatus(supported=True, tool_found=True, message="ok", gpus=[
                g.GpuInfo(index=0, name="X", temperature_c=99.0, available=True)]),
            battery=_battery(percent=90.0, plugged=True),
        )
        report = g.run_diagnostics(snap)
        joined = " ".join(report.recommendations).lower()
        self.assertIn("gpu", joined)
        self.assertNotIn("all checks look healthy", joined)

    def test_unhandled_warning_never_claims_all_healthy(self):
        recs = g.build_recommendations([g.Finding("Network", "WARNING", "no nics")])
        self.assertTrue(recs)
        self.assertFalse(any("all checks look healthy" in r.lower() for r in recs))


class TestPlatformAdvice(unittest.TestCase):
    """Cross-OS support: advice must match the OS it is shown on."""

    def test_disk_advice_mentions_the_os_tool(self):
        with patch("platform.system", return_value="Windows"):
            self.assertIn("Disk Clean-up", g._disk_advice())
        with patch("platform.system", return_value="Darwin"):
            self.assertIn("Optimize Storage", g._disk_advice())
        with patch("platform.system", return_value="Linux"):
            self.assertIn("disk-usage", g._disk_advice())

    def test_battery_advice_mentions_the_os_settings(self):
        with patch("platform.system", return_value="Windows"):
            self.assertIn("Power & battery", g._battery_advice())
        with patch("platform.system", return_value="Darwin"):
            self.assertIn("System Settings > Battery", g._battery_advice())
        with patch("platform.system", return_value="Linux"):
            self.assertIn("upower", g._battery_advice())

    def test_scan_recommendations_use_platform_advice(self):
        findings = [g.Finding("Storage", "WARNING", "C: 90% used",
                              key="Storage:C:")]
        with patch("platform.system", return_value="Darwin"):
            recs = g.build_recommendations(findings)
        self.assertTrue(any("Optimize Storage" in r for r in recs))
        self.assertTrue(any("deletes nothing by itself" in r for r in recs))


class TestPlatformFonts(unittest.TestCase):
    """Fonts are chosen per OS; Tk falls back when a family is missing."""

    def test_fonts_selected_per_os(self):
        with patch("platform.system", return_value="Windows"):
            self.assertEqual(g._default_font_family(), "Segoe UI")
            self.assertEqual(g._default_mono_family(), "Consolas")
        with patch("platform.system", return_value="Darwin"):
            self.assertEqual(g._default_font_family(), "Helvetica Neue")
            self.assertEqual(g._default_mono_family(), "Menlo")
        with patch("platform.system", return_value="Linux"):
            self.assertEqual(g._default_font_family(), "DejaVu Sans")
            self.assertEqual(g._default_mono_family(), "DejaVu Sans Mono")


class TestLauncherScript(unittest.TestCase):
    """The Linux/macOS launcher mirrors the Windows .bat fallback chain."""

    def test_shds_sh_has_full_fallback_chain(self):
        text = Path(__file__).resolve().parent.parent.joinpath("shds.sh").read_text(
            encoding="utf-8")
        for needle in ("pythonw", "python3", "python ", "shds_gui.py",
                       "exec ", "Python was not found"):
            self.assertIn(needle, text)


class TestCollectBattery(unittest.TestCase):
    @unittest.skipUnless(g.HAS_PSUTIL, "psutil required")
    def test_none_battery_is_unavailable_not_invented(self):
        with patch.object(g.psutil, "sensors_battery", return_value=None, create=True):
            snap = g.collect_battery()
        self.assertFalse(snap.available)
        self.assertIsNone(snap.percent)
        self.assertTrue(snap.error)

    @unittest.skipUnless(g.HAS_PSUTIL, "psutil required")
    def test_reads_percent_and_plugged(self):
        fake = types.SimpleNamespace(percent=42.5, power_plugged=True, secsleft=600)
        with patch.object(g.psutil, "sensors_battery", return_value=fake, create=True):
            snap = g.collect_battery()
        self.assertTrue(snap.available)
        self.assertEqual(snap.percent, 42.5)
        self.assertTrue(snap.plugged)
        self.assertEqual(snap.secs_left, 600.0)

    @unittest.skipUnless(g.HAS_PSUTIL, "psutil required")
    def test_unknown_secsleft_sentinel_is_dropped(self):
        fake = types.SimpleNamespace(percent=10.0, power_plugged=False,
                                     secsleft=getattr(g.psutil, "POWER_TIME_UNKNOWN", -1))
        with patch.object(g.psutil, "sensors_battery", return_value=fake, create=True):
            snap = g.collect_battery()
        self.assertIsNone(snap.secs_left)

    @unittest.skipUnless(g.HAS_PSUTIL, "psutil required")
    def test_exception_degrades_to_unavailable(self):
        with patch.object(g.psutil, "sensors_battery",
                          side_effect=OSError("no battery api"), create=True):
            snap = g.collect_battery()
        self.assertFalse(snap.available)


# ---------------------------------------------------------------------------
# Disk I/O rates
# ---------------------------------------------------------------------------
class TestDiskIo(unittest.TestCase):
    def setUp(self):
        self._saved = dict(g._prev_disk_io)
        g._prev_disk_io.update({"read": 0, "write": 0, "t": 0.0})

    def tearDown(self):
        g._prev_disk_io.update(self._saved)

    @unittest.skipUnless(g.HAS_PSUTIL, "psutil required")
    def test_first_sample_has_no_rate(self):
        counters = types.SimpleNamespace(read_bytes=10_000, write_bytes=20_000)
        with patch.object(g.psutil, "disk_io_counters", return_value=counters):
            snap = g.collect_disk_io()
        self.assertTrue(snap.available)
        self.assertIsNone(snap.read_bps)  # honest: no previous sample yet
        self.assertIsNone(snap.write_bps)

    @unittest.skipUnless(g.HAS_PSUTIL, "psutil required")
    def test_second_sample_yields_a_positive_rate(self):
        first = types.SimpleNamespace(read_bytes=1_000, write_bytes=2_000)
        second = types.SimpleNamespace(read_bytes=1_000 + 5_120_000,
                                       write_bytes=2_000 + 1_024_000)
        with patch.object(g.psutil, "disk_io_counters", return_value=first):
            g.collect_disk_io()
        with patch.object(g.psutil, "disk_io_counters", return_value=second):
            snap = g.collect_disk_io()
        self.assertIsNotNone(snap.read_bps)
        self.assertGreater(snap.read_bps, 0)
        self.assertGreater(snap.write_bps, 0)

    @unittest.skipUnless(g.HAS_PSUTIL, "psutil required")
    def test_counter_reset_reports_no_rate(self):
        big = types.SimpleNamespace(read_bytes=10_000_000, write_bytes=10_000_000)
        small = types.SimpleNamespace(read_bytes=5, write_bytes=5)
        with patch.object(g.psutil, "disk_io_counters", return_value=big):
            g.collect_disk_io()
        with patch.object(g.psutil, "disk_io_counters", return_value=small):
            snap = g.collect_disk_io()
        self.assertIsNone(snap.read_bps)
        self.assertIsNone(snap.write_bps)

    @unittest.skipUnless(g.HAS_PSUTIL, "psutil required")
    def test_none_counters_is_unavailable(self):
        with patch.object(g.psutil, "disk_io_counters", return_value=None):
            snap = g.collect_disk_io()
        self.assertFalse(snap.available)

    def test_rate_bytes_rejects_reset_and_zero_dt(self):
        self.assertIsNone(g._rate_bytes(100, 200, 1.0))
        self.assertIsNone(g._rate_bytes(200, 100, 0.0))
        self.assertEqual(g._rate_bytes(200, 100, 2.0), 50.0)

    def test_format_rate_bytes(self):
        self.assertEqual(g.format_rate_bytes(None), "n/a")
        self.assertEqual(g.format_rate_bytes(-1), "n/a")
        self.assertIn("/s", g.format_rate_bytes(1536))


# ---------------------------------------------------------------------------
# Uptime + misc helpers
# ---------------------------------------------------------------------------
class TestHelpers(unittest.TestCase):
    def test_format_uptime_units(self):
        self.assertEqual(g.format_uptime(45), "45s")
        self.assertEqual(g.format_uptime(90), "1m 30s")
        self.assertEqual(g.format_uptime(3600 + 300), "1h 5m")
        self.assertEqual(g.format_uptime(86400 * 3 + 3600 * 4 + 60 * 12), "3d 4h 12m")

    def test_format_uptime_bad_input(self):
        self.assertEqual(g.format_uptime(None), "Unknown")
        self.assertEqual(g.format_uptime("nonsense"), "Unknown")
        self.assertEqual(g.format_uptime(-50), "0s")

    def test_format_uptime_non_finite_is_unknown(self):
        # Regression: int(inf) used to raise OverflowError.
        for value in (float("inf"), float("-inf"), float("nan")):
            self.assertEqual(g.format_uptime(value), "Unknown")

    def test_clamp(self):
        self.assertEqual(g.clamp(150, 0, 100), 100)
        self.assertEqual(g.clamp(-5, 0, 100), 0)
        self.assertEqual(g.clamp(50, 0, 100), 50)
        self.assertEqual(g.clamp(float("nan"), 7, 100), 7)

    @unittest.skipUnless(g.HAS_PSUTIL, "psutil required")
    def test_collect_uptime_is_non_negative(self):
        self.assertGreaterEqual(g.collect_uptime(), 0.0)

    def test_collect_uptime_never_raises_without_psutil(self):
        with patch.object(g, "HAS_PSUTIL", False):
            # None means "unknown"; 0.0 would claim the machine just booted.
            self.assertIsNone(g.collect_uptime())

    def test_snapshot_defaults_include_new_fields(self):
        snap = g.HardwareSnapshot()
        self.assertFalse(snap.battery.available)
        self.assertFalse(snap.disk_io.available)
        self.assertIsNone(snap.uptime_s)

    def test_format_uptime_reports_unknown(self):
        self.assertEqual(g.format_uptime(None), "Unknown")


# ---------------------------------------------------------------------------
# Config persistence
# ---------------------------------------------------------------------------
class TestConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_roundtrip(self):
        target = self.tmp / "config.json"
        self.assertTrue(g.save_config({"a": 1, "thresholds": {"cpu_warn": 50}}, target))
        self.assertEqual(g.load_config(target)["thresholds"]["cpu_warn"], 50)

    def test_missing_file_returns_empty_dict(self):
        self.assertEqual(g.load_config(self.tmp / "nope.json"), {})

    def test_corrupt_file_returns_empty_dict(self):
        target = self.tmp / "config.json"
        target.write_text("{not json", encoding="utf-8")
        self.assertEqual(g.load_config(target), {})

    def test_non_object_json_returns_empty_dict(self):
        target = self.tmp / "config.json"
        target.write_text("[1, 2, 3]", encoding="utf-8")
        self.assertEqual(g.load_config(target), {})

    def test_save_creates_parent_dirs(self):
        target = self.tmp / "deep" / "nested" / "config.json"
        self.assertTrue(g.save_config({"x": 1}, target))
        self.assertTrue(target.exists())

    def test_save_failure_returns_false_without_raising(self):
        # A directory where a file is expected is a guaranteed write failure.
        self.assertFalse(g.save_config({"x": 1}, self.tmp))

    def test_sanitise_keeps_known_numeric_keys(self):
        result = g.sanitise_thresholds({"cpu_warn": 55, "mem_fail": "91"})
        self.assertEqual(result["cpu_warn"], 55)
        self.assertEqual(result["mem_fail"], 91.0)

    def test_sanitise_drops_unknown_and_junk(self):
        result = g.sanitise_thresholds({"cpu_warn": "abc", "bogus": 1, "mem_warn": -5,
                                        "disk_warn": 1e9, "temp_warn": None})
        for key in ("cpu_warn", "mem_warn", "disk_warn", "temp_warn"):
            self.assertEqual(result[key], g.DEFAULT_THRESHOLDS[key])
        self.assertNotIn("bogus", result)

    def test_sanitise_handles_non_dict(self):
        self.assertEqual(g.sanitise_thresholds(None), dict(g.DEFAULT_THRESHOLDS))
        self.assertEqual(g.sanitise_thresholds("nope"), dict(g.DEFAULT_THRESHOLDS))

    def test_config_path_is_under_user_data_root(self):
        self.assertEqual(g.config_path().name, g.CONFIG_FILENAME)


# ---------------------------------------------------------------------------
# History store
# ---------------------------------------------------------------------------
class TestHistoryStore(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = g.HistoryStore(self.tmp / "history.db")

    def tearDown(self):
        self.store.close()

    def _payload(self, ts, score=90, cpu=10.0, mem=20.0, disk=30.0, fails=0, warnings=0):
        return {
            "timestamp": ts,
            "health_score": score,
            "summary": {"PASS": 1, "WARNING": warnings, "FAIL": fails,
                        "UNAVAILABLE": 0},
            "hardware": {"cpu": {"percent": cpu}, "memory": {"percent": mem},
                         "disks": [{"accessible": True, "percent": disk},
                                   {"accessible": False, "percent": None}]},
        }

    def test_open_creates_database(self):
        self.assertTrue(self.store.available, self.store.error)
        self.assertTrue(self.store.path.exists())

    def test_record_and_recent_oldest_first(self):
        self.store.record(self._payload("2026-01-01T00:00:00", score=80))
        self.store.record(self._payload("2026-01-02T00:00:00", score=95))
        entries = self.store.recent()
        self.assertEqual([e.ts for e in entries],
                         ["2026-01-01T00:00:00", "2026-01-02T00:00:00"])
        self.assertEqual([e.score for e in entries], [80, 95])

    def test_record_captures_worst_disk_percent(self):
        self.store.record(self._payload("t", disk=71.5))
        self.assertEqual(self.store.recent()[0].disk, 71.5)

    def test_worst_disk_percent_ignores_inaccessible(self):
        payload = {"hardware": {"disks": [
            {"accessible": False, "percent": 99.0},
            {"accessible": True, "percent": 42.0},
            {"accessible": True, "percent": None}]}}
        self.assertEqual(g.worst_disk_percent(payload), 42.0)

    def test_worst_disk_percent_none_when_nothing_usable(self):
        self.assertIsNone(g.worst_disk_percent({"hardware": {"disks": []}}))
        self.assertIsNone(g.worst_disk_percent({}))

    def test_count_and_clear(self):
        for i in range(4):
            self.store.record(self._payload(f"2026-01-0{i+1}T00:00:00"))
        self.assertEqual(self.store.count(), 4)
        self.assertTrue(self.store.clear())
        self.assertEqual(self.store.count(), 0)
        self.assertEqual(self.store.recent(), [])

    def test_recent_limit_returns_newest_slice(self):
        for i in range(10):
            self.store.record(self._payload(f"ts-{i}", score=i))
        entries = self.store.recent(limit=3)
        self.assertEqual([e.ts for e in entries], ["ts-7", "ts-8", "ts-9"])

    def test_trimming_keeps_table_bounded(self):
        store = g.HistoryStore(self.tmp / "trim.db", max_rows=20)
        try:
            for i in range(25):
                store.record(self._payload(f"ts-{i}"))
            self.assertEqual(store.count(), 20)
            self.assertEqual(store.recent()[0].ts, "ts-5")
        finally:
            store.close()

    def test_minimum_max_rows_floor(self):
        store = g.HistoryStore(self.tmp / "floor.db", max_rows=1)
        try:
            self.assertEqual(store.max_rows, 20)
        finally:
            store.close()

    def test_unavailable_path_degrades_without_raising(self):
        # A directory is never a valid SQLite file.
        store = g.HistoryStore(self.tmp)
        self.assertFalse(store.available)
        self.assertFalse(store.record(self._payload("t")))
        self.assertEqual(store.recent(), [])
        self.assertEqual(store.count(), 0)
        self.assertFalse(store.clear())
        store.close()

    def test_close_is_idempotent(self):
        self.store.close()
        self.store.close()
        self.assertFalse(self.store.available)

    def test_corrupt_database_is_reported_not_raised(self):
        bad = self.tmp / "bad.db"
        bad.write_text("this is not a database", encoding="utf-8")
        store = g.HistoryStore(bad)
        try:
            # Either it opens (SQLite is lazy) or it degrades; both must be safe.
            self.assertFalse(store.record({"timestamp": "t"}))
        except sqlite3.Error as exc:  # pragma: no cover - must never happen
            self.fail(f"HistoryStore leaked sqlite3.Error: {exc}")
        store.close()


# ---------------------------------------------------------------------------
# Reports: TXT / HTML / CSV
# ---------------------------------------------------------------------------
class TestReports(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.snap = _snapshot(
            battery=_battery(percent=55.0, plugged=False),
            disk_io=g.DiskIoSnapshot(available=True, read_bps=1024.0, write_bps=2048.0),
            uptime_s=90061.0,
        )
        self.diag = g.run_diagnostics(self.snap)
        self.payload = g.build_report_dict(self.snap, self.diag, g.DEFAULT_THRESHOLDS)

    def test_payload_has_new_fields(self):
        self.assertEqual(self.payload["health_score"], self.diag.health_score)
        self.assertEqual(self.payload["uptime_human"], "1d 1h 1m")
        self.assertIn("battery", self.payload["hardware"])
        self.assertIn("disk_io", self.payload["hardware"])

    def test_render_txt_mentions_new_sections(self):
        text = g.render_txt(self.payload)
        for needle in ("Health score", "Uptime", "Battery", "Disk I/O"):
            self.assertIn(needle, text)

    def test_render_html_is_self_contained(self):
        doc = g.render_html(self.payload)
        self.assertTrue(doc.startswith("<!DOCTYPE html>"))
        self.assertIn("</html>", doc)
        self.assertNotIn("http://", doc)
        self.assertNotIn("https://", doc)
        self.assertIn(str(self.diag.health_score), doc)

    def test_render_html_escapes_untrusted_values(self):
        payload = dict(self.payload)
        payload["findings"] = [dict(self.payload["findings"][0],
                                    message="<script>alert(1)</script>")]
        doc = g.render_html(payload)
        self.assertNotIn("<script>alert(1)</script>", doc)
        self.assertIn("&lt;script&gt;", doc)

    def test_render_html_includes_chart_when_provided(self):
        doc = g.render_html(self.payload, "QUJD")
        self.assertIn("data:image/png;base64,QUJD", doc)

    def test_render_html_without_chart_says_so(self):
        self.assertIn("Chart unavailable", g.render_html(self.payload))

    def test_render_txt_survives_missing_fields(self):
        # Regression: a hand-edited/partial payload used to raise AttributeError
        # on a null temperature status and TypeError on a null health score.
        text = g.render_txt({
            "health_score": None,
            "hardware": {"temperatures": [
                {"label": "sensor", "status": None, "source": "test"}]},
        })
        self.assertIn("Health score: n/a", text)
        self.assertIn("SENSOR: UNKNOWN", text.upper())

    def test_render_html_escapes_hardware_error_text(self):
        # Error strings come from the OS and must never inject markup.
        payload = {"hardware": {
            "battery": {"available": False, "error": "<b>bad</b>"},
            "disks": [{"accessible": False, "mountpoint": "C:",
                       "error": "<i>gone</i>"}],
        }}
        doc = g.render_html(payload)
        self.assertNotIn("<b>bad</b>", doc)
        self.assertNotIn("<i>gone</i>", doc)
        self.assertIn("&lt;b&gt;bad&lt;/b&gt;", doc)

    def test_save_all_formats(self):
        for fmt in ("json", "txt", "csv", "html"):
            path = g.save_report(self.payload, self.tmp / f"report.{fmt}")
            self.assertTrue(path.exists(), fmt)
            self.assertGreater(path.stat().st_size, 10, fmt)

    def test_saved_json_roundtrips(self):
        path = g.save_report(self.payload, self.tmp / "r.json")
        loaded = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(loaded["health_score"], self.diag.health_score)

    def test_saved_html_contains_findings(self):
        path = g.save_report(self.payload, self.tmp / "r.html")
        doc = path.read_text(encoding="utf-8")
        self.assertIn(self.diag.findings[0].component, doc)

    def test_unsupported_suffix_rejected(self):
        with self.assertRaises(ValueError):
            g.save_report(self.payload, self.tmp / "report.exe")

    def test_html_is_allowed_by_path_validator(self):
        self.assertTrue(str(g.safe_path_for_report(str(self.tmp / "a.html"))))

    def test_render_trend_png_requires_points(self):
        self.assertIsNone(g.render_trend_png_b64([]))

    @unittest.skipUnless(g.HAS_MPL, "matplotlib required")
    def test_render_trend_png_produces_png(self):
        points = [{"ts": f"2026-01-0{i+1}T10:00:00", "score": 90 - i} for i in range(5)]
        b64 = g.render_trend_png_b64(points)
        self.assertIsNotNone(b64)
        raw = base64.b64decode(b64)
        self.assertTrue(raw.startswith(b"\x89PNG"))

    @unittest.skipUnless(g.HAS_MPL, "matplotlib required")
    def test_render_trend_png_survives_dirty_points(self):
        points = [{"ts": None, "score": None}, {"ts": "x", "score": "bad"}]
        self.assertIsNotNone(g.render_trend_png_b64(points))


# ---------------------------------------------------------------------------
# Process details (read-only)
# ---------------------------------------------------------------------------
class TestProcessDetail(unittest.TestCase):
    def test_own_process_detail_is_read_only_and_rich(self):
        detail = g.collect_process_detail(os.getpid())
        if not detail["available"]:
            self.skipTest("process introspection unavailable on this platform")
        fields = detail["fields"]
        self.assertIn("Name", fields)
        self.assertIn("Memory RSS", fields)
        self.assertTrue(detail["children"] is not None)
        # Nothing destructive is ever exposed by the collector.
        self.assertNotIn("terminate", json.dumps(detail).lower())

    @unittest.skipUnless(g.HAS_PSUTIL, "psutil required")
    def test_missing_pid_degrades_without_raising(self):
        detail = g.collect_process_detail(999_999_999)
        self.assertFalse(detail["available"])
        self.assertTrue(detail["error"])

    def test_access_denied_is_reported_per_field(self):
        if not g.HAS_PSUTIL:
            self.skipTest("psutil required")

        class FakeProc:
            def oneshot(self):
                return contextlib.nullcontext()

            def name(self):
                raise g.psutil.AccessDenied(pid=1)

            def __getattr__(self, item):
                def _boom(*_a, **_k):
                    raise g.psutil.AccessDenied(pid=1)
                return _boom

        with patch.object(g.psutil, "Process", return_value=FakeProc()):
            detail = g.collect_process_detail(1)
        self.assertTrue(detail["available"])
        self.assertEqual(detail["fields"]["Name"], "Access denied")
        self.assertIn("Name", detail["notes"])

    def test_without_psutil(self):
        with patch.object(g, "HAS_PSUTIL", False):
            detail = g.collect_process_detail(1234)
        self.assertFalse(detail["available"])
        self.assertIn("psutil", detail["error"])


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------
class TestNotifications(unittest.TestCase):
    def test_send_toast_returns_bool_and_never_raises(self):
        self.assertIsInstance(g.send_toast("title", "message"), bool)

    def test_send_toast_without_plyer_returns_false(self):
        with patch.object(g, "HAS_PLYER", False):
            self.assertFalse(g.send_toast("t", "m"))

    def test_send_toast_swallows_backend_errors(self):
        boom = types.SimpleNamespace(notify=lambda **_k: (_ for _ in ()).throw(
            RuntimeError("no notification service")))
        with patch.object(g, "HAS_PLYER", True), \
                patch.object(g, "_plyer_notification", boom):
            self.assertFalse(g.send_toast("t", "m"))

    def test_alert_cooldown_constant_is_sane(self):
        self.assertGreater(g.ALERT_COOLDOWN_S, 0)


# ---------------------------------------------------------------------------
# Process refresh guard, process details, threshold validation
# ---------------------------------------------------------------------------
class TestProcessRefreshGuard(unittest.TestCase):
    """Regression: monitoring used to spawn an unbounded number of workers."""

    @staticmethod
    def _fake_app():
        # Bypass Tk __init__: these methods only use plain attributes.
        app = object.__new__(g.ScannerApp)
        app._procs_active = False
        app.proc_sort = types.SimpleNamespace(get=lambda: "cpu")
        app._bg_queue = queue.Queue()
        return app

    def test_overlapping_refresh_starts_one_worker(self):
        app = self._fake_app()
        created = []

        class FakeThread:
            def __init__(self, *args, **kwargs):
                created.append(self)

            def start(self):
                pass

        with patch.object(g.threading, "Thread", FakeThread):
            app.refresh_processes()
            app.refresh_processes()
        self.assertEqual(len(created), 1)
        self.assertTrue(app._procs_active)

    def test_worker_clears_guard_on_failure(self):
        app = self._fake_app()
        app._procs_active = True
        with patch.object(g, "collect_processes", side_effect=RuntimeError("boom")), \
                patch.object(g.log, "warning"):
            app._proc_job("cpu")
        self.assertFalse(app._procs_active)

    def test_worker_queues_rows_and_clears_guard(self):
        app = self._fake_app()
        app._procs_active = True
        with patch.object(g, "collect_processes", return_value=[]):
            app._proc_job("mem")
        self.assertFalse(app._procs_active)
        kind, rows = app._bg_queue.get_nowait()
        self.assertEqual(kind, "procs")
        self.assertEqual(rows, [])


class TestProcessDetailJob(unittest.TestCase):
    def test_collection_failure_queues_an_unavailable_detail(self):
        app = object.__new__(g.ScannerApp)
        app._bg_queue = queue.Queue()
        with patch.object(g, "collect_process_detail",
                          side_effect=RuntimeError("boom")), \
                patch.object(g.log, "warning"):
            app._proc_detail_job(123, None)
        kind, (pid, win, detail) = app._bg_queue.get_nowait()
        self.assertEqual(kind, "proc_detail")
        self.assertEqual(pid, 123)
        self.assertIsNone(win)
        self.assertFalse(detail["available"])
        self.assertIn("boom", detail["error"])


# ---------------------------------------------------------------------------
# Polling-loop resilience: one bad queue item must not freeze the app
# ---------------------------------------------------------------------------
class TestPollLoopResilience(unittest.TestCase):
    """Regression: `_poll_bg_queue` only rescheduled when the drain succeeded,
    so a single item whose handler raised would permanently stop all background
    updates (snapshots, scans, process rows) while the window stayed alive
    (scan button wedged on "Scan running", live monitoring frozen)."""

    @staticmethod
    def _fake_app():
        # Bypass Tk __init__: these methods only use injected plain attributes.
        app = object.__new__(g.ScannerApp)
        app._bg_queue = queue.Queue()
        scheduled: list[tuple[int, object]] = []
        app._reschedule = lambda ms, fn: scheduled.append((ms, fn))
        app.scheduled = scheduled
        return app

    def test_poisoned_item_is_skipped_and_polling_continues(self):
        app = self._fake_app()
        with patch.object(g.log, "exception") as fail_log:
            app._bg_queue.put(("procs", None))  # _render_procs(None) raises
            app._poll_bg_queue()
        self.assertEqual(len(app.scheduled), 1)   # the loop kept its heartbeat
        self.assertTrue(fail_log.called)          # the failure was logged
        self.assertTrue(app._bg_queue.empty())
        # The next cycle still processes good items.
        with patch.object(g.ScannerApp, "_render_procs") as render:
            app._bg_queue.put(("procs", [g.ProcEntry(pid=1, name="a")]))
            app._poll_bg_queue()
        self.assertEqual(len(app.scheduled), 2)
        render.assert_called_once()

    def test_poisoned_scan_item_does_not_wedge_the_scan_button(self):
        app = self._fake_app()
        app._scan_active = True
        app.scan_bar = types.SimpleNamespace(start=lambda *_a: None,
                                             stop=lambda: None)
        app.head_status = types.SimpleNamespace(configure=lambda **_kw: None)
        with patch.object(g.log, "exception"):
            app._bg_queue.put(("scan", (1,)))  # tuple unpacking raises
            app._poll_bg_queue()
        self.assertTrue(app._scan_active)      # poisoned item: not reset yet
        app._bg_queue.put(("scan_done", None))
        app._poll_bg_queue()                   # the loop is still alive
        self.assertFalse(app._scan_active)     # scan state was reset

    def test_monitor_poll_survives_snapshot_failure(self):
        app = self._fake_app()
        app.monitor_running = True
        app.monitor = g.MonitorWorker()
        app.chart = None
        app.refresh_interval = types.SimpleNamespace(get=lambda: "1.0")

        def boom(_snap):
            raise TypeError("boom")

        app._apply_snapshot = boom
        app.monitor.samples.put(g.HardwareSnapshot())
        with patch.object(g.log, "exception") as fail_log:
            app._poll_monitor()
        self.assertTrue(fail_log.called)
        self.assertEqual(len(app.scheduled), 1)   # polling continues
        ms, fn = app.scheduled[0]
        self.assertEqual(ms, 1000)
        self.assertEqual(fn, app._poll_monitor)


class TestThresholdValidation(unittest.TestCase):
    def test_defaults_are_valid(self):
        self.assertIsNone(g.ScannerApp.validate_thresholds(dict(g.DEFAULT_THRESHOLDS)))

    def test_inverted_battery_pair_is_rejected(self):
        values = dict(g.DEFAULT_THRESHOLDS, battery_warn=5.0, battery_fail=50.0)
        error = g.ScannerApp.validate_thresholds(values)
        self.assertIsNotNone(error)
        self.assertIn("battery_warn", error)

    def test_equal_battery_pair_is_rejected(self):
        values = dict(g.DEFAULT_THRESHOLDS, battery_warn=25.0, battery_fail=25.0)
        self.assertIsNotNone(g.ScannerApp.validate_thresholds(values))

    def test_normal_battery_pair_is_accepted(self):
        values = dict(g.DEFAULT_THRESHOLDS, battery_warn=30.0, battery_fail=10.0)
        self.assertIsNone(g.ScannerApp.validate_thresholds(values))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
class TestCli(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _run(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = g.main(argv)
        return code, out.getvalue()

    def test_scan_json_exit_code_is_zero_or_one(self):
        dest = self.tmp / "cli.json"
        code, out = self._run(["--scan", "--no-gpu", "--no-history",
                               "--json", str(dest)])
        self.assertIn(code, (0, 1))
        self.assertTrue(dest.exists())
        payload = json.loads(dest.read_text(encoding="utf-8"))
        self.assertIn("health_score", payload)
        self.assertIn("findings", payload)
        self.assertIn("Health score", out)

    def test_exit_code_one_when_a_fail_is_present(self):
        # Force a FAIL by making the disk threshold impossible to satisfy.
        thresholds = self.tmp / "t.json"
        thresholds.write_text(json.dumps(
            {"thresholds": {"disk_warn": 1.0, "disk_fail": 2.0}}), encoding="utf-8")
        code, _out = self._run(["--scan", "--no-gpu", "--no-history", "--quiet",
                                "--thresholds", str(thresholds)])
        if code == 2:
            self.skipTest("no disk metrics available on this machine")
        self.assertEqual(code, 1)

    def test_flat_thresholds_file_is_applied(self):
        # Regression: a flat {"cpu_warn": ...} file used to be silently ignored
        # because the CLI only looked for the nested {"thresholds": {...}} shape.
        thresholds = self.tmp / "flat-thresholds.json"
        thresholds.write_text(json.dumps({"cpu_warn": 1.0, "cpu_fail": 2.0}),
                              encoding="utf-8")
        dest = self.tmp / "flat-report.json"
        code, _out = self._run(["--scan", "--no-gpu", "--no-history", "--quiet",
                                "--thresholds", str(thresholds),
                                "--json", str(dest)])
        self.assertIn(code, (0, 1))
        payload = json.loads(dest.read_text(encoding="utf-8"))
        self.assertEqual(payload["thresholds"]["cpu_warn"], 1.0)
        self.assertEqual(payload["thresholds"]["cpu_fail"], 2.0)

    def test_inconsistent_thresholds_fall_back_to_defaults(self):
        thresholds = self.tmp / "bad-thresholds.json"
        thresholds.write_text(json.dumps({"thresholds": {
            "battery_warn": 5.0, "battery_fail": 50.0}}), encoding="utf-8")
        dest = self.tmp / "bad-report.json"
        code, _out = self._run(["--scan", "--no-gpu", "--no-history", "--quiet",
                                "--thresholds", str(thresholds),
                                "--json", str(dest)])
        self.assertIn(code, (0, 1))
        payload = json.loads(dest.read_text(encoding="utf-8"))
        self.assertEqual(payload["thresholds"]["battery_warn"],
                         g.DEFAULT_THRESHOLDS["battery_warn"])
        self.assertEqual(payload["thresholds"]["battery_fail"],
                         g.DEFAULT_THRESHOLDS["battery_fail"])

    def test_quiet_keeps_the_summary_but_drops_the_details(self):
        code, out = self._run(["--scan", "--no-gpu", "--no-history", "--quiet"])
        self.assertIn(code, (0, 1))
        # --quiet must still report the health score and the counts, otherwise
        # a scheduled run yields nothing but an exit code.
        self.assertIn("Health score:", out)
        self.assertIn("PASS=", out)
        self.assertIn("Done:", out)
        # The per-finding listing is what --quiet is meant to suppress.
        self.assertNotIn("Recommendations:", out)
        self.assertNotIn("[PASS]", out)

    def test_html_output_is_a_document(self):
        dest = self.tmp / "cli.html"
        code, _out = self._run(["--scan", "--no-gpu", "--no-history",
                                "--html", str(dest)])
        self.assertIn(code, (0, 1))
        self.assertTrue(dest.read_text(encoding="utf-8").startswith("<!DOCTYPE html>"))

    def test_all_formats_at_once(self):
        args = ["--scan", "--no-gpu", "--no-history", "--quiet"]
        for fmt in ("json", "txt", "csv", "html"):
            args += [f"--{fmt}", str(self.tmp / f"r.{fmt}")]
        code, _out = self._run(args)
        self.assertIn(code, (0, 1))
        for fmt in ("json", "txt", "csv", "html"):
            self.assertTrue((self.tmp / f"r.{fmt}").exists(), fmt)

    def test_bad_destination_reports_error_code(self):
        bad = self.tmp / "report.exe"  # suffix not allowed
        code, out = self._run(["--scan", "--no-gpu", "--no-history", "--quiet",
                               "--json", str(bad)])
        self.assertEqual(code, 2)
        self.assertIn("Could not write", out)

    def test_no_gpu_skips_gpu_query(self):
        with patch.object(g, "query_gpus") as spy:
            self._run(["--scan", "--no-gpu", "--no-history", "--quiet"])
        spy.assert_not_called()

    def test_parser_documents_exit_codes(self):
        text = g.build_parser().format_help()
        self.assertIn("--scan", text)
        self.assertIn("exit codes", text.lower())

    def test_main_returns_without_gui_when_scan_requested(self):
        with patch.object(g, "ScannerApp") as gui:
            self._run(["--scan", "--no-gpu", "--no-history", "--quiet"])
        gui.assert_not_called()

    def test_unknown_flag_without_scan_does_not_open_gui(self):
        with patch.object(g, "ScannerApp") as gui:
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                code = g.main(["--definitely-not-a-flag"])
        gui.assert_not_called()
        self.assertIn(code, (0, 2))


if __name__ == "__main__":
    unittest.main()
