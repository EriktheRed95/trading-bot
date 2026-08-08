"""Concept-drift monitor — has the strategy's edge decayed out-of-sample?

The single rigorous idea worth taking from the 2026 "AI trading" hype is this:
a backtested edge is a historical artifact. Markets change, and an algorithm's
predictive power decays as conditions shift away from what it was fit on. That
decay is called *concept drift*, and the honest job of the human operator is to
detect it early and cut size (or pause) before it bleeds the account — not to
trust the backtest forever.

This module does not predict anything. It compares the strategy's RECENT realized
behavior against its own historical distribution and answers one question with a
graded verdict: is what's happening now still inside the range the backtest said
to expect, or has it fallen into the tail?

Two checks, both distribution-based (no magic thresholds pulled from the air):

  1. Rolling-Sharpe percentile. Compute the Sharpe of every rolling window of
     length W across the baseline history, then ask where the most-recent window
     ranks in that distribution. A recent window in the bottom 5% is a tail event
     the backtest rarely produced — a drift signal.

  2. Drawdown breach. If the current underwater depth exceeds the WORST drawdown
     ever seen in the baseline, the strategy is doing something out-of-sample that
     it never did in-sample. That is a hard drift flag on its own.

Verdicts: OK (continue) / WATCH (tighten, halve size) / DRIFT (pause, re-examine).

Usage:
    # Single curve: judge its own recent window against its earlier history.
    python concept_drift.py
    python concept_drift.py --equity strategy_c_sp500_equity.csv --window 63

    # Split: baseline distribution from the backtest, judged against a live/paper
    # equity curve you've been recording.
    python concept_drift.py --baseline strategy_c_equity.csv --live live_equity.csv

Reuses metrics.py. No new dependencies.
"""
import argparse

import numpy as np
import pandas as pd

from metrics import compute_metrics, drawdown_series, periods_per_year

# Grading cutoffs, expressed as percentiles of the baseline rolling-Sharpe
# distribution. These are deliberately conservative: a recent window has to be
# genuinely unusual, not merely below average, before we act on it.
DRIFT_PCTILE = 5.0    # recent window in the bottom 5% -> DRIFT
WATCH_PCTILE = 20.0   # recent window in the bottom 20% -> WATCH


def load_equity(path):
    """Read an equity CSV (Date index, single value column) into a Series."""
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    s = df.iloc[:, 0].astype(float).dropna()
    s.index = pd.DatetimeIndex(s.index)
    return s.sort_index()


def rolling_sharpe(equity, window, rf_annual=0.0):
    """Annualized Sharpe of each rolling window of length `window` (in bars).

    Returned Series is indexed by the END timestamp of each window, so the last
    value is the strategy's most-recent realized Sharpe.
    """
    returns = equity.pct_change().dropna()
    if len(returns) < window:
        return pd.Series(dtype=float)
    ppy = periods_per_year(equity.index)
    rf_per = rf_annual / ppy
    mean = returns.rolling(window).mean()
    std = returns.rolling(window).std()
    sharpe = (mean - rf_per) / std.replace(0.0, np.nan) * np.sqrt(ppy)
    return sharpe.dropna()


