"""Windows Event Log crash reader for the Warecare forensic crash analyzer.

Read-only "Windows Event Log Parser" used by the Forensic Crash Analyzer
feature. Every log query runs through PowerShell with the strict subprocess
pattern used everywhere in this package (see ``scanner.diagnostics``): an
explicit argv list, ``shell=False``, ``CREATE_NO_WINDOW`` and an 8-second
timeout. Nothing is started, stopped or written.

This is a pure logic module: no tkinter, no GUI imports, no side effects at
import time and no intra-package imports, so it is safe to import from
background threads and tests, standalone (``import crashlog``) as well as
under the package (``from scanner import crashlog`` — preferred).

Public API (a fixed contract shared with other agents — do not reshape):

    CRASH_EVENT_IDS = {1000, 1001, 1002}    app-crash event ids
    SHUTDOWN_EVENT_ID = 41                  "rebooted without clean shutdown"
    default_crashes_dir() -> Path           <repo root>/crashes (NOT created)
    availability() -> tuple[bool, str]      (True, "") or (False, reason)
    collect_recent_crashes(limit=5)         crash dicts, most recent first
    collect_unexpected_shutdowns(limit=3)   shutdown dicts, most recent first

Every collected record uses the same schema (the keys are part of the
contract); missing details degrade to "" and failed queries degrade to []:

    {"event_id": int,                       # 1000 | 1001 | 1002 | 41
     "timestamp": "YYYY-MM-DD HH:MM:SS",    # local time from the record
     "app": str,                            # program ("Windows" for 41)
     "faulting_module": str,                # "" when not present
     "exception_code": str,                 # e.g. "0xc0000005"; "" if absent
     "message": str,                        # one line, first 200 chars
     "key": str}                            # "event_id|timestamp|app"

Localisation note: crash details are parsed from the (usually English)
event message first; when those markers do not match — e.g. on a localized
Windows — the event's raw Properties array (the wevt payload) is used as
the fallback, so non-English systems still get app/module/code data.
"""

from __future__ import annotations

import json
import logging
import platform
import re
import subprocess
from datetime import datetime
from pathlib import Path

__all__ = [
    "CRASH_EVENT_IDS",
    "SHUTDOWN_EVENT_ID",
    "default_crashes_dir",
    "availability",
    "collect_recent_crashes",
    "collect_unexpected_shutdowns",
]

log = logging.getLogger(__name__)

# Application-crash event IDs: 1000 = Application Error (hard crash),
# 1001 = Windows Error Reporting hand-off, 1002 = Application Hang.
CRASH_EVENT_IDS = {1000, 1001, 1002}

# Kernel-Power "system rebooted without cleanly shutting down first".
SHUTDOWN_EVENT_ID = 41

_MAX_QUERY_LIMIT = 50  # bounds PowerShell output no matter the caller input
_PS_TIMEOUT_S = 8      # mirrors diagnostics.OS_CHECK_TIMEOUT_S
_MESSAGE_MAX_CHARS = 200  # "first ~200 chars" contract for the message field

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# One read-only PowerShell command per query. The cmdlet is wrapped in
# try/catch so "no matching events" exits 0 with no output (i.e. no
# crashes), while real failures (access denied, bad filter) rethrow and
# exit non-zero with the error text on stderr. Records are emitted as
# compact JSON; "Props" is the event's raw wevt payload, used as the
# localized-message fallback. OutputEncoding is pinned to UTF-8 so the
# subprocess pipe is decoded cleanly with errors="replace".
# Literal braces below are doubled for str.format().
_EVENT_QUERY_TEMPLATE = (
    "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
    "try {{ $events = @(Get-WinEvent -FilterHashtable @{{ "
    "LogName='{log_name}'; ID={event_ids} }} -MaxEvents {limit} "
    "-ErrorAction Stop) }} "
    "catch {{ if ($_.Exception.Message -match 'no events were found') "
    "{{ exit 0 }} else {{ throw }} }}; "
    "ConvertTo-Json -Compress -Depth 3 -InputObject @($events | "
    "ForEach-Object {{ [pscustomobject]@{{ Id=[int]$_.Id; "
    "Time=$_.TimeCreated.ToString('yyyy-MM-dd HH:mm:ss'); "
    "Message=[string]$_.Message; "
    "Props=@($_.Properties | ForEach-Object {{ [string]$_.Value }}) }} }})"
)

# Cheap availability probe: cmdlet present + Application log openable.
_AVAILABILITY_SCRIPT = (
    "$cmdlet = Get-Command Get-WinEvent -ErrorAction SilentlyContinue; "
    "if ($null -eq $cmdlet) { Write-Error "
    "'Get-WinEvent cmdlet is not available'; exit 2 }; "
    "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
    "(Get-WinEvent -ListLog Application -ErrorAction Stop).LogName"
)

