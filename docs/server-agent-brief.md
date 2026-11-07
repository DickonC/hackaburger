# Task brief: occupancy processing on the Hackaburger ingest server

> **For an AI coding agent.** Read this whole file before changing code. The data format is defined in
> [`docs/server-api.md`](server-api.md) in the same repo. This brief says what to build on the server and exactly how to calculate counts.

## Context

- **The project:** two Arduino UNO Q boards scan for Bluetooth Low Energy (BLE) devices. The device counts estimate how many people are in a room, which feeds a CO₂ model and a window-opening recommendation.
- **Existing server:** `https://hackaburger.cch-uk.tech`, with
  - `POST /ingest/data`: stores a JSON body `{"device": str, "ts": str|null, "data": object}` and returns `201 {"stored": 1}`
  - `GET /ingest/data?device=…&limit=…`: returns stored rows `{id, device, ts, received_at, data}`
  - auth on every request: header `X-API-Key`, with `401` if missing or wrong
- **The boards** POST every `interval_s` seconds (default 5). The board always starts the connection, because the university network blocks incoming connections. **The only way to send settings to a board is in the reply to its POST.**
- **Two boards, used independently:**
  - `uno-q-1`: whole-room count, "room mode"
  - `uno-q-2`: small zone with an instant reaction for the live demo, "demo mode"

  Treat each board as its own zone. Don't merge their counts.

## What to build

1. **Extend `POST /ingest/data`:**
   - accept the bigger payload and validate it
   - store it as today
   - calculate the server-side count (see "Calculation")
   - **return the board's current `config` in the reply**
2. **Config storage per board,** plus `GET` and `PUT /ingest/config/{device}`.
3. **`GET /occupancy/latest?device=…`:** the current count for the webapp.
4. **`GET /occupancy/history?device=…&from=…&to=…&step_s=60[&threshold_dbm=…]`:** a time series for the CO₂ model, optionally recalculated with a different range.

**Constraints:**
- **Don't break today's behaviour.** A body with only `{"device": …, "data": {"people": n}}` must still be stored and return `201`.
- **Keep existing endpoints and auth unchanged.**
- **Never put the API key in the repo.** Read it from an environment variable.
- **Never store or log real Bluetooth addresses.** The boards only send anonymised `id`s, so this happens automatically as long as you don't add anything that reverses that.

---

