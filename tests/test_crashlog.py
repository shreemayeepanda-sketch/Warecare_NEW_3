"""Forensic Crash Analyzer tests — scanner.crashlog + tab glue.

PowerShell output is mocked at the module boundary exactly like the
diagnostics tests: no real powershell.exe is ever spawned by these tests
(test 7 patches ``subprocess.run`` itself to assert the hardened argv
contract and the timeout degrade). Coverage:

* Event 1000 parsing from the (English) message text: app, faulting
  module, exception code, dedupe key and the exact record schema;
* Localized-message fallback to the raw wevt Properties payload;
* Unknown event ids are dropped; "no events" exits 0 -> [];
* Access-denied / non-admin degrade: [] plus availability() -> (False, ...);
* Kernel-Power event 41 unexpected-shutdown records;
* Hardened subprocess invocation (shell=False, explicit argv, -NoProfile,
  timeout=8, no console window) and timeout degradation;
* Limit bounding (invalid -> no query at all, oversized -> -MaxEvents 50);
* explainer.explain_crash plain-English line for 0xC0000005 with a
  high-RAM pre-crash snapshot, plus decimal-vs-hex code normalisation;
* scanner.app crash-row merging/dedup against saved crashes/*.json files
  with the stored pre-crash snapshot attached.
"""
from __future__ import annotations

import json
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG_PARENT = ROOT / "smart_hardware_diagnostic_scanner"
if str(PKG_PARENT) not in sys.path:
    sys.path.insert(0, str(PKG_PARENT))

import scanner.app as scanner_app  # noqa: E402
import scanner.crashlog as crashlog  # noqa: E402
from scanner.explainer import _normalise_exception_code, explain_crash  # noqa: E402


@pytest.fixture(autouse=True)
def force_windows(monkeypatch):
    """The collectors bail out on non-Windows by design; tests mock the
    PowerShell layer, so force the platform check true deterministically."""
    monkeypatch.setattr(crashlog, "_is_windows", lambda: True)


