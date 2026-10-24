"""Plain-English explanations and safe fixes for diagnostic findings.

The explainer never runs anything. It turns a FAIL/WARNING ``Finding`` into an
:class:`Explanation` whose ``kind`` tells the UI what the "How to fix" button
may do:

* ``uri``     - open a known operating-system settings page (Windows
                ``ms-settings:`` URIs). Opening a page is read-only.
* ``command`` - copy a safe, read-only command to the clipboard so the user
                can inspect the problem and run it manually if they choose.
* ``info``    - show an explanation only; there is no safe automated action.

Matching is code-first (``Finding.code`` from the diagnostics engine) and then
pattern-based, so older or third-party findings still get a useful answer.
"""
from __future__ import annotations

import platform
from dataclasses import dataclass
from typing import Callable

EXPLAINED_STATUSES = ("FAIL", "WARNING")

_STORAGE_SETTINGS_MAC = 'open "x-apple.systempreferences:com.apple.settings.Storage"'
_MACOS_LOG_COMMAND = ('log show --predicate \'eventMessage contains "error"\' '
                      "--last 1h")


@dataclass(frozen=True)
class Explanation:
    """A single plain-English explanation plus one safe, user-run action."""

    title: str
    explanation: str
    fix: str
    kind: str = "info"  # "uri" | "command" | "info"
    target: str = ""    # settings URI or command to copy


def _platform_name(system: str | None) -> str:
    """Normalise a platform name to "Windows", "Linux" or "Darwin"."""
    text = str(system or "").strip().lower()
    if text.startswith("win"):
        return "Windows"
    if text.startswith("mac") or text in ("darwin", "osx"):
        return "Darwin"
    if text.startswith("linux"):
        return "Linux"
    return str(system or "")


# ---------------------------------------------------------------------------
# Builders. They are intentionally small and pure so the UI can call them on
# the main thread without doing any I/O.
# ---------------------------------------------------------------------------
def _windows_update(_system: str = "") -> Explanation:
    return Explanation(
        "Windows Update service is stopped",
        "Windows Update uses the 'wuauserv' service to download and install "
        "fixes. When it is stopped, security updates can be delayed. Windows "
        "also starts this service on demand, so 'stopped' alone is a warning "
        "worth checking — not proof that updates are broken.",
        "Open Windows Update settings and choose 'Check for updates'. The "
        "scanner did not start or stop any service.",
        "uri", "ms-settings:windowsupdate")


def _defender(_system: str = "") -> Explanation:
    return Explanation(
        "Windows Defender real-time protection is off",
        "Real-time protection scans files as they are opened and blocks known "
        "malware. It can be off deliberately (another antivirus is installed) "
        "or accidentally after a policy change.",
        "Open Windows Security > Virus & threat protection and review the "
        "setting. The scanner changes nothing.",
        "uri", "ms-settings:windowsdefender")


def _system_restore(_system: str = "") -> Explanation:
    return Explanation(
        "System Restore is turned off",
        "Restore points let Windows undo a bad driver or update without "
        "deleting personal files. With System Restore off, that rollback "
        "option is missing when something breaks.",
        "The button copies a safe command to your clipboard. Run "
        "'SystemPropertiesProtection.exe' yourself (or search 'Create a "
        "restore point') and switch protection on for the system drive. The "
        "scanner cannot change this setting.",
        "command", "SystemPropertiesProtection.exe")


def _kernel_errors(system: str = "") -> Explanation:
    target = (_MACOS_LOG_COMMAND if _platform_name(system) == "Darwin"
              else "dmesg --level=err,warn")
    return Explanation(
        "Kernel log reports hardware or storage errors",
        "The kernel log contains I/O errors, a segmentation fault or a "
        "hardware machine-check message. Repeated 'I/O error' lines often "
        "mean a failing cable, SSD/HDD or file system; 'segfault' lines are "
        "usually a crashing application rather than broken RAM.",
        "The button copies a read-only log command. Back up important files "
        "first if disk errors repeat, and check the drive's SMART data. The "
        "scanner only reads the log.",
        "command", target)


