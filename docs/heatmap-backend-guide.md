# Task brief: room heatmap and people estimate (backend)

> **For an AI coding agent with access to the Hackaburger server.** Read this whole file before changing code.
> The board message format is in [`server-api.md`](server-api.md). The existing ingest and config work is in [`server-agent-brief.md`](server-agent-brief.md).
> This file adds: combining four boards into one room view, filtering out devices outside the room, a smooth heatmap, and a people estimate.

## Goal

Four Arduino UNO Q boards sit in the four corners of one room. From their Bluetooth scans, the server produces every few seconds:

1. **`people_estimate`:** how many people are in the room. **This is the most important value**, and it feeds the webapp and the CO₂ model.
2. **A smooth heatmap** of where people are in the room.
3. **A debug view** of every device heard, showing whether it counted and, if not, which filter rejected it.

**Must be quick to set up.** Only two numbers are required (room width and length). Everything else has working defaults, and calibration is **automatic**. Nobody has to walk around the room.

## What already exists (don't break it)

- **`POST /ingest/data`:** boards send `{device, ts, data: {devices: [{id, rssi, n}], ...}}` and get `{"stored": 1, "config": {...}}` back.
- **`GET`/`PUT /ingest/config/{device}`:** per-board settings.
- **Auth:** header `X-API-Key` on every request.

## Facts about the incoming data you can rely on

- **Board names:** `uno-q-1` … `uno-q-4`. Each board shows its number as dots on its LED, so people can tell them apart when placing them.
- **`devices`:** every device the board heard in its last `window_s` seconds, **unfiltered**. Each entry has `id` (anonymised, **identical on all boards for the same phone**), `rssi` (median dBm in that window, where closer to 0 means closer) and `n` (number of hits).
- **`ts` is aligned to the clock.** It's always a whole multiple of `interval_s` (e.g. `…:20:00`, `…:20:10`), and the boards are internet time-synced. So the four boards' messages for the same moment carry the **identical `ts`**. Join on `ts`.
- **Board settings for the heatmap:** all four boards use the same `interval_s` (10) and `window_s` (20).
- **Address rotation:** phones change their Bluetooth address about every 15 minutes, so a phone gets a new `id`. That's fine. Never try to link ids over time.

## Required change to the board config: `send_every_s`

Add this field to the per-board config (validated like the others, and included in every `POST /ingest/data` reply):

| Field | Type | Default | Allowed | Meaning |
|---|---|---|---|---|
| `send_every_s` | int | 300 | 0 … 3600 | Seconds between board POSTs, on clock boundaries. `0` = send at every `interval_s`. |

- **Dev mode (default):** `send_every_s: 300`, so one message per board every 5 minutes.
- **Live mode (needed for the heatmap):** `send_every_s: 0, interval_s: 10, window_s: 20` on **all four boards**, so one message per board every 10 s.
- Add `PUT /heatmap/mode` with `{"mode": "live" | "dev"}` that applies these values to all four boards' configs in one call. The boards pick it up at their next POST, which takes at most 5 minutes when switching from dev mode.

---

## Room config: `GET`/`PUT /heatmap/config`

One room. Store it persistently. `PUT` takes a partial object (deep merge), validates it (`422` on invalid values), and returns the full config.

```json
{
  "room": { "width_m": 8.0, "length_m": 6.0, "board_inset_m": 0.5 },
  "boards": {
    "uno-q-1": { "corner": "top_left" },
    "uno-q-2": { "corner": "top_right" },
    "uno-q-3": { "corner": "bottom_right" },
    "uno-q-4": { "corner": "bottom_left" }
  },
  "filter": { "edge_mode": "auto", "edge_dbm": -85, "edge_margin_db": 4, "min_boards": 3, "presence_s": 30 },
  "offsets_mode": "auto",
  "offsets_db": { "uno-q-1": 0, "uno-q-2": 0, "uno-q-3": 0, "uno-q-4": 0 },
  "position": { "weight_k": 2.0 },
  "heatmap": { "cell_m": 0.5, "sigma_m": 1.0, "smoothing_s": 60 },
  "people": { "devices_per_person": 1.4 }
}
```

### Geometry
- **Origin:** the top-left corner of the room. `x` runs right along the top wall (from board 1 towards board 2), up to `width_m`. `y` runs down (from board 1 towards board 4), up to `length_m`.
- **Boards go clockwise, starting top left.** Board positions are **derived** from the room size and the inset (boards sit slightly in from the walls):

  | Board | Corner | Position |
  |---|---|---|
  | `uno-q-1` | `top_left` | (`inset`, `inset`) |
  | `uno-q-2` | `top_right` | (`width − inset`, `inset`) |
  | `uno-q-3` | `bottom_right` | (`width − inset`, `length − inset`) |
  | `uno-q-4` | `bottom_left` | (`inset`, `length − inset`) |

