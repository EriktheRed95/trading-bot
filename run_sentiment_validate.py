"""#2 sub-period + #3 orthogonalization tests for the sentiment tilt.

#3 (the real test): compare baseline vs RAW sentiment vs ORTHOGONAL sentiment
(the part of sentiment NOT explained by momentum/trend). If the orthogonal tilt
still beats baseline -> genuine news alpha. If it collapses to baseline -> the
"edge" was just a momentum/popularity proxy.

#2: slice the baseline vs raw-sentiment equity curves by regime (calm bull,
COVID, 2022 bear, recovery) to see WHERE sentiment helps.
"""
import pandas as pd

from metrics import compute_metrics
from run_sentiment_ab import load_sentiment
from strategy_c import (BROAD_UNIVERSE, MARKET, RISK_OFF_TICKERS, _rebased,
                        load_prices, run_allocator)

PERIODS = {
    "2013-2019 (calm bull)": ("2013-01-01", "2019-12-31"),
    "2020 (COVID)":          ("2020-01-01", "2020-12-31"),
    "2021 (bull)":           ("2021-01-01", "2021-12-31"),
    "2022 (bear)":           ("2022-01-01", "2022-12-31"),
    "2023-2026 (recovery)":  ("2023-01-01", "2026-05-20"),
}


def period_return(eq, s, e):
    seg = eq.loc[s:e]
    if len(seg) < 2:
        return None
    return seg.iloc[-1] / seg.iloc[0] - 1


if __name__ == "__main__":
    print("Loading prices + sentiment...")
    close = load_prices(BROAD_UNIVERSE + list(RISK_OFF_TICKERS))
    sent = load_sentiment()
    start = close[MARKET].dropna().index[200]
    covered = [t for t in BROAD_UNIVERSE if t in sent.columns]
    cov_by_month = sent[covered].notna().sum(axis=1)
    good = cov_by_month[cov_by_month >= 25].index
    sent_start = max(start, pd.Timestamp((good.min() if len(good) else sent.index.min()) + "-01"))

    runs = {
        "Baseline":              dict(sentiment_weight=0.0),
        "Raw sentiment ×2.0":    dict(sentiment_df=sent, sentiment_weight=2.0),
        "Orthogonal sent ×2.0":  dict(sentiment_df=sent, sentiment_weight=2.0, orthogonalize=True),
    }
    curves = {}
    print(f"\n#3 ORTHOGONALIZATION -full window {sent_start.date()} -> {close.index[-1].date()}\n")
    print(f"{'Config':<24}{'CAGR':>7}{'Sharpe':>8}{'maxDD':>8}{'Calmar':>8}")
    print("-" * 55)
    for label, kw in runs.items():
        eq, _, _ = run_allocator(close, BROAD_UNIVERSE, top_n=10, risk_off='dynamic', **kw)
        eqw = _rebased(eq, sent_start)
        curves[label] = eqw
        m = compute_metrics(eqw)
        print(f"{label:<24}{m['cagr']:>6.1f}%{m['sharpe']:>8.2f}"
              f"{m['max_drawdown']:>7.0f}%{m['calmar']:>8.2f}")

    print(f"\n#2 SUB-PERIOD RETURNS -baseline vs raw sentiment\n")
    print(f"{'Period':<24}{'Baseline':>10}{'Sentiment':>11}{'Delta':>8}")
    print("-" * 53)
    base, sentc = curves["Baseline"], curves["Raw sentiment ×2.0"]
    for name, (s, e) in PERIODS.items():
        rb, rs = period_return(base, s, e), period_return(sentc, s, e)
        if rb is None or rs is None:
            continue
        print(f"{name:<24}{rb*100:>9.0f}%{rs*100:>10.0f}%{(rs-rb)*100:>7.0f}%")
