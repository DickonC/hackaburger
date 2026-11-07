# Sensor ↔ server API (draft)

How the two Arduino UNO Q boards send Bluetooth scans to the server, and what the server hands to the webapp and CO₂ model.
**Status:** draft for discussion. Nothing is implemented on the server side yet.

## Setup: two boards

| `sensor_id` | Where | Purpose |
|---|---|---|
| `unoq-room` | inside the room, roughly central | hears people in the room |
| `unoq-outside` | in the corridor or next room, near the door | hears people *outside*, so they can be excluded |

A phone is counted as **in the room** if the room board hears it more strongly than the outside board (and above a minimum signal).
The comparison happens on the **server**. The boards don't know about each other.

## Rules of the connection

- **The board always starts the connection.** It sends an HTTPS POST every `interval_s` seconds (default 10). The server can't contact the board.
  Eduroam blocks incoming connections.
- **Settings travel back in the reply.** To change a board's range or interval, the webapp stores new settings on the server, and the board picks them up from the reply to its next POST.
- **The server must be publicly reachable over HTTPS (port 443)**, e.g. a hosted service or a laptop behind Cloudflare Tunnel or ngrok.
- **Each board sends a secret token** in the `Authorization` header. The server rejects requests without a valid token.
- **All timestamps are UTC, ISO 8601.**

---

## 1. Board → server: `POST /api/v1/readings`

Sent by each board every `interval_s` seconds.

```http
POST /api/v1/readings HTTP/1.1
Host: hackaburger.example.com
Authorization: Bearer <board-token>
Content-Type: application/json
```

```json
{
  "sensor_id": "unoq-room",
  "seq": 1842,
  "window_start": "2026-11-07T17:20:00Z",
  "window_end":   "2026-11-07T17:20:10Z",
  "threshold_dbm": -75,
  "device_count": 41,
  "heard_total": 212,
  "devices": [
    { "id": "a3f9c2e1b7d04e12", "rssi": -52.0, "n": 9 },
    { "id": "7c41d09a5e2bf830", "rssi": -61.5, "n": 4 },
    { "id": "e09b44c1aa7f2d65", "rssi": -73.0, "n": 2 }
  ]
}
```

| Field | Type | Meaning |
|---|---|---|
| `sensor_id` | string | Which board sent this. |
| `seq` | int | Counts up by 1 per message (restarts at 0 after a reboot). Lets the server ignore duplicates. |
| `window_start`, `window_end` | time | The 10 s scan window this message covers. Windows are aligned to the clock (…:00, …:10, …:20), so both boards' windows line up. |
| `threshold_dbm` | int | The board's current range setting. |
| `device_count` | int | Devices this board alone counts within `threshold_dbm`. A fallback if the other board is offline. |
| `heard_total` | int | All devices heard, including far away ones. For information only. |
| `devices` | list | Every device heard in this window with RSSI ≥ `report_floor_dbm` (default −90). It deliberately includes devices beyond the threshold so the server can compare boards. |
| `devices[].id` | string | **Anonymised** device ID: the first 16 hex characters of `HMAC-SHA256(shared_secret, bluetooth_address)`. Both boards use the same secret, so the same phone gets the same `id` on both boards. The server never sees real addresses. |
| `devices[].rssi` | number | Median signal strength in this window, in dBm. Closer to 0 means closer. |
| `devices[].n` | int | Number of times the device was heard in this window. |

### Server → board: reply

```json
{
  "ok": true,
  "server_time": "2026-11-07T17:20:10.412Z",
  "config": {
    "enabled": true,
    "interval_s": 10,
    "threshold_dbm": -75,
    "report_floor_dbm": -90,
    "config_version": 3
  }
}
```

The board applies `config` straight away. If `config_version` is unchanged, nothing changes. `"enabled": false` means the board keeps checking in but sends no device data, so it can be switched on again from the webapp.

| Status | Meaning | What the board does |
|---|---|---|
| `200` | OK | Applies `config`. |
| `400` | Malformed message | Logs the error and drops the message. |
| `401` | Wrong or missing token | Logs the error and keeps retrying slowly (once a minute). |
| `5xx` / no connection | Server down or no Wi-Fi | **Keeps the message** and resends queued messages oldest first once the server is back. It keeps up to about 1 hour, then drops the oldest. |

---

## 2. What the server does with the two boards' messages

For each 10 s window, the server takes both boards' messages and, for every `id`:

