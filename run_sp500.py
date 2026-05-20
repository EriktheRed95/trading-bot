"""Run Strategy C on the full current S&P 500 (survivorship note below).

This removes *my* hand-picking: the holdable universe is the entire current
index, and the ranker chooses the top-N. Honest caveat that remains — these are
*today's* members, so companies that were dropped/delisted/bankrupted before now
are still missing (classic survivorship bias). A fully point-in-time universe
needs a paid bias-free constituents dataset (CRSP / Sharadar / Norgate). But the
regime-gate drawdown edge is universe-independent, so it should survive.
"""
import io

import pandas as pd
import requests

from metrics import compute_metrics
from plot_equity import plot_equity
from strategy_c import (MARKET, RISK_OFF_TICKERS, _rebased, ew_index,
                        load_prices, run_allocator)


def fetch_sp500():
    html = requests.get('https://en.wikipedia.org/wiki/List_of_S%26P_500_companies',
                        headers={'User-Agent': 'Mozilla/5.0'}, timeout=30).text
    df = pd.read_html(io.StringIO(html))[0]
    return [str(s).replace('.', '-') for s in df['Symbol'].tolist()]


if __name__ == "__main__":
    sp = fetch_sp500()
    print(f"S&P 500 universe: {len(sp)} names. Downloading max history (slow)...")
    close = load_prices(sp + list(RISK_OFF_TICKERS))
    print(f"Data: {close.index[0].date()} -> {close.index[-1].date()} "
          f"({close.shape[1]} columns)")

    start = close[MARKET].dropna().index[200]
    eq, stats, w = run_allocator(close, sp, top_n=10, risk_off='dynamic')
    eqw = _rebased(eq, start)
    spy = _rebased(close[MARKET], start)
    ewb = _rebased(ew_index(close, sp), start)

    print(f"\nWindow: {start.date()} -> {close.index[-1].date()}\n")
    print(f"{'Strategy / benchmark':<32} {'CAGR':>6} {'Sharpe':>7} {'maxDD':>7} {'Calmar':>7}")
    print("-" * 64)
    for label, c in [("Strategy C (S&P500, dynamic)", eqw),
                     ("EW-hold S&P 500", ewb),
                     ("SPY buy & hold", spy)]:
        m = compute_metrics(c)
        print(f"{label:<32} {m['cagr']:>5.1f}% {m['sharpe']:>7.2f} "
              f"{m['max_drawdown']:>6.0f}% {m['calmar']:>7.2f}")

    eqw.to_csv("strategy_c_sp500_equity.csv")
    plot_equity(eqw, spy,
                title=(f"Strategy C on full S&P 500 (dynamic risk-off) vs benchmarks\n"
                       f"{start.date()} -> {close.index[-1].date()}"),
                out_path="strategy_c_sp500.png",
                extras=[(ewb, "EW-hold S&P 500", "#e8710a")])
