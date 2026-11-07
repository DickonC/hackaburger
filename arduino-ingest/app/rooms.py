"""Rooms, historic occupancy, window state, CO2 estimate and 24-hour forecast.

People come from the room's board (see main.py) where it has readings, otherwise
from the room's occupancy_history (e.g. the Aalborg office dataset, see dataset.py).
CO2 uses the frontend's transient mass balance. The people forecast uses
occupancy_forecaster: the short-term models (occupancy_core) for the first hour when
the board is live, and the weekday x time-of-day model (rolling_24h) for 24 hours.
A room can borrow another room's models with `model_from`; patterns are matched by
local wall-clock time and scaled by capacity.
"""

import json
import math
import os
import re
import sqlite3
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any
from zoneinfo import available_timezones

import joblib
import pandas as pd
from fastapi import APIRouter, Body, Depends, HTTPException, Query

from app.core import DEVICE_RE, bucket_people, db, is_number, iso, parse_time, require_key, time_range

sys.path.insert(0, os.environ.get("FORECASTER_DIR", "/app/occupancy_forecaster"))
from rolling_24h import fit_calendar_24h, forecast_clock_24h  # noqa: E402

MODELS_DIR = Path(os.environ.get("MODELS_DIR", "/data/models"))
STEP_S = 300  # the forecaster works in 5-minute slots
MIN_HISTORY_POINTS = 28 * 24 * 12  # four weeks, required by fit_calendar_24h
WARMUP_S = 12 * 3600  # CO2 is simulated from outdoor level this long before the requested range
LIVE_MAX_AGE_S = 600  # a board reading older than this doesn't anchor the forecast
ANCHOR_DECAY_MIN = 90  # live-vs-calendar difference fades with this time constant
MODEL_TTL_S = 60
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
ROOM_ID_RE = re.compile(r"^[a-z0-9_-]{1,64}$")

ROOM_DEFAULTS = {
    "capacity": 32,
    "device": None,
    "model_from": None,  # room whose history/models to use, if this room has none of its own
    "scheduled_count": 0,
    "co2_gen_lph": 16.0,  # L/h CO2 per person (16 sedentary, 24 presenting)
    "outdoor_ppm": 420.0,
    "infiltration_ach": 0.25,  # air changes per hour with windows closed
    "k_flow": 350.0,  # m3/h per m2 of open window
    "timezone": "Europe/London",
}
# field -> (type, min, max)
ROOM_FIELDS = {
    "name": (str, 1, 100),
    "volume_m3": (float, 1, 100000),
    "capacity": (int, 1, 10000),
    "device": ("device", None, None),
    "model_from": ("room_ref", None, None),
    "scheduled_count": (int, 0, 10000),
    "windows": ("windows", None, None),
    "co2_gen_lph": (float, 1, 100),
    "outdoor_ppm": (float, 300, 2000),
    "infiltration_ach": (float, 0, 20),
    "k_flow": (float, 0, 5000),
    "timezone": ("timezone", None, None),
}

# The frontend's preset rooms (dataset.py links them to dataset rooms via model_from)
SEED_ROOMS = [
    {"id": "appleton", "name": "Appleton Tower — Study Pod 4.02", "volume_m3": 65.0, "capacity": 32,
     "device": "uno-q-1", "scheduled_count": 12,
     "windows": [{"name": "Win 01 (North)", "area_m2": 1.0}, {"name": "Win 02 (North)", "area_m2": 1.0}]},
    {"id": "forum", "name": "Informatics Forum — Meeting Room 2.14", "volume_m3": 140.0, "capacity": 30,
     "scheduled_count": 20,
     "windows": [{"name": "North Win 1", "area_m2": 1.2}, {"name": "North Win 2", "area_m2": 1.2},
                 {"name": "East Win 1", "area_m2": 1.0}, {"name": "East Win 2", "area_m2": 1.0}]},
    {"id": "personal", "name": "Personal Study Mode — Dorm / Bedroom", "volume_m3": 32.0, "capacity": 2,
     "scheduled_count": 1, "windows": [{"name": "Bedroom Sash Win", "area_m2": 0.8}]},
]

