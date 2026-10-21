# Agent 1 — Threading & Runtime Audit of `ewarecare.py` (standalone build)

Auditor: Agent 1 of 4 (Threading & Runtime Auditor)
Target: `E:\Warecare\ewarecare.py` (untracked standalone flattened build, 6,823 lines)
Date: 2026-10-21 · Python 3.14.7 · customtkinter 6.0.0 · psutil 7.2.2 · pytest 9.1.1

Scope read in full: `MonitorWorker` (4694–4789), `ScanWorker` (4792–4833),
`ActionRule`/`RuleEngine` (3376–3590), `ScannerApp` plumbing — `_reschedule`
(5990–6007), `_poll_bg_queue` (6009–6077), `_apply_snapshot` (6079–6172),
`_automation_log` (5505–5507), Automation page wiring (5433–5503),
`refresh_once`/`run_scan`/`_collect_job`/`_scan_job` (5928–5988),
`toggle_monitoring`/`_poll_monitor` (6573–6635), `refresh_processes`/`_proc_job`
(6638–6666), other worker spawns (5272–5285, 6305, 6342, 6409, 6486, 6536),
`on_closing` (6796–6814), module bootstrap/`__main__` guard (1–120, 6816–6823).

All fixes below were **validated**: a script applied each `OLD` block to a
scratch-copy of `ewarecare.py`; every block matched **exactly once** and the
patched copy turned the full new test suite green
(`11 passed in 7.38s`, see "Test run output" at the end).

---

## Bug 1 — HIGH — Stale `_stop` event read in `MonitorWorker._loop`: an old sampler thread never stops and runs in duplicate alongside a new run

**Evidence** — `ewarecare.py` 4707–4726 (`start` creates a *fresh* stop event),
4732–4749 (`stop`), 4754–4776 (`_loop` reads the event **by attribute** inside
the loop):

```python
4707:    def start(self) -> bool:
4712:            # A fresh stop event per run: a later start() must never re-arm the
4713:            # stop flag of a thread that is still finishing its last sample.
4714:            self._stop = threading.Event()
4715:            thread = threading.Thread(target=self._loop, name="shds-monitor", daemon=True)
...
4754:    def _loop(self) -> None:
4755:        while not self._stop.is_set():
...
4774:            wait = self.interval_s - (time.monotonic() - started)
4775:            if wait > 0:
4776:                self._stop.wait(wait)
```

**Race** (verified deterministically by `test_02`): T1 is mid-sample when the
user stops monitoring and immediately restarts (a normal interaction with the
▶/■ button, or `stop()`+`start()` programmatically). `stop()` sets the *old*
event E1 and clears `running`; the new `start()` replaces `_stop` with a fresh
E2. When T1 finishes its slow sample it re-executes `while not
self._stop.is_set()` and `self._stop.wait(wait)` — against **E2**. Consequences:

* T1 never observes E1 and keeps sampling forever (the "fresh event per run"
  design only works if the event is captured locally — it never is);
* two sampler loops feed the same `samples` queue at double rate (duplicated
  dashboard/chart data) and both append to `history`;
* the reaper thread started by `stop()` times out on `join(3)` and T1 survives
  until process exit — an unkillable orphan even after `on_closing`.

**Root cause**: the loop borrows the mutable `self._stop` attribute instead of
binding the event of *its own run* once at thread start.

**Fix** (2 surgical edits):

```python
OLD:
    def _loop(self) -> None:
        while not self._stop.is_set():

NEW:
    def _loop(self) -> None:
        # Bind the stop event ONCE: start() swaps self._stop between runs, so
        # re-reading the attribute inside the loop would keep an old sampler
        # thread alive forever when a new run arms a fresh event.
        stop = self._stop
        while not stop.is_set():
```

```python
OLD:
            wait = self.interval_s - (time.monotonic() - started)
            if wait > 0:
                self._stop.wait(wait)

NEW:
            wait = self.interval_s - (time.monotonic() - started)
            if wait > 0:
                stop.wait(wait)
```

(`request_stop()` still works: it sets the event the *current* thread bound.)

---

## Bug 2 — HIGH — Kill switch double-fire: no in-flight guard, the same rule can dispatch overlapping actions and terminate the same process twice

**Evidence** — `ewarecare.py` 3496–3502 (`tick` fire block), 3508–3519
(`_dispatch`), 3521–3533 (`_run_action`):

