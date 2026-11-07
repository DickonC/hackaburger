"""Window advice: the CO2 ODE solved for thresholds. Run: ./run-tests"""

import math

import pytest

from app.rooms import step_co2
from app.ventilation import advice, first_crossing, segment_crossing
from tests.test_forecast import KEY, client  # noqa: F401  (reuse the dataset-room fixture)

ROOM = {"volume_m3": 65.0, "co2_gen_lph": 16.0, "outdoor_ppm": 420.0}
Q_CLOSED = 0.25 * 65.0          # infiltration only
Q_OPEN = Q_CLOSED + 350.0 * 2.0  # two 1 m2 windows open
ORIGIN = 1_700_000_000.0
STEP = 300.0


def brute_force(c0, t0, times, people, q, threshold, rising, dt=1.0):
    """Integrate dC/dt with 1-second Euler steps until C crosses threshold (reference answer)."""
    c, prev = c0, ORIGIN
    for t, p in zip(times, people):
        s = max(prev, t0)
        prev = t
        while s < t:
            c += dt / 3600 * (1000 * ROOM["co2_gen_lph"] * p - q * (c - ROOM["outdoor_ppm"])) / ROOM["volume_m3"]
            s += dt
            if (c >= threshold) if rising else (c <= threshold):
                return s
    return None


def forecast(people):
    times = [ORIGIN + (i + 1) * STEP for i in range(len(people))]
    return times, people


def test_segment_crossing_matches_closed_form():
    # 20 people, closed windows: rising from 600 towards a high steady state
    t = segment_crossing(600.0, 20, ROOM, Q_CLOSED, 10.0, 1000.0)
    assert t is not None
    assert step_co2(600.0, 20, ROOM, Q_CLOSED, t) == pytest.approx(1000.0, abs=1e-6)
    # Steady state below threshold -> never
    assert segment_crossing(500.0, 0, ROOM, Q_CLOSED, 10.0, 1000.0) is None
    # Falling with windows open
    t = segment_crossing(1200.0, 5, ROOM, Q_OPEN, 10.0, 600.0)
    assert step_co2(1200.0, 5, ROOM, Q_OPEN, t) == pytest.approx(600.0, abs=1e-6)
    # Crossing after the end of the step -> None
    assert segment_crossing(600.0, 20, ROOM, Q_CLOSED, 0.01, 1000.0) is None


def test_crossing_with_changing_people_matches_brute_force():
    # People arrive over time: 2, then 10, then a 25-person meeting, then they leave
    people = [2] * 6 + [10] * 6 + [25] * 12 + [3] * 12
    times, people = forecast(people)
    exact, _ = first_crossing(500.0, ORIGIN, ORIGIN, times, people, ROOM, Q_CLOSED, 1000.0, rising=True)
    ref = brute_force(500.0, ORIGIN, times, people, Q_CLOSED, 1000.0, rising=True)
    assert exact is not None and ref is not None
    assert abs(exact - ref) < 5  # seconds; Euler with 1 s steps vs exact
    # It is consistent with stepping the forecast: C at the step before is below, after is above
    i = next(i for i, t in enumerate(times) if t >= exact)
    c = 500.0
    for t_prev, (t, p) in zip([ORIGIN] + times, zip(times, people)):
        c_next = step_co2(c, p, ROOM, Q_CLOSED, (t - t_prev) / 3600)
        if t == times[i]:
            assert c < 1000.0 <= c_next
            break
        c = c_next


def test_flush_starts_mid_step_and_matches_brute_force():
    people = [5] * 48  # with 2 windows open, 5 people settle at ~530 ppm, so 600 is reachable
    times, people = forecast(people)
    t0 = ORIGIN + 1234.0  # not on a step boundary
    exact, _ = first_crossing(1000.0, t0, ORIGIN, times, people, ROOM, Q_OPEN, 600.0, rising=False)
    ref = brute_force(1000.0, t0, times, people, Q_OPEN, 600.0, rising=False)
    assert exact is not None and abs(exact - ref) < 5


def test_advice_planned():
    # A 20-person meeting for an hour, then 3 people stay (20 people with windows open settle at ~870 ppm,
    # so the flush only gets to 600 once the meeting ends)
    times, people = forecast([20] * 12 + [3] * 36)
    a = advice(500.0, ORIGIN, times, people, ROOM, Q_CLOSED, Q_OPEN,
               people_low=[15] * 12 + [2] * 36, people_high=[25] * 12 + [4] * 36)
    assert a["status"] == "planned"
    assert 0 < a["heads_up_in_min"] < a["open_window_in_min"] < a["critical_in_min"]
    lo, hi = a["open_window_range_min"]
    assert lo < a["open_window_in_min"] < hi  # more people -> sooner
    assert a["flush_duration_min"] > 0 and a["flush_reaches_target"]
    assert a["message"].startswith("Open a window in ")
    assert "600 ppm" in a["message"]


def test_advice_ok_when_room_stays_fresh():
    # 1 person for 2 hours (with only infiltration they'd pass 1000 ppm after ~3.4 h)
    times, people = forecast([1] * 24)
    a = advice(450.0, ORIGIN, times, people, ROOM, Q_CLOSED, Q_OPEN)
    assert a["status"] == "ok" and a["open_window_in_min"] is None and a["flush_duration_min"] is None


def test_advice_now_and_critical():
    times, people = forecast([20] * 48)
    a = advice(1100.0, ORIGIN, times, people, ROOM, Q_CLOSED, Q_OPEN)
    assert a["status"] == "open_now" and a["open_window_in_min"] == 0
    a = advice(1700.0, ORIGIN, times, people, ROOM, Q_CLOSED, Q_OPEN)
    assert a["status"] == "critical" and a["critical_in_min"] == 0
    assert a["message"].startswith("Cross-ventilate now")


def test_advice_flush_cannot_reach_target():
    # So many people that even with windows open CO2 settles above 600
    times, people = forecast([60] * 48)
    a = advice(1100.0, ORIGIN, times, people, ROOM, Q_CLOSED, Q_OPEN)
    assert a["flush_reaches_target"] is False and a["flush_duration_min"] is None
    assert a["lowest_reachable_ppm"] > 600


def test_advice_window_already_open():
    times, people = forecast([5] * 48)
    a = advice(900.0, ORIGIN, times, people, ROOM, Q_CLOSED, Q_OPEN, window_open_now=True)
    assert a["status"] == "flushing" and a["close_window_in_min"] > 0


def test_forecast_endpoint_includes_ventilation(client):  # noqa: F811
    r = client.get("/dataset/rooms/test-office/forecast", headers=KEY)
    assert r.status_code == 200
    v = r.json()["summary"]["ventilation"]
    assert v["thresholds_ppm"]["target"] == 600
    assert v["status"] in ("ok", "heads_up", "planned", "open_now", "critical")
    assert isinstance(v["message"], str)
