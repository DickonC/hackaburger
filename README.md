# Caelum: a virtual CO₂ sensor

**Make the invisible visible.** CO₂ builds up fast in closed meeting rooms, study pods and bedrooms: often from about 420 ppm to over 1,500 ppm within an hour or two. Nobody can sense it. People notice only the effects: slower thinking and "afternoon brain fog".

Caelum estimates a room's CO₂ **without a CO₂ sensor**. It counts the people in the room with cheap Bluetooth (BLE) scanners and turns that headcount into CO₂ with a mass-balance model of the room (volume, people, open windows). It then shows the effect on concentration and tells people when to open which windows, and for how long, before the air gets stuffy.

Built at HackTheBurgh 2026.

## How it works

1. **Count people, privately.** Four Arduino UNO Q boards, one in each corner of a room, report the anonymised Bluetooth devices they hear. The server keeps the devices inside the room (by signal strength across the four boards), works out roughly where they are for a heatmap, and divides by devices per person to get a headcount. No cameras and no personal data.
2. **Estimate CO₂.** A mass-balance equation steps the room's CO₂ forward from the headcount, the room's volume, infiltration and the open window area.
3. **Predict and act.** The app projects the next 4 hours: how soon the room hits 1,000 ppm, how many windows to open and for how long, and the best time to book the room. It translates CO₂ into a focus score based on published cognitive-performance studies (Harvard COGfx, LBNL).
4. **Forecast occupancy.** People-forecasting models are trained on a public office dataset with real occupancy and CO₂, as a stand-in until the boards have collected weeks of our own data.

**Live demo:** <https://hackaburger.cch-uk.tech/> (room view) and <https://hackaburger.cch-uk.tech/admin.html> (ops console). The **Live Room** uses the real boards. The other rooms run on realistic sample data, following a typical weekday.

## What's here

| Folder | What |
|---|---|
| [`ble_scanner/`](ble_scanner/) | BLE scanner that runs on the Arduino UNO Q |
| [`arduino/`](arduino/) | Microcontroller sketch for the LED matrix |
| [`index.html`](index.html), [`DESIGN_SYSTEM.md`](DESIGN_SYSTEM.md) | Frontend prototype and design system |
| [`arduino-ingest/`](arduino-ingest/) | Backend: board data, people count, rooms, CO₂ estimate, forecast API. Live at `https://hackaburger.cch-uk.tech/ingest/` |
| [`occupancy_forecaster/`](occupancy_forecaster/) | People forecasting models, trained on the Aalborg office dataset (used by the backend) |
| [`docs/`](docs/) | Board ↔ server format ([`server-api.md`](docs/server-api.md)), rooms/CO₂/forecast API and frontend wiring ([`backend-api.md`](docs/backend-api.md)) |

## Data credit

Historic occupancy and CO₂ come from Aalborg University, *A Danish high-resolution dataset for six office rooms with occupancy, indoor environment, heating, ventilation, lighting and room control monitoring*, Zenodo, [doi:10.5281/zenodo.10673763](https://doi.org/10.5281/zenodo.10673763), licensed CC BY 4.0. The data isn't stored in this repo; it's downloaded by `occupancy_forecaster/run.py aalborg` or the backend's `python -m app.dataset`.

## Getting started

```bash
git clone https://github.com/DickonC/hackaburger.git
cd hackaburger
python3 -m http.server 8080   # then open http://localhost:8080/admin.html
```

The backend (`arduino-ingest/`) runs with Docker Compose; see its README and [`docs/backend-api.md`](docs/backend-api.md).

## Frontend & Design System

The frontend architecture follows a **Dual-Persona Design** (Consumer-Friendly Experience + Dedicated Admin Console):
- **Design System & Component Specs:** See [`DESIGN_SYSTEM.md`](DESIGN_SYSTEM.md) for UX principles, Swiss Grid layout rules, Fontshare typography, color palettes, and component selections.
- **File layout (build-free, plain HTML/JS):**
  - [`index.html`](index.html): **Room view** (public, projector / door tablet). Light consumer persona: hero status, crowd gauge, focus capacity, next action, 4-hour forecast, today's schedule.
  - [`admin.html`](admin.html): **Ops console** (same light palette as the room view): sandbox sliders, Arduino fleet, BLE log, charts, demo controls.
  - [`js/engine.js`](js/engine.js): shared engine (`window.Caelum`): rooms, state, mass-balance + Harvard COGfx models, forecast, simulation clock, demo modes, cross-tab sync. No DOM access.
  - [`Frontend mark-up`](Frontend%20mark-up): older single-file prototype, kept for reference only.
- **Run it:** `python3 -m http.server 8080`, then open <http://localhost:8080/admin.html> and click **Open Room View** (or open `index.html` in a second window).
- **Two-screen sync:** the admin page is the authority (runs the clock and demo modes) and broadcasts state over `BroadcastChannel('caelum')`. The room view mirrors it live and sends commands back ("I opened them", room switch). If no admin is found within ~3 s the room view runs its own local simulation, so `index.html` also works on its own. Both pages must be on the same origin and browser profile.
- **Clock and sample rooms:** outside the demos the clock is the real time of day. Each sample room (study pod, meeting room, bedroom; custom rooms get the meeting-room day scaled to their seats) follows a typical weekday in `js/engine.js` (`DAY_PLANS`): bookings with no-shows and a ghost booking, drop-ins, and Caelum's 10-minute fresh-air breaks. On load the engine replays the last 24 hours so CO₂ and the 2-hour chart history match the time of day. Weekends show the same weekday, labelled "Sample weekday". Moving the people slider holds that headcount until the next booking starts or ends. The Live Room has no plan; its people come from the boards.
- **Where people are (admin):** rooms without the four boards show a sample heatmap built from the simulated headcount (labelled "Sample data"); the Live Room shows the real heatmap or "Waiting for the boards". **Expand** (or `M`) fills the browser window with the map; **Close** or `Esc` returns.
- **Demo modes (admin header):**
  - **8-Hour Day (fast-forward):** +5 sim-minutes every 65 ms (about 6 s for the day) from 09:00 to 17:00, with scripted occupancy and automatic window flushes.
  - **Guided Demo (4 steps):** deterministic script with Next/Prev and optional auto-play: 1) 09:00 fresh room, 2) 13:00 meeting fills the room, 3) prediction fires ("1,000 ppm in ~N min, open 2 windows for M min"), 4) windows open, CO2 falls, avoided-peak counterfactual and auto-inserted 10-min calendar flush buffer. The room view shows the step caption and pulses the Next-action card on step 3.
- **Admin keyboard shortcuts** (ignored while typing in a field): `D` start/advance guided demo, `Shift+D` back a step, `S` start/pause 8-hour sim, `W` toggle windows, `+` / `-` occupancy, `R` reset, `M` map full view, `Esc` exit demo / close dialogs / close the map, `?` shortcut hint.

## Working on the project

- Don't push directly to `main`. Create a branch for each piece of work, e.g. `git switch -c feature/short-name`.
- Open a pull request when it's ready and get one teammate to review it before merging.
- Pull `main` often to keep merge conflicts small: `git pull origin main`.
