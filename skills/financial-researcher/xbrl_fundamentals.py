"""Income-statement and cash-flow fundamentals straight from SEC XBRL.

WHY THIS EXISTS. fundamentals.py sourced margins and free cash flow from
yfinance, and those fields are unreliable in ways that are not obvious until you
check them against a filing. Two failures found on 2026-09-07:

  - MU printed an operating margin of 80% against a gross margin of 73%.
    Operating margin cannot exceed gross margin. The filings say 65.4%.
  - AAOI printed free cash flow of -$0.89B on $0.56B of revenue. The filings
    support about -$354M for fiscal 2025.

The skill had already moved OFF hand-rolled XBRL once, because that version
mistagged fiscal years and printed margins above 100%. The fix is not to pick a
side, it is to compute the periods correctly and then ASSERT the relationships
that must hold. A number that fails an accounting identity is a bug, not a datum,
and the caller should be told rather than handed it.

WHAT IT DOES
  - Builds a genuine trailing-twelve-month figure for each flow metric, handling
    both reporting styles: companies that tag discrete quarters, and companies
    that tag cumulative year-to-date periods (AAOI does the latter, which is
    exactly what made the naive four-most-recent-quarters sum wrong).
  - Computes free cash flow as operating cash flow minus capital expenditure,
    both from the filings.
  - Runs sanity checks and returns the failures rather than swallowing them.

Price-based multiples (price/earnings, enterprise value to EBITDA, PEG, market
capitalisation) stay with yfinance. Those need a live price, XBRL has no price,
and they were not the broken part.
"""
from datetime import date

import requests

UA = {"User-Agent": "EriktheRed95 erik9@gmail.com", "Accept-Encoding": "gzip, deflate"}
FACTS = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

