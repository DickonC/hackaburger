# Board ↔ server communication format

How the Arduino UNO Q boards send their Bluetooth scans to the server, and how the server sends settings back.
Built on the existing endpoint `https://hackaburger.cch-uk.tech/ingest/data`.

**Status:** proposal. Sending `{"people": n}` already works. The `config` reply and the `/ingest/config` endpoints below still need to be added on the server.

## The rule

The board **always starts the connection** (eduroam blocks incoming connections). Every `interval_s` seconds it sends one POST.
**The server's reply to that POST carries the board's settings.** The webapp changes settings on the server, and the board picks them up at its next POST.

```
board ── POST /ingest/data  {count + measurements} ──▶ server   (stores it)
board ◀── 201 {"stored": 1, "config": {...}} ───────── server   (board applies config)
webapp ── PUT /ingest/config/uno-q-1 {"threshold_dbm": -60} ──▶ server   (board gets it next round)
```

---

## 1. Board → server: `POST /ingest/data`

Headers:

```
X-API-Key: <API key, never commit it>
Content-Type: application/json
```

Body:

```json
{
  "device": "uno-q-1",
  "ts": "2026-11-07T18:20:05Z",
  "data": {
    "people": 29,
    "devices_in_range": 41,
    "heard_total": 212,
    "config_version": 3,
    "settings": { "threshold_dbm": -75, "window_s": 30, "people_factor": 0.7 },
    "devices": [
      { "id": "a3f9c2e1b7d04e12", "rssi": -52.0, "n": 9, "kind": "apple_device", "static": false },
      { "id": "7c41d09a5e2bf830", "rssi": -61.5, "n": 4, "kind": "earbuds", "static": false },
      { "id": "e09b44c1aa7f2d65", "rssi": -83.0, "n": 1, "kind": "unknown", "static": false }
    ]
  }
}
```

| Field | Type | Meaning |
|---|---|---|
| `device` | string | Board name: `uno-q-1`, `uno-q-2`, … |
| `ts` | string | When the board made this measurement, UTC ISO 8601. **Aligned to the clock:** always a whole multiple of `interval_s` (e.g. `…:20:00`, `…:20:10`), and boards are internet time-synced, so boards with the same `interval_s` produce messages with identical `ts` that describe the same moment. Join boards on `ts` for the heatmap. It can differ from the server's `received_at` when queued messages are resent. |
| `data.people` | int | Estimated people in range: `round(devices_in_range × people_factor)`. **This is the main number for the webapp.** |
| `data.devices_in_range` | int | Devices heard within `threshold_dbm` during the last `window_s` seconds. The LED matrix shows this number. |
| `data.heard_total` | int | All devices heard in the window, at any distance. For information only. |
| `data.config_version` | int | The config version the board was using when it measured. Lets the server see whether a change has arrived yet. |
| `data.settings` | object | The settings this count was calculated with, so old data stays interpretable after settings change. |
| `data.devices` | list | Raw measurements: **every device the board heard in the last `window_s` seconds, at any signal strength**, with no filtering by `threshold_dbm`. The server can recount with any range it likes. Only left out if `send_devices` is `false`. |
| `devices[].id` | string | **Anonymised** device ID: the first 16 hex characters of `HMAC-SHA256(secret, bluetooth_address)`. All boards use the same secret, so the same phone gets the same ID on every board. The server never sees real addresses. Note that phones change their address about every 15 minutes, so IDs don't track anyone long-term either. |
| `devices[].rssi` | number | Median signal strength in the window, in dBm. Closer to 0 means closer to the board. |
| `devices[].n` | int | How often the device was heard in the window. |
| `devices[].kind` | string | The board's guess at the **device category**, from the broadcast content (manufacturer data, service UUIDs, name hints). Only the category is sent, never the name or address. One of the values in the table below. |
| `devices[].static` | bool | `true` if the device uses a public, non-rotating Bluetooth address. That's typical of fixed devices and some laptops, and rare for phones. |

