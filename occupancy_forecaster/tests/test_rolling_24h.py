"""Regression tests for no-live, clock-anchored 24h behaviour."""
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from generate_data import generate
from rolling_24h import (current_local_time, fit_calendar_24h, forecast_clock_24h,
                         append_24h_log, evaluate_24h_log, seasonal_predict)


class TestClockForecast(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.history = generate(weeks=6).set_index('timestamp')['count'].astype(float)
        cls.calendar = fit_calendar_24h(cls.history, 'office_1', 32)

    def test_actual_local_clock_and_288_five_minute_targets(self):
        f = forecast_clock_24h(self.calendar, at='2026-11-07 17:17:31')
        self.assertEqual(len(f), 288)
        self.assertEqual(f['horizon_min'].iloc[-1], 1440)
        self.assertEqual(f['origin_time'].iloc[0], '2026-11-07T17:15:00+00:00')
        self.assertEqual(f['target_time'].iloc[0], '2026-11-07T17:20:00+00:00')
        self.assertEqual(f['target_time'].iloc[-1], '2026-11-08T17:15:00+00:00')
        self.assertTrue((f['validation_status'] == 'awaiting_real_observation').all())
        self.assertTrue(f['observed_people'].isna().all())
        self.assertTrue((f['assumed_people_for_simulation'] >= 0).all())
        self.assertTrue((f['assumed_people_for_simulation'] <= 32).all())

    def test_historical_count_from_past_is_not_used_as_live_today(self):
        f = forecast_clock_24h(self.calendar, at='2026-11-07 17:17:31',
                               observations=self.history, short_bundle=object())
        self.assertEqual(f['observation_mode'].iloc[0], 'no_live_calendar_assumption')
        self.assertEqual(f['model_name'].iloc[-1], 'historical_time_of_week')

    def test_future_day_pattern_and_time_of_week_change(self):
        f = forecast_clock_24h(self.calendar, at='2026-11-09 08:58')
        ts = pd.to_datetime(f['target_time'], utc=True).dt.tz_convert('Europe/London')
        reference = seasonal_predict(self.calendar, pd.DatetimeIndex(ts))
        np.testing.assert_allclose(f['expected_people'], np.round(reference, 2), atol=0.01)
        self.assertGreater(f.loc[f['horizon_min'] == 180, 'expected_people'].iloc[0],
                           f.loc[f['horizon_min'] == 720, 'expected_people'].iloc[0])

    def test_duplicate_issue_log_and_no_fake_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            f = forecast_clock_24h(self.calendar, at='2026-11-07 17:17:31')
            self.assertEqual(append_24h_log(f, path/'log.csv'), 288)
            self.assertEqual(append_24h_log(f, path/'log.csv'), 0)
            data = pd.DataFrame({'timestamp': [pd.Timestamp('2026-11-07 17:00'),
                                                 pd.Timestamp('2026-11-07 17:20')],
                                 'room_id': ['office_1']*2, 'count': [12, 14]})
            data.to_csv(path/'actual.csv', index=False)
            scored = evaluate_24h_log(path/'log.csv', path/'actual.csv',
                                       'office_1', path/'scored.csv')
            self.assertEqual(len(scored), 1)
            self.assertEqual(scored['actual_people'].iloc[0], 14)
            self.assertEqual(scored['horizon_min'].iloc[0], 5)

    def test_daylight_saving_transition_preserves_elapsed_24_hours(self):
        f = forecast_clock_24h(self.calendar, at='2026-10-24 17:17:31')
        times = pd.to_datetime(f['target_time'], utc=True)
        self.assertEqual((times.iloc[-1] - pd.Timestamp('2026-10-24 17:15:00', tz='Europe/London')).total_seconds(),
                         86400)
        self.assertEqual(len(set(times)), 288)


if __name__ == '__main__':
    unittest.main()
