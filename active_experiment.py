"""Isolated 15-minute forward experiment. Public prices only; never broker orders."""
from datetime import datetime, timezone
from pathlib import Path
import threading
import time
import numpy as np
import pandas as pd
import requests
from paper_book import PaperBook, PaperHold
from indicators import calculate_rsi

SYMBOLS = ('BTC-USD', 'ETH-USD')
VERSION = 'active-15m-v1'
COST = .004  # 30 bps fee allowance plus 10 bps spread/slippage, each side


def fetch_prices():
    end = pd.Timestamp.now(tz='UTC').floor('15min')
    start = end - pd.Timedelta(minutes=15 * 220)
    result = {}
    with requests.Session() as session:
        for symbol in SYMBOLS:
            response = session.get(f'https://api.exchange.coinbase.com/products/{symbol}/candles',
                params={'granularity':900, 'start':start.isoformat(), 'end':end.isoformat()}, timeout=15)
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list) or not rows:
                raise PaperHold('Public candle data unavailable; no active fills.')
            result[symbol] = pd.Series({pd.Timestamp(r[0], unit='s', tz='UTC'):float(r[4])
                for r in rows if isinstance(r, list) and len(r)>=5}, dtype=float).sort_index()
    return result


def snapshot(raw, now):
    now = pd.Timestamp(now)
    if now.tzinfo is None:
        raise PaperHold('Aware observation time required.')
    prices, weights, readings, ends = {}, {}, {}, []
    for symbol in SYMBOLS:
        s = raw[symbol].copy().sort_index()
        if s.index.tz is None or s.index.has_duplicates:
            raise PaperHold('Ambiguous candle timestamps; no active fills.')
        s.index = s.index.tz_convert('UTC') + pd.Timedelta(minutes=15)
        s = s.loc[s.index <= now-pd.Timedelta(minutes=2)].tail(200)
        if len(s)!=200 or not np.isfinite(s).all() or (s<=0).any():
            raise PaperHold('Needs 200 valid completed 15-minute bars for both assets.')
        if not (s.index.to_series().diff().dropna()==pd.Timedelta(minutes=15)).all():
            raise PaperHold('Missing 15-minute bars; the entire active account waits.')
        if now-s.index[-1]>pd.Timedelta(minutes=20):
            raise PaperHold('Active prices are stale; no fills or new observations.')
        short, long = float(s.tail(8).mean()), float(s.tail(32).mean())
        rsi = float(calculate_rsi(s).iloc[-1])
        if not np.isfinite(rsi):
            raise PaperHold('RSI unavailable; no active fills.')
        trend = short>long
        # Tactical pullback sleeve is long only while RSI is under 40 and price
        # remains above its 80-bar mean. No hidden state reconstructed as trades.
        pullback = rsi<40 and s.iloc[-1]>s.tail(80).mean()
        weight = .25*int(trend) + .25*int(pullback)
        if weight:
            weights[symbol] = weight
        prices[symbol] = float(s.iloc[-1])
        readings[symbol] = {'trend':bool(trend), 'pullback':bool(pullback), 'rsi':rsi, 'weight':weight}
        ends.append(s.index[-1])
    if len(set(ends))!=1:
        raise PaperHold('Assets do not share the same completed bar; no active fills.')
    return {'asof':ends[0].isoformat(), 'bar_end':ends[0].isoformat(),
        'fetched_at':now.isoformat(), 'strategy':VERSION, 'prices':prices,
        'target_weights':weights, 'readings':readings,
        'cost_bps':40, 'source':'Coinbase Exchange public 15-minute candles'}


class ActiveExperiment:
    def __init__(self, root, fetch=fetch_prices, clock=lambda: datetime.now(timezone.utc)):
        self.root = Path(root)
        self.book = PaperBook(self.root/'active.sqlite3', cadence='signal', cost_rate=COST,
            version=VERSION, max_pending_hours=.5)
        self.reference = PaperBook(self.root/'reference.sqlite3', cadence='signal', cost_rate=COST,
            version=VERSION+'-hold', max_pending_hours=.5)
        self.fetch = fetch
        self.clock = clock
        self.lock = threading.Lock()
        self.last_started = None
        self.message = 'Ready: a separate $10,000 paper experiment. Waiting for completed prices.'

    def pause(self, paused):
        self.book.pause(paused)
        self.reference.pause(paused)

    def cycle(self, raw=None, now=None):
        if self.book.status()['paused']:
            return 'Active experiment paused.'
        raw = self.fetch() if raw is None else raw
        # Observation time is after the network calls, never the fetch start.
        s = snapshot(raw, now or self.clock())
        self.message = self.book.cycle(s)
        self.reference.cycle({**s, 'strategy':VERSION+'-hold',
            'target_weights':{symbol:.5 for symbol in SYMBOLS}})
        return self.message

    def request_cycle(self):
        if self.book.status()['paused'] or not self.lock.acquire(blocking=False):
            return False
        if self.last_started is not None and time.monotonic()-self.last_started<900:
            self.lock.release()
            return False
        self.last_started = time.monotonic()
        def run():
            try:
                self.run_guarded()
            finally:
                self.lock.release()
        threading.Thread(target=run, daemon=True).start()
        return True

    def run_guarded(self):
        """One pass for a caller already holding self.lock. Returns (kind, message)."""
        if self.book.status()['paused']:
            return 'paused', 'Active experiment paused.'
        try:
            self.message = 'Checking completed 15-minute prices…'
            self.cycle()
            return 'ok', self.message
        except PaperHold as exc:
            self.message = str(exc)
            self.book.record_error(self.message)
            return 'held', self.message
        except Exception as exc:
            self.message = f'Active refresh held ({type(exc).__name__}); prior records preserved.'
            self.book.record_error(self.message)
            return 'error', self.message

    def status(self):
        a, b = self.book.status(), self.reference.status()
        ar, br = self.book.record(), self.reference.record()
        aa = {r['asof']:r for r in ar['observations']}
        bb = {r['asof']:r for r in br['observations']}
        common = sorted(aa.keys() & bb.keys())
        matched = None
        if common:
            first, last = common[0], common[-1]
            matched = {'first':first, 'last':last, 'observations':len(common),
                'active_return_pct':100*(aa[last]['equity']/aa[first]['equity']-1),
                'reference_return_pct':100*(bb[last]['equity']/bb[first]['equity']-1)}
        return {'enabled':True, 'version':VERSION, 'message':self.message,
            'busy':self.lock.locked(), 'account':a, 'reference':b, 'matched':matched,
            'records':{'observations':ar['observations'][-50:], 'trades':ar['trades'][-50:]}}
