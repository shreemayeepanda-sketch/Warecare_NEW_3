# Agent 3 — Cross-Platform & Edge Case Audit of `ewarecare.py`

Scope: the three `diagnose_*_os` functions, the `_run_readonly` / `_powershell`
command wrappers, `ActionRule` / `RuleEngine` safety fences, and
`PROTECTED_PROCESS_NAMES`. Original package untouched (read-only);
`ewarecare.py` untouched — Agent 4 applies the edits below verbatim.

All 10 `OLD` blocks below were verified to occur **exactly once** in
`ewarecare.py` before publication, and the full set was applied to a sandbox
copy (temp dir, repo untouched) where my test file goes from 9 red → 34 green,
proving the edits are sufficient and machine-applicable.

---

## BUG-1 — Non-UTF-8 command output crashes or silently vanishes in `_run_readonly` — HIGH

**Evidence** — `ewarecare.py:2457-2472`:

```python
        proc = subprocess.run(
            argv,
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=_CREATE_NO_WINDOW,
        )
    except FileNotFoundError:
        return False, "", f"command not found: {argv[0]}"
    except subprocess.TimeoutExpired:
        return False, "", f"{argv[0]} timed out after {timeout:g}s"
```

`text=True` with no `encoding=` / `errors=` → decoding of the captured bytes
uses the locale codec with `errors="strict"`. One bad byte in `dmesg`,
PowerShell or `tmutil` output:

* **Windows** (empirically confirmed on this host, Python 3.14.7/cp1252):
  `_readerthread` raises `UnicodeDecodeError: 'charmap' codec can't decode
  byte 0x81…` *inside the reader thread* — `_run_readonly` then returns
  `ok=True, out=""`, i.e. the scan issues a **PASS / UNAVAILABLE verdict on
  zero evidence** and prints a traceback to stderr (ugly in a shipped .exe).
* **POSIX**: the same `UnicodeDecodeError` propagates **out of
  `_run_readonly`**, breaking its documented `"Never raises"` contract
  (2452-2455), killing, via `diagnose_os`'s blanket `except` at 2800-2802,
  *all three* Linux checks at once into one opaque finding.

Every other command wrapper in this same file already does it right (lines
714, 783, 863, 1422, 1455: `text=True, encoding="utf-8", errors="replace"`) —
`_run_readonly` is the outlier.

**Fix (single contiguous block, covers both the decode and the belt-and-braces
catch that keeps the "never raises" contract):**

`OLD:`
```python
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=_CREATE_NO_WINDOW,
        )
    except FileNotFoundError:
        return False, "", f"command not found: {argv[0]}"
    except subprocess.TimeoutExpired:
        return False, "", f"{argv[0]} timed out after {timeout:g}s"
```

`NEW:`
```python
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=_CREATE_NO_WINDOW,
        )
    except FileNotFoundError:
        return False, "", f"command not found: {argv[0]}"
    except subprocess.TimeoutExpired:
        return False, "", f"{argv[0]} timed out after {timeout:g}s"
    except UnicodeError as exc:  # defensive: bytes outside the declared codec
        return False, "", f"{argv[0]} produced undecodable output: {exc}"
```

---

## BUG-2 — The kill-safety protection is a single replaceable frozenset: no hard floor; emptying it permits killing csrss/lsass/System — CRITICAL

**Evidence** — the ONLY name gate is one module-level constant
(`ewarecare.py:3346-3358`), consumed once (`3366-3373`):

```python
PROTECTED_PROCESS_NAMES = frozenset({
    ...
    "system", "system idle process", "idle", "registry", "memory compression",
    "smss", "csrss", "wininit", "winlogon", "services", "lsass", "svchost",
    ...
})

def is_protected_process(name: str | None) -> bool:
    if not name:
        return True
    base = str(name).strip().lower()
    base = base[:-4] if base.endswith(".exe") else base
    base = base.strip()
    return (not base) or base in PROTECTED_PROCESS_NAMES
```

The other fences are numeric only: `_MIN_KILLABLE_PID = 5` (line 3361) blocks
PID 0-4 and `self_pid` (`3551`). If the set is ever emptied/reset (future
user-config layer, corrupted settings, refactor accident), `is_protected_process("lsass.exe")`
returns **False** (empirically confirmed) and `RuleEngine._kill_top_process`
(`3548-3556`) will `psutil.Process(pid).terminate()` csrss/lsass/wininit,
which all run with PID > 5. macOS/Linux attack surface (`kernel_task`,
`launchd`) isn't even in the set today, and nothing else protects it.

