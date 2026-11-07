# arduino-ingest

The backend: receives BLE scans from the UNO Q boards, works out the people count, and sends each board its settings in the reply. It also stores rooms and occupancy history, estimates CO₂, and serves the 24-hour forecast using [`occupancy_forecaster`](../occupancy_forecaster/).

- Board API: [`docs/server-api.md`](../docs/server-api.md)
- Calculation rules: [`docs/server-agent-brief.md`](../docs/server-agent-brief.md)
- Rooms, CO₂, forecast, and how to wire the frontend: [`docs/backend-api.md`](../docs/backend-api.md)

Public base URL: `https://hackaburger.cch-uk.tech/ingest/`. Every endpoint except `/health` needs the `X-API-Key` header.

| Method | Path | What |
|---|---|---|
| POST | `/ingest/data` | Board sends a reading (or a list of them). Reply: `{"stored", "dropped", "config"}` |
| GET | `/ingest/data?device=&since=&limit=` | Raw stored messages, newest first |
| GET / PUT | `/ingest/config/{device}` | Read or change a board's settings (`{"preset": "demo"}` works too) |
| GET | `/ingest/occupancy/latest?device=` | Current count, smoothed count and online status |
| GET | `/ingest/occupancy/history?device=&from=&to=&step_s=&threshold_dbm=` | Time series, optionally recounted at another range |
| GET/POST/PUT/DELETE | `/ingest/rooms[/{id}]` | Rooms: volume, windows, capacity, which board is in them |
| PUT | `/ingest/rooms/{id}/windows` | Which windows are open |
| GET/POST/DELETE | `/ingest/rooms/{id}/history` | Historic people counts, plus measured CO₂ where available (the Aalborg dataset rooms) |
| GET | `/ingest/rooms/{id}/occupancy`, `/co2`, `/forecast` | People (board + history), estimated CO₂, 24 h people + CO₂ forecast |
| GET | `/ingest/health`, `/ingest/docs` | Health check and interactive API docs |

## On the server

Runs as the Docker container `hackaburger-ingest` on `127.0.0.1:8094`. The SQLite database is in `data/` and the API key is in `.env`. Neither is in git.
The image is built from the repo root, because it includes `occupancy_forecaster/occupancy_core.py`, `rolling_24h.py` and `aalborg.py` (see `Dockerfile.dockerignore`).
On the first start with an empty `rooms` table, it creates the frontend's three preset rooms. To load the historic data and train the models, run once: `docker exec hackaburger-ingest python -m app.dataset`. This uses the Aalborg office dataset, doi:10.5281/zenodo.10673763, CC BY 4.0; see [`docs/backend-api.md`](../docs/backend-api.md). The downloaded data and trained models are stored in `data/datasets/` and `data/models/`.

```bash
cd ~/hackaburger/arduino-ingest
cp .env.example .env        # first time only, then set API_KEY
docker compose up -d --build  # deploy or update
./run-tests                 # tests in a throwaway container; never touches data/
hackaburger                 # live table in the terminal (symlink to ./live)
```

Host nginx config: `nginx-host.conf`. Only `/ingest/` is forwarded to this container.

## Storage

Table `readings` holds one row per message:
- `data`: the original JSON, stored as received
- the server's calculation alongside it: `measured_at` (from `ts`, otherwise `received_at`), `source`, `threshold_dbm` and `people_factor` (the config in effect at the time), `devices_in_range`, `heard_total`, `people_raw`, and `rssis` (the valid RSSIs, used for recounts)

Table `configs` holds one JSON config per board. `uno-q-1` starts in room mode and `uno-q-2` in demo mode.

Tables `rooms`, `occupancy_history` and `window_events` are described in [`docs/backend-api.md`](../docs/backend-api.md).
