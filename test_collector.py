"""Background collector: synthetic prices, temporary SQLite, no network, no browser.

Every family (core, hourly, active 15m, stock 5m/15m) runs through the same code
it uses in production; only the provider functions and the clock are replaced.
"""
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import Request, build_opener, ProxyHandler

import numpy as np
import pandas as pd

import collector as col
import market_lab
from active_experiment import ActiveExperiment
from collector import Collector, InstanceLock
from paper_book import PaperBook, PaperHold
from stock_experiments import BASKET, StockExperiments
from trading_app import Controller, make_server
from trading_engine import DataUnavailable

ROOT = Path(__file__).resolve().parent
MONDAY = datetime(2026, 9, 21, 19, 3, tzinfo=timezone.utc)   # 15:03 New York, regular session
OPENER = build_opener(ProxyHandler({}))


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, **delta):
        self.now += timedelta(**delta)


def stock_frame(minutes, now):
    end = pd.Timestamp(now - timedelta(minutes=2)).floor(f'{minutes}min')
    starts = pd.date_range(end=end - pd.Timedelta(minutes=minutes), periods=21, freq=f'{minutes}min')
    return pd.DataFrame({s: [100.0 + i for i in range(21)] for s in BASKET}, index=starts)


def crypto_15m(now):
    index = pd.date_range(end=pd.Timestamp(now).floor('15min'), periods=220, freq='15min')
    values = 100 + np.arange(220) * .2 + np.sin(np.arange(220))
    return {t: pd.Series(values * (i + 1), index=index) for i, t in enumerate(['BTC-USD', 'ETH-USD'])}


def hourly_raw(now):
    index = pd.date_range(end=pd.Timestamp(now).floor('h') - pd.Timedelta(hours=1), periods=260, freq='h')
    return pd.DataFrame({'BTC-USD': 100 + np.arange(260) * .1 + np.sin(np.arange(260) / 5)}, index=index)


def core_snapshot(now, asof='2026-09-18'):
    return {'asof': asof, 'fetched_at': now.isoformat(), 'target_weights': {'AAA': 1.0},
            'prices': {'AAA': 100.0, 'SPY': 400.0, 'QQQ': 300.0, 'AGG': 100.0, 'BIL': 90.0}}


class Paper:
    """All five families on disposable books, as main() wires them."""

    def __init__(self, root, clock):
        self.root, self.clock = Path(root), clock
        self.calls = Counter()
        self.core_fn = lambda: core_snapshot(self.clock())
        self.hourly_fn = lambda: hourly_raw(self.clock())
        self.active_fn = lambda: crypto_15m(self.clock())
        self.stock_fn = lambda minutes: stock_frame(minutes, self.clock())
        self.open()

    def open(self):
        self.book = PaperBook(self.root / 'paper.sqlite3')
        self.lab = market_lab.MarketLab(self.root / 'hourly-v1')
        self.lab.clock = self.clock
        self.lab.pause(self.book.is_paused())
        self.active = ActiveExperiment(self.root / 'active-15m-v1', fetch=self._active, clock=self.clock)
        self.stocks = StockExperiments(self.root / 'stock-experiments-v1', fetcher=self._stocks, clock=self.clock)
        if self.book.is_paused():
            self.active.pause(True)
            self.stocks.pause(True)
        self.controller = Controller(self.book, fetch=self._core, lab=self.lab, active=self.active, stocks=self.stocks)
        self.collector = Collector(self.controller, self.root / 'collector.sqlite3', clock=self.clock)
        self.controller.collector = self.collector
        self.collector.begin()
        return self

    def _core(self):
        self.calls['core'] += 1
        return self.core_fn()

    def hourly(self, crypto_days=60):
        self.calls['hourly'] += 1
        return self.hourly_fn()

    def _active(self):
        self.calls['active'] += 1
        return self.active_fn()

    def _stocks(self, minutes):
        self.calls[f'stocks_{minutes}m'] += 1
        return self.stock_fn(minutes)

    def tick(self):
        self.collector.tick()
        self.assert_joined()

    def assert_joined(self):
        if not self.collector.join(10):
            raise AssertionError('collection workers did not finish')

    def family(self, name):
        return self.collector.store.family(name)

    def shown(self, name):
        return next(f for f in self.collector.status()['families'] if f['name'] == name)

    def global_pause(self, value):
        # Same effect as POST /api/pause.
        for target in (self.book, self.active, self.stocks, self.lab):
            target.pause(value)


class CollectorCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.clock = Clock(MONDAY)
        assets = {'BTC-USD': dict(market_lab.ASSETS['BTC-USD'])}
        patcher = patch.object(market_lab, 'ASSETS', assets)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.paper = Paper(self.temp.name, self.clock)
        fetch = patch.object(market_lab, 'fetch_hourly', lambda crypto_days=60: self.paper.hourly(crypto_days))
        fetch.start()
        self.addCleanup(fetch.stop)

    def tearDown(self):
        self.paper.collector.stop(timeout=5)
        self.temp.cleanup()

    def serve(self):
        server = make_server(self.paper.controller, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(5)))
        with OPENER.open(base) as response:
            token = re.search("const token='([^']+)'", response.read().decode())[1]

        def post(path, value):
            request = Request(base + path, data=json.dumps(value).encode(), headers={
                'Content-Type': 'application/json', 'X-Paper-Token': token, 'Origin': base})
            with OPENER.open(request) as response:
                return json.load(response)

        def get(path):
            with OPENER.open(base + path) as response:
                return json.load(response)
        return get, post


class NoBrowserScheduling(CollectorCase):
    FAMILIES = ('core', 'hourly', 'active', 'stocks_5m', 'stocks_15m')

    def test_timer_thread_collects_every_family_without_any_request(self):
        paper = self.paper
        paper.collector.tick_seconds = 0.05
        paper.collector.start()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not all(
                paper.family(n).get('last_outcome') for n in self.FAMILIES):
            time.sleep(0.05)
        status = paper.collector.status()
        self.assertTrue(status['running'])
        self.assertEqual(status['pid'], status['this_pid'])
        paper.collector.stop(timeout=5)
        for name in self.FAMILIES:
            state = paper.family(name)
            self.assertEqual(state['last_outcome'], 'new', (name, state['last_message']))
            self.assertEqual(state['last_source'], 'scheduler')
            self.assertGreater(state['new_observations'], 0)
        # The same completed bars are never requested twice by later ticks.
        self.assertEqual(paper.calls, Counter({n: 1 for n in self.FAMILIES}))
        self.assertEqual(paper.book.status()['observation_count'], 1)
        self.assertEqual(paper.active.book.status()['observation_count'], 1)
        self.assertEqual(paper.lab.books['BTC-USD__trend'].status()['observation_count'], 1)
        self.assertTrue(all(paper.stocks.books[k].status()['observation_count'] == 1 for k in paper.stocks.books))
        self.assertEqual(paper.collector.store.collector()['status'], 'stopped')
        self.assertTrue(all(f['state'] == 'up_to_date' for f in status['families']),
                        [(f['name'], f['state'], f['detail']) for f in status['families']])

    def test_checks_are_distinguished_from_new_observations(self):
        paper = self.paper
        paper.tick()
        # 30 seconds later nothing is due: every family already holds its latest bar.
        self.clock.advance(seconds=30)
        paper.tick()
        self.assertEqual(sum(paper.calls.values()), 5)
        # Six minutes later: a new stock/active slot exists, hourly is due again,
        # and the core is up to date for Friday's session, so it is not fetched.
        self.clock.advance(minutes=6)
        # Providers have not published anything newer than the recorded bars.
        paper.stock_fn = lambda minutes: stock_frame(minutes, MONDAY)
        paper.hourly_fn = lambda: hourly_raw(MONDAY - timedelta(hours=1))
        paper.tick()
        self.assertEqual(paper.calls['core'], 1)
        self.assertEqual(paper.calls['hourly'], 2)
        self.assertEqual(paper.calls['stocks_5m'], 2)
        self.assertEqual(paper.calls['stocks_15m'], 1)     # 15m slot unchanged at 19:09
        hourly, stock = paper.family('hourly'), paper.family('stocks_5m')
        self.assertEqual((hourly['last_outcome'], hourly['checks'], hourly['new_observations']), ('no_change', 2, 3))
        self.assertEqual((stock['last_outcome'], stock['checks'], stock['new_observations']), ('no_change', 2, 4))
        self.assertIn('already recorded', paper.stocks.status()['interval_status']['5'])
        self.assertTrue(all(paper.stocks.books[k].status()['observation_count'] == 1 for k in paper.stocks.books))
        # Fresh data a minute later is a new observation.
        self.clock.advance(minutes=1)
        paper.stock_fn = lambda minutes: stock_frame(minutes, self.clock())
        paper.tick()
        stock = paper.family('stocks_5m')
        self.assertEqual((stock['last_outcome'], stock['checks'], stock['new_observations']), ('new', 3, 8))
        attempts = paper.collector.store.attempts(100, 'stocks_5m')
        self.assertEqual([a['outcome'] for a in attempts], ['new', 'no_change', 'new'])


