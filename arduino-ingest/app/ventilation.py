"""When to open a window, and for how long: the CO2 ODE solved for a threshold.

The room is one well-mixed box of air:

    V dC/dt = 1000 * G * N(t) - Q * (C - C_out)

C indoor CO2 (ppm), V volume (m3), G CO2 per person (L/h), N(t) people, Q airflow (m3/h),
C_out outdoor CO2. The forecast gives N(t) as one value per 5-minute step. With N and Q
constant over a step the ODE solves exactly (this is rooms.step_co2):

    C(t) = C_ss + (C0 - C_ss) * exp(-t / tau),   C_ss = C_out + 1000 * G * N / Q,   tau = V / Q

so stepping through the forecast is the exact solution for that N(t), not an
approximation. Within a step C moves steadily towards C_ss, so it crosses a threshold
at most once per step, and the crossing time is

    t* = tau * ln((C0 - C_ss) / (C_th - C_ss))

Thresholds (team's cognitive bands): 1000 ppm triggers "open a window", 1500 is
critical ("cross-ventilate now"), 800 is an early heads-up, and a flush (all windows
open) aims to bring the room back down to 600.
"""

import math
from datetime import datetime, timezone

HEADS_UP_PPM = 800.0
OPEN_PPM = 1000.0
CRITICAL_PPM = 1500.0
TARGET_PPM = 600.0


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def segment_crossing(c0: float, people: float, room: dict, q_m3h: float, dt_h: float,
                     threshold: float) -> float | None:
    """Hours into a step (constant people and airflow) at which C reaches `threshold`, or None.

    Works in either direction: rising towards a higher steady state or falling towards a lower one.
    """
    if c0 == threshold:
        return 0.0
    q = max(q_m3h, 0.1)
    c_ss = room["outdoor_ppm"] + people * room["co2_gen_lph"] * 1000.0 / q
    # The threshold must lie strictly between where we are and where the step is heading
    if not (min(c0, c_ss) < threshold < max(c0, c_ss)):
        return None
    t = (room["volume_m3"] / q) * math.log((c0 - c_ss) / (threshold - c_ss))
    return t if t <= dt_h else None


def _step(c0: float, people: float, room: dict, q_m3h: float, dt_h: float) -> float:
    q = max(q_m3h, 0.1)
    c_ss = room["outdoor_ppm"] + people * room["co2_gen_lph"] * 1000.0 / q
    return c_ss + (c0 - c_ss) * math.exp(-(q / room["volume_m3"]) * dt_h)


def first_crossing(c0: float, t0: float, origin: float, times: list[float], people: list[float],
                   room: dict, q_m3h: float, threshold: float, rising: bool) -> tuple[float | None, float]:
    """Exact first time >= t0 at which C reaches `threshold`, starting from C(t0) = c0.

    Step i of the forecast runs from times[i-1] (origin for i = 0) to times[i] with people[i],
    as in the forecast endpoint. Returns (epoch or None if not within the forecast, C at the end).
    """
    if (c0 >= threshold) if rising else (c0 <= threshold):
        return t0, c0
    c, prev = c0, origin
    for t, p in zip(times, people):
        start = max(prev, t0)
        prev = t
        if t <= start:
            continue
        dt_h = (t - start) / 3600
        hit = segment_crossing(c, p, room, q_m3h, dt_h, threshold)
        if hit is not None:
            return start + hit * 3600, threshold
        c = _step(c, p, room, q_m3h, dt_h)
    return None, c


def _minutes(t: float | None, origin: float) -> float | None:
    return None if t is None else round((t - origin) / 60, 1)


