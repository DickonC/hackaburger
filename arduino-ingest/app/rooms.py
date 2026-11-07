"""Rooms, historic occupancy, window state, CO2 estimate and 24-hour forecast.

People come from the room's board (see main.py) where it has readings, otherwise
from the room's occupancy_history (imported/synthetic). CO2 uses the frontend's
transient mass balance; the people forecast uses occupancy_forecaster/rolling_24h.py.
"""

import csv
import json
import math
import os
import re
import sqlite3
import sys
import time
import unicodedata
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, available_timezones

import pandas as pd
from fastapi import APIRouter, Body, Depends, HTTPException, Query

from app.core import DEVICE_RE, bucket_people, db, is_number, iso, parse_time, require_key, time_range

sys.path.insert(0, os.environ.get("FORECASTER_DIR", "/app/occupancy_forecaster"))
from rolling_24h import fit_calendar_24h, forecast_clock_24h, seasonal_predict  # noqa: E402

HISTORY_SEED_CSV = os.environ.get("HISTORY_SEED_CSV", "/app/seed/synthetic_occupancy.csv")
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
    "scheduled_count": (int, 0, 10000),
    "windows": ("windows", None, None),
    "co2_gen_lph": (float, 1, 100),
    "outdoor_ppm": (float, 300, 2000),
    "infiltration_ach": (float, 0, 20),
    "k_flow": (float, 0, 5000),
    "timezone": ("timezone", None, None),
}

# The frontend's preset rooms; the synthetic history is loaded for the one with a board
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
SEED_HISTORY_ROOM = "appleton"

router = APIRouter(dependencies=[Depends(require_key)])
_models: dict[str, tuple[float, tuple, Any]] = {}  # room_id -> (fitted_at, key, model)


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
    if conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0] == 0:
        for seed in SEED_ROOMS:
            room = {**ROOM_DEFAULTS, **{k: v for k, v in seed.items() if k != "id"}}
            conn.execute("INSERT INTO rooms (id, room) VALUES (?, ?)", (seed["id"], json.dumps(room)))
        if os.path.exists(HISTORY_SEED_CSV):
            import_csv(conn, SEED_HISTORY_ROOM, HISTORY_SEED_CSV, ROOM_DEFAULTS["timezone"], "synthetic")


def import_csv(conn: sqlite3.Connection, room_id: str, path: str, tz: str, source: str) -> int:
    """Load a forecaster-style CSV (timestamp,room_id,count in local time) as a room's history."""
    zone = ZoneInfo(tz)
    with open(path, newline="") as f:
        rows = [(room_id, datetime.fromisoformat(r["timestamp"]).replace(tzinfo=zone).timestamp(),
                 float(r["count"]), source) for r in csv.DictReader(f)]
    conn.executemany("INSERT OR REPLACE INTO occupancy_history VALUES (?, ?, ?, ?)", rows)
    return len(rows)


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
    return {
        "id": room_id,
        **room,
        "windows": [{**w, "is_open": i in open_idx} for i, w in enumerate(room["windows"])],
        "open_area_m2": round(sum(w["area_m2"] for i, w in enumerate(room["windows"]) if i in open_idx), 2),
        "history": {"points": h[0], "from": iso(h[1]) if h[1] else None, "to": iso(h[2]) if h[2] else None,
                    "enough_for_forecast": h[0] >= MIN_HISTORY_POINTS},
    }


def validate_room(changes: Any, *, partial: bool) -> dict:
    if not isinstance(changes, dict):
        raise HTTPException(422, "body must be a JSON object")
    clean = {}
    for field, value in changes.items():
        if field not in ROOM_FIELDS:
            raise HTTPException(422, f"unknown field: {field}")
        kind, lo, hi = ROOM_FIELDS[field]
        if kind == "device":
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


# ---------------------------------------------------------------- CO2 (same model as the frontend)

def airflow_m3h(room: dict, open_area: float) -> float:
    return room["infiltration_ach"] * room["volume_m3"] + room["k_flow"] * open_area


def step_co2(c0: float, people: float, room: dict, q_m3h: float, dt_h: float) -> float:
    q = max(q_m3h, 0.1)
    decay = math.exp(-(q / room["volume_m3"]) * dt_h)
    out = room["outdoor_ppm"]
    return out + (people * room["co2_gen_lph"] / q) * 1000.0 * (1.0 - decay) + (c0 - out) * decay


def simulate_co2(conn: sqlite3.Connection, room_id: str, room: dict, start: float, end: float,
                 step_s: float, model=None) -> list[dict]:
    """CO2 at start, start+step, ... up to end. `people` is the count used for the interval after `ts`.

    Starts at outdoor level WARMUP_S before `start`. Gaps with no data use the calendar
    model's expectation if there is one, otherwise 0 people.
    """
    warm = start - math.ceil(WARMUP_S / step_s) * step_s
    times = [warm + i * step_s for i in range(math.floor((end - warm) / step_s) + 1)]
    people = occupancy_series(conn, room_id, room, warm, times[-1] + step_s, step_s)
    assumed = [None] * len(people)
    if model is not None and any(v is None for v, _ in people):
        assumed = list(seasonal_predict(model, pd.to_datetime(times, unit="s", utc=True)))
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