1. Takes its RSSI from each board that heard it. A board that didn't hear it counts as −100.
2. Adds each board's calibration offset (see below).
3. The device is **in the room** if `room_rssi > outside_rssi` **and** `room_rssi ≥ threshold_dbm`.

Example for one window:

| `id` | `unoq-room` | `unoq-outside` | Result |
|---|---|---|---|
| `a3f9c2e1b7d04e12` | −52 | −78 | in the room |
| `7c41d09a5e2bf830` | −61.5 | — | in the room |
| `e09b44c1aa7f2d65` | −73 | −55 | outside (heard more strongly in the corridor) |
| `5d20f7b39c18aa04` | −80 | −84 | not counted (below the −75 threshold) |

→ `devices_in_room = 2`, `estimated_people = round(2 × people_factor)`.

If only one board's message arrived for a window, the server uses that board's `device_count` and marks the result `"quality": "single_sensor"`.

**Calibration (once, at setup):** put the same phone halfway between the two boards for a minute. The difference in average RSSI between the boards is the offset. Add it to the weaker board so both boards are compared fairly. `people_factor` (default 0.7) is calibrated against a real headcount.

---

## 3. Server → webapp / CO₂ model

### Latest occupancy: `GET /api/v1/occupancy/latest`

```json
{
  "room": "lab-3",
  "ts": "2026-11-07T17:20:10Z",
  "devices_in_room": 41,
  "estimated_people": 29,
  "quality": "two_sensors",
  "sensors": {
    "unoq-room":    { "online": true, "last_seen": "2026-11-07T17:20:10Z", "threshold_dbm": -75 },
    "unoq-outside": { "online": true, "last_seen": "2026-11-07T17:20:10Z", "threshold_dbm": -75 }
  }
}
```

A board is `online: false` if nothing has arrived from it for 3 intervals (30 s).

### History for the CO₂ model: `GET /api/v1/occupancy?from=…&to=…&step=60`

```json
{
  "room": "lab-3",
  "step_s": 60,
  "points": [
    { "ts": "2026-11-07T17:00:00Z", "estimated_people": 24 },
    { "ts": "2026-11-07T17:01:00Z", "estimated_people": 26 },
    { "ts": "2026-11-07T17:02:00Z", "estimated_people": 27 }
  ]
}
```

`step` averages the 10 s windows into larger steps (e.g. 60 s), which is the time step the CO₂ model uses.

### Change a board's settings: `PUT /api/v1/sensors/{sensor_id}/config`

```json
{ "threshold_dbm": -70, "interval_s": 10, "enabled": true }
```

The server increments `config_version`. The board picks the change up on its next POST, within one interval.

---

## 4. Try it with curl

Pretend to be the room board:

```bash
curl -X POST https://hackaburger.example.com/api/v1/readings \
  -H "Authorization: Bearer dev-token-room" \
  -H "Content-Type: application/json" \
  -d '{"sensor_id":"unoq-room","seq":1,"window_start":"2026-11-07T17:20:00Z","window_end":"2026-11-07T17:20:10Z","threshold_dbm":-75,"device_count":2,"heard_total":3,"devices":[{"id":"a3f9c2e1b7d04e12","rssi":-52,"n":9},{"id":"e09b44c1aa7f2d65","rssi":-73,"n":2}]}'
```

Then the corridor board for the same window:

```bash
curl -X POST https://hackaburger.example.com/api/v1/readings \
  -H "Authorization: Bearer dev-token-outside" \
  -H "Content-Type: application/json" \
  -d '{"sensor_id":"unoq-outside","seq":1,"window_start":"2026-11-07T17:20:00Z","window_end":"2026-11-07T17:20:10Z","threshold_dbm":-75,"device_count":1,"heard_total":2,"devices":[{"id":"e09b44c1aa7f2d65","rssi":-55,"n":6},{"id":"a3f9c2e1b7d04e12","rssi":-78,"n":3}]}'
```

`GET /api/v1/occupancy/latest` should now report `devices_in_room: 1`. Only `a3f9c2e1b7d04e12` is in the room, and `e09b44c1aa7f2d65` is heard more strongly in the corridor.

## Open questions for the team

- Who hosts the server, and where (hosted service or laptop with a tunnel)?
- Room name and IDs: one room for the demo, or should `room` come from server config per board?
- Does the CO₂ model want `estimated_people` or raw `devices_in_room` (and apply its own factor)?
- How long to keep history? One SQLite table of 10 s windows is about 8,640 rows per board per day, which is fine.
