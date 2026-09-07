"""Walk-forward harness for the overnight selection rule.

WHY THIS EXISTS. The 26.9% CAGR reported for the overnight selection rule was
measured with the whole sample visible. The 252-day lookback and the position
count were chosen after seeing how they performed. That is the exact error this
repo's concept-drift work was built to catch, and it is the last thing standing
between "a promising backtest" and "a result worth acting on".

WHAT WALK-FORWARD ACTUALLY MEASURES. Train on a window, pick the best
configuration using ONLY that window, apply it to the NEXT window, roll forward,
and staple the out-of-sample segments into one curve. That curve is the honest
estimate. The gap between it and the in-sample optimum IS the overfitting, stated
as a number instead of a worry.

FOUR CONTROLS, because a walk-forward number alone can still fool you:

  1. FIXED PARAMETERS. Run one sensible configuration unchanged throughout. If
     this matches walk-forward, the parameters never mattered and the strategy is
     robust rather than tuned. If walk-forward is much better, the tuning is
     doing real work. If it is much worse, the tuning is noise.

  2. RANDOM SELECTION. Same N names, chosen at random, held overnight. This is
     the floor. If random does nearly as well, the signal adds nothing and the
     result is just the overnight component of whatever was in the basket.

  3. VOLATILITY RANKING. The selected names (MU, AMD, NVDA, F) are high-beta and
     high-volatility. If ranking on trailing volatility reproduces the result,
     then the "overnight signal" is a volatility proxy wearing a disguise and
     there is no new information. This is the same confound test that the
     momentum orthogonalization ran, applied to the other obvious candidate.

  4. SAME BASKET HELD ALL DAY, out of sample. Keeps the "is it the timing or the
     selection" question answered on out-of-sample data too.

HONESTY RULES OBSERVED HERE
  - Out-of-sample segments are contiguous and NON-OVERLAPPING by construction, so
    the count reported is a real independent-period count. This repo already
    learned what pooling overlapping griddings does: it turned p=0.512 into
    p=0.048 in the drift work.
  - Selection masks use only trailing data (the signal is shifted one session),
    so precomputing them over the whole timeline introduces no lookahead. The
    walk-forward applies to WHICH configuration is chosen, which is the thing
    that was actually fitted.
  - Both data filters from the earlier studies stay on: the price/move filter for
    broken delisted prices, and the degeneracy filter for tickers whose Open is
    frequently identical to a Close.

Nothing here trades. main.py DRY_RUN stays True.
"""
import argparse
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
DEGEN_MAX = 0.10                 # drop a ticker whose Open duplicates a Close this often
START = '2007-01-01'
LOOKBACKS = (21, 63, 126, 252)
NAMES = (10, 20, 33, 50, 83)
FIXED = (252, 33)                # the "sensible default" control
CACHE = Path(r"C:\Users\erik9\AppData\Local\Temp\claude\C--Users-erik9"
             r"\fec8c69e-fbe3-42b7-9d5b-9fabc52842d8\scratchpad\pit_ohlc.pkl")


def equity(r):
    return (1.0 + pd.Series(r).astype(float).fillna(0.0)).cumprod()


def load():
    """Point-in-time universe with both data filters applied."""
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
    mask = ok & member

    # Degeneracy filter: a ticker whose Open is repeatedly identical to a Close
    # is a stub series, not a tradeable one. Remove the ticker, not the bar.
    degen = (((opx - prevx).abs() < 1e-9) | ((opx - clx).abs() < 1e-9)) & mask
    rate = degen.sum() / mask.sum().replace(0, np.nan)
    bad = sorted(rate[rate > DEGEN_MAX].dropna().index)
    for t in bad:
        mask[t] = False
    print(f"degeneracy filter removed {len(bad)} tickers: {bad}")

    vol = full.where(mask).rolling(63).std() * np.sqrt(252)
    return cols, on, intr, full, mask, cl[MARKET].reindex(idx).ffill(), vol


def cumret(leg, mask, n):
    return (1 + leg.where(mask).fillna(0.0)).rolling(n).apply(np.prod, raw=True) - 1.0


