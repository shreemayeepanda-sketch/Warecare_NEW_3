"""Hardware snapshot collection (psutil-based, fully defensive)."""
from __future__ import annotations

import logging
import platform
import socket
import threading
import time
from dataclasses import dataclass, field

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:  # optional dependency
    psutil = None  # type: ignore
    HAS_PSUTIL = False

from .gpu import GpuStatus, query_gpus

log = logging.getLogger("shds.hardware")


@dataclass
class CpuSnapshot:
    available: bool = False
    percent: float | None = None
    per_core: list[float] = field(default_factory=list)
    physical_cores: int | None = None
    logical_cores: int | None = None
    freq_current_mhz: float | None = None
    freq_min_mhz: float | None = None
    freq_max_mhz: float | None = None
    load_avg: tuple | None = None
    error: str = ""


@dataclass
class MemSnapshot:
    available: bool = False
    total: int = 0
    available_bytes: int = 0
    used: int = 0
    percent: float | None = None
    error: str = ""


@dataclass
class DiskEntry:
    device: str = ""
    mountpoint: str = ""
    fstype: str = ""
    total: int = 0
    used: int = 0
    free: int = 0
    percent: float | None = None
    accessible: bool = False
    error: str = ""


@dataclass
class NetSnapshot:
    available: bool = False
    interfaces: list[str] = field(default_factory=list)
    bytes_sent: int = 0
    bytes_recv: int = 0
    up_bps: float | None = None
    down_bps: float | None = None
    error: str = ""


@dataclass
class TempReading:
    label: str = ""
    current: float | None = None
    source: str = ""
    status: str = "unavailable"  # available | unavailable | unsupported


@dataclass
class ProcEntry:
    pid: int = 0
    name: str = ""
    cpu: float = 0.0
    mem_mb: float = 0.0


@dataclass
class HardwareSnapshot:
    cpu: CpuSnapshot = field(default_factory=CpuSnapshot)
    mem: MemSnapshot = field(default_factory=MemSnapshot)
    disks: list[DiskEntry] = field(default_factory=list)
    net: NetSnapshot = field(default_factory=NetSnapshot)
    temps: list[TempReading] = field(default_factory=list)
    gpu: GpuStatus = field(default_factory=GpuStatus)
    hostname: str = ""
    os_info: str = ""
    timestamp: float = 0.0


_prev_net: dict[str, float] = {"sent": 0, "recv": 0, "t": 0.0}
_net_lock = threading.Lock()
_proc_primed = False


def _rate_bps(current: int | None, previous: int | None, dt: float) -> float | None:
    """Rate in bits/s, or None for the first sample or a counter reset.

    A reset (previous > current, e.g. interface restart or reboot) would
    produce a negative delta, so the rate is reported as unavailable instead.
    """
    if current is None or previous is None:
        return None
    if dt <= 0:
        return None
    if current < previous:
        log.info("network counter reset detected (previous=%s current=%s); "
                 "rate unavailable for this sample", previous, current)
        return None
    return (current - previous) * 8.0 / dt


def collect_cpu() -> CpuSnapshot:
    snap = CpuSnapshot()
    if not HAS_PSUTIL:
        snap.error = "psutil not installed"
        return snap
    try:
        snap.percent = float(psutil.cpu_percent(interval=None))
        try:
            snap.per_core = [float(v) for v in psutil.cpu_percent(interval=None, percpu=True)]
        except Exception as exc:  # per-core optional
            log.debug("per-core cpu failed: %s", exc)
            snap.per_core = []
        try:
            snap.physical_cores = psutil.cpu_count(logical=False)
        except Exception:
            snap.physical_cores = None
        try:
            snap.logical_cores = psutil.cpu_count(logical=True)
        except Exception:
            snap.logical_cores = None
        try:
            freq = psutil.cpu_freq()
            if freq:
                snap.freq_current_mhz = freq.current
                snap.freq_min_mhz = freq.min
                snap.freq_max_mhz = freq.max
        except Exception:
            pass
        try:
            if hasattr(psutil, "getloadavg"):
                snap.load_avg = tuple(psutil.getloadavg())
        except Exception:
            snap.load_avg = None
        snap.available = True
    except Exception as exc:
        snap.error = str(exc)
        log.warning("cpu collect failed: %s", exc)
    return snap


def collect_memory() -> MemSnapshot:
    snap = MemSnapshot()
    if not HAS_PSUTIL:
        snap.error = "psutil not installed"
        return snap
    try:
        vm = psutil.virtual_memory()
        snap.total = int(vm.total)
        snap.available_bytes = int(vm.available)
        snap.used = int(vm.used)
        snap.percent = float(vm.percent)
        snap.available = True
    except Exception as exc:
        snap.error = str(exc)
        log.warning("memory collect failed: %s", exc)
    return snap


def collect_disks() -> list[DiskEntry]:
    entries: list[DiskEntry] = []
    if not HAS_PSUTIL:
        return entries
    try:
        parts = psutil.disk_partitions(all=False)
    except Exception as exc:
        log.warning("disk_partitions failed: %s", exc)
        return entries
    seen: set[str] = set()
    for part in parts:
        key = (part.mountpoint or part.device).lower()
        if key in seen:
            continue
        seen.add(key)
        entry = DiskEntry(device=part.device, mountpoint=part.mountpoint, fstype=part.fstype)
        try:
            usage = psutil.disk_usage(part.mountpoint)
            entry.total = int(usage.total)
            entry.used = int(usage.used)
            entry.free = int(usage.free)
            entry.percent = float(usage.percent)
            entry.accessible = True
        except PermissionError as exc:
            entry.error = f"Permission denied: {exc}"
            log.debug("disk %s permission denied", part.mountpoint)
        except (FileNotFoundError, OSError) as exc:
            entry.error = f"Inaccessible: {exc}"
            log.debug("disk %s inaccessible: %s", part.mountpoint, exc)
        except Exception as exc:
            entry.error = str(exc)
            log.warning("disk %s failed: %s", part.mountpoint, exc)
        entries.append(entry)
    return entries


