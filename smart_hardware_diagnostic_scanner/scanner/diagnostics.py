"""Modular diagnostic engine with documented, configurable thresholds."""
from __future__ import annotations

import platform
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime

from .hardware import HardwareSnapshot

# Documented defaults: high usage => WARNING (not "damaged hardware").
DEFAULT_THRESHOLDS = {
    "cpu_warn": 85.0,      # % sustained utilisation -> WARNING
    "cpu_fail": 97.0,      # % extreme utilisation -> FAIL (pressure, not damage)
    "mem_warn": 85.0,      # % used -> WARNING
    "mem_fail": 96.0,      # % used -> FAIL (pressure)
    "disk_warn": 85.0,     # % used per partition -> WARNING
    "disk_fail": 95.0,     # % used per partition -> FAIL (capacity risk)
    "temp_warn": 85.0,     # degC -> WARNING (if a real sensor reports it)
    "temp_fail": 100.0,    # degC -> FAIL
    "gpu_temp_warn": 83.0,
    "gpu_temp_fail": 95.0,
}


@dataclass
class Finding:
    component: str
    status: str  # PASS | WARNING | FAIL | UNAVAILABLE
    message: str
    detail: str = ""
    key: str = ""  # stable identity for cross-scan comparison (see compare_reports)
    code: str = ""  # machine-readable cause, used by scanner.explainer


@dataclass
class DiagnosticReport:
    started_at: float = 0.0
    finished_at: float = 0.0
    findings: list[Finding] = field(default_factory=list)
    summary: dict[str, int] = field(default_factory=dict)
    recommendations: list[str] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return max(0.0, self.finished_at - self.started_at)


def _status_for(value: float | None, warn: float, fail: float) -> str:
    if value is None:
        return "UNAVAILABLE"
    if value >= fail:
        return "FAIL"
    if value >= warn:
        return "WARNING"
    return "PASS"


# Worse-than ordering used by compare_reports. UNAVAILABLE sits above PASS so a
# sensor dropping out shows up as a change, without claiming it failed.
SEVERITY_RANK = {"PASS": 0, "UNAVAILABLE": 1, "WARNING": 2, "FAIL": 3}


def finding_key(finding: Finding) -> str:
    """Stable key for a finding; falls back to the component name."""
    return finding.key or finding.component


def compare_reports(previous: list[Finding] | None,
                    current: list[Finding]) -> dict:
    """Diff two finding lists by stable key.

    Returns {"regressions": [...], "improvements": [...], "added": [...],
    "removed": [...]} where each entry is
    (key, previous_status, current_status, message). Pass the findings lists
    (not the full reports) so callers can use any report object.
    """
    result: dict[str, list] = {"regressions": [], "improvements": [],
                               "added": [], "removed": []}
    if not previous:
        return result
    prev_by_key = {finding_key(f): f for f in previous}
    cur_by_key = {finding_key(f): f for f in current}

    for key, cur in cur_by_key.items():
        if key not in prev_by_key:
            result["added"].append((key, None, cur.status, cur.message))
            continue
        before = SEVERITY_RANK.get(prev_by_key[key].status, 1)
        after = SEVERITY_RANK.get(cur.status, 1)
        if after > before:
            result["regressions"].append((key, prev_by_key[key].status,
                                          cur.status, cur.message))
        elif after < before:
            result["improvements"].append((key, prev_by_key[key].status,
                                           cur.status, cur.message))

    for key, prev in prev_by_key.items():
        if key not in cur_by_key:
            result["removed"].append((key, prev.status, None, prev.message))
    return result


