"""Run the batch backtest and render a BOT-vs-buy-and-hold comparison chart.

Two presets:
  (default)  ~729 days of HOURLY bars  -> results.csv / results.png
  --long     up to 10 YEARS of DAILY bars -> results_10y.csv / results_10y.png

The long preset is a separate experiment: Yahoo caps hourly history at ~730d,
so a decade-long test must use daily bars. Thresholds + option decay were tuned
for hourly, so treat the two charts as different regimes, not the same bot.

Usage:
    python plot_results.py              # 2y hourly, fresh run
    python plot_results.py --long       # 10y daily, fresh run
    python plot_results.py --long --cache   # re-plot from existing CSV
"""
import sys

import matplotlib

matplotlib.use("Agg")  # headless: write a file, never open a window
import matplotlib.pyplot as plt
import pandas as pd

from backtest_engine import (
    BATCH_TICKERS,
    OPTION_THETA_PER_HOUR,
    WARMUP_BARS,
    run_smart_backtest,
)

ID_ORDER = {"ROCKET": 0, "GRINDER": 1, "FORTRESS": 2}

PRESETS = {
    "short": dict(
        period="729d", interval="1h",
        warmup_bars=WARMUP_BARS, theta_per_bar=OPTION_THETA_PER_HOUR,
        label="729d hourly", csv="results.csv", png="results.png",
    ),
    "long": dict(
        period="10y", interval="1d",
        warmup_bars=250,                          # ~1 trading year of daily warm-up
        theta_per_bar=OPTION_THETA_PER_HOUR * 24,  # per-hour -> per-day decay
        label="up to 10y daily", csv="results_10y.csv", png="results_10y.png",
    ),
}


def collect_results(preset):
    rows = []
    for ticker in BATCH_TICKERS:
        res = run_smart_backtest(
            ticker, silent=True,
            period=preset["period"], interval=preset["interval"],
            warmup_bars=preset["warmup_bars"], theta_per_bar=preset["theta_per_bar"],
        )
        if res:
            res["bot_ret"] = float(res["bot_ret"])
            res["hold_ret"] = float(res["hold_ret"])
            rows.append(res)
            print(f"  {res['ticker']:<6} {res['id']:<8} "
                  f"bot {res['bot_ret']:>8.1f}%  hold {res['hold_ret']:>8.1f}%  "
                  f"(from {res.get('start_date', '?')})")
    df = pd.DataFrame(rows)
    df.to_csv(preset["csv"], index=False)
    return df


def plot(df, preset):
    df = df.copy()
    df["id_rank"] = df["id"].map(ID_ORDER).fillna(9)
    df = df.sort_values(["id_rank", "hold_ret"]).reset_index(drop=True)

    wins = int((df["bot_ret"] > df["hold_ret"]).sum())
    n = len(df)

    y = range(n)
    height = 0.4

    fig, ax = plt.subplots(figsize=(11, 8))

    ax.barh([i + height / 2 for i in y], df["hold_ret"], height=height,
            color="#9aa0a6", label="Buy & Hold")
    bot_colors = ["#1a7f37" if b > h else "#d1242f"
                  for b, h in zip(df["bot_ret"], df["hold_ret"])]
    ax.barh([i - height / 2 for i in y], df["bot_ret"], height=height,
            color=bot_colors, label="Bot (green = beat hold)")

    span = max(df[["bot_ret", "hold_ret"]].abs().max()) or 1
    pad = span * 0.01
    for i, (b, h) in enumerate(zip(df["bot_ret"], df["hold_ret"])):
        ax.text(h + (pad if h >= 0 else -pad), i + height / 2, f"{h:.0f}%",
                va="center", ha="left" if h >= 0 else "right", fontsize=8, color="#5f6368")
        ax.text(b + (pad if b >= 0 else -pad), i - height / 2, f"{b:.0f}%",
                va="center", ha="left" if b >= 0 else "right", fontsize=8,
                color="#1a7f37" if b > h else "#d1242f", fontweight="bold")

    labels = [f"{t}\n{i}" for t, i in zip(df["ticker"], df["id"])]
    ax.set_yticks(list(y))
    ax.set_yticklabels(labels, fontsize=9)
    ax.invert_yaxis()
    ax.axvline(0, color="#202124", linewidth=0.8)
    ax.set_xlabel("Total return over backtest window (%)")
    ax.set_title(
        f"Adaptive Hybrid Bot vs Buy-and-Hold\n"
        f"Bot beats buy-and-hold on {wins}/{n} names "
        f"({preset['label']}, slippage + commission applied, no look-ahead)",
        fontsize=12)
    ax.legend(loc="lower right", frameon=False)
    ax.grid(axis="x", linestyle=":", alpha=0.4)
    fig.tight_layout()
    fig.savefig(preset["png"], dpi=130)
    print(f"\nWrote {preset['png']}  (bot wins {wins}/{n})")

    df["outperf"] = df["bot_ret"] - df["hold_ret"]
    print(f"Median bot - hold spread: {df['outperf'].median():.1f}%")
    print(f"Best:  {df.loc[df['outperf'].idxmax(), 'ticker']} ({df['outperf'].max():+.0f}%)")
    print(f"Worst: {df.loc[df['outperf'].idxmin(), 'ticker']} ({df['outperf'].min():+.0f}%)")


if __name__ == "__main__":
    preset = PRESETS["long" if "--long" in sys.argv else "short"]
    if "--cache" in sys.argv:
        print(f"Plotting from cached {preset['csv']} ...")
        data = pd.read_csv(preset["csv"])
    else:
        print(f"Running batch backtest ({preset['label']})...")
        data = collect_results(preset)
    plot(data, preset)