def _inodes(_system: str = "") -> Explanation:
    return Explanation(
        "Running out of inodes (file records)",
        "Every file needs an inode — a small metadata record. A volume can "
        "show plenty of free space and still refuse to create files when it "
        "holds millions of tiny files: caches, logs, mail or build folders "
        "are the usual culprits.",
        "The button copies 'df -i', which lists inode usage per volume. Move "
        "or delete old caches/build folders once you know what is safe. The "
        "scanner deletes nothing.",
        "command", "df -i")


def _systemd(_system: str = "") -> Explanation:
    return Explanation(
        "A systemd service failed",
        "One or more services did not start or crashed. The failed unit is "
        "often a driver helper, VPN or update service, so the machine can "
        "look fine while that feature is silently broken.",
        "The button copies 'systemctl --failed' (read-only) so you can see "
        "the unit names; then use 'journalctl -u <unit>' for the reason. The "
        "scanner starts and stops nothing.",
        "command", "systemctl --failed")


def _macos_log(_system: str = "") -> Explanation:
    return Explanation(
        "macOS log contains recent errors",
        "The unified log recorded messages containing 'error' in the last "
        "hour. Many are harmless app-level complaints, but repeated disk, "
        "network or graphics errors can explain crashes and slow-downs.",
        "The button copies the same read-only query so you can inspect the "
        "lines yourself. Look for repeated patterns rather than single "
        "messages. The scanner changes nothing.",
        "command", _MACOS_LOG_COMMAND)


def _time_machine(_system: str = "") -> Explanation:
    return Explanation(
        "Time Machine backups are stale or missing",
        "Time Machine is macOS's built-in backup. If the last successful "
        "backup is more than a week old (or missing), a disk failure or an "
        "accidental deletion could lose recent work. Common causes are an "
        "unplugged backup disk, a full destination or a failed scheduled run.",
        "The button copies 'tmutil startbackup', which starts a backup when "
        "you run it. Check the destination in Time Machine settings first if "
        "it keeps failing. The scanner never starts a backup by itself.",
        "command", "tmutil startbackup")


def _disk(system: str = "") -> Explanation:
    explanation = (
        "A nearly full disk slows the whole machine: the operating system "
        "needs free space for its page/swap file, temporary files, update "
        "staging and file-system bookkeeping. When space runs low, apps wait "
        "on the disk and saves can fail. This is a capacity problem, not "
        "proof that the drive is damaged.")
    name = _platform_name(system)
    if name == "Windows":
        return Explanation(
            "Disk is nearly full", explanation,
            "Open Windows Storage Sense to review what is using space and "
            "remove temporary files. Nothing is deleted until you confirm.",
            "uri", "ms-settings:storagesense")
    if name == "Darwin":
        return Explanation(
            "Disk is nearly full", explanation,
            "The button copies a command that opens Storage settings, where "
            "macOS suggests large files to review. Nothing is deleted "
            "automatically.",
            "command", _STORAGE_SETTINGS_MAC)
    return Explanation(
        "Disk is nearly full", explanation,
        "The button copies 'baobab', a graphical disk-usage analyser "
        "(install gnome-disk-utility if it is missing). Review large folders "
        "before deleting anything. The scanner deletes nothing.",
        "command", "baobab")


def _cpu(_system: str = "") -> Explanation:
    return Explanation(
        "High CPU load",
        "Sustained high CPU means programs are competing for the processor. "
        "The machine gets hot, fans speed up and everything lags; it does not "
        "mean the CPU is damaged. A single runaway process is the most common "
        "cause.",
        "Open Task Manager (Ctrl+Shift+Esc on Windows), Activity Monitor "
        "(macOS) or top (Linux), sort by CPU and close what you recognise. "
        "The scanner does not terminate processes.",
        "info")


