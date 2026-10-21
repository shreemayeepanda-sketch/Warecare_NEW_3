"""GPU detection: NVIDIA (nvidia-smi), AMD (rocm-smi), Intel (intel_gpu_top).

Optional tools, never fabricated. All queries use explicit argv + shell=False
+ a timeout and never raise: failures degrade to unavailable/unsupported.
"""
from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field

from .utils import which

log = logging.getLogger("shds.gpu")

NVIDIA_SMI_TIMEOUT = 8  # seconds


@dataclass
class GpuInfo:
    """Single GPU reading; unavailable metrics stay as None."""

    index: int = 0
    name: str = "Unavailable"
    utilization_pct: float | None = None
    temperature_c: float | None = None
    memory_used_mb: float | None = None
    memory_total_mb: float | None = None
    fan_pct: float | None = None
    power_w: float | None = None
    available: bool = False
    source: str = "nvidia-smi"
    error: str = ""


@dataclass
class GpuStatus:
    """Overall GPU provider state."""

    supported: bool = False
    tool_found: bool = False
    gpus: list[GpuInfo] = field(default_factory=list)
    message: str = "Unavailable"


def _to_float(raw: str) -> float | None:
    text = (raw or "").strip().lower().replace("w", "").replace("%", "").replace("c", "")
    if text in ("", "n/a", "unknown", "[n/a]", "n/a.", "none"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def query_nvidia_gpus(timeout: int = NVIDIA_SMI_TIMEOUT) -> GpuStatus:
    """Query NVIDIA GPUs safely with explicit argv and shell=False.

    Never raises: all failures are reported as unsupported/unavailable.
    """
    status = GpuStatus()
    exe = which("nvidia-smi")
    if not exe:
        status.message = "nvidia-smi not found — GPU metrics unsupported on this machine."
        log.info("nvidia-smi not found")
        return status
    status.tool_found = True
    argv = [
        exe,
        "--query-gpu=index,name,utilization.gpu,temperature.gpu,memory.used,memory.total,fan.speed,power.draw",
        "--format=csv,noheader,nounits",
    ]
    try:
        proc = subprocess.run(argv, shell=False, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except FileNotFoundError:
        status.message = "nvidia-smi executable disappeared."
        return status
    except subprocess.TimeoutExpired:
        status.message = f"nvidia-smi timed out after {timeout}s."
        log.warning("nvidia-smi timeout")
        return status
    except OSError as exc:
        status.message = f"Could not run nvidia-smi: {exc}"
        log.warning("nvidia-smi OSError: %s", exc)
        return status
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()[:300]
        status.message = f"nvidia-smi failed (exit {proc.returncode}): {err or 'no details'}"
        log.warning("nvidia-smi exit %s: %s", proc.returncode, err)
        return status
    lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
    if not lines:
        status.message = "nvidia-smi returned no GPU rows."
        return status
    gpus: list[GpuInfo] = []
    for line in lines:
        parts = [p.strip() for p in line.split(",")]
        # pad to 8 columns
        while len(parts) < 8:
            parts.append("")
        try:
            idx = int(float(parts[0])) if parts[0] not in ("", "N/A") else len(gpus)
        except ValueError:
            idx = len(gpus)
        gpu = GpuInfo(
            index=idx,
            name=parts[1] or "Unknown NVIDIA GPU",
            utilization_pct=_to_float(parts[2]),
            temperature_c=_to_float(parts[3]),
            memory_used_mb=_to_float(parts[4]),
            memory_total_mb=_to_float(parts[5]),
            fan_pct=_to_float(parts[6]),
            power_w=_to_float(parts[7]),
            available=True,
        )
        gpus.append(gpu)
    status.supported = True
    status.gpus = gpus
    status.message = f"Found {len(gpus)} NVIDIA GPU(s)."
    return status


def _loose_float(value) -> float | None:
    """Parse a possibly string-shaped number ("43.0", 12) or return None."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None  # NaN check


def query_rocm_gpus(timeout: int = NVIDIA_SMI_TIMEOUT) -> GpuStatus:
    """Query AMD GPUs via rocm-smi --json. Never raises; degrades honestly."""
    status = GpuStatus()
    exe = which("rocm-smi")
    if not exe:
        status.message = "rocm-smi not found — AMD GPU metrics unavailable."
        log.info("rocm-smi not found")
        return status
    status.tool_found = True
    argv = [exe, "--showtemp", "--showuse", "--showmemuse", "--json"]
    try:
        proc = subprocess.run(argv, shell=False, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except FileNotFoundError:
        status.message = "rocm-smi executable disappeared."
        return status
    except subprocess.TimeoutExpired:
        status.message = f"rocm-smi timed out after {timeout}s."
        log.warning("rocm-smi timeout")
        return status
    except OSError as exc:
        status.message = f"Could not run rocm-smi: {exc}"
        log.warning("rocm-smi OSError: %s", exc)
        return status
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()[:300]
        status.message = f"rocm-smi failed (exit {proc.returncode}): {err or 'no details'}"
        log.warning("rocm-smi exit %s: %s", proc.returncode, err)
        return status
    try:
        data = json.loads(proc.stdout or "")
    except (ValueError, TypeError):
        status.message = "rocm-smi returned non-JSON output."
        return status
    if not isinstance(data, dict):
        status.message = "rocm-smi JSON was not an object."
        return status
    gpus: list[GpuInfo] = []
    for card, info in data.items():
        if not isinstance(info, dict):
            continue
        util = temp = mem_used = None
        for key, value in info.items():
            low = str(key).lower()
            if "gpu use" in low and util is None:
                util = _loose_float(value)
            elif "temperature" in low and temp is None:
                temp = _loose_float(value)
            elif "vram total used" in low and mem_used is None:
                # Reported in bytes; surface as MiB for consistency with nvidia.
                raw = _loose_float(value)
                if raw is not None:
                    mem_used = raw / (1024 * 1024)
        card_index = 0
        digits = "".join(ch for ch in str(card) if ch.isdigit())
        if digits:
            card_index = int(digits)
        gpus.append(GpuInfo(
            index=card_index,
            name=f"AMD {card}",
            utilization_pct=util,
            temperature_c=temp,
            memory_used_mb=mem_used,
            available=True,
        ))
    if not gpus:
        status.message = "rocm-smi returned no GPU cards."
        return status
    status.supported = True
    status.gpus = gpus
    status.message = f"Found {len(gpus)} AMD GPU(s) via rocm-smi."
    return status


def query_intel_gpus(timeout: int = 3) -> GpuStatus:
    """Query Intel GPUs via intel_gpu_top -J (JSON per line).

    intel_gpu_top streams continuously, so the run is deliberately short:
    a TimeoutExpired is EXPECTED and its partial stdout is parsed. Without
    a full sample the metrics stay unavailable rather than invented.
    """
    status = GpuStatus()
    exe = which("intel_gpu_top")
    if not exe:
        status.message = "intel_gpu_top not found — Intel GPU metrics unavailable."
        log.info("intel_gpu_top not found")
        return status
    status.tool_found = True
    argv = [exe, "-J", "-s", "500"]
    stdout = ""
    timed_out = False
    try:
        proc = subprocess.run(argv, shell=False, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        stdout = proc.stdout or ""
        if proc.returncode != 0:
            err = (proc.stderr or "").strip()[:300]
            status.message = (f"intel_gpu_top failed (exit {proc.returncode}): "
                              f"{err or 'no details'}")
            log.warning("intel_gpu_top exit %s: %s", proc.returncode, err)
            return status
    except FileNotFoundError:
        status.message = "intel_gpu_top executable disappeared."
        return status
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = exc.stdout or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
    except OSError as exc:
        status.message = f"Could not run intel_gpu_top: {exc}"
        log.warning("intel_gpu_top OSError: %s", exc)
        return status

    util = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        gpu = data.get("gpu") or {}
        if isinstance(gpu, dict):
            util = _loose_float(gpu.get("busy"))
            if util is not None:
                break
    if util is None:
        status.message = ("intel_gpu_top produced no usable sample."
                          if timed_out else "intel_gpu_top returned no GPU data.")
        return status
    status.supported = True
    status.gpus = [GpuInfo(index=0, name="Intel GPU", utilization_pct=util,
                           available=True)]
    status.message = "Found 1 Intel GPU via intel_gpu_top."
    return status


def query_gpus(timeout: int = NVIDIA_SMI_TIMEOUT) -> GpuStatus:
    """Vendor chain: NVIDIA first, then AMD (rocm-smi), then Intel.

    Returns the first supported provider; when none is available the
    message lists what was tried so the user can see the whole story.
    """
    tried: list[str] = []
    for name, probe in (("nvidia-smi", query_nvidia_gpus),
                        ("rocm-smi", query_rocm_gpus),
                        ("intel_gpu_top", query_intel_gpus)):
        try:
            status = probe(timeout=timeout)  # type: ignore[operator]
        except Exception as exc:  # a buggy probe must never break the snapshot
            log.warning("gpu probe %s crashed: %s", name, exc)
            tried.append(f"{name} (error: {exc})")
            continue
        if status.supported and status.gpus:
            return status
        tried.append(f"{name} ({status.message[:80]})")
    return GpuStatus(message="No supported GPU metrics available. Tried: "
                     + "; ".join(tried))
