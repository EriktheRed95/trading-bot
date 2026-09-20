"""Isolated forward-only stock paper labs. No broker or historical replay path.

Yahoo bars are start-labelled; only shared completed regular-session closes are
used. NYSE 2026–2027 published sessions are enforced; emergency closures rely on
provider freshness. Missing, stale or discontinuous data holds all accounts of a cadence.
"""
from datetime import datetime, timedelta, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import json
import threading
import sqlite3
from contextlib import contextmanager
import math

import pandas as pd
import yfinance as yf
from paper_book import PaperBook, PaperHold, COST_PER_SIDE

NY = ZoneInfo('America/New_York')
BASKET = ('SPY', 'QQQ', 'AAPL', 'MSFT', 'NVDA', 'AMD')
INTERVALS = (5, 15)
STRATEGIES = ('trend', 'breakout', 'mean_reversion')
INITIAL_CASH = 25000.0
# NYSE published 2026/2027 calendar, verified 2026-09-20.
# https://www.nyse.com/trade/hours-calendars
HOLIDAYS = set('2026-01-01 2026-01-19 2026-02-16 2026-04-03 2026-05-25 2026-06-19 2026-07-03 2026-09-07 2026-11-26 2026-12-25 2027-01-01 2027-01-18 2027-02-15 2027-03-26 2027-05-31 2027-06-18 2027-07-05 2027-09-06 2027-11-25 2027-12-24'.split())
EARLY_CLOSES = set('2026-11-27 2026-12-24 2027-11-26'.split())


def session_close_for(day):
    if day.year not in (2026, 2027) or day.weekday() >= 5 or day.isoformat() in HOLIDAYS:
        return None
    return time(13) if day.isoformat() in EARLY_CLOSES else time(16)

VERSION = 'stock-lab-v1'


def utc_now():
    return datetime.now(timezone.utc)


def session_open(now):
    if now.tzinfo is None or now.utcoffset() is None:
        raise PaperHold('Observation clock must be timezone aware')
    local = now.astimezone(NY)
    close = session_close_for(local.date())
    if close is None:
        return False
    # Collect the completed closing bar with its required provider delay.
    observation_close = (datetime.combine(local.date(), close) + timedelta(minutes=5)).time()
    return time(9, 30) <= local.time().replace(tzinfo=None) < observation_close


def fetch_prices(minutes):
    """One basket request per cadence, not one request per account."""
    data = yf.download(list(BASKET), period='5d', interval=f'{minutes}m',
                       auto_adjust=False, prepost=False, progress=False,
                       threads=False, timeout=15)
    if data.empty:
        raise PaperHold('Provider returned no bars')
    return data['Close']


def completed_prices(frame, minutes, now):
    if not session_open(now):
        raise PaperHold('Outside regular weekday session; no forward observations recorded')
    if not isinstance(frame, pd.DataFrame) or set(BASKET) - set(frame.columns):
        raise PaperHold('Missing basket symbols')
    if not isinstance(frame.index, pd.DatetimeIndex) or frame.index.tz is None:
        raise PaperHold('Provider timestamps must have an explicit timezone')
    if frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
        raise PaperHold('Duplicate or unordered provider bars')
    work = frame.loc[:, list(BASKET)].copy()
    starts = work.index.tz_convert(NY)
    ends = starts + pd.Timedelta(minutes=minutes)
    # Validate session/grid before converting start labels to actual bar ends.
    valid = [(session_close_for(s.date()) is not None and s.date() == e.date()
              and time(9, 30) <= s.time().replace(tzinfo=None)
              and e.time().replace(tzinfo=None) <= session_close_for(s.date())
              and (s.hour * 60 + s.minute - 570) % minutes == 0
              and s.second == 0 and s.microsecond == 0)
             for s, e in zip(starts, ends)]
    work.index = ends.tz_convert('UTC')
    work = work.loc[valid]
    work = work.loc[work.index <= pd.Timestamp(now) - pd.Timedelta(minutes=2)]
    if len(work) < 21:
        raise PaperHold('At least 21 completed shared bars required for indicators')
    latest = work.index[-1]
    if latest.tz_convert(NY).date() != now.astimezone(NY).date() or now - latest.to_pydatetime() > timedelta(minutes=minutes + 5):
        raise PaperHold('Latest shared completed bar is stale; no fills or signals')
    window = work.tail(21)
    if window.isna().any().any() or not all(math.isfinite(float(v)) and float(v) > 0 for v in window.to_numpy().flat):
        raise PaperHold('Incomplete/invalid shared basket bars; no forward filling')
    for a, b in zip(window.index[:-1], window.index[1:]):
        la, lb = a.tz_convert(NY), b.tz_convert(NY)
        if la.date() == lb.date() and b - a != pd.Timedelta(minutes=minutes):
            raise PaperHold('Missing within-session bars in indicator window')
        if la.date() != lb.date():
            next_day = la.date() + timedelta(days=1)
            while next_day < lb.date() and session_close_for(next_day) is None:
                next_day += timedelta(days=1)
            if next_day != lb.date():
                raise PaperHold('Missing full trading session in indicator window')
        if la.date() != lb.date() and la.time().replace(tzinfo=None) != session_close_for(la.date()):
            raise PaperHold('Missing prior session closing bar in indicator window')
        if la.date() != lb.date() and lb.time().replace(tzinfo=None) != (datetime.combine(lb.date(), time(9, 30)) + timedelta(minutes=minutes)).time():
            raise PaperHold('Missing first regular-session bar in indicator window')
    return window