def co2_now(conn: sqlite3.Connection, room_id: str, room: dict, now: float, model) -> float:
    """CO2 at the last 5-minute boundary, then stepped forward to `now`."""
    boundary = math.floor(now / STEP_S) * STEP_S
    p = simulate_co2(conn, room_id, room, boundary, boundary, STEP_S, model)[-1]
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
            "SELECT ts, people, source FROM occupancy_history WHERE room_id = ? AND ts >= ? AND ts < ? "
            "ORDER BY ts LIMIT 50000", (room_id, start, end)).fetchall()
    return {"room_id": room_id,
            "points": [{"ts": iso(r["ts"]), "people": r["people"], "source": r["source"]} for r in rows]}


@router.post("/rooms/{room_id}/history", status_code=201)
def add_history(room_id: str, body: Any = Body(...)):
    """{"source": "manual", "points": [{"ts": "2026-11-07T09:00:00Z", "people": 12}, ...]}; replaces same ts."""
    if not isinstance(body, dict) or not isinstance(body.get("points"), list) or len(body["points"]) > 50000:
        raise HTTPException(422, 'body must be {"source": str, "points": [{"ts", "people"}]} (max 50000)')
    source = body.get("source", "manual")
    if not isinstance(source, str) or not 1 <= len(source) <= 32:
        raise HTTPException(422, "source must be 1-32 characters")
    rows = []
    for p in body["points"]:
        ts = parse_time(p.get("ts")) if isinstance(p, dict) and isinstance(p.get("ts"), str) else None
        people = p.get("people") if isinstance(p, dict) else None
        if ts is None or not is_number(people) or people < 0:
            raise HTTPException(422, "each point needs an ISO ts and a people number >= 0")
        rows.append((room_id, ts, float(people), source))
    with db() as conn:
        get_room(conn, room_id)
        conn.executemany("INSERT OR REPLACE INTO occupancy_history VALUES (?, ?, ?, ?)", rows)
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
        points = simulate_co2(conn, room_id, room, start, end, step_s, calendar_model(conn, room_id, room))
    return {"room_id": room_id, "step_s": step_s, "estimated": True, "points": points}


@router.get("/rooms/{room_id}/forecast")
def read_forecast(room_id: str, hours: int = Query(default=24, ge=1, le=24),
                  as_weekday: str | None = Query(default=None, description="mon..sun: use that day's pattern")):
    """People and CO2 for the next `hours`, every 5 minutes, from the forecaster + live board data."""
    shift_days = 0
    now = time.time()
    with db() as conn:
        room = get_room(conn, room_id)
        model = calendar_model(conn, room_id, room)
        live = live_people(conn, room_id, room, now)
        c_now = co2_now(conn, room_id, room, now, model)
        timeline = window_timeline(conn, room_id, room)
    n = hours * 12
    cap = room["capacity"]

    if model is not None:
        clock = pd.Timestamp(now, unit="s", tz="UTC").tz_convert(room["timezone"])
        if as_weekday:
            wd = as_weekday.strip().lower()[:3]
            if wd not in WEEKDAYS:
                raise HTTPException(422, "as_weekday must be mon, tue, wed, thu, fri, sat or sun")
            shift_days = (clock.day_of_week - WEEKDAYS.index(wd)) % 7
        f = forecast_clock_24h(model, at=clock - pd.Timedelta(days=shift_days), allow_live=False).head(n)
        times = [pd.Timestamp(t).timestamp() + shift_days * 86400 for t in f["target_time"]]
        expected, lower, upper = (list(f[c].astype(float)) for c in ("expected_people", "lower_80", "upper_80"))
        name = "historical_time_of_week"
        if live is not None:
            # Same idea as rolling_24h's blend: today's difference fades into the usual pattern
            correction = live - expected[0]
            for i, t in enumerate(times):
                d = correction * math.exp(-(t - now) / 60 / ANCHOR_DECAY_MIN)
                expected[i], lower[i], upper[i] = (min(cap, max(0.0, v + d)) for v in (expected[i], lower[i], upper[i]))
            name += "+live_anchor"
    else:
        if as_weekday:
            raise HTTPException(409, "as_weekday needs a room with at least four weeks of history")
        origin = math.floor(now / STEP_S) * STEP_S
        times = [origin + (i + 1) * STEP_S for i in range(n)]
        expected = lower = upper = [float(live or 0.0)] * n
        name = "persistence_no_history"

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
        "as_weekday": as_weekday[:3].lower() if model is not None and as_weekday else None,
        "live_people": live,
        "capacity": cap,
        "summary": {"co2_now": round(c_now), "open_area_m2": round(current_area, 2),
                    "peak_co2": peak["co2_ppm"], "peak_at": peak["ts"], "first_above_1000_at": above},
        "points": points,
    }
