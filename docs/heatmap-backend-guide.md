# Task brief: room heatmap and board control (backend)

> **For an AI coding agent with access to the Hackaburger server.** Read this whole file before changing code.
> The board message format is in [`server-api.md`](server-api.md). The existing ingest and config work is in [`server-agent-brief.md`](server-agent-brief.md).
> This file covers two things: **(1) controlling the boards** from the server, and **(2) the room heatmap**, which combines four boards, filters out devices outside the room, and gives a people estimate.
>
> **Most of the backend already exists.** Fit this into the existing code and reuse existing endpoints and storage where they already do the job. Don't rebuild what works. The CO₂ model is out of scope here; this guide only provides its people input.

## Goal

Four Arduino UNO Q boards sit in the four corners of one room. From their Bluetooth scans, the server produces every few seconds:

1. **`people_estimate`:** how many people are in the room. **This is the most important value**, and it's what the existing CO₂ model should use as its people input.
2. **A smooth heatmap** of where people are in the room.
3. **A debug view** of every device heard, showing whether it counted and, if not, which filter rejected it.

**Must be quick to set up.** Only two numbers are required (room width and length). Everything else has working defaults, and calibration is **automatic**. Nobody has to walk around the room.

## What already exists (don't break it)

- **`POST /ingest/data`:** boards send `{device, ts, data: {devices: [{id, rssi, n}], ...}}` and get `{"stored": 1, "config": {...}}` back.
- **`GET`/`PUT /ingest/config/{device}`:** per-board settings.
- **Auth:** header `X-API-Key` on every request.

## Controlling the boards

The boards can't be reached from the server, because eduroam blocks incoming connections. **All control works through the reply to each board's `POST /ingest/data`:** the server returns that board's full `config`, and the board applies it immediately. So the server only has to store the desired settings per board. They take effect at each board's **next POST**.

### Board settings (`/ingest/config/{device}`)

The existing fields, plus one new one, `send_every_s`:

| Field | Default | Allowed | What it does on the board |
|---|---|---|---|
| `enabled` | true | | `false` = the board keeps checking in but sends no device data (a pause). |
| `threshold_dbm` | −75 | −100 … −30 | **LED range:** only the number on the board's LED. It doesn't affect the heatmap. |
| `window_s` | 30 | 2 … 300 | How long a device stays in the board's list after it was last heard. |
| `interval_s` | 5 | 1 … 600 | How often the board measures and updates its LED, on clock boundaries (10 → :00, :10, :20…). |
| `send_every_s` **(new)** | 300 | 0 … 3600 | How often the board POSTs, on clock boundaries. `0` = every `interval_s`. |
| `people_factor` | 0.7 | 0.1 … 2.0 | Only used for the board's own `people` field. |
| `send_devices` | true | | Must stay `true` for the heatmap. |
| `display` | true | | LED display on or off. |
| `config_version` | | | Server-managed: +1 on every change. |

**What the board does with these settings:**
- Clamps out-of-range values, ignores unknown fields and keeps missing fields at their current value.
- Saves the last config, so it survives a reboot.
- Queues messages while offline and resends them later.

**How long a change takes to reach a board:** up to one `send_every_s`. In **dev mode** (300) that's up to 5 minutes, and in **live mode** (0, every 10 s) it's up to 10 s.

**Applied or pending:**
- Every board message contains `data.config_version`, the version the board was running when it measured.
- Compare it with the stored version: if the board's version is lower than the server's, the change is **pending**. Otherwise it's **applied**.
- Expose this per board (see `boards` in `/heatmap/latest`), so the webapp can show "LED range −60 applied" or "waiting for board 3".

### Modes and bulk changes

- **`PUT /heatmap/mode {"mode": "live" | "dev"}`** sets all four boards at once:
  - **live:** `send_every_s: 0, interval_s: 10, window_s: 20`. **Required for the heatmap.**
  - **dev:** `send_every_s: 300`, with `interval_s` and `window_s` unchanged.
- **`PUT /heatmap/boards {…partial config…}`** applies the same fields to all four boards, e.g. `{"threshold_dbm": -60}` for the LED range. Use the same validation as `/ingest/config/{device}`.
- **Rule for the heatmap:** all four boards **must** have the same `interval_s` and `window_s`. Otherwise their `ts` values don't line up. `PUT /heatmap/boards` keeps this rule automatically. Reject per-board changes to `interval_s` or `window_s` with `422`, unless they're applied to all four boards.

### Facts about the incoming data

- **Board names:** `uno-q-1` … `uno-q-4`. Each board shows its number as dots (1–4) at the top left of its LED, so people can tell them apart when placing them.
- **`devices`:** every device the board heard in its last `window_s` seconds, **unfiltered**. Each entry has `id` (anonymised, **identical on all boards for the same phone**), `rssi` (median dBm in that window, where closer to 0 means closer) and `n` (number of hits).
- **`ts` is aligned to the clock.** It's always a whole multiple of `interval_s` (e.g. `…:20:00`, `…:20:10`), and the boards are internet time-synced. So the four boards' messages for the same moment carry the **identical `ts`**. Join on `ts`.
- **Address rotation:** phones change their Bluetooth address about every 15 minutes, so a phone gets a new `id`. That's fine. Never try to link ids over time.

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
  "filter": { "exclude_kinds": ["earbuds", "tracker", "beacon", "fixed"], "stationary_h": 2,
              "edge_mode": "auto", "edge_dbm": -85, "edge_margin_db": 4, "min_boards": 3, "presence_s": 30 },
  "offsets_mode": "auto",
  "offsets_db": { "uno-q-1": 0, "uno-q-2": 0, "uno-q-3": 0, "uno-q-4": 0 },
  "position": { "weight_k": 2.0 },
  "heatmap": { "cell_m": 0.5, "sigma_m": 1.0, "smoothing_s": 60 },
  "people": { "devices_per_person": 1.2 }
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
| `exclude_kinds` | a list of `kind` values (see [`server-api.md`](server-api.md)), possibly empty |
| `stationary_h` | 0 … 48 (0 = off) |
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

