"""Durable paper positions. Signals fill only on a later observed session close.

Fixed shares drift between monthly target changes. The initial capital is virtual.
This module has no brokerage interface and never moves money.

A queued signal may fill only on a bar that completed AFTER the signal was
actually observed (wall-clock), for every cadence. Comparing bar labels alone is
not enough: a delayed provider can publish a bar whose close already happened
before the signal was seen, which would create an impossible fill.

Daily session close, fail-closed: no exchange calendar is bundled and the daily
provider bars carry no close time, so a daily bar is taken to have completed at
13:00 New York, the earliest regular NYSE close (early-close days). A signal
observed later than 13:00 New York therefore cannot fill at that day's close and
waits for the next completed session. On a normal 16:00 day this delays a fill
that would have been legitimate; on an early-close day it prevents an impossible
one. The trade-off is documented rather than assumed away.
"""
from contextlib import contextmanager
from datetime import datetime, time, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import json
import math
import sqlite3
import numpy as np
from strategy_c import COST_PER_SIDE
from execution_model import rebalance

NEW_YORK = ZoneInfo('America/New_York')
EARLIEST_SESSION_CLOSE = time(13, 0)   # NYSE early close; the fail-closed daily bar end
SESSION_CLOSE_BASIS = 'earliest regular NYSE close (13:00 New York), fail-closed; no exchange calendar available'
HOLD_MESSAGE = 'fill held: this bar closed before the queued signal was observed'
UNKNOWN_MESSAGE = ('fill held: the queued signal has no recorded observation time; '
                   'it expires and is re-planned at a later observed bar')


class PaperHold(ValueError):
    pass


def aware(stamp):
    """Parse an ISO timestamp; naive values are taken as UTC."""
    if stamp is None:
        return None
    if isinstance(stamp, datetime):
        value = stamp
    elif isinstance(stamp, str):
        value = datetime.fromisoformat(stamp)
    else:
        raise TypeError('Timestamp must be an ISO string')
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def is_daily_label(asof):
    return 'T' not in asof and ' ' not in asof and len(asof) <= 10


def session_close(label):
    """Fail-closed UTC completion time of a daily session label.

    Without session-close metadata the bar is treated as complete at 13:00 New
    York, a lower bound on the actual close. It is never later than the true
    close, so it can only hold a fill, never authorize an impossible one.
    """
    day = datetime.fromisoformat(label)
    return day.replace(hour=EARLIEST_SESSION_CLOSE.hour, minute=EARLIEST_SESSION_CLOSE.minute,
                       tzinfo=NEW_YORK).astimezone(timezone.utc)


def bar_end(snapshot):
    """UTC completion time of a snapshot's price bar, never later than derivable.

    Hourly labels are already bar-end timestamps; daily labels use the
    fail-closed session close. An explicit `bar_end` is untrusted input: it is
    accepted only if it parses and is not later than the derived time, so a
    caller can make the timing check stricter but never looser. Anything else
    raises PaperHold and the cycle is held without touching state.
    """
    asof = snapshot['asof']
    derived = session_close(asof) if is_daily_label(asof) else aware(asof)
    if snapshot.get('bar_end') is None:
        return derived
    try:
        stated = aware(snapshot['bar_end'])
    except (TypeError, ValueError) as exc:
        raise PaperHold('Malformed bar_end in snapshot; all fills are held.') from exc
    if stated > derived:
        raise PaperHold('Snapshot bar_end is later than the bar can have completed; all fills are held.')
    return stated


