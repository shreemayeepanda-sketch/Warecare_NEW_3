"""Tests for the Auto-Action Rules engine ("The Kill Switch").

Focus (as specified):
* a rule fires only after the FULL duration of sustained breach;
* the breach timer resets the moment the metric drops back below;
* critical system processes are NEVER terminated (mocked terminate);
* the Master Switch blocks everything while it is off.
"""
from __future__ import annotations

import contextlib
import unittest
from unittest.mock import MagicMock, patch

from scanner import actions as A
from scanner.actions import ActionRule, RuleEngine, is_protected_process
from scanner.hardware import (CpuSnapshot, DiskEntry, HardwareSnapshot,
                              MemSnapshot, ProcEntry)


# ---------- fixtures ----------

def make_snapshot(cpu: float | None = 50.0, mem: float | None = 50.0,
                  disks: tuple | None = (50.0, 20.0)) -> HardwareSnapshot:
    """A real HardwareSnapshot with controllable percentage values."""
    snap = HardwareSnapshot()
    snap.cpu = CpuSnapshot(available=cpu is not None, percent=cpu)
    snap.mem = MemSnapshot(available=mem is not None, percent=mem)
    if disks is not None:
        snap.disks = [
            DiskEntry(mountpoint=f"/{letter}", accessible=True, percent=value)
            for letter, value in enumerate(disks, start=1)
        ]
    return snap


def proc(pid: int, name: str, cpu: float = 10.0, mem_mb: float = 10.0) -> ProcEntry:
    return ProcEntry(pid=pid, name=name, cpu=cpu, mem_mb=mem_mb)


class RecordingEngine(RuleEngine):
    """RuleEngine that logs into a list and runs actions synchronously."""

    def __init__(self, **kwargs) -> None:
        self.messages: list[tuple[str, str]] = []
        kwargs.setdefault("on_log",
                          lambda message, tag: self.messages.append((message, tag)))
        kwargs.setdefault("executor", lambda fn: fn())  # no real threads in tests
        kwargs.setdefault("self_pid", 999_999)
        super().__init__(**kwargs)

    def tagged(self, tag: str) -> list[str]:
        return [m for m, t in self.messages if t == tag]

    def all_text(self) -> str:
        return " | ".join(m for m, _ in self.messages)


def cpu_rule(threshold: float = 95.0, duration_s: float = 120.0) -> ActionRule:
    return ActionRule("cpu", threshold, duration_s, "kill_process")


@contextlib.contextmanager
def with_psutil_mocked():
    """Pretend psutil is present, but hand back a MagicMock to assert on."""
    mock_psutil = MagicMock()
    with patch.object(A, "HAS_PSUTIL", True), patch.object(A, "psutil", mock_psutil):
        yield mock_psutil


# ---------- check()/timer semantics ----------

class TestRuleCheck(unittest.TestCase):
    def test_fires_only_after_full_duration(self):
        rule = cpu_rule(95.0, 120.0)
        hot = make_snapshot(cpu=97.0)
        self.assertFalse(rule.check(hot, 0))
        self.assertFalse(rule.check(hot, 119.9))
        self.assertTrue(rule.check(hot, 120.0))
        self.assertTrue(rule.check(hot, 500))
        # Just below the threshold: even ten hours must not fire.
        cool = make_snapshot(cpu=94.9)
        self.assertFalse(rule.check(cool, 36_000))

    def test_unavailable_metric_never_fires(self):
        rule = cpu_rule(95.0, 10.0)
        self.assertIsNone(rule.value(make_snapshot(cpu=None)))
        self.assertFalse(rule.check(make_snapshot(cpu=None), 60))

    def test_disk_metric_watches_worst_partition(self):
        rule = ActionRule("disk", 90.0, 60.0, "alert_only")
        self.assertGreater(rule.value(make_snapshot(disks=(50.0, 93.0))), 92.0)
        self.assertFalse(rule.exceeded(make_snapshot(disks=(50.0, 89.0))))
        self.assertTrue(rule.exceeded(make_snapshot(disks=(50.0, 90.0))))

    def test_unknown_metric_or_action_rejected(self):
        with self.assertRaises(ValueError):
            ActionRule("gpu", 90, 60, "alert_only")
        with self.assertRaises(ValueError):
            ActionRule("cpu", 90, 60, "reboot_machine")


