"""Strategy C on a POINT-IN-TIME S&P 500 universe (the honest free version).

Reconstructs historical index membership by walking Wikipedia's "changes to the
S&P 500" log backwards from today's list, so at each rebalance the bot may only
choose from names that were *actually in the index then* — fixing the selection
hindsight that inflated run_sp500.py.

REMAINING LIMITATION (stated up front): yfinance only serves prices for tickers
that still trade. Companies that were dropped and later delisted/acquired have no
price history, so they silently fall out of the priced universe. That's residual
*price* survivorship we cannot remove for free — the script reports the coverage
gap so the bias is visible. The fully-clean fix needs a dataset with delisted
prices (CRSP / Sharadar / Norgate).
"""
import io

import pandas as pd
import requests

from metrics import compute_metrics
from plot_equity import plot_equity
from strategy_c import (MARKET, RISK_OFF_TICKERS, _rebased, load_prices,
                        run_allocator)

URL = 'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies'
CHANGE_CUTOFF = pd.Timestamp('1990-01-01')  # bound the priced universe


def _norm(s):
    return str(s).strip().replace('.', '-')


def build_membership():
    """Returns (members_asof(date)->set, current_set, priced_universe_list, changes)."""
    html = requests.get(URL, headers={'User-Agent': 'Mozilla/5.0'}, timeout=30).text
    cur_tbl, ch_tbl = pd.read_html(io.StringIO(html))[:2]
    current = {_norm(s) for s in cur_tbl['Symbol']}

    date_col = ('Effective Date', 'Effective Date')
    add_col, rem_col = ('Added', 'Ticker'), ('Removed', 'Ticker')
    changes = []
    for _, r in ch_tbl.iterrows():
        d = pd.to_datetime(r[date_col], errors='coerce')
        if pd.isna(d):
            continue
        a, rm = r[add_col], r[rem_col]
        a = _norm(a) if isinstance(a, str) and a.strip() and a.lower() != 'nan' else None
        rm = _norm(rm) if isinstance(rm, str) and rm.strip() and rm.lower() != 'nan' else None
        changes.append((d, a, rm))
    changes.sort(key=lambda x: x[0], reverse=True)  # newest first

    def members_asof(T):
        m = set(current)
        for d, a, rm in changes:           # undo every change that happened after T
            if d <= T:
                break
            if a:
                m.discard(a)
            if rm:
                m.add(rm)
        return m

    universe = set(current)
    for d, a, rm in changes:
        if d >= CHANGE_CUTOFF:
            if a:
                universe.add(a)
            if rm:
                universe.add(rm)
    return members_asof, current, sorted(universe), changes


if __name__ == "__main__":
    members_asof, current, universe, changes = build_membership()
    print(f"Current members: {len(current)}  |  ever-members since "
          f"{CHANGE_CUTOFF.year}: {len(universe)}  |  change events: {len(changes)}")
    print("Downloading max history for the full ever-member universe (slow)...")
    close = load_prices(universe + list(RISK_OFF_TICKERS))

    priced = [t for t in universe if t in close.columns and close[t].notna().any()]
    print(f"Priced coverage: {len(priced)}/{len(universe)} "
          f"({len(priced)/len(universe)*100:.0f}%) - the gap is delisted-name "
          f"price survivorship we can't fix for free.")

    start = close[MARKET].dropna().index[200]
    eq, stats, w = run_allocator(close, priced, top_n=10, risk_off='dynamic',
                                 holdable_fn=members_asof)
    eqw = _rebased(eq, start)
    spy = _rebased(close[MARKET], start)

    print(f"\nWindow: {start.date()} -> {close.index[-1].date()}\n")
    print(f"{'Strategy / benchmark':<34} {'CAGR':>6} {'Sharpe':>7} {'maxDD':>7} {'Calmar':>7}")
    print("-" * 66)
    for label, c in [("Strategy C (point-in-time S&P)", eqw), ("SPY buy & hold", spy)]:
        m = compute_metrics(c)
        print(f"{label:<34} {m['cagr']:>5.1f}% {m['sharpe']:>7.2f} "
              f"{m['max_drawdown']:>6.0f}% {m['calmar']:>7.2f}")

    eqw.to_csv("strategy_c_pit_equity.csv")
    plot_equity(eqw, spy,
                title=(f"Strategy C — point-in-time S&P 500 membership (dynamic risk-off)\n"
                       f"{start.date()} -> {close.index[-1].date()}  "
                       f"(priced {len(priced)}/{len(universe)} of ever-members)"),
                out_path="strategy_c_pit.png")