class OverlappingRequests(CollectorCase):
    def test_browser_manual_and_scheduler_share_locks_and_due_rules(self):
        paper = self.paper
        release, started = threading.Event(), threading.Event()
        def slow_active():
            started.set()
            release.wait(10)
            return crypto_15m(self.clock())
        paper.active_fn = slow_active
        get, post = self.serve()
        paper.collector.tick()
        self.assertTrue(started.wait(5))
        # Manual button and legacy request path both see the running check.
        answer = post('/api/active-cycle', {})
        self.assertFalse(answer['started'])
        self.assertIn('already running', answer['reason'])
        self.assertFalse(paper.active.request_cycle())
        self.assertTrue(get('/api/active')['busy'])
        release.set()
        paper.assert_joined()
        # The bar is recorded, so a manual check does not refetch it.
        answer = post('/api/active-cycle', {})
        self.assertFalse(answer['started'])
        self.assertIn('already recorded', answer['reason'])
        # An old open tab's automatic heartbeat follows the scheduler's rules.
        self.assertFalse(post('/api/cycle', {'force': False})['started'])
        answer = post('/api/stock-cycle', {})
        self.assertFalse(answer['started'])
        self.assertTrue(all('already recorded' in r for r in answer['reasons'].values()))
        self.assertEqual(paper.calls, Counter({'core': 1, 'hourly': 1, 'active': 1, 'stocks_5m': 1, 'stocks_15m': 1}))
        # An explicit Check now may re-check the core (idempotent: no new observation).
        self.clock.advance(seconds=5)
        self.assertTrue(post('/api/cycle', {'force': True})['started'])
        paper.assert_joined()
        core = paper.family('core')
        self.assertEqual((core['last_source'], core['last_outcome'], core['new_observations']), ('manual', 'no_change', 1))
        self.assertEqual(paper.book.status()['observation_count'], 1)
        # Check now reaches every family, not only the first one that starts
        # (19:17: new 5m, 15m and 15-minute crypto bars are due).
        self.clock.advance(minutes=14)
        self.assertTrue(post('/api/cycle', {'force': True})['started'])
        paper.assert_joined()
        for name in ('core', 'hourly', 'active', 'stocks_5m', 'stocks_15m'):
            self.assertEqual(paper.family(name)['last_source'], 'manual', name)
        # Health is readable without starting anything.
        calls = sum(paper.calls.values())
        health = get('/api/collector')
        self.assertEqual({f['name'] for f in health['families']},
                         {'core', 'hourly', 'active', 'stocks_5m', 'stocks_15m'})
        self.assertEqual(sum(paper.calls.values()), calls)

    def test_concurrent_requests_start_one_worker(self):
        paper = self.paper
        release = threading.Event()
        paper.stock_fn = lambda minutes: (release.wait(10), stock_frame(minutes, self.clock()))[1]
        results = []
        threads = [threading.Thread(target=lambda s=s: results.append(paper.collector.request('stocks_5m', s)[0]))
                   for s in ('scheduler', 'manual', 'browser', 'manual')]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        release.set()
        paper.assert_joined()
        self.assertEqual(sorted(results), [False, False, False, True])
        self.assertEqual(paper.calls['stocks_5m'], 1)


