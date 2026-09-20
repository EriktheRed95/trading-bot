"""Tests for the advisory research companion layer.

Offline only: synthetic OHLCV panels, injected order books and disposable
databases. No network, no runtime state, no brokerage. The central assertion is
negative: with the helpers attached, the frozen core book produces exactly the
record it produces without them.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import tempfile
import threading
import unittest
from urllib.request import urlopen
import numpy as np
import pandas as pd

from unittest.mock import patch
from paper_book import PaperBook, PaperHold, HOLD_MESSAGE, session_close, aware
from trading_app import Controller, make_server
import research_roles as roles
from research_intake import (DailyIntake, BookIntake, align, completed_sessions, parse_book,
                            IntakeUnavailable, FIELDS)
from research_ledger import ResearchLedger
from research_desk import (ResearchDesk, SHADOW_BASELINE, SHADOW_FILTERED, DEFAULT_CRYPTO_PRODUCTS,
                           WallClock, StepClock)

SYMBOLS = ['AAA', 'BBB', 'CCC']
SESSION = '2026-09-11'
OBSERVED = '2026-09-11T20:30:00+00:00'
NOW = datetime(2026, 9, 11, 20, 30, tzinfo=timezone.utc)


def build_panel(symbols=SYMBOLS, periods=320, end=SESSION, volume=2_000_000.0, price=100.0):
    """A clean, boring panel: gentle uptrend, steady volume, no data faults."""
    index = pd.bdate_range(end=end, periods=periods)
    steps = np.arange(len(index))
    panel = {}
    closes = pd.DataFrame({s: price * (1 + 0.0004 * steps + 0.01 * np.sin(steps / (7 + i)))
                           for i, s in enumerate(symbols)}, index=index)
    panel['Close'] = closes
    panel['Open'] = closes.shift(1).bfill() * 1.001
    panel['High'] = closes * 1.01
    panel['Low'] = closes * 0.99
    panel['Volume'] = pd.DataFrame({s: np.full(len(index), volume) for s in symbols}, index=index)
    return {field: panel[field].astype(float) for field in FIELDS}


def core_snapshot(session=SESSION, observed=OBSERVED, weights=None, panel=None, symbols=SYMBOLS):
    """A core-shaped snapshot: same keys the frozen engine emits."""
    panel = panel if panel is not None else build_panel(symbols)
    stamp = pd.Timestamp(session)
    # A core snapshot can name a session the research panel does not carry; the
    # fixture then prices it from the newest row it does have.
    closes = (panel['Close'].loc[stamp] if stamp in panel['Close'].index
              else panel['Close'].iloc[-1])
    return {'asof': session, 'fetched_at': observed, 'risk_on': True,
            'strategy': 'Strategy C / paper v1',
            'target_weights': {'AAA': 0.5, 'BBB': 0.5} if weights is None else weights,
            'prices': {s: float(closes[s]) for s in panel['Close'].columns},
            'eligible': len(symbols), 'excluded': []}


class FakeIntake:
    """Stands in for the cached public download. Counts loads; never fetches."""

    def __init__(self, panel, *, fetched_at=OBSERVED, cached=False, error=None):
        self.panel, self.error = panel, error
        self.meta = {'fetched_at': fetched_at, 'cached': cached, 'sha256': 'testsha',
                     'source': 'synthetic test panel'}
        self.loads = 0

    def load(self, symbols, now=None, require_session=None):
        self.loads += 1
        self.required = require_session
        if self.error:
            raise self.error
        return align(self.panel, symbols), dict(self.meta)


class FakeBooks:
    def __init__(self, books):
        self.books, self.loads = books, 0

    def load(self, products, now=None):
        self.loads += 1
        return dict(self.books)


class ManualClock:
    """A clock a test drives by hand, so a fetch can visibly take time."""

    def __init__(self, start):
        self.now = start
        self.reads = []

    def __call__(self):
        self.reads.append(self.now)
        return self.now

    def advance(self, **delta):
        self.now = self.now + timedelta(**delta)


class SlowIntake(FakeIntake):
    """A fetch that takes real time: loading advances the clock."""

    def __init__(self, panel, clock, minutes, **kwargs):
        super().__init__(panel, **kwargs)
        self.clock, self.minutes = clock, minutes

    def load(self, symbols, now=None, require_session=None):
        result = super().load(symbols, now=now, require_session=require_session)
        self.clock.advance(minutes=self.minutes)
        return result


def synthetic_book(mid=100.0, size=10.0, levels=60, error=None):
    if error:
        return {'error': error, 'fetched_at': OBSERVED, 'source': 'test',
                'depth_policy': 'test policy'}
    bids = [[f'{mid - 0.01 * (i + 1):.4f}', f'{size}', 1] for i in range(levels)]
    asks = [[f'{mid + 0.01 * (i + 1):.4f}', f'{size}', 1] for i in range(levels)]
    return parse_book({'bids': bids, 'asks': asks, 'sequence': 7,
                       'time': '2026-09-11T20:29:00Z'}, NOW)


def make_desk(root, *, panel=None, books=None, intake=None, symbols=SYMBOLS):
    panel = panel if panel is not None else build_panel(symbols)
    return ResearchDesk(root, universe=symbols,
                        intake=intake if intake is not None else FakeIntake(panel),
                        books=FakeBooks(books) if books is not None else None,
                        crypto_products=tuple(books) if books else (),
                        enable_books=bool(books))


class CoreBehaviourTests(unittest.TestCase):
    """The frozen strategy must not notice that the helpers exist."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def _sequence(self, panel):
        """Three sessions of the same synthetic snapshot, one plan and one fill."""
        sessions = [('2026-09-09', '2026-09-09T20:30:00+00:00'),
                    ('2026-09-10', '2026-09-10T20:30:00+00:00'),
                    (SESSION, OBSERVED)]
        return [core_snapshot(session, observed, panel=panel) for session, observed in sessions]

    def _run(self, with_desk, folder):
        panel = build_panel()
        root = self.root / folder
        book = PaperBook(root / 'paper.sqlite3')
        desk = make_desk(root / 'research', panel=panel) if with_desk else None
        outcomes = []
        for snapshot in self._sequence(panel):
            outcomes.append(book.cycle(snapshot))
            if desk:
                desk.observe(snapshot, now=datetime.fromisoformat(snapshot['fetched_at']))
        status = book.status()
        status.pop('events')
        return outcomes, book.record(), status, desk

    def test_core_record_is_identical_with_and_without_the_helpers(self):
        plain_out, plain_record, plain_status, _ = self._run(False, 'plain')
        desk_out, desk_record, desk_status, desk = self._run(True, 'withdesk')
        self.assertEqual(plain_out, desk_out)
        self.assertEqual(json.dumps(plain_record, sort_keys=True, default=str),
                         json.dumps(desk_record, sort_keys=True, default=str))
        self.assertEqual(json.dumps(plain_status, sort_keys=True, default=str),
                         json.dumps(desk_status, sort_keys=True, default=str))
        # And the helpers really did run over those same sessions.
        self.assertEqual(desk.ledger.counts()['sessions'], 3)

    def test_observing_does_not_touch_the_core_database_file(self):
        panel = build_panel()
        root = self.root / 'untouched'
        book = PaperBook(root / 'paper.sqlite3')
        snapshot = core_snapshot(panel=panel)
        book.cycle(snapshot)
        before = (root / 'paper.sqlite3').read_bytes()
        desk = make_desk(root / 'research', panel=panel)
        desk.observe(snapshot, now=NOW)
        self.assertEqual((root / 'paper.sqlite3').read_bytes(), before)
        # The desk's own files live somewhere else entirely.
        self.assertTrue((root / 'research' / 'research.sqlite3').exists())
        self.assertFalse((root / 'research' / 'paper.sqlite3').exists())

    def test_shadow_books_are_separate_files_and_do_not_enable_execution(self):
        desk = make_desk(self.root / 'separate')
        desk.observe(core_snapshot(), now=NOW)
        for name in (SHADOW_BASELINE, SHADOW_FILTERED):
            book = desk.shadow[name]
            self.assertTrue(book.path.exists())
            self.assertEqual(book.status()['mode'], 'PAPER ONLY')
            self.assertEqual(book.version, f'research-v1/{name}')
        # First observed session queues a target; nothing fills on the same bar.
        self.assertEqual(desk.shadow[SHADOW_BASELINE].status()['fill_count'], 0)
        self.assertIsNotNone(desk.shadow[SHADOW_BASELINE].status()['pending'])

    def test_controller_runs_the_desk_after_the_core_and_survives_its_failure(self):
        panel = build_panel()
        snapshot = core_snapshot(panel=panel)
        book = PaperBook(self.root / 'controller.sqlite3')

        class StubDesk:
            def __init__(self, fail):
                self.calls, self.fail, self.message = [], fail, ''

            def observe(self, snap, now=None):
                self.calls.append(snap)
                if self.fail:
                    raise RuntimeError('synthetic desk failure')

            def pause(self, value):
                pass

        for fail in (False, True):
            desk = StubDesk(fail)
            controller = Controller(book, fetch=lambda: snapshot, desk=desk)
            self.assertTrue(controller.request_cycle(True))
            with controller.lock:
                pass
            self.assertEqual(len(desk.calls), 1)
            self.assertEqual(desk.calls[0]['asof'], SESSION)
            self.assertIn('queued targets', controller.status()['outcome'])
            if fail:
                self.assertIn('Research helpers held', desk.message)

    def test_rejected_core_snapshot_never_reaches_the_desk(self):
        """A snapshot the core refused is not an observation.

        `book.cycle` raises PaperHold and changes no state, so the helpers must not
        advise on it or move a shadow book from it.
        """
        class StubDesk:
            def __init__(self):
                self.calls, self.message = [], ''

            def observe(self, snap, now=None):
                self.calls.append(snap)

            def pause(self, value):
                pass

        book = PaperBook(self.root / 'rejected.sqlite3')
        bad = core_snapshot(weights={'AAA': 2.0})          # sum above one
        with self.assertRaises(PaperHold):
            book.cycle(bad)
        desk = StubDesk()
        controller = Controller(book, fetch=lambda: bad, desk=desk)
        self.assertTrue(controller.request_cycle(True))
        with controller.lock:
            pass
        self.assertEqual(desk.calls, [])
        self.assertIn('did not accept a snapshot', desk.message)
        self.assertEqual(book.status()['observation_count'], 0)

    def test_already_processed_core_session_still_reaches_the_desk(self):
        """De-duplication is acceptance, not rejection.

        `book.cycle` returns 'Already processed this session' without raising. The
        snapshot is valid, so the helpers may still read that session.
        """
        class StubDesk:
            def __init__(self):
                self.calls, self.message = [], ''

            def observe(self, snap, now=None):
                self.calls.append(snap)

            def pause(self, value):
                pass

        book = PaperBook(self.root / 'dedupe.sqlite3')
        snapshot = core_snapshot()
        book.cycle(snapshot)
        desk = StubDesk()
        controller = Controller(book, fetch=lambda: snapshot, desk=desk)
        self.assertTrue(controller.request_cycle(True))
        with controller.lock:
            pass
        self.assertEqual(controller.status()['message'], 'Already processed this session')
        self.assertEqual(len(desk.calls), 1)

    def test_controller_without_a_desk_behaves_exactly_as_before(self):
        snapshot = core_snapshot()
        book = PaperBook(self.root / 'nodesk.sqlite3')
        controller = Controller(book, fetch=lambda: snapshot)
        self.assertIsNone(controller.desk)
        self.assertTrue(controller.request_cycle(True))
        with controller.lock:
            pass
        self.assertIn('queued targets', controller.status()['outcome'])

    def test_paused_desk_records_nothing_and_requests_nothing(self):
        intake = FakeIntake(build_panel())
        desk = make_desk(self.root / 'paused', intake=intake)
        desk.pause(True)
        message = desk.observe(core_snapshot(), now=NOW)
        self.assertIn('Paused', message)
        self.assertEqual(intake.loads, 0)
        self.assertEqual(desk.ledger.counts()['advisories'], 0)

    def test_held_core_snapshot_triggers_no_data_request(self):
        intake = FakeIntake(build_panel())
        desk = make_desk(self.root / 'held', intake=intake)
        message = desk.observe(None, now=NOW)
        self.assertIn('no completed snapshot', message)
        self.assertEqual(intake.loads, 0)
        self.assertEqual(desk.ledger.counts()['cycles'], 0)


class StaleAndMissingDataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_core_session_absent_from_research_snapshot_abstains_whole_cycle(self):
        panel = build_panel(end='2026-09-04')          # research data lags the strategy
        desk = make_desk(self.root / 'lag', panel=panel)
        message = desk.observe(core_snapshot(panel=build_panel()), now=NOW)
        self.assertIn('is not present in the research snapshot', message)
        self.assertEqual(desk.ledger.counts()['advisories'], 0)
        self.assertEqual(desk.ledger.counts()['cycles'], 0)
        # The refusal is recorded as a hold, which cannot be read as a reading.
        holds = desk.ledger.holds()
        self.assertEqual(len(holds), 1)
        self.assertEqual(holds[0]['session'], SESSION)
        self.assertIn('not present', holds[0]['reason'])
        # Once the data arrives, the same session is advised for the first time.
        desk.intake = FakeIntake(build_panel())
        desk.observe(core_snapshot(panel=build_panel()), now=NOW)
        self.assertGreater(desk.ledger.counts()['advisories'], 0)
        self.assertEqual(desk.ledger.counts()['holds'], 1)     # and the hold is still on record

    def test_session_that_cannot_have_closed_yet_is_refused(self):
        desk = make_desk(self.root / 'future')
        early = datetime(2026, 9, 11, 12, tzinfo=timezone.utc)   # 08:00 New York
        message = desk.observe(core_snapshot(), now=early)
        self.assertIn('cannot have completed', message)
        self.assertEqual(desk.ledger.counts()['advisories'], 0)

    def test_unavailable_intake_holds_and_preserves_earlier_advisories(self):
        panel = build_panel()
        desk = make_desk(self.root / 'outage', panel=panel)
        desk.observe(core_snapshot(), now=NOW)
        before = desk.ledger.counts()['advisories']
        self.assertGreater(before, 0)
        desk.intake = FakeIntake(panel, error=IntakeUnavailable('provider down'))
        message = desk.observe(core_snapshot('2026-09-14', '2026-09-14T20:30:00+00:00', panel=panel),
                               now=datetime(2026, 9, 14, 20, 30, tzinfo=timezone.utc))
        self.assertIn('research data intake is unavailable', message)
        self.assertEqual(desk.ledger.counts()['advisories'], before)

    def test_missing_volume_in_the_window_abstains_with_a_reason(self):
        panel = build_panel()
        panel['Volume'].iloc[-5, 0] = np.nan
        findings = roles.unusual_activity(panel, ['AAA'], SESSION, OBSERVED, 4.0)
        self.assertTrue(findings[0]['abstained'])
        self.assertIn('missing', findings[0]['abstention_reason'])
        self.assertIsNone(findings[0]['proposed_action'])

    def test_symbol_absent_and_session_absent_abstain_separately(self):
        panel = build_panel()
        missing = roles.unusual_activity(panel, ['ZZZ'], SESSION, OBSERVED, 4.0)[0]
        self.assertIn('not present in the research intake snapshot', missing['abstention_reason'])
        stale = roles.unusual_activity(panel, ['AAA'], '2026-09-18', OBSERVED, 4.0)[0]
        self.assertIn('no bar for the observed session', stale['abstention_reason'])

    def test_short_history_abstains_rather_than_shortening_the_window(self):
        panel = build_panel(periods=120)
        entry = roles.entry_quality(panel, ['AAA'], SESSION, OBSERVED, 4.0)[0]
        self.assertTrue(entry['abstained'])
        self.assertIn('needs 201 completed observations', entry['abstention_reason'])

    def test_completed_sessions_withholds_a_forming_bar(self):
        panel = build_panel()
        before_close = datetime(2026, 9, 11, 18, tzinfo=timezone.utc)   # 14:00 New York
        cut = completed_sessions(panel, before_close)
        self.assertEqual(str(cut['Close'].index[-1].date()), '2026-09-10')
        after_close = completed_sessions(panel, NOW)
        self.assertEqual(str(after_close['Close'].index[-1].date()), SESSION)

    def test_order_book_failure_reports_unavailable_without_inventing_depth(self):
        books = {'BTC-USD': synthetic_book(error='HTTPError: 503')}
        desk = make_desk(self.root / 'bookfail', books=books)
        desk.observe(core_snapshot(), now=NOW)
        rows = [a for a in desk.ledger.advisories() if a['symbol'] == 'BTC-USD']
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]['abstained'])
        self.assertIn('503', rows[0]['abstention_reason'])
        self.assertNotIn('bid_usd', json.dumps(rows[0]['evidence']))


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_advisories_and_digest_survive_a_reopen(self):
        desk = make_desk(self.root / 'persist')
        desk.observe(core_snapshot(), now=NOW)
        counts = desk.ledger.counts()
        reopened = ResearchLedger(self.root / 'persist' / 'research.sqlite3')
        self.assertEqual(reopened.counts()['advisories'], counts['advisories'])
        self.assertEqual(reopened.latest_session()['session'], SESSION)
        digest = reopened.digest()
        self.assertEqual(digest['session'], SESSION)
        self.assertIn('plain_language', digest)
        rows = reopened.advisories(SESSION)
        self.assertTrue(all(row['session'] == SESSION for row in rows))
        self.assertTrue(any(row['role'] == roles.ROLE_ACTIVITY for row in rows))

    def test_repeat_cycle_is_idempotent_and_the_first_reading_stands(self):
        panel = build_panel()
        desk = make_desk(self.root / 'idem', panel=panel)
        desk.observe(core_snapshot(panel=panel), now=NOW)
        first = desk.ledger.counts()
        original = {row['symbol']: row['evidence'].get('session_volume')
                    for row in desk.ledger.advisories(SESSION)
                    if row['role'] == roles.ROLE_ACTIVITY}
        # The same session is observed again, this time with different data. A
        # later read must not rewrite a recorded advisory, add new rows for it,
        # or spend a request finding that out.
        revised = build_panel()
        revised['Volume'].iloc[-1] = revised['Volume'].iloc[-1] * 9
        desk.intake = FakeIntake(revised)
        message = desk.observe(core_snapshot(panel=revised),
                               now=datetime(2026, 9, 11, 22, tzinfo=timezone.utc))
        self.assertIn('already recorded', message)
        self.assertEqual(desk.intake.loads, 0)
        self.assertEqual(desk.ledger.counts(), first)
        after = {row['symbol']: row['evidence'].get('session_volume')
                 for row in desk.ledger.advisories(SESSION)
                 if row['role'] == roles.ROLE_ACTIVITY}
        self.assertEqual(original, after)

    def test_backdated_session_is_refused(self):
        panel = build_panel()
        desk = make_desk(self.root / 'backdate', panel=panel)
        desk.observe(core_snapshot(panel=panel), now=NOW)
        message = desk.observe(core_snapshot('2026-09-04', '2026-09-04T20:30:00+00:00', panel=panel),
                               now=datetime(2026, 9, 14, 20, 30, tzinfo=timezone.utc))
        self.assertIn('older than the recorded session', message)
        self.assertEqual(desk.ledger.counts()['sessions'], 1)

    def test_ledger_version_change_refuses_to_open(self):
        path = self.root / 'version' / 'research.sqlite3'
        ResearchLedger(path)
        with self.assertRaises(ValueError):
            ResearchLedger(path, version='research-ledger-v2')

    def test_daily_intake_cache_avoids_a_second_request(self):
        calls = []

        def downloader(symbols):
            calls.append(tuple(symbols))
            return build_panel(list(symbols))

        intake = DailyIntake(self.root / 'cache', downloader=downloader, ttl_hours=4)
        first, meta = intake.load(SYMBOLS, now=NOW)
        self.assertFalse(meta['cached'])
        second, meta2 = intake.load(SYMBOLS, now=NOW)
        self.assertTrue(meta2['cached'])
        self.assertEqual(len(calls), 1)
        self.assertEqual(intake.requests, 1)
        pd.testing.assert_frame_equal(first['Close'], second['Close'])
        # An expired cache refetches; a different universe refetches.
        later = NOW.replace(day=12)
        intake.load(SYMBOLS, now=later)
        self.assertEqual(len(calls), 2)
        intake.load(SYMBOLS + ['DDD'], now=later)
        self.assertEqual(len(calls), 3)

    def test_required_session_refetches_a_lagging_cache_but_not_every_poll(self):
        calls = []

        def downloader(symbols):
            calls.append(tuple(symbols))
            return build_panel(list(symbols))       # always ends 2026-09-11

        intake = DailyIntake(self.root / 'lagcache', downloader=downloader, ttl_hours=4,
                             min_refetch_minutes=20)
        intake.load(SYMBOLS, now=NOW)
        self.assertEqual(len(calls), 1)
        wanted = pd.Timestamp('2026-09-14')         # a session the snapshot lacks
        # Inside the refetch floor the cache is reused and reported as lacking it.
        _, meta = intake.load(SYMBOLS, now=NOW.replace(minute=40), require_session=wanted)
        self.assertEqual(len(calls), 1)
        self.assertTrue(meta['cached'])
        self.assertFalse(meta['required_session_present'])
        # Past the floor it refreshes early, well inside the four-hour window.
        _, meta = intake.load(SYMBOLS, now=NOW.replace(hour=21, minute=5), require_session=wanted)
        self.assertEqual(len(calls), 2)
        self.assertFalse(meta['cached'])
        # A session the cache does carry never triggers a refetch.
        _, meta = intake.load(SYMBOLS, now=NOW.replace(hour=21, minute=30),
                              require_session=pd.Timestamp(SESSION))
        self.assertEqual(len(calls), 2)
        self.assertTrue(meta['cached'])
        self.assertTrue(meta['required_session_present'])

    def test_desk_asks_the_intake_for_the_core_session(self):
        panel = build_panel()
        desk = make_desk(self.root / 'required', panel=panel)
        desk.observe(core_snapshot(panel=panel), now=NOW)
        self.assertEqual(desk.intake.required, pd.Timestamp(SESSION))

    def test_book_intake_caches_and_bounds_fan_out(self):
        calls = []

        def getter(url, params=None, timeout=20):
            calls.append(url)
            return {'bids': [['99.9', '5', 1]], 'asks': [['100.1', '5', 1]],
                    'sequence': 1, 'time': '2026-09-11T20:29:00Z'}

        intake = BookIntake(self.root / 'books', getter=getter, ttl_minutes=50, max_products=2)
        out = intake.load(['BTC-USD', 'ETH-USD', 'SOL-USD'], now=NOW)
        self.assertEqual(len(calls), 2)
        self.assertIn('bounded to 2 order books', out['SOL-USD']['error'])
        again = intake.load(['BTC-USD', 'ETH-USD'], now=NOW)
        self.assertEqual(len(calls), 2)
        self.assertTrue(again['BTC-USD']['cached'])