## Incoming payload (from the board)

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
      { "id": "e09b44c1aa7f2d65", "rssi": -93.0, "n": 1 }
    ]
  }
}
```

- `devices` lists **every** device the board heard in the last `settings.window_s` seconds, **unfiltered**:
  - `id`: 16 hex characters, anonymised
  - `rssi`: median signal strength in dBm, a negative number where closer to 0 means closer
  - `n`: number of times the device was heard
- `devices` can hold 300–500 entries, so 15–25 KB per request. **Allow request bodies of at least 256 KB.**
- `people`, `devices_in_range`, `heard_total`, `config_version` and `settings` are the board's own values. Store them, but compute your own count from `devices` too (below).
- All fields inside `data` other than `people` may be missing. That's the old format, so handle it gracefully.
- `ts` is the board's measurement time (UTC). It can be older than `received_at` when the board resends queued messages. **Use `ts` for time series**, and fall back to `received_at` when `ts` is null.

**Validation (return `422` with a message if it fails):**
- `device`: a non-empty string of at most 64 characters, matching `[A-Za-z0-9_-]+`.
- `data.devices`, if present: a list of at most 5000 items.
  - each item has `id`: a string of 1–64 characters, `rssi`: a number in [−127, 0], and `n`: an int ≥ 1
  - drop invalid items rather than rejecting the whole message, and report the drop count in the reply as `"dropped": k`
- Body larger than the limit: `413`.

---

## Config (per board)

| Field | Type | Default | Allowed | Meaning |
|---|---|---|---|---|
| `config_version` | int | 1 | ≥ 1 | **Server-managed.** +1 on every successful change. |
| `enabled` | bool | `true` | | `false`: the board keeps checking in but sends no measurements. |
| `threshold_dbm` | int | −75 | −100 … −30 | **Range:** count a device only if `rssi ≥ threshold_dbm`. |
| `window_s` | int | 30 | 2 … 300 | How long the board keeps counting a device after it was last heard. |
| `interval_s` | number | 5 | 1 … 60 | Seconds between board POSTs. |
| `people_factor` | number | 0.7 | 0.1 … 2.0 | People per counted device. |
| `send_devices` | bool | `true` | | Whether the board includes `devices`. |
| `display` | bool | `true` | | Whether the board shows its count on its LED matrix. |

**Initial config:**
- `uno-q-1` (room mode): `threshold_dbm` −75, `window_s` 30, `interval_s` 5
- `uno-q-2` (demo mode): `threshold_dbm` −40, `window_s` 6, `interval_s` 1
- Any other new device name: the defaults above. Create its config on its first POST.

**Presets** the webapp can offer:

| Preset | `threshold_dbm` | `window_s` | `interval_s` |
|---|---|---|---|
| `room` | −75 | 30 | 5 |
| `demo` | −40 | 6 | 1 |
| `venue` | −100 | 30 | 5 |

**Range guide** (rough indoor values, for UI labels):

| `threshold_dbm` | label |
|---|---|
| −40 | arm's length |
| −55 | about 1–2 m (one table) |
| −65 | about 3–5 m (small room) |
| −75 | about 5–10 m (normal room) |
| −85 | about 10–20 m (large hall) |
| −100 | everything the board can hear |

### Config endpoints

- `GET /ingest/config/{device}` → `200` with the full config object.
- `PUT /ingest/config/{device}` with a **partial** object, e.g. `{"threshold_dbm": -40, "window_s": 6, "interval_s": 1}`:
  - Reject unknown fields, wrong types or out-of-range values with `422`. Don't clamp silently on the server.
  - The client must not set `config_version` (`422` if present).
  - Optionally accept `{"preset": "demo"}`, which applies that preset.
  - On success: increase `config_version` by 1, persist, and return `200` with the full new config.
- Every successful `POST /ingest/data` returns:
  ```json
  { "stored": 1, "config": { "config_version": 4, "enabled": true, "threshold_dbm": -40, "window_s": 6,
                             "interval_s": 1, "people_factor": 0.7, "send_devices": true, "display": true } }
  ```
  Always include the **full** config. The board ignores unknown fields and only re-applies when `config_version` changes.

---

## Calculation

Compute this for every stored message that has `data.devices`. Store the results next to the message, or compute them on read. Either is fine.

**Settings to use:**
- `threshold = config.threshold_dbm` and `factor = config.people_factor`, taken from the config **at the time the message was received**.
- For a recount via `?threshold_dbm=…`, use the requested value instead.

**Count for one message:**

```
in_range         = [d for d in data.devices if d.rssi >= threshold]
devices_in_range = len(in_range)
people_raw       = devices_in_range * factor
people           = round(people_raw)
```

- Don't deduplicate further. Each `id` is already unique within a message.
- Don't use `data.people` from the board for the server's own numbers. Keep it only for comparison.

**Smoothed value for the webapp.** Take the median of `people_raw` over the messages of this device whose `ts` falls within the last `smooth_s` seconds:
- `smooth_s` defaults to 30, and to 0 (no smoothing) when the board's current `window_s` ≤ 10, so demo mode stays instant.
- `people_smoothed = round(median(people_raw over that period))`.

**Fallback for messages without `devices`** (the old format, or `send_devices: false`): use `data.devices_in_range` if present, otherwise `data.people`, otherwise skip the message. Mark the result `"source": "board"` instead of `"server"`.

**Online status:** the board is online if `now - last received_at ≤ 3 × config.interval_s + 5 s`.

### Worked example (use as a test)

Config `threshold_dbm = -75`, `people_factor = 0.7`. The message contains:

| id | rssi | counted? |
|---|---|---|
| a3f9… | −52.0 | yes |
| 7c41… | −61.5 | yes |
| 11aa… | −75.0 | yes (`≥`, boundary included) |
| 22bb… | −75.5 | no |
| e09b… | −93.0 | no |

→ `devices_in_range = 3`, `people_raw = 2.1`, `people = 2`.
Recount with `?threshold_dbm=-95` gives `devices_in_range = 5`, `people = round(3.5) = 4`. Use round-half-to-even or half-up, but document which and be consistent.

---

## Read endpoints for the webapp and CO₂ model

`GET /occupancy/latest?device=uno-q-1`:

```json
{
  "device": "uno-q-1",
  "ts": "2026-11-07T18:20:05Z",
  "online": true,
  "devices_in_range": 41,
  "people": 29,
  "people_smoothed": 28,
  "heard_total": 212,
  "source": "server",
  "config": { "config_version": 3, "threshold_dbm": -75, "window_s": 30, "interval_s": 5, "people_factor": 0.7 }
}
```

`GET /occupancy/history?device=uno-q-1&from=2026-11-07T17:00:00Z&to=2026-11-07T18:00:00Z&step_s=60[&threshold_dbm=-65]`:

- Group messages into `step_s` buckets by `ts`. A bucket's value is the **mean** of `people_raw` in that bucket, rounded to one decimal place.
- With `threshold_dbm`, recount every message using that value. This only works for messages that have `devices`. Leave other buckets out.
- Return empty buckets as `null` so gaps stay visible to the CO₂ model.

```json
{ "device": "uno-q-1", "step_s": 60, "threshold_dbm": -75,
  "points": [ { "ts": "2026-11-07T17:00:00Z", "people": 24.3 },
              { "ts": "2026-11-07T17:01:00Z", "people": null },
              { "ts": "2026-11-07T17:02:00Z", "people": 26.0 } ] }
```

---

## Acceptance checks

1. **Old format still works:** `POST {"device":"uno-q-1","data":{"people":7}}` → `201`, the reply contains `stored: 1` and a full `config`, and the row shows up in `GET /ingest/data`.
2. **The worked example above** gives `devices_in_range = 3` and `people = 2`. With `?threshold_dbm=-95` it gives 5 and 4.
3. **Config change reaches the board:** `PUT /ingest/config/uno-q-1 {"threshold_dbm": -40}` → `config_version` goes up by 1, and the reply to the next `POST` from `uno-q-1` carries `threshold_dbm: -40`. A POST from `uno-q-2` is unaffected.
4. **Validation:**
   - `PUT` with `{"threshold_dbm": -20}` → `422`, config unchanged
   - `PUT` with `{"foo": 1}` → `422`
   - a POST with one invalid device item → `201`, with `dropped: 1`
5. **Large message:** a POST with 500 devices (about 25 KB) → `201`, handled in under 300 ms.
6. **Online status:** `online` becomes `false` once no message has arrived for `3 × interval_s + 5 s`.
7. **Resent messages:** a POST whose `ts` is 10 minutes old lands in the right history bucket (by `ts`, not by `received_at`).
8. **Auth:** every new endpoint returns `401` without a valid `X-API-Key`.

## Out of scope (don't build now)

- Combining the two boards into one count, or comparing them to place a device.
- Smarter people estimation than `devices × people_factor`.
- Pushing to boards. Boards can't be reached, and settings only travel in POST replies.
