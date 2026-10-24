"""Predictive Health metrics tests -- scanner.metrics_db + MetricsRecorder.

All database paths are pytest ``tmp_path`` rooted: no test ever touches the
real per-user ``%LOCALAPPDATA%`` directory, never spawns real psutil
sampling and never starts an actual GUI. Coverage:

* Schema v1 creation (both tables + the mount/timestamp index) on first
  write, and the fact that reads never create the database file;
* CPU/RAM round-trip including NULLs and value degradation (NaN/inf,
  negative or junk byte counts degrade to NULL, never raise);
* Idempotency per ``ts`` (incl. two threads racing the same tick);
* Series windows: ascending order, ``hours`` filter with an INJECTED
  ``now_ts``, inclusive boundary row, and the zero-length window edge;
* Prune: strictly-older deletion (a row exactly at the cutoff is kept),
  the deleted-rows return value, and the missing-db no-op;
* availability(): ok on a writable tmp dir and the Windows-safe
  "file-as-directory" unwritable degrade;
* record_sample failure contract: returns False (never raises) when
  sqlite3.connect is patched to blow up, or for an unusable timestamp;
* Cross-thread safety: a writer thread recording while the main thread
  reads, joined well within the suite budget;
* The standalone MetricsRecorder glue: refuses to start (with a debug
  reason) when the shared module is unavailable, psutil is missing or the
  metrics directory is unavailable; a tick records exactly what an
  injected sampler produced; a sampler exception degrades to False;
  start/stop are idempotent and the thread joins cleanly.
"""
from __future__ import annotations

import importlib.util
import logging
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG_PARENT = ROOT / "smart_hardware_diagnostic_scanner"
if str(PKG_PARENT) not in sys.path:
    sys.path.insert(0, str(PKG_PARENT))

STANDALONE = ROOT / "ewarecare.py"

import scanner.metrics_db as metrics_db  # noqa: E402

NOW = 1_700_000_000
H = 3600


def _load_ewarecare():
    """Import the standalone build from its file path.

    Mirrors tests/test_standalone_edge_cases.py: importlib must register the
    module in ``sys.modules`` BEFORE ``exec_module`` (dataclass metadata
    scan), and the file's ``__main__`` guard keeps the GUI from launching.
    A distinct module name avoids colliding with the other standalone test
    files' own loads in the same pytest process.
    """
    assert STANDALONE.exists(), f"missing {STANDALONE}"
    name = "ewarecare_metrics_tests"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, STANDALONE)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    mod = sys.modules[name]
    assert mod.__name__ != "__main__", "GUI must not launch on import"
    return mod


ewarecare = _load_ewarecare()


@pytest.fixture()
def metric_logs():
    """Direct handler-based spy on the "shds.metrics" logger.

    The standalone app's configure_logging() sets propagate=False on the
    "shds" logger, so records from its children never reach the root ---
    pytest's caplog (root handler) would see nothing. Attaching a spy to
    the child logger directly, at DEBUG, observes exactly what the
    MetricsRecorder logs.
    """
    messages: list[str] = []

    class _Spy(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())

    spy = _Spy()
    logger = logging.getLogger("shds.metrics")
    logger.setLevel(logging.DEBUG)
    logger.addHandler(spy)
    try:
        yield messages
    finally:
        logger.removeHandler(spy)
        logger.setLevel(logging.NOTSET)


def _db(tmp_path: Path) -> Path:
    return tmp_path / "metrics.db"


def _objects(db: Path) -> set[str]:
    """All table/index names in the database (the schema v1 fingerprint)."""
    con = sqlite3.connect(str(db))
    try:
        rows = con.execute(
            "SELECT name, type FROM sqlite_master"
            " WHERE type IN ('table', 'index')").fetchall()
        return {row[0] for row in rows}
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Schema + round-trips
# ---------------------------------------------------------------------------


