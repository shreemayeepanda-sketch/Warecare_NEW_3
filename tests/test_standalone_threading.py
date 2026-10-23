"""Agent 1 (Threading & Runtime Auditor) tests for the standalone build.

Target module: ``E:/Warecare/ewarecare.py`` (standalone flattened build).
Scope: thread/queue plumbing races around ``MonitorWorker``, ``RuleEngine``
(the kill switch), the ``ScannerApp`` background workers and the shutdown path.

Bug map (numbering from the historical threading audit, Agent 1):

* Bug 1 (HIGH)    ``MonitorWorker._loop`` reads ``self._stop`` by attribute;
  ``start()`` swaps that event between runs, so an old sampler thread that is
  mid-sample never sees its own stop event -> two sampler loops run at once.
  -> test_02 (RED pre-fix); test_03 guards that the fix keeps restart working.
* Bug 2 (HIGH)    kill switch has no in-flight guard: while an action thread
  is still running (slow victim lookup) a fully re-armed window re-dispatches
  the SAME rule -> same process can be terminated twice.
  -> test_04, test_05 (RED pre-fix).
* Bug 3 (MEDIUM)  ``refresh_processes`` only catches ``(RuntimeError,
  OSError)`` from ``Thread.start``; any other failure wedges ``_procs_active``
  True forever -> process table silently freezes.
  -> test_06 (RED pre-fix); test_07 normal-completion regression.
* Bug 4 (HIGH)    ``on_closing`` shutdown hygiene: uses ``request_stop`` (no
  stop/join of the worker, ``monitor.running`` stays True), never disarms the
  RuleEngine, never stops CustomTkinter's internal scan-bar ``after`` loop,
  leaves ``monitor_running`` True. -> tests 90/91 (RED pre-fix).
* Bug 5 (MEDIUM)  ``ActionRule.value`` raises AttributeError on a partial
  snapshot, breaking ``RuleEngine.tick``'s documented "never raises" contract.
  -> test_08 (RED pre-fix).
* Bug 6 (LOW)     ``_reschedule``'s ``_run`` has no TclError guard (test_92
  documents the raw-destroy path; currently not reproducible).

Safety: nothing here kills real processes, writes the registry or touches the
network. ``collect_snapshot`` / ``collect_processes`` / ``run_diagnostics`` /
``build_report_dict`` are monkeypatched; engine actions use a recording
executor or a patched ``_kill_top_process``. The GUI tests drive the real
``ScannerApp`` with ``update()`` pumps only -- never ``mainloop()``.
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from types import SimpleNamespace

# --------------------------------------------------------------------------
# Bootstrap: import the standalone module from its file path.
# --------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[1]
# Default target is the standalone build; an env override exists only so a
# reviewer can run this suite against a *patched copy* of the module (_AGENT
# validation harness_) without touching the shipped file.
MODULE_PATH = Path(os.environ.get("EW_MODULE_PATH",
                                  str(REPO_ROOT / "ewarecare.py")))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Record (without executing) any GUI launch during import: importing the
# module must not start a main loop thanks to its __main__ guard.
_boot_record: list[str] = []

_real_tk_mainloop = tk.Tk.mainloop


def _recording_tk_mainloop(self, *args, **kwargs):  # pragma: no cover - guard
    _boot_record.append("tk.Tk.mainloop")
    return None


_real_ctk_mainloop = None
try:
    import customtkinter as ctk

    if "mainloop" in ctk.CTk.__dict__:
        _real_ctk_mainloop = ctk.CTk.mainloop

        def _recording_ctk_mainloop(self, *args, **kwargs):  # noqa: E306
            _boot_record.append("ctk.CTk.mainloop")
            return None
except Exception:  # pragma: no cover - customtkinter is a test dependency
    ctk = None

tk.Tk.mainloop = _recording_tk_mainloop
if _real_ctk_mainloop is not None:
    ctk.CTk.mainloop = _recording_ctk_mainloop
try:
    _spec = importlib.util.spec_from_file_location("ewarecare_standalone",
                                                   str(MODULE_PATH))
    assert _spec is not None and _spec.loader is not None
    ew = importlib.util.module_from_spec(_spec)
    sys.modules["ewarecare_standalone"] = ew
    _spec.loader.exec_module(ew)
finally:
    tk.Tk.mainloop = _real_tk_mainloop
    if _real_ctk_mainloop is not None:
        ctk.CTk.mainloop = _real_ctk_mainloop


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def make_snap(cpu: float = 50.0, mem: float = 40.0) -> "ew.HardwareSnapshot":
    """A REAL HardwareSnapshot (the worker queue is filtered with isinstance)
    built from the standalone module's own dataclasses. No psutil involved."""
    return ew.HardwareSnapshot(
        cpu=ew.CpuSnapshot(available=True, percent=cpu, logical_cores=8,
                           physical_cores=4, freq_current_mhz=3200.0),
        mem=ew.MemSnapshot(available=True, total=16 * 2**30, used=8 * 2**30,
                           available_bytes=8 * 2**30, percent=mem),
        disks=[ew.DiskEntry(device="C:", mountpoint="C:\\", fstype="NTFS",
                            total=10**12, used=10**11, free=9 * 10**11,
                            percent=12.0, accessible=True)],
        net=ew.NetSnapshot(available=True, interfaces=["lo0"],
                           up_bps=1000.0, down_bps=2000.0),
        temps=[],
        gpu=ew.GpuStatus(supported=False, message="stub"),
        hostname="unittest-host",
        os_info="UnitTest 1.0",
        timestamp=time.time(),
    )


