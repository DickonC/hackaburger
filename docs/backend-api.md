# Backend API: rooms, CO₂ and forecast

How the frontend (`index.html`) gets real data. The board ↔ server format is in [`server-api.md`](server-api.md); this file covers everything else.

**Base URL:** `https://hackaburger.cch-uk.tech/ingest`

**Headers on every call:** `X-API-Key: <key>` and, for bodies, `Content-Type: application/json`.

Interactive docs, where you can try every call: `https://hackaburger.cch-uk.tech/ingest/docs`.

## How the pieces fit

```
board (uno-q-1) ──POST /data──▶ readings ──┐  people from BLE devices in range (threshold_dbm)
                                           ├─▶ room occupancy (board data wins, history fills gaps)
occupancy_history (synthetic/imported) ────┘        │
                                                    ├─▶ CO₂ estimate (mass balance, room volume + windows)
rooms (volume, windows, capacity, device) ──────────┤
window_events (open/close) ─────────────────────────┘
                                                    └─▶ forecast: occupancy_forecaster calendar model
                                                        + live anchor → people and CO₂ for the next 24 h
```

- **There is no CO₂ sensor.** All CO₂ values are estimated from people and ventilation, using the frontend's `stepMassBalanceCO2` formula.
- **The forecaster predicts people.** CO₂ is calculated from those predicted people.
- **The forecaster needs at least 4 weeks of 5-minute history for a room.** Only `appleton` has that (the synthetic data). Other rooms fall back to holding the current count steady.

## Database tables

| Table | What |
|---|---|
| `readings` | Every board message, plus the server's people count (see `server-api.md`) |
| `configs` | Per-board settings, e.g. `threshold_dbm` |
| `rooms` | One JSON row per room: name, volume, windows, capacity, the `device` (board) in it, CO₂ parameters |
| `occupancy_history` | Historic people per timestamp per room, with `source`: `synthetic`, `manual`, … |
| `window_events` | Every time windows were opened or closed: which window indices are open from that moment |

## Rooms

A room looks like this:

```json
{
  "id": "appleton", "name": "Appleton Tower — Study Pod 4.02",
  "volume_m3": 65.0, "capacity": 32, "device": "uno-q-1", "scheduled_count": 12,
  "windows": [{"name": "Win 01 (North)", "area_m2": 1.0, "is_open": false}, {"...": "..."}],
  "open_area_m2": 0.0,
  "co2_gen_lph": 16.0, "outdoor_ppm": 420.0, "infiltration_ach": 0.25, "k_flow": 350.0,
  "timezone": "Europe/London",
  "history": {"points": 20160, "from": "...", "to": "...", "enough_for_forecast": true}
}
```

| Call | What |
|---|---|
| `GET /rooms` | All rooms. The frontend's 3 presets are pre-created |
| `POST /rooms` | Create a room. Needs `name`, `volume_m3` and `windows` (`[{"name", "area_m2"}]`); optional `id` and the other fields above |
| `GET /rooms/{id}` | One room |
| `PUT /rooms/{id}` | Change any fields (partial update), e.g. `{"device": "uno-q-2"}` |
| `DELETE /rooms/{id}` | Delete the room, its history and its window events |
| `PUT /rooms/{id}/windows` | `{"open": [0, 2]}`: these windows are open from now on. `[]` closes all of them |

Rules:
- Window `area_m2` must be between 0.1 and 20. The frontend currently calls this field `area`.
- `capacity` caps the forecast.
- `device` links the room to a board. That board's live count is used for this room.

## People and history

| Call | What |
|---|---|
| `GET /rooms/{id}/occupancy?from=&to=&step_s=300` | People per bucket, `source`: `sensor` (board), `history` or `null` (no data). Default: the last 24 h |
| `GET /rooms/{id}/history?from=&to=` | Raw stored history points |
| `POST /rooms/{id}/history` | `{"source": "manual", "points": [{"ts": "2026-11-07T09:00:00Z", "people": 12}]}`. Replaces points with the same `ts` |
| `DELETE /rooms/{id}/history[?source=synthetic]` | Delete history, optionally only from one source |

## CO₂

`GET /rooms/{id}/co2?from=&to=&step_s=300` returns the estimated CO₂ timeline. The default is the last 6 hours.