class TestSchemaAndRoundtrip:
    def test_first_write_creates_schema_v1(self, tmp_path):
        """One record_sample call creates both tables + the mount/ts index."""
        db = _db(tmp_path)
        assert metrics_db.record_sample(
            db, NOW, 12.5, 34.5, [("C:/", 55.5, 100, 200)]) is True
        names = _objects(db)
        assert "sample" in names and "disk_sample" in names
        assert "idx_disk_sample_mount_ts" in names

    def test_cpu_ram_roundtrip(self, tmp_path):
        db = _db(tmp_path)
        metrics_db.record_sample(db, NOW, 12.5, 34.5, [])
        metrics_db.record_sample(db, NOW + 60, 0.0, 100.0, [])
        assert metrics_db.cpu_ram_series(db) == [
            (NOW, 12.5, 34.5), (NOW + 60, 0.0, 100.0)]

    def test_nulls_round_trip_through_the_api(self, tmp_path):
        """None cpu/ram and None disk fields read back as None (everywhere)."""
        db = _db(tmp_path)
        metrics_db.record_sample(db, NOW, None, 42.0,
                                 [("C:/", None, None, 500)])
        assert metrics_db.cpu_ram_series(db) == [(NOW, None, 42.0)]
        assert metrics_db.disk_series(db, "C:/") == [(NOW, None, None, 500)]

    def test_unusable_values_degrade_to_null(self, tmp_path):
        """NaN/inf percents and negative/junk byte counts store NULL; a
        mount-less tuple is skipped instead of breaking the primary key."""
        db = _db(tmp_path)
        metrics_db.record_sample(db, NOW, float("nan"), float("inf"),
                                 [("", 10.0, 0, 0),
                                  ("C:/", 50.0, -4, "junk")])
        assert metrics_db.cpu_ram_series(db) == [(NOW, None, None)]
        assert metrics_db.disk_series(db, "C:/") == [(NOW, 50.0, None, None)]

    def test_same_ts_recorded_twice_is_idempotent(self, tmp_path):
        """Re-recording a tick changes nothing (INSERT OR IGNORE per the
        (ts) and (ts, mount) keys); only a physically NEW mount at the same
        tick legitimately gains a row."""
        db = _db(tmp_path)
        disks = [("C:/", 55.0, 100, 200), ("D:/", 10.0, 5, 50)]
        assert metrics_db.record_sample(db, NOW, 10.0, 20.0, disks) is True
        assert metrics_db.record_sample(db, NOW, 99.0, 98.0, disks) is True
        assert metrics_db.cpu_ram_series(db) == [(NOW, 10.0, 20.0)]
        assert metrics_db.disk_mounts(db) == ["C:/", "D:/"]
        assert metrics_db.disk_series(db, "C:/") == [(NOW, 55.0, 100, 200)]
        assert metrics_db.disk_series(db, "D:/") == [(NOW, 10.0, 5, 50)]

    def test_disk_mounts_distinct_and_sorted(self, tmp_path):
        db = _db(tmp_path)
        for index, mount in enumerate(("C:/", "D:/", "A:/")):
            metrics_db.record_sample(db, NOW + index * 60, None, None,
                                     [(mount, 0.0, 0, 1)])
        assert metrics_db.disk_mounts(db) == ["A:/", "C:/", "D:/"]


# ---------------------------------------------------------------------------
# Reads + time windows
# ---------------------------------------------------------------------------


