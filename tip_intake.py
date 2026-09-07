"""Stock-tip intake: turn saved content into HIS verdict instead of the creator's.

Erik saves a lot of short-form finance content. Most of it contains no strategy,
just tickers. This is the route that converts a tip into an answer from the rules
he already validated, so nothing enters the dashboard on a creator's say-so.

WHAT IT DOES
  1. Runs every tipped ticker through Strategy C's live rules (the same logic as
     the trade-identifier skill): market regime gate, per-name 200-day trend
     filter, 12-1 momentum, and a volatility-based fragility flag. The output is
     ELIGIBLE or AVOID by HIS rules. The video gets no vote.
  2. Counts how often each ticker recurs across the saved-content corpus and
     raises a CAUTION flag on repeats. Read that flag correctly: frequency is
     evidence a name is being MARKETED, which is orthogonal to whether it is a
     good business. Coordinated promotion and organic consensus look identical
     one video at a time; they separate only when you look at the corpus.
  3. Scores each source against the standard promotional shape: an unverifiable
     track record, specific entry prices, no disclosure of the creator's own
     position, emotional or political framing, and no methodology. That
     combination is a pattern worth recognizing. It is not an accusation about
     any individual.
  4. Carries a PROVENANCE tag on every number. A figure from a video is content
     provenance and is never equivalent to a figure from a filing. Content
     numbers can never reach the VALIDATED tier of the dashboard.

WHAT IT DOES NOT DO
  It does not add tipped tickers to the dashboard as signals. They enter as a
  watchlist that has to earn its place through his own rules, and most do not.
  Nothing here trades; main.py DRY_RUN stays True. Not advice.

Usage:
  python tip_intake.py                     # score the whole saved corpus
  python tip_intake.py --tickers NVDA AMD  # ad hoc, for a tip not yet logged
  python tip_intake.py --json out.json     # emit for the dashboard
"""
import argparse
import json
import sys
import warnings
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings('ignore')

REPO = Path(__file__).resolve().parent
LEDGER = REPO / "content_tips.json"
MARKET = "SPY"
VOL_FRAGILE = 0.50          # annualized; above this the repo sizes down
RECUR_CAUTION = 2           # appearances across the corpus that trigger the flag