def target_weights(frame, strategy, previous=None):
    previous = previous or {}
    weights = {}
    for symbol in BASKET:
        values = frame[symbol]
        latest = float(values.iloc[-1])
        mean20 = float(values.tail(20).mean())
        if strategy == 'trend':
            enter = latest > mean20 and float(values.tail(5).mean()) > mean20
        elif strategy == 'breakout':
            enter = latest > float(values.iloc[-21:-1].max()) or (previous.get(symbol, 0) > 0 and latest > float(values.tail(10).mean()))
        elif strategy == 'mean_reversion':
            std = float(values.tail(20).std(ddof=0))
            z = (latest - mean20) / std if std > 0 else 0
            enter = z < -1.5 or (previous.get(symbol, 0) > 0 and z < 0)
        else:
            raise ValueError('Unknown strategy')
        if enter:
            weights[symbol] = 1 / len(BASKET)
    return weights


class StockExperiments:
    def __init__(self, root, *, fetcher=fetch_prices, clock=utc_now):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.fetcher, self.clock = fetcher, clock
        self.lock = threading.RLock()
        self.worker = None
        self.meta = self.root / 'stock-lab-state.sqlite'
        with self._connection() as con:
            con.execute('CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,value TEXT)')
        self.books = {}
        for minutes in INTERVALS:
            for strategy in (*STRATEGIES, 'buy_hold'):
                key = f'{strategy}_{minutes}m'
                self.books[key] = PaperBook(self.root / f'{VERSION}-{key}.sqlite', initial_cash=INITIAL_CASH,
                    cadence=f'{minutes}m', cost_rate=COST_PER_SIDE, version=f'{VERSION}-{key}',
                    max_pending_hours=3 * minutes / 60)

    @contextmanager
    def _connection(self):
        con = sqlite3.connect(self.meta, timeout=15)
        try:
            with con:
                yield con
        finally:
            con.close()
    def _get(self, key, default=None):
        with self._connection() as con:
            row = con.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def _put(self, key, value):
        with self._connection() as con:
            con.execute('INSERT OR REPLACE INTO state VALUES(?,?)', (key, json.dumps(value)))

    def _claim_request(self, minutes, now):
        """Durable shared throttle survives rerenders, restarts and other processes."""
        key = f'last_request_{minutes}'
        with self._connection() as con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()
            if row and (now - datetime.fromisoformat(json.loads(row[0]))).total_seconds() < 60:
                return False
            con.execute('INSERT OR REPLACE INTO state VALUES(?,?)', (key, json.dumps(now.isoformat())))
        return True

    def pause(self, paused):
        with self.lock:
            self._put('paused', bool(paused))
            for book in self.books.values():
                book.pause(paused)

    def request_cycle(self, should_pause=lambda: False):
        """Start a nonblocking collection pass. True only when a worker starts."""
        with self.lock:
            if self.worker and self.worker.is_alive():
                return False
            if self._get('paused', False) or should_pause():
                return False
            self.worker = threading.Thread(target=self.run_cycle, args=(should_pause,), daemon=True)
            self.worker.start()
            return True

    def run_cycle(self, should_pause=lambda: False):
        now = self.clock()
        if self._get('paused', False) or should_pause():
            return self.status()
        self._put('last_attempt', now.isoformat())
        if not session_open(now):
            self._put('interval_status', {str(m): 'Outside regular weekday session; waiting for fresh provider bars' for m in INTERVALS})
            return self.status()
        messages = self._get('interval_status', {})
        for minutes in INTERVALS:
            if should_pause() or self._get('paused', False):
                break
            if not self._claim_request(minutes, now):
                continue
            try:
                raw = self.fetcher(minutes)
                observed = self.clock()
                frame = completed_prices(raw, minutes, observed)
                if should_pause() or self._get('paused', False):
                    break
                end = frame.index[-1].isoformat()
                prices = {s: float(frame[s].iloc[-1]) for s in BASKET}
                for strategy in (*STRATEGIES, 'buy_hold'):
                    key = f'{strategy}_{minutes}m'
                    book = self.books[key]
                    status = book.status()
                    prior = (status.get('snapshot') or {}).get('target_weights', {})
                    target = ({s: 1 / len(BASKET) for s in BASKET} if strategy == 'buy_hold'
                              else target_weights(frame, strategy, prior))
                    book.cycle({'asof': end, 'bar_end': end, 'fetched_at': observed.isoformat(),
                                'prices': prices, 'target_weights': target,
                                'source': 'Yahoo Finance via yfinance; raw close, prepost=False',
                                'interval': f'{minutes}m', 'strategy': strategy,
                                'calendar': 'NYSE published 2026-2027 regular sessions; five minute final-bar observation window',
                                'delay_minutes': 2})
                messages[str(minutes)] = f'Observed completed {minutes}m bar ending {end}'
            except Exception as exc:
                messages[str(minutes)] = f'Held: {type(exc).__name__}: {exc}'
        self._put('interval_status', messages)
        return self.status()


    def record(self, account_id):
        if account_id not in self.books:
            raise ValueError('Unknown stock experiment account')
        record = self.books[account_id].record(calendar='US regular session')
        minutes = int(account_id.rsplit('_', 1)[1][:-1])
        # PaperBook's default intraday gap heuristic is hourly. Replace for labs.
        gaps = []
        for previous, row in zip(record['observations'], record['observations'][1:]):
            a, b = datetime.fromisoformat(previous['asof']), datetime.fromisoformat(row['asof'])
            same_day = a.astimezone(NY).date() == b.astimezone(NY).date()
            row['unobserved_bars_possible'] = (b - a > timedelta(minutes=minutes)) if same_day else None
            if row['unobserved_bars_possible']:
                gaps.append({'from_asof': previous['asof'], 'to_asof': row['asof'], 'bar_gap_hours': row['bar_gap_hours']})
        record['gaps'] = gaps
        record['provenance'].update(gap_count=len(gaps), session_close_basis='Provider start plus bar interval; at least 2 minutes delay',
            note='Forward observations only. Same-session cadence gaps flagged; overnight gaps and holidays not independently reconciled. No historical replay. Raw closes exclude dividends.')
        return record

    def status(self):
        accounts = []
        for minutes in INTERVALS:
            benchmark_id = f'buy_hold_{minutes}m'
            reference = self.books[benchmark_id].status()
            reference_rows = {r['asof']: r for r in self.record(benchmark_id)['observations']}
            for strategy in (*STRATEGIES, 'buy_hold'):
                key = f'{strategy}_{minutes}m'
                state = self.books[key].status()
                rows = self.record(key)['observations']
                common = [r for r in rows if r['asof'] in reference_rows]
                # Strict full timestamp match: incomplete cohorts never claim matched performance.
                matched = bool(common) and len(common) == len(rows) == len(reference_rows)
                account_return = (state['equity'] / state['initial'] - 1) * 100
                benchmark_return = (reference['equity'] / reference['initial'] - 1) * 100 if matched else None
                accounts.append({**state, 'id': key, 'label': f'{strategy.replace("_", " ").title()} / {minutes}m',
                    'strategy': strategy, 'is_reference': strategy == 'buy_hold', 'interval': f'{minutes}m', 'return_pct': account_return,
                    'benchmark_id': benchmark_id, 'benchmark_return_pct': benchmark_return,
                    'excess_return_pct': account_return - benchmark_return if matched else None,
                    'matched_observations': len(common), 'matched_record': matched})
        return {'mode': 'PAPER ONLY', 'accounts': accounts, 'capital': INITIAL_CASH * 6,
                'reference_capital': INITIAL_CASH * 2, 'basket': list(BASKET),
                'paused': self._get('paused', False), 'busy': bool(self.worker and self.worker.is_alive()), 'last_attempt': self._get('last_attempt'),
                'interval_status': self._get('interval_status', {}), 'cost_bps_per_side': COST_PER_SIDE * 10000,
                'notes': ['Six isolated $25,000 accounts; two separate same-cadence equal-weight buy-and-hold references.',
                          'Long-only, unlevered; up to one sixth of capital per stock; unallocated cash earns zero.',
                          'More observations are not independent evidence of profitability; correlated basket and strategies.',
                          'Raw provider closes omit dividends and corporate-action reconciliation. No profit projection.',
                          'Runs only when requested by the dashboard; public data is not a real-time execution feed.',
                          'NYSE published 2026–2027 calendar; unsupported years, holidays, early closes and stale data hold trading. Emergency closures rely on provider freshness.']}
