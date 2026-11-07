# Backend API: live rooms and forecasting

The board ↔ server format is in [`server-api.md`](server-api.md). This file covers everything else.

**Base URL:** `https://hackaburger.cch-uk.tech/ingest`

**Headers on every call (except `/health`):** `X-API-Key: <key>` and, for bodies, `Content-Type: application/json`.

Interactive docs, where you can try every call: `https://hackaburger.cch-uk.tech/ingest/docs`.

## Two separate sides

```
LIVE (proves we can sense occupancy)            FORECASTING (the model, on a real dataset)

board ──POST /data──▶ readings                  Aalborg office dataset (Zenodo, 2023)
                        │ people (BLE)              │ python -m app.dataset
rooms + window_events ──┤                        dataset_rooms + dataset_occupancy
                        ▼                           │ people, measured CO₂, windows
  /rooms/{id}/occupancy, /co2                       ▼
  (estimated CO₂ now, from the board)            occupancy_forecaster (trained per room)
                                                    ▼
                                                 /dataset/rooms/{id}/forecast
                                                 (24 h from now, or a replay of 2023)
```

The two sides share no data. The live rooms have no forecast, and the forecasting never reads the boards.

The pitch: once we've collected a few weeks of our own board data, the same import-and-train step would run on `readings` instead of the dataset.

| Side | Tables |
|---|---|
| Live | `readings`, `configs` (boards, see `server-api.md`), `rooms`, `window_events` |
| Forecasting | `dataset_rooms` (calibration, model results), `dataset_occupancy` (people, `co2_ppm`, `window_open` per 5 minutes) |

## Live rooms

A room:

```json
{
  "id": "appleton", "name": "Appleton Tower — Study Pod 4.02",
  "volume_m3": 65.0, "capacity": 32, "device": "uno-q-1", "scheduled_count": 12,
  "windows": [{"name": "Win 01 (North)", "area_m2": 1.0, "is_open": false}],
  "open_area_m2": 0.0,
  "co2_gen_lph": 16.0, "outdoor_ppm": 420.0, "infiltration_ach": 0.25, "k_flow": 350.0,
  "timezone": "Europe/London"
}
```

| Call | What |
|---|---|
| `GET /rooms` | All live rooms. The frontend's 3 presets are pre-created |
| `POST /rooms` | Create one. Needs `name`, `volume_m3` and `windows` (`[{"name", "area_m2"}]`) |
| `GET / PUT / DELETE /rooms/{id}` | Read, partly update (e.g. `{"device": "uno-q-2"}`) or delete |
| `PUT /rooms/{id}/windows` | `{"open": [0, 2]}`: these windows are open from now on. `[]` closes all |
| `GET /rooms/{id}/occupancy?from=&to=&step_s=300` | The board's people count per bucket (`null` where it sent nothing). Default: the last 24 h |
| `GET /rooms/{id}/co2?from=&to=&step_s=300` | Estimated CO₂ timeline, plus `now`: `{co2_ppm, people, open_area_m2, airflow_m3h}`. Default: the last 6 h |

**How CO₂ is estimated:**
- It uses the frontend's mass balance: room volume, airflow `infiltration_ach × volume + k_flow × open window area`, and 16 L/h per person. There's no CO₂ sensor.
- The simulation starts at outdoor level 12 hours back.
- Intervals without board data count as an empty room.

## Forecasting (dataset rooms)