def _memory(_system: str = "") -> Explanation:
    return Explanation(
        "High memory use",
        "When RAM fills up, the operating system swaps data to disk, which is "
        "much slower — the machine stutters and apps may be killed. Browser "
        "tabs and Electron apps are common causes, and a leak grows over "
        "time.",
        "Close unused tabs/apps and save your work; restart the app that keeps "
        "growing. Consider more RAM only if this is chronic. The scanner "
        "changes nothing.",
        "info")


def _gpu_hot(_system: str = "") -> Explanation:
    return Explanation(
        "GPU temperature is high",
        "A hot GPU throttles its clock to protect itself, so games and "
        "rendering slow down and the fans get loud. Dust, blocked vents or a "
        "heavy workload are common causes.",
        "Close GPU-heavy apps, clear the vents and check airflow. Persistent "
        "overheating may need service. The scanner changes nothing.",
        "info")


def _access_denied(system: str = "") -> Explanation:
    admin = "Administrator" if _platform_name(system) == "Windows" else "elevated rights"
    return Explanation(
        "Administrator rights needed",
        "The operating system protects this reading (sensor, system setting or "
        "service list) behind elevated rights. The scanner reports it as "
        "unavailable instead of inventing a value — 'Access Denied' is a "
        "permission wall, not a hardware fault.",
        f"If you trust this app, re-run it with {admin} ('Run as "
        "administrator' on Windows, sudo on Linux/macOS) to read that sensor. "
        "Otherwise treat the value as unknown. The scanner never elevates "
        "itself.",
        "info")


def _generic(finding) -> Explanation:
    component = str(getattr(finding, "component", "Check") or "Check")
    message = str(getattr(finding, "message", "") or "did not pass.")
    return Explanation(
        f"{component}: {message}",
        "This check did not pass. The scanner only reads data, so nothing has "
        "been changed on your machine. A single failed check is a hint to "
        "investigate, not a diagnosis.",
        "Review the detail in the scan output; if the problem persists, use "
        "the operating system's own settings or the hardware vendor's tool. "
        "No automatic fix is available for this item.",
        "info")


_CODE_BUILDERS: dict[str, Callable[[str], Explanation]] = {
    "WINDOWS_UPDATE_STOPPED": _windows_update,
    "DEFENDER_DISABLED": _defender,
    "SYSTEM_RESTORE_OFF": _system_restore,
    "KERNEL_HARDWARE_ERROR": _kernel_errors,
    "INODES_LOW": _inodes,
    "INODES_CRITICAL": _inodes,
    "SYSTEMD_FAILED": _systemd,
    "MACOS_LOG_ERRORS": _macos_log,
    "TIMEMACHINE_STALE": _time_machine,
    "TIMEMACHINE_NO_BACKUP": _time_machine,
    "DISK_FULL": _disk,
    "DISK_LOW": _disk,
    "CPU_HIGH": _cpu,
    "MEM_HIGH": _memory,
    "GPU_HOT": _gpu_hot,
    "ACCESS_DENIED": _access_denied,
}


def known_codes() -> frozenset[str]:
    """Codes that have a dedicated explanation (used by tests and docs)."""
    return frozenset(_CODE_BUILDERS)


def _from_patterns(text: str, system: str) -> Explanation | None:
    """Fallback for findings without a known code (or from older exports)."""
    if any(marker in text for marker in (
            "access denied", "access is denied", "permission denied",
            "permissionerror", "operation not permitted", "privilege")):
        return _access_denied(system)
    if any(marker in text for marker in (
            "i/o error", "hardware error", "machine check", "segfault",
            "ext4-fs error", "xfs (", "btrfs")):
        return _kernel_errors(system)
    if "inode" in text or "df -i" in text:
        return _inodes(system)
    if "wuauserv" in text or "windows update" in text:
        return _windows_update(system)
    if "defender" in text:
        return _defender(system)
    if "system restore" in text or "disablesr" in text:
        return _system_restore(system)
    if "systemd" in text or "failed unit" in text or "systemctl" in text:
        return _systemd(system)
    if "time machine" in text or "timemachine" in text or "tmutil" in text:
        return _time_machine(system)
    if ("storage" in text or "disk" in text or "volume" in text
            or "partition" in text):
        return _disk(system)
    if "cpu" in text:
        return _cpu(system)
    if "memory" in text:
        return _memory(system)
    if "gpu" in text:
        return _gpu_hot(system)
    return None


