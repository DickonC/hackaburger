"""Import the Aalborg office dataset into the backend and train the forecasters.

    docker exec hackaburger-ingest python -m app.dataset

- fills the forecasting tables (dataset_rooms, dataset_occupancy; see forecast.py) with
  rooms aalborg-a..f and their 2023 history: people (cameras), measured CO2, windows
- calibrates each room's ventilation from its measured CO2 (see calibrate_co2)
- trains the short-term models (occupancy_core) into MODELS_DIR/<room>/

The live rooms and board data are not touched. Safe to re-run: the dataset is replaced.
"""

import json
import math
import os
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from app.core import db
from app.forecast import MODELS_DIR, migrate

sys.path.insert(0, os.environ.get("FORECASTER_DIR", "/app/occupancy_forecaster"))
import aalborg  # noqa: E402
from occupancy_core import read_series, train_backtest_forecast  # noqa: E402

RAW_DIR = Path(os.environ.get("DATASET_DIR", "/data/datasets"))
CO2_PER_PERSON_LPH = 16.0  # L/h, as the live rooms' default
K_FLOW = 350.0  # m3/h per m2 of open window, as the live rooms' default
WINDOW_AREA_M2 = 1.0  # not in the dataset


def calibrate_co2(wide: pd.DataFrame, letter: str) -> dict:
    """Fit outdoor level, occupied airflow and effective volume from measured CO2.

    The offices have demand-controlled ventilation, so a constant-airflow model is fitted to
    occupied, windows-closed periods: the steady-state rise per person gives the airflow Q,
    then the 5-minute dynamics give the effective volume V with Q fixed.
    """
    n = wide[f"Room{letter}:People__amount"]
    c = wide[f"Room{letter}:Sensor__CO2"]
    w = wide[f"Room{letter}:Window__opened_closed"]
    outdoor = float(c.quantile(0.02))
    hourly = pd.DataFrame({"n": n, "c": c, "w": w}).resample("1h").mean()
    steady = hourly[(hourly["w"] == 0) & (hourly["n"] >= 1) & (hourly["n"].diff().abs() < 0.3) & hourly["c"].notna()]
    rise = float(np.linalg.lstsq(steady[["n"]].to_numpy(), (steady["c"] - outdoor).to_numpy(), rcond=None)[0][0])
    q = CO2_PER_PERSON_LPH * 1000 / rise  # m3/h

    dc = (c.shift(-1) - c) * 12  # ppm/h over the next 5 minutes
    x = CO2_PER_PERSON_LPH * 1000 * n - q * (c - outdoor)
    ok = dc.notna() & x.notna() & (w == 0) & (dc.abs() < 3000)
    inv_v = float((x[ok] * dc[ok]).sum() / (x[ok] ** 2).sum())
    volume = 1 / inv_v if inv_v > 0 else float("nan")
    if not 20 <= volume <= 400:
        volume = 60.0  # implausible fit: fall back to a typical small office
    return {"outdoor_ppm": round(outdoor), "airflow_m3h": round(q, 1), "volume_m3": round(volume, 1),
            "infiltration_ach": round(q / volume, 3), "steady_hours": len(steady)}


def validate_co2(wide: pd.DataFrame, letter: str, cal: dict) -> float:
    """MAE (ppm) of the calibrated model run freely 06:00-20:00 on weekdays from the measured 06:00 value."""
    n = wide[f"Room{letter}:People__amount"]
    c = wide[f"Room{letter}:Sensor__CO2"]
    w = wide[f"Room{letter}:Window__opened_closed"]
    errors = []
    for day, idx in n.groupby(n.index.date).groups.items():
        day_n, day_c, day_w = n.loc[idx], c.loc[idx], w.loc[idx]
        hours = day_n.index.hour
        sel = (hours >= 6) & (hours < 20)
        if day_n.index[0].day_of_week >= 5 or day_n[sel].isna().any() or day_c[sel].isna().any() or day_n[sel].sum() == 0:
            continue
        sim = float(day_c[sel].iloc[0])
        for people, measured, window in zip(day_n[sel], day_c[sel], day_w[sel]):
            errors.append(abs(sim - measured))
            q = cal["airflow_m3h"] + (K_FLOW * WINDOW_AREA_M2 if window == 1 else 0)
            decay = math.exp(-(q / cal["volume_m3"]) / 12)
            sim = cal["outdoor_ppm"] + people * CO2_PER_PERSON_LPH / q * 1000 * (1 - decay) + (sim - cal["outdoor_ppm"]) * decay
    return round(float(np.mean(errors)), 1) if errors else float("nan")


