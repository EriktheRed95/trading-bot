"""Session completeness, the minimum-session gate, gap attribution and shadow-book counts.

Synthetic books in a temporary runtime only: no live runtime, no network, no real account.
Each case reproduces a defect found in the September 28 - October 6 diagnostic review; the
numbers in the comments are worked by hand, not read back from the code.
"""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_coverage_report import Runtime, START, UNTIL, bars_15m, stock_obs

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'scripts'))
import collection_report as base   # noqa: E402
import coverage_report as cov      # noqa: E402

UTC = timezone.utc
TREND_15 = 'stock-experiments-v1/stock-lab-v1-trend_15m.sqlite'
HOLD_15 = 'stock-experiments-v1/stock-lab-v1-buy_hold_15m.sqlite'
CRYPTO_BOOKS = {
    'hourly_crypto': ('hourly-v1/BTC-USD__trend.sqlite3', 'hourly-v1/BTC-USD__buy-hold.sqlite3'),
    'active': ('active-15m-v1/active.sqlite3', 'active-15m-v1/reference.sqlite3'),
}


def z(*parts):
    return datetime(*parts, tzinfo=UTC)


def observed(labels, delay=2, equity=None, skip=()):
    return [(l, cov.utc(l) + timedelta(minutes=delay), (equity(i) if equity else 1000.0), (equity(i) if equity else 1000.0))
            for i, l in enumerate(labels) if l not in skip]


