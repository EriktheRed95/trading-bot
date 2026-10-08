"""Forward progress view and the coverage-report attribution repairs behind it.

Synthetic books in a temporary runtime only: no live runtime, no network, no real account. The
expected numbers are worked by hand in the comments, not read back from the code.
"""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_coverage_report import Runtime, START, UNTIL, bars_15m, stock_obs

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'scripts'))
import coverage_report as cov      # noqa: E402
import forward_progress as fp      # noqa: E402

UTC = timezone.utc
TREND = 'stock-experiments-v1/stock-lab-v1-trend_15m.sqlite'
REVERT = 'stock-experiments-v1/stock-lab-v1-mean_reversion_15m.sqlite'
BREAK = 'stock-experiments-v1/stock-lab-v1-breakout_15m.sqlite'
HOLD = 'stock-experiments-v1/stock-lab-v1-buy_hold_15m.sqlite'
THREE_DAYS = datetime(2026, 9, 21, 13, 0, tzinfo=UTC), datetime(2026, 9, 23, 20, 30, tzinfo=UTC)   # Mon 09:00 ET .. Wed 16:30 ET


def labels_15m(since, until):
    return [label for label, _ in cov.expected_bars('stocks_15m', since, until)]


def z(*parts):
    return datetime(*parts, tzinfo=UTC)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.rt = Runtime(self.dir.name)

    def stock_pair(self, since, until, skip=(), fills=(), shared_skip=False):
        """A strategy that misses the `skip` bars and a reference that records all of them (or misses them too)."""
        labels = labels_15m(since, until)
        kept = [label for label in labels if label not in skip]
        self.rt.book(TREND, stock_obs(kept), fills)
        self.rt.book(HOLD, stock_obs(kept if shared_skip else labels))
        self.rt.heartbeat(begin=since, end=until)
        return labels

    def progress(self, since, until, **kw):
        return fp.build(self.rt.root, since=since, until=until, now=until, **kw)


class GateArithmetic(Fixture):
    def test_sessions_and_bars_still_needed_are_exact(self):
        since, until = THREE_DAYS
        labels = labels_15m(since, until)
        self.assertEqual(len(labels), 78)                       # three 26-bar sessions
        skip = labels[30:32]                                    # two bars in the middle of Tuesday (bars 5 and 6)
        self.stock_pair(since, until, skip=skip)
        report = self.progress(since, until)
        row = report['comparisons'][0]
        # Monday and Wednesday are complete and fully matched; Tuesday lost two bars, so it cannot count.
        self.assertEqual(row['complete_matched_sessions'], 2)
        self.assertEqual(row['eligible_session_dates'], ['2026-09-21', '2026-09-23'])
        self.assertEqual(row['gates']['sessions'], {'have': 2, 'need': 20, 'remaining': 18, 'met': False})
        # 78 bars minus 2 missed = 76 matched; the gap splits them into runs of 30 and 46 bars: 29 + 45 = 74 intervals.
        self.assertEqual(row['gates']['intervals'], {'have': 74, 'need': 200, 'remaining': 126, 'met': False})
        self.assertEqual(row['gates']['fills'], {'have': 0, 'need': 10, 'remaining': 10, 'met': False})
        further = row['further']
        # The next 18 sessions are 2026-09-24 .. 2026-10-19, all ordinary (26 bars): 468 along the calendar path, and 468 as the
        # conditional ordinary-session equivalent. The calendar still holds three 14-bar early closes (2026-11-27, 2026-12-24,
        # 2027-11-26), so no choice of 18 future sessions needs fewer than 3 * 14 + 15 * 26 = 432 bars: that is the only true lower bound.
        self.assertEqual((further['bars_per_session'], further['sessions_left']), (26, 18))
        self.assertEqual((further['next_sessions_path_bars'], further['next_sessions_path_last_session']), (468, '2026-10-19'))
        self.assertEqual((further['ordinary_session_equivalent_bars'], further['minimum_further_matched_bars_per_account']), (468, 432))
        # 126 intervals are needed, the run continues at the pin (Wednesday's last bar was matched), so no anchor bar: 126 < 432.
        self.assertEqual((further['intervals_left'], further['interval_anchor_bars'], further['bars_for_intervals']), (126, 0, 126))
        self.assertEqual(further['binding_bar_gate'], ['sessions'])
        # No fills at all is the furthest gate (0 of 10), whatever the sessions say.
        self.assertEqual((row['bottleneck_gate'], row['bottleneck_share_pct']), ('fills', 0.0))

    def test_an_open_session_matched_so_far_needs_only_its_remaining_bars(self):
        since = THREE_DAYS[0]
        until = z(2026, 9, 23, 15, 0)                           # 11:00 ET on Wednesday: the session is open
        self.stock_pair(since, until)
        row = self.progress(since, until)['comparisons'][0]
        # Bars ending 13:45, 14:00, 14:15, 14:30 and 14:45Z are due (end + 2 min ready + 5 min grace <= 15:00Z): 5 of 26.
        pending = cov.build(self.rt.root, since=since, until=until, now=until)['comparisons'][0]['pending_sessions']
        self.assertEqual(pending, [{'session': '2026-09-23', 'bars_in_session': 26, 'due_bars': 5, 'matched_due_bars': 5,
                                    'matched_so_far_in_full': True}])
        self.assertEqual(row['complete_matched_sessions'], 2)   # Monday and Tuesday
        # 18 sessions still needed: Wednesday costs its 21 remaining bars, the other 17 cost 26 each = 21 + 442.
        # Path: Wednesday's 21 remaining bars + 17 ordinary sessions (09-24 .. 10-16) = 463. Lower bound: the 18 cheapest options are
        # three 14-bar early closes, Wednesday's 21 and fourteen 26-bar sessions = 42 + 21 + 364 = 427.
        self.assertEqual(row['further']['next_sessions_path_bars'], 463)
        self.assertEqual(row['further']['minimum_further_matched_bars_per_account'], 427)

    def test_a_pending_session_with_a_missed_bar_gets_no_credit(self):
        since = THREE_DAYS[0]
        until = z(2026, 9, 23, 15, 0)
        labels = labels_15m(since, until)
        due_wednesday = [l for l in labels if l.startswith('2026-09-23')]
        self.stock_pair(since, until, skip=due_wednesday[1:2])
        row = self.progress(since, until)['comparisons'][0]
        self.assertEqual(row['complete_matched_sessions'], 2)
        self.assertEqual(row['further']['next_sessions_path_bars'], 18 * 26)   # the lost session cannot be finished, so no credit
        self.assertEqual(row['further']['minimum_further_matched_bars_per_account'], 3 * 14 + 15 * 26)

    def test_open_utc_day_whose_bars_are_all_due_costs_nothing_more(self):
        since, until = z(2026, 9, 21, 0, 0), z(2026, 9, 22, 23, 50)
        labels = [l for l, _ in cov.expected_bars('hourly_crypto', since, until)]
        for rel in ('hourly-v1/BTC-USD__trend.sqlite3', 'hourly-v1/BTC-USD__buy-hold.sqlite3'):
            self.rt.book(rel, [(l, cov.utc(l) + timedelta(minutes=2), 1000.0, 1000.0) for l in labels])
        self.rt.heartbeat(begin=since, end=until)
        row = self.progress(since, until)['comparisons'][0]
        # 09-21 is complete; 09-22 holds all 24 labels but the UTC day only closes at midnight: not counted yet, no bar still owed.
        self.assertEqual(row['complete_matched_sessions'], 1)
        self.assertEqual(row['further']['sessions_left'], 19)
        # 0 for the open day + 18 whole days; crypto has no early close, so the lower bound is the same.
        self.assertEqual((row['further']['next_sessions_path_bars'], row['further']['minimum_further_matched_bars_per_account']), (18 * 24, 18 * 24))
        self.assertEqual(row['further']['ordinary_session_equivalent_bars'], 19 * 24)      # conditional figure, deliberately not the same
        self.assertEqual(row['further']['bars_per_session'], 24)

    def test_met_gates_need_nothing_and_do_not_claim_a_result(self):
        row = {'need': {'sessions': 20, 'intervals': 100, 'fills': 10}, 'have': {'sessions': 25, 'intervals': 120, 'fills': 12},
               'pending_sessions': []}
        further = fp.further_bars(row, [], 24)
        self.assertEqual((further['sessions_left'], further['intervals_left'], further['minimum_further_matched_bars_per_account']), (0, 0, 0))
        self.assertEqual(further['binding_bar_gate'], [])

    def test_progress_rows_carry_no_return_or_date_projection(self):
        since, until = THREE_DAYS
        self.stock_pair(since, until)
        report = self.progress(since, until)
        keys = set(report['comparisons'][0]) | set(report['comparisons'][0]['further'])
        self.assertFalse([k for k in keys if 'return' in k or 'difference' in k or 'eta' in k.lower() or 'forecast' in k.lower()])
        text = fp.render(report)
        self.assertIn('no profit or date projection', text)
        self.assertNotRegex(text, r'\bETA\b|per day|per week|annuali[sz]ed|expected (?:by|date)')