# ---------------------------------------------------------------------------
# Crash explainers. Records come from scanner.crashlog (Windows event log)
# and from the crashes/crash_*.json files the background snapshotter writes;
# the optional ``snapshot`` argument is one such file's pre-crash hardware
# sample ({"cpu", "ram_percent", "gpu_temp", "captured_at"}).
# ---------------------------------------------------------------------------
#: Plain-English lines for the Windows exception codes that turn up in
#: application-crash records (event 1000). Keys are normalised through
#: `_normalise_exception_code`: upper-case, 0x-prefixed hex. Every value is a
#: standalone sentence so it can follow any lead-in. Codes outside this map
#: fall through to the generic explanation instead of being guessed at.
EXCEPTION_CODE_TEXT: dict[str, str] = {
    "0xC0000005": (
        "The process crashed because it tried to read or write memory it "
        "was not allowed to touch (access violation). This is the most "
        "common crash code and does not, by itself, prove faulty RAM."),
    "0x80000003": (
        "The process hit a low-level breakpoint (STATUS_BREAKPOINT). This "
        "is usually a program or its protection layer stopping itself, "
        "not a hardware fault."),
    "0xC0000094": (
        "The process divided an integer by zero — a plain programming bug "
        "in the application or one of its components."),
    "0xC0000096": (
        "The process attempted a privileged CPU instruction it was not "
        "allowed to execute."),
    "0xC00000FD": (
        "The process ran out of stack space (stack overflow), usually from "
        "endless recursion inside the program."),
    "0xC0000409": (
        "The process detected that a stack guard value had been overrun "
        "(stack buffer overrun / fail-fast) and stopped itself rather than "
        "continue with a corrupted stack."),
    "0xC0000135": (
        "The application could not start because a DLL (dynamic-link "
        "library) it needs is missing."),
    "0xC0000142": (
        "The application could not start because one of its DLLs failed to "
        "initialise — often after a half-applied update or a broken runtime "
        "installation."),
    "0xC0000006": (
        "The process could not read back data it had paged out to disk "
        "(in-page error); failing sectors or a disturbed drive fit this "
        "pattern."),
    "0xC000027B": (
        "The app failed with a packaged (WinRT) 'stowed exception', which "
        "is how Microsoft Store apps surface an unhandled error."),
    "0xE0434352": (
        "The process died from an unhandled .NET runtime exception."),
    "0xE06D7363": (
        "The process died from an unhandled Microsoft Visual C++ "
        "exception."),
}

#: Core Windows modules: an access violation inside one of these is the
#: documented trigger for suggesting a System File Checker run. Deliberately
#: short and well-known names only — nothing is inferred about other files.
_WINDOWS_SYSTEM_MODULES = frozenset({
    "ntdll.dll", "kernel32.dll", "kernelbase.dll", "user32.dll",
    "gdi32.dll", "advapi32.dll", "combase.dll", "ole32.dll", "oleaut32.dll",
    "shell32.dll", "msvcrt.dll", "shlwapi.dll", "ws2_32.dll", "win32u.dll",
    "uxtheme.dll", "ntoskrnl.exe",
})

_SHUTDOWN_EVENT_TEXT = (
    "Windows shut down unexpectedly (power loss or forced power-off).")


def known_exception_codes() -> frozenset[str]:
    """Exception codes with a dedicated line (parity with `known_codes`)."""
    return frozenset(EXCEPTION_CODE_TEXT)


def known_windows_system_modules() -> frozenset[str]:
    """Core Windows modules the SFC advice recognises (docs and tests)."""
    return frozenset(_WINDOWS_SYSTEM_MODULES)


