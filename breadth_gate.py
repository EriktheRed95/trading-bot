"""Does market breadth (RSP/SPY) predict anything, or just describe the past?

THE CLAIM. A video calls the equal-weight S&P 500 divided by the cap-weighted
S&P 500 "the equity market's real golden ratio". Rising means breadth is
broadening (more names participating); falling means the market is narrowing
into megacaps. The chart shown is RSP/SPY daily with 50-day and 200-day simple
moving averages and a 14-day relative strength index, annotated "Dominant
broadening" at troughs and "Countercycle narrowing" at peaks.

WHY THIS ONE IS WORTH TESTING AND THE OTHERS WERE NOT. It is fully
MECHANIZABLE. Two liquid exchange-traded funds, a ratio, and a moving average:
every term is defined, so unlike the session-sweep video there is something to
falsify. Breadth is also a genuine professional measure, not an invention.

WHY IT MATTERS TO THIS REPO SPECIFICALLY. Strategy C already has a regime gate:
hold equities only while SPY is above its own 200-day. A breadth gate is a
direct RIVAL to that gate, so the honest question is not "does breadth look
meaningful" but "does it beat, or add to, the gate already in place". That is
the comparison run here.

THE TRAP THIS FILE IS BUILT TO AVOID. Daily forward returns overlap massively,
so a naive t-test on thousands of days invents significance out of
autocorrelation. This repo already learned that lesson the hard way in the
concept-drift work, where pooling across phase offsets turned p=0.512 into
p=0.048. So the honest sample size here is the number of independent REGIME
EPISODES, not the number of days, and that count is reported beside every result.

Nothing here trades. main.py DRY_RUN stays True. Not advice.
"""
import warnings

import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings('ignore')

from metrics import compute_metrics


def load():
    d = yf.download(["RSP", "SPY"], period="max", interval="1d",
                    auto_adjust=True, progress=False)
    cl = d["Close"].dropna()
    cl.index = pd.to_datetime(cl.index).tz_localize(None)
    return cl.sort_index()


def episodes(state):
    """Count contiguous runs of a boolean state. This is the honest sample size:
    a 400-day regime is ONE observation about that regime, not 400."""
    return int((state != state.shift(1)).sum())


def main():
    cl = load()
    ratio = cl["RSP"] / cl["SPY"]
    spy = cl["SPY"]
    print(f"RSP/SPY from {cl.index[0].date()} to {cl.index[-1].date()}, "
          f"{len(cl):,} sessions")

    fwd = {h: spy.pct_change(h).shift(-h) for h in (21, 63, 252)}

    print("\n" + "=" * 74)
    print("TEST 1  DOES THE BREADTH STATE PREDICT FORWARD SPY RETURNS?")
    print("=" * 74)
    for win in (50, 200):
        sma = ratio.rolling(win).mean()
        broad = (ratio > sma)                       # breadth broadening
        # astype(bool) matters: fillna leaves object dtype, and `~` on object
        # does bitwise negation, silently turning True/False into -2/-1.
        broad = broad.shift(1).fillna(False).astype(bool)
        n_ep = episodes(broad[broad.notna()])
        print(f"\nratio above its own {win}-day average    "
              f"({broad.mean()*100:.0f}% of days, {n_ep} independent episodes)")
        print(f"{'horizon':>9}{'broadening':>14}{'narrowing':>13}{'spread':>10}")
        print("-" * 48)
        for h, f in fwd.items():
            a, b = f[broad].mean(), f[~broad].mean()
            if pd.isna(a) or pd.isna(b):
                continue
            print(f"{h:>7}d{a*100:>13.2f}%{b*100:>12.2f}%{(a-b)*100:>9.2f}%")

    print("\n" + "=" * 74)
    print("TEST 2  DOES A BREADTH GATE BEAT THE GATE STRATEGY C ALREADY USES?")
    print("=" * 74)
    print("Same rule throughout: hold SPY when the gate says risk on, else cash.")
    print("The only thing that changes is what opens the gate.\n")

    r = spy.pct_change().fillna(0.0)
    sma200_spy = spy.rolling(200).mean()
    sma200_ratio = ratio.rolling(200).mean()
    sma50_ratio = ratio.rolling(50).mean()

    gates = {
        "buy and hold (no gate)": pd.Series(True, index=spy.index),
        "SPY vs its 200d (Strategy C's gate)": spy > sma200_spy,
        "BREADTH: ratio vs its 200d": ratio > sma200_ratio,
        "BREADTH: ratio vs its 50d": ratio > sma50_ratio,
        "BOTH: price gate AND breadth gate": (spy > sma200_spy) & (ratio > sma200_ratio),
        "EITHER: price gate OR breadth gate": (spy > sma200_spy) | (ratio > sma200_ratio),
    }
    start = sma200_ratio.first_valid_index()
    print(f"{'gate':<38}{'CAGR':>8}{'Sharpe':>8}{'maxDD':>8}{'% in':>7}")
    print("-" * 69)
    for name, g in gates.items():
        g = g.shift(1).fillna(False).loc[start:]
        eq = (1 + r.loc[start:].where(g, 0.0)).cumprod()
        m = compute_metrics(eq)
        print(f"{name:<38}{m['cagr']:>7.1f}%{m['sharpe']:>8.2f}"
              f"{m['max_drawdown']:>7.0f}%{g.mean()*100:>6.0f}%")

    print("\nRead the spread column in Test 1 and the Sharpe column here, not CAGR.")
    print("A gate that sits out part of the time will almost always show a smaller")
    print("drawdown; that is arithmetic, not skill. The question is whether it keeps")
    print("enough return to be worth the time out of the market.")


if __name__ == "__main__":
    main()
