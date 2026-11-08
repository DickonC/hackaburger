"""Switchable edge rule and position fit (docs/heatmap-fit-brief.md acceptance checks). Run: ./run-tests

Recording the golden output (only when the default behaviour is meant to change, which it isn't):
    RECORD_GOLDEN=1 with a writable tests/ mount writes tests/heatmap_default_golden.json.
"""

import json
import math
import os
import random
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import heatmap
from app.core import db
from app.main import app

KEY = {"X-API-Key": "test-key"}
BOARDS = ["uno-q-1", "uno-q-2", "uno-q-3", "uno-q-4"]
GOLDEN = os.path.join(os.path.dirname(__file__), "heatmap_default_golden.json")
# Past slots inside LOOKBACK_S, so other tests' "latest" snapshot (in the future) stays theirs
BASE_TS = math.floor(time.time() / 10) * 10 - 1800
_slot = iter(range(0, 10**6))

# A deterministic config: everything that affects the snapshot is set explicitly
FIXED_CONFIG = {
    "room": {"width_m": 8, "length_m": 6, "board_inset_m": 0.5},
    # explicit x/y: other tests may have left board positions behind (a patch only merges)
    "boards": {"uno-q-1": {"corner": "top_left", "x": 0.5, "y": 0.5},
               "uno-q-2": {"corner": "top_right", "x": 7.5, "y": 0.5},
               "uno-q-3": {"corner": "bottom_right", "x": 7.5, "y": 5.5},
               "uno-q-4": {"corner": "bottom_left", "x": 0.5, "y": 5.5}},
    "offsets_mode": "manual",
    "offsets_db": {"uno-q-1": 1.5, "uno-q-2": -1.0, "uno-q-3": 0.0, "uno-q-4": 2.0},
    "filter": {"exclude_kinds": ["earbuds", "tracker", "beacon", "fixed"], "stationary_h": 0,
               "edge_mode": "manual", "edge_dbm": -84, "edge_margin_db": 4, "min_boards": 3, "presence_s": 0},
    "position": {"weight_k": 1.0},
    "heatmap": {"cell_m": 0.5, "sigma_m": 1.0, "smoothing_s": 0},
    "people": {"devices_per_person": 1.2},
}
OFF = {"filter": {"edge_rule": "weakest"}, "position": {"method": "weighted"}}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def fixed(client):
    """Deterministic config for one test; switches A and B back off afterwards."""
    extra = {b: None for b in client.get("/heatmap/config", headers=KEY).json()["boards"] if b not in BOARDS}
    cfg = {**FIXED_CONFIG, "boards": {**FIXED_CONFIG["boards"], **extra}}
    assert client.put("/heatmap/config", json=cfg, headers=KEY).status_code == 200
    yield client
    if "edge_rule" in client.get("/heatmap/config", headers=KEY).json()["filter"]:
        client.put("/heatmap/config", json=OFF, headers=KEY)


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_ts() -> float:
    return BASE_TS + next(_slot) * 10


def fixture_devices() -> dict[str, tuple[list, str | None]]:
    """~40 devices: inside, outside, 2-board, excluded kinds; fixed seed so it never changes."""
    rng = random.Random(20261108)
    kinds = [None, "phone", "apple_device", "computer", "wearable", "brand_other", "earbuds"]
    devs = {}
    for i in range(40):
        base = rng.uniform(-75, -50)
        rssis = [round(base - rng.uniform(0, 30), 1) for _ in BOARDS]
        if i % 9 == 0:
            rssis[rng.randrange(4)] = None
        if i % 13 == 0:
            rssis[0] = rssis[1] = None
        devs[f"g{i:02d}"] = (rssis, kinds[i % len(kinds)])
    return devs


def run_snapshot(client, devices: dict) -> tuple[float, dict]:
    """POST one ts from all four boards, build it, return (ts, debug json)."""
    ts = new_ts()
    for bi, b in enumerate(BOARDS):
        items = []
        for dev_id, spec in devices.items():
            rssis, kind = spec if isinstance(spec, tuple) else (spec, None)
            if rssis[bi] is not None:
                items.append({"id": dev_id, "rssi": rssis[bi], "n": 2, **({"kind": kind} if kind else {})})
        r = client.post("/data", json={"device": b, "ts": iso(ts), "data": {"devices": items}}, headers=KEY)
        assert r.status_code == 201
    heatmap.tick(time.time() + heatmap.WAIT_S + 1)
    return ts, client.get(f"/heatmap/debug?ts={iso(ts)}", headers=KEY).json()


def snapshot_row(ts: float) -> dict:
    with db() as conn:
        r = conn.execute("SELECT * FROM heatmap_snapshots WHERE ts = ?", (ts,)).fetchone()
    return {"funnel": json.loads(r["funnel"]), "grid": json.loads(r["grid"]),
            "devices_in_room": r["devices_in_room"], "people_estimate": round(r["people_estimate"], 6)}


def observed(ts: float, dbg: dict) -> dict:
    return {"devices": {d["id"]: [d["x"], d["y"], d["status"], d["reason"], d["weakest_dbm"]]
                        for d in sorted(dbg["devices"], key=lambda d: d["id"])},
            **snapshot_row(ts)}


