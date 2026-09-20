# Dashboard and bot: one paper system

Launch `Start-Trading.ps1` or `python main.py`. The compatibility command `python dashboard.py` opens the same application. Both old entrypoints have been replaced; their prior versions remain in git history.

The local control center is http://127.0.0.1:8791. Automatic checks run while a browser page is open, at most every five minutes. Check now requests an immediate check. Pause is persistent; Resume starts checks again. Closing every page stops new requests, although an in-flight cycle may finish. The idle server can remain running. No new task scheduler entry is installed.

The default book starts with $10,000 of virtual cash. Filled positions, queued targets, costs, activity and trade history are stored in `runtime/paper.sqlite3`, excluded from git. This is separate from any real financial account. No brokerage credentials are loaded, and neither the server nor engine includes a live-order route. The old Schwab helper remains historical code, not an active dependency.

## Shared decisions

`trading_engine.signal_snapshot` supplies both the local bot and `build_dashboard.current_portfolio`. The latter still produces the full static research report; it does not export the private paper database into GitHub Actions or Cloudflare. Opening the older static HTML alone does not run the bot. The unified local launcher is the control center.

Inputs are completed daily sessions, with today's bar withheld until 16:15 America/New_York. SPY must have 253 complete observations and be no more than four calendar days old. Stock candidates require complete recent history and finite positive price/volatility; incomplete names are excluded visibly. A loss of more than 20% of universe coverage blocks execution. Defensive mode requires complete gold/Treasury inputs. These are conservative operational thresholds, not an exchange-calendar implementation.

Paper v1 plans targets on the first observed cycle each month, using Strategy C trend, momentum, inverse-volatility and defensive-sleeve rules. It fills only at a later observed session close, deducting the repository's cost-per-side assumption. The fill session must also have closed after the queued signal was actually observed. Because no exchange calendar is available, a daily session is treated as complete at 13:00 New York, the earliest regular close: a signal observed after 13:00 New York waits for the next completed session rather than assuming a 16:00 close, and a bar a provider publishes late can never fill a signal seen after that bar's close. It does not invent missed historical fills when the application was closed. A queued target older than seven calendar days expires and is replaced without executing. Existing shares drift naturally between rebalances; the ledger does not reset weights every day. Pending targets and filled positions are displayed separately. Repeated or concurrent checks for the same date are idempotent.

The simulation uses fractional shares and adjusted historical prices. Corporate actions and retroactive vendor adjustments are not a broker ledger. There are no auction fills, live quotes, exact venue costs, dividends as separate cash flows, tax-lot accounting or brokerage reconciliation. The displayed paper P&L is exploratory. This paper execution convention differs from the historical backtest; the old headline metrics do not validate it.

## Review findings

- The former main.py ran the older rejected multi-asset scorer, while the research dashboard showed Strategy C. Shared active entrypoints now remove this mismatch.
- The September 11 local report contained NaN prices and weights. The ranker had forward-filled inputs while the weight renderer used unfilled data. One validated snapshot now supplies both.
- The latest inspected cloud run failed with KeyError CL=F: the macro renderer indexed missing data before checking its presence. It now shows Unavailable for missing series.
- Strategy C's historical allocator multiplies constant daily weights by returns between monthly target changes; this differs from fixed-share monthly holdings and omits the turnover that maintaining those weights would require. Historical result claims need a separate rerun with consistent accounting and validated price data. The new paper book uses fixed shares.
- The public research headline also carries survivor-selection/data-quality limits. Research evidence is not live performance; the local UI states that clearly.
- GitHub builds are not deployments. At review time CLOUDFLARE_API_TOKEN and HEALTHCHECK_URL_TRADING were not configured in repository secrets, so hosting/heartbeat setup remained incomplete. No hosting, account or credential changes are included here.

## Verification

Run `python -B -m unittest test_unified -v`. Tests use synthetic prices and temporary paper databases, never a brokerage. They cover data gaps, stale and partial bars, consistent report targets, missing macro fields, later-session fills and costs, pause/restart behavior, fixed-share drift, expired targets, duplicate/concurrent checks and local HTTP write protection.

The server binds to loopback, checks Host/Origin on writes and requires a per-process browser token. It does not expose private state through the static research build. This is a single-user local application; remote control would require a separately designed authenticated service.
