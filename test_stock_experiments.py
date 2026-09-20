import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
import threading
import pandas as pd

from stock_experiments import (StockExperiments, BASKET, completed_prices,
                               target_weights, session_open, PaperHold)


def stamp(s='2026-09-21T19:02:00+00:00'):
    return datetime.fromisoformat(s)


def bars(minutes=5, now=None):
    now = now or stamp()
    end = pd.Timestamp(now - timedelta(minutes=2)).floor(f'{minutes}min')
    starts = pd.date_range(end=end - pd.Timedelta(minutes=minutes), periods=21, freq=f'{minutes}min')
    return pd.DataFrame({s: [100 + i for i in range(21)] for s in BASKET}, index=starts)


class StockExperimentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.now = stamp()
        self.requests = []
        def fetch(minutes):
            self.requests.append(minutes)
            return bars(minutes, self.now)
        self.lab = StockExperiments(self.temp.name, fetcher=fetch, clock=lambda: self.now)

    def tearDown(self):
        if self.lab.worker:
            self.lab.worker.join(5)
        self.temp.cleanup()

    def test_capital_accounts_and_references(self):
        result = self.lab.status()
        self.assertEqual(result['capital'], 150000)
        self.assertEqual(result['reference_capital'], 50000)
        self.assertEqual(len(result['accounts']), 8)
        self.assertEqual(sum(r['is_reference'] for r in result['accounts']), 2)
        self.assertTrue(all(r['equity'] == 25000 for r in result['accounts']))

    def test_two_shared_fetches_and_no_first_bar_fills(self):
        result = self.lab.run_cycle()
        self.assertEqual(self.requests, [5, 15])
        self.assertTrue(all(r['fill_count'] == 0 and r['observation_count'] == 1 for r in result['accounts']))
        self.assertTrue(all(r['matched_record'] for r in result['accounts']))

    def test_later_completed_bar_fills_and_costs(self):
        self.lab.run_cycle()
        self.now += timedelta(minutes=5)
        result = self.lab.run_cycle()
        state = next(r for r in result['accounts'] if r['id'] == 'buy_hold_5m')
        self.assertEqual(state['fill_count'], 6)
        self.assertLess(state['equity'], 25000)
        record = self.lab.record('buy_hold_5m')
        for trade in record['trades']:
            self.assertGreater(trade['fill_bar_end'], trade['signal_observed_at'])
            self.assertGreater(trade['cost'], 0)

    def test_persistent_throttle(self):
        self.lab.run_cycle()
        self.lab.run_cycle()
        fresh = StockExperiments(self.temp.name, fetcher=self.lab.fetcher, clock=lambda: self.now)
        fresh.run_cycle()
        self.assertEqual(self.requests, [5, 15])

    def test_same_bar_never_recorded_twice(self):
        self.lab.run_cycle()
        self.now += timedelta(seconds=61)
        result = self.lab.run_cycle()
        self.assertTrue(all(r['observation_count'] == 1 for r in result['accounts']))

    def test_closed_session_fetches_nothing(self):
        self.now = stamp('2026-09-20T19:02:00+00:00')
        self.lab.run_cycle()
        self.assertEqual(self.requests, [])

    def test_pause_fetches_nothing(self):
        self.lab.pause(True)
        self.lab.run_cycle()
        self.assertEqual(self.requests, [])
        self.assertFalse(self.lab.request_cycle())

    def test_global_pause_after_network_prevents_writes(self):
        stopped = [False]
        def fetch(minutes):
            stopped[0] = True
            return bars(minutes, self.now)
        self.lab.fetcher = fetch
        result = self.lab.run_cycle(lambda: stopped[0])
        self.assertTrue(all(r['observation_count'] == 0 for r in result['accounts']))

    def test_nonblocking_worker(self):
        event = threading.Event()
        self.lab.fetcher = lambda m: (event.wait(3), bars(m, self.now))[1]
        self.assertTrue(self.lab.request_cycle())
        self.assertTrue(self.lab.status()['busy'])
        self.assertFalse(self.lab.request_cycle())
        self.lab.pause(True)
        event.set()
        self.lab.worker.join(5)
        self.assertTrue(all(r['observation_count'] == 0 for r in self.lab.status()['accounts']))

    def test_pending_expires_after_gap_not_replayed(self):
        self.lab.run_cycle()
        self.now += timedelta(minutes=20)
        result = self.lab.run_cycle()
        state = next(r for r in result['accounts'] if r['id'] == 'buy_hold_5m')
        self.assertEqual(state['fill_count'], 0)
        self.assertEqual(state['observation_count'], 2)
        self.assertEqual(self.lab.record('buy_hold_5m')['provenance']['gap_count'], 1)

    def test_invalid_one_symbol_holds_entire_interval(self):
        self.lab.run_cycle()
        self.now += timedelta(minutes=5)
        def bad(m):
            frame = bars(m, self.now)
            frame.iloc[-1, 0] = float('nan')
            return frame
        self.lab.fetcher = bad
        result = self.lab.run_cycle()
        self.assertTrue(all(r['observation_count'] == 1 and r['fill_count'] == 0 for r in result['accounts']))

    def test_unmatched_benchmark_withholds_comparison(self):
        self.lab.run_cycle()
        with self.lab.books['trend_5m'].connection() as con:
            con.execute('DELETE FROM observations')
        state = next(r for r in self.lab.status()['accounts'] if r['id'] == 'trend_5m')
        self.assertIsNone(state['benchmark_return_pct'])
        self.assertIsNone(state['excess_return_pct'])

    def test_start_labels_converted_to_end_with_two_minute_delay(self):
        frame = completed_prices(bars(), 5, self.now)
        self.assertEqual(frame.index[-1].isoformat(), '2026-09-21T19:00:00+00:00')
        original = bars()
        extra = pd.DataFrame({s: [122] for s in BASKET}, index=[pd.Timestamp('2026-09-21T19:00:00Z')])
        result = completed_prices(pd.concat([original, extra]), 5, self.now)
        self.assertEqual(result.index[-1], frame.index[-1])

    def test_stale(self):
        with self.assertRaises(PaperHold):
            completed_prices(bars(), 5, self.now + timedelta(minutes=9))

    def test_missing_symbol(self):
        with self.assertRaises(PaperHold):
            completed_prices(bars().drop(columns=['AMD']), 5, self.now)

    def test_naive_timezone(self):
        data = bars()
        data.index = data.index.tz_localize(None)
        with self.assertRaises(PaperHold):
            completed_prices(data, 5, self.now)

    def test_missing_intraday_bar(self):
        data = bars()
        data.index = data.index.where(data.index != data.index[3], data.index[3] - pd.Timedelta(minutes=5))
        with self.assertRaises(PaperHold):
            completed_prices(data, 5, self.now)

    def test_nonpositive_price(self):
        data = bars()
        data.iloc[-1, 2] = -1
        with self.assertRaises(PaperHold):
            completed_prices(data, 5, self.now)

    def test_calendar_closed(self):
        for day in ['2026-11-26T16:00:00+00:00', '2026-11-27T18:30:00+00:00',
                    '2027-12-24T16:00:00+00:00', '2028-09-21T15:00:00+00:00']:
            self.assertFalse(session_open(stamp(day)), day)

    def test_final_regular_bar_observed_after_close(self):
        now = stamp('2026-09-21T20:02:00+00:00')
        frame = completed_prices(bars(5, now), 5, now)
        self.assertEqual(frame.index[-1], pd.Timestamp('2026-09-21T20:00:00Z'))
        self.assertFalse(session_open(stamp('2026-09-21T20:05:00+00:00')))

    def test_early_close_final_bar(self):
        now = stamp('2026-11-27T18:02:00+00:00')
        frame = completed_prices(bars(5, now), 5, now)
        self.assertEqual(frame.index[-1], pd.Timestamp('2026-11-27T18:00:00Z'))
        self.assertFalse(session_open(stamp('2026-11-27T18:05:00+00:00')))

    def test_naive_clock_rejected(self):
        with self.assertRaises(PaperHold):
            session_open(datetime(2026, 9, 21, 10))

    def test_overnight_complete_and_missing_sessions(self):
        now = stamp('2026-09-22T13:37:00+00:00')
        prior = pd.date_range(end='2026-09-21T19:55:00Z', periods=20, freq='5min')
        first = pd.DatetimeIndex([pd.Timestamp('2026-09-22T13:30:00Z')])
        frame = pd.DataFrame({s: list(range(100, 121)) for s in BASKET}, index=prior.append(first))
        completed_prices(frame, 5, now)
        missing_tail = frame.copy()
        missing_tail.index = (prior - pd.Timedelta(minutes=5)).append(first)
        with self.assertRaises(PaperHold):
            completed_prices(missing_tail, 5, now)
        skipped_session = frame.copy()
        skipped_session.index = (prior - pd.Timedelta(days=3)).append(first)
        with self.assertRaises(PaperHold):
            completed_prices(skipped_session, 5, now)

    def test_strategies_distinct_unlevered(self):
        data = bars()
        trend = target_weights(data, 'trend')
        breakout = target_weights(data, 'breakout')
        mean = target_weights(data, 'mean_reversion')
        self.assertEqual(len(trend), 6)
        self.assertEqual(len(breakout), 6)
        self.assertEqual(mean, {})
        data.iloc[-1, :] = 50
        self.assertEqual(target_weights(data, 'trend'), {})
        self.assertEqual(target_weights(data, 'breakout'), {})
        self.assertEqual(len(target_weights(data, 'mean_reversion')), 6)
        self.assertLessEqual(sum(trend.values()), 1)


if __name__ == '__main__':
    unittest.main()
