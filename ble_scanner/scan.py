#!/usr/bin/env python3
"""Local BLE occupancy scanner (prints only, sends nothing over the network).

Runs on the Arduino UNO Q's Linux side (or any machine with Bluetooth, e.g. a Mac
for testing). It continuously scans for BLE advertisements, smooths each device's
RSSI, keeps devices that are "close enough", and periodically prints:
  - the devices currently counted, with their smoothed RSSI
  - the device count and a rough people estimate

Range control: --threshold (dBm). Devices whose smoothed RSSI is weaker (more
negative) than this are ignored. Rough guide: -60 ~ 1-3 m, -70 ~ 3-6 m,
-80 ~ 8-15 m. Calibrate in your actual room.

Usage:
  python3 scan.py                        # defaults
  python3 scan.py --threshold -70 -v     # tighter range, list every device
  python3 scan.py --display              # also show the count on the LED matrix
"""

import argparse
import asyncio
import statistics
import time
from collections import deque
from dataclasses import dataclass, field

from bleak import BleakScanner

from mcu_display import McuDisplay


@dataclass
class DeviceTrack:
    address: str
    name: str | None = None
    rssis: deque = field(default_factory=lambda: deque(maxlen=10))
    last_seen: float = 0.0

    def smoothed_rssi(self) -> float:
        # Median is robust against the frequent +/-10 dB spikes in BLE RSSI.
        return statistics.median(self.rssis)


class OccupancyScanner:
    def __init__(self, threshold: int, window: float):
        self.threshold = threshold
        self.window = window  # seconds a device stays counted after last advert
        self.devices: dict[str, DeviceTrack] = {}

    def on_advertisement(self, device, adv):
        track = self.devices.get(device.address)
        if track is None:
            track = self.devices[device.address] = DeviceTrack(device.address)
        track.name = adv.local_name or device.name or track.name
        track.rssis.append(adv.rssi)
        track.last_seen = time.time()

    def prune(self):
        cutoff = time.time() - self.window
        for addr in [a for a, t in self.devices.items() if t.last_seen < cutoff]:
            del self.devices[addr]

    def in_range(self) -> list[DeviceTrack]:
        self.prune()
        tracks = [t for t in self.devices.values() if t.smoothed_rssi() >= self.threshold]
        return sorted(tracks, key=lambda t: t.smoothed_rssi(), reverse=True)


def report(scanner: OccupancyScanner, people_factor: float, verbose: bool,
           display: McuDisplay | None = None):
    near = scanner.in_range()
    total = len(scanner.devices)
    people = round(len(near) * people_factor)
    stamp = time.strftime("%H:%M:%S")
    print(f"\n[{stamp}] in range: {len(near)} devices "
          f"(of {total} heard, threshold {scanner.threshold} dBm) "
          f"-> ~{people} people")
    if verbose:
        for t in near:
            name = t.name or "-"
            print(f"   {t.smoothed_rssi():6.1f} dBm  {t.address}  {name}")
    if display:
        display.show_count(len(near))


async def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--threshold", type=int, default=-75,
                   help="min smoothed RSSI in dBm to count a device (default -75)")
    p.add_argument("--window", type=float, default=60,
                   help="seconds a device stays counted after its last advert (default 60)")
    p.add_argument("--interval", type=float, default=10,
                   help="seconds between reports (default 10)")
    p.add_argument("--people-factor", type=float, default=0.7,
                   help="people per counted device, calibrate vs a real headcount (default 0.7)")
    p.add_argument("--display", action="store_true",
                   help="show the in-range device count on the UNO Q LED matrix")
    p.add_argument("-v", "--verbose", action="store_true", help="list each counted device")
    args = p.parse_args()

    scanner = OccupancyScanner(args.threshold, args.window)
    display = McuDisplay() if args.display else None
    print(f"Scanning... (Ctrl+C to stop)  threshold={args.threshold} dBm, "
          f"window={args.window}s, report every {args.interval}s")
    async with BleakScanner(detection_callback=scanner.on_advertisement):
        while True:
            await asyncio.sleep(args.interval)
            report(scanner, args.people_factor, args.verbose, display)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
