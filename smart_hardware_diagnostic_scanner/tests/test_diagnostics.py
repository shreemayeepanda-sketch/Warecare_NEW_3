"""Tests for diagnostic threshold logic (mocked snapshots, no real hardware)."""
from __future__ import annotations

import subprocess
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from scanner.diagnostics import (
    DEFAULT_THRESHOLDS,
    Finding,
    _disk_advice,
    build_recommendations,
    diagnose_cpu,
    diagnose_disks,
    diagnose_gpu,
    diagnose_linux_os,
    diagnose_macos_os,
    diagnose_memory,
    diagnose_network,
    diagnose_os,
    diagnose_temps,
    diagnose_windows_os,
    run_diagnostics,
    system_health_score,
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
        rep = run_diagnostics(snap, dict(DEFAULT_THRESHOLDS), include_os=False)
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
        rep = run_diagnostics(snap, dict(DEFAULT_THRESHOLDS), include_os=False)
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


class FakeCommands:
    """``subprocess.run`` stand-in returning canned output by argv substring."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), dict(kwargs)))
        joined = " ".join(str(part) for part in argv)
        for marker, returncode, stdout, stderr in self.responses:
            if marker in joined:
                return subprocess.CompletedProcess(argv, returncode,
                                                   stdout, stderr)
        return subprocess.CompletedProcess(argv, 0, "", "")

    def assert_read_only(self, testcase):
        testcase.assertTrue(self.calls, "no OS command was invoked")
        for argv, kwargs in self.calls:
            testcase.assertIs(kwargs.get("shell"), False, f"shell used: {argv}")
            testcase.assertEqual(kwargs.get("timeout"), 8,
                                 f"timeout not 8s: {argv}")
            testcase.assertTrue(kwargs.get("capture_output"),
                                f"output not captured: {argv}")


class TestWindowsOSChecks(unittest.TestCase):
    def _run(self, responses):
        fake = FakeCommands(responses)
        with patch("scanner.diagnostics.subprocess.run", side_effect=fake):
            findings = diagnose_windows_os()
        fake.assert_read_only(self)
        return {f.key: f for f in findings}, fake

    def test_stopped_update_disabled_defender_restore_off(self):
        findings, _ = self._run([
            ("wuauserv", 0, "Stopped\n", ""),
            ("RealTimeProtectionEnabled", 0, "False\n", ""),
            ("SystemRestore", 0, "1\n", ""),
        ])
        self.assertEqual(findings["OS:WindowsUpdate"].status, "WARNING")
        self.assertEqual(findings["OS:WindowsUpdate"].code,
                         "WINDOWS_UPDATE_STOPPED")
        self.assertEqual(findings["OS:Defender"].status, "WARNING")
        self.assertEqual(findings["OS:Defender"].code, "DEFENDER_DISABLED")
        self.assertEqual(findings["OS:SystemRestore"].status, "WARNING")
        self.assertEqual(findings["OS:SystemRestore"].code,
                         "SYSTEM_RESTORE_OFF")

    def test_healthy_windows_checks_pass(self):
        findings, _ = self._run([
            ("wuauserv", 0, "Running\n", ""),
            ("RealTimeProtectionEnabled", 0, "True\n", ""),
            ("SystemRestore", 0, "", ""),
        ])
        self.assertEqual({f.status for f in findings.values()}, {"PASS"})

    def test_missing_powershell_degrades_to_unavailable(self):
        with patch("scanner.diagnostics.subprocess.run",
                   side_effect=FileNotFoundError("powershell")):
            findings = diagnose_windows_os()
        self.assertEqual({f.status for f in findings}, {"UNAVAILABLE"})
        self.assertEqual(len(findings), 3)

    def test_unexpected_defender_output_is_unavailable_not_fail(self):
        findings, _ = self._run([
            ("wuauserv", 0, "Running\n", ""),
            ("RealTimeProtectionEnabled", 0, "garbage\n", ""),
            ("SystemRestore", 0, "", ""),
        ])
        self.assertEqual(findings["OS:Defender"].status, "UNAVAILABLE")
        self.assertEqual(findings["OS:WindowsUpdate"].status, "PASS")

    def test_unrecognised_service_status_is_unavailable(self):
        findings, _ = self._run([
            ("wuauserv", 0, "Paused\n", ""),
            ("RealTimeProtectionEnabled", 0, "True\n", ""),
            ("SystemRestore", 0, "", ""),
        ])
        self.assertEqual(findings["OS:WindowsUpdate"].status, "UNAVAILABLE")


class TestLinuxOSChecks(unittest.TestCase):
    def _run(self, responses):
        fake = FakeCommands(responses)
        with patch("scanner.diagnostics.subprocess.run", side_effect=fake):
            findings = diagnose_linux_os()
        fake.assert_read_only(self)
        return {f.key: f for f in findings}, fake

    def test_kernel_errors_inodes_and_failed_services(self):
        findings, _ = self._run([
            ("dmesg", 0, (
                "[ 1.0] ata1.00: I/O error, dev sda, sector 2\n"
                "[ 2.0] chrome[9]: segfault at 0\n"), ""),
            ("df", 0, (
                "Filesystem Inodes IUsed IFree IUse% Mounted on\n"
                "/dev/sda1 6553600 6291456 262144 96% /\n"), ""),
            ("systemctl", 0, (
                "backup.service loaded failed failed Nightly backup\n"
                "vpn.service loaded failed failed VPN helper\n"), ""),
        ])
        self.assertEqual(findings["OS:KernelLog"].status, "WARNING")
        self.assertEqual(findings["OS:KernelLog"].code,
                         "KERNEL_HARDWARE_ERROR")
        self.assertIn("I/O error", findings["OS:KernelLog"].detail)
        self.assertEqual(findings["OS:Inodes"].status, "FAIL")
        self.assertEqual(findings["OS:Inodes"].code, "INODES_CRITICAL")
        self.assertIn("/", findings["OS:Inodes"].message)
        self.assertEqual(findings["OS:Systemd"].status, "WARNING")
        self.assertEqual(findings["OS:Systemd"].code, "SYSTEMD_FAILED")
        self.assertIn("backup.service", findings["OS:Systemd"].detail)

    def test_clean_linux_passes(self):
        findings, _ = self._run([
            ("dmesg", 0, "[ 1.0] Linux version 6.1\n", ""),
            ("df", 0, (
                "Filesystem Inodes IUsed IFree IUse% Mounted on\n"
                "/dev/sda1 6553600 65536 6488064 1% /\n"), ""),
            ("systemctl", 0, "", ""),
        ])
        self.assertEqual({f.status for f in findings.values()}, {"PASS"})

    def test_restricted_dmesg_is_unavailable_with_permission_code(self):
        findings, _ = self._run([
            ("dmesg", 1, "", "dmesg: read kernel buffer failed: "
                             "Operation not permitted"),
            ("df", 0, (
                "Filesystem Inodes IUsed IFree IUse% Mounted on\n"
                "/dev/sda1 100 1 99 1% /\n"), ""),
            ("systemctl", 0, "", ""),
        ])
        self.assertEqual(findings["OS:KernelLog"].status, "UNAVAILABLE")
        self.assertEqual(findings["OS:KernelLog"].code, "ACCESS_DENIED")
        self.assertIn("not permitted",
                      findings["OS:KernelLog"].detail.lower())

    def test_missing_systemctl_degrades_only_that_check(self):
        def fake(argv, **kwargs):
            joined = " ".join(argv)
            if "systemctl" in joined:
                raise FileNotFoundError("systemctl")
            if "dmesg" in joined:
                return subprocess.CompletedProcess(argv, 0, "", "")
            return subprocess.CompletedProcess(argv, 0, (
                "Filesystem Inodes IUsed IFree IUse% Mounted on\n"
                "/dev/sda1 100 1 99 1% /\n"), "")

        with patch("scanner.diagnostics.subprocess.run", side_effect=fake):
            findings = {f.key: f for f in diagnose_linux_os()}
        self.assertEqual(findings["OS:Systemd"].status, "UNAVAILABLE")
        self.assertEqual(findings["OS:KernelLog"].status, "PASS")
        self.assertEqual(findings["OS:Inodes"].status, "PASS")


class TestMacOSOSChecks(unittest.TestCase):
    def _run(self, responses):
        fake = FakeCommands(responses)
        with patch("scanner.diagnostics.subprocess.run", side_effect=fake):
            findings = diagnose_macos_os()
        fake.assert_read_only(self)
        return {f.key: f for f in findings}, fake

    def test_log_errors_and_stale_time_machine(self):
        old = (datetime.now() - timedelta(days=40)).strftime(
            "%Y-%m-%d-%H%M%S")
        findings, _ = self._run([
            ("log show", 0, (
                "Timestamp               Ty Process[PID:TID]\n"
                "2026-10-21 10:00:00.000 E kernel[0]: disk0: I/O error\n"
                "2026-10-21 10:01:00.000 E app[1]: request failed\n"), ""),
            ("tmutil", 0, f"/Volumes/Backup/Mac/{old}\n", ""),
        ])
        self.assertEqual(findings["OS:MacLog"].status, "WARNING")
        self.assertEqual(findings["OS:MacLog"].code, "MACOS_LOG_ERRORS")
        self.assertEqual(findings["OS:TimeMachine"].status, "FAIL")
        self.assertEqual(findings["OS:TimeMachine"].code, "TIMEMACHINE_STALE")

    def test_clean_log_and_recent_backup_pass(self):
        recent = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        findings, _ = self._run([
            ("log show", 0,
             "Timestamp               Ty Process[PID:TID]\n", ""),
            ("tmutil", 0, f"/Volumes/Backup/Mac/{recent}\n", ""),
        ])
        self.assertEqual(findings["OS:MacLog"].status, "PASS")
        self.assertEqual(findings["OS:TimeMachine"].status, "PASS")

    def test_no_backups_warns(self):
        findings, _ = self._run([
            ("log show", 0, "", ""),
            ("tmutil", 1, "No backups found.\n", ""),
        ])
        self.assertEqual(findings["OS:TimeMachine"].status, "WARNING")
        self.assertEqual(findings["OS:TimeMachine"].code,
                         "TIMEMACHINE_NO_BACKUP")

    def test_missing_tmutil_is_unavailable(self):
        def fake(argv, **kwargs):
            if "tmutil" in " ".join(argv):
                raise FileNotFoundError("tmutil")
            return subprocess.CompletedProcess(argv, 0, "", "")

        with patch("scanner.diagnostics.subprocess.run", side_effect=fake):
            findings = {f.key: f for f in diagnose_macos_os()}
        self.assertEqual(findings["OS:TimeMachine"].status, "UNAVAILABLE")


class TestOSDispatch(unittest.TestCase):
    def test_unsupported_system_is_unavailable_without_commands(self):
        with patch("scanner.diagnostics.subprocess.run") as run_mock:
            findings = diagnose_os("Plan9")
        run_mock.assert_not_called()
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].status, "UNAVAILABLE")
        self.assertEqual(findings[0].code, "OS_UNSUPPORTED")

    def test_dispatch_follows_platform_system(self):
        fake = FakeCommands([
            ("dmesg", 0, "", ""),
            ("df", 0, (
                "Filesystem Inodes IUsed IFree IUse% Mounted on\n"
                "/dev/sda1 100 1 99 1% /\n"), ""),
            ("systemctl", 0, "", ""),
        ])
        with patch("scanner.diagnostics.subprocess.run", side_effect=fake), \
             patch("platform.system", return_value="Linux"):
            findings = diagnose_os()
        self.assertEqual({f.key for f in findings},
                         {"OS:KernelLog", "OS:Inodes", "OS:Systemd"})


class TestOSCheckIntegration(unittest.TestCase):
    def _snap(self):
        return HardwareSnapshot(
            cpu=CpuSnapshot(available=True, percent=10.0, logical_cores=8,
                            physical_cores=4),
            mem=MemSnapshot(available=True, percent=20.0, total=8, used=2),
            disks=[DiskEntry(device="C:", mountpoint="C:", total=100,
                             used=10, free=90, percent=10.0,
                             accessible=True)],
            net=NetSnapshot(available=True, interfaces=["Ethernet"]),
            temps=[TempReading(label="x", current=None, source="s",
                               status="unsupported")],
            gpu=GpuStatus(supported=False, message="no gpu"),
        )

    def test_include_os_merges_os_findings_and_advice(self):
        os_finding = Finding("OS: Test", "WARNING", "needs attention",
                             key="OS:Test")
        with patch("scanner.diagnostics.diagnose_os",
                   return_value=[os_finding]) as os_mock:
            rep = run_diagnostics(self._snap(), include_os=True)
        os_mock.assert_called_once_with()
        self.assertIn("OS:Test", {f.key for f in rep.findings})
        self.assertTrue(any("how to fix" in r.lower()
                            for r in rep.recommendations))

    def test_include_os_false_is_hermetic(self):
        with patch("scanner.diagnostics.diagnose_os") as os_mock:
            rep = run_diagnostics(self._snap(), include_os=False)
        os_mock.assert_not_called()
        self.assertFalse(any(f.key.startswith("OS:") for f in rep.findings))


class TestSystemHealthScore(unittest.TestCase):
    def _snap(self, cpu=10.0, mem=20.0, disk=30.0):
        return HardwareSnapshot(
            cpu=CpuSnapshot(available=True, percent=cpu, logical_cores=4,
                            physical_cores=2),
            mem=MemSnapshot(available=True, percent=mem, total=100, used=mem),
            disks=[DiskEntry(device="C:", mountpoint="C:", total=100,
                             used=disk, free=100 - disk, percent=disk,
                             accessible=True)],
        )

    def test_perfect_score(self):
        self.assertEqual(system_health_score(self._snap(0, 0, 0), [])["total"],
                         100)

    def test_full_usage_keeps_healthy_os_weight(self):
        # All hardware metrics are maxed out, but the OS checks passed, so the
        # 20% OS share still contributes.
        self.assertEqual(
            system_health_score(self._snap(100, 100, 100), [])["total"], 20)

    def test_os_fail_and_warning_penalise_weighted_component(self):
        health = system_health_score(
            self._snap(0, 0, 0),
            [Finding("OS: A", "FAIL", "x", key="OS:A"),
             Finding("OS: B", "WARNING", "x", key="OS:B")])
        self.assertEqual(health["os_fail"], 1)
        self.assertEqual(health["os_warning"], 1)
        self.assertEqual(health["components"]["os"], 86.0)
        self.assertEqual(health["total"], 97)

    def test_unavailable_os_finding_does_not_lower_score(self):
        health = system_health_score(
            self._snap(0, 0, 0),
            [Finding("OS: A", "UNAVAILABLE", "x", key="OS:A")])
        self.assertEqual(health["total"], 100)
        self.assertEqual(health["components"]["os"], 100.0)

    def test_os_component_excluded_before_first_scan(self):
        health = system_health_score(self._snap(50, 50, 50), None)
        self.assertNotIn("os", health["components"])
        self.assertEqual(health["total"], 50)

    def test_partial_metrics_renormalise(self):
        snap = HardwareSnapshot(cpu=CpuSnapshot(available=True, percent=40.0))
        self.assertEqual(system_health_score(snap, None)["total"], 60)

    def test_no_data_gives_none(self):
        self.assertIsNone(system_health_score(None, None)["total"])
        self.assertIsNone(system_health_score(HardwareSnapshot(), [])["total"])


if __name__ == "__main__":
    unittest.main()
