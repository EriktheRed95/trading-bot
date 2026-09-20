"""Read-only public data intake for the research companion, with snapshot caching.

The frozen core strategy fetches adjusted closes only. Three of the five research
roles need data the core does not carry (share volume, session open/high/low,
and real order-book depth), so this module adds exactly two public sources and
caches every snapshot so a repeated poll costs no request:

  * Daily OHLCV bars for the core universe from Yahoo Finance via yfinance,
    auto-adjusted like the core's own feed. One request per cache window for the
    whole universe, never one request per symbol.
  * Coinbase Exchange public level-2 order book (aggregated levels) for the USD
    crypto references the hourly lab already uses. This is the exchange's
    VISIBLE RESTING BOOK at snapshot time: observable, and cancellable the next
    second. It is not an automated-market-maker pool depth, and no depth figure
    anywhere in this layer is inferred from price, volume or a proxy fund.

Both are plain read-only GET requests. Nothing here authenticates, writes to a
venue, or places an order.

Every snapshot carries the fetch time, the source string and a SHA-256 over the
exact bytes cached, so any advisory can be traced back to the data it was
computed on.
"""
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import hashlib
import json
import numpy as np
import pandas as pd

NEW_YORK = ZoneInfo('America/New_York')
FIELDS = ('Open', 'High', 'Low', 'Close', 'Volume')
DAILY_SOURCE = 'Yahoo Finance daily bars via yfinance (auto-adjusted prices, reported share volume)'
BOOK_SOURCE = 'Coinbase Exchange public order book, level 2 (aggregated price levels)'
COINBASE_BOOK_URL = 'https://api.exchange.coinbase.com/products/{product}/book'
DEPTH_POLICY = ('Visible resting orders on a public exchange book at snapshot time. '
                'Not a liquidity-pool depth, not an executable guarantee, and never '
                'inferred from price, volume or a proxy instrument.')


class IntakeUnavailable(ValueError):
    pass