class ProviderGate(CollectorCase):
    def test_yahoo_families_take_turns_when_yfinance_shares_state(self):
        paper = self.paper
        paper.collector.provider_gates = {'yahoo': threading.Lock()}   # as with yfinance < 1.x
        guard, inflight, peak = threading.Lock(), [0], [0]
        def tracked(inner):
            def call(*args):
                with guard:
                    inflight[0] += 1
                    peak[0] = max(peak[0], inflight[0])
                time.sleep(0.1)
                try:
                    return inner(*args)
                finally:
                    with guard:
                        inflight[0] -= 1
            return call
        paper.core_fn, paper.hourly_fn, paper.stock_fn = (tracked(paper.core_fn), tracked(paper.hourly_fn),
                                                         tracked(paper.stock_fn))
        paper.tick()
        self.assertEqual(peak[0], 1)
        for name in ('core', 'hourly', 'stocks_5m', 'stocks_15m', 'active'):
            self.assertEqual(paper.family(name)['last_outcome'], 'new', name)


class PauseAndRestart(CollectorCase):
    def test_global_pause_persists_across_restart_and_blocks_all_fetches(self):
        paper = self.paper
        paper.global_pause(True)
        paper.tick()
        self.assertEqual(sum(paper.calls.values()), 0)
        self.assertTrue(all(f['state'] == 'paused' for f in paper.collector.status()['families']))
        paper.collector.stop(timeout=1)
        paper.open()                                   # restart on the same runtime
        self.clock.advance(minutes=10)
        paper.tick()
        self.assertEqual(sum(paper.calls.values()), 0)
        self.assertTrue(paper.book.is_paused() and paper.active.book.is_paused() and paper.stocks.is_paused())

    def test_family_pause_is_respected_and_survives_restart(self):
        paper = self.paper
        paper.stocks.pause(True)
        paper.active.pause(True)
        paper.tick()
        self.assertEqual(set(paper.calls), {'core', 'hourly'})
        paper.collector.stop(timeout=1)
        paper.open()
        self.clock.advance(minutes=10)
        paper.tick()
        self.assertNotIn('active', paper.calls)
        self.assertNotIn('stocks_5m', paper.calls)
        shown = paper.shown('stocks_5m')
        self.assertEqual((shown['state'], shown['pause_reason']), ('paused', 'Stock experiments paused.'))
        paper.stocks.pause(False)
        paper.tick()
        self.assertEqual(paper.family('stocks_5m')['last_outcome'], 'new')
        self.assertNotIn('active', paper.calls)

    def test_restart_keeps_throttle_and_marks_interrupted_runs(self):
        paper = self.paper
        paper.tick()
        # Simulate a process that died mid-check: its run marker is left behind.
        paper.collector.store.start('hourly', 'scheduler', self.clock())
        paper.collector.stop(timeout=1)
        paper.open()
        self.assertEqual(paper.collector.interrupted, ['hourly'])
        self.assertIsNone(paper.family('hourly')['running_since'])
        self.assertEqual(paper.collector.store.attempts(1, 'hourly')[0]['outcome'], 'interrupted')
        self.clock.advance(seconds=20)
        paper.tick()
        # Already-recorded bars are not refetched after the restart.
        self.assertEqual(paper.calls['active'], 1)
        self.assertEqual(paper.calls['stocks_5m'], 1)
        self.assertEqual(paper.calls['core'], 1)

    def test_pause_during_request_records_nothing(self):
        paper = self.paper
        def pause_mid_request(minutes):
            paper.global_pause(True)
            return stock_frame(minutes, self.clock())
        paper.stock_fn = pause_mid_request
        paper.tick()
        self.assertTrue(all(paper.stocks.books[k].status()['observation_count'] == 0 for k in paper.stocks.books))
        self.assertEqual(paper.family('stocks_5m')['last_outcome'], 'paused')

    def test_stop_while_running_is_recorded_and_refuses_new_work(self):
        paper = self.paper
        release = threading.Event()
        paper.active_fn = lambda: (release.wait(10), crypto_15m(self.clock()))[1]
        paper.collector.tick()
        unfinished = paper.collector.stop(timeout=2)
        self.assertIn('active', unfinished)
        row = paper.collector.store.collector()
        self.assertEqual(row['status'], 'stopped')
        self.assertIn('unfinished checks:', row['note'])
        self.assertIn('active', row['note'])
        self.assertEqual(paper.collector.request('core', 'manual', force=True), (False, 'Collector is stopping.'))
        release.set()
        paper.assert_joined()


