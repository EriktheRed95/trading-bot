"""Build the unified trading dashboard.

ONE DESIGN RULE GOVERNS THIS FILE: every panel carries an evidence tier on its
face, and the tiers do not look alike. A video-derived idea must never render at
the same visual weight as a backtested one, because putting them side by side at
equal weight is how an unvalidated claim borrows the credibility of a validated
one, and how an idea this repo already killed gets quietly rebuilt.

    TIER 1  VALIDATED             backtested here, with survivorship controls
    TIER 2  TESTED AND REJECTED   the graveyard, kept visible on purpose
    TIER 3  UNTESTED HYPOTHESIS   plausible, no evidence yet, backtest path given
    TIER 4  NOT MECHANIZABLE      cannot be written down as rules

Tier 2 sits high on the page rather than in a footer. Most dashboards show only
what works, which is exactly why disproven ideas creep back in.

The dashboard is READ ONLY and observational. It places no orders, it recommends
no sizing, and it is not a step toward live trading: it reads DRY_RUN out of
main.py and reports what it finds rather than asserting a state.

Numbers come from two places, never from typing:
  - live market data, fetched at build time (the macro panel, the regime gate)
  - overnight_results.json, written by overnight_vs_intraday.py
Static repo results are labeled with the file and script that reproduce them.

Usage:  python build_dashboard.py [--out DIR] [--no-fetch]
"""
import argparse
import json
import re
import warnings
from datetime import datetime
from pathlib import Path

import pandas as pd
import yfinance as yf

warnings.filterwarnings('ignore')

REPO = Path(__file__).resolve().parent
DEFAULT_OUT = Path.home() / "Documents" / "CoworkOS" / "Trading Dashboard"

# Macro panel definitions. The video that suggested this panel defined no
# thresholds at all, so these are OUR choices and the dashboard says so.
MACRO_LOOKBACK = 60          # trading days, about one quarter
MACRO_NEUTRAL_PCT = 2.0      # moves smaller than this read as flat
YIELD_NEUTRAL_BPS = 25.0     # yields move in basis points, not percent


# ----------------------------------------------------------------- live inputs

def read_dry_run():
    """Read the safety lock out of main.py instead of claiming a value for it."""
    try:
        src = (REPO / "main.py").read_text(encoding="utf-8")
    except OSError:
        return None, "main.py could not be read"
    m = re.search(r'^DRY_RUN\s*=\s*(True|False)', src, re.M)
    if not m:
        return None, "no DRY_RUN assignment found in main.py"
    return m.group(1) == "True", f"main.py line {src[:m.start()].count(chr(10)) + 1}"


def fetch_live():
    """Gold, oil, the 10-year yield, and the SPY 200-day regime gate."""
    tickers = ['GC=F', 'CL=F', '^TNX', 'SPY', 'MP', 'USAR']
    data = yf.download(tickers, period='2y', interval='1d',
                       auto_adjust=True, progress=False)
    close = data['Close'] if isinstance(data.columns, pd.MultiIndex) else data[['Close']]
    close.index = pd.to_datetime(close.index).tz_localize(None)
    close = close.sort_index()

    out = {'asof': str(close.index[-1].date()), 'series': {}}
    for t in tickers:
        if t not in close.columns:
            continue
        s = close[t].dropna()
        if len(s) < MACRO_LOOKBACK + 1:
            continue
        last, prior = float(s.iloc[-1]), float(s.iloc[-1 - MACRO_LOOKBACK])
        out['series'][t] = {
            'last': last,
            'pct_60d': (last / prior - 1.0) * 100.0,
            'abs_60d': last - prior,          # basis points for ^TNX
            'n': len(s),
        }

    spy = close['SPY'].dropna()
    sma200 = spy.rolling(200).mean()
    out['regime'] = {
        'spy': float(spy.iloc[-1]),
        'sma200': float(sma200.iloc[-1]),
        'above': bool(spy.iloc[-1] > sma200.iloc[-1]),
        'pct_from_sma': float(spy.iloc[-1] / sma200.iloc[-1] - 1.0) * 100.0,
    }
    return out


def direction(value, neutral):
    if value > neutral:
        return 'rising'
    if value < -neutral:
        return 'falling'
    return 'flat'


def macro_reading(live):
    """Turn the raw moves into the video's 2x2. The MEASUREMENTS are facts; the
    INTERPRETATION below them is an untested hypothesis and is labeled as one."""
    s = live['series']
    gold = direction(s['GC=F']['pct_60d'], MACRO_NEUTRAL_PCT) if 'GC=F' in s else 'unknown'
    oil = direction(s['CL=F']['pct_60d'], MACRO_NEUTRAL_PCT) if 'CL=F' in s else 'unknown'
    # ^TNX is quoted as a percentage yield, so a move is basis points, not a
    # percent change: 4.00 -> 4.40 is +40bp, not +10%.
    yld = direction(s['^TNX']['abs_60d'] * 100.0, YIELD_NEUTRAL_BPS) if '^TNX' in s else 'unknown'

    cell = (oil, yld)
    severe = cell == ('rising', 'falling')
    return {'gold': gold, 'oil': oil, 'yield': yld, 'severe': severe,
            'yield_bps': s['^TNX']['abs_60d'] * 100.0 if '^TNX' in s else None}


# ------------------------------------------------------------------- the panels

