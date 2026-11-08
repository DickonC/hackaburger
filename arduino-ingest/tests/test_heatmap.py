"""Four-board room heatmap (docs/heatmap-backend-guide.md acceptance checks). Run: ./run-tests"""

import math
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import heatmap
from app.main import app

KEY = {"X-API-Key": "test-key"}
BOARDS = ["uno-q-1", "uno-q-2", "uno-q-3", "uno-q-4"]
NOW = math.floor(time.time() / 10) * 10 + 20000  # ahead of the other tests' board messages, so "latest" is ours
_slot = iter(range(0, 10**6))


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        # Deterministic start: manual offsets of 0, no presence check, no time smoothing
        c.put("/heatmap/config", json={"offsets_db": {b: 0 for b in BOARDS}, "room": {"width_m": 8, "length_m": 6},
                                       "filter": {"presence_s": 0, "edge_mode": "auto"},
                                       "heatmap": {"smoothing_s": 0}}, headers=KEY)
        yield c


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_ts() -> float:
    """A fresh clock-aligned ts per snapshot."""
    return NOW + next(_slot) * 10


def gap(slots: int = 12):
    """Skip time (default 2 minutes), so time windows only see this test's snapshots."""
    for _ in range(slots):
        next(_slot)


def send(client, ts, devices_by_board: dict[str, dict], boards=BOARDS, version=None):
    """devices_by_board: {board: {device id: rssi or (rssi, kind)}} -> one POST per board for this ts."""
    def item(i, v):
        rssi, kind = v if isinstance(v, tuple) else (v, None)
        return {"id": i, "rssi": rssi, "n": 2, **({"kind": kind, "static": False} if kind else {})}
    for b in boards:
        data = {"devices": [item(i, v) for i, v in devices_by_board.get(b, {}).items()]}
        if version is not None:
            data["config_version"] = version
        assert client.post("/data", json={"device": b, "ts": iso(ts), "data": data}, headers=KEY).status_code == 201


def device(rssis: list[float | None]) -> dict[str, float]:
    """rssi per board in BOARDS order (None = not heard) -> {board: rssi}."""
    return {b: r for b, r in zip(BOARDS, rssis) if r is not None}


def snapshot(client, ts, devices: dict, boards=BOARDS):
    """Send one ts with these devices ({id: [rssi per board]} or {id: ([rssi per board], kind)}); return debug."""
    by_board = {b: {} for b in BOARDS}
    for dev_id, spec in devices.items():
        rssis, kind = spec if isinstance(spec, tuple) else (spec, None)
        for b, r in device(rssis).items():
            by_board[b][dev_id] = (r, kind) if kind else r
    send(client, ts, by_board, boards)
    heatmap.tick(time.time() + heatmap.WAIT_S + 1)
    return client.get(f"/heatmap/debug?ts={iso(ts)}", headers=KEY).json()


def status(debug, dev_id):
    d = next(d for d in debug["devices"] if d["id"] == dev_id)
    return d["status"], d["reason"], d


def test_auto_edge_from_room_size(client):
    assert client.get("/heatmap/config", headers=KEY).json()["filter"]["edge_dbm"] == -84
    cfg = client.put("/heatmap/config", json={"room": {"width_m": 15, "length_m": 10}}, headers=KEY).json()
    assert cfg["filter"]["edge_dbm"] == -90 and cfg["filter"]["edge_mode"] == "auto"
    assert client.put("/heatmap/config", json={"room": {"width_m": 8, "length_m": 6}},
                      headers=KEY).json()["filter"]["edge_dbm"] == -84


def test_board_positions_are_derived(client):
    latest = client.get("/heatmap/latest", headers=KEY).json()
    pos = {b: (s["x"], s["y"]) for b, s in latest["boards"].items()}
    assert pos == {"uno-q-1": (0.5, 0.5), "uno-q-2": (7.5, 0.5), "uno-q-3": (7.5, 5.5), "uno-q-4": (0.5, 5.5)}


