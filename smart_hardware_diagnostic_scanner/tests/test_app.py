"""Tests for app-level fixes: threshold validation, sort-order threading, layout.

GUI tests run against a real Tk instance with psutil/charts mocked so the
process list and the queue-polling loops never touch real hardware.
"""
from __future__ import annotations

import customtkinter as ctk
import queue
import threading
import time
import types
import unittest
from unittest.mock import MagicMock, patch

try:
    import tkinter as tk
    HAS_TK = True
except ImportError:  # headless CI
    HAS_TK = False

from scanner import app as app_mod
from scanner import icons as icons_mod
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

    def _settle_geometry(self, geometry: str, timeout_s: float = 2.0) -> None:
        """Wait (bounded) for the OS to apply a resize to the mapped window.

        ``geometry()`` only queues the request. On Windows the new size is
        delivered by the window manager asynchronously, so two immediate
        ``update()`` calls can return while the card row is still laid out
        at its requested content width.
        """
        requested_w = int(geometry.split("x")[0])
        # Tk clamps to the app's minsize (960x620); CTk scales both.
        expected_w = self.app._apply_window_scaling(max(960, requested_w))
        deadline = None
        last = None
        stable = 0
        for _ in range(200):  # hard cap, ~4 s of pumping
            self.app.update_idletasks()
            self.app.update()
            if deadline is None:
                # The first pump maps the window and can itself take seconds
                # (icons render, the WM applies the initial size); don't let
                # that consume the settle budget.
                deadline = time.monotonic() + timeout_s
            state = (self.app.winfo_width(), self.app.cards_row.winfo_width())
            if state == last:
                stable += 1
                # Require both a settled row and an applied window width.
                if stable >= 5 and self.app.winfo_width() >= expected_w - 2:
                    return
            else:
                stable = 0
                last = state
            if time.monotonic() >= deadline:
                return
            time.sleep(0.02)

    def _assert_cards_fit(self, geometry: str):
        self.app.geometry(geometry)
        self._settle_geometry(geometry)
        # Force a relayout pass with the final geometry.
        self.app.event_generate("<Configure>", when="tail")
        self.app.update_idletasks()
        self.app.update()
        row_w = self.app.cards_row.winfo_width()
        widths = self._card_widths()
        context = (f"geometry={self.app.winfo_geometry()} requested={geometry} "
                   f"viewable={self.app.winfo_viewable()} state={self.app.state()} "
                   f"row={row_w} widths={widths}")
        self.assertEqual(len(widths), 5, context)
        pad = 8 * 4  # 5 cards, padx=(0, 8) each
        total = sum(widths) + pad
        self.assertLessEqual(total, row_w + 2,
                             f"cards overflow: total {total} > row {row_w} ({context})")
        for w in widths:
            # Cards may shrink with the window but must stay usable.
            self.assertGreaterEqual(w, 60,
                                    f"card collapsed below usable width ({context})")

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


