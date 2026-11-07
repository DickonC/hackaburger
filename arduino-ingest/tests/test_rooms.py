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


def office_history(client, room_id, weeks=5, people=6):
    """Known pattern in the room's local time: `people` on weekdays 09:00-17:00, else 0.

    Ends a day ago, so the history doesn't count as a current (live) reading.
    """
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(client.get(f"/rooms/{room_id}", headers=KEY).json()["timezone"])
    end = math.floor(time.time() / 300) * 300 - 86400
    pts = []
    for i in range(weeks * 2016):
        t = end - i * 300
        local = datetime.fromtimestamp(t, tz)
        busy = local.weekday() < 5 and 9 <= local.hour < 17
        pts.append({"ts": iso(t), "people": people if busy else 0})
    for i in range(0, len(pts), 2000):  # stay under the 256 KB request limit
        assert client.post(f"/rooms/{room_id}/history", json={"points": pts[i:i + 2000]}, headers=KEY).status_code == 201


def local_hour(ts: str, tz: str) -> tuple[int, int]:
    from zoneinfo import ZoneInfo
    d = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(ZoneInfo(tz))
    return d.weekday(), d.hour


def flat_history(client, room_id, people, hours):
    now = time.time()
    pts = [{"ts": iso(now - i * 300), "people": people} for i in range(int(hours * 12) + 1)]
    assert client.post(f"/rooms/{room_id}/history", json={"points": pts}, headers=KEY).status_code == 201


def test_seed_rooms_have_no_synthetic_history(client):
    rooms = {r["id"]: r for r in client.get("/rooms", headers=KEY).json()}
    assert {"appleton", "forum", "personal"} <= set(rooms)
    a = rooms["appleton"]
    assert a["device"] == "uno-q-1" and a["volume_m3"] == 65 and len(a["windows"]) == 2
    assert a["history"]["points"] == 0 and a["model_from"] is None


def test_calendar_forecast_matches_local_wall_clock_and_scales(client):
    # History in Copenhagen; a London room borrows it with twice the capacity
    make_room(client, id="dk-office", timezone="Europe/Copenhagen", capacity=6)
    office_history(client, "dk-office")
    make_room(client, id="uk-room", timezone="Europe/London", capacity=12, model_from="dk-office")
    for room, tz, busy in (("dk-office", "Europe/Copenhagen", 6), ("uk-room", "Europe/London", 12)):
        f = client.get(f"/rooms/{room}/forecast?as_weekday=wed", headers=KEY).json()
        assert f["model"] == "historical_time_of_week" and f["models"]["source_room"] == "dk-office"
        assert len(f["points"]) == 288
        for p in f["points"]:
            _, h = local_hour(p["ts"], tz)
            if 11 <= h < 15:  # well inside 09-17 local, away from the smoothing at the edges
                assert abs(p["people"] - busy) < 0.01, (room, p)
            if h < 7 or h >= 19:
                assert p["people"] < 0.01, (room, p)
            assert 0 <= p["people_lower"] <= p["people"] <= p["people_upper"] <= busy


def test_weekday_override(client):
    sat = client.get("/rooms/dk-office/forecast?as_weekday=sat", headers=KEY).json()
    assert sat["as_weekday"] == "sat" and max(p["people"] for p in sat["points"]) < 0.01
    assert client.get("/rooms/dk-office/forecast?as_weekday=funday", headers=KEY).status_code == 422
    assert client.get("/rooms/forum/forecast?as_weekday=wed", headers=KEY).status_code == 409
    assert len(client.get("/rooms/dk-office/forecast?hours=2", headers=KEY).json()["points"]) == 24


