"""Clock-anchored, 24-hour occupancy projection and later honest validation.

The long horizon is a historical time-of-week expectation, not an extrapolation
from the last synthetic count. Short-horizon boosted/ridge models are used only
when a genuinely recent live reading is supplied. A forecast can be used as an
*assumed* value for downstream simulations, but it is never an observation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from occupancy_core import INTERVAL_MIN, conformal_radius, forecast_from_series, read_series


@dataclass
class Calendar24hModel:
    room_id: str
    capacity: int
    timezone: str
    patterns: dict  # weekday -> 288 smoothed five-minute medians
    fallback: float
    radius_80: float
    calibration_mae: float
    calibration_coverage_80: float
    training_until: str
    calibration_from: str
    calibration_points: int


def current_local_time(tz: str = 'Europe/London', at: str | pd.Timestamp | None = None) -> pd.Timestamp:
    if at is None:
        return pd.Timestamp(datetime.now(ZoneInfo(tz)))
    ts = pd.Timestamp(at)
    if ts.tzinfo is None:
        return ts.tz_localize(tz, ambiguous='raise', nonexistent='raise')
    return ts.tz_convert(tz)


def fit_calendar_24h(series: pd.Series, room_id: str, capacity: int,
                     *, timezone: str = 'Europe/London', calibration_days: int = 14) -> Calendar24hModel:
    """Fit from the *past*, calibrate on a later held-out period.

    A weekday x five-minute median is smoothed over 35 minutes; it is deliberately
    low-complexity because true live observations may be unavailable for 24 hours.
    """
    observed = series.dropna().sort_index()
    if len(observed) < 28 * 24 * 12:
        raise ValueError('At least four weeks of five-minute history are required for the 24-hour calendar model')
    cut = observed.index.max() - pd.Timedelta(days=calibration_days)
    train = observed.loc[observed.index <= cut]
    calibration = observed.loc[observed.index > cut]
    if train.empty or len(calibration) < 100:
        raise ValueError('Insufficient calibration history')
    # Backtest calendar forecasts on dates strictly later than the fitting data.
    df = pd.DataFrame({'count': train.to_numpy(),
                       'weekday': train.index.dayofweek,
                       'slot': train.index.hour * 12 + train.index.minute // INTERVAL_MIN})
    median = df.groupby(['weekday', 'slot'])['count'].median()
    fallback = float(train.median())
    patterns = {}
    for weekday in range(7):
        raw = pd.Series([float(median.get((weekday, slot), fallback)) for slot in range(288)])
        # 35-minute smooth, with end padding to avoid midnight discontinuities.
        padded = pd.concat([raw.iloc[-3:], raw, raw.iloc[:3]], ignore_index=True)
        smooth = padded.rolling(7, center=True, min_periods=1).mean().iloc[3:-3]
        patterns[weekday] = np.clip(smooth.to_numpy(), 0, capacity)
    prototype = Calendar24hModel(room_id, capacity, timezone, patterns, fallback,
                                 0.0, 0.0, 0.0, str(train.index.max()),
                                 str(calibration.index.min()), len(calibration))
    expected = seasonal_predict(prototype, calibration.index)
    errors = calibration.to_numpy() - expected
    q = conformal_radius(errors)
    lower = np.clip(expected - q, 0, capacity)
    upper = np.clip(expected + q, 0, capacity)
    prototype.radius_80 = q
    prototype.calibration_mae = float(np.abs(errors).mean())
    prototype.calibration_coverage_80 = float(np.mean((calibration.to_numpy() >= lower) &
                                                     (calibration.to_numpy() <= upper)))
    return prototype


def seasonal_predict(model: Calendar24hModel, timestamps: pd.DatetimeIndex) -> np.ndarray:
    # tz-aware timestamps are correctly interpreted in the room's local time.
    if timestamps.tz is not None:
        timestamps = timestamps.tz_convert(model.timezone)
    return np.asarray([model.patterns[t.dayofweek][t.hour * 12 + t.minute // INTERVAL_MIN]
                       for t in timestamps], dtype=float)


def _recent_live_reading(series: pd.Series | None, clock: pd.Timestamp, timezone: str) -> bool:
    if series is None or series.dropna().empty:
        return False
    last = series.last_valid_index()
    if last.tzinfo is None:
        last = last.tz_localize(timezone, ambiguous='raise', nonexistent='raise')
    else:
        last = last.tz_convert(timezone)
    age = (clock - last).total_seconds() / 60
    return 0 <= age <= 10


def forecast_clock_24h(calendar_model: Calendar24hModel, *,
                       at: str | pd.Timestamp | None = None,
                       observations: pd.Series | None = None,
                       short_bundle=None,
                       allow_live: bool = True) -> pd.DataFrame:
    """Return 288 future 5-min projections from the *actual room-local clock*.

    If there are no recent readings, calendar predictions are the *assumed*
    current/future occupants. They are not fabricated observed people.
    """
    clock = current_local_time(calendar_model.timezone, at)
    origin = clock.floor(f'{INTERVAL_MIN}min')
    targets = pd.date_range(origin + pd.Timedelta(minutes=INTERVAL_MIN),
                            periods=24 * 60 // INTERVAL_MIN,
                            freq=f'{INTERVAL_MIN}min')
    baseline = seasonal_predict(calendar_model, targets)
    forecast = baseline.copy()
    short_lookup = {}
    live_active = False
    last_live = None
    if allow_live and short_bundle is not None and observations is not None and \
            _recent_live_reading(observations, clock, calendar_model.timezone):
        try:
            short = forecast_from_series(observations, short_bundle)
            for row in short.itertuples(index=False):
                time = pd.Timestamp(row.target_time)
                if time.tzinfo is None:
                    time = time.tz_localize(calendar_model.timezone)
                else:
                    time = time.tz_convert(calendar_model.timezone)
                short_lookup[time] = (float(row.expected_people), float(row.lower_80),
                                      float(row.upper_80), str(row.model_name))
            last_live = observations.last_valid_index()
            if last_live.tzinfo is None:
                last_live = last_live.tz_localize(calendar_model.timezone)
            last_live = last_live.tz_convert(calendar_model.timezone)
            live_active = bool(short_lookup)
        except (ValueError, KeyError):
            # Incomplete lag history: use a calendar-only forecast, never a stale observation.
            live_active = False

    # Blend the end of the short-term model into the long-range calendar expectation.
    correction_60 = 0.0
    if live_active:
        t60 = last_live + pd.Timedelta(minutes=60)
        if t60 in short_lookup:
            cal_at_60 = seasonal_predict(calendar_model, pd.DatetimeIndex([t60]))[0]
            correction_60 = short_lookup[t60][0] - cal_at_60

    rows = []
    for j, target in enumerate(targets):
        pred = float(baseline[j])
        radius = calendar_model.radius_80
        strategy = 'historical_time_of_week'
        if live_active:
            if target in short_lookup:
                val, lo, hi, strategy = short_lookup[target]
                pred = val
                radius = max(radius, (hi - lo) / 2)
                strategy = f'short_term_{strategy}'
            elif target > last_live + pd.Timedelta(minutes=60):
                minutes_after = (target - last_live).total_seconds() / 60 - 60
                pred += correction_60 * np.exp(-minutes_after / 90.0)
                strategy = 'decay_to_historical_pattern'
        pred = float(np.clip(pred, 0, calendar_model.capacity))
        rows.append({
            'room_id': calendar_model.room_id,
            'issued_at': clock.isoformat(),
            'origin_time': origin.isoformat(),
            'target_time': target.isoformat(),
            'horizon_min': (j + 1) * INTERVAL_MIN,
            'model_name': strategy,
            'expected_people': round(pred, 2),
            'lower_80': round(max(0, pred - radius), 2),
            'upper_80': round(min(calendar_model.capacity, pred + radius), 2),
            'assumed_people_for_simulation': int(round(pred)),
            'observed_people': np.nan,
            'validation_status': 'awaiting_real_observation',
            'observation_mode': 'recent_live_anchor' if live_active else 'no_live_calendar_assumption',
            'uncertainty_note': 'indicative_calendar_calibration_not_24h_guarantee',
        })
    return pd.DataFrame(rows)


def append_24h_log(forecast: pd.DataFrame, path: str | Path) -> int:
    """Log the issued predictions so that later observations can genuinely validate them."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    old = pd.read_csv(path) if path.exists() else None
    both = pd.concat([old, forecast], ignore_index=True) if old is not None else forecast.copy()
    key = ['room_id', 'origin_time', 'target_time']
    both = both.drop_duplicates(subset=key, keep='first').sort_values(key)
    added = len(both) - (len(old) if old is not None else 0)
    both.to_csv(path, index=False)
    return added


