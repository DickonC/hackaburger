# Backend API: rooms, CO₂ and forecast

How the frontend (`index.html`) gets real data. The board ↔ server format is in [`server-api.md`](server-api.md); this file covers everything else.

**Base URL:** `https://hackaburger.cch-uk.tech/ingest`

**Headers on every call:** `X-API-Key: <key>` and, for bodies, `Content-Type: application/json`.

Interactive docs, where you can try every call: `https://hackaburger.cch-uk.tech/ingest/docs`.

## How the pieces fit

```
board (uno-q-1) ──POST /data──▶ readings ──┐  people from BLE devices in range (threshold_dbm)
                                           ├─▶ room occupancy (board data wins, history fills gaps)
occupancy_history (Aalborg dataset) ───────┘        │
                                                    ├─▶ CO₂ estimate (mass balance, room volume + windows)
rooms (volume, windows, capacity, device) ──────────┤
window_events (open/close) ─────────────────────────┘
                                                    └─▶ forecast: occupancy_forecaster calendar model
                                                        + live anchor → people and CO₂ for the next 24 h
```

- **There is no CO₂ sensor.** All CO₂ values are estimated from people and ventilation, using the frontend's `stepMassBalanceCO2` formula.
- **The forecaster predicts people.** CO₂ is calculated from those predicted people.
- **The forecaster needs at least 4 weeks of 5-minute history for a room.** The history comes from the **Aalborg office dataset** (below). Rooms without their own history borrow a dataset room's models with `model_from`. Rooms with neither hold the current count steady.

## Historic data: the Aalborg office dataset

