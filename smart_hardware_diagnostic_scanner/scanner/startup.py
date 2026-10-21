"""OS startup-entry enumeration (read-only, cross-OS, fully defensive).

Nothing here disables, deletes, or modifies any startup item — the scanner
only reports what it finds. Every public function is safe to call from a
background thread and never raises: failures degrade into notes.
"""
from __future__ import annotations

import logging
import os
import plistlib
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

try:
    import winreg
    HAS_WINREG = True
except ImportError:  # Windows-only stdlib module
    winreg = None  # type: ignore
    HAS_WINREG = False

log = logging.getLogger("shds.startup")

_RUN_SUBKEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUNONCE_SUBKEY = r"Software\Microsoft\Windows\CurrentVersion\RunOnce"
_DESKTOP_ENTRY_SECTION = "Desktop Entry"

# Display order and labels for the per-kind counts in the summary note.
_KIND_ORDER = ("registry", "autostart", "launchagent", "startup-folder")
_KIND_LABELS = {
    "registry": "registry",
    "autostart": "autostart",
    "launchagent": "launchagent",
    "startup-folder": "folder",
}


@dataclass
class StartupEntry:
    """One startup location found on the OS (reported, never modified)."""

    name: str = ""      # human label
    command: str = ""   # what is launched (path or Exec value)
    source: str = ""    # where it was found: registry key path or file path
    kind: str = ""      # "registry" | "startup-folder" | "autostart" | "launchagent"
    note: str = ""      # extra info (e.g. "hidden" from a desktop file)


def _win_registry_entries() -> tuple[list[StartupEntry], str]:
    """Read Run/RunOnce values under HKCU and HKLM (strictly read-only).

    Never raises: unreadable keys degrade to a note, missing keys are normal.
    """
    if not HAS_WINREG:
        return [], "winreg unavailable (not Windows) — registry entries skipped."
    entries: list[StartupEntry] = []
    errors: list[str] = []
    hives = (
        ("HKCU", winreg.HKEY_CURRENT_USER),
        ("HKLM", winreg.HKEY_LOCAL_MACHINE),
    )
    for hive_name, hive in hives:
        for subkey in (_RUN_SUBKEY, _RUNONCE_SUBKEY):
            full = f"{hive_name}\\{subkey}"
            # Read BOTH registry views: on 64-bit Python the default view is
            # the 64-bit one, so autoruns installed by 32-bit applications
            # (Wow6432Node) would otherwise never be listed.
            views = [(0, "")]
            for flag, label in ((getattr(winreg, "KEY_WOW64_64KEY", 0), " (64-bit)"),
                                (getattr(winreg, "KEY_WOW64_32KEY", 0), " (32-bit)")):
                if flag:
                    views.append((flag, label))
            for view_flag, view_label in views:
                try:
                    with winreg.OpenKey(hive, subkey, 0,
                                        winreg.KEY_READ | view_flag) as key:
                        index = 0
                        while True:
                            try:
                                name, command, _vtype = winreg.EnumValue(key, index)
                            except OSError:
                                break
                            index += 1
                            entries.append(StartupEntry(
                                name=str(name or ""),
                                command=str(command or "").strip(),
                                source=full + view_label,
                                kind="registry",
                            ))
                except FileNotFoundError:
                    continue  # Run/RunOnce absent for this hive — normal
                except OSError as exc:
                    errors.append(f"{full}{view_label}: {exc}")
                    log.info("cannot read registry key %s: %s", full, exc)
    return entries, "; ".join(errors)


def _win_startup_folder_entries() -> tuple[list[StartupEntry], str]:
    """List files in the per-user Start Menu Startup folder (read-only).

    Never raises: a missing APPDATA variable or an unreadable folder degrades
    to a note; a folder that simply does not exist is not an error.
    """
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return [], "APPDATA environment variable not set — startup folder skipped."
    folder = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    try:
        paths = sorted(folder.iterdir())
    except FileNotFoundError:
        return [], ""  # folder does not exist yet — nothing to report
    except OSError as exc:
        return [], f"Cannot read startup folder {folder}: {exc}"
    entries: list[StartupEntry] = []
    for path in paths:
        try:
            if path.is_symlink() or not path.is_file():
                continue
        except OSError:
            continue
        entries.append(StartupEntry(
            name=path.stem,
            command=path.name,
            source=str(path),
            kind="startup-folder",
        ))
    return entries, ""


def _parse_desktop_file(path: Path) -> dict[str, str] | None:
    """Minimal forgiving parse of a .desktop file's [Desktop Entry] section.

    Handles a UTF-8 BOM, skips comments/blank lines and any other section,
    and keeps the first value per key. Returns None for unreadable or
    malformed files (no [Desktop Entry] header at all).
    """
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        log.debug("cannot read desktop file %s: %s", path, exc)
        return None
    keys: dict[str, str] = {}
    section: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        if section != _DESKTOP_ENTRY_SECTION or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key:
            keys.setdefault(key, value.strip())
    if not keys:
        return None
    return keys


