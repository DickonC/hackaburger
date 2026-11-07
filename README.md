# Hackaburger

Hackathon project for Saturday.

> **Status:** Project idea and tech stack not yet decided. Update this section once the team has picked a direction.

## What's here

| Folder | What |
|---|---|
| [`ble_scanner/`](ble_scanner/) | BLE scanner that runs on the Arduino UNO Q |
| [`arduino/`](arduino/) | Microcontroller sketch for the LED matrix |
| [`arduino-ingest/`](arduino-ingest/) | Server: receives scans, counts people, sends settings back. Live at `https://hackaburger.cch-uk.tech/ingest/` |
| [`docs/`](docs/) | Board ↔ server format ([`server-api.md`](docs/server-api.md)) |

## Team

- Dickon Collins ([@DickonC](https://github.com/DickonC))
- _Add teammates here_

## Getting started

```bash
git clone https://github.com/DickonC/hackaburger.git
cd hackaburger
```

Setup and run instructions will go here once the stack is chosen.

## Working on the project

- Don't push directly to `main`. Create a branch for each piece of work, e.g. `git switch -c feature/short-name`.
- Open a pull request when it's ready and get one teammate to review it before merging.
- Pull `main` often to keep merge conflicts small: `git pull origin main`.