def diagnose_cpu(snap: HardwareSnapshot, th: dict) -> list[Finding]:
    cpu = snap.cpu
    if not cpu.available or cpu.percent is None:
        return [Finding("CPU", "UNAVAILABLE",
                        "CPU metrics unavailable.",
                        cpu.error or "psutil missing or access denied.",
                        key="CPU", code="CPU_UNAVAILABLE")]
    st = _status_for(cpu.percent, th["cpu_warn"], th["cpu_fail"])
    cores = f"{cpu.physical_cores}P/{cpu.logical_cores}L" if cpu.logical_cores else "n/a"
    msg = f"CPU utilisation {cpu.percent:.1f}% (cores {cores})"
    if st == "PASS":
        msg += " — healthy."
    elif st == "WARNING":
        msg += " — high load; close heavy apps if sustained."
    else:
        msg += " — extreme load; check runaway processes (no damage claimed)."
    return [Finding("CPU", st, msg, key="CPU",
                    code="CPU_HIGH" if st in ("WARNING", "FAIL") else "")]


def diagnose_memory(snap: HardwareSnapshot, th: dict) -> list[Finding]:
    mem = snap.mem
    if not mem.available or mem.percent is None:
        return [Finding("Memory", "UNAVAILABLE",
                        "Memory metrics unavailable.",
                        mem.error or "psutil missing.", key="Memory",
                        code="MEM_UNAVAILABLE")]
    st = _status_for(mem.percent, th["mem_warn"], th["mem_fail"])
    from .utils import format_bytes
    msg = (f"Memory {mem.percent:.1f}% used "
           f"({format_bytes(mem.used)} / {format_bytes(mem.total)})")
    if st == "WARNING":
        msg += " — memory pressure; consider closing tabs/apps."
    elif st == "FAIL":
        msg += " — critically low free memory; save work, close apps."
    else:
        msg += " — healthy."
    return [Finding("Memory", st, msg, key="Memory",
                    code="MEM_HIGH" if st in ("WARNING", "FAIL") else "")]


def diagnose_disks(snap: HardwareSnapshot, th: dict) -> list[Finding]:
    if not snap.disks:
        return [Finding("Storage", "UNAVAILABLE", "No disk partitions detected.",
                        "psutil missing or access denied.", key="Storage",
                        code="STORAGE_UNAVAILABLE")]
    out: list[Finding] = []
    for d in snap.disks:
        if not d.accessible or d.percent is None:
            out.append(Finding("Storage", "UNAVAILABLE",
                               f"{d.mountpoint or d.device}: inaccessible.",
                               d.error or "permission denied",
                               key=f"Storage:{d.mountpoint or d.device}",
                               code="STORAGE_UNAVAILABLE"))
            continue
        st = _status_for(d.percent, th["disk_warn"], th["disk_fail"])
        code = "DISK_FULL" if st == "FAIL" else ("DISK_LOW" if st == "WARNING" else "")
        out.append(Finding("Storage", st,
                           f"{d.mountpoint} {d.percent:.1f}% used ({d.fstype or 'fs'})",
                           f"total={d.total} used={d.used}",
                           key=f"Storage:{d.mountpoint}", code=code))
    return out


def diagnose_gpu(snap: HardwareSnapshot, th: dict) -> list[Finding]:
    g = snap.gpu
    if not g.supported or not g.gpus:
        return [Finding("GPU", "UNAVAILABLE",
                        "No supported GPU metrics available.",
                        g.message, key="GPU")]
    out: list[Finding] = []
    for gpu in g.gpus:
        key = f"GPU:{gpu.index}"
        if gpu.temperature_c is not None:
            st = _status_for(gpu.temperature_c, th["gpu_temp_warn"], th["gpu_temp_fail"])
            out.append(Finding("GPU", st,
                               f"GPU{gpu.index} {gpu.name}: {gpu.temperature_c:.0f}C" +
                               (f", util {gpu.utilization_pct:.0f}%" if gpu.utilization_pct is not None else ""),
                               key=key,
                               code="GPU_HOT" if st in ("WARNING", "FAIL") else ""))
        elif gpu.utilization_pct is not None:
            out.append(Finding("GPU", "PASS",
                               f"GPU{gpu.index} {gpu.name}: util {gpu.utilization_pct:.0f}% (temp unavailable)",
                               key=key))
        else:
            out.append(Finding("GPU", "UNAVAILABLE",
                               f"GPU{gpu.index} {gpu.name}: metrics unavailable.",
                               key=key))
    return out


