# Hackaburger

Hackathon project for Saturday.

> **Status:** Project idea and tech stack not yet decided. Update this section once the team has picked a direction.

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