router = APIRouter(dependencies=[Depends(require_key)])
_models: dict[str, tuple[float, tuple, Any]] = {}  # room_id -> (fitted_at, key, calendar model)
_bundles: dict[str, tuple[float, Any]] = {}  # room_id -> (file mtime, short-term ForecastBundle)


# ---------------------------------------------------------------- storage

def migrate(conn: sqlite3.Connection):
    conn.execute("CREATE TABLE IF NOT EXISTS rooms (id TEXT PRIMARY KEY, room TEXT NOT NULL)")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS occupancy_history (
            room_id TEXT NOT NULL,
            ts REAL NOT NULL,
            people REAL NOT NULL,
            source TEXT NOT NULL,
            PRIMARY KEY (room_id, ts)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS window_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id TEXT NOT NULL,
            ts REAL NOT NULL,
            open TEXT NOT NULL
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_window_events ON window_events (room_id, ts)")
    have = {r[1] for r in conn.execute("PRAGMA table_info(occupancy_history)")}
    for col, typ in (("co2_ppm", "REAL"), ("window_open", "INTEGER")):  # measured, where the source has them
        if col not in have:
            conn.execute(f"ALTER TABLE occupancy_history ADD COLUMN {col} {typ}")
    if conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0] == 0:
        for seed in SEED_ROOMS:
            room = {**ROOM_DEFAULTS, **{k: v for k, v in seed.items() if k != "id"}}
            conn.execute("INSERT INTO rooms (id, room) VALUES (?, ?)", (seed["id"], json.dumps(room)))


def get_room(conn: sqlite3.Connection, room_id: str) -> dict:
    row = conn.execute("SELECT room FROM rooms WHERE id = ?", (room_id,)).fetchone()
    if not row:
        raise HTTPException(404, f"no room {room_id!r}")
    return {**ROOM_DEFAULTS, **json.loads(row["room"])}


def window_timeline(conn: sqlite3.Connection, room_id: str, room: dict) -> list[tuple[float, float, list[int]]]:
    """(ts, open area m2, open indices) for every window change, oldest first."""
    out = []
    for r in conn.execute("SELECT ts, open FROM window_events WHERE room_id = ? ORDER BY ts, id", (room_id,)):
        idx = [i for i in json.loads(r["open"]) if i < len(room["windows"])]
        out.append((r["ts"], sum(room["windows"][i]["area_m2"] for i in idx), idx))
    return out


def open_area_at(timeline: list, t: float) -> float:
    area = 0.0
    for ts, a, _ in timeline:
        if ts > t:
            break
        area = a
    return area


def room_out(conn: sqlite3.Connection, room_id: str, room: dict) -> dict:
    timeline = window_timeline(conn, room_id, room)
    open_idx = set(timeline[-1][2]) if timeline else set()
    h = conn.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM occupancy_history WHERE room_id = ?",
                     (room_id,)).fetchone()
    sources = [r[0] for r in conn.execute("SELECT DISTINCT source FROM occupancy_history WHERE room_id = ?",
                                          (room_id,))]
    return {
        "id": room_id,
        **room,
        "windows": [{**w, "is_open": i in open_idx} for i, w in enumerate(room["windows"])],
        "open_area_m2": round(sum(w["area_m2"] for i, w in enumerate(room["windows"]) if i in open_idx), 2),
        "history": {"points": h[0], "from": iso(h[1]) if h[1] else None, "to": iso(h[2]) if h[2] else None,
                    "sources": sources, "enough_for_forecast": h[0] >= MIN_HISTORY_POINTS,
                    "short_term_model": (MODELS_DIR / room_id / "forecast_bundle.joblib").exists()},
    }


