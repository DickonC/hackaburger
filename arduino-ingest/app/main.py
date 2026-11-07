"""hackaburger ingest server.

Boards POST BLE scans to /data; the reply carries the board's config. The server
recounts people from the raw device list (see docs/server-agent-brief.md).

Rounding: people = floor(people_raw + 0.5), i.e. round half up (2.5 -> 3).
All paths here are served publicly under /ingest/ (nginx strips the prefix).
"""

import json
import os
import sqlite3
import statistics
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import rooms
from app.core import (API_KEY, bucket_people, check_device, count, db, is_number, iso, parse_time,
                      require_key, round_half_up, time_range)

MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", 256 * 1024))  # matches nginx client_max_body_size
MAX_DEVICES = 5000

DEFAULT_CONFIG = {
    "config_version": 1,
    "enabled": True,
    "threshold_dbm": -75,
    "window_s": 30,
    "interval_s": 5,
    "people_factor": 0.7,
    "send_devices": True,
    "display": True,
}
PRESETS = {
    "room": {"threshold_dbm": -75, "window_s": 30, "interval_s": 5},
    "demo": {"threshold_dbm": -40, "window_s": 6, "interval_s": 1},
    "venue": {"threshold_dbm": -100, "window_s": 30, "interval_s": 5},
}
INITIAL_PRESET = {"uno-q-1": "room", "uno-q-2": "demo"}

# field -> (types, min, max); bool is checked separately because bool is an int subclass
CONFIG_FIELDS = {
    "enabled": (bool, None, None),
    "threshold_dbm": (int, -100, -30),
    "window_s": (int, 2, 300),
    "interval_s": ((int, float), 1, 60),
    "people_factor": ((int, float), 0.1, 2.0),
    "send_devices": (bool, None, None),
    "display": (bool, None, None),
}

NEW_COLUMNS = {
    "measured_at": "REAL",  # unix time from ts, else received_at
    "source": "TEXT",  # 'server' (counted from devices) or 'board' (fallback)
    "threshold_dbm": "INTEGER",  # config at receipt
    "people_factor": "REAL",
    "devices_in_range": "INTEGER",
    "heard_total": "INTEGER",
    "people_raw": "REAL",
    "rssis": "TEXT",  # JSON list of valid device RSSIs, for recounts
}


def clean_devices(devices: list) -> tuple[list[float], int]:
    """Return the RSSIs of valid device items and how many items were dropped."""
    rssis = []
    for d in devices:
        if (
            isinstance(d, dict)
            and isinstance(d.get("id"), str)
            and 1 <= len(d["id"]) <= 64
            and is_number(d.get("rssi"))
            and -127 <= d["rssi"] <= 0
            and isinstance(d.get("n"), int)
            and not isinstance(d["n"], bool)
            and d["n"] >= 1
        ):
            rssis.append(float(d["rssi"]))
    return rssis, len(devices) - len(rssis)


def compute(data: dict, rssis: list[float] | None, threshold: int, factor: float) -> dict:
    """Server-side count for one message (see 'Calculation' in the brief)."""
    if rssis is not None:
        in_range, people_raw = count(rssis, threshold, factor)
        return {"source": "server", "devices_in_range": in_range, "heard_total": len(rssis),
                "people_raw": people_raw}
    heard = data.get("heard_total") if is_number(data.get("heard_total")) else None
    if is_number(data.get("devices_in_range")):
        dir_ = int(data["devices_in_range"])
        return {"source": "board", "devices_in_range": dir_, "heard_total": heard,
                "people_raw": round(dir_ * factor, 6)}
    if is_number(data.get("people")):
        return {"source": "board", "devices_in_range": None, "heard_total": heard,
                "people_raw": float(data["people"])}
    return {"source": None, "devices_in_range": None, "heard_total": heard, "people_raw": None}


def get_config(conn: sqlite3.Connection, device: str) -> dict:
    row = conn.execute("SELECT config FROM configs WHERE device = ?", (device,)).fetchone()
    if row:
        return {**DEFAULT_CONFIG, **json.loads(row["config"])}
    cfg = {**DEFAULT_CONFIG, **PRESETS.get(INITIAL_PRESET.get(device, ""), {})}
    conn.execute("INSERT OR IGNORE INTO configs (device, config) VALUES (?, ?)", (device, json.dumps(cfg)))
    return cfg


