"""Forecasting on dataset rooms (separate from the live boards). Run: ./run-tests"""

import json
import math
import sys
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app

KEY = {"X-API-Key": "test-key"}
TZ = "Europe/Copenhagen"
START = datetime(2023, 3, 6, tzinfo=timezone.utc).timestamp()  # a Monday
WEEKS = 5
ROOM = {"name": "Test office", "capacity": 6, "timezone": TZ, "volume_m3": 60.0, "airflow_m3h": 120.0,
        "outdoor_ppm": 400.0, "co2_gen_lph": 16.0, "k_flow": 350.0, "window_area_m2": 1.0}


def busy(t: float) -> bool:
    from zoneinfo import ZoneInfo
    local = datetime.fromtimestamp(t, ZoneInfo(TZ))
    return local.weekday() < 5 and 9 <= local.hour < 17


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        # A dataset room with a known pattern: 6 people on weekdays 09-17 local, measured CO2 from the mass balance
        from app.core import db
        rows, c_ppm = [], ROOM["outdoor_ppm"]
        for i in range(WEEKS * 2016):
            t = START + i * 300
            people = 6.0 if busy(t) else 0.0
            rows.append(("test-office", t, people, round(c_ppm, 1), 0))
            decay = math.exp(-(ROOM["airflow_m3h"] / ROOM["volume_m3"]) / 12)
            c_ppm = ROOM["outdoor_ppm"] + people * 16 / ROOM["airflow_m3h"] * 1000 * (1 - decay) + (c_ppm - ROOM["outdoor_ppm"]) * decay
        with db() as conn:
            conn.execute("INSERT OR REPLACE INTO dataset_rooms VALUES (?, ?)", ("test-office", json.dumps(ROOM)))
            conn.executemany("INSERT OR REPLACE INTO dataset_occupancy VALUES (?, ?, ?, ?, ?)", rows)
        yield c


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_hour(ts: str) -> int:
    from zoneinfo import ZoneInfo
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(ZoneInfo(TZ)).hour


def test_dataset_rooms_are_separate_from_live_rooms(client):
    ds = client.get("/dataset/rooms", headers=KEY).json()
    assert [r["id"] for r in ds] == ["test-office"]
    assert ds[0]["history"]["points"] == WEEKS * 2016 and ds[0]["history"]["replay_from"]
    assert "test-office" not in {r["id"] for r in client.get("/rooms", headers=KEY).json()}
    assert client.get("/rooms/test-office", headers=KEY).status_code == 404


def test_history_has_measured_co2(client):
    h = client.get(f"/dataset/rooms/test-office/history?from={iso(START)}&to={iso(START + 3600)}", headers=KEY).json()
    assert len(h["points"]) == 12 and h["points"][0]["co2_ppm"] == 400 and h["points"][0]["window_open"] == 0


def test_forecast_from_now_uses_the_weekday_pattern(client):
    f = client.get("/dataset/rooms/test-office/forecast?as_weekday=wed", headers=KEY).json()
    assert f["model"] == "historical_time_of_week" and not f["replay"] and len(f["points"]) == 288
    for p in f["points"]:
        if 11 <= local_hour(p["ts"]) < 15:
            assert abs(p["people"] - 6) < 0.01
        if local_hour(p["ts"]) < 7 or local_hour(p["ts"]) >= 19:
            assert p["people"] < 0.01
        assert "actual_people" not in p
    # CO2 heads for the mass-balance steady state while busy: 400 + 6 * 16 / 120 * 1000 = 1200
    busy_co2 = [p["co2_ppm"] for p in f["points"] if 13 <= local_hour(p["ts"]) < 15]
    assert all(abs(c - 1200) < 15 for c in busy_co2)
    sat = client.get("/dataset/rooms/test-office/forecast?as_weekday=sat", headers=KEY).json()
    assert max(p["people"] for p in sat["points"]) < 0.01