def validate_room(changes: Any, *, partial: bool) -> dict:
    if not isinstance(changes, dict):
        raise HTTPException(422, "body must be a JSON object")
    clean = {}
    for field, value in changes.items():
        if field not in ROOM_FIELDS:
            raise HTTPException(422, f"unknown field: {field}")
        kind, lo, hi = ROOM_FIELDS[field]
        if kind == "room_ref":
            if value is not None and (not isinstance(value, str) or not ROOM_ID_RE.match(value)):
                raise HTTPException(422, "model_from must be null or a room id")
        elif kind == "device":
            if value is not None and (not isinstance(value, str) or not DEVICE_RE.match(value)):
                raise HTTPException(422, "device must be null or 1-64 characters of [A-Za-z0-9_-]")
        elif kind == "timezone":
            if value not in available_timezones():
                raise HTTPException(422, "timezone must be an IANA name, e.g. Europe/London")
        elif kind == "windows":
            value = validate_windows(value)
        elif kind is str:
            if not isinstance(value, str) or not lo <= len(value.strip()) <= hi:
                raise HTTPException(422, f"{field} must be a string of {lo}-{hi} characters")
            value = value.strip()
        elif kind is int:
            if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
                raise HTTPException(422, f"{field} must be an integer between {lo} and {hi}")
        else:
            if not is_number(value) or not lo <= value <= hi:
                raise HTTPException(422, f"{field} must be a number between {lo} and {hi}")
            value = float(value)
        clean[field] = value
    if not partial:
        for required in ("name", "volume_m3", "windows"):
            if required not in clean:
                raise HTTPException(422, f"{required} is required")
    elif not clean:
        raise HTTPException(422, "no changes")
    return clean


def validate_windows(value: Any) -> list[dict]:
    if not isinstance(value, list) or len(value) > 50:
        raise HTTPException(422, "windows must be a list of at most 50 items")
    out = []
    for i, w in enumerate(value):
        if not isinstance(w, dict) or set(w) - {"name", "area_m2"}:
            raise HTTPException(422, "each window is {\"name\": str, \"area_m2\": number}")
        area = w.get("area_m2")
        if not is_number(area) or not 0.1 <= area <= 20:
            raise HTTPException(422, "window area_m2 must be a number between 0.1 and 20")
        name = w.get("name", f"Window {i + 1:02d}")
        if not isinstance(name, str) or not 1 <= len(name) <= 64:
            raise HTTPException(422, "window name must be 1-64 characters")
        out.append({"name": name, "area_m2": float(area)})
    return out


def slugify(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:48] or "room"


# ---------------------------------------------------------------- occupancy

def history_buckets(conn: sqlite3.Connection, room_id: str, start: float, end: float, step_s: float):
    n = math.ceil((end - start) / step_s)
    sums, counts = [0.0] * n, [0] * n
    for r in conn.execute("SELECT ts, people FROM occupancy_history WHERE room_id = ? AND ts >= ? AND ts < ?",
                          (room_id, start, end)):
        i = int((r["ts"] - start) // step_s)
        sums[i] += r["people"]
        counts[i] += 1
    return [sums[i] / counts[i] if counts[i] else None for i in range(n)]


def occupancy_series(conn: sqlite3.Connection, room_id: str, room: dict, start: float, end: float,
                     step_s: float) -> list[tuple[float | None, str | None]]:
    """(people, source) per bucket: the board's readings win over stored history."""
    hist = history_buckets(conn, room_id, start, end, step_s)
    sensor = bucket_people(conn, room["device"], start, end, step_s) if room["device"] else [None] * len(hist)
    return [(s, "sensor") if s is not None else (h, "history") if h is not None else (None, None)
            for s, h in zip(sensor, hist)]


def live_people(conn: sqlite3.Connection, room_id: str, room: dict, now: float) -> float | None:
    """Mean people over the last LIVE_MAX_AGE_S: the board's readings, else recent history points."""
    v, _ = occupancy_series(conn, room_id, room, now - LIVE_MAX_AGE_S, now + 1, LIVE_MAX_AGE_S + 1)[0]
    return None if v is None else round(v, 2)


def calendar_model(conn: sqlite3.Connection, room_id: str, room: dict):
    """The forecaster's weekday x time-of-day model, fitted on history + board data (cached briefly)."""
    h = conn.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM occupancy_history WHERE room_id = ?",
                     (room_id,)).fetchone()
    if h[0] < MIN_HISTORY_POINTS:
        return None
    key = (h[0], h[2], room["device"], room["capacity"], room["timezone"])
    cached = _models.get(room_id)
    if cached and cached[1] == key and time.time() - cached[0] < MODEL_TTL_S:
        return cached[2]
    start, end = math.floor(h[1] / STEP_S) * STEP_S, time.time()
    series = occupancy_series(conn, room_id, room, start, end, STEP_S)
    index = pd.to_datetime([start + i * STEP_S for i in range(len(series))], unit="s", utc=True)
    s = pd.Series([v for v, _ in series], index=index.tz_convert(room["timezone"]).tz_localize(None), dtype=float)
    s = s.resample("5min").median().ffill(limit=2)
    try:
        model = fit_calendar_24h(s, room_id, room["capacity"], timezone=room["timezone"])
    except ValueError:
        model = None
    _models[room_id] = (time.time(), key, model)
    return model


