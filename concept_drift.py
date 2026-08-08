"""Concept-drift monitor: has the strategy's edge decayed out-of-sample?

The one rigorous idea worth taking from the 2026 "AI trading" hype is that a
backtested edge is a historical artifact. Markets move away from whatever the
strategy was fit on, and the operator's honest job is to notice the decay
early rather than trust the backtest forever.

This module does not predict anything. It compares the strategy's recent
realized behavior against its own historical distribution.

WHAT CHANGED, AND WHY IT MATTERS
--------------------------------
The first version graded on a bare percentile: recent window below the 20th
percentile meant WATCH, below the 5th meant DRIFT. That is guaranteed to fire
on 20% and 5% of readings respectively, no matter how well the strategy is
behaving, because a percentile threshold is arithmetic rather than evidence.
Measured on Strategy C's own curve, WATCH fired on exactly 20.0% of windows.
An alarm that chirps one reading in five teaches you to ignore it.

Worse, validating that alarm showed it carried no information at all. Across
129 non-overlapping windows, the quarter following a WATCH returned +6.19%
against +5.59% otherwise, with a permutation p-value of 0.78. The verdict did
not precede weakness. It described a soft patch that had already happened.

Four corrections, plus the audit that should have existed from the start:

1. PERSISTENCE, CALIBRATED TO A TARGET FIRE RATE. A verdict now requires the
   percentile to stay below its threshold for K consecutive windows, and K is
   chosen by measuring the rule against history until it fires at most
   --target-rate of the time. Rolling Sharpe is heavily autocorrelated, so K
   cannot be derived from theory; it has to be measured. Every report prints
   the rule's own historical fire rate next to its verdict.

2. HONEST SAMPLE SIZE. 63-bar windows stepped one day apart share 62/63 of
   their data. Strategy C's 8,102 windows carry roughly 128 windows' worth of
   independent information (lag-1 autocorrelation 0.975). The percentile is
   still a fair point estimate, but its uncertainty is not what n suggests, so
   a moving-block bootstrap reports a confidence band around it.

3. STALENESS. A verdict computed on an equity curve that stopped months ago
   describes the past, not the present, and now says so loudly.

4. MODE HONESTY. Single-curve mode compares a curve's tail against its own
   body. If that curve is a backtest, this cannot detect live-versus-backtest
   divergence, which is what concept drift actually means. Single-curve
   verdicts are therefore labelled DESCRIPTIVE. Only split mode
   (--baseline backtest --live realized) is DIAGNOSTIC.

5. SELF-AUDIT (--validate). Runs the check that decides whether any of this is
   worth acting on: using only non-overlapping windows, do flagged readings
   actually precede worse forward returns than unflagged ones? A permutation
   test answers it without distributional assumptions. If the p-value is
   large, the verdict is descriptive commentary and should be treated as such.

Verdicts: OK (continue) / WATCH (tighten) / DRIFT (pause, re-examine). None of
them are trade instructions; sizing is the operator's decision.

Usage:
    python concept_drift.py                       # Strategy C, calibrated
    python concept_drift.py --window 63 --target-rate 5
    python concept_drift.py --persistence 10      # fixed K, skip calibration
    python concept_drift.py --baseline backtest.csv --live live_equity.csv
    python concept_drift.py --validate            # does the rule predict anything?

Reuses metrics.py. numpy and pandas only, same as the rest of the repo.
"""
import argparse
from datetime import datetime

import numpy as np
import pandas as pd

from metrics import drawdown_series, periods_per_year

# Percentile cutoffs applied to the baseline rolling-Sharpe distribution.
# On their own these fire at exactly their own value (20% and 5%); the
# persistence requirement below is what turns them into a usable rule.
DRIFT_PCTILE = 5.0
WATCH_PCTILE = 20.0

# Default target for how often the whole rule may fire across history. The
# point of calibration is that this number, not the percentile cutoff, is the
# one the operator actually cares about.
TARGET_FIRE_RATE = 5.0

# A verdict older than this many days is reporting history, not the present.
STALE_AFTER_DAYS = 30

BOOTSTRAP_RESAMPLES = 2000
PERMUTATIONS = 20000
SEED = 0  # fixed so two runs on the same data agree