class FailuresAndHungRequests(CollectorCase):
    def test_failing_family_does_not_stop_others(self):
        paper = self.paper
        def broken():
            raise ConnectionError('provider down')
        paper.core_fn = broken
        paper.hourly_fn = broken
        paper.tick()
        core, hourly = paper.family('core'), paper.family('hourly')
        self.assertEqual((core['last_outcome'], core['consecutive_failures']), ('error', 1))
        self.assertEqual((hourly['last_outcome'], hourly['consecutive_failures']), ('error', 1))
        for name in ('active', 'stocks_5m', 'stocks_15m'):
            self.assertEqual(paper.family(name)['last_outcome'], 'new')
        self.assertEqual(paper.shown('core')['state'], 'failing')
        # Retry spacing backs off (10 minutes after one failure), then recovers.
        self.clock.advance(minutes=6)
        paper.tick()
        self.assertEqual(paper.calls['core'], 1)
        self.clock.advance(minutes=5)
        paper.core_fn = lambda: core_snapshot(self.clock())
        paper.tick()
        core = paper.family('core')
        self.assertEqual((core['last_outcome'], core['consecutive_failures']), ('new', 0))
        self.assertEqual(paper.shown('core')['state'], 'up_to_date')

    def test_hung_request_is_visible_and_never_duplicated(self):
        paper = self.paper
        release = threading.Event()
        paper.stock_fn = lambda m: (release.wait(10) if m == 5 else None, stock_frame(m, self.clock()))[1]
        exits = []
        paper.collector.hung_exit_seconds = 15 * 60
        paper.collector.on_hung_exit = exits.append
        paper.collector.tick()
        # The 15-minute cadence is not blocked by the stuck 5-minute request.
        deadline = time.monotonic() + 10
        while paper.family('stocks_15m').get('last_outcome') != 'new' and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(paper.family('stocks_15m')['last_outcome'], 'new')
        for _ in range(2):
            self.clock.advance(minutes=6)
            paper.collector.tick()
        shown = paper.shown('stocks_5m')
        self.assertEqual(shown['state'], 'hung')
        self.assertIn('has not returned', shown['detail'])
        self.assertEqual(paper.calls['stocks_5m'], 1)
        self.assertEqual(exits, [])
        self.clock.advance(minutes=4)
        paper.collector.tick()
        paper.collector.tick()
        self.assertEqual(len(exits), 1)
        self.assertIn('stocks_5m', exits[0])
        release.set()
        paper.assert_joined()
        self.assertIsNone(paper.family('stocks_5m')['hung_since'])
        self.assertNotEqual(paper.shown('stocks_5m')['state'], 'hung')


class RejectedData(CollectorCase):
    def records(self):
        books = [self.paper.book, self.paper.active.book, self.paper.active.reference,
                 *self.paper.stocks.books.values(), *self.paper.lab.books.values()]
        return [(b.record()['observations'], b.record()['trades'], b.status()['cash'], b.status()['holdings'])
                for b in books]

    def test_rejected_data_leaves_every_record_unchanged(self):
        paper = self.paper
        paper.tick()
        before = self.records()
        self.clock.advance(minutes=16)
        def bad_stocks(minutes):
            frame = stock_frame(minutes, self.clock())
            frame.iloc[-1, 0] = float('nan')
            return frame
        def gapped_active():
            raw = crypto_15m(self.clock())
            raw['BTC-USD'] = raw['BTC-USD'].drop(raw['BTC-USD'].index[-5])
            return raw
        def unavailable():
            raise DataUnavailable('Market data is stale; paper orders are held.')
        def invalid_hourly():
            raw = hourly_raw(self.clock())
            raw.iloc[-3:, 0] = -1.0
            return raw
        paper.stock_fn, paper.active_fn, paper.core_fn, paper.hourly_fn = bad_stocks, gapped_active, unavailable, invalid_hourly
        paper.tick()                                   # 15:19 New York: intraday families due
        for name in ('active', 'stocks_5m', 'stocks_15m'):
            self.assertEqual(paper.family(name)['last_outcome'], 'held', name)
        # The lab holds invalid assets itself; the check completes without a new bar.
        self.assertEqual(paper.family('hourly')['last_outcome'], 'no_change')
        self.clock.now = datetime(2026, 9, 21, 20, 20, tzinfo=timezone.utc)   # 16:20: Monday's daily bar due
        paper.tick()
        self.assertEqual(paper.calls['core'], 2)
        self.assertEqual(paper.family('core')['last_outcome'], 'held')
        self.assertEqual(paper.shown('core')['state'], 'failing')
        self.assertEqual(self.records(), before)


