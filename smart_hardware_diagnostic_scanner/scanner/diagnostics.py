"""Modular diagnostic engine with documented, configurable thresholds."""
from __future__ import annotations

import platform
import time
from dataclasses import dataclass, field

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
                        key="CPU")]
    st = _status_for(cpu.percent, th["cpu_warn"], th["cpu_fail"])
    cores = f"{cpu.physical_cores}P/{cpu.logical_cores}L" if cpu.logical_cores else "n/a"
    msg = f"CPU utilisation {cpu.percent:.1f}% (cores {cores})"
    if st == "PASS":
        msg += " — healthy."
    elif st == "WARNING":
        msg += " — high load; close heavy apps if sustained."
    else:
        msg += " — extreme load; check runaway processes (no damage claimed)."
    return [Finding("CPU", st, msg, key="CPU")]


def diagnose_memory(snap: HardwareSnapshot, th: dict) -> list[Finding]:
    mem = snap.mem
    if not mem.available or mem.percent is None:
        return [Finding("Memory", "UNAVAILABLE",
                        "Memory metrics unavailable.",
                        mem.error or "psutil missing.", key="Memory")]
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
    return [Finding("Memory", st, msg, key="Memory")]


def diagnose_disks(snap: HardwareSnapshot, th: dict) -> list[Finding]:
    if not snap.disks:
        return [Finding("Storage", "UNAVAILABLE", "No disk partitions detected.",
                        "psutil missing or access denied.", key="Storage")]
    out: list[Finding] = []
    for d in snap.disks:
        if not d.accessible or d.percent is None:
            out.append(Finding("Storage", "UNAVAILABLE",
                               f"{d.mountpoint or d.device}: inaccessible.",
                               d.error or "permission denied",
                               key=f"Storage:{d.mountpoint or d.device}"))
            continue
        st = _status_for(d.percent, th["disk_warn"], th["disk_fail"])
        out.append(Finding("Storage", st,
                           f"{d.mountpoint} {d.percent:.1f}% used ({d.fstype or 'fs'})",
                           f"total={d.total} used={d.used}",
                           key=f"Storage:{d.mountpoint}"))
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
                               key=key))
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
                        key="Network")]
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


def run_diagnostics(snap: HardwareSnapshot, thresholds: dict | None = None) -> DiagnosticReport:
    """Run all diagnostic rules against a snapshot."""
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
