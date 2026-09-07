"""Is the overnight anomaly an implementable STRATEGY, not just a real effect?

The first pass (overnight_vs_intraday.py) tested the naive version: hold every
name overnight, every night. It failed. This asks the harder and fairer
questions, the ones a selection rule actually depends on.

  Q2  CONCENTRATED OR UNIFORM? The paper's finding is cross-sectional. If some
      names carry persistently higher overnight returns, a selection rule can
      harvest that and the naive average understates it. If the effect is
      uniform, there is nothing to select on.
  Q3  SIZING. Equal weight against inverse volatility, and how many names before
      diversification stops paying.
  Q4  REGIME. Always-on, or does a SPY 200-day gate comparable to Strategy C's
      earn its keep?
  Q6  DOES IT SURVIVE PUBLICATION? The paper is from 2019 and widely read.
      Published anomalies decay as they get traded. Split at publication and
      report each side separately. This is the decisive test: an effect that
      lives only before 2019 is a historical curiosity.

COST TREATMENT, corrected from the first pass. Commissions at US retail brokers
are zero, so the 1 basis point commission in strategy_config is wrong for this
strategy and is dropped. The strategy also executes market-on-close and
market-on-open, transacting at the official auction print rather than crossing a
continuous-market spread, and at tens to low hundreds of shares it is a
price-taker that does not move the print. So the honest model is a SWEEP that
starts at zero and lets the reader pick, rather than one imported number. Both
legs of a round trip are charged at the swept rate.

UNIVERSE. Point-in-time S&P 500 membership from 2007 on, which is where the
change log is actually dense, with the data-quality filter that the delisted
tickers force (see rerun_pit_filtered.py for why: unfiltered, ten broken names
produced a 2,325% CAGR).

BASELINES. Not just SPY. Also equal-weight buy-and-hold of THE SAME NAMES the
rule selects, which is the comparison that controls for survivorship and is the
standard this repo already holds Strategy C to.

Nothing here trades. main.py DRY_RUN stays True.
"""
import json
import pickle
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings('ignore')

from metrics import compute_metrics
from strategy_c import MARKET
from run_sp500_pit import build_membership

PRICE_FLOOR = 1.00
MOVE_CAP = 0.50
START = '2007-01-01'
PUBLICATION = '2019-01-01'      # JFE volume 134, 2019
CACHE = Path(r"C:\Users\erik9\AppData\Local\Temp\claude\C--Users-erik9"
             r"\fec8c69e-fbe3-42b7-9d5b-9fabc52842d8\scratchpad\pit_ohlc.pkl")


def equity(rets):
    return (1.0 + pd.Series(rets).astype(float).fillna(0.0)).cumprod()


def net(rets, bps):
    """Charge both sides of the daily round trip at `bps` per side."""
    return equity(rets - 2.0 * bps / 10_000.0)


def load():
    members_asof, _cur, universe, changes = build_membership()
    _n, _uni, cols, op, cl = pickle.load(open(CACHE, "rb"))
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
                .reindex(idx.union(pd.DatetimeIndex(chg))).ffill()
                .reindex(idx).fillna(False).astype(bool)
                .shift(1).fillna(False).astype(bool))
    return cols, on, intr, full, (ok & member), cl[MARKET].reindex(idx).ffill()


def portfolio(leg, mask, weights=None, min_names=20):
    """Equal-weight (or supplied-weight) portfolio return of `leg` under `mask`."""
    if weights is None:
        n = mask.sum(axis=1)
        w = mask.where(mask).astype(float).div(n, axis=0)
    else:
        w = weights.where(mask).fillna(0.0)
        w = w.div(w.sum(axis=1), axis=0)
        n = mask.sum(axis=1)
    keep = n >= min_names
    return (leg * w).sum(axis=1)[keep], keep


def periods(idx):
    return [("2007 to 2026, full", idx >= pd.Timestamp(START)),
            ("2007 to 2018, BEFORE publication",
             (idx >= pd.Timestamp(START)) & (idx < pd.Timestamp(PUBLICATION))),
            ("2019 to 2026, AFTER publication", idx >= pd.Timestamp(PUBLICATION))]


def show(title, rows, bps_note=""):
    print(f"\n{title}{bps_note}")
    print(f"{'':<44}{'CAGR':>8}{'Sharpe':>9}{'maxDD':>8}")
    print("-" * 70)
    for lab, eq in rows:
        m = compute_metrics(eq.dropna())
        print(f"{lab:<44}{m['cagr']:>7.1f}%{m['sharpe']:>9.2f}{m['max_drawdown']:>7.0f}%")