class TestBreachTimer(unittest.TestCase):
    def test_accumulates_and_fires_after_duration(self):
        engine = RecordingEngine()
        rule = cpu_rule(95.0, 120.0)
        engine.rules.append(rule)
        engine.enabled = True
        hot = make_snapshot(cpu=97.0)

        self.assertEqual(engine.tick(hot, 60), [])  # 60s accumulated
        self.assertEqual(rule.breach_s, 60.0)

        fired = engine.tick(hot, 60)                # 120s → fires now
        self.assertIn(rule, fired)
        self.assertEqual(rule.times_fired, 1)
        self.assertEqual(rule.breach_s, 0.0)        # timer resets after firing

    def test_timer_resets_when_metric_drops_back(self):
        engine = RecordingEngine()
        rule = cpu_rule(95.0, 120.0)
        engine.rules.append(rule)
        engine.enabled = True
        hot = make_snapshot(cpu=97.0)
        cool = make_snapshot(cpu=50.0)

        engine.tick(hot, 60)
        engine.tick(cool, 60)              # below the limit → timer resets
        self.assertEqual(rule.breach_s, 0.0)

        # A fresh breach needs a NEW full window: 60 + 59.9s is not enough.
        engine.tick(hot, 60)
        self.assertEqual(engine.tick(hot, 59.9), [])
        self.assertEqual(rule.times_fired, 0)

        # One more second completes a fresh 120s window → second fire.
        fired = engine.tick(hot, 1)
        self.assertIn(rule, fired)
        self.assertEqual(rule.times_fired, 1)

    def test_master_switch_blocks_everything(self):
        engine = RecordingEngine()
        rule = cpu_rule(95.0, 10.0)
        engine.rules.append(rule)
        hot = make_snapshot(cpu=99.0)

        self.assertFalse(engine.enabled)
        self.assertEqual(engine.tick(hot, 60), [])
        self.assertEqual(rule.breach_s, 0.0, "timer must not run while disabled")

        engine.enabled = True
        self.assertIn(rule, engine.tick(hot, 60))

    def test_no_rules_is_a_noop(self):
        engine = RecordingEngine()
        self.assertEqual(engine.tick(make_snapshot(cpu=99.0), 60), [])

    def test_missing_snapshot_does_not_crash(self):
        engine = RecordingEngine()
        rule = cpu_rule(0.001, 0.001)
        engine.rules.append(rule)
        engine.enabled = True
        self.assertEqual(engine.tick(None, 60), [])
        self.assertEqual(rule.times_fired, 0)


# ---------- kill_process safety ----------