The source is Aalborg University, *A Danish high-resolution dataset for six office rooms with occupancy, indoor environment, heating, ventilation, lighting and room control monitoring*, Zenodo, [doi:10.5281/zenodo.10673763](https://doi.org/10.5281/zenodo.10673763), licence **CC BY 4.0**. Credit it wherever its data is shown.

```bash
docker exec hackaburger-ingest python -m app.dataset   # about 2 minutes; safe to re-run
```

This downloads the full-year CSV (checksum-verified, into `data/datasets/`, not in git) and then:

- **Creates rooms `aalborg-a` … `aalborg-f`.** Their history (2023, 5-minute) holds the camera people count plus the **measured CO₂** and window state (`co2_ppm` and `window_open` in `occupancy_history`). Capacity is set to each room's observed maximum (4–9).
- **Calibrates each room's ventilation from its measured CO₂.**
  - The rooms have demand-controlled ventilation. The steady-state rise per person (+100–180 ppm) gives the occupied airflow: **87–161 m³/h**.
  - Room volume couldn't be fitted reliably from the 5-minute data, so it's set to 60 m³.
  - Running the calibrated model through each weekday from the measured 06:00 value, it stays within **59–97 ppm** of the real sensor on average.
- **Trains the short-term models** (`occupancy_core`: persistence / seasonal / ridge / gradient-boosted trees, chosen per horizon on a validation period) into `data/models/<room>/`.
- **Removes the old synthetic history and links the preset rooms:**
  - `appleton` → `aalborg-a` (capacity 12)
  - `forum` → `aalborg-d` (capacity 20)
  - `personal` → `aalborg-c` (capacity 1)

  Their ventilation is set from the dataset's median **24.9 m³/h per person** × capacity.

**How a borrowed pattern is applied:**
- It's matched by **local wall-clock time**: 10:00 in the room means 10:00 in Aalborg.
- It's scaled by capacity: room people = model people × room capacity / source capacity.

## Database tables

| Table | What |
|---|---|
| `readings` | Every board message, plus the server's people count (see `server-api.md`) |
| `configs` | Per-board settings, e.g. `threshold_dbm` |
| `rooms` | One JSON row per room: name, volume, windows, capacity, the `device` (board) in it, CO₂ parameters |
| `occupancy_history` | Historic people per timestamp per room, with `source`: `aalborg-2023`, `manual`, …; plus measured `co2_ppm` and `window_open` where known |
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
| `PUT /rooms/{id}` | Change any fields (partial update), e.g. `{"device": "uno-q-2"}` or `{"model_from": "aalborg-d"}` |
| `DELETE /rooms/{id}` | Delete the room, its history and its window events |
| `PUT /rooms/{id}/windows` | `{"open": [0, 2]}`: these windows are open from now on. `[]` closes all of them |

Rules:
- Window `area_m2` must be between 0.1 and 20. The frontend currently calls this field `area`.
- `capacity` caps the forecast.
- `device` links the room to a board. That board's live count is used for this room.
- `model_from` borrows another room's history and models. The `history` block shows `sources` and whether a trained short-term model exists.

## People and history

| Call | What |
|---|---|
| `GET /rooms/{id}/occupancy?from=&to=&step_s=300` | People per bucket, `source`: `sensor` (board), `history` or `null` (no data). Default: the last 24 h |
| `GET /rooms/{id}/history?from=&to=` | Raw stored history points |
| `POST /rooms/{id}/history` | `{"source": "manual", "points": [{"ts": "2026-11-07T09:00:00Z", "people": 12, "co2_ppm": 640}]}` (`co2_ppm` optional). Replaces points with the same `ts`. Requests are limited to 256 KB, so send large imports in chunks of about 2,000 points |
| `DELETE /rooms/{id}/history[?source=manual]` | Delete history, optionally only from one source |

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

**People** come from the room's own models, or from those of `model_from`:

- **Board live** (a reading in the last 10 minutes): the **short-term models** (`occupancy_core`) forecast the first hour. Their result then fades into the usual weekday × time-of-day pattern with a 90-minute time constant (`rolling_24h`). The model reports `short_term+historical_time_of_week`, and `models.short_term` lists which candidates were used.
  - These models need the count from 1 day and 1 week ago. Where the board wasn't running then, the usual count for that time is used instead.
- **No live board:** the weekday × time-of-day pattern, with an 80% band (`historical_time_of_week`).
- **A live count but no short-term model** (or with `as_weekday`): the pattern, shifted to start at the live count and fading back with a 90-minute time constant (`+live_anchor`).
- **No history at all:** `persistence_no_history`, which holds the current count steady.

**CO₂ curves:**
- `co2_ppm`, `co2_lower` and `co2_upper` assume the windows stay as they are now.
- `co2_windows_closed` assumes all windows are closed.
- `co2_windows_open` assumes all windows are open.

These match the chart's "no intervention" and "optimal flush" lines and the uncertainty band.

**`as_weekday=mon…sun`** uses another weekday's pattern. The dataset rooms are offices, so a real weekend forecast shows an almost empty room. Use `as_weekday=wed` for a weekday demo on the weekend.

## Frontend connection (`backend.js`)

`index.html` loads [`backend.js`](../backend.js) as its last script. All the backend wiring is in that file; the only line added to `index.html` is the `<script>` tag. It hooks the page's own functions (`getCustomRooms`, `saveCustomRooms`, `switchRoom`, `buildForecastArrays`, `stepSimulationTick`, the window toggles and `pushBlePacketRow`), so the page keeps working while it's being redesigned.

**Data mode for the current room:**

| Mode (badge, bottom-left) | When | Data shown |
|---|---|---|
| `LIVE` | The room's board is online | Board count, server CO₂ and forecast |
| `MODEL` | No live board, but the room has ≥ 4 weeks of history | Forecaster's expected count, server CO₂ and forecast |
| `SIMULATION` | No data for the room, the backend is unreachable, or "simulate" was clicked | The original in-browser simulation |

**What it changes:**
- **Rooms** load from `/rooms`. Creating, editing and deleting custom rooms saves to the server. Old `localStorage` custom rooms are uploaded once.
- **Opening or closing windows** is saved with `PUT /rooms/{id}/windows`.
- **The "Control Strength" select** becomes a **range** select (−40…−100 dBm). **Deploy Config** sends it, with the interval, to `PUT /config/{device}`.
- **The BLE packet log** shows the board's latest anonymised devices, marked as in range or filtered by the board's threshold.
- **Time-warp** and **Reset** still work. Time-warp switches the room to simulation, and Reset switches it back.

**Settings in `localStorage`:**
- `hackaburger_api_key`: you're asked for it once. Leave it empty to use the offline simulation.
- `hackaburger_api_base`: defaults to `https://hackaburger.cch-uk.tech/ingest`.
- `hackaburger_weekday`: set by the badge's "use weekday pattern" link, which shows a Wednesday pattern on weekends.

**If you rename one of the hooked functions in `index.html`,** update `backend.js` too. Without the hook, that feature quietly falls back to simulation.

## Endpoint ↔ frontend map

What each part of the page uses (`backend.js` already does this):

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