The source is Aalborg University, *A Danish high-resolution dataset for six office rooms with occupancy, indoor environment, heating, ventilation, lighting and room control monitoring*, Zenodo, [doi:10.5281/zenodo.10673763](https://doi.org/10.5281/zenodo.10673763), licence **CC BY 4.0**. Credit it wherever its data or forecasts are shown.

```bash
docker exec hackaburger-ingest python -m app.dataset   # about 2 minutes; safe to re-run
```

This downloads the data (checksum-verified, into `data/datasets/`, not in git) and fills `dataset_rooms` and `dataset_occupancy` with rooms `aalborg-a` … `aalborg-f`: six offices, 27 Feb – 31 Dec 2023, every 5 minutes.

**What gets stored per room:**
- **People** (from cameras; missing for whole days or weeks, and kept as missing).
- **Measured CO₂ and window state.**
- **Calibrated ventilation:**
  - The offices have demand-controlled ventilation. The steady-state CO₂ rise per person gives the occupied airflow: 87–161 m³/h.
  - Volume couldn't be fitted, so it's assumed to be 60 m³.
  - Run freely through each weekday, the CO₂ model stays within 59–97 ppm of the real sensor on average.
- **Short-term models** (`occupancy_core`), stored in `data/models/<room>/`. The backtest results are in `dataset_rooms.room.backtest`.

| Call | What |
|---|---|
| `GET /dataset/rooms` | The six rooms: capacity, `airflow_m3h`, `volume_m3`, `calibration`, `backtest`, `history` range, `replay_from` |
| `GET /dataset/rooms/{id}/history?from=&to=` | Recorded `people`, measured `co2_ppm` and `window_open`. Default: the data's last day |
| `GET /dataset/rooms/{id}/forecast?hours=24` | **From now:** the weekday × time-of-day pattern (`rolling_24h`) for this room. `as_weekday=wed` uses another weekday's pattern |
| `GET /dataset/rooms/{id}/forecast?at=2023-10-11T08:00:00Z&hours=6` | **Replay:** forecast from that moment in 2023, using only the data before it (see below) |

**How a replay works:**
- The **short-term models** (persistence / seasonal / ridge / gradient-boosted trees, picked per horizon on validation data) forecast the first hour. Their result then fades into the weekday pattern with a 90-minute time constant (`rolling_24h`).
- Each point also includes what actually happened (`actual_people`, `actual_co2_ppm`).
- The response adds the 2 hours before `at` (`recent`) and the error (`accuracy`: people and CO₂, for the first hour and for the whole range).
- CO₂ starts from the measured value at `at`.
- `at` must be at least a week after the data starts (`replay_from`), because the models use the count from a day and a week earlier. If the cameras were off at `at`, it falls back to the weekday pattern; the response's `model` says which was used.

A forecast looks like this:

```json
{
  "room_id": "aalborg-a", "origin": "2023-10-11T08:00:00Z", "replay": true,
  "model": "short_term+historical_time_of_week", "short_term_models": ["boost_delta", "persistence"],
  "summary": {"people_now": 3, "co2_now": 612, "window_open_now": false, "peak_co2": 690, "peak_at": "...", "first_above_1000_at": null},
  "points": [{"ts": "...", "people": 2.9, "people_lower": 1.8, "people_upper": 4.1,
              "co2_ppm": 618, "co2_lower": 590, "co2_upper": 650, "co2_windows_closed": 618, "co2_windows_open": 455,
              "actual_people": 3, "actual_co2_ppm": 621}],
  "recent": [{"ts": "...", "people": 2, "co2_ppm": 580}],
  "accuracy": {"first_hour": {"people_mae": 0.4, "co2_mae_ppm": 22.1, "points": 12}, "all": {"...": "..."}}
}
```

**The CO₂ curves:**
- `co2_ppm`, `co2_lower` and `co2_upper` keep the window as it was at the start.
- `co2_windows_closed` and `co2_windows_open` assume the window stays closed or open.

**Window advice (`summary.ventilation`):** when to open a window and for how long ([`ventilation.py`](../arduino-ingest/app/ventilation.py)).
- The CO₂ mass balance `V dC/dt = 1000·G·N(t) − Q·(C − C_out)` is solved exactly over each 5-minute forecast step, so the forecast people curve `N(t)` is used as it changes. Each threshold crossing time comes from `t* = (V/Q)·ln((C₀ − C_ss)/(C_th − C_ss))`.
- Thresholds: 800 ppm early heads-up, **1000 ppm "open a window"**, 1500 ppm critical (cross-ventilate). A flush opens all windows when 1000 is reached and lasts until CO₂ is back at **600 ppm**.
- `status`: `ok`, `heads_up` (800–1000), `planned` (window needed later), `open_now`, `critical`, or `flushing` (window already open: `close_window_in_min` says when 600 is reached).
- `open_window_in_min` / `open_window_at`: when CO₂ reaches 1000 with windows shut (`null` if not within the forecast). `open_window_range_min`: the same for the upper and lower people band (more people means sooner).
- `flush_duration_min`: minutes with all windows open to get back to 600. It is `null` with `flush_reaches_target: false` and `lowest_reachable_ppm` when too many people stay for 600 to be reachable.
- `message`: a ready-to-show sentence, e.g. "Open a window in 12 min for about 9 min to get back to 600 ppm."

```json
"ventilation": {"status": "planned", "co2_now": 640, "heads_up_in_min": 5.2, "open_window_in_min": 12.4,
  "open_window_at": "...", "open_window_range_min": [9.8, 16.1], "critical_in_min": null,
  "flush_duration_min": 8.7, "flush_reaches_target": true,
  "thresholds_ppm": {"heads_up": 800, "open": 1000, "critical": 1500, "target": 600},
  "message": "Open a window in 12 min for about 9 min to get back to 600 ppm."}
```

## Frontend

`index.html` (Room view) and `admin.html` (Ops console) load [`backend.js`](../backend.js), which connects the live rooms:
- rooms and windows
- the board's count and estimated CO₂ (from `/rooms/{id}/co2` → `now`)
- the board config ("Deploy Config" → `PUT /config/{device}`)
- the BLE packet log

The forecast chart for a live room is the page's own engine projecting forward from the live state.

Showing the dataset forecasts and replays in the UI (`/dataset/...`) isn't built yet.

## Room heatmap (four corner boards)

This is built to [`heatmap-backend-guide.md`](heatmap-backend-guide.md); all its acceptance checks are covered by `arduino-ingest/tests/test_heatmap.py`. The code is in `arduino-ingest/app/heatmap.py`.

| Call | What |
|---|---|
| `GET /heatmap/latest` | People estimate, smoothed people, funnel, per-board status (online, applied/pending config), calibration in use, heatmap grid, warnings |
| `GET /heatmap/debug[?ts=]` | Every device in a snapshot: RSSI per board, position, `in_room` / `rejected` + reason |
| `GET /heatmap/history?from=&to=&step_s=60` | Mean people estimate per bucket |
| `GET / PUT /heatmap/config` | Room size, board corners, filter, offsets, heatmap and people settings (partial `PUT`, deep merge) |
| `PUT /heatmap/mode` | `{"mode": "live"}` (all boards send every 10 s; needed for the heatmap) or `{"mode": "dev"}` (every 5 min) |
| `PUT /heatmap/boards` | The same board settings for all heatmap boards, e.g. `{"threshold_dbm": -60}` (LED range) |
| `POST /heatmap/calibrate/headcount` | `{"people": n}`, counted in the room now: sets `devices_per_person` |

**Choices beyond the guide:**
- **Calibrated values set by hand switch to manual.** `PUT {"filter": {"edge_dbm": -95}}` switches `edge_mode` to `manual`, and setting `offsets_db` switches `offsets_mode` to `manual`. Otherwise the auto calibration would immediately overwrite the value. Send `"edge_mode": "auto"` to go back.
- **Board timing is enforced.** `PUT /config/{device}` with an `interval_s` or `window_s` that differs from the other heatmap boards returns `422`; change them together with `PUT /heatmap/boards`.
- **`uno-q-2` no longer starts in demo settings,** because all four boards must share the same timing.
- **Extra warnings:**
  - a board that sent recently but is **missing from the latest snapshot**
  - boards with different timing
  - boards still waiting to apply new settings
- **Feeding the CO₂ estimate:** a live room with `"device": "heatmap"` takes its people from the heatmap (`people_estimate`) instead of a single board.
- **Privacy:**
  - Per-device debug rows are deleted after 24 hours.
  - Board messages older than 24 hours lose their device-id list. They keep the anonymous RSSI values (`readings.rssis`), so recounts still work.
- **Timing:** a snapshot is built as soon as all boards have sent for a `ts`, or 15 s after the first one. A background loop checks every 2 s. Board messages more than an hour late don't start a snapshot.
