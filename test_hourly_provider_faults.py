"""Hourly lab under a failing provider: synthetic frames, fake provider, temporary stores, no network.

Evidence behind these tests (live PAPER collector log, read-only): 19 hourly checks stored only
'Hourly refresh held (TypeError)'. The traceback was swallowed by Controller.run_hourly. A symbol
whose Yahoo request fails reaches the lab as an all-NaN column (total outage: empty columns), which
completed_prices() could not handle for a listed instrument, so one failed symbol aborted the whole
cycle, including every symbol after it (crypto is last).
"""
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sys
import tempfile
import traceback
import types
import unittest
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd

import collector as col
import market_lab
from collector import Collector
from paper_book import PaperBook
import trading_app
from trading_app import Controller, fault_site

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'scripts'))
import coverage_report as cov      # noqa: E402

NOW = datetime(2026, 10, 7, 20, 10, tzinfo=timezone.utc)      # Wednesday 16:10 New York, the 15:30 bar just ended
ORDER = ['AAPL', 'SPY', 'BTC-USD']                           # a failing listed symbol sits before the symbols after it


def listed_index():
    sessions = [d for d in pd.bdate_range('2026-08-24', '2026-10-07') if d != pd.Timestamp('2026-09-07')]
    starts = [pd.Timestamp(f'{d.date()}T{h:02d}:30:00', tz='America/New_York') for d in sessions for h in range(9, 16)]
    return pd.DatetimeIndex(starts).tz_convert('UTC')


def provider_frame(failed=(), now=NOW):
    """What fetch_hourly returns: union calendar, one column per symbol that the provider answered."""
    listed = listed_index()
    crypto = pd.date_range(end=pd.Timestamp(now).floor('h') - pd.Timedelta(hours=1), periods=240, freq='h')
    raw = pd.DataFrame({'AAPL': 100. + np.arange(len(listed)) * .01, 'SPY': 400. + np.arange(len(listed)) * .02},
                       index=listed).reindex(listed.union(crypto))
    raw['BTC-USD'] = pd.Series(100. + np.arange(240), index=crypto)
    for symbol in failed:       # yfinance: a symbol whose request raised is an all-NaN column
        raw[symbol] = np.nan
    return raw


def total_outage_frame():
    """yfinance 1.7 with every request failing: the listed columns exist and have no rows; no crypto column."""
    empty = pd.DatetimeIndex([], tz='UTC')
    return pd.DataFrame({'AAPL': pd.Series([], index=empty, dtype=float), 'SPY': pd.Series([], index=empty, dtype=float)})


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class CompletedPricesEmptyInput(unittest.TestCase):
    def test_a_listed_series_with_nothing_usable_is_empty_not_a_typeerror(self):
        utc_empty = pd.Series([], index=pd.DatetimeIndex([], tz='UTC'), dtype=float)       # failed symbol after dropna()
        weekend = pd.Series([10.], index=pd.DatetimeIndex(['2026-10-03T15:00Z']))          # every bar outside the session
        for name, series in {'empty': utc_empty, 'only out-of-session bars': weekend}.items():
            for crypto in (False, True):
                with self.subTest(case=name, crypto=crypto):
                    result = market_lab.completed_prices(series, crypto, NOW)
                    self.assertEqual(len(result), 0 if name == 'empty' or not crypto else 1)
                    self.assertEqual(str(result.index.tz), 'UTC')
                    self.assertTrue(pd.api.types.is_float_dtype(result))

    def test_bars_that_exist_are_completed_exactly_as_before(self):
        # Guard against the fix touching the normal path: start-labelled 15:30 bar ends 16:00 New York.
        s = pd.Series([100., 101.], index=pd.DatetimeIndex(['2026-10-07T18:30:00Z', '2026-10-07T19:30:00Z']))
        result = market_lab.completed_prices(s, False, NOW)
        self.assertEqual(list(result.index), [pd.Timestamp('2026-10-07T19:30:00Z'), pd.Timestamp('2026-10-07T20:00:00Z')])
        self.assertEqual(list(result), [100., 101.])


