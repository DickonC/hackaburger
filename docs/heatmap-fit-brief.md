# Task brief: better in-room filter and position fit (backend, switchable)

> **For an AI coding agent working on `arduino-ingest/app/heatmap.py`.** Read this whole file first.
> Background: [`heatmap-backend-guide.md`](heatmap-backend-guide.md) (current heatmap design) and [`server-api.md`](server-api.md) (board message format).
>
> **Goal:** add two **optional** improvements, each switched by a config field. **The defaults must keep today's behaviour exactly,** so merging this changes nothing live until someone switches it on, and switching back is one config change.
>
> **Do not change the frontend** (`admin.html`, `backend.js`, `index.html`) **or any existing endpoint's response shape.** New fields may only be *added*.

## What to build

| # | Feature | Config switch | Default (= today) | New value |
|---|---|---|---|---|
| A | Room-edge rule using the **second-weakest** board | `filter.edge_rule` | `"weakest"` | `"second_weakest"` |
| B | Device position from a **path-loss fit** instead of a weighted average | `position.method` | `"weighted"` | `"fit"` |

Both are independent, so either can be on or off.

## Why (evidence from a simulation)

We simulated a 5 × 5.2 m room with boards 0.5 m in from each corner. Phones had unknown transmit power (−70 … −50 dBm at 1 m), the path-loss exponent was n = 2.5, measurement noise was ±4 dB, and devices outside got an extra 8 dB wall loss. Over 4,000 devices:

**In-room filter:**

| Rule | Inside devices kept | Outside devices rejected |
|---|---|---|
| weakest ≥ diagonal-based edge (today) | 56 % | 97 % |
| **second-weakest ≥ side-based edge, weakest ≥ edge − 8 dB (A)** | **71 %** | **93 %** |

**Position (inside devices):**

| Method | Median error | 80th percentile | Median distance from centre (truth: 2.06 m) |
|---|---|---|---|
| weighted average, `weight_k` 2 | 0.82 m | 1.25 m | 1.32 m (too clumped) |
| weighted average, `weight_k` 1 (current setting) | 0.77 m | 1.21 m | 2.17 m |
| fit, best cell (argmin) | 0.78 m | 1.27 m | 2.36 m (too spread) |
| **fit, probability-weighted mean (B)** | **0.68 m** | **1.10 m** | **1.96 m** |

**Important finding: do NOT use the fit to decide inside/outside.** Because the phone's transmit power P₀ is unknown and fitted per device, a wall that weakens all four boards equally is indistinguishable from a weaker phone. In the simulation, "best position is outside the room" rejected only 16–66 % of outside devices, or wrongly rejected half the inside ones. **Inside/outside stays a signal-strength rule (feature A).** The fit only improves *where* in-room devices are drawn.

---

## Safety: how to deliver this

1. **Branch:** work on `feature/heatmap-fit`, never directly on `main`. The server deploys automatically from `main`.
2. **Backup tag before merging:**
   ```bash
   git tag backup-before-fit origin/main && git push origin backup-before-fit
   ```
3. **Defaults unchanged:** `edge_rule = "weakest"` and `method = "weighted"`. Add a test that proves the default output is identical to the old code (see acceptance check 1).
4. **Switching on and off** (no deploy needed, takes effect at the next snapshot, ≤ 10 s):
   ```bash
   # on
   curl -X PUT "$API/ingest/heatmap/config" -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
     -d '{"filter":{"edge_rule":"second_weakest","edge_mode":"auto"},"position":{"method":"fit"}}'
   # off (back to today's behaviour)
   curl -X PUT "$API/ingest/heatmap/config" -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
     -d '{"filter":{"edge_rule":"weakest"},"position":{"method":"weighted"}}'
   ```
5. **If the code itself breaks the server** after merging, revert the merge. Never force-push `main`.
   ```bash
   git switch main && git pull && git revert -m 1 <merge-commit> && git push
   ```

---

## Config additions (in `DEFAULT_CONFIG` and the validation spec)

