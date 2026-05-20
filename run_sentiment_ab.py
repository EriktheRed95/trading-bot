"""A/B test: does the FNSPID news-sentiment tilt improve Strategy C?

Loads the monthly sentiment panel built by fnspid_sentiment.py, adds it to the
ranking as a z-score tilt (using only the PREVIOUS month's sentiment, no
look-ahead), and compares Strategy C with vs without the tilt at a few weights.
Honest verdict either way — sentiment alpha is famously weak.
"""
import pandas as pd

from metrics import compute_metrics
from plot_equity import plot_equity
from strategy_c import (BROAD_UNIVERSE, MARKET, RISK_OFF_TICKERS, _rebased,
                        load_prices, run_allocator)

PANEL = "fnspid_sentiment_monthly.csv"


def load_sentiment(path=PANEL):
    df = pd.read_csv(path)
    # Wide: rows = 'YYYY-MM', cols = symbol, values = mean monthly sentiment.
    return df.pivot_table(index="month", columns="symbol", values="sent_mean")


if __name__ == "__main__":
    print("Loading prices + sentiment panel...")
    close = load_prices(BROAD_UNIVERSE + list(RISK_OFF_TICKERS))
    sent = load_sentiment()
    start = close[MARKET].dropna().index[200]

    covered = [t for t in BROAD_UNIVERSE if t in sent.columns]
    # Start where sentiment is actually dense (>=25 universe names with data),
    # so we measure the tilt over the period it's active, not diluted by years
    # where it's all neutral.
    cov_by_month = sent[covered].notna().sum(axis=1)
    good = cov_by_month[cov_by_month >= 25].index
    cov_start = (good.min() if len(good) else sent.index.min())
    print(f"Sentiment covers {len(covered)}/{len(BROAD_UNIVERSE)} universe names; "
          f"dense from {cov_start} -> {sent.index.max()}")
    sent_start = max(start, pd.Timestamp(cov_start + "-01"))

    print(f"\nWindow: {sent_start.date()} -> {close.index[-1].date()}\n")
    print(f"{'Config':<26}{'CAGR':>7}{'Sharpe':>8}{'maxDD':>8}{'Calmar':>8}")
    print("-" * 57)
    curves = {}
    for label, sw in [("No sentiment (baseline)", 0.0), ("Sentiment tilt 0.5", 0.5),
                      ("Sentiment tilt 1.0", 1.0), ("Sentiment tilt 2.0", 2.0)]:
        eq, _stats, _w = run_allocator(close, BROAD_UNIVERSE, top_n=10,
                                       risk_off='dynamic', sentiment_df=sent,
                                       sentiment_weight=sw)
        eqw = _rebased(eq, sent_start)
        curves[label] = eqw
        m = compute_metrics(eqw)
        print(f"{label:<26}{m['cagr']:>6.1f}%{m['sharpe']:>8.2f}"
              f"{m['max_drawdown']:>7.0f}%{m['calmar']:>8.2f}")

    spy = _rebased(close[MARKET], sent_start)
    m = compute_metrics(spy)
    print(f"{'SPY buy & hold':<26}{m['cagr']:>6.1f}%{m['sharpe']:>8.2f}"
          f"{m['max_drawdown']:>7.0f}%{m['calmar']:>8.2f}")

    plot_equity(curves["Sentiment tilt 1.0"], spy,
                title=(f"Strategy C + FNSPID sentiment tilt vs baseline & SPY\n"
                       f"{sent_start.date()} -> {close.index[-1].date()}"),
                out_path="strategy_c_sentiment.png",
                extras=[(curves["No sentiment (baseline)"], "No sentiment (baseline)", "#e8710a")])