def make_masks(on, mask, idx, cols, rb, vol):
    """Precompute a selection mask per (lookback, N), plus the control rankings.

    No lookahead: every signal is shifted one session, so a mask at date d used
    only sessions strictly before d.
    """
    sels, sigs = {}, {}
    for lb in LOOKBACKS:
        sigs[lb] = cumret(on, mask, lb).shift(1)
    sigs['vol'] = vol.shift(1)                       # control 3: rank on volatility

    rng = np.random.default_rng(20260907)
    for key, sig in sigs.items():
        for n in NAMES:
            s = pd.DataFrame(False, index=idx, columns=cols)
            for d in rb:
                x = sig.loc[d].where(mask.loc[d]).dropna()
                if len(x) < 50:
                    continue
                s.loc[d, x.sort_values(ascending=False).head(n).index] = True
            sels[(key, n)] = (s.replace(False, np.nan).ffill(limit=31)
                               .fillna(False).astype(bool)) & mask
    # control 2: random selection, same cadence and count
    for n in NAMES:
        s = pd.DataFrame(False, index=idx, columns=cols)
        for d in rb:
            pool = list(mask.loc[d][mask.loc[d]].index)
            if len(pool) < 50:
                continue
            s.loc[d, list(rng.choice(pool, size=min(n, len(pool)), replace=False))] = True
        sels[('random', n)] = (s.replace(False, np.nan).ffill(limit=31)
                                .fillna(False).astype(bool)) & mask
    return sels


def port(leg, sel, min_names=5):
    n = sel.sum(axis=1)
    w = sel.where(sel).astype(float).div(n, axis=0)
    return (leg * w).sum(axis=1)[n >= min_names]