- Optionally allow `"x"`/`"y"` per board to override the derived position.
- **The only values a user must enter are `width_m` and `length_m`.** The distances between boards follow from those.

### Validation

| Field | Allowed |
|---|---|
| `width_m`, `length_m` | 1 … 100 |
| `board_inset_m` | 0 … 3 |
| `edge_mode`, `offsets_mode` | `"auto"` or `"manual"` |
| `edge_dbm` | −100 … −40 (only used as set when `edge_mode` is `manual`; in `auto` the server overwrites it) |
| `edge_margin_db` | 0 … 15 |
| `min_boards` | 2 … 4 |
| `presence_s` | 0 … 600 |
| `offsets_db.*` | −20 … 20 |
| `weight_k` | 0.5 … 5 |
| `cell_m` | 0.25 … 2 |
| `sigma_m` | 0.25 … 5 |
| `smoothing_s` | 0 … 600 |
| `devices_per_person` | 0.5 … 4 |

---

## Processing: one snapshot per `ts`

**When to compute:** build a snapshot for timestamp `T` when all four boards' messages with `ts == T` have arrived, **or** 15 s after the first one arrived, whichever comes first. Missing boards count as "didn't hear anything" and are reported as offline in the output.

**Combining the boards:** for every device `id` in any of the messages, collect `rssi[b]` for each board `b` that heard it. Then apply the offset: `adj[b] = rssi[b] + offsets_db[b]`.

### In-room filter (in this order, and record the first reason that fails)

| # | Check | Passes if | Reason if it fails |
|---|---|---|---|
| 1 | Heard by enough boards | `count(boards that heard it) ≥ min_boards` | `too_few_boards` |
| 2 | Inside the room edge | `min(adj[b] over boards that heard it) ≥ edge_dbm` | `outside_edge` |
| 3 | Present long enough | passed checks 1 and 2 in **every** snapshot of the last `presence_s` seconds that has this id. It must also have been seen in at least `ceil(presence_s / interval_s)` snapshots. With `presence_s = 0`, skip this check. | `too_brief` |

Devices passing all three are **in the room**.

**Why this works:**
- Inside the room, every corner is at most one room-diagonal away with no wall in between, so even the **weakest** board hears the device reasonably well.
- A device outside is near one wall: one or two boards hear it well, and the far boards hear it through extra distance plus a wall, so 5–15 dB weaker or not at all.
- People walking past in the corridor only appear briefly.

### Position (computed for every device heard by at least 2 boards, including rejected ones, so the debug view can show them)

```
w[b] = 10 ** (adj[b] / (10 * weight_k))      # 0 for boards that didn't hear it
x = Σ w[b] * board_x[b] / Σ w[b]
y = Σ w[b] * board_y[b] / Σ w[b]
```

Clamp to `[0, width_m] × [0, length_m]`. This is a weighted centre: stronger signal pulls the point harder towards that corner. **Expect roughly 2–3 m accuracy per device.** The heatmap is meaningful in aggregate, not for individuals.

### People estimate

```
devices_in_room = number of in-room devices in this snapshot
people_estimate = devices_in_room / devices_per_person         (float)
people_smoothed = round(median(people_estimate over snapshots in the last smoothing_s seconds))
```

`devices_per_person` (default 1.4) accounts for people carrying several Bluetooth devices: phone, watch, earbuds, laptop.

**Headcount calibration:** `POST /heatmap/calibrate/headcount {"people": 23}`. Someone counts the real people in the room.
- Set `devices_per_person = mean(devices_in_room over the last 60 s) / people`, clamped to 0.5 … 4.
- Persist it and return the new value.
- This is the quick fix for "too many / too few".

### Heatmap

1. **Grid:** `cols = ceil(width_m / cell_m)`, `rows = ceil(length_m / cell_m)`.
2. **Per snapshot:** add a 2-D Gaussian (σ = `sigma_m`) centred on each in-room device's position. Scale it so that each device contributes `1 / devices_per_person` in total. The grid then shows **people per cell**, and the sum over all cells ≈ `people_estimate`.
3. **Smoothing over time:** exponential moving average per cell, with `alpha = interval_s / smoothing_s`. Set `alpha = 1` when `smoothing_s = 0`.
4. **Normalisation:** also return `max_value`, so the frontend can colour-scale.

---

## Read endpoints

### `GET /heatmap/latest`, the main endpoint for the webapp

