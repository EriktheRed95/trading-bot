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
| Do stops / position caps help? | **No** — naive trailing stops whipsaw and *worsen* drawdown; disaster-only stops are no-ops. The regime gate + diversification already handle it. |
| Is news sentiment alpha? | **No** — looked strong on the curated pool but collapsed on the broad universe; it's momentum-independent (not a momentum proxy) but weak and universe-dependent. A minor risk-tilt at most. |

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
```

## Honest limitations
- yfinance price-survivorship (delisted names missing); no walk-forward harness; the synthetic-options model in the original engine is approximate.
- Strategy C results lean on a hand-chosen megacap pool (less biased than the trap, not fully clean) — the *fully* clean broad-universe number needs a paid delisted-price dataset (CRSP / Sharadar / Norgate).
- Nothing here is financial advice; live execution stays `DRY_RUN=True`.