def test_short_term_models_used_when_board_is_live(client):
    import sys
    from app.rooms import MODELS_DIR
    sys.path.insert(0, "/app/occupancy_forecaster")
    from occupancy_core import train_backtest_forecast
    import pandas as pd

    # Train the short-term models on dk-office's history, as app.dataset does
    pts = client.get(f"/rooms/dk-office/history?from={iso(time.time() - 40 * 86400)}", headers=KEY).json()["points"]
    idx = pd.to_datetime([p["ts"] for p in pts]).tz_convert("Europe/Copenhagen").tz_localize(None)
    series = pd.Series([p["people"] for p in pts], index=idx, dtype=float).resample("5min").median()
    train_backtest_forecast(series, "dk-office", 6, MODELS_DIR / "dk-office")
    assert client.get("/rooms/dk-office", headers=KEY).json()["history"]["short_term_model"]

    # No live board: calendar only
    client.put("/rooms/uk-room", json={"device": "uk-board"}, headers=KEY)
    assert client.get("/rooms/uk-room/forecast", headers=KEY).json()["model"] == "historical_time_of_week"

    # Board live with 9 people: the short-term models run for the first hour
    for _ in range(3):
        client.post("/data", json={"device": "uk-board", "data": {"people": 9}}, headers=KEY)
    f = client.get("/rooms/uk-room/forecast", headers=KEY).json()
    assert f["model"] == "short_term+historical_time_of_week" and f["live_people"] == 9
    assert f["models"]["short_term"] and set(f["models"]["short_term"]) <= {
        "persistence", "seasonal", "ridge_level", "ridge_delta", "boost_level", "boost_delta"}
    assert all(0 <= p["people"] <= 12 for p in f["points"])

    # A weekday override can't use live lags: falls back to the calendar + live anchor
    f = client.get("/rooms/uk-room/forecast?as_weekday=wed", headers=KEY).json()
    assert f["model"] == "historical_time_of_week+live_anchor" and abs(f["points"][0]["people"] - 9) < 1.5


def test_history_keeps_measured_co2(client):
    make_room(client, id="co2-hist")
    client.post("/rooms/co2-hist/history", json={"source": "lab", "points": [
        {"ts": "2026-11-02T09:00:00Z", "people": 2, "co2_ppm": 610.5}]}, headers=KEY)
    p = client.get("/rooms/co2-hist/history?from=2026-11-02T00:00:00Z&to=2026-11-03T00:00:00Z",
                   headers=KEY).json()["points"][0]
    assert p["co2_ppm"] == 610.5 and p["source"] == "lab"
    assert client.post("/rooms/co2-hist/history", json={"points": [{"ts": "2026-11-02T09:00:00Z", "people": 1,
                       "co2_ppm": "high"}]}, headers=KEY).status_code == 422


def test_co2_calibration_recovers_known_room():
    # Simulate a room with known airflow/volume, then check dataset.calibrate_co2 finds them
    import numpy as np
    import pandas as pd
    from app.dataset import calibrate_co2
    rng = np.random.default_rng(3)
    idx = pd.date_range("2023-03-06", periods=6 * 7 * 288, freq="5min", tz="Europe/Copenhagen")
    q, v, out = 120.0, 80.0, 400.0
    hours = idx.hour + idx.minute / 60
    n = np.where((idx.day_of_week < 5) & (hours >= 9) & (hours < 16), rng.integers(1, 4, len(idx)), 0).astype(float)
    n = pd.Series(n, index=idx).rolling(12, min_periods=1).max().to_numpy()  # people stay for an hour+
    c = np.empty(len(idx)); c[0] = out
    for i in range(1, len(idx)):
        decay = math.exp(-(q / v) / 12)
        c[i] = out + n[i - 1] * 16 / q * 1000 * (1 - decay) + (c[i - 1] - out) * decay
    wide = pd.DataFrame({"RoomZ:People__amount": n, "RoomZ:Sensor__CO2": c + rng.normal(0, 3, len(idx)),
                         "RoomZ:Window__opened_closed": 0.0}, index=idx)
    cal = calibrate_co2(wide, "Z")
    assert abs(cal["airflow_m3h"] - q) / q < 0.1 and abs(cal["volume_m3"] - v) / v < 0.2
    assert abs(cal["outdoor_ppm"] - out) < 15


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
    assert client.put("/rooms/lab-zone-u-1", json={"model_from": "Not An Id"}, headers=KEY).status_code == 422
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
