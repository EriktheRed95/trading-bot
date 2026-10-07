"""Independent closed-day and stock collection-cutoff regressions. Pure synthetic rows."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'scripts'))
import coverage_report as cov
from stock_experiments import session_open

UTC = timezone.utc

def z(month, day, hour=0, minute=0, second=0):
    return datetime(2026, month, day, hour, minute, second, tzinfo=UTC)

def pair(expected):
    accounts = [{'id': 'strategy', 'pair': 'synthetic'}, {'id': 'reference', 'pair': 'synthetic'}]
    data = {account['id']: {'initial': 1000., 'fills': [], 'observations':
            [{'asof': label, 'observed_at': ready, 'equity': 1000., 'cash': 1000.} for label, ready in expected]}
            for account in accounts}
    return accounts, data

class ClosedUtcDay(unittest.TestCase):
    def test_full_labels_remain_partial_before_midnight_and_complete_at_or_after_midnight(self):
        start, midnight = z(9, 21), z(9, 22)
        for kind, count in [('hourly_crypto', 24), ('active', 96)]:
            for delta, eligible in [(timedelta(seconds=-1), 0), (timedelta(0), 1), (timedelta(seconds=1), 1)]:
                with self.subTest(kind=kind, delta=delta):
                    until = midnight + delta
                    expected = cov.expected_bars(kind, start, until)
                    self.assertEqual(len(expected), count)
                    accounts, data = pair(expected)
                    family = cov.family_coverage(kind, accounts, data, expected, [], [], start, until)
                    day = family['sessions'][0]
                    self.assertEqual((day['expected'], day['recorded'], day['full_session_bars']), (count, count, count))
                    self.assertTrue(day['covered_so_far_full'])
                    self.assertEqual(day['full'], bool(eligible))
                    self.assertEqual(day['complete'], bool(eligible))
                    self.assertEqual(day['still_open_at_until'], not eligible)
                    self.assertEqual(day['clipped_by_window_end'], not eligible)
                    self.assertEqual(day['status'], 'complete' if eligible else 'end_clipped')
                    comparison = cov.compare(kind, *accounts, data, expected, start, until)
                    self.assertEqual(comparison['have']['sessions'], eligible)
                    self.assertEqual(comparison['observed_sessions'], 1)

    def test_last_hour_reproducer_does_not_qualify_either_crypto_family(self):
        for kind, until in [('hourly_crypto', z(9, 21, 23, 30)), ('active', z(9, 21, 23, 56))]:
            with self.subTest(kind=kind):
                start = z(9, 21)
                expected = cov.expected_bars(kind, start, until)
                accounts, data = pair(expected)
                result = cov.compare(kind, *accounts, data, expected, start, until)
                self.assertEqual(result['complete_matched_sessions'], 0)
                self.assertEqual(result['partial_matched_sessions'], 1)

    def test_twentieth_observed_day_does_not_pass_default_gate_before_its_midnight(self):
        start, until = z(9, 1), z(9, 20, 23, 30)
        expected = cov.expected_bars('hourly_crypto', start, until)
        accounts, data = pair(expected)
        data['strategy']['fills'] = [{'asof': label, 'ticker': 'SYNTHETIC', 'shares': 1, 'price': 1, 'cost': 0}
                                     for label, _ in expected[:10]]
        result = cov.compare('hourly_crypto', *accounts, data, expected, start, until)
        self.assertEqual((result['observed_sessions'], result['have']['sessions']), (20, 19))
        self.assertEqual(result['have']['fills'], 10)
        self.assertGreaterEqual(result['have']['intervals'], 100)
        self.assertEqual(result['verdict'], 'insufficient_sample')
        self.assertEqual(len(result['insufficient_because']), 1)
        self.assertIn('sessions 19/20', result['insufficient_because'][0])

    def test_start_clipped_final_hour_day_stays_clipped_at_both_ends(self):
        start, until = z(9, 21, 12), z(9, 21, 23, 30)
        expected = cov.expected_bars('hourly_crypto', start, until)
        day = cov.session_table('hourly_crypto', expected, start, until)['2026-09-21']
        self.assertEqual(day['status'], 'start_and_end_clipped')
        self.assertTrue(day['still_open_at_until'])

class StockClosingWindow(unittest.TestCase):
    def test_regular_and_early_close_windows_stop_at_collector_cutoff_for_both_cadences(self):
        # September close 16:00 ET =20:00Z; Thanksgiving Friday close 13:00 ET =18:00Z.
        for close in [z(9, 21, 20), z(11, 27, 18)]:
            for kind in ['stocks_5m', 'stocks_15m']:
                with self.subTest(close=close, kind=kind):
                    ready, cutoff, until = close + timedelta(minutes=2), close + timedelta(minutes=5), close + timedelta(minutes=30)
                    label = close.isoformat()
                    windows = cov.missing_windows(kind, [label], {label: ready}, [label], until)
                    self.assertEqual(windows, [(ready, cutoff)])
                    self.assertTrue(session_open(cutoff - timedelta(seconds=1)))
                    self.assertFalse(session_open(cutoff))
                    self.assertEqual(cov.missing_windows(kind, [label], {label: ready}, [label], close + timedelta(minutes=4)),
                                     [(ready, close + timedelta(minutes=4))])

    def test_failed_closing_check_is_not_blameable_on_silence_after_collection_stops(self):
        for close in [z(9, 21, 20), z(11, 27, 18)]:
            for kind in ['stocks_5m', 'stocks_15m']:
                with self.subTest(close=close, kind=kind):
                    ready, cutoff, until = close + timedelta(minutes=2), close + timedelta(minutes=5), close + timedelta(minutes=30)
                    def check(when, family='hourly', outcome='no_change'):
                        return {'started': when, 'finished': when, 'family': family, 'outcome': outcome, 'message': 'Synthetic provider data rejected'}
                    start = close - timedelta(minutes=30)
                    rows = [check(start + timedelta(minutes=n)) for n in range(0, 36, 5)]
                    failed = check(close + timedelta(minutes=3), kind, 'held')
                    rows.append(failed)
                    silent = cov.silent_periods(rows, start, until)
                    self.assertEqual(silent[0]['from'], cutoff)
                    windows = cov.missing_windows(kind, [close.isoformat()], {close.isoformat(): ready}, [close.isoformat()], until)
                    gap = cov.attribute_gap([failed], silent, windows)
                    self.assertEqual(gap['primary'], 'provider_or_data_failure')
                    self.assertEqual(gap['bar_causes'], {'provider_or_data_failure': 1})
                    self.assertEqual(gap['failed_checks'], {'held': 1})
                    self.assertEqual(gap['silent_overlap_minutes'], 0)
                    self.assertEqual(gap['silent_periods'], [])

    def test_a_check_exactly_at_closing_cutoff_is_not_a_missing_bar_cause(self):
        close, until = z(9, 21, 20), z(9, 22, 21)
        cutoff = close + timedelta(minutes=5)
        windows = cov.missing_windows('stocks_15m', [close.isoformat()], {close.isoformat(): close + timedelta(minutes=2)}, [close.isoformat()], until)
        attempt = {'started': cutoff, 'outcome': 'new', 'message': ''}
        result = cov.attribute_gap([attempt], [], windows, until)
        self.assertEqual(result['accepted_checks'], 0)
        self.assertEqual(result['recovery_checks'], 1)
        self.assertEqual(result['primary'], 'no_attempt_recorded')

    def test_an_interior_stock_bar_keeps_its_normal_latest_bar_window(self):
        end = z(9, 21, 17)
        for kind, minutes in [('stocks_5m', 5), ('stocks_15m', 15)]:
            ready = end + timedelta(minutes=2)
            self.assertEqual(cov.missing_windows(kind, [end.isoformat()], {end.isoformat(): ready}, [end.isoformat()], z(9, 21, 21)),
                             [(ready, ready + timedelta(minutes=minutes))])

class AbsentEvidence(unittest.TestCase):
    def test_empty_attempt_log_does_not_claim_other_family_activity_or_machine_state(self):
        start, until = z(9, 21, 19), z(9, 21, 20)
        silent = cov.silent_periods([], start, until)
        result = cov.attribute_gap([], silent, [(start + timedelta(minutes=2), start + timedelta(minutes=17))])
        self.assertEqual(result['primary'], 'no_attempt_recorded')
        self.assertIn('do not establish why', result['interpretation'])
        self.assertNotIn('Other collector activity was recorded', result['interpretation'])
        self.assertNotIn('asleep', result['interpretation'])

if __name__ == '__main__':
    unittest.main()