**Fix (two blocks): (a) add an irreducible floor set; (b) honor it in the
predicate.**

(a) `OLD:`
```python
# Lowest PID value that may be terminated (0/4 are kernel on Windows).
_MIN_KILLABLE_PID = 5
```

(a) `NEW:`
```python
# Irreducible safety floor: even if PROTECTED_PROCESS_NAMES is ever emptied,
# reset or user-cleared at runtime, these names are still refused outright.
_HARD_PROTECTED_FLOOR = frozenset({
    # Windows kernel / session
    "system", "system idle process", "idle", "registry",
    "memory compression", "smss", "csrss", "wininit", "winlogon",
    "services", "lsass", "svchost", "dwm", "explorer",
    # macOS / Linux
    "kernel_task", "launchd", "init", "systemd",
    # This application itself.
    "python", "pythonw",
})

# Lowest PID value that may be terminated (0/4 are kernel on Windows).
_MIN_KILLABLE_PID = 5
```

(b) `OLD:`
```python
    base = base[:-4] if base.endswith(".exe") else base
    base = base.strip()
    return (not base) or base in PROTECTED_PROCESS_NAMES
```

(b) `NEW:`
```python
    base = base[:-4] if base.endswith(".exe") else base
    base = base.strip()
    return ((not base) or base in _HARD_PROTECTED_FLOOR
            or base in PROTECTED_PROCESS_NAMES)
```

---

## BUG-3 — `ActionRule` accepts `duration_s <= 0`: instant kill on the very first sample — HIGH

**Evidence** — `ewarecare.py:3379-3393` validates only `metric` and
`action_type`, never `duration_s`:

```python
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
```

With `duration=0`, `check()` (`3420-3429`) is `time_elapsed >= 0.0` → True as
soon as `exceeded()` holds, and `RuleEngine.tick` (`3493-3502`) accumulates
the first `dt_s` and checks in the same call. Empirically confirmed on this
host: `ActionRule("cpu", 90, 0, "kill_process")` fires on the **first tick**
after a single 97 % CPU sample and dispatches a kill — which, paired with
BUG-2's old state, would have been `lsass.exe`. The GUI independently
validates 5..86400 s (``5458-5464``), but the standalone class is the safety
boundary and must defend itself. There is no division in `ActionRule` /
`RuleEngine`, so no ZeroDivisionError risk exists — the failure mode of the
degenerate window is the instant fire.

**Fix (reject at construction, consistent with the existing ValueErrors):**

`OLD:`
```python
        self.threshold = float(threshold)
        self.duration_s = float(duration_s)
        self.action_type = action_type
```

`NEW:`
```python
        self.threshold = float(threshold)
        self.duration_s = float(duration_s)
        if self.duration_s <= 0:
            raise ValueError(
                f"duration_s must be > 0 (no sustained-breach window = "
                f"instant fire): got {self.duration_s!r}")
        self.action_type = action_type
```

---

## BUG-4 — Scheduler `subprocess` calls also decode with the locale codec — LOW

**Evidence** — six `text=True` calls without `encoding/errors`:
`ewarecare.py` lines 6425-6426 (`schtasks` add), 6436-6438 (`crontab -l` in
`_schedule_job`), 6457-6459 (`crontab -` write), 6493-6494 (`schtasks`
delete), 6504-6505 (`crontab -l` in `_remove_schedule_job`), 6515-6517
(`crontab -` write). Here the crash class is only *accidentally* survivable:
the outer `except (OSError, ValueError)` at 6470-6471 / 6528-6529 happens to
catch `UnicodeDecodeError` (a `ValueError` subclass), but the data degrades to
an opaque failure and, on Windows, reader-thread tracebacks surface. Same
codebase-convention fix as BUG-1.

**Fixes (six blocks; each `OLD` verified unique with its context):**

4a `OLD:`
```python
                argv = build_schtasks_argv(python_exe, script, day, when)
                proc = subprocess.run(argv, shell=False, capture_output=True,
                                      text=True, timeout=8)
```
4a `NEW:`
```python
                argv = build_schtasks_argv(python_exe, script, day, when)
                proc = subprocess.run(argv, shell=False, capture_output=True,
                                      text=True, encoding="utf-8",
                                      errors="replace", timeout=8)
```

