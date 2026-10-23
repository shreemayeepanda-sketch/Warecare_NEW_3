"""Auto-Action Rules ("The Kill Switch").

This module lets the user pre-authorise automatic responses to sustained
hardware pressure, e.g. "kill the top CPU process when CPU is above 95% for
two minutes". It is deliberately powerful, and just as deliberately fenced:

* Rules never fire on a single sample: the metric must stay above the
  threshold for the whole `duration_s`, and the timer resets the moment it
  drops back below.
* The Master Switch (off by default) must be enabled before anything runs.
* `kill_process` refuses a hard-coded list of critical system processes and
  the app's own process, and only ever terminates the single most demanding
  *eligible* process.
* Actions always execute on a dedicated worker thread, never on the GUI or
  monitoring thread.
* Every action is reported back through the `on_log` callback so the
  Diagnostics output shows exactly what happened.
"""
from __future__ import annotations

import logging
import os
import threading

log = logging.getLogger("shds.actions")

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:  # pragma: no cover - psutil is a core dependency in practice
    psutil = None  # type: ignore
    HAS_PSUTIL = False

# Metrics a rule can watch. All are percentages (0..100).
METRIC_LABELS = {"cpu": "CPU", "mem": "RAM", "disk": "Disk"}

# Actions a rule can take.
ACTION_LABELS = {"kill_process": "Kill Top Process",
                 "clean_temp": "Safe Temp Cleanup",
                 "alert_only": "Alert Only"}

# Critical processes that must NEVER be terminated, even in an emergency.
# Names are compared case-insensitively without a ".exe" suffix.
PROTECTED_PROCESS_NAMES = frozenset({
    # Kernel / session - killing these loses the whole machine.
    "system", "system idle process", "idle", "registry", "memory compression",
    "smss", "csrss", "wininit", "winlogon", "services", "lsass", "svchost",
    "fontdrvhost", "dwm", "winmgmt", "wudfhost", "audiodg",
    # The desktop shell - killing it destroys the user's session.
    "explorer", "taskmgr", "sihost", "shellexperiencehost",
    "searchhost", "startmenuexperiencehost",
    # Anti-malware / security (protected by design, not by permission).
    "securityhealthservice", "msmpeng", "smartscreen",
    # This application itself.
    "python", "pythonw", "pylauncher",
})

# Lowest PID value that may be terminated (0/4 are kernel on Windows).
_MIN_KILLABLE_PID = 5


def is_protected_process(name: str | None) -> bool:
    """True when a process must never be terminated."""
    if not name:
        return True
    base = str(name).strip().lower()
    base = base[:-4] if base.endswith(".exe") else base
    base = base.strip()
    return (not base) or base in PROTECTED_PROCESS_NAMES


class ActionRule:
    """One automatic action triggered by a sustained threshold breach."""

    def __init__(self, metric: str, threshold: float, duration_s: float,
                 action_type: str, label: str = "") -> None:
        if metric not in METRIC_LABELS:
            raise ValueError(f"unknown metric: {metric!r}")
        if action_type not in ACTION_LABELS:
            raise ValueError(f"unknown action_type: {action_type!r}")
        self.metric = metric
        self.threshold = float(threshold)
        self.duration_s = float(duration_s)
        self.action_type = action_type
        self.label = label
        # Live state: consecutive seconds spent above the threshold, and
        # how many times the rule has actually fired.
        self.breach_s = 0.0
        self.times_fired = 0

    # ---------- evaluation ----------
    def value(self, snapshot) -> float | None:
        """The metric the rule watches, or None when it is unavailable."""
        if snapshot is None:
            return None
        if self.metric == "cpu":
            value = getattr(snapshot.cpu, "percent", None)
        elif self.metric == "mem":
            value = getattr(snapshot.mem, "percent", None)
        else:  # disk: the most-used accessible partition
            candidates = [getattr(d, "percent", None) for d in
                          getattr(snapshot, "disks", []) or []
                          if getattr(d, "accessible", False)
                          and getattr(d, "percent", None) is not None]
            value = max(candidates) if candidates else None
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def exceeded(self, snapshot) -> bool:
        """True when the metric is at or above the threshold right now."""
        value = self.value(snapshot)
        return value is not None and value >= self.threshold

    def check(self, snapshot, time_elapsed: float) -> bool:
        """Should the rule fire? True only when the WHOLE duration elapsed.

        Pure predicate: `time_elapsed` is how long the metric has been
        continuously over the threshold. The caller owns the timer (see
        `RuleEngine.tick`); this method never mutates state.
        """
        if not self.exceeded(snapshot):
            return False
        return time_elapsed >= self.duration_s

    # ---------- bookkeeping / display ----------
    def description(self) -> str:
        return (f"{METRIC_LABELS[self.metric]} > {self.threshold:g}% "
                f"for {self.duration_s:g}s → {ACTION_LABELS[self.action_type]}")

    def __repr__(self) -> str:
        return (f"ActionRule({self.metric}, {self.threshold}, "
                f"{self.duration_s}, {self.action_type})")


