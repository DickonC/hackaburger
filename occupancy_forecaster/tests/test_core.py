import unittest
import numpy as np
import pandas as pd
from fixture_data import generate
from occupancy_core import features, baseline_predict, seasonal_profile, conformal_radius, _masks

class TestOccupancyForecaster(unittest.TestCase):
    def test_generator_respects_capacity(self):
        df = generate(weeks=2, capacity=12, seed=1)
        self.assertEqual(len(df), 2*7*288)
        self.assertTrue(((df['count']>=0) & (df['count']<=12)).all())
        self.assertTrue(df['timestamp'].is_monotonic_increasing)

    def test_features_cannot_look_ahead(self):
        idx = pd.date_range('2026-01-01', periods=2500, freq='5min')
        s = pd.Series(np.arange(2500, dtype=float), index=idx)
        original = features(s).iloc[2200]
        s2 = s.copy()
        s2.iloc[2201:] = 999999
        modified = features(s2).iloc[2200]
        pd.testing.assert_series_equal(original, modified)

    def test_split_targets_are_not_in_next_partition(self):
        idx = pd.date_range('2026-01-01', periods=5000, freq='5min')
        train, cal, test = idx[2700], idx[3400], idx[4300]
        a, b, c = _masks(idx, train, cal, test, 60)
        h = pd.Timedelta(minutes=60)
        self.assertTrue(all(idx[a]+h <= train))
        self.assertTrue(all((idx[b]>train) & (idx[b]+h<=cal)))
        self.assertTrue(all((idx[c]>cal) & (idx[c]+h<=test)))

    def test_conformal_radius_nonnegative(self):
        self.assertEqual(conformal_radius(np.array([-1, 2, 3, -4]), .2), 4.0)

    def test_baseline_bounded(self):
        idx = pd.date_range('2026-01-01', periods=2100, freq='5min')
        s = pd.Series(np.arange(len(idx)) % 30, index=idx, dtype=float)
        p = seasonal_profile(s)
        preds = baseline_predict(p, idx[2000:2010], np.full(10, 29.0), 30, 30)
        self.assertTrue(((preds >= 0) & (preds <= 30)).all())

if __name__ == '__main__':
    unittest.main()