def test_outside_edge_and_in_room(client):
    client.put("/heatmap/config", json={"filter": {"edge_dbm": -85}}, headers=KEY)  # -> manual
    assert client.get("/heatmap/config", headers=KEY).json()["filter"]["edge_mode"] == "manual"
    dbg = snapshot(client, new_ts(), {"outside": [-60, -95, -94, -97], "inside": [-62, -70, -74, -68],
                                      "far": [-88, None, None, None]})
    assert status(dbg, "outside")[:2] == ("rejected", "outside_edge")
    assert status(dbg, "far")[:2] == ("rejected", "too_few_boards") and status(dbg, "far")[2]["x"] is None
    st, reason, d = status(dbg, "inside")
    assert (st, reason) == ("in_room", None)
    assert math.dist((d["x"], d["y"]), (0.5, 0.5)) < math.dist((d["x"], d["y"]), (7.5, 5.5))
    assert [x["status"] for x in dbg["devices"]][0] == "in_room"  # in-room first
    assert dbg["filter"] == {"exclude_kinds": ["earbuds", "tracker", "beacon", "fixed"], "stationary_h": 2.0,
                             "edge_dbm": -85, "min_boards": 3, "presence_s": 0}
    assert status(dbg, "inside")[2]["kind"] == "unknown"  # no kind sent


def test_edge_change_applies_on_next_snapshot(client):
    client.put("/heatmap/config", json={"filter": {"edge_dbm": -85}}, headers=KEY)
    devs = {f"e{i}": [-70, -72, -90, -75] for i in range(5)}
    snapshot(client, new_ts(), devs)
    assert client.get("/heatmap/latest", headers=KEY).json()["devices_in_room"] == 0
    client.put("/heatmap/config", json={"filter": {"edge_dbm": -95}}, headers=KEY)
    snapshot(client, new_ts(), devs)
    assert client.get("/heatmap/latest", headers=KEY).json()["devices_in_room"] == 5


def test_presence_check(client):
    client.put("/heatmap/config", json={"filter": {"presence_s": 30, "edge_dbm": -90}}, headers=KEY)
    client.put("/heatmap/boards", json={"interval_s": 10, "window_s": 20}, headers=KEY)
    try:
        t0 = new_ts()
        for i in range(3):  # needs 3 snapshots (30 s / 10 s) that all pass checks 1 and 2
            dbg = snapshot(client, t0 + i * 10, {"stay": [-60, -65, -70, -66], "walker": [-60, -65, -70, -66] if i == 0 else [-60, -95, -95, -95]})
            next(_slot)
            expected = "in_room" if i == 2 else "rejected"
            assert status(dbg, "stay")[0] == expected, (i, status(dbg, "stay"))
        assert status(dbg, "walker")[1] == "outside_edge"
    finally:
        client.put("/heatmap/config", json={"filter": {"presence_s": 0}}, headers=KEY)


def test_people_estimate_grid_and_headcount(client):
    client.put("/heatmap/config", json={"filter": {"edge_dbm": -90}, "people": {"devices_per_person": 1.4},
                                        "heatmap": {"smoothing_s": 0}}, headers=KEY)
    devs = {f"p{i}": [-66, -66, -66, -66] for i in range(14)}  # all in the middle of the room
    gap()
    snapshot(client, new_ts(), devs)
    latest = client.get("/heatmap/latest", headers=KEY).json()
    assert latest["devices_in_room"] == 14 and latest["people_estimate"] == 10.0
    total = sum(map(sum, latest["heatmap"]["values"]))
    assert abs(total - 10) / 10 < 0.1, total
    h = latest["heatmap"]
    assert (h["cols"], h["rows"]) == (16, 12) and h["max_value"] == max(map(max, h["values"]))
    assert latest["funnel"] == {"heard": 14, "plausible_people": 14, "enough_boards": 14, "inside_edge": 14,
                                "present": 14}
    assert latest["kinds_in_room"] == {"unknown": 14}

    r = client.post("/heatmap/calibrate/headcount", json={"people": 10}, headers=KEY).json()
    assert r["devices_per_person"] == 1.4
    r = client.post("/heatmap/calibrate/headcount", json={"people": 7}, headers=KEY).json()
    assert r["devices_per_person"] == 2.0
    assert client.get("/heatmap/config", headers=KEY).json()["people"]["devices_per_person"] == 2.0
    assert client.post("/heatmap/calibrate/headcount", json={"people": 0}, headers=KEY).status_code == 422
    client.put("/heatmap/config", json={"people": {"devices_per_person": 1.4}}, headers=KEY)


def test_default_devices_per_person_is_1_2():
    assert heatmap.DEFAULT_CONFIG["people"]["devices_per_person"] == 1.2