def splits(idx, train_years, test_years):
    """Contiguous, non-overlapping test windows. Returns (train_slice, test_slice)."""
    out = []
    start = idx[0]
    while True:
        tr_end = start + pd.DateOffset(years=train_years)
        te_end = tr_end + pd.DateOffset(years=test_years)
        if tr_end >= idx[-1]:
            break
        tr = (idx >= start) & (idx < tr_end)
        te = (idx >= tr_end) & (idx < min(te_end, idx[-1] + pd.Timedelta(days=1)))
        if te.sum() < 60:
            break
        out.append((tr, te, tr_end))
        start = start + pd.DateOffset(years=test_years)   # rolling origin
        if te_end > idx[-1]:
            break
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--train-years', type=int, default=4)
    ap.add_argument('--test-years', type=int, default=1)
    ap.add_argument('--criterion', choices=['sharpe', 'cagr'], default='sharpe')
    ap.add_argument('--cost-bps', type=float, default=0.0,
                    help='auction slippage per side; commissions are zero')
    args = ap.parse_args()

    cols, on, intr, full, mask, spy, vol = load()
    idx = on.index
    rb = pd.Series(idx, index=idx).groupby(idx.to_period('M')).tail(1).index
    print(f"universe {len(cols)} names, {len(idx):,} sessions "
          f"{idx[0].date()} -> {idx[-1].date()}")
    print("precomputing selection masks (no lookahead: signals are shifted)...")
    sels = make_masks(on, mask, idx, cols, rb, vol)

    grid = [(lb, n) for lb in LOOKBACKS for n in NAMES]
    sp = splits(idx, args.train_years, args.test_years)
    print(f"\n{args.train_years}y train / {args.test_years}y test, rolling origin: "
          f"{len(sp)} non-overlapping out-of-sample windows")
    c = 2.0 * args.cost_bps / 10_000.0

    def ret(key, n, sl, leg=None):
        s = sels[(key, n)].loc[sl]
        return port(on.loc[sl] if leg is None else leg.loc[sl], s)

    # ------------------------------------------------------------ walk forward
    oos, chosen, rows = [], [], []
    oos_fixed, oos_rand, oos_vol, oos_allday, oos_hold, oos_spy = [], [], [], [], [], []
    for tr, te, boundary in sp:
        best, best_score = None, -np.inf
        for lb, n in grid:
            r = ret(lb, n, tr)
            if len(r) < 60:
                continue
            m = compute_metrics(equity(r - c))
            score = m['sharpe'] if args.criterion == 'sharpe' else m['cagr']
            if score > best_score:
                best, best_score = (lb, n), score
        if best is None:
            continue
        lb, n = best
        r_te = ret(lb, n, te)
        m_te = compute_metrics(equity(r_te - c))
        oos.append(r_te - c)
        chosen.append((str(boundary.date()), lb, n))
        oos_fixed.append(ret(FIXED[0], FIXED[1], te) - c)
        oos_rand.append(ret('random', FIXED[1], te) - c)
        oos_vol.append(ret('vol', FIXED[1], te) - c)
        oos_allday.append(ret(lb, n, te, leg=full) - c)
        hold = port(full.loc[te], mask.loc[te], min_names=20)
        oos_hold.append(hold)
        oos_spy.append(spy.loc[te].pct_change())
        rows.append((str(boundary.date()), lb, n, best_score, m_te['cagr'], m_te['sharpe']))

    print(f"\n{'test window from':<18}{'chosen lb':>10}{'N':>5}"
          f"{'  train ' + args.criterion:>15}{'OOS CAGR':>11}{'OOS Sharpe':>12}")
    print("-" * 72)
    for b, lb, n, sc, cg, sh in rows:
        print(f"{b:<18}{lb:>10}{n:>5}{sc:>15.2f}{cg:>10.1f}%{sh:>12.2f}")

    def cat(chunks):
        return equity(pd.concat(chunks).sort_index())

    print(f"\n{'STITCHED OUT-OF-SAMPLE CURVES':<46}{'CAGR':>8}{'Sharpe':>9}{'maxDD':>8}")
    print("-" * 72)
    results = {}
    for lab, chunks in (
            ("walk-forward selected parameters", oos),
            (f"FIXED parameters lb={FIXED[0]} N={FIXED[1]} (control 1)", oos_fixed),
            (f"RANDOM {FIXED[1]} names overnight (control 2, the floor)", oos_rand),
            (f"VOLATILITY-ranked {FIXED[1]} names (control 3, confound)", oos_vol),
            ("same walk-forward basket held ALL DAY (control 4)", oos_allday),
            ("EW hold of all eligible names", oos_hold),
            ("SPY buy and hold", oos_spy)):
        m = compute_metrics(cat(chunks))
        results[lab] = m
        print(f"{lab:<46}{m['cagr']:>7.1f}%{m['sharpe']:>9.2f}{m['max_drawdown']:>7.0f}%")

    # ------------------------------------------------- in-sample vs out-of-sample
    full_sl = np.ones(len(idx), dtype=bool)
    best_is, best_is_score = None, -np.inf
    for lb, n in grid:
        m = compute_metrics(equity(ret(lb, n, full_sl) - c))
        sc = m['sharpe'] if args.criterion == 'sharpe' else m['cagr']
        if sc > best_is_score:
            best_is, best_is_score = (lb, n), sc
    m_is = compute_metrics(equity(ret(best_is[0], best_is[1], full_sl) - c))
    m_oos = results["walk-forward selected parameters"]
    print(f"\nIN-SAMPLE OPTIMUM (whole sample visible, the number I reported before):")
    print(f"   lb={best_is[0]} N={best_is[1]}  ->  {m_is['cagr']:.1f}% CAGR, "
          f"Sharpe {m_is['sharpe']:.2f}, maxDD {m_is['max_drawdown']:.0f}%")
    print(f"WALK-FORWARD OUT-OF-SAMPLE:")
    print(f"   {m_oos['cagr']:.1f}% CAGR, Sharpe {m_oos['sharpe']:.2f}, "
          f"maxDD {m_oos['max_drawdown']:.0f}%")
    print(f"OVERFITTING GAP: {m_is['cagr'] - m_oos['cagr']:+.1f} points of CAGR, "
          f"{m_is['sharpe'] - m_oos['sharpe']:+.2f} of Sharpe")

    cnt = pd.Series([f"lb{lb}/N{n}" for _b, lb, n in chosen]).value_counts()
    print(f"\nPARAMETER STABILITY across {len(chosen)} windows "
          f"(a rule that keeps changing its mind is fitting noise):")
    for k, v in cnt.items():
        print(f"   {k:<14}{v:>3} windows")

    json.dump({'results': results, 'chosen': chosen,
               'in_sample_best': list(best_is), 'in_sample': m_is,
               'settings': vars(args)},
              open("walk_forward_overnight.json", "w"), indent=2, default=float)
    print("\nwrote walk_forward_overnight.json")


if __name__ == "__main__":
    main()