def build_panels(live, macro, on_res):
    """Every panel, each stamped with its tier. Order is deliberate."""
    ov = {r['label']: r for r in on_res.get('universe_rows', [])} if on_res else {}
    ov_single = ({r['label']: r for r in on_res['single_name']['rows']}
                 if on_res and 'single_name' in on_res else {})

    def g(d, k, f):
        return d.get(k, {}).get(f)

    panels = []

    # ---------------------------------------------------------- TIER 1
    panels.append(dict(
        tier=1, title="Strategy C: trend-following, regime-gated allocator",
        subtitle="The only strategy in this stack with earned authority.",
        body=[
            "Long only. Holds by default and sits out when the regime turns. "
            "Rebalances monthly, with costs charged on turnover.",
        ],
        rules=[
            "Market regime gate: leave equities when SPY is below its 200-day "
            "simple moving average (SMA).",
            "Per-name trend filter: eligible only if above its own 200-day SMA "
            "and 12-month-minus-1-month momentum is positive.",
            "Ranking: z-score blend of 12-1 momentum, 6-1 momentum, and trend "
            "strength (percent above the 200-day SMA).",
            "Selection: hold the top N. Sizing: inverse volatility, so each "
            "holding contributes roughly equal risk.",
            "Risk-off sleeve, dynamic: hold gold or long Treasuries only while "
            "each is itself above its own 200-day SMA, otherwise cash.",
        ],
        metrics=[
            ("Strategy C, dynamic risk-off", "23.2%", "1.09", "-26%", "0.89"),
            ("Strategy C, cash sleeve", "20.9%", "1.03", "-28%", "0.74"),
            ("Equal-weight hold, same 65 names", "19.8%", "0.99", "-49%", "0.40"),
            ("SPY buy and hold", "10.8%", "0.64", "-55%", "0.20"),
        ],
        metric_note="65-name pool, 1993 to 2026. It beats equal-weight holding "
                    "of the same names, so the edge is the strategy and not the "
                    "stock list.",
        robust="Across every universe tested: Sharpe roughly 1.1 to 1.25 against "
               "SPY's 0.64, and drawdowns of -26% to -35% against SPY's -55%. "
               "The honest headline is not large returns. It is about half the "
               "drawdown at nearly double the Sharpe ratio.",
        source="strategy_c.py, README.md. Reproduce: python strategy_c.py",
    ))

    panels.append(dict(
        tier=1, title="Survivorship discipline",
        subtitle="The control that makes every other number here believable.",
        body=[
            "Point-in-time index membership is reconstructed from the change log "
            "so the allocator may only pick names that were actually in the index "
            "on that date.",
        ],
        metrics=[
            ("Today's S&P 500 run backwards", "40.6%", "", "", "a trap"),
            ("Point-in-time membership", "32.2%", "", "", "honest"),
        ],
        metric_note="Removing selection hindsight erased about 8 points of fake "
                    "compound annual growth rate (CAGR). 682 of 867 ever-members "
                    "could be priced (79%); the missing 21% are delisted names "
                    "that free data will not serve.",
        source="run_sp500_pit.py against run_sp500.py",
    ))

    # ---------------------------------------------------------- TIER 2
    rejected = [
        dict(
            title="Mean reversion as a strategy",
            killer="Beat buy-and-hold on 2 of 14 names hourly over 2 years, and "
                   "0 of 14 daily over 10 years. It fights trends.",
            note="This is the finding the whole repo pivoted on. Strategy C "
                 "exists because mean reversion lost.",
            source="backtest_engine.py, algo_*.py",
        ),
        dict(
            title="Trailing stops, position caps, and sector caps",
            killer="No overlay beat the baseline. A naive 20% trailing stop made "
                   "drawdown WORSE through whipsaw and V-shaped recoveries. A "
                   "disaster-only stop (200-day break and -35%) was a no-op. "
                   "A 15% per-name cap and a 30% sector cap did not help either.",
            note="The regime gate, the per-name trend filter, and diversified "
                 "inverse-volatility sizing already do this job. The strategy is "
                 "not missing a safety layer, it is one.",
            source="run_overlay.py, run_overlay_smart.py",
        ),
        dict(
            title="News sentiment as an alpha source",
            killer="Looked strong on the curated mega-cap pool (Sharpe 1.04 to "
                   "1.15). On the broad point-in-time universe the return edge "
                   "collapsed: CAGR 16.5% to 15.6%. The apparent edge was a size "
                   "and curation artifact.",
            note="Not a total loss. The signal is real and momentum-independent, "
                 "and a small drawdown gain survived (-33% to -28%). It is a minor "
                 "risk tilt at most, not alpha.",
            source="run_sentiment_validate.py, run_sentiment_pit_ab.py",
        ),
        dict(
            title="The bare-percentile concept-drift WATCH rule",
            killer="No predictive value. Median permutation p-value 0.647, 0% of "
                   "phase offsets reach p below 0.05, and 57% point the wrong way. "
                   "Flags did not precede weakness.",
            note="Careful with the scope of this one. The NEWER calibrated rule "
                 "was not disproven, it is untestable on a single backtest curve. "
                 "It appears below under untested hypotheses.",
            source="concept_drift.py --validate",
        ),
        dict(
            title="Fixing survivorship with today's index membership",
            killer="Running the allocator on today's S&P 500 produced a fake 40.6% "
                   "CAGR. The list is secretly pre-loaded with future winners.",
            note="Only point-in-time membership is honest, and it cut the number "
                 "to 32.2%.",
            source="run_sp500.py, the cautionary version",
        ),
    ]

    if ov:
        be = on_res.get('breakeven_bps')
        drag = on_res.get('annual_cost_drag_pct')
        rejected.insert(0, dict(
            title="Buy at the close and sell at the open, as a retail strategy",
            fresh=True,
            killer=(
                f"Tested here for the first time, and it does not survive costs. "
                f"Gross, the effect is REAL: {g(ov,'Overnight only  (ZERO costs)','cagr'):.1f}% CAGR "
                f"at Sharpe {g(ov,'Overnight only  (ZERO costs)','sharpe'):.2f} with a "
                f"{g(ov,'Overnight only  (ZERO costs)','max_drawdown'):.0f}% drawdown, against SPY's "
                f"{g(ov,'SPY buy and hold (net)','cagr'):.1f}% at Sharpe "
                f"{g(ov,'SPY buy and hold (net)','sharpe'):.2f}. Net of this repo's own "
                f"cost model it is {g(ov,'Overnight only  (net, 6bp/side)','cagr'):.1f}% CAGR, "
                f"a total loss."),
            note=(
                f"A daily round trip pays the spread about 504 times a year, which "
                f"is {drag:.0f}% of capital annually at 6 basis points per side. Buy "
                f"and hold pays it twice, ever. The overnight leg only beats SPY "
                f"below roughly 0.4 basis points per side and turns outright "
                f"negative above {be} basis points. That is a market-maker cost "
                f"structure, not a retail one."),
            caveat=(
                "Survivorship position, stated rather than buried. This ran on "
                "the 65-name pool, which is controlled by construction but not "
                "fully clean, and the gross numbers are therefore probably "
                "flattered. That bias runs in favor of this conclusion, not "
                "against it: a cleaner universe would lower the gross line while "
                "the cost drag stayed exactly where it is, so the rejection only "
                "gets stronger. The cost arithmetic itself does not depend on the "
                "universe at all. Run --universe pit to confirm on point-in-time "
                "membership."),
            source="overnight_vs_intraday.py. Reproduce: python "
                   "overnight_vs_intraday.py --json overnight_results.json",
        ))

    rejected.append(dict(
        title="The multi-asset bot's two centerpieces",
        killer="Both were already dead here. Its signal engine is mean reversion "
               "at -2.3 standard deviations below a 20-period moving average, and "
               "its risk control is a hard 1% stop. See the first two entries "
               "above.",
        note="The video showed no backtest, no track record, no drawdown, and no "
             "Sharpe ratio. Its average-true-range sizing is not rejected, it is "
             "simply not new: inverse-volatility sizing already does the same job. "
             "Its 1% account risk figure of $480 implies a $48,000 account, which "
             "is the video author's, not a sizing recommendation for anyone.",
        source="Cross-referenced against this repo's own results",
    ))

    for r in rejected:
        panels.append(dict(tier=2, **r))

    # ---------------------------------------------------------- TIER 3
    panels.append(dict(
        tier=3, title="Macro regime read: gold, oil, and the 10-year yield",
        subtitle="Live measurements, untested interpretation.",
        macro=True,
        body=[
            "The source video offered no entries, no exits, no stops, and no "
            "sizing. It is a way of reading the tape, so it is built here as a "
            "regime panel and not as a strategy.",
            "The claimed logic: gold rising means markets are pricing FUTURE "
            "risk, while gold falling can mean risk is actively occurring and "
            "holders are liquidating to meet margin calls. Oil rising raises the "
            "odds of conflict continuing. Yields rising price inflation or "
            "growth; falling yields price recession.",
            "The one combination the video treats as the real insight: OIL "
            "RISING WHILE YIELDS FALL, meaning energy stress and recession being "
            "priced at the same time.",
        ],
        backtest_path=[
            "Classify every month since 1990 into the 2x2 of oil direction by "
            "yield direction using the thresholds stated on this panel.",
            "Measure forward 1, 3, and 6 month SPY returns and drawdowns per "
            "cell. The question is whether the oil-up/yields-down cell actually "
            "precedes worse outcomes.",
            "Apply the repo's own standard from concept_drift.py --validate: use "
            "non-overlapping windows, test each phase offset separately, never "
            "pool, and report a permutation p-value.",
            "Falsify it the same way the WATCH rule was falsified. If the median "
            "permutation p-value does not clear 0.05, this panel stays a "
            "thermometer and never touches position sizing.",
        ],
        source="Video derived. No backtest exists yet.",
    ))

    panels.append(dict(
        tier=3, title="Correlation filter on concurrent risk-on positions",
        subtitle="The one genuinely good idea in the multi-asset video.",
        body=[
            "The claim: when two selected names are effectively the same bet, "
            "holding both is one position wearing two tickers. The example given "
            "was blocking an additional risk-on position when the S&P 500 index "
            "(SPX) and the Nasdaq 100 index (NDX) are both long at 0.94 "
            "correlation, while still allowing gold at -0.12.",
            "That is real portfolio thinking rather than a signal, which is why "
            "it is the only part of that video worth keeping.",
        ],
        prior="Be skeptical before testing. This is a close relative of the "
              "sector cap, since sector membership is a crude correlation proxy, "
              "and both the 30% sector cap and the 15% per-name cap were tested "
              "in run_overlay.py and did not beat the baseline. Inverse-volatility "
              "sizing across 10 names may already capture most of the benefit. "
              "The honest prior is that this fails too.",
        backtest_path=[
            "Add a correlation screen in strategy_c.run_allocator at the point "
            "where top-N weights are set, after ranking and before sizing.",
            "Compute the trailing 60-day correlation matrix of daily returns "
            "among the selected names. Walk the ranking from the top: when a "
            "candidate exceeds a correlation threshold against an already-held "
            "name, skip it and take the next eligible name instead.",
            "Sweep the threshold from 0.70 to 0.95. A result that only works at "
            "one value is a fitted artifact, not an effect.",
            "Compare Sharpe, max drawdown, and Calmar against the unfiltered "
            "baseline on the 65-name pool AND on the point-in-time S&P run, "
            "because that is the test that killed the sentiment tilt.",
            "Falsification rule set in advance: if Sharpe and max drawdown do "
            "not BOTH improve across most of the threshold range and on both "
            "universes, it joins the rejected list above.",
        ],
        source="Video derived. No backtest exists yet.",
    ))

    panels.append(dict(
        tier=3, title="The calibrated concept-drift rule",
        subtitle="Not disproven. Untestable on the data available.",
        body=[
            "The replacement for the rejected bare-percentile rule requires the "
            "percentile to stay below its cutoff for K consecutive windows, with "
            "K measured against history until the whole rule fires on at most a "
            "target rate. On Strategy C, K of 23 yields a 4.9% fire rate.",
            "Calibrating to that rarity leaves roughly 6 independent flagged "
            "observations, which no test can separate from noise. The validator "
            "reports UNDERPOWERED rather than inventing a verdict.",
        ],
        backtest_path=[
            "Rarity and testability trade off directly against each other on a "
            "single 32-year curve. More data is the only unblock.",
            "Feed a real live equity curve into split mode. Single-curve mode is "
            "descriptive by construction and cannot detect live-versus-backtest "
            "divergence, which is what concept drift means.",
            "Until then it earns no right to change position sizing.",
        ],
        source="concept_drift.py, README.md",
    ))

    # ---------------------------------------------------------- TIER 4
    panels.append(dict(
        tier=4, title="Session sweep and fair value gap reversal",
        subtitle="Recorded here so it does not get proposed again.",
        body=[
            "The method as stated: mark the Asia and London session highs and "
            "lows, wait for a sweep of one, drop to the 1-minute chart, find a "
            "fair value gap reversal, place the stop at the swing low, and target "
            "the next draw on liquidity.",
        ],
        why=[
            "Every load-bearing term is undefined. What counts as a sweep rather "
            "than an ordinary breach is not specified.",
            "Which fair value gap qualifies, and how large it must be, is not "
            "specified.",
            "Which swing low, over what lookback, is not specified.",
            "The next draw on liquidity is identified by eye after the fact.",
            "Each decision is discretionary, so the same chart yields different "
            "trades for different readers, and there is nothing to backtest. "
            "A rule you cannot write down is a rule you cannot falsify.",
        ],
        source="Video derived. Not implementable as stated.",
    ))

    # ---------------------------------------------------------- watchlist
    s = live['series']
    panels.append(dict(
        tier=5, title="Watchlist: rare earths",
        subtitle="A thesis to research, not dashboard logic.",
        body=[
            "The pitch was United States supply-chain reshoring in rare earth "
            "elements, naming MP Materials (MP) and USA Rare Earth (USAR). It is "
            "a narrative, not a strategy: it contains no rules.",
            "Deliberately no verdict is computed here. Prices below are raw "
            "observation so the names are not invisible, and nothing more.",
        ],
        watch=[(t, s[t]['last'], s[t]['pct_60d']) for t in ('MP', 'USAR') if t in s],
        actions=[
            "Run the fundamental-check skill on MP and USAR for the quality and "
            "valuation read.",
            "Run the financial-researcher skill for the filings-level memo.",
            "Run the trade-identifier skill if you want the Strategy C rules "
            "verdict, which is the only verdict in this stack with a backtest "
            "behind it.",
        ],
        source="Video derived. Route through the existing skills.",
    ))

    return panels


