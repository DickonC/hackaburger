"""Board settings (configs table): defaults, validation and versioned updates.

The board picks up its config from the reply to its next POST /data; every change bumps
config_version, and the board reports the version it measured with (data.config_version).
"""

import json
import sqlite3
from typing import Any

from fastapi import HTTPException

DEFAULT_CONFIG = {
    "config_version": 1,
    "enabled": True,
    "threshold_dbm": -75,
    "window_s": 30,
    "interval_s": 5,
    "people_factor": 0.7,
    "send_devices": True,
    "display": True,
    "send_every_s": 300,  # POST on clock boundaries every N s; 0 = every interval_s (live)
}
PRESETS = {
    "room": {"threshold_dbm": -75, "window_s": 30, "interval_s": 5, "send_every_s": 300},
    "demo": {"threshold_dbm": -40, "window_s": 6, "interval_s": 1, "send_every_s": 0},
    "venue": {"threshold_dbm": -100, "window_s": 30, "interval_s": 5, "send_every_s": 300},
}

# field -> (types, min, max); bool is checked separately because bool is an int subclass
CONFIG_FIELDS = {
    "enabled": (bool, None, None),
    "threshold_dbm": (int, -100, -30),
    "window_s": (int, 2, 300),
    "interval_s": ((int, float), 1, 600),
    "send_every_s": (int, 0, 3600),
    "people_factor": ((int, float), 0.1, 2.0),
    "send_devices": (bool, None, None),
    "display": (bool, None, None),
}


def get_config(conn: sqlite3.Connection, device: str) -> dict:
    row = conn.execute("SELECT config FROM configs WHERE device = ?", (device,)).fetchone()
    if row:
        return {**DEFAULT_CONFIG, **json.loads(row["config"])}
    cfg = dict(DEFAULT_CONFIG)
    conn.execute("INSERT OR IGNORE INTO configs (device, config) VALUES (?, ?)", (device, json.dumps(cfg)))
    return cfg


def validate_changes(changes: Any) -> dict:
    """A partial config (or {"preset": ...}) -> the fields to set. 422 on anything invalid; no clamping."""
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
    return update


def apply_changes(conn: sqlite3.Connection, device: str, update: dict) -> dict:
    """Store the new fields and bump config_version (call inside a write transaction)."""
    cfg = get_config(conn, device)
    cfg.update(update)
    cfg["config_version"] += 1
    conn.execute("UPDATE configs SET config = ? WHERE device = ?", (json.dumps(cfg), device))
    return cfg
