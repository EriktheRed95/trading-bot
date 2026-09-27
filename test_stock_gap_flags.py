"""Cross-session gap flags in StockExperiments.record().

Regression for the live record of 2026-09-21/22: collection stopped at 12:45 ET
and resumed at 12:05 ET next day, yet record() reported gap_count == 0 because
any pair of observations on different dates was treated as "not evaluated".
"""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import pandas as pd

from stock_experiments import BASKET, StockExperiments


def frame_ending(end_utc, minutes=5, periods=21):
    """Start-labelled bars whose last bar ends at end_utc, all inside one session."""
    starts = pd.date_range(end=pd.Timestamp(end_utc) - pd.Timedelta(minutes=minutes), periods=periods,
                           freq='%dmin' % minutes)
    return pd.DataFrame({s: [100 + i for i in range(periods)] for s in BASKET}, index=starts)


def overnight_frame(prior_close_utc, first_start_utc, minutes=5):
    prior = pd.date_range(end=pd.Timestamp(prior_close_utc) - pd.Timedelta(minutes=minutes), periods=20,
                          freq='%dmin' % minutes)
    index = prior.append(pd.DatetimeIndex([pd.Timestamp(first_start_utc)]))
    return pd.DataFrame({s: list(range(100, 121)) for s in BASKET}, index=index)


class CrossSessionGapFlags(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.frames = []
        self.now = None
        self.lab = StockExperiments(self.temp.name, fetcher=lambda m: self.frames[-1], clock=lambda: self.now)

    def tearDown(self):
        self.temp.cleanup()

    def observe(self, now, frame):
        self.now = datetime.fromisoformat(now)
        self.frames.append(frame)
        self.lab.run_cycle()

    def test_partial_sessions_are_flagged_across_dates(self):
        # Day 1: last observed bar ends 12:45 ET; day 2: first observed bar ends 12:05 ET.
        self.observe('2026-09-21T16:47:00+00:00', frame_ending('2026-09-21T16:45:00Z'))
        self.observe('2026-09-22T16:07:00+00:00', frame_ending('2026-09-22T16:05:00Z'))
        record = self.lab.record('buy_hold_5m')
        self.assertEqual(record['provenance']['observation_count'], 2)
        self.assertTrue(record['observations'][1]['unobserved_bars_possible'])
        self.assertEqual(record['provenance']['gap_count'], 1)

    def test_complete_overnight_break_not_flagged(self):
        # Day 1 final bar ends 16:00 ET; day 2 first bar ends 09:35 ET: nothing missing.
        self.observe('2026-09-21T20:02:00+00:00', frame_ending('2026-09-21T20:00:00Z'))
        self.observe('2026-09-22T13:37:00+00:00', overnight_frame('2026-09-21T20:00:00Z', '2026-09-22T13:30:00Z'))
        record = self.lab.record('buy_hold_5m')
        self.assertEqual(record['provenance']['observation_count'], 2)
        self.assertFalse(record['observations'][1]['unobserved_bars_possible'])
        self.assertEqual(record['provenance']['gap_count'], 0)

    def test_skipped_whole_session_flagged(self):
        # Close of Monday observed, Tuesday never observed, Wednesday observed from 11:20 ET.
        self.observe('2026-09-21T20:02:00+00:00', frame_ending('2026-09-21T20:00:00Z'))
        self.observe('2026-09-23T15:22:00+00:00', frame_ending('2026-09-23T15:20:00Z'))
        record = self.lab.record('buy_hold_5m')
        self.assertEqual(record['provenance']['observation_count'], 2)
        self.assertEqual(record['provenance']['gap_count'], 1)

    def test_weekend_break_complete(self):
        # Friday close to Monday first bar is a complete break.
        self.observe('2026-09-18T20:02:00+00:00', frame_ending('2026-09-18T20:00:00Z'))
        self.observe('2026-09-21T13:37:00+00:00', overnight_frame('2026-09-18T20:00:00Z', '2026-09-21T13:30:00Z'))
        record = self.lab.record('buy_hold_5m')
        self.assertEqual(record['provenance']['observation_count'], 2)
        self.assertEqual(record['provenance']['gap_count'], 0)


if __name__ == '__main__':
    unittest.main()