# Several tags mean the same line across filers and eras. First hit wins.
TAGS = {
    "revenue": ["RevenueFromContractWithCustomerExcludingAssessedTax",
                "RevenueFromContractWithCustomerIncludingAssessedTax",
                "Revenues", "SalesRevenueNet"],
    "gross_profit": ["GrossProfit"],
    "operating_income": ["OperatingIncomeLoss"],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "nonoperating": ["NonoperatingIncomeExpense", "OtherNonoperatingIncomeExpense"],
    "ocf": ["NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment",
              "PaymentsToAcquireProductiveAssets"],
    "equity": ["StockholdersEquity",
               "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
}


def _days(a, b):
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def _periods(facts, names):
    """Deduplicated duration facts for the first tag that has any data."""
    for tag in names:
        node = facts.get("facts", {}).get("us-gaap", {}).get(tag)
        if not node:
            continue
        rows = {}
        for unit, items in node.get("units", {}).items():
            if unit != "USD":
                continue
            for it in items:
                if not it.get("start") or not it.get("end"):
                    continue
                key = (it["start"], it["end"])
                # A 10-K restates the same period a 10-Q reported; prefer the 10-K.
                prev = rows.get(key)
                if prev is None or (prev["form"].startswith("10-Q")
                                    and it.get("form", "").startswith("10-K")):
                    rows[key] = {"start": it["start"], "end": it["end"],
                                 "val": it["val"], "form": it.get("form", ""),
                                 "days": _days(it["start"], it["end"])}
        if rows:
            return sorted(rows.values(), key=lambda p: (p["end"], p["days"])), tag
    return [], None


def _chain(quarters, end, n=4):
    """n consecutive quarters ending at `end`, or None if they do not chain."""
    by_end = {}
    for q in quarters:
        by_end.setdefault(q["end"], []).append(q)
    out, cursor = [], end
    for _ in range(n):
        cand = by_end.get(cursor)
        if not cand:
            return None
        q = cand[0]
        out.append(q)
        # the previous quarter should end within a few days before this start
        prev_end = None
        for e in sorted(by_end, reverse=True):
            if 0 <= _days(e, q["start"]) <= 6:
                prev_end = e
                break
        cursor = prev_end
        if cursor is None:
            break
    return out if len(out) == n else None


def ttm(periods, asof=None):
    """Trailing twelve months, handling discrete-quarter and cumulative filers.

    Returns (value, method, end_date). method explains how it was derived so the
    caller can show its work instead of asserting a number.
    """
    if not periods:
        return None, None, None
    ps = [p for p in periods if asof is None or p["end"] <= asof]
    if not ps:
        return None, None, None
    end = max(p["end"] for p in ps)

    ann = [p for p in ps if 350 <= p["days"] <= 380 and p["end"] == end]
    if ann:
        return ann[0]["val"], "annual period", end

    q = [p for p in ps if 80 <= p["days"] <= 100]
    chained = _chain(q, end)
    if chained:
        return sum(p["val"] for p in chained), "four consecutive quarters", end

    # Cumulative filer: TTM = last full year + year-to-date now - year-to-date then.
    cur = max([p for p in ps if p["end"] == end], key=lambda p: p["days"])
    fy = [p for p in ps if 350 <= p["days"] <= 380 and p["end"] < cur["end"]]
    same = [p for p in ps if abs(p["days"] - cur["days"]) <= 6 and p["end"] < cur["end"]]
    if fy and same and cur["days"] < 350:
        f = max(fy, key=lambda p: p["end"])
        pr = max(same, key=lambda p: p["end"])
        return f["val"] + cur["val"] - pr["val"], "annual plus year-to-date rollforward", end
    if fy:
        f = max(fy, key=lambda p: p["end"])
        return f["val"], "most recent full year", f["end"]
    return None, None, None


def _cik(ticker):
    r = requests.get("https://www.sec.gov/files/company_tickers.json",
                     headers=UA, timeout=30)
    r.raise_for_status()
    for row in r.json().values():
        if row["ticker"].upper() == ticker.upper():
            return str(row["cik_str"]).zfill(10)
    return None


def pull(ticker):
    """Return XBRL-derived fundamentals plus any sanity failures."""
    cik = _cik(ticker)
    if not cik:
        return None
    r = requests.get(FACTS.format(cik=cik), headers=UA, timeout=60)
    if r.status_code != 200:
        return None
    facts = r.json()

    out = {"cik": cik, "source": {}, "warnings": []}
    series = {}
    for key, names in TAGS.items():
        ps, tag = _periods(facts, names)
        series[key] = ps
        val, how, end = ttm(ps)
        out[key] = val
        if val is not None:
            out["source"][key] = f"{tag}, {how}, through {end}"

    # prior-year TTM for an honest growth rate (not a single volatile quarter)
    rev_end = None
    if series["revenue"]:
        _v, _h, rev_end = ttm(series["revenue"])
    if rev_end:
        cutoff = date.fromisoformat(rev_end).replace(
            year=date.fromisoformat(rev_end).year - 1).isoformat()
        prior, how, _e = ttm(series["revenue"], asof=cutoff)
        if prior and out.get("revenue"):
            out["rev_growth_ttm"] = out["revenue"] / prior - 1.0
            out["source"]["rev_growth_ttm"] = f"trailing twelve months over the prior twelve, through {rev_end}"

    rev = out.get("revenue")
    if rev:
        for k, m in (("gross_profit", "gross_margin"),
                     ("operating_income", "operating_margin"),
                     ("net_income", "net_margin")):
            if out.get(k) is not None:
                out[m] = out[k] / rev

    if out.get("ocf") is not None:
        out["fcf"] = out["ocf"] - (out.get("capex") or 0.0)
        out["source"]["fcf"] = "operating cash flow minus capital expenditure, both from filings"

    if out.get("net_income") is not None and out.get("equity"):
        out["roe"] = out["net_income"] / out["equity"]

    # ---- sanity checks. A number that breaks an identity is a bug, not a datum.
    gm, om, nm = out.get("gross_margin"), out.get("operating_margin"), out.get("net_margin")
    if gm is not None and om is not None and om > gm + 1e-9:
        out["warnings"].append(
            f"IMPOSSIBLE: operating margin {om:.1%} exceeds gross margin {gm:.1%}")
    if (out.get("net_income") is not None and out.get("operating_income") is not None
            and out["net_income"] > out["operating_income"] > 0):
        gap = out["net_income"] - out["operating_income"]
        out["nonop_gap"] = gap
        out["nonop_share"] = gap / out["net_income"]
        out["warnings"].append(
            f"{gap / out['net_income']:.0%} of net income sits ABOVE operating income, "
            f"so it is not from operations"
            + (f" (non-operating income {out['nonoperating'] / 1e9:+.2f}B)"
               if out.get("nonoperating") is not None else ""))
    if nm is not None and abs(nm) > 1.5:
        out["warnings"].append(f"IMPLAUSIBLE: net margin {nm:.0%}")
    return out


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    for t in [a.upper() for a in sys.argv[1:]] or ["MU", "MRVL", "AAOI"]:
        d = pull(t)
        print(f"\n{t}")
        if not d:
            print("   no EDGAR match")
            continue
        b = lambda k: f"${d[k] / 1e9:+.2f}B" if d.get(k) is not None else "n/a"
        print(f"   revenue {b('revenue')}  gross {b('gross_profit')}  "
              f"operating {b('operating_income')}  net {b('net_income')}")
        for m in ("gross_margin", "operating_margin", "net_margin"):
            if d.get(m) is not None:
                print(f"   {m:<18}{d[m]:>8.1%}")
        print(f"   ocf {b('ocf')}  capex {b('capex')}  FCF {b('fcf')}")
        if d.get("rev_growth_ttm") is not None:
            print(f"   revenue growth (TTM over prior TTM): {d['rev_growth_ttm']:+.1%}")
        for w in d["warnings"]:
            print(f"   WARNING: {w}")