# ------------------------------------------------------------------- rendering

TIERS = {
    1: ("VALIDATED", "validated", "Backtested in this repo, with survivorship controls."),
    2: ("TESTED AND REJECTED", "rejected", "Already disproven here. Do not rebuild."),
    3: ("UNTESTED HYPOTHESIS", "untested", "Plausible. No evidence yet. Backtest path given."),
    4: ("NOT MECHANIZABLE", "nomech", "Cannot be written down as falsifiable rules."),
    5: ("WATCHLIST", "watch", "Research queue. Not dashboard logic."),
}

CSS = """
*{box-sizing:border-box}
:root{
  color-scheme:light;
  --plane:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --axis:#c3c2b7; --ring:rgba(11,11,11,0.10);
  --good:#0ca30c; --warning:#fab219; --serious:#ec835a; --critical:#d03b3b;
  --s1:#2a78d6; --s2:#eb6834;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  color-scheme:dark;
  --plane:#0d0d0d; --surface:#1a1a19; --ink:#fff; --ink2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --axis:#383835; --ring:rgba(255,255,255,0.10);
  --s1:#3987e5; --s2:#d95926;
}}
:root[data-theme="dark"]{
  color-scheme:dark;
  --plane:#0d0d0d; --surface:#1a1a19; --ink:#fff; --ink2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --axis:#383835; --ring:rgba(255,255,255,0.10);
  --s1:#3987e5; --s2:#d95926;
}
body{margin:0;background:var(--plane);color:var(--ink);
  font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif;}
.wrap{max-width:980px;margin:0 auto;padding:32px 20px 72px}
h1{font-size:1.7rem;margin:0 0 6px;letter-spacing:-.02em}
.sub{color:var(--ink2);margin:0 0 22px;font-size:.95rem}

.safety{display:flex;gap:12px;align-items:flex-start;background:var(--surface);
  border:1px solid var(--ring);border-left:5px solid var(--good);
  border-radius:10px;padding:14px 16px;margin:0 0 14px}
.safety.bad{border-left-color:var(--critical)}
.safety b{display:block;margin-bottom:2px}
.safety p{margin:0;color:var(--ink2);font-size:.9rem}

.legend{background:var(--surface);border:1px solid var(--ring);border-radius:10px;
  padding:14px 16px;margin:0 0 30px}
.legend h2{font-size:.78rem;letter-spacing:.09em;text-transform:uppercase;
  color:var(--muted);margin:0 0 10px;font-weight:700}
.legend ul{margin:0;padding:0;list-style:none;display:grid;gap:8px}
.legend li{display:flex;gap:10px;align-items:baseline;font-size:.87rem;color:var(--ink2)}

.badge{display:inline-block;font-size:.66rem;font-weight:800;letter-spacing:.1em;
  text-transform:uppercase;padding:3px 8px;border-radius:4px;white-space:nowrap;
  border:1px solid transparent}
.b-validated{background:var(--good);color:#fff}
.b-rejected{background:var(--critical);color:#fff}
.b-untested{background:transparent;color:#8a6100;border-color:var(--warning)}
.b-nomech{background:transparent;color:var(--muted);border-color:var(--axis)}
.b-watch{background:transparent;color:var(--ink2);border-color:var(--axis)}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]) .b-untested{color:var(--warning)}}
:root[data-theme="dark"] .b-untested{color:var(--warning)}

section{margin:0 0 34px}
section>h2{font-size:.78rem;letter-spacing:.09em;text-transform:uppercase;
  color:var(--muted);margin:0 0 4px;font-weight:700}
section>p.secnote{margin:0 0 14px;color:var(--ink2);font-size:.87rem;max-width:74ch}

/* Visual weight is the whole point: tier 1 is solid and loud, tier 3 and 4 are
   dashed, lighter, smaller. An untested idea must never look validated. */
.card{background:var(--surface);border:1px solid var(--ring);border-radius:10px;
  padding:18px 20px;margin:0 0 14px}
.card h3{margin:10px 0 2px;letter-spacing:-.01em}
.card .st{color:var(--ink2);font-size:.88rem;margin:0 0 10px}
.card p{margin:0 0 10px;color:var(--ink2);font-size:.92rem;max-width:74ch}
.card .src{font-size:.78rem;color:var(--muted);margin:12px 0 0;
  padding-top:10px;border-top:1px solid var(--grid);font-family:ui-monospace,monospace}

.t-validated{border-left:6px solid var(--good)}
.t-validated h3{font-size:1.3rem;font-weight:750}
.t-rejected{border-left:6px solid var(--critical)}
.t-rejected h3{font-size:1.08rem;font-weight:700}
.t-untested{border:2px dashed var(--warning);border-left-width:6px;opacity:.95}
.t-untested h3{font-size:1rem;font-weight:650}
.t-nomech{border:2px dashed var(--axis);border-left-width:6px;opacity:.88}
.t-nomech h3{font-size:.98rem;font-weight:650}
.t-watch{border:2px dashed var(--axis);border-left-width:6px;opacity:.92}
.t-watch h3{font-size:.98rem;font-weight:650}

.evbar{display:flex;gap:3px;margin:0 0 2px}
.evbar i{height:4px;width:26px;border-radius:2px;background:var(--grid)}
.evbar i.on{background:var(--good)}
.evbar.rej i.on{background:var(--critical)}
.evbar.uns i{border:1px dashed var(--warning);background:transparent;height:6px}

ul.rules,ul.why,ul.path,ul.acts{margin:6px 0 10px;padding-left:20px;
  color:var(--ink2);font-size:.9rem}
ul.rules li,ul.why li,ul.path li,ul.acts li{margin:0 0 5px;max-width:72ch}
.killer{background:rgba(208,59,59,.07);border-radius:7px;padding:11px 13px;
  margin:0 0 10px;font-size:.92rem;color:var(--ink)}
.killer b{color:var(--critical)}
.pathbox{border:1px dashed var(--warning);border-radius:7px;padding:11px 13px;margin:10px 0 0}
.pathbox h4,.priorbox h4{margin:0 0 6px;font-size:.72rem;letter-spacing:.09em;
  text-transform:uppercase;color:var(--muted);font-weight:700}
.priorbox{background:rgba(250,178,25,.09);border-radius:7px;padding:11px 13px;margin:0 0 10px;
  font-size:.9rem;color:var(--ink2)}
.caveat{border:1px solid var(--grid);border-radius:7px;padding:11px 13px;margin:10px 0 0;
  font-size:.87rem;color:var(--ink2)}
.caveat h4{margin:0 0 6px;font-size:.72rem;letter-spacing:.09em;text-transform:uppercase;
  color:var(--muted);font-weight:700}
.fresh{display:inline-block;font-size:.62rem;font-weight:800;letter-spacing:.08em;
  padding:2px 6px;border-radius:3px;background:var(--critical);color:#fff;margin-left:6px}

table{border-collapse:collapse;width:100%;margin:8px 0 6px;font-size:.88rem;
  font-variant-numeric:tabular-nums}
th,td{text-align:right;padding:6px 8px;border-bottom:1px solid var(--grid)}
th:first-child,td:first-child{text-align:left;font-variant-numeric:normal}
th{color:var(--muted);font-weight:600;font-size:.78rem;text-transform:uppercase;
  letter-spacing:.05em}
tr.hero td{font-weight:700;color:var(--ink)}
.note{font-size:.83rem;color:var(--muted);margin:2px 0 0;max-width:74ch}

.mgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;
  margin:12px 0}
.tile{background:var(--plane);border:1px solid var(--grid);border-radius:8px;padding:11px 13px}
.tile .k{font-size:.72rem;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
.tile .v{font-size:1.32rem;font-weight:700;margin:3px 0 1px}
.tile .d{font-size:.82rem;color:var(--ink2)}
.dir-rising::before{content:"\\2191 "}
.dir-falling::before{content:"\\2193 "}
.dir-flat::before{content:"\\2192 "}

.cell{border:1px solid var(--grid);border-radius:8px;padding:10px 12px;font-size:.85rem;
  background:var(--plane);color:var(--ink2)}
.cell b{display:block;color:var(--ink);font-size:.8rem;margin-bottom:3px}
.cell.here{border:2px solid var(--ink2);background:var(--surface)}
.cell.severe{border-color:var(--critical)}
.cell.here.severe{border:2px solid var(--critical);background:rgba(208,59,59,.08)}
.q2{display:grid;grid-template-columns:1fr 1fr;gap:9px;margin:10px 0}
.reading{margin:12px 0 0;font-size:.9rem;color:var(--ink2)}
.reading b{color:var(--ink)}
@media (max-width:620px){.q2{grid-template-columns:1fr}}
.alarm{background:rgba(208,59,59,.09);border:1px solid var(--critical);border-radius:7px;
  padding:11px 13px;margin:10px 0 0;font-size:.9rem}

.chartwrap{background:var(--surface);border:1px solid var(--grid);border-radius:8px;
  padding:12px;margin:12px 0;overflow-x:auto}
.lg{display:flex;gap:16px;flex-wrap:wrap;margin:0 0 8px;font-size:.82rem;color:var(--ink2)}
.lg span{display:flex;align-items:center;gap:6px}
.lg i{width:14px;height:3px;border-radius:2px;display:inline-block}
svg{display:block;max-width:100%;height:auto}
.tip{position:fixed;pointer-events:none;background:var(--surface);color:var(--ink);
  border:1px solid var(--ring);border-radius:6px;padding:6px 9px;font-size:.8rem;
  box-shadow:0 3px 12px rgba(0,0,0,.14);opacity:0;transition:opacity .1s;z-index:9}
circle.pt{transition:r .1s}
circle.pt:hover{r:7}
footer{margin-top:44px;padding-top:18px;border-top:1px solid var(--grid);
  color:var(--muted);font-size:.8rem}
footer p{max-width:74ch}
"""