def load_bundle(room_id: str):
    """Short-term ForecastBundle trained by app.dataset (only files this server wrote are loaded)."""
    path = MODELS_DIR / room_id / "forecast_bundle.joblib"
    if not path.exists():
        return None
    mtime = path.stat().st_mtime
    cached = _bundles.get(room_id)
    if not cached or cached[0] != mtime:
        _bundles[room_id] = (mtime, joblib.load(path))
    return _bundles[room_id][1]


def room_models(conn: sqlite3.Connection, room_id: str, room: dict) -> dict | None:
    """The calendar + short-term models for a room: its own, or those of room['model_from']."""
    source_id = room["model_from"] or room_id
    source = room
    if source_id != room_id:
        row = conn.execute("SELECT room FROM rooms WHERE id = ?", (source_id,)).fetchone()
        if not row:
            return None
        source = {**ROOM_DEFAULTS, **json.loads(row["room"])}
    calendar = calendar_model(conn, source_id, source)
    if calendar is None:
        return None
    return {"source": source_id, "calendar": calendar, "bundle": load_bundle(source_id),
            "scale": source["capacity"] / room["capacity"]}  # room people -> model people


def wall_times(epochs, tz: str) -> pd.DatetimeIndex:
    return pd.to_datetime(list(epochs), unit="s", utc=True).tz_convert(tz).tz_localize(None)


