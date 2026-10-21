"""Tests for app-level fixes: threshold validation, sort-order threading, layout.

GUI tests run against a real Tk instance with psutil/charts mocked so the
process list and the queue-polling loops never touch real hardware.
"""
from __future__ import annotations

import queue
import threading
import types
import unittest
from unittest.mock import MagicMock, patch

try:
    import tkinter as tk
    HAS_TK = True
except ImportError:  # headless CI
    HAS_TK = False

from scanner import app as app_mod
from scanner.diagnostics import DEFAULT_THRESHOLDS


class TestThresholdValidation(unittest.TestCase):
    """Pure logic tests — no Tkinter required (validate_thresholds is static)."""

    def test_all_values_validated_before_use(self):  # fix 4
        # Mixed bag: one out-of-range value among otherwise valid ones must be
        # reported; a partial application can never happen because
        # apply_thresholds only mutates state after validation passes.
        ok = dict(DEFAULT_THRESHOLDS)
        bad = dict(DEFAULT_THRESHOLDS, cpu_fail=250.0)
        for values in (ok, bad):
            err = app_mod.ScannerApp.validate_thresholds(values)
            if values is ok:
                self.assertIsNone(err)
            else:
                self.assertIsNotNone(err)

    def test_valid_thresholds_pass(self):
        self.assertIsNone(app_mod.ScannerApp.validate_thresholds(dict(DEFAULT_THRESHOLDS)))

    def test_warn_equal_fail_rejected(self):
        values = dict(DEFAULT_THRESHOLDS, cpu_warn=97.0, cpu_fail=97.0)
        err = app_mod.ScannerApp.validate_thresholds(values)
        self.assertIsNotNone(err)
        self.assertIn("cpu_warn", err)
        self.assertIn("cpu_fail", err)

    def test_warn_greater_than_fail_rejected(self):
        values = dict(DEFAULT_THRESHOLDS, mem_warn=99.0, mem_fail=90.0)
        err = app_mod.ScannerApp.validate_thresholds(values)
        self.assertIsNotNone(err)
        self.assertIn("mem_warn", err)

    def test_warn_less_than_fail_accepted(self):
        values = dict(DEFAULT_THRESHOLDS, disk_warn=80.0, disk_fail=99.0)
        self.assertIsNone(app_mod.ScannerApp.validate_thresholds(values))

    def test_out_of_range_rejected(self):
        for key, val in (("cpu_warn", -1.0), ("temp_fail", 300.0),
                         ("gpu_temp_warn", 201.0)):
            values = dict(DEFAULT_THRESHOLDS)
            values[key] = val
            err = app_mod.ScannerApp.validate_thresholds(values)
            self.assertIsNotNone(err)
            self.assertIn(key, err)

    def test_pairs_independent(self):
        # A bad pair must not block an unrelated valid pair.
        values = dict(DEFAULT_THRESHOLDS, temp_warn=200.0, temp_fail=190.0,
                      cpu_warn=10.0, cpu_fail=90.0)
        err = app_mod.ScannerApp.validate_thresholds(values)
        self.assertIsNotNone(err)
        self.assertIn("temp_warn", err)