class RoleReadingTests(unittest.TestCase):
    def test_unusual_volume_compares_against_strictly_prior_sessions(self):
        panel = build_panel()
        panel['Volume'].iloc[-1, 0] = 2_000_000.0 * 4
        finding = roles.unusual_activity(panel, ['AAA'], SESSION, OBSERVED, 4.0)[0]
        self.assertEqual(finding['verdict'], 'unusually heavy')
        self.assertAlmostEqual(finding['evidence']['ratio_to_median'], 4.0, places=6)
        self.assertEqual(finding['evidence']['comparison_sessions'], roles.VOLUME_WINDOW)
        self.assertEqual(finding['evidence']['prior_sessions_at_or_above'], 0)
        self.assertIn('4.0 times its median volume', finding['proposed_action'])
        # The observed session's own volume is excluded from its own median.
        self.assertAlmostEqual(finding['evidence']['trailing_median_volume'], 2_000_000.0)

    def test_thin_volume_reads_as_thin_and_ordinary_volume_proposes_nothing(self):
        panel = build_panel()
        panel['Volume'].iloc[-1, 0] = 2_000_000.0 * 0.2
        thin = roles.unusual_activity(panel, ['AAA'], SESSION, OBSERVED, 4.0)[0]
        self.assertEqual(thin['verdict'], 'unusually thin')
        ordinary = roles.unusual_activity(build_panel(), ['AAA'], SESSION, OBSERVED, 4.0)[0]
        self.assertEqual(ordinary['verdict'], 'ordinary')
        self.assertEqual(ordinary['proposed_action'], roles.NO_ACTION)

    def test_extended_close_is_flagged_and_a_mid_range_close_is_not(self):
        panel = build_panel()
        index = panel['Close'].index
        # A steep final leg puts the close at the top of its range and far above
        # the 200-session average, which is what "extended" describes.
        ramp = panel['Close']['AAA'].copy()
        ramp.iloc[-25:] = ramp.iloc[-26] * np.linspace(1.02, 1.45, 25)
        panel['Close']['AAA'] = ramp
        panel['High']['AAA'] = ramp * 1.005
        panel['Low']['AAA'] = ramp * 0.995
        panel['Open']['AAA'] = ramp.shift(1).bfill()
        finding = roles.entry_quality(panel, ['AAA'], SESSION, OBSERVED, 4.0)[0]
        self.assertEqual(finding['verdict'], 'extended')
        self.assertGreaterEqual(finding['evidence']['range_position_pct'], roles.EXTENDED_RANGE_PCT)
        self.assertGreaterEqual(finding['evidence']['pct_above_trailing_average'],
                                roles.EXTENDED_TREND_PCT)
        calm = roles.entry_quality(build_panel(), ['AAA'], SESSION, OBSERVED, 4.0)[0]
        self.assertEqual(calm['verdict'], 'inside its recent range')
        self.assertIsNotNone(calm['evidence']['overnight_gap_pct'])

    def test_signal_noise_catches_the_repository_own_failure_modes(self):
        stale = build_panel()
        stale['Close']['AAA'] = 100.0                      # every session prints no change
        stale['Open']['AAA'] = 100.0
        finding = roles.signal_noise(stale, ['AAA'], SESSION, OBSERVED, 4.0)[0]
        self.assertEqual(finding['verdict'], 'suspect')
        text = ' '.join(finding['evidence']['failures'])
        self.assertIn('print no change', text)
        self.assertIn('open exactly at the prior close', text)

        jump = build_panel()
        jump['Close'].iloc[-1, 0] = jump['Close'].iloc[-2, 0] * 3
        moved = roles.signal_noise(jump, ['AAA'], SESSION, OBSERVED, 4.0)[0]
        self.assertEqual(moved['verdict'], 'suspect')
        self.assertEqual(moved['evidence']['extreme_move_count'], 1)

        clean = roles.signal_noise(build_panel(), ['AAA'], SESSION, OBSERVED, 4.0)[0]
        self.assertEqual(clean['verdict'], 'usable')
        self.assertEqual(clean['evidence']['failures'], [])

    def test_cross_check_reports_a_disagreement_with_the_core_price(self):
        panel = build_panel()
        price = float(panel['Close'].loc[pd.Timestamp(SESSION), 'AAA'])
        agree = roles.signal_noise(panel, ['AAA'], SESSION, OBSERVED, 4.0,
                                   core_prices={'AAA': price})[0]
        self.assertEqual(agree['verdict'], 'usable')
        self.assertAlmostEqual(agree['evidence']['price_difference_pct'], 0.0, places=9)
        differ = roles.signal_noise(panel, ['AAA'], SESSION, OBSERVED, 4.0,
                                    core_prices={'AAA': price * 1.05})[0]
        self.assertEqual(differ['verdict'], 'suspect')
        self.assertIn('away from the price the strategy used', ' '.join(differ['evidence']['failures']))

    def test_listed_names_report_traded_dollars_and_never_a_depth_number(self):
        finding = roles.liquidity_depth(build_panel(), ['AAA'], SESSION, OBSERVED, 4.0)[0]
        self.assertEqual(finding['role'], roles.ROLE_LIQUIDITY)
        self.assertIn('median_dollar_volume', finding['evidence'])
        self.assertEqual(finding['evidence']['quoted_spread'], roles.EQUITY_SPREAD_UNAVAILABLE)
        self.assertEqual(finding['evidence']['visible_book_depth'], roles.EQUITY_DEPTH_UNAVAILABLE)
        for key in ('bid_usd', 'ask_usd', 'bands_bps', 'mid'):
            self.assertNotIn(key, finding['evidence'])

    def test_thin_traded_volume_proposes_a_size_review(self):
        panel = build_panel(volume=1_000.0)
        finding = roles.liquidity_depth(panel, ['AAA'], SESSION, OBSERVED, 4.0)[0]
        self.assertEqual(finding['verdict'], 'thin for this pool')
        self.assertIn('size review', finding['proposed_action'])

    def test_visible_depth_is_measured_from_the_book_and_marked_as_a_lower_bound(self):
        book = synthetic_book(mid=100.0, size=10.0, levels=60)
        measured = roles.visible_depth(book)
        self.assertAlmostEqual(measured['mid'], 100.0, places=6)
        self.assertAlmostEqual(measured['spread_bps'], 2.0, places=3)
        # 25bp of a 100 mid is 0.25, so levels at 0.01 steps give 25 per side.
        band = measured['bands_bps']['25']
        self.assertEqual(band['bid_levels'], 25)
        self.assertTrue(band['band_fully_covered'])
        self.assertAlmostEqual(band['bid_usd'], sum(p * s for p, s in book['bids'][:25]), places=6)
        # A book truncated inside the band is reported as a lower bound, not padded.
        shallow = roles.visible_depth(synthetic_book(levels=5))
        self.assertFalse(shallow['bands_bps']['25']['band_fully_covered'])

    def test_crossed_and_malformed_books_are_refused(self):
        with self.assertRaises(IntakeUnavailable):
            parse_book({'bids': [['101', '1', 1]], 'asks': [['100', '1', 1]]}, NOW)
        with self.assertRaises(IntakeUnavailable):
            parse_book({'bids': [], 'asks': [['100', '1', 1]]}, NOW)
        with self.assertRaises(IntakeUnavailable):
            parse_book({'nope': 1}, NOW)

    def test_no_confidence_score_anywhere_in_a_reading(self):
        panel = build_panel()
        findings = (roles.unusual_activity(panel, SYMBOLS, SESSION, OBSERVED, 4.0)
                    + roles.entry_quality(panel, SYMBOLS, SESSION, OBSERVED, 4.0)
                    + roles.signal_noise(panel, SYMBOLS, SESSION, OBSERVED, 4.0)
                    + roles.liquidity_depth(panel, SYMBOLS, SESSION, OBSERVED, 4.0,
                                            books={'BTC-USD': synthetic_book()}))
        blob = json.dumps(findings, default=str).lower()
        for word in ('confidence', 'probability', 'score', 'forecast', 'predict'):
            self.assertNotIn(word, blob)

    def test_abstention_never_defers_a_name(self):
        panel = build_panel()
        panel['Volume'].iloc[-3, 0] = np.nan
        findings = roles.unusual_activity(panel, ['AAA'], SESSION, OBSERVED, 4.0)
        self.assertTrue(findings[0]['abstained'])
        self.assertEqual(roles.deferral_symbols(findings), {})


class LookaheadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def _extended(self, panel):
        return panel

    def test_later_sessions_cannot_change_an_earlier_reading(self):
        panel = build_panel()
        future = build_panel(periods=325, end='2026-09-18')
        # Make the future violent so any leak would be obvious.
        future['Volume'].iloc[-4:] = 99_000_000.0
        future['Close'].iloc[-4:] = future['Close'].iloc[-5] * 2
        for field in FIELDS:
            future[field].loc[panel[field].index] = panel[field]
        args = (SESSION, OBSERVED, 4.0)
        for role in (roles.unusual_activity, roles.entry_quality, roles.signal_noise,
                     roles.liquidity_depth):
            self.assertEqual(json.dumps(role(panel, SYMBOLS, *args), default=str),
                             json.dumps(role(future, SYMBOLS, *args), default=str),
                             f'{role.__name__} used data after the observed session')

    def test_desk_uses_the_core_session_even_when_newer_data_exists(self):
        panel = build_panel(periods=325, end='2026-09-18')
        desk = make_desk(self.root / 'align', panel=panel)
        desk.observe(core_snapshot(panel=panel), now=NOW)
        cycle = desk.ledger.latest_session()
        self.assertEqual(cycle['session'], SESSION)
        self.assertEqual(cycle['core_session'], SESSION)
        rows = desk.ledger.advisories()
        self.assertTrue(rows)
        self.assertTrue(all(row['session'] == SESSION for row in rows))

    def test_shadow_fill_waits_for_a_later_observed_bar(self):
        panel = build_panel()
        desk = make_desk(self.root / 'shadowguard', panel=panel)
        desk.observe(core_snapshot(panel=panel), now=NOW)
        self.assertEqual(desk.shadow[SHADOW_BASELINE].status()['fill_count'], 0)
        later = build_panel(periods=321, end='2026-09-14')
        desk.intake = FakeIntake(later)
        desk.observe(core_snapshot('2026-09-14', '2026-09-14T20:30:00+00:00', panel=later),
                     now=datetime(2026, 9, 14, 20, 30, tzinfo=timezone.utc))
        status = desk.shadow[SHADOW_BASELINE].status()
        self.assertEqual(status['fill_count'], 2)
        for trade in status['trades']:
            self.assertEqual(trade['signal_date'], SESSION)
            self.assertEqual(trade['asof'], '2026-09-14')