class Grouping(Fixture):
    def test_equal_states_are_grouped_and_nearer_accounts_rank_first(self):
        since, until = THREE_DAYS
        labels = labels_15m(since, until)
        skip = labels[30:32]
        self.stock_pair(since, until, skip=skip)                                     # trend: Monday and Wednesday only
        self.rt.book(REVERT, stock_obs([l for l in labels if l not in skip]))        # same state as trend
        self.rt.book(BREAK, stock_obs(labels))                                       # all three sessions
        closest = self.progress(since, until)['closest']
        self.assertEqual([len(c['strategy_accounts']) for c in closest], [1, 2])
        self.assertEqual(closest[0]['strategy_accounts'], ['breakout 15m'])
        self.assertEqual(closest[1]['strategy_accounts'], ['mean_reversion 15m', 'trend 15m'])
        # All fills are 0 of 10, so the order comes from the bars still owed: 17 sessions (442) before 18 (468).
        self.assertEqual([c['best']['further']['next_sessions_path_bars'] for c in closest], [442, 468])

    def test_comparisons_sharing_a_strategy_are_one_line_of_evidence(self):
        since, until = z(2026, 9, 21, 0, 0), z(2026, 9, 26, 0, 0)
        expected = cov.expected_bars('core', since, until)
        self.assertEqual(len(expected), 5)                                           # Monday..Friday
        obs = [(label, ready + timedelta(minutes=1), 10000.0, 0.0) for label, ready in expected]
        for rel in ('paper.sqlite3', 'hourly-v1/core-benchmark-SPY.sqlite3', 'hourly-v1/core-benchmark-QQQ.sqlite3'):
            self.rt.book(rel, obs, initial=10000.0)
        self.rt.heartbeat(begin=since, end=until)
        report = self.progress(since, until)
        self.assertEqual((report['families']['core']['comparisons'], report['families']['core']['evidence_lines']), (2, 1))
        self.assertEqual(len(report['closest']), 1)
        state = report['closest'][0]
        self.assertEqual((state['references'], state['comparisons']), (['benchmark QQQ', 'benchmark SPY'], 2))
        self.assertTrue(state['identical_across_references'])
        self.assertEqual(state['best']['complete_matched_sessions'], 5)


class FillsAndFairness(Fixture):
    def test_a_rebalance_of_several_legs_is_one_fill_event(self):
        since, until = THREE_DAYS
        labels = labels_15m(since, until)
        legs = [(labels[10], ticker, 1.0, 100.0, 0.06) for ticker in ('AAA', 'BBB', 'CCC')]
        self.stock_pair(since, until, fills=legs + [(labels[40], 'AAA', -1.0, 101.0, 0.06)])
        row = self.progress(since, until)['comparisons'][0]
        self.assertEqual(row['gates']['fills']['have'], 4)           # the gate counts legs, as before
        self.assertEqual(row['strategy_fill_events'], 2)             # two distinct bars

    def test_fairness_flags_are_read_from_the_books(self):
        since, until = THREE_DAYS
        labels = labels_15m(since, until)
        invested = [(l, cov.utc(l) + timedelta(minutes=2), 1000.0, 500.0) for l in labels]          # half in cash throughout
        reference = [(l, cov.utc(l) + timedelta(minutes=2), 25000.0, 0.0) for l in labels]
        self.rt.book(TREND, invested, [(labels[3], 'AAA', 1.0, 100.0, 0.06)], initial=1000.0)
        self.rt.book(HOLD, reference, [('2026-09-18T15:00:00+00:00', 'BBB', 1.0, 100.0, 0.06)], initial=25000.0)
        self.rt.heartbeat(begin=since, end=until)
        fair = self.progress(since, until)['comparisons'][0]['fairness']
        self.assertEqual((fair['mean_exposure_pct_strategy'], fair['mean_exposure_pct_reference']), (50.0, 100.0))
        self.assertEqual(fair['reference_minus_strategy_exposure_pp'], 50.0)
        self.assertEqual(fair['strategy_traded_not_in_reference'], ['AAA'])
        self.assertEqual(fair['flags'], ['initial_capital_differs', 'reference_more_invested', 'different_universe',
                                         'reference_entered_before_window'])
        self.assertEqual(fair['strategy_fees_in_window'], 0.06)

    def test_a_matched_reference_raises_no_flag(self):
        since, until = THREE_DAYS
        labels = labels_15m(since, until)
        self.rt.book(TREND, stock_obs(labels), [(labels[3], 'AAA', 1.0, 100.0, 0.06)])
        self.rt.book(HOLD, stock_obs(labels), [(labels[0], 'AAA', 1.0, 100.0, 0.06)])
        self.rt.heartbeat(begin=since, end=until)
        fair = self.progress(since, until)['comparisons'][0]['fairness']
        self.assertEqual(fair['flags'], [])
        self.assertEqual(fair['reference_minus_strategy_exposure_pp'], 0.0)


