"""Live picks — Strategy C ranking with an optional Bot 2 sentiment tilt.

This is where the systematic core (Strategy C) and Bot 2 (news/sentiment) meet:
the same trend/momentum composite that the backtest uses, computed as of TODAY,
optionally nudged by a news-sentiment z-score. It prints the portfolio the bot
would hold right now, and shows how sentiment shifted the ranking.

LIVE ONLY — this is not a backtest. Free news is recent-only, so the sentiment
tilt can inform today's picks but cannot be honestly backtested without
point-in-time historical news (a paid dataset). Keep the tilt modest; sentiment
is noisy and decays fast.
"""
import numpy as np
import pandas as pd

from news_sentiment import universe_sentiment
from strategy_c import (BROAD_UNIVERSE, MARKET, RISK_OFF_TICKERS, _zscore,
                        load_prices)


def live_ranking(close, holdable, top_n=10, sentiment_weight=0.5, use_sentiment=True):
    close = close.sort_index().ffill()
    holdable = [t for t in holdable if t in close.columns]
    d = close.index[-1]

    sma = close.rolling(200).mean()
    mom_long = close.shift(21) / close.shift(252) - 1.0
    mom_mid = close.shift(21) / close.shift(126) - 1.0
    trend = close / sma - 1.0
    vol = close.pct_change().rolling(63).std() * np.sqrt(252)
    risk_on = bool(close[MARKET].iloc[-1] > sma[MARKET].iloc[-1])

    elig = [t for t in holdable
            if close.at[d, t] > sma.at[d, t] and mom_long.at[d, t] > 0
            and not np.isnan(vol.at[d, t]) and vol.at[d, t] > 0]

    base = (_zscore(mom_long.loc[d, elig]) + _zscore(mom_mid.loc[d, elig])
            + _zscore(trend.loc[d, elig]))

    tilt = pd.Series(0.0, index=elig)
    sent_raw = {}
    if use_sentiment and elig:
        sent = universe_sentiment(elig)
        sent_raw = {t: sent[t]["mean"] for t in elig}
        tilt = _zscore(pd.Series(sent_raw)) * sentiment_weight

    composite = (base + tilt).sort_values(ascending=False)
    return {
        "date": d, "risk_on": risk_on, "eligible": elig,
        "base": base, "tilt": tilt, "sentiment": sent_raw,
        "picks_with": composite.head(top_n).index.tolist(),
        "picks_without": base.sort_values(ascending=False).head(top_n).index.tolist(),
        "composite": composite,
    }


if __name__ == "__main__":
    print("Loading 2y of prices for live ranking...")
    close = load_prices(BROAD_UNIVERSE + list(RISK_OFF_TICKERS), period="2y")
    r = live_ranking(close, BROAD_UNIVERSE, top_n=10)

    print(f"\nAs of {r['date'].date()} - market regime: "
          f"{'RISK-ON (hold equities)' if r['risk_on'] else 'DEFENSIVE (go to risk-off sleeve)'}")

    if not r["risk_on"]:
        sma = close.rolling(200).mean()
        for c in RISK_OFF_TICKERS:
            up = close[c].iloc[-1] > sma[c].iloc[-1]
            print(f"  {c}: {'trending up -> hold' if up else 'not trending -> skip'}")

    print(f"\nTop 10 picks WITH sentiment tilt vs WITHOUT:")
    print(f"{'rank':<5}{'with tilt':<10}{'without':<10}{'moved?'}")
    for i in range(min(10, len(r["picks_with"]))):
        wt = r["picks_with"][i]
        wo = r["picks_without"][i] if i < len(r["picks_without"]) else "-"
        print(f"{i+1:<5}{wt:<10}{wo:<10}{'' if wt == wo else '<-- changed'}")

    print(f"\nSentiment scores driving the tilt:")
    for t in r["picks_with"][:10]:
        print(f"  {t:<6} sentiment={r['sentiment'].get(t, 0):+.3f}  "
              f"base_z={r['base'].get(t, 0):+.2f}  tilt={r['tilt'].get(t, 0):+.2f}")
