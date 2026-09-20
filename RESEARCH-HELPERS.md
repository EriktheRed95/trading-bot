# Research helpers: five advisory readers beside the frozen strategy

A paper-only companion layer. Five deterministic helpers read the same completed
session the existing Strategy C paper core just read, write down what they saw
with full provenance, and keep two extra virtual books so the advisory rule can
be measured forward instead of asserted. Open the dashboard and use the
**Research helpers** tab.

The helpers cannot change a core target, a core fill, the accounting or the
baseline books. There is no order route and no live execution switch. They are
plain rules in code: no language model is called, per tick or otherwise, so
nothing has to be left running.

> **Where this exists.** This layer lives in this copy of the repository. It is
> not installed in the already-running dashboard, which still has four tabs, no
> research endpoint and no research database. Installing it is the deliberate
> step described under Files.

## The five helpers

| Helper | What it measures | What it can propose |
|---|---|---|
| Unusual volume and activity | The observed session's share volume against the median of the 60 sessions before it, plus how many of those prior sessions were as heavy | Flag a name for entry review when the session is at least 3x, or at most a third of, its own median |
| Entry quality and timing | Where the close sits inside the 20-session high/low range, how far it is above its own 200-session average, the overnight gap, and the session range against its median | Flag a name as extended when it closes in the top decile of its range and more than 10% above its average |
| Independent signal and noise filter | On its own independent fetch: share of sessions printing no change, share opening exactly at the prior close, single-session moves beyond 50%, sub-dollar closes, missing sessions, and the gap against the price the strategy used | Exclude a name from advisory use and name every failure |
| Liquidity and visible depth | Listed names: median dollars actually traded per session. Crypto references: real visible resting size within 25 and 100 basis points of the mid, from a public exchange book | Flag a size review below a $20M median; report depth as observation only |
| Supervisor report | Coverage, freshness, the notes proposed, every abstention reason, and disagreements between helpers on the same name | Nothing. It reports, it does not overrule |

Thresholds are fixed in `research_roles.py` and were not searched against
outcomes. Several are inherited from failures this repository already found:
the stale-print and degenerate-open limits come from the overnight study's
`Open == prior Close` tickers, the 50% move cap and dollar floor from
`rerun_pit_filtered.py` and `thesis_intake.py`.

## What it deliberately does not do

**No score.** There is no confidence number anywhere. A score computed from a
threshold distance reads as information and carries none. A reading gives the
measurement, how many completed observations it came from, how old it is, and
the explicit reason when a helper declines to speak.

**No depth for listed names.** No equity quote or order-book feed is wired in,
so quoted spread and book depth read *Unavailable* for stocks and funds. Traded
dollar volume is history, not depth, and is never converted into one. No pool
depth is inferred from price, volume or a proxy fund anywhere.

**No forecast.** "Extended" describes where a close sits today. Nothing here
predicts a return or ranks names for purchase.

## How lookahead and replay are prevented

* Only completed sessions are read, on the core's own 16:15 New York convention.
* The helpers observe the session the **core** used. If the core is holding, or
  its session is missing from the research snapshot, the whole cycle abstains
  rather than choosing its own session.
* A session older than one already recorded is refused, so history cannot enter
  the forward record as if it had been decided live.
* A `(session, role, symbol)` reading is written once. A later cycle that sees
  the same session again fetches nothing and rewrites nothing.
* Refusals are appended to their own `holds` table, so a hold is never read as a
  reading and a later successful retry does not erase it.
* The shadow books use the frozen `PaperBook` unchanged, including its
  observed-time fill guard.
* A shadow signal is timed from the **research reading**, not from the core's
  earlier fetch, because the filtered target does not exist until the helpers have
  read the session. A bar that completed in between therefore cannot fill it. The
  core's fetch time is kept as provenance only.
* The reading's own timestamp is taken **after** every fetch and every
  measurement, not when the cycle began. Those differ by the whole fetch, and a
  fill guard reads the difference. The cycle start is recorded separately. The
  clock is injectable, an injected time still advances, and the production clock
  is clamped non-decreasing so a backwards system adjustment cannot rewind an
  instant that already gated a fill.