def test_obvious_non_people_are_excluded(client):
    client.put("/heatmap/config", json={"filter": {"edge_dbm": -90}}, headers=KEY)
    strong = [-55, -63, -70, -66]
    devs = {"buds": (strong, "earbuds"), "tag": (strong, "tracker"), "phone": (strong, "apple_device"),
            "laptop": (strong, "computer")}
    dbg = snapshot(client, new_ts(), devs)
    assert status(dbg, "buds")[:2] == ("rejected", "not_a_person") and status(dbg, "buds")[2]["kind"] == "earbuds"
    assert status(dbg, "tag")[1] == "not_a_person"
    assert status(dbg, "phone")[0] == "in_room" and status(dbg, "laptop")[0] == "in_room"
    latest = client.get("/heatmap/latest", headers=KEY).json()
    assert latest["funnel"]["heard"] == 4 and latest["funnel"]["plausible_people"] == 2
    assert latest["kinds_in_room"] == {"apple_device": 1, "computer": 1}

    # Boards disagree: one board thinks it's earbuds -> excluded; the most specific kind is reported
    by_board = {b: {"mixed": (-60, "earbuds" if b == "uno-q-3" else "unknown")} for b in BOARDS}
    ts = new_ts()
    send(client, ts, by_board)
    dbg = client.get(f"/heatmap/debug?ts={iso(ts)}", headers=KEY).json()
    assert status(dbg, "mixed")[1] == "not_a_person" and status(dbg, "mixed")[2]["kind"] == "earbuds"

    client.put("/heatmap/config", json={"filter": {"exclude_kinds": []}}, headers=KEY)
    try:
        dbg = snapshot(client, new_ts(), devs)
        assert all(status(dbg, i)[0] == "in_room" for i in devs)
    finally:
        client.put("/heatmap/config", json={"filter": {"exclude_kinds": ["earbuds", "tracker", "beacon", "fixed"]}},
                   headers=KEY)
    assert client.put("/heatmap/config", json={"filter": {"exclude_kinds": ["toaster"]}}, headers=KEY).status_code == 422


def test_stationary_devices_are_excluded(client):
    # 0.01 h = 36 s: an id seen in every snapshot for longer than that is a fixed device
    client.put("/heatmap/config", json={"filter": {"stationary_h": 0.01, "edge_dbm": -90}}, headers=KEY)
    try:
        t0 = new_ts()
        for i in range(6):
            dbg = snapshot(client, t0 + i * 10, {"tv": [-60, -65, -70, -66], f"phone{i}": [-60, -65, -70, -66]})
            next(_slot)
            st = status(dbg, "tv")
            assert st[1] == ("stationary" if i * 10 >= 36 else None), (i, st)
            assert status(dbg, f"phone{i}")[0] == "in_room"  # new ids (rotating addresses) still count
        # After an absence longer than 3 send periods (+15 s) the run starts again
        gap(100)
        dbg = snapshot(client, new_ts(), {"tv": [-60, -65, -70, -66]})
        assert status(dbg, "tv")[0] == "in_room"
    finally:
        client.put("/heatmap/config", json={"filter": {"stationary_h": 2}}, headers=KEY)


def test_one_board_offline(client):
    client.put("/heatmap/config", json={"filter": {"edge_dbm": -90}}, headers=KEY)
    ts = new_ts()
    dbg = snapshot(client, ts, {"a": [-60, -65, -70, None]}, boards=BOARDS[:3])  # uno-q-4 sends nothing
    assert status(dbg, "a")[0] == "in_room"
    latest = client.get("/heatmap/latest", headers=KEY).json()
    assert latest["ts"] == iso(ts) and latest["boards"]["uno-q-4"]["heard"] == 0
    assert any(w.startswith("uno-q-4 missing from this snapshot: using 3 boards") for w in latest["warnings"])


def test_snapshot_waits_for_all_boards(client):
    ts = new_ts()
    send(client, ts, {b: {"w": -60} for b in BOARDS[:2]}, boards=BOARDS[:2])
    heatmap.tick(time.time())  # only 2 of 4 boards, just arrived: not yet
    assert client.get(f"/heatmap/debug?ts={iso(ts)}", headers=KEY).json()["ts"] != iso(ts)
    send(client, ts, {b: {"w": -60} for b in BOARDS[2:]}, boards=BOARDS[2:])  # complete -> built on ingest
    assert client.get(f"/heatmap/debug?ts={iso(ts)}", headers=KEY).json()["ts"] == iso(ts)


