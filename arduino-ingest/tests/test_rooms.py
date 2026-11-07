"""Rooms, history, CO2 estimate and forecast. Run: ./run-tests"""

import math
import statistics
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app

KEY = {"X-API-Key": "test-key"}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_room(client, **fields):
    body = {"name": "Test room", "volume_m3": 500, "windows": [{"name": "W1", "area_m2": 1.0}], **fields}
    r = client.post("/rooms", json=body, headers=KEY)
    assert r.status_code == 201, r.text
    return r.json()


def flat_history(client, room_id, people, hours):
    now = time.time()
    pts = [{"ts": iso(now - i * 300), "people": people} for i in range(int(hours * 12) + 1)]
    assert client.post(f"/rooms/{room_id}/history", json={"points": pts}, headers=KEY).status_code == 201


def test_seed_rooms_and_synthetic_history(client):
    rooms = {r["id"]: r for r in client.get("/rooms", headers=KEY).json()}
    assert {"appleton", "forum", "personal"} <= set(rooms)
    a = rooms["appleton"]
    assert a["device"] == "uno-q-1" and a["volume_m3"] == 65 and len(a["windows"]) == 2
    assert a["history"]["points"] == 20160 and a["history"]["enough_for_forecast"]
    assert rooms["forum"]["history"]["points"] == 0


def test_room_crud_and_validation(client):
    for bad in [{"name": "x", "windows": []},  # no volume
                {"name": "x", "volume_m3": 50, "windows": [{"area_m2": 0}]},
                {"name": "x", "volume_m3": 50, "windows": [], "colour": "red"},
                {"name": "x", "volume_m3": 50, "windows": [], "capacity": 2.5},
                {"name": "x", "volume_m3": 50, "windows": [], "timezone": "Mars/Base"}]:
        assert client.post("/rooms", json=bad, headers=KEY).status_code == 422, bad
    r = make_room(client, name="Lab Zone Ü 1")
    assert r["id"] == "lab-zone-u-1" and r["capacity"] == 32 and r["co2_gen_lph"] == 16
    assert make_room(client, name="Lab Zone Ü 1")["id"] == "lab-zone-u-1-2"
    assert client.post("/rooms", json={"id": "lab-zone-u-1", "name": "x", "volume_m3": 9, "windows": []},
                       headers=KEY).status_code == 409
    up = client.put("/rooms/lab-zone-u-1", json={"capacity": 12, "device": "uno-q-9"}, headers=KEY).json()
    assert up["capacity"] == 12 and up["device"] == "uno-q-9" and up["name"] == "Lab Zone Ü 1"
    assert client.put("/rooms/lab-zone-u-1", json={"volume_m3": -1}, headers=KEY).status_code == 422
    assert client.delete("/rooms/lab-zone-u-1", headers=KEY).status_code == 200
    assert client.get("/rooms/lab-zone-u-1", headers=KEY).status_code == 404


def test_windows(client):
    r = make_room(client, id="win-test", windows=[{"area_m2": 1.0}, {"area_m2": 0.5}, {"area_m2": 2.0}])
    assert r["open_area_m2"] == 0 and not any(w["is_open"] for w in r["windows"])
    r = client.put("/rooms/win-test/windows", json={"open": [0, 2]}, headers=KEY).json()
    assert [w["is_open"] for w in r["windows"]] == [True, False, True] and r["open_area_m2"] == 3.0
    assert client.put("/rooms/win-test/windows", json={"open": [3]}, headers=KEY).status_code == 422
    r = client.put("/rooms/win-test/windows", json={"open": []}, headers=KEY).json()
    assert r["open_area_m2"] == 0


def test_co2_reaches_mass_balance_steady_state(client):
    # V=500 m3, 2 ACH -> Q=1000 m3/h, time constant 30 min; 8 people * 16 L/h -> 420 + 128 ppm
    make_room(client, id="co2-test", infiltration_ach=2.0)
    flat_history(client, "co2-test", 8, 30)
    pts = client.get("/rooms/co2-test/co2", headers=KEY).json()["points"]
    assert abs(pts[-1]["co2_ppm"] - 548) <= 1
    assert pts[-2]["people"] == 8 and pts[-2]["people_source"] == "history"  # last point: the future 5 min

    # Opening the 1 m2 window adds 350 m3/h -> steady state 420 + 128 / 1.35
    client.put("/rooms/co2-test/windows", json={"open": [0]}, headers=KEY)
    f = client.get("/rooms/co2-test/forecast", headers=KEY).json()
    assert f["model"] == "persistence_no_history" and f["live_people"] == 8
    assert abs(f["summary"]["co2_now"] - 548) <= 1 and f["summary"]["open_area_m2"] == 1.0
    end = f["points"][-1]
    assert abs(end["co2_ppm"] - (420 + 128 / 1.35)) <= 1
    assert abs(end["co2_windows_closed"] - 548) <= 1
    assert end["co2_windows_open"] == end["co2_ppm"]