4b `OLD:`
```python
                    proc = subprocess.run(["crontab", "-l"], shell=False,
                                          capture_output=True, text=True,
                                          timeout=8)
```
4b `NEW:`
```python
                    proc = subprocess.run(["crontab", "-l"], shell=False,
                                          capture_output=True, text=True,
                                          encoding="utf-8", errors="replace",
                                          timeout=8)
```

4c `OLD:`
```python
                proc = subprocess.run(["crontab", "-"], shell=False,
                                      input=crontab_text, capture_output=True,
                                      text=True, timeout=8)
```
4c `NEW:`
```python
                proc = subprocess.run(["crontab", "-"], shell=False,
                                      input=crontab_text, capture_output=True,
                                      text=True, encoding="utf-8",
                                      errors="replace", timeout=8)
```

4d `OLD:`
```python
                argv = ["schtasks", "/Delete", "/TN", SCHTASKS_TASK_NAME, "/F"]
                proc = subprocess.run(argv, shell=False, capture_output=True,
                                      text=True, timeout=8)
```
4d `NEW:`
```python
                argv = ["schtasks", "/Delete", "/TN", SCHTASKS_TASK_NAME, "/F"]
                proc = subprocess.run(argv, shell=False, capture_output=True,
                                      text=True, encoding="utf-8",
                                      errors="replace", timeout=8)
```

4e `OLD:`
```python
                proc = subprocess.run(["crontab", "-l"], shell=False,
                                      capture_output=True, text=True, timeout=8)
```
4e `NEW:`
```python
                proc = subprocess.run(["crontab", "-l"], shell=False,
                                      capture_output=True, text=True,
                                      encoding="utf-8", errors="replace",
                                      timeout=8)
```

4f `OLD:`
```python
                proc = subprocess.run(["crontab", "-"], shell=False,
                                      input=new_text, capture_output=True,
                                      text=True, timeout=8)
```
4f `NEW:`
```python
                proc = subprocess.run(["crontab", "-"], shell=False,
                                      input=new_text, capture_output=True,
                                      text=True, encoding="utf-8",
                                      errors="replace", timeout=8)
```

---

## Verified clean (no bug)

Inspected and runtime-verified against the standalone module:

* Parsers tolerate hostile text: `_kernel_error_lines(None)`, `_inode_usage(" \n")`,
  `_failed_units(None)`, `_macos_log_errors("")`, `_diag_first_line(None)` — all
  safe; missing `df` columns and decimal-comma percents are skipped (no
  IndexError/int() crash); `_failed_units` only extracts from non-empty lines.
* Empty-but-clean Linux/macOS/Windows command output with rc=0 degrades as the
  codebase documents (matches package `test_dispatch_follows_platform_system`):
  `dmesg` empty → PASS ("nothing found" is truthful), `df` empty → UNAVAILABLE,
  `systemctl` empty → PASS, `log show` empty/header-only → PASS, `tmutil`
  empty-ok → UNAVAILABLE (unrecognised path), PowerShell nulls → UNAVAILABLE
  (System Restore PASS).
* Nonzero exit code WITH stdout data is never trusted: Windows/`dmesg`/
  `log show` all → UNAVAILABLE (data ignored on failure).
* `subprocess.TimeoutExpired` and `FileNotFoundError` are handled by
  `_run_readonly` and degrade per-check, not per-scan.
* `diagnose_os`'s blanket `except` (2800-2802) contains any unexpected throw
  from the dispatch — tested.
* `RuleEngine.tick` guards `dt_s > 0` (3494); no rate/threshold division exists
  in `ActionRule`/`RuleEngine` (so nothing can ZeroDivisionError); the
  degenerate-cooldown hazard is bug BUG-3, reported above.
* `ActionRule` GUI construction path (5450-5470) is unaffected by the BUG-3
  fix (it already enforces 5..86400); duration 0.001..86400 fixtures in the
  package tests remain constructible.

---

## Test run output (`python -m pytest tests/test_standalone_edge_cases.py -q`)

Harness: Python 3.14.7, pytest 9.1.1, Windows host. No OS diagnostic command
is ever executed (mocked `subprocess.run`, as the package tests do); the one
real process is a harmless child interpreter (`sys.executable -c`) used to
emit invalid byte sequences.

