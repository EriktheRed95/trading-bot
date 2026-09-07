"""Overnight vs intraday returns: an honest test of a viral claim.

THE CLAIM (a video, citing a real paper): buy at the close, sell at the next
open, and you capture nearly all of the equity risk premium. The clip shows
Micron at +138,330% overnight vs -99.92% intraday.

THE PAPER IS REAL. The video's arithmetic is not.
  Lou, Polk & Skouras, "A tug of war: Overnight versus intraday expected
  returns", Journal of Financial Economics 134(1), 2019, pp. 192-213.
The documented effect is a genuine, published anomaly. What the video adds is
(a) one cherry-picked survivor and (b) ZERO transaction costs on a strategy
that round-trips about 252 times a year. This script removes both.

WHAT THIS SCRIPT TESTS
  1. Decompose every day into its two legs, exactly and without overlap:
        overnight[t] = Open[t]  / Close[t-1] - 1     (held ~17.5 hours)
        intraday[t]  = Close[t] / Open[t]    - 1     (held ~6.5 hours)
     The two compound to the full day return, which is asserted, not assumed.
  2. Charge the SAME cost model the rest of this repo uses
     (strategy_config.SLIPPAGE_BPS + COMMISSION_BPS), on every side.
     Buy-and-hold pays it twice in a lifetime. Overnight pays it 504 times a
     year. That asymmetry is the entire experiment.
  3. Run a universe, not one name, under this repo's survivorship discipline
     (--universe megacap, or --universe pit for point-in-time S&P membership).
  4. Sweep the cost assumption to find the BREAKEVEN cost per side: the number
     that decides whether a retail account can actually harvest this.

WHAT IT DOES NOT DO
  No entries, exits or sizing are recommended here, and nothing in this file
  trades. It answers one question: does the overnight effect survive costs?
"""
import argparse
import json
import warnings
from datetime import date

import numpy as np
import pandas as pd
import yfinance as yf

from metrics import compute_metrics
from strategy_c import BROAD_UNIVERSE, MARKET
from strategy_config import COMMISSION_BPS, SLIPPAGE_BPS

warnings.filterwarnings('ignore')

COST_PER_SIDE = (SLIPPAGE_BPS + COMMISSION_BPS) / 10_000.0   # 6 bp, repo-wide
SIDES_PER_DAY = 2          # every daily round trip is an entry plus an exit
OPEN_PENALTY = 1.0         # multiplier on open-fill cost; see --open-penalty


def load_ohlc(tickers, period="max"):
    """Adjusted daily Open and Close, one column per ticker.

    auto_adjust scales Open and Close by the same daily factor, so the intraday
    leg is unchanged by adjustment and the overnight leg correctly carries the
    dividend drop. Using raw prices would hand the overnight leg a fake loss on
    every ex-dividend date.
    """
    tickers = sorted(set(tickers))
    data = yf.download(tickers, period=period, interval="1d",
                       auto_adjust=True, progress=False, threads=True)
    if isinstance(data.columns, pd.MultiIndex):
        op, cl = data['Open'], data['Close']
    else:
        op, cl = data[['Open']].copy(), data[['Close']].copy()
        op.columns = cl.columns = tickers
    op.index = pd.to_datetime(op.index).tz_localize(None)
    cl.index = pd.to_datetime(cl.index).tz_localize(None)
    return op.sort_index(), cl.sort_index()


def decompose(op, cl, verify=True):
    """Split each day into (overnight, intraday) simple returns.

    Returns two DataFrames aligned to `cl`. The first row of each is NaN
    because an overnight leg needs a prior close.
    """
    prev_close = cl.shift(1)
    overnight = op / prev_close - 1.0
    intraday = cl / op - 1.0

    if verify:
        # The two legs must compound to the full close-to-close day. If this
        # fails, the price data is inconsistent and every number below is junk.
        full = cl / prev_close - 1.0
        recombined = (1 + overnight) * (1 + intraday) - 1.0
        err = (recombined - full).abs().to_numpy()
        worst = float(np.nanmax(err)) if err.size and not np.all(np.isnan(err)) else 0.0
        if worst > 1e-9:
            raise AssertionError(
                f"overnight/intraday legs do not recombine to the full day "
                f"(max error {worst:.2e}). Price data is inconsistent.")
    return overnight, intraday


def _equity(net_returns):
    """Compound a return series into an equity curve starting at 1.0."""
    r = pd.Series(net_returns).astype(float).fillna(0.0)
    return (1.0 + r).cumprod()


def leg_equity(leg_returns, cost_per_side=COST_PER_SIDE, open_penalty=OPEN_PENALTY):
    """Equity curve for trading ONE leg every day, net of round-trip costs.

    An overnight trade buys at a close and sells at an open; an intraday trade
    buys at an open and sells at a close. Either way one of the two fills lands
    on the open auction, which is the widest-spread moment of the day, so
    open_penalty scales that side's cost.
    """
    per_round_trip = cost_per_side * (1.0 + open_penalty)
    return _equity(leg_returns - per_round_trip)


