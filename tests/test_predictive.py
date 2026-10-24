"""Predictive Health forecaster tests -- scanner.predictive, pure math.

Headless by construction: no threads, no GUI, no real disk, no
psutil/customtkinter -- the entire file is hand-computed arithmetic
against the stdlib-only OLS + ETA horizon code, plus (now that the
sibling module has landed) a sqlite round-trip through
scanner.metrics_db with a tmp_path database and a pinned clock.

Timestamps use a fixed unix base T0 so every eta is deterministic on
any machine or timezone: expectations are built with
datetime.fromtimestamp exactly like the module does, so comparisons
cannot drift with wall clock. Coverage:

* fit_ols exactness on hand-computed lines (slope 2.0, intercept 50.0,
  r2 == 1.0), a hand-derived noisy fit (slope 4.2, r2 1764/1840),
  constant-x / constant-y degeneration (r2 None, not a fake 0.0),
  empty and single-point inputs, and the from_points classmethod;
* forecast_disk_full statuses, one exact string per branch:
  insufficient_data (empty series, all-None series, single point,
  11 of 12 samples, 200 samples crammed into 199 s), and the boundary
  the other way (exactly 12 samples spanning exactly 6 h passes),
  no_trend (perfectly flat), shrinking (negative slope), unreliable
  (hand-computed r2 = 51076/219076 < 0.60), ok with noise around a
  line (r2 in range, eta within tolerance of the true horizon), ok
  with exact eta arithmetic (minutes_to_full == 1140), already-full
  lines (0 minutes, eta at the last sample), far_future horizons
  (eta representable vs overflowed past datetime's range);
* the documented cleaning policy: None entries dropped,
  duplicate timestamps averaged (and never double-counted for
  min_samples);
* 100% accounting: rate_pct_per_day == slope * 24.0;
* predict_all integration: record_sample round-trip producing a "C:"
  ok forecast and a "D:" shrinking forecast, a missing database that
  yields [], a corrupt database that yields [], and an all-NULL-pct
  mount that degrades to insufficient_data without raising. Skipped
  via skipif if scanner.metrics_db is ever unavailable.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG_PARENT = ROOT / "smart_hardware_diagnostic_scanner"
if str(PKG_PARENT) not in sys.path:
    sys.path.insert(0, str(PKG_PARENT))

import scanner.predictive as predictive  # noqa: E402

try:  # optional sibling module, owned by another agent in this feature
    import scanner.metrics_db as metrics_db  # noqa: E402
except ImportError:  # pragma: no cover - only before metrics_db lands
    metrics_db = None

T0 = 1_710_000_000  # fixed epoch second; keeps every expectation exact


def _series(pcts, step=3600, t0=T0):
    """Ascending (t0 + i*step, pct) series, None entries passed through."""
    return [(t0 + i * step, pct) for i, pct in enumerate(pcts)]


def _dts(ts):
    """The exact eta arithmetic the module itself uses."""
    return datetime.fromtimestamp(ts)


# ---------------------------------------------------------------------------
# fit_ols / OlsFit
# ---------------------------------------------------------------------------


def test_fit_ols_exact_line():
    points = [(x, 50.0 + 2.0 * x) for x in range(6)]  # x = 0..5
    fit = predictive.fit_ols(points)
    assert fit.slope == pytest.approx(2.0, abs=1e-9)
    assert fit.intercept == pytest.approx(50.0, abs=1e-9)
    assert fit.r2 == pytest.approx(1.0, abs=1e-9)


def test_fit_ols_noisy_hand_values():
    # Centered around (0, 52): slope = 42/10, r2 = 42^2 / (10 * 184).
    points = [(-2, 45), (-1, 47), (0, 51), (1, 55), (2, 62)]
    fit = predictive.fit_ols(points)
    assert fit.slope == pytest.approx(4.2)
    assert fit.intercept == pytest.approx(52.0)
    assert fit.r2 == pytest.approx(1764 / 1840)
    assert 0.0 < fit.r2 < 1.0


def test_fit_ols_constant_axes_degenerate():
    # All x identical: the constant fit is all there is, r2 undefined.
    fit = predictive.fit_ols([(1, 10), (1, 20), (1, 30)])
    assert fit.slope == 0.0
    assert fit.intercept == pytest.approx(20.0)
    assert fit.r2 is None
    # All y identical: slope is a true 0.0, but r2 is 0/0, not 0.0.
    fit = predictive.fit_ols([(0, 20), (1, 20), (2, 20)])
    assert fit.slope == 0.0
    assert fit.intercept == pytest.approx(20.0)
    assert fit.r2 is None


def test_fit_ols_empty_and_single_point():
    fit = predictive.fit_ols([])
    assert (fit.slope, fit.intercept, fit.r2) == (0.0, 0.0, None)
    fit = predictive.fit_ols([(3, 9)])
    assert fit.slope == 0.0
    assert fit.intercept == pytest.approx(9.0)
    assert fit.r2 is None


def test_ols_from_points_matches_function():
    points = [(x, 50.0 + 2.0 * x) for x in range(6)]
    assert predictive.OlsFit.from_points(points) == predictive.fit_ols(points)


# ---------------------------------------------------------------------------
# forecast_disk_full -- insufficient_data gate
# ---------------------------------------------------------------------------


def test_forecast_nothing_usable_insufficient():
    for series in ([], [(T0, None), (T0 + 60, None)], [(T0, 42.0)]):
        pred = predictive.forecast_disk_full("C:", series)
        assert pred.status == "insufficient_data"
        assert pred.mount == "C:"
        assert pred.minutes_to_full is None
        assert pred.eta is None
        assert pred.rate_pct_per_day is None
        assert pred.r2 is None


def test_forecast_below_each_gate_insufficient():
    # 11 usable samples over a beautiful 11 h span: still short one point.
    eleven = _series([40.0 + 2.0 * i for i in range(11)])
    assert predictive.forecast_disk_full("C:", eleven).status == \
        "insufficient_data"
    # Plenty of samples (200) but crammed into 199 seconds: span gate.
    crammed = _series([40.0 + 0.1 * i for i in range(200)], step=1)
    assert predictive.forecast_disk_full("C:", crammed).status == \
        "insufficient_data"


def test_forecast_exact_boundary_gates_pass():
    # Exactly 12 unique samples whose span is exactly 6 h: both gates are
    # inclusive, so this series gets a fit. Flat data deep inside the
    # coverage -> no_trend, proving both gates were passed.
    pcts = [51.0] * 12
    series = [(T0 + i * 60, 51.0) for i in range(11)]
    series.append((T0 + 21600, 51.0))
    assert len(series) == 12
    pred = predictive.forecast_disk_full("C:", series)
    assert pred.status == "no_trend"
    assert predictive.OlsFit.from_points  # stable import shape, quiet lint


# ---------------------------------------------------------------------------
# forecast_disk_full -- trend branches
# ---------------------------------------------------------------------------


def test_forecast_flat_series_no_trend():
    # 30 samples over 8 h at exactly 50%: descriptive numbers, no date.
    pred = predictive.forecast_disk_full("C:", _series([50.0] * 30, step=960))
    assert pred.status == "no_trend"
    assert pred.minutes_to_full is None and pred.eta is None
    assert pred.rate_pct_per_day == 0.0
    assert pred.r2 is None  # constant y: r2 is undefined, not 0


def test_forecast_decreasing_series_shrinking():
    # 89 down by 4 each hour: slope -4.0 pct/h -> -96.0 pct/day exactly.
    pred = predictive.forecast_disk_full("C:", _series([89.0 - 4.0 * i
                                                        for i in range(12)]))
    assert pred.status == "shrinking"
    assert pred.minutes_to_full is None and pred.eta is None
    assert pred.rate_pct_per_day == pytest.approx(-96.0, abs=1e-9)
    assert pred.r2 == pytest.approx(1.0, abs=1e-9)


def test_forecast_noisy_rising_series_ok():
    # Deterministic jitter around a true 2 pct/h line ending at ~64%:
    # the perfect-line horizon would be (100 - 64) / 2 = 18 h = 1080 min.
    w = [0, 1, -1, 0, 1, -1, 0, 1, -1, 0, 1, -1]
    pred = predictive.forecast_disk_full("C:", _series([40.0 + 2.0 * i + w_i
                                                        for i, w_i in
                                                        enumerate(w)]))
    assert pred.status == "ok"
    assert pred.r2 is not None and 0.0 < pred.r2 <= 1.0
    assert pred.rate_pct_per_day == pytest.approx(24.0 * 2.0, rel=0.15)
    assert 1000 <= pred.minutes_to_full <= 1260
    true_eta = _dts(T0 + 11 * 3600) + timedelta(minutes=1080)
    assert abs(pred.eta - true_eta) <= timedelta(minutes=180)


def test_forecast_exact_eta_arithmetic():
    # Perfect line 40 .. 62 over 11 h: slopes/r2/intercepts are exact,
    # so minutes_to_full = ceil((100 - 62) / 2 * 60) = 1140 exactly.
    pred = predictive.forecast_disk_full("C:", _series([40.0 + 2.0 * i
                                                        for i in range(12)]))
    assert pred.status == "ok"
    assert pred.minutes_to_full == 1140
    assert pred.eta == _dts(T0 + 11 * 3600 + 1140 * 60)
    # The brief-style check: counted forward from the FIRST sample.
    from_first = _dts(T0 + (11 * 60 + 1140) * 60)
    assert abs(pred.eta - from_first) < timedelta(minutes=1)
    assert pred.rate_pct_per_day == pytest.approx(48.0, abs=1e-9)
    assert pred.r2 == pytest.approx(1.0, abs=1e-9)


def test_rate_pct_per_day_is_slope_times_24():
    series = _series([40.0 + 2.0 * i for i in range(12)])
    last_ts = series[-1][0]
    pred = predictive.forecast_disk_full("C:", series)
    # Same fit convention as the module (hours back from last sample).
    fit = predictive.fit_ols(((t - last_ts) / 3600.0, p) for t, p in series)
    assert pred.rate_pct_per_day == pytest.approx(fit.slope * 24.0)


def test_forecast_unreliable_low_r2():
    # 30 + 2i + 10*(-1)^i: strong sawtooth on a rising line. Hand-derived
    # in one-centered units: slope = 226/143 pct/h, r2 = 226^2/219076.
    pcts = [30.0 + 2.0 * i + 10.0 * (1.0 if i % 2 == 0 else -1.0)
            for i in range(12)]
    pred = predictive.forecast_disk_full("C:", _series(pcts))
    assert pred.status == "unreliable"
    assert pred.r2 == pytest.approx(51076 / 219076, rel=1e-9)
    assert pred.r2 < 0.60
    assert pred.rate_pct_per_day == pytest.approx(226.0 / 143.0 * 24.0,
                                                  rel=1e-9)
    assert pred.minutes_to_full is None and pred.eta is None


def test_forecast_already_full_zero_minutes():
    # Line ending exactly at 100%: minutes 0, eta at the last sample.
    pred = predictive.forecast_disk_full("C:", _series([45.0 + 5.0 * i
                                                        for i in range(12)]))
    last_ts = T0 + 11 * 3600
    assert pred.status == "ok"
    assert pred.minutes_to_full == 0
    assert pred.eta == _dts(last_ts)
    # Line already past 100% (ends at 103): same degenerate answer.
    pred = predictive.forecast_disk_full("C:", _series([48.0 + 5.0 * i
                                                        for i in range(12)]))
    assert pred.minutes_to_full == 0
    assert pred.eta == _dts(last_ts)


def test_forecast_far_future_horizons():
    # Tiny-but-real slope: ~1.5e7 minutes out, beyond the 3650-day
    # default. eta is far but still representable by datetime.
    pred = predictive.forecast_disk_full("C:", _series([50.0 + 0.0002 * i
                                                        for i in range(12)]))
    assert pred.status == "far_future"
    assert pred.minutes_to_full > 3650 * 1440
    assert pred.eta is not None
    assert (pred.eta - _dts(T0 + 11 * 3600)).total_seconds() > 3650 * 86400
    # Pathologically tiny slope: minutes in the trillions, so the eta
    # arithmetic falls off datetime's representable range -> eta None.
    pred = predictive.forecast_disk_full("C:", _series([50.0 + 1e-8 * i
                                                        for i in range(12)]))
    assert pred.status == "far_future"
    assert pred.minutes_to_full is not None and pred.minutes_to_full == \
        pytest.approx(3e11, rel=0.2)
    assert pred.eta is None


# ---------------------------------------------------------------------------
# documented cleaning policy
# ---------------------------------------------------------------------------


def test_none_entries_dropped_policy():
    seq = _series([None if i in (3, 7, 9) else 40.0 + 2.0 * i
                   for i in range(15)])
    reference = [(T0 + i * 3600, 40.0 + 2.0 * i) for i in range(15)
                 if i not in (3, 7, 9)]
    pred = predictive.forecast_disk_full("C:", seq)
    assert pred == predictive.forecast_disk_full("C:", reference)
    assert pred.status == "ok"
    assert pred.minutes_to_full == 960  # line ends at 68% -> 16 h -> 960


def test_duplicate_timestamps_averaged_policy():
    ts6 = T0 + 6 * 3600
    reference = [(T0 + i * 3600, 40.0 + 2.0 * i) for i in range(12)
                 if i != 6]
    reference.append((ts6, 45.0))
    duplicated = [(T0 + i * 3600, 40.0 + 2.0 * i) for i in range(12)
                  if i != 6]
    duplicated.append((ts6, 40.0))
    duplicated.append((ts6, 50.0))  # averages to the same 45.0 point
    pred = predictive.forecast_disk_full("C:", duplicated)
    assert pred == predictive.forecast_disk_full("C:", reference)
    assert pred.status == "ok"
    # 11 unique timestamps with one revisited: 12 raw rows, 11 kept ->
    # the min_samples gate counts unique kept points, not raw rows.
    eleven = [(T0 + i * 3600, 40.0 + 2.0 * i) for i in range(11)]
    eleven.append((eleven[5][0], 99.0))
    assert predictive.forecast_disk_full("C:", eleven).status == \
        "insufficient_data"


# ---------------------------------------------------------------------------
# status vocabulary contract
# ---------------------------------------------------------------------------


def test_status_vocabulary_exhaustive():
    allowed = {"ok", "insufficient_data", "no_trend", "shrinking",
               "unreliable", "far_future"}
    statuses = [
        predictive.forecast_disk_full("M", []).status,               # gate
        predictive.forecast_disk_full("M", _series([40 + 2 * i
                                                    for i in range(12)])
                                      ).status,                      # ok
        predictive.forecast_disk_full("M", _series([51.0] * 12)).status,
        predictive.forecast_disk_full("M", _series([89.0 - 4.0 * i
                                                    for i in range(12)])
                                      ).status,                      # shr
        predictive.forecast_disk_full(
            "M", _series([30.0 + 2.0 * i + 10.0 * (-1.0) ** i
                          for i in range(12)])).status,            # unrel
        predictive.forecast_disk_full("M", _series([50.0 + 0.0002 * i
                                                    for i in range(12)])
                                      ).status,                # far future
    ]
    assert set(statuses) == allowed


# ---------------------------------------------------------------------------
# predict_all integration (scanner.metrics_db)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(metrics_db is None,
                    reason="scanner.metrics_db not available yet")
def test_predict_all_roundtrip_ok_and_shrinking(tmp_path):
    db = tmp_path / "metrics.db"
    last_tick = T0 + 79 * 300  # 80 ticks 300 s apart: span 6.583 h
    for i in range(80):
        ts = T0 + i * 300
        rows = [("C:", 50.0 + 12.0 * i / 79.0, None, None),
                ("D:", 60.0 - 40.0 * i / 79.0, None, None)]
        assert metrics_db.record_sample(db, ts, None, None, rows) is True
    preds = predictive.predict_all(db, now_ts=last_tick)
    assert [p.mount for p in preds] == ["C:", "D:"]
    c, d = preds
    # C: rises 50 -> 62 over 6.583 h (1.82 pct/h): eta ~ 1251 minutes out.
    assert c.status == "ok"
    assert c.rate_pct_per_day > 0
    assert 1150 <= c.minutes_to_full <= 1350
    expected = _dts(last_tick) + timedelta(minutes=1251)
    assert abs(c.eta - expected) <= timedelta(minutes=30)
    # D: falls 60 -> 20 over the window: a cleanup story, no ETA quoted.
    assert d.status == "shrinking"
    assert d.rate_pct_per_day < 0
    assert d.minutes_to_full is None and d.eta is None


@pytest.mark.skipif(metrics_db is None,
                    reason="scanner.metrics_db not available yet")
def test_predict_all_missing_db_returns_empty(tmp_path):
    # Never fabricate: a database that does not exist yields no mounts.
    assert predictive.predict_all(tmp_path / "absent.db") == []


@pytest.mark.skipif(metrics_db is None,
                    reason="scanner.metrics_db not available yet")
def test_predict_all_corrupt_db_returns_empty(tmp_path):
    db = tmp_path / "corrupt.db"
    db.write_bytes(b"not a database" * 32)
    # metrics_db shields its reads; predict_all must not raise either.
    assert predictive.predict_all(db) == []


@pytest.mark.skipif(metrics_db is None,
                    reason="scanner.metrics_db not available yet")
def test_predict_all_all_null_mount_insufficient(tmp_path):
    db = tmp_path / "metrics.db"
    for i in range(20):
        ok = metrics_db.record_sample(db, T0 + i * 300, None, None,
                                      [("E:", None, None, None)])
        assert ok is True
    preds = predictive.predict_all(db, now_ts=T0 + 19 * 300)
    assert [p.mount for p in preds] == ["E:"]
    assert preds[0].status == "insufficient_data"
    assert preds[0].minutes_to_full is None and preds[0].eta is None