class TestReadsAndWindows:
    def test_reads_never_create_the_db(self, tmp_path):
        """Reads on a database that does not exist yet: honest empties, and
        the file must still be missing afterwards."""
        db = _db(tmp_path)
        assert metrics_db.last_ts(db) is None
        assert metrics_db.disk_mounts(db) == []
        assert metrics_db.disk_series(db, "C:/") == []
        assert metrics_db.cpu_ram_series(db) == []
        assert not db.exists()

    def test_last_ts_is_max_across_both_tables(self, tmp_path):
        db = _db(tmp_path)
        metrics_db.record_sample(db, NOW, None, None, [("C:/", 1, 1, 2)])
        metrics_db.record_sample(db, NOW + 5 * H, 3.0, 4.0, [])
        assert metrics_db.last_ts(db) == NOW + 5 * H

    def test_disk_series_hours_window_is_inclusive_with_injected_now(self,
                                                                     tmp_path):
        """hours=24 and an injected now: the row exactly at the boundary
        (now - 24 h) is INCLUDED, older rows are not."""
        db = _db(tmp_path)
        metrics_db.record_sample(db, NOW - 25 * H, None, None,
                                 [("C:/", 5.0, 5, 10)])
        metrics_db.record_sample(db, NOW - 24 * H, None, None,
                                 [("C:/", 15.0, 15, 20)])
        metrics_db.record_sample(db, NOW - 3 * H, None, None,
                                 [("C:/", 65.0, 65, 70)])
        series = metrics_db.disk_series(db, "C:/", hours=24.0, now_ts=NOW)
        assert [row[1] for row in series] == [15.0, 65.0]
        # A 25 h-day window reaches the 25 h-old row too (boundary inclusive).
        assert [row[1] for row in
                metrics_db.disk_series(db, "C:/", hours=25.0, now_ts=NOW)] \
            == [5.0, 15.0, 65.0]
        unknown = metrics_db.disk_series(db, "D:/", hours=24.0, now_ts=NOW)
        assert unknown == []

    def test_cpu_ram_series_hours_window(self, tmp_path):
        db = _db(tmp_path)
        metrics_db.record_sample(db, NOW - 30 * H, 1.0, 2.0, [])
        metrics_db.record_sample(db, NOW - 2 * H, 3.0, 4.0, [])
        metrics_db.record_sample(db, NOW - 0.5 * H, 5.0, 6.0, [])
        assert metrics_db.cpu_ram_series(db, hours=2.0, now_ts=NOW) == [
            (NOW - 2 * H, 3.0, 4.0), (NOW - 0.5 * H, 5.0, 6.0)]

    def test_zero_hours_window_keeps_only_exact_boundary_rows(self, tmp_path):
        """hours=0: cutoff == now, so only a sample stamped exactly `now`
        survives the filter (a documented never-empty-by-invention edge)."""
        db = _db(tmp_path)
        metrics_db.record_sample(db, NOW - 1, 1.0, 1.0, [])
        metrics_db.record_sample(db, NOW, 7.0, 8.0, [])
        assert metrics_db.cpu_ram_series(db, hours=0.0, now_ts=NOW) == [
            (NOW, 7.0, 8.0)]


# ---------------------------------------------------------------------------
# Prune (retention)
# ---------------------------------------------------------------------------


class TestPrune:
    def test_prune_keeps_the_row_exactly_at_the_cutoff(self, tmp_path):
        """Deletion is strictly-older: with keep_hours == 3 and an injected
        now, the sample stamped at exactly now-3h must survive while the
        one a second older is dropped -- from both tables."""
        db = _db(tmp_path)
        metrics_db.record_sample(db, NOW - 3 * H - 1, 1.0, 1.0,
                                 [("C:/", 90.0, 9, 10)])
        metrics_db.record_sample(db, NOW - 3 * H, 2.0, 2.0,
                                 [("C:/", 80.0, 8, 10)])
        metrics_db.record_sample(db, NOW - 1 * H, 3.0, 3.0,
                                 [("C:/", 70.0, 7, 10)])
        deleted = metrics_db.prune(db, keep_hours=3.0, now_ts=NOW)
        assert deleted == 2  # one sample row + one disk row from 3h+1s
        assert metrics_db.cpu_ram_series(db) == [
            (NOW - 3 * H, 2.0, 2.0), (NOW - 1 * H, 3.0, 3.0)]
        assert [row[1] for row in metrics_db.disk_series(db, "C:/")] == \
            [80.0, 70.0]

    def test_prune_on_missing_db_is_a_silent_noop(self, tmp_path):
        db = _db(tmp_path)
        assert metrics_db.prune(db, keep_hours=1.0, now_ts=NOW) == 0
        assert not db.exists()  # retention must not create the file either


# ---------------------------------------------------------------------------
# availability() + degradation contracts
# ---------------------------------------------------------------------------


class TestAvailabilityAndFailures:
    def test_availability_ok_on_a_writable_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(metrics_db, "default_db_path",
                            lambda: tmp_path / "metrics.db")
        assert metrics_db.availability() == (True, "ok")
        # The probe file is gone again -- availability must not litter.
        assert not (tmp_path / ".metrics_write_probe").exists()

    def test_availability_blocked_by_a_file_in_the_path(self, tmp_path,
                                                       monkeypatch):
        """Windows-safe unwritable probe: a FILE occupies the directory
        position, so mkdir degrade fires with a readable reason instead of
        raising (chmod semantics are unreliable on Windows)."""
        blocker = tmp_path / "blocker"
        blocker.write_text("occupied", encoding="utf-8")
        monkeypatch.setattr(metrics_db, "default_db_path",
                            lambda: blocker / "metrics.db")
        ok, reason = metrics_db.availability()
        assert ok is False
        assert "writable" in reason

    def test_record_sample_returns_false_when_connect_fails(self, tmp_path,
                                                            monkeypatch):
        def broken_connect(*_args, **_kwargs):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(sqlite3, "connect", broken_connect)
        ok = metrics_db.record_sample(_db(tmp_path), NOW, 1.0, 2.0,
                                      [("C:/", 3.0, 3, 4)])
        assert ok is False  # degraded, never raised into the caller thread

    def test_record_sample_rejects_an_unusable_timestamp(self, tmp_path):
        db = _db(tmp_path)
        assert metrics_db.record_sample(db, "not-a-number", 1.0, 1.0,
                                        []) is False
        assert not db.exists()  # nothing was created for a dropped sample