# English message markers (case-insensitive); localized text falls back
# to the raw Properties array instead.
_APP_CRASH_RE = re.compile(r"faulting application name:\s*([^,\r\n]+)", re.I)
_MODULE_RE = re.compile(r"faulting module name:\s*([^,\r\n]+)", re.I)
_EXC_CODE_RE = re.compile(r"exception code:?\s*(0x[0-9a-fA-F]+|\d+)", re.I)
_WER_P1_RE = re.compile(r"\bP1:\s*(.+?)(?=\s+P\d+:|$)", re.I)
_HANG_APP_RE = re.compile(r"\bthe program\s+([^\s,]+)", re.I)
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
_EXECUTABLE_RE = re.compile(r"\.(exe|scr)\W*$", re.I)
_WER_REPORT_PATH_RE = re.compile(r"WER[\\/]", re.I)
_NTSTATUS_RE = re.compile(r"^0x[89abcdef][0-9a-f]{7}$", re.I)
_HEXISH_RE = re.compile(r"^(?:0x[0-9a-fA-F]{1,8}|\d{1,10})$")
_ACCESS_DENIED_RE = re.compile(
    r"access is denied|unauthorized|winerror 5|denied", re.I)

# ---------------------------------------------------------------------------
# PowerShell runner (mirrors diagnostics._run_readonly / _powershell)
# ---------------------------------------------------------------------------


def _run_powershell(script: str,
                    timeout: float = _PS_TIMEOUT_S) -> tuple[bool, str, str]:
    """Run one read-only PowerShell snippet; return ``(ok, stdout, error)``.

    Never raises: a missing binary, a permission error or a timeout is
    reported through the error text so callers can degrade instead of
    crashing. Output is decoded with ``errors="replace"`` so odd bytes
    become replacement characters rather than UnicodeErrors.
    """
    argv = ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]
    try:
        proc = subprocess.run(
            argv,
            shell=False,
            check=False,
            capture_output=True,
            timeout=timeout,
            creationflags=_CREATE_NO_WINDOW,
        )
    except FileNotFoundError:
        return False, "", "powershell not found (command not found)"
    except subprocess.TimeoutExpired:
        return False, "", f"powershell timed out after {timeout:g}s"
    except (PermissionError, OSError) as exc:
        return False, "", f"powershell could not run: {exc}"
    out = (proc.stdout or b"").decode("utf-8", errors="replace")
    err = (proc.stderr or b"").decode("utf-8", errors="replace")
    if proc.returncode != 0:
        detail = err.strip() or out.strip()[:120] or "no error text"
        return False, out, (f"powershell exited with code {proc.returncode}: "
                            f"{detail[:300]}")
    return True, out, ""


def _is_windows() -> bool:
    return platform.system().lower() == "windows"


# ---------------------------------------------------------------------------
# Small value helpers (strings, limits, timestamps, payload properties)
# ---------------------------------------------------------------------------


def _bounded_limit(limit: int, fallback: int) -> int:
    """Sanitize a caller limit: non-int -> fallback, <=0 -> 0, max 50."""
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return fallback
    if value < 1:
        return 0
    return min(value, _MAX_QUERY_LIMIT)


def _one_line(text: str) -> str:
    """Collapse every whitespace run (incl. newlines) to single spaces."""
    return " ".join(re.sub(r"\s+", " ", text or "").split()).strip()


def _shorten(text: str, max_chars: int) -> str:
    return (text or "").strip()[:max_chars].rstrip()


def _as_str(value) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return str(value)


def _as_int(value) -> int:
    try:
        text = str(value).strip()
        try:
            return int(text)
        except ValueError:
            return int(float(text))
    except (TypeError, ValueError, OverflowError):
        return 0


def _format_timestamp(value) -> str:
    """Normalize one record time to "YYYY-MM-DD HH:MM:SS" local time."""
    text = _as_str(value).strip()
    if _TIMESTAMP_RE.match(text):
        return text
    try:
        return datetime.fromisoformat(text).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        log.debug("unrecognised event timestamp %r", text)
        return ""


def _clean_name(value) -> str:
    """Trim a program/module value (name or full path) to its last segment."""
    text = _as_str(value).strip().strip('"').strip()
    if not text:
        return ""
    segments = [seg for seg in text.replace("/", "\\").split("\\") if seg]
    name = segments[-1].strip() if segments else ""
    return _shorten(name or text, 150)


def _property_list(value) -> list[str]:
    """Raw wevt payload values as stripped strings ("" when malformed)."""
    if not isinstance(value, list):
        return []
    return [(_as_str(item) or "").strip() for item in value]


