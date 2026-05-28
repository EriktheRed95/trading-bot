"""Fundamental researcher - a Buffett-style snapshot from free SEC EDGAR data.

ticker -> CIK -> EDGAR companyfacts (structured XBRL financials) -> compute the
quality/growth/balance-sheet/cash/valuation checklist, flag red flags, and print
the URL of the latest 10-K for narrative reading.

Only needs `requests` + (optionally) `yfinance` for the current price.
EDGAR requires a descriptive User-Agent; set one below.

Usage:  python fundamentals.py AAPL
"""
import sys

import requests

# SEC requires a "Name email" User-Agent; punctuation/parentheses can trip its WAF.
UA = {"User-Agent": "EriktheRed95 erik9@gmail.com", "Accept-Encoding": "gzip, deflate"}
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
SUBS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"

REV = ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet"]
NI = ["NetIncomeLoss"]
GROSS = ["GrossProfit"]
OPINC = ["OperatingIncomeLoss"]
EQUITY = ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"]
CUR_ASSETS = ["AssetsCurrent"]
CUR_LIAB = ["LiabilitiesCurrent"]
DEBT = ["LongTermDebtNoncurrent", "LongTermDebt"]
CASH = ["CashAndCashEquivalentsAtCarryingValue"]
OCF = ["NetCashProvidedByUsedInOperatingActivities",
       "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"]
CAPEX = ["PaymentsToAcquirePropertyPlantAndEquipment"]
SHARES = ["WeightedAverageNumberOfDilutedSharesOutstanding",
          "WeightedAverageNumberOfSharesOutstandingBasic"]


def get_json(url):
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    return r.json()


def ticker_to_cik(ticker):
    data = get_json(TICKERS_URL)
    for row in data.values():
        if row["ticker"].upper() == ticker.upper():
            return str(row["cik_str"]).zfill(10), row["title"]
    return None, None


def annual(facts, concepts):
    """Latest-per-fiscal-year annual values for the first matching concept.
    Returns list of (fiscal_year, value) sorted ascending."""
    src = facts.get("facts", {})
    for space in ("us-gaap", "dei"):
        for c in concepts:
            node = src.get(space, {}).get(c)
            if not node:
                continue
            units = node.get("units", {})
            arr = units.get("USD") or units.get("shares") or next(iter(units.values()), [])
            rows = [x for x in arr if str(x.get("form", "")).startswith("10-K")
                    and x.get("fp") == "FY" and x.get("fy")]
            if not rows:
                rows = [x for x in arr if str(x.get("form", "")).startswith("10-K") and x.get("fy")]
            if not rows:
                continue
            byfy = {}
            for x in rows:
                fy = x["fy"]
                if fy not in byfy or x.get("end", "") > byfy[fy].get("end", ""):
                    byfy[fy] = x
            return [(fy, byfy[fy]["val"]) for fy in sorted(byfy)]
    return []


def cagr(series):
    s = [(fy, v) for fy, v in series if v and v > 0]
    if len(s) < 2:
        return None
    yrs = s[-1][0] - s[0][0]
    return (s[-1][1] / s[0][1]) ** (1 / yrs) - 1 if yrs > 0 else None


def latest(series):
    return series[-1][1] if series else None


def pct(x):
    return f"{x*100:.1f}%" if x is not None else "n/a"


def latest_10k(cik):
    subs = get_json(SUBS_URL.format(cik=cik))
    rec = subs.get("filings", {}).get("recent", {})
    for i, form in enumerate(rec.get("form", [])):
        if form == "10-K":
            acc = rec["accessionNumber"][i].replace("-", "")
            doc = rec["primaryDocument"][i]
            cik_int = int(cik)
            url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc}/{doc}"
            return url, rec["filingDate"][i]
    return None, None


def show_legend():
    """Print the fundamental part of the metric legend so guidance appears with results."""
    import os
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "METRICS_LEGEND.md")
    try:
        txt = open(p, encoding="utf-8").read()
        body = txt[txt.index("## Part B"):].strip()
    except Exception:
        return
    print("\n" + "=" * 64)
    print("HOW TO READ THIS - metric legend (want vs avoid):\n")
    print(body)


