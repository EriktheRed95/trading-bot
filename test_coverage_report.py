"""Coverage report and baseline collection report: synthetic books in a temporary runtime.

Nothing here touches the live runtime, the network or a real paper account. Fixtures
write the same SQLite schemas the collector and PaperBook create, with hand-picked times.
"""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from collector import CollectorStore
from paper_book import PaperBook

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'scripts'))
import collection_report as base   # noqa: E402
import coverage_report as cov      # noqa: E402

UTC = timezone.utc
START = datetime(2026, 9, 21, 14, 0, tzinfo=UTC)     # Monday 10:00 New York
UNTIL = datetime(2026, 9, 21, 20, 30, tzinfo=UTC)    # after the 16:00 close and every grace period


def at(minutes, base_time=START):
    return base_time + timedelta(minutes=minutes)


class Runtime:
    """A disposable runtime folder with the schemas the real services write."""

    def __init__(self, root):
        self.root = Path(root)
        self.store = CollectorStore(self.root / 'collector.sqlite3')

    def book(self, rel, observations, fills=(), initial=1000.0):
        path = self.root / rel
        PaperBook(path, initial_cash=initial)
        with closing(sqlite3.connect(path)) as con, con:
            for asof, seen, equity, cash in observations:
                con.execute('INSERT INTO observations VALUES(?,?,?,?,?,?)',
                            (asof, seen.isoformat() if seen else None, equity, cash, '{}', 'test'))
            for asof, ticker, shares, price, cost in fills:
                con.execute('INSERT INTO trades(asof,signal_date,ticker,shares,price,cost) VALUES(?,?,?,?,?,?)',
                            (asof, asof, ticker, shares, price, cost))
        return path

    def attempt(self, family, when, outcome='new', message='ok', source='scheduler', finished=None):
        with self.store.connect() as c:
            c.execute('INSERT INTO attempts(family,source,started_at,finished_at,outcome,new_observations,latest_bar,message) '
                      'VALUES(?,?,?,?,?,?,?,?)', (family, source, when.isoformat(), (finished or when).isoformat(),
                                                  outcome, 1 if outcome == 'new' else 0, None, message))

    def heartbeat(self, begin=START, end=UNTIL, skip=(), step=5):
        """Hourly-family checks every few minutes, except inside the (from, to) periods in skip."""
        t = begin
        while t <= end:
            if not any(a <= t <= b for a, b in skip):
                self.attempt('hourly', t, 'no_change')
            t += timedelta(minutes=step)


def bars_15m(skip=(), since=None):
    """Bar ends (UTC) the 15-minute stock lab should record between START (or `since`) and UNTIL."""
    return [label for label, _ in cov.expected_bars('stocks_15m', since or START, UNTIL) if label not in skip]


def stock_obs(labels, equity=25000.0, seen_after=2):
    return [(label, cov.utc(label) + timedelta(minutes=seen_after), equity, equity) for label in labels]


class ExpectedBars(unittest.TestCase):
    def test_full_regular_session_has_expected_bar_counts(self):
        day = datetime(2026, 9, 21, 13, 0, tzinfo=UTC), datetime(2026, 9, 22, 3, 0, tzinfo=UTC)
        self.assertEqual(len(cov.expected_bars('stocks_5m', *day)), 78)
        self.assertEqual(len(cov.expected_bars('stocks_15m', *day)), 26)
        listed = [label for label, _ in cov.expected_bars('hourly_listed', *day)]
        self.assertEqual(listed[0], '2026-09-21T14:30:00+00:00')     # 10:30 New York
        self.assertEqual(listed[-1], '2026-09-21T20:00:00+00:00')    # capped at the 16:00 close
        self.assertEqual(len(listed), 7)
        self.assertEqual(len(cov.expected_bars('core', *day)), 1)

    def test_holiday_and_weekend_expect_nothing_from_nyse_families(self):
        labor_day = datetime(2026, 9, 7, 13, 0, tzinfo=UTC), datetime(2026, 9, 8, 3, 0, tzinfo=UTC)
        weekend = datetime(2026, 9, 19, 13, 0, tzinfo=UTC), datetime(2026, 9, 20, 3, 0, tzinfo=UTC)
        for window in (labor_day, weekend):
            for kind in ('stocks_5m', 'stocks_15m', 'hourly_listed', 'core'):
                self.assertEqual(cov.expected_bars(kind, *window), [], (kind, window))
        self.assertEqual(len(cov.expected_bars('hourly_crypto', *weekend)), 14)  # crypto never closes

    def test_window_start_and_grace_bound_the_expected_set(self):
        first = cov.expected_bars('stocks_5m', START, UNTIL)[0][0]
        self.assertEqual(first, START.isoformat())                    # bar ending exactly at the window start
        self.assertNotIn(at(-5).isoformat(), [label for label, _ in cov.expected_bars('stocks_5m', START, UNTIL)])
        bar_end = datetime(2026, 9, 21, 15, 0, tzinfo=UTC)
        ready_plus_grace = bar_end + timedelta(minutes=2 + 5)
        labels = lambda until: [l for l, _ in cov.expected_bars('stocks_5m', START, until)]
        self.assertNotIn(bar_end.isoformat(), labels(ready_plus_grace - timedelta(seconds=1)))   # pending, not missed
        self.assertIn(bar_end.isoformat(), labels(ready_plus_grace))

    def test_calendar_limit_is_reported(self):
        self.assertTrue(cov.calendar_covered(START, UNTIL))
        self.assertFalse(cov.calendar_covered(datetime(2028, 1, 3, tzinfo=UTC), datetime(2028, 1, 4, tzinfo=UTC)))
        self.assertEqual(cov.expected_bars('stocks_5m', datetime(2028, 1, 3, 15, tzinfo=UTC), datetime(2028, 1, 4, tzinfo=UTC)), [])