* Each cycle commits an immutable **prepared decision** before it touches a shadow
  book. A cycle interrupted anywhere is finished later from that stored decision,
  with the same targets, prices and observation time, and with no new data
  request, so a retry can never quietly substitute fresher data.
* The desk is only asked to observe a snapshot the core **accepted**. A snapshot
  the core rejected is not an observation and produces no advisory.

## Data intake and request budget

Two public sources, read-only GET requests, both cached on disk:

* Daily OHLCV for the 67-name core universe, one batched Yahoo request per cache
  window (4 hours, refreshed early only when the core's session is missing and
  the cache is over 20 minutes old).
* Coinbase Exchange public level-2 order books for four USD crypto references
  the hourly lab already tracks, cached 50 minutes.

Because a recorded session is never re-read, and a prepared decision is replayed
rather than recomputed, the real cost is **one batched download plus four order
books per completed session**. Extra dashboard polls and crash recovery add
nothing. Verify with `python research_evidence.py`, which prints the request
counters and then proves a repeat cycle spends none. Add `--offline --core-from
<saved evidence.json>` to replay a recorded decision against the cached panel with
no request at all.

SPY is not in the research universe: the helpers advise on holdings, not on the
regime gate, so the gate's own instrument is not integrity-checked. Adding it is a
reasonable next step and would invalidate the current cached snapshot.

## The shadow evaluation

Two virtual books, both starting the day the helpers first observed:

* `shadow-baseline` takes the strategy's target verbatim.
* `shadow-filtered` drops the names a helper flagged and holds that share as
  cash. Redistributing it would be a different strategy.

They are compared only with each other, never with the core book, whose record
is longer. Neither is real money and neither adds capital to the core. A useful
comparison needs many sessions; a short record shows nothing.

The comparison uses the **intersection of the sessions both books actually
observed**, reported at the latest shared session. A count of observations would
not establish that: a cycle that updated one book and died leaves the other
behind, and comparing each book's own latest mark would invent a difference no
shared session supports. Any session only one book recorded is named and
excluded.

Freshness is measured from the conservative 13:00 New York completion bound the
paper accounting uses, not the closing bell, so it is an **upper** bound on the
data's age: "at most 17.8 hours old", about three hours less on an ordinary
session. It is measured to the moment the reading was complete, not to the moment
its cycle started. Both instants are recorded exactly and shown in the panel.

## Files

| File | Role |
|---|---|
| `research_roles.py` | The five helpers as pure functions. No I/O, no clock, no state |
| `research_intake.py` | Cached public OHLCV and order-book intake with provenance |
| `research_ledger.py` | Append-only store: prepared decisions, advisories, digests, holds |
| `research_desk.py` | Injectable clock, session alignment, prepared decisions, shadow books, UI payload |
| `test_research_helpers.py` | 58 offline tests |
| `research_evidence.py` | One cycle into an isolated evidence directory, live or fully offline |
| `research_selfcheck.py` | Offline audit of a saved desk directory |
| `research_summarize.py` | Plain table from a saved evidence file |
| `research_ui_check.py` | Syntax and wiring check for the dashboard section |
| `research_volume_check.py` | Measures how the feed treats volume across a split |

Two existing files gained additive hooks only: `trading_app.py` (an optional
`desk`, one read-only `GET /api/research-desk`, pause participation, and a
`--no-research-helpers` flag) and `trading_ui.html` (the new tab). With no desk
attached, both behave exactly as before, which the tests assert.

## Running and verifying

```
python main.py                                  # dashboard with the helpers enabled
python main.py --no-research-helpers            # dashboard without them
python -B -m unittest test_research_helpers     # 58 offline tests
python -B -m unittest test_unified test_accounting test_observation_guard test_research_helpers
python research_evidence.py                     # one real cycle, isolated directory
python research_summarize.py                    # read the saved evidence
python research_ui_check.py                     # dashboard section wiring
```

State lives in `runtime/research-v1/` when the app runs it, and in
`runtime/research-evidence/` for evidence collection. Neither touches
`runtime/paper.sqlite3` or `runtime/hourly-v1/`.