def test_mode_bulk_change_and_pending(client):
    before = {b: client.get(f"/config/{b}", headers=KEY).json()["config_version"] for b in BOARDS}
    r = client.put("/heatmap/mode", json={"mode": "live"}, headers=KEY).json()
    for b in BOARDS:
        c = r["boards"][b]
        assert (c["send_every_s"], c["interval_s"], c["window_s"]) == (0, 10, 20)
        assert c["config_version"] == before[b] + 1
    assert client.get("/heatmap/latest", headers=KEY).json()["mode"] == "live"

    r = client.put("/heatmap/boards", json={"threshold_dbm": -60}, headers=KEY).json()
    versions = {b: r["boards"][b]["config_version"] for b in BOARDS}
    assert all(r["boards"][b]["threshold_dbm"] == -60 for b in BOARDS)
    boards = client.get("/heatmap/latest", headers=KEY).json()["boards"]
    assert all(boards[b]["config"]["pending"] for b in BOARDS)
    send(client, new_ts(), {}, boards=["uno-q-1"], version=versions["uno-q-1"])
    boards = client.get("/heatmap/latest", headers=KEY).json()["boards"]
    assert not boards["uno-q-1"]["config"]["pending"] and boards["uno-q-1"]["config"]["applied_version"] == versions["uno-q-1"]
    assert boards["uno-q-2"]["config"]["pending"]

    r = client.put("/heatmap/mode", json={"mode": "dev"}, headers=KEY).json()
    assert all(r["boards"][b]["send_every_s"] == 300 and r["boards"][b]["interval_s"] == 10 for b in BOARDS)
    latest = client.get("/heatmap/latest", headers=KEY).json()
    assert latest["mode"] == "dev" and any("dev mode" in w for w in latest["warnings"])
    assert client.put("/heatmap/mode", json={"mode": "party"}, headers=KEY).status_code == 422


def test_boards_must_share_timing(client):
    client.put("/heatmap/boards", json={"interval_s": 10, "window_s": 20}, headers=KEY)
    assert client.put("/config/uno-q-2", json={"interval_s": 5}, headers=KEY).status_code == 422
    assert client.put("/config/uno-q-2", json={"window_s": 7}, headers=KEY).status_code == 422
    assert client.put("/config/uno-q-2", json={"threshold_dbm": -50}, headers=KEY).status_code == 200
    r = client.put("/heatmap/boards", json={"interval_s": 5}, headers=KEY)
    assert r.status_code == 200 and all(c["interval_s"] == 5 for c in r.json()["boards"].values())
    assert client.put("/config/not-a-heatmap-board", json={"interval_s": 3}, headers=KEY).status_code == 200
    assert client.put("/heatmap/boards", json={"interval_s": 0}, headers=KEY).status_code == 422


def test_config_validation(client):
    for bad in ({"room": {"width_m": 0.5}}, {"filter": {"min_boards": 5}}, {"filter": {"edge_mode": "magic"}},
                {"heatmap": {"cell_m": 3}}, {"offsets_db": {"uno-q-1": 25}}, {"colour": "red"},
                {"room": {"depth_m": 3}}, {"boards": {"uno-q-1": {"corner": "middle"}}}, {"filter": {"presence_s": 1.5}}):
        assert client.put("/heatmap/config", json=bad, headers=KEY).status_code == 422, bad
    cfg = client.put("/heatmap/config", json={"boards": {"uno-q-2": {"corner": "top_right", "x": 6.0}}}, headers=KEY).json()
    assert cfg["boards"]["uno-q-2"] == {"corner": "top_right", "x": 6.0} and len(cfg["boards"]) == 4
    client.put("/heatmap/config", json={"boards": {"uno-q-2": {"corner": "top_right"}}}, headers=KEY)