class HeldAndBlocked(Fixture):
    def test_a_held_account_is_blocked_with_its_stored_reason(self):
        since, until = START, UNTIL
        expected = cov.expected_bars('hourly_listed', since, until)
        live = [(l, cov.utc(l) + timedelta(minutes=6), 1000.0, 1000.0) for l, _ in expected]
        for rel in ('hourly-v1/AAPL__trend.sqlite3', 'hourly-v1/AAPL__buy-hold.sqlite3'):
            self.rt.book(rel, live)
        for rel in ('hourly-v1/FXA__trend.sqlite3', 'hourly-v1/FXA__buy-hold.sqlite3'):
            self.rt.book(rel, [])
        (self.rt.root / 'hourly-v1' / 'quality.json').write_text(json.dumps(
            {'observed_at': (UNTIL + timedelta(minutes=40)).isoformat(),
             'holds': {'FXA': 'Needs 200 valid completed hourly bars', 'AAPL': 'Market closed or data stale; no new fills'}}), encoding='utf-8')
        self.rt.heartbeat()
        report = self.progress(since, until)
        held = report['held']
        self.assertEqual(held['blocked_comparisons'], [{'comparison': 'FXA trend vs FXA buy-hold', 'instrument': 'FXA',
                                                       'reason': 'Needs 200 valid completed hourly bars'}])
        self.assertEqual(held['readiness_holds'], {'FXA': 'Needs 200 valid completed hourly bars'})
        self.assertEqual(held['market_closed_or_stale_symbols'], 1)
        self.assertTrue(held['quality_file_written_after_window_end'])
        self.assertEqual(report['families']['hourly_listed']['blocked_comparisons'], 1)
        aapl = next(r for r in report['comparisons'] if r['instrument'] == 'AAPL')
        self.assertFalse(aapl['blocked_no_matched_bar'])


class Evidence(Fixture):
    def test_missing_bars_are_split_by_evidence_and_failure_class(self):
        since, until = THREE_DAYS
        labels = labels_15m(since, until)
        skip = labels[10:12] + labels[40:41]
        self.stock_pair(since, until, skip=skip, shared_skip=True)               # no account of the family recorded them
        # Bar 10 fails with a stored TypeError, bar 11 is rejected by data checks, bar 40 sees no attempt at all.
        self.rt.attempt('stocks_15m', cov.utc(labels[10]) + timedelta(minutes=3), 'error', 'Collection failed (TypeError); existing paper state preserved.')
        self.rt.attempt('stocks_15m', cov.utc(labels[11]) + timedelta(minutes=3), 'held', 'Held: PaperHold: Provider returned no bars')
        ledger = self.progress(since, until)['evidence']['stocks_15m']
        self.assertEqual((ledger['expected_bars'], ledger['recorded_bars'], ledger['missing_bars']), (78, 75, 3))
        self.assertEqual(ledger['missing_bars_by_evidence'], {'provider_or_data_failure': 2, 'no_attempt_recorded': 1})
        self.assertEqual(ledger['bars_blamed_on_a_failed_check_by_class'], {'unclassified': 1, 'data_rejected': 1})
        self.assertEqual(ledger['missing_bars_with_no_attempt_evidence'], 1)
        self.assertEqual(ledger['failed_checks_in_missed_bar_windows_by_class'], {'unclassified': 1, 'data_rejected': 1})

    def test_silence_inside_the_current_process_rules_out_a_restart_only(self):
        since, until = START, UNTIL
        # Silences 14:55 -> 15:45 (50 min) before the process started at 17:00, and 17:55 -> 18:55 (60 min) after it.
        self.rt.heartbeat(skip=[(z(2026, 9, 21, 15, 0), z(2026, 9, 21, 15, 40)), (z(2026, 9, 21, 18, 0), z(2026, 9, 21, 18, 50))])
        self.rt.store.begin(4242, z(2026, 9, 21, 17, 0))
        self.rt.store.heartbeat(UNTIL)
        self.rt.book(TREND, stock_obs(bars_15m()))
        self.rt.book(HOLD, stock_obs(bars_15m()))
        silence = self.progress(since, until)['silence']
        self.assertEqual(silence['periods'], 2)
        self.assertEqual(silence['minutes'], 110.0)
        self.assertEqual(silence['inside_recorded_process_span']['periods'], 1)
        self.assertEqual(silence['inside_recorded_process_span']['minutes'], 60.0)
        self.assertEqual(silence['inside_recorded_process_span']['pid'], 4242)
        self.assertEqual(silence['after_recorded_stop'], {'periods': 0, 'minutes': 0.0})
        self.assertIn('does not distinguish sleep', silence['note'])

    def test_baseline_counts_what_changed_between_two_pins(self):
        since, until = THREE_DAYS
        labels = labels_15m(since, until)
        self.stock_pair(since, until, skip=labels[30:32])
        monday_tuesday_close = z(2026, 9, 22, 20, 30)
        report = self.progress(since, until, baseline_until=monday_tuesday_close)
        change = report['change_since_baseline']
        # By Tuesday's close only Monday was complete and matched; Tuesday lost two bars. Wednesday then added one.
        self.assertEqual(change['comparisons_that_gained_complete_matched_sessions'], 1)
        self.assertEqual(change['examples'][0]['complete_matched_sessions_added'], 1)
        self.assertEqual(change['families']['stocks_15m']['complete_sessions_added'], 1)
        self.assertEqual(change['families']['stocks_15m']['fully_recorded_sessions_added'], 1)
        self.assertEqual((change['thresholds_met_baseline'], change['comparisons_baseline']), (0, 1))
        self.assertIn('baseline pin 2026-09-22 20:30Z', fp.render(report))