def fake_proc(pid: int = 424242, name: str = "fakevictim-test.exe") -> SimpleNamespace:
    """ProcEntry-shaped fake (eligible kill candidate shape, never used)."""
    return SimpleNamespace(pid=pid, name=name, cpu=90.0, mem_mb=128.0)


def wait_until(predicate, timeout: float = 5.0, step: float = 0.02,
               pump=None) -> bool:
    """Bounded wait; returns False on timeout instead of hanging forever."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        if pump is not None:
            pump()
        time.sleep(step)
    return predicate()


def pump(app, seconds: float, step: float = 0.04) -> list[str]:
    """Drive the Tk event loop with update() for ~seconds (never mainloop).
    Returns Tcl errors raised by update() itself (post-destroy idle pumps are
    expected to raise "application has been destroyed")."""
    errors: list[str] = []
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            app.update()
        except tk.TclError as exc:
            errors.append(str(exc))
        time.sleep(step)
    return errors


def _install_recorder(app) -> list:
    """Capture exceptions that escape Tk callbacks (report_callback_exception)."""
    recorded: list = []

    def _recorder(exc, val, tb):  # tkinter calls with 3 args
        recorded.append((exc, val))

    app.report_callback_exception = _recorder
    return recorded


# --------------------------------------------------------------------------
# 0x - Unit tests (no Tk windows)
# --------------------------------------------------------------------------
def test_01_import_has_main_guard_and_never_launches_gui():
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert 'if __name__ == "__main__":' in source
    assert "raise SystemExit(main())" in source
    # The import at file load time recorded no mainloop call.
    assert _boot_record == [], _boot_record
    assert hasattr(ew, "main") and callable(ew.main)
    assert hasattr(ew, "ScannerApp") and hasattr(ew, "MonitorWorker")
    assert hasattr(ew, "RuleEngine") and hasattr(ew, "ActionRule")


def test_02_monitor_worker_stop_start_race_leaves_no_immortal_thread(monkeypatch):
    """Bug 1: the old sampler must exit on its OWN stop event even when a new
    run is started before it finishes its final sample. RED pre-fix (the old
    thread reads the *current* self._stop attribute and survives forever)."""
    lock = threading.Lock()
    state = {"calls": 0}
    first_sample_started = threading.Event()
    release = threading.Event()

    def fake_collect(include_gpu=False):
        with lock:
            state["calls"] += 1
        if state["calls"] == 1:
            first_sample_started.set()
            release.wait(5.0)  # worker pinned INSIDE its first sample
        else:
            time.sleep(0.02)
        return make_snap(cpu=12.5, mem=33.0)

    monkeypatch.setattr(ew, "collect_snapshot", fake_collect)

    w = ew.MonitorWorker(interval_s=0.5)
    t1 = t2 = None
    try:
        assert w.start() is True
        t1 = w._thread
        assert t1 is not None
        assert wait_until(first_sample_started.is_set, 3.0), "T1 never sampled"
        assert not w._stop.is_set()

        # Stop while T1 is mid-sample, then immediately start a new run.
        w.stop(timeout=0.05)
        assert w.running is False
        assert w._thread is None
        assert w._stop.is_set(), "the OLD run's stop event must be set"
        assert w.start() is True  # arms a fresh stop event + new thread
        t2 = w._thread
        assert t2 is not None and t2 is not t1

        # The new run must actually sample.
        assert wait_until(lambda: state["calls"] >= 3, 3.0), "new run dead"

        # Release T1's frozen sample. T1 must exit on its own (old) event.
        release.set()
        t1.join(timeout=3.0)
        assert not t1.is_alive(), (
            "old sampler thread ignored its own stop event and keeps running "
            "alongside the new one (stale self._stop attribute read)")
    finally:
        release.set()
        w.stop(timeout=3.0)
        with contextlib.suppress(Exception):
            if t1 is not None:
                t1.join(timeout=3.0)
            if t2 is not None:
                t2.join(timeout=3.0)
        w.poll()  # drain


def test_03_monitor_worker_restart_still_samples(monkeypatch):
    """Companion to Bug 1: the fresh-stop-event design must not be 'fixed' by
    breaking restarts. GREEN pre- and post-fix."""
    counter = {"n": 0}
    lock = threading.Lock()

    def fake_collect(include_gpu=False):
        with lock:
            counter["n"] += 1
        return make_snap(cpu=5.0, mem=5.0)

    monkeypatch.setattr(ew, "collect_snapshot", fake_collect)

    w = ew.MonitorWorker(interval_s=0.5)
    try:
        assert w.start() is True
        assert wait_until(lambda: counter["n"] >= 1, 3.0)
        first = counter["n"]
        w.stop(timeout=3.0)
        assert w.start() is True
        assert wait_until(lambda: counter["n"] >= first + 1, 3.0), (
            "a fresh run must sample independently of the finished old run")
    finally:
        w.stop(timeout=3.0)
        w.poll()


def test_04_kill_switch_no_double_dispatch_while_action_in_flight():
    """Bug 2: while an action is still pending (executor never runs it), the
    re-armed window must NOT dispatch the same rule again. RED pre-fix: the
    engine happily queues 6 overlapping kill actions."""
    submitted: list = []
    logged: list[tuple[str, str]] = []

    engine = ew.RuleEngine(on_log=lambda m, t: logged.append((m, t)),
                           executor=submitted.append)
    rule = ew.ActionRule("cpu", 10.0, 2.0, "kill_process")
    engine.rules = [rule]
    engine.enabled = True
    snap = make_snap(cpu=95.0)

    for _ in range(12):  # 12 sustained ticks (dt_s=1)
        engine.tick(snap, 1.0)

    assert len(submitted) == 1, (
        f"exact one in-flight action allowed, got {len(submitted)} "
        "dispatches for the same rule")
    assert rule.times_fired == 1, rule.times_fired
    assert any("still running" in msg for msg, _ in logged), logged


def test_05_kill_switch_refire_allowed_after_action_completes(monkeypatch):
    """Bug 2, completion path: the in-flight guard must clear when the action
    finishes, and a *new full window* must be able to fire again. Also proves
    no second dispatch happens while the first action is blocked mid-flight."""
    gate = threading.Event()
    inside_kill = threading.Event()
    submitted: list = []
    logged: list[tuple[str, str]] = []

    engine = ew.RuleEngine(on_log=lambda m, t: logged.append((m, t)),
                           executor=submitted.append)
    rule = ew.ActionRule("cpu", 10.0, 2.0, "kill_process")
    engine.rules = [rule]
    engine.enabled = True
    snap = make_snap(cpu=95.0)

    # Never touch real psutil: stub the victim selection/terminate step.
    def fake_kill(self, rule_, snapshot):
        inside_kill.set()  # signal: the action thread is inside the kill
        gate.wait(5.0)
        return ("fake kill done", "FAIL")

    monkeypatch.setattr(ew.RuleEngine, "_kill_top_process", fake_kill)

    engine.tick(snap, 1.0)                      # breach 1s
    assert engine.tick(snap, 1.0) == [rule]     # breach 2s -> fired
    assert len(submitted) == 1

    runner = threading.Thread(target=submitted.pop(0), name="shds-action-test",
                              daemon=True)
    runner.start()
    try:
        assert wait_until(inside_kill.is_set, 5.0), "fake kill never started"
        # While the action is blocked mid-kill, 6 more breached ticks must
        # NOT dispatch a second concurrent action.
        for _ in range(6):
            engine.tick(snap, 1.0)
        assert len(submitted) == 0, (
            f"no double-fire while an action is running; got {len(submitted)} "
            "extra dispatches")
        assert any("still running" in msg for msg, _ in logged)
    finally:
        gate.set()
        runner.join(timeout=5.0)
    assert not runner.is_alive()
    assert not rule.in_flight.is_set(), "in-flight flag must clear on completion"

    submitted_snap = engine.tick(snap, 1.0)     # new window: 1s, not yet
    assert submitted_snap == []
    fired = engine.tick(snap, 1.0)              # 2s -> allowed again
    assert fired == [rule]
    assert len(submitted) == 1
    assert rule.times_fired == 2


class _FakeProcApp:
    """Minimal stand-in exposing exactly what refresh_processes needs."""

    def __init__(self) -> None:
        self._procs_active = False
        self._bg_queue: queue.Queue = queue.Queue()
        self.proc_sort = SimpleNamespace(get=lambda: "cpu")

    refresh_processes = ew.ScannerApp.refresh_processes
    _proc_job = ew.ScannerApp._proc_job


def test_06_procs_active_cleared_when_thread_start_fails_unexpectedly(monkeypatch):
    """Bug 3: Thread.start can raise beyond (RuntimeError, OSError); the guard
    flag must STILL be cleared or the process table freezes forever. RED
    pre-fix (flag stays True + exception propagates)."""
    state = {"raised": False}

    class _BoomThread(threading.Thread):
        def start(self):
            state["raised"] = True
            raise RuntimeError("simulated thread-limit") if False else ValueError(
                "simulated resource exhaustion")

    monkeypatch.setattr(ew.threading, "Thread", _BoomThread)

    app = _FakeProcApp()
    with contextlib.suppress(Exception):  # start failure handling varies
        app.refresh_processes()

    assert state["raised"], "the simulated start() failure never ran"
    assert app._procs_active is False, (
        "_procs_active wedged True after a failed thread start; the Processes "
        "view would silently stop updating for the rest of the session")


def test_07_procs_active_cleared_after_normal_worker_cycle(monkeypatch):
    """Regression: a successful (fake) collect passes through the queue and
    clears the flag in the worker's finally. GREEN pre- and post-fix."""
    rows = [fake_proc()]
    monkeypatch.setattr(ew, "collect_processes", lambda limit=12, sort_by="cpu":
                        list(rows))
    app = _FakeProcApp()
    app.refresh_processes()
    item = app._bg_queue.get(timeout=5.0)
    assert item[0] == "procs" and item[1] == rows
    assert wait_until(lambda: not app._procs_active, 5.0)


