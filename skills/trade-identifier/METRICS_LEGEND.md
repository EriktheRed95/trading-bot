# Metrics Legend — what each number means, and what's good vs. bad

A plain-English key for the outputs of both skills. **These are rules of thumb, not hard rules** — "good" varies by industry (a software firm's margins dwarf a grocer's) and growth stage. Use as orientation, not gospel.

---

## Part A — Trade Identifier (technical: "when & how much risk")

| Metric | What it is | ✅ WANT | ⚠️ CAUTION | ❌ AVOID |
|---|---|---|---|---|
| **Market regime** (SPY vs its 200-day) | Is the whole market trending up? | RISK-ON (SPY above 200-day) | — | RISK-OFF (SPY below 200-day) → bot goes defensive |
| **Price vs 200-day SMA** | The stock's long-term trend line (avg of ~last 200 trading days ≈ 10 months). The line between uptrend and downtrend | Above 200d; higher % above = stronger trend | within ~±3% (chop) | Below 200d (downtrend) |
| **12-1 momentum** | Return over the last 12 months, skipping the most recent month | Positive, ideally strong (>20%) | small positive | Negative / flat |
| **6-1 momentum** | Same, shorter window (6 mo) — a confirmation | Positive | mixed | Negative |
| **Annualized volatility** | How violently the price swings per year | <25% → normal size | 25–50% → reduce size | >50% → small / fragile, high gap-down risk |
| **VERDICT** | The rule output | `ELIGIBLE` = above 200d **and** positive 12-1 momentum | — | `AVOID` = fails the trend filter |

**Key reminder:** the bot's edge is *risk control, not prediction*. `ELIGIBLE` ≠ "will go up" — it means "passes the trend rules." High-volatility names still get a *small* position even when eligible.

---

## Part B — Financial Researcher (fundamental: "what's worth owning")

### Profitability & quality
| Metric | What it is | ✅ Strong | ⚠️ OK | ❌ Weak |
|---|---|---|---|---|
| **Gross margin** | % of revenue left after direct cost of the product | >40% | 20–40% | <20% (commodity / thin) |
| **Operating margin** | % left after running the business | >20% | 10–20% | <10%; negative = losing money on operations |
| **Net margin** | % of revenue that becomes bottom-line profit | >15% | 5–15% | <5%; negative |
| **ROE** (return on equity) | Profit ÷ shareholder equity — how well it compounds your capital | >15% *consistently* (Buffett favorite); >20% excellent | 10–15% | <10%. *Caveat:* a huge ROE (e.g. 150%) is usually buyback/leverage-distorted, not pure quality |
| **Revenue CAGR** | Annualized revenue growth | >10% | 3–10% | flat / declining |
| **Net income CAGR** | Annualized earnings growth | >10% & steady | positive | erratic / declining |

### Cash generation (what Buffett cares about most)
| Metric | What it is | ✅ Strong | ⚠️ OK | ❌ Weak |
|---|---|---|---|---|
| **FCF** (free cash flow) | Cash left after running *and* maintaining the business (operating cash − capex) | Positive & growing | positive but lumpy | Negative (burning cash) |
| **FCF margin** | FCF ÷ revenue | >15% | 5–15% | <5% / negative |
| **Shares: buyback vs dilution** | Is the share count shrinking (good) or growing (bad)? | Buyback (shares ↓) | flat | Dilution (shares ↑), especially large |

### Balance-sheet strength (can it survive a downturn?)
| Metric | What it is | ✅ Strong | ⚠️ OK | ❌ Weak |
|---|---|---|---|---|
| **Debt/Equity** | Leverage | <0.5 conservative | 0.5–1 moderate | >2 high / risky |
| **Current ratio** | Short-term assets ÷ short-term liabilities | >1.5 comfortable | 1–1.5 | <1 tight (note: a few great firms like AAPL run <1 by design) |
| **Cash** | Liquidity buffer | more is safer | — | thin cash + high debt = fragile |

### Valuation (a great business at a stupid price is still a bad buy)
| Metric | What it is | ✅ Cheaper | ⚠️ Fair | ❌ Expensive |
|---|---|---|---|---|
| **P/E** (price ÷ earnings) | Years of earnings you're paying for | <15 | 15–25 | >40 (lots of growth already priced in) |
| **P/FCF** (price ÷ free cash flow) | Same idea, on cash | <20 | 20–30 | >30 |
| **FCF yield** (FCF ÷ market cap) | Cash return for your price (inverse of P/FCF) | >5% | 2–5% | <2% |

**Context matters:** a fast grower "deserves" a higher P/E — 40× isn't automatically bad if growth is 30%+. And *cheap can be a trap* (cheap for a reason). Valuation is judgment, not a threshold.

### Verdict tiers
- **Wonderful business at a fair price** — high quality + reasonable valuation (the Buffett ideal)
- **Quality but pricey** — great business, expensive stock (wait for a better entry)
- **Mediocre** — unremarkable economics
- **Avoid** — weak/declining business or red flags
- **Pre-profit speculation** — no real business to value yet (story stock)

---

*Not financial advice — a reference for interpreting the tools' output. You make the calls.*
