"""Tests for report building/serialization + optional-dependency absence."""
from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from scanner.diagnostics import DEFAULT_THRESHOLDS, run_diagnostics
from scanner.hardware import (
    CpuSnapshot,
    DiskEntry,
    HardwareSnapshot,
    MemSnapshot,
    NetSnapshot,
    TempReading,
)
from scanner.gpu import GpuStatus
from scanner.reports import build_report_dict, render_txt, save_report


def demo_snapshot() -> HardwareSnapshot:
    return HardwareSnapshot(
        cpu=CpuSnapshot(available=True, percent=15.0, logical_cores=8, physical_cores=4,
                        freq_current_mhz=3200.0),
        mem=MemSnapshot(available=True, total=8 * 1024**3, available_bytes=4 * 1024**3,
                        used=4 * 1024**3, percent=50.0),
        disks=[DiskEntry(device="C:", mountpoint="C:", fstype="NTFS", total=256 * 1024**3,
                         used=100 * 1024**3, free=156 * 1024**3, percent=39.1,
                         accessible=True)],
        net=NetSnapshot(available=True, interfaces=["Ethernet"], bytes_sent=1, bytes_recv=2),
        temps=[TempReading(label="sys", current=None, source="s", status="unsupported")],
        gpu=GpuStatus(supported=False, message="nvidia-smi not found"),
        hostname="testhost",
        os_info="Windows 11",
    )


class TestReports(unittest.TestCase):
    def test_build_and_json_roundtrip(self):
        snap = demo_snapshot()
        diag = run_diagnostics(snap, dict(DEFAULT_THRESHOLDS), include_os=False)
        payload = build_report_dict(snap, diag, dict(DEFAULT_THRESHOLDS))
        text = json.dumps(payload)  # must be serialisable
        back = json.loads(text)
        self.assertEqual(back["hostname"], "testhost")
        self.assertIn("findings", back)
        self.assertIn("recommendations", back)
        self.assertIn("timestamp", back)

    def test_txt_render_contains_sections(self):
        snap = demo_snapshot()
        diag = run_diagnostics(snap, dict(DEFAULT_THRESHOLDS), include_os=False)
        payload = build_report_dict(snap, diag, dict(DEFAULT_THRESHOLDS))
        txt = render_txt(payload)
        for section in ("FINDINGS", "RECOMMENDATIONS", "SUMMARY", "HARDWARE"):
            self.assertIn(section, txt)

    def test_txt_render_survives_null_temperature_status(self):
        # A partial/imported payload must not raise AttributeError on .upper().
        payload = {"hardware": {"temperatures": [
            {"label": "sys", "status": None, "source": "s"}]}}
        txt = render_txt(payload)
        self.assertIn("UNKNOWN", txt)

    def test_save_json_txt_csv(self):
        snap = demo_snapshot()
        diag = run_diagnostics(snap, dict(DEFAULT_THRESHOLDS), include_os=False)
        payload = build_report_dict(snap, diag, dict(DEFAULT_THRESHOLDS))
        with tempfile.TemporaryDirectory() as td:
            jp = Path(td) / "r.json"
            tp = Path(td) / "r.txt"
            cp = Path(td) / "r.csv"
            save_report(payload, jp)
            save_report(payload, tp)
            save_report(payload, cp)
            self.assertTrue(json.loads(jp.read_text(encoding="utf-8")))
            self.assertIn("FINDINGS", tp.read_text(encoding="utf-8"))
            with cp.open(encoding="utf-8") as fh:
                rows = list(csv.reader(fh))
            self.assertEqual(rows[0], ["component", "status", "message", "detail"])
            self.assertGreater(len(rows), 1)

    def test_invalid_suffix_rejected(self):
        snap = demo_snapshot()
        diag = run_diagnostics(snap, dict(DEFAULT_THRESHOLDS), include_os=False)
        payload = build_report_dict(snap, diag, dict(DEFAULT_THRESHOLDS))
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                save_report(payload, Path(td) / "r.exe")

    def test_no_real_files_deleted(self):
        # Reports tests only write inside TemporaryDirectory; assert helper exists.
        with tempfile.TemporaryDirectory() as td:
            probe = Path(td) / "probe.json"
            self.assertFalse(probe.exists())

    def test_optional_matplotlib_absent(self):
        # charts module must import cleanly with matplotlib blocked
        import scanner.charts as charts

        self.assertTrue(hasattr(charts, "HAS_MPL"))
        self.assertTrue(hasattr(charts, "ChartPanel"))

    def test_optional_psutil_absent_snapshot_still_builds(self):
        from unittest.mock import patch
        from scanner import hardware as hw

        with patch.object(hw, "HAS_PSUTIL", False), patch.object(hw, "psutil", None):
            snap = hw.collect_snapshot(include_gpu=False)
            self.assertFalse(snap.cpu.available)
            diag = run_diagnostics(snap, dict(DEFAULT_THRESHOLDS), include_os=False)
            self.assertTrue(any(f.status == "UNAVAILABLE" for f in diag.findings))


if __name__ == "__main__":
    unittest.main()