class RuleEngine:
    """Checks the rules on every monitoring tick and runs the actions.

    `tick(snapshot, dt_s)` is called on every monitoring tick (dt_s = the
    sampling interval). If a rule's threshold has been exceeded once, its
    breach timer accumulates; the moment a sample comes back under the
    threshold the timer resets to zero. When a rule fires:

    * the breach timer resets (so another fire needs another full window);
    * the action runs on a worker thread created by `executor`
      (`threading.Thread(..., daemon=True)` by default — never the caller's
      thread, so the GUI can never freeze on a terminate() call);
    * the outcome is reported via `on_log(message, tag)`.

    Note that `on_log` may be called from a worker thread — it exist
    precisely so UI code can funnel the message back to the main thread.
    """

    def __init__(self, on_log=None, executor=None, self_pid: int | None = None,
                 clean_action=None, proc_provider=None) -> None:
        self.rules: list[ActionRule] = []
        self.enabled = False  # Master Switch — OFF until the user grants it.
        self._on_log = on_log
        self._executor = executor
        self.self_pid = self_pid if self_pid is not None else os.getpid()
        # clean_temp action: a callable returning a summary string. None means
        # "not wired up" and the action degrades to a log line (never deletes).
        self._clean_action = clean_action
        # kill_process victim source: callable(sort_by) -> rows with
        # .pid/.name/.cpu/.mem_mb. None falls back to psutil via collect_processes.
        self._proc_provider = proc_provider

    # ---------- logging helpers ----------
    def _log(self, message: str, tag: str = "AUTO") -> None:
        if self._on_log:
            try:
                self._on_log(message, tag)
            except Exception:  # noqa: BLE001 - logging must never break actions
                log.exception("actions on_log callback failed")

    # ---------- the tick ----------
    def tick(self, snapshot, dt_s: float) -> list[ActionRule]:
        """Evaluate every rule against one monitoring sample.

        Returns the rules that fired this tick (already dispatched to the
        executor). Never raises: a broken snapshot deactivates the rules for
        that tick only.
        """
        if not self.enabled or not self.rules:
            return []
        fired: list[ActionRule] = []
        for rule in self.rules:
            if rule.exceeded(snapshot):
                if dt_s > 0:
                    rule.breach_s += dt_s
                if rule.check(snapshot, rule.breach_s):
                    # Reset first: a second fire needs another full window,
                    # even if the action thread starts slowly.
                    rule.breach_s = 0.0
                    rule.times_fired += 1
                    fired.append(rule)
                    self._dispatch(rule, snapshot)
            else:
                # Threshold dropped below the limit → the timer resets.
                rule.breach_s = 0.0
        return fired

    def _dispatch(self, rule: ActionRule, snapshot) -> None:
        """Start the action on its own thread; never block the caller."""
        try:
            if self._executor:
                self._executor(lambda: self._run_action(rule, snapshot))
            else:
                threading.Thread(target=self._run_action, name="shds-action",
                                 args=(rule, snapshot), daemon=True).start()
        except (RuntimeError, OSError) as exc:
            log.warning("action thread failed to start: %s", exc)
            self._log(f"Auto-action could not start its worker thread: {exc}",
                      "WARNING")

    def _run_action(self, rule: ActionRule, snapshot) -> None:
        """Execute one action (runs on a worker thread)."""
        try:
            if rule.action_type == "kill_process":
                message, tag = self._kill_top_process(rule, snapshot)
            elif rule.action_type == "clean_temp":
                message, tag = self._clean_temp(rule, snapshot)
            else:  # alert_only
                message, tag = self._alert(rule, snapshot)
            self._log(message, tag)
        except Exception as exc:  # noqa: BLE001 - one bad action must not kill the engine
            log.exception("auto-action failed: %s", exc)
            self._log(f"Auto-action '{rule.action_type}' failed: {exc}", "WARNING")

    # ---------- actions ----------
    def _kill_top_process(self, rule: ActionRule, snapshot) -> tuple[str, str]:
        """Terminate ONE safe victim: the most demanding eligible process.

        Sorting follows the watched metric (CPU breach → sort by CPU,
        RAM breach → sort by memory; a Disk breach has no sensible victim and
        falls back to CPU). Protected and system processes are skipped.
        """
        if not HAS_PSUTIL:
            return ("psutil is unavailable, so the kill_process action ran "
                    "with no one to safely terminate.", "WARNING")
        sort_by = "mem" if rule.metric == "mem" else "cpu"
        rows = self._victims(sort_by)
        for row in rows:
            pid = getattr(row, "pid", 0)
            name = getattr(row, "name", "")
            if pid < _MIN_KILLABLE_PID or pid == self.self_pid:
                continue
            if is_protected_process(name):
                continue
            try:
                psutil.Process(int(pid)).terminate()
            except Exception as exc:  # noqa: BLE001 - a refused kill is logged
                return (f"Auto-action tried to kill {name} (PID {pid}) but the "
                        f"system refused: {exc}", "WARNING")
            metric_text = METRIC_LABELS[rule.metric].upper()
            return (f"Killed {name} (PID {pid}) because {metric_text} was "
                    f">{rule.threshold:g}% for {rule.duration_s:g}s.", "FAIL")
        return ("Auto-action (kill top process) fired, but every candidate was "
                "protected or unkillable — nothing was terminated.", "WARNING")

    def _victims(self, sort_by: str = "cpu") -> list:
        """Top candidate processes for the kill action."""
        if self._proc_provider is not None:
            return list(self._proc_provider(sort_by))
        if not HAS_PSUTIL:
            return []
        from .hardware import collect_processes
        try:
            return collect_processes(limit=8, sort_by=sort_by)
        except Exception as exc:  # noqa: BLE001 - defensive
            log.warning("collect_processes failed inside kill action: %s", exc)
            return []

    def _clean_temp(self, rule: ActionRule, snapshot) -> tuple[str, str]:  # noqa: ARG002
        """Run the app-provided safe temp cleanup (or explain its absence)."""
        if self._clean_action is None:
            return ("Auto-action (safe temp cleanup) fired, but no cleanup "
                    "handler is configured — nothing was deleted.", "WARNING")
        summary = str(self._clean_action())
        return (f"Auto-action (safe temp cleanup) fired: {summary}", "PASS")

    def _alert(self, rule: ActionRule, snapshot) -> tuple[str, str]:  # noqa: ARG002
        """Report the sustained breach without taking any action."""
        metric_text = METRIC_LABELS[rule.metric].upper()
        return (f"WARNING: {metric_text} stayed above {rule.threshold:g}% "
                f"for {rule.duration_s:g}s (alert-only rule).", "WARNING")