class TestPollLoopResilience(unittest.TestCase):
    """Regression: `_poll_bg_queue` only rescheduled when the drain succeeded,
    so a single item whose handler raised would permanently stop all background
    updates (snapshots, scans, process rows) while the window stayed alive."""

    @staticmethod
    def _fake_app():
        # Bypass Tk __init__: these methods only use injected plain attributes.
        app = object.__new__(app_mod.ScannerApp)
        app._bg_queue = queue.Queue()
        scheduled: list[tuple[int, object]] = []
        app._reschedule = lambda ms, fn: scheduled.append((ms, fn))
        app.scheduled = scheduled
        return app

    def test_poisoned_item_is_skipped_and_polling_continues(self):
        app = self._fake_app()
        with patch.object(app_mod.log, "exception") as fail_log:
            app._bg_queue.put(("procs", None))  # _render_procs(None) raises
            app._poll_bg_queue()
        self.assertEqual(len(app.scheduled), 1)   # the loop kept its heartbeat
        self.assertTrue(fail_log.called)          # the failure was logged
        self.assertTrue(app._bg_queue.empty())
        # The next cycle still processes good items.
        with patch.object(app_mod.ScannerApp, "_render_procs") as render:
            app._bg_queue.put(("procs",
                               [types.SimpleNamespace(pid=1, name="a")]))
            app._poll_bg_queue()
        self.assertEqual(len(app.scheduled), 2)
        render.assert_called_once()

    def test_poisoned_scan_item_does_not_wedge_the_scan_button(self):
        app = self._fake_app()
        app._scan_active = True
        app.scan_bar = types.SimpleNamespace(start=lambda *_a: None,
                                             stop=lambda: None)
        app.head_status = types.SimpleNamespace(configure=lambda **_kw: None)
        with patch.object(app_mod.log, "exception"):
            app._bg_queue.put(("scan", (1,)))  # tuple unpacking raises
            app._poll_bg_queue()
        self.assertTrue(app._scan_active)      # poisoned item: not reset yet
        app._bg_queue.put(("scan_done", None))
        app._poll_bg_queue()                   # the loop is still alive
        self.assertFalse(app._scan_active)     # scan state was reset

    def test_monitor_poll_survives_snapshot_failure(self):
        app = self._fake_app()
        app.monitor_running = True
        app.monitor = app_mod.MonitorWorker()
        app.chart = None
        app.refresh_interval = types.SimpleNamespace(get=lambda: "1.0")

        def boom(_snap):
            raise TypeError("boom")

        app._apply_snapshot = boom
        app.monitor.samples.put(app_mod.HardwareSnapshot())
        with patch.object(app_mod.log, "exception") as fail_log:
            app._poll_monitor()
        self.assertTrue(fail_log.called)
        self.assertEqual(len(app.scheduled), 1)   # polling continues
        ms, fn = app.scheduled[0]
        self.assertEqual(ms, 1000)
        self.assertEqual(fn, app._poll_monitor)


