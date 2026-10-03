"""Read-only collection report straight from the runtime databases.

Counts forward observations each family actually recorded, independent of the
collector's own bookkeeping, and shows the collector heartbeat and check log.
Opens every SQLite file read-only; makes no network request and writes nothing.

    python -B scripts/collection_report.py
    python -B scripts/collection_report.py --since 2026-09-28T13:30:00+00:00
    python -B scripts/collection_report.py --json
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

ROOT = Path(__file__).resolve().parent.parent
FAMILIES = {
    'core': ['paper.sqlite3'],
    'core benchmarks': ['hourly-v1/core-benchmark-*.sqlite3'],
    'hourly': ['hourly-v1/*__*.sqlite3'],
    'active': ['active-15m-v1/active.sqlite3', 'active-15m-v1/reference.sqlite3'],
    'stocks_5m': ['stock-experiments-v1/stock-lab-v1-*_5m.sqlite'],
    'stocks_15m': ['stock-experiments-v1/stock-lab-v1-*_15m.sqlite'],
}


def read_only(path):
    con = sqlite3.connect(f'file:{path.as_posix()}?mode=ro', uri=True, timeout=15)
    con.row_factory = sqlite3.Row
    return con


HEARTBEAT_STALE_SECONDS = 90   # collector.Collector.status: 3 ticks of 15 s, at least 90 s


def aware(stamp):
    """Parse an ISO time as UTC. Stored times are UTC strings, so any other offset
    must be converted before it is compared with them as text."""
    value = datetime.fromisoformat(stamp)
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def family_report(runtime, patterns, since):
    books = sorted({p for pattern in patterns for p in runtime.glob(pattern)})
    total = new = unreadable = 0
    latest_bar = latest_seen = None
    for path in books:
        try:
            con = read_only(path)
        except sqlite3.DatabaseError:
            unreadable += 1
            continue
        try:
            rows = con.execute('SELECT asof, observed_at FROM observations').fetchall()
        except sqlite3.DatabaseError:
            unreadable += 1
            continue
        finally:
            con.close()
        total += len(rows)
        for row in rows:
            seen = aware(row['observed_at']) if row['observed_at'] else None
            if since and seen and seen > since:
                new += 1
            if latest_bar is None or row['asof'] > latest_bar:
                latest_bar = row['asof']
            if seen and (latest_seen is None or seen > latest_seen):
                latest_seen = seen
    return {'accounts': len(books), 'unreadable_accounts': unreadable, 'observations': total,
            'observed_after_since': new if since else None,
            'latest_bar': latest_bar, 'latest_observed_at': latest_seen.isoformat() if latest_seen else None}


def collector_report(runtime, since):
    path = runtime / 'collector.sqlite3'
    if not path.exists():
        return {'installed': False}
    try:
        return _collector_report(path, since)
    except sqlite3.DatabaseError as exc:
        # An empty or half-created file must not take the whole report down.
        return {'installed': False, 'unreadable': str(exc)}


def _collector_report(path, since):
    con = read_only(path)
    try:
        row = con.execute('SELECT * FROM collector WHERE id=1').fetchone()
        families = [dict(r) for r in con.execute(
            'SELECT name,last_attempt_at,last_source,last_outcome,checks,new_observations,consecutive_failures,'
            'last_new_observation_at,latest_bar,running_since,hung_since,blocked_reason FROM families ORDER BY name')]
        query = 'SELECT family,source,outcome,count(*) n,sum(new_observations) new FROM attempts'
        args = ()
        if since:
            query += ' WHERE started_at > ?'
            args = (since.isoformat(),)
        attempts = [dict(r) for r in con.execute(query + ' GROUP BY family,source,outcome ORDER BY family', args)]
    finally:
        con.close()
    heartbeat = dict(row) if row else {}
    if heartbeat.get('heartbeat_at'):
        heartbeat['heartbeat_age_seconds'] = round(
            (datetime.now(timezone.utc) - aware(heartbeat['heartbeat_at'])).total_seconds())
        # A process that died without a clean stop still says 'running'; only an old heartbeat reveals it.
        heartbeat['heartbeat_stale'] = (heartbeat.get('status') == 'running'
                                        and heartbeat['heartbeat_age_seconds'] > HEARTBEAT_STALE_SECONDS)
    return {'installed': True, 'process': heartbeat, 'families': families, 'attempts': attempts}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--runtime', type=Path, default=ROOT / 'runtime')
    parser.add_argument('--since', type=aware, help='Count observations recorded after this ISO time')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    report = {'generated_at': datetime.now(timezone.utc).isoformat(),
              'since': args.since.isoformat() if args.since else None,
              'families': {name: family_report(args.runtime, patterns, args.since)
                           for name, patterns in FAMILIES.items()},
              'collector': collector_report(args.runtime, args.since)}
    if args.json:
        print(json.dumps(report, indent=2))
        return
    print(f"Collection report {report['generated_at']}" + (f" (new since {report['since']})" if args.since else ''))
    for name, f in report['families'].items():
        extra = f", {f['observed_after_since']} after --since" if args.since else ''
        bad = f", {f['unreadable_accounts']} UNREADABLE" if f['unreadable_accounts'] else ''
        print(f"  {name:16} {f['observations']:6} observations in {f['accounts']:3} accounts{bad}{extra}; "
              f"latest bar {f['latest_bar']}; latest observed {f['latest_observed_at']}")
    c = report['collector']
    if not c['installed']:
        print('  collector: ' + (f"collector.sqlite3 unreadable ({c['unreadable']})" if c.get('unreadable') else
                                 'no collector.sqlite3 yet (collector never ran with this runtime)'))
        return
    p = c['process']
    stale = ' STALE: status says running but the heartbeat stopped' if p.get('heartbeat_stale') else ''
    print(f"  collector pid {p.get('pid')} status {p.get('status')} started {p.get('started_at')} "
          f"heartbeat {p.get('heartbeat_at')} ({p.get('heartbeat_age_seconds')} s ago){stale} note {p.get('note')}")
    for f in c['families']:
        print(f"    {f['name']:11} last {f['last_outcome']} via {f['last_source']} at {f['last_attempt_at']}; "
              f"{f['checks']} checks, {f['new_observations']} new, failures {f['consecutive_failures']}; "
              f"latest bar {f['latest_bar']}" + (f"; RUNNING since {f['running_since']}" if f['running_since'] else '')
              + (f"; HUNG since {f['hung_since']}" if f['hung_since'] else '')
              + (f"; waiting: {f['blocked_reason']}" if f['blocked_reason'] else ''))
    for a in c['attempts']:
        print(f"    attempts {a['family']:11} {a['source']:9} {a['outcome']:12} x{a['n']} (new {a['new']})")


if __name__ == '__main__':
    main()
