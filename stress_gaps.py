"""Synthetic gap-shock stress test — the defense against UNPREDICTABLE deaths.

Some collapses (fraud reveals, weekend bankruptcies, SVB-style bank runs) gap
down overnight faster than any trend exit can react. You can't predict them, so
the only defense is structural: keep each position small enough that one gap
can't sink the book. This quantifies that.

Method: take Strategy C's actual daily holdings, then Monte-Carlo inject rare
catastrophic overnight gaps (each held name has a small hazard per day; a hit
marks that position down by 40-90% of its value). We compare diversification
levels (top-3 vs top-10 vs top-20). The lesson to expect: diversification barely
changes the *median* outcome, but it crushes the *tail* (worst-case) — a -80%
gap on a 30% position is portfolio-ending; on a 5% position it's a scratch.
"""
import numpy as np
import pandas as pd

from strategy_c import (BROAD_UNIVERSE, MARKET, RISK_OFF_TICKERS, _rebased,
                        load_prices, run_allocator)

HAZARD_ANNUAL = 0.02      # per held name, prob of a catastrophic gap per year
GAP_RANGE = (0.40, 0.90)  # magnitude of an overnight gap-down
N_TRIALS = 500
SEED = 7


def port_returns(weights, rets, start):
    cols = weights.columns
    return (weights.shift(1) * rets[cols]).sum(axis=1).loc[start:]


def cagr(eq, years):
    return (eq[-1] / eq[0]) ** (1 / years) - 1


def max_dd(eq):
    return (eq / np.maximum.accumulate(eq) - 1).min()


def stress(weights, rets, start, equity_cols, rng):
    base = port_returns(weights, rets, start).values
    n = len(base)
    years = n / 252.0

    # All held equity name-days = the exposure surface for gaps.
    wsub = weights[equity_cols].loc[start:]
    mask = wsub.values > 0.01
    day_pos, col_pos = np.where(mask)
    w_arr = wsub.values[day_pos, col_pos]
    expected = HAZARD_ANNUAL / 252.0 * len(day_pos)

    base_eq = 10_000 * np.cumprod(1 + base)
    out = {"base_cagr": cagr(base_eq, years), "base_dd": max_dd(base_eq),
           "cagrs": [], "dds": []}
    for _ in range(N_TRIALS):
        ret = base.copy()
        k = rng.poisson(expected)
        if k:
            pick = rng.integers(0, len(day_pos), size=k)
            g = rng.uniform(*GAP_RANGE, size=k)
            np.add.at(ret, day_pos[pick], -w_arr[pick] * g)
        eq = 10_000 * np.cumprod(1 + ret)
        out["cagrs"].append(cagr(eq, years))
        out["dds"].append(max_dd(eq))
    out["cagrs"] = np.array(out["cagrs"])
    out["dds"] = np.array(out["dds"])
    return out


if __name__ == "__main__":
    rng = np.random.default_rng(SEED)
    print("Loading data...")
    close = load_prices(BROAD_UNIVERSE + list(RISK_OFF_TICKERS))
    rets = close.pct_change().fillna(0.0)
    start = close[MARKET].dropna().index[200]
    equity_cols = [c for c in BROAD_UNIVERSE if c in close.columns]

    configs = {"Concentrated (top 3)": 3, "Strategy C (top 10)": 10,
               "Diversified (top 20)": 20}

    print(f"\nGap stress: {N_TRIALS} trials, hazard {HAZARD_ANNUAL:.0%}/yr per name, "
          f"gap {GAP_RANGE[0]:.0%}-{GAP_RANGE[1]:.0%}\n")
    print(f"{'Config':<22}{'no-gap CAGR':>12}{'median CAGR':>12}"
          f"{'worst CAGR':>12}{'median DD':>11}{'worst DD':>10}")
    print("-" * 79)
    for name, n in configs.items():
        _, _, w = run_allocator(close, BROAD_UNIVERSE, top_n=n, risk_off='dynamic')
        r = stress(w, rets, start, equity_cols, rng)
        print(f"{name:<22}{r['base_cagr']*100:>11.1f}%{np.median(r['cagrs'])*100:>11.1f}%"
              f"{np.percentile(r['cagrs'], 5)*100:>11.1f}%"
              f"{np.median(r['dds'])*100:>10.0f}%{r['dds'].min()*100:>9.0f}%")

    print("\n('worst' = 5th-percentile CAGR and deepest drawdown across trials - "
          "the tail is what diversification protects.)")