def _autostart_entries(directories: list[Path]) -> list[StartupEntry]:
    """Parse *.desktop files in the given XDG autostart dirs (read-only).

    Missing or unreadable directories simply contribute no entries; malformed
    desktop files are skipped with a debug log. Never raises.
    """
    entries: list[StartupEntry] = []
    for directory in directories:
        try:
            paths = sorted(directory.glob("*.desktop"))
        except OSError as exc:
            log.debug("cannot list autostart dir %s: %s", directory, exc)
            continue
        for path in paths:
            try:
                if not path.is_file():
                    continue
            except OSError:
                continue
            keys = _parse_desktop_file(path)
            if keys is None:
                log.debug("ignoring malformed desktop file: %s", path)
                continue
            note = ""
            if str(keys.get("Hidden", "")).strip().lower() == "true":
                note = "hidden"
            elif str(keys.get("NoDisplay", "")).strip().lower() == "true":
                note = "not shown"
            entries.append(StartupEntry(
                name=keys.get("Name") or path.stem,
                command=keys.get("Exec", ""),
                source=str(path),
                kind="autostart",
                note=note,
            ))
    return entries


def _launchagent_entries(directories: list[Path]) -> list[StartupEntry]:
    """Parse *.plist LaunchAgents in the given directories (read-only).

    Missing directories contribute nothing; malformed or non-dict plists are
    skipped with a debug log. Never raises.
    """
    entries: list[StartupEntry] = []
    for directory in directories:
        try:
            paths = sorted(directory.glob("*.plist"))
        except OSError as exc:
            log.debug("cannot list LaunchAgents dir %s: %s", directory, exc)
            continue
        for path in paths:
            try:
                if not path.is_file():
                    continue
                with path.open("rb") as handle:
                    data = plistlib.load(handle)
            except Exception as exc:  # forgiving: one bad plist must not stop the scan
                log.debug("ignoring malformed plist %s: %s", path, exc)
                continue
            if not isinstance(data, dict):
                log.debug("ignoring non-dict plist: %s", path)
                continue
            label = data.get("Label")
            if isinstance(label, str) and label.strip():
                name = label.strip()
            else:
                name = path.stem
            program_args = data.get("ProgramArguments")
            if isinstance(program_args, list) and program_args:
                command = " ".join(str(part) for part in program_args)
            else:
                program = data.get("Program")
                command = program if isinstance(program, str) else ""
            entries.append(StartupEntry(
                name=name,
                command=command,
                source=str(path),
                kind="launchagent",
            ))
    return entries


def user_startup_dirs() -> list[Path]:
    """Platform-appropriate autostart directories (pure-ish, creates nothing).

    Candidate paths are returned regardless of existence — enumeration skips
    missing/unreadable dirs on its own, and this function never touches the
    filesystem beyond expanding "~". On Windows the startup sources live in
    the registry and the Start Menu folder, so no directories apply.
    """
    try:
        home = os.path.expanduser("~")
    except (OSError, RuntimeError) as exc:
        log.warning("cannot expand home directory: %s", exc)
        return []
    if sys.platform == "win32":
        return []
    if sys.platform == "darwin":
        candidates = [
            Path(home) / "Library" / "LaunchAgents",
            Path("/Library/LaunchAgents"),
        ]
    else:
        candidates = [
            Path(home) / ".config" / "autostart",
            Path("/etc/xdg/autostart"),
        ]
    return candidates


def _guard_source(
    fn: Callable[[], tuple[list[StartupEntry], str]],
    label: str,
) -> tuple[list[StartupEntry], str]:
    """Run one platform source, converting a surprise failure into a note."""
    try:
        return fn()
    except Exception as exc:
        log.warning("%s enumeration failed unexpectedly: %s", label, exc)
        return [], f"{label} enumeration failed: {exc}"


def _guard_listing(
    fn: Callable[[], list[StartupEntry]],
    label: str,
) -> tuple[list[StartupEntry], str]:
    """Run one directory-based source, converting a failure into a note."""
    try:
        return fn(), ""
    except Exception as exc:
        log.warning("%s enumeration failed unexpectedly: %s", label, exc)
        return [], f"{label} enumeration failed: {exc}"


def collect_startup_entries() -> tuple[list[StartupEntry], str]:
    """Enumerate OS startup entries across all sources for this platform.

    Strictly read-only — nothing is disabled, deleted, or modified. Sources
    degrade independently, so one failing source never hides the others.
    Never raises; the note summarises per-kind counts and, when applicable,
    any source-level errors.
    """
    sources: list[tuple[list[StartupEntry], str]] = []
    try:
        if sys.platform == "win32":
            sources.append(_guard_source(_win_registry_entries, "Windows registry"))
            sources.append(_guard_source(_win_startup_folder_entries, "startup folder"))
        else:
            directories = user_startup_dirs()
            if sys.platform == "darwin":
                sources.append(_guard_listing(
                    lambda: _launchagent_entries(directories), "LaunchAgents"))
            else:
                sources.append(_guard_listing(
                    lambda: _autostart_entries(directories), "autostart"))
    except Exception as exc:  # absolute safety net around the dispatch itself
        log.warning("startup enumeration failed unexpectedly: %s", exc)
        return [], f"Startup enumeration failed: {exc}"

    entries: list[StartupEntry] = []
    errors: list[str] = []
    for source_entries, source_error in sources:
        entries.extend(source_entries)
        if source_error:
            errors.append(source_error)

    if entries:
        counts: dict[str, int] = {}
        for entry in entries:
            counts[entry.kind] = counts.get(entry.kind, 0) + 1
        breakdown = ", ".join(
            f"{counts[kind]} {_KIND_LABELS.get(kind, kind)}"
            for kind in _KIND_ORDER
            if counts.get(kind)
        )
        note = f"{len(entries)} startup entries found ({breakdown})."
    else:
        note = "No startup entries found."
    if errors:
        note += "; source errors: " + "; ".join(errors)
    return entries, note
