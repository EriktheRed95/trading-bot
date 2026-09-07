"""Re-run the point-in-time Strategy C number with a data-quality filter.

WHY. The README's 32.2% point-in-time CAGR was computed on a universe that
deliberately includes delisted names, and free price data on delisted tickers is
broken. Cooper Industries (CBE) delisted in 2012 yet still prints rows in 2016
with a prior close of $0.005 against an open of $170. Ten of the 682 priced names
print single-session moves above 500%. Those names are not merely present: they
PASS Strategy C's eligibility filter on 13,072 name-days and reach the momentum
top 10 on 190 rebalance dates, with CPWR and MI still reading eligible in 2026,
years after they stopped trading.

So 32.2% was never shown to be wrong. It was shown to be unverified. This script
verifies it.

METHOD. Two runs, identical in every respect except the eligibility rule:
  A. UNFILTERED, reproducing the published number as a control. If this does not
     land near 32.2%, the comparison below means nothing and the script says so.
  B. FILTERED, where a name is eligible on a date only if its entire signal
     window is trustworthy.

THE FILTER, stated so it can be argued with:
  A name-day is UNTRUSTWORTHY if the close is under $1, or the close-to-close
  move exceeds +/-50%. A name is then INELIGIBLE on date d if any untrustworthy
  day falls in its trailing 252-session signal window, because that is the window
  the 12-1 momentum and 200-day trend filters actually read. Excluding the bad
  bar alone would not help: the contamination enters through the lookback.

This is deliberately conservative. It removes real acquisition pops along with
the garbage. The alternative, leaving in a bar that implies +3,399,900%, is worse.
"""
import pickle
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings('ignore')

from metrics import compute_metrics
from strategy_c import MARKET, RISK_OFF_TICKERS, _rebased, load_prices, run_allocator
from run_sp500_pit import build_membership

PRICE_FLOOR = 1.00        # dollars
MOVE_CAP = 0.50           # close-to-close
SIGNAL_WINDOW = 252       # the 12-1 momentum lookback
CACHE = Path(r"C:\Users\erik9\AppData\Local\Temp\claude\C--Users-erik9"
             r"\fec8c69e-fbe3-42b7-9d5b-9fabc52842d8\scratchpad\pit_ohlc.pkl")


def load_close():
    """Adjusted closes for the ever-member universe plus SPY and the sleeve."""
    members_asof, _cur, universe, _ch = build_membership()
    if CACHE.exists():
        print("using cached prices for the ever-member universe...")
        _n, _uni, cols, _op, cl = pickle.load(open(CACHE, "rb"))
        need = [t for t in RISK_OFF_TICKERS if t not in cl.columns]
        if need:
            print(f"fetching the risk-off sleeve {need}...")
            extra = load_prices(need)
            cl = cl.join(extra[[c for c in need if c in extra.columns]], how="left")
    else:
        print("downloading the full ever-member universe (slow)...")
        cl = load_prices(universe + list(RISK_OFF_TICKERS))
        cols = [t for t in universe if t in cl.columns and cl[t].notna().any()]
    priced = [t for t in universe if t in cl.columns and cl[t].notna().any()]
    return members_asof, universe, priced, cl.sort_index()


def build_clean_mask(close, priced):
    """True where a name's whole signal window is trustworthy."""
    px = close[priced]
    step = px / px.shift(1) - 1.0
    bad = (px < PRICE_FLOOR) | (step.abs() > MOVE_CAP)
    bad = bad.fillna(False)
    # A single bad bar poisons every signal that reads over it.
    poisoned = bad.rolling(SIGNAL_WINDOW, min_periods=1).max().fillna(0).astype(bool)
    clean = ~poisoned
    n_bad = int(bad.sum().sum())
    worst = step.abs().max().sort_values(ascending=False).head(10)
    print(f"\nuntrustworthy name-days: {n_bad:,} "
          f"({n_bad / max(1, int(px.notna().sum().sum())) * 100:.3f}% of priced)")
    print("worst single-session moves by name:")
    for t, v in worst.items():
        print(f"   {t:<7} {v:>14.1%}")
    return clean


def run(close, priced, holdable_fn, label):
    start = close[MARKET].dropna().index[200]
    eq, stats, _w = run_allocator(close, priced, top_n=10, risk_off='dynamic',
                                  holdable_fn=holdable_fn)
    eqw = _rebased(eq, start)
    m = compute_metrics(eqw)
    print(f"{label:<44}{m['cagr']:>7.1f}%{m['sharpe']:>8.2f}"
          f"{m['max_drawdown']:>7.0f}%{m['calmar']:>8.2f}")
    return eqw, m


if __name__ == "__main__":
    members_asof, universe, priced, close = load_close()
    print(f"ever-members {len(universe)}, priced {len(priced)} "
          f"({len(priced) / len(universe) * 100:.0f}%)")

    clean = build_clean_mask(close, priced)

    def holdable_unfiltered(d):
        return members_asof(d)

    def holdable_filtered(d):
        members = members_asof(d)
        if d not in clean.index:
            return members
        row = clean.loc[d]
        return {t for t in members if t in row.index and bool(row[t])}

    start = close[MARKET].dropna().index[200]
    print(f"\nwindow {start.date()} -> {close.index[-1].date()}\n")
    print(f"{'run':<44}{'CAGR':>8}{'Sharpe':>8}{'maxDD':>7}{'Calmar':>8}")
    print("-" * 75)

    eq_a, m_a = run(close, priced, holdable_unfiltered, "A. UNFILTERED (reproduces the README)")
    eq_b, m_b = run(close, priced, holdable_filtered, "B. FILTERED (data-quality screen on)")
    spy = _rebased(close[MARKET], start)
    m_s = compute_metrics(spy)
    print(f"{'SPY buy and hold':<44}{m_s['cagr']:>7.1f}%{m_s['sharpe']:>8.2f}"
          f"{m_s['max_drawdown']:>7.0f}%{m_s['calmar']:>8.2f}")

    print()
    if abs(m_a['cagr'] - 32.2) > 3.0:
        print(f"CONTROL FAILED. The unfiltered run reproduces {m_a['cagr']:.1f}%, not the "
              f"32.2% in the README.\nThe two runs above are still comparable to each "
              f"other, but neither is\ncomparable to the published number, so do not read "
              f"the delta as 'the\ncontamination was worth X points'. Investigate the "
              f"discrepancy first.")
    else:
        print(f"Control holds: unfiltered reproduces {m_a['cagr']:.1f}% against the "
              f"README's 32.2%.")
    print(f"Filtering moves CAGR {m_a['cagr']:.1f}% -> {m_b['cagr']:.1f}% "
          f"({m_b['cagr'] - m_a['cagr']:+.1f} points), Sharpe "
          f"{m_a['sharpe']:.2f} -> {m_b['sharpe']:.2f}, maxDD "
          f"{m_a['max_drawdown']:.0f}% -> {m_b['max_drawdown']:.0f}%.")
    eq_b.to_csv("strategy_c_pit_filtered_equity.csv")
    print("wrote strategy_c_pit_filtered_equity.csv")