def diagnose_network(snap: HardwareSnapshot, th: dict) -> list[Finding]:  # noqa: ARG001
    n = snap.net
    if not n.available:
        return [Finding("Network", "UNAVAILABLE", "Network counters unavailable.",
                        n.error or "psutil missing.", key="Network")]
    if not n.interfaces:
        return [Finding("Network", "WARNING", "No network interfaces reported.",
                        key="Network", code="NETWORK_NO_INTERFACES")]
    return [Finding("Network", "PASS",
                    f"{len(n.interfaces)} interface(s): {', '.join(n.interfaces[:6])}",
                    key="Network")]


def diagnose_temps(snap: HardwareSnapshot, th: dict) -> list[Finding]:
    real = [t for t in snap.temps if t.status == "available" and t.current is not None]
    if not real:
        return [Finding("Sensors", "UNAVAILABLE",
                        "No reliable temperature sensors exposed on this machine.",
                        "Unsupported sensors are reported, never invented.")]
    out: list[Finding] = []
    for t in real[:8]:
        st = _status_for(t.current, th["temp_warn"], th["temp_fail"])
        out.append(Finding("Sensors", st, f"{t.label}: {t.current:.1f}C (source: {t.source})",
                           key=f"Sensors:{t.label}"))
    return out


# ---------------------------------------------------------------------------
# Cross-platform OS health checks.
#
# Every check is strictly read-only: subprocess.run() gets an explicit argv,
# shell=False and an 8-second timeout, and nothing is started, stopped or
# modified. Missing/permission-denied/slow commands degrade to UNAVAILABLE
# instead of raising, so a scan always completes.
# ---------------------------------------------------------------------------
OS_CHECK_TIMEOUT_S = 8
OS_INODE_WARN_PCT = 90
OS_INODE_FAIL_PCT = 95
OS_KERNEL_ERRORS_FAIL = 5
OS_SYSTEMD_FAILED_FAIL = 5
OS_LOG_LINES_FAIL = 50
OS_BACKUP_WARN_DAYS = 7
OS_BACKUP_FAIL_DAYS = 30

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_TM_BACKUP_RE = re.compile(r"(\d{4}-\d{2}-\d{2}-\d{6})")


def _run_readonly(argv: list[str],
                  timeout: float = OS_CHECK_TIMEOUT_S) -> tuple[bool, str, str]:
    """Run one read-only command; return ``(ok, stdout, error_text)``.

    Never raises: a missing binary, a permission error or a timeout is
    reported through the error text so callers can degrade to UNAVAILABLE.
    """
    try:
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
    except (PermissionError, OSError) as exc:
        return False, "", f"{argv[0]} could not run: {exc}"
    out = proc.stdout or ""
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        suffix = f": {err[:300]}" if err else ""
        return False, out, f"{argv[0]} exited with code {proc.returncode}{suffix}"
    return True, out, ""


