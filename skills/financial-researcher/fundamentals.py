"""Fundamental researcher - a Buffett-style snapshot, built to do the heavy lifting
in the script so Claude spends few tokens.

What changed (token-efficiency rewrite):
  - BATCH: `python fundamentals.py T1 T2 T3 ...` processes the whole basket in ONE
    process, so Claude synthesizes all names in a single pass instead of one
    agent per ticker (the boot cost was being paid N times).
  - CLEAN NUMBERS: margins/returns/leverage/valuation come from yfinance (same
    source + logic as the trade screen's fundamentals.py), NOT hand-rolled XBRL.
    The old XBRL math mistagged fiscal years and printed >100% margins, forcing
    Claude to reconcile against out-of-band "known-good" values. Gone.
  - PROVISIONAL VERDICT: the script pre-computes a Buffett verdict bucket so Claude
    only refines the prose, not derives the call.
  - 10-K URL still from SEC EDGAR; add --filing to also fetch a compact narrative
    digest inline (business / customer concentration / risks), so one call yields
    everything needed for the memo.
  - The metric legend no longer prints every run; add --legend to show it.

Usage:
  python fundamentals.py NVDA RKLB PL            # numbers + provisional verdict + 10-K URL
  python fundamentals.py NVDA RKLB --filing      # also fetch the 10-K narrative digest
  python fundamentals.py NVDA --legend           # print the metric legend too

NOT financial advice - a discipline check. Pair with trade-identifier (when/how much).
"""
import os
import sys

import numpy as np
import requests

# SEC requires a "Name email" User-Agent; punctuation/parentheses can trip its WAF.
UA = {"User-Agent": "EriktheRed95 erik9@gmail.com", "Accept-Encoding": "gzip, deflate"}
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"

_CIK_CACHE = {}


# ----------------------------------------------------------------------------
# Clean fundamentals from yfinance (same fields + handling as the trade screen)
# ----------------------------------------------------------------------------
def _get(info, *keys):
    for k in keys:
        v = info.get(k)
        if v is not None and not (isinstance(v, float) and np.isnan(v)):
            return v
    return None


def fundamentals(t):
    import yfinance as yf
    tk = yf.Ticker(t)
    try:
        info = tk.info or {}
    except Exception:
        info = {}
    if not info or (_get(info, "marketCap") is None and _get(info, "trailingPE") is None
                    and _get(info, "totalRevenue") is None):
        return None
    d2e = _get(info, "debtToEquity")  # yfinance reports as a percentage (50 = 0.5x)
    out = {
        "name": _get(info, "shortName", "longName") or t,
        "mktcap": _get(info, "marketCap"),
        "pe": _get(info, "trailingPE"),
        "fpe": _get(info, "forwardPE"),
        "ps": _get(info, "priceToSalesTrailing12Months"),
        "ev_ebitda": _get(info, "enterpriseToEbitda"),
        "peg": _get(info, "pegRatio", "trailingPegRatio"),
        "gm": _get(info, "grossMargins"),
        "om": _get(info, "operatingMargins"),
        "nm": _get(info, "profitMargins"),
        "roe": _get(info, "returnOnEquity"),
        "roa": _get(info, "returnOnAssets"),
        "rev_g": _get(info, "revenueGrowth"),
        "d2e": (d2e / 100.0) if d2e is not None else None,
        "current": _get(info, "currentRatio"),
        "fcf": _get(info, "freeCashflow"),
        "rev": _get(info, "totalRevenue"),
        "warnings": [],
        "from_filings": False,
        "sources": {},
        "nonop_share": None,
    }

    # Income-statement and cash-flow numbers come from the FILINGS, not yfinance.
    # yfinance printed an 80% operating margin against a 73% gross margin for MU,
    # and -$0.89B of free cash flow for AAOI against about -$0.34B in the filings.
    # Price-based multiples stay with yfinance: XBRL has no price.
    try:
        import xbrl_fundamentals as xf
        x = xf.pull(t)
    except Exception as e:
        x = None
        out["warnings"].append(f"filing pull failed, falling back to yfinance: {e}")
    if x:
        out["from_filings"] = True
        out["sources"] = x.get("source", {})
        out["warnings"] += x.get("warnings", [])
        out["nonop_share"] = x.get("nonop_share")
        for src, dst in (("gross_margin", "gm"), ("operating_margin", "om"),
                         ("net_margin", "nm"), ("fcf", "fcf"),
                         ("rev_growth_ttm", "rev_g"), ("roe", "roe"),
                         ("revenue", "rev")):
            if x.get(src) is not None:
                out[dst] = x[src]
    return out


