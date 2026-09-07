"""Gap-shock stress test for the OVERNIGHT selection rule.

WHY THIS MATTERS MORE HERE THAN ANYWHERE ELSE IN THE REPO. stress_gaps.py
established that unpredictable overnight gaps are the one risk no indicator sees
coming, and that position sizing is the only defense: worst-case drawdown fell
from -77% at three names to -31% at twenty.

The overnight strategy is the purest possible exposure to exactly that risk. It
holds through every gap window and earns nothing intraday to cushion one. And
the walk-forward harness keeps selecting N=10, because Sharpe rewards
concentration, which is the sizing the repo's own stress test already flagged as
dangerous. Sharpe cannot see a tail that has not happened yet. This can.

Method mirrors stress_gaps.py so the numbers are comparable: take the strategy's
actual holdings, then Monte-Carlo inject rare catastrophic overnight gaps (a
small per-name-day hazard; a hit marks that position down 40-90%). Report the
MEDIAN and the TAIL separately, because diversification barely moves the median
and is entirely a tail story.
"""
import pickle
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings('ignore')

from metrics import compute_metrics
from walk_forward_overnight import (LOOKBACKS, PRICE_FLOOR, MOVE_CAP, DEGEN_MAX,
                                    START, CACHE, load, cumret, port, equity)

HAZARD_ANNUAL = 0.02       # matches stress_gaps.py
GAP_RANGE = (0.40, 0.90)
N_TRIALS = 500
SEED = 7
LOOKBACK = 252


def main():
    cols, on, intr, full, mask, spy, vol = load()
    idx = on.index
    rb = pd.Series(idx, index=idx).groupby(idx.to_period('M')).tail(1).index
    sig = cumret(on, mask, LOOKBACK).shift(1)
    rng = np.random.default_rng(SEED)

    print(f"\nOvernight selection rule under synthetic gap shocks")
    print(f"hazard {HAZARD_ANNUAL:.0%}/name/year, gap magnitude "
          f"{GAP_RANGE[0]:.0%}-{GAP_RANGE[1]:.0%}, {N_TRIALS} trials")
    print(f"{'N':>4}{'pos':>7}{'base CAGR':>11}{'base DD':>9}"
          f"{'median CAGR':>13}{'p5 CAGR':>10}{'p5 DD':>8}{'WORST DD':>10}")
    print("-" * 74)

    rows = []
    for n in (10, 20, 33, 50, 83):
        sel = pd.DataFrame(False, index=idx, columns=cols)
        for d in rb:
            x = sig.loc[d].where(mask.loc[d]).dropna()
            if len(x) < 50:
                continue
            sel.loc[d, x.sort_values(ascending=False).head(n).index] = True
        sel = (sel.replace(False, np.nan).ffill(limit=31)
                  .fillna(False).astype(bool)) & mask

        cnt = sel.sum(axis=1)
        keep = cnt >= min(5, n)
        w = sel.where(sel).astype(float).div(cnt, axis=0)[keep]
        legs = on.where(mask).loc[keep.index[keep]]
        base = (legs * w).sum(axis=1).dropna()

        # exposure surface: every held name-night is a chance to be gapped
        wv = w.reindex(base.index).fillna(0.0).to_numpy()
        day_pos, col_pos = np.where(wv > 1e-9)
        weights_at = wv[day_pos, col_pos]
        expected = HAZARD_ANNUAL / 252.0 * len(day_pos)

        b = base.to_numpy()
        base_eq = equity(pd.Series(b, index=base.index))
        m0 = compute_metrics(base_eq)

        cagrs, dds = [], []
        for _ in range(N_TRIALS):
            r = b.copy()
            k = rng.poisson(expected)
            if k:
                pick = rng.integers(0, len(day_pos), size=k)
                g = rng.uniform(*GAP_RANGE, size=k)
                np.add.at(r, day_pos[pick], -weights_at[pick] * g)
            eq = equity(pd.Series(r, index=base.index))
            m = compute_metrics(eq)
            cagrs.append(m['cagr'])
            dds.append(m['max_drawdown'])
        cagrs, dds = np.array(cagrs), np.array(dds)
        print(f"{n:>4}{100/n:>6.1f}%{m0['cagr']:>10.1f}%{m0['max_drawdown']:>8.0f}%"
              f"{np.median(cagrs):>12.1f}%{np.percentile(cagrs, 5):>9.1f}%"
              f"{np.percentile(dds, 5):>7.0f}%{dds.min():>9.0f}%")
        rows.append((n, m0['cagr'], m0['max_drawdown'], float(np.median(cagrs)),
                     float(np.percentile(cagrs, 5)), float(np.percentile(dds, 5)),
                     float(dds.min())))

    print("\nRead the two right-hand columns, not the left ones. Diversification")
    print("barely moves the median outcome and is almost entirely a tail story,")
    print("which is the same lesson stress_gaps.py reached for Strategy C.")
    best_tail = max(rows, key=lambda r: r[6])
    print(f"\nWorst-case drawdown is least bad at N={best_tail[0]} "
          f"({best_tail[6]:.0f}%), against {rows[0][6]:.0f}% at N=10.")


if __name__ == "__main__":
    main()