class TestSystemHealthAndFixUI(unittest.TestCase):
    """Dashboard System Health strip and Diagnostics 'How to fix' wiring."""

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

    def _texts(self, widget):
        texts = []
        for child in widget.winfo_children():
            try:
                value = child.cget("text")
            except Exception:  # noqa: BLE001 - widget without a text option
                value = None
            if isinstance(value, str):
                texts.append(value)
            texts.extend(self._texts(child))
        return texts

    def _fix_buttons(self, widget):
        buttons = []
        for child in widget.winfo_children():
            if child.__class__.__name__ == "CTkButton":
                buttons.append(child)
            buttons.extend(self._fix_buttons(child))
        return buttons

    def test_fail_and_warning_findings_get_a_fix_button(self):
        from scanner.diagnostics import Finding
        findings = [
            Finding("CPU", "FAIL", "too hot", key="CPU"),
            Finding("Memory", "WARNING", "pressure", key="Memory"),
            Finding("Network", "PASS", "ok", key="Network"),
        ]
        self.app._render_fix_buttons(findings)
        buttons = self._fix_buttons(self.app.fix_frame)
        self.assertEqual([b.cget("text") for b in buttons],
                         ["How to fix", "How to fix"])
        texts = self._texts(self.app.fix_frame)
        self.assertTrue(any("[FAIL]" in t for t in texts))
        self.assertTrue(any("[WARNING]" in t for t in texts))
        self.assertFalse(any("[PASS]" in t for t in texts))

    def test_no_actionable_findings_shows_an_honest_empty_state(self):
        from scanner.diagnostics import Finding
        self.app._render_fix_buttons(
            [Finding("CPU", "PASS", "ok", key="CPU")])
        self.assertIn("No FAIL or WARNING findings — nothing to fix.",
                      self._texts(self.app.fix_frame))

    def test_command_fix_is_copied_and_never_executed(self):
        from scanner.diagnostics import Finding
        finding = Finding("OS: systemd", "FAIL", "failed unit",
                          key="OS:Systemd", code="SYSTEMD_FAILED")
        with patch.object(self.app, "clipboard_clear"), \
             patch.object(self.app, "clipboard_append") as append_mock, \
             patch.object(app_mod.messagebox, "showinfo") as info_mock:
            self.app._show_fix(finding)
        append_mock.assert_called_once_with("systemctl --failed")
        info_mock.assert_called_once()

    def test_health_strip_renders_score_and_hides_os_before_a_scan(self):
        from scanner.hardware import CpuSnapshot, HardwareSnapshot
        self.app.latest_snap = HardwareSnapshot(
            cpu=CpuSnapshot(available=True, percent=10.0))
        self.app.latest_diag = None
        self.app._update_system_health()
        self.assertEqual(self.app.health_var.get(), "90")
        self.assertIn("CPU 90", self.app.health_detail.cget("text"))
        self.assertIn("run a scan for OS checks",
                      self.app.health_detail.cget("text"))

    def test_health_strip_includes_os_penalty_after_a_scan(self):
        from scanner.diagnostics import DiagnosticReport, Finding
        from scanner.hardware import CpuSnapshot, HardwareSnapshot
        self.app.latest_snap = HardwareSnapshot(
            cpu=CpuSnapshot(available=True, percent=0.0))
        self.app.latest_diag = DiagnosticReport(findings=[
            Finding("OS: Test", "FAIL", "x", key="OS:Test")])
        self.app._update_system_health()
        # CPU 100 (30%) + OS 90 (20%) renormalised over 0.5 -> 96.
        self.assertEqual(self.app.health_var.get(), "96")
        self.assertIn("OS 1 fail / 0 warn",
                      self.app.health_detail.cget("text"))


class TestVectorIcons(unittest.TestCase):
    """The Pillow-drawn vector icon layer (scanner.icons)."""

    def setUp(self):
        icons_mod.clear_cache()

    def tearDown(self):
        icons_mod.clear_cache()

    def test_every_icon_name_renders_as_a_ctk_image(self):
        for name in icons_mod.ICON_NAMES:
            img = icons_mod.get_icon(name, 24, "#000000")
            self.assertIsInstance(img, ctk.CTkImage)
            self.assertEqual(img.cget("size"), (24, 24))

    def test_painters_cover_navigation_and_card_maps(self):
        # Sidebar icon for every page, card icon for every metric card.
        self.assertEqual(set(icons_mod.NAV_ICONS), set(app_mod.NAV_ITEMS))
        self.assertTrue(set(icons_mod.NAV_ICONS.values())
                        .issubset(icons_mod.ICON_NAMES))
        self.assertEqual(set(icons_mod.CARD_ICONS),
                         {"CPU", "RAM", "GPU", "Disk", "Network"})
        self.assertTrue(set(icons_mod.CARD_ICONS.values())
                        .issubset(icons_mod.ICON_NAMES))

    def test_unknown_name_raises_key_error(self):
        with self.assertRaises(KeyError) as ctx:
            icons_mod.get_icon("no-such-icon", 24, "#000000")
        self.assertIn("unknown icon name", str(ctx.exception))

    def test_size_is_clamped_and_ints_are_used(self):
        # Degenerate sizes clamp to the 8 px floor; floats truncate.
        self.assertEqual(icons_mod.get_icon("cpu", 3, "#000000").cget("size"),
                         (8, 8))
        self.assertEqual(
            icons_mod.get_icon("cpu", -5, "#000000").cget("size"), (8, 8))
        self.assertEqual(icons_mod.get_icon("cpu", 22.7, "#000000").cget("size"),
                         (22, 22))

    def test_default_colour_comes_from_the_active_palette(self):
        with patch.object(icons_mod.T, "NEON_CYAN", "#ABC123"):
            icons_mod.get_icon("cpu", 12)
            self.assertIn(("cpu", 12, "#ABC123"), icons_mod._BITMAP_CACHE)
        # An explicit colour bypasses the palette entirely.
        icons_mod.get_icon("cpu", 12, "#123456")
        self.assertIn(("cpu", 12, "#123456"), icons_mod._BITMAP_CACHE)

    def test_bitmap_is_cached_but_ctkimage_is_fresh(self):
        first = icons_mod.get_icon("gpu", 24, "#99AABB")
        second = icons_mod.get_icon("gpu", 24, "#99AABB")
        self.assertIsNot(first, second)  # fresh CTkImage wrapper per call
        self.assertIs(first.cget("light_image"), second.cget("light_image"))
        self.assertIs(first.cget("light_image"), first.cget("dark_image"))

    def test_clear_cache_drops_bitmaps(self):
        first = icons_mod.get_icon("disk", 16, "#0F0F0F")
        icons_mod.clear_cache()
        self.assertEqual(icons_mod._BITMAP_CACHE, {})
        again = icons_mod.get_icon("disk", 16, "#0F0F0F")
        self.assertIsNot(again.cget("light_image"), first.cget("light_image"))

    def test_bitmaps_are_squares_with_transparent_corners(self):
        # Bitmaps are cached at round(size * _HI_DPI_SCALE) — the physical
        # resolution CTk displays them at on scaled (>=125%) screens — so Tk
        # shows the strokes 1:1 instead of badly upscaling a 1x bitmap.
        display_px = round(20 * icons_mod._HI_DPI_SCALE)
        for name in icons_mod.ICON_NAMES:
            bitmap = icons_mod.get_icon(name, 20, "#000000").cget("light_image")
            with self.subTest(name=name):
                self.assertEqual(bitmap.size, (display_px, display_px))
                self.assertEqual(bitmap.mode, "RGBA")
                self.assertEqual(bitmap.getpixel((0, 0))[3], 0,
                                 "icon left a stroke at the canvas corner")


