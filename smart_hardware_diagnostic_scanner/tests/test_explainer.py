"""Tests for the plain-English Error Explainer (no real OS commands)."""
from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from scanner.diagnostics import Finding, diagnose_linux_os
from scanner.explainer import (
    EXPLAINED_STATUSES,
    Explanation,
    explain_finding,
    known_codes,
)


def _completed(argv, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def _fake_linux_run(argv, **_kwargs):
    """Fake `dmesg` / `df -i` / `systemctl` output for the explainer tests."""
    joined = " ".join(str(part) for part in argv)
    if "dmesg" in joined:
        return _completed(argv, 0, (
            "[ 123.4] ata1.00: I/O error, dev sda, sector 2048\n"
            "[ 124.0] chrome[321]: segfault at 0 ip 00007f\n"
            "[ 125.0] usb 1-1: new high-speed USB device number 4\n"))
    if joined.startswith("df "):
        return _completed(argv, 0, (
            "Filesystem Inodes IUsed IFree IUse% Mounted on\n"
            "/dev/sda1 6553600 65536 6488064 1% /\n"))
    if "systemctl" in joined:
        return _completed(argv, 0, "")
    return _completed(argv, 0, "")


class TestFindingToExplanation(unittest.TestCase):
    def _linux_findings(self):
        with patch("scanner.diagnostics.subprocess.run",
                   side_effect=_fake_linux_run):
            return diagnose_linux_os()

    def test_fake_dmesg_output_is_identified_and_explained(self):
        findings = self._linux_findings()
        kernel = next(f for f in findings if f.key == "OS:KernelLog")
        self.assertEqual(kernel.status, "WARNING")
        self.assertEqual(kernel.code, "KERNEL_HARDWARE_ERROR")
        explanation = explain_finding(kernel, system="Linux")
        self.assertIsNotNone(explanation)
        self.assertIn("I/O error", explanation.explanation)
        self.assertIn("segfault", explanation.explanation)
        self.assertEqual(explanation.kind, "command")
        self.assertIn("dmesg", explanation.target)
        # The explanation never claims it changed or deleted anything.
        self.assertIn("only reads", explanation.fix)

    def test_unavailable_findings_are_not_explained(self):
        # dmesg unreadable (common without root) must not invent a fix.
        with patch("scanner.diagnostics.subprocess.run",
                   side_effect=PermissionError("denied")):
            findings = diagnose_linux_os()
        kernel = next(f for f in findings if f.key == "OS:KernelLog")
        self.assertEqual(kernel.status, "UNAVAILABLE")
        self.assertIsNone(explain_finding(kernel, system="Linux"))

    def test_pass_findings_are_not_explained(self):
        finding = Finding("CPU", "PASS", "healthy", key="CPU")
        self.assertIsNone(explain_finding(finding, system="Windows"))

    def test_none_finding_is_safe(self):
        self.assertIsNone(explain_finding(None, system="Windows"))

    def test_access_denied_mentions_administrator(self):
        finding = Finding("Storage", "WARNING", "Access Denied reading sensor",
                          detail="permission denied", key="Storage:C:")
        explanation = explain_finding(finding, system="Windows")
        self.assertIsNotNone(explanation)
        self.assertIn("Administrator", explanation.fix)
        self.assertIn("permission", explanation.explanation.lower())
        self.assertEqual(explanation.kind, "info")

    def test_disk_full_windows_offers_storage_sense_uri(self):
        finding = Finding("Storage", "FAIL", "C: 96.4% used (NTFS)",
                          key="Storage:C:", code="DISK_FULL")
        explanation = explain_finding(finding, system="Windows")
        self.assertIsNotNone(explanation)
        self.assertIn("slows", explanation.explanation)
        self.assertEqual(explanation.kind, "uri")
        self.assertEqual(explanation.target, "ms-settings:storagesense")

    def test_disk_full_linux_offers_baobab_command(self):
        finding = Finding("Storage", "FAIL", "/ 97.0% used (ext4)",
                          key="Storage:/", code="DISK_FULL")
        explanation = explain_finding(finding, system="Linux")
        self.assertEqual(explanation.kind, "command")
        self.assertIn("baobab", explanation.target)

    def test_disk_full_macos_offers_storage_settings_command(self):
        finding = Finding("Storage", "FAIL", "/ 96.0% used (apfs)",
                          key="Storage:/", code="DISK_FULL")
        explanation = explain_finding(finding, system="Darwin")
        self.assertEqual(explanation.kind, "command")
        self.assertIn("x-apple.systempreferences", explanation.target)

    def test_disk_pattern_without_code_still_gets_storage_fix(self):
        finding = Finding("Storage", "WARNING", "D: 88.0% used (NTFS)")
        explanation = explain_finding(finding, system="Windows")
        self.assertIsNotNone(explanation)
        self.assertEqual(explanation.target, "ms-settings:storagesense")

    def test_windows_os_codes_have_specific_targets(self):
        cases = {
            "WINDOWS_UPDATE_STOPPED": "ms-settings:windowsupdate",
            "DEFENDER_DISABLED": "ms-settings:windowsdefender",
        }
        for code, target in cases.items():
            explanation = explain_finding(
                Finding("OS: Test", "WARNING", "x", key="OS:Test", code=code),
                system="Windows")
            self.assertIsNotNone(explanation, code)
            self.assertEqual(explanation.kind, "uri")
            self.assertEqual(explanation.target, target)

    def test_system_restore_copies_safe_applet_command(self):
        explanation = explain_finding(
            Finding("OS: Test", "WARNING", "off", key="OS:Test",
                    code="SYSTEM_RESTORE_OFF"),
            system="Windows")
        self.assertEqual(explanation.kind, "command")
        self.assertIn("SystemPropertiesProtection", explanation.target)

    def test_systemd_and_inode_fixes_are_read_only_inspection(self):
        systemd = explain_finding(
            Finding("OS: Test", "WARNING", "failed", key="OS:Test",
                    code="SYSTEMD_FAILED"),
            system="Linux")
        self.assertEqual(systemd.kind, "command")
        self.assertIn("systemctl --failed", systemd.target)
        inodes = explain_finding(
            Finding("OS: Test", "FAIL", "inodes", key="OS:Test",
                    code="INODES_CRITICAL"),
            system="Linux")
        self.assertEqual(inodes.kind, "command")
        self.assertEqual(inodes.target, "df -i")

    def test_macos_log_and_time_machine_fixes(self):
        log = explain_finding(
            Finding("OS: Test", "WARNING", "errors", key="OS:Test",
                    code="MACOS_LOG_ERRORS"),
            system="Darwin")
        self.assertIn("log show", log.target)
        tm = explain_finding(
            Finding("OS: Test", "FAIL", "old", key="OS:Test",
                    code="TIMEMACHINE_STALE"),
            system="Darwin")
        self.assertEqual(tm.kind, "command")
        self.assertEqual(tm.target, "tmutil startbackup")

    def test_unknown_fail_gets_generic_honest_explanation(self):
        finding = Finding("Widget", "FAIL", "the widget broke",
                          detail="error 42", key="Widget")
        explanation = explain_finding(finding, system="Windows")
        self.assertIsNotNone(explanation)
        self.assertEqual(explanation.kind, "info")
        self.assertIn("Widget", explanation.title)
        self.assertIn("No automatic fix", explanation.fix)
        self.assertIn("nothing has been changed", explanation.explanation)

    def test_all_explained_kinds_are_known(self):
        finding = Finding("CPU", "WARNING", "high", code="CPU_HIGH")
        explanation = explain_finding(finding, system="Linux")
        self.assertIn(explanation.kind, ("uri", "command", "info"))

    def test_codes_emitted_by_os_checks_have_explanations(self):
        expected = {
            "WINDOWS_UPDATE_STOPPED", "DEFENDER_DISABLED",
            "SYSTEM_RESTORE_OFF", "KERNEL_HARDWARE_ERROR", "INODES_LOW",
            "INODES_CRITICAL", "SYSTEMD_FAILED", "MACOS_LOG_ERRORS",
            "TIMEMACHINE_STALE", "TIMEMACHINE_NO_BACKUP", "DISK_FULL",
            "DISK_LOW", "CPU_HIGH", "MEM_HIGH", "GPU_HOT", "ACCESS_DENIED",
        }
        self.assertTrue(expected.issubset(known_codes()),
                        expected - known_codes())

    def test_explanation_is_frozen_and_has_no_side_effects(self):
        explanation = explain_finding(
            Finding("Storage", "FAIL", "full", code="DISK_FULL"),
            system="Windows")
        self.assertIsInstance(explanation, Explanation)
        with self.assertRaises(Exception):
            explanation.kind = "command"  # frozen dataclass
        self.assertIn("FAIL", EXPLAINED_STATUSES)


if __name__ == "__main__":
    unittest.main()
