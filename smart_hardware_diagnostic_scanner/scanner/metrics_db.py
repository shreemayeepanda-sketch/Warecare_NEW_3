"""SQLite metrics time-series for the Predictive Health engine.

An append-only history of CPU %, RAM % and per-mount disk usage, recorded
roughly every 60 s by a background thread (the standalone app's
``MetricsRecorder``), powering the disk-full forecast in
``scanner.predictive``. SQLite only -- no new dependencies, no numpy.

This is a pure logic module: no tkinter, no GUI imports, no side effects at
import time and no intra-package imports, so it is safe to import from
background threads and tests, standalone (``import metrics_db``) as well as
under the package (``from scanner import metrics_db`` -- preferred).

Data root: the per-user application-data directory used all across the app
("C:\\Users\\<you>\\AppData\\Local\\SmartHardwareDiagnosticScanner" on
Windows, XDG/Library equivalents elsewhere) -- the same root
``scanner.logging_config`` picks, so every app artefact lives together.
Callers pass the db path explicitly (the recorder resolves
``default_db_path()`` once); tests pass ``tmp_path``-rooted paths.

Schema v1 (created lazily on the first write; never modified or dropped):

    CREATE TABLE IF NOT EXISTS sample (     -- one row per recording tick
        ts       INTEGER PRIMARY KEY,       -- unix epoch seconds (UTC)
        cpu_pct  REAL,                      -- 0..100 or NULL
        ram_pct  REAL                       -- 0..100 or NULL
    );
    CREATE TABLE IF NOT EXISTS disk_sample (  -- one row per mount per tick
        ts          INTEGER NOT NULL,        -- unix epoch seconds (UTC)
        mount       TEXT    NOT NULL,        -- mount point (e.g. "C:\\")
        pct         REAL,                    -- used percent 0..100 or NULL
        used_bytes  INTEGER,                 -- or NULL
        total_bytes INTEGER,                 -- or NULL
        PRIMARY KEY (ts, mount)
    );
    CREATE INDEX IF NOT EXISTS idx_disk_sample_mount_ts
        ON disk_sample (mount, ts);

Ingest contract: samples are idempotent per tick via the PRIMARY KEYs and
``INSERT OR IGNORE`` semantics -- re-recording the same ``ts`` changes
nothing (except a genuinely new mount, which is a new (ts, mount) key), so
a retried write or restarted recorder can never duplicate history. ``ts``
is an int (seconds); non-finite percents and impossible byte counts
degrade to NULL.

Concurrency: one short-lived connection per call (created, used and closed
within one call on one thread), WAL journal + ``busy_timeout`` set on every
connection so a read during the recorder's write never raises "database is
locked" on Windows. Failures never propagate into caller threads: writes
return False, reads return empty/None results, everything is logged.

Public API (a fixed contract shared with other agents -- do not reshape):

    default_db_path() -> Path          per-user metrics.db (NOT created)
    availability() -> tuple[bool, str] (True, "ok") or (False, reason)
    record_sample(db_path, ts, cpu_pct, ram_pct, disks) -> bool
    last_ts(db_path) -> int | None
    disk_mounts(db_path) -> list[str]
    disk_series(db_path, mount, hours=None, now_ts=None) -> list[...]
    cpu_ram_series(db_path, hours=None, now_ts=None) -> list[...]
    prune(db_path, keep_hours=720.0, now_ts=None) -> int

Time-window semantics (documented edge): ``hours``-limited queries return
rows with ``ts >= now - hours*3600`` -- the boundary row itself is INCLUDED
("newer than" is inclusive at the exact cutoff). ``prune`` is the mirror
image: only rows with ``ts < cutoff`` are deleted, a row exactly at the
cutoff is KEPT. ``int(time.time())`` is used when ``now_ts`` is None.
"""

from __future__ import annotations

import logging
import math
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Sequence

__all__ = ["default_db_path", "availability", "record_sample", "last_ts",
           "disk_mounts", "disk_series", "cpu_ram_series", "prune"]

log = logging.getLogger(__name__)

APP_NAME = "SmartHardwareDiagnosticScanner"

_BUSY_TIMEOUT_MS = 5000
_CONNECT_TIMEOUT_S = 5.0

_SCHEMA_STATEMENTS = (
    "CREATE TABLE IF NOT EXISTS sample ("
    " ts INTEGER PRIMARY KEY, cpu_pct REAL, ram_pct REAL)",
    "CREATE TABLE IF NOT EXISTS disk_sample ("
    " ts INTEGER NOT NULL, mount TEXT NOT NULL, pct REAL,"
    " used_bytes INTEGER, total_bytes INTEGER,"
    " PRIMARY KEY (ts, mount))",
    "CREATE INDEX IF NOT EXISTS idx_disk_sample_mount_ts"
    " ON disk_sample (mount, ts)",
)

# ---------------------------------------------------------------------------
# Path + availability (per-user root, mirroring the app's data-root logic)
# ---------------------------------------------------------------------------


