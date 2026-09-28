"""Background paper collection: one in-process scheduler for every family.

This is not a second collection engine. Each family runs through the same code,
locks and pause state as its dashboard button (Controller.run_core/run_hourly,
ActiveExperiment.run_guarded, StockExperiments.run_interval). The scheduler only
decides when a check is due, records what happened, and keeps one family's
failure or stuck request from blocking the others.

A check is a provider request. A new observation is a bar a paper book had not
recorded before; re-checking an already recorded bar is not one. Missed bars
are never backfilled. It runs only while this server process runs on a powered,
awake, online computer. Paper only: no broker, credentials or orders.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time
import traceback
from typing import Callable

from stock_experiments import NY, INTERVALS, session_close_for, session_open

UTC = timezone.utc
SUCCESS = {'new', 'no_change', 'partial'}   # the family's data was accepted
FAILURE = {'held', 'error'}                 # rejected data or an exception
CHECKS = SUCCESS | FAILURE                  # outcomes that made a provider request
CALENDAR_YEARS = (2026, 2027)               # bundled NYSE calendar (stock_experiments)
CORE_READY = dtime(16, 15)                  # trading_engine withholds today's bar until then
STATE_LABELS = {
    'up_to_date': 'Current', 'awaiting': 'Waiting for the next bar', 'overdue': 'Overdue',
    'market_closed': 'Market closed', 'never': 'No observation yet', 'paused': 'Paused',
    'hung': 'Unhealthy: request not returning', 'failing': 'Failing: checks held or errored',
    'unknown': 'Calendar not bundled'}
NOTES = [
    'Collection runs inside this local server process on a timer. It does not need a browser page, '
    'this dashboard or any AI session; closing every page does not stop it.',
    'It runs only while the server process runs and the computer is powered on, awake and online. When '
    'started by the Windows logon task it runs only while you are signed in (a locked screen is fine). '
    'Sleep, hibernation, shutdown, sign-out or a network outage stop collection. There is no cloud copy.',
    'Missed bars are never backfilled: after an interruption the next check records only the latest '
    'completed bar, and the gap stays visible in each account record.',
    'A check is a provider request. A new observation is a bar the paper books had not recorded; '
    're-checking an already recorded bar is not a new observation.',
    'Paper only. No brokerage connection, credentials or orders.']


def utc_now():
    return datetime.now(UTC)


def parse(stamp):
    if not stamp:
        return None
    value = datetime.fromisoformat(stamp)
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def iso(value):
    return value.astimezone(UTC).isoformat() if value else None


def et(value):
    return value.astimezone(NY).strftime('%Y-%m-%d %H:%M ET') if value else 'none'


def floor_minutes(value, minutes):
    value = value.astimezone(UTC).replace(second=0, microsecond=0)
    return value - timedelta(minutes=(value.hour * 60 + value.minute) % minutes)


def _since(now, stamp):
    value = parse(stamp)
    return (now - value).total_seconds() if value else None


def _recent(now, state, seconds):
    elapsed = _since(now, state.get('last_attempt_at'))
    return elapsed is not None and 0 <= elapsed < seconds


def _spacing(base, failures, cap):
    """Retry spacing: the base interval, doubled per consecutive failure, capped."""
    return min(cap, base * 2 ** min(failures, 6)) if failures else base


# ---------------------------------------------------------------- calendars

def calendar_known(day):
    return day.year in CALENDAR_YEARS


def next_session_day(day):
    for _ in range(15):
        day += timedelta(days=1)
        if not calendar_known(day):
            return None
        if session_close_for(day):
            return day
    return None


def core_expected_session(now):
    """Latest NYSE session whose daily bar the core may use now, or None if unknown."""
    local = now.astimezone(NY)
    day = local.date()
    for _ in range(15):
        if not calendar_known(day):
            return None
        if session_close_for(day) and (day < local.date() or local.time() >= CORE_READY):
            return day
        day -= timedelta(days=1)
    return None


def core_next_ready(now):
    local = now.astimezone(NY)
    day = local.date()
    if not (session_close_for(day) and local.time() < CORE_READY):
        day = next_session_day(day)
    return datetime.combine(day, CORE_READY, tzinfo=NY) if day else None


def stock_expected_bar(now, minutes):
    """End of the latest regular-session bar that can be complete (2-minute delay)."""
    local = now.astimezone(NY)
    close = session_close_for(local.date())
    if close is None:
        return None
    start = datetime.combine(local.date(), dtime(9, 30), tzinfo=NY)
    end = min(now - timedelta(minutes=2), datetime.combine(local.date(), close, tzinfo=NY))
    if end < start + timedelta(minutes=minutes):
        return None
    steps = int((end - start).total_seconds() // (minutes * 60))
    return (start + timedelta(minutes=minutes * steps)).astimezone(UTC)


def listed_hourly_expected_bar(now):
    """Latest hourly listed-fund bar the market lab can use (label basis, 5-minute delay)."""
    local = now.astimezone(NY)
    close = session_close_for(local.date())
    if close is None:
        return None
    ready = now - timedelta(minutes=5)
    start = datetime.combine(local.date(), dtime(9, 30), tzinfo=NY)
    close_dt = datetime.combine(local.date(), close, tzinfo=NY)
    four = datetime.combine(local.date(), dtime(16), tzinfo=NY)
    latest = None
    while start < close_dt:
        end = min(start + timedelta(hours=1), four)
        if end <= ready:
            latest = end
        start += timedelta(hours=1)
    return latest.astimezone(UTC) if latest else None


def next_open(now):
    local = now.astimezone(NY)
    day = local.date()
    if session_close_for(day) and local.time() < dtime(9, 30):
        return datetime.combine(day, dtime(9, 30), tzinfo=NY)
    day = next_session_day(day)
    return datetime.combine(day, dtime(9, 30), tzinfo=NY) if day else None


# ------------------------------------------------------------ expectations

def bar_state(latest, expected, step, now, first=None, grace=timedelta(minutes=5)):
    """Compare the latest recorded bar with the latest bar that should exist.

    One bar behind is 'awaiting' (collection in progress or provider delay). The
    first bar of a session gets a short grace, because the previous session's
    close is necessarily more than one bar older.
    """
    if latest is None:
        return 'never', 'No forward observation recorded yet.'
    if latest >= expected:
        return 'up_to_date', f'Latest completed bar {et(latest)} is recorded.'
    if latest >= expected - step or (first is not None and expected <= first and now <= expected + grace):
        return 'awaiting', (f'Bar ending {et(expected)} is due and not yet recorded; '
                            f'latest recorded {et(latest)}.')
    return 'overdue', (f'Latest recorded bar {et(latest)}; bars through {et(expected)} should exist. '
                       'Missed bars are not backfilled.')


def core_due(now, state, source, force):
    if force:
        return True, 'Manual check requested.'
    partial = state.get('last_outcome') == 'partial'
    spacing = 1800 if partial else _spacing(300, state.get('consecutive_failures') or 0, 1800)
    if _recent(now, state, spacing):
        return False, f'Checked less than {spacing // 60} minutes ago.'
    expected, latest = core_expected_session(now), state.get('latest_bar')
    if expected and latest and latest >= expected.isoformat() and not partial:
        return False, (f'Session {latest} is recorded; the next daily bar is used after '
                       f'{et(core_next_ready(now))}.')
    return True, 'Due.'


def core_health(now, state):
    latest = state.get('latest_bar')
    expected = core_expected_session(now)
    if expected is None:
        return 'unknown', ('The bundled NYSE calendar does not cover this date; the core still checks '
                           f'every 5 minutes. Latest recorded session: {latest or "none"}.')
    if latest is None:
        return 'never', 'No completed session recorded yet.'
    today_closed = session_close_for(now.astimezone(NY).date()) is None
    if latest >= expected.isoformat():
        return 'up_to_date', (f'Session {latest} is recorded. Next daily bar is used after '
                              f'{et(core_next_ready(now))}.' + (' Market closed today.' if today_closed else ''))
    ready = datetime.combine(expected, CORE_READY, tzinfo=NY)
    previous = expected - timedelta(days=1)
    while session_close_for(previous) is None and previous > expected - timedelta(days=10):
        previous -= timedelta(days=1)
    if latest >= previous.isoformat() and now <= ready + timedelta(hours=3):
        return 'awaiting', f'Session {expected} became usable at {et(ready)}; provider publication can lag.'
    return 'overdue', (f'Latest recorded session {latest}; session {expected} should be available. '
                       'Missed sessions are not backfilled.')


def hourly_due(now, state, source, force):
    spacing = 60 if source == 'manual' else _spacing(300, state.get('consecutive_failures') or 0, 1800)
    if _recent(now, state, spacing):
        return False, f'Checked less than {spacing // 60} minutes ago.'
    return True, 'Due.'


def hourly_health(now, state):
    groups = json.loads(state.get('extra') or '{}').get('groups', {})
    order = ['overdue', 'never', 'awaiting', 'up_to_date', 'market_closed']
    parts, states = [], []
    if 'crypto' in groups or not groups:
        expected = floor_minutes(now - timedelta(minutes=5), 60)
        s, d = bar_state(parse(groups.get('crypto')), expected, timedelta(hours=1), now)
        states.append(s)
        parts.append('Crypto (24/7): ' + d)
    if 'listed' in groups:
        local = now.astimezone(NY)
        close = session_close_for(local.date())
        expected = listed_hourly_expected_bar(now)
        # The lab stops using listed bars 100 minutes after they end; later, the
        # session is simply closed and any missing tail stays visible as a gap.
        if (expected is None or close is None
                or now > datetime.combine(local.date(), close, tzinfo=NY) + timedelta(hours=2)):
            states.append('market_closed')
            parts.append(f'Listed funds: no hourly bar due now; latest recorded bar {et(parse(groups["listed"]))}.')
        else:
            first = datetime.combine(local.date(), dtime(10, 30), tzinfo=NY)
            s, d = bar_state(parse(groups['listed']), expected, timedelta(hours=1), now, first, timedelta(minutes=10))
            states.append(s)
            parts.append('Listed funds (NYSE session): ' + d)
    return min(states, key=order.index), ' '.join(parts)


def active_due(now, state, source, force):
    slot = floor_minutes(now - timedelta(minutes=2), 15)
    latest = parse(state.get('latest_bar'))
    if latest and latest >= slot:
        return False, f'Latest completed 15-minute bar ({et(slot)}) is already recorded.'
    spacing = 30 if source == 'manual' else _spacing(120, state.get('consecutive_failures') or 0, 900)
    if _recent(now, state, spacing):
        return False, f'Retrying at most every {spacing} seconds.'
    return True, 'Due.'


def active_health(now, state):
    expected = floor_minutes(now - timedelta(minutes=2), 15)
    return bar_state(parse(state.get('latest_bar')), expected, timedelta(minutes=15), now)


def stock_due(minutes):
    def due(now, state, source, force):
        if not session_open(now):
            return False, 'Market closed (NYSE regular-session calendar).'
        slot = stock_expected_bar(now, minutes)
        if slot is None:
            return False, f'Session open; the first completed {minutes}-minute bar is not available yet.'
        latest = parse(state.get('latest_bar'))
        if latest and latest >= slot:
            return False, f'Latest completed {minutes}-minute bar ({et(slot)}) is already recorded.'
        spacing = 60 if source == 'manual' else _spacing(60, state.get('consecutive_failures') or 0, minutes * 60)
        if _recent(now, state, spacing):
            return False, f'Retrying at most every {spacing} seconds.'
        return True, 'Due.'
    return due


def stock_health(minutes):
    def health(now, state):
        latest = parse(state.get('latest_bar'))
        if not calendar_known(now.astimezone(NY).date()):
            return 'unknown', 'The bundled NYSE calendar does not cover this year; stock labs hold.'
        if not session_open(now):
            return 'market_closed', (f'No regular session now. Latest recorded bar {et(latest)}. '
                                     f'Next session opens {et(next_open(now))}.')
        expected = stock_expected_bar(now, minutes)
        if expected is None:
            return 'awaiting', f'Session open; the first completed {minutes}-minute bar is not available yet.'
        local = now.astimezone(NY)
        first = datetime.combine(local.date(), dtime(9, 30), tzinfo=NY) + timedelta(minutes=minutes)
        return bar_state(latest, expected, timedelta(minutes=minutes), now, first)
    return health


# ------------------------------------------------------------------ families

@dataclass
class Family:
    name: str
    label: str
    schedule: str
    lock: object                 # shared with the family's dashboard path
    run: Callable                # (should_pause) -> (kind, message)
    books: Callable              # () -> [(group, PaperBook)] defining observations
    paused: Callable             # () -> reason string when paused, else None
    due: Callable                # (now, state, source, force) -> (bool, reason)
    health: Callable             # (now, state) -> (state, detail)
    deadline: float              # seconds before a running check is reported hung
    on_start: Callable = None
    provider: str = None         # shared data source, for yahoo_gate()


def yahoo_gate():
    """None when yfinance downloads may overlap, else a lock that serializes them.

    yfinance 1.x keeps per-call download state; older releases (still allowed by
    requirements.txt) share module-level state between threads, so concurrent
    family downloads could mix up each other's prices.
    """
    try:
        import yfinance.multi
        return None if hasattr(yfinance.multi, '_DownloadCtx') else threading.Lock()
    except ImportError:
        return None


def build_families(controller):
    book = controller.book
    def global_pause():
        return 'Global pause is on.' if book.is_paused() else None
    families = {'core': Family(
        'core', 'Core daily portfolio, benchmarks and research helpers',
        'After each NYSE session (bar used from 16:15 New York); rechecked every 5 minutes until recorded',
        controller.lock, lambda should_pause: controller.run_core(), lambda: [('core', book)],
        global_pause, core_due, core_health, deadline=900, provider='yahoo')}
    if controller.lab:
        lab = controller.lab
        def hourly_books():
            from market_lab import ASSETS
            return [('crypto' if ASSETS.get(key.split('__')[0], {}).get('group') == 'Crypto' else 'listed', b)
                    for key, b in list(lab.books.items())]
        families['hourly'] = Family(
            'hourly', 'Hourly market lab', 'Every 5 minutes; completed hourly bars',
            controller.hourly_lock, controller.run_hourly, hourly_books, global_pause,
            hourly_due, hourly_health, deadline=900, provider='yahoo')
    if controller.active:
        active = controller.active
        families['active'] = Family(
            'active', 'Active 15-minute crypto experiment', 'Shortly after each 15-minute bar completes, 24/7',
            active.lock, lambda should_pause: active.run_guarded(), lambda: [('active', active.book)],
            lambda: global_pause() or ('Active experiment paused.' if active.book.is_paused() else None),
            active_due, active_health, deadline=300,
            on_start=lambda: setattr(active, 'last_started', time.monotonic()))
    if controller.stocks:
        stocks = controller.stocks
        for minutes in INTERVALS:
            families[f'stocks_{minutes}m'] = Family(
                f'stocks_{minutes}m', f'Stock strategy accounts, {minutes}-minute bars',
                f'Shortly after each {minutes}-minute bar during NYSE regular sessions',
                stocks.interval_locks[minutes],
                lambda should_pause, m=minutes: stocks.run_interval(m, should_pause),
                lambda m=minutes: [(key, b) for key, b in stocks.books.items() if key.endswith(f'_{m}m')],
                lambda: global_pause() or ('Stock experiments paused.' if stocks.is_paused() else None),
                stock_due(minutes), stock_health(minutes), deadline=300, provider='yahoo')
    return families


def measure(family):
    count, latest, observed, groups = 0, None, None, {}
    for group, book in family.books():
        marker = book.observation_marker()
        count += marker['count']
        bar = marker['latest_bar']
        if bar:
            latest = bar if latest is None or bar > latest else latest
            groups[group] = bar if group not in groups or bar > groups[group] else groups[group]
        seen = marker['latest_observed_at']
        if seen and (observed is None or parse(seen) > parse(observed)):
            observed = seen
    return {'count': count, 'latest_bar': latest, 'latest_observed_at': observed,
            'groups': groups if family.name == 'hourly' else {}}


# --------------------------------------------------------------------- store

FAMILY_COLUMNS = ('name', 'last_attempt_at', 'last_source', 'last_finished_at', 'last_outcome',
                  'last_message', 'checks', 'new_observations', 'consecutive_failures', 'last_success_at',
                  'last_new_observation_at', 'latest_bar', 'latest_observed_at', 'extra',
                  'running_since', 'running_source', 'hung_since', 'blocked_reason', 'blocked_at')


class CollectorStore:
    """Durable heartbeat, per-family outcome and a bounded attempt log."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS collector(id INTEGER PRIMARY KEY CHECK(id=1), pid INTEGER,
                started_at TEXT, heartbeat_at TEXT, stopped_at TEXT, status TEXT, note TEXT);
            CREATE TABLE IF NOT EXISTS families(name TEXT PRIMARY KEY, last_attempt_at TEXT,
                last_source TEXT, last_finished_at TEXT, last_outcome TEXT, last_message TEXT,
                checks INTEGER NOT NULL DEFAULT 0, new_observations INTEGER NOT NULL DEFAULT 0,
                consecutive_failures INTEGER NOT NULL DEFAULT 0, last_success_at TEXT,
                last_new_observation_at TEXT, latest_bar TEXT, latest_observed_at TEXT, extra TEXT,
                running_since TEXT, running_source TEXT, hung_since TEXT, blocked_reason TEXT, blocked_at TEXT);
            CREATE TABLE IF NOT EXISTS attempts(id INTEGER PRIMARY KEY, family TEXT, source TEXT,
                started_at TEXT, finished_at TEXT, outcome TEXT, new_observations INTEGER,
                latest_bar TEXT, message TEXT);''')

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=15)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    def _ensure(self, c, name):
        c.execute('INSERT OR IGNORE INTO families(name) VALUES(?)', (name,))

    def begin(self, pid, now):
        """Start a process; runs left unfinished by a previous process are closed as interrupted."""
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            stale = c.execute('SELECT name,running_since,running_source FROM families '
                              'WHERE running_since IS NOT NULL').fetchall()
            for row in stale:
                c.execute('INSERT INTO attempts(family,source,started_at,finished_at,outcome,new_observations,message) '
                          'VALUES(?,?,?,?,?,?,?)', (row['name'], row['running_source'], row['running_since'],
                          iso(now), 'interrupted', 0, 'The previous server process ended before this check '
                          'finished. Unfinished paper-book transactions are rolled back by SQLite.'))
            c.execute('UPDATE families SET running_since=NULL,running_source=NULL,hung_since=NULL')
            c.execute('INSERT OR REPLACE INTO collector VALUES(1,?,?,?,NULL,?,NULL)',
                      (pid, iso(now), iso(now), 'running'))
        return [row['name'] for row in stale]

    def heartbeat(self, now):
        with self.connect() as c:
            c.execute("UPDATE collector SET heartbeat_at=? WHERE id=1 AND status='running'", (iso(now),))

    def end(self, now, status, note):
        with self.connect() as c:
            c.execute('UPDATE collector SET stopped_at=?,status=?,note=? WHERE id=1', (iso(now), status, note))

    def collector(self):
        with self.connect() as c:
            row = c.execute('SELECT * FROM collector WHERE id=1').fetchone()
        return dict(row) if row else {}

    def family(self, name):
        with self.connect() as c:
            row = c.execute('SELECT * FROM families WHERE name=?', (name,)).fetchone()
        return dict(row) if row else {'name': name}

    def families(self):
        with self.connect() as c:
            return {row['name']: dict(row) for row in c.execute('SELECT * FROM families')}

    def set_marker(self, name, marker):
        with self.connect() as c:
            self._ensure(c, name)
            c.execute('UPDATE families SET latest_bar=?,latest_observed_at=?,extra=? WHERE name=?',
                      (marker['latest_bar'], marker['latest_observed_at'],
                       json.dumps({'groups': marker['groups']}), name))

    def set_blocked(self, name, reason, now):
        with self.connect() as c:
            self._ensure(c, name)
            c.execute('UPDATE families SET blocked_reason=?,blocked_at=? WHERE name=?', (reason, iso(now), name))

    def start(self, name, source, now):
        with self.connect() as c:
            self._ensure(c, name)
            c.execute('UPDATE families SET last_attempt_at=?,last_source=?,running_since=?,running_source=?,'
                      'hung_since=NULL,blocked_reason=NULL WHERE name=?', (iso(now), source, iso(now), source, name))

    def mark_hung(self, name, now):
        with self.connect() as c:
            c.execute('UPDATE families SET hung_since=? WHERE name=? AND hung_since IS NULL '
                      'AND running_since IS NOT NULL', (iso(now), name))

    def finish(self, name, source, started, finished, outcome, message, new, marker):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            self._ensure(c, name)
            row = dict(c.execute('SELECT * FROM families WHERE name=?', (name,)).fetchone())
            failures = (row['consecutive_failures'] + 1 if outcome in FAILURE else
                        0 if outcome in SUCCESS else row['consecutive_failures'])
            values = {'last_finished_at': iso(finished), 'last_outcome': outcome, 'last_message': message,
                      'checks': row['checks'] + (outcome in CHECKS), 'new_observations': row['new_observations'] + new,
                      'consecutive_failures': failures,
                      'last_success_at': iso(finished) if outcome in SUCCESS else row['last_success_at'],
                      'last_new_observation_at': iso(finished) if new else row['last_new_observation_at'],
                      'running_since': None, 'running_source': None, 'hung_since': None}
            if marker:
                values.update(latest_bar=marker['latest_bar'], latest_observed_at=marker['latest_observed_at'],
                              extra=json.dumps({'groups': marker['groups']}))
            c.execute('UPDATE families SET ' + ','.join(f'{k}=?' for k in values) + ' WHERE name=?',
                      (*values.values(), name))
            c.execute('INSERT INTO attempts(family,source,started_at,finished_at,outcome,new_observations,'
                      'latest_bar,message) VALUES(?,?,?,?,?,?,?,?)',
                      (name, source, iso(started), iso(finished), outcome, new,
                       marker['latest_bar'] if marker else row['latest_bar'], message))
            c.execute('DELETE FROM attempts WHERE id <= (SELECT max(id) FROM attempts) - 5000')

    def attempts(self, limit=30, family=None):
        with self.connect() as c:
            if family:
                rows = c.execute('SELECT * FROM attempts WHERE family=? ORDER BY id DESC LIMIT ?', (family, limit))
            else:
                rows = c.execute('SELECT * FROM attempts ORDER BY id DESC LIMIT ?', (limit,))
            return [dict(r) for r in rows]


# ----------------------------------------------------------------- collector

class Collector:
    def __init__(self, controller, path, *, clock=utc_now, tick_seconds=15, families=None,
                 hung_exit_minutes=0, on_hung_exit=None):
        self.controller = controller
        self.clock = clock
        self.store = CollectorStore(path)
        self.families = families if families is not None else build_families(controller)
        self.tick_seconds = tick_seconds
        self.hung_exit_seconds = (hung_exit_minutes or 0) * 60
        self.on_hung_exit = on_hung_exit
        self.stopping = False
        self.pid = os.getpid()
        self.workers = {}
        self._stop = threading.Event()
        self._thread = None
        self._blocked = {}
        self._hung_exit_sent = False
        self.interrupted = []
        gate = yahoo_gate()
        self.provider_gates = {'yahoo': gate} if gate else {}

    # lifecycle ---------------------------------------------------------
    def begin(self):
        """Record this process as the collector without starting the timer thread."""
        self.interrupted = self.store.begin(self.pid, self.clock())
        self.refresh_markers()

    def start(self):
        self.begin()
        self._thread = threading.Thread(target=self._loop, name='paper-collector', daemon=True)
        self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                self._log('scheduler tick failed')
            self._stop.wait(self.tick_seconds)

    def stop(self, timeout=10, reason='stopped'):
        """Stop scheduling, let running checks finish briefly, record the shutdown."""
        self.stopping = True
        self._stop.set()
        deadline = time.monotonic() + timeout
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(max(0, deadline - time.monotonic()))
        for name, (thread, _, _) in list(self.workers.items()):
            thread.join(max(0, deadline - time.monotonic()))
        unfinished = [name for name, (thread, _, _) in list(self.workers.items()) if thread.is_alive()]
        note = reason + (f'; unfinished checks: {", ".join(unfinished)}' if unfinished else '')
        try:
            self.store.end(self.clock(), 'stopped', note)
        except Exception:
            self._log('could not record shutdown')
        return unfinished

    def refresh_markers(self):
        for name, family in self.families.items():
            if family.lock.locked():
                continue
            try:
                self.store.set_marker(name, measure(family))
            except Exception:
                self._log(f'could not read {name} records')

    # scheduling --------------------------------------------------------
    def tick(self, now=None):
        now = now or self.clock()
        self.store.heartbeat(now)
        for name in self.families:
            try:
                self._check_hung(name, now)
                self.request(name, 'scheduler', now=now)
            except Exception:
                self._log(f'{name} scheduling failed')

    def _block(self, name, reason, now):
        if self._blocked.get(name) != reason:
            self._blocked[name] = reason
            self.store.set_blocked(name, reason, now)

    def should_pause(self, family):
        return self.stopping or family.paused() is not None

    def request(self, name, source, force=False, now=None):
        """Start one check if the family is unpaused, idle and due. Returns (started, reason).

        Scheduler, browser and manual requests all pass here and through the
        family's own lock, so none of them can duplicate another's work.
        """
        family = self.families.get(name)
        if family is None:
            return False, 'Unknown collection family.'
        now = now or self.clock()
        if self.stopping:
            return False, 'Collector is stopping.'
        reason = family.paused()
        if reason:
            self._block(name, reason, now)
            return False, reason
        if not family.lock.acquire(blocking=False):
            return False, 'A check for this family is already running.'
        started = False
        try:
            due, reason = family.due(now, self.store.family(name), source, force)
            if not due:
                self._block(name, reason, now)
                return False, reason
            self.store.start(name, source, now)
            self._blocked[name] = None
            if family.on_start:
                family.on_start()
            thread = threading.Thread(target=self._work, args=(family, source, now),
                                      name=f'paper-{name}', daemon=True)
            self.workers[name] = (thread, now, source)
            thread.start()
            started = True
            return True, 'Started.'
        finally:
            if not started:
                self.workers.pop(name, None)
                family.lock.release()

    def _work(self, family, source, started):
        kind, message, new, marker = 'error', 'Check did not complete.', 0, None
        try:
            before = measure(family)
            gate = self.provider_gates.get(family.provider)
            if gate:
                with gate:
                    kind, message = family.run(lambda: self.should_pause(family))
            else:
                kind, message = family.run(lambda: self.should_pause(family))
            marker = measure(family)
            new = max(0, marker['count'] - before['count'])
        except Exception as exc:
            kind, message = 'error', f'Collection failed ({type(exc).__name__}); existing paper state preserved.'
            self._log(f'{family.name} check failed')
        finally:
            outcome = ('new' if new else 'no_change') if kind == 'ok' else kind
            try:
                self.store.finish(family.name, source, started, self.clock(), outcome, message, new, marker)
            except Exception:
                self._log(f'could not record {family.name} outcome')
            finally:
                self.workers.pop(family.name, None)
                family.lock.release()

    def _check_hung(self, name, now):
        worker = self.workers.get(name)
        if not worker:
            return
        elapsed = (now - worker[1]).total_seconds()
        if elapsed > self.families[name].deadline:
            self.store.mark_hung(name, now)
        if (self.hung_exit_seconds and elapsed > self.hung_exit_seconds and self.on_hung_exit
                and not self._hung_exit_sent):
            self._hung_exit_sent = True
            self.on_hung_exit(f'{name} check has not returned for {elapsed / 60:.0f} minutes')

    def join(self, timeout=10):
        """Wait for running checks (tests and shutdown)."""
        deadline = time.monotonic() + timeout
        for _, (thread, _, _) in list(self.workers.items()):
            thread.join(max(0, deadline - time.monotonic()))
        return not any(t.is_alive() for t, _, _ in list(self.workers.values()))

    # status ------------------------------------------------------------
    def status(self, now=None):
        now = now or self.clock()
        row = self.store.collector()
        states = self.store.families()
        age = _since(now, row.get('heartbeat_at'))
        alive = bool(self._thread and self._thread.is_alive()) and not self.stopping
        running = alive and age is not None and age <= max(3 * self.tick_seconds, 90)
        families = []
        for name, family in self.families.items():
            state = states.get(name, {'name': name})
            paused = family.paused()
            active = name in self.workers
            hung = bool(state.get('hung_since')) and active
            expectation, detail = family.health(now, state)
            failing = (state.get('consecutive_failures') or 0) > 0
            if paused:
                shown = 'paused'
            elif hung:
                shown = 'hung'
            elif failing and expectation not in ('up_to_date', 'market_closed'):
                shown = 'failing'
            else:
                shown = expectation
            if hung:
                detail = (f'The check started {et(parse(state.get("running_since")))} has not returned. No second '
                          'request starts until it does; restart the server if this persists. ' + detail)
            families.append({
                'name': name, 'label': family.label, 'schedule': family.schedule,
                'state': shown, 'state_label': STATE_LABELS.get(shown, shown), 'expectation': expectation,
                'detail': detail, 'paused': bool(paused), 'pause_reason': paused, 'running': active,
                'hung': hung, 'failing': failing,
                **{k: state.get(k) for k in FAMILY_COLUMNS if k not in ('name', 'extra')},
                'groups': json.loads(state.get('extra') or '{}').get('groups', {})})
        return {'enabled': True, 'mode': 'PAPER ONLY', 'pid': row.get('pid'), 'this_pid': self.pid,
                'started_at': row.get('started_at'), 'heartbeat_at': row.get('heartbeat_at'),
                'heartbeat_age_seconds': age, 'status': row.get('status'), 'note': row.get('note'),
                'running': running, 'tick_seconds': self.tick_seconds, 'now': iso(now),
                'interrupted_on_start': self.interrupted,
                'families': families, 'recent_attempts': self.store.attempts(30), 'notes': NOTES}

    def _log(self, message):
        print(f'{utc_now().isoformat()} collector: {message}\n{traceback.format_exc()}', file=sys.stderr, flush=True)


# ------------------------------------------------------------ single process

class InstanceLock:
    """Exclusive OS file lock held for the life of the server process.

    The OS releases it when the process exits, however it exits, so a crashed
    server never blocks its replacement. A second server for the same runtime
    cannot acquire it and exits without touching anything.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.handle = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, 'a+')
        try:
            handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self.handle = handle
        return True

    def release(self):
        if not self.handle:
            return
        try:
            self.handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle, fcntl.LOCK_UN)
        except OSError:
            pass
        self.handle.close()
        self.handle = None