TIP_JS = """
(function(){
 var t=document.createElement('div');t.className='tip';document.body.appendChild(t);
 document.querySelectorAll('circle.pt').forEach(function(c){
  c.addEventListener('mouseenter',function(e){
    t.textContent=c.getAttribute('data-tip');t.style.opacity=1;});
  c.addEventListener('mousemove',function(e){
    t.style.left=(e.clientX+13)+'px';t.style.top=(e.clientY-12)+'px';});
  c.addEventListener('mouseleave',function(){t.style.opacity=0;});
 });
})();
"""


def cost_chart(sweep, spy_cagr, xmax=6.0):
    """Line chart: compound annual growth rate as a function of cost per side.

    One y-axis, two series, both in the same unit. Direct labels plus a legend,
    a zero baseline, and the SPY reference line that is the real bar to clear.
    """
    pts = [p for p in sweep if p['bps'] <= xmax]
    if not pts:
        return ""
    W, H = 720, 300
    L, R, T, B = 52, 118, 16, 38
    pw, ph = W - L - R, H - T - B
    ys = [p['overnight_cagr'] for p in pts] + [p['intraday_cagr'] for p in pts] + [0, spy_cagr]
    ymin, ymax = min(ys), max(ys)
    pad = (ymax - ymin) * .12 or 1
    ymin, ymax = ymin - pad, ymax + pad

    def X(v):
        return L + (v / xmax) * pw

    def Y(v):
        return T + (ymax - v) / (ymax - ymin) * ph

    def path(key):
        return " ".join(("M" if i == 0 else "L") + f"{X(p['bps']):.1f},{Y(p[key]):.1f}"
                        for i, p in enumerate(pts))

    o = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Compound annual '
         f'growth rate against trading cost per side">']
    # gridlines and y ticks
    step = 5
    tick = int(ymin // step) * step
    while tick <= ymax:
        if ymin <= tick <= ymax:
            y = Y(tick)
            o.append(f'<line x1="{L}" y1="{y:.1f}" x2="{L+pw}" y2="{y:.1f}" '
                     f'stroke="var(--grid)" stroke-width="1"/>')
            o.append(f'<text x="{L-8}" y="{y+3.5:.1f}" text-anchor="end" font-size="11" '
                     f'fill="var(--muted)">{tick:g}%</text>')
        tick += step
    # the region where overnight actually wins
    win = [p['bps'] for p in pts if p['overnight_cagr'] > spy_cagr]
    if win:
        o.append(f'<rect x="{L}" y="{T}" width="{X(max(win))-L:.1f}" height="{ph}" '
                 f'fill="var(--good)" opacity="0.07"/>')
        o.append(f'<text x="{X(max(win))+5:.1f}" y="{T+13}" font-size="10.5" '
                 f'fill="var(--muted)">only region where overnight beats SPY</text>')
    # SPY reference and zero
    o.append(f'<line x1="{L}" y1="{Y(spy_cagr):.1f}" x2="{L+pw}" y2="{Y(spy_cagr):.1f}" '
             f'stroke="var(--ink2)" stroke-width="1.5" stroke-dasharray="5 4"/>')
    o.append(f'<text x="{L+pw+6}" y="{Y(spy_cagr)+3.5:.1f}" font-size="11" '
             f'fill="var(--ink2)">SPY hold {spy_cagr:.1f}%</text>')
    o.append(f'<line x1="{L}" y1="{Y(0):.1f}" x2="{L+pw}" y2="{Y(0):.1f}" '
             f'stroke="var(--axis)" stroke-width="1.5"/>')
    # series
    for key, col, lab in (('overnight_cagr', 'var(--s1)', 'Overnight'),
                          ('intraday_cagr', 'var(--s2)', 'Intraday')):
        o.append(f'<path d="{path(key)}" fill="none" stroke="{col}" stroke-width="2" '
                 f'stroke-linejoin="round" stroke-linecap="round"/>')
        last = pts[-1]
        o.append(f'<text x="{X(last["bps"])+7:.1f}" y="{Y(last[key])+4:.1f}" font-size="11.5" '
                 f'font-weight="650" fill="{col}">{lab}</text>')
        for p in pts:
            o.append(
                f'<circle class="pt" cx="{X(p["bps"]):.1f}" cy="{Y(p[key]):.1f}" r="4" '
                f'fill="{col}" stroke="var(--surface)" stroke-width="2" '
                f'data-tip="{p["bps"]:g} bp per side: {lab} {p[key]:.1f}% CAGR"/>')
    # x axis
    for p in pts:
        o.append(f'<text x="{X(p["bps"]):.1f}" y="{T+ph+16}" text-anchor="middle" '
                 f'font-size="10.5" fill="var(--muted)">{p["bps"]:g}</text>')
    o.append(f'<text x="{L+pw/2:.0f}" y="{H-6}" text-anchor="middle" font-size="11" '
             f'fill="var(--ink2)">cost per side (basis points)</text>')
    o.append('</svg>')
    return "".join(o)


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def render_card(p, on_res, spy_cagr):
    label, cls, _ = TIERS[p['tier']]
    o = [f'<article class="card t-{cls}">']
    bar = {1: '<span class="evbar"><i class="on"></i><i class="on"></i><i class="on"></i></span>',
           2: '<span class="evbar rej"><i class="on"></i><i class="on"></i><i class="on"></i></span>',
           3: '<span class="evbar uns"><i></i><i></i><i></i></span>',
           4: '<span class="evbar uns"><i></i><i></i><i></i></span>',
           5: '<span class="evbar uns"><i></i><i></i><i></i></span>'}[p['tier']]
    o.append(bar)
    o.append(f'<div><span class="badge b-{cls}">{label}</span>'
             + ('<span class="fresh">NEW THIS BUILD</span>' if p.get('fresh') else '')
             + '</div>')
    o.append(f'<h3>{esc(p["title"])}</h3>')
    if p.get('subtitle'):
        o.append(f'<p class="st">{esc(p["subtitle"])}</p>')
    if p.get('killer'):
        o.append(f'<div class="killer"><b>Why it was rejected:</b> {esc(p["killer"])}</div>')
    for para in p.get('body', []):
        o.append(f'<p>{esc(para)}</p>')
    if p.get('rules'):
        o.append('<ul class="rules">' + "".join(f'<li>{esc(r)}</li>' for r in p['rules']) + '</ul>')
    if p.get('metrics'):
        o.append('<table><thead><tr><th>Variant</th><th>CAGR</th><th>Sharpe</th>'
                 '<th>Max DD</th><th>Calmar</th></tr></thead><tbody>')
        for i, row in enumerate(p['metrics']):
            hero = ' class="hero"' if i == 0 else ''
            o.append(f'<tr{hero}>' + "".join(f'<td>{esc(c)}</td>' for c in row) + '</tr>')
        o.append('</tbody></table>')
    if p.get('metric_note'):
        o.append(f'<p class="note">{esc(p["metric_note"])}</p>')
    if p.get('robust'):
        o.append(f'<p><strong>{esc(p["robust"])}</strong></p>')
    if p.get('macro'):
        o.append(render_macro(p))
    if p.get('chart'):
        o.append(p['chart'])
    if p.get('watch'):
        o.append('<div class="mgrid">')
        for t, last, pct in p['watch']:
            o.append(f'<div class="tile"><div class="k">{esc(t)}</div>'
                     f'<div class="v">${last:,.2f}</div>'
                     f'<div class="d">{pct:+.1f}% over {MACRO_LOOKBACK} sessions</div></div>')
        o.append('</div>')
    if p.get('prior'):
        o.append(f'<div class="priorbox"><h4>Prior before testing</h4>{esc(p["prior"])}</div>')
    if p.get('why'):
        o.append('<ul class="why">' + "".join(f'<li>{esc(w)}</li>' for w in p['why']) + '</ul>')
    if p.get('backtest_path'):
        o.append('<div class="pathbox"><h4>Backtest path to promote or kill it</h4><ul class="path">'
                 + "".join(f'<li>{esc(s)}</li>' for s in p['backtest_path']) + '</ul></div>')
    if p.get('actions'):
        o.append('<ul class="acts">' + "".join(f'<li>{esc(a)}</li>' for a in p['actions']) + '</ul>')
    if p.get('note'):
        o.append(f'<p class="note">{esc(p["note"])}</p>')
    if p.get('caveat'):
        o.append(f'<div class="caveat"><h4>Where this study is weak</h4>'
                 f'{esc(p["caveat"])}</div>')
    o.append(f'<p class="src">{esc(p["source"])}</p></article>')
    return "".join(o)


def render_macro(p):
    m, live = p['_macro'], p['_live']
    s = live['series']
    o = ['<div class="mgrid">']
    for tick, name, val, sub in (
            ('GC=F', 'Gold', f"{s['GC=F']['last']:,.0f}",
             f"{s['GC=F']['pct_60d']:+.1f}% / {MACRO_LOOKBACK}d"),
            ('CL=F', 'Crude oil', f"{s['CL=F']['last']:,.2f}",
             f"{s['CL=F']['pct_60d']:+.1f}% / {MACRO_LOOKBACK}d"),
            ('^TNX', '10-year yield', f"{s['^TNX']['last']:.2f}%",
             f"{m['yield_bps']:+.0f} bp / {MACRO_LOOKBACK}d")):
        if tick not in s:
            continue
        d = {'GC=F': m['gold'], 'CL=F': m['oil'], '^TNX': m['yield']}[tick]
        o.append(f'<div class="tile"><div class="k">{name}</div><div class="v">{val}</div>'
                 f'<div class="d dir-{d}">{sub} ({d})</div></div>')
    o.append('</div>')
    o.append('<p class="note">Measurements above are facts. Everything below is '
             'the video\'s interpretation of them, which has never been tested. '
             'The video gave no thresholds at all, so "rising" and "falling" are '
             f'defined here as a move of more than {MACRO_NEUTRAL_PCT:g}% over '
             f'{MACRO_LOOKBACK} trading days, and more than {YIELD_NEUTRAL_BPS:g} '
             'basis points for the yield. Those cutoffs are our choice, and a '
             'different choice would move the panel into a different cell.</p>')

    cells = [(('rising', 'rising'), 'Oil up, yields up',
              'Energy stress with growth or inflation priced.'),
             (('rising', 'falling'), 'Oil up, yields down',
              'Claimed severe-downside combination: conflict continuing while '
              'recession is priced.'),
             (('falling', 'rising'), 'Oil down, yields up',
              'Growth priced without energy stress.'),
             (('falling', 'falling'), 'Oil down, yields down',
              'Disinflation or demand weakness.')]
    # A "flat" reading sits in no cell at all. Say so out loud rather than
    # rendering a grid with nothing lit and letting it look like an all-clear.
    off_grid = 'flat' in (m['oil'], m['yield'])
    o.append(f'<p class="reading"><b>Current reading:</b> oil {m["oil"]}, yields '
             f'{m["yield"]}, gold {m["gold"]}.'
             + (' One or both of oil and yields are inside the neutral band, so '
                'the grid below has no lit cell. That is an undefined reading, '
                'not a benign one.' if off_grid else '') + '</p>')
    o.append('<div class="q2">')
    for key, name, desc in cells:
        here = (m['oil'], m['yield']) == key
        sev = key == ('rising', 'falling')
        c = 'cell' + (' here' if here else '') + (' severe' if sev else '')
        tag = ' &lt;- current reading' if here else ''
        o.append(f'<div class="{c}"><b>{name}{tag}</b>{esc(desc)}</div>')
    o.append('</div>')
    if m['severe']:
        o.append('<div class="alarm"><b>The oil-up, yields-down cell is the current '
                 'reading.</b> Per the video this is the severe-downside combination. '
                 'It has never been backtested, it is not a signal, and it must not '
                 'change position sizing. Strategy C\'s 200-day gate above is the '
                 'only regime read here with evidence behind it.</div>')
    else:
        o.append('<p class="note">Not currently in the claimed severe-downside cell. '
                 'That is an observation, not an all-clear: this panel has no '
                 'demonstrated predictive value in any cell, including this one.</p>')
    return "".join(o)


def render_html(panels, live, macro, on_res, dry_run, dry_src):
    spy_cagr = 10.86
    if on_res:
        for r in on_res.get('universe_rows', []):
            if r['label'].startswith('SPY buy and hold'):
                spy_cagr = r['cagr']

    # attach live context and the chart to the panels that need them
    for p in panels:
        if p.get('macro'):
            p['_macro'], p['_live'] = macro, live
        if p['tier'] == 2 and p.get('fresh') and on_res:
            p['chart'] = (
                '<div class="chartwrap"><div class="lg">'
                '<span><i style="background:var(--s1)"></i>Overnight (buy close, sell open)</span>'
                '<span><i style="background:var(--s2)"></i>Intraday (buy open, sell close)</span>'
                '<span><i style="background:var(--ink2);height:2px"></i>SPY buy and hold</span>'
                '</div>' + cost_chart(on_res['sweep'], spy_cagr) + '</div>'
                '<p class="note">Read the left edge, not the middle. The published '
                'effect is real at zero cost. It is gone before the cost assumption '
                'reaches one basis point per side, and this repo assumes six.</p>')

    r = live['regime']
    gate = ("RISK ON" if r['above'] else "RISK OFF")
    gate_txt = (f"SPY {r['spy']:,.2f} is {abs(r['pct_from_sma']):.1f}% "
                f"{'above' if r['above'] else 'below'} its 200-day simple moving "
                f"average of {r['sma200']:,.2f}.")

    o = [f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
         f'<meta name="viewport" content="width=device-width,initial-scale=1">'
         f'<title>Trading Dashboard</title><style>{CSS}</style></head><body><div class="wrap">']
    o.append('<h1>Trading dashboard</h1>')
    o.append(f'<p class="sub">Every panel carries its evidence tier on its face. '
             f'Market data as of {live["asof"]}. Built '
             f'{datetime.now():%Y-%m-%d %H:%M}.</p>')

    ok = dry_run is True
    o.append(f'<div class="safety{"" if ok else " bad"}">'
             f'<div><b>{"DRY_RUN = True. Nothing here trades." if ok else "CHECK main.py"}</b>'
             f'<p>{"This dashboard is read only and observational. It places no orders and recommends no position sizes. Read live from " + esc(dry_src) + "." if ok else "Expected DRY_RUN = True. Found: " + esc(str(dry_run)) + " (" + esc(dry_src) + ")."}</p></div></div>')
    o.append(f'<div class="safety{"" if r["above"] else " bad"}">'
             f'<div><b>Strategy C regime gate: {gate}</b><p>{gate_txt} This is the '
             f'one live reading on this page with a backtest behind it.</p></div></div>')

    o.append('<div class="legend"><h2>Evidence tiers</h2><ul>')
    for t in (1, 2, 3, 4, 5):
        label, cls, desc = TIERS[t]
        o.append(f'<li><span class="badge b-{cls}">{label}</span><span>{desc}</span></li>')
    o.append('</ul></div>')

    heads = {
        1: ("Validated", "Backtested here under survivorship controls. This is the "
                         "only tier that has earned the right to influence a decision."),
        2: ("Tested and rejected", "Kept deliberately visible and high on the page. "
                                   "Every entry below was believed, tested, and killed here. "
                                   "Do not rebuild them, and do not let an agent rebuild them."),
        3: ("Untested hypotheses", "Plausible and unproven. Each carries the specific "
                                   "backtest that would promote it or kill it. None of "
                                   "these may influence sizing until that test is run."),
        4: ("Not mechanizable", "Cannot be reduced to falsifiable rules. Recorded so it "
                                "does not get proposed again."),
        5: ("Watchlist", "Research queue. Not dashboard logic, and no verdict is "
                         "computed here."),
    }
    for t in (1, 2, 3, 4, 5):
        group = [p for p in panels if p['tier'] == t]
        if not group:
            continue
        h, note = heads[t]
        o.append(f'<section><h2>{h}</h2><p class="secnote">{note}</p>')
        for p in group:
            o.append(render_card(p, on_res, spy_cagr))
        o.append('</section>')

    o.append('<footer><p>Read only and observational. Nothing on this page is '
             'financial advice, and neither the author of this repo nor the tool '
             'that generated the page is a licensed advisor. Sizing and trading '
             'decisions belong to the account holder.</p>'
             '<p>Rebuild with <code>python build_dashboard.py</code>. Refresh the '
             'overnight study with <code>python overnight_vs_intraday.py --json '
             'overnight_results.json</code>.</p></footer>')
    o.append(f'</div><script>{TIP_JS}</script></body></html>')
    return "".join(o)


def render_md(panels, live, macro, on_res, dry_run, dry_src):
    """Plain-text mirror. This is the file a future agent will actually read."""
    r = live['regime']
    L = ["# Trading dashboard", "",
         f"Market data as of {live['asof']}. Built {datetime.now():%Y-%m-%d %H:%M}.", "",
         "## Read this first",
         "",
         "Every entry carries an evidence tier. The tiers are not decoration: an "
         "untested idea sitting next to a backtested one at equal weight is how "
         "unvalidated content borrows credibility it did not earn, and how an idea "
         "this repo already disproved gets rebuilt by someone who did not know.",
         "",
         "- VALIDATED: backtested in this repo with survivorship controls.",
         "- TESTED AND REJECTED: already disproven here. Do not rebuild.",
         "- UNTESTED HYPOTHESIS: plausible, no evidence, backtest path given.",
         "- NOT MECHANIZABLE: cannot be written as falsifiable rules.",
         "- WATCHLIST: research queue, not dashboard logic.",
         "",
         f"DRY_RUN is {dry_run} (read from {dry_src}). This dashboard is read only "
         f"and observational. It places no orders and recommends no sizing.",
         "",
         f"Strategy C regime gate right now: {'RISK ON' if r['above'] else 'RISK OFF'}. "
         f"SPY {r['spy']:,.2f} is {abs(r['pct_from_sma']):.1f}% "
         f"{'above' if r['above'] else 'below'} its 200-day simple moving average.",
         ""]
    heads = {1: "Validated", 2: "Tested and rejected", 3: "Untested hypotheses",
             4: "Not mechanizable", 5: "Watchlist"}
    for t in (1, 2, 3, 4, 5):
        group = [p for p in panels if p['tier'] == t]
        if not group:
            continue
        L += [f"## {heads[t]}", ""]
        for p in group:
            L.append(f"### [{TIERS[t][0]}] {p['title']}")
            if p.get('subtitle'):
                L.append(f"*{p['subtitle']}*")
            L.append("")
            if p.get('killer'):
                L += [f"**Why it was rejected:** {p['killer']}", ""]
            for para in p.get('body', []):
                L += [para, ""]
            if p.get('macro'):
                s = live['series']
                L += [f"Live measurements as of {live['asof']} (facts; the "
                      f"interpretation above them is not):"]
                if 'GC=F' in s:
                    L.append(f"- Gold {s['GC=F']['last']:,.0f}, "
                             f"{s['GC=F']['pct_60d']:+.1f}% over {MACRO_LOOKBACK} "
                             f"sessions ({macro['gold']})")
                if 'CL=F' in s:
                    L.append(f"- Crude oil {s['CL=F']['last']:,.2f}, "
                             f"{s['CL=F']['pct_60d']:+.1f}% over {MACRO_LOOKBACK} "
                             f"sessions ({macro['oil']})")
                if '^TNX' in s:
                    L.append(f"- 10-year yield {s['^TNX']['last']:.2f}%, "
                             f"{macro['yield_bps']:+.0f} basis points over "
                             f"{MACRO_LOOKBACK} sessions ({macro['yield']})")
                L += ["",
                      f"Current cell: oil {macro['oil']}, yields {macro['yield']}. "
                      + ("A reading is inside the neutral band, so this sits in no "
                         "cell of the 2x2. Undefined, not benign."
                         if 'flat' in (macro['oil'], macro['yield']) else
                         ("This IS the claimed severe-downside cell. It has never "
                          "been backtested and must not change sizing."
                          if macro['severe'] else
                          "Not the claimed severe-downside cell, which is not an "
                          "all-clear: no cell here has demonstrated predictive value.")),
                      "",
                      f"Thresholds are ours, not the video's, which gave none: "
                      f"more than {MACRO_NEUTRAL_PCT:g}% over {MACRO_LOOKBACK} "
                      f"trading days, and more than {YIELD_NEUTRAL_BPS:g} basis "
                      f"points for the yield.", ""]
            for key, head in (('rules', 'Rules'), ('why', 'Why not'),
                              ('backtest_path', 'Backtest path to promote or kill it'),
                              ('actions', 'Next actions')):
                if p.get(key):
                    L.append(f"{head}:")
                    L += [f"- {x}" for x in p[key]]
                    L.append("")
            if p.get('metrics'):
                L += ["| Variant | CAGR | Sharpe | Max DD | Calmar |",
                      "|---|---:|---:|---:|---:|"]
                L += ["| " + " | ".join(str(c) for c in row) + " |" for row in p['metrics']]
                L.append("")
            for key in ('metric_note', 'robust', 'prior', 'note'):
                if p.get(key):
                    L += [p[key], ""]
            if p.get('caveat'):
                L += [f"**Where this study is weak:** {p['caveat']}", ""]
            if p.get('watch'):
                for tk, last, pct in p['watch']:
                    L.append(f"- {tk}: ${last:,.2f}, {pct:+.1f}% over "
                             f"{MACRO_LOOKBACK} sessions (raw observation, no verdict)")
                L.append("")
            L += [f"Source: {p['source']}", ""]
    L += ["---", "",
          "Read only and observational. Nothing here is financial advice, and "
          "neither the repo author nor the generating tool is a licensed advisor. "
          "Sizing and trading decisions belong to the account holder.", ""]
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--out', default=str(DEFAULT_OUT))
    args = ap.parse_args()

    dry_run, dry_src = read_dry_run()
    if dry_run is not True:
        print(f"WARNING: DRY_RUN is not True ({dry_run} via {dry_src}). "
              f"Building anyway and flagging it on the page.")

    print("Fetching live market data...")
    live = fetch_live()
    macro = macro_reading(live)

    res_path = REPO / "overnight_results.json"
    on_res = json.loads(res_path.read_text()) if res_path.exists() else None
    if on_res is None:
        print("NOTE: overnight_results.json not found. The overnight panel will be "
              "omitted. Run: python overnight_vs_intraday.py --json overnight_results.json")

    panels = build_panels(live, macro, on_res)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    html = out / "TRADING-DASHBOARD.html"
    md = out / "TRADING-DASHBOARD.md"
    html.write_text(render_html(panels, live, macro, on_res, dry_run, dry_src),
                    encoding="utf-8")
    md.write_text(render_md(panels, live, macro, on_res, dry_run, dry_src),
                  encoding="utf-8")
    print(f"Wrote {html}")
    print(f"Wrote {md}")
    print(f"\nRegime: {'RISK ON' if live['regime']['above'] else 'RISK OFF'} | "
          f"gold {macro['gold']}, oil {macro['oil']}, yields {macro['yield']}"
          f"{'  [claimed severe-downside cell]' if macro['severe'] else ''}")


if __name__ == "__main__":
    main()
