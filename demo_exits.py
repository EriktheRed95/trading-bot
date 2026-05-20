"""Demo: how much of a crash does a trend exit actually avoid?

For real big-crashers we DO have data for (dot-com 2000-02, GFC 2007-09, the
2022 drawdown), this compares four outcomes from the pre-crash peak:

  - HOLD to the trough          (what buy-and-hold suffers)
  - exit on first close < 200d  (Strategy C's slow gate)
  - exit on first close < 50d   (a faster trend stop)
  - exit on -15% trailing stop  (fastest)

The point: most "deaths" are slow enough that a trend rule exits *long before*
the bottom — direct evidence that the dead names excluded from our universe
(Lehman, Enron, ...) would have been CUT, not ridden to zero. The genuinely
instantaneous gaps are what the synthetic stress test (stress_gaps.py) covers.
"""
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
import yfinance as yf

EPISODES = {
    "Dot-com (2000-02)": (("2000-01-01", "2003-01-01"),
                          ["CSCO", "INTC", "ORCL", "QCOM", "MU"]),
    "GFC (2007-09)":      (("2007-06-01", "2009-07-01"),
                          ["C", "BAC", "AIG", "GE", "MS", "GS"]),
    "2022 drawdown":      (("2021-09-01", "2023-02-01"),
                          ["META", "NFLX", "PYPL", "NVDA", "TSLA", "RIVN"]),
}


def load(tickers):
    data = yf.download(sorted(set(tickers)), period="max", interval="1d",
                       auto_adjust=True, progress=False)
    close = data["Close"] if isinstance(data.columns, pd.MultiIndex) else data[["Close"]]
    close.index = pd.to_datetime(close.index).tz_localize(None)
    return close.sort_index()


def first_true(mask):
    hits = mask[mask].index
    return hits[0] if len(hits) else None


def analyze(s, start, end):
    s = s.dropna()
    win = s.loc[start:end]
    if len(win) < 60:
        return None
    sma200, sma50 = s.rolling(200).mean(), s.rolling(50).mean()
    peak_date = win.idxmax()
    peak = win.loc[peak_date]
    post = s.loc[peak_date:end]
    trough = post.min()
    hold_dd = trough / peak - 1

    def dd_at(exit_date):
        if exit_date is None:
            return None
        return s.loc[exit_date] / peak - 1

    e200 = first_true((post < sma200.loc[post.index]))
    e50 = first_true((post < sma50.loc[post.index]))
    trail = first_true(post < post.cummax() * 0.85)
    return {"peak": peak_date.date(), "hold_dd": hold_dd,
            "dd200": dd_at(e200), "dd50": dd_at(e50), "dd_trail": dd_at(trail)}


def fmt(x):
    return f"{x*100:>5.0f}%" if x is not None else "   --"


if __name__ == "__main__":
    all_tk = sorted({t for _, ts in EPISODES.values() for t in ts})
    print("Downloading crash-era data...")
    close = load(all_tk)

    rows = []
    for era, ((start, end), tks) in EPISODES.items():
        print(f"\n{era}    {'HOLD':>7} {'exit<200d':>10} {'exit<50d':>10} {'-15% trail':>11}")
        print("-" * 56)
        for t in tks:
            if t not in close.columns:
                print(f"  {t:<6} (no data)")
                continue
            r = analyze(close[t], start, end)
            if not r:
                print(f"  {t:<6} (insufficient window)")
                continue
            print(f"  {t:<6} {fmt(r['hold_dd']):>9} {fmt(r['dd200']):>10} "
                  f"{fmt(r['dd50']):>10} {fmt(r['dd_trail']):>11}")
            rows.append({"era": era, "ticker": t, **r})

    df = pd.DataFrame(rows)
    if not df.empty:
        print("\n=== Average max loss from the peak ===")
        print(f"  HOLD to trough : {df['hold_dd'].mean()*100:>5.0f}%")
        print(f"  exit < 200d    : {df['dd200'].dropna().mean()*100:>5.0f}%")
        print(f"  exit < 50d     : {df['dd50'].dropna().mean()*100:>5.0f}%")
        print(f"  -15% trailing  : {df['dd_trail'].dropna().mean()*100:>5.0f}%")

    # Illustrative chart: Citigroup through the GFC.
    if "C" in close.columns:
        s = close["C"].loc["2006-06-01":"2009-12-31"].dropna()
        sma200 = s.rolling(200).mean()
        fig, ax = plt.subplots(figsize=(10, 5))
        ax.plot(s.index, s.values, color="#1a73e8", lw=1.4, label="Citigroup (C)")
        ax.plot(sma200.index, sma200.values, color="#d1242f", lw=1.2, label="200-day SMA")
        ex = first_true((s < sma200.loc[s.index]).loc[s.idxmax():])
        if ex is not None:
            ax.axvline(ex, color="#1a7f37", ls="--", lw=1.2,
                       label=f"200d exit ({ex.date()}, {s.loc[ex]/s.loc[s.idxmax()]-1:.0%} from peak)")
        ax.set_yscale("log")
        ax.set_title("Trend exit vs riding it down — Citigroup through the GFC")
        ax.legend(frameon=False)
        fig.tight_layout()
        fig.savefig("demo_exit_citi.png", dpi=130)
        print("\nWrote demo_exit_citi.png")