def collect_network() -> NetSnapshot:
    snap = NetSnapshot()
    if not HAS_PSUTIL:
        snap.error = "psutil not installed"
        return snap
    try:
        addrs = psutil.net_if_addrs()
        snap.interfaces = sorted(addrs.keys())
        counters = psutil.net_io_counters()
        if counters is not None:
            snap.bytes_sent = int(counters.bytes_sent)
            snap.bytes_recv = int(counters.bytes_recv)
            now = time.monotonic()
            with _net_lock:
                prev_sent = _prev_net.get("sent", 0)
                prev_recv = _prev_net.get("recv", 0)
                prev_t = _prev_net.get("t", 0.0)
                if prev_t and now > prev_t:
                    dt = now - prev_t
                    # Sent and received counters reset independently (interface
                    # restart, reboot, adapter swap); handle each separately.
                    snap.up_bps = _rate_bps(snap.bytes_sent, prev_sent, dt)
                    snap.down_bps = _rate_bps(snap.bytes_recv, prev_recv, dt)
                else:
                    snap.up_bps = None  # first sample
                    snap.down_bps = None
                _prev_net.update({"sent": snap.bytes_sent, "recv": snap.bytes_recv, "t": now})
        snap.available = True
    except Exception as exc:
        snap.error = str(exc)
        log.warning("network collect failed: %s", exc)
    return snap


def collect_temps() -> list[TempReading]:
    """Query sensors; never invent readings. RAM temp reported unsupported."""
    readings: list[TempReading] = []
    temps: dict = {}
    if not HAS_PSUTIL:
        readings.append(TempReading(label="sensors", current=None, source="psutil",
                                    status="unsupported"))
    else:
        try:
            temps = psutil.sensors_temperatures()
        except Exception as exc:
            log.debug("sensors_temperatures failed: %s", exc)
            temps = {}
    if not temps:
        readings.append(TempReading(label="system", current=None,
                                    source="psutil.sensors_temperatures",
                                    status="unsupported"))
    else:
        for chip, items in temps.items():
            for item in items:
                label = f"{chip}:{getattr(item, 'label', '') or 'sensor'}".strip(":")
                cur = getattr(item, "current", None)
                try:
                    cur_f = float(cur) if cur not in (None, "") else None
                except (TypeError, ValueError):
                    cur_f = None
                readings.append(TempReading(
                    label=label,
                    current=cur_f,
                    source="psutil.sensors_temperatures",
                    status="available" if cur_f is not None else "unavailable",
                ))
    # Explicit honesty: standard psutil cannot read physical RAM temperature.
    readings.append(TempReading(label="RAM", current=None,
                                source="no reliable sensor interface",
                                status="unsupported"))
    return readings


def collect_processes(limit: int = 12, sort_by: str = "cpu") -> list[ProcEntry]:
    """Return top processes; never terminates anything."""
    global _proc_primed
    out: list[ProcEntry] = []
    if not HAS_PSUTIL:
        return out
    try:
        if not _proc_primed:
            # One-time ~100 ms cost (worker thread only): prime psutil's
            # per-process CPU counters so the first real pass is not all 0.0.
            for _p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info"]):
                pass
            _proc_primed = True
            time.sleep(0.1)
        procs = []
        for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info"]):
            try:
                info = p.info
                mem_mb = 0.0
                if info.get("memory_info"):
                    mem_mb = float(info["memory_info"].rss) / (1024 * 1024)
                procs.append(ProcEntry(
                    pid=int(info.get("pid", 0)),
                    name=str(info.get("name", "?"))[:60],
                    cpu=float(info.get("cpu_percent") or 0.0),
                    mem_mb=round(mem_mb, 1),
                ))
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            except Exception as exc:
                log.debug("process row failed: %s", exc)
                continue
    except Exception as exc:
        log.warning("process_iter failed: %s", exc)
        return out
    key = (lambda e: e.mem_mb) if sort_by == "mem" else (lambda e: e.cpu)
    procs.sort(key=key, reverse=True)
    return procs[: max(1, limit)]


def collect_snapshot(include_gpu: bool = True) -> HardwareSnapshot:
    """Collect a full snapshot; each section degrades independently."""
    try:
        hostname = socket.gethostname()
    except Exception:
        hostname = "unknown"
    snap = HardwareSnapshot(
        cpu=collect_cpu(),
        mem=collect_memory(),
        disks=collect_disks(),
        net=collect_network(),
        temps=collect_temps(),
        hostname=hostname,
        os_info=f"{platform.system()} {platform.release()} ({platform.version()})",
        timestamp=time.time(),
    )
    if include_gpu:
        try:
            snap.gpu = query_gpus()
        except Exception as exc:  # never let GPU break the snapshot
            log.warning("gpu query failed: %s", exc)
            snap.gpu = GpuStatus(message=f"GPU query failed: {exc}")
    return snap