def _normalise_exception_code(value) -> str:
    """Return '0x'-prefixed upper-case hex for an exception code, or "".

    Accepts ints (0xC0000005 == 3221225477) and string spellings: hex with or
    without the 0x prefix ("0xc0000005", "C0000005") and plain decimal
    ("3221225477"). Digits-only strings read as decimal unless a 0x prefix
    says otherwise, because a hex code with no a–f digit is indistinguishable
    from an ordinary number. Unparseable values stay unknown — the explainer
    never guesses.
    """
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return f"0x{value & 0xFFFFFFFF:08X}"
    raw = str(value).strip().lower()
    if not raw:
        return ""
    prefixed = raw.startswith("0x")
    text = raw[2:] if prefixed else raw.replace(" ", "").replace("_", "")
    if not text:
        return ""
    try:
        # Hex only when spelled out (0x prefix) or when a–f digits appear;
        # otherwise digits-only text is read as decimal (NTSTATUS codes also
        # surface as decimal strings in the event log).
        if prefixed or any(ch in text for ch in "abcdef"):
            parsed = int(text, 16)
        elif text.isdigit():
            parsed = int(text, 10)
        else:
            return ""
    except ValueError:
        return ""
    return f"0x{parsed & 0xFFFFFFFF:08X}"


def _crash_event_id(crash: dict) -> int | None:
    """Event id as int, or None when the record does not carry one."""
    try:
        return int(crash.get("event_id"))
    except (TypeError, ValueError, AttributeError):
        return None


def _crash_app_name(crash: dict) -> str:
    """App name with an honest fallback (never a guessed program)."""
    return str(crash.get("app") or "").strip() or "The application"


def _snapshot_ram_percent(snapshot: dict | None) -> float | None:
    """Recorded RAM percent from a pre-crash snapshot dict, or None."""
    if not isinstance(snapshot, dict):
        return None
    for key in ("ram_percent", "ram", "mem_percent"):
        value = snapshot.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def _crash_lead(app: str, event_id: int | None) -> tuple[str, str]:
    """Title and opening sentence for one stopped-application record."""
    if event_id == 1002:
        return (f"Hang: {app}",
                f"{app} stopped responding (Windows event 1002) and was "
                "closed or had to be restarted.")
    if event_id == 1001:
        return (f"Crash report: {app}",
                f"Windows Error Reporting recorded a crash report for "
                f"{app} (event 1001).")
    title = f"Crash: {app}"
    if event_id is not None:
        return title, f"{app} stopped working unexpectedly (event {event_id})."
    return title, f"{app} stopped working unexpectedly."


def _crash_fix_text(app: str, code: str, module: str, ram: float | None,
                    event_id: int | None) -> str:
    """Compose generic-but-grounded 'How to fix' steps from the record.

    Only fields the record actually carries are referenced; nothing about
    the user's machine is invented, and the scanner never runs any of it.
    """
    steps: list[str] = []
    if event_id == 41:
        steps.append(
            "Check whether power was lost or the machine was switched off "
            "while it was running (held power button, battery change, wall "
            "switch or outage). If it repeats with no obvious power cause, "
            "run 'sfc /scannow' in an administrator terminal and check the "
            "drive's SMART health.")
    if ram is not None and ram >= 85.0:
        steps.append(
            f"Close memory-heavy programs before trying {app} again — the "
            f"record shows RAM was at {ram:.0f}% in the seconds before the "
            "crash.")
    if event_id != 41:
        if module:
            steps.append(
                f"Update or reinstall the software package that owns "
                f"'{module}': the record names it as the faulting module, "
                "so that package is the first thing to repair.")
        else:
            steps.append(
                f"Update or reinstall {app} from its official source "
                "before anything else.")
    if code == "0xC0000005" and module.lower() in _WINDOWS_SYSTEM_MODULES:
        steps.append(
            f"An access violation inside the Windows module '{module}' is "
            "worth checking with System File Checker: open an "
            "'Administrator' terminal and run 'sfc /scannow'. It takes "
            "several minutes; the scanner never runs it for you.")
    if code in ("0xC0000135", "0xC0000142"):
        steps.append(
            "This start-up failure points at a missing or broken runtime "
            "component: use the 'repair' mode of the installer that "
            "provides the DLL named in the row before reinstalling the "
            "whole application.")
    steps.append(
        "Keep free disk space on the system drive and install pending "
        "Windows and driver updates. These are the standard first steps "
        "for this record — nothing else was changed by the scanner.")
    return " ".join(steps)