class AdvisoryAvailabilityTests(unittest.TestCase):
    """A shadow decision must be timed from when the ADVISORY existed.

    The filtered target does not exist until the helpers have read the session.
    Timing its signal from the core's earlier fetch would let a bar that completed
    in between fill a decision that had not been taken yet.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_shadow_signal_carries_the_decision_time_not_the_cycle_start(self):
        panel = build_panel()
        desk = make_desk(self.root / 'timing', panel=panel)
        start = datetime(2026, 9, 11, 23, 45, tzinfo=timezone.utc)
        desk.observe(core_snapshot(panel=panel), now=start)
        # An injected time seeds a clock that still advances, so the decision is
        # stamped strictly after the cycle began, exactly as in production.
        decision = start + timedelta(seconds=desk.injected_step_seconds)
        for name in (SHADOW_BASELINE, SHADOW_FILTERED):
            pending = desk.shadow[name].status()['pending']
            self.assertEqual(pending['observed_at'], decision.isoformat())
            self.assertGreater(aware(pending['observed_at']), start)
            self.assertNotEqual(pending['observed_at'], OBSERVED)   # not the core's fetch time
        # Both instants are kept, and the core's own fetch time stays provenance.
        digest = desk.ledger.digest()
        self.assertEqual(digest['cycle_started_at'], start.isoformat())
        self.assertEqual(digest['observed_at'], decision.isoformat())
        self.assertEqual(digest['core_observed_at'], OBSERVED)
        stored = desk.shadow[SHADOW_BASELINE].status()['snapshot']
        self.assertEqual(stored['core_observed_at'], OBSERVED)
        self.assertEqual(stored['fetched_at'], decision.isoformat())
        # Every stored reading carries the decision time, not the start.
        for row in desk.ledger.advisories():
            self.assertEqual(row['observed_at'], decision.isoformat())

    def test_delayed_research_cannot_fill_on_a_bar_that_closed_before_it_read(self):
        friday, monday, tuesday = '2026-09-11', '2026-09-14', '2026-09-15'
        panels = {friday: build_panel(end=friday),
                  monday: build_panel(periods=321, end=monday),
                  tuesday: build_panel(periods=322, end=tuesday)}
        desk = make_desk(self.root / 'delayed', panel=panels[friday])

        # The core read Friday's close on Friday evening; the helpers were not
        # run until Monday afternoon, after Monday's conservative bound.
        late_reading = datetime(2026, 9, 14, 20, 0, tzinfo=timezone.utc)
        desk.observe(core_snapshot(friday, f'{friday}T20:30:00+00:00', panel=panels[friday]),
                     now=late_reading)
        self.assertGreaterEqual(
            aware(desk.shadow[SHADOW_FILTERED].status()['pending']['observed_at']), late_reading)
        # Counterfactual this test exists to prevent: timed from the core's fetch,
        # Monday's bound WOULD have satisfied the fill guard.
        self.assertGreater(session_close(monday), aware(f'{friday}T20:30:00+00:00'))
        self.assertLess(session_close(monday), late_reading)

        desk.intake = FakeIntake(panels[monday])
        desk.observe(core_snapshot(monday, f'{monday}T20:30:00+00:00', panel=panels[monday]),
                     now=datetime(2026, 9, 14, 20, 35, tzinfo=timezone.utc))
        for name in (SHADOW_BASELINE, SHADOW_FILTERED):
            status = desk.shadow[name].status()
            self.assertEqual(status['fill_count'], 0, f'{name} filled before its advisory existed')
            self.assertIn(HOLD_MESSAGE, status['outcome'])
            self.assertEqual(status['pending']['signal_date'], friday)

        # Tuesday's bound is after the reading, so it may fill.
        desk.intake = FakeIntake(panels[tuesday])
        desk.observe(core_snapshot(tuesday, f'{tuesday}T20:30:00+00:00', panel=panels[tuesday]),
                     now=datetime(2026, 9, 15, 20, 30, tzinfo=timezone.utc))
        for name in (SHADOW_BASELINE, SHADOW_FILTERED):
            status = desk.shadow[name].status()
            self.assertEqual(status['fill_count'], 2)
            self.assertTrue(all(trade['asof'] == tuesday and trade['signal_date'] == friday
                                for trade in status['trades']))


class DecisionClockTests(unittest.TestCase):
    """The decision time is read AFTER the fetch, and the difference matters."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_clocks_progress_and_never_move_backwards(self):
        step = StepClock(NOW, step_seconds=2)
        self.assertEqual([step(), step(), step()],
                         [NOW, NOW + timedelta(seconds=2), NOW + timedelta(seconds=4)])
        wall = WallClock()
        first = wall()
        self.assertGreaterEqual(wall(), first)
        # A backwards system adjustment cannot rewind an instant already handed out.
        wall._last = first + timedelta(hours=1)
        self.assertEqual(wall(), first + timedelta(hours=1))

    def test_a_slow_fetch_crossing_the_execution_bound_rejects_a_stale_fill(self):
        """The regression for the cycle-start timestamp.

        The cycle starts at 12:50 New York, before Monday's 13:00 completion
        bound. Fetching takes twenty minutes, so the reading is only complete at
        13:10, after that bound. Monday's bar therefore must not fill the signal:
        the advisory did not exist when Monday's bar completed. Timed from the
        cycle start, it would have.
        """
        friday, monday, tuesday = '2026-09-11', '2026-09-14', '2026-09-15'
        panels = {friday: build_panel(end=friday),
                  monday: build_panel(periods=321, end=monday),
                  tuesday: build_panel(periods=322, end=tuesday)}
        start = datetime(2026, 9, 14, 16, 50, tzinfo=timezone.utc)      # 12:50 New York
        clock = ManualClock(start)
        desk = ResearchDesk(self.root / 'slow', universe=SYMBOLS, clock=clock,
                            intake=SlowIntake(panels[friday], clock, minutes=20),
                            books=None, crypto_products=(), enable_books=False)
        desk.observe(core_snapshot(friday, f'{friday}T20:30:00+00:00', panel=panels[friday]))

        decision = datetime(2026, 9, 14, 17, 10, tzinfo=timezone.utc)   # 13:10 New York
        digest = desk.ledger.digest()
        self.assertEqual(digest['cycle_started_at'], start.isoformat())
        self.assertEqual(digest['observed_at'], decision.isoformat())
        # The bound sits strictly between the two instants. That is the whole point.
        self.assertLess(start, session_close(monday))
        self.assertLess(session_close(monday), decision)
        for name in (SHADOW_BASELINE, SHADOW_FILTERED):
            self.assertEqual(desk.shadow[name].status()['pending']['observed_at'],
                             decision.isoformat())

        # Monday's bar completed at 13:00, before the reading existed: no fill.
        desk.clock = ManualClock(datetime(2026, 9, 14, 20, 30, tzinfo=timezone.utc))
        desk.intake = FakeIntake(panels[monday])
        desk.observe(core_snapshot(monday, f'{monday}T20:30:00+00:00', panel=panels[monday]))
        for name in (SHADOW_BASELINE, SHADOW_FILTERED):
            status = desk.shadow[name].status()
            self.assertEqual(status['fill_count'], 0,
                             f'{name} filled on a bar that closed before its advisory existed')
            self.assertIn(HOLD_MESSAGE, status['outcome'])
            self.assertEqual(status['pending']['signal_date'], friday)

        # Tuesday's bound is after the reading, so it may fill.
        desk.clock = ManualClock(datetime(2026, 9, 15, 20, 30, tzinfo=timezone.utc))
        desk.intake = FakeIntake(panels[tuesday])
        desk.observe(core_snapshot(tuesday, f'{tuesday}T20:30:00+00:00', panel=panels[tuesday]))
        for name in (SHADOW_BASELINE, SHADOW_FILTERED):
            status = desk.shadow[name].status()
            self.assertEqual(status['fill_count'], 2)
            self.assertTrue(all(trade['asof'] == tuesday and trade['signal_date'] == friday
                                for trade in status['trades']))

    def test_stored_reading_and_intake_timestamps_are_chronologically_consistent(self):
        """cycle start <= intake fetch <= decision, everywhere it is recorded."""
        downloads = []

        def downloader(symbols):
            downloads.append(tuple(symbols))
            return build_panel(list(symbols))

        clock = ManualClock(NOW)
        intake = DailyIntake(self.root / 'chrono' / 'intake', downloader=downloader, ttl_hours=4)
        desk = ResearchDesk(self.root / 'chrono', universe=SYMBOLS, clock=clock, intake=intake,
                            books=None, crypto_products=(), enable_books=False)
        # A fetch that takes a measurable amount of time.
        real_load = intake.load

        def timed_load(*args, **kwargs):
            result = real_load(*args, **kwargs)
            clock.advance(seconds=90)
            return result

        intake.load = timed_load
        desk.observe(core_snapshot())
        self.assertEqual(len(downloads), 1)

        digest = desk.ledger.digest()
        decision = aware(digest['observed_at'])
        started = aware(digest['cycle_started_at'])
        fetched = aware(digest['intake']['fetched_at'])
        self.assertLessEqual(started, fetched)
        self.assertLessEqual(fetched, decision)
        self.assertEqual(decision - started, timedelta(seconds=90))
        self.assertFalse(digest['intake']['cached'])

        cycle = desk.ledger.latest_session()
        self.assertEqual(cycle['observed_at'], decision.isoformat())
        self.assertEqual(cycle['intake_fetched_at'], fetched.isoformat())
        self.assertLessEqual(aware(cycle['intake_fetched_at']), aware(cycle['observed_at']))
        stored = desk.ledger.decision(SESSION)
        self.assertEqual(stored['cycle_started_at'], started.isoformat())
        self.assertEqual(stored['observed_at'], decision.isoformat())
        for row in desk.ledger.advisories():
            self.assertEqual(row['observed_at'], decision.isoformat())
        for name in (SHADOW_BASELINE, SHADOW_FILTERED):
            record = desk.shadow[name].record()
            self.assertTrue(record['observations'])
            for observation in record['observations']:
                self.assertEqual(observation['observed_at'], decision.isoformat())
        # Freshness is measured to the decision, not to the cycle start.
        self.assertAlmostEqual(digest['session_age_hours'],
                               (decision - session_close(SESSION)).total_seconds() / 3600,
                               places=9)

    def test_recovery_keeps_the_original_decision_time_even_on_a_later_clock(self):
        panel = build_panel()
        clock = ManualClock(NOW)
        desk = ResearchDesk(self.root / 'recover', universe=SYMBOLS, clock=clock,
                            intake=FakeIntake(panel), books=None, crypto_products=(),
                            enable_books=False)
        with patch.object(desk.ledger, 'record', side_effect=RuntimeError('synthetic crash')):
            with self.assertRaises(RuntimeError):
                desk.observe(core_snapshot(panel=panel))
        original = desk.ledger.decision(SESSION)['observed_at']
        self.assertEqual(original, NOW.isoformat())

        desk.clock = ManualClock(NOW + timedelta(hours=6))   # the retry happens much later
        desk.observe(core_snapshot(panel=panel))
        self.assertEqual(desk.ledger.decision(SESSION)['observed_at'], original)
        self.assertEqual(desk.ledger.digest()['observed_at'], original)
        for row in desk.ledger.advisories():
            self.assertEqual(row['observed_at'], original)
        for name in (SHADOW_BASELINE, SHADOW_FILTERED):
            self.assertEqual(desk.shadow[name].status()['pending']['observed_at'], original)