**Step 0: drop obvious non-people.** Every device carries a `kind` from the board.
- **Excluded category:** if a device's `kind` is in `exclude_kinds` on **any** board, reject it with reason `not_a_person`. Boards can disagree, and the most specific category wins.
- **Fixed device:** reject devices that are **stationary**: the same `id` has been seen continuously for at least `stationary_h` hours. Phones rotate their address about every 15 minutes, so an id that stays for hours is almost always a TV, printer, speaker or similar. Reason: `stationary`.
- Then check the following:

| # | Check | Passes if | Reason if it fails |
|---|---|---|---|
| 0 | Not an obvious non-person | `kind` not in `exclude_kinds` **and** not stationary | `not_a_person` / `stationary` |
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

`devices_per_person` accounts for people carrying several Bluetooth devices. Earbuds and trackers are already removed in step 0, so what's left per person is mostly a phone, sometimes a laptop or watch. **Use 1.2 as the default** (in the config example above too).

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
  "funnel": { "heard": 312, "plausible_people": 251, "enough_boards": 120, "inside_edge": 38, "present": 33 },
  "kinds_in_room": { "apple_device": 19, "computer": 6, "unknown": 6, "brand_other": 2 },
  "boards": {
    "uno-q-1": { "corner": "top_left", "x": 0.5, "y": 0.5, "online": true, "last_ts": "2026-11-07T21:30:10Z", "heard": 201,
                 "config": { "version": 5, "applied_version": 5, "pending": false, "threshold_dbm": -75, "send_every_s": 0, "interval_s": 10 } },
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
- `boards[*].config` shows the board's current settings, and whether the latest change is still `pending` (`applied_version` < `version`). The other boards have the same `config` object; the example only shows it for board 1.
- Add a warning when `mode` is `dev`, because the snapshot is then up to 5 minutes old.

### `GET /heatmap/debug[?ts=…]`, every device in a snapshot (default: the latest)

```json
{
  "ts": "2026-11-07T21:30:10Z",
  "filter": { "edge_dbm": -85, "min_boards": 3, "presence_s": 30 },
  "devices": [
    { "id": "a3f9c2e1b7d04e12", "rssi": { "uno-q-1": -58, "uno-q-2": -66, "uno-q-3": -71, "uno-q-4": -69 },
      "weakest_dbm": -71, "boards_heard": 4, "x": 3.1, "y": 2.4, "kind": "apple_device", "status": "in_room", "reason": null },
    { "id": "e09b44c1aa7f2d65", "rssi": { "uno-q-1": -61, "uno-q-2": -90, "uno-q-3": null, "uno-q-4": -93 },
      "weakest_dbm": -93, "boards_heard": 3, "x": 0.9, "y": 1.2, "kind": "apple_device", "status": "rejected", "reason": "outside_edge" },
    { "id": "77ab0e4c19d2f3a1", "rssi": { "uno-q-1": -55, "uno-q-2": -63, "uno-q-3": -70, "uno-q-4": -66 },
      "weakest_dbm": -70, "boards_heard": 4, "x": 2.2, "y": 1.9, "kind": "earbuds", "status": "rejected", "reason": "not_a_person" },
    { "id": "5d20f7b39c18aa04", "rssi": { "uno-q-1": -88, "uno-q-2": null, "uno-q-3": null, "uno-q-4": null },
      "weakest_dbm": -88, "boards_heard": 1, "x": null, "y": null, "kind": "unknown", "status": "rejected", "reason": "too_few_boards" }
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

## Hand-off to the CO₂ model

The CO₂ model already exists and is the backend teammate's own work. This guide doesn't change it. Just give it the room's people count:
- **Live:** `people_smoothed` from `/heatmap/latest`.
- **History:** `GET /heatmap/history`, with one people value per time step.

Store the people estimate permanently. It's anonymous.

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
10. **Bulk LED range:** `PUT /heatmap/boards {"threshold_dbm": -60}` raises `config_version` on all four boards. Each board shows `pending: true` until a message arrives with the new version, then `pending: false`.
11. **Heatmap rule:** `PUT /ingest/config/uno-q-2 {"interval_s": 5}` on its own → `422`. The same value via `PUT /heatmap/boards` → `200`, for all four boards.
12. **Obvious non-people:** a device with `kind: "earbuds"` and strong RSSI on all four boards → `rejected`, `not_a_person`. After `PUT /heatmap/config {"filter": {"exclude_kinds": []}}` it counts as `in_room`.
13. **Stationary:** an id present in every snapshot for more than `stationary_h` hours → `rejected`, `stationary`.
14. **Auth:** all new endpoints return `401` without a valid `X-API-Key`.

## Quick start (what the team does on the day)

1. Place the boards in the corners, about 0.5 m in from the walls, **clockwise starting top left: 1 dot, 2 dots, 3 dots, 4 dots** on the LED. Plug them into power. They join eduroam and start by themselves.
2. In the webapp, enter the room's **width** (wall from board 1 to board 2) and **length** (wall from board 1 to board 4).
3. Switch to **live mode**. Boards in dev mode can take up to 5 minutes to pick that up.
4. After about 30 s the heatmap and people estimate appear.
5. Done. Calibration runs automatically. Optional: count the people once and use **headcount calibration**, and fine-tune the **room edge** in the debug view.