class ReadOnlyReport(CollectorCase):
    def test_report_counts_actual_new_observations_without_writing(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('collection_report', ROOT / 'scripts' / 'collection_report.py')
        report = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(report)
        paper = self.paper
        paper.tick()
        runtime = Path(self.temp.name)
        files = {p: p.stat().st_mtime_ns for p in runtime.rglob('*.sqlite*')}
        since = MONDAY - timedelta(seconds=1)
        counts = {name: report.family_report(runtime, patterns, since) for name, patterns in report.FAMILIES.items()}
        self.assertEqual({k: v['observed_after_since'] for k, v in counts.items()},
                         {'core': 1, 'core benchmarks': 4, 'hourly': 3, 'active': 2, 'stocks_5m': 4, 'stocks_15m': 4})
        self.assertEqual(report.family_report(runtime, report.FAMILIES['core'], MONDAY)['observed_after_since'], 0)
        summary = report.collector_report(runtime, since)
        self.assertEqual({a['family'] for a in summary['attempts'] if a['outcome'] == 'new'},
                         {'core', 'hourly', 'active', 'stocks_5m', 'stocks_15m'})
        self.assertEqual({p: p.stat().st_mtime_ns for p in runtime.rglob('*.sqlite*')}, files)


class MarketClosedVersusOverdue(unittest.TestCase):
    def at(self, stamp):
        return datetime.fromisoformat(stamp)

    def test_stock_states(self):
        health, due = col.stock_health(5), col.stock_due(5)
        sunday = self.at('2026-09-20T18:00:00+00:00')
        self.assertEqual(health(sunday, {'latest_bar': '2026-09-18T20:00:00+00:00'})[0], 'market_closed')
        self.assertFalse(due(sunday, {}, 'scheduler', False)[0])
        thanksgiving = self.at('2026-11-26T16:00:00+00:00')
        self.assertEqual(health(thanksgiving, {})[0], 'market_closed')
        self.assertEqual(health(MONDAY, {'latest_bar': '2026-09-21T19:00:00+00:00'})[0], 'up_to_date')
        self.assertEqual(health(MONDAY, {'latest_bar': '2026-09-21T18:55:00+00:00'})[0], 'awaiting')
        self.assertEqual(health(MONDAY, {'latest_bar': '2026-09-21T18:30:00+00:00'})[0], 'overdue')
        self.assertEqual(health(MONDAY, {})[0], 'never')
        before_first_bar = self.at('2026-09-21T13:33:00+00:00')
        self.assertEqual(health(before_first_bar, {'latest_bar': '2026-09-18T20:00:00+00:00'})[0], 'awaiting')
        self.assertFalse(due(before_first_bar, {}, 'scheduler', False)[0])
        # First bar of a new session is due at 09:37; yesterday's close is not "overdue" yet.
        first = self.at('2026-09-21T13:37:30+00:00')
        self.assertEqual(col.stock_expected_bar(first, 5), self.at('2026-09-21T13:35:00+00:00'))
        self.assertEqual(health(first, {'latest_bar': '2026-09-18T20:00:00+00:00'})[0], 'awaiting')
        # Early close: the 13:00 bar is final; after the observation window the market is closed.
        early = self.at('2026-11-27T18:03:00+00:00')
        self.assertEqual(col.stock_expected_bar(early, 15), self.at('2026-11-27T18:00:00+00:00'))
        self.assertEqual(health(self.at('2026-11-27T18:30:00+00:00'), {})[0], 'market_closed')
        self.assertEqual(col.stock_health(15)(self.at('2028-03-01T15:00:00+00:00'), {})[0], 'unknown')

    def test_core_states(self):
        saturday = self.at('2026-09-26T15:00:00+00:00')
        state, detail = col.core_health(saturday, {'latest_bar': '2026-09-25'})
        self.assertEqual(state, 'up_to_date')
        self.assertIn('Market closed today', detail)
        self.assertFalse(col.core_due(saturday, {'latest_bar': '2026-09-25'}, 'scheduler', False)[0])
        monday_evening = self.at('2026-09-21T20:30:00+00:00')   # 16:30 New York
        self.assertEqual(col.core_health(monday_evening, {'latest_bar': '2026-09-18'})[0], 'awaiting')
        self.assertTrue(col.core_due(monday_evening, {'latest_bar': '2026-09-18'}, 'scheduler', False)[0])
        self.assertEqual(col.core_health(self.at('2026-09-22T00:30:00+00:00'), {'latest_bar': '2026-09-18'})[0], 'overdue')
        self.assertEqual(col.core_health(self.at('2026-09-22T21:00:00+00:00'), {'latest_bar': '2026-09-18'})[0], 'overdue')
        # During Monday's session Friday is the latest usable session.
        self.assertEqual(col.core_expected_session(MONDAY).isoformat(), '2026-09-18')
        self.assertEqual(col.core_expected_session(self.at('2026-11-26T22:00:00+00:00')).isoformat(), '2026-11-25')
        self.assertEqual(col.core_health(self.at('2028-01-05T22:00:00+00:00'), {'latest_bar': '2027-12-31'})[0], 'unknown')
        self.assertTrue(col.core_due(self.at('2028-01-05T22:00:00+00:00'), {'latest_bar': '2027-12-31'}, 'scheduler', False)[0])

    def test_crypto_states(self):
        self.assertEqual(col.active_health(MONDAY, {'latest_bar': '2026-09-21T19:00:00+00:00'})[0], 'up_to_date')
        self.assertEqual(col.active_health(MONDAY, {'latest_bar': '2026-09-21T18:15:00+00:00'})[0], 'overdue')
        sunday = self.at('2026-09-20T18:10:00+00:00')
        self.assertEqual(col.hourly_health(sunday, {'extra': json.dumps({'groups': {
            'crypto': '2026-09-20T18:00:00+00:00', 'listed': '2026-09-18T20:00:00+00:00'}})})[0], 'up_to_date')
        self.assertEqual(col.hourly_health(sunday, {'extra': json.dumps({'groups': {
            'crypto': '2026-09-20T14:00:00+00:00'}})})[0], 'overdue')
        self.assertEqual(col.hourly_health(MONDAY, {'extra': json.dumps({'groups': {
            'crypto': '2026-09-21T18:00:00+00:00', 'listed': '2026-09-21T15:30:00+00:00'}})})[0], 'overdue')


class SingleProcess(unittest.TestCase):
    def test_instance_lock_is_exclusive(self):
        with tempfile.TemporaryDirectory() as temp:
            first, second = InstanceLock(Path(temp) / 'x.lock'), InstanceLock(Path(temp) / 'x.lock')
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            first.release()
            self.assertTrue(second.acquire())
            second.release()

    def test_port_cannot_be_shared(self):
        with tempfile.TemporaryDirectory() as temp:
            controller = Controller(PaperBook(Path(temp) / 'p.sqlite3'))
            server = make_server(controller, 0)
            try:
                with self.assertRaises(OSError):
                    make_server(controller, server.server_port)
            finally:
                server.server_close()

    def test_second_server_process_exits_without_touching_the_first(self):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / 'runtime' / 'paper.sqlite3'
            args = [sys.executable, '-B', str(ROOT / 'main.py'), '--no-browser', '--no-scheduler',
                    '--no-research-helpers', '--port', str(port), '--state', str(state)]
            first = subprocess.Popen(args, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 60
                status = None
                while time.monotonic() < deadline and status is None:
                    try:
                        with OPENER.open(f'http://127.0.0.1:{port}/api/collector', timeout=2) as r:
                            status = json.load(r)
                    except OSError:
                        time.sleep(0.25)
                self.assertIsNotNone(status, 'first server did not start')
                self.assertEqual((status['enabled'], status['pid']), (False, first.pid))
                same_runtime = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=60)
                self.assertEqual(same_runtime.returncode, 0)
                self.assertIn('already owns', same_runtime.stderr)
                other = [*args[:-1], str(Path(temp) / 'other' / 'paper.sqlite3')]
                same_port = subprocess.run(other, cwd=ROOT, capture_output=True, text=True, timeout=60)
                self.assertEqual(same_port.returncode, 4)
                self.assertIn('unavailable', same_port.stderr)
                self.assertIsNone(first.poll())
            finally:
                first.terminate()
                first.communicate(timeout=10)


if __name__ == '__main__':
    unittest.main()