def migrate(conn: sqlite3.Connection):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device TEXT NOT NULL,
            device_ts TEXT,
            received_at TEXT NOT NULL,
            data TEXT NOT NULL
        )"""
    )
    conn.execute("CREATE TABLE IF NOT EXISTS configs (device TEXT PRIMARY KEY, config TEXT NOT NULL)")
    have = {r["name"] for r in conn.execute("PRAGMA table_info(readings)")}
    for col, typ in NEW_COLUMNS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE readings ADD COLUMN {col} {typ}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_device_measured ON readings (device, measured_at)")

    # Backfill rows stored before the server did its own counting, using the device's current config
    for r in conn.execute("SELECT * FROM readings WHERE measured_at IS NULL").fetchall():
        cfg = get_config(conn, r["device"])
        data = json.loads(r["data"])
        devices = data.get("devices")
        rssis = clean_devices(devices)[0] if isinstance(devices, list) else None
        measured = parse_time(r["device_ts"]) or parse_time(r["received_at"])
        conn.execute(
            """UPDATE readings SET measured_at=?, source=?, threshold_dbm=?, people_factor=?,
               devices_in_range=?, heard_total=?, people_raw=?, rssis=? WHERE id=?""",
            (measured, *store_values(data, rssis, cfg), r["id"]),
        )


def store_values(data: dict, rssis: list[float] | None, cfg: dict) -> tuple:
    c = compute(data, rssis, cfg["threshold_dbm"], cfg["people_factor"])
    return (c["source"], cfg["threshold_dbm"], cfg["people_factor"], c["devices_in_range"],
            c["heard_total"], c["people_raw"], json.dumps(rssis) if rssis is not None else None)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not API_KEY:
        raise RuntimeError("API_KEY is not set")
    with db() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        migrate(conn)
        rooms.migrate(conn)
    yield


app = FastAPI(title="hackaburger arduino-ingest", lifespan=lifespan)
app.include_router(rooms.router)
# The frontend may be opened from anywhere (file://, localhost); every call still needs X-API-Key
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def limit_body_size(request: Request, call_next):
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > MAX_BODY_BYTES:
        return JSONResponse({"detail": f"body larger than {MAX_BODY_BYTES} bytes"}, status_code=413)
    return await call_next(request)


def validate_reading(r: Any) -> tuple[str, str | None, dict, list[float] | None, int]:
    if not isinstance(r, dict):
        raise HTTPException(422, "each reading must be a JSON object")
    device = check_device(r.get("device"))
    ts = r.get("ts")
    if ts is not None and not isinstance(ts, str):
        raise HTTPException(422, "ts must be a string or null")
    data = r.get("data")
    if not isinstance(data, dict):
        raise HTTPException(422, "data must be a JSON object")
    devices = data.get("devices")
    if devices is None:
        return device, ts, data, None, 0
    if not isinstance(devices, list) or len(devices) > MAX_DEVICES:
        raise HTTPException(422, f"data.devices must be a list of at most {MAX_DEVICES} items")
    rssis, dropped = clean_devices(devices)
    return device, ts, data, rssis, dropped


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/data", dependencies=[Depends(require_key)], status_code=201)
def ingest(body: Any = Body(...)):
    """One reading or a list of readings (e.g. queued resends)."""
    readings = [validate_reading(r) for r in (body if isinstance(body, list) else [body])]
    if not readings:
        raise HTTPException(422, "empty list")
    now = datetime.now(timezone.utc)
    dropped = 0
    with db() as conn:
        for device, ts, data, rssis, k in readings:
            dropped += k
            cfg = get_config(conn, device)
            measured = parse_time(ts) or now.timestamp()
            conn.execute(
                """INSERT INTO readings (device, device_ts, received_at, data, measured_at, source,
                   threshold_dbm, people_factor, devices_in_range, heard_total, people_raw, rssis)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (device, ts, now.isoformat(), json.dumps(data), measured, *store_values(data, rssis, cfg)),
            )
        # A batch normally comes from one board; reply with the last reading's config
        cfg = get_config(conn, readings[-1][0])
    return {"stored": len(readings), "dropped": dropped, "config": cfg}


@app.get("/data", dependencies=[Depends(require_key)])
def list_readings(
    device: str | None = None,
    since: str | None = Query(default=None, description="ISO timestamp; only readings received after this"),
    limit: int = Query(default=100, ge=1, le=5000),
):
    sql = "SELECT id, device, device_ts, received_at, data FROM readings WHERE 1=1"
    args: list[Any] = []
    if device:
        sql += " AND device = ?"
        args.append(device)
    if since:
        sql += " AND received_at > ?"
        args.append(since)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with db() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [
        {
            "id": r["id"],
            "device": r["device"],
            "ts": r["device_ts"],
            "received_at": r["received_at"],
            "data": json.loads(r["data"]),
        }
        for r in rows
    ]


