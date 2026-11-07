"""Room heatmap from four corner boards (docs/heatmap-backend-guide.md).

Boards send unfiltered, anonymised device lists with clock-aligned `ts`, so the four
messages for one moment share a `ts`. For each `ts` the server builds a snapshot when all
boards have sent, or WAIT_S after the first one did:

1. join the boards' RSSI per device id and add each board's offset;
2. in-room filter: not an obvious non-person (kind in exclude_kinds, or the same id seen
   continuously for stationary_h hours), heard by >= min_boards, weakest signal >= edge_dbm,
   and present for presence_s (the first failing check is the device's `reason`);
3. position = signal-weighted centre of the board positions;
4. people_estimate = in-room devices / devices_per_person; heatmap = a Gaussian per device
   (scaled to people), smoothed per cell over time.

The room edge (from the room size) and the board offsets (from devices all four boards
hear) calibrate themselves unless switched to manual. Board control goes through the
normal config replies (boards.py). Per-device rows are deleted after 24 h.
"""

import asyncio
import copy
import json
import logging
import math
import os
import sqlite3
import statistics
import threading
import time
from typing import Any

import numpy as np
from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool

from app.boards import apply_changes, get_config, validate_changes
from app.core import DEVICE_RE, db, is_number, iso, parse_time, require_key, time_range

log = logging.getLogger("heatmap")

TICK_S = float(os.environ.get("HEATMAP_TICK_S", 2))  # background loop period; 0 disables it (tests)
WAIT_S = 15  # build a snapshot this long after its first board message, even if boards are missing
LOOKBACK_S = 3600  # board messages older than this don't start a snapshot (e.g. late resends)
RSSI_1M, PATH_LOSS_N = -59.0, 2.2  # indoor path-loss model for the automatic room edge
OFFSETS_EVERY_S, OFFSETS_WINDOW_S, OFFSETS_MIN_DEVICES = 60, 600, 30
DEBUG_KEEP_S = 86400  # per-device rows (and board device lists) are kept for at most 24 h
CORNERS = ("top_left", "top_right", "bottom_right", "bottom_left")
MODES = {"live": {"send_every_s": 0, "interval_s": 10, "window_s": 20}, "dev": {"send_every_s": 300}}
# Device categories the boards send (docs/server-api.md), most specific first; the generic ones last
KINDS = ("earbuds", "tracker", "beacon", "fixed", "apple_device", "phone", "computer", "wearable",
         "brand_other", "unknown")
NOT_PEOPLE = ["earbuds", "tracker", "beacon", "fixed"]

DEFAULT_CONFIG = {
    "room": {"width_m": 8.0, "length_m": 6.0, "board_inset_m": 0.5},
    "boards": {f"uno-q-{i + 1}": {"corner": c} for i, c in enumerate(CORNERS)},
    "filter": {"exclude_kinds": NOT_PEOPLE, "stationary_h": 2.0, "edge_mode": "auto", "edge_dbm": -85.0,
               "edge_margin_db": 4.0, "min_boards": 3, "presence_s": 30},
    "offsets_mode": "auto",
    "offsets_db": {f"uno-q-{i + 1}": 0.0 for i in range(4)},
    "position": {"weight_k": 2.0},
    "heatmap": {"cell_m": 0.5, "sigma_m": 1.0, "smoothing_s": 60},
    "people": {"devices_per_person": 1.2},
}
# section -> field -> (kind, min, max)
FIELDS = {
    "room": {"width_m": (float, 1, 100), "length_m": (float, 1, 100), "board_inset_m": (float, 0, 3)},
    "filter": {"exclude_kinds": ("kinds",), "stationary_h": (float, 0, 48),
               "edge_mode": ("mode",), "edge_dbm": (float, -100, -40), "edge_margin_db": (float, 0, 15),
               "min_boards": (int, 2, 4), "presence_s": (int, 0, 600)},
    "position": {"weight_k": (float, 0.5, 5)},
    "heatmap": {"cell_m": (float, 0.25, 2), "sigma_m": (float, 0.25, 5), "smoothing_s": (int, 0, 600)},
    "people": {"devices_per_person": (float, 0.5, 4)},
}