class TestKillProcess(unittest.TestCase):
    def _engine(self, rows) -> RecordingEngine:
        return RecordingEngine(proc_provider=lambda sort_by: rows)

    def test_kills_highest_cpu_eligible_process(self):
        rows = [proc(456, "explorer.exe", cpu=20.0),
                proc(123, "chrome.exe", cpu=99.0),
                proc(789, "notepad.exe", cpu=40.0)]
        engine = self._engine(rows)
        engine.rules.append(cpu_rule(95.0, 60.0))
        engine.enabled = True
        with with_psutil_mocked() as psutil_mock:
            fired = engine.tick(make_snapshot(cpu=97.0), 60)
        self.assertEqual(len(fired), 1)
        psutil_mock.Process.assert_called_once_with(123)
        psutil_mock.Process.return_value.terminate.assert_called_once_with()
        self.assertEqual(engine.tagged("FAIL"),
                         ["Killed chrome.exe (PID 123) because CPU was >95% for 60s."])

    def test_critical_processes_are_never_terminated(self):
        # ALL candidates are protected: nothing may be terminated at all.
        rows = [proc(1, "System Idle Process", cpu=100.0),
                proc(4, "System", cpu=99.0),
                proc(700, "csrss.exe", cpu=98.0),
                proc(800, "explorer.exe", cpu=97.0),
                proc(900, "wininit.exe", cpu=96.0),
                proc(950, "MEMORY COMPRESSION", cpu=95.0)]
        engine = self._engine(rows)
        engine.rules.append(cpu_rule(95.0, 60.0))
        engine.enabled = True
        with with_psutil_mocked() as psutil_mock:
            fired = engine.tick(make_snapshot(cpu=97.0), 60)
        self.assertEqual(len(fired), 1)  # the rule fired…
        psutil_mock.Process.assert_not_called()  # …but killed NOBODY
        self.assertIn("nothing was terminated", engine.all_text())

    def test_skips_protected_and_kills_next_candidate(self):
        rows = [proc(800, "explorer.exe", cpu=98.0),      # protected shell
                proc(1234, "chrome.exe", cpu=95.5),       # top eligible
                proc(700, "csrss.exe", cpu=60.0)]         # protected session
        engine = self._engine(rows)
        engine.rules.append(cpu_rule(95.0, 60.0))
        engine.enabled = True
        with with_psutil_mocked() as psutil_mock:
            engine.tick(make_snapshot(cpu=97.0), 60)
        psutil_mock.Process.assert_called_once_with(1234)
        psutil_mock.Process.return_value.terminate.assert_called_once_with()

    def test_never_kills_the_app_itself(self):
        rows = [proc(999_999, "chrome.exe", cpu=99.0)]    # the engine's own PID
        engine = self._engine(rows)
        engine.rules.append(cpu_rule(95.0, 60.0))
        engine.enabled = True
        with with_psutil_mocked() as psutil_mock:
            engine.tick(make_snapshot(cpu=97.0), 60)
        psutil_mock.Process.assert_not_called()
        self.assertIn("nothing was terminated", engine.all_text())

    def test_never_kills_kernel_pids(self):
        rows = [proc(0, "invoice-worker", cpu=99.0),
                proc(4, "invoice-worker", cpu=98.0)]
        engine = self._engine(rows)
        engine.rules.append(cpu_rule(95.0, 60.0))
        engine.enabled = True
        with with_psutil_mocked() as psutil_mock:
            engine.tick(make_snapshot(cpu=97.0), 60)
        psutil_mock.Process.assert_not_called()

    def test_ram_rule_asks_provider_for_memory_sort(self):
        seen: list[str] = []

        def provider(sort_by: str) -> list:
            seen.append(sort_by)
            return []

        engine = RecordingEngine(proc_provider=provider)
        engine.rules.append(ActionRule("mem", 95.0, 60.0, "kill_process"))
        engine.enabled = True
        with with_psutil_mocked():
            engine.tick(make_snapshot(mem=97.0), 60)
        self.assertEqual(seen[:1], ["mem"])

    def test_kill_without_psutil_is_refused_loudly(self):
        engine = self._engine([proc(123, "chrome.exe", cpu=99.0)])
        engine.rules.append(cpu_rule(95.0, 60.0))
        engine.enabled = True
        with patch.object(A, "HAS_PSUTIL", False):
            fired = engine.tick(make_snapshot(cpu=97.0), 60)
        self.assertEqual(len(fired), 1)
        self.assertIn("psutil is unavailable", engine.all_text())
        self.assertTrue(engine.tagged("WARNING"), "refusal must be logged")

    def test_dead_pid_terminate_errors_are_reported_not_raised(self):
        broken = MagicMock()
        broken.Process.side_effect = Exception("AccessDenied")  # e.g. dead PID
        engine = self._engine([proc(123, "chrome.exe", cpu=99.0)])
        engine.rules.append(cpu_rule(95.0, 60.0))
        engine.enabled = True
        with patch.multiple(A, HAS_PSUTIL=True, psutil=broken):
            fired = engine.tick(make_snapshot(cpu=97.0), 60)
        self.assertEqual(len(fired), 1)
        self.assertIn("refused", engine.all_text())
        self.assertEqual(len(engine.tagged("WARNING")), 1)