def _completed(stdout: str | bytes, returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess(
        args=["powershell"], returncode=returncode,
        stdout=stdout if isinstance(stdout, bytes)
        else stdout.encode("utf-8"),
        stderr=stderr.encode("utf-8"))


def _event(record: dict) -> str:
    """ConvertTo-Json -Compress -Depth 3 -InputObject @(...) equivalent."""
    return json.dumps(record, ensure_ascii=False)


EVENT_1000_JSON = _event([
    {"Id": 1000, "Time": "2026-10-24 13:45:12",
     "Message": ("Application: chrome.exe\r\nFaulting application name: "
                 "chrome.exe, version: 141.0.7390.66, time stamp: 0x68f2\r\n"
                 "Faulting module name: ntdll.dll, version: 10.0.26100.1, "
                 "time stamp: 0x01234567\r\nException code: 0xc0000005\r\n"
                 "Fault offset: 0x000a1d34\r\nFaulting process id: 0x1a2b"),
     "Props": ["chrome.exe", "141.0.7390.66", "68f2aabb", "ntdll.dll",
               "10.0.26100.1", "01234567", "0xc0000005"]},
    {"Id": 1001, "Time": "2026-10-24 09:11:55",
     "Message": ("Fault bucket , type 5\r\nEvent Name: APPCRASH Response: "
                 "not available\r\nP1: notepad.exe P2: 10.0.26100.1 P3: x"),
     "Props": [r"C:\ProgramData\Microsoft\Windows\WER\ReportArchive"
               r"\AppCrash_notepad.exe_123",
               "APPCRASH", "not available", "notepad.exe", "10.0.26100.1",
               "x"]},
])

# Localized Windows: the message carries no English markers, so the raw
# wevt Event 1000 template is used — props[0] app name, props[3] module
# name, props[6] exception code.
LOCALIZED_EVENT_1000_JSON = _event([
    {"Id": 1000, "Time": "2026-10-24 13:45:12",
     "Message": "Fehlerhafte Anwendung: chrome.exe, Fehlerhaftes Modul: "
                "ntdll.dll, Ausnahmecode: 0xc0000005",
     "Props": [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
               "141.0.7390.66", "68f2aabb", "ntdll.dll", "10.0.26100.1",
               "01234567", "0xc0000005"]},
])


# ---------------------------------------------------------------------------
# PowerShell output mocking (the selector the user asked for)
# ---------------------------------------------------------------------------


class TestCollectRecentCrashes:
    def test_event_1000_parses_message_fields(self, monkeypatch):
        """Event ID 1000: app/module/code parsed, exact schema + dedupe key.

        The 1001 record's P1 app is parsed too; records stay newest first.
        """
        monkeypatch.setattr(crashlog, "_run_powershell",
                            lambda script, timeout=8: (True, EVENT_1000_JSON, ""))
        crashes = crashlog.collect_recent_crashes(limit=5)
        assert [c["event_id"] for c in crashes] == [1000, 1001]
        crash = crashes[0]
        assert set(crash) == {"event_id", "timestamp", "app",
                              "faulting_module", "exception_code",
                              "message", "key"}
        assert crash["app"] == "chrome.exe"
        assert crash["faulting_module"] == "ntdll.dll"
        assert crash["exception_code"] == "0xc0000005"
        assert crash["timestamp"] == "2026-10-24 13:45:12"
        assert crash["key"] == "1000|2026-10-24 13:45:12|chrome.exe"
        assert crash["message"].startswith("Application: chrome.exe")
        # Event 1001 falls back to the rawWER payload (message has no path).
        assert crashes[1]["app"] == "notepad.exe"

    def test_localized_message_uses_event_payload_fallback(self, monkeypatch):
        """Localized (non-English) Event 1000 text: details still come from
        the raw wevt Properties array — app/module/code never invented."""
        monkeypatch.setattr(crashlog, "_run_powershell",
                            lambda script, timeout=8:
                            (True, LOCALIZED_EVENT_1000_JSON, ""))
        crashes = crashlog.collect_recent_crashes(limit=5)
        assert len(crashes) == 1
        crash = crashes[0]
        assert crash["app"] == "chrome.exe"
        assert crash["faulting_module"] == "ntdll.dll"
        assert crash["exception_code"] == "0xc0000005"

    def test_unknown_event_ids_dropped_and_single_record_unwrapped(self,
                                                                  monkeypatch):
        """Records whose Id is not in {1000,1001,1002} are filtered; a lone
        record (ConvertTo-Json unwraps the array to one object) still works."""
        single = json.dumps({
            "Id": 999, "Time": "2026-10-24 13:45:12", "Message": "unrelated",
            "Props": []})
        mixed = json.dumps([
            {"Id": 999, "Time": "2026-10-24 12:00:00", "Message": "x",
             "Props": []},
            {"Id": 1002, "Time": "2026-10-24 12:30:00", "Message": "", "Props":
             ["notepad.exe"]},
        ])
        outputs = iter([(True, mixed, ""), (True, single, "")])
        monkeypatch.setattr(crashlog, "_run_powershell",
                            lambda script, timeout=8: next(outputs))
        crashes = crashlog.collect_recent_crashes(limit=5)
        assert [c["event_id"] for c in crashes] == [1002]
        assert crashes[0]["app"] == "notepad.exe"
        assert crashes[0]["exception_code"] == ""

    def test_no_events_degrades_to_empty_list(self, monkeypatch):
        """'No events found' exits 0 with no output -> [] (not an error)."""
        monkeypatch.setattr(crashlog, "_run_powershell",
                            lambda script, timeout=8: (True, "", ""))
        assert crashlog.collect_recent_crashes(limit=5) == []
        assert crashlog.collect_unexpected_shutdowns(limit=3) == []

    def test_access_denied_degrades_and_availability_reports_it(self,
                                                                monkeypatch):
        """Non-admin / denied event log: collect_* never raise; availability
        degrades to (False, reason) so the UI can show UNAVAILABLE."""
        denied = (False, "",
                  "powershell exited with code 1: Get-WinEvent : Access is "
                  "denied")
        monkeypatch.setattr(crashlog, "_run_powershell",
                            lambda script, timeout=8: denied)
        assert crashlog.collect_recent_crashes(limit=5) == []
        ok, reason = crashlog.availability()
        assert ok is False
        assert "denied" in reason.lower()


# ---------------------------------------------------------------------------
# Shutdown records + invocation hardening + limits
# ---------------------------------------------------------------------------


class TestShutdownsAndInvocation:
    def test_event_41_shutdown_record_shape(self, monkeypatch):
        """Kernel-Power 41: app is 'Windows', module/code empty, message
        leads with the unexpected-shutdown explainer."""
        events = _event([
            {"Id": 41, "Time": "2026-10-23 02:14:09",
             "Message": "The system has rebooted without cleanly shutting "
                        "down first.", "Props": []},
        ])
        monkeypatch.setattr(crashlog, "_run_powershell",
                            lambda script, timeout=8: (True, events, ""))
        shutdowns = crashlog.collect_unexpected_shutdowns(limit=3)
        assert len(shutdowns) == 1
        record = shutdowns[0]
        assert record["event_id"] == 41
        assert record["app"] == "Windows"
        assert record["faulting_module"] == ""
        assert record["exception_code"] == ""
        assert record["message"].startswith(
            "Unexpected shutdown or power loss")

    def test_powershell_invocation_is_hardened_and_times_out(self,
                                                             monkeypatch):
        """Real _run_powershell call: explicit argv (no shell), -NoProfile,
        -NonInteractive, 8 s timeout, no console window on Windows; a
        TimeoutExpired degrades to an error instead of raising."""
        captured: dict = {}
        res = types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

        def fake_run(argv, **kwargs):
            captured.update(argv=argv, kwargs=kwargs)
            return res

        with patch.object(crashlog.subprocess, "run", side_effect=fake_run):
            ok, out, err = crashlog._run_powershell("Get-ChildItem C:/")
        assert ok is True and out == "" and err == ""
        argv = captured["argv"]
        assert isinstance(argv, list) and argv[0] == "powershell"
        assert "-NoProfile" in argv and "-NonInteractive" in argv
        assert argv[-1] == "Get-ChildItem C:/"
        kwargs = captured["kwargs"]
        assert kwargs.get("shell") is False
        assert kwargs.get("timeout") == 8
        assert kwargs.get("check") is False
        if sys.platform.startswith("win"):
            assert kwargs.get("creationflags") == subprocess.CREATE_NO_WINDOW

        with patch.object(crashlog.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired(
                              cmd="powershell", timeout=8.0)):
            ok, out, err = crashlog._run_powershell("whatever")
        assert ok is False and out == "" and "timed out" in err

    def test_limit_bounding_and_invalid_limit(self, monkeypatch):
        """limit<=0/invalid queries nothing and returns []; oversized limits
        are clamped to 50 events inside the emitted -FilterHashtable script."""
        calls: list[str] = []

        def fake_run(script, timeout=8):
            calls.append(script)
            return True, "", ""

        monkeypatch.setattr(crashlog, "_run_powershell", fake_run)
        assert crashlog.collect_recent_crashes(limit=0) == []
        assert crashlog.collect_recent_crashes(limit=-3) == []
        assert len(calls) == 0  # non-positive limits never query the log
        assert crashlog.collect_recent_crashes(limit="bogus") == []  # fallback
        assert len(calls) == 1 and "-MaxEvents 5" in calls[0]
        crashes = crashlog.collect_recent_crashes(limit=999)
        assert crashes == []
        assert len(calls) == 2 and "-MaxEvents 50" in calls[1]
        assert "ID=1000, 1001, 1002" in calls[0]
        # Shutdowns clamp independently (default 3, cap 50).
        calls.clear()
        assert crashlog.collect_unexpected_shutdowns(limit=999) == []
        assert len(calls) == 1
        assert "-MaxEvents 50" in calls[0]
        assert "ID=41" in calls[0]
        assert "LogName='System'" in calls[0]


# ---------------------------------------------------------------------------
# Explainer + tab row merging
# ---------------------------------------------------------------------------


class TestExplainerAndRowMerge:
    def test_explain_crash_ram_line_and_code_normalisation(self):
        """0xC0000005 with a pre-crash RAM snapshot >= 85% gets the exact
        'RAM was at 98%' line; decimal-spelled codes normalise from decimal
        (a hex read would misspell the NTSTATUS)."""
        snapshot = {"cpu": 43.0, "ram_percent": 98.2, "gpu_temp": 71,
                    "captured_at": "2026-10-24 13:45:00"}
        out = explain_crash({
            "event_id": 1000, "timestamp": "2026-10-24 13:45:12",
            "app": "chrome.exe", "faulting_module": "ntdll.dll",
            "exception_code": "0xc0000005", "message": "crash!", "key": "k"},
            snapshot)
        assert out is not None
        assert "Your RAM was at 98% when this happened." in out.explanation
        assert "chrome.exe" in out.explanation
        # Decimal string codes parse as DECIMAL, not hex:
        # 3221225545 == 0xC0000049.
        assert _normalise_exception_code("3221225545") == "0xC0000049"
        assert _normalise_exception_code("0xC0000005") == "0xC0000005"
        assert _normalise_exception_code(3221225477) == "0xC0000005"

    def test_crash_rows_merge_and_dedupe_saved_files(self, monkeypatch,
                                                     tmp_path):
        """The Crash Log tab merges crashes/crash_*.json records that the
        background watcher saved: identical event-log rows are de-duplicated
        (and upgraded with the stored snapshot), other files become rows."""
        crash_json = {"event_id": 1000, "timestamp": "2026-10-24 13:45:12",
                      "app": "chrome.exe", "faulting_module": "ntdll.dll",
                      "exception_code": "0xc0000005", "message": "boom!",
                      "key": "1000|2026-10-24 13:45:12|chrome.exe"}
        hang_json = {"event_id": 1002, "timestamp": "2026-10-24 15:02:00",
                     "app": "notepad.exe", "faulting_module": "",
                     "exception_code": "", "message": "hang", "key": "k2"}
        snapshot = {"cpu": 88.0, "ram_percent": 97.0, "gpu_temp": None,
                    "captured_at": "2026-10-24 13:45:00"}
        (tmp_path / "crash_20261024_134512.json").write_text(json.dumps({
            "saved_at": "2026-10-24 13:45:30", "crash": crash_json,
            "snapshot": snapshot}), encoding="utf-8")
        (tmp_path / "crash_20261024_150200.json").write_text(json.dumps({
            "saved_at": "2026-10-24 15:02:01", "crash": hang_json,
            "snapshot": None}), encoding="utf-8")
        (tmp_path / "crash_broken.json").write_text("{not json",
                                                    encoding="utf-8")

        fake_crashlog = types.SimpleNamespace(
            default_crashes_dir=lambda: tmp_path,
            availability=lambda: (True, ""),
            collect_recent_crashes=lambda limit: [crash_json],
            collect_unexpected_shutdowns=lambda limit: [])
        monkeypatch.setattr(scanner_app, "_crashlog", fake_crashlog)
        monkeypatch.setattr(scanner_app, "_crash_dirs", lambda: [tmp_path])

        rows, note = scanner_app._collect_crash_rows()
        real_rows = [r for r in rows if not r["status_row"]]
        assert [r["crash"]["key"] for r in real_rows] == \
            ["k2", "1000|2026-10-24 13:45:12|chrome.exe"]  # newest first
        chrome_row = next(r for r in real_rows
                          if r["crash"]["app"] == "chrome.exe")
        assert chrome_row["snapshot"] == snapshot  # snapshot upgraded in
        notepad_row = next(r for r in real_rows
                           if r["crash"]["app"] == "notepad.exe")
        assert notepad_row["snapshot"] is None
        assert "crash(es)" in note and "saved file record(s) merged" in note
        # Exactly one row per crash: no duplicate from the twin event log
        # record that the fake collector also returns.
        assert len(real_rows) == 2
