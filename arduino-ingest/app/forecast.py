"""Forecasting on the Aalborg office dataset, separate from the live boards.

The dataset rooms and their history live in their own tables (dataset_rooms,
dataset_occupancy) and are loaded by dataset.py. Nothing here reads the boards' data:
the live side only shows that we can sense occupancy; with a few weeks of our own
readings the same models would be trained on those instead.

Forecasts use occupancy_forecaster:
- the weekday x time-of-day model (rolling_24h) for 24 hours from now;
- with ?at=<a 2023 time> ("replay"), the forecast is made from the data up to that
  moment, so the short-term models (occupancy_core: persistence / seasonal / ridge /
  gradient-boosted trees, chosen per horizon) run on the first hour, and each point
  comes with what actually happened (people and measured CO2).
CO2 uses the same mass balance as the live rooms, with each room's calibrated airflow.
"""

import json
import math
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

import joblib
import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query

from app.core import db, iso, parse_time, require_key, time_range
from app.rooms import step_co2

sys.path.insert(0, os.environ.get("FORECASTER_DIR", "/app/occupancy_forecaster"))
from rolling_24h import fit_calendar_24h, forecast_clock_24h  # noqa: E402

MODELS_DIR = Path(os.environ.get("MODELS_DIR", "/data/models"))
STEP_S = 300
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
LAG_S = 7 * 86400 + 2 * 3600  # the short-term models need a week (+ a little) of history before `at`
WARMUP_S = 12 * 3600

router = APIRouter(prefix="/dataset", dependencies=[Depends(require_key)])
_calendars: dict[str, tuple[tuple, Any]] = {}  # room_id -> (history key, calendar model)
_bundles: dict[str, tuple[float, Any]] = {}  # room_id -> (file mtime, short-term ForecastBundle)


def migrate(conn: sqlite3.Connection):
    conn.execute("CREATE TABLE IF NOT EXISTS dataset_rooms (id TEXT PRIMARY KEY, room TEXT NOT NULL)")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS dataset_occupancy (
            room_id TEXT NOT NULL,
            ts REAL NOT NULL,
            people REAL,
            co2_ppm REAL,
            window_open INTEGER,
            PRIMARY KEY (room_id, ts)
        )"""
    )


def get_room(conn: sqlite3.Connection, room_id: str) -> dict:
    row = conn.execute("SELECT room FROM dataset_rooms WHERE id = ?", (room_id,)).fetchone()
    if not row:
        raise HTTPException(404, f"no dataset room {room_id!r}")
    return json.loads(row["room"])


def history_range(conn: sqlite3.Connection, room_id: str) -> tuple[int, float | None, float | None]:
    return tuple(conn.execute(
        "SELECT COUNT(people), MIN(ts), MAX(ts) FROM dataset_occupancy WHERE room_id = ? AND people IS NOT NULL",
        (room_id,)).fetchone())


def room_out(conn: sqlite3.Connection, room_id: str, room: dict) -> dict:
    n, first, last = history_range(conn, room_id)
    return {"id": room_id, **room,
            "history": {"points": n, "from": iso(first) if first else None, "to": iso(last) if last else None,
                        "replay_from": iso(first + LAG_S) if first else None},
            "short_term_model": (MODELS_DIR / room_id / "forecast_bundle.joblib").exists()}


def people_series(conn: sqlite3.Connection, room_id: str, tz: str, until: float | None = None) -> pd.Series:
    """5-minute people counts on the room's local wall clock (missing slots stay NaN, short gaps filled)."""
    sql, args = "SELECT ts, people FROM dataset_occupancy WHERE room_id = ? AND people IS NOT NULL", [room_id]
    if until is not None:
        sql, args = sql + " AND ts <= ?", args + [until]
    rows = conn.execute(sql + " ORDER BY ts", args).fetchall()
    idx = pd.to_datetime([r["ts"] for r in rows], unit="s", utc=True).tz_convert(tz).tz_localize(None)
    s = pd.Series([r["people"] for r in rows], index=idx, dtype=float)
    return s.resample("5min").median().ffill(limit=2)  # as occupancy_core.read_series


def calendar_model(conn: sqlite3.Connection, room_id: str, room: dict):
    key = history_range(conn, room_id)
    cached = _calendars.get(room_id)
    if cached and cached[0] == key:
        return cached[1]
    try:
        model = fit_calendar_24h(people_series(conn, room_id, room["timezone"]), room_id, room["capacity"],
                                 timezone=room["timezone"])
    except ValueError:
        model = None
    _calendars[room_id] = (key, model)
    return model


