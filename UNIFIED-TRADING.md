# Dashboard and bot: one paper system

Launch `Start-Trading.ps1` or `python main.py`. The compatibility command `python dashboard.py` opens the same application. Both old entrypoints have been replaced; their prior versions remain in git history.

The local control center is http://127.0.0.1:8791. Pause is persistent, and Resume starts checks again. Check now requests an immediate check.

## Background collection

Automatic checks come from a background collector inside the server process (`collector.py`). It needs no browser page, dashboard tab or AI session, and closing every page does not stop it. The collector does not add a second way of collecting data. Every family uses its own run code, locks and pause state, the same ones its dashboard button uses:

| Family | Code path | When a check is due |
|---|---|---|
| Core daily portfolio, core benchmarks and research helpers | `Controller.run_core` | When the bundled NYSE calendar says a newer completed session can exist (from 16:15 New York). It rechecks every 5 minutes until that session is recorded, backing off to 30 minutes after failures. |
| Hourly market lab | `Controller.run_hourly` | Every 5 minutes, as before, backing off to 30 minutes after failures. |
| Active 15-minute crypto | `ActiveExperiment.run_guarded` | Once the latest completed 15-minute bar (2-minute delay) is not recorded. Retries every 2 minutes, backing off to 15. |
| Stock labs, 5m and 15m separately | `StockExperiments.run_interval` | During NYSE regular sessions only, once the latest completed bar is not recorded. Retries every minute, which is the existing durable per-cadence throttle, backing off to one bar interval. |

The timer ticks every 15 seconds. A family starts only when it is unpaused, idle and due. The scheduler, dashboard buttons and any old open tab all pass through the same `Collector.request` gate and family lock, so one cannot duplicate another's work. The single exception is Check now for the core: it deliberately re-checks even when the session is already recorded. That is idempotent: the book records `Already processed this session`, which is not a new observation.

Each family runs in its own worker thread, so a failing or hung family does not stop the others. When a check has not returned within 5 minutes (intraday families) or 15 minutes (core and hourly), the family is shown as *Unhealthy: request not returning*. No second worker starts while its lock is held. Network calls keep their existing timeouts. If yfinance is older than 1.x and shares download state between threads, Yahoo-backed families take turns instead of fetching in parallel.

`runtime/collector.sqlite3` stores the collector process heartbeat and, for each family: last attempt, source (scheduler, browser or manual), outcome, message, check count, new-observation count, consecutive failures, latest recorded bar and when it was observed. It also keeps the latest 5,000 attempts. A *check* is a provider request. A *new observation* is a bar the paper books had not recorded, measured by comparing each book's observation count before and after the check. Outcomes are `new`, `no_change`, `partial` (core accepted but a benchmark or helper step failed; retried after 30 minutes), `held` (data rejected, nothing recorded), `error`, `closed`, `skipped`, `paused` or `interrupted` (the process ended mid-check).

The dashboard's Collection health panel reads `/api/collector`, which never starts a check. Per family it shows Current, Waiting for the next bar, Overdue, Market closed, No observation yet, Paused, Failing, Unhealthy or Calendar not bundled. Market closed and overdue come from the bundled NYSE 2026–2027 calendar, with holidays and 13:00 early closes, and crypto runs around the clock. Outside 2026–2027 the core still checks every 5 minutes, and the stock labs hold, as before.

### What it needs, honestly

- **The computer must be on, awake and online, and the server process must be running.** There is no cloud service. Nothing runs while the computer is off, asleep or hibernating, and the scheduled task never wakes it.
- **Signed in.** The optional scheduled task (below) starts the server at logon and relaunches it every 10 minutes if it has stopped, but only while you are signed in. A locked screen is fine. Signing out stops it.
- **Offline or sleep.** Checks fail or do not run, and the dashboard shows the families as failing or overdue. After waking or reconnecting, the next check records only the latest completed bar. Missed bars are never backfilled, never replayed as live observations and never synthesized. They stay visible as gaps in each account record. Pending targets expire under their existing rules.
- Restarting is safe. Every paper-book write is a single SQLite transaction. A check interrupted by a crash or by `Stop-PaperCollector.ps1` is rolled back, and the next start logs it as `interrupted`. Global and family pause state are stored in the paper databases and survive restarts.

### Single process

The server holds an exclusive lock on `runtime/collector.lock` for its lifetime (released by the OS however it exits) and binds 127.0.0.1:8791 exclusively. A second launch for the same runtime exits immediately with code 0 and touches nothing. A different program holding the port makes the server exit with code 4. Nothing is ever killed automatically.

### Scheduled start (optional, per user, no administrator rights)

```
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\Install-PaperCollector.ps1 -DryRun     # show, register nothing
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\Install-PaperCollector.ps1             # register
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\Install-PaperCollector.ps1 -Uninstall  # remove
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\Stop-PaperCollector.ps1 [-DisableTask] [-WhatIf]
```

The task *TradingBot Paper Collector* runs `pythonw.exe -B main.py --no-browser --restart-hung-after 60` from this folder, with no window. It runs as the current user, logged on only, at standard rights, with *do not start a new instance* set and no time limit. The installer refuses to add a second task that references this repository unless `-Force` is given. With `--restart-hung-after 60`, a server whose check has been stuck for an hour exits with code 75 so the task can start a fresh process. The hidden service logs to `runtime/service.log` and `runtime/service-errors.log`.

`Stop-PaperCollector.ps1` stops one process, and only if it is the single listener on the port, answers `/api/collector` as PAPER ONLY with that same PID, and is python/pythonw running main.py or dashboard.py. It never stops python broadly. Without `-DisableTask`, the task starts the server again within 10 minutes.

`Start-Trading.ps1` still opens the dashboard, starting the server (with its collector) first if needed. `python main.py --no-scheduler` serves the dashboard with manual checks only. `python -B scripts/collection_report.py [--since ISO]` reads the runtime databases read-only and counts the observations each family actually recorded.

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

`python -B -m unittest test_collector -v`, `node test_collector_ui.cjs` and `node test_stock_ui.cjs` cover the background collector. All five families collect with no browser request. Overlapping scheduler, browser and manual requests start one worker. Global and family pause survive restarts. Failing and hung families are isolated. Closed markets are distinguished from overdue observations. Rejected data leaves every record unchanged. Single-process startup and the read-only report are also tested. They make no network requests.

The server binds to loopback, checks Host/Origin on writes and requires a per-process browser token. It does not expose private state through the static research build. This is a single-user local application; remote control would require a separately designed authenticated service.

## Coverage audit and research imports

`python -B scripts/coverage_report.py [--since ISO] [--until ISO] [--json]` audits unattended collection from the collector's first attempt: expected versus recorded bars per session, gaps and their causes (collector silent, provider or data failure, checks accepted without the bar), held accounts, observation latency, fees and exposure, and strategy-versus-reference comparisons on matched bars only. It opens every database read-only, forces no check, and reports insufficient samples instead of any projection. Pin `--until` to reproduce a report. `python -B -m unittest test_coverage_report -v` covers it and the baseline `collection_report.py`.

The **Research imports** tab stages public video or carousel links and local video or image files, then reads one item at a time with Gemini only after explicit per-item approval. Imports are unverified source material kept in `runtime/research-imports/`; they never change a paper strategy, place an order or count as a validated finding. See `RESEARCH-IMPORT.md`. `python -B -m unittest test_research_import -v` and `node test_research_import_ui.cjs` cover it without any provider or network call.