def expected_people(models: dict, room: dict, epochs) -> list[float]:
    """The calendar model's usual count at these times (room-local wall clock), in room people."""
    cal = models["calendar"]
    return [float(cal.patterns[t.day_of_week][t.hour * 12 + t.minute // 5]) / models["scale"]
            for t in wall_times(epochs, room["timezone"])]


def observation_series(conn: sqlite3.Connection, room: dict, models: dict, now: float) -> pd.Series | None:
    """Live board counts for the short-term models, in model people on the room's wall clock.

    The models need the count 1 day and 1 week ago; slots the board didn't cover are filled
    with the usual (calendar) count for that time. None if the board isn't live now.
    """
    if not room["device"]:
        return None
    end = math.floor(now / STEP_S) * STEP_S + STEP_S  # includes the current, partial slot
    start = end - (2016 + 25) * STEP_S
    live = bucket_people(conn, room["device"], start, end, STEP_S)
    if live[-1] is None:
        live, end = live[:-1], end - STEP_S
        if live[-1] is None:
            return None
    times = [start + i * STEP_S for i in range(len(live))]
    usual = expected_people(models, room, times)
    values = [(v if v is not None else u) * models["scale"] for v, u in zip(live, usual)]
    s = pd.Series(values, index=wall_times(times, room["timezone"]), dtype=float)
    return s[~s.index.duplicated(keep="last")]


def forecast_people(conn: sqlite3.Connection, room: dict, models: dict, now: float, n: int, shift_days: int):
    """(times, expected, lower, upper, used_short_term) for the n 5-minute slots after now."""
    cal, scale, tz = models["calendar"], models["scale"], room["timezone"]
    wall_now = pd.Timestamp(now, unit="s", tz="UTC").tz_convert(tz).tz_localize(None) - pd.Timedelta(days=shift_days)
    obs = observation_series(conn, room, models, now) if models["bundle"] is not None and not shift_days else None
    # A naive `at` is read in the model's timezone, so the room's wall clock maps onto the model's
    f = forecast_clock_24h(cal, at=wall_now, observations=obs, short_bundle=models["bundle"],
                           allow_live=obs is not None).head(n)
    times = [(pd.Timestamp(t).tz_localize(None) + pd.Timedelta(days=shift_days))
             .tz_localize(tz, ambiguous=True, nonexistent="shift_forward").timestamp() for t in f["target_time"]]
    cap = room["capacity"]
    cols = [[min(cap, max(0.0, v / scale)) for v in f[c].astype(float)] for c in ("expected_people", "lower_80", "upper_80")]
    used_short = f["observation_mode"].iloc[0] == "recent_live_anchor"
    short_models = sorted({m.removeprefix("short_term_") for m in f["model_name"] if m.startswith("short_term_")})
    return times, *cols, used_short, short_models


# ---------------------------------------------------------------- CO2 (same model as the frontend)

def airflow_m3h(room: dict, open_area: float) -> float:
    return room["infiltration_ach"] * room["volume_m3"] + room["k_flow"] * open_area


def step_co2(c0: float, people: float, room: dict, q_m3h: float, dt_h: float) -> float:
    q = max(q_m3h, 0.1)
    decay = math.exp(-(q / room["volume_m3"]) * dt_h)
    out = room["outdoor_ppm"]
    return out + (people * room["co2_gen_lph"] / q) * 1000.0 * (1.0 - decay) + (c0 - out) * decay


def simulate_co2(conn: sqlite3.Connection, room_id: str, room: dict, start: float, end: float,
                 step_s: float, models: dict | None = None) -> list[dict]:
    """CO2 at start, start+step, ... up to end. `people` is the count used for the interval after `ts`.

    Starts at outdoor level WARMUP_S before `start`. Gaps with no data use the calendar
    models' usual count for that time if there are models, otherwise 0 people.
    """
    warm = start - math.ceil(WARMUP_S / step_s) * step_s
    times = [warm + i * step_s for i in range(math.floor((end - warm) / step_s) + 1)]
    people = occupancy_series(conn, room_id, room, warm, times[-1] + step_s, step_s)
    assumed = [None] * len(people)
    if models is not None and any(v is None for v, _ in people):
        assumed = expected_people(models, room, times)
    timeline = window_timeline(conn, room_id, room)
    c = room["outdoor_ppm"]
    points = []
    for i, t in enumerate(times):
        n, source = people[i]
        if n is None:
            n, source = (assumed[i], "assumed") if assumed[i] is not None else (0.0, "none")
        area = open_area_at(timeline, t)
        if t >= start:
            points.append({"ts": iso(t), "co2_ppm": round(c), "people": round(n, 1), "people_source": source,
                           "open_area_m2": round(area, 2)})
        c = step_co2(c, n, room, airflow_m3h(room, area), step_s / 3600)
    return points


def co2_now(conn: sqlite3.Connection, room_id: str, room: dict, now: float, models) -> float:
    """CO2 at the last 5-minute boundary, then stepped forward to `now`."""
    boundary = math.floor(now / STEP_S) * STEP_S
    p = simulate_co2(conn, room_id, room, boundary, boundary, STEP_S, models)[-1]
    area = open_area_at(window_timeline(conn, room_id, room), boundary)
    return step_co2(p["co2_ppm"], p["people"], room, airflow_m3h(room, area), (now - boundary) / 3600)


# ---------------------------------------------------------------- endpoints

@router.get("/rooms")
def list_rooms():
    with db() as conn:
        rows = conn.execute("SELECT id, room FROM rooms ORDER BY rowid").fetchall()
        return [room_out(conn, r["id"], {**ROOM_DEFAULTS, **json.loads(r["room"])}) for r in rows]


@router.post("/rooms", status_code=201)
def create_room(body: Any = Body(...)):
    if not isinstance(body, dict):
        raise HTTPException(422, "body must be a JSON object")
    body = dict(body)
    room_id = body.pop("id", None)
    room = {**ROOM_DEFAULTS, **validate_room(body, partial=False)}
    with db() as conn:
        if room_id is None:
            base = room_id = slugify(room["name"])
            n = 2
            while conn.execute("SELECT 1 FROM rooms WHERE id = ?", (room_id,)).fetchone():
                room_id, n = f"{base}-{n}", n + 1
        elif not isinstance(room_id, str) or not ROOM_ID_RE.match(room_id):
            raise HTTPException(422, "id must be 1-64 characters of [a-z0-9_-]")
        elif conn.execute("SELECT 1 FROM rooms WHERE id = ?", (room_id,)).fetchone():
            raise HTTPException(409, f"room {room_id!r} already exists")
        conn.execute("INSERT INTO rooms (id, room) VALUES (?, ?)", (room_id, json.dumps(room)))
        return room_out(conn, room_id, room)


@router.get("/rooms/{room_id}")
def read_room(room_id: str):
    with db() as conn:
        return room_out(conn, room_id, get_room(conn, room_id))


@router.put("/rooms/{room_id}")
def update_room(room_id: str, changes: Any = Body(...)):
    clean = validate_room(changes, partial=True)
    with db() as conn:
        room = {**get_room(conn, room_id), **clean}
        conn.execute("UPDATE rooms SET room = ? WHERE id = ?", (json.dumps(room), room_id))
        return room_out(conn, room_id, room)


@router.delete("/rooms/{room_id}")
def delete_room(room_id: str):
    with db() as conn:
        get_room(conn, room_id)
        for table in ("occupancy_history", "window_events"):
            conn.execute(f"DELETE FROM {table} WHERE room_id = ?", (room_id,))
        conn.execute("DELETE FROM rooms WHERE id = ?", (room_id,))
    _models.pop(room_id, None)
    return {"deleted": room_id}


@router.put("/rooms/{room_id}/windows")
def set_windows(room_id: str, body: Any = Body(...)):
    """{"open": [0, 2]}: indices of the windows that are open from now on ([] closes all)."""
    if not isinstance(body, dict) or set(body) != {"open"} or not isinstance(body["open"], list):
        raise HTTPException(422, 'body must be {"open": [window indices]}')
    with db() as conn:
        room = get_room(conn, room_id)
        idx = body["open"]
        if any(isinstance(i, bool) or not isinstance(i, int) or not 0 <= i < len(room["windows"]) for i in idx):
            raise HTTPException(422, f"window indices must be 0-{len(room['windows']) - 1}")
        conn.execute("INSERT INTO window_events (room_id, ts, open) VALUES (?, ?, ?)",
                     (room_id, time.time(), json.dumps(sorted(set(idx)))))
        return room_out(conn, room_id, room)


@router.get("/rooms/{room_id}/history")
def read_history(room_id: str, from_: str | None = Query(default=None, alias="from"), to: str | None = None):
    start, end = time_range(from_, to, 86400, time.time())
    with db() as conn:
        get_room(conn, room_id)
        rows = conn.execute(
            "SELECT ts, people, source, co2_ppm, window_open FROM occupancy_history "
            "WHERE room_id = ? AND ts >= ? AND ts < ? ORDER BY ts LIMIT 50000", (room_id, start, end)).fetchall()
    return {"room_id": room_id,
            "points": [{"ts": iso(r["ts"]), "people": r["people"], "source": r["source"],
                        "co2_ppm": r["co2_ppm"], "window_open": r["window_open"]} for r in rows]}


@router.post("/rooms/{room_id}/history", status_code=201)
def add_history(room_id: str, body: Any = Body(...)):
    """{"source": "manual", "points": [{"ts": "2026-11-07T09:00:00Z", "people": 12, "co2_ppm": 640}, ...]}

    co2_ppm (measured) is optional. Points with the same ts are replaced.
    """
    if not isinstance(body, dict) or not isinstance(body.get("points"), list) or len(body["points"]) > 50000:
        raise HTTPException(422, 'body must be {"source": str, "points": [{"ts", "people"}]} (max 50000)')
    source = body.get("source", "manual")
    if not isinstance(source, str) or not 1 <= len(source) <= 32:
        raise HTTPException(422, "source must be 1-32 characters")
    rows = []
    for p in body["points"]:
        ts = parse_time(p.get("ts")) if isinstance(p, dict) and isinstance(p.get("ts"), str) else None
        people = p.get("people") if isinstance(p, dict) else None
        co2 = p.get("co2_ppm") if isinstance(p, dict) else None
        if ts is None or not is_number(people) or people < 0 or (co2 is not None and not is_number(co2)):
            raise HTTPException(422, "each point needs an ISO ts, a people number >= 0 and optionally co2_ppm")
        rows.append((room_id, ts, float(people), source, None if co2 is None else float(co2)))
    with db() as conn:
        get_room(conn, room_id)
        conn.executemany("INSERT OR REPLACE INTO occupancy_history (room_id, ts, people, source, co2_ppm) "
                         "VALUES (?, ?, ?, ?, ?)", rows)
    return {"stored": len(rows)}


@router.delete("/rooms/{room_id}/history")
def delete_history(room_id: str, source: str | None = None):
    with db() as conn:
        get_room(conn, room_id)
        sql, args = "DELETE FROM occupancy_history WHERE room_id = ?", [room_id]
        if source:
            sql, args = sql + " AND source = ?", args + [source]
        n = conn.execute(sql, args).rowcount
    _models.pop(room_id, None)
    return {"deleted": n}


@router.get("/rooms/{room_id}/occupancy")
def read_occupancy(room_id: str, from_: str | None = Query(default=None, alias="from"), to: str | None = None,
                   step_s: int = Query(default=STEP_S, ge=60, le=86400)):
    start, end = time_range(from_, to, 86400, time.time())
    if (end - start) / step_s > 10000:
        raise HTTPException(422, "too many points; increase step_s or shorten the range")
    with db() as conn:
        room = get_room(conn, room_id)
        series = occupancy_series(conn, room_id, room, start, end, step_s)
    return {"room_id": room_id, "device": room["device"], "step_s": step_s,
            "points": [{"ts": iso(start + i * step_s), "people": None if v is None else round(v, 1), "source": s}
                       for i, (v, s) in enumerate(series)]}


@router.get("/rooms/{room_id}/co2")
def read_co2(room_id: str, from_: str | None = Query(default=None, alias="from"), to: str | None = None,
             step_s: int = Query(default=STEP_S, ge=60, le=3600)):
    """Estimated CO2 from people and window state; there is no CO2 sensor."""
    start, end = time_range(from_, to, 6 * 3600, time.time())
    if (end - start) / step_s > 5000:
        raise HTTPException(422, "too many points; increase step_s or shorten the range")
    with db() as conn:
        room = get_room(conn, room_id)
        points = simulate_co2(conn, room_id, room, start, end, step_s, room_models(conn, room_id, room))
    return {"room_id": room_id, "step_s": step_s, "estimated": True, "points": points}


@router.get("/rooms/{room_id}/forecast")
def read_forecast(room_id: str, hours: int = Query(default=24, ge=1, le=24),
                  as_weekday: str | None = Query(default=None, description="mon..sun: use that day's pattern")):
    """People and CO2 for the next `hours`, every 5 minutes.

    People: the forecaster's short-term models for the first hour when the room's board is
    live (fading into the usual pattern, as in rolling_24h), otherwise the weekday x
    time-of-day pattern; rooms without any models hold the current count.
    """
    now = time.time()
    n = hours * 12
    with db() as conn:
        room = get_room(conn, room_id)
        models = room_models(conn, room_id, room)
        live = live_people(conn, room_id, room, now)
        c_now = co2_now(conn, room_id, room, now, models)
        timeline = window_timeline(conn, room_id, room)
        shift_days = 0
        if as_weekday:
            if models is None:
                raise HTTPException(409, "as_weekday needs a room with history (or model_from)")
            wd = as_weekday.strip().lower()[:3]
            if wd not in WEEKDAYS:
                raise HTTPException(422, "as_weekday must be mon, tue, wed, thu, fri, sat or sun")
            today = pd.Timestamp(now, unit="s", tz="UTC").tz_convert(room["timezone"]).day_of_week
            shift_days = (today - WEEKDAYS.index(wd)) % 7
        if models is not None:
            times, expected, lower, upper, used_short, short_models = forecast_people(
                conn, room, models, now, n, shift_days)
        else:
            origin = math.floor(now / STEP_S) * STEP_S
            times = [origin + (i + 1) * STEP_S for i in range(n)]
            expected = lower = upper = [float(live or 0.0)] * n
            used_short, short_models = False, []
    cap = room["capacity"]

    if models is None:
        name = "persistence_no_history"
    elif used_short:
        name = "short_term+historical_time_of_week"
    else:
        name = "historical_time_of_week"
        if live is not None:
            # No short-term model run (no bundle, or a weekday override): fade today's difference out
            correction = live - expected[0]
            for i, t in enumerate(times):
                d = correction * math.exp(-(t - now) / 60 / ANCHOR_DECAY_MIN)
                expected[i], lower[i], upper[i] = (min(cap, max(0.0, v + d)) for v in (expected[i], lower[i], upper[i]))
            name += "+live_anchor"

    current_area = open_area_at(timeline, now)
    total_area = sum(w["area_m2"] for w in room["windows"])
    scenarios = {"co2_ppm": (expected, current_area), "co2_lower": (lower, current_area),
                 "co2_upper": (upper, current_area), "co2_windows_closed": (expected, 0.0),
                 "co2_windows_open": (expected, total_area)}
    curves = {}
    for key, (people, area) in scenarios.items():
        c, prev, out = c_now, now, []
        for t, p in zip(times, people):
            c = step_co2(c, p, room, airflow_m3h(room, area), (t - prev) / 3600)
            prev = t
            out.append(round(c))
        curves[key] = out

    points = [{"ts": iso(t), "people": round(expected[i], 2), "people_lower": round(lower[i], 2),
               "people_upper": round(upper[i], 2), **{k: v[i] for k, v in curves.items()}}
              for i, t in enumerate(times)]
    peak = max(points, key=lambda p: p["co2_ppm"])
    above = next((p["ts"] for p in points if p["co2_ppm"] >= 1000), None)
    return {
        "room_id": room_id,
        "generated_at": iso(now),
        "model": name,
        "models": {"source_room": models["source"] if models else None, "short_term": short_models,
                   "scale": round(1 / models["scale"], 3) if models else None},
        "as_weekday": as_weekday.strip().lower()[:3] if as_weekday else None,
        "live_people": live,
        "capacity": cap,
        "summary": {"co2_now": round(c_now), "open_area_m2": round(current_area, 2),
                    "peak_co2": peak["co2_ppm"], "peak_at": peak["ts"], "first_above_1000_at": above},
        "points": points,
    }