def explain_crash(crash: dict | None,
                  snapshot: dict | None = None) -> Explanation | None:
    """Explain one crash record from the Windows event log, in plain English.

    ``crash`` uses the dict shape produced by scanner.crashlog (event_id,
    timestamp, app, faulting_module, exception_code, message, key);
    ``snapshot`` is the optional pre-crash hardware sample saved beside it
    in crashes/crash_*.json. Returns ``None`` for empty input — no invented
    explanations — and performs no I/O; the result is deterministic.

    Dedicated lines exist for the common exception codes (0xC0000005,
    0x80000003, 0xC0000094, 0xC0000409, …) and for the unexpected-shutdown
    event (41); an access violation adds the recorded RAM percent when the
    snapshot shows 85% or more. Everything else falls back to a generic
    explanation — pattern-matching the raw message (via `explain_finding`)
    is skipped on purpose, because crash text often mentions subsystem
    words ("disk", "memory") that would produce unrelated hardware advice.
    """
    if not crash:
        return None
    app = _crash_app_name(crash)
    event_id = _crash_event_id(crash)
    code = _normalise_exception_code(crash.get("exception_code"))
    module = str(crash.get("faulting_module") or "").strip()
    ram = _snapshot_ram_percent(snapshot)

    if event_id == 41:  # scanner.crashlog.SHUTDOWN_EVENT_ID
        return Explanation(
            "Windows — unexpected shutdown",
            _SHUTDOWN_EVENT_TEXT
            + " Windows also logs it after a held power button or an outage "
              "during use; if the machine was frozen first, the frozen "
              "program is the starting point, not the power supply.",
            _crash_fix_text(app, code, module, ram, event_id=event_id),
            "info")

    title, lead = _crash_lead(app, event_id)
    parts = [lead]
    if module:
        parts.append(f"Windows recorded '{module}' as the faulting module.")
    code_line = EXCEPTION_CODE_TEXT.get(code)
    if code_line is not None:
        parts.append(code_line)
        if code == "0xC0000005" and ram is not None and ram >= 85.0:
            parts.append(f"Your RAM was at {ram:.0f}% when this happened.")
    elif code:
        parts.append(
            f"The record lists exception code {code}; no dedicated "
            "explanation is available for it yet.")
    else:
        # Neither a mapped code nor a code field at all (e.g. a bare hang
        # or WER record): honest generic text instead of a guess.
        parts.append(
            "This record carries no exception code, so there is no "
            "dedicated explanation for it yet. The scanner only reads "
            "data; it changes nothing.")
    return Explanation(
        title, " ".join(part.strip() for part in parts if part.strip()),
        _crash_fix_text(app, code, module, ram, event_id=event_id),
        "info")


def explain_finding(finding, system: str | None = None) -> Explanation | None:
    """Explain a FAIL/WARNING finding; return None for anything else.

    ``system`` defaults to ``platform.system()`` so the disk/access-denied
    fixes name the right settings. The result is deterministic and performs
    no I/O; opening a URI or copying a command is the caller's job.
    """
    if finding is None:
        return None
    status = str(getattr(finding, "status", "") or "").upper()
    if status not in EXPLAINED_STATUSES:
        return None
    name = _platform_name(system if system is not None else platform.system())

    code = str(getattr(finding, "code", "") or "").strip().upper()
    builder = _CODE_BUILDERS.get(code)
    if builder is not None:
        return builder(name)

    text = " ".join(
        str(getattr(finding, attr, "") or "")
        for attr in ("component", "message", "detail", "key")).lower()
    matched = _from_patterns(text, name)
    if matched is not None:
        return matched
    return _generic(finding)