def _powershell(script: str) -> tuple[bool, str, str]:
    """Run a read-only PowerShell snippet (explicit argv, never a shell)."""
    return _run_readonly(["powershell", "-NoProfile", "-NonInteractive",
                          "-Command", script])


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def diagnose_windows_os() -> list[Finding]:
    """Read-only Windows checks: Update service, Defender, System Restore."""
    out: list[Finding] = []

    ok, text, err = _powershell(
        "(Get-Service -Name wuauserv -ErrorAction Stop).Status")
    if not ok:
        out.append(Finding(
            "OS: Windows Update", "UNAVAILABLE",
            "Could not read the Windows Update service status.", err,
            key="OS:WindowsUpdate", code="OS_CHECK_UNAVAILABLE"))
    else:
        status = _first_line(text).lower()
        if status == "running":
            out.append(Finding(
                "OS: Windows Update", "PASS",
                "Windows Update service (wuauserv) is running.",
                key="OS:WindowsUpdate"))
        elif status in ("stopped", "stop pending", "start pending"):
            out.append(Finding(
                "OS: Windows Update", "WARNING",
                "Windows Update service (wuauserv) is stopped — updates may not "
                "install until it starts.",
                "Windows can start this service on demand; open Windows Update "
                "and choose 'Check for updates'.",
                key="OS:WindowsUpdate", code="WINDOWS_UPDATE_STOPPED"))
        else:
            out.append(Finding(
                "OS: Windows Update", "UNAVAILABLE",
                f"Unrecognised Windows Update service status '{status or 'empty'}'.",
                key="OS:WindowsUpdate", code="OS_CHECK_UNKNOWN"))

    ok, text, err = _powershell(
        "(Get-MpComputerStatus -ErrorAction Stop).RealTimeProtectionEnabled")
    value = _first_line(text).lower()
    if not ok or value not in ("true", "false"):
        out.append(Finding(
            "OS: Defender", "UNAVAILABLE",
            "Windows Defender status could not be read.",
            err or f"unexpected output: {value or 'empty'}",
            key="OS:Defender", code="OS_CHECK_UNAVAILABLE"))
    elif value == "false":
        out.append(Finding(
            "OS: Defender", "WARNING",
            "Windows Defender real-time protection is disabled.",
            "A third-party antivirus can also switch this off; confirm in "
            "Windows Security before changing anything.",
            key="OS:Defender", code="DEFENDER_DISABLED"))
    else:
        out.append(Finding(
            "OS: Defender", "PASS",
            "Windows Defender real-time protection is enabled.",
            key="OS:Defender"))

    # DisableSR=1 is the documented policy marker for "System Restore off".
    # A missing value means protection was not disabled this way.
    ok, text, err = _powershell(
        r"(Get-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\Windows NT"
        r"\CurrentVersion\SystemRestore' -Name DisableSR "
        r"-ErrorAction SilentlyContinue).DisableSR")
    if not ok:
        out.append(Finding(
            "OS: System Restore", "UNAVAILABLE",
            "System Restore status could not be read.", err,
            key="OS:SystemRestore", code="OS_CHECK_UNAVAILABLE"))
    elif _first_line(text) == "1":
        out.append(Finding(
            "OS: System Restore", "WARNING",
            "System Restore is turned off (DisableSR=1).",
            "Without restore points there is no quick rollback after a bad "
            "driver or update.",
            key="OS:SystemRestore", code="SYSTEM_RESTORE_OFF"))
    else:
        out.append(Finding(
            "OS: System Restore", "PASS",
            "System Restore is not disabled by policy (DisableSR not set).",
            "Existing restore points are not inspected; this is a policy check.",
            key="OS:SystemRestore"))
    return out


def _kernel_error_lines(text: str) -> list[str]:
    """Pick kernel-log lines that describe real hardware/software errors."""
    hits: list[str] = []
    for raw in (text or "").splitlines():
        low = raw.lower()
        if ("i/o error" in low or "hardware error" in low
                or "machine check" in low or "segfault" in low
                or "ext4-fs error" in low
                or ("xfs (" in low and "error" in low)
                or ("btrfs" in low and ("error" in low or "corrupt" in low))):
            line = raw.strip()
            if line:
                hits.append(line[:300])
    return hits


def _inode_usage(text: str) -> tuple[int, str] | None:
    """Worst inode percentage from ``df -i -P`` output: (percent, mount)."""
    worst: tuple[int, str] | None = None
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) < 6:
            continue
        pct = parts[4].rstrip("%")
        if not pct.isdigit():
            continue  # header row ("IUse%") or non-numeric entry
        value = int(pct)
        mount = " ".join(parts[5:])
        if worst is None or value > worst[0]:
            worst = (value, mount)
    return worst


