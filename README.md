# Hackaburger

Hackathon project for Saturday.

> **Status:** Project idea and tech stack not yet decided. Update this section once the team has picked a direction.

## What's here

| Folder | What |
|---|---|
| [`ble_scanner/`](ble_scanner/) | BLE scanner that runs on the Arduino UNO Q |
| [`arduino/`](arduino/) | Microcontroller sketch for the LED matrix |
| [`index.html`](index.html), [`DESIGN_SYSTEM.md`](DESIGN_SYSTEM.md) | Frontend prototype and design system |
| [`arduino-ingest/`](arduino-ingest/) | Backend: board data, people count, rooms, CO₂ estimate, forecast API. Live at `https://hackaburger.cch-uk.tech/ingest/` |
| [`occupancy_forecaster/`](occupancy_forecaster/) | People forecasting model (used by the backend) |
| [`docs/`](docs/) | Board ↔ server format ([`server-api.md`](docs/server-api.md)), rooms/CO₂/forecast API and frontend wiring ([`backend-api.md`](docs/backend-api.md)) |

## Team

- Dickon Collins ([@DickonC](https://github.com/DickonC))
- _Add teammates here_

## Getting started

```bash
git clone https://github.com/DickonC/hackaburger.git
cd hackaburger
```

Setup and run instructions will go here once the stack is chosen.

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
- **Demo modes (admin header, "Demo" group; the team picks one later, both work):**
  - **8-Hour Day (fast-forward):** +5 sim-minutes every 130 ms from 09:00 to 17:00, with scripted occupancy and automatic window flushes.
  - **Guided Demo (4 steps):** deterministic script with Next/Prev and optional auto-play: 1) 09:00 fresh room, 2) 13:00 meeting fills the room, 3) prediction fires ("1,000 ppm in ~N min, open 2 windows for M min"), 4) windows open, CO2 falls, avoided-peak counterfactual and auto-inserted 10-min calendar flush buffer. The room view shows the step caption and pulses the Next-action card on step 3.
- **Admin keyboard shortcuts** (ignored while typing in a field): `D` start/advance guided demo, `Shift+D` back a step, `S` start/pause 8-hour sim, `W` toggle windows, `+` / `-` occupancy, `R` reset, `Esc` exit demo / close dialogs, `?` shortcut hint.

## Working on the project

- Don't push directly to `main`. Create a branch for each piece of work, e.g. `git switch -c feature/short-name`.
- Open a pull request when it's ready and get one teammate to review it before merging.
- Pull `main` often to keep merge conflicts small: `git pull origin main`.
