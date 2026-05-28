---
name: financial-researcher
description: Deep fundamental research on a public company — a Warren-Buffett-style memo built from free SEC EDGAR filings. Pulls structured financials (margins, ROE, growth, free cash flow, debt, share dilution, valuation) plus the latest 10-K narrative, then judges business quality, moat, balance-sheet strength, valuation, and red flags. Use when Erik asks "is <company> a good business / good investment", "deep dive <ticker>", "read <ticker>'s financials / 10-K", "what would Buffett think of <ticker>", or wants fundamentals to complement the technical trade-identifier. Cuts through hype with the actual numbers.
---

# Financial Researcher (fundamental deep dive)

Erik can't read a 300-page 10-K; this does the reading and the math. It answers the question the technical bot **can't**: *is this a quality business worth owning at a sensible price?* (the "what to own" question — vs. the trade-identifier's "when / how much risk").

## When to use
- "Is GOOGL a good investment?" / "Deep dive CRDO" / "Read Oracle's financials"
- "What would Buffett think of <ticker>?" / "Is this overpriced?"
- Any fundamentals / financial-statement / 10-K question, or to complement a technical read.

## How to run it
The script (needs `requests`, and `yfinance` for the live price) hits free SEC EDGAR.

```bash
python fundamentals.py TICKER
```

It prints a structured snapshot — revenue/earnings/FCF and their CAGRs, gross/operating/net margins, ROE, debt/equity, current ratio, cash, recent share dilution/buyback, and valuation (P/E, P/FCF, FCF yield) — flags obvious problems, and gives the **URL of the latest 10-K**.

## Then read the narrative
After running the script, read the 10-K with **`python read_filing.py <10-K url>`** (do NOT use WebFetch — SEC's WAF blocks it; this helper uses the correct SEC User-Agent). It prints the Business overview, customer-concentration sentences, and the risk-factor summary. Use it to extract:
- **Business & moat:** what the company actually does; durable competitive advantage (brand, network, switching costs, scale, patents)? Pricing power?
- **MD&A:** management's explanation of growth drivers, margin trends, segment performance.
- **Risk factors:** the top real risks (not boilerplate); customer concentration, competition, regulation, leverage, going-concern.
- **Anything alarming:** litigation, restatements, auditor concerns, aggressive accounting.

## Synthesize the Buffett memo
Combine the numbers + narrative into a short memo:
1. **Business & moat** — what it does, and whether it has a durable advantage (look for *high, stable ROE/margins over time* = pricing power).
2. **Quality & growth** — margin levels and trend, revenue/earnings/FCF growth, ROE.
3. **Balance sheet** — leverage, liquidity, cash. Can it survive a downturn?
4. **Cash generation** — is it a real cash machine (positive, growing FCF), and does it return cash (buybacks) or dilute?
5. **Valuation** — cheap/fair/expensive on P/E, P/FCF, FCF yield. A wonderful business at a stupid price is still a bad buy.
6. **Red flags** — from the script + the filing.
7. **Verdict** — one of: *wonderful business at a fair price* / *quality but pricey* / *mediocre* / *avoid* / *pre-profit speculation (un-investable on a Buffett basis)*.

## Honest framing (important — match the project's rigor)
- **Fundamental quality is a selection discipline, not guaranteed alpha.** Value/quality investing underperformed growth for much of 2010–2020; "cheap" can stay cheap (value traps); even Buffett trailed the index for long stretches. Don't promise outperformance.
- It answers *what's worth owning*, not *when* — it's slow-moving (quarterly). For timing and position-sizing, use the **trade-identifier** skill.
- **Data caveats:** numbers are as-reported XBRL; raw share counts are NOT split-adjusted (the script only compares a short recent window); cash excludes long-term marketable securities; ROIC is approximated by ROE/margins. Treat as a high-quality first pass, not audited analysis.
- **Pre-revenue / pre-profit names** (quantum, eVTOL, early AI): the memo should say plainly that there's no business to value yet — it's speculation, not investment.
- **Not financial advice.** Research and judgment to inform Erik; he makes the calls. Never place trades.

## Companion
Pairs with the **trade-identifier** skill: this = *what to own* (quality + value); trade-identifier = *when / how much risk* (trend + regime + sizing). A high-conviction position should pass **both** — a quality business that's also in an uptrend at a non-crazy price.
