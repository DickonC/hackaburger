#!/usr/bin/env python3
"""BLE occupancy scanner for the Arduino UNO Q.

Scans continuously for Bluetooth Low Energy advertisements and every
`interval_s` seconds:
  - counts devices whose median RSSI over the last `window_s` seconds is
    >= `threshold_dbm` (the range) and estimates people from that
  - prints the count (and with -v every counted device)
  - with --display: shows the count on the UNO Q LED matrix
  - with --send: POSTs the count plus every device heard (anonymised) to the
    ingest server and applies the settings the server replies with
    (format: docs/server-api.md)

Settings start from built-in defaults, then the last config saved from the
server (config.json, only with --send), then any flags given on the command
line. With --send the server's replies can change them at any time.

Server credentials live in ble_scanner/.env (never commit it):
  INGEST_API_KEY=...          required for --send
  INGEST_DEVICE=uno-q-1       board name on the server
  INGEST_ID_SECRET=...        shared by all boards; anonymises device addresses
  INGEST_URL=...              optional, defaults to the Hackaburger server

Usage:
  python3 scan.py -v                                   # local only
  python3 scan.py --display --threshold -40 --window 6 --interval 1
  python3 scan.py --display --send                     # agreed server format
"""

import argparse
import asyncio
import statistics
import time
from collections import deque
from datetime import datetime, timezone

from bleak import BleakScanner

from mcu_display import McuDisplay
from server_client import (DEFAULT_URL, DeviceIdHasher, ServerClient, default_config,
                           load_env, load_saved_config, merge_config, save_config)

MAX_SAMPLES_PER_DEVICE = 300


class OccupancyScanner:
    """Keeps (time, rssi) samples per device for the last `window_s` seconds."""

    def __init__(self, window_s: float):
        self.window_s = window_s
        self.samples: dict[str, deque] = {}
        self.names: dict[str, str] = {}

    def on_advertisement(self, device, adv):
        q = self.samples.get(device.address)
        if q is None:
            q = self.samples[device.address] = deque(maxlen=MAX_SAMPLES_PER_DEVICE)
        q.append((time.time(), adv.rssi))
        name = adv.local_name or device.name
        if name:
            self.names[device.address] = name

    def snapshot(self) -> list[tuple[str, float, int]]:
        """[(address, median_rssi, n)] for every device heard in the window, strongest first."""
        cutoff = time.time() - self.window_s
        out = []
        for addr in list(self.samples):
            q = self.samples[addr]
            while q and q[0][0] < cutoff:
                q.popleft()
            if not q:
                del self.samples[addr]
                self.names.pop(addr, None)
                continue
            out.append((addr, statistics.median(r for _, r in q), len(q)))
        out.sort(key=lambda d: d[1], reverse=True)
        return out


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_message(device: str, cfg: dict, heard, in_range, hasher) -> dict:
    data = {
        "people": None,
        "devices_in_range": len(in_range),
        "heard_total": len(heard),
        "config_version": cfg["config_version"],
        "settings": {
            "threshold_dbm": cfg["threshold_dbm"],
            "window_s": cfg["window_s"],
            "people_factor": cfg["people_factor"],
        },
    }
    if cfg["enabled"]:
        data["people"] = round(len(in_range) * cfg["people_factor"])
        if cfg["send_devices"]:
            data["devices"] = [
                {"id": hasher(addr), "rssi": round(rssi, 1), "n": n}
                for addr, rssi, n in heard
            ]
    else:
        data["devices_in_range"] = None
    return {"device": device, "ts": utc_now(), "data": data}


async def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--threshold", type=int, help="range: min median RSSI in dBm to count a device (default -75)")
    p.add_argument("--window", type=int, help="seconds a device stays counted after it was last heard (default 30)")
    p.add_argument("--interval", type=float, help="seconds between reports/POSTs (default 5)")
    p.add_argument("--people-factor", type=float, help="people per counted device (default 0.7)")
    p.add_argument("--display", action="store_true", help="show the in-range count on the UNO Q LED matrix")
    p.add_argument("--send", action="store_true", help="POST to the ingest server (needs .env)")
    p.add_argument("--device", help="board name on the server (overrides INGEST_DEVICE)")
    p.add_argument("--send-interval", type=float,
                   help="POST at most every N seconds (local override, e.g. 300 while the backend "
                        "is in development); display still updates every interval")
    p.add_argument("-v", "--verbose", action="store_true", help="list each counted device")
    args = p.parse_args()

    # Settings: defaults -> saved server config -> command-line flags.
    cfg = default_config()
    if args.send:
        saved = load_saved_config()
        if saved:
            cfg = saved
    cli = {"threshold_dbm": args.threshold, "window_s": args.window,
           "interval_s": args.interval, "people_factor": args.people_factor}
    cfg, _ = merge_config(cfg, {k: v for k, v in cli.items() if v is not None})

    client = hasher = None
    device = "local"
    if args.send:
        env = load_env()
        missing = [k for k in ("INGEST_API_KEY", "INGEST_ID_SECRET") if not env.get(k)]
        if missing:
            raise SystemExit(f"--send needs {', '.join(missing)} in {__file__.rsplit('/', 1)[0]}/.env")
        device = args.device or env.get("INGEST_DEVICE") or "uno-q-1"
        client = ServerClient(env.get("INGEST_URL") or DEFAULT_URL, env["INGEST_API_KEY"], device)
        hasher = DeviceIdHasher(env["INGEST_ID_SECRET"])

    display = McuDisplay() if args.display else None
    scanner = OccupancyScanner(cfg["window_s"])

    send_desc = "off"
    if client:
        send_desc = f"every {args.send_interval:g}s" if args.send_interval else "every interval"
    print(f"Scanning as '{device}'... (Ctrl+C to stop)  threshold={cfg['threshold_dbm']} dBm, "
          f"window={cfg['window_s']}s, interval={cfg['interval_s']}s, "
          f"send={send_desc}, display={'on' if display else 'off'}")
    # First POST goes out after the first interval, then every --send-interval.
    last_send = float("-inf")

    async with BleakScanner(detection_callback=scanner.on_advertisement):
        while True:
            await asyncio.sleep(cfg["interval_s"])
            scanner.window_s = cfg["window_s"]
            heard = scanner.snapshot()
            in_range = [d for d in heard if d[1] >= cfg["threshold_dbm"]]
            people = round(len(in_range) * cfg["people_factor"])

            stamp = time.strftime("%H:%M:%S")
            state = "" if cfg["enabled"] else "  [disabled by server]"
            print(f"\n[{stamp}] in range: {len(in_range)} devices "
                  f"(of {len(heard)} heard, threshold {cfg['threshold_dbm']} dBm) "
                  f"-> ~{people} people{state}")
            if args.verbose:
                for addr, rssi, n in in_range:
                    print(f"   {rssi:6.1f} dBm  n={n:<3} {addr}  {scanner.names.get(addr, '-')}")

            if display and cfg["display"]:
                display.show_count(len(in_range) if cfg["enabled"] else 0)

            due = (args.send_interval is None
                   or time.monotonic() - last_send >= args.send_interval - 0.5)
            if client and due:
                last_send = time.monotonic()
                msg = build_message(device, cfg, heard, in_range, hasher)
                new_cfg = await asyncio.to_thread(client.send, msg)
                print(f"   [server] {client.last_status}")
                if new_cfg is not None:
                    merged, changed = merge_config(cfg, new_cfg)
                    if changed:
                        cfg = merged
                        save_config(cfg)
                        print("   [server] new settings: " +
                              ", ".join(f"{k}={cfg[k]}" for k in changed))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