def load_equity(path):
    """Read an equity CSV (Date index, single value column) into a Series."""
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    s = df.iloc[:, 0].astype(float).dropna()
    s.index = pd.DatetimeIndex(s.index)
    return s.sort_index()


def rolling_sharpe(equity, window, rf_annual=0.0):
    """Annualized Sharpe of each rolling window of length `window` (in bars).

    Indexed by the END timestamp of each window, so the last value is the
    strategy's most-recent realized Sharpe.
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


# --------------------------------------------------------------------------
# 2. Honest sample size
# --------------------------------------------------------------------------

def effective_sample_size(sharpes, window):
    """How many independent observations the overlapping windows are worth.

    Two estimates, because neither is authoritative on its own: the naive
    count of non-overlapping windows, and the autocorrelation-adjusted size
    n * (1 - r) / (1 + r), the standard correction for a first-order
    dependent series. Reported together so the reader sees the range.
    """
    n = len(sharpes)
    non_overlap = n // window if window else n
    r = float(sharpes.autocorr(lag=1)) if n > 2 else 0.0
    r = min(max(r, 0.0), 0.999)
    adjusted = n * (1.0 - r) / (1.0 + r)
    return {"n_windows": n, "non_overlapping": non_overlap,
            "lag1_autocorr": r, "autocorr_adjusted": adjusted}


def percentile_confidence_band(baseline, value, window, level=90.0,
                               resamples=BOOTSTRAP_RESAMPLES):
    """Moving-block bootstrap band around the percentile rank of `value`.

    Blocks of length `window` are resampled with replacement, which preserves
    the autocorrelation an ordinary bootstrap would destroy and would
    therefore produce a falsely tight band from.
    """
    arr = baseline.to_numpy(dtype=float)
    n = len(arr)
    if n < window * 2:
        return None
    rng = np.random.default_rng(SEED)
    n_blocks = int(np.ceil(n / window))
    starts_max = n - window
    pctiles = np.empty(resamples)
    for i in range(resamples):
        starts = rng.integers(0, starts_max + 1, size=n_blocks)
        sample = np.concatenate([arr[s:s + window] for s in starts])[:n]
        pctiles[i] = (sample < value).mean() * 100.0
    lo = float(np.percentile(pctiles, (100.0 - level) / 2))
    hi = float(np.percentile(pctiles, 100.0 - (100.0 - level) / 2))
    return {"low": lo, "high": hi, "level": level}


# --------------------------------------------------------------------------
# 1. Persistence, calibrated against history
# --------------------------------------------------------------------------

def _flag_series(sharpes, pctile_threshold, persistence):
    """Boolean series: would the rule have fired at each point in history?

    Ranks each window against the whole distribution, then requires
    `persistence` consecutive windows below the cutoff. Matches exactly what
    assess() does live, so the fire rate it produces is the real one.
    """
    ranks = sharpes.rank(pct=True) * 100.0
    below = ranks < pctile_threshold
    if persistence <= 1:
        return below
    return below.rolling(persistence).sum() >= persistence


def historical_fire_rate(sharpes, pctile_threshold, persistence):
    """Fraction of history at which this exact rule would have raised a flag."""
    flags = _flag_series(sharpes, pctile_threshold, persistence).dropna()
    if flags.empty:
        return float("nan")
    return float(flags.mean() * 100.0)


def calibrate_persistence(sharpes, pctile_threshold, target_rate, max_k=120):
    """Smallest persistence K whose historical fire rate is at or below target.

    Cannot be derived analytically: consecutive rolling-Sharpe readings are
    almost the same number (lag-1 autocorrelation near 0.98), so K consecutive
    breaches is nowhere near as rare as independence would imply. Measured.
    """
    for k in range(1, max_k + 1):
        if historical_fire_rate(sharpes, pctile_threshold, k) <= target_rate:
            return k, True
    return max_k, False


# --------------------------------------------------------------------------
# 3 + 4. Staleness and mode honesty
# --------------------------------------------------------------------------

def staleness(equity, asof=None):
    """How far behind the present the curve's last bar is."""
    last = pd.Timestamp(equity.index.max())
    now = pd.Timestamp(asof) if asof is not None else pd.Timestamp(datetime.now())
    days = int((now.normalize() - last.normalize()).days)
    return {"last_bar": last, "asof": now, "days": days,
            "is_stale": days > STALE_AFTER_DAYS}


