"""Thesis intake: the step the tip path does not have.

A TIP hands you a ticker, and tip_intake.py answers it with Erik's rules. A
THESIS hands you a structural claim that implies a basket, and the missing work
is figuring out what actually expresses it. That is this file.

THE FIVE STEPS
  1. State the structural claim and what would have to be true for it to hold.
  2. Verify the load-bearing number. A thesis resting on one statistic is only
     as good as that statistic, which the water video demonstrated by resting on
     a figure its own author has since retracted.
  3. Enumerate the investable expressions, which the videos skip entirely. That
     means the named companies AND the sector funds nobody mentions, checked for
     what they actually hold rather than assumed to express the argument.
  4. Run the TRADABILITY GATE below.
  5. Only then run the survivors through the same screens tips get.

THE TRADABILITY GATE, which is the reusable part.
  A signal computed on a price series that is not really trading is not a
  signal. Before any screen runs, each candidate is measured on:
    - median dollar volume per day
    - quoted bid-ask spread
    - the fraction of sessions with a ZERO price change, meaning a stale print
    - where a liquid home listing exists, the correlation between the two
  Harmonic Drive is the worked example. Its United States over-the-counter
  receipt HSYDF shows 180 stale sessions out of 501, a 1.8% quoted spread, about
  $100k of volume a day, and a daily return correlation of 0.28 against its own
  Tokyo listing. Same company, same economics. The trend screen calls it
  ELIGIBLE with 861% momentum; the Tokyo line says 162% over the same window.
  The verdict is an artifact of stale prints catching up, not a read on the
  business.
  This is the same failure class as the degenerate-Open tickers found in the
  overnight work, where SW printed Open equal to Close on 80% of bars. Different
  dataset, identical lesson: check that the series is real before trusting a
  number computed from it.

Everything here is UNTESTED HYPOTHESIS under the dashboard's tiering. A
structural argument beats a tip and is still not a backtest. Nothing trades;
main.py DRY_RUN stays True. Not advice.

Usage:
  python thesis_intake.py                 # score the logged theses
  python thesis_intake.py --json out.json # emit for the dashboard
"""
import argparse
import json
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings('ignore')

REPO = Path(__file__).resolve().parent
LEDGER = REPO / "content_theses.json"

# Tradability thresholds. Stated so they can be argued with rather than buried.
MIN_DOLLAR_VOL = 1_000_000     # median dollars traded per day
MAX_SPREAD = 0.005             # quoted bid-ask, 50 basis points
MAX_STALE = 0.10               # fraction of sessions with zero price change

# Where a thin listing has a liquid home line, name it so the two can be compared.
HOME_LISTING = {"HSYDF": "6324.T"}


CANARIES = ("SPY", "AAPL")          # if these look wrong, the quote feed is wrong


def quotes_usable():
    """Is the bid-ask feed live enough to judge anyone by?

    Quoted spreads from a free feed are garbage while the market is closed. On
    2026-09-07, a market holiday, AAPL quoted a 5.16% spread. Judging an ordinary
    exchange-traded fund unreachable on that basis would be a false negative
    manufactured by the data, so the gate checks its own instrument first: if a
    canary that is known to trade at a hair-thin spread does not show one, the
    spread test is disabled and the verdict rests on the measures that do not
    depend on a live quote.
    """
    worst, detail = 0.0, []
    for c in CANARIES:
        try:
            i = yf.Ticker(c).info or {}
        except Exception:
            return False, "quote lookup failed"
        b, a = i.get("bid"), i.get("ask")
        if not (b and a and a > 0):
            return False, f"{c} has no two-sided quote"
        s = (a - b) / ((a + b) / 2)
        detail.append(f"{c} {s:.2%}")
        worst = max(worst, s)
    return worst <= 0.005, ", ".join(detail)