```json
{
  "ts": "2026-11-07T21:30:10Z",
  "age_s": 4,
  "mode": "live",
  "people_estimate": 23.6,
  "people_smoothed": 24,
  "devices_in_room": 33,
  "devices_heard": 312,
  "funnel": { "heard": 312, "enough_boards": 141, "inside_edge": 38, "present": 33 },
  "boards": {
    "uno-q-1": { "corner": "top_left", "x": 0.5, "y": 0.5, "online": true, "last_ts": "2026-11-07T21:30:10Z", "heard": 201 },
    "uno-q-2": { "corner": "top_right", "x": 7.5, "y": 0.5, "online": true, "last_ts": "2026-11-07T21:30:10Z", "heard": 188 },
    "uno-q-3": { "corner": "bottom_right", "x": 7.5, "y": 5.5, "online": true, "last_ts": "2026-11-07T21:30:10Z", "heard": 176 },
    "uno-q-4": { "corner": "bottom_left", "x": 0.5, "y": 5.5, "online": false, "last_ts": "2026-11-07T21:25:00Z", "heard": 0 }
  },
  "room": { "width_m": 8.0, "length_m": 6.0 },
  "calibration": { "edge_mode": "auto", "edge_dbm": -84, "offsets_mode": "auto",
                   "offsets_db": { "uno-q-1": 1.5, "uno-q-2": -0.5, "uno-q-3": 0.0, "uno-q-4": -1.0 } },
  "heatmap": { "cell_m": 0.5, "cols": 16, "rows": 12, "max_value": 0.9, "values": [[0.0, 0.1, "..."], "..."] },
  "warnings": ["uno-q-4 offline: using 3 boards"]
}
```

- `values` is row-major: `values[row][col]`, where row 0 is the top wall.
- A board is `online` if its last message is no older than `3 × its send interval + 15 s`.
- Add a warning when `mode` is `dev`, because the snapshot is then up to 5 minutes old.

### `GET /heatmap/debug[?ts=…]`, every device in a snapshot (default: the latest)

```json
{
  "ts": "2026-11-07T21:30:10Z",
  "filter": { "edge_dbm": -85, "min_boards": 3, "presence_s": 30 },
  "devices": [
    { "id": "a3f9c2e1b7d04e12", "rssi": { "uno-q-1": -58, "uno-q-2": -66, "uno-q-3": -71, "uno-q-4": -69 },
      "weakest_dbm": -71, "boards_heard": 4, "x": 3.1, "y": 2.4, "status": "in_room", "reason": null },
    { "id": "e09b44c1aa7f2d65", "rssi": { "uno-q-1": -61, "uno-q-2": -90, "uno-q-3": null, "uno-q-4": -93 },
      "weakest_dbm": -93, "boards_heard": 3, "x": 0.9, "y": 1.2, "status": "rejected", "reason": "outside_edge" },
    { "id": "5d20f7b39c18aa04", "rssi": { "uno-q-1": -88, "uno-q-2": null, "uno-q-3": null, "uno-q-4": null },
      "weakest_dbm": -88, "boards_heard": 1, "x": null, "y": null, "status": "rejected", "reason": "too_few_boards" }
  ]
}
```

- Sort `in_room` devices first, then by `weakest_dbm` descending.
- This view is how people tune `edge_dbm` live, so keep it fast (< 200 ms for 500 devices).

### `GET /heatmap/history?from=…&to=…&step_s=60`, for the CO₂ model

Returns `[{"ts", "people": mean people_estimate in that bucket (1 decimal) or null}]`. Leave empty buckets as `null`.

---

## Automatic calibration (no walking around)

Both are on by default (`"auto"`), and both are recomputed continuously. The user can switch either one to `"manual"` in the webapp and set the values by hand. Put the values in use into `/heatmap/latest` (`calibration` object) so the webapp can show them.

### Room edge from the room size (`edge_mode: "auto"`)

The edge must sit just below the weakest signal a phone *inside* the room can produce. The worst case inside is a phone in one corner heard by the board in the opposite corner, across the diagonal. A standard indoor path-loss model gives that value from the room size alone:

```
d_max    = distance between the two farthest-apart boards (≈ room diagonal minus the insets)
rssi_far = RSSI_1M − 10 · PATH_LOSS_N · log10(d_max)     with RSSI_1M = −59 dBm, PATH_LOSS_N = 2.2
edge_dbm = round(rssi_far − edge_margin_db)              clamp to −100 … −60
```

**Examples:** an 8 × 6 m room (d_max ≈ 8.6 m) gives −84 dBm. A 15 × 10 m room (d_max ≈ 16.6 m) gives −90 dBm.

Recompute it whenever the room size changes. Changing `edge_margin_db` (default 4) makes the edge looser or stricter without leaving auto mode.

### Board offsets from the data (`offsets_mode: "auto"`)

Each board's radio and position read slightly differently. Instead of a calibration walk, use the many devices that all four boards hear:

