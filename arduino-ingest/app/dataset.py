"""Import the Aalborg office dataset into the backend and train the forecasters.

    docker exec hackaburger-ingest python -m app.dataset

- creates/updates rooms aalborg-a..f with their 2023 history (people, measured CO2, windows)
- calibrates each room's ventilation from its measured CO2 (see calibrate_co2)
- trains the short-term models (occupancy_core) into MODELS_DIR/<room>/
- removes the old synthetic history and points the preset rooms at a dataset room
  (model_from), with ventilation scaled from the dataset's airflow per person

Safe to re-run: the dataset rooms' history is replaced, not appended.
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
from app.rooms import MODELS_DIR, ROOM_DEFAULTS, migrate

sys.path.insert(0, os.environ.get("FORECASTER_DIR", "/app/occupancy_forecaster"))
import aalborg  # noqa: E402
from occupancy_core import read_series, train_backtest_forecast  # noqa: E402

RAW_DIR = Path(os.environ.get("DATASET_DIR", "/data/datasets"))
SOURCE = "aalborg-2023"
CO2_PER_PERSON_LPH = ROOM_DEFAULTS["co2_gen_lph"]
# Preset rooms borrow a dataset room's patterns (scaled by capacity); capacity = the frontend's booked count
PRESET_LINKS = {"appleton": ("aalborg-a", 12), "forum": ("aalborg-d", 20), "personal": ("aalborg-c", 1)}


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
            q = cal["airflow_m3h"] + (ROOM_DEFAULTS["k_flow"] * 1.0 if window == 1 else 0)
            decay = math.exp(-(q / cal["volume_m3"]) / 12)
            sim = cal["outdoor_ppm"] + people * CO2_PER_PERSON_LPH / q * 1000 * (1 - decay) + (sim - cal["outdoor_ppm"]) * decay
    return round(float(np.mean(errors)), 1) if errors else float("nan")


def import_rooms(conn: sqlite3.Connection, wide: pd.DataFrame) -> dict:
    report = {}
    for letter in aalborg.ROOMS:
        rid = aalborg.room_id(letter)
        cal = calibrate_co2(wide, letter)
        n = wide[f"Room{letter}:People__amount"]
        room = {**ROOM_DEFAULTS, "name": f"Aalborg office {letter} (dataset)", "volume_m3": cal["volume_m3"],
                "capacity": int(n.max()), "timezone": aalborg.TIMEZONE, "outdoor_ppm": float(cal["outdoor_ppm"]),
                "infiltration_ach": cal["infiltration_ach"], "windows": [{"name": "Window", "area_m2": 1.0}]}
        conn.execute("INSERT OR REPLACE INTO rooms (id, room) VALUES (?, ?)", (rid, json.dumps(room)))
        conn.execute("DELETE FROM occupancy_history WHERE room_id = ?", (rid,))
        rows = pd.DataFrame({"ts": wide.index.map(lambda t: t.timestamp()), "people": n,
                             "co2": wide[f"Room{letter}:Sensor__CO2"],
                             "window": wide[f"Room{letter}:Window__opened_closed"]}).dropna(subset=["people"])
        conn.executemany(
            "INSERT OR REPLACE INTO occupancy_history (room_id, ts, people, source, co2_ppm, window_open) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(rid, t, p, SOURCE, None if math.isnan(co2) else co2, None if math.isnan(win) else int(win))
             for t, p, co2, win in rows.itertuples(index=False)])
        report[rid] = {**cal, "capacity": room["capacity"], "history_points": len(rows),
                       "co2_model_mae_ppm": validate_co2(wide, letter, cal)}
    return report


def link_presets(conn: sqlite3.Connection, report: dict):
    """Drop synthetic history; give the preset rooms dataset patterns and dataset-based ventilation."""
    conn.execute("DELETE FROM occupancy_history WHERE source = 'synthetic'")
    per_person = float(np.median([r["airflow_m3h"] / r["capacity"] for r in report.values()]))
    for rid, (source, capacity) in PRESET_LINKS.items():
        row = conn.execute("SELECT room FROM rooms WHERE id = ?", (rid,)).fetchone()
        if not row:
            continue
        room = {**ROOM_DEFAULTS, **json.loads(row[0])}
        room.update(model_from=source, capacity=capacity,
                    infiltration_ach=round(per_person * capacity / room["volume_m3"], 3))
        conn.execute("UPDATE rooms SET room = ? WHERE id = ?", (json.dumps(room), rid))
    return per_person


def train_models(csv_path: Path, report: dict):
    for rid, info in report.items():
        out = MODELS_DIR / rid
        series = read_series(csv_path, rid)
        result = train_backtest_forecast(series, rid, info["capacity"], out)
        chosen = result["metrics"][result["metrics"]["selected"]]
        info["selected_models"] = (chosen.groupby("horizon_min")["model"].agg(lambda m: m.mode()[0])
                                   .loc[[15, 30, 60]].to_dict())


def main():
    raw = aalborg.download(RAW_DIR / "raw")
    wide = aalborg.load_wide(raw)
    csv_path = RAW_DIR / "aalborg_occupancy.csv"
    aalborg.to_long(wide).to_csv(csv_path, index=False)
    with db() as conn:
        migrate(conn)
        report = import_rooms(conn, wide)
        per_person = link_presets(conn, report)
    train_models(csv_path, report)
    (MODELS_DIR / "import_report.json").write_text(json.dumps(report, indent=2, default=str))
    print(f"Aalborg office dataset (doi:{aalborg.DOI}, CC BY 4.0) imported.")
    print(f"Preset rooms: ventilation {per_person:.1f} m3/h per person (dataset median); links {PRESET_LINKS}")
    for rid, info in report.items():
        print(f"  {rid}: {info['history_points']} points, capacity {info['capacity']}, V {info['volume_m3']} m3, "
              f"airflow {info['airflow_m3h']} m3/h (ACH {info['infiltration_ach']}), outdoor {info['outdoor_ppm']} ppm, "
              f"CO2 model MAE {info['co2_model_mae_ppm']} ppm, models {info.get('selected_models')}")


if __name__ == "__main__":
    main()
