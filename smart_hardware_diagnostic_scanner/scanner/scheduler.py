"""Pure builders for the optional weekly headless scan (schtasks on Windows, cron elsewhere).

No I/O happens here and no subprocess is ever started by this module: the GUI
composes the argv/lines with these helpers and owns running (and removing) them.
"""
from __future__ import annotations

import logging
import shlex

log = logging.getLogger("shds.scheduler")

SCHEDULE_MARKER = "# SHDS weekly scan (added by Smart Hardware Diagnostic Scanner)"
VALID_DAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")

SCHTASKS_TASK_NAME = "SHDS weekly scan"  # /TN value used by the Windows builder


def validate_time(value: str) -> bool:
    """Strict "HH:MM" 24h check (00:00-23:59); no leniency for "9:00" or "24:00"."""
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        return False
    hour_text, minute_text = value[:2], value[3:]
    if not all(ch in "0123456789" for ch in hour_text + minute_text):
        return False
    return int(hour_text) <= 23 and int(minute_text) <= 59


def _check_day(day: str) -> str:
    """Return the day normalized to uppercase; raises ValueError when invalid."""
    if not isinstance(day, str) or day.strip().upper() not in VALID_DAYS:
        log.warning("invalid schedule day: %r", day)
        raise ValueError(f"day must be one of: {', '.join(VALID_DAYS)} (got {day!r})")
    return day.strip().upper()


def day_to_cron_dow(day: str) -> int:
    """Map a day name to cron day-of-week (MON=1 ... SAT=6, SUN=0). Raises ValueError."""
    key = _check_day(day)
    return 0 if key == "SUN" else VALID_DAYS.index(key) + 1


def scan_args() -> list[str]:
    """Arguments for a headless scan run, shared by every scheduler backend."""
    return ["--scan", "--quiet", "--no-history"]


def _check_day_and_time(day: str, when: str) -> str:
    """Validate both schedule inputs for the builders; returns the normalized day."""
    key = _check_day(day)
    if not validate_time(when):
        log.warning("invalid schedule time: %r", when)
        raise ValueError(f"when must be strict 24h HH:MM between 00:00 and 23:59 (got {when!r})")
    return key


def build_schtasks_argv(python_exe: str, script: str, day: str = "MON", when: str = "09:00") -> list[str]:
    """Build the "schtasks /Create" argv for a weekly scan (caller runs it with shell=False).

    The /TR command keeps embedded quotes on purpose so paths with spaces
    survive the single /TR argument. Raises ValueError for a bad day or time.
    """
    key = _check_day_and_time(day, when)
    quoted_command = f'"{python_exe}" "{script}" ' + " ".join(scan_args())
    return [
        "schtasks", "/Create", "/F",
        "/TN", SCHTASKS_TASK_NAME,
        "/SC", "WEEKLY",
        "/D", key,
        "/ST", when,
        "/TR", quoted_command,
    ]


def _cron_token(value: str) -> str:
    """One shell- and cron-safe token for an executable/script path.

    cron splits the command on whitespace, so a path containing a space must
    be quoted or the schedule silently breaks. Newlines are rejected outright:
    they would inject a second, unmarked crontab line that the removal logic
    could never clean up. '%' is escaped because cron itself turns it into a
    newline.
    """
    text = str(value)
    if "\n" in text or "\r" in text:
        log.warning("rejecting path containing a newline: %r", value)
        raise ValueError(f"path must not contain newlines (got {value!r})")
    return shlex.quote(text.replace("%", r"\%"))


def build_cron_line(python_exe: str, script: str, day: str = "MON", when: str = "09:00") -> str:
    """Build one crontab line for the weekly scan (marker at the end so removal finds it).

    Raises ValueError for a bad day or time, or for a path that cannot be
    expressed safely in a crontab line.
    """
    key = _check_day_and_time(day, when)
    hour_text, minute_text = when.split(":", 1)
    hour, minute = int(hour_text), int(minute_text)
    dow = day_to_cron_dow(key)
    body = " ".join(scan_args())
    return (f"{minute} {hour} * * {dow} {_cron_token(python_exe)} "
            f"{_cron_token(script)} {body} {SCHEDULE_MARKER}")


def strip_scheduled_entries(text: str) -> tuple[str, int]:
    """Remove every line containing SCHEDULE_MARKER; return (cleaned_text, removed_count).

    All other lines are preserved as-is (line ending style included); trailing
    blank lines left behind by removals are collapsed.
    """
    if not isinstance(text, str):
        return "", 0
    kept: list[str] = []
    removed = 0
    for line in text.split("\n"):
        if SCHEDULE_MARKER in line:
            removed += 1
            continue
        kept.append(line)
    if removed:
        while kept and not kept[-1].strip():
            kept.pop()
    return "\n".join(kept), removed


def merge_crontab(existing: str | None) -> str:
    """Normalize an existing crontab so the caller can append a fresh scan line.

    None/empty means no crontab existed and yields "". Otherwise old scheduled
    scan entries are stripped and the remaining jobs are kept intact.
    """
    if not isinstance(existing, str) or not existing.strip():
        return ""
    cleaned, _removed = strip_scheduled_entries(existing)
    return cleaned