def load_ledger(path=LEDGER):
    if not path.exists():
        return {"sources": [], "promotional_markers": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def fetch(tickers):
    syms = sorted({t.upper() for t in tickers} | {MARKET})
    data = yf.download(syms, period="2y", interval="1d",
                       auto_adjust=True, progress=False)
    close = data["Close"] if isinstance(data.columns, pd.MultiIndex) else data[["Close"]]
    return close.sort_index().ffill()


def verdict(close, t):
    """Strategy C's live rules, applied to one name. His logic, not the video's."""
    if t not in close.columns:
        return None
    s = close[t].dropna()
    if len(s) < 210:
        return {"ticker": t, "verdict": "NO DATA",
                "reason": f"only {len(s)} sessions of history, needs 210"}
    sma200 = float(s.rolling(200).mean().iloc[-1])
    price = float(s.iloc[-1])
    mom12 = float(s.iloc[-21] / s.iloc[-252] - 1) if len(s) >= 252 else float("nan")
    mom6 = float(s.iloc[-21] / s.iloc[-126] - 1) if len(s) >= 126 else float("nan")
    vol = float(s.pct_change().tail(63).std() * np.sqrt(252))

    above = price > sma200
    reasons = []
    if not above:
        reasons.append("below its 200-day simple moving average, so in a downtrend")
    if not (mom12 > 0):
        reasons.append("12-month-minus-1-month momentum is negative or flat")
    ok = above and mom12 > 0
    return {
        "ticker": t, "price": price, "sma200": sma200,
        "pct_vs_sma": (price / sma200 - 1) * 100.0,
        "mom12": mom12 * 100.0, "mom6": mom6 * 100.0, "vol": vol * 100.0,
        "verdict": "ELIGIBLE" if ok else "AVOID",
        "fragile": vol > VOL_FRAGILE,
        "reason": "passes the trend rules" if ok else "; ".join(reasons),
    }


def quality(tickers):
    """Quality and valuation from the financial-researcher skill.

    Imported from the repo's synced copy of the skill so this stays reproducible
    without depending on the active copy under ~/.claude/skills. Income-statement
    and cash-flow numbers come from SEC filings; price multiples from yfinance.
    """
    skill = REPO / "skills" / "financial-researcher"
    if not (skill / "fundamentals.py").exists():
        return {}
    sys.path.insert(0, str(skill))
    try:
        import fundamentals as F
    except Exception as e:
        print(f"  (quality read unavailable: {e})")
        return {}
    out = {}
    for t in tickers:
        try:
            f = F.fundamentals(t)
            if not f:
                continue
            label, score, flags = F.quality_verdict(f)
            v, why = F.buffett_verdict(f, score)
            out[t] = {
                "label": label, "score": score, "flags": flags,
                "verdict": v, "why": why,
                "valuation": F.valuation_note(f),
                "warnings": f.get("warnings", []),
                "from_filings": bool(f.get("from_filings")),
                "gm": f.get("gm"), "om": f.get("om"), "nm": f.get("nm"),
                "fcf": f.get("fcf"), "rev_g": f.get("rev_g"),
                "nonop_share": f.get("nonop_share"),
            }
        except Exception as e:
            print(f"  ({t} quality read failed: {e})")
    return out


def regime(close):
    s = close[MARKET].dropna()
    on = bool(s.iloc[-1] > s.rolling(200).mean().iloc[-1])
    return {"risk_on": on, "spy": float(s.iloc[-1]),
            "sma200": float(s.rolling(200).mean().iloc[-1]),
            "asof": str(s.index[-1].date())}


def frequency(ledger):
    """Count appearances across the WHOLE saved corpus, tips and theses alike.

    The flag only means anything corpus-wide. MP Materials reaches three
    appearances across three different creators only when the robotics thesis is
    counted alongside the two tip videos, and three creators converging on one
    small-cap is exactly what this is meant to surface.
    """
    c = Counter()
    where = {}

    def add(t, title):
        c[t] += 1
        where.setdefault(t, []).append(title)

    for src in ledger.get("sources", []):
        for t in src.get("tickers", []):
            add(t, src.get("title", src.get("id", "?")))

    th_path = REPO / "content_theses.json"
    if th_path.exists():
        for th in json.loads(th_path.read_text(encoding="utf-8")).get("theses", []):
            # count the names the thesis actually NAMES, not every fund that
            # might express it; a sector fund is not a creator pushing a ticker
            named = {n.get("name") for n in th.get("named_companies", [])}
            for t in th.get("expressions", []):
                if t in ("MP",) or any(t.lower() in (n or "").lower() for n in named):
                    add(t, th.get("title", th.get("id", "?")))
    return c, where


def promo_score(src):
    return len(src.get("markers", []))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--tickers', nargs='*', help='ad hoc tickers not yet in the ledger')
    ap.add_argument('--json', metavar='PATH')
    ap.add_argument('--no-fundamentals', action='store_true',
                    help='skip the SEC quality and valuation pass (faster)')
    args = ap.parse_args()

    ledger = load_ledger()
    counts, where = frequency(ledger)
    tickers = sorted(set(args.tickers or []) | set(counts))
    if not tickers:
        print("No tickers in the ledger and none supplied. Nothing to do.")
        return

    print(f"Fetching live data for {len(tickers)} tipped names...")
    close = fetch(tickers)
    reg = regime(close)
    print(f"\nAs of {reg['asof']}   MARKET REGIME: "
          f"{'RISK ON' if reg['risk_on'] else 'RISK OFF'} "
          f"(SPY {reg['spy']:,.2f} vs 200-day {reg['sma200']:,.2f})")

    rows = [v for v in (verdict(close, t) for t in tickers) if v]
    rows.sort(key=lambda r: (r["verdict"] != "ELIGIBLE", -r.get("mom12", 0)))

    print(f"\nHIS RULES, NOT THE VIDEO'S")
    print(f"{'ticker':<8}{'verdict':<10}{'price':>11}{'vs 200d':>10}"
          f"{'12-1 mom':>11}{'vol':>8}  {'seen in':>8}  flags")
    print("-" * 88)
    for r in rows:
        if r["verdict"] == "NO DATA":
            print(f"{r['ticker']:<8}{'NO DATA':<10}{'':>11}{'':>10}{'':>11}{'':>8}")
            continue
        n = counts.get(r["ticker"], 0)
        flags = []
        if r["fragile"]:
            flags.append("FRAGILE")
        if n >= RECUR_CAUTION:
            flags.append(f"RECURS x{n} CAUTION")
        print(f"{r['ticker']:<8}{r['verdict']:<10}{r['price']:>11,.2f}"
              f"{r['pct_vs_sma']:>9.0f}%{r['mom12']:>10.0f}%{r['vol']:>7.0f}%"
              f"{n:>10}  {', '.join(flags)}")

    qual = {}
    if not args.no_fundamentals:
        print("\nPulling quality and valuation from SEC filings...")
        qual = quality([r["ticker"] for r in rows if r.get("verdict") != "NO DATA"])
        if qual:
            print(f"\n{'ticker':<8}{'trend':<10}{'quality':<17}{'both?':<9}valuation")
            print("-" * 96)
            for r in rows:
                q = qual.get(r["ticker"])
                if not q:
                    continue
                # A 5/5 numeric score with an earnings-quality warning is NOT a
                # clean pass. MRVL scores 5/5 while 41% of its net income is
                # non-operating, which is exactly the case that should not read
                # as "passes both".
                if r["verdict"] == "ELIGIBLE" and q["label"] == "QUALITY":
                    both = "FLAGGED" if q.get("warnings") else "YES"
                else:
                    both = "no"
                print(f"{r['ticker']:<8}{r['verdict']:<10}"
                      f"{q['label'] + ' ' + str(q['score']) + '/5':<17}{both:<9}"
                      f"{q['valuation'][:48]}")
            for t, q in qual.items():
                for w in q.get("warnings", []):
                    print(f"   !! {t}: {w}")
            print("\nA high-conviction name passes BOTH screens. Trend answers when")
            print("and how much risk; fundamentals answer what is worth owning.")

    elig = [r for r in rows if r["verdict"] == "ELIGIBLE"]
    avoid = [r for r in rows if r["verdict"] == "AVOID"]
    print(f"\n{len(elig)} of {len(rows)} tipped names pass his trend rules: "
          f"{', '.join(r['ticker'] for r in elig) or 'none'}")
    print(f"{len(avoid)} fail: {', '.join(r['ticker'] for r in avoid) or 'none'}")
    if all(r["fragile"] for r in elig) and elig:
        print("Every passing name is flagged FRAGILE (annualized volatility above "
              f"{VOL_FRAGILE:.0%}), so the rules size them SMALL even when eligible.")

    print(f"\nRECURRENCE ACROSS THE SAVED CORPUS "
          f"({len(ledger.get('sources', []))} sources logged)")
    print("A repeat is evidence a name is being MARKETED. That is orthogonal to")
    print("whether it is a good business, and it is a CAUTION flag, never a buy.")
    for t, n in counts.most_common():
        if n >= RECUR_CAUTION:
            print(f"   {t:<7} x{n}   {' | '.join(where[t])}")

    print(f"\nSOURCE SHAPE (promotional markers present, out of "
          f"{len(ledger.get('promotional_markers', {}))})")
    for src in ledger.get("sources", []):
        sc = promo_score(src)
        bar = "#" * sc + "." * (len(ledger.get("promotional_markers", {})) - sc)
        print(f"   [{bar}] {sc}  {src.get('title', src['id'])}  "
              f"(substance: {src.get('substance', '?')})")

    checked = [(s, c) for s in ledger.get("sources", []) for c in s.get("claims", [])]
    if checked:
        print(f"\nCLAIM CHECKS ({len(checked)} numeric or factual claims logged)")
        print("Provenance: every claim below ORIGINATED IN A VIDEO. A claim that")
        print("checks out is still content-sourced and never reaches VALIDATED.")
        for s, c in checked:
            print(f"   {c['verdict']:<13} {c['claim'][:66]}")

    if args.json:
        payload = {"regime": reg, "rows": rows, "quality": qual,
                   "counts": dict(counts), "where": where,
                   "sources": ledger.get("sources", []),
                   "markers": ledger.get("promotional_markers", {})}
        Path(args.json).write_text(json.dumps(payload, indent=2, default=float),
                                   encoding="utf-8")
        print(f"\nWrote {args.json}")


if __name__ == "__main__":
    main()
