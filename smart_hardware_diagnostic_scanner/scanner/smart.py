"""SMART disk health via smartctl (optional tool, honest degradation)."""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field

from .utils import which

log = logging.getLogger("shds.smart")

SMARTCTL_TIMEOUT = 8  # seconds


@dataclass
class SmartDevice:
    """Single disk health reading; missing details stay empty."""

    device: str = ""             # device path as smartctl reported it
    model: str = ""              # best-effort model name (may be "")
    status: str = "unavailable"  # "PASS" | "FAIL" | "UNAVAILABLE"
    detail: str = ""


@dataclass
class SmartStatus:
    """Overall SMART provider state."""

    supported: bool = False
    tool_found: bool = False
    devices: list[SmartDevice] = field(default_factory=list)
    message: str = "Unavailable"


def scan_argv(exe: str) -> list[str]:
    """argv for a device enumeration run (pure; exported for tests)."""
    return [exe, "--scan"]


def health_argv(exe: str, device: str, dev_type: str | None = None) -> list[str]:
    """argv for one device health query (pure).

    `dev_type` is the "-d <type>" hint smartctl --scan prints for USB
    bridges, NVMe and other devices that need an explicit driver
    ("/dev/sdb -d sat"). Without it the health query fails and the disk is
    reported UNAVAILABLE even though it is perfectly healthy.
    """
    argv = [exe]
    if dev_type:
        argv += ["-d", dev_type]
    return argv + ["--health", device]


def classify_smart_exit(code: int) -> str:
    """Map a smartctl exit code to PASS/FAIL/UNAVAILABLE.

    smartctl exits with a bitfield, and this mapping is deliberately coarse:

    - bit 3 (value 8) means "SMART health of the device failed" -> "FAIL"
      (other set bits alongside 8 are checksum/attribute details, not
      stronger verdicts, so 8|1 -> "FAIL" too);
    - exit code 0 means the device passed -> "PASS";
    - anything else (command line errors, permission problems, unsupported
      device, firmware quirks) means no health verdict was obtained and is
      reported honestly as "UNAVAILABLE" instead of guessed.
    """
    if code & 8:
        return "FAIL"
    if code == 0:
        return "PASS"
    return "UNAVAILABLE"


def _first_line(text: str | None) -> str:
    """First non-empty line of text, trimmed ("" for empty text)."""
    for line in (text or "").splitlines():
        clean = line.strip()
        if clean:
            return clean
    return ""


def _device_of(line: str) -> str:
    """Device path token from one smartctl --scan row ("" for junk lines).

    smartctl always reports POSIX-style device paths, so the token containing
    "/dev/" wins; when uncertain, old-style indexed rows ("# 0  <dev> ...")
    fall back to their third whitespace token. Every other line is skipped.
    """
    tokens = (line or "").split()
    for token in tokens:
        if "/dev/" in token:
            return token.rstrip(",;")
    if len(tokens) >= 3 and tokens[0] == "#" and tokens[1].isdigit():
        candidate = tokens[2].rstrip(",;")
        if candidate and not candidate.startswith("#"):
            return candidate
    return ""


def _device_type_of(line: str) -> str:
    """The "-d <type>" driver hint from one smartctl --scan row ("" if absent).

    smartctl --scan prints rows such as
    "/dev/sdb -d sat # /dev/sdb [SAT], ATA device" — the type is required
    for USB bridges and NVMe devices.
    """
    tokens = (line or "").split()
    for index, token in enumerate(tokens):
        if token == "-d" and index + 1 < len(tokens):
            candidate = tokens[index + 1].rstrip(",;")
            if candidate and not candidate.startswith("#"):
                return candidate
    return ""


def _model_of(text: str | None) -> str:
    """Device model name from smartctl output.

    smartctl prints "Model Family" *before* "Device Model", so the first line
    mentioning "model" is usually the generic family string. Prefer the real
    device model and fall back to the family only when it is all we have.
    """
    family = ""
    for line in (text or "").splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key = key.strip().lower()
        value = value.strip()
        if not value:
            continue
        if key == "device model":
            return value[:80]
        if key == "model family" and not family:
            family = value[:80]
    return family


def _health_check(exe: str, device: str, timeout: int,
                  dev_type: str | None = None) -> SmartDevice:
    """Run one health query; a failure degrades that device only."""
    row = SmartDevice(device=device, status="UNAVAILABLE")
    try:
        proc = subprocess.run(health_argv(exe, device, dev_type), shell=False,
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except FileNotFoundError:
        row.detail = "smartctl executable disappeared."
    except subprocess.TimeoutExpired:
        row.detail = f"smartctl timed out after {timeout}s."
    except OSError as exc:
        row.detail = f"Could not run smartctl: {exc}"
    else:
        row.status = classify_smart_exit(proc.returncode)
        row.model = _model_of(proc.stdout)
        detail = _first_line(proc.stderr) or _first_line(proc.stdout)
        row.detail = detail[:200]
    return row


def query_smart_devices(timeout: int = SMARTCTL_TIMEOUT,
                        max_devices: int = 8) -> SmartStatus:
    """Enumerate disks and run one health query each; never raises.

    smartctl (from the smartmontools package) is optional: when it is not
    installed the status honestly reports the tool as missing instead of
    inventing health data.
    """
    status = SmartStatus()
    exe = which("smartctl")
    if not exe:
        status.message = ("smartctl not found — install smartmontools to enable "
                          "disk SMART checks.")
        log.info("smartctl not found")
        return status
    status.tool_found = True
    try:
        proc = subprocess.run(scan_argv(exe), shell=False, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except FileNotFoundError:
        status.message = "smartctl executable disappeared."
        return status
    except subprocess.TimeoutExpired:
        status.message = f"smartctl --scan timed out after {timeout}s."
        log.warning("smartctl --scan timeout")
        return status
    except OSError as exc:
        status.message = f"Could not run smartctl: {exc}"
        log.warning("smartctl OSError: %s", exc)
        return status
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()[:300]
        status.message = f"smartctl --scan failed (exit {proc.returncode}): {err or 'no details'}"
        log.warning("smartctl --scan exit %s: %s", proc.returncode, err)
        return status
    devices: list[tuple[str, str]] = []
    for line in (proc.stdout or "").splitlines():
        device = _device_of(line)
        if device:
            devices.append((device, _device_type_of(line)))
    if not devices:
        status.message = "smartctl reported no usable devices."
        return status
    for device, dev_type in devices[: max(0, max_devices)]:
        try:
            row = _health_check(exe, device, timeout, dev_type)
        except Exception as exc:  # belt and braces: the loop never aborts
            log.warning("smartctl health check crashed for %s: %s", device, exc)
            row = SmartDevice(device=device, status="UNAVAILABLE", detail=str(exc))
        status.devices.append(row)
    passed = sum(1 for d in status.devices if d.status == "PASS")
    failed = sum(1 for d in status.devices if d.status == "FAIL")
    status.supported = bool(status.devices)
    status.message = (f"{len(status.devices)} device(s) checked: "
                      f"{passed} PASS / {failed} FAIL.")
    return status
