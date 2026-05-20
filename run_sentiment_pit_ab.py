"""#1 — Does the sentiment edge survive on a BROAD, point-in-time S&P universe?

If sentiment is just a mega-cap-popularity proxy, its edge should shrink when the
ranker chooses from the whole point-in-time index (not a curated megacap pool).
Same point-in-time membership reconstruction as run_sp500_pit.py, plus the
sentiment tilt.
"""
import pandas as pd

from metrics import compute_metrics
from run_sentiment_ab import load_sentiment
from run_sp500_pit import build_membership
from strategy_c import (MARKET, RISK_OFF_TICKERS, _rebased, load_prices,
                        run_allocator)

if __name__ == "__main__":
    members_asof, current, universe, _ = build_membership()
    print(f"Point-in-time universe: {len(universe)} ever-members. Downloading...")
    close = load_prices(universe + list(RISK_OFF_TICKERS))
    sent = load_sentiment()
    priced = [t for t in universe if t in close.columns and close[t].notna().any()]

    start = close[MARKET].dropna().index[200]
    covered = [t for t in priced if t in sent.columns]
    cov_by_month = sent[covered].notna().sum(axis=1)
    good = cov_by_month[cov_by_month >= 50].index
    cov_start = good.min() if len(good) else sent.index.min()
    sent_start = max(start, pd.Timestamp(cov_start + "-01"))
    print(f"Priced {len(priced)}; sentiment covers {len(covered)}; "
          f"dense from {cov_start}")

    print(f"\nWindow: {sent_start.date()} -> {close.index[-1].date()}\n")
    print(f"{'Config':<26}{'CAGR':>7}{'Sharpe':>8}{'maxDD':>8}{'Calmar':>8}")
    print("-" * 57)
    for label, sw, orth in [("No sentiment (baseline)", 0.0, False),
                            ("Sentiment ×1.0", 1.0, False),
                            ("Sentiment ×2.0", 2.0, False),
                            ("Sentiment ×2.0 orthogonal", 2.0, True)]:
        eq, _, _ = run_allocator(close, priced, top_n=10, risk_off='dynamic',
                                 holdable_fn=members_asof, sentiment_df=sent,
                                 sentiment_weight=sw, orthogonalize=orth)
        m = compute_metrics(_rebased(eq, sent_start))
        print(f"{label:<26}{m['cagr']:>6.1f}%{m['sharpe']:>8.2f}"
              f"{m['max_drawdown']:>7.0f}%{m['calmar']:>8.2f}")

    spy = _rebased(close[MARKET], sent_start)
    m = compute_metrics(spy)
    print(f"{'SPY buy & hold':<26}{m['cagr']:>6.1f}%{m['sharpe']:>8.2f}"
          f"{m['max_drawdown']:>7.0f}%{m['calmar']:>8.2f}")