def tradability(tickers):
    """Is this series real enough to compute a signal on?"""
    spread_ok, canary = quotes_usable()
    print(f"quote-feed canary: {canary} -> spread test "
          f"{'ENABLED' if spread_ok else 'DISABLED (feed is stale, likely a closed market)'}")
    syms = sorted(set(tickers) | set(HOME_LISTING.values()))
    d = yf.download(syms, period="2y", interval="1d", auto_adjust=True, progress=False)
    cl, vol = d["Close"], d["Volume"]
    out = {}
    for t in tickers:
        if t not in cl.columns:
            out[t] = {"ticker": t, "status": "NO DATA"}
            continue
        s = cl[t].dropna()
        v = vol[t].reindex(s.index).fillna(0)
        r = s.pct_change().dropna()
        stale = float((r.abs() < 1e-12).mean()) if len(r) else 1.0
        dv = float((s * v).replace(0, np.nan).median()) if len(s) else 0.0
        info = {}
        try:
            info = yf.Ticker(t).info or {}
        except Exception:
            pass
        bid, ask = info.get("bid"), info.get("ask")
        spread = ((ask - bid) / ((ask + bid) / 2)) if (bid and ask and ask > 0) else None

        is_etf = str(info.get("quoteType", "")).upper() == "ETF"
        rec = {"ticker": t, "dollar_vol": dv, "spread": spread, "stale_frac": stale,
               "n_obs": int(len(s)), "exchange": info.get("exchange"),
               "home_corr": None, "home": HOME_LISTING.get(t),
               "is_etf": is_etf, "spread_measurable": spread_ok}

        home = HOME_LISTING.get(t)
        if home and home in cl.columns:
            both = cl[[t, home]].dropna()
            if len(both) > 60:
                c = both.pct_change().dropna()
                rec["home_corr"] = float(c[t].corr(c[home]))
                if len(both) >= 252:
                    rec["mom_self"] = float(both[t].iloc[-21] / both[t].iloc[-252] - 1)
                    rec["mom_home"] = float(both[home].iloc[-21] / both[home].iloc[-252] - 1)

        fails, notes = [], []
        # An exchange-traded fund's tradability is NOT bounded by its own screen
        # volume: authorised participants create and redeem against the
        # underlying basket, so an ordinary fund holding liquid names is far more
        # reachable than a stock trading the same dollars. Report the volume,
        # do not fail a fund on it.
        if dv < MIN_DOLLAR_VOL:
            if is_etf:
                notes.append(f"${dv/1e6:.2f}M median daily volume, but this is a "
                             f"fund: creation and redemption against the basket "
                             f"means screen volume understates what is reachable")
            else:
                fails.append(f"thin: ${dv/1e6:.2f}M median daily volume, "
                             f"under the ${MIN_DOLLAR_VOL/1e6:.0f}M floor")
        if spread is not None and spread > MAX_SPREAD:
            if spread_ok:
                fails.append(f"wide: {spread:.2%} quoted spread, so a round trip "
                             f"costs about {2*spread:.1%}")
            else:
                notes.append(f"{spread:.2%} quoted spread NOT COUNTED: the feed "
                             f"failed its own canary, so this number is not real")
        if stale > MAX_STALE:
            fails.append(f"stale: {stale:.0%} of sessions print zero change")
        if rec["home_corr"] is not None and rec["home_corr"] < 0.60:
            fails.append(f"not tracking: {rec['home_corr']:.2f} daily correlation "
                         f"with its own {home} listing")
        rec["fails"] = fails
        rec["notes"] = notes
        rec["status"] = "TRADEABLE" if not fails else "NOT TRADEABLE AT SIZE"
        out[t] = rec
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--json', metavar='PATH')
    args = ap.parse_args()

    if not LEDGER.exists():
        print(f"No thesis ledger at {LEDGER}")
        return
    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    theses = ledger.get("theses", [])
    every = sorted({t for th in theses for t in th.get("expressions", [])})

    print(f"THESIS INTAKE: {len(theses)} logged, {len(every)} candidate expressions")
    print("Every thesis below is UNTESTED HYPOTHESIS. A structural argument beats")
    print("a tip and is still not a backtest.\n")

    for th in theses:
        print("=" * 78)
        print(th["title"])
        print("=" * 78)
        print(f"CLAIM: {th['structural_claim']}\n")
        print("What would have to be true:")
        for w in th.get("what_must_be_true", []):
            print(f"   - {w}")
        lb = th.get("load_bearing_claim") or {}
        if lb:
            print(f"\nLOAD-BEARING NUMBER: {lb['claim']}")
            print(f"   VERDICT: {lb['verdict']}")
            print(f"   checked against: {lb['checked_against']}")
        for c in th.get("named_companies", []):
            acc = ("US-tradeable" if c["tradeable_us"] is True
                   else "nominal US access" if c["tradeable_us"] == "nominal"
                   else "NOT reachable from a US brokerage")
            print(f"\n   {c['name']} ({c['role']}): {acc}")
            print(f"      {c['us_access']}")
        print()

    print("=" * 78)
    print("TRADABILITY GATE (run BEFORE any screen, because a signal computed on")
    print("a series that is not really trading is not a signal)")
    print("=" * 78)
    tr = tradability(every)
    print(f"{'ticker':<8}{'status':<24}{'$vol/day':>11}{'spread':>9}{'stale':>8}{'home corr':>11}")
    print("-" * 72)
    for t in every:
        r = tr[t]
        if r.get("status") == "NO DATA":
            print(f"{t:<8}{'NO DATA':<24}")
            continue
        sp = f"{r['spread']:.2%}" if r.get("spread") is not None else "n/a"
        hc = f"{r['home_corr']:.2f}" if r.get("home_corr") is not None else "-"
        print(f"{t:<8}{r['status']:<24}${r['dollar_vol']/1e6:>9.2f}M{sp:>9}"
              f"{r['stale_frac']:>7.0%}{hc:>11}")
    for t in every:
        for f in tr[t].get("fails", []):
            print(f"   !! {t}: {f}")
        for n in tr[t].get("notes", []):
            print(f"   .. {t}: {n}")
        r = tr[t]
        if r.get("mom_self") is not None:
            print(f"   !! {t}: 12-1 momentum reads {r['mom_self']:+.0%} on this line "
                  f"but {r['mom_home']:+.0%} on {r['home']} over the same window. "
                  f"Same company. The screen would be reading stale prints.")

    ok = [t for t in every if tr[t].get("status") == "TRADEABLE"]
    bad = [t for t in every if tr[t].get("status") not in ("TRADEABLE", "NO DATA")]
    print(f"\nPASS the gate: {', '.join(ok) or 'none'}")
    print(f"FAIL the gate: {', '.join(bad) or 'none'}")
    print("\nOnly the passing set is worth running through the trend and quality")
    print("screens. Running a screen on a failing series produces a confident")
    print("verdict built on noise, which is worse than no verdict.")

    if args.json:
        Path(args.json).write_text(json.dumps(
            {"theses": theses, "tradability": tr, "pass": ok, "fail": bad,
             "thresholds": {"min_dollar_vol": MIN_DOLLAR_VOL,
                            "max_spread": MAX_SPREAD, "max_stale": MAX_STALE}},
            indent=2, default=float), encoding="utf-8")
        print(f"\nWrote {args.json}")


if __name__ == "__main__":
    main()
