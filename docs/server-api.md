# Board ↔ server communication format

How the Arduino UNO Q boards send their Bluetooth scans to the server, and how the server sends settings back.
Built on the existing endpoint `https://hackaburger.cch-uk.tech/ingest/data`.

**Status:** live on the server (`arduino-ingest/`). Everything below is implemented, including the `config` reply, `/ingest/config` and the `/ingest/occupancy/*` read endpoints. The server also replies with `"dropped": k`, the number of invalid `devices` items it ignored.

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
      { "id": "a3f9c2e1b7d04e12", "rssi": -52.0, "n": 9 },
      { "id": "7c41d09a5e2bf830", "rssi": -61.5, "n": 4 },
      { "id": "e09b44c1aa7f2d65", "rssi": -83.0, "n": 1 }
    ]
  }
}
```

| Field | Type | Meaning |
|---|---|---|
| `device` | string | Board name: `uno-q-1`, `uno-q-2`, … |
| `ts` | string | When the board made this measurement, UTC ISO 8601. It can differ from the server's `received_at` when queued messages are resent. |
| `data.people` | int | Estimated people in range: `round(devices_in_range × people_factor)`. **This is the main number for the webapp.** |
| `data.devices_in_range` | int | Devices heard within `threshold_dbm` during the last `window_s` seconds. The LED matrix shows this number. |
| `data.heard_total` | int | All devices heard in the window, at any distance. For information only. |
| `data.config_version` | int | The config version the board was using when it measured. Lets the server see whether a change has arrived yet. |
| `data.settings` | object | The settings this count was calculated with, so old data stays interpretable after settings change. |
| `data.devices` | list | Raw measurements: **every device the board heard in the last `window_s` seconds, at any signal strength**, with no filtering by `threshold_dbm`. The server can recount with any range it likes. Only left out if `send_devices` is `false`. |
| `devices[].id` | string | **Anonymised** device ID: the first 16 hex characters of `HMAC-SHA256(secret, bluetooth_address)`. All boards use the same secret, so the same phone gets the same ID on every board. The server never sees real addresses. Note that phones change their address about every 15 minutes, so IDs don't track anyone long-term either. |
| `devices[].rssi` | number | Median signal strength in the window, in dBm. Closer to 0 means closer to the board. |
| `devices[].n` | int | How often the device was heard in the window. |

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
    "display": true
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
| `interval_s` | number | `5` | `1` … `60` | Seconds between POSTs, and so also how often the LED display updates. |
| `people_factor` | number | `0.7` | `0.1` … `2.0` | People per device. Calibrate against a real headcount. |
| `send_devices` | bool | `true` | | Whether to include the raw `devices` list (all devices heard). |
| `display` | bool | `true` | | Show `devices_in_range` on the LED matrix. |

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

### Latest count: `GET /ingest/occupancy/latest?device=uno-q-1`

The server's own count, recalculated from `devices` with the board's config at the time of receipt:

```json
{
  "device": "uno-q-1", "ts": "2026-11-07T18:20:05Z", "online": true,
  "devices_in_range": 41, "people": 29, "people_smoothed": 28, "heard_total": 212,
  "source": "server", "board_people": 29,
  "config": { "config_version": 3, "threshold_dbm": -75, "...": "..." }
}
```

- `people_smoothed` is the median over the last 30 s of measurements, or the same as `people` when `window_s` ≤ 10 (demo mode).
- `source` is `"server"` when counted from `devices`, or `"board"` for messages without `devices`. Those use `devices_in_range × people_factor`, otherwise `people`.
- `board_people` is the board's own `people` value, for comparison.
- **Online** means the last message arrived within `3 × interval_s + 5 s`.
- **Rounding** is half up (2.5 → 3).
- Returns `404` if the board has never sent anything.

### Time series: `GET /ingest/occupancy/history?device=uno-q-1&from=…&to=…&step_s=60[&threshold_dbm=-65]`

- `from` and `to` are ISO times. The defaults are the last hour up to now.
- Messages are grouped by their `ts` into `step_s` buckets. Each bucket is the mean of the unrounded people estimate, to 1 decimal place. Empty buckets are `null`.
- `threshold_dbm` recounts every message at that range. Messages without `devices` are left out.
- At most 10 000 points per request.

```json
{ "device": "uno-q-1", "step_s": 60, "threshold_dbm": null,
  "points": [ { "ts": "2026-11-07T17:00:00Z", "people": 24.3 }, { "ts": "2026-11-07T17:01:00Z", "people": null } ] }
```

The raw stored messages are still available at `GET /ingest/data?device=uno-q-1&limit=1`.

### Read settings: `GET /ingest/config/uno-q-1`

Returns the same `config` object the board gets.

### Change settings: `PUT /ingest/config/uno-q-1`

Send only the fields to change:

```json
{ "threshold_dbm": -40, "window_s": 6, "interval_s": 1 }
```

The server validates them against the table above, increases `config_version`, and returns the full new `config`. `{"preset": "room" | "demo" | "venue"}` applies a preset.
It returns `422` for unknown fields, wrong types, out-of-range values or `config_version`, and doesn't clamp.
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

## Answered questions

- **Nested lists and size limit:** nested JSON is fine. The limit is **256 KB per request**, which is 10× a 500-device message. Above that, nginx returns `413`.
- **Who builds `/ingest/config` and the `config` reply:** done, in `arduino-ingest/app/main.py`.
- **`people` or a recount:** use the server's numbers from `/ingest/occupancy/*`. They're counted from `devices`, so a range change in `history?threshold_dbm=` also applies to old data. The board's own value is kept as `board_people`.
