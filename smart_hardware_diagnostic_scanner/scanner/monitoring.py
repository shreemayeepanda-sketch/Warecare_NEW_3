"""Background monitoring worker: samples hardware on a thread, GUI polls a queue."""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field

from .hardware import HardwareSnapshot, collect_snapshot

log = logging.getLogger("shds.monitoring")

MAX_HISTORY = 120  # bounded chart history


@dataclass
class Sample:
    t: float
    cpu: float | None
    mem: float | None
    gpu: float | None = None
    up_bps: float | None = None
    down_bps: float | None = None


class MonitorWorker:
    """Single cancellable sampler thread. Tkinter polls `poll()` on the main thread."""

    def __init__(self, interval_s: float = 1.0, include_gpu: bool = False) -> None:
        self.interval_s = max(0.5, float(interval_s))
        self.include_gpu = include_gpu
        self.samples: queue.Queue[Sample | HardwareSnapshot] = queue.Queue()
        self.history: list[Sample] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.running = False

    def start(self) -> bool:
        """Start sampling. Returns False if already running (no overlapping loops)."""
        with self._lock:
            if self.running:
                return False
            # A fresh stop event per run: a later start() must never re-arm the
            # stop flag of a thread that is still finishing its last sample.
            self._stop = threading.Event()
            thread = threading.Thread(target=self._loop, name="shds-monitor", daemon=True)
            try:
                thread.start()
            except (RuntimeError, OSError) as exc:
                # Thread creation failed (thread limit, interpreter shutdown...).
                # `running` must stay False, otherwise monitoring can never be
                # started again for the lifetime of the process.
                log.warning("monitor thread failed to start: %s", exc)
                return False
            self._thread = thread
            self.running = True
            return True

    def request_stop(self) -> None:
        """Signal the worker to stop without joining (main-thread safe)."""
        self._stop.set()

    def stop(self, timeout: float = 3.0) -> None:
        """Ask the sampler to stop. Never blocks the GUI thread for long.

        The worker may be inside a slow sample (e.g. an 8s nvidia-smi call),
        so the join is done on a helper thread instead of freezing the UI.
        The per-run stop event guarantees the old thread exits on its own
        even if it is still busy.
        """
        with self._lock:
            if not self.running:
                return
            self._stop.set()
            thread = self._thread
            self.running = False
            self._thread = None
        if thread is not None and thread.is_alive():
            threading.Thread(target=thread.join, args=(timeout,),
                             name="shds-monitor-reap", daemon=True).start()

    def set_interval(self, interval_s: float) -> None:
        self.interval_s = max(0.5, float(interval_s))

    def _loop(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                snap = collect_snapshot(include_gpu=self.include_gpu)
                sample = Sample(
                    t=time.time(),
                    cpu=snap.cpu.percent,
                    mem=snap.mem.percent,
                    gpu=(snap.gpu.gpus[0].utilization_pct if snap.gpu.gpus else None),
                    up_bps=snap.net.up_bps,
                    down_bps=snap.net.down_bps,
                )
                self.samples.put(sample)
                self.samples.put(snap)
                self.history.append(sample)
                if len(self.history) > MAX_HISTORY:
                    del self.history[: len(self.history) - MAX_HISTORY]
            except Exception as exc:  # worker must never die silently-crashing the GUI
                log.warning("monitor sample failed: %s", exc)
            wait = self.interval_s - (time.monotonic() - started)
            if wait > 0:
                self._stop.wait(wait)

    def poll(self) -> list:
        """Drain the queue (call from the GUI thread only)."""
        items: list = []
        try:
            while True:
                items.append(self.samples.get_nowait())
        except queue.Empty:
            pass
        return items

    def clear_history(self) -> None:
        self.history.clear()


@dataclass
class ScanWorker:
    """One-shot diagnostic scan on a background thread with result queue."""

    result: queue.Queue = field(default_factory=queue.Queue)
    _stop: threading.Event = field(default_factory=threading.Event)
    running: bool = False

    def start(self, snapshot: HardwareSnapshot, thresholds: dict | None = None) -> bool:
        if self.running:
            return False
        from .diagnostics import run_diagnostics

        self._stop.clear()
        self.running = True

        def _job() -> None:
            try:
                # Simulate staged progress so the GUI progress bar animates.
                for _ in range(4):
                    if self._stop.is_set():
                        break
                    time.sleep(0.15)
                if not self._stop.is_set():
                    self.result.put(run_diagnostics(snapshot, thresholds))
            except Exception as exc:
                log.exception("scan failed: %s", exc)
                self.result.put(exc)
            finally:
                self.running = False

        try:
            threading.Thread(target=_job, name="shds-scan", daemon=True).start()
        except (RuntimeError, OSError) as exc:
            # Never leave `running` stuck True: the scan button would stay
            # disabled for the rest of the session.
            log.warning("scan thread failed to start: %s", exc)
            self.running = False
            return False
        return True

    def cancel(self) -> None:
        self._stop.set()
