"""Repeatable coverage and matched-comparison audit of unattended paper collection.

Read-only. Opens every SQLite file with mode=ro, parses two log files for process
evidence, makes no network request, forces no cycle and writes nothing. Pin
--since and --until to reproduce a report exactly.

It answers, for one window (default: first collector attempt to now):
  * which bars should have been recorded (bundled NYSE calendar, 24/7 crypto) and which were;
  * per session, whether it was complete (closed and not clipped by either end of the window)
    and fully recorded, with coverage so far reported separately for clipped and open periods;
  * why each gap happened, judged on the missing bars' own readiness windows: no collector
    attempt recorded, provider/data failure (held or error checks) or checks accepted without
    the bar appearing. Machine sleep is never proven from these files;
  * held accounts, error reasons, recorded process interruptions and observation latency;
  * costs and exposure inside the window;
  * matched strategy/reference periods, compared only on bars both accounts recorded and
    never across a gap. The minimum-session gate counts complete sessions fully matched by
    both accounts; observed and partial days are counted separately.

It reports insufficient samples instead of a verdict. There is no annualised figure, no
projection and no claim of profit. Meeting a sample threshold lets a difference be
discussed; it is still not evidence of an edge.

    python -B scripts/coverage_report.py
    python -B scripts/coverage_report.py --since 2026-09-28T16:40:18+00:00 --until 2026-09-30T21:00:00+00:00
    python -B scripts/coverage_report.py --json
"""
import argparse
from collections import Counter
from datetime import datetime, time as dtime, timedelta, timezone
import json
from pathlib import Path
import re
import sqlite3
import statistics
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from collection_report import aware, read_only   # noqa: E402  (read-only helpers shared with the baseline)
from collector import CALENDAR_YEARS, CORE_READY, FAILURE, SUCCESS, _spacing   # noqa: E402
from stock_experiments import NY, session_close_for   # noqa: E402

UTC = timezone.utc
# How long after a bar becomes collectable it may still be 'pending' rather than missed,
# and how long after the bar's end the provider delay applies. Both mirror collector.py.
# 'span' is how long a bar stays the latest completed bar (the collector records only that one),
# so a bar's readiness window is [ready, ready + span). The core has no fixed span: its window
# lasts until the next session's bar is ready.
KINDS = {
    'core': {'label': 'Core daily', 'delay': None, 'grace': 30, 'min_intervals': 20, 'span': None},
    'hourly_listed': {'label': 'Hourly listed funds', 'delay': 5, 'grace': 10, 'min_intervals': 100, 'span': 60},
    'hourly_crypto': {'label': 'Hourly crypto', 'delay': 5, 'grace': 10, 'min_intervals': 100, 'span': 60},
    'active': {'label': 'Active 15m crypto', 'delay': 2, 'grace': 8, 'min_intervals': 500, 'span': 15},
    'stocks_5m': {'label': 'Stock labs 5m', 'delay': 2, 'grace': 5, 'min_intervals': 500, 'span': 5},
    'stocks_15m': {'label': 'Stock labs 15m', 'delay': 2, 'grace': 5, 'min_intervals': 200, 'span': 15},
}
COLLECTOR_FAMILY = {'core': 'core', 'hourly_listed': 'hourly', 'hourly_crypto': 'hourly', 'active': 'active',
                    'stocks_5m': 'stocks_5m', 'stocks_15m': 'stocks_15m'}
MIN_SESSIONS = 20            # complete sessions (UTC days for 24/7 markets) fully matched by both accounts before a comparison may be discussed
MIN_FILLS = 10              # completed fills by the strategy account in the window
SILENCE_SLACK = timedelta(minutes=5)
SILENT_BAR_SHARE = 0.5       # a missing bar counts as silent when at least this share of its window had no attempt
SESSION_BASIS = 'complete_closed_sessions_matched_by_strategy_and_reference'
SILENCE_CAUSE = ('no attempts recorded by any family: computer asleep or off, the server process not running, '
                 'or the process suspended or stuck; these are not distinguishable from the databases')
TOP = 3                      # message variants kept per gap or error group


def utc(stamp):
    return aware(stamp) if stamp else None


def iso(value):
    return value.astimezone(UTC).isoformat() if value else None


def key_time(key):
    """UTC time of an observation label. A daily label is its 16:00 New York close."""
    if len(key) <= 10:
        return datetime.combine(datetime.fromisoformat(key).date(), dtime(16), tzinfo=NY).astimezone(UTC)
    return utc(key)


def session_of(kind, key):
    """Calendar day an observation belongs to (New York for NYSE families, UTC for 24/7)."""
    when = key_time(key)
    return (when.astimezone(UTC) if kind in ('hourly_crypto', 'active') else when.astimezone(NY)).date().isoformat()


# ------------------------------------------------------------ expected bars

def ny_days(start, until):
    day, last = start.astimezone(NY).date() - timedelta(days=1), until.astimezone(NY).date()
    while day <= last:
        yield day
        day += timedelta(days=1)