class CrashRecoveryTests(unittest.TestCase):
    """A cycle that dies part-way must not leave a fabricated comparison."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_crash_between_shadow_commits_claims_nothing_and_recovers_original_targets(self):
        panel = build_panel()
        desk = make_desk(self.root / 'halfway', panel=panel)
        snapshot = core_snapshot(panel=panel)

        def explode(*args, **kwargs):
            raise RuntimeError('synthetic crash after the first shadow commit')

        with patch.object(desk.shadow[SHADOW_FILTERED], 'cycle', side_effect=explode):
            with self.assertRaises(RuntimeError):
                desk.observe(snapshot, now=NOW)

        # The decision is on record; the advisories are not, and only one book moved.
        counts = desk.ledger.counts()
        self.assertEqual(counts['decisions'], 1)
        self.assertEqual(counts['advisories'], 0)
        self.assertEqual(counts['cycles'], 0)
        self.assertEqual(desk.shadow[SHADOW_BASELINE].status()['observation_count'], 1)
        self.assertEqual(desk.shadow[SHADOW_FILTERED].status()['observation_count'], 0)

        # No matched session exists, so no comparison is presented.
        comparison = desk._matched_comparison()
        self.assertEqual(comparison['matched']['sessions'], 0)
        self.assertIsNone(comparison['matched']['difference'])
        self.assertEqual(comparison['matched']['unmatched_sessions'], [SESSION])
        self.assertIn('no session has yet been observed by both', comparison['summary'])
        self.assertIn('excluded from the comparison', comparison['summary'])

        # The retry sees DIFFERENT source data. It must still apply the decision
        # that was actually taken, and must not spend a request to find out.
        changed = build_panel()
        changed['Volume'].iloc[-1, 0] = 2_000_000.0 * 6      # would flag AAA if recomputed
        desk.intake = FakeIntake(changed)
        message = desk.observe(core_snapshot(panel=changed), now=NOW.replace(hour=23))
        self.assertIn('Recovered an unfinished cycle', message)
        self.assertEqual(desk.intake.loads, 0)
        self.assertEqual(desk.shadow[SHADOW_FILTERED].status()['pending']['weights'],
                         {'AAA': 0.5, 'BBB': 0.5})
        # The ORIGINAL decision time, not the retry's, and not the cycle start.
        original_decision = (NOW + timedelta(seconds=desk.injected_step_seconds)).isoformat()
        self.assertEqual(desk.shadow[SHADOW_FILTERED].status()['pending']['observed_at'],
                         original_decision)
        self.assertEqual(desk.ledger.decision(SESSION)['observed_at'], original_decision)
        self.assertEqual(desk.ledger.decision(SESSION)['cycle_started_at'], NOW.isoformat())
        digest = desk.ledger.digest()
        self.assertEqual(digest['deferrals'], {})
        self.assertTrue(any('did not finish' in note for note in digest['notes']))
        after = desk._matched_comparison()
        self.assertEqual(after['matched']['sessions'], 1)
        self.assertEqual(after['matched']['latest'], SESSION)
        self.assertEqual(after['matched']['unmatched_sessions'], [])
        self.assertEqual(desk.ledger.counts()['advisories'], 9)

    def test_crash_before_ledger_persistence_recovers_without_double_cycling(self):
        panel = build_panel()
        desk = make_desk(self.root / 'beforeledger', panel=panel)
        snapshot = core_snapshot(panel=panel)
        with patch.object(desk.ledger, 'record', side_effect=RuntimeError('synthetic crash')):
            with self.assertRaises(RuntimeError):
                desk.observe(snapshot, now=NOW)
        self.assertEqual(desk.ledger.counts()['advisories'], 0)
        self.assertEqual(desk.ledger.counts()['decisions'], 1)
        for name in (SHADOW_BASELINE, SHADOW_FILTERED):
            self.assertEqual(desk.shadow[name].status()['observation_count'], 1)

        desk.intake = FakeIntake(build_panel())
        message = desk.observe(snapshot, now=NOW.replace(hour=23))
        self.assertIn('Recovered an unfinished cycle', message)
        self.assertEqual(desk.intake.loads, 0)
        for name in (SHADOW_BASELINE, SHADOW_FILTERED):
            # Re-applying the decision does not add a second observation.
            self.assertEqual(desk.shadow[name].status()['observation_count'], 1)
        self.assertEqual(desk.ledger.counts()['advisories'], 9)
        self.assertEqual(desk.ledger.counts()['cycles'], 1)
        # A third call is the ordinary already-recorded path again.
        self.assertIn('already recorded', desk.observe(snapshot, now=NOW.replace(hour=23)))

    def test_prepared_decision_is_immutable(self):
        ledger = ResearchLedger(self.root / 'immutable' / 'research.sqlite3')
        first, created = ledger.prepare_decision(
            SESSION, {'session': SESSION, 'observed_at': OBSERVED, 'core_target': {'AAA': 1.0}})
        self.assertTrue(created)
        second, created_again = ledger.prepare_decision(
            SESSION, {'session': SESSION, 'observed_at': '2026-09-11T23:00:00+00:00',
                      'core_target': {'BBB': 1.0}})
        self.assertFalse(created_again)
        self.assertEqual(second, first)
        self.assertEqual(ledger.decision(SESSION)['core_target'], {'AAA': 1.0})

    def test_matched_comparison_uses_the_latest_shared_session_not_each_book_own_mark(self):
        panel = build_panel()
        desk = make_desk(self.root / 'matched', panel=panel)
        desk.observe(core_snapshot(panel=panel), now=NOW)
        later = build_panel(periods=321, end='2026-09-14')
        # Only the baseline book is advanced to the later session, by hand.
        desk.shadow[SHADOW_BASELINE].cycle(
            {'asof': '2026-09-14', 'fetched_at': '2026-09-14T20:30:00+00:00',
             'prices': {s: float(later['Close'].iloc[-1][s]) for s in SYMBOLS},
             'target_weights': {'AAA': 0.5, 'BBB': 0.5}})
        comparison = desk._matched_comparison()
        self.assertEqual(comparison['matched']['latest'], SESSION)
        self.assertEqual(comparison['matched']['unmatched_sessions'], ['2026-09-14'])
        self.assertEqual(comparison['books'][SHADOW_BASELINE]['latest_session'], '2026-09-14')
        self.assertEqual(comparison['books'][SHADOW_FILTERED]['latest_session'], SESSION)
        # The comparison is taken at the shared session, where both are $10,000.
        self.assertAlmostEqual(comparison['matched']['difference'], 0.0, places=6)
        self.assertIn('excluded from the comparison', comparison['summary'])


class ShadowEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_filtered_book_drops_a_flagged_name_and_leaves_the_weight_in_cash(self):
        panel = build_panel()
        panel['Volume'].iloc[-1, 0] = 2_000_000.0 * 6      # AAA trades unusually heavy
        desk = make_desk(self.root / 'filter', panel=panel)
        desk.observe(core_snapshot(panel=panel), now=NOW)
        baseline = desk.shadow[SHADOW_BASELINE].status()['pending']['weights']
        filtered = desk.shadow[SHADOW_FILTERED].status()['pending']['weights']
        self.assertEqual(baseline, {'AAA': 0.5, 'BBB': 0.5})
        self.assertEqual(filtered, {'BBB': 0.5})
        digest = desk.ledger.digest()
        self.assertIn('AAA', digest['deferrals'])
        self.assertIn('AAA', digest['deferrals_in_core_target'])
        self.assertEqual(sum(filtered.values()), 0.5)      # the rest stays in cash

    def test_quiet_session_defers_nothing_and_says_so(self):
        desk = make_desk(self.root / 'quiet')
        desk.observe(core_snapshot(), now=NOW)
        digest = desk.ledger.digest()
        self.assertEqual(digest['deferrals'], {})
        self.assertTrue(any('No helper proposed an action' in line
                            for line in digest['plain_language']))
        self.assertEqual(desk.shadow[SHADOW_BASELINE].status()['pending']['weights'],
                         desk.shadow[SHADOW_FILTERED].status()['pending']['weights'])

    def test_digest_reports_coverage_freshness_and_abstention_reasons(self):
        panel = build_panel()
        panel['Volume'].iloc[-2, 2] = np.nan               # CCC cannot be read
        desk = make_desk(self.root / 'coverage', panel=panel)
        desk.observe(core_snapshot(panel=panel), now=NOW)
        digest = desk.ledger.digest()
        activity = digest['coverage'][roles.ROLE_ACTIVITY]
        self.assertEqual(activity['examined'], 3)
        self.assertEqual(activity['abstained'], 1)
        self.assertTrue(activity['abstention_reasons'])
        self.assertGreater(digest['session_age_hours'], 0)
        self.assertTrue(any('freshness' in line for line in digest['plain_language']))
        self.assertIn('No confidence score', digest['scoring_policy'])

    def test_supervisor_records_disagreement_between_helpers(self):
        panel = build_panel()
        panel['Volume'].iloc[-1, 0] = 2_000_000.0 * 6
        desk = make_desk(self.root / 'disagree', panel=panel)
        desk.observe(core_snapshot(panel=panel), now=NOW)
        digest = desk.ledger.digest()
        rows = [row for row in digest['disagreements'] if row['symbol'] == 'AAA']
        self.assertEqual(len(rows), 1)
        self.assertIn(roles.ROLE_ACTIVITY, rows[0]['flagged_by'])
        self.assertIn(roles.ROLE_ENTRY, rows[0]['no_action_from'])

    def test_shadow_summary_states_the_record_is_too_short_to_judge(self):
        desk = make_desk(self.root / 'short')
        desk.observe(core_snapshot(), now=NOW)
        summary = desk.ledger.digest()['shadow']['summary']
        self.assertIn('virtual', summary)
        self.assertIn('cannot show whether the filter helps', summary)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def _serve(self, desk):
        book = PaperBook(self.root / 'http.sqlite3')
        server = make_server(Controller(book, desk=desk), 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread, f'http://127.0.0.1:{server.server_port}'

    def test_endpoint_serves_the_record_read_only(self):
        desk = make_desk(self.root / 'api')
        desk.observe(core_snapshot(), now=NOW)
        server, thread, base = self._serve(desk)
        try:
            payload = json.load(urlopen(base + '/api/research-desk'))
            self.assertEqual(payload['version'], 'research-v1')
            self.assertEqual(payload['digest']['session'], SESSION)
            self.assertTrue(payload['advisories'])
            self.assertIn('Advisory only', payload['policy']['authority'])
            self.assertIn('No language model is called', payload['policy']['llm'])
            before = desk.ledger.counts()
            json.load(urlopen(base + '/api/research-desk'))
            json.load(urlopen(base + '/api/research-desk'))
            self.assertEqual(desk.ledger.counts(), before)   # repeated GETs change nothing
            html = urlopen(base).read().decode()
            self.assertIn('Research helpers', html)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_endpoint_reports_disabled_without_a_desk(self):
        server, thread, base = self._serve(None)
        try:
            payload = json.load(urlopen(base + '/api/research-desk'))
            self.assertFalse(payload['enabled'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_status_payload_is_strict_json_serializable(self):
        desk = make_desk(self.root / 'json', books={'BTC-USD': synthetic_book()})
        desk.observe(core_snapshot(), now=NOW)
        json.dumps(desk.status(), allow_nan=False)

    def test_payload_carries_every_field_the_dashboard_renders(self):
        """Contract with the research section of trading_ui.html.

        The dashboard reads these paths by name. If one is renamed, the panel
        silently renders blanks, which is exactly the failure this repository
        already hit once with NaN prices and unfilled weights.
        """
        panel = build_panel()
        panel['Volume'].iloc[-1, 0] = 2_000_000.0 * 6      # ensure a note exists
        desk = make_desk(self.root / 'contract', panel=panel,
                         books={'BTC-USD': synthetic_book()})
        desk.observe(core_snapshot(panel=panel), now=NOW)
        desk.observe(core_snapshot('2026-09-04', '2026-09-04T20:30:00+00:00', panel=panel),
                     now=datetime(2026, 9, 14, 20, 30, tzinfo=timezone.utc))  # makes a hold
        payload = desk.status()
        for key in ('version', 'message', 'digest', 'advisories', 'counts', 'history',
                    'holds', 'policy'):
            self.assertIn(key, payload)
        for key in ('unavailable', 'depth_policy', 'scoring', 'authority', 'llm'):
            self.assertIn(key, payload['policy'])
        digest = payload['digest']
        for key in ('session', 'session_age_hours', 'plain_language', 'coverage', 'deferrals',
                    'deferrals_in_core_target', 'disagreements', 'shadow', 'scoring_policy'):
            self.assertIn(key, digest)
        for values in digest['coverage'].values():
            for key in ('title', 'examined', 'reported', 'abstained', 'abstention_reasons',
                        'actions'):
                self.assertIn(key, values)
        self.assertIn('summary', digest['shadow'])
        self.assertEqual(sorted(digest['shadow']['books']), [SHADOW_BASELINE, SHADOW_FILTERED])
        for values in digest['shadow']['books'].values():
            for key in ('equity', 'cash', 'fills', 'observations'):
                self.assertIn(key, values)
        for row in payload['advisories']:
            for key in ('role', 'symbol', 'verdict', 'abstained', 'abstention_reason',
                        'proposed_action', 'observations_used', 'session_age_hours'):
                self.assertIn(key, row)
        for row in payload['history']:
            for key in ('session', 'observed_at', 'intake_fetched_at', 'intake_cached',
                        'outcome'):
                self.assertIn(key, row)
        self.assertTrue(payload['holds'])
        for row in payload['holds']:
            for key in ('observed_at', 'session', 'reason'):
                self.assertIn(key, row)
        actions = [a for values in digest['coverage'].values() for a in values['actions']]
        self.assertTrue(actions, 'the note panel would render empty in this scenario')


if __name__ == '__main__':
    unittest.main()
