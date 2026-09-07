# Architecture & Methodology

How the pieces of this repo fit together, what each module does, and — just as important — **what the experiments actually proved**. The README has the headline results; this is the engineer's map.

> **One-line thesis:** the original mean-reversion bot loses to buy-and-hold; a trend-following, regime-gated allocator (**Strategy C**) wins on *risk-adjusted* terms; bolt-on stops and a news-sentiment tilt were tested honestly and mostly *rejected*. The durable edge is **risk control, not prediction.**

---

## Layers at a glance

```
DATA  ── yfinance (prices), SEC EDGAR (fundamentals), FNSPID (news), Wikipedia (index membership)
  │
SIGNALS ── indicators.py · metrics.py · senses_macro.py
  │
STRATEGIES
  ├─ Original "Sorting Hat" engine (mean-reversion)  → backtest_engine.py + algo_*.py   [LOSES]
  └─ Strategy C (trend-following allocator)           → strategy_c.py                     [WINS risk-adjusted]
  │
VALIDATION ── universe/survivorship · risk overlay · gap-down · sentiment studies
  │
OUTPUT ── plot_results.py · plot_equity.py · README charts
```

---

## Module map

### Original engine — the "Sorting Hat" (mean-reversion; the honest failure)
- **`backtest_engine.py`** — classifies each asset as Rocket / Grinder / Fortress (vol + ADX) and applies a different mean-reversion strategy to each. Now instrumented to record per-bar equity curves. *Result: beats buy-and-hold on only 2/14 (hourly) and 0/14 (daily).*
- **`algo_stocks.py` / `algo_crypto.py` / `algo_forex.py`** — the per-asset-class scoring rules (RSI / MACD / SMA / Bollinger).
- **`system_strategy_evaluator.py`** — routes a ticker to the right algo.
- **`indicators.py`** — single source of truth for RSI / MACD / Bollinger / ADX.
- **`strategy_config.py`** — shared thresholds + cost params (slippage/commission) so live and backtest stay in sync.
- **`senses_macro.py`** — VIX / 10-Y-yield macro regime modifier.
- **`system_senses_stream.py`** — live OHLCV fetch. **`system_execution_client.py`** — Charles Schwab API client. **`main.py`** — live loop (`DRY_RUN=True`). **`journal.py`** / **`view_journal.py`** — AES-encrypted trade journal on Google Drive. **`dashboard.py`** — Streamlit GUI. **`cleanup.py`** — journal maintenance.

### Strategy C — the version that wins
- **`strategy_c.py`** — the core. Long-only, trend-following, regime-gated ranking allocator:
  - `run_allocator(...)` — fast monthly-rebalanced engine: SPY 200-day regime gate → per-name trend filter (above own 200d + positive 12-1 momentum) → z-blend momentum rank → top-N inverse-vol sizing → dynamic risk-off sleeve (`risk_off='dynamic'` holds gold/Treasuries only while trending, else cash). Optional `holdable_fn` (point-in-time membership), `sentiment_df`/`sentiment_weight` (news tilt), `orthogonalize`.
  - `run_with_overlay(...)` — daily holdings engine adding intra-month stops + per-name/sector position caps (used to test the risk overlay).
  - `ew_index(...)` — equal-weight benchmark; `SECTORS` — sector map for caps.
- **`metrics.py`** — Sharpe / CAGR / annualized vol / max drawdown / Calmar from any equity curve (annualization inferred from timestamps).
- **`plot_equity.py`** — 2-panel equity + drawdown chart. **`plot_results.py`** — the original-engine results table/chart (two timeframe profiles).

