"""Collect one real observation from the research desk and save it as evidence.

Read-only. It fetches public data with GET requests, runs one advisory cycle into
an ISOLATED evidence directory, and writes what it saw to disk. It does not open
the core paper book, the hourly lab or the desk directory the application uses,
and it places no order.

    python research_evidence.py                 # one cycle into runtime/research-evidence
    python research_evidence.py --no-books      # skip public order books
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path

# Proxy variables inherited from this shell break direct public GETs here. They
# are removed inside this process only, and their values are never printed.
for _name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY',
              'http_proxy', 'https_proxy', 'all_proxy'):
    os.environ.pop(_name, None)

ROOT = Path(__file__).resolve().parent


def cached_core_snapshot(out, core_from):
    """Replay a RECORDED core decision against the panel already on disk.

    This does not recompute the strategy: it reuses the session, observation time
    and target weights a previous live run recorded, and prices them from the
    cached research panel. The point is to exercise the desk end to end on real
    recorded prices with no request of any kind. Because there is only one feed in
    this mode, the independent price cross-check is trivially zero and proves
    nothing, which the saved report states.

    The regime instrument SPY is not part of the research universe, so the frozen
    `signal_snapshot` cannot be re-run from this panel at all; that is why the
    recorded decision is replayed rather than recalculated.
    """
    import pandas as pd
    path = out / 'desk' / 'intake' / 'daily_Close.csv'
    if not path.exists():
        raise SystemExit(f'No cached panel at {path}; run once without --offline first.')
    if not core_from or not Path(core_from).exists():
        raise SystemExit('--offline needs --core-from pointing at a saved evidence.json')
    saved = json.loads(Path(core_from).read_text(encoding='utf-8'))
    recorded = saved['core_snapshot']
    close = pd.read_csv(path, index_col=0, parse_dates=True).astype(float)
    close.index = pd.to_datetime(close.index).tz_localize(None).normalize()
    row = close.loc[pd.Timestamp(recorded['asof'])]
    return {'asof': recorded['asof'], 'fetched_at': recorded['fetched_at'],
            'bar_end': recorded['bar_end'], 'risk_on': recorded['risk_on'],
            'strategy': recorded['strategy'] + ' (recorded decision replayed offline)',
            'target_weights': saved['core_target'],
            'prices': {symbol: float(value) for symbol, value in row.items()
                       if pd.notna(value)},
            'eligible': recorded.get('eligible'), 'excluded': []}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=ROOT / 'runtime' / 'research-evidence')
    parser.add_argument('--no-books', action='store_true')
    parser.add_argument('--offline', action='store_true',
                        help='Make no request at all: replay a recorded core decision against the '
                             'already cached research panel and pin the cache so it is always '
                             'reused. Real recorded prices, zero network.')
    parser.add_argument('--core-from', type=Path,
                        help='Saved evidence.json holding the recorded core decision to replay')
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    from trading_engine import signal_snapshot, DataUnavailable
    from research_desk import ResearchDesk

    now = datetime.now(timezone.utc)
    report = {'collected_at': now.isoformat(), 'out': str(args.out), 'offline': args.offline}
    try:
        snapshot = (cached_core_snapshot(args.out, args.core_from) if args.offline
                    else signal_snapshot())
    except DataUnavailable as exc:
        report['core_snapshot'] = f'unavailable: {exc}'
        (args.out / 'evidence.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(f'Core snapshot unavailable: {exc}')
        return
    report['core_snapshot'] = {k: snapshot[k] for k in
                               ('asof', 'fetched_at', 'bar_end', 'risk_on', 'strategy',
                                'eligible', 'cash_pct') if k in snapshot}
    report['core_target'] = snapshot['target_weights']
    print(f"Core session {snapshot['asof']}, risk_on={snapshot['risk_on']}, "
          f"{len(snapshot['target_weights'])} target name(s)")

    if args.offline:
        from research_intake import DailyIntake
        # Pin the cache so the recorded snapshot is always reused: this mode is
        # guaranteed to make no request of any kind.
        desk = ResearchDesk(args.out / 'desk', enable_books=False,
                            intake=DailyIntake(args.out / 'desk' / 'intake', ttl_hours=10 ** 6),
                            crypto_products=())
    else:
        desk = ResearchDesk(args.out / 'desk', enable_books=not args.no_books)
    # No injected clock: the desk stamps its own reading, which is genuinely after
    # the core's fetch. That ordering is the point of the shadow timing fix.
    outcome = desk.observe(snapshot)
    print(f'Desk outcome: {outcome}')

    status = desk.status()
    report['outcome'] = outcome
    report['counts'] = status['counts']
    report['cycle'] = status['cycle']
    report['digest'] = status['digest']
    report['holds'] = status['holds']
    report['advisories'] = status['advisories']
    report['shadow'] = status['shadow']
    # The shadow books must time their decisions from the research reading, not
    # from the core's earlier fetch. Record both so the distinction is visible.
    report['decision_timing'] = {
        'core_fetched_at': snapshot['fetched_at'],
        'research_observed_at': (status['digest'] or {}).get('observed_at'),
        'shadow_signal_observed_at': {
            name: (book.status()['pending'] or {}).get('observed_at')
            for name, book in desk.shadow.items()},
        'conservative_bar_end': snapshot['bar_end'],
        'note': ('The virtual books time their signal from the research reading, because that is '
                 'when an advisory existed. The core fetch time is provenance only.'),
    }
    report['requests'] = {
        'daily_ohlcv': getattr(desk.intake, 'requests', None),
        'order_books': getattr(desk.books, 'requests', None),
        'note': ('One batched daily request for the whole universe plus at most four public '
                 'order-book GETs. A second cycle inside the cache window adds none.'),
    }
    # Prove the cache: a second observation of the same session must add no
    # request and no row.
    before = dict(status['counts'])
    repeat = desk.observe(snapshot)
    report['repeat_cycle'] = {'message': repeat, 'counts_before': before,
                              'counts_after': desk.status()['counts'],
                              'daily_requests_after': getattr(desk.intake, 'requests', None),
                              'book_requests_after': getattr(desk.books, 'requests', None)}
    (args.out / 'evidence.json').write_text(
        json.dumps(report, indent=2, default=str), encoding='utf-8')

    digest = status['digest'] or {}
    for line in digest.get('plain_language', []):
        print(' -', line)
    for role, values in (digest.get('coverage') or {}).items():
        print(f"   {values['title']}: examined {values['examined']}, reported "
              f"{values['reported']}, silent {values['abstained']}")
    print(f"Repeat cycle: {repeat}")
    print(f"Wrote {args.out / 'evidence.json'}")


if __name__ == '__main__':
    main()
