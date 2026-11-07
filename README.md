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
| [`occupancy_forecaster/`](occupancy_forecaster/) | People forecasting models, trained on the Aalborg office dataset (used by the backend) |
| [`docs/`](docs/) | Board ↔ server format ([`server-api.md`](docs/server-api.md)), rooms/CO₂/forecast API and frontend wiring ([`backend-api.md`](docs/backend-api.md)) |

## Data credit

Historic occupancy and CO₂ come from Aalborg University, *A Danish high-resolution dataset for six office rooms with occupancy, indoor environment, heating, ventilation, lighting and room control monitoring*, Zenodo, [doi:10.5281/zenodo.10673763](https://doi.org/10.5281/zenodo.10673763), licensed CC BY 4.0. The data isn't stored in this repo; it's downloaded by `occupancy_forecaster/run.py aalborg` or the backend's `python -m app.dataset`.

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
- **Interactive Prototype:** Open [`index.html`](index.html) or [`Frontend mark-up`](Frontend%20mark-up) in your browser (or serve via `python3 -m http.server 8080`) to test the live prototype and toggle between Consumer and Admin modes.

## Working on the project

- Don't push directly to `main`. Create a branch for each piece of work, e.g. `git switch -c feature/short-name`.
- Open a pull request when it's ready and get one teammate to review it before merging.
- Pull `main` often to keep merge conflicts small: `git pull origin main`.
