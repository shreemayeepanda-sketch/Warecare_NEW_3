"""Predictive disk-full forecasting for the Predictive Health card.

Pure-stdlib forecaster behind the "Predictive Health" card: it takes a
short history of disk ``used_pct`` samples for one mounted volume and
fits a straight line through it (ordinary least squares) to estimate
when that volume reaches 100% used. Deliberately dependency-free --
standard library only, no numpy/scipy/matplotlib, and no imports from
this repo: the whole selling point is that this runs inside the packaged
scanner with zero extra installs.

Data in: ``series`` is a list of ``(ts_unix_int, used_pct)`` tuples
sorted ascending, roughly one sample every 60 s, as recorded by
``scanner.metrics_db.disk_series``. Percentages may be ``None`` (the
recorder stores NULL) and timestamps may repeat. Cleaning policy,
applied identically before any fit:

* an entry whose percentage is ``None`` or non-finite is dropped;
* duplicate timestamps collapse into one point by averaging their
  known (non-None) percentages -- a duplicate is a re-read, and the
  average blunts a flaky sample without inventing a new timestamp;
  a duplicate group that is all None disappears with its timestamp;
* entries whose timestamp is not a whole number of seconds are floored
  to the second, the result is sorted ascending (idempotent), and the
  ``min_samples`` count refers to these unique kept points.

The exhaustive meaning of every ``Prediction.status`` value lives on the
:data:`Prediction` docstring -- UI code branches on those exact strings.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

__all__ = [
    "OlsFit",
    "Prediction",
    "fit_ols",
    "forecast_disk_full",
    "predict_all",
]

log = logging.getLogger("shds.predictive")

#: Slopes with |slope| at or below this many percentage points per hour
#: count as flat ("no_trend"); anything above is a real filling rate.
SLOPE_EPS_PCT_PER_HOUR = 1e-9


@dataclass
class OlsFit:
    """Ordinary least squares of ``y`` against ``x``.

    ``slope``/``intercept`` define ``y ~ slope*x + intercept``. ``r2`` is
    the coefficient of determination clamped into [0.0, 1.0], or None
    when it is undefined (all x identical, or all y identical): a fake
    0.0 quality for a fit that explains nothing would be worse than
    "unknown", and a zero-slope fit needs no quality gate anyway.
    """

    slope: float
    intercept: float
    r2: float | None

    @classmethod
    def from_points(cls, points: Sequence[tuple[float, float]]) -> OlsFit:
        """Fit ``(x, y)`` pairs by OLS; see :func:`fit_ols` for details."""
        return fit_ols(points)


def fit_ols(points: Sequence[tuple[float, float]]) -> OlsFit:
    """Fit ``(x, y)`` points with ordinary least squares.

    ``slope = cov(x, y) / var(x)``, with both moments taken around the
    mean so raw unix timestamps in x cannot lose precision to
    catastrophic cancellation. ``r2 = Sxy^2 / (Sxx * Syy)`` is exactly
    the coefficient of determination for a linear fit (it cannot be
    negative in exact arithmetic; floating-point dust is clamped into
    [0.0, 1.0]). When the reducing denominator is 0.0 -- all x equal,
    or all y equal -- ``r2`` is None instead of a fake 0.0; an
    all-x-identical input degenerates to slope 0.0 / intercept mean(y),
    and an empty or single-point input to slope 0.0, intercept mean(y)
    (0.0 when empty) and r2 None. Non-finite pairs dropped before fit.
    """
    usable = [(float(x), float(y)) for x, y in points
              if math.isfinite(float(x)) and math.isfinite(float(y))]
    if not usable:
        return OlsFit(0.0, 0.0, None)
    n = len(usable)
    mean_x = sum(x for x, _ in usable) / n
    mean_y = sum(y for _, y in usable) / n
    var_x = sum((x - mean_x) ** 2 for x, _ in usable)
    cov_xy = sum((x - mean_x) * (y - mean_y) for x, y in usable)
    if var_x <= 0.0:  # x never varies: the constant fit is all there is
        return OlsFit(0.0, mean_y, None)
    slope = cov_xy / var_x
    intercept = mean_y - slope * mean_x
    ss_yy = sum((y - mean_y) ** 2 for _, y in usable)
    if ss_yy <= 0.0:  # y never varies: r2 is 0/0, report None not 0.0
        return OlsFit(slope, intercept, None)
    return OlsFit(slope, intercept, min(1.0, max(0.0, cov_xy * cov_xy
                                                 / (var_x * ss_yy))))


@dataclass
class Prediction:
    """Forecast verdict for one mounted volume.

    Status contract (exact strings; the UI branches on these and never
    on prose -- the field order is mount, minutes, eta, rate, r2, to
    keep positional construction readable):

    ``ok``
        Data sufficient, the fitted slope is meaningfully positive
        (filling, slope > 1e-9 pct/hour) and qualifies on quality
        (``r2 >= r2_floor``), and the horizon lands within
        ``far_future_days``. All fields populated and usable.
    ``insufficient_data``
        No attempt was made. Fewer unique samples than ``min_samples``,
        a first-to-last span below ``min_span_hours`` (exactly at the
        boundary passes both gates), or no usable percentage at all:
        a missing history never becomes a forecast. Every numeric field
        is None.
    ``no_trend``
        Data suffices but the fitted slope is flat (|slope| <= 1e-9
        pct/hour): the volume is not filling at a measurable rate.
        ``minutes_to_full``/``eta`` are None; ``rate_pct_per_day``
        (approximately 0) and ``r2`` follow from the real fit.
    ``shrinking``
        The fitted slope is negative: usage fell over the window
        (cleanup, retention, user action). No full-disk forecast.
        Like ``no_trend``, only ``rate_pct_per_day`` (negative) and
        ``r2`` are populated; ``minutes_to_full``/``eta`` are None.
    ``unreliable``
        A positive slope exists but the fit fails the quality gate --
        ``r2`` present but below ``r2_floor``, ``r2`` undefined
        (degenerate variance), or the arithmetic degenerated to a
        non-finite value. An ETA would be invented precision:
        ``minutes_to_full``/``eta`` are None while ``rate_pct_per_day``
        and the too-low (or missing) ``r2`` stay visible for honesty.
    ``far_future``
        The fit qualifies and a real forecast was computed, but the
        horizon lies strictly beyond ``far_future_days * 1440``
        minutes. ``minutes_to_full`` keeps the raw computed value and
        ``eta`` is populated only when it still fits inside a
        ``datetime`` (else None); treat this as "not filling soon
        enough to matter", not as a date.

    ``minutes_to_full`` is whole minutes via ``ceil``; ``rate_pct_per_day``
    is always ``slope * 24.0`` where slope is pct/hour; ``eta`` is local
    time at ``last_ts + minutes*60`` and, pathologically, may be None
    even for ``ok`` if that arithmetic leaves what ``datetime`` can
    represent (never expected for sane unix seconds).
    """

    mount: str
    minutes_to_full: int | None
    eta: datetime | None
    rate_pct_per_day: float | None
    r2: float | None
    status: str


def _clean_series(
    series: Sequence[tuple[int, float | None]],
) -> list[tuple[int, float]]:
    """Apply the documented cleaning policy to a raw series (see module)."""
    grouped: dict[int, list[float]] = {}
    for entry in series or ():
        try:
            ts, pct = entry
            key = int(ts)
            value = float(pct)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value):
            continue
        grouped.setdefault(key, []).append(value)
    return [(ts, sum(values) / len(values))
            for ts, values in sorted(grouped.items())]


def forecast_disk_full(
    mount: str,
    series: Sequence[tuple[int, float | None]],
    min_samples: int = 12,
    min_span_hours: float = 6.0,
    r2_floor: float = 0.60,
    far_future_days: float = 3650.0,
) -> Prediction:
    """Estimate when volume ``mount`` reaches 100% used.

    ``min_samples`` is the unique-sample floor, ``min_span_hours`` the
    first-to-last coverage floor (both inclusive as documented), and
    ``far_future_days`` is measured per day (times 1440 for minutes).
    Never raises on bad series data: unusable entries are dropped and a
    series with nothing usable is ``insufficient_data`` (see
    :data:`Prediction` for the exact status contract). Both gates and
    the far-future bound are inclusive-at-boundary; only strictly-below
    or strictly-beyond demotes.
    """
    def _bare(status: str) -> Prediction:
        """A verdict with no numbers at all behind it."""
        return Prediction(mount, None, None, None, None, status)

    kept = _clean_series(series)
    if len(kept) < min_samples:
        log.debug("%s: %d usable samples, needs %d", mount, len(kept),
                  min_samples)
        return _bare("insufficient_data")
    last_ts = kept[-1][0]
    if (last_ts - kept[0][0]) / 3600.0 < min_span_hours:
        log.debug("%s: span below %.3fh gate", mount, min_span_hours)
        return _bare("insufficient_data")

    # Fit in hours with the origin at the last sample: the slope comes
    # out in pct/hour directly, and the intercept is exactly the fitted
    # percentage at the last sample (used as the ETA's starting point).
    fit = fit_ols(((ts - last_ts) / 3600.0, pct) for ts, pct in kept)
    rate = fit.slope * 24.0
    if not math.isfinite(fit.slope) or (fit.r2 is not None
                                        and not math.isfinite(fit.r2)):
        return Prediction(mount, None, None, None, fit.r2, "unreliable")
    if abs(fit.slope) <= SLOPE_EPS_PCT_PER_HOUR:
        return Prediction(mount, None, None, rate, fit.r2, "no_trend")
    if fit.slope < 0.0:
        return Prediction(mount, None, None, rate, fit.r2, "shrinking")
    # Filling: qualify the fit before quoting any date.
    if fit.r2 is None or fit.r2 < r2_floor:
        return Prediction(mount, None, None, rate, fit.r2, "unreliable")

    fitted_last = fit.intercept  # fitted pct at the last sample
    if not math.isfinite(fitted_last):  # unreachable on sane data; guard
        return Prediction(mount, None, None, rate, None, "unreliable")
    minutes = 0
    if fitted_last < 100.0:
        hours_to_full = (100.0 - fitted_last) / fit.slope
        minutes = max(0, math.ceil(hours_to_full * 60.0))  # clamp fp dust
    if minutes > far_future_days * 1440.0:
        try:
            eta = datetime.fromtimestamp(last_ts) + timedelta(minutes=minutes)
        except (OverflowError, OSError, ValueError):
            eta = None  # beyond what a datetime can represent
        return Prediction(mount, minutes, eta, rate, fit.r2, "far_future")
    try:
        eta = datetime.fromtimestamp(last_ts + minutes * 60)
    except (OSError, OverflowError, ValueError):
        log.debug("%s: eta out of datetime range; kept minutes only", mount)
        eta = None
    return Prediction(mount, minutes, eta, rate, fit.r2, "ok")


def predict_all(
    db_path: str | Path | None = None,
    *,
    now_ts: int | None = None,
    **forecast_kwargs: Any,
) -> list[Prediction]:
    """Forecast every mount in the metrics history (optional integration).

    ``scanner.metrics_db`` is the sibling SQLite module of this feature
    and is imported lazily inside this function so the pure math above
    stays importable standalone (tests import just this module freely).
    ``db_path=None`` resolves to ``metrics_db.default_db_path()``;
    ``now_ts`` is forwarded to ``disk_series`` so tests can pin the
    clock; ``forecast_kwargs`` pass through to
    :func:`forecast_disk_full`.

    Failure policy (no errors escape): a missing/unopenable module or a
    mount listing that fails yields ``[]``; one mount whose series
    cannot be fetched or forecast degrades to an ``insufficient_data``
    Prediction so the card still lists the drive instead of silently
    dropping it.
    """
    try:
        from . import metrics_db  # noqa: PLC0415 -- deliberate lazy import
    except Exception as exc:  # the optional sibling may simply not exist
        log.debug("metrics_db unavailable; no predictions: %s", exc)
        return []
    try:
        path = db_path if db_path is not None else metrics_db.default_db_path()
        mounts = metrics_db.disk_mounts(path)
    except Exception as exc:
        log.debug("metrics_db mount listing failed: %s", exc)
        return []
    predictions: list[Prediction] = []
    for mount in mounts or []:
        try:
            rows = metrics_db.disk_series(path, mount, now_ts=now_ts)
            points = [(int(row[0]), row[1]) for row in rows if len(row) >= 2]
            predictions.append(
                forecast_disk_full(mount, points, **forecast_kwargs))
        except Exception as exc:
            log.debug("%s: series/forecast failed: %s", mount, exc)
            predictions.append(Prediction(mount, None, None, None, None,
                                          "insufficient_data"))
    return predictions
