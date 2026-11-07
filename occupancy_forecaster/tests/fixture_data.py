"""Test fixture: reproducible SYNTHETIC five-minute occupancy for unit tests only.

The product trains on the real Aalborg office dataset (see aalborg.py); this
generator just gives the tests a small, deterministic series."""
from __future__ import annotations

import argparse
from pathlib import Path
import numpy as np
import pandas as pd


def generate(weeks: int = 10, capacity: int = 32, seed: int = 43,
             start: str = '2026-08-28 15:00', room_id: str = 'office_1') -> pd.DataFrame:
    if weeks < 2 or capacity < 1:
        raise ValueError('Use at least 2 weeks and a positive capacity')
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=weeks * 7 * 24 * 12, freq='5min')
    counts = np.zeros(len(idx), dtype=int)
    x = 0
    day_amp = {}
    day_shift = {}
    day_meetings = {}
    for day in idx.normalize().unique():
        day_amp[day] = float(rng.uniform(0.78, 1.15))
        day_shift[day] = float(rng.normal(0, 22))
        day_meetings[day] = [(int(rng.integers(9, 17)), float(rng.uniform(1, 5))) for _ in range(2)]

    for k, t in enumerate(idx):
        weekday = t.dayofweek
        minute = t.hour * 60 + t.minute
        d = t.normalize()
        if weekday < 5:
            shifted = minute - day_shift[d]
            # An office's arrival, lunch break, afternoon and departure patterns.
            morning = 0.62 * np.exp(-0.5 * ((shifted - 625) / 115)**2)
            afternoon = 0.83 * np.exp(-0.5 * ((shifted - 885) / 115)**2)
            lunchtime_dip = 0.12 * np.exp(-0.5 * ((shifted - 755) / 42)**2)
            fraction = max(0.0, morning + afternoon - lunchtime_dip)
            weekday_factor = [1.0, 1.05, 1.02, 0.98, 0.75][weekday]
            target = capacity * min(0.97, fraction * 0.85 * weekday_factor * day_amp[d])
            for mh, ma in day_meetings[d]:
                target += ma * np.exp(-0.5 * ((minute - 60 * mh) / 33)**2)
        else:
            target = capacity * (0.03 + 0.11 * np.exp(-0.5 * ((minute - 790) / 120)**2))
        # Bounded, persistent dynamics with unpredictable arrivals/departures.
        innovation = rng.normal(0, 0.95)
        step = int(np.clip(np.rint((target - x) * 0.26 + innovation), -4, 4))
        x = int(np.clip(x + step, 0, capacity))
        counts[k] = x

    return pd.DataFrame({
        'timestamp': idx,
        'room_id': room_id,
        'count': counts,
    })

