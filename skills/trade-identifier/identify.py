"""Trade identifier — apply the trading bot's Strategy C rules to live tickers.

Standalone (only needs yfinance, pandas, numpy). For each ticker it fetches live
daily data and reports the disciplined verdict from Strategy C's logic:
  - market regime gate (is SPY above its 200d SMA?)
  - per-name trend filter (above its own 200d? positive 12-1 month momentum?)
  - momentum + trend-strength rank
  - annualized volatility -> position-size / fragility flag

Usage:  python identify.py NVDA ACHR GOOGL ...

NOT financial advice — a discipline check, not a recommendation to trade.
"""
import sys

import numpy as np
import pandas as pd
import yfinance as yf

MARKET = "SPY"


def fetch(tickers):
    syms = sorted(set(t.upper() for t in tickers) | {MARKET})
    data = yf.download(syms, period="2y", interval="1d", auto_adjust=True, progress=False)
    close = data["Close"] if isinstance(data.columns, pd.MultiIndex) else data[["Close"]]
    return close.sort_index().ffill()


def signals(close, t):
    if t not in close.columns:
        return None
    s = close[t].dropna()
    if len(s) < 210:
        return None
    sma200 = s.rolling(200).mean().iloc[-1]
    price = s.iloc[-1]
    mom12 = (s.iloc[-21] / s.iloc[-252] - 1) if len(s) >= 252 else np.nan
    mom6 = (s.iloc[-21] / s.iloc[-126] - 1) if len(s) >= 126 else np.nan
    vol = s.pct_change().tail(63).std() * np.sqrt(252)
    return {"price": price, "sma200": sma200, "above": price > sma200,
            "trend": price / sma200 - 1, "mom12": mom12, "mom6": mom6, "vol": vol}


def size_note(vol):
    if np.isnan(vol):
        return "unknown vol"
    if vol < 0.25:
        return f"normal size (vol {vol:.0%})"
    if vol < 0.50:
        return f"reduce size (elevated vol {vol:.0%})"
    return f"SMALL / fragile - high gap-down risk (vol {vol:.0%})"


def verdict(sig):
    if sig is None:
        return "NO DATA", "insufficient price history"
    pos_mom = (sig["mom12"] is not None) and (not np.isnan(sig["mom12"])) and sig["mom12"] > 0
    if sig["above"] and pos_mom:
        return "ELIGIBLE (trend buy candidate)", "above 200d + positive 12-1 momentum"
    reasons = []
    if not sig["above"]:
        reasons.append("below 200d (downtrend)")
    if not pos_mom:
        reasons.append("negative/flat 12-1 momentum")
    return "AVOID (per rules)", "; ".join(reasons) or "fails trend filter"


def _z(a):
    a = np.array(a, float)
    sd = np.nanstd(a)
    return (a - np.nanmean(a)) / sd if sd else a * 0.0


def show_legend():
    """Print the technical part of the metric legend so guidance appears with results."""
    import os
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "METRICS_LEGEND.md")
    try:
        txt = open(p, encoding="utf-8").read()
        body = txt[txt.index("## Part A"):txt.index("## Part B")].strip()
    except Exception:
        return
    print("\n" + "=" * 64)
    print("HOW TO READ THIS - metric legend (want vs avoid):\n")
    print(body)


def main(tickers):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    close = fetch(tickers)
    spy = close[MARKET].dropna()
    risk_on = spy.iloc[-1] > spy.rolling(200).mean().iloc[-1]
    print(f"As of {close.index[-1].date()}  |  MARKET REGIME: "
          f"{'RISK-ON (SPY above 200d)' if risk_on else 'RISK-OFF (SPY below 200d) — bot would be defensive'}")
    print("=" * 64)

    rows = []
    for t in [x.upper() for x in tickers]:
        sig = signals(close, t)
        v, why = verdict(sig)
        rows.append((t, sig, v, why))
        print(f"\n{t}")
        if sig is None:
            print(f"   {v} — {why}")
            continue
        print(f"   price ${sig['price']:.2f} | 200d ${sig['sma200']:.2f} "
              f"| {sig['trend']:+.0%} vs 200d")
        m12 = f"{sig['mom12']:+.0%}" if not np.isnan(sig['mom12']) else "n/a"
        m6 = f"{sig['mom6']:+.0%}" if not np.isnan(sig['mom6']) else "n/a"
        print(f"   12-1 momentum {m12} | 6-1 momentum {m6}")
        print(f"   VERDICT: {v}  ({why})")
        print(f"   size: {size_note(sig['vol'])}")
        if not risk_on and v.startswith("ELIGIBLE"):
            print("   note: market regime is RISK-OFF — bot would hold this only at reduced exposure")

    # Rank the eligible names if several were given.
    elig = [(t, s) for t, s, v, _ in rows if v.startswith("ELIGIBLE")]
    if len(elig) > 1:
        comp = (_z([s["mom12"] for _, s in elig]) + _z([s["mom6"] for _, s in elig])
                + _z([s["trend"] for _, s in elig]))
        order = np.argsort(comp)[::-1]
        print("\n" + "=" * 64)
        print("RANK among eligible (strongest trend/momentum first):")
        for r, i in enumerate(order, 1):
            print(f"   {r}. {elig[i][0]}")

    show_legend()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python identify.py TICKER [TICKER ...]")
        sys.exit(1)
    main(sys.argv[1:])