def replay_suggestions(n: pd.Series, limit: int = 12) -> list[str]:
    """Weekday 08:00-14:00 moments where a replay can use the short-term models and be checked.

    The count must be recorded at the moment, 5 minutes, 1 day and 1 week before it, and for
    at least 90% of the following 8 hours. Spread over the year, busiest first.
    """
    s = n.groupby(n.index).first()  # DST fall-back duplicates
    s = s.asfreq("5min")
    ok = []
    for t in s.index[(s.index.day_of_week < 5) & (s.index.minute == 0) & (s.index.hour >= 8) & (s.index.hour <= 14)]:
        if any(pd.isna(s.get(t - pd.Timedelta(d))) for d in ("0min", "5min", "1D", "7D")):
            continue
        ahead = s.loc[t:t + pd.Timedelta("8h")]
        if ahead.notna().mean() >= 0.9:
            ok.append((t, float(ahead.sum())))
    by_week = {}
    for t, busy in ok:  # at most one per ISO week: the busiest
        key = t.isocalendar()[:2]
        if key not in by_week or busy > by_week[key][1]:
            by_week[key] = (t, busy)
    best = sorted(by_week.values(), key=lambda x: -x[1])[:limit]
    return [t.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ") for t, _ in sorted(best)]


def import_rooms(conn: sqlite3.Connection, wide: pd.DataFrame) -> dict:
    report = {}
    conn.execute("DELETE FROM dataset_occupancy WHERE room_id LIKE 'aalborg-%'")
    for letter in aalborg.ROOMS:
        rid = aalborg.room_id(letter)
        cal = calibrate_co2(wide, letter)
        n = wide[f"Room{letter}:People__amount"]
        co2 = wide[f"Room{letter}:Sensor__CO2"]
        win = wide[f"Room{letter}:Window__opened_closed"]
        rows = [(rid, t.timestamp(), None if math.isnan(p) else p, None if math.isnan(c) else c,
                 None if math.isnan(w) else int(w))
                for t, p, c, w in zip(wide.index, n, co2, win) if not (math.isnan(p) and math.isnan(c))]
        conn.executemany("INSERT OR REPLACE INTO dataset_occupancy VALUES (?, ?, ?, ?, ?)", rows)
        report[rid] = {
            "name": f"Aalborg office {letter}",
            "capacity": int(n.max()),
            "timezone": aalborg.TIMEZONE,
            "volume_m3": cal["volume_m3"],
            "airflow_m3h": cal["airflow_m3h"],
            "outdoor_ppm": float(cal["outdoor_ppm"]),
            "co2_gen_lph": CO2_PER_PERSON_LPH,
            "k_flow": K_FLOW,
            "window_area_m2": WINDOW_AREA_M2,
            "calibration": {"steady_hours": cal["steady_hours"], "co2_model_mae_ppm": validate_co2(wide, letter, cal),
                            "volume_assumed": cal["volume_m3"] == 60.0},
            "replay_suggestions": replay_suggestions(n),
            "source": f"doi:{aalborg.DOI} (CC BY 4.0)",
        }
    return report


def train_models(csv_path: Path, report: dict):
    for rid, info in report.items():
        series = read_series(csv_path, rid)
        result = train_backtest_forecast(series, rid, info["capacity"], MODELS_DIR / rid)
        metrics = result["metrics"]
        backtest = {}
        for h in (15, 30, 60):
            m = metrics[metrics["horizon_min"] == h]
            backtest[f"{h}min"] = {
                "selected": m[m["selected"]]["model"].mode()[0],
                "mae": round(float(m[m["selected"]]["mae"].mean()), 3),
                "persistence_mae": round(float(m[m["model"] == "persistence"]["mae"].mean()), 3),
                "boosted_trees_mae": round(float(m[m["model"].str.startswith("boost")].groupby("model")["mae"].mean().min()), 3),
            }
        info["backtest"] = backtest


def main():
    raw = aalborg.download(RAW_DIR / "raw")
    wide = aalborg.load_wide(raw)
    csv_path = RAW_DIR / "aalborg_occupancy.csv"
    aalborg.to_long(wide).to_csv(csv_path, index=False)
    with db() as conn:
        migrate(conn)
        report = import_rooms(conn, wide)
    train_models(csv_path, report)
    with db() as conn:
        for rid, room in report.items():
            conn.execute("INSERT OR REPLACE INTO dataset_rooms (id, room) VALUES (?, ?)", (rid, json.dumps(room)))
    print(f"Aalborg office dataset (doi:{aalborg.DOI}, CC BY 4.0) imported into dataset_rooms/dataset_occupancy.")
    for rid, r in report.items():
        print(f"  {rid}: capacity {r['capacity']}, airflow {r['airflow_m3h']} m3/h, V {r['volume_m3']} m3"
              f"{' (assumed)' if r['calibration']['volume_assumed'] else ''}, outdoor {r['outdoor_ppm']} ppm, "
              f"CO2 model MAE {r['calibration']['co2_model_mae_ppm']} ppm, backtest {r['backtest']}")


if __name__ == "__main__":
    main()