def quality_verdict(f):
    """Score balance-sheet + profitability health (0-5). Same rules as the screen."""
    score, flags = 0, []
    nm, roe, fcf, rev_g, d2e, current = (
        f["nm"], f["roe"], f["fcf"], f["rev_g"], f["d2e"], f["current"])
    if nm is not None:
        if nm > 0.10:
            score += 1
        elif nm <= 0:
            flags.append("unprofitable (negative net margin)")
    if roe is not None and roe > 0.15:
        score += 1
    if fcf is not None:
        if fcf > 0:
            score += 1
        else:
            flags.append("burning cash (negative FCF)")
    if rev_g is not None:
        if rev_g > 0.05:
            score += 1
        elif rev_g < 0:
            flags.append("revenue shrinking")
    if d2e is not None:
        if d2e < 1.0:
            score += 1
        elif d2e > 2.0:
            flags.append(f"high leverage (D/E {d2e:.1f}x)")
    if current is not None and current < 1.0:
        flags.append(f"weak liquidity (current ratio {current:.2f})")
    if score >= 4 and not any("unprofit" in x or "burning" in x for x in flags):
        label = "QUALITY"
    elif (nm is not None and nm <= 0) or (fcf is not None and fcf < 0):
        label = "SPECULATIVE"
    else:
        label = "MIXED"
    return label, score, flags


def fcf_yield(f):
    return (f["fcf"] / f["mktcap"]) if (f["fcf"] and f["mktcap"]) else None


def valuation_note(f):
    notes = []
    if f["pe"] is not None:
        notes.append(f"P/E {f['pe']:.1f}")
    elif f["nm"] is not None and f["nm"] <= 0:
        notes.append("no P/E (unprofitable)")
    if f["fpe"] is not None:
        notes.append(f"fwd P/E {f['fpe']:.1f}")
    if f["ev_ebitda"] is not None:
        notes.append(f"EV/EBITDA {f['ev_ebitda']:.1f}")
    if f["peg"] is not None:
        notes.append(f"PEG {f['peg']:.2f}")
    fy = fcf_yield(f)
    if fy is not None:
        notes.append(f"FCF yield {fy:+.1%}")
    return ", ".join(notes) if notes else "limited valuation data"


def buffett_verdict(f, score):
    """See below. Note the non-operating adjustment added 2026-09-07."""
    """Pre-compute a PROVISIONAL Buffett bucket from the numbers alone.
    Claude refines this with the 10-K narrative - it is a starting point, not final."""
    nm, fcf, d2e, rev_g, current = f["nm"], f["fcf"], f["d2e"], f["rev_g"], f["current"]
    peg, fpe = f["peg"], f["fpe"]
    # Cheap on growth-adjusted terms overrides a low FCF yield (natural for fast
    # growers); only call it rich when it is NOT cheap and the multiple is steep.
    cheap = (peg is not None and 0 < peg < 1.2) or (fpe is not None and 0 < fpe < 18)
    # A price/earnings multiple built on earnings that are largely NOT from
    # operations is flattered: the denominator is not repeatable. Marvell showed
    # 41% of net income sitting above operating income. Refuse to call that cheap.
    nonop = f.get("nonop_share")
    if nonop is not None and nonop > 0.25:
        cheap = False
    rich = (not cheap) and ((peg is not None and peg > 2.0)
                            or (fpe is not None and fpe > 30))
    # "Pre-profit" means it does not actually earn money (negative net margin).
    # A profitable business with negative FCF is a cash/capex flag, not pre-profit.
    if nm is not None and nm < 0:
        distress = sum(bool(x) for x in (
            fcf is not None and fcf < 0,
            d2e is not None and d2e > 3.0,
            current is not None and current < 0.7,
            rev_g is not None and rev_g < 0,
        ))
        if distress >= 3:
            return "avoid", "unprofitable + leverage/liquidity/shrinking distress"
        return "pre-profit speculation", "negative net margin - no earnings to value"
    burning = fcf is not None and fcf < 0
    if nonop is not None and nonop > 0.25:
        return ("earnings quality flag",
                f"{nonop:.0%} of net income is non-operating, so the P/E is "
                f"flattered - judge it on operating earnings, not reported ones")
    if score >= 4 and not burning:
        return ("quality but pricey", "strong business, demanding multiple") if rich \
            else ("wonderful business at a fair price", "quality at a reasonable price")
    if rich:
        return "quality but pricey", "full price" + (", and burning cash" if burning else " for middling quality")
    return "mediocre", "cash-burning despite profits" if burning else "profitable but unremarkable economics"


# ----------------------------------------------------------------------------
# SEC EDGAR: ticker -> CIK -> latest 10-K URL
# ----------------------------------------------------------------------------
def _get_json(url):
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    return r.json()


def ticker_to_cik(ticker):
    if not _CIK_CACHE:
        try:
            for row in _get_json(TICKERS_URL).values():
                _CIK_CACHE[row["ticker"].upper()] = (str(row["cik_str"]).zfill(10), row["title"])
        except Exception:
            return None, None
    return _CIK_CACHE.get(ticker.upper(), (None, None))