def utc(now=None):
    """Normalise an observation time to UTC; naive input is refused."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('UTC-aware observation time required')
    return now.astimezone(timezone.utc)


def panel_bytes(panel):
    """Canonical bytes for a panel, used for both caching and provenance."""
    parts = []
    for field in FIELDS:
        frame = panel[field]
        parts.append(field.encode())
        parts.append(frame.sort_index().to_csv().encode())
    return b'\x00'.join(parts)


def panel_sha256(panel):
    return hashlib.sha256(panel_bytes(panel)).hexdigest()


def yf_download_ohlcv(symbols):
    """Default downloader: one batched request for the whole universe."""
    import yfinance as yf
    raw = yf.download(sorted(set(symbols)), period='2y', interval='1d', auto_adjust=True,
                      progress=False, threads=4, timeout=30)
    if raw is None or getattr(raw, 'empty', True):
        raise IntakeUnavailable('Empty daily OHLCV download')
    panel = {}
    for field in FIELDS:
        if isinstance(raw.columns, pd.MultiIndex):
            if field not in raw.columns.get_level_values(0):
                raise IntakeUnavailable(f'Daily download is missing {field}')
            frame = raw[field].copy()
        else:
            if field not in raw.columns:
                raise IntakeUnavailable(f'Daily download is missing {field}')
            frame = raw[[field]].copy()
        frame.index = pd.to_datetime(frame.index).tz_localize(None).normalize()
        panel[field] = frame.sort_index()
    return panel


def align(panel, symbols):
    """One column per requested symbol; an absent symbol is explicitly empty."""
    symbols = sorted(set(symbols))
    out = {}
    for field in FIELDS:
        frame = panel[field].copy()
        for symbol in symbols:
            if symbol not in frame.columns:
                frame[symbol] = np.nan
        frame = frame[symbols].astype(float)
        frame.index = pd.to_datetime(frame.index).tz_localize(None).normalize()
        out[field] = frame.sort_index()
    return out


def completed_sessions(panel, now):
    """Withhold any bar that may still be forming.

    Same convention as the frozen core engine: today's daily bar is not a
    completed observation until 16:15 New York. Nothing is forward filled, and a
    non-finite value stays missing so a role abstains instead of guessing.
    """
    local = utc(now).astimezone(NEW_YORK)
    today = pd.Timestamp(local.date())
    out = {}
    for field in FIELDS:
        frame = panel[field]
        frame = frame.loc[frame.index <= today]
        if (local.hour, local.minute) < (16, 15):
            frame = frame.loc[frame.index < today]
        frame = frame[~frame.index.duplicated(keep='last')]
        out[field] = frame.where(np.isfinite(frame))
    return out


class DailyIntake:
    """Cached daily OHLCV snapshot.

    A call inside `ttl_hours` of the cached fetch reuses the file on disk and
    issues no request, so polling the dashboard does not fan out to the data
    provider. The cache stores the snapshot exactly as fetched; the completed
    session cut is applied at use time, so a bar that has since completed can be
    used from the same snapshot without a new request.
    """

    def __init__(self, root, downloader=yf_download_ohlcv, ttl_hours=4.0,
                 min_refetch_minutes=20.0):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.downloader = downloader
        self.ttl_hours = ttl_hours
        # When a caller needs a session the cache does not carry, the cache is
        # refreshed early rather than holding the caller back for the whole TTL.
        # This floor bounds how often that can happen while a provider lags.
        self.min_refetch_minutes = min_refetch_minutes
        self.requests = 0

    def _meta_path(self):
        return self.root / 'daily.json'

    def _field_path(self, field):
        return self.root / f'daily_{field}.csv'

    def _read_cache(self, symbols, now):
        if not self._meta_path().exists():
            return None
        try:
            meta = json.loads(self._meta_path().read_text(encoding='utf-8'))
            if sorted(meta['symbols']) != sorted(set(symbols)):
                return None
            fetched = utc(datetime.fromisoformat(meta['fetched_at']))
            age = (utc(now) - fetched).total_seconds()
            if age < 0 or age > self.ttl_hours * 3600:
                return None
            blob = b'\x00'.join(
                part for field in FIELDS
                for part in (field.encode(), self._field_path(field).read_bytes()))
            if hashlib.sha256(blob).hexdigest() != meta['sha256']:
                return None
            panel = {}
            for field in FIELDS:
                frame = pd.read_csv(self._field_path(field), index_col=0, parse_dates=True)
                frame.index = pd.to_datetime(frame.index).normalize()
                panel[field] = frame.astype(float).sort_index()
            return panel, meta
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _write_cache(self, panel, symbols, fetched_at):
        blobs = {field: panel[field].sort_index().to_csv().encode() for field in FIELDS}
        for field, data in blobs.items():
            temp = self.root / f'daily_{field}.tmp'
            temp.write_bytes(data)
            temp.replace(self._field_path(field))
        sha = hashlib.sha256(b'\x00'.join(
            part for field in FIELDS for part in (field.encode(), blobs[field]))).hexdigest()
        meta = {'fetched_at': fetched_at.isoformat(), 'symbols': sorted(set(symbols)),
                'sha256': sha, 'source': DAILY_SOURCE, 'ttl_hours': self.ttl_hours}
        temp = self.root / 'daily.tmp'
        temp.write_text(json.dumps(meta, indent=2), encoding='utf-8')
        temp.replace(self._meta_path())
        return meta

    def load(self, symbols, now=None, require_session=None):
        """Return (panel, meta). meta carries fetched_at, source, sha256, cached.

        `require_session` is a session the caller must have. If the cache does not
        contain it and the cache is older than `min_refetch_minutes`, the snapshot
        is refreshed early. This is what keeps a lagging provider from blocking
        the helpers for a whole cache window, without polling it every cycle.
        """
        now = utc(now)
        symbols = sorted(set(symbols))
        cached = self._read_cache(symbols, now)
        if cached:
            panel, meta = cached
            age_hours = (now - utc(datetime.fromisoformat(meta['fetched_at']))).total_seconds() / 3600
            has_session = (require_session is None
                           or pd.Timestamp(require_session) in panel['Close'].index)
            if has_session or age_hours * 60 < self.min_refetch_minutes:
                return panel, {**meta, 'cached': True, 'age_hours': round(age_hours, 3),
                               'required_session_present': has_session}
        panel = align(self.downloader(symbols), symbols)
        self.requests += 1
        meta = self._write_cache(panel, symbols, now)
        return panel, {**meta, 'cached': False, 'age_hours': 0.0,
                       'required_session_present': (require_session is None or
                                                    pd.Timestamp(require_session) in panel['Close'].index)}


def requests_get_json(url, params=None, timeout=20):
    """Read-only public GET. No authentication, no write verbs."""
    import requests
    response = requests.get(url, params=params, timeout=timeout)
    response.raise_for_status()
    return response.json()


def parse_book(payload, fetched_at):
    """Normalise a level-2 book payload. Malformed input raises, never guesses."""
    try:
        bids = [(float(price), float(size)) for price, size, *_ in payload['bids']]
        asks = [(float(price), float(size)) for price, size, *_ in payload['asks']]
    except (KeyError, TypeError, ValueError) as exc:
        raise IntakeUnavailable('Malformed order book payload') from exc
    bids = [(p, s) for p, s in bids if np.isfinite(p) and np.isfinite(s) and p > 0 and s > 0]
    asks = [(p, s) for p, s in asks if np.isfinite(p) and np.isfinite(s) and p > 0 and s > 0]
    if not bids or not asks:
        raise IntakeUnavailable('Order book has no usable two-sided quote')
    bids.sort(key=lambda row: -row[0])
    asks.sort(key=lambda row: row[0])
    if asks[0][0] < bids[0][0]:
        raise IntakeUnavailable('Order book is crossed; snapshot is not usable')
    stamp = payload.get('time')
    try:
        stamp = (datetime.fromisoformat(str(stamp).replace('Z', '+00:00'))
                 .astimezone(timezone.utc).isoformat()) if stamp else None
    except (AttributeError, ValueError):
        stamp = None
    return {'bids': bids, 'asks': asks, 'book_time': stamp,
            'fetched_at': fetched_at.isoformat(), 'sequence': payload.get('sequence'),
            'source': BOOK_SOURCE, 'depth_policy': DEPTH_POLICY}


class BookIntake:
    """Bounded, cached public level-2 book snapshots. GET only.

    `max_products` bounds fan-out per cycle and `ttl_minutes` bounds request
    rate. A product that fails is reported with its error, never filled in from
    another source or estimated from price history.
    """

    def __init__(self, root, getter=requests_get_json, ttl_minutes=50.0, max_products=4):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.getter = getter
        self.ttl_minutes = ttl_minutes
        self.max_products = max_products
        self.requests = 0
        self.path = self.root / 'books.json'

    def _cache(self):
        if self.path.exists():
            try:
                cached = json.loads(self.path.read_text(encoding='utf-8'))
                return cached if isinstance(cached, dict) else {}
            except (OSError, ValueError):
                return {}
        return {}

    def load(self, products, now=None):
        """Return {product: book snapshot or {'error': reason}}."""
        now = utc(now)
        cache = self._cache()
        requested = sorted(set(products))
        out = {}
        for product in requested[:self.max_products]:
            entry = cache.get(product)
            if isinstance(entry, dict) and entry.get('fetched_at'):
                try:
                    age = (now - utc(datetime.fromisoformat(entry['fetched_at']))).total_seconds()
                    if 0 <= age <= self.ttl_minutes * 60:
                        out[product] = {**entry, 'cached': True,
                                        'age_minutes': round(age / 60, 3)}
                        continue
                except ValueError:
                    pass
            try:
                payload = self.getter(COINBASE_BOOK_URL.format(product=product), params={'level': 2})
                self.requests += 1
                book = parse_book(payload, now)
                cache[product] = book
                out[product] = {**book, 'cached': False, 'age_minutes': 0.0}
            except Exception as exc:
                # Network, HTTP, JSON and shape failures are reported as
                # unavailable. No substitute source and no estimated depth.
                out[product] = {'error': f'{type(exc).__name__}: {exc}'[:200],
                                'fetched_at': now.isoformat(), 'source': BOOK_SOURCE,
                                'depth_policy': DEPTH_POLICY}
        for product in requested[self.max_products:]:
            out[product] = {'error': f'Not requested this cycle: intake is bounded to '
                                     f'{self.max_products} order books per cycle',
                            'fetched_at': now.isoformat(), 'source': BOOK_SOURCE,
                            'depth_policy': DEPTH_POLICY}
        temp = self.path.with_suffix('.tmp')
        temp.write_text(json.dumps(cache), encoding='utf-8')
        temp.replace(self.path)
        return out