def expected_bars(kind, start, until):
    """[(label, ready_utc)] for bars that complete at or after `start` and were due by `until`.

    Labels match observations.asof. A bar is due once it is ready plus its grace has passed.
    Empty for NYSE families outside the bundled 2026-2027 calendar (see calendar_covered).
    """
    spec = KINDS[kind]
    grace = timedelta(minutes=spec['grace'])
    out = []

    def add(label, end, ready):
        if end >= start and ready + grace <= until:
            out.append((label, ready))

    if kind == 'core':
        for day in ny_days(start, until):
            if session_close_for(day):
                ready = datetime.combine(day, CORE_READY, tzinfo=NY).astimezone(UTC)
                add(day.isoformat(), ready, ready)
        return out
    delay = timedelta(minutes=spec['delay'])
    if kind in ('hourly_crypto', 'active'):
        step = timedelta(hours=1) if kind == 'hourly_crypto' else timedelta(minutes=15)
        seconds = int(step.total_seconds())
        epoch = int(start.timestamp())
        end = datetime.fromtimestamp(-(-epoch // seconds) * seconds, UTC)
        while end + delay + grace <= until:
            add(end.isoformat(), end, end + delay)
            end += step
        return out
    for day in ny_days(start, until):
        close = session_close_for(day)
        if close is None:
            continue
        open_dt = datetime.combine(day, dtime(9, 30), tzinfo=NY)
        close_dt = datetime.combine(day, close, tzinfo=NY)
        if kind == 'hourly_listed':
            # Same ends as collector.listed_hourly_expected_bar: hourly from 09:30, last bar capped at 16:00.
            four = datetime.combine(day, dtime(16), tzinfo=NY)
            begin = open_dt
            while begin < close_dt:
                end = min(begin + timedelta(hours=1), four).astimezone(UTC)
                add(end.isoformat(), end, end + delay)
                begin += timedelta(hours=1)
        else:
            minutes = int(kind.split('_')[1][:-1])
            end = open_dt + timedelta(minutes=minutes)
            while end <= close_dt:
                add(end.astimezone(UTC).isoformat(), end.astimezone(UTC), end.astimezone(UTC) + delay)
                end += timedelta(minutes=minutes)
    return out


def full_session(kind, day):
    """[(label, ready_utc)] of every bar of one session, whatever the report window (calendar-driven).

    Uses the same expected_bars rules, so holidays, 13:00 early closes and 24/7 UTC days
    follow the bundled calendar rather than a count of weekdays.
    """
    begin = datetime.fromisoformat(day).replace(tzinfo=UTC)
    return [(label, ready) for label, ready in expected_bars(kind, begin, begin + timedelta(days=3))
            if session_of(kind, label) == day]


def session_table(kind, expected, start, until):
    """{session: info} for every session holding a bar the window expects.

    A session is complete only when the window holds every bar of it: none before `start`
    (start-clipped) and none still pending at `until` (end-clipped or open). Coverage inside a
    clipped session is coverage so far, not completeness.
    """
    inside = {label for label, _ in expected}
    due_by_day = {}
    for label, _ in expected:
        due_by_day.setdefault(session_of(kind, label), []).append(label)
    table = {}
    for day in sorted(due_by_day):
        full = full_session(kind, day)
        # The core's window edge is its ready time; every other bar's is the bar end itself.
        edge = (lambda label, ready: ready) if kind == 'core' else (lambda label, ready: key_time(label))
        outside = [(label, ready) for label, ready in full if label not in inside]
        before = [label for label, ready in outside if edge(label, ready) < start]
        after = [label for label, ready in outside if label not in before]
        last = max((edge(label, ready) for label, ready in full), default=None)
        # All bar labels can be due before the UTC day closes (23:00 hourly, 23:45 active).
        # The sample gate requires a closed day as well as every label, so wait until midnight.
        closes = (datetime.fromisoformat(day).replace(tzinfo=UTC) + timedelta(days=1)
                  if kind in ('hourly_crypto', 'active') else last)
        still_open = bool(closes and until < closes)
        end_clipped = bool(after) or still_open
        status = ('complete' if full and not outside and not still_open else
                  'start_and_end_clipped' if before and end_clipped else 'start_clipped' if before else 'end_clipped')
        table[day] = {'labels': [label for label, _ in full], 'due': due_by_day[day], 'status': status,
                      'complete': status == 'complete', 'start_clipped': bool(before), 'end_clipped': end_clipped,
                      'still_open_at_until': still_open}
    return table


def calendar_covered(start, until):
    return all(year in CALENDAR_YEARS for year in (start.astimezone(NY).year, until.astimezone(NY).year))


# ------------------------------------------------------------- account data

def discover(runtime):
    """Every collected paper account, tagged with its kind and comparison role."""
    accounts = []

    def add(path, kind, name, role, pair):
        if path.exists():
            accounts.append({'id': name, 'path': path, 'kind': kind, 'role': role, 'pair': pair})

    add(runtime / 'paper.sqlite3', 'core', 'core', 'strategy', 'core')
    for path in sorted(runtime.glob('hourly-v1/core-benchmark-*.sqlite3')):
        add(path, 'core', 'benchmark ' + path.stem.removeprefix('core-benchmark-'), 'reference', 'core')
    for path in sorted(runtime.glob('research-v1/shadow-*.sqlite3')):
        role = 'reference' if path.stem == 'shadow-baseline' else 'strategy'
        add(path, 'core', 'research ' + path.stem, role, 'research shadow')
    for path in sorted(runtime.glob('hourly-v1/*__*.sqlite3')):
        symbol, strategy = path.stem.split('__', 1)
        kind = 'hourly_crypto' if symbol.endswith('-USD') else 'hourly_listed'
        add(path, kind, f'{symbol} {strategy}', 'reference' if strategy == 'buy-hold' else 'strategy', symbol)
    add(runtime / 'active-15m-v1' / 'active.sqlite3', 'active', 'active', 'strategy', 'active')
    add(runtime / 'active-15m-v1' / 'reference.sqlite3', 'active', 'active reference', 'reference', 'active')
    for minutes in (5, 15):
        for path in sorted(runtime.glob(f'stock-experiments-v1/stock-lab-v1-*_{minutes}m.sqlite')):
            strategy = path.stem.removeprefix('stock-lab-v1-').removesuffix(f'_{minutes}m')
            add(path, f'stocks_{minutes}m', f'{strategy} {minutes}m', 'reference' if strategy == 'buy_hold' else 'strategy',
                f'stocks {minutes}m')
    return accounts


def load_account(account):
    """Observations and fills of one account; unreadable accounts are flagged, never guessed at."""
    try:
        con = read_only(account['path'])
    except sqlite3.DatabaseError as exc:
        return {'error': str(exc), 'observations': [], 'fills': [], 'initial': None}
    try:
        rows = con.execute('SELECT asof,observed_at,equity,cash FROM observations ORDER BY asof').fetchall()
        fills = con.execute('SELECT asof,ticker,shares,price,cost FROM trades ORDER BY id').fetchall()
        initial = con.execute('SELECT initial FROM book WHERE id=1').fetchone()
        return {'error': None, 'initial': initial[0] if initial else None,
                'observations': [{'asof': r['asof'], 'observed_at': utc(r['observed_at']),
                                  'equity': r['equity'], 'cash': r['cash']} for r in rows],
                'fills': [dict(r) for r in fills]}
    except sqlite3.DatabaseError as exc:
        return {'error': str(exc), 'observations': [], 'fills': [], 'initial': None}
    finally:
        con.close()


def load_attempts(runtime):
    path = runtime / 'collector.sqlite3'
    empty = {'installed': False, 'attempts': [], 'process': {}, 'families': {}}
    if not path.exists():
        return empty
    try:
        con = read_only(path)
    except sqlite3.DatabaseError:
        return empty
    try:
        attempts = [{'family': r['family'], 'source': r['source'], 'started': utc(r['started_at']),
                     'finished': utc(r['finished_at']), 'outcome': r['outcome'], 'new': r['new_observations'] or 0,
                     'message': r['message'] or ''}
                    for r in con.execute('SELECT * FROM attempts ORDER BY started_at, id')]
        process = con.execute('SELECT * FROM collector WHERE id=1').fetchone()
        families = {r['name']: dict(r) for r in con.execute('SELECT * FROM families')}
    except sqlite3.DatabaseError:
        return empty
    finally:
        con.close()
    return {'installed': True, 'attempts': attempts, 'process': dict(process) if process else {}, 'families': families}


def process_evidence(runtime):
    """Process starts and relaunch refusals recorded in the hidden service's own logs.

    Only two fixed line shapes are read, so nothing else in a log reaches the report.
    """
    pids, refusals = [], []
    for name in ('service.log', 'server.log'):
        try:
            text = (runtime / name).read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        pids += [int(m) for m in re.findall(r'^PAPER ONLY.*\(pid (\d+),', text, re.M)]
    try:
        text = (runtime / 'service-errors.log').read_text(encoding='utf-8', errors='replace')
        refusals = [utc(m) for m in re.findall(r'^(\d{4}-\d\d-\d\dT[\d:.]+\+00:00) another paper server already owns', text, re.M)]
    except OSError:
        pass
    return {'logged_process_starts': len(pids), 'pids': pids,
            'relaunch_refusals': {'count': len(refusals), 'first': iso(min(refusals, default=None)),
                                  'last': iso(max(refusals, default=None))}}


def read_holds(runtime):
    try:
        data = json.loads((runtime / 'hourly-v1' / 'quality.json').read_text(encoding='utf-8'))
        return {'observed_at': data.get('observed_at'), 'holds': dict(data.get('holds') or {})}
    except (OSError, ValueError, AttributeError):
        return {'observed_at': None, 'holds': {}}


# --------------------------------------------------------- silence and errors

def silent_periods(attempts, start, until):
    """Stretches with no attempt from any family, judged against the hourly family's 5-minute cadence
    (30 minutes when it is failing).

    Every family's attempts count as evidence that the process was running, so another family's
    check inside an hourly retry interval ends the silence. The databases still cannot tell sleep,
    shutdown, a suspended or stuck process and a pause apart; all look like silence. A check the
    collector recorded as interrupted names its own cause.
    """
    rows = sorted((a for a in attempts if start <= a['started'] <= until), key=lambda a: a['started'])
    interrupted = {a['started'] for a in rows if a['outcome'] == 'interrupted'}
    periods, failures, previous = [], 0, None
    # Backoff comes from the hourly family when it is present; otherwise from whatever family recorded.
    driver = 'hourly' if any(a['family'] == 'hourly' for a in rows) else None

    def allowed():
        return timedelta(seconds=_spacing(300, failures, 1800)) + SILENCE_SLACK

    def cause(begin):
        return ('process ended while a check was running (recorded as interrupted)' if begin in interrupted else
                SILENCE_CAUSE)

    for row in rows:
        if previous is None:
            if row['started'] - start > allowed():
                periods.append({'from': start, 'to': row['started'], 'cause': 'collector had not started recording'})
        elif row['started'] - previous > allowed():
            resumed = Counter(a['outcome'] for a in rows if row['started'] <= a['started'] <= row['started'] + timedelta(seconds=90))
            periods.append({'from': previous, 'to': row['started'], 'cause': cause(previous),
                            'outcomes_on_resume': dict(resumed)})
        if driver is None or row['family'] == driver:
            failures = failures + 1 if row['outcome'] in FAILURE else 0 if row['outcome'] in SUCCESS else failures
        previous = row['started']
    if previous is not None and until - previous > allowed():
        periods.append({'from': previous, 'to': until, 'cause': cause(previous) + '; still silent at the end of the window'})
    for p in periods:
        p['minutes'] = round((p['to'] - p['from']).total_seconds() / 60, 1)
    return periods


def group_messages(rows, limit=TOP):
    counts = Counter(re.sub(r'\d{4}-\d\d-\d\dT[\d:.+\-]+', '<time>', r['message'])[:160] for r in rows)
    return [{'message': m, 'count': n} for m, n in counts.most_common(limit)]


def attempt_summary(attempts, start, until):
    inside = [a for a in attempts if start <= a['started'] <= until]
    by_family = {}
    for family in sorted({a['family'] for a in inside}):
        rows = [a for a in inside if a['family'] == family]
        outcomes = Counter(a['outcome'] for a in rows)
        failed = [a for a in rows if a['outcome'] in FAILURE or a['outcome'] == 'interrupted']
        by_family[family] = {'checks': len(rows), 'outcomes': dict(outcomes), 'reasons': group_messages(failed, 6),
                             'first_failure': iso(min((a['started'] for a in failed), default=None)),
                             'last_failure': iso(max((a['started'] for a in failed), default=None))}
    return by_family


# ------------------------------------------------------------------ coverage

def missing_windows(kind, run, ready, order, until):
    """[(begin, end)] during which each missing bar was the latest completed bar and could have been recorded.

    The collector only records the latest completed bar, so a bar missing means no accepted check
    ran in [ready, ready + span) (core: until the next session's bar is ready), cut at `until`.
    """
    position = {label: i for i, label in enumerate(order)}
    span = KINDS[kind]['span']
    windows = []
    for label in run:
        begin = ready[label]
        following = order[position[label] + 1] if position[label] + 1 < len(order) else None
        end = begin + timedelta(minutes=span) if span else (ready[following] if following else until)
        if span and following and begin < ready[following] < end:
            end = ready[following]
        if kind in ('stocks_5m', 'stocks_15m'):
            # stock_due() follows session_open(): collection stops five minutes after the
            # exchange close, even when the final bar would remain latest for longer.
            local_day = key_time(label).astimezone(NY).date()
            close = session_close_for(local_day)
            if close:
                cutoff = (datetime.combine(local_day, close, tzinfo=NY) + timedelta(minutes=5)).astimezone(UTC)
                end = min(end, cutoff)
        windows.append((begin, min(end, until)))
    return windows


def overlap_seconds(begin, end, periods):
    return sum(max(0.0, (min(end, p['to']) - max(begin, p['from'])).total_seconds()) for p in periods)


INTERPRETATION = {
    'collector_silent': ("No collector attempt was recorded in these bars' readiness windows. Machine sleep or shutdown, "
                         'a suspended or stuck process and a pause cannot be told apart from these files.'),
    'provider_or_data_failure': ("Checks ran in these bars' readiness windows but the provider or data was rejected or "
                                 'errored; see top_messages.'),
    'checks_accepted_without_bar': "Checks inside these bars' readiness windows were accepted but the bar did not appear.",
    'no_attempt_recorded': ("No attempt from this family was recorded in these bars' readiness windows; "
                             'the available files do not establish why or whether other collector activity occurred.'),
}


def attribute_gap(attempts, silent, windows, recovery_to=None):
    """Why bars were missed, from what the collector recorded inside each missing bar's own window.

    A check that ran after the last missing bar's window (the recovery check that finally recorded
    the next bar) and a silence that only overlaps the recovery interval are not causes of the
    missing bars; the recovery checks are counted separately.
    """
    def inside(when, window):
        return window[0] <= when < window[1]

    def within(when):
        return any(inside(when, w) for w in windows)

    failed = [a for a in attempts if a['outcome'] in FAILURE and within(a['started'])]
    accepted = [a for a in attempts if a['outcome'] in SUCCESS and within(a['started'])]
    last = max(end for _, end in windows)
    recovery = [a for a in attempts if a['outcome'] in SUCCESS and recovery_to is not None and last <= a['started'] <= recovery_to]
    quiet = [p for p in silent if any(overlap_seconds(b, e, [p]) > 0 for b, e in windows)]
    per_bar = []
    for window in windows:
        length = (window[1] - window[0]).total_seconds()
        share = overlap_seconds(window[0], window[1], quiet)
        if length and share >= SILENT_BAR_SHARE * length:
            per_bar.append('collector_silent')
        elif any(inside(a['started'], window) for a in failed):
            per_bar.append('provider_or_data_failure')
        elif any(inside(a['started'], window) for a in accepted):
            per_bar.append('checks_accepted_without_bar')
        elif share > 0:
            per_bar.append('collector_silent')
        else:
            per_bar.append('no_attempt_recorded')
    counts = Counter(per_bar)
    priority = list(INTERPRETATION)
    causes = sorted(counts, key=lambda c: (-counts[c], priority.index(c)))
    if failed and 'provider_or_data_failure' not in causes:
        causes.append('provider_or_data_failure')      # visible even when it is not the main cause
    if quiet and 'collector_silent' not in causes:
        causes.append('collector_silent')
    silent_minutes = sum(overlap_seconds(b, e, quiet) for b, e in windows) / 60
    interpretation = ' '.join(INTERPRETATION[c] for c in causes)
    if any(p['cause'].startswith('process ended') for p in quiet):
        interpretation += ' A check recorded as interrupted shows the process ended mid-check.'
    return {'primary': causes[0], 'causes': causes, 'bar_causes': dict(counts),
            'failed_checks': dict(Counter(a['outcome'] for a in failed)), 'accepted_checks': len(accepted),
            'recovery_checks': len(recovery), 'missing_window_to': iso(last),
            'silent_overlap_minutes': round(silent_minutes, 1),
            'silent_periods': [{'from': iso(p['from']), 'to': iso(p['to']), 'cause': p['cause']} for p in quiet],
            'top_messages': group_messages(failed), 'interpretation': interpretation}


def family_coverage(kind, accounts, data, expected, silent, attempts, start, until, table=None):
    """Family-level coverage: a bar counts as recorded if any account of the family recorded it."""
    labels = [label for label, _ in expected]
    ready = dict(expected)
    recorded = {}
    for acct in accounts:
        for o in data[acct['id']]['observations']:
            if o['observed_at'] and start <= o['observed_at'] <= until:
                first = recorded.get(o['asof'])
                recorded[o['asof']] = min(first, o['observed_at']) if first else o['observed_at']
    got = [label for label in labels if label in recorded]
    missing = [label for label in labels if label not in recorded]
    table = table or session_table(kind, expected, start, until)
    sessions = []
    for day, info in table.items():
        due = info['due']
        have = [label for label in due if label in recorded]
        sessions.append({'session': day,
                         # expected/recorded/missing/coverage_pct are coverage so far: bars the window expects of this session.
                         'expected': len(due), 'recorded': len(have), 'missing': len(due) - len(have),
                         'coverage_pct': round(100 * len(have) / len(due), 1),
                         # full: a complete session (closed, not clipped) with every bar recorded.
                         'full': info['complete'] and len(have) == len(due),
                         'complete': info['complete'], 'status': info['status'],
                         'full_session_bars': len(info['labels']),
                         'covered_so_far_full': len(have) == len(due),
                         # Bars before the window start are excluded, not missed; bars still pending at the end are not due yet.
                         'clipped_by_window_start': info['start_clipped'], 'clipped_by_window_end': info['end_clipped'],
                         'still_open_at_until': info['still_open_at_until']})
    gaps, run = [], []
    order = sorted(labels, key=key_time)
    for index, label in enumerate(order + [None]):
        if label is not None and label not in recorded:
            run.append(label)
            continue
        if run:
            last = order.index(run[-1])
            following = order[last + 1] if last + 1 < len(order) else None
            begin = ready[run[0]]
            finish = recorded[following] if following else until
            windows = missing_windows(kind, run, ready, order, until)
            why = attribute_gap(attempts, silent, windows, recorded[following] if following else None)
            # window_to still runs through the recovery observation; missing_window_to ends with the missing bars' own windows.
            gaps.append({'from_bar': run[0], 'to_bar': run[-1], 'missing_bars': len(run), 'sessions': sorted({session_of(kind, x) for x in run}),
                         'window_from': iso(begin), 'window_to': iso(finish), 'recovered_with': following,
                         'recovery_observed_at': iso(recorded[following]) if following else None, **why})
            run = []
    lag = [(recorded[l] - ready[l]).total_seconds() / 60 for l in got]
    late = [x for x in lag if x > KINDS[kind]['grace']]
    before = sum(1 for a in accounts for o in data[a['id']]['observations'] if o['observed_at'] and o['observed_at'] < start)
    extra = sorted(set(recorded) - set(labels), key=key_time)
    return {'kind': kind, 'label': KINDS[kind]['label'], 'accounts': len(accounts), 'expected_bars': len(labels),
            'recorded_bars': len(got), 'missing_bars': len(missing),
            'coverage_pct': round(100 * len(got) / len(labels), 1) if labels else None,
            'sessions': sessions, 'full_sessions': sum(1 for s in sessions if s['full']),
            'complete_sessions': sum(1 for s in sessions if s['complete']),
            'partial_sessions': sum(1 for s in sessions if not s['complete']),
            'sessions_note': ('full_sessions counts complete (closed, unclipped) sessions with every bar recorded; start-clipped, '
                              'end-clipped and still-open sessions are partial and report coverage so far only.'),
            'gaps': gaps,
            'observation_latency_minutes': ({'median': round(statistics.median(lag), 1), 'max': round(max(lag), 1),
                                             'late_count': len(late), 'late_threshold': KINDS[kind]['grace']} if lag else None),
            'late_bars': [{'bar': l, 'minutes_after_ready': round(x, 1)} for l, x in zip(got, lag)
                          if x > KINDS[kind]['grace']][:10],
            'observed_before_window_excluded': before, 'recorded_outside_expected': extra[:10],
            'recorded_outside_expected_count': len(extra)}


# ------------------------------------------------------------------ economics

ECONOMICS_BASIS = ('whole_window: every bar this account recorded inside the window, including bars outside the matched '
                    'intervals that the comparison returns use. Not the costs or exposure of the matched return alone.')


def economics(info, start, until):
    """Costs, fills and exposure from every observation recorded inside the window (whole-window basis)."""
    rows = [o for o in info['observations'] if o['observed_at'] and start <= o['observed_at'] <= until]
    bars = {o['asof'] for o in rows}
    fills = [f for f in info['fills'] if f['asof'] in bars]
    exposure = [min(1.0, max(0.0, 1 - o['cash'] / o['equity'])) for o in rows if o['equity'] and o['equity'] > 0]
    mean_equity = statistics.fmean(o['equity'] for o in rows) if rows else None
    notional = sum(abs(f['shares'] * f['price']) for f in fills)
    return {'basis': ECONOMICS_BASIS, 'observations': len(rows), 'fills': len(fills), 'fees_paid': round(sum(f['cost'] for f in fills), 2),
            'fees_pct_of_initial': round(100 * sum(f['cost'] for f in fills) / info['initial'], 3) if info['initial'] else None,
            'turnover_pct_of_mean_equity': round(100 * notional / mean_equity, 1) if mean_equity else None,
            'mean_exposure_pct': round(100 * statistics.fmean(exposure), 1) if exposure else None,
            'bars_invested_pct': round(100 * sum(1 for x in exposure if x > 0.001) / len(exposure), 1) if exposure else None,
            'max_exposure_pct': round(100 * max(exposure), 1) if exposure else None}


def compare(kind, strategy, reference, data, expected, start, until, table=None):
    """Strategy against its reference on bars both recorded, never across a gap.

    The minimum-session gate counts complete sessions in which every bar was recorded by both
    accounts. Days with any matched bar are still counted, but only as observed sessions.
    """
    def window(acct):
        return {o['asof']: o['equity'] for o in data[acct['id']]['observations']
                if o['observed_at'] and start <= o['observed_at'] <= until}
    mine, theirs = window(strategy), window(reference)
    common = sorted(set(mine) & set(theirs), key=key_time)
    common_set = set(common)
    order = sorted({label for label, _ in expected} | set(mine) | set(theirs), key=key_time)
    position = {label: i for i, label in enumerate(order)}
    segments, current = [], []
    for label in common:
        if current and position[label] != position[current[-1]] + 1:
            segments.append(current)
            current = []
        current.append(label)
    if current:
        segments.append(current)

    def compounded(equity):
        value = 1.0
        for seg in segments:
            if len(seg) > 1 and equity[seg[0]] > 0:
                value *= equity[seg[-1]] / equity[seg[0]]
        return round(100 * (value - 1), 3)

    table = table if table is not None else session_table(kind, expected, start, until)
    intervals = sum(len(s) - 1 for s in segments)
    observed = sorted({session_of(kind, label) for label in common})
    eligible = [day for day in observed if day in table and table[day]['complete']
                and all(label in common_set for label in table[day]['labels'])]
    fills = economics(data[strategy['id']], start, until)['fills']
    need = {'sessions': MIN_SESSIONS, 'intervals': KINDS[kind]['min_intervals'], 'fills': MIN_FILLS}
    have = {'sessions': len(eligible), 'intervals': intervals, 'fills': fills, 'observed_sessions': len(observed)}
    short = []
    for name in need:
        if have[name] >= need[name]:
            continue
        if name == 'sessions':
            short.append(f"sessions {have[name]}/{need[name]} complete fully matched "
                         f"({len(observed)} observed, {len(observed) - len(eligible)} partial or not fully matched)")
        else:
            short.append(f'{name} {have[name]}/{need[name]}')
    result = {'pair': strategy['pair'], 'kind': kind, 'strategy': strategy['id'], 'reference': reference['id'],
              'matched_bars': len(common), 'segments': len(segments), 'return_intervals': intervals,
              # `sessions` keeps its old meaning (days with at least one matched bar); the gate uses the complete count below.
              'sessions': len(observed), 'observed_sessions': len(observed),
              'complete_matched_sessions': len(eligible), 'partial_matched_sessions': len(observed) - len(eligible),
              'eligible_session_dates': eligible,
              'strategy_only_bars': len(set(mine) - set(theirs)), 'reference_only_bars': len(set(theirs) - set(mine)),
              'need': need, 'have': have,
              'sessions_basis': SESSION_BASIS,
              'fills_basis': 'whole_window (the sample gate counts all fills in the window, not only fills inside matched intervals)',
              'gate_basis': {'sessions': SESSION_BASIS, 'intervals': 'matched_contiguous_runs',
                             'fills': 'whole_window_fills_by_the_strategy_account'},
              'return_basis': 'matched_contiguous_runs_only',
              'verdict': 'insufficient_sample' if short else 'sample_thresholds_met_descriptive_only',
              'insufficient_because': short}
    if common and len(common) > 1:
        # Descriptive only. Compounded over contiguous matched runs, so moves across a gap are excluded.
        result.update(descriptive_strategy_return_pct=compounded(mine), descriptive_reference_return_pct=compounded(theirs))
        result['descriptive_difference_pp'] = round(result['descriptive_strategy_return_pct']
                                                    - result['descriptive_reference_return_pct'], 3)
    else:
        result.update(descriptive_strategy_return_pct=None, descriptive_reference_return_pct=None,
                      descriptive_difference_pp=None)
    return result


# --------------------------------------------------------------------- build

def build(runtime, since=None, until=None, now=None):
    runtime = Path(runtime)
    now = now or datetime.now(UTC)
    until = until or now
    collector = load_attempts(runtime)
    attempts = collector['attempts']
    start = since or min((a['started'] for a in attempts), default=None)
    if start is None:
        raise SystemExit('No collector attempts recorded; pass --since to name the window start.')
    if start > until:
        raise SystemExit('--since is after --until.')
    accounts = discover(runtime)
    data = {a['id']: load_account(a) for a in accounts}
    holds = read_holds(runtime)
    covered = calendar_covered(start, until)
    silent = silent_periods(attempts, start, until)
    in_window = [a for a in attempts if start <= a['started'] <= until]
    retained_from = min((a['started'] for a in attempts), default=None)
    notes = ['Window is [since, until]. Bars that completed before `since` are excluded; bars still inside their grace period at `until` are pending, not missed.',
             'Silence is inferred from missing collector attempts of every family. The databases cannot distinguish sleep, shutdown, a suspended or stuck process and a global pause, so no gap is attributed to a proven machine state.',
             'Gap causes are judged on the readiness window of each missing bar; a recovery check or a silence that only overlaps the recovery interval is not a cause of the missing bars.',
             'A session is complete only if the window holds all of its bars: start-clipped, end-clipped and still-open sessions are partial and show coverage so far. The minimum-session gate counts complete sessions fully matched by both accounts.',
             'No bar is ever backfilled, so a missed bar stays missed; comparisons use only bars both accounts recorded and never span a gap.',
             'Returns are descriptive and unannualised. Sample thresholds gate discussion only; they are not evidence of an edge. Nothing here is a projection.']
    if not covered:
        notes.append('The bundled NYSE calendar does not cover this window; NYSE-family expectations are empty and cannot be judged.')
    if len(attempts) >= 5000:
        notes.append('collector.sqlite3 keeps only the latest 5,000 attempts; earlier failures in a long window may be missing.')
    if retained_from and retained_from > start:
        notes.append(f'The attempt log starts {iso(retained_from)}, after the window start; earlier causes cannot be attributed.')
    if not in_window:
        notes.append('No collector attempts are available inside this window. Missing attempt evidence does not establish another family was active, or prove sleep, shutdown or a provider failure.')
    families, expected_by, tables = {}, {}, {}
    for kind in KINDS:
        members = [a for a in accounts if a['kind'] == kind]
        if not members:
            continue
        expected_by[kind] = expected_bars(kind, start, until) if kind in ('hourly_crypto', 'active') or covered else []
        fam_attempts = [a for a in attempts if a['family'] == COLLECTOR_FAMILY[kind]]
        tables[kind] = session_table(kind, expected_by[kind], start, until)
        cov = family_coverage(kind, members, data, expected_by[kind], silent, fam_attempts, start, until, tables[kind])
        # Accounts that recorded fewer expected bars than the family as a whole.
        recorded_labels = {label for label, _ in expected_by[kind]}
        per_account = []
        for acct in members:
            mine = {o['asof'] for o in data[acct['id']]['observations'] if o['observed_at'] and start <= o['observed_at'] <= until}
            got = len(mine & recorded_labels)
            per_account.append({'account': acct['id'], 'recorded_expected': got, 'of': len(recorded_labels),
                                'coverage_pct': round(100 * got / len(recorded_labels), 1) if recorded_labels else None,
                                'unreadable': data[acct['id']]['error'],
                                'held_reason': holds['holds'].get(acct['pair']) if kind.startswith('hourly') else None})
        # An account that recorded fewer bars than its family held back on its own (for example a data hold).
        cov['accounts_below_family_coverage'] = [p for p in per_account if p['coverage_pct'] is not None
                                                 and cov['coverage_pct'] is not None and p['coverage_pct'] < cov['coverage_pct']]
        cov['accounts_with_no_recorded_bar'] = [p['account'] for p in per_account if not p['recorded_expected']]
        cov['economics'] = {a['id']: economics(data[a['id']], start, until) for a in members}
        families[kind] = cov
    comparisons = []
    for pair in sorted({(a['kind'], a['pair']) for a in accounts}):
        kind, name = pair
        members = [a for a in accounts if a['kind'] == kind and a['pair'] == name]
        refs = [a for a in members if a['role'] == 'reference']
        for strategy in (a for a in members if a['role'] == 'strategy'):
            for ref in refs:
                comparisons.append(compare(kind, strategy, ref, data, expected_by.get(kind, []), start, until, tables.get(kind)))
    excluded = [{'kind': 'before_window', 'until': iso(start),
                 'detail': 'Everything recorded or completed before the window start: earlier manual and pre-deployment runs.',
                 'observations_excluded': {k: v['observed_before_window_excluded'] for k, v in families.items()}}]
    excluded += [{'kind': 'collector_silent', 'from': iso(p['from']), 'to': iso(p['to']), 'minutes': p['minutes'], 'detail': p['cause']}
                 for p in silent]
    for kind, fam in families.items():
        excluded += [{'kind': 'gapped_bars', 'family': kind, 'from_bar': g['from_bar'], 'to_bar': g['to_bar'],
                      'missing_bars': g['missing_bars'], 'detail': g['primary']} for g in fam['gaps']]
    process = collector['process']
    beat = utc(process.get('heartbeat_at'))
    return {
        'generated_at': iso(now),
        'window': {'since': iso(start), 'until': iso(until), 'calendar_covered': covered,
                   'hours': round((until - start).total_seconds() / 3600, 2)},
        'process': {'current': {**{k: process.get(k) for k in ('pid', 'started_at', 'heartbeat_at', 'status', 'stopped_at')},
                                # Measured against the window end, so a dated copy of the databases is not mistaken for a live one.
                                'heartbeat_minutes_before_window_end': round((until - beat).total_seconds() / 60, 1) if beat else None},
                    'recorded_interrupted_checks': [{'family': a['family'], 'check_started': iso(a['started']),
                                                     'recovered_at': iso(a['finished'])}
                                                    for a in in_window if a['outcome'] == 'interrupted'],
                    'logs': process_evidence(runtime), 'silent_periods': [
                        {**p, 'from': iso(p['from']), 'to': iso(p['to'])} for p in silent]},
        'checks': attempt_summary(attempts, start, until),
        'held_accounts': {'hourly_quality': holds,
                          'accounts_with_no_recorded_bar': {k: v['accounts_with_no_recorded_bar'] for k, v in families.items()},
                          'held_checks_by_family': {k: v['outcomes'].get('held', 0) for k, v in attempt_summary(attempts, start, until).items()}},
        'families': families, 'excluded_periods': excluded, 'comparisons': comparisons,
        'summary': {'comparisons': len(comparisons),
                    'insufficient_sample': sum(1 for c in comparisons if c['verdict'] == 'insufficient_sample'),
                    'thresholds_met': sum(1 for c in comparisons if c['verdict'] != 'insufficient_sample')},
        'notes': notes}


# --------------------------------------------------------------------- text

def short_time(stamp):
    return stamp[:16].replace('T', ' ') + 'Z' if stamp else 'none'


def render(report, gap_limit=8):
    w = report['window']
    lines = [f"Coverage report generated {short_time(report['generated_at'])}",
             f"Window {short_time(w['since'])} to {short_time(w['until'])} ({w['hours']} h). Read-only; nothing was run or changed."]
    p = report['process']
    cur = p['current']
    lines += ['', 'PROCESS AND INTERRUPTIONS',
              f"  collector pid {cur['pid']} status {cur['status']} started {short_time(cur['started_at'])}, last heartbeat {short_time(cur['heartbeat_at'])} "
              f"({cur['heartbeat_minutes_before_window_end']} min before the window end; a recorded heartbeat is not proof the process is running now)",
              f"  service logs record {p['logs']['logged_process_starts']} process start(s), pids {p['logs']['pids']} (undated); "
              f"{p['logs']['relaunch_refusals']['count']} scheduled relaunches found the server already running"]
    for i in p['recorded_interrupted_checks']:
        lines.append(f"  interrupted check: {i['family']} started {short_time(i['check_started'])}; next process recovered it {short_time(i['recovered_at'])}")
    for s in p['silent_periods']:
        lines.append(f"  SILENT {short_time(s['from'])} -> {short_time(s['to'])} ({s['minutes']} min): {s['cause']}"
                     + (f"; first checks on resume {s['outcomes_on_resume']}" if s.get('outcomes_on_resume') else ''))
    if not p['silent_periods']:
        lines.append('  no silent periods found')
    lines += ['', 'CHECKS AND ERROR REASONS (collector attempt log)']
    for family, c in report['checks'].items():
        lines.append(f"  {family:11} {c['checks']:4} checks {c['outcomes']}")
        for r in c['reasons']:
            lines.append(f"      x{r['count']:<3} {r['message']}")
    lines += ['', 'COVERAGE (expected bars from the NYSE calendar / 24-7 clock; pending bars are not counted as missed)']
    for kind, f in report['families'].items():
        pct = 'n/a' if f['coverage_pct'] is None else f"{f['coverage_pct']}%"
        lines.append(f"  {f['label']:22} {f['recorded_bars']:5}/{f['expected_bars']:<5} bars recorded = {pct:6} "
                     f"{f['complete_sessions']} complete / {f['partial_sessions']} partial sessions ({f['full_sessions']} full), "
                     f"{len(f['gaps'])} gap(s), {f['accounts']} accounts")
        lat = f['observation_latency_minutes']
        if lat:
            lines.append(f"      latency after ready: median {lat['median']} min, max {lat['max']} min, {lat['late_count']} later than {lat['late_threshold']} min")
        for s in f['sessions']:
            if not s['full']:
                note = '' if s['complete'] else f" {s['status']}: coverage so far, the session has {s['full_session_bars']} bars"
                lines.append(f"      session {s['session']}: {s['recorded']}/{s['expected']} ({s['coverage_pct']}%){note}")
        for g in f['gaps'][:gap_limit]:
            lines.append(f"      GAP {g['missing_bars']} bar(s) {short_time(g['from_bar'] if len(g['from_bar']) > 10 else g['from_bar'] + 'T00:00')}"
                         f" .. {short_time(g['to_bar'] if len(g['to_bar']) > 10 else g['to_bar'] + 'T00:00')}: {', '.join(g['causes'])}"
                         + (f" {g['failed_checks']}" if g['failed_checks'] else '')
                         + (f" e.g. {g['top_messages'][0]['message']}" if g['top_messages'] else ''))
        if len(f['gaps']) > gap_limit:
            lines.append(f"      ... {len(f['gaps']) - gap_limit} more gaps (see --json)")
        if f['accounts_with_no_recorded_bar']:
            lines.append(f"      accounts with no recorded bar in the window: {', '.join(f['accounts_with_no_recorded_bar'])}")
    hold = report['held_accounts']['hourly_quality']
    lines += ['', 'HELD ACCOUNTS',
              f"  hourly lab holds as of {short_time(hold['observed_at'])}: "
              + (', '.join(f'{k} ({v})' for k, v in hold['holds'].items()) or 'none')]
    for family, n in report['held_accounts']['held_checks_by_family'].items():
        if n:
            lines.append(f"  {family}: {n} check(s) rejected the data (every account of that family is held together)")
    lines += ['', 'WHOLE-WINDOW COSTS AND EXPOSURE (paper fees; no broker; every bar recorded in the window, NOT limited to the matched '
              'intervals the returns below use)']
    for kind, f in report['families'].items():
        econ = f['economics']
        fills = sum(e['fills'] for e in econ.values())
        fees = sum(e['fees_paid'] for e in econ.values())
        exposures = [e['mean_exposure_pct'] for e in econ.values() if e['mean_exposure_pct'] is not None]
        lines.append(f"  {f['label']:22} {fills:4} fills, ${fees:,.2f} fees; mean exposure by account "
                     f"{min(exposures):.0f}%..{max(exposures):.0f}%" if exposures else f"  {f['label']:22} no observations")
    lines += ['', 'MATCHED STRATEGY / REFERENCE PERIODS (returns use matched bars only, gaps excluded; descriptive, unannualised; '
              'the session gate counts complete sessions fully matched by both accounts; fill counts in the sample gate are whole-window)']
    s = report['summary']
    lines.append(f"  {s['comparisons']} comparisons: {s['insufficient_sample']} insufficient sample, "
                 f"{s['thresholds_met']} meet the sample thresholds (still not evidence of an edge)")
    shown = Counter()
    for c in report['comparisons']:
        shown[c['kind']] += 1
        if shown[c['kind']] > 6:
            continue
        diff = '' if c['descriptive_difference_pp'] is None else f" [descriptive {c['descriptive_difference_pp']:+.2f} pp]"
        lines.append(f"  {c['strategy']:30} vs {c['reference']:22} {c['matched_bars']:4} matched bars, {c['segments']} run(s), "
                     f"{c['verdict'].upper()}: {'; '.join(c['insufficient_because']) or 'thresholds met'}; "
                     f"complete matched sessions {c['complete_matched_sessions']} (observed {c['observed_sessions']}){diff}")
    for kind, n in shown.items():
        if n > 6:
            lines.append(f"  ... {n - 6} more {kind} comparisons (see --json)")
    lines += ['', 'EXCLUDED OR GAPPED PERIODS: ' + str(len(report['excluded_periods'])) + ' entries (details above and in --json)', '']
    lines += ['NOTES'] + [f'  - {n}' for n in report['notes']]
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--runtime', type=Path, default=ROOT / 'runtime')
    parser.add_argument('--since', type=aware, help='Window start (default: first collector attempt)')
    parser.add_argument('--until', type=aware, help='Window end (default: now); pin it to reproduce a report')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    if not args.runtime.is_dir():
        raise SystemExit(f'Runtime folder not found: {args.runtime}')
    report = build(args.runtime, args.since, args.until)
    print(json.dumps(report, indent=2, default=str) if args.json else render(report))


if __name__ == '__main__':
    main()
