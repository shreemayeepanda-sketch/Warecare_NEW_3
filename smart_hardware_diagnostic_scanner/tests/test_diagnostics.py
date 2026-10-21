"""Tests for diagnostic threshold logic (mocked snapshots, no real hardware)."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from scanner.diagnostics import (
    DEFAULT_THRESHOLDS,
    _disk_advice,
    build_recommendations,
    diagnose_cpu,
    diagnose_disks,
    diagnose_gpu,
    diagnose_memory,
    diagnose_network,
    diagnose_temps,
    run_diagnostics,
)
from scanner.gpu import GpuInfo, GpuStatus
from scanner.hardware import (
    CpuSnapshot,
    DiskEntry,
    HardwareSnapshot,
    MemSnapshot,
    NetSnapshot,
    TempReading,
)


def make_snap(**kwargs) -> HardwareSnapshot:
    snap = HardwareSnapshot()
    for k, v in kwargs.items():
        setattr(snap, k, v)
    return snap


class TestThresholdLogic(unittest.TestCase):
    def test_cpu_pass_warn_fail(self):
        th = dict(DEFAULT_THRESHOLDS)
        self.assertEqual(diagnose_cpu(make_snap(cpu=CpuSnapshot(available=True, percent=10.0,
                                                               logical_cores=8, physical_cores=4)),
                                      th)[0].status, "PASS")
        self.assertEqual(diagnose_cpu(make_snap(cpu=CpuSnapshot(available=True, percent=90.0)),
                                      th)[0].status, "WARNING")
        self.assertEqual(diagnose_cpu(make_snap(cpu=CpuSnapshot(available=True, percent=99.0)),
                                      th)[0].status, "FAIL")

    def test_cpu_unavailable(self):
        th = dict(DEFAULT_THRESHOLDS)
        f = diagnose_cpu(make_snap(cpu=CpuSnapshot(available=False, error="no psutil")), th)[0]
        self.assertEqual(f.status, "UNAVAILABLE")

    def test_memory_thresholds(self):
        th = dict(DEFAULT_THRESHOLDS)
        self.assertEqual(diagnose_memory(make_snap(mem=MemSnapshot(available=True, percent=20.0,
                                                                  total=8 * 1024**3,
                                                                  used=1 * 1024**3)), th)[0].status, "PASS")
        self.assertEqual(diagnose_memory(make_snap(mem=MemSnapshot(available=True, percent=90.0,
                                                                  total=8 * 1024**3,
                                                                  used=7 * 1024**3)), th)[0].status, "WARNING")
        self.assertEqual(diagnose_memory(make_snap(mem=MemSnapshot(available=True, percent=98.0,
                                                                  total=8 * 1024**3,
                                                                  used=1)), th)[0].status, "FAIL")

    def test_high_reading_is_warning_not_damage_claim(self):
        th = dict(DEFAULT_THRESHOLDS)
        f = diagnose_cpu(make_snap(cpu=CpuSnapshot(available=True, percent=90.0)), th)[0]
        self.assertEqual(f.status, "WARNING")
        self.assertNotIn("damag", (f.message + f.detail).lower())

    def test_custom_thresholds_respected(self):
        th = dict(DEFAULT_THRESHOLDS)
        th["cpu_warn"] = 50.0
        th["cpu_fail"] = 60.0
        f = diagnose_cpu(make_snap(cpu=CpuSnapshot(available=True, percent=55.0)), th)[0]
        self.assertEqual(f.status, "WARNING")

    def test_disk_inaccessible(self):
        th = dict(DEFAULT_THRESHOLDS)
        snap = make_snap(disks=[DiskEntry(device="C:", mountpoint="C:",
                                          accessible=False, error="Permission denied")])
        self.assertEqual(diagnose_disks(snap, th)[0].status, "UNAVAILABLE")

    def test_disk_warn_fail(self):
        th = dict(DEFAULT_THRESHOLDS)
        snap = make_snap(disks=[DiskEntry(device="C:", mountpoint="C:", fstype="NTFS",
                                          total=100, used=90, free=10, percent=90.0,
                                          accessible=True)])
        self.assertEqual(diagnose_disks(snap, th)[0].status, "WARNING")
        snap.disks[0].percent = 97.0
        self.assertEqual(diagnose_disks(snap, th)[0].status, "FAIL")

    def test_no_disks_unavailable(self):
        th = dict(DEFAULT_THRESHOLDS)
        self.assertEqual(diagnose_disks(make_snap(disks=[]), th)[0].status, "UNAVAILABLE")

    def test_gpu_unavailable_when_no_tool(self):
        th = dict(DEFAULT_THRESHOLDS)
        snap = make_snap(gpu=GpuStatus(supported=False, message="nvidia-smi not found"))
        self.assertEqual(diagnose_gpu(snap, th)[0].status, "UNAVAILABLE")

    def test_network_no_interfaces_warning(self):
        th = dict(DEFAULT_THRESHOLDS)
        snap = make_snap(net=NetSnapshot(available=True, interfaces=[]))
        self.assertEqual(diagnose_network(snap, th)[0].status, "WARNING")

    def test_temps_unsupported_when_no_sensors(self):
        th = dict(DEFAULT_THRESHOLDS)
        snap = make_snap(temps=[TempReading(label="system", current=None,
                                            source="x", status="unsupported")])
        self.assertEqual(diagnose_temps(snap, th)[0].status, "UNAVAILABLE")

    def test_temps_never_invented(self):
        th = dict(DEFAULT_THRESHOLDS)
        snap = make_snap(temps=[TempReading(label="CPU", current=95.0,
                                            source="psutil", status="available")])
        f = diagnose_temps(snap, th)[0]
        self.assertIn("95", f.message)
        self.assertIn("psutil", f.message)

    def test_full_run_summary_counts(self):
        snap = HardwareSnapshot(
            cpu=CpuSnapshot(available=True, percent=10.0, logical_cores=8, physical_cores=4),
            mem=MemSnapshot(available=True, percent=20.0, total=8, used=2),
            disks=[DiskEntry(device="C:", mountpoint="C:", total=100, used=10,
                             free=90, percent=10.0, accessible=True)],
            net=NetSnapshot(available=True, interfaces=["Ethernet"]),
            temps=[TempReading(label="x", current=None, source="s", status="unsupported")],
            gpu=GpuStatus(supported=False, message="no gpu"),
        )
        rep = run_diagnostics(snap, dict(DEFAULT_THRESHOLDS))
        total = sum(rep.summary.values())
        self.assertEqual(total, len(rep.findings))
        self.assertGreaterEqual(rep.duration_s, 0.0)
        self.assertTrue(rep.recommendations)

    def test_recommendations_safe_no_destructive_advice(self):
        from scanner.diagnostics import Finding
        recs = build_recommendations([Finding("CPU", "PASS", "ok")])
        joined = " ".join(recs).lower()
        for banned in ("delete system", "disable service", "format", "registry"):
            self.assertNotIn(banned, joined)

    def test_gpu_overheat_gets_advice_and_never_all_healthy(self):
        snap = HardwareSnapshot(
            cpu=CpuSnapshot(available=True, percent=5.0, logical_cores=8, physical_cores=4),
            mem=MemSnapshot(available=True, percent=10.0, total=1000, used=100),
            disks=[DiskEntry(device="C:", mountpoint="C:", total=100, used=10,
                             free=90, percent=10.0, accessible=True)],
            net=NetSnapshot(available=True, interfaces=["Ethernet"]),
            temps=[TempReading(label="cpu", current=50.0, source="s",
                               status="available")],
            gpu=GpuStatus(supported=True, tool_found=True, message="ok", gpus=[
                GpuInfo(index=0, name="X", temperature_c=99.0, available=True)]),
        )
        rep = run_diagnostics(snap, dict(DEFAULT_THRESHOLDS))
        joined = " ".join(rep.recommendations).lower()
        self.assertIn("gpu", joined)
        self.assertNotIn("all checks look healthy", joined)

    def test_unhandled_warning_never_claims_all_healthy(self):
        from scanner.diagnostics import Finding
        recs = build_recommendations([Finding("Network", "WARNING", "no nics")])
        self.assertTrue(recs)
        self.assertFalse(any("all checks look healthy" in r.lower() for r in recs))


class TestPlatformAdvice(unittest.TestCase):
    """Cross-OS support: advice must match the OS it is shown on."""

    def test_disk_advice_mentions_the_os_tool(self):
        with patch("platform.system", return_value="Windows"):
            self.assertIn("Disk Clean-up", _disk_advice())
        with patch("platform.system", return_value="Darwin"):
            self.assertIn("Optimize Storage", _disk_advice())
        with patch("platform.system", return_value="Linux"):
            self.assertIn("disk-usage", _disk_advice())

    def test_scan_recommendations_use_platform_advice(self):
        from scanner.diagnostics import Finding
        findings = [Finding("Storage", "WARNING", "C: 90% used")]
        with patch("platform.system", return_value="Linux"):
            recs = build_recommendations(findings)
        self.assertTrue(any("disk-usage" in r for r in recs))
        self.assertTrue(any("deletes nothing by itself" in r for r in recs))


if __name__ == "__main__":
    unittest.main()