def hold_equity(cl_col, cost_per_side=COST_PER_SIDE):
    """Buy-and-hold benchmark: costs charged twice, ever, not twice a day."""
    r = cl_col.pct_change()
    eq = _equity(r)
    return eq * (1.0 - cost_per_side) ** 2


def summarize(equity, label):
    m = compute_metrics(equity.dropna())
    return dict(label=label, **m)


def print_table(rows, title):
    print(f"\n{title}")
    print(f"{'':<34} {'total':>14} {'CAGR':>7} {'Sharpe':>7} {'maxDD':>7}")
    print("-" * 74)
    for r in rows:
        tot = r['total_return']
        tot_s = f"{tot:>13,.0f}%" if abs(tot) >= 10_000 else f"{tot:>13,.1f}%"
        print(f"{r['label']:<34} {tot_s} {r['cagr']:>6.1f}% "
              f"{r['sharpe']:>7.2f} {r['max_drawdown']:>6.0f}%")


def single_name(ticker, cost_per_side=COST_PER_SIDE, open_penalty=OPEN_PENALTY):
    """Reproduce the video's single-name claim, gross and then net."""
    op, cl = load_ohlc([ticker])
    on, intr = decompose(op, cl)
    on, intr, c = on[ticker].dropna(), intr[ticker].dropna(), cl[ticker].dropna()

    rows = [
        summarize(_equity(on), "Overnight only  (ZERO costs)"),
        summarize(_equity(intr), "Intraday only   (ZERO costs)"),
        summarize(leg_equity(on, cost_per_side, open_penalty),
                  f"Overnight only  (net, {cost_per_side*1e4:.0f}bp/side)"),
        summarize(leg_equity(intr, cost_per_side, open_penalty),
                  f"Intraday only   (net, {cost_per_side*1e4:.0f}bp/side)"),
        summarize(hold_equity(c, cost_per_side), "Buy and hold    (net)"),
    ]
    span = f"{c.index[0].date()} -> {c.index[-1].date()}  ({len(c):,} sessions)"
    print_table(rows, f"{ticker} single name   {span}")
    print("\nThe first two lines are the video's framing. The next two are the "
          "same data\nwith this repo's own cost model applied. One name is not "
          "evidence either way;\nsee the universe run below for that.")
    return rows


def universe_run(tickers, cost_per_side=COST_PER_SIDE, open_penalty=OPEN_PENALTY,
                 holdable_fn=None, min_names=5, start=None):
    """Equal-weight the legs across a universe, rebalanced daily.

    holdable_fn(date) -> set of tickers allowed on that date. Supplying the
    point-in-time membership function from run_sp500_pit.py removes the
    selection hindsight that inflated run_sp500.py to a fake 40.6% CAGR.
    """
    op, cl = load_ohlc(list(tickers) + [MARKET])
    on, intr = decompose(op, cl)
    cols = [t for t in tickers if t in cl.columns and cl[t].notna().any()]
    on, intr = on[cols], intr[cols]

    mask = on.notna() & intr.notna()
    if holdable_fn is not None:
        rows = []
        for dt in on.index:
            members = holdable_fn(dt)
            rows.append([t in members for t in cols])
        allowed = pd.DataFrame(rows, index=on.index, columns=cols)
        # The overnight position is ENTERED at the close of the prior session, so
        # only membership known by then may gate it. Using same-day membership
        # would leak one day of index-change hindsight into the decision.
        allowed = allowed.shift(1).fillna(False).astype(bool)
        mask &= allowed

    n = mask.sum(axis=1)
    keep = n >= min_names
    if start is not None:
        keep &= (on.index >= pd.Timestamp(start))
    w = mask.where(mask).astype(float).div(n, axis=0)          # equal weight

    on_p = (on * w).sum(axis=1)[keep]
    intr_p = (intr * w).sum(axis=1)[keep]
    # The benchmark that actually matters. Holding the SAME names all day is what
    # the overnight trader gives up, and it is the standard this repo already
    # holds Strategy C to. SPY alone flatters any strategy run on megacaps.
    full = (cl[cols] / cl[cols].shift(1) - 1.0)
    full_p = (full * w).sum(axis=1)[keep]
    spy = cl[MARKET].reindex(on_p.index).dropna()

    rows = [
        summarize(leg_equity(on_p, cost_per_side, open_penalty),
                  f"Overnight only  (net, {cost_per_side*1e4:.0f}bp/side)"),
        summarize(leg_equity(intr_p, cost_per_side, open_penalty),
                  f"Intraday only   (net, {cost_per_side*1e4:.0f}bp/side)"),
        summarize(_equity(on_p), "Overnight only  (ZERO costs)"),
        summarize(_equity(intr_p), "Intraday only   (ZERO costs)"),
        summarize(_equity(full_p), "EW hold, SAME names (ZERO costs)"),
        summarize(hold_equity(spy, cost_per_side), "SPY buy and hold (net)"),
    ]
    span = (f"{on_p.index[0].date()} -> {on_p.index[-1].date()}  "
            f"({len(on_p):,} sessions, {len(cols)} names priced)")
    print_table(rows, f"Equal-weight universe   {span}")
    return rows, on_p, intr_p