# ---------------------------------------------------------------------------
# Cross-thread safety (the recorder writes while the UI reads)
# ---------------------------------------------------------------------------


class TestCrossThread:
    def test_writer_thread_records_while_main_thread_reads(self, tmp_path):
        """A background writer and simultaneous main-thread reads must not
        raise, interleave corrupt rows or drop samples (WAL + busy_timeout
        carry the contention)."""
        db = _db(tmp_path)
        ticks = 8
        done = threading.Event()

        def simulate_recorder():
            for offset in range(ticks):
                stored = metrics_db.record_sample(
                    db, NOW + offset, 11.0 + offset, 55.0,
                    [("C:/", 50.0 + offset, 100 + offset, 200)])
                assert stored is True
                time.sleep(0.002)  # give readers a real interleaving chance
            done.set()

        writer = threading.Thread(target=simulate_recorder, name="writer")
        writer.start()
        reads = 0
        while not done.wait(timeout=0.05):
            metrics_db.disk_series(db, "C:/")
            metrics_db.cpu_ram_series(db)
            metrics_db.last_ts(db)
            reads += 1
        writer.join(timeout=10.0)
        assert not writer.is_alive() and reads >= 1
        assert metrics_db.disk_mounts(db) == ["C:/"]
        series = metrics_db.disk_series(db, "C:/")
        assert [row[0] for row in series] == [NOW + o for o in range(ticks)]
        assert metrics_db.cpu_ram_series(db)[0] == (NOW, 11.0, 55.0)

    def test_concurrent_same_ts_writers_stay_idempotent(self, tmp_path):
        """Two threads recording the SAME tick: both report success, but the
        (ts, mount) primary key keeps exactly one row."""
        db = _db(tmp_path)
        results: list[bool] = []
        barrier = threading.Barrier(2)
        lock = threading.Lock()

        def writer(pct: float) -> None:
            barrier.wait(timeout=10.0)
            ok = metrics_db.record_sample(db, NOW, pct, 40.0,
                                          [("C:/", pct, 1, 2)])
            with lock:
                results.append(ok)

        threads = [threading.Thread(target=writer, args=(value, ),
                                    name=f"writer-{value}")
                   for value in (10.0, 22.0)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10.0)
        assert results == [True, True]
        rows = metrics_db.disk_series(db, "C:/")
        assert len(rows) == 1
        assert rows[0][1] in (10.0, 22.0)  # exactly one winner, no mixing
        assert metrics_db.cpu_ram_series(db) in (
            [(NOW, 10.0, 40.0)], [(NOW, 22.0, 40.0)])
        assert metrics_db.last_ts(db) == NOW


# ---------------------------------------------------------------------------
# Standalone MetricsRecorder glue (ewarecare.py integration)
# ---------------------------------------------------------------------------


