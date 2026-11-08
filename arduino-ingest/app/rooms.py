"""Live rooms: the rooms our boards are in, their window state and estimated CO2.

People come only from the room's board (readings, see main.py); the forecasting side
(forecast.py) is separate and uses the Aalborg dataset. CO2 is the frontend's transient
mass balance, run on the board's counts and the window state.
"""

import json
import math
import re
import sqlite3
import time
import unicodedata
from typing import Any
from zoneinfo import available_timezones

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from app import heatmap
from app.core import DEVICE_RE, bucket_people, db, is_number, iso, require_key, time_range

STEP_S = 300
WARMUP_S = 12 * 3600  # CO2 is simulated from outdoor level this long before the requested range
LIVE_MAX_AGE_S = 600  # a board reading older than this isn't "now"
ROOM_ID_RE = re.compile(r"^[a-z0-9_-]{1,64}$")
HEATMAP_DEVICE = "heatmap"  # a room whose people come from the four-board heatmap

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

# The frontend's preset rooms
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
    {"id": "live", "name": "Live Room", "volume_m3": 120.0, "capacity": 24,
     "device": "heatmap", "scheduled_count": 0,
     "windows": [{"name": "Window 1", "area_m2": 1.2}, {"name": "Window 2", "area_m2": 1.2}]},
]

router = APIRouter(dependencies=[Depends(require_key)])


# ---------------------------------------------------------------- storage

def migrate(conn: sqlite3.Connection):
    conn.execute("CREATE TABLE IF NOT EXISTS rooms (id TEXT PRIMARY KEY, room TEXT NOT NULL)")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS window_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id TEXT NOT NULL,
            ts REAL NOT NULL,
            open TEXT NOT NULL
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_window_events ON window_events (room_id, ts)")
    # Earlier versions mixed dataset rooms and history into the live tables (now in forecast.py)
    conn.execute("DROP TABLE IF EXISTS occupancy_history")
    conn.execute("DELETE FROM rooms WHERE id LIKE 'aalborg-%'")
    for row in conn.execute("SELECT id, room FROM rooms WHERE room LIKE '%model_from%'").fetchall():
        room = json.loads(row["room"])
        room.pop("model_from", None)
        conn.execute("UPDATE rooms SET room = ? WHERE id = ?", (json.dumps(room), row["id"]))
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
    return {
        "id": room_id,
        **room,
        "windows": [{**w, "is_open": i in open_idx} for i, w in enumerate(room["windows"])],
        "open_area_m2": round(sum(w["area_m2"] for i, w in enumerate(room["windows"]) if i in open_idx), 2),
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


# ---------------------------------------------------------------- people and CO2

def board_people(conn: sqlite3.Connection, room: dict, start: float, end: float, step_s: float) -> list[float | None]:
    """The room's people per bucket (None where there's no data).

    device is a board name (that board's count) or "heatmap" (the four-board heatmap's estimate).
    """
    n = math.ceil((end - start) / step_s)
    if room["device"] == HEATMAP_DEVICE:
        return heatmap.people_buckets(conn, start, end, step_s)
    return bucket_people(conn, room["device"], start, end, step_s) if room["device"] else [None] * n


def airflow_m3h(room: dict, open_area: float) -> float:
    return room["infiltration_ach"] * room["volume_m3"] + room["k_flow"] * open_area


def step_co2(c0: float, people: float, room: dict, q_m3h: float, dt_h: float) -> float:
    q = max(q_m3h, 0.1)
    decay = math.exp(-(q / room["volume_m3"]) * dt_h)
    out = room["outdoor_ppm"]
    return out + (people * room["co2_gen_lph"] / q) * 1000.0 * (1.0 - decay) + (c0 - out) * decay


def simulate_co2(conn: sqlite3.Connection, room_id: str, room: dict, start: float, end: float,
                 step_s: float) -> list[dict]:
    """CO2 at start, start+step, ... up to end. `people` is the board count for the interval after `ts`.

    Starts at outdoor level WARMUP_S before `start`. Intervals without board data count as empty.
    """
    warm = start - math.ceil(WARMUP_S / step_s) * step_s
    times = [warm + i * step_s for i in range(math.floor((end - warm) / step_s) + 1)]
    people = board_people(conn, room, warm, times[-1] + step_s, step_s)
    timeline = window_timeline(conn, room_id, room)
    c = room["outdoor_ppm"]
    points = []
    for t, n in zip(times, people):
        area = open_area_at(timeline, t)
        if t >= start:
            points.append({"ts": iso(t), "co2_ppm": round(c), "people": None if n is None else round(n, 1),
                           "open_area_m2": round(area, 2)})
        c = step_co2(c, n or 0.0, room, airflow_m3h(room, area), step_s / 3600)
    return points


def current_state(conn: sqlite3.Connection, room_id: str, room: dict, now: float) -> dict:
    """CO2 now (stepped from the last 5-minute boundary) and the board's count over the last 10 minutes."""
    boundary = math.floor(now / STEP_S) * STEP_S
    p = simulate_co2(conn, room_id, room, boundary, boundary, STEP_S)[-1]
    area = open_area_at(window_timeline(conn, room_id, room), now)
    people = board_people(conn, room, now - LIVE_MAX_AGE_S, now + 1, LIVE_MAX_AGE_S + 1)[0]
    co2 = step_co2(p["co2_ppm"], p["people"] or 0.0, room, airflow_m3h(room, area), (now - boundary) / 3600)
    return {"ts": iso(now), "co2_ppm": round(co2), "people": None if people is None else round(people, 2),
            "open_area_m2": round(area, 2), "airflow_m3h": round(airflow_m3h(room, area), 1)}


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
        conn.execute("DELETE FROM window_events WHERE room_id = ?", (room_id,))
        conn.execute("DELETE FROM rooms WHERE id = ?", (room_id,))
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


@router.get("/rooms/{room_id}/occupancy")
def read_occupancy(room_id: str, from_: str | None = Query(default=None, alias="from"), to: str | None = None,
                   step_s: int = Query(default=STEP_S, ge=60, le=86400)):
    """The room's board count per bucket (null where the board sent nothing). Default: last 24 h."""
    start, end = time_range(from_, to, 86400, time.time())
    if (end - start) / step_s > 10000:
        raise HTTPException(422, "too many points; increase step_s or shorten the range")
    with db() as conn:
        room = get_room(conn, room_id)
        values = board_people(conn, room, start, end, step_s)
    return {"room_id": room_id, "device": room["device"], "step_s": step_s,
            "points": [{"ts": iso(start + i * step_s), "people": None if v is None else round(v, 1)}
                       for i, v in enumerate(values)]}


@router.get("/rooms/{room_id}/co2")
def read_co2(room_id: str, from_: str | None = Query(default=None, alias="from"), to: str | None = None,
             step_s: int = Query(default=STEP_S, ge=60, le=3600)):
    """Estimated CO2 from the board's counts and window state (there is no CO2 sensor). Default: last 6 h."""
    now = time.time()
    start, end = time_range(from_, to, 6 * 3600, now)
    if (end - start) / step_s > 5000:
        raise HTTPException(422, "too many points; increase step_s or shorten the range")
    with db() as conn:
        room = get_room(conn, room_id)
        points = simulate_co2(conn, room_id, room, start, end, step_s)
        current = current_state(conn, room_id, room, now)
    return {"room_id": room_id, "step_s": step_s, "estimated": True, "now": current, "points": points}