router = APIRouter(prefix="/heatmap", dependencies=[Depends(require_key)])
_lock = threading.Lock()  # one snapshot builder at a time (ingest hook + background loop)
_last_offsets = 0.0
_last_prune = 0.0


# ---------------------------------------------------------------- storage and config

def migrate(conn: sqlite3.Connection):
    conn.execute("CREATE TABLE IF NOT EXISTS heatmap_config (id INTEGER PRIMARY KEY CHECK (id = 1), config TEXT NOT NULL)")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS heatmap_snapshots (
            ts REAL PRIMARY KEY,
            computed_at REAL NOT NULL,
            people_estimate REAL NOT NULL,
            devices_in_room INTEGER NOT NULL,
            devices_heard INTEGER NOT NULL,
            funnel TEXT NOT NULL,
            boards TEXT NOT NULL,      -- per board: messages present, devices heard
            filter TEXT NOT NULL,      -- edge/min_boards/presence and offsets used
            grid TEXT NOT NULL,        -- time-smoothed people per cell, row-major
            grid_raw_sum REAL NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS heatmap_devices (
            ts REAL NOT NULL,
            id TEXT NOT NULL,
            rssi TEXT NOT NULL,
            boards_heard INTEGER NOT NULL,
            weakest_dbm REAL,
            x REAL,
            y REAL,
            pass_edge INTEGER NOT NULL,  -- passed checks 1 and 2 (for the presence check)
            status TEXT NOT NULL,
            reason TEXT,
            PRIMARY KEY (ts, id)
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_heatmap_devices_id ON heatmap_devices (id, ts)")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS heatmap_tracks (
            id TEXT PRIMARY KEY,
            first_ts REAL NOT NULL,  -- start of the current continuous run of snapshots with this id
            last_ts REAL NOT NULL
        )"""
    )
    for table, col in (("heatmap_devices", "kind TEXT"), ("heatmap_snapshots", "kinds TEXT")):
        if col.split()[0] not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col}")


def merged(base: dict, patch: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merged(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_config(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT config FROM heatmap_config WHERE id = 1").fetchone()
    cfg = merged(DEFAULT_CONFIG, json.loads(row["config"]) if row else {})
    if row and "boards" in json.loads(row["config"]):
        cfg["boards"] = json.loads(row["config"])["boards"]  # the stored board list replaces the default one
    if cfg["filter"]["edge_mode"] == "auto":
        cfg["filter"]["edge_dbm"] = auto_edge(cfg)
    for b in cfg["boards"]:
        cfg["offsets_db"].setdefault(b, 0.0)
    return cfg


def save_config(conn: sqlite3.Connection, cfg: dict):
    conn.execute("INSERT OR REPLACE INTO heatmap_config (id, config) VALUES (1, ?)", (json.dumps(cfg),))


def board_positions(cfg: dict) -> dict[str, tuple[float, float]]:
    w, l, inset = cfg["room"]["width_m"], cfg["room"]["length_m"], cfg["room"]["board_inset_m"]
    corner_xy = {"top_left": (inset, inset), "top_right": (w - inset, inset),
                 "bottom_right": (w - inset, l - inset), "bottom_left": (inset, l - inset)}
    out = {}
    for b, spec in cfg["boards"].items():
        x, y = corner_xy.get(spec.get("corner"), (None, None))
        out[b] = (float(spec.get("x", x)), float(spec.get("y", y)))
    return out


def auto_edge(cfg: dict) -> float:
    """The weakest RSSI a phone inside the room should produce, minus a margin (path-loss model)."""
    pos = list(board_positions(cfg).values())
    d_max = max((math.dist(a, b) for i, a in enumerate(pos) for b in pos[i + 1:]), default=1.0)
    rssi_far = RSSI_1M - 10 * PATH_LOSS_N * math.log10(max(d_max, 1.0))
    return float(min(-60, max(-100, round(rssi_far - cfg["filter"]["edge_margin_db"]))))


def validate_patch(patch: Any, current: dict) -> dict:
    """Deep-merge a partial config onto the current one, with 422 on anything invalid."""
    if not isinstance(patch, dict) or not patch:
        raise HTTPException(422, "body must be a non-empty JSON object")
    patch = copy.deepcopy(patch)
    for section, value in patch.items():
        if section in FIELDS:
            if not isinstance(value, dict):
                raise HTTPException(422, f"{section} must be an object")
            for field, v in value.items():
                spec = FIELDS[section].get(field)
                if spec is None:
                    raise HTTPException(422, f"unknown field: {section}.{field}")
                value[field] = check_value(f"{section}.{field}", v, spec)
        elif section == "offsets_mode":
            check_value("offsets_mode", value, ("mode",))
        elif section == "offsets_db":
            if not isinstance(value, dict):
                raise HTTPException(422, "offsets_db must be an object of board -> dB")
            for b, v in value.items():
                value[b] = check_value(f"offsets_db.{b}", v, (float, -20, 20))
        elif section == "boards":
            if not isinstance(value, dict):
                raise HTTPException(422, "boards must be an object of board -> {corner, x?, y?}")
            for b, spec in value.items():
                if not isinstance(b, str) or not DEVICE_RE.match(b):
                    raise HTTPException(422, f"invalid board name: {b!r}")
                if spec is None:
                    continue  # removes the board
                if not isinstance(spec, dict) or set(spec) - {"corner", "x", "y"}:
                    raise HTTPException(422, f"boards.{b} must be {{corner, x?, y?}}")
                if "corner" in spec and spec["corner"] not in CORNERS:
                    raise HTTPException(422, f"boards.{b}.corner must be one of {list(CORNERS)}")
                for axis in ("x", "y"):
                    if axis in spec:
                        spec[axis] = check_value(f"boards.{b}.{axis}", spec[axis], (float, 0, 100))
        else:
            raise HTTPException(422, f"unknown field: {section}")
    # Setting a calibrated value by hand means switching that calibration to manual
    if "edge_dbm" in patch.get("filter", {}) and "edge_mode" not in patch["filter"]:
        patch["filter"]["edge_mode"] = "manual"
    if "offsets_db" in patch and "offsets_mode" not in patch:
        patch["offsets_mode"] = "manual"
    new = merged(current, patch)
    if "boards" in patch:
        new["boards"] = {b: merged(current["boards"].get(b, {}), s)
                         for b, s in merged(current["boards"], patch["boards"]).items() if s is not None}
    for b, spec in new["boards"].items():
        if "corner" not in spec and not ("x" in spec and "y" in spec):
            raise HTTPException(422, f"boards.{b} needs a corner or both x and y")
    if not 2 <= len(new["boards"]) <= 8:
        raise HTTPException(422, "the heatmap needs 2-8 boards")
    if new["filter"]["min_boards"] > len(new["boards"]):
        raise HTTPException(422, "filter.min_boards can't exceed the number of boards")
    new["offsets_db"] = {b: new["offsets_db"].get(b, 0.0) for b in new["boards"]}
    return new


def check_value(name: str, v: Any, spec: tuple):
    if spec[0] == "kinds":
        if not isinstance(v, list) or any(k not in KINDS for k in v):
            raise HTTPException(422, f"{name} must be a list of {list(KINDS)}")
        return sorted(set(v), key=KINDS.index)
    if spec[0] == "mode":
        if v not in ("auto", "manual"):
            raise HTTPException(422, f"{name} must be 'auto' or 'manual'")
        return v
    kind, lo, hi = spec
    if kind is int and (isinstance(v, bool) or not isinstance(v, int)):
        raise HTTPException(422, f"{name} must be an integer")
    if not is_number(v) or not lo <= v <= hi:
        raise HTTPException(422, f"{name} must be a number between {lo} and {hi}")
    return kind(v)


# ---------------------------------------------------------------- snapshots

def board_messages(conn: sqlite3.Connection, boards: list[str], ts: float) -> dict[str, dict]:
    """The latest message per board for this ts: {board: {id: (rssi, kind)}}."""
    marks = ",".join("?" * len(boards))
    out = {}
    for r in conn.execute(f"SELECT device, data FROM readings WHERE measured_at = ? AND device IN ({marks}) "
                          "ORDER BY id", (ts, *boards)):
        devices = json.loads(r["data"]).get("devices") or []
        out[r["device"]] = {d["id"]: (float(d["rssi"]), d.get("kind") if d.get("kind") in KINDS else "unknown")
                            for d in devices
                            if isinstance(d, dict) and isinstance(d.get("id"), str) and is_number(d.get("rssi"))}
    return out


def device_kind(kinds: list[str]) -> str:
    """Boards can disagree: the most specific category wins (generic brand_other/unknown last)."""
    return min(kinds, key=KINDS.index) if kinds else "unknown"


def update_tracks(conn: sqlite3.Connection, ids: set[str], ts: float, max_gap_s: float) -> dict[str, float]:
    """Continuous presence per id: {id: first_ts of its current run}. A gap > max_gap_s starts a new run."""
    first = {}
    for dev_id in ids:
        row = conn.execute("SELECT first_ts, last_ts FROM heatmap_tracks WHERE id = ?", (dev_id,)).fetchone()
        if row and ts - row["last_ts"] <= max_gap_s and ts >= row["first_ts"]:
            first[dev_id] = row["first_ts"]
            conn.execute("UPDATE heatmap_tracks SET last_ts = MAX(last_ts, ?) WHERE id = ?", (ts, dev_id))
        else:
            first[dev_id] = ts
            conn.execute("INSERT OR REPLACE INTO heatmap_tracks VALUES (?, ?, ?)", (dev_id, ts, ts))
    return first


def gaussian_grid(cfg: dict, points: list[tuple[float, float]]) -> np.ndarray:
    """People per cell: each point spreads 1/devices_per_person over a 2-D Gaussian (clipped at the walls)."""
    cell, sigma = cfg["heatmap"]["cell_m"], cfg["heatmap"]["sigma_m"]
    cols = math.ceil(cfg["room"]["width_m"] / cell)
    rows = math.ceil(cfg["room"]["length_m"] / cell)
    xs = (np.arange(cols) + 0.5) * cell
    ys = (np.arange(rows) + 0.5) * cell
    grid = np.zeros((rows, cols))
    scale = cell * cell / (2 * math.pi * sigma * sigma) / cfg["people"]["devices_per_person"]
    for x, y in points:
        gx = np.exp(-((xs - x) ** 2) / (2 * sigma * sigma))
        gy = np.exp(-((ys - y) ** 2) / (2 * sigma * sigma))
        grid += np.outer(gy, gx) * scale
    return grid


def build_snapshot(conn: sqlite3.Connection, ts: float, cfg: dict, now: float):
    boards = list(cfg["boards"])
    pos = board_positions(cfg)
    heard = board_messages(conn, boards, ts)
    offsets, f = cfg["offsets_db"], cfg["filter"]
    edge, min_boards, presence_s = f["edge_dbm"], f["min_boards"], f["presence_s"]
    exclude, stationary_s = set(f["exclude_kinds"]), f["stationary_h"] * 3600
    k = cfg["position"]["weight_k"]
    w, l = cfg["room"]["width_m"], cfg["room"]["length_m"]
    bc = get_config(conn, boards[0])
    interval = bc["interval_s"]
    needed = math.ceil(presence_s / interval) if presence_s else 0
    history: dict[str, list[int]] = {}
    if presence_s:
        for r in conn.execute("SELECT id, pass_edge FROM heatmap_devices WHERE ts > ? AND ts < ?",
                              (ts - presence_s, ts)):
            history.setdefault(r["id"], []).append(r["pass_edge"])

    ids = set().union(*heard.values()) if heard else set()
    # A missed snapshot or two doesn't break a run; a longer gap (e.g. a board offline) does
    run_start = update_tracks(conn, ids, ts, 3 * (bc["send_every_s"] or interval) + WAIT_S)
    rows, in_room_xy, kinds_in_room = [], [], {}
    funnel = {"heard": len(ids), "plausible_people": 0, "enough_boards": 0, "inside_edge": 0, "present": 0}
    for dev_id in ids:
        seen = {b: heard[b][dev_id] for b in boards if dev_id in heard.get(b, {})}
        rssi = {b: seen[b][0] if b in seen else None for b in boards}
        board_kinds = [v[1] for v in seen.values()]
        kind = device_kind(board_kinds)
        adj = {b: v + offsets.get(b, 0.0) for b, v in rssi.items() if v is not None}
        weakest = min(adj.values())
        x = y = None
        if len(adj) >= 2:
            wts = {b: 10 ** (v / (10 * k)) for b, v in adj.items()}
            total = sum(wts.values())
            x = min(w, max(0.0, sum(wts[b] * pos[b][0] for b in wts) / total))
            y = min(l, max(0.0, sum(wts[b] * pos[b][1] for b in wts) / total))
        reason = None
        if any(k in exclude for k in board_kinds):
            reason = "not_a_person"
        elif stationary_s and ts - run_start[dev_id] >= stationary_s:
            reason = "stationary"
        elif len(adj) < min_boards:
            funnel["plausible_people"] += 1
            reason = "too_few_boards"
        else:
            funnel["plausible_people"] += 1
            funnel["enough_boards"] += 1
            if weakest < edge:
                reason = "outside_edge"
            else:
                funnel["inside_edge"] += 1
                past = history.get(dev_id, [])
                if presence_s and (not all(past) or len(past) + 1 < needed):
                    reason = "too_brief"
        pass_edge = reason in (None, "too_brief")
        if reason is None:
            funnel["present"] += 1
            in_room_xy.append((x, y))
            kinds_in_room[kind] = kinds_in_room.get(kind, 0) + 1
        rows.append((ts, dev_id, json.dumps(rssi), len(adj), round(weakest, 1),
                     None if x is None else round(x, 2), None if y is None else round(y, 2),
                     int(pass_edge), "in_room" if reason is None else "rejected", reason, kind))
    conn.executemany("INSERT OR REPLACE INTO heatmap_devices (ts, id, rssi, boards_heard, weakest_dbm, x, y, "
                     "pass_edge, status, reason, kind) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)

    dpp = cfg["people"]["devices_per_person"]
    grid = gaussian_grid(cfg, in_room_xy)
    prev = conn.execute("SELECT grid FROM heatmap_snapshots WHERE ts < ? ORDER BY ts DESC LIMIT 1", (ts,)).fetchone()
    smoothing = cfg["heatmap"]["smoothing_s"]
    alpha = 1.0 if smoothing == 0 else min(1.0, interval / smoothing)
    smoothed = grid
    if prev:
        old = np.array(json.loads(prev["grid"]))
        if old.shape == grid.shape:
            smoothed = alpha * grid + (1 - alpha) * old
    board_stats = {b: {"message": b in heard, "heard": len(heard.get(b, {}))} for b in boards}
    used = {"edge_dbm": edge, "min_boards": min_boards, "presence_s": presence_s, "offsets_db": offsets,
            "exclude_kinds": sorted(exclude, key=KINDS.index), "stationary_h": f["stationary_h"]}
    kinds_sorted = dict(sorted(kinds_in_room.items(), key=lambda kv: -kv[1]))
    conn.execute("INSERT OR REPLACE INTO heatmap_snapshots (ts, computed_at, people_estimate, devices_in_room, "
                 "devices_heard, funnel, boards, filter, grid, grid_raw_sum, kinds) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (ts, now, len(in_room_xy) / dpp, len(in_room_xy), len(ids), json.dumps(funnel),
                  json.dumps(board_stats), json.dumps(used), json.dumps(np.round(smoothed, 4).tolist()),
                  float(grid.sum()), json.dumps(kinds_sorted)))


def ready_snapshots(conn: sqlite3.Connection, boards: list[str], now: float) -> list[float]:
    marks = ",".join("?" * len(boards))
    pending = conn.execute(
        f"""SELECT measured_at, COUNT(DISTINCT device) AS n, MIN(received_at) AS first FROM readings
            WHERE device IN ({marks}) AND measured_at > ?
              AND measured_at NOT IN (SELECT ts FROM heatmap_snapshots WHERE ts > ?)
            GROUP BY measured_at ORDER BY measured_at""",
        (*boards, now - LOOKBACK_S, now - LOOKBACK_S)).fetchall()
    return [r["measured_at"] for r in pending
            if r["n"] >= len(boards) or now - parse_time(r["first"]) >= WAIT_S]


def update_offsets(conn: sqlite3.Connection, cfg: dict):
    """Auto offsets: line up each board's median RSSI over in-room devices that all boards hear."""
    boards = list(cfg["boards"])
    latest = conn.execute("SELECT MAX(ts) FROM heatmap_snapshots").fetchone()[0]
    if latest is None:
        return
    rows = [json.loads(r["rssi"]) for r in conn.execute(
        "SELECT rssi FROM heatmap_devices WHERE ts > ? AND status = 'in_room' AND boards_heard = ?",
        (latest - OFFSETS_WINDOW_S, len(boards)))]
    if len(rows) < OFFSETS_MIN_DEVICES:
        return
    med = {b: statistics.median(r[b] for r in rows) for b in boards}
    mean = statistics.fmean(med.values())
    for b in boards:
        new = min(10.0, max(-10.0, mean - med[b]))
        cfg["offsets_db"][b] = round(0.8 * cfg["offsets_db"].get(b, 0.0) + 0.2 * new, 2)
    save_config(conn, {k: v for k, v in cfg.items()})


def tick(now: float | None = None):
    """Build every snapshot that is ready, recalibrate offsets each minute, prune old device rows."""
    global _last_offsets, _last_prune
    now = time.time() if now is None else now
    with _lock, db() as conn:
        cfg = load_config(conn)
        for ts in ready_snapshots(conn, list(cfg["boards"]), now):
            build_snapshot(conn, ts, cfg, now)
        if cfg["offsets_mode"] == "auto" and now - _last_offsets >= OFFSETS_EVERY_S:
            _last_offsets = now
            update_offsets(conn, cfg)
        if now - _last_prune >= 3600:
            _last_prune = now
            prune(conn, now)


def prune(conn: sqlite3.Connection, now: float):
    cutoff = now - DEBUG_KEEP_S
    conn.execute("DELETE FROM heatmap_devices WHERE ts < ?", (cutoff,))
    conn.execute("DELETE FROM heatmap_tracks WHERE last_ts < ?", (cutoff,))
    # Board messages keep their anonymous RSSI list (readings.rssis) but lose the device ids
    conn.execute("UPDATE readings SET data = json_remove(data, '$.devices') "
                 "WHERE measured_at < ? AND json_extract(data, '$.devices') IS NOT NULL", (cutoff,))


def after_ingest(devices: set[str]):
    """Called after board messages are stored: build snapshots that are now complete."""
    try:
        with db() as conn:
            boards = set(load_config(conn)["boards"])
        if devices & boards:
            tick()
    except Exception:
        log.exception("heatmap snapshot failed")


async def background_loop():
    while True:
        try:
            await run_in_threadpool(tick)
        except Exception:
            log.exception("heatmap tick failed")
        await asyncio.sleep(TICK_S)


def check_single_board_change(conn: sqlite3.Connection, device: str, update: dict):
    """Heatmap boards must share interval_s and window_s, or their ts won't line up."""
    boards = load_config(conn)["boards"]
    if device not in boards:
        return
    for field in ("interval_s", "window_s"):
        if field in update:
            for other in boards:
                if other != device and get_config(conn, other)[field] != update[field]:
                    raise HTTPException(422, f"heatmap boards must share {field}: change it for all of them "
                                             "with PUT /heatmap/boards")


def people_buckets(conn: sqlite3.Connection, start: float, end: float, step_s: float) -> list[float | None]:
    """Mean people_estimate per bucket (for /heatmap/history and live rooms with device 'heatmap')."""
    n = math.ceil((end - start) / step_s)
    sums, counts = [0.0] * n, [0] * n
    for r in conn.execute("SELECT ts, people_estimate FROM heatmap_snapshots WHERE ts >= ? AND ts < ?", (start, end)):
        i = int((r["ts"] - start) // step_s)
        sums[i] += r["people_estimate"]
        counts[i] += 1
    return [sums[i] / counts[i] if counts[i] else None for i in range(n)]


# ---------------------------------------------------------------- endpoints

@router.get("/config")
def read_config():
    with db() as conn:
        return load_config(conn)


@router.put("/config")
def update_config(patch: Any = Body(...)):
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        new = validate_patch(patch, load_config(conn))
        save_config(conn, new)
        return load_config(conn)


@router.put("/mode")
def set_mode(body: Any = Body(...)):
    """{"mode": "live"} (send every interval, needed for the heatmap) or {"mode": "dev"} (every 5 min)."""
    if not isinstance(body, dict) or body.get("mode") not in MODES or set(body) != {"mode"}:
        raise HTTPException(422, 'body must be {"mode": "live"} or {"mode": "dev"}')
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        boards = list(load_config(conn)["boards"])
        configs = {b: apply_changes(conn, b, MODES[body["mode"]]) for b in boards}
    return {"mode": body["mode"], "boards": configs}


@router.put("/boards")
def update_boards(changes: Any = Body(...)):
    """The same partial board config for every heatmap board (as PUT /config/{device})."""
    update = validate_changes(changes)
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        boards = list(load_config(conn)["boards"])
        return {"boards": {b: apply_changes(conn, b, update) for b in boards}}


@router.post("/calibrate/headcount")
def calibrate_headcount(body: Any = Body(...)):
    """{"people": n} counted in the room now: devices_per_person = mean in-room devices (last 60 s) / n."""
    people = body.get("people") if isinstance(body, dict) else None
    if not is_number(people) or not 0 < people <= 10000:
        raise HTTPException(422, 'body must be {"people": number > 0}')
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        latest = conn.execute("SELECT MAX(ts) FROM heatmap_snapshots").fetchone()[0]
        if latest is None:
            raise HTTPException(409, "no heatmap snapshots yet")
        counts = [r[0] for r in conn.execute(
            "SELECT devices_in_room FROM heatmap_snapshots WHERE ts > ?", (latest - 60,))]
        mean = statistics.fmean(counts)
        cfg = load_config(conn)
        cfg["people"]["devices_per_person"] = round(min(4.0, max(0.5, mean / people)), 3)
        save_config(conn, cfg)
    return {"devices_per_person": cfg["people"]["devices_per_person"], "devices_in_room_mean": round(mean, 2),
            "snapshots": len(counts)}


def board_status(conn: sqlite3.Connection, cfg: dict, snap_boards: dict, now: float) -> dict:
    pos = board_positions(cfg)
    out = {}
    for b, spec in cfg["boards"].items():
        bc = get_config(conn, b)
        last = conn.execute(
            "SELECT received_at, measured_at, json_extract(data, '$.config_version') AS v FROM readings "
            "WHERE device = ? ORDER BY id DESC LIMIT 1", (b,)).fetchone()
        period = bc["send_every_s"] or bc["interval_s"]
        online = bool(last) and now - parse_time(last["received_at"]) <= 3 * period + 15
        applied = int(last["v"]) if last and last["v"] is not None else None
        out[b] = {"corner": spec.get("corner"), "x": round(pos[b][0], 2), "y": round(pos[b][1], 2),
                  "online": online, "last_ts": iso(last["measured_at"]) if last else None,
                  "heard": snap_boards.get(b, {}).get("heard", 0),
                  "config": {"version": bc["config_version"], "applied_version": applied,
                             "pending": applied is None or applied < bc["config_version"],
                             "threshold_dbm": bc["threshold_dbm"], "send_every_s": bc["send_every_s"],
                             "interval_s": bc["interval_s"], "window_s": bc["window_s"]}}
    return out


@router.get("/latest")
def latest():
    now = time.time()
    with db() as conn:
        cfg = load_config(conn)
        snap = conn.execute("SELECT * FROM heatmap_snapshots ORDER BY ts DESC LIMIT 1").fetchone()
        boards = board_status(conn, cfg, json.loads(snap["boards"]) if snap else {}, now)
        smoothing = cfg["heatmap"]["smoothing_s"]
        recent = [r[0] for r in conn.execute("SELECT people_estimate FROM heatmap_snapshots WHERE ts > ? AND ts <= ?",
                                             (snap["ts"] - smoothing, snap["ts"]))] if snap and smoothing else []
    mode = "live" if all(b["config"]["send_every_s"] == 0 for b in boards.values()) else "dev"
    warnings = []
    offline = [b for b, s in boards.items() if not s["online"]]
    for b in offline:
        warnings.append(f"{b} offline: using {len(boards) - len(offline)} boards")
    if snap:
        in_snap = json.loads(snap["boards"])
        missing = [b for b in boards if b not in offline and not in_snap.get(b, {}).get("message")]
        for b in missing:
            warnings.append(f"{b} missing from this snapshot: using {len(boards) - len(missing) - len(offline)} boards")
    if mode == "dev":
        warnings.append("dev mode: boards send every few minutes, so the heatmap can be minutes old "
                        "(PUT /heatmap/mode {\"mode\": \"live\"})")
    if len({(b["config"]["interval_s"], b["config"]["window_s"]) for b in boards.values()}) > 1:
        warnings.append("boards have different interval_s/window_s: set them together with PUT /heatmap/boards")
    if any(b["config"]["pending"] for b in boards.values()):
        warnings.append("waiting for boards to apply new settings: "
                        + ", ".join(b for b, s in boards.items() if s["config"]["pending"]))
    calibration = {"edge_mode": cfg["filter"]["edge_mode"], "edge_dbm": cfg["filter"]["edge_dbm"],
                   "offsets_mode": cfg["offsets_mode"], "offsets_db": cfg["offsets_db"]}
    room = {"width_m": cfg["room"]["width_m"], "length_m": cfg["room"]["length_m"]}
    if not snap:
        warnings.append("no snapshot yet: waiting for board data")
        return {"ts": None, "age_s": None, "mode": mode, "people_estimate": None, "people_smoothed": None,
                "devices_in_room": None, "devices_heard": None, "funnel": None, "kinds_in_room": None,
                "boards": boards, "room": room,
                "calibration": calibration, "heatmap": None, "warnings": warnings}
    grid = json.loads(snap["grid"])
    smoothed = statistics.median(recent) if recent else snap["people_estimate"]
    return {
        "ts": iso(snap["ts"]),
        "age_s": round(now - snap["ts"]),
        "mode": mode,
        "people_estimate": round(snap["people_estimate"], 2),
        "people_smoothed": math.floor(smoothed + 0.5),
        "devices_in_room": snap["devices_in_room"],
        "devices_heard": snap["devices_heard"],
        "funnel": json.loads(snap["funnel"]),
        "kinds_in_room": json.loads(snap["kinds"]) if snap["kinds"] else {},
        "boards": boards,
        "room": room,
        "calibration": calibration,
        "heatmap": {"cell_m": cfg["heatmap"]["cell_m"], "cols": len(grid[0]) if grid else 0, "rows": len(grid),
                    "max_value": max((max(r) for r in grid), default=0.0), "values": grid},
        "warnings": warnings,
    }


@router.get("/debug")
def debug(ts: str | None = Query(default=None, description="a snapshot ts (default: the latest)")):
    with db() as conn:
        if ts:
            t = parse_time(ts)
            if t is None:
                raise HTTPException(422, "ts must be an ISO time")
            snap = conn.execute("SELECT ts, filter FROM heatmap_snapshots WHERE ts <= ? ORDER BY ts DESC LIMIT 1",
                                (t,)).fetchone()
        else:
            snap = conn.execute("SELECT ts, filter FROM heatmap_snapshots ORDER BY ts DESC LIMIT 1").fetchone()
        if not snap:
            raise HTTPException(404, "no heatmap snapshot")
        rows = conn.execute(
            "SELECT * FROM heatmap_devices WHERE ts = ? ORDER BY status = 'in_room' DESC, weakest_dbm DESC",
            (snap["ts"],)).fetchall()
    used = json.loads(snap["filter"])
    return {"ts": iso(snap["ts"]),
            "filter": {k: used[k] for k in ("exclude_kinds", "stationary_h", "edge_dbm", "min_boards", "presence_s")
                       if k in used},
            "offsets_db": used["offsets_db"],
            "devices": [{"id": r["id"], "rssi": json.loads(r["rssi"]), "weakest_dbm": r["weakest_dbm"],
                         "boards_heard": r["boards_heard"], "x": r["x"], "y": r["y"], "kind": r["kind"] or "unknown",
                         "status": r["status"],
                         "reason": r["reason"]} for r in rows]}


@router.get("/history")
def history(from_: str | None = Query(default=None, alias="from"), to: str | None = None,
            step_s: int = Query(default=60, ge=10, le=86400)):
    """Mean people_estimate per bucket (1 decimal, null when empty), e.g. for the CO2 model."""
    start, end = time_range(from_, to, 3600, time.time())
    if (end - start) / step_s > 10000:
        raise HTTPException(422, "too many points; increase step_s or shorten the range")
    with db() as conn:
        values = people_buckets(conn, start, end, step_s)
    return [{"ts": iso(start + i * step_s), "people": None if v is None else round(v, 1)} for i, v in enumerate(values)]