```json
{"points": [{"ts": "...", "co2_ppm": 980, "people": 8.0, "people_source": "sensor", "open_area_m2": 0.0}]}
```

- `people` is the count used for the 5 minutes after `ts`.
- `people_source: "assumed"` means there was no data, so the forecaster's usual value for that time was used.
- The simulation starts at outdoor level 12 hours before `from`.

## Forecast

`GET /rooms/{id}/forecast?hours=24[&as_weekday=wed]` returns one point every 5 minutes from now:

```json
{
  "model": "historical_time_of_week+live_anchor", "live_people": 9.1, "capacity": 32, "as_weekday": null,
  "summary": {"co2_now": 1180, "open_area_m2": 0.0, "peak_co2": 1650, "peak_at": "...", "first_above_1000_at": "..."},
  "points": [{"ts": "...", "people": 9.0, "people_lower": 6.7, "people_upper": 11.3,
              "co2_ppm": 1190, "co2_lower": 1170, "co2_upper": 1210,
              "co2_windows_closed": 1190, "co2_windows_open": 640}]
}
```

**People** come from `occupancy_forecaster/rolling_24h.py`: weekday × time-of-day medians, with an 80% band.
- If the room's board has sent data in the last 10 minutes, the forecast starts at that live count. The difference from the usual pattern fades out with a 90-minute time constant (`+live_anchor`).
- Rooms without enough history use `persistence_no_history`, which holds the current count steady.

**CO₂ curves:**
- `co2_ppm`, `co2_lower` and `co2_upper` assume the windows stay as they are now.
- `co2_windows_closed` assumes all windows are closed.
- `co2_windows_open` assumes all windows are open.

These match the chart's "no intervention" and "optimal flush" lines and the uncertainty band.

**`as_weekday=mon…sun`** uses another weekday's pattern. The synthetic data is an office, so a real Saturday forecast shows an almost empty room. Use `as_weekday=wed` for a weekday demo on the weekend.

## Wiring the frontend

`index.html` currently simulates everything in the browser. Replace these:

| Frontend now | Replace with |
|---|---|
| `DEFAULT_ROOMS` + `localStorage` `caelum_custom_rooms` (`getAllRooms`, `saveNewCustomRoom`, `deleteCurrentCustomRoom`) | `GET/POST/DELETE /rooms` (window field `area` → `area_m2`) |
| `STATE.occupancy` (slider / time-warp presets) | `GET /occupancy/latest?device=<room.device>` → `people_smoothed`, or the last point of `/rooms/{id}/occupancy` |
| `STATE.co2` and `stepSimulationTick` | `GET /rooms/{id}/forecast` → `summary.co2_now` (poll every 5–10 s) |
| `buildForecastArrays()` | Past: `GET /rooms/{id}/co2`. Future: `forecast.points` (`co2_ppm`, `co2_upper/lower`, `co2_windows_open`) |
| `toggleIndividualWindow`, `setOpenWindowsCount`, `closeAllWindowsAndStopTimer` | `PUT /rooms/{id}/windows {"open": [...]}` |
| "BLE strength" select (`+4 dBm` transmit power) | The boards have no transmit-power setting. Use a **range** select (`threshold_dbm` −40…−100, labels in `server-api.md`) |
| "Deploy Config" button (`alert(...)`) | `PUT /config/{device}` with `{"threshold_dbm": …, "interval_s": …, "window_s": …}`, or `{"preset": "demo"}` |
| Arduino fleet in `localStorage` `caelum_arduino_fleet` | `GET /config/{device}` + `GET /occupancy/latest?device=` (`online`, `heard_total`, `devices_in_range`) |
| BLE packet sniffer rows (random) | `GET /data?device=uno-q-1&limit=1` → `data.devices` (anonymised ids + RSSI) |

Example:

```js
const API = 'https://hackaburger.cch-uk.tech/ingest';
const KEY = localStorage.getItem('hackaburger_api_key') || prompt('API key');
localStorage.setItem('hackaburger_api_key', KEY);
const api = (path, opts = {}) => fetch(API + path, {
  ...opts, headers: {'X-API-Key': KEY, 'Content-Type': 'application/json', ...(opts.headers || {})},
}).then(r => r.ok ? r.json() : Promise.reject(r));

const f = await api('/rooms/appleton/forecast?as_weekday=wed');
STATE.co2 = f.summary.co2_now;
```