class TestIconSidebarAndCards(unittest.TestCase):
    """New widget set: the 240 px icon sidebar and the rounded CTkFrame cards.

    These checks only read construction-time options (no event pump), so no
    background callback can interfere with the asserted widget state.
    """

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

    def test_sidebar_is_240_px_wide(self):
        self.app.update_idletasks()
        self.app.update()
        self.assertEqual(self.app.sidebar.winfo_width(), 240)

    def test_nav_buttons_are_left_aligned_icon_pills(self):
        self.assertEqual(set(self.app.nav_buttons), set(app_mod.NAV_ITEMS))
        for item in app_mod.NAV_ITEMS:
            btn = self.app.nav_buttons[item]
            self.assertIsInstance(btn, ctk.CTkButton)
            self.assertEqual(btn.cget("anchor"), "w")
            self.assertEqual(int(btn.cget("corner_radius")), 8)
            image = btn.cget("image")
            self.assertIsInstance(image, ctk.CTkImage)
            self.assertEqual(image.cget("size"),
                             (app_mod.ICON_SIZE, app_mod.ICON_SIZE))

    def test_show_page_highlights_only_the_active_nav_item(self):
        active = self.app.nav_buttons["Dashboard"]  # Dashboard starts active
        self.assertEqual(active.cget("fg_color"), app_mod.T.BTN_SIDE_ACTIVE_BG)
        self.assertEqual(active.cget("text_color"), app_mod.T.BTN_SIDE_ACTIVE_FG)
        inactive = self.app.nav_buttons["Processes"]
        self.assertEqual(inactive.cget("fg_color"), app_mod.T.BTN_SIDE_BG)
        self.assertEqual(inactive.cget("text_color"), app_mod.T.BTN_SIDE_FG)
        self.app.show_page("Processes")
        self.assertEqual(self.app.nav_buttons["Dashboard"].cget("fg_color"),
                         app_mod.T.BTN_SIDE_BG)
        active = self.app.nav_buttons["Processes"]
        self.assertEqual(active.cget("fg_color"), app_mod.T.BTN_SIDE_ACTIVE_BG)
        self.assertEqual(active.cget("text_color"), app_mod.T.BTN_SIDE_ACTIVE_FG)

    def test_active_nav_icon_is_colour_mapped_accent(self):
        # Icons are bitmaps: the active/inactive versions must be the cached
        # cyan/muted renderings, not just "an icon".
        self.app.show_page("Processes")
        image = self.app.nav_buttons["Processes"].cget("image")
        accent = icons_mod._BITMAP_CACHE[
            (icons_mod.NAV_ICONS["Processes"], app_mod.ICON_SIZE,
             app_mod.T.NEON_CYAN)]
        self.assertIs(image.cget("light_image"), accent)
        muted = icons_mod._BITMAP_CACHE[
            (icons_mod.NAV_ICONS["Dashboard"], app_mod.ICON_SIZE,
             app_mod.T.FG_MUTED)]
        dashboard = self.app.nav_buttons["Dashboard"].cget("image")
        self.assertIs(dashboard.cget("light_image"), muted)

    def test_dark_nav_highlight_matches_the_spec_colours(self):
        self.app.apply_theme("dark")
        self.app.show_page("Diagnostics")
        btn = self.app.nav_buttons["Diagnostics"]
        self.assertEqual(btn.cget("fg_color"), "#1E1E2E")
        self.assertEqual(btn.cget("text_color"), "#00E5FF")
        other = self.app.nav_buttons["Startup"]
        self.assertEqual(other.cget("fg_color"), "transparent")

    def test_five_metric_cards_are_rounded_ctk_frames(self):
        self.assertEqual(len(self.app._card_slots), 5)
        for card, _col in self.app._card_slots:
            self.assertIsInstance(card, ctk.CTkFrame)
            self.assertEqual(card.cget("corner_radius"), 12)
            self.assertEqual(int(card.cget("border_width")), 1)
            self.assertEqual(card.cget("border_color"), app_mod.T.BORDER)
            self.assertEqual(card.cget("fg_color"), app_mod.T.BG_CARD)

    def test_cards_registry_still_holds_the_4_tuple_contract(self):
        for title in ("CPU", "RAM", "GPU", "Disk", "Network"):
            with self.subTest(title=title):
                val, bar, detail, dot = self.app.cards[title]
                self.assertEqual(val.cget("text"), "—")
                self.assertIsInstance(bar, ctk.CTkProgressBar)

    def test_every_card_carries_its_mapped_vector_icon(self):
        self.assertEqual(set(self.app._card_icons), set(icons_mod.CARD_ICONS))
        for title, (icon_label, icon_name, accent_key) in \
                self.app._card_icons.items():
            with self.subTest(title=title):
                self.assertEqual(icon_name, icons_mod.CARD_ICONS[title])
                self.assertIsInstance(icon_label, ctk.CTkLabel)
                self.assertEqual(icon_label.cget("text"), "")
                self.assertEqual(icon_label.cget("bg_color"),
                                 app_mod.T.BG_CARD)
                image = icon_label.cget("image")
                self.assertIsInstance(image, ctk.CTkImage)
                self.assertEqual(image.cget("size"), (22, 22))
                self.assertTrue(accent_key, "accent key must be recorded")

    def test_hero_health_card_holds_shield_icon_and_score(self):
        self.assertIsInstance(self.app.health_icon, ctk.CTkLabel)
        image = self.app.health_icon.cget("image")
        self.assertIsInstance(image, ctk.CTkImage)
        self.assertEqual(image.cget("size"), (28, 28))
        self.assertIsInstance(self.app.health_bar, ctk.CTkProgressBar)
        self.assertEqual(self.app.health_bar.get(), 0)
        self.assertEqual(self.app.health_var.get(), "—")


