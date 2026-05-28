# Skills

Two Claude skills built on this project's logic. They turn the bot's discipline into on-demand checks you can ask for in plain language.

| Skill | Question it answers | Key files |
|---|---|---|
| **trade-identifier** | *When / how much risk?* — applies Strategy C's rules (200-day regime gate, trend filter, 12-1 momentum, volatility sizing) to any ticker(s) for an ELIGIBLE / AVOID verdict, with live data. | `SKILL.md`, `identify.py`, `METRICS_LEGEND.md` |
| **financial-researcher** | *What's worth owning?* — a Buffett-style fundamental memo from free SEC EDGAR data (margins, ROE, growth, FCF, debt, dilution, valuation) plus the latest 10-K narrative. | `SKILL.md`, `fundamentals.py`, `read_filing.py`, `METRICS_LEGEND.md` |

A high-conviction position should pass **both**: a quality business (financial-researcher) that's also in an uptrend at a sane price (trade-identifier).

## How they're used
Each skill is a self-contained bundle: `SKILL.md` (instructions + trigger description) plus the script(s) it calls and a shared `METRICS_LEGEND.md`. When a script prints results it also appends the relevant part of the legend, so the "want vs. avoid" guidance always travels with the numbers.

## Active vs. versioned copies
- **Active copies** (what Claude actually runs) live in `~/.claude/skills/<skill>/`.
- **The copies here** are the version-controlled backup + documentation on GitHub.

When a skill changes, update both — or treat this repo copy as the source of truth and copy it back into `~/.claude/skills/`. (Same files; just two locations.)

## Dependencies
`yfinance`, `pandas`, `numpy` (trade-identifier); `requests` + `yfinance` (financial-researcher). SEC EDGAR needs a clean `Name email` User-Agent (set in `fundamentals.py` / `read_filing.py`).

*Not financial advice — discipline tools to inform decisions, not to place trades.*