def test_co2_matches_frontend_formula(client):
    # One step of the frontend's stepMassBalanceCO2(1280, 8, 65, 16.25, 1/60, 16) with outdoor 420
    from app.rooms import step_co2
    room = {"volume_m3": 65.0, "co2_gen_lph": 16.0, "outdoor_ppm": 420.0}
    q, dt = 0.25 * 65, 1 / 60
    decay = math.exp(-(q / 65) * dt)
    expected = 420 + (8 * 16 / q) * 1000 * (1 - decay) + (1280 - 420) * decay
    assert step_co2(1280, 8, room, q, dt) == pytest.approx(expected)


def test_occupancy_prefers_sensor_over_history(client):
    make_room(client, id="merge-test", device="merge-board")
    flat_history(client, "merge-test", 3, 1)
    client.post("/data", json={"device": "merge-board", "data": {"people": 11}}, headers=KEY)
    pts = client.get(f"/rooms/merge-test/occupancy?from={iso(time.time() - 1800)}", headers=KEY).json()["points"]
    assert pts[-1] == {"ts": pts[-1]["ts"], "people": 11.0, "source": "sensor"}
    assert pts[0]["source"] == "history" and pts[0]["people"] == 3


def test_forecast_uses_forecaster_and_weekday_override(client):
    f = client.get("/rooms/appleton/forecast?as_weekday=wed", headers=KEY).json()
    assert len(f["points"]) == 288 and f["model"].startswith("historical_time_of_week")
    assert f["as_weekday"] == "wed"
    sat = client.get("/rooms/appleton/forecast?as_weekday=saturday", headers=KEY).json()
    assert statistics.mean(p["people"] for p in f["points"]) > 3 * statistics.mean(p["people"] for p in sat["points"])
    assert all(0 <= p["people_lower"] <= p["people"] <= p["people_upper"] <= 32 for p in f["points"])
    t0 = datetime.fromisoformat(f["points"][0]["ts"].replace("Z", "+00:00")).timestamp()
    assert 0 < t0 - time.time() <= 300 and f["points"][1]["ts"] > f["points"][0]["ts"]
    assert client.get("/rooms/appleton/forecast?hours=2", headers=KEY).json()["points"].__len__() == 24
    assert client.get("/rooms/appleton/forecast?as_weekday=funday", headers=KEY).status_code == 422


def test_forecast_live_anchor_fades(client):
    client.put("/rooms/appleton", json={"device": "anchor-board"}, headers=KEY)
    try:
        base = client.get("/rooms/appleton/forecast?as_weekday=sat", headers=KEY).json()
        client.post("/data", json={"device": "anchor-board", "data": {"people": 30}}, headers=KEY)
        f = client.get("/rooms/appleton/forecast?as_weekday=sat", headers=KEY).json()
        assert f["model"] == "historical_time_of_week+live_anchor" and f["live_people"] == 30
        assert abs(f["points"][0]["people"] - 30) < 2  # starts at the live count
        assert abs(f["points"][-1]["people"] - base["points"][-1]["people"]) < 0.5  # back to the pattern in 24 h
    finally:
        client.put("/rooms/appleton", json={"device": "uno-q-1"}, headers=KEY)


def test_history_import_and_delete(client):
    make_room(client, id="hist-test")
    r = client.post("/rooms/hist-test/history", json={"source": "survey", "points": [
        {"ts": "2026-11-02T09:00:00Z", "people": 4}, {"ts": "2026-11-02T09:05:00Z", "people": 6}]}, headers=KEY)
    assert r.json() == {"stored": 2}
    h = client.get("/rooms/hist-test/history?from=2026-11-02T00:00:00Z&to=2026-11-03T00:00:00Z", headers=KEY).json()
    assert [p["people"] for p in h["points"]] == [4, 6] and h["points"][0]["source"] == "survey"
    assert client.post("/rooms/hist-test/history", json={"points": [{"ts": "nope", "people": 1}]},
                       headers=KEY).status_code == 422
    assert client.delete("/rooms/hist-test/history?source=survey", headers=KEY).json() == {"deleted": 2}


@pytest.mark.parametrize("method,path", [
    ("get", "/rooms"), ("post", "/rooms"), ("get", "/rooms/appleton"), ("put", "/rooms/appleton"),
    ("delete", "/rooms/appleton"), ("put", "/rooms/appleton/windows"), ("get", "/rooms/appleton/history"),
    ("post", "/rooms/appleton/history"), ("get", "/rooms/appleton/occupancy"), ("get", "/rooms/appleton/co2"),
    ("get", "/rooms/appleton/forecast"),
])
def test_rooms_auth_required(client, method, path):
    kwargs = {"json": {}} if method in ("post", "put") else {}
    assert client.request(method, path, **kwargs).status_code == 401


def test_cors_preflight_allows_frontend(client):
    r = client.options("/rooms", headers={"Origin": "http://localhost:8080", "Access-Control-Request-Method": "GET",
                                          "Access-Control-Request-Headers": "x-api-key"})
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] == "*"
