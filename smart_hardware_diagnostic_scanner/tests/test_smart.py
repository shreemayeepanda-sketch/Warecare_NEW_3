"""Tests for SMART checks: pure helpers, missing tool, timeouts, parsing."""
from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from scanner import smart as sm

PASSED_OUT = (
    "smartctl 7.4 2023-08-08 r5262 [x86_64-w64-mingw32-win10] (local build)\n"
    "\n"
    "=== START OF INFORMATION SECTION ===\n"
    "Device Model:     WDC WD10EZEX-08WN4A0\n"
    "=== START OF READ SMART DATA SECTION ===\n"
    "SMART overall-health self-assessment test result: PASSED\n"
)

FAILED_OUT = (
    "Device Model:     WDC WD10EZEX-08WN4A0\n"
    "SMART overall-health self-assessment test result: FAILED!\n"
)

SCAN_OUT = ("# 1  /dev/sda   -d ata #  , device is N/A\n"
            "/dev/sdb -d ata #  , device is N/A\n"
            "junk line without any device\n")


def _run_stub(test, scan_stdout=SCAN_OUT, script=()):
    """subprocess.run stand-in: script items are results or exception instances.

    Returns (run_fn, health_devices) so tests can assert on health calls.
    """
    health_devices: list[str] = []
    health_types: list[str] = []
    scan_proc = subprocess.CompletedProcess(args=["smartctl", "--scan"],
                                            returncode=0,
                                            stdout=scan_stdout, stderr="")
    steps = iter(script)

    def run(argv, **kwargs):
        test.assertIsInstance(argv, list)
        test.assertEqual(argv[0], "smartctl")
        test.assertFalse(kwargs.get("shell", True))
        test.assertTrue(kwargs.get("capture_output"))
        test.assertTrue(kwargs.get("text"))
        test.assertEqual(kwargs.get("timeout"), 8)
        if argv[1:2] == ["--scan"]:
            test.assertEqual(argv, ["smartctl", "--scan"])
            return scan_proc
        # The health query may carry the "-d <type>" hint that smartctl --scan
        # prints for USB bridges / NVMe devices.
        if argv[1:2] == ["-d"]:
            health_types.append(argv[2])
            test.assertEqual(argv[3], "--health")
            health_devices.append(argv[4])
        else:
            test.assertEqual(argv[1], "--health")
            health_devices.append(argv[2])
        step = next(steps)
        if isinstance(step, BaseException):
            raise step
        return step

    return run, health_devices


class TestPureHelpers(unittest.TestCase):
    def test_classify_smart_exit(self):
        self.assertEqual(sm.classify_smart_exit(0), "PASS")
        self.assertEqual(sm.classify_smart_exit(8), "FAIL")
        self.assertEqual(sm.classify_smart_exit(9), "FAIL")  # 8 | 1: health failed
        self.assertEqual(sm.classify_smart_exit(4), "UNAVAILABLE")
        self.assertEqual(sm.classify_smart_exit(2), "UNAVAILABLE")

    def test_argv_lists_are_pure(self):
        self.assertEqual(sm.scan_argv("smartctl"), ["smartctl", "--scan"])
        self.assertEqual(sm.health_argv("smartctl", "/dev/sda"),
                         ["smartctl", "--health", "/dev/sda"])

    def test_device_token_extraction(self):
        # Windows/Linux --scan rows, indexed or bare, resolve to the path.
        self.assertEqual(sm._device_of("/dev/sda -d ata #  , device is N/A"),
                         "/dev/sda")
        self.assertEqual(sm._device_of("# 0  /dev/sda   -d ata #  ,"), "/dev/sda")
        self.assertEqual(sm._device_of("# 2  /dev/nvme0   -d nvme #"), "/dev/nvme0")
        # Uncertain indexed rows fall back to the third whitespace token.
        self.assertEqual(sm._device_of("# 7  odd-device-name"), "odd-device-name")
        # Junk lines are skipped.
        self.assertEqual(sm._device_of(""), "")
        self.assertEqual(sm._device_of("   "), "")
        self.assertEqual(sm._device_of("junk line without device"), "")
        self.assertEqual(sm._device_of("=== START OF INFORMATION SECTION ==="), "")
        self.assertEqual(sm._device_of("# just-a-header"), "")

    def test_model_extraction(self):
        self.assertEqual(sm._model_of(PASSED_OUT), "WDC WD10EZEX-08WN4A0")
        self.assertEqual(sm._model_of("no model mention here"), "")