def _property(props: list[str], index: int) -> str:
    if 0 <= index < len(props):
        return props[index] or ""
    return ""


# ---------------------------------------------------------------------------
# Record decoding
# ---------------------------------------------------------------------------


def _decode_records(raw: str) -> list[dict] | None:
    """Decode ConvertTo-Json output into dict records (None => unparseable)."""
    text = (raw or "").lstrip("\ufeff").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if isinstance(data, dict):
        data = [data]  # ConvertTo-Json unwraps single-element arrays
    if not isinstance(data, list):
        return None
    return [item for item in data if isinstance(item, dict)]


def _query_records(script: str) -> list[dict]:
    """Run one event query; any failure is logged and returns []."""
    ok, out, err = _run_powershell(script)
    if not ok:
        log.warning("Windows event log query failed: %s", err or "unknown error")
        return []
    records = _decode_records(out)
    if records is None:
        log.warning("Windows event log returned unparseable output: %.200r", out)
        return []
    return records

# ---------------------------------------------------------------------------
# Per-record crash description (message text first, raw payload fallback)
# ---------------------------------------------------------------------------


def _fallback_app(event_id: int, props: list[str]) -> str:
    """App name from the raw payload when the message text was unhelpful."""
    if event_id == 1001:
        # WER layouts differ between builds: some lead with the report
        # container path, some with bucket id/type/.../P1 values.
        for value in [_property(props, 0), _property(props, 5)] + props:
            if _WER_REPORT_PATH_RE.search(value or ""):
                return _clean_name(value)
        for value in [_property(props, 5)] + props:
            if _EXECUTABLE_RE.search(value or ""):
                return _clean_name(value)
        return ""
    # Events 1000/1002 carry the program in the first property (a short
    # name or a full path — _clean_name handles both).
    first = _clean_name(_property(props, 0))
    if first:
        return first
    for value in props:
        if _EXECUTABLE_RE.search(value or ""):
            return _clean_name(value)
    return ""


def _fallback_exception_code(props: list[str]) -> str:
    """Exception code from the payload for localized Event 1000 messages.

    The Event 1000 template holds the code in property 6, e.g. "0xc0000005";
    the NTSTATUS-shaped scan is a safety net for slightly different layouts.
    """
    candidate = _property(props, 6).strip()
    if candidate and _HEXISH_RE.match(candidate):
        return candidate
    for value in props:
        if _NTSTATUS_RE.match(value or ""):
            return value
    return ""


def _describe_crash(event_id: int, message: str,
                    props: list[str]) -> tuple[str, str, str]:
    """Extract ``(app, faulting_module, exception_code)`` for one record."""
    app = module = code = ""
    if event_id == 1001:
        match = _WER_P1_RE.search(message)
        if match:
            app = _clean_name(match.group(1))
    elif event_id == 1002:
        match = _HANG_APP_RE.search(message)
        if match:
            app = _clean_name(match.group(1))
    else:  # Event 1000: "Faulting application name: X, version: ..."
        match = _APP_CRASH_RE.search(message)
        if match:
            app = _as_str(match.group(1)).strip()
        match = _MODULE_RE.search(message)
        if match:
            module = _as_str(match.group(1)).strip()
    match = _EXC_CODE_RE.search(message)
    if match:
        code = _as_str(match.group(1)).strip()
    if not app:
        app = _fallback_app(event_id, props)
    if not module and event_id == 1000:
        module = _clean_name(_property(props, 3))
    if not code and event_id == 1000:
        code = _fallback_exception_code(props)
    return app, module, code