def status_of(dbg: dict, dev_id: str) -> dict:
    return next(d for d in dbg["devices"] if d["id"] == dev_id)


# 1. Defaults keep today's behaviour exactly ----------------------------------------------------

def test_defaults_unchanged(fixed):
    ts, dbg = run_snapshot(fixed, fixture_devices())
    got = observed(ts, dbg)
    if os.environ.get("RECORD_GOLDEN"):
        with open(GOLDEN, "w") as f:
            json.dump(got, f, indent=1, sort_keys=True)
    with open(GOLDEN) as f:
        expected = json.load(f)
    assert got == expected


# 2-4. Edge rule "second_weakest" -----------------------------------------------------------

SMALL_BOARDS = {"uno-q-1": {"x": 0.5, "y": 0.5}, "uno-q-2": {"x": 4.5, "y": 0.5},
                "uno-q-3": {"x": 4.5, "y": 4.7}, "uno-q-4": {"x": 0.5, "y": 4.7}}
SMALL_ROOM = {"room": {"width_m": 5, "length_m": 5.2, "board_inset_m": 0.5}, "boards": SMALL_BOARDS,
              "offsets_db": {b: 0 for b in BOARDS}, "filter": {"edge_dbm": -79}}


def test_second_weakest_keeps_a_corner_device(fixed):
    fixed.put("/heatmap/config", json=SMALL_ROOM, headers=KEY)
    devs = {"corner": [-45, -70, -81, -72]}
    _, dbg = run_snapshot(fixed, devs)
    assert (status_of(dbg, "corner")["status"], status_of(dbg, "corner")["reason"]) == ("rejected", "outside_edge")
    fixed.put("/heatmap/config", json={"filter": {"edge_rule": "second_weakest", "weakest_slack_db": 8}}, headers=KEY)
    _, dbg = run_snapshot(fixed, devs)
    d = status_of(dbg, "corner")
    assert (d["status"], d["reason"]) == ("in_room", None)
    assert d["weakest_dbm"] == -81 and d["second_weakest_dbm"] == -72
    assert dbg["edge_rule"] == "second_weakest"


def test_second_weakest_rejects_a_device_behind_a_wall(fixed):
    fixed.put("/heatmap/config", json={**SMALL_ROOM, "filter": {"edge_dbm": -79, "edge_rule": "second_weakest"}},
              headers=KEY)
    _, dbg = run_snapshot(fixed, {"wall": [-66, -68, -84, -85]})
    assert (status_of(dbg, "wall")["status"], status_of(dbg, "wall")["reason"]) == ("rejected", "outside_edge")


def test_auto_edge_per_rule(fixed):
    room = {"room": {"width_m": 5, "length_m": 5.2, "board_inset_m": 0.5}, "boards": SMALL_BOARDS}
    cfg = fixed.put("/heatmap/config", json={**room, "filter": {"edge_mode": "auto", "edge_margin_db": 4,
                                                                  "edge_rule": "weakest"}}, headers=KEY).json()
    assert abs(cfg["filter"]["edge_dbm"] - (-80)) <= 1
    cfg = fixed.put("/heatmap/config", json={"filter": {"edge_rule": "second_weakest"}}, headers=KEY).json()
    assert abs(cfg["filter"]["edge_dbm"] - (-79)) <= 1 and cfg["filter"]["edge_mode"] == "auto"


# 5-6. Position fit -------------------------------------------------------------------------

def fit_cfg(**position) -> dict:
    cfg = heatmap.merged(heatmap.DEFAULT_CONFIG, {"room": {"width_m": 5, "length_m": 5.2, "board_inset_m": 0.5}})
    cfg["position"].update({"method": "fit", "path_loss_n": 2.5, "noise_db": 3.5, **position})
    return cfg


def true_rssi(pos, xy, p0, n=2.5, noise=None, rng=None):
    out = {}
    for b, bxy in pos.items():
        v = p0 - 10 * n * math.log10(max(math.dist(xy, bxy), 0.5))
        if noise:
            v += rng.gauss(0, noise)
        out[b] = v
    return out


def weighted_xy(pos, adj, k=1.0):
    wts = {b: 10 ** (v / (10 * k)) for b, v in adj.items()}
    total = sum(wts.values())
    return (sum(wts[b] * pos[b][0] for b in wts) / total, sum(wts[b] * pos[b][1] for b in wts) / total)


@pytest.mark.parametrize("p0", [-62, -50, -72])
def test_fit_recovers_a_position_without_noise(p0):
    cfg = fit_cfg()
    pos = heatmap.board_positions(cfg)
    x, y, rms = heatmap.PositionFit(cfg, pos).locate(true_rssi(pos, (1.0, 4.0), p0))
    assert math.dist((x, y), (1.0, 4.0)) < 0.5, (p0, x, y)