def _failed_units(text: str) -> list[str]:
    """Unit names from ``systemctl --failed --no-legend`` (read-only)."""
    units: list[str] = []
    for line in (text or "").splitlines():
        line = line.strip().lstrip("●").strip()
        if line:
            units.append(line.split()[0])
    return units


def diagnose_linux_os() -> list[Finding]:
    """Read-only Linux checks: kernel log, inodes, failed systemd units."""
    out: list[Finding] = []

    ok, text, err = _run_readonly(["dmesg"])
    if not ok:
        low = err.lower()
        denied = ("permission" in low or "not permitted" in low)
        out.append(Finding(
            "OS: Kernel Log", "UNAVAILABLE",
            "Kernel log (dmesg) could not be read.",
            err + (" — dmesg is often restricted to root; elevated rights are "
                   "only needed to read it, not to fix it." if denied else ""),
            key="OS:KernelLog",
            code="ACCESS_DENIED" if denied else "OS_CHECK_UNAVAILABLE"))
    else:
        hits = _kernel_error_lines(text)
        if not hits:
            out.append(Finding(
                "OS: Kernel Log", "PASS",
                "No hardware or storage errors found in the kernel log.",
                key="OS:KernelLog"))
        else:
            st = "FAIL" if len(hits) >= OS_KERNEL_ERRORS_FAIL else "WARNING"
            out.append(Finding(
                "OS: Kernel Log", st,
                f"Kernel log contains {len(hits)} hardware/software "
                "error line(s).",
                " | ".join(hits[:5])[:500],
                key="OS:KernelLog", code="KERNEL_HARDWARE_ERROR"))

    ok, text, err = _run_readonly(["df", "-i", "-P"])
    worst = _inode_usage(text) if ok else None
    if not ok or worst is None:
        out.append(Finding(
            "OS: Inodes", "UNAVAILABLE",
            "Inode usage could not be read.", err,
            key="OS:Inodes", code="OS_CHECK_UNAVAILABLE"))
    else:
        pct, mount = worst
        if pct >= OS_INODE_FAIL_PCT:
            st, code = "FAIL", "INODES_CRITICAL"
        elif pct >= OS_INODE_WARN_PCT:
            st, code = "WARNING", "INODES_LOW"
        else:
            st, code = "PASS", ""
        out.append(Finding(
            "OS: Inodes", st,
            f"Highest inode usage {pct}% ({mount}).",
            "Inodes are file metadata records: a volume can have free space "
            "and still be unable to create files when inodes run out.",
            key="OS:Inodes", code=code))

    ok, text, err = _run_readonly(
        ["systemctl", "--failed", "--no-legend", "--no-pager"])
    if not ok:
        out.append(Finding(
            "OS: systemd", "UNAVAILABLE",
            "Failed systemd services could not be listed.", err,
            key="OS:Systemd", code="OS_CHECK_UNAVAILABLE"))
    else:
        units = _failed_units(text)
        if not units:
            out.append(Finding(
                "OS: systemd", "PASS",
                "No failed systemd services.",
                key="OS:Systemd"))
        else:
            st = "FAIL" if len(units) >= OS_SYSTEMD_FAILED_FAIL else "WARNING"
            out.append(Finding(
                "OS: systemd", st,
                f"{len(units)} systemd service(s) have failed.",
                ", ".join(units[:5])[:500],
                key="OS:Systemd", code="SYSTEMD_FAILED"))
    return out


def _macos_log_errors(text: str, sample_limit: int = 3) -> tuple[int, list[str]]:
    """Count non-header lines from ``log show``; keep a few samples."""
    count = 0
    samples: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("Timestamp"):
            continue
        count += 1
        if len(samples) < sample_limit:
            samples.append(line[:240])
    return count, samples


