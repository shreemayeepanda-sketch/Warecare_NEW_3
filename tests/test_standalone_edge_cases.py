"""Standalone-build edge-case audit (Agent 3 — Cross-Platform & Edge Cases).

She tests the flattened single-file build ``ewarecare.py`` ONLY (the original
package under ``smart_hardware_diagnostic_scanner/`` is never imported):

* ``diagnose_windows_os`` / ``diagnose_linux_os`` / ``diagnose_macos_os`` must
  degrade to ``UNAVAILABLE`` status (or the documented not-an-error
  equivalent, ``PASS`` when a log is legitimately empty) for empty /
  whitespace-only / malformed / timeout / non-UTF-8 command output — never
  raise;
* the kill_process safety fence must have an irreducible hard floor: emptying
  (or corrupting) ``PROTECTED_PROCESS_NAMES`` must NOT make the engine able to
  terminate csrss / lsass / System / kernel_task and friends;
* ``ActionRule`` must refuse ``duration_s`` of 0 or negative — a rule with no
  sustained-breach window fires on the very first monitoring sample, which
  voids the "fires only after the FULL duration" safety invariant.

Command output is mocked the same way the package tests in
``smart_hardware_diagnostic_scanner/tests/test_diagnostics.py`` do it:
``subprocess.run`` is patched and handed ``CompletedProcess`` fakes, so no
real ``dmesg`` / ``df`` / ``systemctl`` / ``log show`` / ``tmutil`` /
``powershell`` is ever executed. The single exception is the non-UTF-8
decoding contract test, which runs a harmless child of THIS interpreter
(``sys.executable -c``) writing a few invalid byte sequences to stdout —
no OS diagnostic command, no network access, no process management.

BUG numbering matches the historical edge-case audit (Agent 3):
  BUG-1 non-UTF-8 command output crashes / vanishes in ``_run_readonly``
  BUG-2 no hard floor under ``PROTECTED_PROCESS_NAMES``
  BUG-3 ``ActionRule(duration_s <= 0)`` accepted → instant kill on first tick
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
STANDALONE = ROOT / "ewarecare.py"


def _load_ewarecare():
    """Import the standalone build from its file path.

    Importlib must register the module in ``sys.modules`` BEFORE
    ``exec_module``: the file contains ``@dataclass`` definitions whose
    metadata scan calls ``sys.modules.get(cls.__module__).__dict__``.
    The module loads under a non-``__main__`` name so the
    ``if __name__ == "__main__": raise SystemExit(main())`` guard at the end
    does NOT launch the GUI; both facts are asserted.
    """
    assert STANDALONE.exists(), f"missing {STANDALONE}"
    source = STANDALONE.read_text(encoding="utf-8")
    assert 'if __name__ == "__main__":' in source, \
        "standalone lost its __main__ guard; the import would launch the GUI"
    name = "ewarecare_audit"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, STANDALONE)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    mod = sys.modules[name]
    assert mod.__name__ != "__main__", "GUI must not launch on import"
    assert callable(getattr(mod, "main", None))
    return mod


ewarecare = _load_ewarecare()


# ---------- fakes (style borrowed from the package's test_diagnostics.py) -----

class FakeCommands:
    """Marker-matching fake for ``subprocess.run`` returning CompletedProcess."""

    def __init__(self, responses) -> None:
        # responses: list of (marker, returncode, stdout, stderr)
        self.responses = list(responses)
        self.calls: list[tuple[list, dict]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), dict(kwargs)))
        joined = " ".join(str(part) for part in argv)
        for marker, returncode, stdout, stderr in self.responses:
            if marker in joined:
                return subprocess.CompletedProcess(
                    argv, returncode, stdout, stderr)
        return subprocess.CompletedProcess(argv, 0, "", "")

    def assert_read_only(self, testcase) -> None:
        testcase.assertTrue(self.calls, "no OS command was invoked")
        for argv, kwargs in self.calls:
            testcase.assertIs(kwargs.get("shell"), False, f"shell used: {argv}")
            testcase.assertTrue(kwargs.get("capture_output"),
                                f"output not captured: {argv}")


def always_timeout(argv, timeout=None, **kwargs):
    """Simulate every real command hanging (raises the documented crash way)."""
    raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout or 8)


_LINUX_DF_HEADER = (
    "Filesystem Inodes IUsed IFree IUse% Mounted on\n"
    "/dev/sda1 6553600 65536 6488064 1% /\n")

_LINUX_OK = [
    ("dmesg", 0, "[ 1.0] Linux version 6.1\n", ""),
    ("df", 0, _LINUX_DF_HEADER, ""),
    ("systemctl", 0, "", ""),
]


def run_linux(responses, runner=None):
    fake = runner or FakeCommands(responses)
    with patch.object(ewarecare.subprocess, "run", side_effect=fake):
        findings = ewarecare.diagnose_linux_os()
    return findings, fake


def run_macos(responses, runner=None):
    fake = runner or FakeCommands(responses)
    with patch.object(ewarecare.subprocess, "run", side_effect=fake):
        findings = ewarecare.diagnose_macos_os()
    return findings, fake


def run_windows(responses, runner=None):
    fake = runner or FakeCommands(responses)
    with patch.object(ewarecare.subprocess, "run", side_effect=fake):
        findings = ewarecare.diagnose_windows_os()
    return findings, fake


def hot_cpu_snapshot():
    snap = ewarecare.HardwareSnapshot()
    snap.cpu = ewarecare.CpuSnapshot(available=True, percent=97.0)
    return snap


# ---------- Linux: empty / whitespace / malformed output ----------------------

class TestLinuxDegradation(unittest.TestCase):
    def test_all_empty_output_with_zero_exit_never_raises(self):
        # The StopIteration / .split(None) / IndexError hazard class: every
        # parser must cope. rc=0 with no data: kernel log honestly says
        # "nothing found" (PASS), df and systemctl degrade.
        findings, fake = run_linux([("dmesg", 0, "", ""),
                                    ("df", 0, "", ""),
                                    ("systemctl", 0, "", "")])
        by_key = {f.key: f for f in findings}
        self.assertEqual(by_key["OS:KernelLog"].status, "PASS")
        self.assertEqual(by_key["OS:Inodes"].status, "UNAVAILABLE")
        self.assertEqual(by_key["OS:Systemd"].status, "PASS")
        fake.assert_read_only(self)

    def test_whitespace_only_output_degrades_not_crashes(self):
        findings, _ = run_linux([("dmesg", 0, " \n\t\n   \n", ""),
                                 ("df", 0, "   \n \n", ""),
                                 ("systemctl", 0, "  \n", "")])
        by_key = {f.key: f for f in findings}
        self.assertEqual(by_key["OS:KernelLog"].status, "PASS")
        self.assertEqual(by_key["OS:Inodes"].status, "UNAVAILABLE")
        self.assertEqual(by_key["OS:Systemd"].status, "PASS")

    def test_missing_df_columns_yields_unavailable(self):
        # ``df`` rows that lost columns must be skipped until UNAVAILABLE,
        # never raise IndexError / int() ValueError.
        findings, _ = run_linux([("dmesg", 0, "", ""),
                                 ("df", 0, "/dev/sda1 6553600 96% /\n", ""),
                                 ("systemctl", 0, "", "")])
        by_key = {f.key: f for f in findings}
        self.assertEqual(by_key["OS:Inodes"].status, "UNAVAILABLE")

    def test_unexpected_units_in_inode_column_are_skipped(self):
        # "96,5%" (decimal comma) is not an int: the row must be ignored,
        # not crash the scan.
        findings, _ = run_linux([("dmesg", 0, "", ""),
                                 ("df", 0, (
                                     "Filesystem Inodes IUsed IFree IUse% "
                                     "Mounted on\n"
                                     "overlay 6553600 6291456 262144 "
                                     "96,5% /var/lib/docker\n"), ""),
                                 ("systemctl", 0, "", "")])
        by_key = {f.key: f for f in findings}
        self.assertEqual(by_key["OS:Inodes"].status, "UNAVAILABLE")

    def test_dmesg_garbage_lines_never_crash(self):
        findings, _ = run_linux([
            ("dmesg", 0, "\x0c\n[garbage @#$%]\n???\n--none--\n", ""),
            ("df", 0, _LINUX_DF_HEADER, ""),
            ("systemctl", 0, "", ""),
        ])
        self.assertEqual(findings[0].status, "PASS")

    def test_nonzero_exit_with_data_ignores_the_data(self):
        # dmesg exiting 3 while still printing "error" lines: without rc==0
        # the data is untrustworthy, so the finding must be UNAVAILABLE.
        findings, _ = run_linux([("dmesg", 3, "ata1.00: I/O error, dev sda\n", ""),
                                 ("df", 0, _LINUX_DF_HEADER, ""),
                                 ("systemctl", 0, "", "")])
        by_key = {f.key: f for f in findings}
        self.assertEqual(by_key["OS:KernelLog"].status, "UNAVAILABLE")
        self.assertNotEqual(by_key["OS:Inodes"].status, "UNAVAILABLE")

    def test_timeout_degrades_every_check(self):
        with patch.object(ewarecare.subprocess, "run",
                          side_effect=always_timeout):
            findings = ewarecare.diagnose_linux_os()
        self.assertEqual(len(findings), 3)
        self.assertEqual({f.status for f in findings}, {"UNAVAILABLE"})

    def test_only_systemctl_missing_degrades_just_that_check(self):
        def fake(argv, **kwargs):
            if "systemctl" in " ".join(argv):
                raise FileNotFoundError("systemctl")
            return subprocess.CompletedProcess(argv, 0, "", "")

        with patch.object(ewarecare.subprocess, "run", side_effect=fake):
            findings = {f.key: f
                        for f in ewarecare.diagnose_linux_os()}
        self.assertEqual(findings["OS:Systemd"].status, "UNAVAILABLE")
        self.assertEqual(findings["OS:KernelLog"].status, "PASS")
        self.assertEqual(findings["OS:Inodes"].status, "UNAVAILABLE")


# ---------- macOS ------------------------------------------------------------

class TestMacOSDegradation(unittest.TestCase):
    def test_empty_system_log_and_empty_tmutil_never_raise(self):
        findings, fake = run_macos([("log show", 0, "", ""),
                                    ("tmutil", 0, "", "")])
        by_key = {f.key: f for f in findings}
        self.assertEqual(by_key["OS:MacLog"].status, "PASS")
        self.assertEqual(by_key["OS:TimeMachine"].status, "UNAVAILABLE")
        fake.assert_read_only(self)

    def test_whitespace_only_outputs(self):
        findings, _ = run_macos([("log show", 0, "  \n   \n", ""),
                                 ("tmutil", 0, "  \n", "")])
        by_key = {f.key: f for f in findings}
        self.assertEqual(by_key["OS:MacLog"].status, "PASS")
        self.assertEqual(by_key["OS:TimeMachine"].status, "UNAVAILABLE")

    def test_log_header_only_counts_zero_errors(self):
        findings, _ = run_macos([
            ("log show", 0, "Timestamp               Ty Process[PID:TID]\n"
                            "Timestamp               Ty Process[PID:TID]\n", ""),
            ("tmutil", 0, "", ""),
        ])
        by_key = {f.key: f for f in findings}
        self.assertEqual(by_key["OS:MacLog"].status, "PASS")

    def test_nonzero_exit_with_data_ignores_the_data(self):
        findings, _ = run_macos([("log show", 5, "kernel[0]: disk I/O error\n", ""),
                                 ("tmutil", 0, "", "")])
        by_key = {f.key: f for f in findings}
        self.assertEqual(by_key["OS:MacLog"].status, "UNAVAILABLE")
        self.assertEqual(by_key["OS:TimeMachine"].status, "UNAVAILABLE")

    def test_timeout_degrades_both_checks(self):
        with patch.object(ewarecare.subprocess, "run",
                          side_effect=always_timeout):
            findings = ewarecare.diagnose_macos_os()
        self.assertEqual(len(findings), 2)
        self.assertEqual({f.status for f in findings}, {"UNAVAILABLE"})

    def test_missing_tmutil_binary_is_not_a_crash(self):
        def fake(argv, **kwargs):
            if "tmutil" in " ".join(argv):
                raise FileNotFoundError("tmutil")
            return subprocess.CompletedProcess(argv, 0, "", "")

        with patch.object(ewarecare.subprocess, "run", side_effect=fake):
            findings = {f.key: f for f in ewarecare.diagnose_macos_os()}
        self.assertEqual(findings["OS:TimeMachine"].status, "UNAVAILABLE")
        self.assertEqual(findings["OS:MacLog"].status, "PASS")


# ---------- Windows -----------------------------------------------------------

class TestWindowsDegradation(unittest.TestCase):
    def test_all_null_output_with_zero_exit_never_raises(self):
        findings, fake = run_windows([("wuauserv", 0, "", ""),
                                      ("RealTimeProtectionEnabled", 0, "", ""),
                                      ("SystemRestore", 0, "", "")])
        by_key = {f.key: f for f in findings}
        self.assertEqual(by_key["OS:WindowsUpdate"].status, "UNAVAILABLE")
        self.assertEqual(by_key["OS:Defender"].status, "UNAVAILABLE")
        self.assertEqual(by_key["OS:SystemRestore"].status, "PASS")
        fake.assert_read_only(self)

    def test_whitespace_only_ps_output(self):
        findings, _ = run_windows([("wuauserv", 0, "  \r\n", ""),
                                   ("RealTimeProtectionEnabled", 0, " \n", ""),
                                   ("SystemRestore", 0, " \n", "")])
        by_key = {f.key: f for f in findings}
        self.assertEqual(by_key["OS:WindowsUpdate"].status, "UNAVAILABLE")
        self.assertEqual(by_key["OS:Defender"].status, "UNAVAILABLE")

    def test_missing_columns_garbage_output_is_unavailable(self):
        findings, _ = run_windows([("wuauserv", 0, "Paused,42,\n", ""),
                                   ("RealTimeProtectionEnabled", 0, "\x07\n", ""),
                                   ("SystemRestore", 0, "0 junk junk\n", "")])
        by_key = {f.key: f for f in findings}
        self.assertEqual(by_key["OS:WindowsUpdate"].status, "UNAVAILABLE")
        self.assertEqual(by_key["OS:Defender"].status, "UNAVAILABLE")

    def test_nonzero_exit_with_data_ignores_the_data(self):
        findings, _ = run_windows([("wuauserv", 125, "Running\n", ""),
                                   ("RealTimeProtectionEnabled", 9, "True\n", ""),
                                   ("SystemRestore", 1, "1\n", "")])
        by_key = {f.key: f for f in findings}
        self.assertEqual(len(by_key), 3)
        self.assertTrue(all(f.status == "UNAVAILABLE" for f in by_key.values()))

    def test_timeout_degrades_every_check(self):
        with patch.object(ewarecare.subprocess, "run",
                          side_effect=always_timeout):
            findings = ewarecare.diagnose_windows_os()
        self.assertEqual(len(findings), 3)
        self.assertEqual({f.status for f in findings}, {"UNAVAILABLE"})

    def test_missing_powershell_binary_is_not_a_crash(self):
        with patch.object(ewarecare.subprocess, "run",
                          side_effect=FileNotFoundError("powershell")):
            findings = ewarecare.diagnose_windows_os()
        self.assertEqual(len(findings), 3)
        self.assertEqual({f.status for f in findings}, {"UNAVAILABLE"})


# ---------- non-UTF-8 / UnicodeDecodeError contract (BUG-1) -------------------

class TestNonUTF8DecodeContract(unittest.TestCase):
    """Bad byte soup must degrade, not crash the scan.

    Two failure modes of ``text=True`` without ``errors="replace"``:
    * POSIX: ``subprocess.run`` raises UnicodeDecodeError out of
      ``_run_readonly``, whose docstring promises "Never raises" — the whole
      Linux/macOS section collapses through diagnose_os's blanket except.
    * Windows (observed on this build): the reader thread raises inside
      communicate() and the main thread silently loses the entire stdout, so
      a PASS/UNAVAILABLE verdict is issued on zero evidence.
    """

    def test_decodable_mojibake_flows_through_parsers(self):
        # Already-decoded garbage (what errors="replace" hands over) must not
        # trip any parser.
        garbled = "I/O\ufffd error \ufeff\ufffd\ufffd ata1.00\n"
        findings, _ = run_linux([("dmesg", 0, garbled, ""),
                                 ("df", 0, _LINUX_DF_HEADER, ""),
                                 ("systemctl", 0, "", "")])
        self.assertIn(findings[0].status, ("WARNING", "PASS", "UNAVAILABLE"))

    def test_decode_error_from_command_degrades_to_unavailable(self):
        # The real subprocess.run raises UnicodeDecodeError for undecodable
        # bytes: simulate that failure for dmesg exactly.
        boom = UnicodeDecodeError("utf-8", b"\x81\xff", 0, 2,
                                  "invalid start byte")

        def fake(argv, **kwargs):
            if "dmesg" in " ".join(argv):
                raise boom
            return subprocess.CompletedProcess(argv, 0, _LINUX_DF_HEADER, "")

        with patch.object(ewarecare.subprocess, "run", side_effect=fake):
            findings = ewarecare.diagnose_linux_os()   # BUG-1: must not raise
        by_key = {f.key: f for f in findings}
        self.assertEqual(by_key["OS:KernelLog"].status, "UNAVAILABLE")

    def test_decode_error_from_command_is_reported_not_raised(self):
        # Same event observed at the wrapper level: ok=False + an error text
        # instead of the exception escaping.
        boom = UnicodeDecodeError("utf-8", b"\x81\xff", 0, 2,
                                  "invalid start byte")

        def fake(argv, **kwargs):
            raise boom

        with patch.object(ewarecare.subprocess, "run", side_effect=fake):
            ok, out, err = ewarecare._run_readonly(["dmesg"])  # BUG-1
        self.assertFalse(ok)
        self.assertEqual(out, "")
        self.assertTrue(err.strip())

    def test_real_non_utf8_bytes_are_salvaged_not_lost(self):
        # End-to-end through the REAL command runner: a child of this very
        # interpreter emits bytes invalid in BOTH cp1252 (0x81 is undefined
        # there) and UTF-8 (0xFF cannot start a sequence).
        child = "import sys; sys.stdout.buffer.write(b'OK:\\x81\\xffoops')"
        argv = [sys.executable, "-c", child]
        ok, out, err = ewarecare._run_readonly(argv, timeout=45)  # BUG-1
        self.assertTrue(ok, f"child should exit 0, got error: {err}")
        self.assertTrue(out.strip(),
                        "non-UTF-8 output must still yield text (BUG-1)")
        self.assertIn("OK:", out)
        self.assertIn("\ufffd", out)


# ---------- dispatch contract ---------------------------------------------------

class TestDiagnoseDispatchDegradation(unittest.TestCase):
    def test_dispatch_never_raises_all_unavailable(self):
        with patch.object(ewarecare.subprocess, "run",
                          side_effect=always_timeout):
            findings = ewarecare.diagnose_os("Darwin")
        self.assertEqual(len(findings), 2)
        self.assertEqual({f.status for f in findings}, {"UNAVAILABLE"})


# ---------- safety fence: hard floor under PROTECTED_PROCESS_NAMES (BUG-2) ----

class TestProtectedFloor(unittest.TestCase):
    CRITICAL = (
        "System", "system.exe", "System Idle Process", "Idle",
        "smss.exe", "csrss", "csrss.exe", "wininit.exe", "winlogon.exe",
        "services.exe", "LSASS.EXE", "lsass", "svchost.exe",
        "dwm.exe", "explorer", "registry.exe", "Memory Compression",
        "kernel_task", "launchd", "python.exe", "pythonw",
    )

    def test_floor_survives_emptied_protected_set(self):
        with patch.object(ewarecare, "PROTECTED_PROCESS_NAMES", frozenset()):
            for name in self.CRITICAL:
                self.assertTrue(ewarecare.is_protected_process(name), name)

    def test_floor_survives_replaced_protected_set(self):
        with patch.object(ewarecare, "PROTECTED_PROCESS_NAMES",
                          frozenset({"notepad"})):
            for name in ("csrss.exe", "lsass", "System Idle Process",
                         "winlogon.exe", "kernel_task"):
                self.assertTrue(ewarecare.is_protected_process(name), name)

    def test_normal_processes_remain_killable_with_emptied_set(self):
        with patch.object(ewarecare, "PROTECTED_PROCESS_NAMES", frozenset()):
            self.assertFalse(ewarecare.is_protected_process("chrome.exe"))
            self.assertFalse(ewarecare.is_protected_process("notepad"))

    def test_engine_never_kills_critical_processes_when_set_emptied(self):
        rows = [ewarecare.ProcEntry(pid=700, name="csrss.exe", cpu=99.0),
                ewarecare.ProcEntry(pid=1234, name="chrome.exe", cpu=40.0)]
        engine = ewarecare.RuleEngine(
            on_log=lambda m, t: None,
            executor=lambda fn: fn(),           # synchronous, no real threads
            self_pid=999_999,
            proc_provider=lambda sort_by: rows)
        rule = ewarecare.ActionRule("cpu", 95.0, 60.0, "kill_process")
        engine.rules.append(rule)
        engine.enabled = True
        mock_psutil = MagicMock()
        # The engine re-validates the LIVE process name before terminating
        # (PID-reuse safety); a bare MagicMock would hand back a non-string
        # name, which is_protected_process deliberately refuses, so give the
        # mock the identity the snapshot claimed.
        mock_psutil.Process.return_value.name.return_value = "chrome.exe"
        with patch.object(ewarecare, "HAS_PSUTIL", True), \
             patch.object(ewarecare, "psutil", mock_psutil), \
             patch.object(ewarecare, "PROTECTED_PROCESS_NAMES", frozenset()):
            fired = engine.tick(hot_cpu_snapshot(), 60)
        self.assertEqual(len(fired), 1)
        mock_psutil.Process.assert_called_once_with(1234)
        mock_psutil.Process.return_value.terminate.assert_called_once_with()

    def test_engine_kills_nothing_when_only_critical_candidates(self):
        rows = [ewarecare.ProcEntry(pid=800, name="lsass.exe", cpu=99.0),
                ewarecare.ProcEntry(pid=810, name="System", cpu=98.0),
                ewarecare.ProcEntry(pid=820, name="kernel_task", cpu=97.0)]
        engine = ewarecare.RuleEngine(
            on_log=lambda m, t: None, executor=lambda fn: fn(),
            self_pid=999_999, proc_provider=lambda sort_by: rows)
        engine.rules.append(
            ewarecare.ActionRule("cpu", 95.0, 60.0, "kill_process"))
        engine.enabled = True
        mock_psutil = MagicMock()
        with patch.object(ewarecare, "HAS_PSUTIL", True), \
             patch.object(ewarecare, "psutil", mock_psutil), \
             patch.object(ewarecare, "PROTECTED_PROCESS_NAMES", frozenset()):
            engine.tick(hot_cpu_snapshot(), 60)
        mock_psutil.Process.assert_not_called()


# ---------- ActionRule duration validation (BUG-3) ----------------------------

class TestRuleDurationValidation(unittest.TestCase):
    def test_zero_duration_is_rejected(self):
        # A 0 s window means "fire on the first sample": the sustained-breach
        # design ("fires only after the FULL duration") would be void, and an
        # accidental 0 turns kill_process into a one-sample kill.
        with self.assertRaises(ValueError):                 # BUG-3
            ewarecare.ActionRule("cpu", 95.0, 0.0, "kill_process")
        with self.assertRaises(ValueError):
            ewarecare.ActionRule("mem", 90.0, 0, "alert_only")

    def test_negative_duration_is_rejected(self):
        with self.assertRaises(ValueError):                 # BUG-3
            ewarecare.ActionRule("cpu", 95.0, -1.0, "clean_temp")
        with self.assertRaises(ValueError):
            ewarecare.ActionRule("disk", 90.0, -60, "alert_only")

    def test_tiny_positive_duration_still_valid(self):
        rule = ewarecare.ActionRule("cpu", 95.0, 0.5, "alert_only")
        self.assertEqual(rule.duration_s, 0.5)

    def test_gui_range_and_existing_fixtures_still_constructible(self):
        for seconds in (0.5, 1.0, 5.0, 120.0, 86400.0):
            rule = ewarecare.ActionRule(
                "cpu", 95.0, seconds,
                "alert_only" if seconds < 5 else "kill_process")
            self.assertEqual(rule.duration_s, seconds)


if __name__ == "__main__":
    unittest.main()