def assess(baseline_equity, test_equity, window=63, rf_annual=0.0):
    """Compare a test curve's recent window against a baseline distribution.

    When baseline_equity is the same object as test_equity (single-curve mode),
    the most-recent window is excluded from the baseline so it isn't compared
    against itself.
    """
    single = test_equity is baseline_equity

    base_sharpes = rolling_sharpe(baseline_equity, window, rf_annual)
    test_sharpes = rolling_sharpe(test_equity, window, rf_annual)
    if base_sharpes.empty or test_sharpes.empty:
        raise ValueError(
            f"Not enough history for a {window}-bar window "
            f"(baseline has {len(baseline_equity)} bars, "
            f"test has {len(test_equity)} bars)."
        )

    recent = float(test_sharpes.iloc[-1])
    recent_end = test_sharpes.index[-1]

    dist = base_sharpes.iloc[:-1] if single else base_sharpes
    pctile = float((dist < recent).mean() * 100.0)

    # Drawdown breach: current underwater depth vs worst in the baseline.
    base_max_dd = float(drawdown_series(baseline_equity).min())   # <= 0
    cur_dd = float(drawdown_series(test_equity).iloc[-1])         # <= 0
    dd_breach = cur_dd < base_max_dd

    # Hit-rate context (share of up-bars), recent window vs baseline average.
    test_ret = test_equity.pct_change().dropna()
    base_ret = baseline_equity.pct_change().dropna()
    recent_hit = float((test_ret.iloc[-window:] > 0).mean() * 100.0)
    base_hit = float((base_ret > 0).mean() * 100.0)

    if pctile < DRIFT_PCTILE or dd_breach:
        verdict = "DRIFT"
    elif pctile < WATCH_PCTILE:
        verdict = "WATCH"
    else:
        verdict = "OK"

    return {
        "verdict": verdict,
        "window": window,
        "recent_end": recent_end,
        "recent_sharpe": recent,
        "baseline_sharpe_median": float(dist.median()),
        "sharpe_percentile": pctile,
        "recent_hit_rate": recent_hit,
        "baseline_hit_rate": base_hit,
        "current_drawdown": cur_dd * 100.0,
        "baseline_max_drawdown": base_max_dd * 100.0,
        "drawdown_breach": dd_breach,
        "single_curve": single,
    }


RECS = {
    "OK": "Recent behavior is inside the backtest's normal range. Keep running the "
          "system as designed; no change to sizing.",
    "WATCH": "Recent risk-adjusted return has slipped into the lower tail of the "
             "backtest. Not an emergency, but tighten up: halve new position sizes "
             "and watch the next few weeks before adding risk.",
    "DRIFT": "The strategy is performing worse than it almost ever did in-sample, or "
             "it has breached its worst historical drawdown. Treat the edge as "
             "suspect: pause new entries, reduce exposure, and re-examine the regime "
             "gate and universe before trusting fresh signals.",
}


def print_report(r):
    line = "=" * 64
    print(line)
    print(f"  CONCEPT-DRIFT MONITOR  ->  {r['verdict']}")
    print(line)
    mode = "single curve (recent window vs its own history)" if r["single_curve"] \
        else "split (live curve vs backtest baseline)"
    print(f"  Mode              : {mode}")
    print(f"  Window            : {r['window']} bars, ending {r['recent_end'].date()}")
    print()
    print(f"  Recent Sharpe     : {r['recent_sharpe']:+.2f}")
    print(f"  Baseline median   : {r['baseline_sharpe_median']:+.2f}")
    print(f"  Percentile rank   : {r['sharpe_percentile']:.1f}  "
          f"(bottom {DRIFT_PCTILE:.0f}% = DRIFT, bottom {WATCH_PCTILE:.0f}% = WATCH)")
    print()
    print(f"  Recent hit rate   : {r['recent_hit_rate']:.1f}%  "
          f"(baseline {r['baseline_hit_rate']:.1f}%)")
    print(f"  Current drawdown  : {r['current_drawdown']:.1f}%")
    print(f"  Worst in baseline : {r['baseline_max_drawdown']:.1f}%"
          + ("   <-- BREACHED" if r["drawdown_breach"] else ""))
    print(line)
    print("  " + RECS[r["verdict"]].replace("\n", "\n  "))
    print(line)


def main():
    ap = argparse.ArgumentParser(description="Detect concept drift in a strategy's "
                                             "realized equity curve.")
    ap.add_argument("--equity", default="strategy_c_equity.csv",
                    help="Equity CSV for single-curve mode (default: "
                         "strategy_c_equity.csv).")
    ap.add_argument("--baseline", help="Backtest equity CSV (split mode).")
    ap.add_argument("--live", help="Live/paper equity CSV to judge (split mode).")
    ap.add_argument("--window", type=int, default=63,
                    help="Rolling window length in bars (default 63 ~ one quarter).")
    ap.add_argument("--rf", type=float, default=0.0,
                    help="Annual risk-free rate, e.g. 0.04 for 4%% (default 0).")
    args = ap.parse_args()

    if args.live or args.baseline:
        if not (args.live and args.baseline):
            ap.error("split mode needs both --baseline and --live")
        base = load_equity(args.baseline)
        test = load_equity(args.live)
    else:
        base = test = load_equity(args.equity)

    r = assess(base, test, window=args.window, rf_annual=args.rf)
    print_report(r)


if __name__ == "__main__":
    main()