def _diagnose_time_machine() -> Finding:
    """Read-only Time Machine check based on ``tmutil latestbackup``."""
    ok, text, err = _run_readonly(["tmutil", "latestbackup"])
    combined = f"{text}\n{err}".lower()
    if "command not found" in combined:
        return Finding(
            "OS: Time Machine", "UNAVAILABLE",
            "Time Machine's tmutil command is not available.",
            err, key="OS:TimeMachine", code="OS_CHECK_UNAVAILABLE")
    if "no backups" in combined:
        return Finding(
            "OS: Time Machine", "WARNING",
            "Time Machine reports no completed backups.",
            "Backups are not running; open Time Machine settings and choose a "
            "backup disk.",
            key="OS:TimeMachine", code="TIMEMACHINE_NO_BACKUP")
    if not ok:
        return Finding(
            "OS: Time Machine", "UNAVAILABLE",
            "Time Machine backup status could not be read.", err,
            key="OS:TimeMachine", code="OS_CHECK_UNAVAILABLE")
    path = _first_line(text)
    match = _TM_BACKUP_RE.search(path)
    if match is None:
        return Finding(
            "OS: Time Machine", "UNAVAILABLE",
            "Time Machine returned an unrecognised backup path.",
            path[:300], key="OS:TimeMachine", code="OS_CHECK_UNKNOWN")
    try:
        when = datetime.strptime(match.group(1), "%Y-%m-%d-%H%M%S")
    except ValueError:
        return Finding(
            "OS: Time Machine", "UNAVAILABLE",
            "Time Machine backup timestamp could not be parsed.",
            path[:300], key="OS:TimeMachine", code="OS_CHECK_UNKNOWN")
    age_days = max(0.0, (datetime.now() - when).total_seconds() / 86400.0)
    if age_days <= OS_BACKUP_WARN_DAYS:
        return Finding(
            "OS: Time Machine", "PASS",
            f"Last Time Machine backup: {age_days:.1f} day(s) ago.",
            key="OS:TimeMachine")
    st = "FAIL" if age_days > OS_BACKUP_FAIL_DAYS else "WARNING"
    return Finding(
        "OS: Time Machine", st,
        f"Last Time Machine backup is {age_days:.0f} days old.",
        "Backups older than a week are a data-loss risk; check the Time "
        "Machine destination and run a backup.",
        key="OS:TimeMachine", code="TIMEMACHINE_STALE")


def diagnose_macos_os() -> list[Finding]:
    """Read-only macOS checks: recent unified-log errors and Time Machine."""
    out: list[Finding] = []
    ok, text, err = _run_readonly(
        ["log", "show", "--predicate", 'eventMessage contains "error"',
         "--last", "1h"])
    if not ok:
        out.append(Finding(
            "OS: System Log", "UNAVAILABLE",
            "Recent macOS system log errors could not be read.", err,
            key="OS:MacLog", code="OS_CHECK_UNAVAILABLE"))
    else:
        count, samples = _macos_log_errors(text)
        if count == 0:
            out.append(Finding(
                "OS: System Log", "PASS",
                "No 'error' messages in the last hour of the unified log.",
                key="OS:MacLog"))
        else:
            st = "FAIL" if count >= OS_LOG_LINES_FAIL else "WARNING"
            out.append(Finding(
                "OS: System Log", st,
                f"Unified log has {count} line(s) mentioning 'error' in the "
                "last hour.",
                " | ".join(samples)[:500],
                key="OS:MacLog", code="MACOS_LOG_ERRORS"))
    out.append(_diagnose_time_machine())
    return out


def diagnose_os(system: str | None = None) -> list[Finding]:
    """Run the read-only checks for the current (or given) operating system."""
    system = system or platform.system()
    try:
        if system == "Windows":
            return diagnose_windows_os()
        if system == "Linux":
            return diagnose_linux_os()
        if system == "Darwin":
            return diagnose_macos_os()
    except Exception as exc:  # noqa: BLE001 - an OS check must never break a scan
        return [Finding("OS", "UNAVAILABLE", "OS-level health checks failed.",
                        str(exc), key="OS", code="OS_CHECK_ERROR")]
    return [Finding(
        "OS", "UNAVAILABLE",
        f"No OS-level checks are implemented for {system or 'this system'}.",
        "Hardware checks still run normally.", key="OS",
        code="OS_UNSUPPORTED")]