def evaluate_24h_log(log_path: str | Path, observed_path: str | Path,
                     room_id: str, output_path: str | Path,
                     *, timezone: str = 'Europe/London') -> pd.DataFrame:
    """Only actual subsequently supplied readings count as validation.

    Never substitute model predictions or historical synthetic samples for
    observations. Missing values remain unscored; local timezone is explicit.
    """
    path = Path(log_path)
    if not path.exists():
        raise FileNotFoundError(f'No issued 24-hour forecasts: {path}')
    log = pd.read_csv(path)
    log = log.loc[log['room_id'].astype(str) == str(room_id)].copy()
    observations = read_series(observed_path, room_id, fill_short_gaps=False)
    index = observations.index
    if index.tz is None:
        # Typical Arduino timestamps are local wall-clock time; for DST-proof
        # deployments, include timezone offsets in sensor timestamps.
        time_target = pd.to_datetime(log['target_time'], utc=True).dt.tz_convert(timezone).dt.tz_localize(None)
        time_origin = pd.to_datetime(log['origin_time'], utc=True).dt.tz_convert(timezone).dt.tz_localize(None)
    else:
        time_target = pd.to_datetime(log['target_time'], utc=True).dt.tz_convert(index.tz)
        time_origin = pd.to_datetime(log['origin_time'], utc=True).dt.tz_convert(index.tz)
    log['target_local'] = time_target
    log['origin_local'] = time_origin
    log['actual_people'] = time_target.map(observations)
    matured = log.loc[(time_target > time_origin) & (time_target <= index.max())]
    scored = matured.dropna(subset=['actual_people']).copy()
    scored['error'] = scored['actual_people'] - scored['expected_people']
    scored['abs_error'] = scored['error'].abs()
    scored['covered_80'] = ((scored['actual_people'] >= scored['lower_80']) &
                            (scored['actual_people'] <= scored['upper_80']))
    scored['validation_status'] = 'validated_against_supplied_observation'
    scored = scored.sort_values(['target_local', 'horizon_min'])
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    scored.to_csv(output_path, index=False)
    return scored