### Device categories (`kind`)

| `kind` | Typically | Counts as a person? |
|---|---|---|
| `apple_device` | iPhone, iPad, Mac, Apple Watch (Apple "Nearby Info", Handoff, AirDrop) | yes |
| `phone` | other phone signals | yes |
| `computer` | Windows laptops and PCs (Microsoft data) | yes |
| `wearable` | watches, fitness bands | yes |
| `brand_other` | Samsung/Google data without a clearer hint | yes |
| `unknown` | nothing recognisable. Many Android phones fall here. | yes |
| `earbuds` | AirPods, Beats, Fast Pair headphones | **no**, an accessory |
| `tracker` | AirTag / Find My, Tile, SmartTag, Chipolo | **no** |
| `beacon` | iBeacon, Eddystone | **no** |
| `fixed` | Apple TV and HomePod (AirPlay), TVs, printers, smart speakers | **no** |

**The board's own count** (`devices_in_range`, `people`, LED) already leaves out `earbuds`, `tracker`, `beacon` and `fixed`. The `devices` list still contains **all** devices with their `kind`, so the server can decide for itself.
In a first test at the venue, 84 devices broke down as 33 `apple_device`, 17 `computer`, 13 `unknown`, 8 `earbuds`, 7 `tracker` and 6 `fixed`.

In a busy venue the board hears 300–500 devices, which makes a message of roughly 15–25 KB. Every 5 s that's about 4 KB/s per board, which is fine. With `send_devices: false` a message is under 300 bytes.

---

## 2. Server → board: reply

```json
{
  "stored": 1,
  "config": {
    "config_version": 4,
    "enabled": true,
    "threshold_dbm": -60,
    "window_s": 10,
    "interval_s": 2,
    "people_factor": 0.7,
    "send_devices": true,
    "display": true,
    "send_every_s": 300
  }
}
```

### Settings (`config`)

| Field | Type | Default | Allowed | Meaning |
|---|---|---|---|---|
| `config_version` | int | 1 | ≥ 1 | Increased by the server on every change. The board only re-applies settings when it changes. |
| `enabled` | bool | `true` | | `false`: the board keeps checking in every `interval_s` but sends no measurements (`people` is `null`, no `devices`). |
| `threshold_dbm` | int | `-75` | `-100` … `-30` | **The range** for the board's own count (`devices_in_range`, `people`, LED display). Only devices at least this strong are counted. Does **not** filter `devices`. See the table below. |
| `window_s` | int | `30` | `2` … `300` | A device stays counted this long after it was last heard. Short = reacts fast, but the count jumps. Long = stable. |
| `interval_s` | number | `5` | `1` … `600` | Seconds between POSTs, and so also how often the LED display updates. Use 300 (5 min) while the backend is in development. |
| `people_factor` | number | `0.7` | `0.1` … `2.0` | People per device. Calibrate against a real headcount. |
| `send_devices` | bool | `true` | | Whether to include the raw `devices` list (all devices heard). |
| `display` | bool | `true` | | Show `devices_in_range` on the LED matrix. |
| `send_every_s` | int | `300` | `0` … `3600` | Seconds between POSTs, on clock boundaries (300 = every full 5 min). `0` = send at every `interval_s` (**live mode, needed for the heatmap**). Measuring and the LED still run every `interval_s`. |

**Range guide for `threshold_dbm`** (rough indoor values. Calibrate on site):

| `threshold_dbm` | Distance | Use |
|---|---|---|
| `-40` | arm's length | demo: phone on the board |
| `-55` | about 1–2 m | one table |
| `-65` | about 3–5 m | small room |
| `-75` | about 5–10 m | normal room (**default**) |
| `-85` | about 10–20 m, through walls | large hall |
| `-100` | everything the board can hear | no filter |

**Suggested presets:**