def main(ticker):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    cik, name = ticker_to_cik(ticker)
    if not cik:
        print(f"Ticker {ticker} not found in SEC EDGAR (no US filer match).")
        return
    facts = get_json(FACTS_URL.format(cik=cik))

    rev, ni = annual(facts, REV), annual(facts, NI)
    gross, opinc = annual(facts, GROSS), annual(facts, OPINC)
    eq, ca, cl = annual(facts, EQUITY), annual(facts, CUR_ASSETS), annual(facts, CUR_LIAB)
    debt, cash = annual(facts, DEBT), annual(facts, CASH)
    ocf, capex, sh = annual(facts, OCF), annual(facts, CAPEX), annual(facts, SHARES)

    fy_lo = rev[0][0] if rev else (ni[0][0] if ni else "?")
    fy_hi = rev[-1][0] if rev else (ni[-1][0] if ni else "?")
    print(f"=== FUNDAMENTAL SNAPSHOT: {ticker.upper()} - {name} (CIK {cik}) ===")
    print(f"Fiscal years: {fy_lo}-{fy_hi}\n")

    def margin(num, den):
        n, d = latest(num), latest(den)
        return (n / d) if (n is not None and d) else None

    print("QUALITY / PROFITABILITY")
    print(f"  Revenue (latest): {latest(rev)/1e9:.2f}B   rev CAGR: {pct(cagr(rev))}" if latest(rev) else "  Revenue: n/a")
    print(f"  Net income (latest): {latest(ni)/1e9:.2f}B   NI CAGR: {pct(cagr(ni))}" if latest(ni) else "  Net income: n/a")
    rev_latest = latest(rev)
    has_rev = bool(rev_latest and rev_latest >= 1e7)
    if has_rev:
        print(f"  Gross margin: {pct(margin(gross, rev))} | Operating margin: {pct(margin(opinc, rev))} | Net margin: {pct(margin(ni, rev))}")
    else:
        print("  Margins: n/a - negligible revenue (pre-commercial)")
    roe = (latest(ni) / latest(eq)) if (latest(ni) is not None and latest(eq)) else None
    print(f"  ROE (latest): {pct(roe)}")

    print("\nCASH GENERATION")
    fcf_series = []
    omap = dict(ocf)
    cmap = dict(capex)
    for fy in sorted(set(omap) & set(cmap)):
        fcf_series.append((fy, omap[fy] - cmap[fy]))
    fcf = latest(fcf_series)
    print(f"  Free cash flow (latest): {fcf/1e9:.2f}B" if fcf is not None else "  Free cash flow: n/a")
    print(f"  FCF margin: {pct((fcf/rev_latest) if (fcf is not None and has_rev) else None)} | FCF CAGR: {pct(cagr(fcf_series))}")

    print("\nBALANCE SHEET")
    de = (latest(debt) / latest(eq)) if (latest(debt) is not None and latest(eq)) else None
    cr = (latest(ca) / latest(cl)) if (latest(ca) is not None and latest(cl)) else None
    print(f"  Debt/Equity: {de:.2f}" if de is not None else "  Debt/Equity: n/a")
    print(f"  Current ratio: {cr:.2f}" if cr is not None else "  Current ratio: n/a")
    print(f"  Cash: {latest(cash)/1e9:.2f}B" if latest(cash) is not None else "  Cash: n/a")

    # Raw XBRL share counts are NOT split-adjusted, so only compare a short
    # recent window (splits are rare) and label it approximate.
    recent_sh = sh[-4:] if len(sh) >= 4 else sh
    dil = None
    if len(recent_sh) >= 2 and recent_sh[0][1] and recent_sh[-1][1]:
        dil = recent_sh[-1][1] / recent_sh[0][1] - 1
        tag = "buyback (good)" if dil < -0.01 else ("dilution (watch)" if dil > 0.01 else "flat")
        print(f"\n  Diluted shares {recent_sh[0][0]}->{recent_sh[-1][0]}: {dil*100:+.1f}% -> {tag}  (raw, not split-adjusted)")

    # Valuation (needs current price)
    print("\nVALUATION")
    try:
        import yfinance as yf
        px = yf.Ticker(ticker).history(period="5d", auto_adjust=True)["Close"].dropna().iloc[-1]
        shares_now = latest(sh)
        if shares_now:
            mktcap = px * shares_now
            pe = mktcap / latest(ni) if latest(ni) and latest(ni) > 0 else None
            pfcf = mktcap / fcf if fcf and fcf > 0 else None
            fcf_yield = fcf / mktcap if fcf and mktcap else None
            print(f"  Price ${px:.2f} | approx market cap {mktcap/1e9:.1f}B")
            print(f"  P/E: {pe:.1f}" if pe else "  P/E: n/a (no positive earnings)")
            print(f"  P/FCF: {pfcf:.1f}" if pfcf else "  P/FCF: n/a")
            print(f"  FCF yield: {pct(fcf_yield)}")
    except Exception as e:
        print(f"  (price/valuation unavailable: {e})")

    # Red flags
    flags = []
    if latest(ni) is not None and latest(ni) <= 0:
        flags.append("unprofitable (negative net income)")
    if fcf is not None and fcf <= 0:
        flags.append("negative free cash flow")
    if de is not None and de > 2:
        flags.append(f"high leverage (D/E {de:.1f})")
    if dil is not None and dil > 0.10:
        flags.append("recent share dilution")
    nm = margin(ni, rev)
    if has_rev and nm is not None and nm < 0:
        flags.append("negative net margin")
    print("\nRED FLAGS: " + ("; ".join(flags) if flags else "none obvious from the numbers"))

    url, filed = latest_10k(cik)
    print(f"\nLATEST 10-K (read the narrative): {url}" if url else "\nLATEST 10-K: not found")
    if filed:
        print(f"  filed {filed}")

    show_legend()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python fundamentals.py TICKER")
        sys.exit(1)
    main(sys.argv[1])