class CoverageAndGaps(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.rt = Runtime(self.dir.name)

    def build(self, **kw):
        return cov.build(self.rt.root, since=START, until=UNTIL, now=UNTIL, **kw)

    def two_accounts(self, labels, strategy_labels=None):
        self.rt.book('stock-experiments-v1/stock-lab-v1-trend_15m.sqlite', stock_obs(strategy_labels or labels))
        self.rt.book('stock-experiments-v1/stock-lab-v1-buy_hold_15m.sqlite', stock_obs(labels))

    def test_complete_session_is_fully_covered_without_gaps(self):
        self.two_accounts(bars_15m())
        self.rt.heartbeat()
        fam = self.build()['families']['stocks_15m']
        self.assertEqual((fam['expected_bars'], fam['missing_bars'], fam['coverage_pct']), (25, 0, 100.0))
        self.assertEqual(fam['gaps'], [])
        # START (10:00 ET) opens the window mid-session: every due bar is recorded, but the session is only
        # clipped coverage so far, not a complete session (see test_coverage_semantics for complete sessions).
        self.assertEqual((fam['full_sessions'], fam['complete_sessions'], fam['partial_sessions']), (0, 0, 1))
        self.assertEqual(fam['sessions'][0]['status'], 'start_clipped')
        self.assertTrue(fam['sessions'][0]['covered_so_far_full'])
        self.assertEqual(self.build()['process']['silent_periods'], [])

    def test_missing_bars_are_attributed_to_provider_failure(self):
        labels = bars_15m()
        missing = labels[4:7]
        self.two_accounts([l for l in labels if l not in missing])
        self.rt.heartbeat()
        for label in missing:
            self.rt.attempt('stocks_15m', cov.utc(label) + timedelta(minutes=3), 'held', 'Held: PaperHold: Provider returned no bars')
        gap = self.build()['families']['stocks_15m']['gaps']
        self.assertEqual(len(gap), 1)
        self.assertEqual((gap[0]['missing_bars'], gap[0]['primary']), (3, 'provider_or_data_failure'))
        self.assertEqual(gap[0]['failed_checks'], {'held': 3})
        self.assertEqual(gap[0]['top_messages'][0]['message'], 'Held: PaperHold: Provider returned no bars')
        self.assertNotIn('collector_silent', gap[0]['causes'])

    def test_silence_is_attributed_to_the_collector_not_the_provider(self):
        labels = bars_15m()
        missing = labels[8:12]
        self.two_accounts([l for l in labels if l not in missing])
        begin, end = cov.utc(missing[0]) - timedelta(minutes=4), cov.utc(labels[12]) + timedelta(minutes=2)
        self.rt.heartbeat(skip=[(begin + timedelta(seconds=1), end - timedelta(seconds=1))])
        report = self.build()
        self.assertEqual(report['families']['stocks_15m']['gaps'][0]['primary'], 'collector_silent')
        silent = report['process']['silent_periods']
        self.assertEqual(len(silent), 1)
        self.assertGreater(silent[0]['minutes'], 50)
        self.assertIn('asleep or off', silent[0]['cause'])

    def test_accepted_checks_without_a_bar_are_not_blamed_on_a_failure(self):
        labels = bars_15m()
        missing = labels[2:3]
        self.two_accounts([l for l in labels if l not in missing])
        self.rt.heartbeat()
        self.rt.attempt('stocks_15m', cov.utc(missing[0]) + timedelta(minutes=3), 'no_change', 'No new bar')
        gap = self.build()['families']['stocks_15m']['gaps'][0]
        self.assertEqual(gap['primary'], 'checks_accepted_without_bar')

    def test_gap_with_no_attempts_at_all_says_so(self):
        labels = bars_15m()
        missing = labels[10:11]
        self.two_accounts([l for l in labels if l not in missing])
        self.rt.heartbeat()
        self.assertEqual(self.build()['families']['stocks_15m']['gaps'][0]['primary'], 'no_attempt_recorded')

    def test_interrupted_check_names_its_own_cause(self):
        self.two_accounts(bars_15m())
        gap_start, gap_end = at(120), at(200)
        self.rt.heartbeat(skip=[(gap_start + timedelta(seconds=1), gap_end - timedelta(seconds=1))])
        self.rt.attempt('hourly', gap_start, 'interrupted', 'The previous server process ended', finished=gap_end)
        report = self.build()
        self.assertEqual(len(report['process']['recorded_interrupted_checks']), 1)
        self.assertIn('interrupted', report['process']['silent_periods'][0]['cause'])

    def test_normal_cadence_and_backoff_are_not_silence(self):
        t = START
        for spacing in [5, 5, 6, 5]:
            self.rt.attempt('hourly', t, 'no_change')
            t += timedelta(minutes=spacing)
        for spacing in [10, 20, 30, 30]:      # consecutive failures back off to 30 minutes
            self.rt.attempt('hourly', t, 'error', 'Hourly refresh held (TypeError)')
            t += timedelta(minutes=spacing)
        self.rt.attempt('hourly', t, 'no_change')
        self.two_accounts(bars_15m())
        self.assertEqual(cov.silent_periods(self.rt_attempts(), START, t), [])

    def rt_attempts(self):
        return cov.load_attempts(self.rt.root)['attempts']

    def test_tail_silence_up_to_until_is_reported(self):
        self.rt.heartbeat(end=at(60))
        self.two_accounts(bars_15m())
        periods = self.build()['process']['silent_periods']
        self.assertEqual(len(periods), 1)
        self.assertIn('still silent', periods[0]['cause'])

    def test_observations_before_the_window_are_excluded_not_counted(self):
        labels = bars_15m()
        early = cov.utc('2026-09-18T19:45:00+00:00')
        obs = stock_obs(labels) + [('2026-09-18T19:45:00+00:00', early + timedelta(minutes=2), 25000.0, 25000.0)]
        self.rt.book('stock-experiments-v1/stock-lab-v1-buy_hold_15m.sqlite', obs)
        self.rt.heartbeat()
        fam = self.build()['families']['stocks_15m']
        self.assertEqual(fam['observed_before_window_excluded'], 1)
        self.assertEqual(fam['recorded_bars'], 25)

    def test_bar_recorded_late_is_flagged_but_not_missing(self):
        labels = bars_15m()
        obs = stock_obs(labels)
        slow = labels[5]
        obs[5] = (slow, cov.utc(slow) + timedelta(minutes=41), 25000.0, 25000.0)
        self.rt.book('stock-experiments-v1/stock-lab-v1-buy_hold_15m.sqlite', obs)
        self.rt.heartbeat()
        fam = self.build()['families']['stocks_15m']
        self.assertEqual(fam['missing_bars'], 0)
        self.assertEqual(fam['observation_latency_minutes']['late_count'], 1)
        self.assertEqual(fam['late_bars'][0]['bar'], slow)

    def test_held_hourly_accounts_show_their_reason_and_zero_coverage(self):
        expected = cov.expected_bars('hourly_listed', START, UNTIL)
        live = [(l, cov.utc(l) + timedelta(minutes=6), 1000.0, 1000.0) for l, _ in expected]
        self.rt.book('hourly-v1/AAPL__buy-hold.sqlite3', live)
        self.rt.book('hourly-v1/FXA__buy-hold.sqlite3', [])
        (self.rt.root / 'hourly-v1' / 'quality.json').write_text(json.dumps(
            {'observed_at': UNTIL.isoformat(), 'holds': {'FXA': 'Needs 200 valid completed hourly bars'}}), encoding='utf-8')
        self.rt.heartbeat()
        report = self.build()
        fam = report['families']['hourly_listed']
        self.assertEqual(fam['accounts_with_no_recorded_bar'], ['FXA buy-hold'])
        self.assertEqual(fam['accounts_below_family_coverage'][0]['held_reason'], 'Needs 200 valid completed hourly bars')
        self.assertEqual(report['held_accounts']['hourly_quality']['holds'], {'FXA': 'Needs 200 valid completed hourly bars'})

    def test_unreadable_account_is_flagged_not_fatal(self):
        self.two_accounts(bars_15m())
        (self.rt.root / 'stock-experiments-v1' / 'stock-lab-v1-mean_reversion_15m.sqlite').write_bytes(b'not a database' * 50)
        self.rt.heartbeat()
        fam = self.build()['families']['stocks_15m']
        self.assertIn('mean_reversion 15m', fam['accounts_with_no_recorded_bar'])
        self.assertEqual(fam['recorded_bars'], 25)

    def test_unsupported_calendar_year_is_noted_and_nothing_is_judged(self):
        self.two_accounts([])
        self.rt.heartbeat(begin=datetime(2028, 1, 3, 15, tzinfo=UTC), end=datetime(2028, 1, 3, 20, tzinfo=UTC))
        report = cov.build(self.rt.root, since=datetime(2028, 1, 3, 15, tzinfo=UTC),
                           until=datetime(2028, 1, 3, 21, tzinfo=UTC), now=datetime(2028, 1, 3, 21, tzinfo=UTC))
        self.assertFalse(report['window']['calendar_covered'])
        self.assertEqual(report['families']['stocks_15m']['expected_bars'], 0)
        self.assertTrue(any('calendar' in n for n in report['notes']))


class CostsAndExposure(unittest.TestCase):
    def test_only_window_fills_count_and_exposure_is_cash_based(self):
        with tempfile.TemporaryDirectory() as tmp:
            rt = Runtime(tmp)
            labels = bars_15m()
            obs = [(l, cov.utc(l) + timedelta(minutes=2), 1000.0, 1000.0 if i < 10 else 250.0) for i, l in enumerate(labels)]
            fills = [(labels[10], 'SPY', 1.0, 750.0, 0.45), ('2026-09-18T15:00:00+00:00', 'SPY', 1.0, 700.0, 9.99)]
            rt.book('stock-experiments-v1/stock-lab-v1-trend_15m.sqlite', obs + [
                ('2026-09-18T15:00:00+00:00', cov.utc('2026-09-18T15:02:00+00:00'), 1000.0, 1000.0)], fills)
            econ = cov.build(rt.root, since=START, until=UNTIL, now=UNTIL, )['families']['stocks_15m']['economics']['trend 15m']
            self.assertEqual((econ['fills'], econ['fees_paid']), (1, 0.45))
            self.assertAlmostEqual(econ['mean_exposure_pct'], 100 * 0.75 * 15 / 25, places=1)
            self.assertEqual(econ['max_exposure_pct'], 75.0)
            self.assertAlmostEqual(econ['bars_invested_pct'], 60.0)


class BasisLabels(unittest.TestCase):
    """Whole-window economics must never read as the costs or exposure of the matched return."""

    def test_economics_and_comparisons_declare_their_different_bases(self):
        with tempfile.TemporaryDirectory() as tmp:
            rt = Runtime(tmp)
            labels = bars_15m()
            rt.book('stock-experiments-v1/stock-lab-v1-trend_15m.sqlite', stock_obs(labels[:10] + labels[14:]))
            rt.book('stock-experiments-v1/stock-lab-v1-buy_hold_15m.sqlite', stock_obs(labels))
            rt.heartbeat()
            report = cov.build(rt.root, since=START, until=UNTIL, now=UNTIL)
            econ = report['families']['stocks_15m']['economics']['trend 15m']
            self.assertTrue(econ['basis'].startswith('whole_window'))
            self.assertIn('Not the costs or exposure of the matched return alone', econ['basis'])
            comparison = report['comparisons'][0]
            self.assertEqual(comparison['return_basis'], 'matched_contiguous_runs_only')
            self.assertTrue(comparison['fills_basis'].startswith('whole_window'))
            self.assertEqual(comparison['segments'], 2)                       # the gap splits the matched runs; economics span it
            self.assertEqual(econ['observations'], len(labels) - 4)
            text = cov.render(report)
            self.assertIn('WHOLE-WINDOW COSTS AND EXPOSURE', text)
            self.assertIn('NOT limited to the matched intervals', text)
            self.assertIn('returns use matched bars only', text)


class MatchedComparison(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.rt = Runtime(self.dir.name)

    def pair(self, strategy_equity, reference_equity, since=START):
        labels = bars_15m(since=since)
        def obs(values):
            return [(l, cov.utc(l) + timedelta(minutes=2), v, v) for l, v in zip(labels, values) if v is not None]
        self.rt.book('stock-experiments-v1/stock-lab-v1-trend_15m.sqlite', obs(strategy_equity))
        self.rt.book('stock-experiments-v1/stock-lab-v1-buy_hold_15m.sqlite', obs(reference_equity))
        self.rt.heartbeat()
        comparisons = cov.build(self.rt.root, since=since, until=UNTIL, now=UNTIL)['comparisons']
        self.assertEqual(len(comparisons), 1)
        return comparisons[0]

    def test_returns_are_compounded_over_matched_runs_and_skip_the_gap(self):
        strategy = [100, 110, 121, None, 500, 550, 605] + [605] * 18
        reference = [100, 100, 100, 100, 200, 200, 200] + [200] * 18
        result = self.pair(strategy, reference)
        self.assertEqual(result['matched_bars'], 24)   # the strategy missed one bar the reference recorded
        self.assertEqual(result['reference_only_bars'], 1)
        self.assertEqual(result['segments'], 2)
        # (121/100) * (605/500) - 1 = 46.41 %; the 121 -> 500 move across the gap is excluded.
        self.assertAlmostEqual(result['descriptive_strategy_return_pct'], 46.41, places=2)
        self.assertAlmostEqual(result['descriptive_reference_return_pct'], 0.0, places=2)

    def test_small_samples_are_reported_as_insufficient_with_reasons(self):
        result = self.pair([100 + i for i in range(25)], [100] * 25)
        self.assertEqual(result['verdict'], 'insufficient_sample')
        reasons = ' '.join(result['insufficient_because'])
        self.assertIn('sessions 0/20', reasons)           # the only session is clipped by START, so it is observed but not complete
        self.assertIn('1 observed', reasons)
        self.assertIn('intervals 24/200', reasons)
        self.assertIn('fills 0/10', reasons)

    def test_meeting_thresholds_still_is_not_a_performance_claim(self):
        with patch.object(cov, 'MIN_SESSIONS', 1), patch.object(cov, 'MIN_FILLS', 0), \
                patch.dict(cov.KINDS['stocks_15m'], {'min_intervals': 10}):
            # A window opening at 09:00 ET holds the whole 26-bar session, so it can count toward the gate.
            result = self.pair([100 + i for i in range(26)], [100] * 26, since=datetime(2026, 9, 21, 13, 0, tzinfo=UTC))
        self.assertEqual(result['complete_matched_sessions'], 1)
        self.assertEqual(result['verdict'], 'sample_thresholds_met_descriptive_only')
        self.assertEqual(result['insufficient_because'], [])

    def test_report_has_no_projection_or_annualised_fields(self):
        self.pair([100 + i for i in range(25)], [100] * 25)
        keys = set()

        def walk(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    keys.add(str(k).lower())
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)
        walk(cov.build(self.rt.root, since=START, until=UNTIL, now=UNTIL))
        for word in ('annual', 'project', 'forecast', 'expected_return', 'cagr', 'sharpe'):
            self.assertFalse([k for k in keys if word in k], word)
        self.assertTrue(all(k.startswith('descriptive_') for k in keys if k.endswith('_return_pct')))

    def test_no_common_bars_gives_no_return(self):
        labels = bars_15m()
        self.rt.book('stock-experiments-v1/stock-lab-v1-trend_15m.sqlite', stock_obs(labels[:5]))
        self.rt.book('stock-experiments-v1/stock-lab-v1-buy_hold_15m.sqlite', stock_obs(labels[10:15]))
        self.rt.heartbeat()
        result = cov.build(self.rt.root, since=START, until=UNTIL, now=UNTIL)['comparisons'][0]
        self.assertEqual(result['matched_bars'], 0)
        self.assertIsNone(result['descriptive_difference_pp'])
        self.assertEqual(result['verdict'], 'insufficient_sample')


class ReadOnlyAndCli(unittest.TestCase):
    def snapshot(self, root):
        return {str(p.relative_to(root)): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
                for p in sorted(Path(root).rglob('*')) if p.is_file()}

    def test_report_writes_nothing_and_creates_no_journal_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            rt = Runtime(tmp)
            rt.book('stock-experiments-v1/stock-lab-v1-trend_15m.sqlite', stock_obs(bars_15m()))
            rt.book('stock-experiments-v1/stock-lab-v1-buy_hold_15m.sqlite', stock_obs(bars_15m()))
            rt.heartbeat()
            before = self.snapshot(tmp)
            report = cov.build(rt.root, since=START, until=UNTIL, now=UNTIL)
            cov.render(report)
            self.assertEqual(self.snapshot(tmp), before)

    def test_database_handles_reject_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Runtime(tmp).book('paper.sqlite3', [])
            con = base.read_only(path)
            with self.assertRaises(sqlite3.OperationalError):
                con.execute('INSERT INTO events(at,message) VALUES(1,2)')
            con.close()

    def test_cli_emits_json_and_rejects_a_missing_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            rt = Runtime(tmp)
            rt.book('stock-experiments-v1/stock-lab-v1-buy_hold_15m.sqlite', stock_obs(bars_15m()))
            rt.heartbeat()
            script = str(ROOT / 'scripts' / 'coverage_report.py')
            out = subprocess.run([sys.executable, '-B', script, '--runtime', tmp, '--since', START.isoformat(),
                                  '--until', UNTIL.isoformat(), '--json'], capture_output=True, text=True, timeout=120)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(json.loads(out.stdout)['families']['stocks_15m']['expected_bars'], 25)
            text = subprocess.run([sys.executable, '-B', script, '--runtime', tmp, '--since', START.isoformat(),
                                   '--until', UNTIL.isoformat()], capture_output=True, text=True, timeout=120)
            self.assertIn('COVERAGE', text.stdout)
            missing = subprocess.run([sys.executable, '-B', script, '--runtime', tmp + '-missing'],
                                     capture_output=True, text=True, timeout=120)
            self.assertNotEqual(missing.returncode, 0)

    def test_empty_runtime_asks_for_a_window_instead_of_guessing(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                cov.build(Path(tmp))


class BaselineReportRegressions(unittest.TestCase):
    """Defects found in scripts/collection_report.py during the September 28 audit."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.rt = Runtime(self.dir.name)

    def test_since_with_a_local_offset_is_compared_in_utc(self):
        self.rt.attempt('core', datetime(2026, 9, 28, 23, 0, tzinfo=UTC), 'new')
        self.rt.attempt('core', datetime(2026, 9, 29, 1, 0, tzinfo=UTC), 'new')
        since = base.aware('2026-09-28T20:00:00-04:00')          # 2026-09-29T00:00Z
        self.assertEqual(since.isoformat(), '2026-09-29T00:00:00+00:00')
        attempts = base.collector_report(self.rt.root, since)['attempts']
        self.assertEqual([(a['family'], a['n']) for a in attempts], [('core', 1)])

    def test_naive_since_is_taken_as_utc(self):
        self.assertEqual(base.aware('2026-09-29T00:00:00').isoformat(), '2026-09-29T00:00:00+00:00')

    def test_dead_process_with_running_status_is_flagged_stale(self):
        long_ago = datetime.now(UTC) - timedelta(hours=3)
        self.rt.store.begin(4242, long_ago)           # status 'running', heartbeat three hours old
        process = base.collector_report(self.rt.root, None)['process']
        self.assertTrue(process['heartbeat_stale'])
        self.rt.store.heartbeat(datetime.now(UTC))
        self.assertFalse(base.collector_report(self.rt.root, None)['process']['heartbeat_stale'])

    def test_unreadable_account_is_counted_instead_of_silently_skipped(self):
        self.rt.book('paper.sqlite3', [('2026-09-21', START, 1000.0, 0.0)])
        bad = self.rt.root / 'hourly-v1' / 'AAA__trend.sqlite3'
        bad.parent.mkdir(parents=True)
        bad.write_bytes(b'garbage' * 100)
        report = base.family_report(self.rt.root, ['hourly-v1/*__*.sqlite3'], None)
        self.assertEqual((report['accounts'], report['unreadable_accounts'], report['observations']), (1, 1, 0))
        self.assertEqual(base.family_report(self.rt.root, ['paper.sqlite3'], None)['unreadable_accounts'], 0)

    def test_empty_collector_database_does_not_crash_the_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'collector.sqlite3').write_bytes(b'')
            result = base.collector_report(Path(tmp), None)
            self.assertFalse(result['installed'])
            self.assertIn('unreadable', result)


if __name__ == '__main__':
    unittest.main()