class TestProcessSortSelection(unittest.TestCase):
    """Fix 3: the worker must receive a plain string, never read a Tk variable."""

    def setUp(self):
        if not HAS_TK:
            self.skipTest("Tkinter not available")
        with patch.object(app_mod, "HAS_PSUTIL", False), \
             patch.object(app_mod, "HAS_MPL", False), \
             patch.object(app_mod.messagebox, "showwarning"), \
             patch.object(app_mod.messagebox, "showinfo"), \
             patch.object(app_mod.messagebox, "showerror"):
            self.app = app_mod.ScannerApp()
        self.addCleanup(self.app.destroy)

    def test_worker_receives_plain_sort_value_without_tk_access(self):
        calls = []

        def fake_collect(limit, sort_by):
            calls.append({"limit": limit, "sort_by": sort_by})
            return []

        self.app.proc_sort.set("mem")
        with patch.object(app_mod, "collect_processes", side_effect=fake_collect), \
             patch.object(app_mod.log, "warning"):
            self.app.update_idletasks()
            self.app.refresh_processes()
            # Synchronous drain of the one-shot worker: deterministic, no sleeps.
            deadline = 50  # update cycles
            while deadline and not calls:
                self.app.update()
                deadline -= 1
            self.assertTrue(calls, "worker never produced rows")
            # Plain value was passed to the worker:
            self.assertEqual(calls[0]["sort_by"], "mem")
            self.assertEqual(calls[0]["limit"], 14)

    def test_worker_failure_is_logged_not_raised(self):
        with patch.object(app_mod, "collect_processes", side_effect=RuntimeError("boom")), \
             patch.object(app_mod.log, "warning") as warn_mock:
            self.app.update_idletasks()
            self.app.refresh_processes()
            deadline = 50
            while deadline:
                self.app.update()
                deadline -= 1
            warn_mock.assert_called()

    def test_overlapping_process_refresh_starts_one_worker(self):
        created = []

        class FakeThread:
            def __init__(self, *args, **kwargs):
                created.append(self)

            def start(self):
                pass

        self.app._procs_active = False
        with patch.object(app_mod.threading, "Thread", FakeThread):
            self.app.refresh_processes()
            self.app.refresh_processes()
        self.assertEqual(len(created), 1)
        self.assertTrue(self.app._procs_active)
        self.app._procs_active = False

    def test_proc_job_failure_clears_guard(self):
        self.app._procs_active = True
        with patch.object(app_mod, "collect_processes",
                          side_effect=RuntimeError("boom")), \
             patch.object(app_mod.log, "warning"):
            self.app._proc_job("cpu")
        self.assertFalse(self.app._procs_active)

    def test_run_scan_thread_failure_resets_state(self):
        self.app._scan_active = False
        with patch.object(app_mod.threading.Thread, "start",
                          side_effect=RuntimeError("no threads")), \
             patch.object(app_mod.log, "warning"):
            self.app.run_scan()
        self.assertFalse(self.app._scan_active)

    def test_refresh_once_starts_one_worker_while_active(self):
        created = []

        class FakeThread:
            def __init__(self, *args, **kwargs):
                self.started = 0
                created.append(self)

            def start(self):
                self.started += 1

        with patch.object(app_mod.threading, "Thread", FakeThread):
            self.app._refresh_active = False
            self.app.refresh_once()
            self.app.refresh_once()
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].started, 1)
        self.assertTrue(self.app._refresh_active)
        self.app._refresh_active = False

    def test_refresh_once_start_failure_resets_guard(self):
        self.app._refresh_active = False
        with patch.object(app_mod.threading.Thread, "start",
                          side_effect=RuntimeError("no threads")):
            self.app.refresh_once()
            self.app.refresh_once()
        self.assertFalse(self.app._refresh_active)

    def test_collect_job_clears_refresh_guard(self):
        self.app._refresh_active = True
        with patch.object(app_mod, "collect_snapshot",
                          return_value=app_mod.HardwareSnapshot()):
            self.app._collect_job()
        self.assertFalse(self.app._refresh_active)
        kind, _data = self.app._bg_queue.get_nowait()
        self.assertEqual(kind, "snapshot")

    def test_poll_bg_queue_preserves_event_order(self):
        calls = []
        rows = [types.SimpleNamespace(pid=1, name="x", cpu=0.0, mem_mb=0.0)]
        self.app._bg_queue.put(("procs", rows))
        self.app._bg_queue.put(("snapshot", app_mod.HardwareSnapshot()))
        with patch.object(self.app, "_render_procs",
                          side_effect=lambda data: calls.append("procs")), \
             patch.object(self.app, "_apply_snapshot",
                          side_effect=lambda data: calls.append("snapshot")), \
             patch.object(self.app, "_reschedule"):
            self.app._poll_bg_queue()
        self.assertEqual(calls[:2], ["procs", "snapshot"])
        self.assertFalse(hasattr(self.app, "_drain_procs"))
        self.assertFalse(hasattr(self.app, "_apply_monitor_items"))

    def test_sort_var_fallback_on_tcl_error(self):
        # If the variable read fails on the main thread (destroyed app etc.),
        # refresh_processes must still start the worker with the default "cpu".
        class BrokenVar:
            def get(self):
                raise tk.TclError("variable destroyed")

        calls = []

        def fake_collect(limit, sort_by):
            calls.append({"sort_by": sort_by})
            return []

        original = self.app.proc_sort
        self.app.proc_sort = BrokenVar()
        try:
            with patch.object(app_mod, "collect_processes", side_effect=fake_collect):
                self.app.update_idletasks()
                self.app.refresh_processes()
                deadline = 50
                while deadline and not calls:
                    self.app.update()
                    deadline -= 1
                self.assertTrue(calls)
                self.assertEqual(calls[0]["sort_by"], "cpu")
        finally:
            self.app.proc_sort = original


