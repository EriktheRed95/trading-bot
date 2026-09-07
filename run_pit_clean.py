"""Point-in-time overnight test, with the delisted-data contamination filtered.

WHY A FILTER IS NEEDED. Point-in-time membership deliberately pulls delisted
names back into the universe, and those are exactly the tickers yfinance serves
broken data for. Cooper Industries (CBE) delisted in 2012 yet still prints rows
in 2016 with prev_close = $0.005 against open = $170 on the SAME day, an
implied +3,399,900% overnight return, repeated dozens of times. Ten names in the
priced universe print >500% overnight moves; their median low price is $0.08.
Unfiltered, ten broken tickers out of 682 produced a 2,325% CAGR.

THE FILTER, stated so it can be argued with:
  - both sides of a leg must price at or above $1
  - a single-session leg move beyond +/-50% is dropped for that name-day
An S&P 500 member essentially never moves 50% overnight; the rare genuine cases
(acquisition pops) are worth losing to remove garbage that is thousands of
percent wide. The dropped fraction is reported so the filter's footprint is
visible rather than hidden.
"""
import json
import pickle
import sys
import warnings
from pathlib import Path

warnings.filterwarnings('ignore')
sys.path.insert(0, r"C:\Users\erik9\code\trading-bot")
import numpy as np
import pandas as pd
from metrics import compute_metrics
import overnight_vs_intraday as O
from run_sp500_pit import build_membership

PRICE_FLOOR = 1.00
MOVE_CAP = 0.50

CACHE = Path(__file__).parent / "pit_ohlc.pkl"
_none, universe, cols, op, cl = pickle.load(open(CACHE, "rb"))
members_asof, _cur, _uni, changes = build_membership()
print(f"universe {len(universe)} ever-members, {len(cols)} priced", flush=True)

on, intr = O.decompose(op, cl)
on, intr = on[cols], intr[cols]
prev = cl[cols].shift(1)
full = cl[cols] / prev - 1.0

idx = on.index[on.index >= pd.Timestamp('1990-01-01')]
on, intr, full = on.loc[idx], intr.loc[idx], full.loc[idx]
opx, clx, prevx = op[cols].loc[idx], cl[cols].loc[idx], prev.loc[idx]

# --- the sanity filter -----------------------------------------------------
priced = (prevx >= PRICE_FLOOR) & (opx >= PRICE_FLOOR) & (clx >= PRICE_FLOOR)
sane = (on.abs() <= MOVE_CAP) & (intr.abs() <= MOVE_CAP)
raw_ok = on.notna() & intr.notna()
clean = raw_ok & priced & sane
dropped = int((raw_ok & ~clean).sum().sum())
print(f"filter drops {dropped:,} of {int(raw_ok.sum().sum()):,} name-days "
      f"({dropped/max(1,int(raw_ok.sum().sum()))*100:.3f}%)", flush=True)

# --- point-in-time membership, evaluated only where it changes -------------
chg = sorted({d for d, _a, _r in changes if d >= idx[0]} | {idx[0]})
snap = {d: [t in members_asof(d) for t in cols] for d in chg}
allowed = (pd.DataFrame.from_dict(snap, orient='index', columns=cols)
             .reindex(idx.union(pd.DatetimeIndex(chg))).ffill()
             .reindex(idx).fillna(False).astype(bool).shift(1)
             .fillna(False).astype(bool))
base = clean & allowed

out = {'filter': {'price_floor': PRICE_FLOOR, 'move_cap': MOVE_CAP,
                  'dropped_name_days': dropped}}
for label, start in [("FULL 1990+ (membership sparse before 2007)", None),
                     ("2007+ (membership genuinely point-in-time)", "2007-01-01")]:
    mask = base.copy()
    n = mask.sum(axis=1)
    keep = n >= 20
    if start:
        keep &= (on.index >= pd.Timestamp(start))
    w = mask.where(mask).astype(float).div(n, axis=0)
    on_p, intr_p = (on * w).sum(axis=1)[keep], (intr * w).sum(axis=1)[keep]
    full_p = (full * w).sum(axis=1)[keep]
    spy = cl[O.MARKET].reindex(on_p.index).dropna()

    print(f"\n{'='*74}\n{label}")
    print(f"{on_p.index[0].date()} -> {on_p.index[-1].date()}  "
          f"({len(on_p):,} sessions, avg {n[keep].mean():.0f} names/day)")
    print(f"{'':<40}{'CAGR':>8}{'Sharpe':>9}{'maxDD':>8}")
    print("-" * 66)
    res = []
    for lab, eq in [("Overnight leg  (ZERO costs)", O._equity(on_p)),
                    ("Intraday leg   (ZERO costs)", O._equity(intr_p)),
                    ("EW hold SAME names (ZERO costs)", O._equity(full_p)),
                    ("SPY buy and hold (net)", O.hold_equity(spy)),
                    ("Overnight leg  (net 6bp/side)", O.leg_equity(on_p)),
                    ("Intraday leg   (net 6bp/side)", O.leg_equity(intr_p))]:
        m = compute_metrics(eq)
        print(f"{lab:<40}{m['cagr']:>7.1f}%{m['sharpe']:>9.2f}{m['max_drawdown']:>7.0f}%")
        res.append(dict(label=lab, **m))
    be = None
    print("\n  cost/side   overnight CAGR   overnight Sharpe")
    for b in (0, 0.25, 0.5, 1, 2, 3, 6):
        m = compute_metrics(O.leg_equity(on_p, b / 1e4))
        flag = ''
        if be is None and m['cagr'] <= 0:
            be, flag = b, '   <- negative here'
        print(f"  {b:>7}bp {m['cagr']:>15.1f}% {m['sharpe']:>17.2f}{flag}")
    out[label] = dict(rows=res, breakeven_bps=be, sessions=int(len(on_p)),
                      span=[str(on_p.index[0].date()), str(on_p.index[-1].date())],
                      avg_names=float(n[keep].mean()))

json.dump(out, open(r"C:\Users\erik9\code\trading-bot\overnight_results_pit.json", "w"),
          indent=2, default=float)
print("\nwrote overnight_results_pit.json")