class TestProtectedNames(unittest.TestCase):
    def test_critical_system_processes(self):
        for name in ("System", "system.exe", "Idle", "System Idle Process",
                     "csrss.exe", "CSRSS", "wininit.exe", "winlogon.exe",
                     "services.exe", "lsass.exe", "smss.exe", "svchost.exe",
                     "dwm.exe", "explorer.exe", "Registry", "registry",
                     "Memory Compression", "taskmgr.exe", "python.exe"):
            self.assertTrue(is_protected_process(name), name)

    def test_normal_user_processes(self):
        for name in ("chrome.exe", "chrome", "Code.exe", "notepad.exe",
                     "shds-scan-worker", "some_game.exe"):
            self.assertFalse(is_protected_process(name), name)

    def test_empty_or_missing_names_are_treated_as_unsafe(self):
        self.assertTrue(is_protected_process(""))
        self.assertTrue(is_protected_process(None))
        self.assertTrue(is_protected_process("   "))


class TestAlertOnlyAndCleanTemp(unittest.TestCase):
    def test_alert_only_reports_without_killing(self):
        # No proc provider: a stray kill attempt would raise loudly in tests.
        engine = RecordingEngine()
        engine.rules.append(ActionRule("cpu", 90.0, 60.0, "alert_only"))
        engine.enabled = True
        fired = engine.tick(make_snapshot(cpu=95.0), 60)
        self.assertEqual(len(fired), 1)
        text = engine.all_text()
        self.assertIn("stayed above 90%", text)
        self.assertIn("alert-only", text)
        self.assertTrue(all(tag == "WARNING" for _, tag in engine.messages))

    def test_clean_temp_runs_injected_handler(self):
        calls: list[int] = []
        engine = RecordingEngine(
            clean_action=lambda: calls.append(1) or "removed 3 files")
        engine.rules.append(ActionRule("cpu", 90.0, 60.0, "clean_temp"))
        engine.enabled = True
        fired = engine.tick(make_snapshot(cpu=95.0), 60)
        self.assertEqual(len(fired), 1)
        self.assertEqual(calls, [1])
        self.assertIn("Auto-action (safe temp cleanup)", engine.all_text())
        self.assertIn("removed 3 files", engine.all_text())

    def test_clean_temp_without_handler_deletes_nothing(self):
        engine = RecordingEngine()  # clean_action=None
        engine.rules.append(ActionRule("cpu", 90.0, 60.0, "clean_temp"))
        engine.enabled = True
        fired = engine.tick(make_snapshot(cpu=95.0), 60)
        self.assertEqual(len(fired), 1)
        self.assertIn("no cleanup handler", engine.all_text())

    def test_failing_action_is_contained(self):
        def boom() -> str:
            raise RuntimeError("bank accounts crashed")

        engine = RecordingEngine(clean_action=boom)
        engine.rules.append(ActionRule("cpu", 90.0, 60.0, "clean_temp"))
        engine.enabled = True
        fired = engine.tick(make_snapshot(cpu=95.0), 60)
        self.assertEqual(len(fired), 1)
        self.assertIn("failed", engine.all_text())

    def test_description_is_human_readable(self):
        rule = ActionRule("mem", 95, 120, "kill_process")
        self.assertEqual(rule.description(),
                         "RAM > 95% for 120s → Kill Top Process")


class TestThreadSafetyOfDispatch(unittest.TestCase):
    def test_actions_are_dispatched_to_executor_not_run_inline(self):
        engine = RecordingEngine()
        submitted: list = []
        engine._executor = lambda fn: submitted.append(fn)
        rule = cpu_rule(95.0, 60.0)
        engine.rules.append(rule)
        engine.enabled = True
        fired = engine.tick(make_snapshot(cpu=97.0), 60)
        self.assertEqual(len(fired), 1)
        self.assertEqual(len(submitted), 1)   # one job handed off to a worker
        # The caller (GUI thread) is free immediately: timer already reset.
        self.assertEqual(rule.breach_s, 0.0)


if __name__ == "__main__":
    unittest.main()
