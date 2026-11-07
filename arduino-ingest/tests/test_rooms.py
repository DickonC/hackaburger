"""Live rooms: CRUD, windows, board occupancy and estimated CO2. Run: ./run-tests"""

import math
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


def board_history(client, device, people, hours):
    """The board reported `people` every 5 minutes for the last `hours`."""
    now = time.time()
    msgs = [{"device": device, "ts": iso(now - i * 300), "data": {"people": people}} for i in range(int(hours * 12) + 1)]
    for i in range(0, len(msgs), 500):
        assert client.post("/data", json=msgs[i:i + 500], headers=KEY).status_code == 201


def test_seed_rooms(client):
    rooms = {r["id"]: r for r in client.get("/rooms", headers=KEY).json()}
    assert {"appleton", "forum", "personal"} <= set(rooms) and not any(i.startswith("aalborg") for i in rooms)
    a = rooms["appleton"]
    assert a["device"] == "uno-q-1" and a["volume_m3"] == 65 and len(a["windows"]) == 2 and "model_from" not in a


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
    assert client.put("/rooms/lab-zone-u-1", json={"model_from": "aalborg-a"}, headers=KEY).status_code == 422
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
    make_room(client, id="co2-test", infiltration_ach=2.0, device="co2-board")
    board_history(client, "co2-board", 8, 20)
    r = client.get("/rooms/co2-test/co2", headers=KEY).json()
    assert r["estimated"] and abs(r["points"][-2]["co2_ppm"] - 548) <= 1 and r["points"][-2]["people"] == 8
    assert abs(r["now"]["co2_ppm"] - 548) <= 1 and r["now"]["people"] == 8 and r["now"]["airflow_m3h"] == 1000

    # Opening the 1 m2 window adds 350 m3/h
    client.put("/rooms/co2-test/windows", json={"open": [0]}, headers=KEY)
    assert client.get("/rooms/co2-test/co2", headers=KEY).json()["now"]["airflow_m3h"] == 1350


def test_co2_counts_rooms_without_board_data_as_empty(client):
    make_room(client, id="no-board")
    r = client.get("/rooms/no-board/co2", headers=KEY).json()
    assert r["now"]["co2_ppm"] == 420 and r["now"]["people"] is None
    assert all(p["people"] is None and p["co2_ppm"] == 420 for p in r["points"])


def test_live_rooms_have_no_forecast(client):
    assert client.get("/rooms/appleton/forecast", headers=KEY).status_code in (404, 405)
    assert client.get("/rooms/appleton/history", headers=KEY).status_code in (404, 405)


def test_co2_matches_frontend_formula(client):
    # One step of the frontend's stepMassBalanceCO2(1280, 8, 65, 16.25, 1/60, 16) with outdoor 420
    from app.rooms import step_co2
    room = {"volume_m3": 65.0, "co2_gen_lph": 16.0, "outdoor_ppm": 420.0}
    q, dt = 0.25 * 65, 1 / 60
    decay = math.exp(-(q / 65) * dt)
    expected = 420 + (8 * 16 / q) * 1000 * (1 - decay) + (1280 - 420) * decay
    assert step_co2(1280, 8, room, q, dt) == pytest.approx(expected)


def test_occupancy_is_board_only(client):
    make_room(client, id="occ-test", device="occ-board")
    client.post("/data", json={"device": "occ-board", "data": {"people": 11}}, headers=KEY)
    pts = client.get(f"/rooms/occ-test/occupancy?from={iso(time.time() - 1800)}", headers=KEY).json()["points"]
    assert pts[-1]["people"] == 11.0 and pts[0]["people"] is None


@pytest.mark.parametrize("method,path", [
    ("get", "/rooms"), ("post", "/rooms"), ("get", "/rooms/appleton"), ("put", "/rooms/appleton"),
    ("delete", "/rooms/appleton"), ("put", "/rooms/appleton/windows"), ("get", "/rooms/appleton/occupancy"),
    ("get", "/rooms/appleton/co2"),
])
def test_rooms_auth_required(client, method, path):
    kwargs = {"json": {}} if method in ("post", "put") else {}
    assert client.request(method, path, **kwargs).status_code == 401


def test_cors_preflight_allows_frontend(client):
    r = client.options("/rooms", headers={"Origin": "http://localhost:8080", "Access-Control-Request-Method": "GET",
                                          "Access-Control-Request-Headers": "x-api-key"})
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] == "*"