def test_replay_runs_short_term_models_and_compares_with_what_happened(client):
    sys.path.insert(0, "/app/occupancy_forecaster")
    from occupancy_core import train_backtest_forecast
    from app.core import db
    from app.forecast import MODELS_DIR, people_series
    with db() as conn:
        series = people_series(conn, "test-office", TZ)
    train_backtest_forecast(series, "test-office", 6, MODELS_DIR / "test-office")

    at = START + 23 * 86400 + 9 * 3600  # Wednesday of week 4, 10:00 Copenhagen (UTC+1)
    f = client.get(f"/dataset/rooms/test-office/forecast?at={iso(at)}&hours=6", headers=KEY).json()
    assert f["replay"] and f["origin"] == iso(at) and f["model"] == "short_term+historical_time_of_week"
    assert f["short_term_models"] and len(f["points"]) == 72
    assert f["summary"]["people_now"] == 6 and f["summary"]["co2_now"] == round(f["recent"][-1]["co2_ppm"])  # measured
    for p in f["points"]:
        assert p["actual_people"] == (6.0 if busy(datetime.fromisoformat(p["ts"].replace("Z", "+00:00")).timestamp()) else 0.0)
        assert p["actual_co2_ppm"] is not None
    assert f["accuracy"]["first_hour"]["people_mae"] < 0.5 and f["accuracy"]["all"]["points"] == 72
    assert len(f["recent"]) == 25 and f["recent"][-1]["people"] == 6


@pytest.mark.parametrize("query", [
    f"at={iso(START + 3600)}",  # less than a week of data before it
    "at=2030-01-01T00:00:00Z",  # after the data
    "at=nonsense",
    f"at={iso(START + 23 * 86400)}&as_weekday=wed",
    "as_weekday=funday",
])
def test_forecast_validation(client, query):
    assert client.get(f"/dataset/rooms/test-office/forecast?{query}", headers=KEY).status_code == 422


def test_unknown_room(client):
    assert client.get("/dataset/rooms/nope/forecast", headers=KEY).status_code == 404


@pytest.mark.parametrize("path", ["/dataset/rooms", "/dataset/rooms/test-office",
                                  "/dataset/rooms/test-office/history", "/dataset/rooms/test-office/forecast"])
def test_dataset_auth_required(client, path):
    assert client.get(path).status_code == 401


def test_replay_suggestions_need_complete_data():
    import numpy as np
    import pandas as pd
    from app.dataset import replay_suggestions
    idx = pd.date_range("2023-03-06", periods=4 * 7 * 288, freq="5min", tz=TZ)
    n = pd.Series(np.where((idx.day_of_week < 5) & (idx.hour >= 9) & (idx.hour < 17), 3.0, 0.0), index=idx)
    n[(idx >= pd.Timestamp("2023-03-20", tz=TZ)) & (idx < pd.Timestamp("2023-03-23", tz=TZ))] = np.nan  # cameras off
    got = [pd.Timestamp(t).tz_convert(TZ) for t in replay_suggestions(n)]
    assert got and all(t >= pd.Timestamp("2023-03-13", tz=TZ) for t in got)  # needs a week before
    assert all(t.day_of_week < 5 and 8 <= t.hour <= 14 and t.minute == 0 for t in got)
    for t in got:  # no gap at, 1 day or 1 week before
        for d in ("0min", "1D", "7D"):
            assert not pd.isna(n.get(t - pd.Timedelta(d)))
    assert len({t.isocalendar()[:2] for t in got}) == len(got)  # one per week


def test_co2_calibration_recovers_known_room():
    # Simulate a room with known airflow/volume, then check dataset.calibrate_co2 finds them
    import numpy as np
    import pandas as pd
    from app.dataset import calibrate_co2
    rng = np.random.default_rng(3)
    idx = pd.date_range("2023-03-06", periods=6 * 7 * 288, freq="5min", tz=TZ)
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