class TestDashboardLayout(unittest.TestCase):
    """Fix 1: five cards must fit the row width at default and min sizes."""

    def setUp(self):
        if not HAS_TK:
            self.skipTest("Tkinter not available")
        with patch.object(app_mod, "HAS_PSUTIL", False), \
             patch.object(app_mod, "HAS_MPL", False), \
             patch.object(app_mod.messagebox, "showwarning"), \
             patch.object(app_mod.messagebox, "showinfo"), \
             patch.object(app_mod.messagebox, "showerror"):
            self.app = app_mod.ScannerApp()
        self.addCleanup(self.app.destroy)

    def _card_widths(self):
        return [card.winfo_width() for card, _ in self.app._card_slots]

    def _assert_cards_fit(self, geometry: str):
        self.app.geometry(geometry)
        self.app.update_idletasks()
        self.app.update()
        # Force a relayout pass with the final geometry.
        self.app.event_generate("<Configure>", when="tail")
        self.app.update_idletasks()
        self.app.update()
        row_w = self.app.cards_row.winfo_width()
        widths = self._card_widths()
        self.assertEqual(len(widths), 5)
        pad = 8 * 4  # 5 cards, padx=(0, 8) each
        total = sum(widths) + pad
        self.assertLessEqual(total, row_w + 2,
                             f"cards overflow: total {total} > row {row_w}")
        for w in widths:
            # Cards may shrink with the window but must stay usable.
            self.assertGreaterEqual(w, 60, "card collapsed below usable width")

    def test_cards_fit_default_geometry(self):
        self._assert_cards_fit("1180x760")

    def test_cards_fit_minimum_geometry(self):
        self._assert_cards_fit("960x620")

    def test_cards_fit_narrow_geometry(self):
        self._assert_cards_fit("700x560")

    def test_layout_cards_ignores_tiny_width(self):
        # A <Configure> with a degenerate width (window not laid out yet) must
        # be ignored: the cards' configured width stays at the minimum.
        before = [int(c.cget("width")) for c, _ in self.app._card_slots]
        self.app._on_cards_resize(type("E", (), {"width": 1})())
        after = [int(c.cget("width")) for c, _ in self.app._card_slots]
        self.assertEqual(before, after)
        self.assertTrue(all(w >= app_mod.T.CARD_MIN_WIDTH for w in after),
                        f"cards configured below minimum width: {after}")

    def test_show_page_updates_header_title(self):
        for name in app_mod.NAV_ITEMS:
            self.app.show_page(name)
            self.assertEqual(self.app.page_title.cget("text"), name)

    def test_reschedule_does_not_schedule_after_destroy(self):
        with patch.object(self.app, "winfo_exists", return_value=False), \
             patch.object(self.app, "after") as after_mock:
            self.app._reschedule(250, lambda: None)
        after_mock.assert_not_called()

    def test_reschedule_swallows_tcl_error(self):
        with patch.object(self.app, "after",
                          side_effect=tk.TclError("invalid command name")):
            self.app._reschedule(250, lambda: None)

    def test_reschedule_tracks_the_after_id(self):
        self.app._after_ids.clear()
        with patch.object(self.app, "after", return_value="after#42"):
            self.app._reschedule(250, lambda: None)
        self.assertIn("after#42", self.app._after_ids)

    def test_on_closing_cancels_pending_callbacks(self):
        self.app._after_ids = {"after#1", "after#2"}
        with patch.object(self.app.monitor, "request_stop"), \
             patch.object(self.app, "after_cancel") as cancel_mock, \
             patch.object(self.app, "destroy"):
            self.app.monitor_running = True
            self.app.on_closing()
        self.assertEqual(cancel_mock.call_count, 2)
        self.assertEqual(self.app._after_ids, set())

    def test_on_closing_requests_stop_without_join(self):
        self.app.monitor_running = True
        with patch.object(self.app.monitor, "request_stop") as request_mock, \
             patch.object(self.app.monitor, "stop") as stop_mock, \
             patch.object(self.app, "destroy"):
            self.app.on_closing()
        request_mock.assert_called_once_with()
        stop_mock.assert_not_called()

    def test_poll_monitor_tcl_error_on_refresh_interval(self):
        self.app.monitor_running = True
        self.app.refresh_interval = MagicMock()
        self.app.refresh_interval.get.side_effect = tk.TclError("destroyed")
        with patch.object(self.app, "_reschedule") as reschedule_mock:
            self.app._poll_monitor()
        self.assertTrue(reschedule_mock.called)
        self.assertEqual(reschedule_mock.call_args[0][0], 1000)
        self.assertEqual(reschedule_mock.call_args[0][1], self.app._poll_monitor)

    def test_refresh_error_does_not_clear_scan_state(self):
        self.app._scan_active = True
        old_status_text = self.app.head_status.cget("text")
        self.app._bg_queue.put(("refresh_error", "Refresh failed: boom"))
        with patch.object(self.app, "_reschedule"):
            self.app._poll_bg_queue()
        self.assertTrue(self.app._scan_active)
        self.assertEqual(self.app.head_status.cget("text"), old_status_text)
        self.assertEqual(self.app.status_var.get(), "Refresh failed: boom")


class TestMonitorRequestStop(unittest.TestCase):
    def test_request_stop_does_not_join_and_is_safe_when_idle(self):
        from scanner.monitoring import MonitorWorker

        worker = MonitorWorker()
        worker.request_stop()  # safe even when never started
        self.assertTrue(worker._stop.is_set())

        worker._stop.clear()
        worker.running = True
        worker._thread = MagicMock()
        with patch.object(threading.Thread, "join") as join_mock:
            worker.request_stop()
        self.assertTrue(worker._stop.is_set())
        join_mock.assert_not_called()
        worker._thread.join.assert_not_called()