def _crash_dict(event_id: int, timestamp: str, app: str, module: str,
                code: str, message: str) -> dict:
    """Build one record with the exact contracted schema and dedupe key."""
    return {
        "event_id": event_id,
        "timestamp": timestamp,
        "app": app,
        "faulting_module": module,
        "exception_code": code,
        "message": message,
        "key": f"{event_id}|{timestamp}|{app}",
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def default_crashes_dir() -> Path:
    """Repo-root ``crashes`` directory: parents[2] of this file.

    scanner -> smart_hardware_diagnostic_scanner -> repo root. Callers own
    creating it; this module never does (read-only contract).
    """
    return Path(__file__).resolve().parents[2] / "crashes"


def availability() -> tuple[bool, str]:
    """Report whether the Windows Application event log is readable now.

    One read-only PowerShell probe (explicit argv, 8 s timeout): checks the
    ``Get-WinEvent`` cmdlet exists and the Application log can be opened.
    ``(True, "")`` on success; ``(False, reason)`` on a missing cmdlet,
    access denied, a broken/missing PowerShell or a timeout. Never raises.
    """
    if not _is_windows():
        return False, (f"Windows Event Log only exists on Microsoft Windows "
                       f"(found: {platform.system()})")
    ok, out, err = _run_powershell(_AVAILABILITY_SCRIPT)
    fields = out.strip().split()
    if ok and fields and fields[0].lower() == "application":
        return True, ""
    reason = err.strip() or f"unexpected event-log probe output: {out.strip()[:120]!r}"
    if _ACCESS_DENIED_RE.search(reason):
        reason = "Access denied while reading the Windows Event Log: " + reason
    return False, _shorten(reason, 300)


def collect_recent_crashes(limit: int = 5) -> list[dict]:
    """Collect recent application crash events, most recent first.

    Queries the Application log for IDs 1000/1001/1002 with one read-only
    PowerShell command (``-MaxEvents <limit>``). Details are parsed from
    the message text first and fall back to the raw event Properties array
    when the localized text lacks the usual English markers. Returns []
    (after logging) when PowerShell is missing, access is denied or the
    query times out; use availability() to explain that to the user.
    """
    count = _bounded_limit(limit, fallback=5)
    if not count or not _is_windows():
        return []
    script = _EVENT_QUERY_TEMPLATE.format(
        log_name="Application",
        event_ids=", ".join(str(event_id) for event_id in sorted(CRASH_EVENT_IDS)),
        limit=count,
    )
    crashes: list[dict] = []
    for record in _query_records(script) or []:
        event_id = _as_int(record.get("Id"))
        if event_id not in CRASH_EVENT_IDS:
            continue
        full = _one_line(_as_str(record.get("Message")))
        props = _property_list(record.get("Props"))
        app, module, code = _describe_crash(event_id, full, props)
        timestamp = _format_timestamp(record.get("Time"))
        crashes.append(_crash_dict(
            event_id, timestamp, app, module, code,
            _shorten(full, _MESSAGE_MAX_CHARS)))
    crashes.sort(key=lambda item: item["timestamp"], reverse=True)
    return crashes


def collect_unexpected_shutdowns(limit: int = 3) -> list[dict]:
    """Collect unexpected shutdowns (System log event 41), most recent first.

    Event 41 means Windows rebooted without shutting down cleanly (power
    loss, crash, or a held power button). "app" is always "Windows";
    faulting_module and exception_code stay "". Same degradation rules as
    collect_recent_crashes().
    """
    count = _bounded_limit(limit, fallback=3)
    if not count or not _is_windows():
        return []
    script = _EVENT_QUERY_TEMPLATE.format(
        log_name="System",
        event_ids=str(SHUTDOWN_EVENT_ID),
        limit=count,
    )
    shutdowns: list[dict] = []
    for record in _query_records(script) or []:
        event_id = _as_int(record.get("Id"))
        if event_id != SHUTDOWN_EVENT_ID:
            continue
        detail = _one_line(_as_str(record.get("Message")))
        if detail:
            message = "Unexpected shutdown or power loss — " + detail
        else:
            message = ("Unexpected shutdown or power loss — Windows restarted "
                       "without a clean shutdown (Kernel-Power event 41).")
        timestamp = _format_timestamp(record.get("Time"))
        shutdowns.append(_crash_dict(
            SHUTDOWN_EVENT_ID, timestamp, "Windows", "", "",
            _shorten(message, _MESSAGE_MAX_CHARS)))
    shutdowns.sort(key=lambda item: item["timestamp"], reverse=True)
    return shutdowns


# ---------------------------------------------------------------------------
# Demo (Windows only; harmless no-op elsewhere)
# ---------------------------------------------------------------------------


def _demo() -> None:
    """Small demo: ``python scanner/crashlog.py``."""
    print("Warecare crash-log reader — demo")
    print("=" * 40)
    ok, reason = availability()
    if not ok:
        print(f"Event log: UNAVAILABLE ({reason})")
        return
    print("Event log: available")
    crashes = collect_recent_crashes()
    print(f"\nRecent application crashes ({len(crashes)}):")
    if not crashes:
        print("  (none found)")
    for index, crash in enumerate(crashes, 1):
        print(f"  {index}. [{crash['event_id']}] {crash['timestamp']} | "
              f"app: {crash['app'] or '?'} | "
              f"module: {crash['faulting_module'] or '-'} | "
              f"code: {crash['exception_code'] or '-'}")
        print(f"     {crash['message']}")
    shutdowns = collect_unexpected_shutdowns()
    print(f"\nUnexpected shutdowns ({len(shutdowns)}):")
    if not shutdowns:
        print("  (none found)")
    for index, item in enumerate(shutdowns, 1):
        print(f"  {index}. [{item['event_id']}] {item['timestamp']}")
        print(f"     {item['message']}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING,
                        format="%(levelname)s: %(message)s")
    _demo()