def test_08_rule_engine_tick_survives_broken_snapshots():
    """Bug 5: RuleEngine.tick's contract (its docstring) is "Never raises: a
    broken snapshot deactivates the rules for that tick only" — but a partial
    snapshot object (no `.cpu`/`.mem` sections) makes ActionRule.value raise
    AttributeError out of tick. RED pre-fix. None input must stay tolerated."""
    engine = ew.RuleEngine(on_log=lambda m, t: None)
    rule = ew.ActionRule("cpu", 10.0, 5.0, "alert_only")
    engine.rules = [rule]
    engine.enabled = True
    assert engine.tick(None, 1.0) == []
    assert engine.tick(object(), 1.0) == []
    assert engine.tick(SimpleNamespace(cpu=None, mem=None, disks=[]), 1.0) == []
    assert rule.breach_s == 0.0


# --------------------------------------------------------------------------
# 9x - Integration tests on the real ScannerApp (update() pumps, NO mainloop)
# --------------------------------------------------------------------------
def test_90_gui_close_stops_monitor_disarms_engine_and_leaks_nothing(
        monkeypatch):
    """Integration: start monitoring with a fake sample source, arm an
    alert-only rule, let it fire through the real poll loop, then close via
    on_closing(). Asserts post-fix shutdown contract (Bug 4) plus armor:

    * no TclError escapes any Tk callback (recorder installed),
    * worker stop event set and the thread joins within a timeout,
    * monitor.running / monitor_running cleared,  (RED pre-fix, Bug 4)
    * RuleEngine disarmed after close,            (RED pre-fix, Bug 4)
    * _after_ids cancelled, _procs_active not stuck True,
    * no shds-monitor thread left, no new non-daemon threads.
    """
    monkeypatch.setattr(ew, "collect_snapshot",
                        lambda include_gpu=True: make_snap(cpu=95.0, mem=95.0))
    rows = [fake_proc()]
    monkeypatch.setattr(ew, "collect_processes", lambda limit=12, sort_by="cpu":
                        list(rows))

    app = ew.ScannerApp()
    recorded_cb = _install_recorder(app)
    engine = app.actions_engine
    closed = {"flag": False}
    try:
        err = pump(app, 0.3)
        assert not err, f"TclError while pumping the live app: {err[:2]}"

        app.actions_engine.rules.clear()
        app.actions_engine.rules.append(ew.ActionRule("cpu", 50.0, 1.0,
                                                      "alert_only"))
        app.actions_engine.enabled = True

        app.toggle_monitoring()
        assert app.monitor_running is True
        assert app.monitor.running is True
        worker = app.monitor._thread
        assert worker is not None

        # The rule must fire through the genuine after-loop poll chain.
        fired = wait_until(lambda: engine.rules[0].times_fired >= 1, 12.0,
                           pump=lambda: pump(app, 0.05))
        assert fired, "alert_only rule never fired through _poll_monitor"

        stop_event = app.monitor._stop
        assert stop_event is not None and not stop_event.is_set()
        assert not recorded_cb, f"callback errors while live: {recorded_cb[:2]}"

        pre_names = {t.name for t in threading.enumerate()}
        app.on_closing()
        closed["flag"] = True

        # Post-close pumping must produce no fresh callback errors.
        post_errs = pump(app, 0.8)
        expected = tuple(e for e in post_errs
                         if "application has been destroyed" not in e)
        assert not expected, f"unexpected post-destroy TclErrors: {expected[:2]}"
        assert not recorded_cb, (
            f"TclError escaped Tk callbacks during close: {recorded_cb[:2]}")

        # Worker shutdown contract.
        assert stop_event.is_set(), "worker stop event must be set by close"
        worker.join(timeout=6.0)
        assert not worker.is_alive(), "monitor worker not joined within 6s"

        # --- RED pre-fix (Bug 4): state must be fully stopped/disarmed ---
        assert app.monitor.running is False, (
            "monitor.running stays True after close (request_stop never "
            "clears it)")
        assert app.monitor_running is False, (
            "monitor_running stays True after close; UI would claim live "
            "monitoring forever if the app were re-shown")
        assert engine.enabled is False, (
            "kill switch still armed after close; auto-actions could fire "
            "after the window is gone")

        # After-loop plumbing fully cancelled.
        assert app._after_ids == set(), app._after_ids

        # The process guard flag must not be stuck True.
        assert wait_until(lambda: not app._procs_active, 3.0), (
            "_procs_active stuck True after close")

        # No orphan sampler thread, no new non-daemon threads.
        assert wait_until(
            lambda: not any(t.name == "shds-monitor"
                            for t in threading.enumerate()), 4.0), (
            "shds-monitor thread still alive after close")
        new_threads = [t for t in threading.enumerate()
                       if t.name not in pre_names and t.name != "MainThread"]
        bad = [t.name for t in new_threads if not t.is_daemon()]
        assert not bad, f"non-daemon threads created by the app: {bad}"
    finally:
        if not closed["flag"]:
            with contextlib.suppress(Exception):
                app.on_closing()
        with contextlib.suppress(Exception):
            pump(app, 0.2)


