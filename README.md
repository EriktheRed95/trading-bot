# 🤖 Adaptive Hybrid Trading Engine

A multi-regime backtesting and live-trading framework that classifies each asset by its volatility/trend profile, applies a different strategy to each class, and overlays a macro filter for global risk-on/risk-off conditions.

Built in Python around a clean four-layer architecture, with a Streamlit dashboard and an AES-encrypted trade journal synced to Google Drive.

> **Status:** Backtesting engine + Streamlit dashboard are working. Live execution via Charles Schwab API is wired but `DRY_RUN=True` by default.

---

## 🎯 The problem this solves

Most retail "trading bot" projects pick one strategy (trend-following or mean-reversion) and apply it to every ticker. That works for the subset of stocks that match the strategy, and silently loses money on the rest. A high-volatility momentum stock like NVDA needs different treatment than a choppy laggard like F, which needs different treatment than a low-vol blue chip like JPM.

This engine **classifies the asset first**, then chooses the strategy.

---

## 🧠 The "Sorting Hat" classifier

Each asset gets bucketed using a rolling-window calculation of **annualized volatility** and **Wilder's ADX** (trend strength):

| Identity   | Profile                           | Strategy                                                       | Core / Satellite |
|------------|-----------------------------------|----------------------------------------------------------------|------------------|
| 🚀 Rocket   | High vol (>35%) + strong trend (ADX > 25) | Trend-follow + synthetic 3× options on dips, RSI-based exits | 80% / 20%        |
| ⚔️ Grinder  | Mid vol (>20%) + choppy (ADX ≤ 25) | Mean-reversion: only longs above SMA200, only shorts below     | 0% / 100%        |
| 🛡️ Fortress | Low vol (<20%)                    | Buy-and-hold; deploys satellite cash only on deep dips          | 90% / 10%        |

Classification uses only data available **up to the start of the simulation** — no future leakage (this was a real bug; see *Engineering decisions* below).

---

## 🏗️ Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                       senses (input)                         │
│  system_senses_stream.py  ·  senses_macro.py                 │
│  • Yahoo Finance OHLCV   • VIX, 10Y Treasury yields          │
└───────────────────────────┬──────────────────────────────────┘
                            ▼
┌──────────────────────────────────────────────────────────────┐
│                       brain (decide)                         │
│  system_strategy_evaluator.py  (routes by asset class)       │
│    ├── algo_stocks.py    (RSI + MACD + SMA50/200)            │
│    ├── algo_crypto.py    (SMA200 filter + RSI/MACD)          │
│    └── algo_forex.py     (Bollinger mean-reversion + RSI)    │
│  indicators.py           (one source of truth for math)      │
│  strategy_config.py      (shared thresholds for live + bt)   │
└───────────────────────────┬──────────────────────────────────┘
                            ▼
┌──────────────────────────────────────────────────────────────┐
│                       hands (execute)                        │
│  system_execution_client.py  (Charles Schwab API)            │
│  main.py                     (live loop)                     │
│  backtest_engine.py          (vectorized historical sim)     │
└───────────────────────────┬──────────────────────────────────┘
                            ▼
