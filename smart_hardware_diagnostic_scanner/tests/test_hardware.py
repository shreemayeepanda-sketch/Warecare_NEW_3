"""Tests for hardware collection: calculations, missing sensors, error paths."""
from __future__ import annotations

import threading
import unittest
from unittest.mock import MagicMock, patch

from scanner import hardware as hw
from scanner.utils import format_bytes, pct


class TestUtils(unittest.TestCase):
    def test_format_bytes(self):
        self.assertEqual(format_bytes(0), "0 B")
        self.assertEqual(format_bytes(1024), "1.00 KB")
        self.assertEqual(format_bytes(8 * 1024**3), "8.00 GB")
        self.assertEqual(format_bytes(-5), "Unavailable")
        self.assertEqual(format_bytes("junk"), "Unavailable")

    def test_format_bytes_non_finite(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            self.assertEqual(format_bytes(value), "Unavailable")

    def test_pct(self):
        self.assertEqual(pct(50, 100), 50.0)
        self.assertIsNone(pct(1, 0))
        self.assertIsNone(pct(1, -3))

    def test_pct_non_finite(self):
        self.assertIsNone(pct(1, float("nan")))
        self.assertIsNone(pct(float("nan"), 100))
        self.assertIsNone(pct(1, float("inf")))
        self.assertIsNone(pct(float("inf"), 100))


class _RecordingLock:
    """Context manager that counts acquisitions and delegates to a real lock."""

    def __init__(self) -> None:
        self.entries = 0
        self._inner = threading.Lock()

    def __enter__(self):
        self.entries += 1
        self._inner.acquire()
        return self

    def __exit__(self, *exc_info):
        self._inner.release()


class TestHardwareCollection(unittest.TestCase):
    def test_collect_without_psutil(self):
        with patch.object(hw, "HAS_PSUTIL", False), patch.object(hw, "psutil", None):
            cpu = hw.collect_cpu()
            self.assertFalse(cpu.available)
            self.assertIn("psutil", cpu.error)
            self.assertEqual(hw.collect_memory().available, False)
            self.assertEqual(hw.collect_disks(), [])
            temps = hw.collect_temps()
            self.assertTrue(any(t.status == "unsupported" for t in temps))

    def test_collect_cpu_mock(self):
        fake = MagicMock()
        fake.cpu_percent.side_effect = [42.5, [10.0, 20.0]]
        fake.cpu_count.side_effect = [4, 8]
        fake.cpu_freq.return_value = MagicMock(current=3200.0, min=800.0, max=4200.0)
        fake.getloadavg.side_effect = OSError("no loadavg on Windows")
        with patch.object(hw, "HAS_PSUTIL", True), patch.object(hw, "psutil", fake):
            snap = hw.collect_cpu()
            self.assertTrue(snap.available)
            self.assertAlmostEqual(snap.percent, 42.5)
            self.assertEqual(snap.per_core, [10.0, 20.0])
            self.assertEqual(snap.physical_cores, 4)
            self.assertEqual(snap.logical_cores, 8)

    def test_collect_memory_mock(self):
        fake = MagicMock()
        fake.virtual_memory.return_value = MagicMock(total=8 * 1024**3,
                                                     available=2 * 1024**3,
                                                     used=6 * 1024**3, percent=75.0)
        with patch.object(hw, "HAS_PSUTIL", True), patch.object(hw, "psutil", fake):
            mem = hw.collect_memory()
            self.assertTrue(mem.available)
            self.assertEqual(mem.total, 8 * 1024**3)
            self.assertEqual(mem.percent, 75.0)

    def test_collect_disks_invalid_path(self):
        fake = MagicMock()
        part = MagicMock(device="Z:", mountpoint="Z:", fstype="NTFS")
        fake.disk_partitions.return_value = [part]
        fake.disk_usage.side_effect = FileNotFoundError("no such drive")
        with patch.object(hw, "HAS_PSUTIL", True), patch.object(hw, "psutil", fake):
            disks = hw.collect_disks()
            self.assertEqual(len(disks), 1)
            self.assertFalse(disks[0].accessible)
            self.assertIn("Inaccessible", disks[0].error)

    def test_collect_disks_dedupes(self):
        fake = MagicMock()
        p1 = MagicMock(device="C:", mountpoint="C:", fstype="NTFS")
        p2 = MagicMock(device="C:", mountpoint="c:", fstype="NTFS")
        fake.disk_partitions.return_value = [p1, p2]
        fake.disk_usage.return_value = MagicMock(total=100, used=10, free=90, percent=10.0)
        with patch.object(hw, "HAS_PSUTIL", True), patch.object(hw, "psutil", fake):
            self.assertEqual(len(hw.collect_disks()), 1)

    def test_network_counter_reset(self):
        fake = MagicMock()
        fake.net_if_addrs.return_value = {"Ethernet": [], "Wi-Fi": []}
        fake.net_io_counters.return_value = MagicMock(bytes_sent=1000, bytes_recv=2000)
        hw._prev_net.update({"sent": 0, "recv": 0, "t": 0.0})
        # Deterministic clock: two back-to-back samples can land on the same
        # Windows monotonic tick, which honestly yields "no rate yet" and
        # would make this test flaky rather than wrong.
        with patch.object(hw, "HAS_PSUTIL", True), patch.object(hw, "psutil", fake), \
             patch.object(hw.time, "monotonic", side_effect=[100.0, 101.0, 102.0]):
            first = hw.collect_network()
            self.assertIsNone(first.up_bps)  # first sample has no rate
            self.assertIsNone(first.down_bps)
            # sent counter resets (smaller than before) -> upload unavailable,
            # recv counter keeps increasing -> download rate still computed
            fake.net_io_counters.return_value = MagicMock(bytes_sent=10, bytes_recv=9000)
            second = hw.collect_network()
            self.assertTrue(second.available)
            self.assertIsNone(second.up_bps)  # sent counter reset detected
            self.assertGreater(second.down_bps or 0.0, 0.0)  # recv grew: real rate
            # received counter resets -> download unavailable, upload keeps growing
            fake.net_io_counters.return_value = MagicMock(bytes_sent=5000, bytes_recv=20)
            third = hw.collect_network()
            self.assertIsNone(third.down_bps)  # recv counter reset detected
            self.assertGreater(third.up_bps or 0.0, 0.0)

    def test_rate_bps_rejects_negative_delta(self):
        # unit test for the shared helper: reset, equal, growing, bad dt
        self.assertIsNone(hw._rate_bps(10, 1000, 1.0))  # counter reset
        self.assertEqual(hw._rate_bps(1000, 1000, 1.0), 0.0)  # idle
        self.assertEqual(hw._rate_bps(3000, 1000, 2.0), 8000.0)  # growing
        self.assertIsNone(hw._rate_bps(1000, 500, 0.0))  # no elapsed time

    def test_network_update_is_lock_guarded(self):
        fake = MagicMock()
        fake.net_if_addrs.return_value = {}
        fake.net_io_counters.return_value = MagicMock(bytes_sent=100, bytes_recv=200)
        lock = _RecordingLock()
        hw._prev_net.update({"sent": 0, "recv": 0, "t": 0.0})
        with patch.object(hw, "HAS_PSUTIL", True), patch.object(hw, "psutil", fake), \
             patch.object(hw, "_net_lock", lock):
            first = hw.collect_network()
            second = hw.collect_network()
        self.assertEqual(lock.entries, 2)
        self.assertIsNone(first.up_bps)
        for snap in (first, second):
            for rate in (snap.up_bps, snap.down_bps):
                self.assertTrue(rate is None or rate >= 0.0)

    def test_temps_no_sensors_unsupported(self):
        fake = MagicMock()
        fake.sensors_temperatures.return_value = {}
        with patch.object(hw, "HAS_PSUTIL", True), patch.object(hw, "psutil", fake):
            temps = hw.collect_temps()
            self.assertTrue(all(t.status in ("unsupported", "unavailable") for t in temps))
            ram = [t for t in temps if t.label == "RAM"]
            self.assertEqual(len(ram), 1)
            self.assertEqual(ram[0].status, "unsupported")

    def test_processes_access_denied_skipped(self):
        class Denied(Exception):
            pass

        fake = MagicMock()
        fake.NoSuchProcess = type("NoSuchProcess", (Exception,), {})
        fake.AccessDenied = type("AccessDenied", (Exception,), {})
        fake.ZombieProcess = type("ZombieProcess", (Exception,), {})

        good = MagicMock()
        good.info = {"pid": 1, "name": "ok", "cpu_percent": 5.0,
                     "memory_info": MagicMock(rss=10 * 1024 * 1024)}
        bad = MagicMock()
        type(bad).info = property(lambda self: (_ for _ in ()).throw(fake.AccessDenied()))
        fake.process_iter.return_value = [good, bad]
        with patch.object(hw, "HAS_PSUTIL", True), patch.object(hw, "psutil", fake):
            rows = hw.collect_processes(limit=5)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].name, "ok")

    def test_processes_first_pass_is_primed(self):
        self.addCleanup(setattr, hw, "_proc_primed", False)
        hw._proc_primed = False
        fake = MagicMock()
        fake.process_iter.return_value = []
        with patch.object(hw, "HAS_PSUTIL", True), patch.object(hw, "psutil", fake), \
             patch.object(hw.time, "sleep") as sleep_mock:
            rows = hw.collect_processes(limit=5)
            self.assertEqual(rows, [])
            self.assertTrue(hw._proc_primed)
            self.assertEqual(fake.process_iter.call_count, 2)
            sleep_mock.assert_called_once_with(0.1)

    def test_snapshot_never_raises_on_gpu_failure(self):
        with patch.object(hw, "query_gpus", side_effect=RuntimeError("boom")):
            snap = hw.collect_snapshot(include_gpu=True)
            self.assertIn("boom", snap.gpu.message)

    def test_temps_always_includes_one_ram_entry(self):
        cases = [
            (False, {}),
            (True, {}),
            (True, {"coretemp": [MagicMock(current=55.0, label="Core 0")]}),
        ]
        for has_psutil, sensors in cases:
            with self.subTest(has_psutil=has_psutil, sensors=sensors):
                fake = MagicMock()
                fake.sensors_temperatures.return_value = sensors
                with patch.object(hw, "HAS_PSUTIL", has_psutil), \
                     patch.object(hw, "psutil", fake):
                    temps = hw.collect_temps()
                ram_entries = [t for t in temps if t.label == "RAM"]
                self.assertEqual(len(ram_entries), 1,
                                 f"expected exactly one RAM entry, got {len(ram_entries)}")
                self.assertEqual(ram_entries[0].status, "unsupported")
                self.assertIsNone(ram_entries[0].current)
                self.assertEqual(ram_entries[0].source,
                                 "no reliable sensor interface")
                self.assertEqual(sum(1 for t in temps if t.label == "RAM"), 1)


if __name__ == "__main__":
    unittest.main()