@app.get("/config/{device}", dependencies=[Depends(require_key)])
def read_config(device: str):
    check_device(device)
    with db() as conn:
        return get_config(conn, device)


@app.put("/config/{device}", dependencies=[Depends(require_key)])
def update_config(device: str, changes: Any = Body(...)):
    check_device(device)
    if not isinstance(changes, dict) or not changes:
        raise HTTPException(422, "body must be a non-empty JSON object")
    changes = dict(changes)
    if "config_version" in changes:
        raise HTTPException(422, "config_version is managed by the server")
    update = {}
    if "preset" in changes:
        preset = changes.pop("preset")
        if preset not in PRESETS:
            raise HTTPException(422, f"preset must be one of {sorted(PRESETS)}")
        update.update(PRESETS[preset])
    for field, value in changes.items():
        if field not in CONFIG_FIELDS:
            raise HTTPException(422, f"unknown field: {field}")
        types, lo, hi = CONFIG_FIELDS[field]
        if types is bool:
            if not isinstance(value, bool):
                raise HTTPException(422, f"{field} must be true or false")
        else:
            if isinstance(value, bool) or not isinstance(value, types):
                raise HTTPException(422, f"{field} must be {'an integer' if types is int else 'a number'}")
            if not lo <= value <= hi:
                raise HTTPException(422, f"{field} must be between {lo} and {hi}")
        update[field] = value
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cfg = get_config(conn, device)
        cfg.update(update)
        cfg["config_version"] += 1
        conn.execute("UPDATE configs SET config = ? WHERE device = ?", (json.dumps(cfg), device))
    return cfg


@app.get("/occupancy/latest", dependencies=[Depends(require_key)])
def occupancy_latest(device: str):
    check_device(device)
    with db() as conn:
        cfg = get_config(conn, device)
        last = conn.execute(
            "SELECT * FROM readings WHERE device = ? ORDER BY measured_at DESC, id DESC LIMIT 1", (device,)
        ).fetchone()
        if not last:
            raise HTTPException(404, f"no readings for {device}")
        last_received = conn.execute(
            "SELECT MAX(received_at) FROM readings WHERE device = ?", (device,)
        ).fetchone()[0]
        # Smooth over the smooth_s seconds up to the latest measurement; demo mode stays instant
        smooth_s = 0 if cfg["window_s"] <= 10 else 30
        raws = [
            r[0]
            for r in conn.execute(
                """SELECT people_raw FROM readings WHERE device = ? AND people_raw IS NOT NULL
                   AND measured_at > ? AND measured_at <= ?""",
                (device, last["measured_at"] - smooth_s, last["measured_at"]),
            )
        ] if smooth_s else []
    data = json.loads(last["data"])
    online = time.time() - parse_time(last_received) <= 3 * cfg["interval_s"] + 5
    people = round_half_up(last["people_raw"])
    return {
        "device": device,
        "ts": iso(last["measured_at"]),
        "online": online,
        "devices_in_range": last["devices_in_range"],
        "people": people,
        "people_smoothed": round_half_up(statistics.median(raws)) if raws else people,
        "heard_total": last["heard_total"],
        "source": last["source"],
        "board_people": data.get("people"),
        "config": cfg,
    }


@app.get("/occupancy/history", dependencies=[Depends(require_key)])
def occupancy_history(
    device: str,
    from_: str | None = Query(default=None, alias="from", description="ISO time; default to - 1 hour"),
    to: str | None = Query(default=None, description="ISO time; default now"),
    step_s: int = Query(default=60, ge=1, le=86400),
    threshold_dbm: int | None = Query(default=None, ge=-127, le=0, description="recount with this range"),
):
    check_device(device)
    start, end = time_range(from_, to, 3600, time.time())
    if (end - start) / step_s > 10000:
        raise HTTPException(422, "too many points; increase step_s or shorten the range")
    with db() as conn:
        values = bucket_people(conn, device, start, end, step_s, threshold_dbm)
    return {
        "device": device,
        "step_s": step_s,
        "threshold_dbm": threshold_dbm,
        "points": [
            {"ts": iso(start + i * step_s), "people": None if v is None else round(v, 1)}
            for i, v in enumerate(values)
        ],
    }