```jsonc
"filter": {
  // …existing fields unchanged…
  "edge_rule": "weakest",          // "weakest" | "second_weakest"
  "weakest_slack_db": 8.0          // only used by second_weakest: the weakest board may be this much below the edge
},
"position": {
  "weight_k": 1.0,                 // existing, used by method "weighted"
  "method": "weighted",            // "weighted" | "fit"
  "path_loss_n": 2.5,              // fit: path-loss exponent
  "fit_cell_m": 0.25,              // fit: grid resolution
  "noise_db": 3.5                  // fit: assumed RSSI noise (σ) for the probability weights
}
```

| Field | Type | Allowed |
|---|---|---|
| `filter.edge_rule` | string | `"weakest"`, `"second_weakest"` |
| `filter.weakest_slack_db` | float | 0 … 20 |
| `position.method` | string | `"weighted"`, `"fit"` |
| `position.path_loss_n` | float | 1.5 … 4.0 |
| `position.fit_cell_m` | float | 0.1 … 1.0 |
| `position.noise_db` | float | 1 … 10 |

**The stored config has no new fields yet.** `load_config` must deep-merge it over `DEFAULT_CONFIG` (it probably already does via `merged`). Existing configs then get the defaults automatically. Return the new fields from `GET /heatmap/config`.

---

## A. Edge rule `"second_weakest"`

**Idea.** For a device inside the room, at most **one** board is far away (the opposite corner, across the diagonal). The **second-farthest** board is at most about one **side length** away. A device outside, behind a wall, has **two** far boards (both on the opposite side) plus the wall loss. So the second-weakest signal separates inside from outside better than the weakest. It's also robust when a single board is briefly blocked.

**Rule** (on offset-adjusted RSSI `adj[b]` of the boards that heard the device; `min_boards` ≥ 3 is already checked before this):

```
s = sorted(adj.values())          # ascending: s[0] weakest, s[1] second-weakest
if edge_rule == "weakest":        # today
    passes = s[0] >= edge
else:                             # "second_weakest"
    passes = s[1] >= edge and s[0] >= edge - weakest_slack_db
reason if not passes: "outside_edge"   (unchanged reason name, so the frontend already handles it)
```

**Auto edge** (when `edge_mode == "auto"`). Today it uses the longest distance between any two boards (the diagonal). For `second_weakest`, use the **longer room side** instead:

```
if edge_rule == "second_weakest":
    d_ref = max(width_m, length_m)
else:
    d_ref = max distance between two boards          # today's auto_edge, unchanged
edge = clamp(round(RSSI_1M - 10*PATH_LOSS_N*log10(max(d_ref, 1)) - edge_margin_db), -100, -60)
```

Keep `RSSI_1M = -59`, `PATH_LOSS_N = 2.2` and `edge_margin_db` as they are. **Manual mode** (`edge_mode == "manual"`): `edge_dbm` is used as `edge` for whichever rule is active.

**Store and expose:**
- **`weakest_dbm` stays** in the device rows.
- **New field: `second_weakest_dbm`** in each `/heatmap/debug` device. Add a nullable column, the same way `kind` was added in `migrate`.
- **The funnel's `inside_edge` count** follows the active rule.

---

## B. Position method `"fit"`

**Model.** Received signal at board `b` for a device at position `p`, with unknown phone power `P0`:

```
adj[b] ≈ P0 − 10 · n · log10( max(dist(p, board_b), 0.5) )
```

`P0` is unknown per device, so fit it. Only the **differences** between boards then decide the position, independent of the phone.

**Algorithm** for each device with **≥ 3 boards heard**. For devices with only 2 boards, keep the weighted average, since they're rejected anyway and only drawn in debug.

1. **Grid over the room only:** cell centres `x = fit_cell_m/2, 3·fit_cell_m/2, … < width_m`, and the same for y. Precompute once per snapshot:
   - `R[c, b] = −10 · n · log10(max(dist(cell_c, board_b), 0.5))`, an array of shape `[cells, 4]`.
