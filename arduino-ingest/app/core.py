"""Shared helpers: database, time handling, auth and people bucketing."""

import json
import math
import os
import re
import secrets
import sqlite3
import statistics
from datetime import datetime, timezone
from typing import Any

from fastapi import Header, HTTPException

DB_PATH = os.environ.get("DB_PATH", "/data/ingest.db")
API_KEY = os.environ.get("API_KEY", "")
DEVICE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def parse_time(s: str | None) -> float | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def round_half_up(x: float | None) -> int | None:
    return None if x is None else math.floor(x + 0.5)


def is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def require_key(x_api_key: str = Header(default="")):
    if not secrets.compare_digest(x_api_key, API_KEY):
        raise HTTPException(status_code=401, detail="invalid API key")


def check_device(device: Any) -> str:
    if not isinstance(device, str) or not DEVICE_RE.match(device):
        raise HTTPException(422, "device must be 1-64 characters of [A-Za-z0-9_-]")
    return device


def time_range(from_: str | None, to: str | None, default_s: float, now: float) -> tuple[float, float]:
    end = parse_time(to) if to else now
    start = parse_time(from_) if from_ else (end - default_s if end else None)
    if start is None or end is None:
        raise HTTPException(422, "from/to must be ISO 8601 timestamps")
    if end <= start:
        raise HTTPException(422, "to must be after from")
    return start, end


def count(rssis: list[float], threshold: float, factor: float) -> tuple[int, float]:
    in_range = sum(1 for r in rssis if r >= threshold)
    return in_range, round(in_range * factor, 6)


def bucket_people(conn: sqlite3.Connection, device: str, start: float, end: float, step_s: float,
                  threshold_dbm: int | None = None) -> list[float | None]:
    """Mean unrounded people estimate of a board's readings per step_s bucket, by measurement time.

    With threshold_dbm, every message is recounted at that range (messages without devices are skipped).
    """
    n_buckets = math.ceil((end - start) / step_s)
    rows = conn.execute(
        """SELECT measured_at, people_raw, people_factor, rssis FROM readings
           WHERE device = ? AND measured_at >= ? AND measured_at < ?""",
        (device, start, end),
    ).fetchall()
    buckets: list[list[float]] = [[] for _ in range(n_buckets)]
    for r in rows:
        if threshold_dbm is None:
            value = r["people_raw"]
        elif r["rssis"] is not None:
            value = count(json.loads(r["rssis"]), threshold_dbm, r["people_factor"])[1]
        else:
            value = None  # can't recount without the raw device list
        if value is not None:
            buckets[int((r["measured_at"] - start) // step_s)].append(value)
    return [statistics.fmean(b) if b else None for b in buckets]