class ReadOnlyAndEmpty(Fixture):
    def snapshot(self):
        return {str(p.relative_to(self.rt.root)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(self.rt.root.rglob('*')) if p.is_file()}

    def test_the_command_line_view_writes_nothing(self):
        since, until = THREE_DAYS
        self.stock_pair(since, until, skip=labels_15m(since, until)[30:32])
        before = self.snapshot()
        for extra in ([], ['--json']):
            done = subprocess.run([sys.executable, '-B', str(ROOT / 'scripts' / 'forward_progress.py'), '--runtime', str(self.rt.root),
                                   '--since', since.isoformat(), '--until', until.isoformat(), '--baseline-until', '2026-09-22T20:30:00+00:00'] + extra,
                                  capture_output=True, text=True, timeout=120)
            self.assertEqual(done.returncode, 0, done.stderr)
            if extra:
                self.assertEqual(json.loads(done.stdout)['status'], {'comparisons': 1, 'thresholds_met': 0})
            else:
                self.assertIn('STATUS: 0 of 1 comparisons meet the sample gates.', done.stdout)
        self.assertEqual(self.snapshot(), before)

    def test_without_attempts_or_a_start_it_stops_with_the_coverage_message(self):
        with self.assertRaises(SystemExit) as caught:
            fp.build(self.rt.root, until=UNTIL, now=UNTIL)
        self.assertIn('No collector attempts recorded', str(caught.exception))

    def test_a_pinned_start_with_an_empty_attempt_log_still_reports(self):
        since, until = THREE_DAYS
        labels = labels_15m(since, until)
        self.rt.book(TREND, stock_obs(labels))
        self.rt.book(HOLD, stock_obs(labels))
        report = self.progress(since, until)
        self.assertEqual(report['silence']['periods'], 0)
        self.assertEqual(report['comparisons'][0]['complete_matched_sessions'], 3)
        self.assertEqual(report['evidence']['stocks_15m']['missing_bars'], 0)
        self.assertIn('STATUS: 0 of 1 comparisons', fp.render(report))


class AttributionRepairs(Fixture):
    """The report must not call an unexplained exception a provider fault, and must not pass off live holds as a pinned fact."""

    def test_failure_classes_follow_only_what_the_stored_message_says(self):
        for message, expected in (
                ('Active refresh held (ConnectionError); prior records preserved.', 'network_error'),
                ('Collection failed (URLError); existing paper state preserved.', 'network_error'),
                ('Collection failed (ReadTimeout); existing paper state preserved.', 'network_error'),
                ('Held: PaperHold: Provider returned no bars', 'data_rejected'),
                ('A held or queued asset has no current price; all fills are held.', 'data_rejected'),
                ('Hourly refresh held (TypeError); previous records preserved.', 'unclassified'),
                ('Collection failed (KeyError); existing paper state preserved.', 'unclassified'),
                ('', 'unclassified'), (None, 'unclassified')):
            with self.subTest(message=message):
                self.assertEqual(cov.failure_class(message), expected)

    def gap_for(self, *attempts, missing=4):
        labels = bars_15m()
        gone = labels[missing:missing + 1]
        self.stock_pair(START, UNTIL, skip=gone, shared_skip=True)
        for outcome, message in attempts:
            self.rt.attempt('stocks_15m', cov.utc(gone[0]) + timedelta(minutes=3), outcome, message)
        return cov.build(self.rt.root, since=START, until=UNTIL, now=UNTIL)['families']['stocks_15m']['gaps'][0]

    def test_an_unexplained_exception_keeps_its_cause_key_but_is_not_called_a_provider_fault(self):
        gap = self.gap_for(('error', 'Hourly refresh held (TypeError); previous records preserved.'))
        self.assertEqual((gap['primary'], gap['bar_causes']), ('provider_or_data_failure', {'provider_or_data_failure': 1}))
        self.assertEqual((gap['failure_classes'], gap['bar_failure_classes']), ({'unclassified': 1}, {'unclassified': 1}))
        self.assertIn('not established as provider faults', gap['interpretation'])
        self.assertNotIn('provider or data was rejected', gap['interpretation'])

    def test_a_named_network_error_is_a_network_error(self):
        gap = self.gap_for(('error', 'Active refresh held (ConnectionError); prior records preserved.'))
        self.assertEqual(gap['bar_failure_classes'], {'network_error': 1})
        self.assertNotIn('not established', gap['interpretation'])

    def test_an_established_class_outranks_an_unclassified_one_in_the_same_bar_window(self):
        gap = self.gap_for(('error', 'Collection failed (TypeError); existing paper state preserved.'),
                           ('held', 'Held: PaperHold: Provider returned no bars'))
        self.assertEqual(gap['bar_failure_classes'], {'data_rejected': 1})
        self.assertEqual(gap['failure_classes'], {'unclassified': 1, 'data_rejected': 1})
        self.assertEqual(gap['failed_checks'], {'error': 1, 'held': 1})

    def quality(self, observed_at):
        self.rt.book('hourly-v1/AAPL__buy-hold.sqlite3', [])
        (self.rt.root / 'hourly-v1' / 'quality.json').write_text(json.dumps(
            {'observed_at': observed_at.isoformat(),
             'holds': {'FXA': 'Needs 200 valid completed hourly bars', 'AAPL': 'Market closed or data stale; no new fills',
                       'SPY': 'Market closed or data stale; no new fills'}}), encoding='utf-8')

    def test_market_closed_holds_are_kept_apart_from_readiness_holds(self):
        self.stock_pair(START, UNTIL)
        self.quality(UNTIL + timedelta(minutes=3))
        report = cov.build(self.rt.root, since=START, until=UNTIL, now=UNTIL)
        hold = report['held_accounts']['hourly_quality']
        self.assertEqual(sorted(hold['holds']), ['AAPL', 'FXA', 'SPY'])                       # the stored mapping is unchanged
        self.assertEqual(hold['readiness_holds'], {'FXA': 'Needs 200 valid completed hourly bars'})
        self.assertEqual(hold['market_closed_or_stale'], ['AAPL', 'SPY'])
        self.assertTrue(hold['observed_after_window_end'])
        text = cov.render(report)
        self.assertIn('readiness holds as of', text)
        self.assertIn('live file, written after the window end', text)
        self.assertIn('FXA (Needs 200 valid completed hourly bars)', text)
        self.assertIn('2 more symbol(s) show only a market-closed-or-stale state', text)
        self.assertNotIn('AAPL (Market closed', text)

    def test_a_quality_file_written_inside_the_window_is_not_called_live(self):
        self.stock_pair(START, UNTIL)
        self.quality(UNTIL - timedelta(minutes=3))
        report = cov.build(self.rt.root, since=START, until=UNTIL, now=UNTIL)
        self.assertFalse(report['held_accounts']['hourly_quality']['observed_after_window_end'])
        self.assertNotIn('live file', cov.render(report))

    def test_missing_or_broken_quality_file_is_empty_not_fatal(self):
        self.stock_pair(START, UNTIL)
        hold = cov.build(self.rt.root, since=START, until=UNTIL, now=UNTIL)['held_accounts']['hourly_quality']
        self.assertEqual((hold['holds'], hold['readiness_holds'], hold['market_closed_or_stale'], hold['observed_after_window_end']),
                         ({}, {}, [], False))
        (self.rt.root / 'hourly-v1').mkdir(exist_ok=True)
        (self.rt.root / 'hourly-v1' / 'quality.json').write_text(json.dumps({'observed_at': 'not a time', 'holds': {'X': 'y'}}), encoding='utf-8')
        hold = cov.build(self.rt.root, since=START, until=UNTIL, now=UNTIL)['held_accounts']['hourly_quality']
        self.assertEqual((hold['holds'], hold['observed_after_window_end']), ({'X': 'y'}, False))      # an unreadable time hides nothing
        (self.rt.root / 'hourly-v1' / 'quality.json').write_text('{broken', encoding='utf-8')
        self.assertEqual(cov.build(self.rt.root, since=START, until=UNTIL, now=UNTIL)['held_accounts']['hourly_quality']['holds'], {})

    def test_a_pinned_window_older_than_the_heartbeat_says_so_instead_of_a_negative_age(self):
        self.stock_pair(START, UNTIL)
        self.rt.store.begin(99, START)
        self.rt.store.heartbeat(UNTIL + timedelta(minutes=4))
        report = cov.build(self.rt.root, since=START, until=UNTIL, now=UNTIL)
        current = report['process']['current']
        self.assertTrue(current['heartbeat_after_window_end'])
        self.assertEqual(current['heartbeat_minutes_before_window_end'], -4.0)             # key and sign kept for existing readers
        text = cov.render(report)
        self.assertIn('4.0 min after the window end: the databases are newer than this pinned window', text)
        self.assertNotIn('-4.0 min before', text)

    def test_a_heartbeat_inside_the_window_is_still_reported_as_before_its_end(self):
        self.stock_pair(START, UNTIL)
        self.rt.store.begin(99, START)
        self.rt.store.heartbeat(UNTIL - timedelta(minutes=4))
        report = cov.build(self.rt.root, since=START, until=UNTIL, now=UNTIL)
        self.assertFalse(report['process']['current']['heartbeat_after_window_end'])
        self.assertIn('4.0 min before the window end', cov.render(report))


# ------------------------------------------------------------------ repairs after independent review

def append_rows(root, rel, observations, fills=()):
    """Add rows to an existing book the way a still-running collector would (a later write, after the pin)."""
    with closing(sqlite3.connect(Path(root) / rel)) as con, con:
        for asof, seen, equity, cash in observations:
            con.execute('INSERT INTO observations VALUES(?,?,?,?,?,?)', (asof, seen.isoformat(), equity, cash, '{}', 'test'))
        for asof, ticker, shares, price, cost in fills:
            con.execute('INSERT INTO trades(asof,signal_date,ticker,shares,price,cost) VALUES(?,?,?,?,?,?)',
                        (asof, asof, ticker, shares, price, cost))


class PinStability(Fixture):
    """Evidence recorded after the pin must not change anything a pinned report says (the collector keeps writing)."""

    def two_books(self):
        since, until = THREE_DAYS
        labels = labels_15m(since, until)
        self.rt.book(TREND, stock_obs(labels), [(labels[3], 'AAA', 1.0, 100.0, 0.06)])
        self.rt.book(HOLD, stock_obs(labels))                                  # the reference never filled
        self.rt.heartbeat(begin=since, end=until)
        return since, until

    def thursday(self):
        later = labels_15m(THREE_DAYS[0], z(2026, 9, 24, 21, 0))[len(labels_15m(*THREE_DAYS)):]
        self.assertEqual(len(later), 26)
        return later

    def test_post_pin_observations_and_fills_leave_the_pinned_view_unchanged(self):
        since, until = self.two_books()
        before = self.progress(since, until)
        self.assertEqual(before['comparisons'][0]['fairness']['flags'], [])
        later = self.thursday()
        append_rows(self.rt.root, TREND, stock_obs(later), [(later[2], 'AAA', 1.0, 100.0, 0.06)])
        append_rows(self.rt.root, HOLD, stock_obs(later), [(later[0], 'BBB', 5.0, 100.0, 0.5)])      # the reference's first and only fill
        after = self.progress(since, until)
        row = after['comparisons'][0]
        self.assertNotIn('different_universe', row['fairness']['flags'])
        self.assertNotIn('reference_entered_before_window', row['fairness']['flags'])
        self.assertEqual(row['fairness']['reference_fills_before_window'], 0)
        self.assertEqual(row['fairness']['strategy_traded_not_in_reference'], ['AAA'])
        self.assertEqual(row['gates']['fills'], {'have': 1, 'need': 10, 'remaining': 9, 'met': False})
        self.assertEqual(row['fairness']['reference_fees_in_window'], 0)
        for key in ('comparisons', 'closest', 'evidence', 'sessions', 'families', 'silence'):
            self.assertEqual(after[key], before[key], key)
        # The same pin moved forward one session does see them: the repair clips, it does not hide evidence.
        moved = self.progress(since, z(2026, 9, 24, 21, 0))['comparisons'][0]['fairness']
        self.assertIn('different_universe', moved['flags'])
        self.assertEqual(moved['reference_fills_before_window'], 0)
        self.assertNotIn('reference_entered_before_window', moved['flags'])

    def test_a_reference_fill_before_the_window_is_still_flagged_control(self):
        since, until = self.two_books()
        append_rows(self.rt.root, HOLD, [], [('2026-09-18T15:00:00+00:00', 'AAA', 5.0, 100.0, 0.5)])        # recorded long before the window
        fair = self.progress(since, until)['comparisons'][0]['fairness']
        self.assertEqual((fair['flags'], fair['reference_fills_before_window']), (['reference_entered_before_window'], 1))

    def test_an_entry_before_the_window_is_flagged_even_when_the_reference_also_traded_inside_it(self):
        since, until = self.two_books()
        labels = labels_15m(since, until)
        append_rows(self.rt.root, HOLD, [], [('2026-09-18T15:00:00+00:00', 'AAA', 5.0, 100.0, 0.5), (labels[6], 'AAA', 1.0, 100.0, 0.1)])
        fair = self.progress(since, until)['comparisons'][0]['fairness']
        self.assertIn('reference_entered_before_window', fair['flags'])      # absence of in-window fills was the old, wrong test
        self.assertEqual(fair['reference_fills_before_window'], 1)

    def test_a_reference_whose_only_fill_is_inside_the_window_was_not_entered_before_it(self):
        since, until = self.two_books()
        labels = labels_15m(since, until)
        append_rows(self.rt.root, HOLD, [], [(labels[0], 'AAA', 5.0, 100.0, 0.5)])
        fair = self.progress(since, until)['comparisons'][0]['fairness']
        self.assertEqual((fair['flags'], fair['reference_fills_before_window']), ([], 0))

    def test_a_fill_whose_bar_was_observed_after_the_pin_is_not_pinned_history(self):
        since, until = THREE_DAYS
        info = {'observations': [{'asof': '2026-09-23T19:45:00+00:00', 'observed_at': until + timedelta(minutes=10)},
                                 {'asof': '2026-09-22T19:45:00+00:00', 'observed_at': until - timedelta(days=1)}],
                'fills': [{'asof': '2026-09-23T19:45:00+00:00', 'ticker': 'LATE'}, {'asof': '2026-09-22T19:45:00+00:00', 'ticker': 'ON_TIME'},
                          {'asof': '2026-09-24T14:00:00+00:00', 'ticker': 'UNOBSERVED_AFTER'}, {'asof': '2026-09-18T15:00:00+00:00', 'ticker': 'OLD'}]}
        self.assertEqual([f['ticker'] for _, f in fp.history_by_pin(info, until)], ['OLD', 'ON_TIME'])

    def test_a_collector_writing_during_the_report_neither_blocks_nor_leaks_into_it(self):
        since, until = self.two_books()
        before = self.progress(since, until)
        later = self.thursday()
        writer = sqlite3.connect(self.rt.root / HOLD, isolation_level=None, timeout=0)
        self.addCleanup(writer.close)
        collector = sqlite3.connect(self.rt.root / 'collector.sqlite3', isolation_level=None, timeout=0)
        self.addCleanup(collector.close)
        writer.execute('BEGIN IMMEDIATE')
        collector.execute('BEGIN IMMEDIATE')
        for asof, seen, equity, cash in stock_obs(later):
            writer.execute('INSERT INTO observations VALUES(?,?,?,?,?,?)', (asof, seen.isoformat(), equity, cash, '{}', 'test'))
        writer.execute('INSERT INTO trades(asof,signal_date,ticker,shares,price,cost) VALUES(?,?,?,?,?,?)', (later[0], later[0], 'BBB', 1.0, 100.0, 0.1))
        collector.execute("INSERT INTO attempts(family,source,started_at,finished_at,outcome,new_observations,message) VALUES"
                          "('stocks_15m','scheduler',?,?,'held',0,'Held: PaperHold: Provider returned no bars')",
                          (until.isoformat(), until.isoformat()))
        for how in ('in process', 'command line'):
            if how == 'in process':
                during = self.progress(since, until)
            else:
                done = subprocess.run([sys.executable, '-B', str(ROOT / 'scripts' / 'forward_progress.py'), '--runtime', str(self.rt.root),
                                       '--since', since.isoformat(), '--until', until.isoformat(), '--json'],
                                      capture_output=True, text=True, timeout=120)
                self.assertEqual(done.returncode, 0, done.stderr)
                during = json.loads(done.stdout)
            self.assertEqual(during['comparisons'][0]['gates'], before['comparisons'][0]['gates'], how)
            self.assertEqual(during['comparisons'][0]['fairness'], before['comparisons'][0]['fairness'], how)
            self.assertEqual(during['checks'], before['checks'], how)       # the uncommitted failed check is not seen
        writer.commit()
        collector.commit()
        after = self.progress(since, until)
        self.assertEqual(after['comparisons'], before['comparisons'])
        self.assertEqual(after['evidence'], before['evidence'])

    def test_a_book_locked_by_a_writer_is_flagged_unreadable_and_nothing_is_inferred(self):
        since, until = self.two_books()
        before = self.progress(since, until)

        def quick(path):
            con = sqlite3.connect(f'file:{path.as_posix()}?mode=ro', uri=True, timeout=0.1)
            con.row_factory = sqlite3.Row
            return con
        lock = sqlite3.connect(self.rt.root / TREND, isolation_level=None, timeout=0)
        self.addCleanup(lock.close)
        lock.execute('BEGIN EXCLUSIVE')
        with patch.object(cov, 'read_only', quick):
            locked = self.progress(since, until)
        row = locked['comparisons'][0]
        self.assertTrue(row['blocked_no_matched_bar'])
        self.assertEqual(row['complete_matched_sessions'], 0)
        self.assertEqual(row['fairness']['flags'], ['unreadable_account'])        # not initial_capital_differs, not reference_more_invested
        self.assertIn('locked', row['fairness']['unreadable']['trend 15m'])
        self.assertEqual(locked['status']['thresholds_met'], 0)
        lock.rollback()
        self.assertEqual(self.progress(since, until)['comparisons'], before['comparisons'])


class CalendarBounds(Fixture):
    """Bars still needed must be attainable counts, not ordinary-session multiples."""

    SINCE, PIN = z(2026, 11, 23, 13, 0), z(2026, 11, 25, 22, 0)         # Mon 08:00 ET .. Wed 17:00 ET; 2026-11-26 is Thanksgiving

    def record(self, until):
        labels = labels_15m(self.SINCE, until)
        fills = [(labels[i], 'AAA', 1.0, 100.0, 0.06) for i in range(10)]
        self.rt.book(TREND, stock_obs(labels), fills)
        self.rt.book(HOLD, stock_obs(labels))
        self.rt.heartbeat(begin=self.SINCE, end=until)
        return labels

    def test_an_early_close_makes_the_ordinary_multiple_too_large_to_be_a_minimum(self):
        labels = self.record(self.PIN)
        self.assertEqual(len(labels), 78)
        report = self.progress(self.SINCE, self.PIN)
        row = report['comparisons'][0]
        self.assertEqual((row['gates']['sessions']['have'], row['gates']['fills']['met']), (3, True))
        further = row['further']
        self.assertEqual(further['sessions_left'], 17)
        # The next 17 sessions are 11-27 (early close, 14 bars) and 16 ordinary ones through 12-21: 14 + 16 * 26 = 430.
        self.assertEqual((further['next_sessions_path_bars'], further['next_sessions_path_last_session']), (430, '2026-12-21'))
        self.assertEqual(further['ordinary_session_equivalent_bars'], 17 * 26)               # 442: conditional, larger than 430
        self.assertGreater(further['ordinary_session_equivalent_bars'], further['next_sessions_path_bars'])
        # Bound: the bundled calendar holds exactly three 14-bar early closes, so 17 cheapest sessions are 3 * 14 + 14 * 26 = 406.
        self.assertEqual(further['minimum_further_matched_bars_per_account'], 406)
        self.assertLessEqual(further['minimum_further_matched_bars_per_account'], further['next_sessions_path_bars'])
        text = fp.render(report)
        self.assertNotRegex(text, r'at least \d+ further')
        self.assertIn('Ordinary-session equivalent (conditional, not a bound): 442', text)
        self.assertIn('needs fewer than 406 bars', text)

    def test_recording_the_next_seventeen_sessions_meets_the_gates_in_the_counted_bars(self):
        labels = self.record(self.PIN)
        path = self.progress(self.SINCE, self.PIN)['comparisons'][0]['further']['next_sessions_path_bars']
        done = z(2026, 12, 21, 22, 0)                                                         # after the 12-21 close and every grace period
        grown = labels_15m(self.SINCE, done)
        self.assertEqual(len(grown) - len(labels), path)                                      # the figure is exactly the bars to record
        extra = grown[len(labels):]
        append_rows(self.rt.root, TREND, stock_obs(extra))
        append_rows(self.rt.root, HOLD, stock_obs(extra))
        self.rt.heartbeat(begin=self.PIN, end=done)
        row = self.progress(self.SINCE, done)['comparisons'][0]
        self.assertEqual((row['gates']['sessions']['have'], row['gates']['intervals']['met'], row['gates']['fills']['met']), (20, True, True))
        self.assertEqual(row['verdict'], 'sample_thresholds_met_descriptive_only')
        # One session fewer is not enough: the figure is not an overstatement either.
        short = self.progress(self.SINCE, z(2026, 12, 18, 22, 0))['comparisons'][0]
        self.assertEqual(short['gates']['sessions']['have'], 19)

    def test_a_missed_trailing_bar_makes_the_next_matched_bar_an_anchor_with_no_interval(self):
        since, until = THREE_DAYS
        labels = labels_15m(since, until)
        self.rt.book(TREND, stock_obs(labels[:-1]))                                          # the strategy missed the last due bar
        self.rt.book(HOLD, stock_obs(labels))
        self.rt.heartbeat(begin=since, end=until)
        row = self.progress(since, until)['comparisons'][0]
        self.assertEqual(row['further']['interval_anchor_bars'], 1)
        self.assertEqual(row['further']['bars_for_intervals'], row['further']['intervals_left'] + 1)
        self.assertEqual(row['gates']['intervals']['have'], 76)                              # 77 matched bars in one run
        self.setUp()
        self.rt.book(TREND, stock_obs(labels))
        self.rt.book(HOLD, stock_obs(labels))
        self.rt.heartbeat(begin=since, end=until)
        control = self.progress(since, until)['comparisons'][0]['further']
        self.assertEqual((control['interval_anchor_bars'], control['bars_for_intervals']), (0, control['intervals_left']))

    def test_interval_gate_arithmetic_and_calendar_exhaustion(self):
        def case(**kw):
            return {'need': {'sessions': kw.get('need_sessions', 1), 'intervals': 100, 'fills': 0},
                    'have': {'sessions': 1, 'intervals': 50, 'fills': 0}, 'pending_sessions': [], 'continues_at_pin': kw['continues']}
        future = [('2026-12-01', 30), ('2026-12-02', 30)]
        broken = fp.further_bars(case(continues=False), future)
        self.assertEqual((broken['bars_for_intervals'], broken['minimum_further_matched_bars_per_account'], broken['next_sessions_path_bars']),
                         (51, 51, 60))                                                       # 50 intervals + 1 anchor; whole sessions of 30
        running = fp.further_bars(case(continues=True), future)
        self.assertEqual((running['bars_for_intervals'], running['minimum_further_matched_bars_per_account'], running['next_sessions_path_bars']),
                         (50, 50, 60))
        self.assertEqual(running['binding_bar_gate'], ['intervals'])
        short = fp.further_bars(case(continues=True), future[:1])
        self.assertEqual((short['minimum_further_matched_bars_per_account'], short['next_sessions_path_bars']), (50, None))   # calendar ends first
        none = fp.further_bars(case(continues=True, need_sessions=4), future)
        self.assertEqual((none['minimum_further_matched_bars_per_account'], none['next_sessions_path_bars']), (None, None))
        self.assertIn('beyond the bundled calendar', fp.render(self.fake_report(none)))

    def fake_report(self, further):
        row = {'gates': {n: {'have': 0, 'need': 1, 'remaining': 1, 'met': False} for n in fp.GATES}, 'bottleneck_gate': 'sessions',
               'bottleneck_share_pct': 0.0, 'strategy_fill_events': 0, 'further': further, 'kind': 'stocks_15m'}
        return {'window': {'since': START.isoformat(), 'until': UNTIL.isoformat(), 'hours': 1}, 'pinned': True, 'status': {'thresholds_met': 0, 'comparisons': 1},
                'families': {}, 'closest': [{'best': row, 'strategy_accounts': ['a'], 'references': ['b'], 'identical_across_references': True}],
                'sessions': {}, 'evidence': {}, 'silence': {'periods': 0, 'minutes': 0, 'share_of_window_pct': 0,
                                                            'inside_recorded_process_span': {'periods': 0, 'minutes': 0, 'pid': 1, 'since': None, 'stopped_at': None},
                                                            'after_recorded_stop': {'periods': 0, 'minutes': 0}, 'periods_ending_in_a_failed_first_check': 0,
                                                            'interrupted_checks': 0, 'note': 'n'},
                'checks': {}, 'held': {'blocked_comparisons': [], 'quality_file_written_after_window_end': False, 'market_closed_or_stale_symbols': 0},
                'fairness_flags': {}, 'guardrails': []}


class RecordedProcessSpan(Fixture):
    """Silence is classified against the span the process marker records, including a stored stop."""

    def silent_run(self, stop_at=None, restart_at=None):
        self.rt.book(TREND, stock_obs(bars_15m()))
        self.rt.book(HOLD, stock_obs(bars_15m()))
        self.rt.heartbeat(skip=[(z(2026, 9, 21, 16, 0), z(2026, 9, 21, 18, 0))])               # no attempt 15:55 -> 18:05 (130 min)
        self.rt.store.begin(1, START)
        if restart_at:
            self.rt.store.start('hourly', 'scheduler', z(2026, 9, 21, 14, 30))
            self.rt.store.begin(2, restart_at)
        if stop_at:
            self.rt.store.end(stop_at, 'stopped', 'test stop')
        else:
            self.rt.store.heartbeat(UNTIL)
        return self.progress(START, UNTIL)

    def test_a_silence_after_a_recorded_stop_is_not_inside_the_process_span(self):
        silence = self.silent_run(stop_at=z(2026, 9, 21, 15, 0))['silence']
        self.assertEqual((silence['periods'], silence['minutes']), (1, 130.0))
        self.assertEqual(silence['inside_recorded_process_span']['periods'], 0)
        self.assertEqual(silence['inside_recorded_process_span']['stopped_at'], z(2026, 9, 21, 15, 0).isoformat())
        self.assertEqual(silence['after_recorded_stop'], {'periods': 1, 'minutes': 130.0})
        self.assertIn('not a check that the process is alive now', silence['note'])

    def test_a_silence_that_straddles_the_stop_is_not_counted_inside(self):
        silence = self.silent_run(stop_at=z(2026, 9, 21, 17, 0))['silence']
        self.assertEqual(silence['inside_recorded_process_span']['periods'], 0)
        self.assertEqual(silence['after_recorded_stop']['periods'], 1)

    def test_a_silence_before_the_stop_is_inside_the_span(self):
        silence = self.silent_run(stop_at=z(2026, 9, 21, 19, 0))['silence']
        self.assertEqual((silence['inside_recorded_process_span']['periods'], silence['inside_recorded_process_span']['minutes']), (1, 130.0))
        self.assertEqual(silence['after_recorded_stop']['periods'], 0)

    def test_without_a_recorded_stop_the_open_span_keeps_the_earlier_behaviour(self):
        silence = self.silent_run()['silence']
        self.assertEqual((silence['inside_recorded_process_span']['periods'], silence['inside_recorded_process_span']['stopped_at']), (1, None))
        self.assertEqual(silence['after_recorded_stop']['periods'], 0)

    def test_the_text_states_the_stop_and_does_not_call_it_proof_of_liveness(self):
        text = fp.render(self.silent_run(stop_at=z(2026, 9, 21, 15, 0)))
        self.assertIn('2026-09-21 14:00Z to 2026-09-21 15:00Z', text)
        self.assertIn('1 (130.0 min) end after its recorded stop', text)
        self.assertNotIn('rule out a restart', text)
        self.assertNotIn('only rules out', text)

    def test_a_restart_replaces_the_marker_and_earlier_silence_is_outside_the_new_span(self):
        report = self.silent_run(restart_at=z(2026, 9, 21, 18, 5))
        silence = report['silence']
        self.assertEqual(silence['inside_recorded_process_span']['pid'], 2)
        self.assertEqual(silence['periods'], 1)
        self.assertEqual(silence['inside_recorded_process_span']['periods'], 0)               # the stretch belongs to the first process
        self.assertEqual(silence['interrupted_checks'], 1)                                    # the check left running was closed out as interrupted


class OsErrorAttribution(Fixture):
    """A class name alone cannot show a network cause for OSError, which also covers local file faults."""

    def test_generic_and_local_os_errors_are_not_network_errors(self):
        for name in ('OSError', 'FileNotFoundError', 'PermissionError', 'IsADirectoryError', 'BrokenPipeError', 'BlockingIOError', 'TypeError'):
            with self.subTest(name=name):
                self.assertEqual(cov.failure_class(f'Collection failed ({name}); existing paper state preserved.'), 'unclassified')

    def test_explicit_network_names_stay_network_errors_control(self):
        for name in ('ConnectionError', 'ConnectionResetError', 'ConnectTimeout', 'ReadTimeout', 'TimeoutError', 'URLError', 'HTTPError',
                     'SSLError', 'gaierror', 'ProxyError'):
            with self.subTest(name=name):
                self.assertEqual(cov.failure_class(f'Active refresh held ({name}); prior records preserved.'), 'network_error')

    def test_a_missed_bar_blamed_on_a_local_file_error_stays_unclassified_and_unasserted(self):
        labels = bars_15m()
        gone = labels[4:5]
        self.rt.book(TREND, stock_obs([l for l in labels if l not in gone]))
        self.rt.book(HOLD, stock_obs([l for l in labels if l not in gone]))
        self.rt.heartbeat()
        self.rt.attempt('stocks_15m', cov.utc(gone[0]) + timedelta(minutes=3), 'error', 'Collection failed (OSError); existing paper state preserved.')
        gap = cov.build(self.rt.root, since=START, until=UNTIL, now=UNTIL)['families']['stocks_15m']['gaps'][0]
        self.assertEqual((gap['primary'], gap['bar_failure_classes'], gap['failure_classes']), ('provider_or_data_failure', {'unclassified': 1}, {'unclassified': 1}))
        self.assertIn('not established as provider faults', gap['interpretation'])
        view = self.progress(START, UNTIL)
        self.assertEqual(view['evidence']['stocks_15m']['bars_blamed_on_a_failed_check_by_class'], {'unclassified': 1})
        self.assertEqual(view['checks']['stocks_15m']['failed_checks_by_class'], {'unclassified': 1})

    def test_failed_checks_and_bars_are_counted_separately(self):
        labels = bars_15m()
        gone = labels[4:5]
        self.rt.book(TREND, stock_obs([l for l in labels if l not in gone]))
        self.rt.book(HOLD, stock_obs([l for l in labels if l not in gone]))
        self.rt.heartbeat()
        when = cov.utc(gone[0]) + timedelta(minutes=3)
        for i in range(3):                                                                   # three failed checks, all inside one missed bar's window
            self.rt.attempt('stocks_15m', when + timedelta(seconds=i * 30), 'held', 'Held: PaperHold: Provider returned no bars')
        self.rt.attempt('stocks_15m', cov.utc(labels[10]) + timedelta(minutes=3), 'error', 'Active refresh held (ConnectionError); prior records preserved.')
        view = self.progress(START, UNTIL)
        self.assertEqual(view['evidence']['stocks_15m']['bars_blamed_on_a_failed_check_by_class'], {'data_rejected': 1})   # one bar
        self.assertEqual(view['checks']['stocks_15m']['failed_checks_by_class'], {'data_rejected': 3, 'network_error': 1})  # four checks, one outside any gap
        self.assertIn('checks, not bars', fp.render(view))


class FailedCheckTotals(Fixture):
    """Class totals count every raw failed attempt in the window; the six-item reasons display is only a presentation."""

    def books(self):
        self.rt.book(TREND, stock_obs(bars_15m()))
        self.rt.book(HOLD, stock_obs(bars_15m()))
        self.rt.heartbeat()

    def fail(self, minutes, outcome, message, family='stocks_15m'):
        self.rt.attempt(family, START + timedelta(minutes=minutes), outcome, message)

    def view(self):
        view = self.progress(START, UNTIL)
        self.assertEqual(view['checks']['stocks_15m']['failed_checks_by_class'],
                         cov.build(self.rt.root, since=START, until=UNTIL, now=UNTIL)['checks']['stocks_15m']['failed_checks_by_class'])
        return view

    def test_a_seventh_distinct_network_error_is_counted_while_the_display_stays_at_six(self):
        self.books()
        for i in range(7):                                                          # seven different messages, one attempt each
            self.fail(10 + i, 'error', f'Collection failed (ConnectionError) via route {i}')
        checks = self.view()['checks']['stocks_15m']
        self.assertEqual(checks['failed_checks_by_class'], {'network_error': 7})
        self.assertEqual(len(checks['reasons']), 6)                                 # the cap is kept
        self.assertEqual(sum(r['count'] for r in checks['reasons']), 6)             # so the display alone could only ever show six
        self.assertEqual(checks['outcomes']['error'], 7)

    def test_a_class_that_appears_only_outside_the_displayed_top_six_is_still_counted(self):
        self.books()
        for i, name in enumerate(('KeyError', 'ValueError', 'TypeError', 'AttributeError', 'IndexError', 'RuntimeError')):
            for repeat in range(2):
                self.fail(10 + i * 3 + repeat, 'error', f'Collection failed ({name}); existing paper state preserved.')
        self.fail(40, 'held', 'Held: PaperHold: Provider returned no bars')            # the rarest reason: not among the top six
        checks = self.view()['checks']['stocks_15m']
        self.assertEqual(len(checks['reasons']), 6)
        self.assertFalse([r for r in checks['reasons'] if 'PaperHold' in r['message']])
        self.assertEqual(checks['failed_checks_by_class'], {'unclassified': 12, 'data_rejected': 1})

    def test_mixed_reasons_repeats_and_truncated_messages_keep_exact_totals(self):
        self.books()
        for i in range(7):
            self.fail(10 + i, 'error', f'Active refresh held (ConnectionError); route {i}')
        for i in range(5):
            self.fail(20 + i, 'held', 'Held: PaperHold: Provider returned no bars')      # one reason repeated five times
        for i in range(3):
            self.fail(30 + i, 'error', 'Hourly refresh held (TypeError); previous records preserved.')
        for i in range(2):
            self.fail(40 + i, 'error', 'Collection failed (OSError); existing paper state preserved.')
        self.fail(50, 'error', 'x' * 170 + ' (ConnectionError)')                        # the class sits beyond the 160-character display cut
        checks = self.view()['checks']['stocks_15m']
        self.assertEqual(checks['failed_checks_by_class'], {'network_error': 8, 'data_rejected': 5, 'unclassified': 5})
        self.assertEqual(sum(checks['failed_checks_by_class'].values()), checks['outcomes']['error'] + checks['outcomes']['held'])
        self.assertEqual(len(checks['reasons']), 6)
        text = fp.render(self.progress(START, UNTIL))
        self.assertIn('data_rejected 5, network_error 8, unclassified 5', text)
        self.assertIn('checks, not bars', text)

    def test_interrupted_successful_and_out_of_window_attempts_are_not_counted(self):
        self.books()
        for i in range(2):
            self.fail(10 + i, 'interrupted', 'The previous server process ended before this check finished. Unfinished paper-book transactions are rolled back by SQLite.')
        self.fail(20, 'new', 'ok')
        self.fail(21, 'no_change', 'Connection reset')                                  # a success is never a failure, whatever its text says
        self.fail(-1, 'error', 'Collection failed (ConnectionError) before the window')
        self.fail(int((UNTIL - START).total_seconds() / 60) + 1, 'error', 'Collection failed (ConnectionError) after the pin')
        self.fail(0, 'error', 'Collection failed (ConnectionError) exactly at the start')      # the window is [since, until], both ends inclusive
        self.fail(int((UNTIL - START).total_seconds() / 60), 'held', 'Held: PaperHold: Provider returned no bars')    # exactly at the pin
        checks = self.view()['checks']['stocks_15m']
        self.assertEqual(checks['failed_checks_by_class'], {'network_error': 1, 'data_rejected': 1})
        self.assertEqual(checks['outcomes']['interrupted'], 2)
        self.assertEqual(sum(r['count'] for r in checks['reasons'] if r['message'].startswith('The previous server process ended')), 2)   # still displayed

    def test_no_failures_gives_empty_totals_and_the_class_name_controls_hold(self):
        self.books()
        self.fail(10, 'error', 'Collection failed (OSError); existing paper state preserved.')
        self.fail(11, 'error', 'Collection failed (TypeError); existing paper state preserved.')
        self.assertEqual(self.view()['checks']['stocks_15m']['failed_checks_by_class'], {'unclassified': 2})
        self.setUp()
        self.books()
        self.assertEqual(self.progress(START, UNTIL)['checks']['hourly']['failed_checks_by_class'], {})

    def test_the_other_families_keep_their_own_totals(self):
        self.books()
        for i in range(7):
            self.fail(10 + i, 'error', f'Active refresh held (ConnectionError); route {i}', family='active')
        self.fail(30, 'held', 'Held: PaperHold: Provider returned no bars')
        checks = self.progress(START, UNTIL)['checks']
        self.assertEqual(checks['active']['failed_checks_by_class'], {'network_error': 7})
        self.assertEqual(checks['stocks_15m']['failed_checks_by_class'], {'data_rejected': 1})


if __name__ == '__main__':
    unittest.main()