# Weights for the combined System Health score (see system_health_score).
HEALTH_WEIGHTS = {"cpu": 0.30, "mem": 0.25, "disk": 0.25, "os": 0.20}


def _health_metric_score(percent: float) -> float:
    """Turn a usage percentage into a 0..100 'headroom' score."""
    return max(0.0, min(100.0, 100.0 - float(percent)))


def system_health_score(snap: HardwareSnapshot | None,
                        findings: list[Finding] | None = None) -> dict:
    """Combine CPU, RAM, worst disk and OS-level findings into 0..100.

    Weights: CPU 30%, memory 25%, disk 25%, OS-level 20%. Metrics that are
    unavailable are left out and the remaining weights renormalise, so a
    missing sensor never pretends the machine is broken. Pass ``findings=None``
    when no diagnostic scan has run yet: the OS component is then excluded
    (unknown is not unhealthy) and renormalised away as well.

    Only findings whose stable key/component starts with ``OS`` feed the OS
    part: WARNING costs 4 points, FAIL costs 10, capped at 60. UNAVAILABLE
    never lowers the score because a check that could not run proves nothing.
    """
    components: dict[str, float] = {}
    weights: dict[str, float] = {}
    if snap is not None:
        if snap.cpu.available and snap.cpu.percent is not None:
            components["cpu"] = _health_metric_score(snap.cpu.percent)
            weights["cpu"] = HEALTH_WEIGHTS["cpu"]
        if snap.mem.available and snap.mem.percent is not None:
            components["mem"] = _health_metric_score(snap.mem.percent)
            weights["mem"] = HEALTH_WEIGHTS["mem"]
        accessible = [d for d in snap.disks
                      if d.accessible and d.percent is not None]
        if accessible:
            components["disk"] = _health_metric_score(
                max(d.percent for d in accessible if d.percent is not None))
            weights["disk"] = HEALTH_WEIGHTS["disk"]

    # Without a single live metric a "health score" would be invented; report
    # no score instead (the UI shows a dash).
    if not weights:
        return {"total": None, "components": {}, "os_fail": 0, "os_warning": 0}

    os_fail = 0
    os_warning = 0
    if findings is not None:
        os_findings = [f for f in findings
                       if (f.key or f.component).startswith("OS")]
        os_fail = sum(1 for f in os_findings if f.status == "FAIL")
        os_warning = sum(1 for f in os_findings if f.status == "WARNING")
        penalty = min(60.0, os_fail * 10.0 + os_warning * 4.0)
        components["os"] = 100.0 - penalty
        weights["os"] = HEALTH_WEIGHTS["os"]

    result = {
        "total": None,
        "components": {k: round(v, 1) for k, v in components.items()},
        "os_fail": os_fail,
        "os_warning": os_warning,
    }
    if not weights:
        return result
    total = (sum(components[k] * weights[k] for k in weights)
             / sum(weights.values()))
    result["total"] = int(round(max(0.0, min(100.0, total))))
    return result


def _disk_advice() -> str:
    """Platform-appropriate 'free up disk space' advice (no tool is run)."""
    system = platform.system().lower()
    if system == "windows":
        return ("Disk nearly full: free space via Windows Disk Clean-up; "
                "empty the Recycle Bin.")
    if system == "darwin":
        return ("Disk nearly full: free space via System Settings > General > "
                "Storage (Optimize Storage).")
    return ("Disk nearly full: free space with your distribution's disk-usage "
            "tool (e.g. baobab); clear package caches and old journal logs.")


