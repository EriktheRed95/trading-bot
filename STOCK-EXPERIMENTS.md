# Stock strategy accounts

Six new forward-paper strategy accounts start at $25,000 each ($150,000 strategy capital). Two independent $25,000 equal-weight references are comparison capital, not strategy allocations. Existing core, crypto and hourly accounts are retained unchanged.

All accounts use SPY, QQQ, AAPL, MSFT, NVDA and AMD. Each strategy runs separately on 5-minute and 15-minute completed bars:

- Trend: long when price and the 5-bar average are above the 20-bar average.
- Breakout: enter above the preceding 20 closes; retain the signal while above the 10-bar average.
- Mean reversion: enter below -1.5 standard deviations from the 20-bar mean; retain the signal while below the mean.

Each qualifying symbol receives one sixth of the target portfolio. Unused allocations stay in cash. Long-only, no leverage, no shorting; positions may remain overnight. Signals can change each completed bar but fills require a later observed completed bar, after the original signal was actually observed. Checks are throttled to one per minute per cadence, shared across accounts. No missed bars are replayed into the forward ledger. Pending targets expire after three bar intervals.

The server's background collector (see UNIFIED-TRADING.md) checks each cadence separately, shortly after each bar completes during NYSE regular sessions. It stops checking once that bar is recorded, and retries at most once a minute while it is not. The 5m and 15m cadences have separate locks, so a stuck request for one does not block the other. The dashboard does not need to be open. The computer must be on, awake and online, and the local server running. Global pause stops all experiments, and the stock group also has its own persistent pause. Nothing connects to a brokerage.

Costs: 6 basis points per side for strategy and reference, inherited from the existing paper engine. Raw provider closes are price returns, not total returns. Dividends, corporate actions, order-book capacity, actual spreads and taxes are not fully modeled. Results are experiments, not projected profits; more correlated accounts do not replace a longer out-of-sample record.

Data: yfinance public Yahoo prices with pre/post-market bars excluded. Indicators need 21 shared valid bars, two-minute completion buffer and fresh prices. Bounded NYSE2026/2027 published holiday/early-close calendar from https://www.nyse.com/trade/hours-calendars checked September20,2026; unsupported years hold. Emergency closures still rely on provider freshness. A short post-close collection window permits the final regular-session bar; after-hours bars remain excluded.

The new dashboard tab shows each account, cash/equity, fill/observation counts, and comparisons only for matching account/reference records. The record selector shows recent observations and fills. Full record endpoint: /api/record?account=stocks:trend_5m (other IDs from /api/stock-experiments).

New runtime directory: runtime/stock-experiments-v1. It holds eight versioned SQLite account files and a small durable pause/throttle metadata database. Historical prices warm indicators only and never seed fictitious trades.