```python
3492:        for rule in self.rules:
3493:            if rule.exceeded(snapshot):
3494:                if dt_s > 0:
3495:                    rule.breach_s += dt_s
3496:                if rule.check(snapshot, rule.breach_s):
3497:                    # Reset first: a second fire needs another full window,
3498:                    # even if the action thread starts slowly.
3499:                    rule.breach_s = 0.0
3500:                    rule.times_fired += 1
3501:                    fired.append(rule)
3502:                    self._dispatch(rule, snapshot)
...
3514:            threading.Thread(target=self._run_action, name="shds-action",
3515:                             args=(rule, snapshot), daemon=True).start()
```

`tick()` runs on the Tk main thread only (single call site, `_poll_monitor`
line 6628), so the *breach timer itself* is not racy. The race is
**check-and-act across cycles vs. the asynchronous action thread**: `tick`
never learns when a previously dispatched action actually finished. While the
first action thread is still running (`_victims` → `collect_processes` can
take hundreds of ms or worse; `psutil.Process.terminate()` at 3556 is
asynchronous so the victim may still be top-of-list next round), the breach
timer re-accumulates a full window and the **same rule is dispatched again**.
Deterministic red results: `test_04` — 12 sustained ticks dispatched **6
overlapping `kill_process` actions for one rule**; `test_05` — **3 extra
dispatches while the first kill was provably mid-flight**. Two concurrent
`_kill_top_process` runs can terminate the same PID twice, and two concurrent
`clean_temp` sweeps race the temp dir. The log will even *claim two kills*.

**Root cause**: fire-state is only the breach-timer reset; there is no
in-flight marker on the rule and no lock/event guarding dispatch→completion.

**Fix** (4 surgical edits — per-rule `in_flight` Event; never blocks the main
thread; a skipped fire is logged once per full re-armed crossing):

```python
OLD:
        self.breach_s = 0.0
        self.times_fired = 0

NEW:
        self.breach_s = 0.0
        self.times_fired = 0
        # Double-fire guard: set on dispatch, cleared when the action thread
        # finishes (see RuleEngine._dispatch / _run_action).
        self.in_flight = threading.Event()
```

```python
OLD:
                if rule.check(snapshot, rule.breach_s):
                    # Reset first: a second fire needs another full window,
                    # even if the action thread starts slowly.
                    rule.breach_s = 0.0
                    rule.times_fired += 1
                    fired.append(rule)
                    self._dispatch(rule, snapshot)

NEW:
                if rule.check(snapshot, rule.breach_s):
                    # Reset first: a second fire needs another full window,
                    # even if the action thread starts slowly.
                    rule.breach_s = 0.0
                    if rule.in_flight.is_set():
                        # The previous action for this rule is still running
                        # (a slow victim lookup or slow sample path): never
                        # dispatch a second concurrent one, or the same
                        # process can be terminated twice.
                        self._log(f"Auto-action '{rule.action_type}' skipped "
                                  "— previous action is still running.",
                                  "WARNING")
                        continue
                    rule.times_fired += 1
                    fired.append(rule)
                    self._dispatch(rule, snapshot)
```

```python
OLD:
        try:
            if self._executor:
                self._executor(lambda: self._run_action(rule, snapshot))
            else:
                threading.Thread(target=self._run_action, name="shds-action",
                                 args=(rule, snapshot), daemon=True).start()
        except (RuntimeError, OSError) as exc:
            log_actions.warning("action thread failed to start: %s", exc)

NEW:
        rule.in_flight.set()
        try:
            if self._executor:
                self._executor(lambda: self._run_action(rule, snapshot))
            else:
                threading.Thread(target=self._run_action, name="shds-action",
                                 args=(rule, snapshot), daemon=True).start()
        except (RuntimeError, OSError) as exc:
            rule.in_flight.clear()
            log_actions.warning("action thread failed to start: %s", exc)
```

```python
OLD:
        except Exception as exc:  # noqa: BLE001 - one bad action must not kill the engine
            log_actions.exception("auto-action failed: %s", exc)
            self._log(f"Auto-action '{rule.action_type}' failed: {exc}", "WARNING")

NEW:
        except Exception as exc:  # noqa: BLE001 - one bad action must not kill the engine
            log_actions.exception("auto-action failed: %s", exc)
            self._log(f"Auto-action '{rule.action_type}' failed: {exc}", "WARNING")
        finally:
            # The action is done: after a fresh full breach window the rule
            # may fire again. Runs even when the action raised.
            rule.in_flight.clear()
```

