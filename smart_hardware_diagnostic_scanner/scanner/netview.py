"""Per-process network connections via psutil (read-only)."""
from __future__ import annotations

import logging
import socket
from dataclasses import dataclass
from typing import Iterable

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:  # optional dependency
    psutil = None  # type: ignore
    HAS_PSUTIL = False

log = logging.getLogger("shds.netview")

_SOCKET_KINDS = {
    socket.SOCK_STREAM: "TCP",
    socket.SOCK_DGRAM: "UDP",
}


@dataclass
class ConnRow:
    """One socket row; pid 0 / empty process name when the owner is unknown."""

    kind: str = ""      # "TCP" | "UDP" | other socket type name
    local: str = ""     # "ip:port" formatted
    remote: str = ""    # "ip:port" or "-" for listening sockets
    status: str = ""    # e.g. "LISTEN", "ESTABLISHED", "NONE"
    pid: int = 0
    process: str = ""   # process name or "" when unknown


def _format_addr(addr) -> str:
    """Format a psutil address tuple as "ip:port" ("-" when unknown).

    Dual-stack tuples (ip, port, flowinfo, scopeid) use their first two
    elements. An IPv6 ip (detected via ":" in the ip) is wrapped in brackets
    so the port stays readable, e.g. "[::1]:8080". Port 0 is shown as 0.
    """
    if not addr or len(addr) < 2:
        return "-"
    ip_text = str(addr[0] or "")
    if not ip_text:
        return "-"
    try:
        port = int(addr[1])
    except (TypeError, ValueError):
        return "-"
    if ":" in ip_text:
        return f"[{ip_text}]:{port}"
    return f"{ip_text}:{port}"


def _socket_kind(value) -> str:
    """Human readable socket type ("TCP"/"UDP" or the raw type as text)."""
    try:
        return _SOCKET_KINDS.get(int(value), str(value))
    except (TypeError, ValueError):
        return str(value)


def _conn_row(c, names: dict[int, str]) -> ConnRow:
    """One ConnRow from a psutil connection entry (never signals)."""
    pid = int(getattr(c, "pid", 0) or 0)
    return ConnRow(
        kind=_socket_kind(getattr(c, "type", 0)),
        local=_format_addr(getattr(c, "laddr", None)),
        remote=_format_addr(getattr(c, "raddr", None)),
        status=str(getattr(c, "status", "") or ""),
        pid=pid,
        process=names.get(pid, ""),
    )


def _sort_key(row: ConnRow) -> tuple[int, int, str]:
    """TCP ESTABLISHED first, then other TCP, then UDP; then by pid."""
    if row.kind == "TCP" and row.status == "ESTABLISHED":
        group = 0
    elif row.kind == "TCP":
        group = 1
    else:
        group = 2
    return (group, row.pid, row.local)


def _name_for(pid: int) -> str:
    """Name of one process or "" — best-effort, never signals anything."""
    try:
        return str(psutil.Process(pid).name() or "")[:60]
    except Exception as exc:  # NoSuchProcess, AccessDenied, anything else
        log.debug("pid %s name lookup failed: %s", pid, exc)
        return ""


def _pid_names(pids: Iterable[int]) -> dict[int, str]:
    """pid -> process name map; only pids above 0 are candidates."""
    names: dict[int, str] = {}
    for pid in sorted({p for p in pids if p > 0}):
        names[pid] = _name_for(pid)
    return names


def collect_connections(limit: int = 200) -> tuple[list[ConnRow], str]:
    """Snapshot the connection table with owning process names.

    Read-only and never raises: rows are built from psutil's connection
    table plus process command names; nothing is terminated, signalled, or
    otherwise touched. All failures degrade to an empty list and a message.
    """
    if not HAS_PSUTIL:
        return [], "psutil not installed"
    try:
        conns = psutil.net_connections(kind="inet")
    except psutil.AccessDenied:
        return [], "connection table requires elevated privileges on this system"
    except Exception as exc:
        log.warning("net_connections failed: %s", exc)
        return [], f"could not read connection table: {exc}"
    names = _pid_names(getattr(c, "pid", 0) or 0 for c in conns)
    rows: list[ConnRow] = []
    for c in conns:
        try:
            rows.append(_conn_row(c, names))
        except Exception as exc:
            log.debug("connection row skipped: %s", exc)
    rows.sort(key=_sort_key)
    try:
        cap = int(limit)
    except (TypeError, ValueError):
        cap = 200
    shown = rows[: max(0, cap)]
    if not shown:
        return [], "no connections reported."
    if len(shown) < len(rows):
        return shown, f"showing first {len(shown)} of {len(rows)} connections"
    return shown, f"{len(shown)} connection(s) listed."