class Fixture(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.rt = Runtime(self.dir.name)

    def labels(self, kind, since, until):
        return [l for l, _ in cov.expected_bars(kind, since, until)]

    def row(self, report, kind, day):
        return next(s for s in report['families'][kind]['sessions'] if s['session'] == day)


class SessionCompleteness(Fixture):
    """A day or session is complete only if the window holds every bar of it."""

    def crypto_report(self, kind, since, until, skip=()):
        labels = self.labels(kind, since, until)
        for rel in CRYPTO_BOOKS[kind]:
            self.rt.book(rel, observed(labels, skip=skip))
        self.rt.heartbeat(begin=since, end=until)
        return cov.build(self.rt.root, since=since, until=until, now=until), labels

    def test_open_utc_day_is_not_a_complete_session(self):
        # 2026-09-21 holds 24 hourly / 96 fifteen-minute bars. 2026-09-22 is open at 06:30Z:
        # hourly bars 00:00..06:00 (7) and 15-minute bars 00:00..06:15 (26) are due so far.
        since, until = z(2026, 9, 21, 0, 0), z(2026, 9, 22, 6, 30)
        for kind, complete_bars, open_bars in (('hourly_crypto', 24, 7), ('active', 96, 26)):
            with self.subTest(kind=kind):
                self.setUp()
                report, labels = self.crypto_report(kind, since, until)
                fam = report['families'][kind]
                self.assertEqual(len(labels), complete_bars + open_bars)
                self.assertEqual((fam['recorded_bars'], fam['coverage_pct']), (complete_bars + open_bars, 100.0))   # coverage so far
                done, today = self.row(report, kind, '2026-09-21'), self.row(report, kind, '2026-09-22')
                self.assertTrue(done['full'] and done['complete'])
                self.assertEqual(done['status'], 'complete')
                self.assertEqual((today['expected'], today['recorded'], today['full_session_bars']), (open_bars, open_bars, complete_bars))
                self.assertFalse(today['full'])
                self.assertFalse(today['complete'])
                self.assertTrue(today['covered_so_far_full'])
                self.assertTrue(today['clipped_by_window_end'] and today['still_open_at_until'])
                self.assertEqual(today['status'], 'end_clipped')
                self.assertEqual((fam['full_sessions'], fam['complete_sessions'], fam['partial_sessions']), (1, 1, 1))

    def test_start_clipped_crypto_day_is_partial_even_when_every_remaining_bar_is_recorded(self):
        since, until = z(2026, 9, 21, 12, 0), z(2026, 9, 22, 0, 30)
        report, _ = self.crypto_report('hourly_crypto', since, until)
        first = self.row(report, 'hourly_crypto', '2026-09-21')
        self.assertEqual((first['expected'], first['recorded'], first['full_session_bars']), (12, 12, 24))   # 12:00..23:00
        self.assertEqual((first['status'], first['full'], first['clipped_by_window_start']), ('start_clipped', False, True))
        self.assertEqual(report['families']['hourly_crypto']['full_sessions'], 0)

    def test_missing_bar_keeps_a_closed_day_complete_but_not_full(self):
        since, until = z(2026, 9, 21, 0, 0), z(2026, 9, 22, 0, 30)
        labels = self.labels('hourly_crypto', since, until)
        report, _ = self.crypto_report('hourly_crypto', since, until, skip={labels[5]})
        day = self.row(report, 'hourly_crypto', '2026-09-21')
        self.assertEqual((day['recorded'], day['missing'], day['complete'], day['full']), (23, 1, True, False))
        fam = report['families']['hourly_crypto']
        self.assertEqual(self.row(report, 'hourly_crypto', '2026-09-22')['status'], 'end_clipped')   # only its 00:00 bar is due
        self.assertEqual((fam['complete_sessions'], fam['full_sessions'], fam['partial_sessions']), (1, 0, 1))

    def stock_report(self, since, until, kind='stocks_15m'):
        labels = self.labels(kind, since, until)
        self.rt.book(TREND_15, stock_obs(labels))
        self.rt.book(HOLD_15, stock_obs(labels))
        return cov.build(self.rt.root, since=since, until=until, now=until), labels

    def test_regular_session_complete_from_the_calendar(self):
        report, labels = self.stock_report(z(2026, 9, 21, 13, 0), z(2026, 9, 21, 20, 30))
        self.assertEqual(len(labels), 26)                                   # 09:45..16:00 ET
        day = self.row(report, 'stocks_15m', '2026-09-21')
        self.assertEqual((day['status'], day['full'], day['full_session_bars']), ('complete', True, 26))
        self.assertEqual(report['families']['stocks_15m']['full_sessions'], 1)

    def test_holiday_is_not_a_missing_session_and_early_close_has_fewer_bars(self):
        # Labor Day 2026-09-07 has no session. Friday 09-04 and Tuesday 09-08 are both complete.
        report, labels = self.stock_report(z(2026, 9, 4, 13, 0), z(2026, 9, 9, 3, 0))
        fam = report['families']['stocks_15m']
        self.assertEqual([s['session'] for s in fam['sessions']], ['2026-09-04', '2026-09-08'])
        self.assertEqual((len(labels), fam['complete_sessions'], fam['full_sessions']), (52, 2, 2))
        # The day after Thanksgiving closes at 13:00 ET: bar ends 09:45, 10:00, ..., 13:00 are 13 intervals, 14 bars.
        self.setUp()
        report, labels = self.stock_report(z(2026, 11, 27, 13, 0), z(2026, 11, 28, 3, 0))
        day = self.row(report, 'stocks_15m', '2026-11-27')
        self.assertEqual((len(labels), day['full_session_bars'], day['status'], day['full']), (14, 14, 'complete', True))

    def test_stock_session_clipped_at_either_end_is_reported_as_such(self):
        cases = ((z(2026, 9, 21, 14, 0), z(2026, 9, 21, 20, 30), 'start_clipped', 25),
                 (z(2026, 9, 21, 13, 0), z(2026, 9, 21, 17, 0), 'end_clipped', 13),         # bars due by 17:00Z: 09:45..12:45 ET
                 (z(2026, 9, 21, 14, 0), z(2026, 9, 21, 17, 0), 'start_and_end_clipped', 12))
        for since, until, status, bars in cases:
            with self.subTest(status=status):
                self.setUp()
                report, _ = self.stock_report(since, until)
                day = self.row(report, 'stocks_15m', '2026-09-21')
                self.assertEqual((day['status'], day['expected'], day['recorded'], day['full_session_bars']), (status, bars, bars, 26))
                self.assertFalse(day['full'])
                self.assertTrue(day['covered_so_far_full'])
                self.assertEqual(report['families']['stocks_15m']['full_sessions'], 0)

    def test_render_separates_complete_from_partial_sessions(self):
        report, _ = self.stock_report(z(2026, 9, 21, 14, 0), z(2026, 9, 21, 20, 30))
        text = cov.render(report)
        self.assertIn('0 complete', text)
        self.assertIn('1 partial', text)


class SessionGate(Fixture):
    """The minimum-session gate counts complete sessions fully matched by both accounts."""

    SINCE, UNTIL = z(2026, 9, 21, 0, 0), z(2026, 9, 23, 6, 30)       # 24 + 24 + 7 hourly crypto bars

    def pair(self, strategy_skip=(), reference_skip=(), fills=()):
        labels = self.labels('hourly_crypto', self.SINCE, self.UNTIL)
        self.assertEqual(len(labels), 55)
        strategy, reference = CRYPTO_BOOKS['hourly_crypto']
        self.rt.book(strategy, observed(labels, equity=lambda i: 1000.0 + i, skip=[labels[k] for k in strategy_skip]),
                     [(labels[k], 'BTC-USD', 0.01, 100.0, 0.5) for k in fills])
        self.rt.book(reference, observed(labels, skip=[labels[k] for k in reference_skip]))
        self.rt.heartbeat(begin=self.SINCE, end=self.UNTIL)
        results = cov.build(self.rt.root, since=self.SINCE, until=self.UNTIL, now=self.UNTIL)['comparisons']
        return next(c for c in results if c['strategy'].endswith('trend'))

    def test_day_with_a_single_matched_bar_no_longer_counts(self):
        # Strategy misses index 34 (09-22 10:00). 09-21 is complete and fully matched; 09-22 is
        # complete but one bar short; 09-23 is open (7 bars, all matched).
        result = self.pair(strategy_skip=[34])
        self.assertEqual((result['matched_bars'], result['segments'], result['return_intervals']), (54, 2, 52))
        self.assertEqual(result['observed_sessions'], 3)
        self.assertEqual(result['sessions'], 3)                           # legacy key keeps its meaning: observed days
        self.assertEqual((result['complete_matched_sessions'], result['partial_matched_sessions']), (1, 2))
        self.assertEqual(result['eligible_session_dates'], ['2026-09-21'])
        self.assertEqual(result['have']['sessions'], 1)
        self.assertEqual(result['have']['observed_sessions'], 3)
        self.assertEqual(result['sessions_basis'], 'complete_closed_sessions_matched_by_strategy_and_reference')

    def test_both_accounts_must_hold_every_bar(self):
        # The reference, not the strategy, misses index 5 (09-21 05:00): only 09-22 qualifies.
        result = self.pair(reference_skip=[5])
        self.assertEqual(result['eligible_session_dates'], ['2026-09-22'])
        self.assertEqual(result['strategy_only_bars'], 1)

    def test_gate_uses_complete_sessions_and_gives_explicit_reasons(self):
        with patch.object(cov, 'MIN_SESSIONS', 2), patch.object(cov, 'MIN_FILLS', 0), \
                patch.dict(cov.KINDS['hourly_crypto'], {'min_intervals': 10}):
            short = self.pair(strategy_skip=[34])
        self.assertEqual(short['verdict'], 'insufficient_sample')
        self.assertEqual(len(short['insufficient_because']), 1)
        reason = short['insufficient_because'][0]
        self.assertTrue(reason.startswith('sessions 1/2'), reason)
        self.assertIn('complete fully matched', reason)
        self.assertIn('3 observed', reason)
        self.setUp()
        with patch.object(cov, 'MIN_SESSIONS', 1), patch.object(cov, 'MIN_FILLS', 0), \
                patch.dict(cov.KINDS['hourly_crypto'], {'min_intervals': 10}):
            met = self.pair(strategy_skip=[34])
        self.assertEqual(met['verdict'], 'sample_thresholds_met_descriptive_only')
        self.assertEqual(met['insufficient_because'], [])

    def test_descriptive_returns_are_unchanged_and_still_descriptive(self):
        # Runs [0..33] and [35..54]: (1033/1000) * (1054/1035) - 1 = 5.196 %; the reference is flat.
        result = self.pair(strategy_skip=[34])
        self.assertAlmostEqual(result['descriptive_strategy_return_pct'], 5.196, places=2)
        self.assertAlmostEqual(result['descriptive_reference_return_pct'], 0.0, places=3)
        self.assertEqual(result['return_basis'], 'matched_contiguous_runs_only')

    def test_fill_gate_stays_whole_window(self):
        # Two fills: one inside a matched run, one on a bar of the partial day. Both count.
        result = self.pair(strategy_skip=[34], fills=[2, 40])
        self.assertEqual(result['have']['fills'], 2)
        self.assertTrue(result['fills_basis'].startswith('whole_window'))
        self.assertTrue(result['gate_basis']['fills'].startswith('whole_window'))
        self.assertEqual(result['gate_basis']['intervals'], 'matched_contiguous_runs')
        self.assertEqual(result['gate_basis']['sessions'], result['sessions_basis'])

    def test_stock_partial_first_session_is_not_eligible(self):
        # START (10:00 ET) clips the only session: it is observed and fully matched but not complete.
        labels = bars_15m()
        self.rt.book(TREND_15, stock_obs(labels))
        self.rt.book(HOLD_15, stock_obs(labels))
        self.rt.heartbeat()
        result = cov.build(self.rt.root, since=START, until=UNTIL, now=UNTIL)['comparisons'][0]
        self.assertEqual((result['observed_sessions'], result['complete_matched_sessions'], result['partial_matched_sessions']), (1, 0, 1))
        self.assertIn('sessions 0/20', result['insufficient_because'][0])

    def test_complete_stock_session_with_both_accounts_is_eligible(self):
        since, until = z(2026, 9, 21, 13, 0), z(2026, 9, 21, 20, 30)
        labels = self.labels('stocks_15m', since, until)
        self.rt.book(TREND_15, stock_obs(labels))
        self.rt.book(HOLD_15, stock_obs(labels))
        result = cov.build(self.rt.root, since=since, until=until, now=until)['comparisons'][0]
        self.assertEqual((result['complete_matched_sessions'], result['eligible_session_dates']), (1, ['2026-09-21']))

    def test_render_labels_observed_and_complete_sessions(self):
        self.pair(strategy_skip=[34])
        text = cov.render(cov.build(self.rt.root, since=self.SINCE, until=self.UNTIL, now=self.UNTIL))
        self.assertIn('complete matched sessions 1', text)
        self.assertIn('observed 3', text)


class GapAttribution(Fixture):
    """Causes come from the missing bars' own readiness windows, not from the recovery interval."""

    def setup_gap(self, missing, recovery_seen=None):
        labels = bars_15m()
        obs = stock_obs(labels, seen_after=2)
        for label in missing:
            obs = [o for o in obs if o[0] != label]
        if recovery_seen is not None:
            index = next(i for i, o in enumerate(obs) if o[0] == labels[labels.index(missing[-1]) + 1])
            obs[index] = (obs[index][0], recovery_seen, 25000.0, 25000.0)
        self.rt.book(TREND_15, obs)
        self.rt.book(HOLD_15, obs)
        return labels

    def gaps(self):
        return cov.build(self.rt.root, since=START, until=UNTIL, now=UNTIL)

    def test_recovery_interval_overlap_is_not_collector_silence(self):
        # 15m bars 15:00..15:45Z are missing; every check in their windows (15:02..16:02Z) was held.
        # The hourly family goes quiet at 16:05Z, after the missing bars' windows, and the stock
        # family's recovery check at 16:08Z records the 16:00Z bar.
        labels = self.setup_gap(bars_15m()[4:8], recovery_seen=START + timedelta(minutes=128))
        for minutes in (63, 78, 93, 108):                                 # 15:03, 15:18, 15:33, 15:48Z
            self.rt.attempt('stocks_15m', START + timedelta(minutes=minutes), 'held', 'Held: PaperHold: Provider returned no bars')
        self.rt.attempt('stocks_15m', START + timedelta(minutes=128), 'new')                      # recovery 16:08Z
        self.rt.heartbeat(skip=[(START + timedelta(minutes=126), START + timedelta(minutes=170))])   # quiet 16:06..16:50Z
        report = self.gaps()
        gap = report['families']['stocks_15m']['gaps'][0]
        self.assertEqual(gap['from_bar'], labels[4])
        self.assertEqual(gap['missing_window_to'], '2026-09-21T16:02:00+00:00')
        self.assertEqual(gap['primary'], 'provider_or_data_failure')
        self.assertEqual(gap['causes'], ['provider_or_data_failure'])
        self.assertEqual((gap['failed_checks'], gap['accepted_checks']), ({'held': 4}, 0))
        self.assertEqual(gap['recovery_checks'], 1)
        self.assertEqual(gap['silent_periods'], [])
        self.assertEqual(gap['bar_causes'], {'provider_or_data_failure': 4})

    def test_recovery_check_alone_is_not_checks_accepted_without_a_bar(self):
        labels = bars_15m()
        self.setup_gap(labels[10:11], recovery_seen=cov.utc(labels[11]) + timedelta(minutes=4))
        self.rt.heartbeat()
        self.rt.attempt('stocks_15m', cov.utc(labels[11]) + timedelta(minutes=3), 'new')           # recovery after the window
        gap = self.gaps()['families']['stocks_15m']['gaps'][0]
        self.assertEqual(gap['primary'], 'no_attempt_recorded')
        self.assertEqual((gap['accepted_checks'], gap['recovery_checks']), (0, 1))

    def test_accepted_check_inside_the_window_still_counts(self):
        labels = self.setup_gap(bars_15m()[2:3])
        self.rt.heartbeat()
        self.rt.attempt('stocks_15m', cov.utc(labels[2]) + timedelta(minutes=3), 'no_change')
        gap = self.gaps()['families']['stocks_15m']['gaps'][0]
        self.assertEqual((gap['primary'], gap['accepted_checks']), ('checks_accepted_without_bar', 1))

    def test_mixed_evidence_keeps_both_causes_visible(self):
        # 16:30, 16:45, 17:00Z missing. Quiet 16:31..17:04Z covers the first two windows entirely and
        # 3 of 15 minutes of the third, where a held check at 17:10Z falls.
        labels = self.setup_gap(bars_15m()[10:13])
        self.rt.heartbeat(skip=[(START + timedelta(minutes=151), START + timedelta(minutes=183))])
        self.rt.attempt('stocks_15m', START + timedelta(minutes=190), 'held', 'Held: PaperHold: Provider returned no bars')
        self.rt.attempt('stocks_15m', START + timedelta(minutes=197), 'new')
        gap = self.gaps()['families']['stocks_15m']['gaps'][0]
        self.assertEqual(gap['from_bar'], labels[10])
        self.assertEqual(gap['bar_causes'], {'collector_silent': 2, 'provider_or_data_failure': 1})
        self.assertEqual(gap['primary'], 'collector_silent')
        self.assertEqual(gap['causes'], ['collector_silent', 'provider_or_data_failure'])
        self.assertEqual(gap['failed_checks'], {'held': 1})
        self.assertEqual(len(gap['silent_periods']), 1)

    def test_silence_does_not_claim_a_proven_machine_state(self):
        labels = self.setup_gap(bars_15m()[8:12])
        self.rt.heartbeat(skip=[(cov.utc(labels[8]) - timedelta(minutes=4) + timedelta(seconds=1),
                                 cov.utc(labels[12]) + timedelta(minutes=2) - timedelta(seconds=1))])
        report = self.gaps()
        cause = report['process']['silent_periods'][0]['cause']
        self.assertIn('asleep or off', cause)
        self.assertIn('suspended or stuck', cause)
        self.assertIn('not distinguishable', cause)
        gap = report['families']['stocks_15m']['gaps'][0]
        self.assertEqual(gap['primary'], 'collector_silent')
        self.assertIn('cannot be told apart', gap['interpretation'])

    def test_other_family_attempts_break_an_hourly_only_silence(self):
        self.rt.book(TREND_15, stock_obs(bars_15m()))
        quiet = (START + timedelta(minutes=61), START + timedelta(minutes=120))
        self.rt.heartbeat(skip=[quiet])
        t = quiet[0]
        while t <= quiet[1]:                                              # another family kept checking
            self.rt.attempt('active', t, 'new')
            t += timedelta(minutes=5)
        self.assertEqual(self.gaps()['process']['silent_periods'], [])

    def test_genuine_silence_is_still_reported_when_no_family_checked(self):
        self.rt.book(TREND_15, stock_obs(bars_15m()))
        self.rt.heartbeat(skip=[(START + timedelta(minutes=61), START + timedelta(minutes=120))])
        periods = self.gaps()['process']['silent_periods']
        self.assertEqual(len(periods), 1)
        self.assertGreater(periods[0]['minutes'], 55)


class ShadowBooksInBaselineReport(Fixture):
    def setUp(self):
        super().setUp()
        seen = z(2026, 9, 22, 21, 0)
        self.rt.book('paper.sqlite3', [('2026-09-21', seen, 1000.0, 0.0), ('2026-09-22', seen, 1001.0, 0.0)])
        for name in ('SPY', 'QQQ'):
            self.rt.book(f'hourly-v1/core-benchmark-{name}.sqlite3', [('2026-09-21', seen, 1000.0, 0.0), ('2026-09-22', seen, 1002.0, 0.0)])
        for name in ('baseline', 'filtered'):
            self.rt.book(f'research-v1/shadow-{name}.sqlite3', [('2026-09-21', seen, 1000.0, 0.0), ('2026-09-22', seen, 999.0, 0.0)])
        self.since = z(2026, 9, 22, 0, 0)

    def report(self, **kw):
        return base.build_report(self.rt.root, self.since, **kw)

    def test_research_shadow_books_are_counted_in_their_own_family(self):
        families = self.report()['families']
        self.assertEqual(families['core research shadows']['accounts'], 2)
        self.assertEqual(families['core research shadows']['observations'], 4)
        self.assertEqual(families['core research shadows']['observed_after_since'], 4)
        self.assertEqual((families['core']['accounts'], families['core']['observations']), (1, 2))          # existing keys keep their meaning
        self.assertEqual((families['core benchmarks']['accounts'], families['core benchmarks']['observations']), (2, 4))

    def test_totals_separate_account_observations_from_unique_bars(self):
        report = self.report()
        totals = report['totals']
        self.assertEqual(totals['accounts'], 5)
        self.assertEqual(totals['account_observations'], 2 + 4 + 4)
        self.assertEqual(totals['account_observations_after_since'], 5 * 2)        # every row was observed 2026-09-22T21:00Z
        # Two distinct daily bars exist in every family; unique bars never multiply by account count.
        self.assertEqual(report['families']['core research shadows']['unique_bars'], 2)
        self.assertEqual(report['families']['core benchmarks']['unique_bars'], 2)
        self.assertEqual(totals['unique_family_bars'], 6)                           # core 2 + benchmarks 2 + shadows 2
        self.assertEqual(totals['unique_family_bars_after_since'], 6)
        self.assertIn('not unique bars', totals['note'])

    def test_unique_bars_after_since_only_counts_labels_seen_after_since(self):
        self.rt.book('research-v1/shadow-old.sqlite3', [('2026-09-18', z(2026, 9, 19, 21, 0), 1000.0, 0.0)])
        shadows = self.report()['families']['core research shadows']
        self.assertEqual((shadows['accounts'], shadows['observations'], shadows['observed_after_since']), (3, 5, 4))
        self.assertEqual((shadows['unique_bars'], shadows['unique_bars_after_since']), (3, 2))

    def test_a_book_matched_by_two_families_is_counted_once(self):
        families = dict(base.FAMILIES, **{'duplicate shadows': ['research-v1/shadow-*.sqlite3']})
        with patch.object(base, 'FAMILIES', families):
            report = self.report()
        self.assertEqual(report['totals']['accounts'], 5)
        self.assertEqual(report['totals']['account_observations'], 10)
        self.assertEqual(report['families']['duplicate shadows']['accounts'], 0)
        self.assertEqual(report['totals']['duplicate_books_skipped'], 2)

    def test_existing_family_report_signature_and_keys_still_work(self):
        result = base.family_report(self.rt.root, base.FAMILIES['core'], self.since)
        for key in ('accounts', 'unreadable_accounts', 'observations', 'observed_after_since', 'latest_bar', 'latest_observed_at'):
            self.assertIn(key, result)

    def test_cli_json_includes_shadows_and_totals(self):
        import subprocess
        out = subprocess.run([sys.executable, '-B', str(ROOT / 'scripts' / 'collection_report.py'), '--runtime', str(self.rt.root),
                              '--since', self.since.isoformat(), '--json'], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        data = json.loads(out.stdout)
        self.assertIn('core research shadows', data['families'])
        self.assertEqual(data['totals']['account_observations'], 10)
        text = subprocess.run([sys.executable, '-B', str(ROOT / 'scripts' / 'collection_report.py'), '--runtime', str(self.rt.root),
                               '--since', self.since.isoformat()], capture_output=True, text=True, timeout=120)
        self.assertIn('core research shadows', text.stdout)
        self.assertIn('unique bars', text.stdout)


if __name__ == '__main__':
    unittest.main()