def cost_sweep(on_p, intr_p, open_penalty=OPEN_PENALTY,
               bps=(0, 0.25, 0.5, 1, 1.5, 2, 2.5, 3, 5, 6, 10, 20)):
    """At what per-side cost does the overnight edge stop existing?

    This is the decision-relevant number. The published effect is real; the
    question a retail account actually faces is whether it clears the spread.
    """
    print(f"\nCost sensitivity   (open-fill penalty x{open_penalty:.1f}; "
          f"repo default is {COST_PER_SIDE*1e4:.0f}bp/side)")
    print(f"{'cost/side':>10} {'overnight CAGR':>16} {'intraday CAGR':>15} "
          f"{'overnight Sharpe':>18}")
    print("-" * 63)
    breakeven = None
    rows = []
    for b in bps:
        c = b / 10_000.0
        m_on = compute_metrics(leg_equity(on_p, c, open_penalty))
        m_in = compute_metrics(leg_equity(intr_p, c, open_penalty))
        flag = ''
        if breakeven is None and m_on['cagr'] <= 0:
            breakeven, flag = b, '   <- turns negative here'
        rows.append((b, m_on['cagr'], m_in['cagr'], m_on['sharpe']))
        print(f"{b:>7}bp {m_on['cagr']:>15.1f}% {m_in['cagr']:>14.1f}% "
              f"{m_on['sharpe']:>18.2f}{flag}")
    return breakeven, rows


def _write_json(path, payload):
    if not path:
        return
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(payload, fh, indent=2, default=float)
    print(f"\nWrote {path}")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--ticker', default='MU',
                   help="single name to reproduce the video's claim on (default MU)")
    p.add_argument('--universe', choices=['megacap', 'pit', 'none'], default='megacap',
                   help="megacap = this repo's 65-name pool; pit = point-in-time "
                        "S&P 500 membership (honest but slow)")
    p.add_argument('--cost-bps', type=float, default=SLIPPAGE_BPS + COMMISSION_BPS,
                   help='cost per side in basis points (default: the repo model)')
    p.add_argument('--open-penalty', type=float, default=OPEN_PENALTY,
                   help='multiplier on the open-auction fill cost (default 1.0, '
                        'i.e. the open is charged the same as the close)')
    p.add_argument('--start', metavar='YYYY-MM-DD',
                   help='restrict the universe run to this start date onward '
                        '(use it when membership data is only trustworthy from '
                        'a certain year)')
    p.add_argument('--skip-single', action='store_true')
    p.add_argument('--json', metavar='PATH',
                   help='also write the results to PATH as JSON, so a dashboard '
                        'can display computed numbers instead of typed-in ones')
    args = p.parse_args()

    cost = args.cost_bps / 10_000.0
    out = dict(generated=str(date.today()), cost_bps_per_side=args.cost_bps,
               open_penalty=args.open_penalty, universe=args.universe)

    if not args.skip_single:
        out['single_name'] = dict(ticker=args.ticker,
                                  rows=single_name(args.ticker, cost, args.open_penalty))

    if args.universe == 'none':
        _write_json(args.json, out)
        return

    if args.universe == 'pit':
        from run_sp500_pit import build_membership
        members_asof, _current, universe, _ch = build_membership()
        print(f"\nPoint-in-time universe: {len(universe)} ever-members. "
              f"Downloading (slow)...")
        rows, on_p, intr_p = universe_run(universe, cost, args.open_penalty,
                                          holdable_fn=members_asof,
                                          start=args.start)
    else:
        rows, on_p, intr_p = universe_run(BROAD_UNIVERSE, cost, args.open_penalty,
                                          start=args.start)

    breakeven, sweep = cost_sweep(on_p, intr_p, args.open_penalty)
    out['universe_rows'] = rows
    out['sweep'] = [dict(bps=b, overnight_cagr=oc, intraday_cagr=ic,
                         overnight_sharpe=sh) for b, oc, ic, sh in sweep]
    out['breakeven_bps'] = breakeven
    out['sessions'] = int(len(on_p))
    out['span'] = [str(on_p.index[0].date()), str(on_p.index[-1].date())]
    out['annual_cost_drag_pct'] = (cost * (1 + args.open_penalty)) * 252 * 100
    _write_json(args.json, out)

    print("\nRead this the way the rest of the repo reads results: the gross "
          "numbers are\nthe advertisement, the net numbers are the trade. A "
          "daily round trip pays the\nspread 504 times a year; buy-and-hold "
          "pays it twice.")
    if breakeven is not None:
        print(f"The overnight leg turns negative at {breakeven}bp per side. "
              f"Anything you cannot\nexecute below that is not an edge, it is "
              f"a fee.")


if __name__ == "__main__":
    main()