def advice(c0: float, origin: float, times: list[float], people: list[float], room: dict,
           q_closed: float, q_open: float, window_open_now: bool = False,
           people_low: list[float] | None = None, people_high: list[float] | None = None) -> dict:
    """Window advice for the forecast starting at `origin` with CO2 `c0`.

    q_closed / q_open: airflow (m3/h) with windows shut / all windows open.
    people_low / people_high: the forecast's 80% band, for a range on "open in X min".
    """
    def cross(threshold: float, ppl: list[float], q: float) -> float | None:
        return first_crossing(c0, origin, origin, times, ppl, room, q, threshold, rising=True)[0]

    q_now = q_open if window_open_now else q_closed
    out = {
        "thresholds_ppm": {"heads_up": HEADS_UP_PPM, "open": OPEN_PPM, "critical": CRITICAL_PPM,
                           "target": TARGET_PPM},
        "window_open_now": window_open_now,
        "co2_now": round(c0),
    }

    if window_open_now:
        # Windows already open: when has the flush done its job?
        done, c_end = first_crossing(c0, origin, origin, times, people, room, q_open, TARGET_PPM, rising=False)
        out.update(status="flushing", close_window_in_min=_minutes(done, origin), close_window_at=_iso(done) if done else None,
                   lowest_reachable_ppm=None if done else round(c_end))
        out["message"] = (f"Windows open: back to {TARGET_PPM:.0f} ppm in {_minutes(done, origin):.0f} min, then close."
                          if done is not None else
                          f"Windows open, but CO2 won't reach {TARGET_PPM:.0f} ppm with this many people.")
        return out

    heads_up, open_at, critical = (cross(th, people, q_now) for th in (HEADS_UP_PPM, OPEN_PPM, CRITICAL_PPM))
    # More people -> CO2 rises sooner, so the upper people band gives the earliest time
    earliest = cross(OPEN_PPM, people_high, q_now) if people_high else None
    latest = cross(OPEN_PPM, people_low, q_now) if people_low else None

    out.update({
        "heads_up_in_min": _minutes(heads_up, origin),
        "open_window_in_min": _minutes(open_at, origin),
        "open_window_at": _iso(open_at) if open_at is not None else None,
        "open_window_range_min": [_minutes(earliest, origin), _minutes(latest, origin)]
        if people_high or people_low else None,
        "critical_in_min": _minutes(critical, origin),
        "critical_at": _iso(critical) if critical is not None else None,
    })

    flush_min = None
    if open_at is not None:
        # Open all windows when 1000 is reached (or now, if already past it) and flush down to the target
        c_open = max(c0, OPEN_PPM) if open_at == origin else OPEN_PPM
        done, c_end = first_crossing(c_open, open_at, origin, times, people, room, q_open, TARGET_PPM, rising=False)
        flush_min = _minutes(done, open_at) if done is not None else None
        out["flush_duration_min"] = flush_min
        out["flush_reaches_target"] = done is not None
        if done is None:
            out["lowest_reachable_ppm"] = round(c_end)
    else:
        out["flush_duration_min"] = None

    if open_at is None and heads_up is not None:
        # Mild drag (800-1000) now or soon, but the forecast doesn't reach the 1000 trigger
        out["status"] = "heads_up"
        out["message"] = (f"CO2 is {c0:.0f} ppm (mild drag), but stays below {OPEN_PPM:.0f} ppm: no window needed yet."
                          if heads_up == origin else
                          f"CO2 reaches {HEADS_UP_PPM:.0f} ppm in {_minutes(heads_up, origin):.0f} min, "
                          f"but stays below {OPEN_PPM:.0f} ppm: no window needed yet.")
    elif open_at is None:
        out["status"] = "ok"
        out["message"] = f"No window needed: CO2 stays below {OPEN_PPM:.0f} ppm in the forecast."
    elif open_at == origin:
        out["status"] = "critical" if c0 >= CRITICAL_PPM else "open_now"
        out["message"] = ("Cross-ventilate now: open all windows" if c0 >= CRITICAL_PPM else "Open a window now")
    else:
        mins = _minutes(open_at, origin)
        out["status"] = "heads_up" if heads_up == origin else "planned"
        out["message"] = f"Open a window in {mins:.0f} min"
    if open_at is not None:
        out["message"] += (f" for about {flush_min:.0f} min to get back to {TARGET_PPM:.0f} ppm."
                           if flush_min is not None else ", and keep it open: the room won't get down to "
                           f"{TARGET_PPM:.0f} ppm with this many people.")
    return out