def assess(baseline_equity, test_equity, window=63, rf_annual=0.0,
           persistence=None, target_rate=TARGET_FIRE_RATE, asof=None):
    """Compare a test curve's recent window against a baseline distribution.

    In single-curve mode the most-recent window is excluded from the baseline
    so it is not compared against itself.
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

    # 1. Calibrate the persistence requirement, then apply it.
    calibrated = False
    if persistence is None:
        persistence, converged = calibrate_persistence(
            base_sharpes, WATCH_PCTILE, target_rate)
        calibrated = converged
    watch_rate = historical_fire_rate(base_sharpes, WATCH_PCTILE, persistence)
    drift_rate = historical_fire_rate(base_sharpes, DRIFT_PCTILE, persistence)
    naive_watch_rate = historical_fire_rate(base_sharpes, WATCH_PCTILE, 1)

    # How long the test curve has sat below each cutoff, most recent first.
    test_ranks = test_sharpes.apply(lambda v: (dist < v).mean() * 100.0)

    def _streak(threshold):
        n = 0
        for v in test_ranks.iloc[::-1]:
            if v < threshold:
                n += 1
            else:
                break
        return n

    watch_streak, drift_streak = _streak(WATCH_PCTILE), _streak(DRIFT_PCTILE)

    # Drawdown breach stays a hard flag. It is not percentile-based, so it
    # carries no built-in fire rate, and a depth never reached in-sample is
    # genuinely out-of-sample behavior.
    base_max_dd = float(drawdown_series(baseline_equity).min())
    cur_dd = float(drawdown_series(test_equity).iloc[-1])
    dd_breach = cur_dd < base_max_dd

    if dd_breach or (pctile < DRIFT_PCTILE and drift_streak >= persistence):
        verdict = "DRIFT"
    elif pctile < WATCH_PCTILE and watch_streak >= persistence:
        verdict = "WATCH"
    else:
        verdict = "OK"

    # What the old bare-percentile rule would have said, so the difference
    # between "unusual" and "actionable" stays visible.
    naive = ("DRIFT" if (pctile < DRIFT_PCTILE or dd_breach)
             else "WATCH" if pctile < WATCH_PCTILE else "OK")

    test_ret = test_equity.pct_change().dropna()
    base_ret = baseline_equity.pct_change().dropna()

    return {
        "verdict": verdict,
        "naive_verdict": naive,
        "mode": "split" if not single else "single",
        # 4. Single-curve mode cannot see live-versus-backtest divergence.
        "interpretation": "DIAGNOSTIC" if not single else "DESCRIPTIVE",
        "window": window,
        "recent_end": recent_end,
        "recent_sharpe": recent,
        "baseline_sharpe_median": float(dist.median()),
        "sharpe_percentile": pctile,
        "percentile_band": percentile_confidence_band(dist, recent, window),
        "persistence": persistence,
        "persistence_calibrated": calibrated,
        "target_rate": target_rate,
        "watch_streak": watch_streak,
        "drift_streak": drift_streak,
        "watch_fire_rate": watch_rate,
        "drift_fire_rate": drift_rate,
        "naive_watch_fire_rate": naive_watch_rate,
        "sample": effective_sample_size(base_sharpes, window),
        "staleness": staleness(test_equity, asof),
        "recent_hit_rate": float((test_ret.iloc[-window:] > 0).mean() * 100.0),
        "baseline_hit_rate": float((base_ret > 0).mean() * 100.0),
        "current_drawdown": cur_dd * 100.0,
        "baseline_max_drawdown": base_max_dd * 100.0,
        "drawdown_breach": dd_breach,
        "single_curve": single,
    }


# --------------------------------------------------------------------------
# 5. Self-audit: does the rule precede anything?
# --------------------------------------------------------------------------

def validate(equity, window=63, rf_annual=0.0, persistence=1,
             pctile_threshold=WATCH_PCTILE, permutations=2000):
    """Did flagged readings actually precede worse forward returns?

    Each observation must be a NON-OVERLAPPING window, or the test silently
    reads the same quarter hundreds of times and manufactures significance.
    A window-length grid gives one such set, and there are `window` distinct
    phase offsets of that grid, so the test runs once per phase and the spread
    across phases is reported.

    Deliberately NOT pooled across phases. Pooling looks like it multiplies
    the sample by `window`, but every bar reappears in every phase, so the
    pooled p-value is inflated by exactly the dependence this function exists
    to avoid. On Strategy C, pooling turned a p of 0.78 into 0.048 without a
    single new observation. Phases also share data with each other, so these
    are not independent tests either; the spread is a sensitivity check on the
    arbitrary choice of gridding, not evidence multiplied.

    Reports statistical power honestly. Calibrating the rule to fire on 5% of
    history means roughly 0.05 * (bars / window) independent flagged
    observations, which on 32 years of daily data is single digits. A rule
    that rare cannot be validated on this much data, and saying so is the
    correct output rather than a p-value nobody should trust.
    """
    rs = rolling_sharpe(equity, window, rf_annual)
    fwd = equity.pct_change(window).shift(-window).reindex(rs.index)
    flags = _flag_series(rs, pctile_threshold, persistence)
    rng = np.random.default_rng(SEED)

    per_phase = []
    for phase in range(window):
        idx = np.arange(phase, len(rs), window)
        f, u = [], []
        for i in idx:
            v, fl = fwd.iloc[i], flags.iloc[i]
            if pd.isna(v) or pd.isna(fl):
                continue
            (f if bool(fl) else u).append(float(v))
        if len(f) < 2 or len(u) < 2:
            continue
        a, b = np.array(f), np.array(u)
        obs = float(a.mean() - b.mean())
        pool = np.concatenate([a, b])
        k = len(a)
        null = np.empty(permutations)
        for i in range(permutations):
            p = rng.permutation(pool)
            null[i] = p[:k].mean() - p[k:].mean()
        per_phase.append({"diff": obs,
                          "p": float((np.abs(null) >= abs(obs)).mean()),
                          "n_flag": len(a), "n_un": len(u)})

    independent = len(rs) // window
    if not per_phase:
        return {"ok": False, "independent_windows": independent,
                "reason": f"the rule flags too little of history to test. With "
                          f"~{independent} independent {window}-bar windows, no "
                          f"phase offset contains even 2 flagged observations."}

    diffs = np.array([d["diff"] for d in per_phase]) * 100.0
    ps = np.array([d["p"] for d in per_phase])
    med_flag = float(np.median([d["n_flag"] for d in per_phase]))

    return {
        "ok": True,
        "window": window,
        "persistence": persistence,
        "pctile_threshold": pctile_threshold,
        "independent_windows": independent,
        "phases_tested": len(per_phase),
        "median_flagged_per_phase": med_flag,
        "diff_median": float(np.median(diffs)),
        "diff_lo": float(np.percentile(diffs, 5)),
        "diff_hi": float(np.percentile(diffs, 95)),
        "p_median": float(np.median(ps)),
        "p_min": float(ps.min()),
        "frac_significant": float((ps < 0.05).mean() * 100.0),
        "frac_wrong_direction": float((diffs > 0).mean() * 100.0),
        # Fewer than ~10 independent flagged observations cannot support a
        # conclusion in either direction, and pretending otherwise is how a
        # monitor ends up trusted for no reason.
        "underpowered": med_flag < 10,
    }


RECS = {
    "OK": "Recent behavior is inside the range this strategy has always "
          "produced. Nothing here argues for a change.",
    "WATCH": "Risk-adjusted return has been in the lower tail long enough to "
             "clear the calibrated persistence bar. That makes it unusual, "
             "not predictive: check the validation below before treating it "
             "as a reason to act.",
    "DRIFT": "The strategy has been performing worse than it almost ever did "
             "in-sample, or it has breached its worst historical drawdown. "
             "Treat the edge as suspect and re-examine the regime gate and "
             "universe before trusting fresh signals.",
}


def print_report(r):
    line = "=" * 72
    print(line)
    print(f"  CONCEPT-DRIFT MONITOR  ->  {r['verdict']}   [{r['interpretation']}]")
    print(line)

    # 3. Staleness first: a stale verdict should be read as history.
    st = r["staleness"]
    if st["is_stale"]:
        print(f"  !! STALE: the curve's last bar is {st['last_bar'].date()}, "
              f"{st['days']} days ago.")
        print("     This describes a window that closed months back, not the "
              "present.")
        print()

    # 4. Say plainly what this mode can and cannot establish.
    if r["single_curve"]:
        print("  MODE: single curve. This ranks the curve's own tail against "
              "its own body.")
        print("     If this curve is a backtest, it CANNOT detect live-versus-"
              "backtest")
        print("     divergence, which is what concept drift means. Verdict is "
              "descriptive.")
        print("     For a diagnostic read: --baseline backtest.csv --live "
              "realized.csv")
    else:
        print("  MODE: split. Live curve judged against the backtest baseline. "
              "Diagnostic.")
    print()

    print(f"  Window            : {r['window']} bars, ending {r['recent_end'].date()}")
    print(f"  Recent Sharpe     : {r['recent_sharpe']:+.2f}"
          f"   (baseline median {r['baseline_sharpe_median']:+.2f})")

    band = r["percentile_band"]
    band_txt = (f"  [{band['level']:.0f}% band {band['low']:.1f} to {band['high']:.1f}]"
                if band else "")
    print(f"  Percentile rank   : {r['sharpe_percentile']:.1f}{band_txt}")

    # 2. Never print the inflated window count without its honest counterpart.
    s = r["sample"]
    print(f"  Sample size       : {s['n_windows']:,} overlapping windows, worth "
          f"~{s['non_overlapping']:,} independent")
    print(f"                      (lag-1 autocorrelation {s['lag1_autocorr']:.3f}; "
          f"autocorr-adjusted ~{s['autocorr_adjusted']:.0f})")
    print()

    # 1. The rule's own base rate, next to its verdict.
    how = "calibrated" if r["persistence_calibrated"] else "fixed"
    print(f"  Rule              : below the {WATCH_PCTILE:.0f}th percentile for "
          f"{r['persistence']} consecutive windows ({how})")
    print(f"  Fires historically: {r['watch_fire_rate']:.1f}% of the time "
          f"(target was {r['target_rate']:.0f}%)")
    print(f"                      a bare percentile cut fires "
          f"{r['naive_watch_fire_rate']:.1f}% of the time by construction")
    print(f"  Current streak    : {r['watch_streak']} consecutive windows below "
          f"the {WATCH_PCTILE:.0f}th")
    # An OK that turns on one more day is not an all-clear, and printing it as
    # a bare "OK" would be the same overconfidence in the other direction.
    short_by = r["persistence"] - r["watch_streak"]
    if r["verdict"] == "OK" and 0 < short_by <= max(2, r["persistence"] // 10):
        print(f"  !! MARGINAL       : {short_by} window(s) short of flagging. This "
              f"is a boundary")
        print(f"                      reading, not a clean pass; it flips if the "
              f"streak continues.")
    if r["naive_verdict"] != r["verdict"]:
        print(f"  NOTE              : the old bare-percentile rule would have "
              f"said {r['naive_verdict']}"
              f" (it fires {r['naive_watch_fire_rate']:.0f}% of the time).")
    print()

    print(f"  Recent hit rate   : {r['recent_hit_rate']:.1f}%  "
          f"(baseline {r['baseline_hit_rate']:.1f}%)")
    print(f"  Current drawdown  : {r['current_drawdown']:.1f}%")
    print(f"  Worst in baseline : {r['baseline_max_drawdown']:.1f}%"
          + ("   <-- BREACHED" if r["drawdown_breach"] else ""))
    print(line)
    print("  " + RECS[r["verdict"]].replace("\n", "\n  "))
    print(line)
    print("  Not a trade instruction. Position sizing is the operator's call.")
    print(line)


def print_validation(v):
    line = "=" * 72
    print()
    print(line)
    print("  SELF-AUDIT: does a flag precede anything?")
    print(line)
    if not v.get("ok"):
        print(f"  UNTESTABLE: {v['reason']}")
        print()
        print("  READ: this is not a pass. A rule nobody can test is a rule nobody")
        print("  should act on. Either loosen it (raise --target-rate) so it fires")
        print("  often enough to audit, or accept the verdict as commentary only.")
        print(line)
        return

    print(f"  Rule tested       : below the {v['pctile_threshold']:.0f}th percentile "
          f"for {v['persistence']} consecutive windows")
    print(f"  Design            : non-overlapping {v['window']}-bar windows, tested "
          f"once per phase")
    print(f"                      offset ({v['phases_tested']} offsets, "
          f"~{v['independent_windows']} independent windows each). Not pooled: "
          f"pooling")
    print(f"                      reuses every bar {v['phases_tested']} times and "
          f"inflates the p-value.")
    print()
    print(f"  Flagged observations per offset : ~{v['median_flagged_per_phase']:.0f} "
          f"(median)")
    print(f"  Forward-return gap, flagged minus not:")
    print(f"      median {v['diff_median']:+.2f} pct pts, "
          f"5th to 95th across offsets {v['diff_lo']:+.2f} to {v['diff_hi']:+.2f}")
    print(f"  Permutation p-value            : median {v['p_median']:.3f}, "
          f"best {v['p_min']:.3f}")
    print(f"  Offsets reaching p < 0.05      : {v['frac_significant']:.0f}%")
    print(f"  Offsets where the gap points   : {v['frac_wrong_direction']:.0f}% "
          f"the WRONG way (flag preceded BETTER returns)")
    print()

    if v["underpowered"]:
        print(f"  READ: UNDERPOWERED, so no conclusion either way. The calibrated "
              f"rule")
        print(f"  leaves only ~{v['median_flagged_per_phase']:.0f} independent flagged "
              f"observations, and no test on that")
        print("  many can separate signal from noise. This is the honest cost of")
        print("  calibrating to a rare fire rate on a single 32-year curve, and it")
        print("  means the verdict above stays descriptive until there is either a")
        print("  live curve to judge or a looser rule that can actually be audited.")
    elif v["p_median"] > 0.10:
        print("  READ: no detectable relationship. On this curve the flag does not")
        print("  precede weakness, so treat the verdict above as a description of")
        print("  what already happened, not a forecast. A rule that cannot pass")
        print("  this test has not earned the right to change position sizing.")
    elif v["diff_median"] < 0:
        print("  READ: flagged readings did precede weaker forward returns across")
        print("  most griddings. The verdict carries information.")
    else:
        print("  READ: the gap points the WRONG way; flags preceded BETTER returns.")
        print("  Acting on them would have cost money, not saved it.")
    print(line)


def main():
    ap = argparse.ArgumentParser(
        description="Detect concept drift in a strategy's realized equity curve.")
    ap.add_argument("--equity", default="strategy_c_equity.csv",
                    help="Equity CSV for single-curve mode (default: "
                         "strategy_c_equity.csv).")
    ap.add_argument("--baseline", help="Backtest equity CSV (split mode).")
    ap.add_argument("--live", help="Live/paper equity CSV to judge (split mode).")
    ap.add_argument("--window", type=int, default=63,
                    help="Rolling window length in bars (default 63 ~ one quarter).")
    ap.add_argument("--rf", type=float, default=0.0,
                    help="Annual risk-free rate, e.g. 0.04 for 4%% (default 0).")
    ap.add_argument("--persistence", type=int, default=None,
                    help="Consecutive windows below the cutoff before flagging. "
                         "Omit to calibrate against --target-rate.")
    ap.add_argument("--target-rate", type=float, default=TARGET_FIRE_RATE,
                    help="Max %% of history the rule may fire on when calibrating "
                         f"(default {TARGET_FIRE_RATE:.0f}).")
    ap.add_argument("--validate", action="store_true",
                    help="Audit whether the rule precedes worse forward returns.")
    ap.add_argument("--asof", help="Treat this date as today, for staleness "
                                    "(default: the real current date).")
    args = ap.parse_args()

    if args.live or args.baseline:
        if not (args.live and args.baseline):
            ap.error("split mode needs both --baseline and --live")
        base = load_equity(args.baseline)
        test = load_equity(args.live)
    else:
        base = test = load_equity(args.equity)

    r = assess(base, test, window=args.window, rf_annual=args.rf,
               persistence=args.persistence, target_rate=args.target_rate,
               asof=args.asof)
    print_report(r)

    if args.validate:
        print_validation(validate(base, window=args.window, rf_annual=args.rf,
                                   persistence=r["persistence"]))


if __name__ == "__main__":
    main()