def possible_unobserved_bars(previous, current, calendar):
    """Heuristic: could bars exist between two consecutive observations?

    Daily labels: more than one business day apart. Hourly, 24/7 markets: any
    gap above one hour. Hourly, US regular session: a gap above one hour that
    is not a plain overnight/weekend break between a 16:00 session-end bar and
    the next session's first completed bar. Holidays and early closes are not
    reconciled, so flags are "possible", not confirmed. None means the calendar
    is unknown and the check was not evaluated.
    """
    if is_daily_label(previous['asof']) and is_daily_label(current['asof']):
        return int(np.busday_count(previous['asof'], current['asof'])) > 1
    a, b = bar_end(previous), bar_end(current)
    gap = b - a
    if calendar == '24/7':
        return gap > timedelta(hours=1, minutes=1)
    if calendar == 'US regular session':
        if gap <= timedelta(hours=1, minutes=1):
            return False
        la, lb = a.astimezone(NEW_YORK), b.astimezone(NEW_YORK)
        overnight = ((la.hour, la.minute) == (16, 0) and (lb.hour, lb.minute) <= (10, 30)
                     and int(np.busday_count(la.date(), lb.date())) == 1)
        return not overnight
    return None


class PaperBook:
    def __init__(self, path, initial_cash=10000.0, *, cadence="monthly", cost_rate=COST_PER_SIDE, version="core-v2", max_pending_hours=168):
        self.cadence, self.cost_rate, self.version = cadence, cost_rate, version
        self.max_pending_hours = max_pending_hours
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS book(id INTEGER PRIMARY KEY CHECK(id=1), cash REAL, initial REAL, paused INTEGER DEFAULT 0, planned_month TEXT);
            CREATE TABLE IF NOT EXISTS positions(ticker TEXT PRIMARY KEY, shares REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS pending(id INTEGER PRIMARY KEY CHECK(id=1), signal_date TEXT, weights TEXT);
            CREATE TABLE IF NOT EXISTS cycles(asof TEXT PRIMARY KEY, snapshot TEXT, outcome TEXT);
            CREATE TABLE IF NOT EXISTS trades(id INTEGER PRIMARY KEY, asof TEXT, signal_date TEXT, ticker TEXT, shares REAL, price REAL, cost REAL);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, at TEXT, message TEXT);
            CREATE TABLE IF NOT EXISTS observations(asof TEXT PRIMARY KEY, observed_at TEXT, equity REAL, cash REAL, positions TEXT, version TEXT);
            CREATE TABLE IF NOT EXISTS configuration(id INTEGER PRIMARY KEY CHECK(id=1), settings TEXT);
            CREATE TABLE IF NOT EXISTS planned_target(id INTEGER PRIMARY KEY CHECK(id=1), weights TEXT);''')
            c.execute('INSERT OR IGNORE INTO book(id,cash,initial) VALUES(1,?,?)', (initial_cash,initial_cash))
            # The settings hash must stay stable so existing versioned books keep opening.
            settings=json.dumps({'cadence':cadence,'cost_rate':cost_rate,'version':version,'max_pending_hours':max_pending_hours},sort_keys=True)
            existing=c.execute('SELECT settings FROM configuration WHERE id=1').fetchone()
            if existing and existing[0] != settings:
                raise PaperHold('Account configuration changed: create a new versioned book.')
            c.execute('INSERT OR IGNORE INTO configuration VALUES(1,?)',(settings,))

    @contextmanager
    def connection(self):
        c = sqlite3.connect(self.path, timeout=15)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    def pause(self, paused):
        with self.connection() as c:
            c.execute('UPDATE book SET paused=? WHERE id=1', (int(paused),))

    def is_paused(self):
        with self.connection() as c:
            return bool(c.execute('SELECT paused FROM book WHERE id=1').fetchone()[0])

    def observation_marker(self):
        """Read-only count and latest recorded bar. A scheduler compares markers
        before and after a check to tell a new observation from a duplicate."""
        with self.connection() as c:
            count = c.execute('SELECT count(*) FROM observations').fetchone()[0]
            row = c.execute('SELECT asof,observed_at FROM observations ORDER BY asof DESC LIMIT 1').fetchone()
        return {'count':count, 'latest_bar':row['asof'] if row else None,
                'latest_observed_at':row['observed_at'] if row else None}

    def record_error(self, message):
        with self.connection() as c:
            c.execute('INSERT INTO events(at,message) VALUES(?,?)', (datetime.now(timezone.utc).isoformat(),message))

    @staticmethod
    def _signal_observed_at(c, signal_date):
        """When the queued signal's bar was actually observed, from the durable record.

        The observation row is written in the same transaction that queues the
        signal. Older rows without one fall back to the stored snapshot; if
        neither exists (legacy data) the result is None and the signal cannot
        fill: it is held until it expires and is re-planned with provenance.
        """
        row = c.execute('SELECT observed_at FROM observations WHERE asof=?', (signal_date,)).fetchone()
        stamp = row['observed_at'] if row else None
        if not stamp:
            row = c.execute('SELECT snapshot FROM cycles WHERE asof=?', (signal_date,)).fetchone()
            stamp = json.loads(row['snapshot']).get('fetched_at') if row else None
        return aware(stamp) if stamp else None

    def cycle(self, snapshot):
        date, prices, weights = snapshot['asof'], snapshot['prices'], snapshot['target_weights']
        if any(not math.isfinite(w) or w < 0 for w in weights.values()) or sum(weights.values()) > 1.000001:
            raise PaperHold('Invalid target weights.')
        if any(not math.isfinite(p) or p <= 0 for p in prices.values()):
            raise PaperHold('Invalid prices.')
        execution_end = bar_end(snapshot)
        observed_iso = snapshot.get('fetched_at') or datetime.now(timezone.utc).isoformat()
        try:
            observed_now = aware(observed_iso)
        except (TypeError, ValueError) as exc:
            raise PaperHold('Malformed fetched_at in snapshot; all fills are held.') from exc
        if execution_end > observed_now:
            raise PaperHold('Snapshot bar completes after its observation time; all fills are held.')
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            book = c.execute('SELECT * FROM book WHERE id=1').fetchone()
            if book['paused']:
                return 'Paused'
            last_cycle = c.execute('SELECT asof,snapshot FROM cycles ORDER BY asof DESC LIMIT 1').fetchone()
            last = last_cycle['asof'] if last_cycle else None
            if last and date <= last:
                return 'Already processed this session'
            positions = {r['ticker']:r['shares'] for r in c.execute('SELECT * FROM positions')}
            pending = c.execute('SELECT * FROM pending WHERE id=1').fetchone()
            expired = pending and (datetime.fromisoformat(date)-datetime.fromisoformat(pending['signal_date'])).total_seconds() > self.max_pending_hours*3600
            if expired:
                c.execute('DELETE FROM pending')
                c.execute('UPDATE book SET planned_month=NULL WHERE id=1')
                pending = None
            required = set(positions) | (set(json.loads(pending['weights'])) if pending else set())
            if required - prices.keys():
                raise PaperHold('A held or queued asset has no current price; all fills are held.')
            cash = book['cash']
            outcome = 'Expired old targets without filling' if expired else 'Marked existing holdings'
            # Every cadence: the execution bar must end after the signal was
            # actually observed, not merely carry a later label. A delayed
            # provider bar whose close preceded the observation cannot fill.
            # An unknowable observation time fails closed.
            observed_signal = self._signal_observed_at(c, pending['signal_date']) if pending else None
            if pending and date > pending['signal_date']:
                if observed_signal is None:
                    outcome += '; ' + UNKNOWN_MESSAGE
                elif execution_end > observed_signal:
                    target = json.loads(pending['weights'])
                    try:
                        cash, desired, fills = rebalance(cash, positions, prices, target, self.cost_rate)
                    except ValueError as exc:
                        raise PaperHold(str(exc)) from exc
                    for fill in fills:
                        c.execute('INSERT INTO trades(asof,signal_date,ticker,shares,price,cost) VALUES(?,?,?,?,?,?)',
                                  (date,pending['signal_date'],fill['ticker'],fill['shares'],fill['price'],fill['cost']))
                    positions = desired
                    c.execute('DELETE FROM positions')
                    c.executemany('INSERT INTO positions(ticker,shares) VALUES(?,?)', desired.items())
                    c.execute('UPDATE book SET cash=? WHERE id=1', (max(0,cash),))
                    c.execute('DELETE FROM pending')
                    outcome = 'Filled prior targets at this observed session close'
                else:
                    outcome += '; ' + HOLD_MESSAGE
            # Monthly planning is explicit; first observed cycle in each month.
            planned=c.execute('SELECT weights FROM planned_target WHERE id=1').fetchone()
            previous = json.loads(planned[0]) if planned else None
            plan = (book['planned_month'] != date[:7]) if self.cadence == 'monthly' else (previous != weights)
            if (expired or plan) and not c.execute('SELECT 1 FROM pending').fetchone():
                c.execute('INSERT INTO pending VALUES(1,?,?)', (date,json.dumps(weights,allow_nan=False)))
                c.execute('INSERT OR REPLACE INTO planned_target VALUES(1,?)',(json.dumps(weights),))
                c.execute('UPDATE book SET planned_month=? WHERE id=1', (date[:7],))
                outcome += '; queued targets for a later observed bar'
            value=cash+sum(q*prices[t] for t,q in positions.items())
            c.execute('INSERT INTO observations VALUES(?,?,?,?,?,?)',
                      (date,observed_iso,value,cash,json.dumps(positions),self.version))
            # Persist the bound actually used for this decision so exports match
            # execution, even if derivation rules change later.
            stored={**snapshot,'execution_bar_end':execution_end.isoformat(),
                    'execution_bar_end_basis':'snapshot' if snapshot.get('bar_end') is not None else 'label'}
            c.execute('INSERT INTO cycles VALUES(?,?,?)', (date,json.dumps(stored,allow_nan=False),outcome))
            return outcome

    def record(self, calendar=None):
        """Complete forward record with provenance.

        Observations carry the cycle outcome, the execution-time bound actually
        used for that cycle (bar_end, from the stored cycle; reconstructed from
        the label only for rows written before it was persisted) and the elapsed
        hours since the previous observed bar label/observation. Fills carry the
        observation times and execution bounds of both the fill bar and the
        signal bar. Gap flags are heuristic (see possible_unobserved_bars). Fixed
        shares mean equity across a gap still reflects the net price move; what
        is missing is the intermediate path plus any signals and fills that
        would have occurred on bars that did exist.
        """
        if calendar is None and self.cadence == 'monthly':
            calendar = 'US regular session'
        with self.connection() as c:
            observations=[dict(r) for r in c.execute(
                'SELECT o.*, c.outcome, c.snapshot FROM observations o LEFT JOIN cycles c ON c.asof=o.asof ORDER BY o.asof')]
            trades=[dict(r) for r in c.execute(
                '''SELECT t.*, f.observed_at AS observed_at, s.observed_at AS signal_observed_at
                   FROM trades t LEFT JOIN observations f ON f.asof=t.asof
                   LEFT JOIN observations s ON s.asof=t.signal_date ORDER BY t.id''')]
        gaps=[];previous=None;bounds={}
        for row in observations:
            stored=json.loads(row.pop('snapshot')) if row.get('snapshot') else None
            if stored and stored.get('execution_bar_end'):
                end,basis=aware(stored['execution_bar_end']),stored.get('execution_bar_end_basis','snapshot')
            else:
                try:
                    end=bar_end(stored) if stored else bar_end(row)
                except PaperHold:
                    end=bar_end({'asof':row['asof']})
                basis='reconstructed'
            row['bar_end'],row['bar_end_basis']=end.isoformat(),basis;bounds[row['asof']]=end
            row['bar_gap_hours']=row['observation_gap_hours']=None;row['unobserved_bars_possible']=None
            if previous:
                row['bar_gap_hours']=round((bar_end({'asof':row['asof']})-bar_end({'asof':previous['asof']})).total_seconds()/3600,3)
                if previous['observed_at'] and row['observed_at']:
                    row['observation_gap_hours']=round((aware(row['observed_at'])-aware(previous['observed_at'])).total_seconds()/3600,3)
                row['unobserved_bars_possible']=possible_unobserved_bars(previous,row,calendar)
                if row['unobserved_bars_possible']:
                    gaps.append({'from_asof':previous['asof'],'to_asof':row['asof'],'bar_gap_hours':row['bar_gap_hours'],
                                 'equity_change':row['equity']-previous['equity']})
            previous=row
        for trade in trades:
            trade['fill_bar_end']=(bounds.get(trade['asof']) or bar_end({'asof':trade['asof']})).isoformat()
            trade['signal_bar_end']=(bounds.get(trade['signal_date']) or bar_end({'asof':trade['signal_date']})).isoformat()
        return {'observations':observations,'trades':trades,'gaps':gaps,
                'provenance':{'version':self.version,'cadence':self.cadence,'calendar':calendar,
                    'observation_count':len(observations),'fill_count':len(trades),'gap_count':len(gaps),
                    'first_observed_at':observations[0]['observed_at'] if observations else None,
                    'last_observed_at':observations[-1]['observed_at'] if observations else None,
                    'session_close_basis':SESSION_CLOSE_BASIS,
                    'note':('Observations exist only for bars seen while a cycle ran. A flagged interval may '
                            'contain bars that were never observed; the flag does not prove bars existed, '
                            'because exchange holidays are not reconciled. Where bars did exist, no marks, '
                            'signals or fills were recorded for them. Held shares are fixed, so equity across '
                            'a gap reflects the net price move. Fills require a bar that completed after the '
                            'signal was observed; bar_end is the execution-time bound used for that decision, '
                            'a lower bound on the actual bar end, not the actual close. Daily bounds are 13:00 '
                            'New York. bar_gap_hours uses bar labels.')}}

    def status(self):
        with self.connection() as c:
            b = dict(c.execute('SELECT * FROM book WHERE id=1').fetchone())
            latest = c.execute('SELECT * FROM cycles ORDER BY asof DESC LIMIT 1').fetchone()
            snapshot = json.loads(latest['snapshot']) if latest else None
            prices = snapshot['prices'] if snapshot else {}
            holdings = [dict(r) for r in c.execute('SELECT * FROM positions ORDER BY ticker')]
            for row in holdings:
                row['price'] = prices.get(row['ticker'])
                row['value'] = row['shares']*row['price'] if row['price'] is not None else None
            equity = b['cash'] + sum(r['value'] or 0 for r in holdings)
            pending = c.execute('SELECT * FROM pending').fetchone()
            observed = self._signal_observed_at(c, pending['signal_date']) if pending else None
            return {'mode':'PAPER ONLY', 'cash':b['cash'], 'initial':b['initial'], 'equity':equity,
                    'pnl':equity-b['initial'],
                    'fill_count':c.execute('SELECT count(*) FROM trades').fetchone()[0],
                    'observation_count':c.execute('SELECT count(*) FROM observations').fetchone()[0],
                    'since':c.execute('SELECT min(observed_at) FROM observations').fetchone()[0], 'paused':bool(b['paused']), 'holdings':holdings,
                    'snapshot':snapshot, 'outcome':latest['outcome'] if latest else 'Waiting for first cycle',
                    'pending':{'signal_date':pending['signal_date'],'weights':json.loads(pending['weights']),
                               'observed_at':observed.isoformat() if observed else None} if pending else None,
                    'trades':[dict(r) for r in c.execute('SELECT * FROM trades ORDER BY id DESC LIMIT 50')],
                    'events':[dict(r) for r in c.execute('SELECT * FROM events ORDER BY id DESC LIMIT 10')]}