def test_history_and_live_room_people(client):
    client.put("/heatmap/config", json={"filter": {"edge_dbm": -90}, "people": {"devices_per_person": 1.0}}, headers=KEY)
    gap()
    ts = new_ts()
    gap()
    snapshot(client, ts, {f"h{i}": [-65, -65, -65, -65] for i in range(6)})
    hist = client.get(f"/heatmap/history?from={iso(ts - 30)}&to={iso(ts + 30)}&step_s=60", headers=KEY).json()
    assert hist == [{"ts": iso(ts - 30), "people": 6.0}]
    # A live room can take its people from the heatmap (for the CO2 estimate)
    client.post("/rooms", json={"id": "heatmap-room", "name": "Hall", "volume_m3": 300, "windows": [],
                                "device": "heatmap"}, headers=KEY)
    occ = client.get(f"/rooms/heatmap-room/occupancy?from={iso(ts - 150)}&to={iso(ts + 150)}&step_s=300",
                     headers=KEY).json()
    assert occ["points"][0]["people"] is not None
    client.put("/heatmap/config", json={"people": {"devices_per_person": 1.4}}, headers=KEY)


def test_auto_offsets(client):
    client.put("/heatmap/config", json={"filter": {"edge_dbm": -95}, "offsets_mode": "auto"}, headers=KEY)
    heatmap._last_offsets = 0
    gap(70)  # offsets use the last 10 minutes of snapshots: only this test's devices
    # Board 2 reads 5 dB low on every device all four boards hear
    snapshot(client, new_ts(), {f"o{i}": [-65, -70, -65, -65] for i in range(40)})
    heatmap._last_offsets = 0
    heatmap.tick(time.time() + heatmap.WAIT_S + 1)
    offsets = client.get("/heatmap/config", headers=KEY).json()["offsets_db"]
    assert offsets["uno-q-2"] > 0.5 and all(offsets[b] < 0 for b in ("uno-q-1", "uno-q-3", "uno-q-4"))
    assert client.get("/heatmap/latest", headers=KEY).json()["calibration"]["offsets_mode"] == "auto"
    client.put("/heatmap/config", json={"offsets_db": {b: 0 for b in BOARDS}}, headers=KEY)  # back to manual 0


def test_debug_is_fast_with_500_devices(client):
    client.put("/heatmap/config", json={"filter": {"edge_dbm": -85}}, headers=KEY)
    import random
    rng = random.Random(1)
    devs = {f"{i:016x}": [rng.uniform(-95, -55) if rng.random() > 0.1 else None for _ in BOARDS] for i in range(500)}
    ts = new_ts()
    t0 = time.perf_counter()
    snapshot(client, ts, devs)
    built = time.perf_counter() - t0
    t0 = time.perf_counter()
    dbg = client.get(f"/heatmap/debug?ts={iso(ts)}", headers=KEY).json()
    assert time.perf_counter() - t0 < 0.2 and len(dbg["devices"]) == 500
    assert built < 3


def test_privacy_prune(client):
    from app.core import db
    old = time.time() - 2 * 86400
    with db() as conn:
        conn.execute("INSERT INTO heatmap_devices (ts, id, rssi, boards_heard, weakest_dbm, x, y, pass_edge, status, "
                     "reason, kind) VALUES (?, 'old-id', '{}', 4, -60, 1, 1, 1, 'in_room', NULL, NULL)", (old,))
        conn.execute("INSERT INTO heatmap_tracks VALUES ('old-id', ?, ?)", (old, old))
        conn.execute("INSERT INTO readings (device, device_ts, received_at, data, measured_at, rssis) "
                     "VALUES ('uno-q-9', NULL, ?, ?, ?, '[-60]')", (iso(old), '{"devices": [{"id": "x", "rssi": -60, "n": 1}]}', old))
        heatmap.prune(conn, time.time())
        assert conn.execute("SELECT COUNT(*) FROM heatmap_devices WHERE id = 'old-id'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM heatmap_tracks WHERE id = 'old-id'").fetchone()[0] == 0
        row = conn.execute("SELECT data, rssis FROM readings WHERE device = 'uno-q-9'").fetchone()
        assert row["data"] == "{}" and row["rssis"] == "[-60]"


@pytest.mark.parametrize("method,path", [
    ("get", "/heatmap/latest"), ("get", "/heatmap/debug"), ("get", "/heatmap/history"), ("get", "/heatmap/config"),
    ("put", "/heatmap/config"), ("put", "/heatmap/mode"), ("put", "/heatmap/boards"),
    ("post", "/heatmap/calibrate/headcount"),
])
def test_heatmap_auth(client, method, path):
    kwargs = {"json": {}} if method in ("put", "post") else {}
    assert client.request(method, path, **kwargs).status_code == 401
