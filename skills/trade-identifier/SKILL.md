---
name: trade-identifier
description: Apply the trading bot's Strategy C rules (200-day market-regime gate, per-name trend filter, 12-1 momentum, volatility-based sizing and fragility flags) to one or more stock tickers for a disciplined ELIGIBLE / AVOID verdict with live data. Use whenever Erik asks "what does the bot think of <ticker>", shares a watchlist or a stock-tip / fin-fluencer screenshot, asks whether a name is a buy/avoid/overextended, wants a trend or momentum read, or wants several tickers ranked. Cuts through hype with rules instead of vibes.
---

# Trade Identifier

Turns Erik's trading-bot discipline (Strategy C) into an on-demand check on any ticker, using **live** market data. It answers "does the bot's logic say this is a trend-buy candidate or an avoid?" — and helps answer free-form questions about specific tickers, always grounded in the rules rather than hype.

## When to use
- "What does the bot think of NVDA?" / "Is ACHR a buy?" / "Rank these for me: …"
- Erik pastes a screenshot or list of stock tips → extract the tickers and run them.
- Any question about a stock's trend, momentum, volatility, or risk posture.

## How to run it
The script is self-contained (needs `yfinance`, `pandas`, `numpy` — already installed).

```bash
python identify.py TICKER [TICKER ...]
```

Example: `python identify.py NVDA GOOGL ACHR RGTI`

It prints, for each ticker: price vs its 200-day SMA, 12-1 and 6-1 month momentum, annualized volatility, a **VERDICT** (ELIGIBLE / AVOID / NO DATA), a size/fragility note, and — if several names are eligible — a momentum/trend **ranking**. It also reports the **market regime** (SPY above/below its 200-day).

## The rules it applies (Strategy C)
- **Market regime gate:** if SPY is below its 200-day SMA, the bot goes defensive — flag that any "eligible" name would be held only at reduced exposure.
- **Trend filter:** a name is `ELIGIBLE` only if it's **above its own 200-day SMA** AND has **positive 12-1 month momentum**. Otherwise `AVOID`.
- **Sizing / fragility:** volatility >50% annualized → small position, high gap-down risk; 25–50% → reduce; <25% → normal.
- **Ranking:** among eligible names, stronger combined 12-1 + 6-1 momentum + trend-strength ranks higher.

## How to answer Erik
1. Run `identify.py` on the ticker(s) to get live signals — never eyeball it.
2. Give the verdict and the **reason** (e.g., "AVOID — below its 200-day and −49% 12-1 momentum"), then answer his specific question using those numbers plus the bot's philosophy.
3. Keep the framing honest, consistent with the project's findings:
   - The bot's durable edge is **risk control, not stock-picking or prediction.** Don't forecast price targets.
   - Treat fin-fluencer hype and "everything doubles" targets as noise; the rules are the antidote.
   - High-volatility story-stocks (quantum, eVTOL, pre-profit AI) are where active trading destroyed value in backtests and gap-down risk is highest — flag them as such even if "eligible."
4. **Not financial advice.** This is a discipline check. Never place trades or tell Erik to move money — he makes the calls.

## Companion
This reads **charts/technicals only** (the "when / how much risk" question). A separate fundamental-research skill (financial-statement deep dive — the "is this a quality business at a fair price" question) is planned to complement it. When both exist, combine: fundamentals for *what* to own, this for *when* and *how much*.