Pre-fix (repo state — expected red, every failure maps to a bug):

```
============================== short test summary info ===========================
FAILED tests/test_standalone_edge_cases.py::TestNonUTF8DecodeContract::test_decode_error_from_command_degrades_to_unavailable
FAILED tests/test_standalone_edge_cases.py::TestNonUTF8DecodeContract::test_decode_error_from_command_is_reported_not_raised
FAILED tests/test_standalone_edge_cases.py::TestNonUTF8DecodeContract::test_real_non_utf8_bytes_are_salvaged_not_lost
FAILED tests/test_standalone_edge_cases.py::TestProtectedFloor::test_engine_kills_nothing_when_only_critical_candidates
FAILED tests/test_standalone_edge_cases.py::TestProtectedFloor::test_engine_never_kills_critical_processes_when_set_emptied
FAILED tests/test_standalone_edge_cases.py::TestProtectedFloor::test_floor_survives_emptied_protected_set
FAILED tests/test_standalone_edge_cases.py::TestProtectedFloor::test_floor_survives_replaced_protected_set
FAILED tests/test_standalone_edge_cases.py::TestRuleDurationValidation::test_negative_duration_is_rejected
FAILED tests/test_standalone_edge_cases.py::TestRuleDurationValidation::test_zero_duration_is_rejected
9 failed, 25 passed, 1 warning in 1.24s
```

The pre-fix warning is itself evidence for BUG-1 (pytest catches the
`UnicodeDecodeError` raised in the subprocess reader thread:
`'charmap' codec can't decode byte 0x81 in position 3`).

Post-fix (all bug edits above applied to a sandbox copy of `ewarecare.py`;
the repo itself untouched):

```
..................................                                  [100%]
34 passed in 0.82s
```

## Tests expected red pre-fix → bug reproduced

| Test | Red symptom pre-fix | Bug |
|---|---|---|
| `TestNonUTF8DecodeContract::test_decode_error_from_command_degrades_to_unavailable` | `UnicodeDecodeError` escapes `diagnose_linux_os()` | BUG-1 |
| `TestNonUTF8DecodeContract::test_decode_error_from_command_is_reported_not_raised` | `UnicodeDecodeError` escapes `_run_readonly` ("Never raises" violated) | BUG-1 |
| `TestNonUTF8DecodeContract::test_real_non_utf8_bytes_are_salvaged_not_lost` | `'' is not true` — stdout silently lost (Windows reader-thread crash) | BUG-1 |
| `TestProtectedFloor::test_floor_survives_emptied_protected_set` | `False is not true : System` (also csrss/lsass/…) | BUG-2 |
| `TestProtectedFloor::test_floor_survives_replaced_protected_set` | `False is not true : csrss.exe` | BUG-2 |
| `TestProtectedFloor::test_engine_never_kills_critical_processes_when_set_emptied` | `Process(700)` called — csrss scheduled for terminate | BUG-2 |
| `TestProtectedFloor::test_engine_kills_nothing_when_only_critical_candidates` | `Process(800)` called — lsass terminated | BUG-2 |
| `TestRuleDurationValidation::test_zero_duration_is_rejected` | `ValueError not raised` (verified separately: rule fires on first tick) | BUG-3 |
| `TestRuleDurationValidation::test_negative_duration_is_rejected` | `ValueError not raised` | BUG-3 |

## Unverifiable / notes

* Real `dmesg`, `df`, `systemctl`, `log show`, `tmutil`, `journalctl`,
  `system_profiler` streams cannot be executed on this Windows host; all are
  mocked. The macOS `diagnose_macos_os` path uses only `log show` + `tmutil`
  (no `system_profiler`/`pmset`/`ps` in this build) — those commands are not
  exercised and were therefore not audited against this function.
* The POSIX main-thread `UnicodeDecodeError` mode was verified by simulation
  (the real run's decoder behavior); on Windows the same defect manifests as
  silent stdout loss (both reproduced above).
* `kernel_task`, `launchd`, `init`, `systemd` added by the BUG-2 floor are a
  policy choice (macOS/Linux invariants); actual macOS process names could
  not be observed on this host.
* PyInstaller `.exe` stderr visibility of reader-thread tracebacks was
  reasoned, not executed (no build in the audit boundary).
* BUG-4 severity is LOW because the scheduler happens to catch
  `UnicodeDecodeError` via its `except (OSError, ValueError)`.
