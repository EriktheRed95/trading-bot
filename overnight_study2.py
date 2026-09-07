"""The two tests that decide whether overnight selection is a real edge.

Study 1 found the effect is strongly concentrated: selecting the top quintile on
trailing overnight return grosses 21.0% CAGR at Sharpe 1.42 against 7.9% for
holding every name overnight, and it survives publication. That is promising
enough to deserve the two questions that usually kill this kind of result.

TEST A: IS IT JUST MOMENTUM WEARING A COSTUME?
  Strategy C already harvests 12-1 momentum. If ranking on trailing OVERNIGHT
  return picks the same names as ranking on trailing TOTAL return, there is no
  new information here and the honest move is to route it back into Strategy C
  rather than build a second system. This repo already ran exactly this test on
  the sentiment tilt and it is the test that dismantled the story, so the
  overnight signal gets the same treatment: rank on each, measure the overlap,
  and orthogonalize the overnight signal against momentum to see whether the
  RESIDUAL still carries the effect.

TEST B: IS THE EDGE THE SELECTION OR THE OVERNIGHT TIMING?
  A top-quintile basket held OVERNIGHT ONLY must be compared against the same
  basket held ALL DAY. If holding the same selected names around the clock wins,
  then the selection rule is the edge and the overnight timing is a drag that
  adds 504 transactions a year for nothing. This is the same
  equal-weight-hold-of-the-same-names discipline that made this repo's earlier
  results defensible.

Plus the cost sweep on the SELECTED portfolio, which study 1 only ran on the
all-names version. Commissions are zero at US retail brokers; the swept number
is auction slippage for market-on-close and market-on-open execution.
"""
import json
import pickle
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings('ignore')

from metrics import compute_metrics
from strategy_c import MARKET
from run_sp500_pit import build_membership

PRICE_FLOOR, MOVE_CAP = 1.00, 0.50
START, PUBLICATION = '2007-01-01', '2019-01-01'
LOOKBACK = 252
CACHE = Path(r"C:\Users\erik9\AppData\Local\Temp\claude\C--Users-erik9"
             r"\fec8c69e-fbe3-42b7-9d5b-9fabc52842d8\scratchpad\pit_ohlc.pkl")


def equity(r):
    return (1.0 + pd.Series(r).astype(float).fillna(0.0)).cumprod()


def load():
    members_asof, _c, universe, changes = build_membership()
    _n, _u, cols, op, cl = pickle.load(open(CACHE, "rb"))
    on = op[cols] / cl[cols].shift(1) - 1.0
    intr = cl[cols] / op[cols] - 1.0
    full = cl[cols] / cl[cols].shift(1) - 1.0
    idx = on.index[on.index >= pd.Timestamp(START)]
    on, intr, full = on.loc[idx], intr.loc[idx], full.loc[idx]
    opx, clx, prevx = op[cols].loc[idx], cl[cols].loc[idx], cl[cols].shift(1).loc[idx]
    ok = (on.notna() & intr.notna()
          & (prevx >= PRICE_FLOOR) & (opx >= PRICE_FLOOR) & (clx >= PRICE_FLOOR)
          & (on.abs() <= MOVE_CAP) & (intr.abs() <= MOVE_CAP))
    chg = sorted({d for d, _a, _r in changes if d >= idx[0]} | {idx[0]})
    snap = {d: [t in members_asof(d) for t in cols] for d in chg}
    member = (pd.DataFrame.from_dict(snap, orient='index', columns=cols)
                .reindex(idx.union(pd.DatetimeIndex(chg))).ffill().reindex(idx)
                .fillna(False).astype(bool).shift(1).fillna(False).astype(bool))
    return cols, on, intr, full, (ok & member), cl[MARKET].reindex(idx).ffill()


def cumret(leg, mask, n):
    return (1 + leg.where(mask).fillna(0.0)).rolling(n).apply(np.prod, raw=True) - 1.0


def zs(row):
    sd = row.std()
    return (row - row.mean()) / sd if sd and not np.isnan(sd) else row * 0.0