class TestMetricsRecorder:
    def _patch_paths(self, monkeypatch, tmp_path):
        """Keep every probe/write inside tmp_path for recorder tests."""
        monkeypatch.setattr(metrics_db, "default_db_path",
                            lambda: tmp_path / "metrics.db")

    def test_recorder_refuses_when_metrics_unavailable(self, monkeypatch,
                                                       tmp_path, metric_logs):
        self._patch_paths(monkeypatch, tmp_path)
        monkeypatch.setattr(ewarecare, "METRICS_AVAILABLE", False)
        rec = ewarecare.MetricsRecorder(db_path=tmp_path / "metrics.db")
        assert rec.start() is False
        assert rec.running is False and rec._thread is None
        assert len(metric_logs) == 1  # the one debug line that explains why
        assert "metrics_db unavailable" in metric_logs[0].lower()

    def test_recorder_refuses_without_psutil_when_no_sampler(self, monkeypatch,
                                                             tmp_path,
                                                             metric_logs):
        self._patch_paths(monkeypatch, tmp_path)
        monkeypatch.setattr(ewarecare, "HAS_PSUTIL", False)
        rec = ewarecare.MetricsRecorder(db_path=tmp_path / "metrics.db")
        assert rec.start() is False
        assert len(metric_logs) == 1
        assert "psutil" in metric_logs[0].lower()

    def test_recorder_refuses_when_db_directory_unavailable(self, monkeypatch,
                                                            tmp_path,
                                                            metric_logs):
        """availability() failing degrades the start with its reason logged."""
        self._patch_paths(monkeypatch, tmp_path)
        monkeypatch.setattr(metrics_db, "availability",
                            lambda: (False, "disk gone"))
        rec = ewarecare.MetricsRecorder(db_path=tmp_path / "metrics.db")
        assert rec.start() is False
        assert len(metric_logs) == 1
        assert "disk gone" in metric_logs[0]

    def test_recorder_tick_uses_injected_sampler(self, tmp_path):
        """One tick stores exactly what the injected sampler produced (no
        real psutil call anywhere in this test)."""
        db = tmp_path / "metrics.db"
        calls: list = []

        def fake_sampler():
            calls.append(1)
            return 12.75, 50.25, [("C:/", 61.5, 12345, 67890)]

        rec = ewarecare.MetricsRecorder(db_path=db, interval=3600.0,
                                        sampler=fake_sampler)
        assert rec.interval == 3600.0
        assert rec.tick() is True
        assert len(calls) == 1
        rows = metrics_db.cpu_ram_series(db)
        assert len(rows) == 1
        assert rows[0][1] == 12.75 and rows[0][2] == 50.25
        ts = rows[0][0]  # tick-chosen timestamp, boundary-proof pairing
        assert metrics_db.disk_series(db, "C:/")[-1] == (ts, 61.5, 12345,
                                                         67890)

    def test_recorder_tick_survives_sampler_exception(self, tmp_path,
                                                      metric_logs):
        def exploding_sampler():
            raise RuntimeError("sensors unplugged")

        rec = ewarecare.MetricsRecorder(
            db_path=tmp_path / "metrics.db", sampler=exploding_sampler)
        assert rec.tick() is False  # degraded, not raised
        assert not (tmp_path / "metrics.db").exists()  # nothing half-written
        assert any("sensors unplugged" in message
                   for message in metric_logs)

    def test_recorder_start_stop_lifecycle(self, tmp_path, monkeypatch):
        """start() -> ticks flow into the db; a second start is refused; stop
        joins the thread (twice) and the flags end consistent."""
        db = tmp_path / "metrics.db"
        self._patch_paths(monkeypatch, tmp_path)
        counter = []
        lock = threading.Lock()

        def fake_sampler():
            with lock:
                counter.append(1)
            return 1.5, 2.5, [("C:/", 3.5, 7, 9)]

        rec = ewarecare.MetricsRecorder(db_path=db, interval=0.05,
                                        sampler=fake_sampler)
        assert rec.start() is True
        try:
            assert rec.start() is False  # idempotent while running
            # Let the loop tick before stopping: a stop() fired within the
            # first interval legitimately exits the loop with zero ticks.
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and len(counter) < 3:
                time.sleep(0.02)
            assert len(counter) >= 3, "recorder did not tick while running"
        finally:
            assert rec.stop() is None  # None-returning join on a helper
        assert rec.stop() is None  # ...and stopping an idle recorder is fine
        assert rec.running is False
        rows = metrics_db.cpu_ram_series(db)
        # Every tick recorded the same second-metered values; samples falling
        # inside one wall-clock second collapse to one idempotent row.
        assert 1 <= len(rows) <= len(counter)
        assert len({row[0] for row in rows}) == len(rows)  # one row per ts
        assert all(row[1] == 1.5 and row[2] == 2.5 for row in rows)
        assert metrics_db.disk_mounts(db) == ["C:/"]

    def test_recorder_defaults_are_the_documented_cadence(self, tmp_path):
        rec = ewarecare.MetricsRecorder(db_path=tmp_path / "metrics.db")
        assert rec.interval == 60.0  # ~60 s sampling cadence
        assert rec.db_path == tmp_path / "metrics.db"
        assert rec.sampler is None  # default psutil sampler
        assert rec.keep_hours == 720.0  # 30-day retention window