┌──────────────────────────────────────────────────────────────┐
│                  black box (audit trail)                     │
│  journal.py  →  AES-Fernet encrypted JSON → Google Drive     │
└──────────────────────────────────────────────────────────────┘
```

---

## 🔬 Engineering decisions worth highlighting

The interesting work on this project was **finding bugs in my own backtester** that were inflating returns. A backtest that runs cleanly is not the same as a backtest that's correct.

**1. Removed look-ahead bias in the classifier.** The original `classify_asset()` was called once at the start of the simulation using the *entire* dataframe — including data from the future. This meant the bot "knew" PLTR would turn into a Rocket years before the trade window opened. Refactored to `classify_asset_at(df, end_idx)` which only sees `df.iloc[end_idx - 1000 : end_idx]`. Reported returns dropped substantially; that's the point.

**2. Wired the macro regime filter into the backtest.** The `analyze_market_regime()` function (VIX/yield scoring) had been used by `main.py` (the live loop) for months but never by `backtest_engine.py`. Backtests were therefore not representative of what the live bot would actually do. Now both paths use the same scoring path.

**3. Added per-side slippage + commission.** Five bps slippage + one bp commission per side, applied on every fill — including option-position entries/exits and short-cover. For high-frequency Grinder strategies this is the difference between "winner" and "loser."

**4. Consolidated four duplicate RSI implementations** that had drifted apart. Three still used `.where()` (crash-prone on flat-price segments) while one had been patched to use `.clip()` + epsilon. Moved to a single `indicators.py` module.

**5. Replaced hardcoded encryption password** with an env-var-derived key and a randomly generated 16-byte salt. The original `password = b"my_super_secret_bot_password"` made the encryption theater — anyone with the source could decrypt the journal.

**6. Unified live and backtest thresholds.** `main.py` was using `total_score >= 4` while the backtest used `>= 3` for Rockets and `>= 4.5` for Grinders. Extracted both to `strategy_config.py` so a change moves them in lockstep.

---

## 🧰 Stack

`Python 3.10+` · `pandas` · `numpy` · `yfinance` · `streamlit` · `plotly` · `cryptography` (Fernet) · `schwab-py` · `google-api-python-client`

---

## 📁 Project structure

```
TradingBot/
├── backtest_engine.py          # Vectorized historical simulator
├── main.py                     # Live trading loop (DRY_RUN=True by default)
├── dashboard.py                # Streamlit GUI
├── indicators.py               # Shared technical-indicator math
├── strategy_config.py          # Thresholds + cost params (shared)
├── senses_macro.py             # VIX / TNX macro filter
├── system_senses_stream.py     # Live OHLCV fetcher
├── system_strategy_evaluator.py # Brain (asset-class router)
├── system_execution_client.py  # Schwab API client
├── algo_stocks.py / algo_crypto.py / algo_forex.py
├── journal.py                  # Encrypted Google Drive trade log
└── requirements.txt
```

---

## 🚀 Running it locally

```bash
# 1. Clone and install
git clone https://github.com/EriktheRed95/trading-bot.git
cd trading-bot
pip install -r requirements.txt

# 2. Set up the journal encryption key (one-time)
python -c "import secrets; print(secrets.token_urlsafe(32))"
# Windows (User scope, persists across shells):
[Environment]::SetEnvironmentVariable("TRADINGBOT_JOURNAL_KEY", "<paste-key>", "User")

# 3. Run a backtest across the default ticker list
python backtest_engine.py

# 4. Or launch the live dashboard
streamlit run dashboard.py
```

For **live execution** (still off by default), supply `schwab_keys.json` with your Schwab developer credentials and toggle `DRY_RUN=False` in `main.py`.

---

## 📉 Honest limitations

- **Yahoo Finance's hourly endpoint caps at ~730 days.** That's one walk-through per ticker; not enough for walk-forward validation. Real validation needs multi-decade daily data + a real walk-forward harness.
- **The synthetic-options model is approximate.** Constant 3× delta, fixed 0.17%/day theta, no IV, no spreads. Useful as a "what if I'd held a leveraged position" proxy; not a real options simulator.
- **No transaction logging or PnL attribution.** Trades print to console and (encrypted) go to the journal, but I don't aggregate them into a per-strategy PnL report yet.

---

## 🔭 Future work

- Replace yfinance with a proper data provider (Polygon, Alpaca, or Databento) for longer history and lower latency.
- Walk-forward backtest harness: train on a window, test on the next window, roll forward.
- Multi-ticker portfolio allocator: capital flows toward the top-N highest-ADX names each week.
- Alpaca paper-trading integration so the live loop has a forward-test channel without Schwab.
- Per-strategy PnL attribution and a "trade tape" in the dashboard.