def select(signal, mask, idx, cols, rb, frac=5, min_pool=50):
    sel = pd.DataFrame(False, index=idx, columns=cols)
    for d in rb:
        s = signal.loc[d].where(mask.loc[d]).dropna()
        if len(s) < min_pool:
            continue
        sel.loc[d, s.sort_values(ascending=False).head(max(20, len(s) // frac)).index] = True
    sel = sel.replace(False, np.nan).ffill(limit=31).fillna(False).astype(bool)
    return sel & mask


def port(leg, mask, min_names=20):
    n = mask.sum(axis=1)
    w = mask.where(mask).astype(float).div(n, axis=0)
    return (leg * w).sum(axis=1)[n >= min_names]


def line(lab, eq):
    m = compute_metrics(eq.dropna())
    print(f"{lab:<50}{m['cagr']:>7.1f}%{m['sharpe']:>9.2f}{m['max_drawdown']:>7.0f}%")
    return m


def head(t):
    print(f"\n{t}")
    print(f"{'':<50}{'CAGR':>8}{'Sharpe':>9}{'maxDD':>8}")
    print("-" * 76)


def main():
    cols, on, intr, full, mask, spy = load()
    idx = on.index
    rb = pd.Series(idx, index=idx).groupby(idx.to_period('M')).tail(1).index
    out = {}

    sig_on = cumret(on, mask, LOOKBACK).shift(1)      # trailing overnight return
    sig_mom = cumret(full, mask, LOOKBACK).shift(1)   # plain 12-month momentum
    sig_in = cumret(intr, mask, LOOKBACK).shift(1)    # trailing intraday return

    # ------------------------------------------------------------- TEST A
    print("=" * 76)
    print("TEST A  IS THE OVERNIGHT SIGNAL JUST MOMENTUM?")
    print("=" * 76)

    # 1) cross-sectional correlation and name overlap at each rebalance
    corrs, overlaps = [], []
    for d in rb:
        a = sig_on.loc[d].where(mask.loc[d]).dropna()
        b = sig_mom.loc[d].reindex(a.index).dropna()
        a = a.reindex(b.index)
        if len(a) < 50:
            continue
        corrs.append(a.rank().corr(b.rank()))   # Spearman = Pearson on ranks
        ta = set(a.sort_values(ascending=False).head(max(20, len(a) // 5)).index)
        tb = set(b.sort_values(ascending=False).head(max(20, len(b) // 5)).index)
        overlaps.append(len(ta & tb) / max(1, len(ta)))
    print(f"\nrank correlation, overnight signal vs 12-month momentum:")
    print(f"   median Spearman rho = {np.median(corrs):.3f}   "
          f"(n = {len(corrs)} rebalances)")
    print(f"   median name overlap in the selected quintile = {np.median(overlaps)*100:.0f}%")
    print(f"\n   Note on sample size: these {len(corrs)} rebalances are monthly and")
    print(f"   non-overlapping, so they are honest independent observations. No")
    print(f"   pooling across overlapping windows is done anywhere in this file.")

    # 2) orthogonalize the overnight signal against momentum, cross-sectionally
    resid = pd.DataFrame(np.nan, index=rb, columns=cols)
    for d in rb:
        a = sig_on.loc[d].where(mask.loc[d]).dropna()
        b = sig_mom.loc[d].reindex(a.index)
        keep = b.notna()
        a, b = a[keep], b[keep]
        if len(a) < 50:
            continue
        az, bz = zs(a), zs(b)
        denom = float((bz * bz).sum())
        beta = float((az * bz).sum()) / denom if denom else 0.0
        resid.loc[d, a.index] = (az - beta * bz).values
    resid = resid.reindex(idx).ffill(limit=31)

    head("selection signal compared, top quintile held OVERNIGHT, ZERO cost")
    res = {}
    for lab, sig in (("rank on trailing OVERNIGHT return", sig_on),
                     ("rank on 12-month MOMENTUM (close to close)", sig_mom),
                     ("rank on trailing INTRADAY return", sig_in),
                     ("rank on OVERNIGHT RESIDUAL vs momentum", resid)):
        s = select(sig, mask, idx, cols, rb)
        res[lab] = line("  " + lab, equity(port(on.where(mask), s)))
    line("  no selection, ALL names overnight", equity(port(on.where(mask), mask)))
    out['test_a'] = {k: v for k, v in res.items()}
    out['rho_median'] = float(np.median(corrs))
    out['overlap_median'] = float(np.median(overlaps))

    # ------------------------------------------------------------- TEST B
    print("\n" + "=" * 76)
    print("TEST B  IS THE EDGE THE SELECTION, OR THE OVERNIGHT TIMING?")
    print("=" * 76)
    sel_on = select(sig_on, mask, idx, cols, rb)
    gate = (spy > spy.rolling(200).mean()).shift(1).fillna(False)

    for lab, m_ in (("2007 to 2026, full", idx >= pd.Timestamp(START)),
                    ("2007 to 2018, BEFORE publication",
                     (idx >= pd.Timestamp(START)) & (idx < pd.Timestamp(PUBLICATION))),
                    ("2019 to 2026, AFTER publication", idx >= pd.Timestamp(PUBLICATION))):
        mm = pd.DataFrame(np.repeat(m_[:, None], len(cols), axis=1),
                          index=idx, columns=cols)
        s, mk = sel_on & mm, mask & mm
        on_p = port(on.where(mask), s)
        fu_p = port(full.where(mask), s)          # SAME names, held all day
        all_fu = port(full.where(mask), mk)
        g = on_p.where(gate.reindex(on_p.index).fillna(False), 0.0)
        head(lab + "   [ZERO cost]")
        line("  top quintile, OVERNIGHT only", equity(on_p))
        line("  top quintile, OVERNIGHT only + SPY 200d gate", equity(g))
        line("  SAME top quintile, held ALL DAY", equity(fu_p))
        line("  EW hold of ALL names (survivorship baseline)", equity(all_fu))
        line("  SPY buy and hold", equity(spy.reindex(on_p.index).dropna().pct_change()))

    # ------------------------------------------------------------- COSTS
    print("\n" + "=" * 76)
    print("COST SWEEP ON THE SELECTED PORTFOLIO")
    print("commissions are ZERO at US retail brokers; this is auction slippage")
    print("for market-on-close and market-on-open execution")
    print("=" * 76)
    for lab, m_ in (("2007 to 2026, full", idx >= pd.Timestamp(START)),
                    ("2019 to 2026, AFTER publication", idx >= pd.Timestamp(PUBLICATION))):
        mm = pd.DataFrame(np.repeat(m_[:, None], len(cols), axis=1),
                          index=idx, columns=cols)
        s, mk = sel_on & mm, mask & mm
        on_p = port(on.where(mask), s)
        fu_p = port(full.where(mask), s)
        gate_p = on_p.where(gate.reindex(on_p.index).fillna(False), 0.0)
        bar_same = compute_metrics(equity(fu_p))['cagr']
        bar_all = compute_metrics(equity(port(full.where(mask), mk)))['cagr']
        print(f"\n{lab}")
        print(f"   bars to clear: same names held all day = {bar_same:.1f}%, "
              f"EW hold all names = {bar_all:.1f}%")
        print(f"{'bp/side':>9}{'overnight':>12}{'+gate':>10}{'Sharpe':>9}   verdict")
        for b in (0, 0.25, 0.5, 0.75, 1, 1.5, 2, 3):
            c = 2.0 * b / 10_000.0
            m1 = compute_metrics(equity(on_p - c))
            m2 = compute_metrics(equity(gate_p - c))
            v = ("beats both" if m1['cagr'] > max(bar_same, bar_all)
                 else "beats EW-all only" if m1['cagr'] > bar_all
                 else "beats neither")
            print(f"{b:>8}bp{m1['cagr']:>11.1f}%{m2['cagr']:>9.1f}%"
                  f"{m1['sharpe']:>9.2f}   {v}")

    json.dump(out, open("overnight_study2.json", "w"), indent=2, default=float)
    print("\nwrote overnight_study2.json")


if __name__ == "__main__":
    main()