def _user_data_root() -> Path:
    """Return the per-user application-data root (logging_config's choice)."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / APP_NAME
        base = os.environ.get("APPDATA")  # rare: LOCALAPPDATA missing
        if base:
            return Path(base) / APP_NAME
        return Path.home() / "AppData" / "Local" / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    base = os.environ.get("XDG_DATA_HOME")
    if base:
        return Path(base) / APP_NAME
    return Path.home() / ".local" / "share" / APP_NAME


def default_db_path() -> Path:
    """Per-user ``metrics.db`` path (the directory is NOT created here)."""
    return _user_data_root() / "metrics.db"


def availability() -> tuple[bool, str]:
    """``(True, "ok")`` when the per-user data directory is writable now,
    else ``(False, reason)``. Probes a temp file -- never raises, never
    leaves the probe behind."""
    try:
        directory = default_db_path().parent
    except Exception as exc:
        return False, f"could not resolve a metrics database path: {exc}"
    probe = directory / ".metrics_write_probe"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe.write_text("probe", encoding="utf-8")
        probe.unlink()
    except (OSError, PermissionError) as exc:
        return False, f"metrics directory not writable ({directory}): {exc}"
    return True, "ok"


# ---------------------------------------------------------------------------
# Connection + value helpers
# ---------------------------------------------------------------------------


def _connect(db_path: Path, ensure_schema: bool = True) -> sqlite3.Connection:
    """Open one short-lived connection; WAL + busy timeout; optional schema.

    Every connection is created, used and closed within one call on one
    thread, so the sqlite3 ``check_same_thread`` contract still holds; the
    busy timeout plus WAL are what make concurrent recorder-writes/UI-reads
    safe on Windows. Raises (sqlite3.Error, OSError) -- callers shield.
    """
    conn = sqlite3.connect(str(db_path), timeout=_CONNECT_TIMEOUT_S)
    try:
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        try:
            # WAL keeps a recorder write from blocking a reader; unsupported
            # on some filesystems (network shares) -> stay in journal mode.
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            log.debug("WAL journal unavailable for %s", db_path)
        conn.execute("PRAGMA synchronous=NORMAL")
        if ensure_schema:
            for statement in _SCHEMA_STATEMENTS:
                conn.execute(statement)
            conn.commit()
    except (sqlite3.Error, OSError):
        conn.close()
        raise
    return conn


def _close_quiet(conn) -> None:
    """Close a connection; a close failure is logged, never raised."""
    try:
        conn.close()
    except sqlite3.Error:
        log.debug("metrics connection close failed")


def _cutoff_ts(hours: float | None, now_ts: int | None) -> int | None:
    """Cutoff epoch seconds for an ``hours`` window (None => unbounded).

    Inclusive-by-contract helper: callers use ``ts >= cutoff`` to keep the
    boundary row, or ``ts < cutoff`` (prune) to delete only older rows.
    """
    if hours is None:
        return None
    try:
        window_s = float(hours) * 3600.0
    except (TypeError, ValueError):
        log.debug("metrics window %r unusable; treating as unbounded", hours)
        return None
    if not math.isfinite(window_s):
        return None
    try:
        base = int(now_ts) if now_ts is not None else int(time.time())
    except (TypeError, ValueError, OverflowError):
        base = int(time.time())
    return int(base - window_s)


def _pct_or_none(value) -> float | None:
    """A finite float, or None (never NaN/inf); never raises."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _bytes_or_none(value) -> int | None:
    """A non-negative int, or None; never raises."""
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number >= 0 else None


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def record_sample(db_path: Path, ts: int, cpu_pct: float | None,
                  ram_pct: float | None,
                  disks: Sequence[tuple[str, float | None, int | None,
                                          int | None]]) -> bool:
    """Record one atomic tick: CPU/RAM percent + per-mount disk usage.

    ``disks`` holds ``(mount, used_pct, used_bytes, total_bytes)`` tuples;
    ``None`` values are stored as NULL, unusable values degrade to NULL and
    empty mounts are skipped. Idempotent per ``ts`` (INSERT OR IGNORE).
    Returns True on success; False + a logged warning on any database or
    filesystem failure -- never raises into caller threads.
    """
    try:
        timestamp = int(ts)
    except (TypeError, ValueError, OverflowError):
        log.warning("record_sample: unusable timestamp %r; sample dropped", ts)
        return False
    rows: list[tuple[str, float | None, int | None, int | None]] = []
    for entry in disks or ():
        try:
            mount, pct, used_bytes, total_bytes = entry
        except (TypeError, ValueError):
            log.debug("record_sample: unusable disk entry %r skipped", entry)
            continue
        mount_name = str(mount or "").strip()
        if not mount_name:
            continue  # a mount-less disk row cannot be keyed or forecast
        rows.append((mount_name, _pct_or_none(pct), _bytes_or_none(used_bytes),
                     _bytes_or_none(total_bytes)))
    try:
        conn = _connect(Path(db_path), ensure_schema=True)
    except Exception as exc:
        log.warning("metrics db not openable (%s): %s", db_path, exc)
        return False
    try:
        with conn:  # one transaction: all-or-nothing per tick
            conn.execute(
                "INSERT OR IGNORE INTO sample (ts, cpu_pct, ram_pct)"
                " VALUES (?, ?, ?)",
                (timestamp, _pct_or_none(cpu_pct), _pct_or_none(ram_pct)))
            for mount, pct, used, total in rows:
                conn.execute(
                    "INSERT OR IGNORE INTO disk_sample"
                    " (ts, mount, pct, used_bytes, total_bytes)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (timestamp, mount, pct, used, total))
        return True
    except Exception as exc:
        log.warning("record_sample failed at ts=%s: %s", timestamp, exc)
        return False
    finally:
        _close_quiet(conn)


def prune(db_path: Path, keep_hours: float = 720.0,
          now_ts: int | None = None) -> int:
    """Delete rows strictly older than ``keep_hours`` from both tables.

    Returns the number of rows deleted (sample + disk_sample); a row
    exactly at the cutoff is kept (see the module time-window note); a
    missing database file is a silent no-op (pruning never creates it);
    failures return 0 (logged) -- never raises into caller threads;
    VACUUM is intentionally NOT run (WAL reuses most space over time).
    """
    cutoff = _cutoff_ts(keep_hours, now_ts)
    if cutoff is None or not Path(db_path).is_file():
        return 0
    try:
        conn = _connect(Path(db_path), ensure_schema=True)
    except Exception as exc:
        log.warning("metrics db not openable for prune (%s): %s", db_path, exc)
        return 0
    try:
        with conn:  # both tables pruned in one transaction
            deleted = max(int(conn.execute(
                "DELETE FROM sample WHERE ts < ?", (cutoff,)).rowcount), 0)
            deleted += max(int(conn.execute(
                "DELETE FROM disk_sample WHERE ts < ?", (cutoff,)).rowcount), 0)
        return deleted
    except Exception as exc:
        log.warning("metrics prune failed (cutoff=%s): %s", cutoff, exc)
        return 0
    finally:
        _close_quiet(conn)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def _fetch(db_path: Path, query: str, params: Sequence = ()) -> list | None:
    """Run one read on an EXISTING database; None if missing/failing.

    Reads never create the database file (a fresh install must be able to
    query an empty history without writing to disk) and never raise.
    """
    path = Path(db_path)
    if not path.is_file():
        return None
    try:
        conn = _connect(path, ensure_schema=False)
    except Exception as exc:
        log.debug("metrics db read unavailable (%s): %s", path, exc)
        return None
    try:
        return conn.execute(query, tuple(params)).fetchall()
    except Exception as exc:
        log.debug("metrics read failed (%s): %s", path, exc)
        return None
    finally:
        _close_quiet(conn)


def last_ts(db_path: Path) -> int | None:
    """Newest recorded epoch second of any sample, or None for no history."""
    rows = _fetch(db_path, "SELECT MAX(ts) FROM (SELECT MAX(ts) AS ts"
                           " FROM sample UNION ALL SELECT MAX(ts)"
                           " FROM disk_sample)")
    if not rows:
        return None
    value = rows[0][0]
    return int(value) if value is not None else None


def disk_mounts(db_path: Path) -> list[str]:
    """Distinct mounts ever seen, sorted; empty list when no history."""
    rows = _fetch(db_path,
                  "SELECT DISTINCT mount FROM disk_sample ORDER BY mount")
    return [str(row[0]) for row in (rows or [])]


def disk_series(db_path: Path, mount: str, hours: float | None = None,
                now_ts: int | None = None
                ) -> list[tuple[int, float | None, int | None, int | None]]:
    """One mount's ``(ts, used_pct, used_bytes, total_bytes)`` history,
    ascending by ``ts`` with NULLs preserved; ``hours``/``now_ts`` window
    semantics per the module docstring. Unknown mount -> []."""
    cutoff = _cutoff_ts(hours, now_ts)
    if cutoff is None:
        rows = _fetch(db_path, "SELECT ts, pct, used_bytes, total_bytes FROM"
                               " disk_sample WHERE mount = ? ORDER BY ts ASC",
                      (str(mount),))
    else:
        rows = _fetch(db_path, "SELECT ts, pct, used_bytes, total_bytes FROM"
                               " disk_sample WHERE mount = ? AND ts >= ?"
                               " ORDER BY ts ASC", (str(mount), cutoff))
    return [(int(row[0]), row[1], row[2], row[3]) for row in (rows or [])]


def cpu_ram_series(db_path: Path, hours: float | None = None,
                   now_ts: int | None = None
                   ) -> list[tuple[int, float | None, float | None]]:
    """System ``(ts, cpu_pct, ram_pct)`` history from ``sample``, ascending
    by ``ts`` with NULLs preserved; window semantics match disk_series().
    No history -> []."""
    cutoff = _cutoff_ts(hours, now_ts)
    if cutoff is None:
        rows = _fetch(db_path, "SELECT ts, cpu_pct, ram_pct FROM sample"
                               " ORDER BY ts ASC")
    else:
        rows = _fetch(db_path, "SELECT ts, cpu_pct, ram_pct FROM sample"
                               " WHERE ts >= ? ORDER BY ts ASC", (cutoff,))
    return [(int(row[0]), row[1], row[2]) for row in (rows or [])]