def latest_10k(cik):
    try:
        rec = _get_json(SUBS_URL.format(cik=cik)).get("filings", {}).get("recent", {})
    except Exception:
        return None, None
    for i, form in enumerate(rec.get("form", [])):
        if form in ("10-K", "20-F"):
            acc = rec["accessionNumber"][i].replace("-", "")
            doc = rec["primaryDocument"][i]
            url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{doc}"
            return url, rec["filingDate"][i]
    return None, None


# ----------------------------------------------------------------------------
# Formatting
# ----------------------------------------------------------------------------
def fmt_pct(x):
    return f"{x:+.0%}" if x is not None else "n/a"


def show_legend():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "METRICS_LEGEND.md")
    try:
        txt = open(p, encoding="utf-8").read()
        body = txt[txt.index("## Part B"):].strip()
    except Exception:
        return
    print("\n" + "=" * 64)
    print("HOW TO READ THIS - metric legend (want vs avoid):\n")
    print(body)


def report(ticker, want_filing):
    print(f"\n{ticker}")
    f = fundamentals(ticker)
    if f is None:
        print("   NO DATA - ticker not found or no fundamentals available")
        return
    print(f"   {f['name']}")
    label, score, flags = quality_verdict(f)
    src = "SEC filings" if f.get("from_filings") else "yfinance (FILINGS UNAVAILABLE)"
    print(f"   margins [{src}]: gross {fmt_pct(f['gm'])} | op {fmt_pct(f['om'])} | net {fmt_pct(f['nm'])}")
    print(f"   returns: ROE {fmt_pct(f['roe'])} | ROA {fmt_pct(f['roa'])} | rev growth {fmt_pct(f['rev_g'])}")
    bs = []
    if f["d2e"] is not None:
        bs.append(f"D/E {f['d2e']:.2f}x")
    if f["current"] is not None:
        bs.append(f"current ratio {f['current']:.2f}")
    if f["fcf"] is not None:
        bs.append(f"FCF ${f['fcf']/1e9:+.2f}B")
    if bs:
        print(f"   balance/cash: {' | '.join(bs)}")
    for w in f.get("warnings", []):
        print(f"   !! {w}")
    print(f"   QUALITY: {label} (score {score}/5)" + (f"  flags: {'; '.join(flags)}" if flags else ""))
    print(f"   VALUATION: {valuation_note(f)}")
    verdict, why = buffett_verdict(f, score)
    print(f"   PROVISIONAL VERDICT: {verdict}  ({why})  <- refine with the 10-K narrative")

    cik, _name = ticker_to_cik(ticker)
    if not cik:
        print("   10-K: no SEC EDGAR filer match (foreign/OTC line) - narrative is web-only")
        return
    url, filed = latest_10k(cik)
    if not url:
        print("   10-K: not found in recent EDGAR filings")
        return
    print(f"   10-K: {url}  (filed {filed})")
    if want_filing:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        try:
            import read_filing
            print("   --- 10-K NARRATIVE DIGEST ---")
            for line in read_filing.digest(url, company=f["name"]).splitlines():
                print("   " + line)
        except Exception as e:
            print(f"   (filing digest unavailable: {e})")


def main(argv):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    want_filing = "--filing" in argv
    want_legend = "--legend" in argv
    tickers = [a.upper() for a in argv if not a.startswith("--")]
    if not tickers:
        print("usage: python fundamentals.py TICKER [TICKER ...] [--filing] [--legend]")
        sys.exit(1)
    print("FUNDAMENTAL RESEARCHER - Buffett snapshot (clean numbers, batch). Not advice.")
    print("Provisional verdict is from the numbers; refine with the 10-K narrative.")
    print("=" * 70)
    for t in tickers:
        try:
            report(t, want_filing)
        except Exception as e:
            print(f"\n{t}\n   ERROR: {e}")
    if want_legend:
        show_legend()


def _archive(text, subfolder, label):
    """Save a copy of the run to C:\\Users\\erik9\\CoworkOS\\<subfolder>\\."""
    import datetime
    base = os.path.join(r"C:\Users\erik9\CoworkOS", subfolder)
    os.makedirs(base, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y-%m-%d-%H%M%S")
    path = os.path.join(base, f"{label}-{ts}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


if __name__ == "__main__":
    import io
    import contextlib
    _buf = io.StringIO()
    with contextlib.redirect_stdout(_buf):
        main(sys.argv[1:])
    _text = _buf.getvalue()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    try:
        print(_text, end="")
    except UnicodeEncodeError:
        sys.stdout.buffer.write(_text.encode("utf-8", "replace"))
    try:
        _label = "-".join(a.upper() for a in sys.argv[1:] if not a.startswith("--"))[:40] or "research"
        _p = _archive(_text, "Research", _label)
        print(f"\n[snapshot saved to {_p}]")
    except Exception as _e:
        print(f"\n[could not save archive: {_e}]")
