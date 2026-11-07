"""Leakage-aware, multi-horizon occupancy research and forecasting.

Four ML candidates: Ridge / gradient boosting x level / delta target.
Two benchmarks: persistence and calendar-seasonal adjustment.
Chronological fit -> model selection -> conformal calibration -> blind test.
No features or labels are obtained from after the forecast origin.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import warnings

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import mean_absolute_error

INTERVAL_MIN = 5
HORIZONS = tuple(range(5, 61, 5))
ALPHA = 0.20
CANDIDATES = ('persistence', 'seasonal', 'ridge_level', 'ridge_delta',
              'boost_level', 'boost_delta')
FEATURE_COLUMNS = [
    'current', 'lag_1', 'lag_3', 'lag_6', 'lag_12', 'lag_24',
    'lag_288', 'lag_2016', 'mean_15', 'mean_60',
    'std_30', 'momentum_15', 'momentum_60', 'hour_sin', 'hour_cos',
    'week_sin', 'week_cos', 'weekend'
]


def read_series(path: str | Path, room_id: str, *, fill_short_gaps: bool = True) -> pd.Series:
    df = pd.read_csv(path, parse_dates=['timestamp'])
    needed = {'timestamp', 'room_id', 'count'}
    if missing := (needed - set(df.columns)):
        raise ValueError(f'CSV missing columns: {sorted(missing)}')
    df = df[df['room_id'].astype(str) == str(room_id)].copy()
    if df.empty:
        raise ValueError(f'No readings for room_id={room_id!r}')
    df['count'] = pd.to_numeric(df['count'], errors='coerce')
    if (df['count'].dropna() < 0).any():
        raise ValueError('Occupancy counts cannot be negative')
    series = (df.dropna(subset=['timestamp', 'count'])
              .set_index('timestamp')['count'].sort_index()
              .resample('5min').median())
    if fill_short_gaps:
        series = series.ffill(limit=2)
    # In live scoring, a missing observation is NOT an actual count.
    if fill_short_gaps and series.notna().sum() < 28 * 24 * 12:
        raise ValueError('Need at least about four weeks of regularly sampled readings '
                         'for the current weekly-lag and chronological-backtest settings.')
    return series.astype(float).rename('count')


def features(series: pd.Series) -> pd.DataFrame:
    s = series
    X = pd.DataFrame(index=s.index)
    X['current'] = s
    for lag in (1, 3, 6, 12, 24, 288, 2016):
        X[f'lag_{lag}'] = s.shift(lag)
    X['mean_15'] = s.rolling(3, min_periods=3).mean()
    X['mean_60'] = s.rolling(12, min_periods=12).mean()
    X['std_30'] = s.rolling(6, min_periods=6).std()
    X['momentum_15'] = s - s.shift(3)
    X['momentum_60'] = s - s.shift(12)
    minute = s.index.hour * 60 + s.index.minute
    dayphase = 2 * np.pi * minute / 1440
    weekphase = 2 * np.pi * (s.index.dayofweek * 1440 + minute) / (7 * 1440)
    X['hour_sin'] = np.sin(dayphase)
    X['hour_cos'] = np.cos(dayphase)
    X['week_sin'] = np.sin(weekphase)
    X['week_cos'] = np.cos(weekphase)
    X['weekend'] = (s.index.dayofweek >= 5).astype(float)
    return X[FEATURE_COLUMNS]


def make_model(name: str):
    if name.startswith('ridge_'):
        # Scaling is fitted on training observations only, inside the pipeline.
        return make_pipeline(StandardScaler(), Ridge(alpha=30.0))
    if name.startswith('boost_'):
        return HistGradientBoostingRegressor(
            loss='squared_error', learning_rate=0.08, max_iter=90,
            max_leaf_nodes=15, min_samples_leaf=35, l2_regularization=3.0,
            early_stopping=False, random_state=37)
    raise ValueError(f'Not an ML model: {name}')


def seasonal_profile(training: pd.Series) -> dict:
    observed = training.dropna()
    index = observed.index
    key = list(zip(index.dayofweek, index.hour * 12 + index.minute // 5))
    a = pd.DataFrame({'key': key, 'count': observed.to_numpy()})
    return {'weekly': a.groupby('key')['count'].median().to_dict(),
            'overall': float(observed.median())}


def _profile_at(profile: dict, timestamps: pd.DatetimeIndex) -> np.ndarray:
    return np.asarray([
        profile['weekly'].get((t.dayofweek, t.hour * 12 + t.minute // 5),
                              profile['overall']) for t in timestamps
    ], dtype=float)


def baseline_predict(profile: dict, origin_times: pd.DatetimeIndex,
                     current: np.ndarray, horizon_min: int, capacity: int) -> np.ndarray:
    future_times = origin_times + pd.Timedelta(minutes=horizon_min)
    decay = np.exp(-horizon_min / 35.)
    return np.clip(_profile_at(profile, future_times) +
                   decay * (current - _profile_at(profile, origin_times)), 0, capacity)


def conformal_radius(errors: np.ndarray, alpha: float = ALPHA) -> float:
    residuals = np.sort(np.abs(np.asarray(errors, dtype=float)))
    if not len(residuals):
        raise ValueError('No calibration residuals')
    k = min(len(residuals), int(np.ceil((len(residuals) + 1) * (1 - alpha))))
    return float(residuals[k - 1])


def split_masks(index: pd.DatetimeIndex, train_end: pd.Timestamp,
                select_end: pd.Timestamp, calibration_end: pd.Timestamp,
                test_end: pd.Timestamp, horizon_min: int) -> dict[str, np.ndarray]:
    """The entire target timestamp must fall INSIDE its own chronological split.

    Gaps of up to one horizon arise naturally around split boundaries, preventing
    knowledge of future targets from influencing earlier fitting decisions.
    """
    h = pd.Timedelta(minutes=horizon_min)
    return {
        'train': index + h <= train_end,
        'selection': (index > train_end) & (index + h <= select_end),
        'calibration': (index > select_end) & (index + h <= calibration_end),
        'test': (index > calibration_end) & (index + h <= test_end),
    }


def _masks(index, train_end, cal_end, test_end, horizon):
    """Legacy three-way mask API retained for compatibility with existing tests."""
    m = split_masks(index, train_end, train_end, cal_end, test_end, horizon)
    return m['train'], m['calibration'], m['test']


def _candidate_predictions(X: pd.DataFrame, current: np.ndarray,
                           origin: pd.DatetimeIndex, models: dict,
                           profile: dict, horizon: int, capacity: int) -> dict[str, np.ndarray]:
    result = {
        'persistence': np.clip(current, 0, capacity),
        'seasonal': baseline_predict(profile, origin, current, horizon, capacity),
    }
    for name, model in models.items():
        value = model.predict(X)
        if name.endswith('_delta'):
            value = current + value
        result[name] = np.clip(value, 0, capacity)
    return result


def fit_horizon(s: pd.Series, X: pd.DataFrame, horizon: int,
                train_end: pd.Timestamp, select_end: pd.Timestamp,
                calibration_end: pd.Timestamp, test_end: pd.Timestamp,
                capacity: int) -> tuple[dict, pd.DataFrame, pd.DataFrame]:
    y = s.shift(-horizon // INTERVAL_MIN)
    valid = X.notna().all(axis=1) & y.notna()
    masks = split_masks(X.index, train_end, select_end, calibration_end, test_end, horizon)
    masks = {key: (mask & valid) for key, mask in masks.items()}
    if min(int(mask.sum()) for mask in masks.values()) < 40:
        raise ValueError('Insufficient train/selection/calibration/test observations')
    current = X['current']
    ml_models = {}
    for name in CANDIDATES:
        if name in ('persistence', 'seasonal'):
            continue
        target = (y - current) if name.endswith('_delta') else y
        model = make_model(name)
        model.fit(X.loc[masks['train']], target.loc[masks['train']])
        ml_models[name] = model
    profile = seasonal_profile(s.loc[:train_end])
    predictions = {}
    for split in ('selection', 'calibration', 'test'):
        mask = masks[split]
        data = X.loc[mask]
        predictions[split] = (
            y.loc[mask].to_numpy(),
            _candidate_predictions(data, data['current'].to_numpy(),
                                   data.index, ml_models, profile, horizon, capacity),
            data.index,
        )
    sel_y, sel_preds, _ = predictions['selection']
    selection_errors = {name: float(mean_absolute_error(sel_y, sel_preds[name]))
                        for name in CANDIDATES}
    # Selection uses ONLY selection-period errors, not calibration/test errors.
    # Ties prefer simpler candidates in the order of CANDIDATES.
    selected = min(CANDIDATES, key=lambda name: (selection_errors[name],
                                                CANDIDATES.index(name)))
    cal_y, cal_preds, _ = predictions['calibration']
    radius = conformal_radius(cal_y - cal_preds[selected])
    test_y, test_preds, test_origins = predictions['test']
    chosen = test_preds[selected]
    lower = np.maximum(0, chosen - radius)
    upper = np.minimum(capacity, chosen + radius)
    rows = []
    for name in CANDIDATES:
        yhat = test_preds[name]
        rows.append({'horizon_min': horizon, 'model': name,
                     'selected': name == selected,
                     'n_test': len(test_y),
                     'mae': float(mean_absolute_error(test_y, yhat)),
                     'rmse': float(np.sqrt(np.mean((test_y - yhat) ** 2))),
                     'selection_mae': selection_errors[name],
                     'interval_coverage': float(np.mean((test_y >= lower) & (test_y <= upper))) if name == selected else np.nan,
                     'interval_mean_width': float(np.mean(upper - lower)) if name == selected else np.nan})
    detail = pd.DataFrame({
        'origin': test_origins,
        'target_time': test_origins + pd.Timedelta(minutes=horizon),
        'horizon_min': horizon,
        'actual': test_y,
        'predicted': chosen,
        'selected_model': selected,
        'lower_80': lower,
        'upper_80': upper,
        **{f'pred_{name}': test_preds[name] for name in CANDIDATES},
    })
    artifact = {'models': ml_models, 'profile': profile,
                'selected_model': selected, 'radius': radius,
                'selection_errors': selection_errors}
    return artifact, pd.DataFrame(rows), detail


@dataclass
class ForecastBundle:
    room_id: str
    capacity: int
    horizons: dict
    training_end: str
    latest_data_at_training: str
    interval_min: int = INTERVAL_MIN


def train_backtest_forecast(series: pd.Series, room_id: str, capacity: int,
                            output_dir: str | Path) -> dict:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    if capacity < series.max():
        raise ValueError(f'Capacity {capacity} is smaller than observed count {series.max():.0f}')
    X = features(series)
    idx = X.index
    if len(idx) < 28 * 24 * 12:
        warnings.warn('Less than 4 weeks: weekly patterns may not generalise', stacklevel=2)
    # Expanding, chronological, *disjoint test* windows.
    # Training / model selection / interval calibration / blind test.
    periods = [(0.50, 0.60, 0.70, 0.80),
               (0.65, 0.75, 0.85, 0.95)]
    metric_parts, backtest_parts = [], []
    for fold, fractions in enumerate(periods, start=1):
        bounds = [idx[int((len(idx) - 1) * f)] for f in fractions]
        print(f'Walk-forward fold {fold}/2: train <= {bounds[0]}, '
              f'select <= {bounds[1]}, calibrate <= {bounds[2]}, test <= {bounds[3]}')
        for horizon in HORIZONS:
            _, m, details = fit_horizon(series, X, horizon, *bounds, capacity)
            m['fold'] = fold
            details['fold'] = fold
            metric_parts.append(m)
            backtest_parts.append(details)
    metrics = pd.concat(metric_parts, ignore_index=True)
    backtest = pd.concat(backtest_parts, ignore_index=True)
    metrics.to_csv(output_dir / 'backtest_metrics.csv', index=False)
    backtest.to_csv(output_dir / 'backtest_predictions.csv', index=False)

    # Final models use 80% train, 10% selection, 10% calibration.
    # There is no held-out test in this refit; its TEST estimates come from above.
    bounds = [idx[int((len(idx) - 1) * f)] for f in (0.80, 0.90, 1.0)]
    artifacts = {}
    selection_rows = []
    for horizon in HORIZONS:
        artifacts[horizon] = fit_final_horizon(series, X, horizon, *bounds, capacity)
        selection_rows.append({'horizon_min': horizon,
                               'selected_model': artifacts[horizon]['selected_model'],
                               'conformal_radius': artifacts[horizon]['radius'],
                               'selection_mae': artifacts[horizon]['selection_errors'][artifacts[horizon]['selected_model']]})
    pd.DataFrame(selection_rows).to_csv(output_dir / 'model_selection.csv', index=False)
    bundle = ForecastBundle(room_id, capacity, artifacts, str(bounds[0]), str(idx[-1]))
    joblib.dump(bundle, output_dir / 'forecast_bundle.joblib')
    try:
        future = forecast_from_series(series, bundle)
    except ValueError as e:
        # e.g. the last reading follows a sensor gap, so its day/week lags are missing
        warnings.warn(f'No forecast from the latest reading: {e}', stacklevel=2)
        future = pd.DataFrame(columns=['room_id', 'origin_time', 'target_time', 'horizon_min', 'model_name',
                                       'expected_people', 'lower_80', 'upper_80'])
    future.to_csv(output_dir / 'forecast.csv', index=False)
    return {'metrics': metrics, 'backtest': backtest, 'forecast': future,
            'bundle': bundle}


def fit_final_horizon(s: pd.Series, X: pd.DataFrame, horizon: int,
                      train_end: pd.Timestamp, select_end: pd.Timestamp,
                      calibration_end: pd.Timestamp, capacity: int) -> dict:
    y = s.shift(-horizon // INTERVAL_MIN)
    valid = X.notna().all(axis=1) & y.notna()
    m = split_masks(X.index, train_end, select_end, calibration_end,
                    calibration_end, horizon)
    masks = {k: (m[k] & valid) for k in ('train', 'selection', 'calibration')}
    if min(int(mask.sum()) for mask in masks.values()) < 40:
        raise ValueError('Insufficient data for final training and calibration')
    current = X['current']
    trained = {}
    for name in CANDIDATES:
        if name in ('persistence', 'seasonal'):
            continue
        model = make_model(name)
        target = y-current if name.endswith('_delta') else y
        model.fit(X.loc[masks['train']], target.loc[masks['train']])
        trained[name] = model
    profile = seasonal_profile(s.loc[:train_end])
    predictions = {}
    for split in ('selection', 'calibration'):
        data = X.loc[masks[split]]
        preds = _candidate_predictions(data, data['current'].to_numpy(),
                                       data.index, trained, profile, horizon, capacity)
        predictions[split] = (y.loc[masks[split]].to_numpy(), preds)
    sel_y, sel_preds = predictions['selection']
    selection_errors = {name: float(mean_absolute_error(sel_y, sel_preds[name]))
                        for name in CANDIDATES}
    selected = min(CANDIDATES, key=lambda name: (selection_errors[name],
                                                CANDIDATES.index(name)))
    cal_y, cal_preds = predictions['calibration']
    radius = conformal_radius(cal_y - cal_preds[selected])
    return {'models': trained, 'profile': profile,
            'selected_model': selected, 'radius': radius,
            'selection_errors': selection_errors}


def forecast_from_series(series: pd.Series, bundle: ForecastBundle) -> pd.DataFrame:
    X = features(series)
    last_valid = series.last_valid_index()
    if last_valid is None:
        raise ValueError('No valid current observation')
    latest = X.loc[[last_valid]]
    if latest.isna().any(axis=None):
        raise ValueError('Latest data missing required lagged features')
    records = []
    for horizon in HORIZONS:
        item = bundle.horizons[horizon]
        name = item['selected_model']
        predicted = _candidate_predictions(latest, latest['current'].to_numpy(),
                                           latest.index, item['models'],
                                           item['profile'], horizon, bundle.capacity)[name][0]
        radius = item['radius']
        records.append({'room_id': bundle.room_id,
                        'origin_time': last_valid,
                        'target_time': last_valid + pd.Timedelta(minutes=horizon),
                        'horizon_min': horizon,
                        'model_name': name,
                        'expected_people': round(float(predicted), 2),
                        'lower_80': round(max(0, predicted-radius), 2),
                        'upper_80': round(min(bundle.capacity, predicted+radius), 2)})
    return pd.DataFrame.from_records(records)


def append_forecast_log(forecast: pd.DataFrame, log_path: str | Path) -> int:
    """Store predictions when issued, not when their outcomes become available."""
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    new = forecast.copy()
    if path.exists():
        old = pd.read_csv(path)
        merged = pd.concat([old, new], ignore_index=True)
    else:
        merged = new
    # CSV reload converts timestamps to strings. Normalise both old and new
    # timestamps before deduplicating so a repeated forecast is not double-counted.
    for column in ('origin_time', 'target_time'):
        merged[column] = pd.to_datetime(merged[column])
    key = ['room_id', 'origin_time', 'horizon_min', 'model_name']
    merged = merged.drop_duplicates(subset=key, keep='first')
    added = len(merged) - (len(old) if path.exists() else 0)
    merged.to_csv(path, index=False)
    return max(0, added)


def evaluate_forecast_log(log_path: str | Path, observations_path: str | Path,
                          room_id: str, output_path: str | Path) -> pd.DataFrame:
    """Only score logged predictions after a *real observed* target sample exists."""
    log_path = Path(log_path)
    if not log_path.exists():
        raise FileNotFoundError(f'No forecast log at {log_path}. Run forecast first.')
    log = pd.read_csv(log_path, parse_dates=['origin_time', 'target_time'])
    log = log.loc[log['room_id'].astype(str) == str(room_id)].copy()
    observed = read_series(observations_path, room_id, fill_short_gaps=False)
    # Use exact 5-minute bucket timestamps and don't score from filled gaps.
    log = log.loc[(log['target_time'] > log['origin_time']) &
                  (log['origin_time'] <= observed.index.max())].copy()
    log['actual'] = log['target_time'].map(observed)
    evaluated = log.dropna(subset=['actual']).copy()
    evaluated['error'] = evaluated['actual'] - evaluated['expected_people']
    evaluated['abs_error'] = evaluated['error'].abs()
    evaluated['covered_80'] = ((evaluated['actual'] >= evaluated['lower_80']) &
                               (evaluated['actual'] <= evaluated['upper_80']))
    evaluated = evaluated.sort_values(['target_time', 'horizon_min'])
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    evaluated.to_csv(output_path, index=False)
    return evaluated