class LabCycleUnderProviderFaults(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        assets = {s: dict(market_lab.ASSETS[s]) for s in ORDER}
        patcher = patch.object(market_lab, 'ASSETS', assets)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.lab = market_lab.MarketLab(Path(self.temp.name) / 'hourly-v1')
        self.lab.clock = Clock(NOW)

    def observations(self, key):
        return self.lab.books[key].status()['observation_count']

    def holds(self):
        return json.loads((self.lab.root / 'quality.json').read_text(encoding='utf-8'))['holds']

    def test_one_failed_listed_symbol_does_not_abort_the_symbols_after_it(self):
        self.lab.cycle(provider_frame(failed=['AAPL']), NOW)
        # The failed symbol is held with the existing "provider gave nothing" reason and records nothing ...
        self.assertEqual(self.holds(), {'AAPL': 'No source data'})
        self.assertEqual(self.observations('AAPL__buy-hold'), 0)
        self.assertFalse((self.lab.root / 'observed' / 'AAPL.csv').exists())
        # ... while the listed symbol and the crypto symbol after it are processed as usual.
        self.assertEqual(self.observations('SPY__buy-hold'), 1)
        self.assertEqual(self.observations('SPY__trend'), 1)
        self.assertEqual(self.observations('BTC-USD__buy-hold'), 1)
        self.assertEqual(self.observations('BTC-USD__trend'), 1)

    def test_the_failed_symbol_records_again_once_the_provider_answers(self):
        self.lab.cycle(provider_frame(failed=['AAPL']), NOW)
        later = NOW + timedelta(minutes=5)
        self.lab.cycle(provider_frame(), later)
        self.assertEqual(self.holds(), {})
        self.assertEqual(self.observations('AAPL__buy-hold'), 1)
        self.assertEqual(self.observations('SPY__buy-hold'), 1)       # the same completed bar is not recorded twice

    def test_healthy_provider_is_unchanged(self):
        message = self.lab.cycle(provider_frame(), NOW)
        self.assertEqual(self.holds(), {})
        self.assertEqual(message, '7 experiment observations processed; 0 assets held.')


class HourlyHarness(unittest.TestCase):
    """Controller.run_hourly and the real Collector scheduler with a fake provider (no tests of its own)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        assets = {s: dict(market_lab.ASSETS[s]) for s in ORDER}
        patcher = patch.object(market_lab, 'ASSETS', assets)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.clock = Clock(NOW)
        self.frame = provider_frame
        self.calls = []
        patch_fetch = patch.object(market_lab, 'fetch_hourly', self._fetch)
        patch_fetch.start()
        self.addCleanup(patch_fetch.stop)
        self.lab = market_lab.MarketLab(root / 'hourly-v1')
        self.lab.clock = self.clock
        self.controller = Controller(PaperBook(root / 'paper.sqlite3'), lab=self.lab)
        families = {'hourly': col.build_families(self.controller)['hourly']}
        self.collector = Collector(self.controller, root / 'collector.sqlite3', clock=self.clock, families=families)
        self.collector.begin()
        self.addCleanup(lambda: self.collector.stop(timeout=5))

    def _fetch(self, crypto_days=60):
        self.calls.append(self.clock())
        return self.frame()

    def tick(self):
        self.collector.tick()
        self.assertTrue(self.collector.join(10))
        return self.collector.store.family('hourly')


class HourlyOutcomesUnderProviderFaults(HourlyHarness):
    def test_a_total_outage_is_a_held_check_not_an_accepted_one(self):
        self.frame = total_outage_frame
        kind, message = self.controller.run_hourly()
        self.assertEqual(kind, 'held')
        self.assertIn('Provider returned no source data', message)
        self.assertEqual(cov.failure_class(message), 'data_rejected')
        holds = json.loads((self.lab.root / 'quality.json').read_text(encoding='utf-8'))['holds']
        self.assertEqual(holds, {s: 'No source data' for s in ORDER})
        self.assertTrue(all(b.status()['observation_count'] == 0 for b in self.lab.books.values()))

    def test_a_partial_failure_is_an_accepted_check_that_records_the_rest(self):
        self.frame = lambda: provider_frame(failed=['AAPL'])
        kind, message = self.controller.run_hourly()
        self.assertEqual(kind, 'ok')
        self.assertEqual(message, '5 experiment observations processed; 1 assets held.')

    def test_outage_keeps_the_scheduler_backoff_and_recovery_records_the_bar(self):
        self.frame = total_outage_frame
        state = self.tick()
        self.assertIn(state['last_outcome'], col.FAILURE)
        self.assertEqual(state['consecutive_failures'], 1)
        # One failure doubles the spacing to 10 minutes, exactly as for any other failed hourly check.
        self.clock.now += timedelta(minutes=9)
        self.tick()
        self.assertEqual(len(self.calls), 1)
        self.clock.now += timedelta(minutes=2)
        self.frame = provider_frame
        state = self.tick()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual((state['last_outcome'], state['consecutive_failures']), ('new', 0))
        self.assertEqual(state['new_observations'], 7)
        self.assertEqual(len([a for a in self.collector.store.attempts(10, 'hourly')]), 2)


class HourlyFaultDiagnostics(HourlyHarness):
    """An unexpected exception still holds the check, and now names where it was raised (bounded, no free text)."""

    def test_an_unexpected_exception_names_class_and_project_location_only(self):
        secret = 'https://provider.example/v8?apikey=SECRET123'
        with patch.object(market_lab, 'target_series', side_effect=TypeError(secret)):
            kind, message = self.controller.run_hourly()
        self.assertEqual(kind, 'error')
        self.assertTrue(message.startswith('Hourly refresh held (TypeError); previous records preserved.'), message)
        self.assertIn('market_lab.py:', message)
        self.assertIn(' cycle', message)
        self.assertNotIn('SECRET123', message)                     # the exception text is never stored
        self.assertNotIn('provider.example', message)
        self.assertIsNone(re.search(r'[A-Za-z]:[\\/]|[\\/]', message), message)   # file names only, no directories
        self.assertLessEqual(len(message), 240)
        self.assertEqual(self.lab.message, message)
        self.assertEqual(cov.failure_class(message), 'unclassified')    # the reporting parser still reads the class

    def test_an_exception_with_no_traceback_has_no_site(self):
        self.assertEqual(fault_site(ValueError('never raised')), '')
        # A pseudo file name such as <string> is not project code, whatever the working directory is.
        try:
            exec(compile('raise TypeError(1)', '<string>', 'exec'))
        except TypeError as exc:
            self.assertNotIn('<string>', fault_site(exc))
            self.assertIn('test_hourly_provider_faults.py:', fault_site(exc))     # the real project frame is kept

    def test_a_fault_raised_outside_project_code_is_located_at_its_project_caller(self):
        broken = pd.DataFrame({'AAPL': ['x']}, index=pd.DatetimeIndex(['2026-10-07T18:30:00Z']))   # object column
        self.frame = lambda: broken
        kind, message = self.controller.run_hourly()
        self.assertEqual(kind, 'error')
        self.assertIn('Hourly refresh held (', message)
        self.assertIn('market_lab.py:', message)
        self.assertIn(' completed_prices', message)      # innermost project frame, not a pandas internal
        self.assertNotIn('site-packages', message)


ORIGINAL = 'Hourly refresh held (TypeError); previous records preserved.'


def raiser(name='boom', filename='market_lab.py', rename=None):
    """A callable that raises TypeError from a synthetic project file: absolute path in the project directory."""
    code = compile(f'def {name}(*args):\n    raise TypeError("https://provider.example/?apikey=SECRET123")\n',
                   str(trading_app.ROOT / filename), 'exec')
    scope = {}
    exec(code, scope)
    function = scope[name]
    if rename:
        function.__code__ = function.__code__.replace(co_name=rename)
    return function


def raised(function):
    try:
        function()
    except TypeError as exc:
        return exc


class BrokenPath(type(Path())):
    def resolve(self, *args, **kwargs):
        raise OSError('stub: cannot resolve')


class HourlyFaultDiagnosticsAreBestEffort(HourlyHarness):
    """A failure while locating the fault must never replace, hide or change the original error outcome."""

    def run_with(self, function, **patches):
        with ExitStack() as stack:
            stack.enter_context(patch.object(market_lab, 'target_series', side_effect=function))
            for name, value in patches.items():
                stack.enter_context(patch.object(trading_app, name, value))
            return self.controller.run_hourly()

    def assert_original_outcome(self, result):
        kind, message = result
        self.assertEqual((kind, message), ('error', ORIGINAL))
        self.assertEqual(self.lab.message, ORIGINAL)

    def test_a_failing_traceback_extraction_keeps_the_original_outcome_and_message(self):
        def broken_extract(tb):
            raise RuntimeError('stub: cannot read the traceback')
        result = self.run_with(raiser(), traceback=types.SimpleNamespace(extract_tb=broken_extract))
        self.assert_original_outcome(result)
        self.assertTrue(fault_site(raised(raiser())).startswith('market_lab.py:2 boom'))      # the same fault, unpatched, is located

    def test_a_failing_path_resolution_keeps_the_original_outcome_and_message(self):
        self.assert_original_outcome(self.run_with(raiser(), Path=BrokenPath))
        with patch.object(trading_app, 'Path', BrokenPath):
            self.assertEqual(fault_site(raised(raiser())), '')

    def test_a_path_constructor_failure_and_an_odd_frame_do_not_escape(self):
        self.assert_original_outcome(self.run_with(raiser(), Path=Mock(side_effect=OSError('stub'))))
        odd = [traceback.FrameSummary(str(trading_app.ROOT / 'market_lab.py'), None, 'cycle')]      # no line number
        with patch.object(trading_app.traceback, 'extract_tb', return_value=odd):
            self.assertEqual(fault_site(raised(raiser())), 'market_lab.py:0 cycle')
        with patch.object(trading_app.traceback, 'extract_tb', return_value=[object()]):          # not a frame at all
            self.assertEqual(fault_site(raised(raiser())), '')

    def test_the_location_never_exceeds_the_character_limit(self):
        self.assertEqual(trading_app.FAULT_SITE_CHARS, 160)
        long_file = 'f' * 300 + '.py'
        for function in (raiser(name='x' * 400, filename=long_file), raiser(name='short', filename=long_file),
                         raiser(name='x' * 400)):
            site = fault_site(raised(function))
            self.assertTrue(0 < len(site) <= 160, len(site))
            self.assertNotRegex(site, r'[\\/]')
        kind, message = self.run_with(raiser(name='x' * 400, filename=long_file))
        self.assertEqual(kind, 'error')
        self.assertTrue(message.startswith(ORIGINAL + ' Raised at '))
        self.assertLessEqual(len(message), len(ORIGINAL) + len(' Raised at .') + 160)
        self.assertNotIn('SECRET123', message)
        self.assertNotIn('provider.example', message)

    def test_whole_frames_are_kept_while_they_fit_and_the_rest_dropped(self):
        def level(depth, name):
            if depth == 0:
                raise TypeError('x')
            return level(depth - 1, name)
        exc = None
        try:
            level(5, 'x')
        except TypeError as caught:
            exc = caught
        site = fault_site(exc)                                  # test file frames only: three levels, each short
        self.assertEqual(site.count(' <- '), 2)
        self.assertEqual(len(site.split(' <- ')), 3)
        self.assertTrue(all(part.startswith('test_hourly_provider_faults.py:') for part in site.split(' <- ')))
        tight = fault_site(exc, limit=70)                       # a smaller limit drops whole later frames, never cuts one mid-frame
        self.assertLessEqual(len(tight), 70)
        self.assertEqual(tight, ' <- '.join(site.split(' <- ')[:len(tight.split(' <- '))]))
        self.assertEqual(fault_site(exc, limit=10), site[:10])   # a first frame alone over the limit is cut

    def test_separator_and_control_characters_in_names_are_neutralised(self):
        site = fault_site(raised(raiser(rename='evil\nname http://p.example/?k=SECRET\\x')))
        self.assertTrue(site.startswith('market_lab.py:'), site)
        self.assertNotRegex(site, r'[\n\r\t/\\=]')
        self.assertEqual(site.count(' '), 1 + 3 * site.count(' <- '))      # only the separator spaces: one per frame, two per ' <- '

    def test_a_clean_diagnostic_still_reports_the_normal_case(self):
        kind, message = self.run_with(raiser())
        self.assertEqual(kind, 'error')
        self.assertRegex(message, r'^Hourly refresh held \(TypeError\); previous records preserved\. Raised at market_lab\.py:\d+ boom <- ')


if __name__ == '__main__':
    unittest.main()