def test_fit_beats_weighted_average_with_noise():
    cfg = fit_cfg()
    pos = heatmap.board_positions(cfg)
    fit = heatmap.PositionFit(cfg, pos)
    rng = random.Random(42)
    fit_err, avg_err = [], []
    for _ in range(500):
        xy = (rng.uniform(0, 5), rng.uniform(0, 5.2))
        adj = true_rssi(pos, xy, rng.uniform(-70, -50), noise=4, rng=rng)
        located = fit.locate(adj)
        fxy = located[:2] if located else weighted_xy(pos, adj)
        fit_err.append(math.dist(fxy, xy))
        avg_err.append(math.dist(weighted_xy(pos, adj), xy))
    fit_err.sort()
    avg_err.sort()
    assert fit_err[250] <= avg_err[250], (fit_err[250], avg_err[250])


def test_fit_is_used_in_snapshots_and_stays_in_the_room(fixed):
    fixed.put("/heatmap/config", json={"position": {"method": "fit"}}, headers=KEY)
    _, dbg = run_snapshot(fixed, fixture_devices())
    assert dbg["position_method"] == "fit"
    placed = [d for d in dbg["devices"] if d["boards_heard"] >= 3]
    assert placed and all(d["fit_rms_db"] is not None for d in placed)
    assert all(0 <= d["x"] <= 8 and 0 <= d["y"] <= 6 for d in placed)
    two = [d for d in dbg["devices"] if d["boards_heard"] == 2]
    assert two and all(d["fit_rms_db"] is None and d["x"] is not None for d in two)  # weighted fallback


# 7. Switching back gives today's behaviour ----------------------------------------------------

def test_switching_back(fixed):
    fixed.put("/heatmap/config", json={"filter": {"edge_rule": "second_weakest"}, "position": {"method": "fit"}},
              headers=KEY)
    run_snapshot(fixed, fixture_devices())
    fixed.put("/heatmap/config", json=OFF, headers=KEY)
    ts, dbg = run_snapshot(fixed, fixture_devices())
    with open(GOLDEN) as f:
        assert observed(ts, dbg) == json.load(f)


# 8. Validation --------------------------------------------------------------------------------

@pytest.mark.parametrize("patch", [{"position": {"method": "magic"}}, {"position": {"path_loss_n": 10}},
                                   {"filter": {"edge_rule": "x"}}, {"filter": {"weakest_slack_db": 21}},
                                   {"position": {"fit_cell_m": 0.05}}, {"position": {"noise_db": 0}}])
def test_validation(client, patch):
    assert client.put("/heatmap/config", json=patch, headers=KEY).status_code == 422


def test_new_fields_have_defaults_in_get_config(client):
    cfg = client.get("/heatmap/config", headers=KEY).json()
    assert {"edge_rule", "weakest_slack_db"} <= set(cfg["filter"])
    assert {"method", "path_loss_n", "fit_cell_m", "noise_db"} <= set(cfg["position"])
    defaults = heatmap.DEFAULT_CONFIG
    assert defaults["filter"]["edge_rule"] == "weakest" and defaults["position"]["method"] == "weighted"


# 9. API shape: only additions -----------------------------------------------------------------

def test_api_shape_only_additions(fixed):
    run_snapshot(fixed, fixture_devices())
    latest = fixed.get("/heatmap/latest", headers=KEY).json()
    assert {"ts", "age_s", "mode", "people_estimate", "people_smoothed", "devices_in_room", "devices_heard",
            "funnel", "kinds_in_room", "boards", "room", "calibration", "heatmap", "warnings"} <= set(latest)
    assert {"edge_mode", "edge_dbm", "offsets_mode", "offsets_db"} <= set(latest["calibration"])
    dbg = fixed.get("/heatmap/debug", headers=KEY).json()
    assert {"ts", "filter", "offsets_db", "devices"} <= set(dbg)
    assert set(dbg["filter"]) == {"exclude_kinds", "stationary_h", "edge_dbm", "min_boards", "presence_s"}
    assert {"id", "rssi", "weakest_dbm", "boards_heard", "x", "y", "kind", "status", "reason"} <= set(dbg["devices"][0])


# 10. Performance ------------------------------------------------------------------------------

def test_fit_snapshot_with_900_devices_is_fast(fixed):
    fixed.put("/heatmap/config", json={"room": {"width_m": 7.5, "length_m": 10}, "position": {"method": "fit"},
                                       "filter": {"edge_rule": "second_weakest"}}, headers=KEY)
    rng = random.Random(7)
    devs = {f"p{i:03d}": [round(rng.uniform(-95, -50), 1) for _ in BOARDS] for i in range(900)}
    ts = new_ts()
    for bi, b in enumerate(BOARDS):
        items = [{"id": d, "rssi": r[bi], "n": 2} for d, r in devs.items()]
        assert fixed.post("/data", json={"device": b, "ts": iso(ts), "data": {"devices": items}},
                          headers=KEY).status_code == 201
    cfg = fixed.get("/heatmap/config", headers=KEY).json()
    with db() as conn:
        started = time.perf_counter()
        heatmap.build_snapshot(conn, ts + 0.5, heatmap.load_config(conn), time.time())
        elapsed = time.perf_counter() - started
    assert cfg["position"]["method"] == "fit"
    assert elapsed < 0.3, f"{elapsed * 1000:.0f} ms"