def test_91_gui_close_cancels_ctk_scan_animation_loop(monkeypatch):
    """Bug 4d: run_scan() starts CustomTkinter's internal 20 ms after-loop on
    the scan bar; on_closing() must stop it. RED pre-fix (loop keeps running
    and spams 'invalid command name' after destroy in a frozen build)."""
    release_scan = threading.Event()
    monkeypatch.setattr(ew, "collect_snapshot",
                        lambda include_gpu=True: release_scan.wait(10.0)
                        or make_snap(cpu=10.0, mem=10.0))
    monkeypatch.setattr(ew, "run_diagnostics",
                        lambda snap, thresholds=None, **k: SimpleNamespace(
                            findings=[], summary={}, recommendations=[],
                            duration_s=0.1, finished_at=0))
    monkeypatch.setattr(ew, "build_report_dict",
                        lambda snap, diag, thresholds: {})

    app = ew.ScannerApp()
    recorded_cb = _install_recorder(app)
    closed = {"flag": False}
    try:
        err = pump(app, 0.2)
        assert not err, err[:2]
        app.run_scan()  # scan_bar.start() -> CTk internal after loop
        assert app._scan_active is True
        assert wait_until(
            lambda: getattr(app.scan_bar, "_loop_running", False) is True, 2.0,
            pump=lambda: pump(app, 0.05)), "CTk scan animation never started"
        app.on_closing()
        closed["flag"] = True
        pump(app, 0.4)
        assert not recorded_cb, recorded_cb[:2]
        loop_running = getattr(app.scan_bar, "_loop_running", None)
        assert loop_running is False, (
            "CTk scan-bar animation loop still running after close (it ticks "
            "a destroyed widget on an untracked internal after job)")
        assert app._scan_active is False
    finally:
        release_scan.set()  # unblock the scan worker, it will exit quietly
        if not closed["flag"]:
            with contextlib.suppress(Exception):
                app.on_closing()
        with contextlib.suppress(Exception):
            pump(app, 0.2)


def test_92_gui_raw_destroy_without_on_closing_records_no_callback_errors():
    """Armor for the hardening fix: destroying the app WITHOUT going through
    on_closing() must not raise exceptions into report_callback_exception.
    (A real embedder/fallback close path; pending after jobs are cancelled
    with the root widget, so this is expected to be clean.)"""
    app = ew.ScannerApp()
    recorded_cb = _install_recorder(app)
    try:
        # Destroy directly, bypassing on_closing: raw embedder-style teardown.
        with contextlib.suppress(tk.TclError):
            app.destroy()
        post_errs = pump(app, 0.5)
        unexpected = tuple(e for e in post_errs
                           if "application has been destroyed" not in e)
        assert not unexpected, f"unexpected post-destroy TclErrors: {unexpected[:2]}"
        assert not recorded_cb, (
            f"TclError escaped Tk callbacks on raw destroy: {recorded_cb[:2]}")
    finally:
        with contextlib.suppress(Exception):
            pump(app, 0.1)