1. Every 60 s, take all devices from the last 10 minutes that were **in the room** and heard by **all four** boards.
2. For each board `b`, compute `med[b] = median(rssi[b])` over those devices (raw, without offsets).
3. Set `offsets_db[b] = mean(med) − med[b]`, then clamp to −10 … 10 dB.
4. Smooth it: `offset = 0.8 · old + 0.2 · new`, so one crowded corner doesn't swing it.
5. Only update when at least 30 such devices exist. Otherwise keep the current offsets, starting at 0.

This assumes that over many devices, people aren't all crowded around one board. That's good enough for a hackathon. If people really do cluster in one corner, the user can switch to manual.

### Headcount (optional, just counting)

`POST /heatmap/calibrate/headcount {"people": n}`, as described under "People estimate", is the only manual calibration. Someone counts the people once, and nobody has to walk anywhere.

## CO₂ model input

There's **no CO₂ sensor**. The CO₂ model *simulates* the room's CO₂ from the number of people. For the hackathon it is fed by **recorded occupancy (historical data)**, not by live measurements:

- **Store the people estimate permanently,** one value per snapshot. Unlike per-device debug data, it's anonymous, so keep it.
- **`GET /heatmap/history?from=…&to=…&step_s=60`** (above) gives the model a people time series to simulate and predict from.
- **Optionally, `GET /heatmap/profile?weekday=…&step_s=900`:** the average people count per time of day across all recorded days. It's handy for "typical Tuesday" predictions.
- **To show the CO₂ model working before much data exists,** allow importing a recorded or synthetic series via `POST /heatmap/history/import [{"ts", "people"}]`, marked as `"source": "import"`.

In a real deployment, the model would combine measured data with predictions from old data. That's out of scope here: the team decided to use old data only.

## Two thresholds: don't mix them up

| | `edge_dbm` (room edge) | `threshold_dbm` (LED range) |
|---|---|---|
| Lives in | `/heatmap/config` (server) | `/ingest/config/{device}` (sent to each board) |
| Affects | which devices count as **in the room**: heatmap, people estimate, CO₂ | **only** the number on each board's LED |
| Takes effect | immediately, on the next snapshot (the boards send unfiltered data) | at each board's next POST |
| Lower value (e.g. −95) | includes weaker, farther devices, with more risk of counting people outside | each LED counts devices farther away |
| Higher value (e.g. −70) | stricter: only devices clearly inside, with risk of missing people in the corners | each LED counts only nearby devices |

## Privacy

- **Store only the anonymised ids,** never anything that could re-identify a phone.
- **Keep per-device rows (debug data) for at most 24 h.** Aggregated counts and heatmaps can be kept.
- **Don't link ids over time.** Phones rotate their address about every 15 min. That's a feature, not a bug.

## Acceptance checks

1. **With only `width_m` and `length_m` set** and all four boards in live mode, `GET /heatmap/latest` returns a grid, `people_estimate` and four online boards within 30 s.
2. **A device that fails the edge check:** a snapshot where a device has `rssi` {−60, −95, −94, −97} with `edge_dbm = −85` → `rejected`, `outside_edge`.
3. **A device that passes:** with `presence_s = 0` and `rssi` {−62, −70, −74, −68} → `in_room`, and its position lies closer to board 1 than to board 3.
4. **Live `edge_dbm` change:** `PUT /heatmap/config {"filter": {"edge_dbm": -95}}` changes `devices_in_room` on the next snapshot, without any board round trip.
5. **Auto edge:** with `edge_mode: "auto"`, room 8 × 6 m, inset 0.5 m and margin 4 → `edge_dbm` = −84 (±1). After `PUT {"room": {"width_m": 15, "length_m": 10}}` it becomes −90.
6. **Headcount calibration:** `POST /heatmap/calibrate/headcount {"people": 10}` with 14 in-room devices on average → `devices_per_person = 1.4`.
7. **One board offline:** with `min_boards = 3`, the snapshot still works, and `warnings` mentions it.
8. **Grid total:** the sum of all `heatmap.values` ≈ `people_estimate` (±10 %, before time smoothing; Gaussians near the walls are clipped).
9. **Mode switch:** `PUT /heatmap/mode {"mode": "live"}` sets `send_every_s: 0, interval_s: 10, window_s: 20` on all four board configs, and increments their `config_version`.
10. **Auth:** all new endpoints return `401` without a valid `X-API-Key`.

## Quick start (what the team does on the day)

1. Place the boards in the corners, about 0.5 m in from the walls, **clockwise starting top left: 1 dot, 2 dots, 3 dots, 4 dots** on the LED. Plug them into power. They join eduroam and start by themselves.
2. In the webapp, enter the room's **width** (wall from board 1 to board 2) and **length** (wall from board 1 to board 4).
3. Switch to **live mode**. Boards in dev mode can take up to 5 minutes to pick that up.
4. After about 30 s the heatmap and people estimate appear.
5. Done. Calibration runs automatically. Optional: count the people once and use **headcount calibration**, and fine-tune the **room edge** in the debug view.