def build_recommendations(findings: list[Finding]) -> list[str]:
    recs: list[str] = []
    # A component can emit several findings (one per GPU, one per volume), so
    # keep the WORST status per component. A plain "last one wins" mapping
    # would let a cool second GPU hide a first GPU that is overheating.
    comps: dict[str, str] = {}
    for f in findings:
        current = comps.get(f.component)
        if current is None or SEVERITY_RANK.get(f.status, 0) > SEVERITY_RANK.get(current, 0):
            comps[f.component] = f.status
    if comps.get("CPU") in ("WARNING", "FAIL"):
        recs.append("High CPU: identify the top process in the Processes tab; close or restart it. No settings were changed.")
    if comps.get("Memory") in ("WARNING", "FAIL"):
        recs.append("High memory: close unused browser tabs/apps and save work; consider adding RAM only if pressure is chronic.")
    if any(f.component == "Storage" and f.status in ("WARNING", "FAIL") for f in findings):
        recs.append(_disk_advice() + " This app deletes nothing by itself.")
    if any(f.component == "Sensors" and f.status in ("WARNING", "FAIL") for f in findings):
        recs.append("High temperature: ensure vents are clear and fans run; avoid blocking airflow. Seek service if it persists.")
    if comps.get("GPU") in ("WARNING", "FAIL"):
        recs.append("GPU running hot or heavily loaded: close GPU-heavy apps and "
                    "check airflow/cooling; no settings were changed.")
    if comps.get("GPU") == "UNAVAILABLE":
        recs.append("GPU metrics unavailable: install vendor drivers / nvidia-smi for NVIDIA, or use vendor tools for AMD/Intel.")
    if any(f.component.startswith("OS") and f.status in ("WARNING", "FAIL")
           for f in findings):
        recs.append("OS-level check flagged an issue: use the 'How to fix' buttons "
                    "in the Diagnostics tab for a plain-English explanation and the "
                    "exact settings page (or a safe read-only command). This app "
                    "changes nothing by itself.")
    if comps.get("Sensors") == "UNAVAILABLE":
        recs.append("Temperature sensors unsupported here: readings are honestly reported as unavailable, not estimated.")
    if not recs:
        if any(f.status in ("WARNING", "FAIL") for f in findings):
            # Unhandled component-specific problem (e.g. a missing NIC): never
            # claim "all healthy" when something was actually flagged.
            recs.append("Some checks need attention — review the findings above.")
        else:
            recs.append("All checks look healthy. Re-run the scan after heavy workloads to compare.")
    void = [f for f in findings if f.status == "UNAVAILABLE"]
    if void:
        recs.append(f"{len(void)} check(s) unavailable — see findings for the exact reason (missing sensor, driver, or permission).")
    return recs


def run_diagnostics(snap: HardwareSnapshot, thresholds: dict | None = None,
                    include_os: bool = True) -> DiagnosticReport:
    """Run all diagnostic rules against a snapshot.

    ``include_os`` controls the read-only OS checks (Windows/Linux/macOS);
    tests that exercise threshold logic can turn them off to stay hermetic.
    """
    th = dict(DEFAULT_THRESHOLDS)
    if thresholds:
        th.update(thresholds)
    started = time.time()
    findings: list[Finding] = []
    findings += diagnose_cpu(snap, th)
    findings += diagnose_memory(snap, th)
    findings += diagnose_disks(snap, th)
    findings += diagnose_gpu(snap, th)
    findings += diagnose_network(snap, th)
    findings += diagnose_temps(snap, th)
    if include_os:
        findings += diagnose_os()
    summary = {"PASS": 0, "WARNING": 0, "FAIL": 0, "UNAVAILABLE": 0}
    for f in findings:
        summary[f.status] = summary.get(f.status, 0) + 1
    report = DiagnosticReport(
        started_at=started,
        finished_at=time.time(),
        findings=findings,
        summary=summary,
        recommendations=build_recommendations(findings),
    )
    return report
