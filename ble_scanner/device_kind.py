"""Guess what kind of device a BLE advertisement comes from.

Only the *category* is sent to the server, never the address or name.
The point is to drop obvious non-people (earbuds, trackers, beacons, fixed
devices) from the count. It is a heuristic: many devices stay "unknown", and
"unknown" devices are still counted.

Kinds (see docs/server-api.md):
  apple_device  Apple Nearby Info / Handoff / AirDrop etc.: iPhone, iPad, Mac, Watch
  phone         other phone-like signals (e.g. exposure-notification service)
  computer      Microsoft (Windows Swift Pair / CDP): laptops, PCs
  wearable      heart-rate / fitness services, watch names
  earbuds       AirPods/Beats proximity pairing, Google Fast Pair, headphone names
  tracker       Apple Find My, Tile, Samsung SmartTag, Chipolo
  beacon        iBeacon / Eddystone
  fixed         AirPlay targets, TVs, printers, speakers that stay put
  brand_other   Samsung / Google manufacturer data without a clearer hint
  unknown       nothing recognisable
"""

import re

# Bluetooth SIG company identifiers
APPLE, MICROSOFT, SAMSUNG, GOOGLE = 0x004C, 0x0006, 0x0075, 0x00E0

# Apple Continuity message types (first byte of each TLV in Apple manufacturer data)
APPLE_TYPES = {
    0x02: "beacon",        # iBeacon
    0x05: "apple_device",  # AirDrop
    0x07: "earbuds",       # proximity pairing (AirPods, Beats)
    0x09: "fixed",         # AirPlay target (Apple TV, HomePod)
    0x0A: "fixed",         # AirPlay source
    0x0C: "apple_device",  # Handoff
    0x0F: "apple_device",  # Nearby Action
    0x10: "apple_device",  # Nearby Info (iPhones send this constantly)
    0x12: "tracker",       # Find My (AirTags, separated devices)
}

# 16-bit service UUIDs, as they appear in full 128-bit form
def _uuid16(u: str) -> str:
    return f"0000{u}-0000-1000-8000-00805f9b34fb"

SERVICE_KINDS = {
    _uuid16("fe2c"): "earbuds",   # Google Fast Pair (mostly headphones)
    _uuid16("feed"): "tracker",   # Tile
    _uuid16("fd84"): "tracker",   # Tile
    _uuid16("fd5a"): "tracker",   # Samsung SmartTag
    _uuid16("fe33"): "tracker",   # Chipolo
    _uuid16("feaa"): "beacon",    # Eddystone (incl. Google Find Hub tags)
    _uuid16("fd6f"): "phone",     # Exposure notification
    _uuid16("180d"): "wearable",  # Heart rate
    _uuid16("1814"): "wearable",  # Running speed and cadence
}

NAME_HINTS = [
    (re.compile(r"airpods|buds|beats|wh-|wf-|jbl|bose|headphone|earbud|soundcore", re.I), "earbuds"),
    (re.compile(r"\btv\b|\[tv\]|bravia|roku|chromecast|fire ?tv|printer|deskjet|laserjet|"
                r"officejet|epson|sonos|homepod|echo dot|echo show|nest (mini|hub|audio)", re.I), "fixed"),
    (re.compile(r"watch|\bband\b|fitbit|garmin|amazfit|whoop", re.I), "wearable"),
    (re.compile(r"tile|smarttag|chipolo|airtag", re.I), "tracker"),
]

# Order of preference when one device sends several kinds of advertisement
_RANK = {"unknown": 0, "brand_other": 1, "phone": 2, "computer": 2, "apple_device": 3,
         "wearable": 3, "earbuds": 4, "tracker": 4, "beacon": 4, "fixed": 4}

EXCLUDED_KINDS = {"earbuds", "tracker", "beacon", "fixed"}


def _apple_kind(data: bytes) -> str | None:
    """Walk the TLVs in Apple manufacturer data; return the most telling kind.

    Macs advertise as AirPlay receivers too, so a personal-device message
    (Nearby Info, Handoff, AirDrop, Nearby Action) beats "fixed".
    """
    found = set()
    i = 0
    while i + 1 < len(data):
        t, ln = data[i], data[i + 1]
        k = APPLE_TYPES.get(t)
        if k:
            found.add(k)
        i += 2 + ln
    if not found:
        return None
    if "apple_device" in found and "fixed" in found:
        found.discard("fixed")
    return max(found, key=lambda k: _RANK[k])


def classify(manufacturer_data: dict, service_uuids, name: str | None) -> str:
    kinds = []
    if APPLE in manufacturer_data:
        kinds.append(_apple_kind(bytes(manufacturer_data[APPLE])) or "apple_device")
    if MICROSOFT in manufacturer_data:
        kinds.append("computer")
    if SAMSUNG in manufacturer_data or GOOGLE in manufacturer_data:
        kinds.append("brand_other")
    for u in service_uuids or ():
        k = SERVICE_KINDS.get(str(u).lower())
        if k:
            kinds.append(k)
    if name:
        for rx, k in NAME_HINTS:
            if rx.search(name):
                kinds.append(k)
                break
    if not kinds:
        return "unknown"
    return max(kinds, key=lambda k: _RANK[k])


def better(old: str | None, new: str) -> str:
    """Keep the most specific kind seen for a device.

    Exception: once a device has shown personal-device signals (apple_device),
    an AirPlay advertisement doesn't turn it into "fixed" (Macs do both).
    """
    if old is None:
        return new
    if {old, new} == {"apple_device", "fixed"}:
        return "apple_device"
    return new if _RANK[new] > _RANK[old] else old
