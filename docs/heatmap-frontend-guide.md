# Heatmap webapp: what the frontend needs

Short guide for the **room heatmap view** and the **board controls**. All data comes from the backend described in [`heatmap-backend-guide.md`](heatmap-backend-guide.md), where the exact JSON is. Most of the webapp already exists, so add these views to it rather than starting over.

## The setup in one picture

```
 board 1 ●            board 2 ●●
   ┌──────── width_m ────────┐
   │                         │
   │        the room         │ length_m
   │                         │
   └─────────────────────────┘
 board 4 ●●●●          board 3 ●●●
```

- **Four boards in the corners, clockwise from top left.** Each board shows its number as dots on its LED (1–4 dots), so draw the same dots in the webapp next to each corner.
- **The user only has to enter two numbers:**
  - **width** = the wall from board 1 to board 2
  - **length** = the wall from board 1 to board 4

  The board positions follow from those.

## Main view (what everyone sees)

1. **People in the room, as a big number.** Use `people_smoothed` from `GET /heatmap/latest`. **This is the headline value.** Show `devices_in_room` small next to it, e.g. "≈ 24 people (33 devices)".
2. **Room rectangle with the heatmap.**
   - Draw the room to scale (`room.width_m` × `room.length_m`).
   - Lay `heatmap.values[row][col]` over it as a **smooth** colour gradient: bilinear interpolation or a canvas blur, no visible cells. Row 0 is the top wall.
   - Colour-scale with `heatmap.max_value`.
3. **Boards in the corners.**
   - Show each board's dot count and status: green when `online`, grey with "offline since …" when not.
   - Tooltip: the number of devices that board heard.
4. **Freshness.**
   - Show "updated N s ago" (`age_s`).
   - If `mode` is `dev`, show a clear banner: "Dev mode: updates every 5 min. Switch to live".
   - Show `warnings` from the response.

Poll `GET /heatmap/latest` every **5 s** in live mode and every **60 s** in dev mode.

## Controls

| Control | Calls | Explain to the user as |
|---|---|---|
| Room width / length (m) | `PUT /heatmap/config {"room": {"width_m", "length_m"}}` | "Measure the walls between the boards." |
| **Room edge**: an **Auto** toggle (default on) plus a slider, −100 … −40 dBm | Auto: `PUT /heatmap/config {"filter": {"edge_mode": "auto"}}`. Moving the slider sends `{"filter": {"edge_mode": "manual", "edge_dbm": v}}` | "Which devices count as *inside* the room. **Auto** works it out from the room size. Move left to include weaker, farther signals, but you may start counting people outside. Move right to be stricter, but you may miss people in the corners. Affects the heatmap and the people count immediately." In auto mode, show the slider at the current `calibration.edge_dbm`. |
| **LED range** slider, −100 … −30 dBm (default −75) | `PUT /heatmap/boards {"threshold_dbm"}`, which sets all 4 boards | "Only changes the number on the boards' LED displays. Doesn't affect the heatmap or the people count. Reaches the boards at their next update." |
| Mode: **Live (10 s)** / **Dev (5 min)** | `PUT /heatmap/mode {"mode": "live" \| "dev"}` | "Live is needed for the heatmap. Switching from dev can take up to 5 min to reach the boards." |
| **Devices per person** (default 1.2) | `PUT /heatmap/config {"people": {"devices_per_person"}}` | "Earbuds and trackers are already filtered out. On average, how many of the remaining devices (phone, laptop, watch) does one person carry?" |
| **"Ignore accessories & fixed devices"** toggle (default on) | `PUT /heatmap/config {"filter": {"exclude_kinds": ["earbuds","tracker","beacon","fixed"]}}`. Off = `[]` | "Don't count earbuds, AirTags, beacons, TVs and printers as people." |
| **"Calibrate with headcount"** button: the user types the real number of people | `POST /heatmap/calibrate/headcount {"people": n}` | "Count the people in the room once. The app adjusts devices per person to match." |