class TestQuerySmartDevices(unittest.TestCase):
    def test_missing_tool_degrades(self):
        with patch.object(sm, "which", return_value=None):
            st = sm.query_smart_devices()
            self.assertFalse(st.supported)
            self.assertFalse(st.tool_found)
            self.assertEqual(st.devices, [])
            self.assertIn("not found", st.message)
            self.assertIn("smartmontools", st.message)

    def test_scan_nonzero_exit(self):
        fake = subprocess.CompletedProcess(args=["smartctl"], returncode=2,
                                           stdout="", stderr="permission denied")
        with patch.object(sm, "which", return_value="smartctl"), \
             patch.object(sm.subprocess, "run", return_value=fake):
            st = sm.query_smart_devices()
            self.assertTrue(st.tool_found)
            self.assertFalse(st.supported)
            self.assertIn("failed", st.message)
            self.assertIn("permission denied", st.message)

    def test_scan_timeout(self):
        with patch.object(sm, "which", return_value="smartctl"), \
             patch.object(sm.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired(cmd="x", timeout=8)):
            st = sm.query_smart_devices(timeout=1)
            self.assertFalse(st.supported)
            self.assertIn("timed out", st.message)

    def test_scan_oserror(self):
        with patch.object(sm, "which", return_value="smartctl"), \
             patch.object(sm.subprocess, "run", side_effect=OSError("gone")):
            st = sm.query_smart_devices()
            self.assertFalse(st.supported)
            self.assertTrue(st.tool_found)

    def test_no_devices_parsed(self):
        fake = subprocess.CompletedProcess(args=["smartctl"], returncode=0,
                                           stdout="no matching devices found\n",
                                           stderr="")
        with patch.object(sm, "which", return_value="smartctl"), \
             patch.object(sm.subprocess, "run", return_value=fake):
            st = sm.query_smart_devices()
            self.assertFalse(st.supported)
            self.assertEqual(st.devices, [])


class TestParsedRuns(unittest.TestCase):
    def test_healthy_scan_two_pass_devices(self):
        healthy = subprocess.CompletedProcess(args=["smartctl", "--health", "x"],
                                              returncode=0,
                                              stdout=PASSED_OUT, stderr="")
        run_fn, called = _run_stub(self, script=[healthy, healthy])
        with patch.object(sm, "which", return_value="smartctl"), \
             patch.object(sm.subprocess, "run", side_effect=run_fn):
            st = sm.query_smart_devices()
        self.assertTrue(st.supported)
        self.assertEqual(st.tool_found, True)
        self.assertEqual([d.status for d in st.devices], ["PASS", "PASS"])
        self.assertEqual([d.device for d in st.devices], ["/dev/sda", "/dev/sdb"])
        self.assertEqual(st.devices[0].model, "WDC WD10EZEX-08WN4A0")
        self.assertEqual(called, ["/dev/sda", "/dev/sdb"])
        self.assertEqual(st.message, "2 device(s) checked: 2 PASS / 0 FAIL.")

    def test_failing_health_maps_to_fail(self):
        fail = subprocess.CompletedProcess(args=["smartctl", "--health", "x"],
                                           returncode=8,
                                           stdout=FAILED_OUT, stderr="")
        run_fn, called = _run_stub(self, scan_stdout="# 1  /dev/sda   -d ata #",
                                   script=[fail])
        with patch.object(sm, "which", return_value="smartctl"), \
             patch.object(sm.subprocess, "run", side_effect=run_fn):
            st = sm.query_smart_devices()
        self.assertTrue(st.supported)
        self.assertEqual(st.devices[0].status, "FAIL")
        # detail: first non-empty output line, per the query contract
        self.assertTrue(st.devices[0].detail.startswith("Device Model"))
        self.assertEqual(st.message, "1 device(s) checked: 0 PASS / 1 FAIL.")

    def test_health_timeout_degrades_one_device(self):
        timeout = subprocess.TimeoutExpired(cmd="smartctl", timeout=8)
        run_fn, called = _run_stub(self, scan_stdout="# 1  /dev/sda   -d ata #",
                                   script=[timeout])
        with patch.object(sm, "which", return_value="smartctl"), \
             patch.object(sm.subprocess, "run", side_effect=run_fn):
            st = sm.query_smart_devices()
        self.assertTrue(st.supported)
        self.assertEqual(len(st.devices), 1)
        self.assertEqual(st.devices[0].status, "UNAVAILABLE")
        self.assertIn("timed out", st.devices[0].detail)
        self.assertEqual(called, ["/dev/sda"])

    def test_device_error_never_aborts_loop(self):
        run_fn, called = _run_stub(
            self, scan_stdout="# 1  /dev/sda\n# 2  /dev/sdb\n",
            script=[OSError("spawn failed"), OSError("gone")])
        with patch.object(sm, "which", return_value="smartctl"), \
             patch.object(sm.subprocess, "run", side_effect=run_fn):
            st = sm.query_smart_devices()
        self.assertEqual(len(st.devices), 2)
        self.assertEqual(called, ["/dev/sda", "/dev/sdb"])
        for dev in st.devices:
            self.assertEqual(dev.status, "UNAVAILABLE")
            dev_error = dev.detail
            self.assertIn("smartctl", dev_error)

    def test_max_devices_caps(self):
        scan = "# 1  /dev/sda\n# 2  /dev/sdb\n# 3  /dev/sdc\n"
        ok = subprocess.CompletedProcess(args=["smartctl", "--health", "x"],
                                         returncode=0,
                                         stdout=PASSED_OUT, stderr="")
        run_fn, called = _run_stub(self, scan_stdout=scan, script=[ok, ok])
        with patch.object(sm, "which", return_value="smartctl"), \
             patch.object(sm.subprocess, "run", side_effect=run_fn):
            st = sm.query_smart_devices(max_devices=2)
        self.assertEqual(len(st.devices), 2)
        self.assertEqual(called, ["/dev/sda", "/dev/sdb"])
        self.assertEqual(st.message, "2 device(s) checked: 2 PASS / 0 FAIL.")


if __name__ == "__main__":
    unittest.main()
