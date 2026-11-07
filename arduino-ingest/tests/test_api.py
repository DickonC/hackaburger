"""Acceptance checks from docs/server-agent-brief.md.

Run: ./run-tests   (uses a throwaway database inside a container)
"""

import os
import random
import tempfile
import time
from datetime import datetime, timedelta, timezone

os.environ["API_KEY"] = "test-key"
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")

import pytest
from fastapi.testclient import TestClient

from app.main import app

KEY = {"X-API-Key": "test-key"}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def dev(id_, rssi, n=1):
    return {"id": id_, "rssi": rssi, "n": n}


EXAMPLE = [dev("a3f9", -52.0, 9), dev("7c41", -61.5, 4), dev("11aa", -75.0), dev("22bb", -75.5), dev("e09b", -93.0)]


def test_old_format_still_works(client):
    r = client.post("/data", json={"device": "uno-q-1", "data": {"people": 7}}, headers=KEY)
    assert r.status_code == 201
    body = r.json()
    assert body["stored"] == 1
    assert set(body["config"]) == {"config_version", "enabled", "threshold_dbm", "window_s",
                                   "interval_s", "people_factor", "send_devices", "display"}
    rows = client.get("/data?device=uno-q-1&limit=1", headers=KEY).json()
    assert rows[0]["data"] == {"people": 7}
    latest = client.get("/occupancy/latest?device=uno-q-1", headers=KEY).json()
    assert latest["people"] == 7 and latest["source"] == "board"


def test_initial_configs(client):
    c1 = client.get("/config/uno-q-1", headers=KEY).json()
    c2 = client.get("/config/uno-q-2", headers=KEY).json()
    c3 = client.get("/config/other-board", headers=KEY).json()
    assert (c1["threshold_dbm"], c1["window_s"], c1["interval_s"]) == (-75, 30, 5)
    assert (c2["threshold_dbm"], c2["window_s"], c2["interval_s"]) == (-40, 6, 1)
    assert c3 == {"config_version": 1, "enabled": True, "threshold_dbm": -75, "window_s": 30,
                  "interval_s": 5, "people_factor": 0.7, "send_devices": True, "display": True}


def test_worked_example_and_recount(client):
    t = datetime(2026, 11, 7, 12, 0, 30, tzinfo=timezone.utc)
    r = client.post("/data", json={"device": "example", "ts": iso(t), "data": {"devices": EXAMPLE}}, headers=KEY)
    assert r.status_code == 201
    latest = client.get("/occupancy/latest?device=example", headers=KEY).json()
    assert latest["devices_in_range"] == 3 and latest["people"] == 2 and latest["source"] == "server"
    assert latest["heard_total"] == 5

    q = "/occupancy/history?device=example&from=2026-11-07T12:00:00Z&to=2026-11-07T12:02:00Z&step_s=60"
    pts = client.get(q, headers=KEY).json()["points"]
    assert pts == [{"ts": "2026-11-07T12:00:00Z", "people": 2.1}, {"ts": "2026-11-07T12:01:00Z", "people": None}]
    pts = client.get(q + "&threshold_dbm=-95", headers=KEY).json()["points"]
    assert pts[0]["people"] == 3.5  # 5 devices * 0.7; rounds (half up) to 4 people


def test_config_change_reaches_only_that_board(client):
    before = client.get("/config/uno-q-1", headers=KEY).json()
    r = client.put("/config/uno-q-1", json={"threshold_dbm": -40}, headers=KEY)
    assert r.status_code == 200
    assert r.json()["config_version"] == before["config_version"] + 1
    reply = client.post("/data", json={"device": "uno-q-1", "data": {"people": 1}}, headers=KEY).json()
    assert reply["config"]["threshold_dbm"] == -40
    reply = client.post("/data", json={"device": "uno-q-2", "data": {"people": 1}}, headers=KEY).json()
    assert reply["config"]["threshold_dbm"] == -40 and reply["config"]["config_version"] == 1

    r = client.put("/config/uno-q-1", json={"preset": "room"}, headers=KEY).json()
    assert (r["threshold_dbm"], r["window_s"], r["interval_s"]) == (-75, 30, 5)


