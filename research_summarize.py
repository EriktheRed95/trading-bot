"""Print a plain table of what the helpers recorded for the strategy's own names.

Offline: reads a saved evidence file only. Nothing is fetched or recomputed.

    python research_summarize.py runtime/research-evidence/evidence.json
"""
import argparse
import json
from pathlib import Path

import research_roles as roles


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path', nargs='?', type=Path,
                        default=Path('runtime') / 'research-evidence' / 'evidence.json')
    args = parser.parse_args()
    data = json.loads(args.path.read_text(encoding='utf-8'))
    target = sorted(data.get('core_target') or {})
    rows = {}
    for row in data.get('advisories', []):
        rows.setdefault(row['symbol'], {})[row['role']] = row

    print(f"Session {data['core_snapshot']['asof']}, read at {data['cycle']['observed_at']}")
    print(f"Strategy target: {len(target)} name(s). Readings below are advisory only.\n")
    header = (f"{'name':<7}{'vol x median':>13}{'range pos':>11}{'vs 200d':>9}"
              f"{'$vol/day':>11}{'integrity':>11}{'price check':>13}")
    print(header)
    print('-' * len(header))
    for symbol in target:
        found = rows.get(symbol, {})
        activity = found.get(roles.ROLE_ACTIVITY, {}).get('evidence', {})
        entry = found.get(roles.ROLE_ENTRY, {}).get('evidence', {})
        filt = found.get(roles.ROLE_FILTER, {})
        liquid = found.get(roles.ROLE_LIQUIDITY, {}).get('evidence', {})
        ratio = activity.get('ratio_to_median')
        position = entry.get('range_position_pct')
        trend = entry.get('pct_above_trailing_average')
        dollars = liquid.get('median_dollar_volume')
        difference = (filt.get('evidence') or {}).get('price_difference_pct')
        print(f"{symbol:<7}"
              f"{(f'{ratio:.2f}x' if ratio is not None else '—'):>13}"
              f"{(f'{position:.0f}%' if position is not None else '—'):>11}"
              f"{(f'{trend:+.1f}%' if trend is not None else '—'):>9}"
              f"{(f'${dollars/1e6:.0f}M' if dollars else '—'):>11}"
              f"{(filt.get('verdict') or '—'):>11}"
              f"{(f'{difference:+.4f}%' if difference is not None else '—'):>13}")

    books = [row for row in data.get('advisories', []) if '-USD' in row['symbol']]
    if books:
        print('\nVisible order-book depth, from public exchange snapshots '
              '(resting orders, cancellable):')
        print(f"{'product':<10}{'spread':>9}{'bids 25bp':>13}{'asks 25bp':>13}"
              f"{'bids 100bp':>13}{'asks 100bp':>13}")
        for row in books:
            evidence = row['evidence']
            if row['abstained']:
                print(f"{row['symbol']:<10}  unavailable: {row['abstention_reason']}")
                continue
            bands = evidence['bands_bps']
            print(f"{row['symbol']:<10}{evidence['spread_bps']:>8.2f}bp"
                  f"{bands['25']['bid_usd']/1e6:>12.2f}M{bands['25']['ask_usd']/1e6:>12.2f}M"
                  f"{bands['100']['bid_usd']/1e6:>12.2f}M{bands['100']['ask_usd']/1e6:>12.2f}M")
    print(f"\nRequests this cycle: {data['requests']['daily_ohlcv']} batched daily download(s), "
          f"{data['requests']['order_books']} order book(s).")
    print(f"Repeat cycle: {data['repeat_cycle']['message']}")
    print(f"Requests after the repeat: {data['repeat_cycle']['daily_requests_after']} daily, "
          f"{data['repeat_cycle']['book_requests_after']} books.")


if __name__ == '__main__':
    main()