| Preset | `threshold_dbm` | `window_s` | `interval_s` |
|---|---|---|---|
| Room (default) | -75 | 30 | 5 |
| Demo (instant) | -40 | 6 | 1 |
| Whole venue | -100 | 30 | 5 |

### Rules for the board

- The board **ignores unknown fields** and **keeps the current value for missing fields**. So a reply of just `{"stored": 1}`, which is what the server returns today, changes nothing.
- Values outside the allowed range are clamped to the nearest allowed value. A wrongly typed value is ignored.
- The board saves the last config to a file, so after a reboot it starts with the latest settings even before the server answers.
- New settings take effect from the next measurement. The window is not reset, so the first count after a range change is already correct.

### Status codes

| Status | Meaning | What the board does |
|---|---|---|
| `201` | Stored | Applies `config`, if there is one. |
| `400` / `422` | Malformed message | Logs it and drops the message. Retrying wouldn't help. |
| `401` / `403` | Wrong or missing API key | Logs it and keeps measuring. Tries again once a minute. |
| `413` | Message too large | Resends without `devices`. |
| `5xx`, timeout, no Wi-Fi | Server or network problem | **Keeps the message** (up to 720 messages, about 1 hour at 5 s) and resends queued messages oldest first with the next successful POST. Drops the oldest when full. |

---

## 3. Webapp ↔ server

These endpoints are for the webapp, not the board. They use the same `X-API-Key` header.

### Latest count: `GET /ingest/data?device=uno-q-1&limit=1` (exists already)

```json
[
  {
    "id": 2,
    "device": "uno-q-1",
    "ts": "2026-11-07T18:20:05Z",
    "received_at": "2026-11-07T18:20:05.412Z",
    "data": { "people": 29, "devices_in_range": 41, "...": "..." }
  }
]
```

A board counts as **offline** if its last `received_at` is older than `3 × interval_s`.

### Read settings: `GET /ingest/config/uno-q-1` (new)

Returns the same `config` object the board gets.

### Change settings: `PUT /ingest/config/uno-q-1` (new)

Send only the fields to change:

```json
{ "threshold_dbm": -40, "window_s": 6, "interval_s": 1 }
```

The server validates them against the table above (`422` if invalid), increases `config_version`, and returns the full new `config`.
The board picks the change up on its next POST, so within one `interval_s`.

---

## 4. Example exchange

1. **Room mode.** The board sends `people: 29, devices_in_range: 41` with `settings.threshold_dbm: -75`. The server replies `stored: 1` with `config_version: 3` (no change).
2. **Switch to demo mode.** The webapp sends `PUT /ingest/config/uno-q-1 {"threshold_dbm": -40, "window_s": 6, "interval_s": 1}`. The server moves to `config_version: 4`.
3. **Next POST, within 5 s.** The board still sends its old-config count (`config_version: 3`). The reply carries `config_version: 4`, and the board applies it.
4. **Following POSTs, now every 1 s.** The board sends `config_version: 4`, `devices_in_range: 0`. A judge puts a phone on the board, and the next POST says `devices_in_range: 1`. The LED matrix and the webapp both show it within about 1–2 s.

## 5. Try it with curl

```bash
curl -X POST https://hackaburger.cch-uk.tech/ingest/data \
  -H "X-API-Key: $INGEST_API_KEY" -H "Content-Type: application/json" \
  -d '{"device":"uno-q-1","ts":"2026-11-07T18:20:05Z","data":{"people":1,"devices_in_range":1,"heard_total":3,"config_version":1,"settings":{"threshold_dbm":-75,"window_s":30,"people_factor":0.7},"devices":[{"id":"a3f9c2e1b7d04e12","rssi":-52,"n":9}]}}'
```

## Open questions for the team

- Does the server accept nested lists in `data`, and what's its size limit? This affects `send_devices`.
- Who builds `/ingest/config` and the `config` reply?
- Should the webapp and CO₂ model use `people` (board's estimate) or recalculate from `devices` with their own factor?