(`threading` is already imported at module scope — `_dispatch` itself uses it
at line 3514.)

---

## Bug 3 — MEDIUM — `_procs_active` can wedge True forever on an unexpected thread-start failure → process table silently freezes

**Evidence** — `ewarecare.py` 6644–6656 (`refresh_processes`), 6658–6666
(`_proc_job`):

```python
6644:        if self._procs_active:
6645:            return
...
6650:        self._procs_active = True
6651:        try:
6652:            threading.Thread(target=self._proc_job, args=(sort_by,),
6653:                             name="shds-procs", daemon=True).start()
6654:        except (RuntimeError, OSError) as exc:
6655:            log_app.warning("process thread failed to start: %s", exc)
6656:            self._procs_active = False
```

Verified: the worker-side contract is fine — `_proc_job` clears the flag in a
`finally` (6665–6666) even when `collect_processes` raises (covered by
`test_07`, green pre-fix). The gap is the **start side**: `.start()` can raise
anything (e.g. `MemoryError`, `RecursionError`); those are not caught here, so
the exception escapes `refresh_processes` (swallowed into `_poll_monitor`'s /
`_poll_bg_queue`'s blanket handlers and merely logged) **leaving
`_procs_active == True` forever**. Every subsequent `_apply_snapshot` →
`refresh_processes` call short-circuits at 6644: the Processes view and
process-based kill-switch victim data silently stop updating for the rest of
the session. Same shape as `refresh_once` 5937/`run_scan` 5968 but uniquely
flag-wedging.

**Root cause**: the guard flag is set before `start()` but only cleared for two
specific exception types.

**Fix** (1 edit):

```python
OLD:
        except (RuntimeError, OSError) as exc:
            log_app.warning("process thread failed to start: %s", exc)
            self._procs_active = False

NEW:
        except Exception as exc:  # noqa: BLE001 - ANY start failure must
            # clear the guard flag: refresh_processes only runs while it is
            # False, so a wedged True flag freezes the process table forever.
            log_app.warning("process thread failed to start: %s", exc)
            self._procs_active = False
```

(Deliberately *not* a try/finally around `start()`: while a worker is running
the flag *must* stay True — the worker's own `finally` owns it.)

---

## Bug 4 — HIGH — `on_closing` leaves live machinery running: worker not stopped/joined, kill switch still armed, UI state stale, CTk scan animation loop never cancelled

**Evidence** — `ewarecare.py` 6796–6814, with `toggle_monitoring` (6574) and
`run_scan`/`scan_bar.start(12)` (5960) for context:

```python
6796:    def on_closing(self) -> None:
6797:        try:
6798:            if self.monitor_running:
6799:                self.monitor.request_stop()
6800:        except Exception:
6801:            pass
6802:        self._scan_active = False
6803:        # Cancel pending callbacks first: a callback that fires after destroy()
...
6810:        self._after_ids.clear()
6811:        try:
6812:            self.destroy()
6813:        except tk.TclError:
6814:            pass
```

Four concrete defects, all reproduced by the integration tests:

1. **`request_stop()` instead of `stop()`** — the event gets set (so the thread
   *does* exit on its next loop check), but `MonitorWorker.running` is never
   cleared and nothing joins the worker (`stop()`'s reaper at 4747–4749 is the
   join path; `request_stop` has none). `test_90` red: `monitor.running is
   True` after close. With Bug 1 present this can additionally leave a truly
   orphaned sampler alive after close.
2. **`monitor_running` never reset** — the header flag/UI state stays "live"
   (`test_90` red). Meaningful for re-shown/embedded windows and for any
   post-close UI assert; stale-True forever otherwise.
3. **Kill switch stays armed** — `self.actions_engine.enabled` is never set to
   False, and no rule state is cleared. A re-shown window (or an embedder that
   resurrects the object) resumes with rules armed; combined with (1), the
   engine may dispatch an action right after the window is gone. `test_90` red
   (`engine.enabled is True` after close).
4. **CustomTkinter's internal scan-bar `after` loop is never cancelled** —
   `run_scan()` calls `self.scan_bar.start(12)` (5960), which lands in CTk
   6.0.0's `CTkProgressBar.start` → `_internal_loop`, self-perpetuating every
   20 ms via `self.after(20, self._internal_loop)` — an after id **not tracked
   in `_after_ids`**. Closing mid-scan leaves `_loop_running=True`; after
   `destroy()` the loop keeps firing as an invalid Tcl command
   (`invalid command name "..._internal_loop"` on stderr — verified
   empirically; in a frozen .exe this is console/log noise every 20 ms until
   the process exits). `test_91` red (`_loop_running is True` after close).

(The good news, verified and covered by armor tests: the `after` callbacks the
app itself schedules are all tracked and cancelled correctly, and even a raw
`destroy()` bypassing `on_closing` produces **no** Python-level
`report_callback_exception` today — see "Could NOT fully verify"/observations.)

**Root cause**: shutdown assumes the sampler dies by event alone and forgets
the engine and the untracked CTk animation loop.

**Fix** (1 edit, replacing the top block of `on_closing`; everything from
`self._scan_active = False` down is kept unchanged):

```python
OLD:
    def on_closing(self) -> None:
        try:
            if self.monitor_running:
                self.monitor.request_stop()
        except Exception:
            pass
        self._scan_active = False

NEW:
    def on_closing(self) -> None:
        self.monitor_running = False
        self._monitor_polling = False
        try:
            # stop() (not just request_stop) also arms a helper thread that
            # joins the sampler and clears `monitor.running`: no monitoring
            # loop or stale "live" state may outlive the window.
            self.monitor.stop()
        except Exception:
            pass
        try:
            # Disarm the kill switch: no further auto-action may fire after
            # the window is gone (an in-flight action still finishes on its
            # thread, but nothing new is dispatched).
            self.actions_engine.enabled = False
        except Exception:
            pass
        try:
            # Cancel CustomTkinter's internal animation `after` loop: nothing
            # else stops it on shutdown and it keeps ticking after destroy.
            self.scan_bar.stop()
        except Exception:
            pass
        self._scan_active = False
```

---

## Bug 5 — MEDIUM — `ActionRule.value` raises `AttributeError` on a partial snapshot, breaking `RuleEngine.tick`'s documented "never raises" contract

**Evidence** — `ewarecare.py` 3396–3413 (`value`) vs the contract at 3487:

```python
3396:    def value(self, snapshot) -> float | None:
3397:        """The metric the rule watches, or None when it is unavailable."""
3398:        if snapshot is None:
3399:            return None
3400:        if self.metric == "cpu":
3401:            value = getattr(snapshot.cpu, "percent", None)
3402:        elif self.metric == "mem":
3403:            value = getattr(snapshot.mem, "percent", None)
...
3482:    def tick(self, snapshot, dt_s: float) -> list[ActionRule]:
3487:        Returns the rules that fired this tick (already dispatched to the
3488:        executor). Never raises: a broken snapshot deactivates the rules for
```

Reproduced by `test_08`: `tick(object(), 1.0)` → `AttributeError: 'object'
object has no attribute 'cpu'` straight out of `tick`. Today the app masks
this in `_poll_monitor`'s blanket handler — the symptom is a silently disabled
auto-action chain for that tick plus error-log spam — but any future/adapted
caller of the engine gets an exception where the API promises "never raises"
(e.g. a degrade-mode collector returning a half-built snapshot section).

**Root cause**: the section objects (`snapshot.cpu` / `snapshot.mem`) are read
without a getattr fallback, unlike `disks` (3405–3408), which already does.

**Fix** (1 edit):

```python
OLD:
        if snapshot is None:
            return None
        if self.metric == "cpu":
            value = getattr(snapshot.cpu, "percent", None)
        elif self.metric == "mem":
            value = getattr(snapshot.mem, "percent", None)

NEW:
        if snapshot is None:
            return None
        # A broken/partial snapshot degrades to "no value" (never raises):
        # RuleEngine.tick promises to never raise on unusable input.
        if self.metric == "cpu":
            value = getattr(getattr(snapshot, "cpu", None), "percent", None)
        elif self.metric == "mem":
            value = getattr(getattr(snapshot, "mem", None), "percent", None)
```

---

## Bug 6 — LOW / HARDENING — `_reschedule`'s `_run` callback has no TclError guard; an escaping error goes straight to `report_callback_exception`

**Evidence** — `ewarecare.py` 5990–6007:

```python
5999:        def _run() -> None:
6000:            self._after_ids.discard(holder.get("id", ""))
6001:            fn()
```

Every scheduled after-fn (`_poll_bg_queue`, `_poll_monitor`, `refresh_once`)
currently guards their own widget access, and a raw `destroy()` empirically
deletes the callback Tcl commands (test_92 is clean) — so **this is not
reproducible through todays verified paths**. It is included as machine-
applicable defense-in-depth: the ONLY barrier between a stray `TclError` from
`fn()` and stderr tracebacks / a frozen-build console window is nothing at all.

**Fix** (1 edit):

```python
OLD:
        def _run() -> None:
            self._after_ids.discard(holder.get("id", ""))
            fn()

NEW:
        def _run() -> None:
            self._after_ids.discard(holder.get("id", ""))
            try:
                fn()
            except tk.TclError:
                # The window died between scheduling and firing (a destroy
                # path that bypassed on_closing): never leak a callback
                # traceback through report_callback_exception.
                pass
```

If Agent 4 prefers the strictest minimal patch set, Bug 6 may be skipped
without regressing any red test — the other 6 fixes are required for green.

---

## Verified-clean areas (no bug)

* `MonitorWorker.poll()` / `ScanWorker`: queue draining on main thread only;
  worker `finally` clears `running` (4819–4820); start-failure resets (4822–4830).
* `_poll_bg_queue` (6009–6077): per-item `except Exception` isolation + `finally`
  re-schedule; post-destroy it dies correctly (`_reschedule` checks
  `winfo_exists`, which raises `TclError` post-destroy → caught, loop stops).
* `_poll_monitor` (6595–6635): blanket guard + unconditional re-schedule; the
  single `tick` call site on the main thread (no concurrent breed of double-fire).
* `_reschedule` bookkeeping: ALL app `after` calls go through it (single
  `self.after(` site, line 6004); `on_closing` cancels `_after_ids` correctly.
* `_automation_log` is correctly thread-side (`_bg_queue` funnel, main thread
  drains) — no widget access off-thread.
* GUI-after-destroy via the real close path: no `TclError` escapes
  (`test_90`/`test_92` record `report_callback_exception`; clean).
* Thread-start failures in `refresh_once`/`run_scan`/`ScanWorker` reset their
  flags for the `(RuntimeError, OSError)` case.

---

## Test run output

Pre-fix (real `E:\Warecare\ewarecare.py`), command
`python -m pytest tests/test_standalone_threading.py -q` from `E:\Warecare`:

```
.F.FFF.FFF.                                                              [100%]
================================== short summary ==================================
FAILED tests/test_standalone_threading.py::test_02_monitor_worker_stop_start_race_leaves_no_immortal_thread
FAILED tests/test_standalone_threading.py::test_04_kill_switch_no_double_dispatch_while_action_in_flight
FAILED tests/test_standalone_threading.py::test_05_kill_switch_refire_allowed_after_action_completes
FAILED tests/test_standalone_threading.py::test_06_procs_active_cleared_when_thread_start_fails_unexpectedly
FAILED tests/test_standalone_threading.py::test_08_rule_engine_tick_survives_broken_snapshots
FAILED tests/test_standalone_threading.py::test_90_gui_close_stops_monitor_disarms_engine_and_leaks_nothing
FAILED tests/test_standalone_threading.py::test_91_gui_close_cancels_ctk_scan_animation_loop
7 failed, 4 passed in 12.38s
```

Key failing-moment excerpts (full tracebacks in the pytest run):

```
test_02: AssertionError: old sampler thread ignored its own stop event and keeps running
         alongside the new one (stale self._stop attribute read)
test_04: AssertionError: exact one in-flight action allowed, got 6 dispatches for the same rule
test_05: AssertionError: no double-fire while an action is running; got 3 extra dispatches
test_06: AssertionError: _procs_active wedged True after a failed thread start; ...
test_08: AttributeError: 'object' object has no attribute 'cpu'   (ewarecare.py:3401, via tick :3493)
test_90: AssertionError: monitor.running stays True after close (request_stop never clears it)
test_91: AssertionError: CTk scan-bar animation loop still running after close ...   (_loop_running True)
```

Post-fix validation (same test file run against a scratch copy with exactly the
`OLD`→`NEW` blocks above applied — `ewarecare.py` itself untouched):

```
patched copy written: C:\Users\Devansh_13\AppData\Local\Temp\opencode\ewarecare_fixed_check.py
all OLD blocks applied exactly once
...........                                                              [100%]
11 passed in 7.38s
```

## Tests expected RED pre-fix (and the bug each reproduces)

| Test | Bug | Red assertion |
|---|---|---|
| `test_02_monitor_worker_stop_start_race_leaves_no_immortal_thread` | 1 | old sampler alive after `stop()`+`start()` dance |
| `test_04_kill_switch_no_double_dispatch_while_action_in_flight` | 2 | 6 dispatches instead of 1 for one rule |
| `test_05_kill_switch_refire_allowed_after_action_completes` | 2 | 3 extra dispatches while action 1 is blocked |
| `test_06_procs_active_cleared_when_thread_start_fails_unexpectedly` | 3 | `_procs_active` wedged True |
| `test_08_rule_engine_tick_survives_broken_snapshots` | 5 | `AttributeError` escapes `tick(object())` |
| `test_90_gui_close_stops_monitor_disarms_engine_and_leaks_nothing` | 4 (+1/2 armor) | `monitor.running` True after close (`monitor_running`/`engine.enabled` red too) |
| `test_91_gui_close_cancels_ctk_scan_animation_loop` | 4d | CTk `_loop_running` True after close |

Green pre-fix (regression/armor): `test_01` (`__main__` guard, import is
side-effect free), `test_03` (restart keeps sampling — guards the Bug 1 fix),
`test_07` (`_procs_active` cleared on normal completion), `test_92` (raw
destroy without `on_closing` is clean today). Total 11 tests, ~12 s.

## Safety

No real process was ever terminated, no registry/network touched: engine tests
use a recording executor or a patched `_kill_top_process`; integration tests
monkeypatch `collect_snapshot`/`collect_processes`/`run_diagnostics`/
`build_report_dict` and use `alert_only`-type rules. The GUI tests build the
real `ScannerApp` and drive it with `update()` pumps only — never `mainloop()`;
the bootstrap asserts a `__main__` guard exists and that importing records no
mainloop call.

## Could NOT fully verify / observations (no fixes proposed)

1. **CustomTkinter root's own internal after jobs** (`check_dpi_scaling`,
   `update`, `windows_set_titlebar_icon`) also survive `destroy()` and print
   `invalid command name ...` to stderr on ANY close — cosmetic Tcl-command
   noise inside the library, not reachable by a surgical fix in this file.
   (Verified empirically on 6.0.0; our Bug 4d fix removes the *scan-bar* loop,
   which is the one we control.)
2. **A hung `_proc_job` worker** (`collect_processes` blocked on a wedged
   psutil/WMI call) keeps `_procs_active` True and freezes process refresh by
   design; no watchdog was added (would be an invasive change). The `finally`
   covers every completion path including raises — verified by test_07.
3. **`MonitorWorker.history`** is appended/sliced from the worker thread and
   `clear_history()` runs on the main thread; each operation is GIL-atomic and
   the race is cosmetic (worst case a stale chart list length). Not fixed.
4. **Breach-timer dt divergence**: `tick` is fed `self.monitor.interval_s`
   while polls run at `_poll_interval_ms()` (from the UI variable). If a user
   changes the refresh interval mid-run the accumulated breach time drifts
   from wall-clock. Timing drift only — it does not change fire *counts* — and
   a surgical fix would touch the variant plumbing, so it is reported only.
5. **Reaper join window**: `stop()`'s helper `join(3)` can be shorter than a
   worst-case 8 s GPU sample. Once Bug 1 is fixed the thread always exits on
   its own event, so the reaper is best-effort; no change made.
6. `test_91` asserts CTk's private `getattr(bar, "_loop_running", None)`
   (present in 6.0.0; a sentinel default keeps it non-crashing if CTk renames).
7. `_poll_monitor` draining multiple snapshots per cycle always ticks ONCE
   with the newest snapshot — a "one tick per poll" analysis confirmed no GUI
   path can call `tick` twice per cycle (kill-switch double-fire is exclusively
   Bug 2's cross-cycle race).
