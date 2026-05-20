"""Equity-curve + drawdown visualization for a backtest.

Reusable: plot_equity(bot_curve, hold_curve, ...) draws a two-panel chart
(growth of $10k on a log axis, plus the underwater/drawdown curve) and
annotates Sharpe / CAGR / max-drawdown for both series.

Run directly to build an equal-weight portfolio across the batch (over the
window the tickers share) and chart bot vs buy-and-hold:
    python plot_equity.py           # daily, up to 10y
    python plot_equity.py --short   # hourly, ~2y
"""
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from backtest_engine import BATCH_TICKERS, run_smart_backtest
from metrics import compute_metrics, drawdown_series
from plot_results import PRESETS


def plot_equity(bot_curve, hold_curve, title, out_path, extras=None):
    bm = compute_metrics(bot_curve)
    hm = compute_metrics(hold_curve)

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(11, 8), sharex=True, gridspec_kw={"height_ratios": [3, 1]})

    ax1.plot(hold_curve.index, hold_curve.values, color="#9aa0a6", lw=1.6,
             label=(f"Buy & Hold  ·  CAGR {hm['cagr']:.0f}%  "
                    f"Sharpe {hm['sharpe']:.2f}  maxDD {hm['max_drawdown']:.0f}%"))
    for series, label, color in (extras or []):
        em = compute_metrics(series)
        ax1.plot(series.index, series.values, color=color, lw=1.4,
                 label=(f"{label}  ·  CAGR {em['cagr']:.0f}%  "
                        f"Sharpe {em['sharpe']:.2f}  maxDD {em['max_drawdown']:.0f}%"))
    ax1.plot(bot_curve.index, bot_curve.values, color="#1a73e8", lw=1.6,
             label=(f"Bot  ·  CAGR {bm['cagr']:.0f}%  "
                    f"Sharpe {bm['sharpe']:.2f}  maxDD {bm['max_drawdown']:.0f}%"))
    ax1.set_yscale("log")
    ax1.set_ylabel("Portfolio value ($, log)")
    ax1.set_title(title, fontsize=12)
    ax1.legend(loc="upper left", frameon=False, fontsize=9)
    ax1.grid(True, which="both", linestyle=":", alpha=0.35)

    ax2.fill_between(hold_curve.index, drawdown_series(hold_curve) * 100, 0,
                     color="#9aa0a6", alpha=0.5)
    ax2.fill_between(bot_curve.index, drawdown_series(bot_curve) * 100, 0,
                     color="#1a73e8", alpha=0.45)
    ax2.set_ylabel("Drawdown (%)")
    ax2.grid(True, linestyle=":", alpha=0.35)

    fig.tight_layout()
    fig.savefig(out_path, dpi=130)
    print(f"Wrote {out_path}")
    print(f"  Bot : CAGR {bm['cagr']:.1f}%  Sharpe {bm['sharpe']:.2f}  "
          f"maxDD {bm['max_drawdown']:.1f}%  Calmar {bm['calmar']:.2f}")
    print(f"  Hold: CAGR {hm['cagr']:.1f}%  Sharpe {hm['sharpe']:.2f}  "
          f"maxDD {hm['max_drawdown']:.1f}%  Calmar {hm['calmar']:.2f}")


def build_portfolio(preset):
    """Equal-weight portfolio over the window the tickers share."""
    bots, holds = [], []
    for ticker in BATCH_TICKERS:
        r = run_smart_backtest(
            ticker, silent=True, return_curves=True,
            period=preset["period"], interval=preset["interval"],
            warmup_bars=preset["warmup_bars"], theta_per_bar=preset["theta_per_bar"])
        if r:
            bots.append(r["bot_curve"].rename(ticker))
            holds.append(r["hold_curve"].rename(ticker))

    bot_df = pd.concat(bots, axis=1).sort_index()
    hold_df = pd.concat(holds, axis=1).sort_index()

    # Keep tickers that cover the full common window (drop late listings so the
    # portfolio is a fair, fixed basket rather than a changing one).
    earliest = min(s.first_valid_index() for s in bots)
    keep = [t for t in bot_df.columns
            if (bot_df[t].first_valid_index() - earliest).days <= 45]
    print(f"Portfolio basket ({len(keep)}): {', '.join(keep)}")

    bot_df = bot_df[keep].dropna()
    hold_df = hold_df[keep].dropna()

    # Equal-weight: each name normalized to its own start, then averaged.
    bot_port = (bot_df / bot_df.iloc[0]).mean(axis=1) * 10_000
    hold_port = (hold_df / hold_df.iloc[0]).mean(axis=1) * 10_000
    return bot_port, hold_port, keep


if __name__ == "__main__":
    name = "short" if "--short" in sys.argv else "long"
    preset = PRESETS[name]
    print(f"Building equal-weight portfolio ({preset['label']})...")
    bot_port, hold_port, keep = build_portfolio(preset)
    plot_equity(
        bot_port, hold_port,
        title=(f"Equal-weight portfolio of {len(keep)} names — Bot vs Buy & Hold "
               f"({preset['label']})"),
        out_path=f"equity_{name}.png")