2. **For one device:** let `H` = the boards that heard it, and `a[b] = adj[b]` for b in H.
   ```
   P0[c]  = mean_b∈H ( a[b] − R[c,b] )                 # best phone power for this cell
   err[c] = Σ_b∈H ( a[b] − R[c,b] − P0[c] )²           # dB² misfit
   err[c] = +∞ where P0[c] < −80 or P0[c] > −30         # physically implausible phone power
   L[c]   = exp( −(err[c] − min(err)) / (2 · noise_db²) )
   (x, y) = Σ_c L[c]·cell_c / Σ_c L[c]                  # probability-weighted mean
   ```
3. **If every cell is implausible** (all `err` infinite), fall back to the weighted average for that device.
4. **Output** `x`, `y` as today, rounded to 0.01 m, so it's always inside the room. Optionally add `fit_rms_db = sqrt(min(err)/|H|)` to the debug device. The frontend ignores unknown fields.

**Do not reject devices based on the fit** (see "Why"). Inside/outside is decided only by the edge rule.

**Vectorise with numpy:** stack all devices with the same set of boards heard, and compute `[devices, cells, boards]` in one go. For a 7.5 × 10 m room at 0.25 m that's about 1,200 cells. With about 900 devices × 4 boards it's ~4 M operations per snapshot, which is well under 100 ms.

**Heatmap grid:** unchanged. It's still a Gaussian per in-room device at its `(x, y)` with `sigma_m`. Only the positions it uses come from the fit.

---

## Acceptance checks (write these as tests in `arduino-ingest/tests/`)

1. **Defaults unchanged:** with the default config, a snapshot built from a fixed fixture gives **exactly** the same `x`, `y`, `status`, `reason`, funnel and grid as before this change. Record the expected values from the current code before changing it.
2. **`second_weakest` keeps a corner device:** boards at (0.5, 0.5), (4.5, 0.5), (4.5, 4.7), (0.5, 4.7) in a 5 × 5.2 m room, `edge = −79`, and a device with adjusted RSSI {−45, −70, −81, −72}. With `weakest` it's rejected (`outside_edge`). With `second_weakest` (slack 8) it's kept, because s[1] = −72 ≥ −79 and s[0] = −81 ≥ −87.
3. **`second_weakest` rejects a device behind a wall:** RSSI {−66, −68, −84, −85} → `outside_edge`, because s[1] = −84 < −79.
4. **Auto edge per rule:** room 5 × 5.2 m, margin 4 → `weakest` gives −80 (±1), and `second_weakest` gives −79 (±1), from `max(5, 5.2)`.
5. **Fit recovers a position without noise:** generate RSSI for a device at (1.0, 4.0) with n = 2.5 and P0 = −62. The fit (`path_loss_n` 2.5, `noise_db` 3.5) returns a position within 0.5 m of (1.0, 4.0), independent of P0. Repeat with P0 = −50 and P0 = −72.
6. **Fit with noise beats the weighted average on average:** 500 random inside devices, ±4 dB noise, P0 random in −70 … −50. The fit's median error ≤ the weighted average's (`weight_k` 1) median error. Use a fixed random seed.
7. **Switching back:** `PUT {"filter":{"edge_rule":"weakest"},"position":{"method":"weighted"}}` → the next snapshot matches check 1's behaviour.
8. **Validation:** `PUT {"position":{"method":"magic"}}` → `422`. Same for `path_loss_n: 10` and `edge_rule: "x"`.
9. **API shape:** `/heatmap/latest` and `/heatmap/debug` contain all fields they had before. The new fields are only additions.
10. **Performance:** building a snapshot with 900 devices and the fit on takes < 300 ms.

## When you're done

- Run the existing test suite plus the new tests. All must pass.
- Push the branch, open a pull request, and in its description:
  - list the new config fields and their defaults
  - give the on and off `curl` commands from above
  - state that defaults keep today's behaviour
- **After merging** (still default = off), switch on in this order and check the admin page after each step:
  1. `{"filter":{"edge_rule":"second_weakest","edge_mode":"auto"}}`. The in-room count should rise a bit, and outside devices should still be rejected.
  2. `{"position":{"method":"fit"}}`. Dots should spread more naturally. Run the corner test: a phone held next to board N for 30 s should land near corner N.
- **Note:** the live room edge was last set **manually to −99 dBm**, which effectively disables the edge filter. Step 1 switches it back to `auto`.