@pytest.mark.parametrize("body", [
    {"threshold_dbm": -20}, {"foo": 1}, {"config_version": 9}, {"enabled": 1},
    {"threshold_dbm": -40.5}, {"interval_s": True}, {"preset": "party"}, {},
])
def test_config_validation(client, body):
    before = client.get("/config/uno-q-1", headers=KEY).json()
    assert client.put("/config/uno-q-1", json=body, headers=KEY).status_code == 422
    assert client.get("/config/uno-q-1", headers=KEY).json() == before


def test_invalid_device_items_are_dropped(client):
    devices = [dev("ok", -50), {"id": "bad", "rssi": 5, "n": 1}]
    r = client.post("/data", json={"device": "drop-test", "data": {"devices": devices}}, headers=KEY)
    assert r.status_code == 201 and r.json()["dropped"] == 1


@pytest.mark.parametrize("body", [
    {"device": "", "data": {}}, {"device": "has space", "data": {}}, {"device": "x" * 65, "data": {}},
    {"device": "x", "data": {"devices": "nope"}}, {"device": "x", "data": {"devices": [dev("a", -50)] * 5001}},
])
def test_ingest_validation(client, body):
    assert client.post("/data", json=body, headers=KEY).status_code == 422


def test_large_message_is_fast(client):
    devices = [dev(f"{i:016x}", random.uniform(-100, -30), random.randint(1, 20)) for i in range(500)]
    t0 = time.perf_counter()
    r = client.post("/data", json={"device": "big", "data": {"devices": devices}}, headers=KEY)
    assert r.status_code == 201
    assert time.perf_counter() - t0 < 0.3


def test_body_too_large(client):
    r = client.post("/data", content=b"x" * (256 * 1024 + 1), headers={**KEY, "Content-Type": "application/json"})
    assert r.status_code == 413


def test_online_status(client):
    client.put("/config/online-test", json={"interval_s": 1}, headers=KEY)
    client.post("/data", json={"device": "online-test", "data": {"people": 1}}, headers=KEY)
    assert client.get("/occupancy/latest?device=online-test", headers=KEY).json()["online"] is True
    time.sleep(8.2)  # 3 * 1 + 5 s
    assert client.get("/occupancy/latest?device=online-test", headers=KEY).json()["online"] is False


def test_resent_message_lands_in_its_own_bucket(client):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    old = now - timedelta(minutes=10)
    client.post("/data", json={"device": "resend", "ts": iso(old + timedelta(seconds=5)),
                               "data": {"devices": EXAMPLE}}, headers=KEY)
    pts = client.get(f"/occupancy/history?device=resend&from={iso(old)}&to={iso(now)}&step_s=60",
                     headers=KEY).json()["points"]
    assert pts[0] == {"ts": iso(old), "people": 2.1}
    assert all(p["people"] is None for p in pts[1:])


def test_smoothing_uses_median_unless_demo_mode(client):
    t = datetime(2026, 11, 7, 13, 0, 0, tzinfo=timezone.utc)
    for i, k in enumerate([10, 10, 30]):  # room mode: median of 7, 7, 21 people_raw -> 7
        devices = [dev(f"{j}", -50) for j in range(k)]
        client.post("/data", json={"device": "smooth", "ts": iso(t + timedelta(seconds=5 * i)),
                                   "data": {"devices": devices}}, headers=KEY)
    latest = client.get("/occupancy/latest?device=smooth", headers=KEY).json()
    assert latest["people"] == 21 and latest["people_smoothed"] == 7
    client.put("/config/smooth", json={"preset": "demo"}, headers=KEY)
    latest = client.get("/occupancy/latest?device=smooth", headers=KEY).json()
    assert latest["people_smoothed"] == latest["people"]  # demo threshold doesn't recount stored rows


@pytest.mark.parametrize("method,path", [
    ("get", "/data"), ("post", "/data"), ("get", "/config/uno-q-1"), ("put", "/config/uno-q-1"),
    ("get", "/occupancy/latest?device=uno-q-1"), ("get", "/occupancy/history?device=uno-q-1"),
])
def test_auth_required(client, method, path):
    kwargs = {} if method == "get" else {"json": {}}
    assert client.request(method, path, **kwargs).status_code == 401
    assert client.request(method, path, headers={"X-API-Key": "wrong"}, **kwargs).status_code == 401
