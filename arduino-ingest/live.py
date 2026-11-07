"""Live terminal view of room occupancy. Runs inside the ingest container:

    ./live              # refresh every 2s, show last 15 readings
    ./live -n 30 -i 5   # show 30 readings, refresh every 5s
"""

import argparse
import json
import os
import sqlite3
import time
import urllib.request
from datetime import datetime, timezone

p = argparse.ArgumentParser()
p.add_argument("-n", type=int, default=15, help="readings to show")
p.add_argument("-i", type=float, default=2, help="refresh interval in seconds")
args = p.parse_args()


def ago(t: float) -> str:
    s = int(time.time() - t)
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    return f"{s // 3600}h {s % 3600 // 60}m ago"


def latest(device: str) -> dict:
    # Ask the running API so the numbers match what the webapp sees
    req = urllib.request.Request(f"http://localhost:8000/occupancy/latest?device={device}",
                                 headers={"X-API-Key": os.environ["API_KEY"]})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.load(r)


def fmt(v) -> str:
    return "-" if v is None else str(v)


def render() -> str:
    conn = sqlite3.connect("file:/data/ingest.db?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    devices = [r[0] for r in conn.execute("SELECT DISTINCT device FROM readings ORDER BY device")]
    rows = conn.execute(
        """SELECT id, device, measured_at, source, threshold_dbm, devices_in_range, heard_total,
           people_raw, data FROM readings ORDER BY id DESC LIMIT ?""", (args.n,)
    ).fetchall()
    total = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
    conn.close()

    now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    out = [f"HACKABURGER occupancy   {now}   ({total} readings, Ctrl+C to quit)", ""]
    if not rows:
        return "\n".join(out + ["No readings yet."])

    out.append(f"  {'NOW':<16} {'status':<8} {'people':>6} {'smooth':>6} {'in range':>8} {'heard':>6}  "
               f"{'range':>6}  {'cfg v':>5}  updated")
    for device in devices:
        try:
            l = latest(device)
        except Exception as e:
            out.append(f"  {device:<16} (couldn't read latest: {e})")
            continue
        c = l["config"]
        status = "online" if l["online"] else "OFFLINE"
        updated = ago(datetime.fromisoformat(l["ts"].replace("Z", "+00:00")).timestamp())
        out.append(f"  {device:<16} {status:<8} {fmt(l['people']):>6} {fmt(l['people_smoothed']):>6} "
                   f"{fmt(l['devices_in_range']):>8} {fmt(l['heard_total']):>6}  {c['threshold_dbm']:>4}dB  "
                   f"{c['config_version']:>5}  {updated}")

    out += ["", f"LAST {len(rows)} READINGS",
            f"  {'id':>5}  {'device':<16} {'measured':<9} {'people':>6} {'board':>5} {'in range':>8} "
            f"{'heard':>6}  {'range':>6}  source"]
    for r in rows:
        board = json.loads(r["data"]).get("people")
        t = datetime.fromtimestamp(r["measured_at"], timezone.utc).strftime("%H:%M:%S")
        people = None if r["people_raw"] is None else round(r["people_raw"], 1)
        out.append(f"  {r['id']:>5}  {r['device']:<16} {t:<9} {fmt(people):>6} {fmt(board):>5} "
                   f"{fmt(r['devices_in_range']):>8} {fmt(r['heard_total']):>6}  {r['threshold_dbm']:>4}dB  "
                   f"{fmt(r['source'])}")
    return "\n".join(out)


try:
    while True:
        frame = render()
        print("\033[H\033[2J" + frame, flush=True)
        time.sleep(args.i)
except KeyboardInterrupt:
    print()