Keep the room-edge and LED-range controls clearly apart. They are **two different thresholds**, and mixing them up is the most likely source of confusion.

## Board control panel

The boards can't be contacted directly. Every setting is stored on the server, and each board picks it up **at its next message**: within 10 s in live mode, but up to 5 minutes in dev mode. The panel must make that visible, so nobody thinks a setting "didn't work".

Data comes from `boards` in `GET /heatmap/latest`. Show one row or card per board, in corner order 1 → 4:

| Show | From |
|---|---|
| Board number as dots (●, ●●, ●●●, ●●●●) and its corner | `corner` |
| Online / offline, and last seen | `online`, `last_ts` |
| Devices it hears | `heard` |
| Current LED range and send mode | `config.threshold_dbm`, `config.send_every_s` (0 = live) |
| **Applied ✓ / pending ⏳** for the latest change | `config.pending` |

**Controls for all boards at once.** The heatmap needs all four boards on the same timing, so keep these global:
- **Live / Dev** switch → `PUT /heatmap/mode`.
- **LED range** slider → `PUT /heatmap/boards {"threshold_dbm"}`.
- **Pause / resume** sending → `PUT /heatmap/boards {"enabled": false | true}`.
- **LED display on/off** → `PUT /heatmap/boards {"display": false | true}`.

After any change, show "pending" per board until it flips to "applied". In dev mode, add a hint: "Takes up to 5 min. Switch to Live for instant changes."

## Advanced view: all devices and what was filtered out

**This is worth building.** It makes the filtering visible, it's how we tune the room edge live, and it's a strong moment in the demo for the judges ("312 Bluetooth devices heard, 33 actually in this room").

Data comes from `GET /heatmap/debug`. Poll it every 5 s while the view is open.

1. **Filter funnel** at the top, from `funnel` in `/heatmap/latest`:
   `312 heard → 251 plausible people → 120 heard by ≥ 3 boards → 38 inside room edge → 33 present ≥ 30 s`.
   A horizontal bar or step diagram works well.
2. **All devices as dots on the room map**, at their `x`/`y`:
   - **in room:** filled dot.
   - **rejected:** hollow or grey dot, coloured by `reason`:
     - `outside_edge`: orange. These are usually the interesting ones, people just outside the walls.
     - `too_brief`: yellow. These are people passing by.
     - `not_a_person` / `stationary`: light grey with a small icon per `kind` (earbuds, tag, TV…).
   - **too_few_boards:** these have no position. Count them in a side note instead.
   - Hovering a dot shows its four RSSI values and the reason it was rejected.
3. **Device table:** id (shortened), **kind**, RSSI per board (1–4), weakest, boards heard, status and reason. It should be sortable and filterable by kind.
4. **What's in the room by type,** from `kinds_in_room`, e.g. "19 Apple devices, 6 laptops, 6 unknown". This makes a good small bar chart.
5. **Live edge tuning:** show the room-edge slider here too. When it moves, the dots change colour on the next poll, so the user sees directly what the threshold does.
6. **Calibration status:** calibration is automatic, and nobody has to walk around. Show `calibration` from `/heatmap/latest`: the room edge (auto or manual, value in dBm) and each board's offset in dB. Add a small "auto" or "manual" switch for the offsets: `PUT /heatmap/config {"offsets_mode": …}`.

## Setup flow (keep it this short)

1. Enter width and length.
2. Switch to **Live**.
3. Done. The room edge and board offsets calibrate themselves.
4. Optional: "Calibrate with headcount" (just count the people).

There are **no calibration walks**. Don't build UI for them.

## Practical notes

- **Don't put the API key in browser code.** Call the backend through your own server-side route or proxy that adds `X-API-Key`.
- **Handle missing data gracefully:** no snapshot yet, a board offline, or `values` empty. Show a friendly state, not a blank map.
- **Keep the map responsive.** The room can be any aspect ratio, and it must fit on a phone too.
- **Be honest about accuracy:** label the heatmap "approximate (±2–3 m)". Individual dots in the advanced view are rough. The value is in the overall pattern and the count.
