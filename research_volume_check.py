"""Check how the daily feed treats volume around a known share split.

Role 1 compares a session's volume with its own trailing median, and role 4
multiplies adjusted close by reported volume. Both readings are wrong across a
split if price and volume are adjusted differently, so this measures it instead
of assuming. Read-only public GETs; prints ratios, never advice.
"""
import os

for _name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY',
              'http_proxy', 'https_proxy', 'all_proxy'):
    os.environ.pop(_name, None)

import argparse
import pandas as pd
import yfinance as yf


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ticker', default='NVDA')
    parser.add_argument('--split', default='2024-06-10', help='known split date')
    parser.add_argument('--period', default='5y')
    args = parser.parse_args()

    frames = {}
    for adjust in (True, False):
        raw = yf.download(args.ticker, period=args.period, interval='1d',
                          auto_adjust=adjust, progress=False, timeout=30)
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)
        raw.index = pd.to_datetime(raw.index).tz_localize(None).normalize()
        frames[adjust] = raw

    split = pd.Timestamp(args.split)
    print(f'{args.ticker}, {args.period}, split reference {split.date()}')
    for adjust, frame in frames.items():
        window = frame.loc[split - pd.Timedelta(days=12):split + pd.Timedelta(days=12)]
        if window.empty:
            print(f'  auto_adjust={adjust}: no rows around the split date')
            continue
        before = window.loc[:split - pd.Timedelta(days=1)]
        after = window.loc[split:]
        if before.empty or after.empty:
            print(f'  auto_adjust={adjust}: not enough rows on both sides')
            continue
        print(f'  auto_adjust={adjust}: close {before["Close"].median():,.2f} -> '
              f'{after["Close"].median():,.2f} '
              f'(x{after["Close"].median() / before["Close"].median():.2f}), '
              f'volume {before["Volume"].median():,.0f} -> {after["Volume"].median():,.0f} '
              f'(x{after["Volume"].median() / before["Volume"].median():.2f})')
    common = frames[True].index.intersection(frames[False].index)
    same = (frames[True].loc[common, 'Volume'] == frames[False].loc[common, 'Volume'])
    print(f'  volume identical with and without auto_adjust on '
          f'{same.mean() * 100:.1f}% of {len(common)} shared sessions')
    dollars = frames[True].loc[common, 'Close'] * frames[True].loc[common, 'Volume']
    raw_dollars = frames[False].loc[common, 'Close'] * frames[False].loc[common, 'Volume']
    ratio = (dollars / raw_dollars).dropna()
    print(f'  adjusted dollar volume / unadjusted, median {ratio.median():.3f}, '
          f'min {ratio.min():.3f}, max {ratio.max():.3f}')


if __name__ == '__main__':
    main()