class TestThemeIconRefresh(unittest.TestCase):
    """A live theme toggle must redraw the colour-bearing vector icons and
    re-resolve every rounded card's stored background literal."""

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
        # Leave the global palette in the dark default for later tests.
        self.addCleanup(self.app.apply_theme, "dark")

    def test_light_toggle_redraws_icons_with_light_palette_colours(self):
        _, light = app_mod.T.palettes()
        expected = {("security", 28, light["NEON_CYAN"])}
        for _title, icon_name, accent_key in self.app._card_titles:
            expected.add((icon_name, 22, light[accent_key]))
        with patch.object(app_mod.icons, "get_icon",
                          wraps=app_mod.icons.get_icon) as spy:
            self.app.apply_theme("light")
        seen = {(call.args[0], call.args[1], call.args[2])
                for call in spy.call_args_list}
        self.assertTrue(expected <= seen,
                        "light mode missed the icon redraws for the palette: "
                        f"missing {sorted(expected - seen)}")
        self.assertEqual(self.app._card_icons["CPU"][0].cget("bg_color"),
                         app_mod.T.BG_CARD)
        self.assertEqual(self.app.theme_btn.cget("text"), "◐  Switch to dark")

    def test_light_toggle_re_resolves_stored_card_backgrounds(self):
        self.app.apply_theme("light")
        for card, _col in self.app._card_slots:
            with self.subTest():
                self.assertIsInstance(card, ctk.CTkFrame)
                self.assertEqual(card.cget("fg_color"), app_mod.T.BG_CARD)
                self.assertEqual(card.cget("border_color"), app_mod.T.BORDER)
                self.assertEqual(card.cget("corner_radius"), 12)
        # The light palette actually replaced the dark card background.
        self.assertNotEqual(app_mod.T.BG_CARD, "#1C1C28")
        # And the icon labels still hold fresh CTkImages at their sizes.
        self.assertEqual(
            self.app._card_icons["RAM"][0].cget("image").cget("size"), (22, 22))

    def test_dark_round_trip_restores_the_spec_colours(self):
        self.app.apply_theme("light")
        self.app.apply_theme("dark")
        for card, _col in self.app._card_slots:
            self.assertEqual(card.cget("fg_color"), "#1C1C28")
        self.assertEqual(self.app.theme_btn.cget("text"), "◐  Switch to light")