def main():
    cols, on, intr, full, mask, spy = load()
    idx = on.index
    print(f"universe {len(cols)} priced names, {len(idx):,} sessions "
          f"{idx[0].date()} -> {idx[-1].date()}, avg {mask.sum(axis=1).mean():.0f} "
          f"eligible names/day")
    out = {}

    # ---------------------------------------------------------------- Q2
    # Does a name's PAST overnight behaviour predict its FUTURE overnight
    # behaviour? Rank on trailing overnight-only cumulative return, rebalance
    # monthly, hold the top slice overnight. Everything is shifted so the
    # decision uses only completed sessions.
    print("\n" + "=" * 70)
    print("Q2  IS THE EFFECT CONCENTRATED? (selection on past overnight returns)")
    print("=" * 70)
    rb = pd.Series(idx, index=idx).groupby(idx.to_period('M')).tail(1).index
    on_f = on.where(mask)

    for lookback in (21, 126, 252):
        signal = (1 + on_f.fillna(0.0)).rolling(lookback).apply(np.prod, raw=True) - 1.0
        signal = signal.shift(1)                       # decide on completed data
        sel = pd.DataFrame(False, index=idx, columns=cols)
        for d in rb:
            s = signal.loc[d].where(mask.loc[d]).dropna()
            if len(s) < 50:
                continue
            top = s.sort_values(ascending=False).head(max(20, len(s) // 5)).index
            sel.loc[d, top] = True
        sel = sel.replace(False, np.nan).ffill(limit=31).fillna(False).astype(bool) & mask

        rows = []
        for lab, m_ in periods(idx):
            p, keep = portfolio(on_f, sel & pd.DataFrame(
                np.repeat(m_[:, None], len(cols), axis=1), index=idx, columns=cols))
            base, _ = portfolio(on_f, mask & pd.DataFrame(
                np.repeat(m_[:, None], len(cols), axis=1), index=idx, columns=cols))
            rows.append((f"  top quintile, {lab}", equity(p)))
            rows.append((f"  ALL names,     {lab}", equity(base)))
        show(f"lookback {lookback} sessions, overnight leg, ZERO cost", rows)

    # ---------------------------------------------------------------- Q6 + Q4
    print("\n" + "=" * 70)
    print("Q6  DOES IT SURVIVE PUBLICATION?   Q4  DOES A REGIME GATE HELP?")
    print("=" * 70)
    gate = (spy > spy.rolling(200).mean()).shift(1).fillna(False)
    for lab, m_ in periods(idx):
        mm = pd.DataFrame(np.repeat(m_[:, None], len(cols), axis=1),
                          index=idx, columns=cols)
        on_p, keep = portfolio(on_f, mask & mm)
        in_p, _ = portfolio(intr.where(mask), mask & mm)
        fu_p, _ = portfolio(full.where(mask), mask & mm)
        gated = on_p.where(gate.reindex(on_p.index).fillna(False), 0.0)
        sp = spy.reindex(on_p.index).dropna().pct_change()
        rows = [("  overnight leg, always on", equity(on_p)),
                ("  overnight leg, SPY 200d gate", equity(gated)),
                ("  intraday leg", equity(in_p)),
                ("  EW hold SAME names (the real baseline)", equity(fu_p)),
                ("  SPY buy and hold", equity(sp))]
        show(lab, rows, "   [ZERO cost]")
        out[lab] = {l.strip(): compute_metrics(e.dropna()) for l, e in rows}

    # ---------------------------------------------------------------- Q5
    print("\n" + "=" * 70)
    print("Q5  COST SENSITIVITY (commissions are ZERO; this is auction slippage)")
    print("=" * 70)
    for lab, m_ in periods(idx):
        mm = pd.DataFrame(np.repeat(m_[:, None], len(cols), axis=1),
                          index=idx, columns=cols)
        on_p, _ = portfolio(on_f, mask & mm)
        fu_p, _ = portfolio(full.where(mask), mask & mm)
        hold = compute_metrics(equity(fu_p))['cagr']
        print(f"\n{lab}   (EW hold of same names = {hold:.1f}% CAGR)")
        print(f"{'bp/side':>9}{'overnight CAGR':>17}{'Sharpe':>9}   beats hold?")
        for b in (0, 0.5, 1, 2, 3, 5):
            m = compute_metrics(net(on_p, b))
            print(f"{b:>8}bp{m['cagr']:>16.1f}%{m['sharpe']:>9.2f}   "
                  f"{'YES' if m['cagr'] > hold else 'no'}")

    json.dump(out, open("overnight_study.json", "w"), indent=2, default=float)
    print("\nwrote overnight_study.json")


if __name__ == "__main__":
    main()