### Validation studies (the rigor)
- **Universe / survivorship:** `run_sp500.py` (current full S&P 500 — a *cautionary* 40.6% mirage), `run_sp500_pit.py` (point-in-time membership reconstructed from Wikipedia's change log — the honest version, ~32% with the coverage gap reported).
- **Gap-down risk:** `demo_exits.py` (trend exits cap real crashers at −15–25% vs −84% held), `stress_gaps.py` (synthetic overnight-gap Monte Carlo — diversification cuts worst-case drawdown −77%→−31%).
- **Risk overlay:** `run_overlay.py` + `run_overlay_smart.py` (stops/caps tested — *baseline already wins*; even a disaster-only stop is a no-op).
- **Overnight vs intraday:** `overnight_vs_intraday.py` decomposes every session into its close-to-open and open-to-close legs (asserting the two recombine to the full day), charges the repo cost model on every side, runs a universe rather than one name, and sweeps cost to find the breakeven. Tests the "buy the close, sell the open" claim from Lou, Polk & Skouras, *JFE* 134(1), 2019. *Result: real gross, unharvestable net.*

### Bot 2 — news / sentiment / industry (a complementary idea-feeder, not a standalone strategy)
- **`news_sentiment.py`** — VADER scoring over headlines (pluggable provider). **`industry_map.py`** — thematic baskets + correlation peers ("who benefits from the SpaceX IPO"). **`live_picks.py`** — wires sentiment into Strategy C's *live* ranking as a tilt.
- **`fnspid_sentiment.py`** — streams the 23 GB FNSPID news dataset → a 13 MB monthly sentiment panel. `run_sentiment_ab.py` / `run_sentiment_pit_ab.py` / `run_sentiment_validate.py` — the A/B + orthogonalization + sub-period tests. *Result: real but weak, not alpha (see below).*

### Skills (see `skills/`)
Two Claude skills built on this logic: **trade-identifier** (technical verdict on any ticker) and **financial-researcher** (EDGAR fundamental memo). Versioned here; the active copies live in `~/.claude/skills/`.

---

## What the experiments proved (honest findings)

| Question | Verdict |
|---|---|
| Does the mean-reversion "Sorting Hat" beat buy-and-hold? | **No** — 2/14 (hourly), 0/14 (daily), worse over longer horizons. Mean-reversion fights trends. |
| Does trend-following + regime gate win? | **Yes, risk-adjusted** — Strategy C: Sharpe ~1.1 vs SPY 0.64, drawdown ~−26% vs −55%, on the megacap pool it beats even EW-hold of the *same names*. |
| Best risk-off sleeve? | **Dynamic** ("flight to what's working") — beats cash/gold/Treasuries on every metric. |
| Can you fix survivorship with today's S&P 500? | **No — that's the trap** (inflates to a fake 40.6% CAGR). Only *point-in-time* membership (+ ideally delisted prices) is honest. |
| Is the 32.2% point-in-time figure real? | **Partly. It is 24.7% once the bad data is screened out** ([`rerun_pit_filtered.py`](rerun_pit_filtered.py)). Control first: the unfiltered path reproduces **32.3%** against the published 32.2%, so the harness is sound and the delta is meaningful. Screening out untrustworthy name-days (close under $1, or a close-to-close move beyond ±50%, poisoning the whole 252-day signal window) costs **7.6 points of CAGR** and drops Sharpe 1.22 → 1.01, drawdown roughly unchanged at -34%. That is nearly as large as the ~8 points that removing selection hindsight cost. Strategy C still beats SPY (10.9%, 0.65, -55%) comfortably, but **the headline number was inflated by broken delisted-ticker data, and the repo's "Sharpe ~1.1-1.25" claim should read ~1.0-1.1.** The filter is deliberately conservative and removes some real events, so the true value likely sits between 24.7% and 32.3%, nearer the former. |
| Do stops / position caps help? | **No** — naive trailing stops whipsaw and *worsen* drawdown; disaster-only stops are no-ops. The regime gate + diversification already handle it. |
| Is news sentiment alpha? | **No** — looked strong on the curated pool but collapsed on the broad universe; it's momentum-independent (not a momentum proxy) but weak and universe-dependent. A minor risk-tilt at most. |
| Does buying the close and selling the open, **on every name**, survive costs? | **No.** On point-in-time S&P membership 2007-2026 (~417 names/day) the undifferentiated overnight leg grosses 7.9% CAGR at Sharpe 0.69 vs SPY's 11.0% at 0.63, and 12.0% for equal-weight holding the same names — no edge even at zero cost. Net at 6bp/side: **-20.2%**. |
| Does the overnight effect survive **selection**? | **Yes, and this reverses the line above.** Ranking on trailing 252-day overnight return and holding the top names overnight only, point-in-time, data-filtered, degeneracy-filtered: **top-33 (3% positions) = 26.9% CAGR, Sharpe 1.49; post-2019 22.9% at Sharpe 1.24.** The naive test failed because it had no selection, not because the effect is absent. Survives every attempt to break it: it is **not momentum** (median Spearman rho 0.41, 42% name overlap; the momentum-orthogonalized residual still returns 18.2% at Sharpe 1.26 vs 12.6% for ranking on momentum itself), it is **not a data artifact** (holds on the clean 65-name megacap pool at 35.6% vs 20.6% for the same names held all day; `Open == prior Close` on only 0.0-3.1% of days for the selected names), and the **timing is the edge, not just the selection** (same basket held all day: 13.6% at Sharpe 0.65 vs 21.0% at 1.42 overnight). A SPY 200-day gate lifts Sharpe to 1.81 and cuts drawdown to -13%. |
| Is it *implementable*? | **Unresolved, and this is now the only thing that matters.** The entire result rides on capturing the official opening and closing auction prints (market-on-open / market-on-close), because the backtest measures exactly those prints. Commissions are zero at US retail brokers, so the binding cost is auction slippage. Sensitivity: full period it beats every benchmark up to ~1bp/side; **post-2019 it beats them only below ~0.25bp/side on RETURN**, though its Sharpe (1.2-1.7) and drawdown (-13% gated) stay far better than SPY's 0.93 / -34% well beyond that. Not deployable until real fills are measured against the print. |
| Can free price data be trusted on **stale/stub tickers**? | **No, a second distinct failure from the delisted-price one.** 16 tickers (SW, BMC, CFC, CPWR, RSH, TIE, EA, THC…) print `Open` identical to a `Close` on more than 10% of bars — SW does it on 80%. They pass a price floor and a move cap because the prices look ordinary; only the degeneracy check catches them. They inflated the top-20 overnight result from 32.2% to 37.8% CAGR. **Any study splitting the day into legs must screen for degenerate Opens.** |
| Can free price data be trusted on delisted tickers? | **No, and this is a live risk to `run_sp500_pit.py`.** CBE delisted in 2012 yet prints 2016 rows with prev-close $0.005 against open $170 (+3,399,900% overnight), dozens of times. 10 of 682 priced names print >500% moves; unfiltered they produced a **2,325% CAGR**. Point-in-time membership deliberately restores delisted names, which are exactly the ones free data serves badly. **These names pass Strategy C's eligibility filter on 13,072 name-days and reach the momentum top-10 on 190 rebalance dates** (CPWR and MI still read eligible in 2026). The 32.2% PIT figure is **unverified, not disproven** — re-run it with a price floor ($1 both sides) and a ±50% single-session cap, which drops 2.8% of name-days. |

The recurring lesson: **be skeptical of any result that looks too good — the harder test usually deflates it.**

---

## Data sources (all free)
- **yfinance** — daily/hourly prices. (Caps hourly history at ~730 days; delisted tickers are dropped → price-survivorship.)
- **SEC EDGAR** — `data.sec.gov` companyfacts (structured financials) + filings; needs a clean `Name email` User-Agent.
- **FNSPID** (HuggingFace) — historical financial news for the sentiment backtest.
- **Wikipedia** — S&P 500 constituents + change log for point-in-time membership.

## How to run
```bash
pip install -r requirements.txt

python backtest_engine.py          # original engine (mean-reversion) batch
python plot_results.py [--long]    # original engine results + chart
python strategy_c.py               # Strategy C on the megacap pool (risk-off variants)
python run_sp500_pit.py            # Strategy C on point-in-time S&P 500
python demo_exits.py               # gap-down exit-timing proof
python stress_gaps.py              # synthetic gap stress test
python run_overlay_smart.py        # risk-overlay A/B
python run_sentiment_validate.py   # sentiment orthogonalization + sub-period
python overnight_vs_intraday.py --json overnight_results.json   # overnight-effect cost test
python build_dashboard.py          # evidence-tiered dashboard -> Documents/CoworkOS
```

## The dashboard (`build_dashboard.py`)

Renders every idea in this stack into one page under
`Documents/CoworkOS/Trading Dashboard/`, as HTML and as Markdown. Its one design
rule: **every panel carries an evidence tier on its face**, and the tiers do not
look alike (validated is solid and prominent, untested is dashed and lighter).
Ideas already disproven here get their own section, kept high on the page rather
than in a footer, so neither a human nor an agent rebuilds them. It is read-only,
reads `DRY_RUN` out of `main.py` rather than asserting it, and pulls live gold /
oil / 10-year-yield / SPY-regime readings at build time.

## Honest limitations
- yfinance price-survivorship (delisted names missing); no walk-forward harness; the synthetic-options model in the original engine is approximate.
- Strategy C results lean on a hand-chosen megacap pool (less biased than the trap, not fully clean) — the *fully* clean broad-universe number needs a paid delisted-price dataset (CRSP / Sharadar / Norgate).
- Nothing here is financial advice; live execution stays `DRY_RUN=True`.
