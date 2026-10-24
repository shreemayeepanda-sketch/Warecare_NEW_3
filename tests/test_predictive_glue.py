"""Predictive Health glue tests: the Trends card's pure formatting layer.

The forecast engine itself lives in scanner.predictive (verified by
test_predictive.py) and the metrics history in scanner.metrics_db
(test_metrics_db.py). This file pins the layer between engine and
window: the module-level helpers in the standalone ``ewarecare.py`` that

* turn one Prediction into the card's ``Disk C: ...`` line (exact copy
  per documented status, ASCII, thresholds inclusive);
* mark the urgent window with the theme's warning key;
* build the card's (lines, hint) payload, including the honest
  empty-db / failed-collect entries -- an empty metrics.db is the
  "monitoring never ran" state, not a forecast of nothing;
* report why the card cannot forecast at all when an optional module
  is missing.

Ground rules: headless. Every assertion here runs against plain data --
no Tk window, no metrics.db on the real user directory (the one UI test
patches the collect path's db away from the user directory is NOT
needed: the card's data collector is wrapped and reads-only, and the
single window test exercises layout only). One test constructs the real
ScannerApp, mirroring tests/test_standalone_ui.py's minimal pattern.
"""
from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG_PARENT = ROOT / "smart_hardware_diagnostic_scanner"
for _path in (str(ROOT), str(PKG_PARENT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import ewarecare as ew  # noqa: E402
from scanner import metrics_db  # noqa: E402
from scanner import predictive  # noqa: E402
from scanner.predictive import Prediction  # noqa: E402


def pred(mount="C:"):
    """Build a Prediction (positional field order: mount, minutes, eta,
    rate, r2, status) with keyword sugar for the status-heavy tests."""

    def build(status, minutes=None, eta=None, rate=None, r2=None):
        return Prediction(mount, minutes, eta, rate, r2, status)

    return build


ok = pred("C:")


# ---------------------------------------------------------------------------
# trend_line: one line per documented status
# ---------------------------------------------------------------------------
def test_trend_line_ok_below_a_day_uses_hours():
    # 1439 minutes -> hours copy, not days (the 1440 boundary is the switch)
    assert ew.trend_line(ok("ok", minutes=1439)) == "Disk C: full in 24 hrs"


def test_trend_line_ok_at_a_day_uses_days():
    # 1440 minutes crosses the hours->days boundary and stays singular
    assert ew.trend_line(ok("ok", minutes=1440)) == "Disk C: full in 1 day"


def test_trend_line_ok_quotes_days_at_the_45_day_edge():
    # 65280 min = 45.33 d -> rounds to 45 -> still inside the full-in copy
    assert ew.trend_line(ok("ok", minutes=65280)) == "Disk C: full in 45 days"


def test_trend_line_ok_appends_the_eta_date_on_day_horizons():
    line = ew.trend_line(ok("ok", minutes=20160,
                            eta=datetime(2026, 11, 7, 5, 30), rate=0.6,
                            r2=0.9))
    assert line == "Disk C: full in 14 days (by Nov 7)"


def test_trend_line_ok_appends_no_date_on_hour_horizons():
    line = ew.trend_line(ok("ok", minutes=720, eta=datetime(2026, 11, 7),
                            rate=6.0, r2=0.9))
    assert "(by " not in line
    assert line == "Disk C: full in 12 hrs"


def test_trend_line_ok_beyond_45_days_quotes_the_rate():
    # 66240 min = 46 d, past the soon window: state the rate, not a date
    line = ew.trend_line(ok("ok", minutes=66240, rate=1.234, r2=0.9))
    assert line == "Disk C: growing 1.2%/day"


def test_trend_line_exact_copy_per_non_ok_status():
    cases = [
        ("no_trend", "Disk C: stable"),
        ("shrinking", "Disk C: freeing space"),
        ("unreliable", "Disk C: trend unclear"),
        ("far_future", "Disk C: very slow growth"),
        ("insufficient_data", "Disk C: collecting data..."),
    ]
    for status, expected in cases:
        # far_future keeps minutes/eta by contract; they must NOT leak
        # into the copy -- "not filling soon" is the whole message.
        line = ew.trend_line(ok(status, minutes=5000000,
                                eta=datetime(2800, 1, 1), rate=0.01))
        assert line == expected, status


def test_trend_line_unknown_status_and_broken_object_never_raise():
    assert ew.trend_line(ok("weird_status")) == "Disk C: trend unclear"
    assert ew.trend_line(object()) == "Disk ?: trend unclear"


def test_trend_urgency_marks_only_the_urgent_window():
    seven_days = 7 * 1440
    assert ew.trend_urgency(ok("ok", minutes=seven_days)) \
        == ew.TREND_WARN_KEY == "NEON_YELLOW"
    assert ew.trend_urgency(ok("ok", minutes=seven_days + 1440)) == ""
    # A kept far-future horizon is never urgent; no status, no urgency.
    assert ew.trend_urgency(ok("far_future", minutes=seven_days)) == ""
    assert ew.trend_urgency(ok("insufficient_data")) == ""


# ---------------------------------------------------------------------------
# trends_lines / trends_summary: the whole card payload
# ---------------------------------------------------------------------------
def test_trends_summary_no_mounts_and_recorder_off_says_monitoring():
    lines, hint = ew.trends_summary([], recorder_running=False,
                                    mounts_recorded=False)
    assert lines == ["Collecting metrics..."]
    assert hint == "Starts with monitoring."


def test_trends_summary_no_mounts_while_recording_listens_quietly():
    lines, hint = ew.trends_summary([], recorder_running=True,
                                    mounts_recorded=False)
    assert lines == ["Collecting metrics..."]
    assert hint == ""


def test_trends_summary_failed_collect_degrades_without_hint():
    """The off-thread collect itself failed; no hint may be claimed
    unless the mount state is KNOWN empty per the tri-state contract."""
    lines, hint = ew.trends_summary(None, recorder_running=False,
                                    mounts_recorded=True)
    assert lines == ["Collecting metrics..."]
    assert hint == ""
    lines, hint = ew.trends_summary(None, recorder_running=False,
                                    mounts_recorded=None)
    assert lines == ["Collecting metrics..."]
    assert hint == ""


def test_trends_summary_all_insufficient_is_collecting_per_drive():
    ins = pred("C:")
    ins_d = pred("D:")
    lines, hint = ew.trends_summary(
        [ins("insufficient_data"), ins_d("insufficient_data")],
        recorder_running=False, mounts_recorded=True)
    assert lines == ["Disk C: collecting data...",
                     "Disk D: collecting data..."]
    assert hint == "first forecast in ~6 h of data"


def test_trends_summary_orders_worst_first_among_ok_horizons():
    slow = pred("C:")
    fast = pred("D:")
    lines, hint = ew.trends_summary(
        [slow("ok", minutes=57600), fast("ok", minutes=18720)],
        recorder_running=False, mounts_recorded=True)
    # 13 days fills before 40 days and must be listed first.
    assert lines == ["Disk D: full in 13 days", "Disk C: full in 40 days"]
    assert hint == ""


def test_trends_summary_mixed_statuses_order_and_far_future_copy():
    rows = [
        pred("D:")("ok", minutes=11520),           # 8 days
        pred("E:")("shrinking", rate=-0.5),
        pred("C:")("ok", minutes=10080),           # 7 days, urgent
        pred("F:")("insufficient_data"),
        pred("G:")("far_future", minutes=5000000, rate=0.01),
        pred("H:")("no_trend", rate=0.0),
    ]
    pairs, hint = ew.trends_lines(rows, recorder_running=False,
                                  mounts_recorded=True)
    texts = [str(text) for text, _warn in pairs]
    # Horizons sort first (soonest: 7d before 8d before the far-future
    # kilometres of minutes), then the no-horizon statuses, still
    # collecting last.
    assert texts == ["Disk C: full in 7 days",
                     "Disk D: full in 8 days",
                     "Disk G: very slow growth",
                     "Disk E: freeing space",
                     "Disk H: stable",
                     "Disk F: collecting data..."]
    assert hint == ""
    warn = [key for _text, key in pairs]
    assert warn[0] == ew.TREND_WARN_KEY      # 7 days -> urgent
    assert warn[1] == ""                      # 8 days -> neutral


def test_trends_summary_caps_drives_with_an_explicit_tail():
    rows = [pred(f"D{i}")(f"insufficient_data") for i in range(9)]
    lines, hint = ew.trends_summary(rows, recorder_running=False,
                                    mounts_recorded=True)
    assert len(lines) == ew.TREND_MAX_LINES + 1
    assert lines[-1] == "... and 3 more drives"
    assert hint == "first forecast in ~6 h of data"


# ---------------------------------------------------------------------------
# Module-missing + failure degrade copies (off-thread collector equivalents,
# exercised as pure functions, no threads)
# ---------------------------------------------------------------------------
def test_trends_payload_metrics_module_missing_copy(monkeypatch):
    monkeypatch.setattr(ew, "METRICS_AVAILABLE", False)
    monkeypatch.setattr(ew, "_metrics", None)
    pairs, hint = ew.trends_payload(recorder_running=False)
    assert [str(text) for text, _warn in pairs] == \
        ["Trends unavailable - metrics module missing"]
    assert hint == ""


def test_trends_payload_predictive_module_missing_copy(monkeypatch):
    monkeypatch.setattr(ew, "PREDICTIVE_AVAILABLE", False)
    monkeypatch.setattr(ew, "_predictive", None)
    pairs, hint = ew.trends_payload(recorder_running=False)
    assert [str(text) for text, _warn in pairs] == \
        ["Trends unavailable - prediction module missing"]
    assert hint == ""


def test_trends_payload_predict_all_failure_degrades(monkeypatch, tmp_path):
    """The threading gate as a pure call: default_db_path is pinned to an
    empty tmp db (reads never create the file) and predict_all is made to
    raise -- the payload must degrade to the collecting line + hint."""
    def _boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(metrics_db, "default_db_path", lambda: tmp_path)
    monkeypatch.setattr(predictive, "predict_all", _boom)
    pairs, hint = ew.trends_payload(recorder_running=False)
    assert [str(text) for text, _warn in pairs] == ["Collecting metrics..."]
    assert "monitoring" in hint


def test_trends_payload_empty_db_and_missing_module_flags_are_honest():
    # Unpatched repo state: both optional modules import (the package ships
    # them), and the reason guard is exactly "" until one is missing.
    assert ew.METRICS_AVAILABLE is True
    assert ew.PREDICTIVE_AVAILABLE is True
    assert ew.trends_unavailable_reason() == ""


# ---------------------------------------------------------------------------
# The one UI interaction test (same minimal window pattern as the audit
# suite: real ScannerApp, pump() instead of mainloop, no dialogs)
# ---------------------------------------------------------------------------
def pump(app, ms):
    """Pump the Tk event loop for ~ms milliseconds (never mainloop)."""
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        app.update()


def walk(widget):
    """All descendants of `widget` (same recursion the app's re-theme uses)."""
    out = []
    for child in widget.winfo_children():
        out.append(child)
        out.extend(walk(child))
    return out


@pytest.fixture(scope="module")
def app():
    """The one scanner window for this module (single Tk root)."""
    with patch.object(ew.messagebox, "showwarning"), \
         patch.object(ew.messagebox, "showinfo"), \
         patch.object(ew.messagebox, "showerror"):
        instance = ew.ScannerApp()
    pump(instance, 400)
    yield instance
    instance.on_closing()        # cancels after-loops, destroys the window
    pump(instance, 30)


def test_trends_card_title_and_body_exist(app):
    """The Trends card is on the Dashboard with its title and one body line.

    Body content depends on the live machine (a real metrics.db may hold
    actual forecasts) -- assert the card structure, not a specific copy.
    """
    dashboard = app.pages["Dashboard"]
    titles = [str(w.cget("text")) for w in walk(dashboard)
              if w.winfo_class() == "Label"]
    assert "Trends" in titles, "the Trends card title is missing"
    body_labels = [w for w in walk(app.trends_lines)
                   if w.winfo_class() == "Label"]
    assert body_labels, "the Trends card body rendered no line"
    assert all(str(w.cget("text")) for w in body_labels), \
        "the Trends card rendered an empty line label"
    # The queued payload cache exists and is well-formed so the theme
    # toggle's re-render has something honest to redraw.
    cached = app._trends_last
    assert isinstance(cached, tuple) and len(cached) == 2
    lines, hint = cached
    assert lines and isinstance(hint, str)
