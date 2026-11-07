import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from generate_data import generate
from occupancy_core import (CANDIDATES, features, split_masks, fit_horizon,
                            append_forecast_log, evaluate_forecast_log)


class TestResearchDesign(unittest.TestCase):
    def test_model_targets_respect_four_chronological_segments(self):
        index = pd.date_range('2026-01-01', periods=5000, freq='5min')
        a,b,c,d = [index[n] for n in (2300,2800,3500,4100)]
        h = pd.Timedelta(minutes=60)
        m = split_masks(index, a,b,c,d,60)
        self.assertTrue(((index[m['train']] + h) <= a).all())
        self.assertTrue(((index[m['selection']] > a) & (index[m['selection']] + h <= b)).all())
        self.assertTrue(((index[m['calibration']] > b) & (index[m['calibration']] + h <= c)).all())
        self.assertTrue(((index[m['test']] > c) & (index[m['test']] + h <= d)).all())
        for i, name in enumerate(m):
            for other in list(m)[i+1:]:
                self.assertFalse((m[name] & m[other]).any())

    def test_test_labels_do_not_change_selected_model(self):
        df = generate(weeks=5, seed=51)
        s = df.set_index('timestamp')['count'].astype(float)
        idx = s.index
        bounds = [idx[int((len(idx)-1)*f)] for f in (.50, .60, .70, .85)]
        a, metrics, detail = fit_horizon(s, features(s), 30, *bounds, 32)
        self.assertEqual(set(metrics['model']), set(CANDIDATES))
        self.assertEqual(len(detail['selected_model'].unique()), 1)
        # Mutate only held-out outcomes; earlier fitting and selection must be identical.
        tampered = s.copy()
        tampered.loc[tampered.index > bounds[2]] = 0
        b, _, _ = fit_horizon(tampered, features(tampered), 30, *bounds, 32)
        self.assertEqual(a['selected_model'], b['selected_model'])
        self.assertEqual(a['selection_errors'], b['selection_errors'])
        self.assertEqual(a['radius'], b['radius'])

    def test_forecast_log_idempotent_and_scores_only_observed_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            log = folder/'forecast_log.csv'
            readings = folder/'obs.csv'
            origin = pd.Timestamp('2026-11-07 10:00:00')
            records = pd.DataFrame({
                'room_id': ['room_a', 'room_a', 'room_a'],
                'origin_time': [origin]*3,
                'target_time': [origin+pd.Timedelta(minutes=m) for m in (5,10,15)],
                'horizon_min':[5,10,15],
                'model_name':['ridge_delta']*3,
                'expected_people':[10,12,14],
                'lower_80':[8,10,12],
                'upper_80':[12,14,16],
            })
            self.assertEqual(append_forecast_log(records, log), 3)
            self.assertEqual(append_forecast_log(records, log), 0)
            pd.DataFrame({
                'timestamp':[origin,origin+pd.Timedelta(minutes=5),origin+pd.Timedelta(minutes=10)],
                'room_id':['room_a']*3,
                'count':[9,11,13],
            }).to_csv(readings,index=False)
            ev = evaluate_forecast_log(log, readings, 'room_a', folder/'evaluation.csv')
            self.assertEqual(len(ev), 2)
            self.assertEqual(set(ev['horizon_min']), {5,10})
            self.assertTrue((ev['abs_error'] == 1).all())
            self.assertTrue(ev['covered_80'].all())
            # A missing sample cannot be substituted with a forward-filled count.
            late = pd.DataFrame({'timestamp':[origin+pd.Timedelta(minutes=20)],
                                 'room_id':['room_a'], 'count':[15]})
            old = pd.read_csv(readings)
            pd.concat([old,late],ignore_index=True).to_csv(readings,index=False)
            ev = evaluate_forecast_log(log, readings, 'room_a', folder/'evaluation.csv')
            self.assertEqual(len(ev), 2)


if __name__ == '__main__':
    unittest.main()