def load_bundle(room_id: str):
    """Short-term ForecastBundle trained by dataset.py (only files this server wrote are loaded)."""
    path = MODELS_DIR / room_id / "forecast_bundle.joblib"
    if not path.exists():
        return None
    mtime = path.stat().st_mtime
    cached = _bundles.get(room_id)
    if not cached or cached[0] != mtime:
        _bundles[room_id] = (mtime, joblib.load(path))
    return _bundles[room_id][1]


def pattern_at(model, epochs, tz: str) -> list[float]:
    local = pd.to_datetime(list(epochs), unit="s", utc=True).tz_convert(tz)
    return [float(model.patterns[t.day_of_week][t.hour * 12 + t.minute // 5]) for t in local]


def measured(conn: sqlite3.Connection, room_id: str, start: float, end: float) -> dict[float, Any]:
    """ts (5-minute slot) -> row with people, co2_ppm, window_open."""
    rows = conn.execute("SELECT ts, people, co2_ppm, window_open FROM dataset_occupancy "
                        "WHERE room_id = ? AND ts >= ? AND ts <= ?", (room_id, start, end)).fetchall()
    return {math.floor(r["ts"] / STEP_S) * STEP_S: r for r in rows}


def co2_room(room: dict) -> dict:
    """The fields step_co2 needs (same mass balance as the live rooms)."""
    return {"volume_m3": room["volume_m3"], "co2_gen_lph": room["co2_gen_lph"], "outdoor_ppm": room["outdoor_ppm"]}


@router.get("/rooms")
def list_rooms():
    with db() as conn:
        rows = conn.execute("SELECT id, room FROM dataset_rooms ORDER BY id").fetchall()
        return [room_out(conn, r["id"], json.loads(r["room"])) for r in rows]


@router.get("/rooms/{room_id}")
def read_room(room_id: str):
    with db() as conn:
        return room_out(conn, room_id, get_room(conn, room_id))


@router.get("/rooms/{room_id}/history")
def read_history(room_id: str, from_: str | None = Query(default=None, alias="from"), to: str | None = None):
    """Recorded people (cameras), measured CO2 and window state. Default: the last day of the data."""
    with db() as conn:
        get_room(conn, room_id)
        _, _, last = history_range(conn, room_id)
        start, end = time_range(from_, to, 86400, (last or time.time()) + 1)
        rows = conn.execute(
            "SELECT ts, people, co2_ppm, window_open FROM dataset_occupancy WHERE room_id = ? AND ts >= ? "
            "AND ts < ? ORDER BY ts LIMIT 50000", (room_id, start, end)).fetchall()
    return {"room_id": room_id, "points": [
        {"ts": iso(r["ts"]), "people": r["people"], "co2_ppm": r["co2_ppm"], "window_open": r["window_open"]}
        for r in rows]}


@router.get("/rooms/{room_id}/forecast")
def read_forecast(room_id: str, hours: int = Query(default=24, ge=1, le=24),
                  at: str | None = Query(default=None, description="replay: forecast from this time in the data"),
                  as_weekday: str | None = Query(default=None, description="mon..sun: use that day's pattern")):
    """People and CO2 every 5 minutes for `hours`, from now or (replay) from `at`."""
    with db() as conn:
        room = get_room(conn, room_id)
        model = calendar_model(conn, room_id, room)
        if model is None:
            raise HTTPException(409, "this room has less than four weeks of history")
        tz = room["timezone"]
        _, first, last = history_range(conn, room_id)
        replay = at is not None
        if replay:
            origin = parse_time(at)
            if origin is None or not first + LAG_S <= origin <= last:
                raise HTTPException(422, f"at must be an ISO time between {iso(first + LAG_S)} and {iso(last)}")
            if as_weekday:
                raise HTTPException(422, "as_weekday can't be combined with at")
        else:
            origin = time.time()
        shift_days = 0
        if as_weekday:
            wd = as_weekday.strip().lower()[:3]
            if wd not in WEEKDAYS:
                raise HTTPException(422, "as_weekday must be mon, tue, wed, thu, fri, sat or sun")
            shift_days = (pd.Timestamp(origin, unit="s", tz="UTC").tz_convert(tz).day_of_week - WEEKDAYS.index(wd)) % 7
        origin = math.floor(origin / STEP_S) * STEP_S if replay else origin

        # People: forecast_clock_24h reads a naive `at` as the model's (= the room's) local time
        clock = pd.Timestamp(origin - shift_days * 86400, unit="s", tz="UTC").tz_convert(tz).tz_localize(None)
        bundle = load_bundle(room_id) if replay else None
        obs = people_series(conn, room_id, tz, until=origin) if bundle is not None else None
        f = forecast_clock_24h(model, at=clock, observations=obs, short_bundle=bundle,
                               allow_live=obs is not None).head(hours * 12)
        times = [pd.Timestamp(t).timestamp() + shift_days * 86400 for t in f["target_time"]]
        expected, lower, upper = (list(f[c].astype(float)) for c in ("expected_people", "lower_80", "upper_80"))
        used_short = f["observation_mode"].iloc[0] == "recent_live_anchor"
        short_models = sorted({m.removeprefix("short_term_") for m in f["model_name"] if m.startswith("short_term_")})

        # CO2 start: measured in a replay; otherwise simulated over the usual pattern of the last 12 h
        actual = measured(conn, room_id, origin - 2 * 3600, times[-1]) if replay else {}
        now_row = actual.get(origin)
        window_now = bool(now_row and now_row["window_open"]) if replay else False
        cr = co2_room(room)
        if replay and now_row and now_row["co2_ppm"] is not None:
            c0 = float(now_row["co2_ppm"])
        else:
            warm = [origin - WARMUP_S + i * STEP_S for i in range(WARMUP_S // STEP_S)]
            c0 = room["outdoor_ppm"]
            for t, p in zip(warm, pattern_at(model, [t - shift_days * 86400 for t in warm], tz)):
                c0 = step_co2(c0, p, cr, room["airflow_m3h"], STEP_S / 3600)
    people_now = (now_row["people"] if replay and now_row else None)
    if people_now is None:
        people_now = round(pattern_at(model, [origin - shift_days * 86400], tz)[0], 2)

    window_q = room["airflow_m3h"] + room["k_flow"] * room["window_area_m2"]
    q_now = window_q if window_now else room["airflow_m3h"]
    scenarios = {"co2_ppm": (expected, q_now), "co2_lower": (lower, q_now), "co2_upper": (upper, q_now),
                 "co2_windows_closed": (expected, room["airflow_m3h"]), "co2_windows_open": (expected, window_q)}
    curves = {}
    for key, (people, q) in scenarios.items():
        c, prev, out = c0, origin, []
        for t, p in zip(times, people):
            c = step_co2(c, p, cr, q, (t - prev) / 3600)
            prev = t
            out.append(round(c))
        curves[key] = out

    points = []
    for i, t in enumerate(times):
        p = {"ts": iso(t), "people": round(expected[i], 2), "people_lower": round(lower[i], 2),
             "people_upper": round(upper[i], 2), **{k: v[i] for k, v in curves.items()}}
        if replay:
            row = actual.get(t)
            p["actual_people"] = row["people"] if row else None
            p["actual_co2_ppm"] = row["co2_ppm"] if row else None
        points.append(p)

    peak = max(points, key=lambda p: p["co2_ppm"])
    summary = {"people_now": people_now, "co2_now": round(c0), "window_open_now": window_now,
               "peak_co2": peak["co2_ppm"], "peak_at": peak["ts"],
               "first_above_1000_at": next((p["ts"] for p in points if p["co2_ppm"] >= 1000), None)}
    result = {"room_id": room_id, "origin": iso(origin), "replay": replay,
              "as_weekday": as_weekday.strip().lower()[:3] if as_weekday else None,
              "model": "short_term+historical_time_of_week" if used_short else "historical_time_of_week",
              "short_term_models": short_models, "summary": summary, "points": points}
    if replay:
        result["recent"] = [{"ts": iso(t), "people": actual[t]["people"] if t in actual else None,
                             "co2_ppm": actual[t]["co2_ppm"] if t in actual else None}
                            for t in range(int(origin) - 2 * 3600, int(origin) + 1, STEP_S)]
        result["accuracy"] = accuracy(points)
    return result


def accuracy(points: list[dict]) -> dict:
    """Mean absolute error against what happened, for the first hour and the whole range."""
    out = {}
    for label, pts in (("first_hour", points[:12]), ("all", points)):
        people = [abs(p["people"] - p["actual_people"]) for p in pts if p["actual_people"] is not None]
        co2 = [abs(p["co2_ppm"] - p["actual_co2_ppm"]) for p in pts if p["actual_co2_ppm"] is not None]
        out[label] = {"people_mae": round(sum(people) / len(people), 2) if people else None,
                      "co2_mae_ppm": round(sum(co2) / len(co2), 1) if co2 else None, "points": len(people)}
    return out

