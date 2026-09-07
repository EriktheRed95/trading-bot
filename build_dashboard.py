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


def current_portfolio(top_n=10):
    """What Strategy C's RULES select as of today, with inverse-volatility weights.

    This is a MODEL portfolio, not a position book. DRY_RUN is True and nothing
    in this repo has ever traded, so there are no held positions to report. What
    this answers is the honest live question: if the validated strategy were run
    today, what would it hold and at what weight?

    Ranking comes from live_picks.live_ranking with the sentiment tilt OFF, since
    that tilt is in the rejected tier. Sizing repeats run_allocator's own rule:
    weight proportional to 1/volatility, normalized.
    """
    from live_picks import live_ranking
    from strategy_c import BROAD_UNIVERSE, RISK_OFF_TICKERS, load_prices

    close = load_prices(BROAD_UNIVERSE + list(RISK_OFF_TICKERS), period="2y")
    r = live_ranking(close, BROAD_UNIVERSE, top_n=top_n, use_sentiment=False)
    d = r['date']
    sma = close.rolling(200).mean()
    vol = close.pct_change().rolling(63).std() * (252 ** 0.5)

    out = {'asof': str(d.date()), 'risk_on': r['risk_on'],
           'n_eligible': len(r['eligible']), 'top_n': top_n, 'holdings': [],
           'risk_off': [], 'history': {}}

    if r['risk_on']:
        picks = r['picks_without'][:top_n]
        inv = 1.0 / vol.loc[d, picks]
        w = inv / inv.sum()
        for t in picks:
            out['holdings'].append({
                'ticker': t, 'weight': float(w[t]) * 100.0,
                'vol': float(vol.loc[d, t]) * 100.0,
                'price': float(close.at[d, t]),
                'above_sma': float(close.at[d, t] / sma.at[d, t] - 1.0) * 100.0,
                'score': float(r['base'].get(t, float('nan'))),
            })
            out['history'][t] = [float(x) for x in close[t].dropna().iloc[-126:]]
    else:
        # Dynamic risk-off sleeve: hold each candidate only while it is trending.
        trending = [c for c in RISK_OFF_TICKERS
                    if c in close.columns and close[c].iloc[-1] > sma[c].iloc[-1]]
        for c in RISK_OFF_TICKERS:
            if c not in close.columns:
                continue
            up = c in trending
            out['risk_off'].append({
                'ticker': c, 'trending': up,
                'weight': (100.0 / len(trending)) if up and trending else 0.0,
                'price': float(close[c].iloc[-1]),
                'above_sma': float(close[c].iloc[-1] / sma[c].iloc[-1] - 1.0) * 100.0,
            })
            out['history'][c] = [float(x) for x in close[c].dropna().iloc[-126:]]
        out['cash_pct'] = 0.0 if trending else 100.0

    spy = close['SPY'].dropna()
    out['history']['SPY'] = [float(x) for x in spy.iloc[-378:]]
    out['history']['SPY_SMA200'] = [float(x) for x in sma['SPY'].dropna().iloc[-378:]]
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

def build_panels(live, macro, on_res, port=None, tips=None, theses=None):
    """Every panel, each stamped with its tier. Order is deliberate."""
    ov = {r['label']: r for r in on_res.get('universe_rows', [])} if on_res else {}
    ov_single = ({r['label']: r for r in on_res['single_name']['rows']}
                 if on_res and 'single_name' in on_res else {})

    def g(d, k, f):
        return d.get(k, {}).get(f)

    panels = []

    # ---------------------------------------------------------- TIER 1
    if port:
        panels.append(dict(
            tier=1, title="Current model portfolio",
            subtitle="What Strategy C's rules select as of today. Not a position book.",
            portfolio=True,
            body=[
                "There are no active positions to report, and that is not an "
                "omission. DRY_RUN is True, nothing in this repo has ever placed "
                "an order, and no live or paper account is connected. What this "
                "panel answers instead is the live question worth asking: if the "
                "validated strategy ran today, what would it hold and at what "
                "weight?",
                "Ranking is the repo's own live_picks ranking with the news "
                "sentiment tilt switched OFF, because that tilt sits in the "
                "rejected tier. Sizing repeats the allocator's rule: weight "
                "proportional to one divided by volatility, so each name "
                "contributes roughly equal risk. Strategy C rebalances MONTHLY, "
                "so this is a snapshot of the ranking, not a daily trade list.",
            ],
            source="live_picks.py + strategy_c.py. Reproduce: python live_picks.py",
        ))

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
        tier=1, title="Every strategy side by side",
        subtitle="Same 65 names, same window, so the comparison is real.",
        body=[
            "Backtest numbers are usually incomparable because each one quietly "
            "uses a different universe or window. These do not. Every row below "
            "is the same 65-name pool over 1993-11 to 2026-09, which is why the "
            "overnight legs were re-run on Strategy C's window specifically to "
            "sit in this table.",
            "The tier column is the point. Two rows have earned their numbers. "
            "Four are shown precisely because they were tested and failed.",
        ],
        comparison=[
            ("Strategy C, dynamic risk-off", "23.2%", "1.09", "-26%", 1),
            ("Strategy C, cash sleeve", "20.9%", "1.03", "-28%", 1),
            ("Equal-weight hold, same 65 names", "19.9%", "0.99", "-49%", 0),
            ("Overnight leg, ZERO costs", "13.8%", "1.19", "-30%", 2),
            ("SPY buy and hold", "10.8%", "0.64", "-55%", 0),
            ("Intraday leg, ZERO costs", "5.5%", "0.40", "-43%", 2),
            ("Overnight leg, net 6bp per side", "-15.9%", "-1.45", "-100%", 2),
            ("Intraday leg, net 6bp per side", "-22.0%", "-1.40", "-100%", 2),
        ],
        readings=[
            "Read the zero-cost overnight row carefully, because it is the one "
            "place the video's idea genuinely shines: Sharpe 1.19 actually beats "
            "Strategy C's 1.09. That is the published anomaly showing up in this "
            "data, and it deserves to be stated rather than buried.",
            "It still loses. Even given free trading it returns 13.8% against "
            "19.9% for simply holding the same names, because trading only the "
            "overnight leg throws away the intraday leg's 5.5%. Better ratio, "
            "much less money, and only in a world without costs.",
            "Charge the repo's own costs and both legs go to roughly -100%. The "
            "gap between the 13.8% row and the -15.9% row is nothing but "
            "spread paid 504 times a year.",
            "The original mean-reversion engine is absent from this table on "
            "purpose. It was tested per-name on different windows, not as a "
            "portfolio, so putting it here would fake a comparability it does "
            "not have. Its result stands separately: it beat buy-and-hold on 2 "
            "of 14 names hourly and 0 of 14 daily.",
        ],
        comparison_pit=[
            ("Equal-weight hold, same names", "12.0%", "0.64", "-54%", 0),
            ("SPY buy and hold", "11.0%", "0.63", "-55%", 0),
            ("Overnight leg, ZERO costs", "7.9%", "0.69", "-32%", 2),
            ("Intraday leg, ZERO costs", "4.1%", "0.33", "-45%", 2),
            ("Overnight leg, net 6bp per side", "-20.2%", "-1.81", "-99%", 2),
            ("Intraday leg, net 6bp per side", "-23.0%", "-1.51", "-99%", 2),
        ],
        pit_note="Second table, the honest universe: point-in-time S&P 500 "
                 "membership, 2007 to 2026, about 417 names a day. This is the "
                 "one that matters, and it is where the overnight idea dies "
                 "properly. Its gross Sharpe falls from 1.19 on the megacaps to "
                 "0.69 here, against SPY's 0.63, so the risk-adjusted edge is "
                 "essentially gone before costs are charged at all. Strategy C is "
                 "absent from this table because it has not been re-run under the "
                 "data-quality filter described in the rejected section below.",
        metric_note="Cross-check on the pipeline: this table's equal-weight hold "
                    "row computes 19.9%, against 19.8% in the repo's own README "
                    "from a completely separate code path. That agreement is the "
                    "reason to trust the overnight rows beside it.",
        source="strategy_c.py, README.md, and overnight_vs_intraday.py "
               "--start 1993-11-01",
    ))

    panels.append(dict(
        tier=6, title="Overnight effect, with selection",
        subtitle="Survived four attempts to kill it. Blocked on execution, not on evidence.",
        body=[
            "This began in the rejected list below and was moved here, which is "
            "the tiering doing its job in the direction nobody expects. The first "
            "test held EVERY name overnight and failed. That was a result about "
            "the absence of selection, not about the effect.",
            "Rank the point-in-time S&P 500 by trailing 252-day overnight return, "
            "rebalance monthly, hold the top names overnight only, and sell into "
            "the opening auction.",
        ],
        metrics=[
            ("Top 33, 3% positions", "26.9%", "1.49", "-31%", ""),
            ("Top 20, 5% positions", "32.2%", "1.59", "-31%", ""),
            ("Top quintile plus SPY 200-day gate", "17.1%", "1.81", "-13%", ""),
            ("Same basket held ALL DAY", "13.6%", "0.65", "-60%", ""),
            ("SPY buy and hold", "11.0%", "0.63", "-55%", ""),
        ],
        metric_note="Point-in-time membership, 2007 to 2026, zero cost, after "
                    "both data filters. Post-2019 at 3% positions it still "
                    "returns 22.9% at Sharpe 1.24.",
        readings=[
            "It is not momentum. Median rank correlation with 12-month momentum "
            "is 0.41 with 42% name overlap, and the momentum-orthogonalized "
            "residual still returns 18.2% at Sharpe 1.26, against 12.6% for "
            "ranking on momentum itself. The residual carries the effect, which "
            "is the same test that dismantled the sentiment tilt.",
            "It is not a data artifact. It holds on the clean 65-name megacap "
            "pool at 35.6%, and the selected names show an Open equal to the "
            "prior Close on only 0.0% to 3.1% of days.",
            "The timing is the edge, not just the selection. The identical "
            "basket held all day returns 13.6% at Sharpe 0.65 against 21.0% at "
            "1.42 held overnight. The intraday leg of these names is actively "
            "harmful.",
            "It survived publication, with decay. Sharpe 1.59 before 2019, 1.20 "
            "after.",
        ],
        blocker=(
            "Execution, and it is unresolved. Every number here assumes fills at "
            "the official opening and closing auction prints, because that is "
            "exactly what the backtest measures. Commissions are zero at United "
            "States retail brokers, so auction slippage is the only binding cost. "
            "Full period the strategy beats every benchmark up to about 1 basis "
            "point per side; POST-2019 it beats them only below roughly 0.25 "
            "basis points per side on return. That is a very thin margin, and "
            "nobody has measured what fills actually look like against the print. "
            "Until that is measured this is a finding, not a strategy."),
        caveat=(
            "What is still missing, stated plainly. There is no walk-forward "
            "harness, so the 252-day lookback and the position count were chosen "
            "with the whole sample visible, even though all three lookbacks "
            "tested worked and improved monotonically. Tail risk is one "
            "historical path: this repo's own gap stress test cut worst-case "
            "drawdown from -77% at three names to -31% at twenty, so "
            "concentration at ten names is more dangerous than its measured -29% "
            "suggests. And the data is daily bars from a free source, not the "
            "trade-and-quote data the paper used with a bid-ask midpoint "
            "robustness check."),
        source="overnight_study.py, overnight_study2.py. Paper: Lou, Polk and "
               "Skouras, Journal of Financial Economics 134(1), 2019, 192-213",
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
            ("Point-in-time membership", "32.3%", "1.22", "-35%", "still dirty"),
            ("Point-in-time PLUS data filter", "24.7%", "1.01", "-34%", "honest"),
            ("SPY buy and hold", "10.9%", "0.65", "-55%", "benchmark"),
        ],
        metric_note="Two separate inflations, each worth about 8 points. Removing "
                    "selection hindsight took 40.6% to 32.3%. Removing broken "
                    "delisted-ticker data took another 7.6 points off, to 24.7%, "
                    "and Sharpe from 1.22 to 1.01. The control holds: the "
                    "unfiltered path reproduces 32.3% against the published "
                    "32.2%, so the harness is sound and the gap is real. The "
                    "repo's older claim of Sharpe 1.1 to 1.25 should now read "
                    "roughly 1.0 to 1.1. The filter is conservative and removes "
                    "some genuine events, so the true figure likely sits between "
                    "24.7% and 32.3%, nearer the lower end.",
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
            title="Buy at the close and sell at the open on EVERY name",
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
                "SCOPE, and read it carefully. This rejection covers the "
                "UNDIFFERENTIATED version only: hold every name, every night. On "
                "point-in-time membership that grosses 7.9% CAGR at Sharpe 0.69 "
                "against SPY's 11.0% at 0.63, so it has no edge even at zero "
                "cost. Adding a SELECTION rule changes the answer completely and "
                "that version now sits in the execution-blocked tier above at "
                "26.9% and Sharpe 1.49. The failure here was the absence of "
                "selection, not the absence of an effect. Do not cite this card "
                "as evidence against the selected strategy."),
            source="overnight_vs_intraday.py. Reproduce: python "
                   "overnight_vs_intraday.py --json overnight_results.json",
        ))
        rejected.insert(1, dict(
            title="Trusting free price data on delisted tickers",
            fresh=True,
            killer=(
                "Found while running the point-in-time confirmation above, and it "
                "contaminates any unfiltered study on that universe. Cooper "
                "Industries (CBE) delisted in 2012 but still prints rows in 2016 "
                "showing a prior close of $0.005 against an open of $170 on the "
                "same day, an implied +3,399,900% overnight return, dozens of "
                "times. Ten of the 682 priced names print moves above 500%. "
                "Unfiltered, those ten produced a 2,325% CAGR for the whole "
                "portfolio."),
            note=(
                "The irony is the important part: point-in-time membership exists "
                "to pull delisted names back in, and the delisted names are "
                "precisely the ones free data serves badly. Honesty about "
                "survivorship imports a data-quality problem. The fix used here "
                "is a stated filter, both sides of a leg priced at $1 or above and "
                "any single-session move beyond plus or minus 50% dropped, which "
                "removes 2.8% of name-days."),
            caveat=(
                "This is a live risk to run_sp500_pit.py, not just to the overnight "
                "study. Those broken names PASS Strategy C's eligibility filter on "
                "13,072 name-days and reach the momentum top 10 on 190 rebalance "
                "dates. CPWR and MI still read as eligible in 2026, years after "
                "they stopped trading, which cannot be real. The 32.2% "
                "point-in-time CAGR should be re-run with the same price and "
                "return sanity filter before it is quoted again. That number has "
                "not been shown to be wrong, it has been shown to be unverified."),
            source="Diagnosed via overnight_vs_intraday.py --universe pit",
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

    if theses:
        for th in theses.get('theses', []):
            panels.append(dict(
                tier=3, title=f"Thesis: {th['title']}",
                subtitle="A structural argument, which beats a tip and is still "
                         "not a backtest.",
                thesis=th,
                body=[th['structural_claim']],
                source=f"thesis_intake.py, content_theses.json. Source: "
                       f"{th.get('url', '')}",
            ))
        panels.append(dict(
            tier=3, title="Tradability gate",
            subtitle="Run before any screen, because a signal on a series that "
                     "is not trading is not a signal.",
            gate=True,
            body=[
                "A thesis is worth nothing if its expressions cannot be reached. "
                "Each candidate is measured on median dollar volume, quoted "
                "spread, the share of sessions printing zero change, and where a "
                "liquid home listing exists, the correlation between the two.",
                "The gate validates its own instrument first. Quoted spreads from "
                "a free feed are meaningless while the market is closed, so it "
                "checks a canary known to trade at a hair-thin spread. If the "
                "canary fails, the spread test switches off and the verdict rests "
                "only on measures that do not need a live quote.",
            ],
            source="thesis_intake.py",
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
    if tips:
        counts = tips.get('counts', {})
        panels.append(dict(
            tier=5, title="Stock tips from saved content",
            subtitle="Run through his rules, not the creator's. Most do not survive.",
            tipverdicts=True,
            body=[
                "Saved short-form finance content is a recurring input, so it gets "
                "a route rather than an argument each time. Tickers go in, and "
                "what comes out is Strategy C's own verdict: market regime gate, "
                "per-name 200-day trend filter, 12-month-minus-1-month momentum, "
                "and a volatility fragility flag. The video gets no vote.",
                "These names are NOT signals and are not wired into anything. They "
                "are a watchlist that has to earn its place through rules that "
                "were backtested, and most of them do not.",
            ],
            actions=[
                "For quality and valuation rather than trend, run the "
                "fundamental-check and financial-researcher skills, which read "
                "SEC EDGAR filings directly.",
                "A name that passes here has passed a TREND test only. Eligible "
                "does not mean it will go up, it means it is not in a downtrend "
                "with negative momentum.",
            ],
            source="tip_intake.py, content_tips.json. Reproduce: python "
                   "tip_intake.py --json tip_intake.json",
        ))
        panels.append(dict(
            tier=5, title="How the video numbers checked out",
            subtitle="Provenance matters more than the verdicts.",
            claimledger=True,
            body=[
                "Every figure below originated in a video. A claim that checks "
                "out is still content-sourced, and content-sourced numbers can "
                "never reach the validated tier no matter how well they verify. "
                "That rule is the point of this panel.",
                "The danger is not wholesale fabrication, which is easy to spot. "
                "It is the mixture: genuinely verifiable facts sitting beside an "
                "unsupported number, where the checkable parts lend credibility "
                "to the rest.",
            ],
            source="content_tips.json. Verified against SEC EDGAR companyfacts "
                   "(Micron CIK 0000723125) and company press releases",
        ))

    return panels


# ------------------------------------------------------------------- rendering

TIERS = {
    1: ("VALIDATED", "validated", "Backtested in this repo, with survivorship controls."),
    2: ("TESTED AND REJECTED", "rejected", "Already disproven here. Do not rebuild."),
    3: ("UNTESTED HYPOTHESIS", "untested", "Plausible. No evidence yet. Backtest path given."),
    4: ("NOT MECHANIZABLE", "nomech", "Cannot be written down as falsifiable rules."),
    5: ("WATCHLIST", "watch", "Research queue. Not dashboard logic."),
    6: ("TESTED, EXECUTION-BLOCKED", "blocked",
        "Survived every test run against it, but has a named blocker that makes "
        "it undeployable. Not validated, not rejected."),
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
.t-blocked{border:1px solid var(--ring);border-left:6px solid var(--warning)}
.t-blocked h3{font-size:1.14rem;font-weight:700}
.b-blocked{background:var(--warning);color:#3a2a00}
.blocker{background:rgba(250,178,25,.12);border:1px solid var(--warning);border-radius:7px;
  padding:11px 13px;margin:10px 0 0;font-size:.9rem;color:var(--ink)}
.blocker h4{margin:0 0 6px;font-size:.72rem;letter-spacing:.09em;text-transform:uppercase;
  color:var(--muted);font-weight:700}
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
.sub4{margin:12px 0 4px;font-size:.72rem;letter-spacing:.09em;text-transform:uppercase;color:var(--muted);font-weight:700}
.chip{display:inline-block;font-size:.62rem;font-weight:700;letter-spacing:.06em;
  text-transform:uppercase;padding:2px 6px;border-radius:3px;margin-right:4px;
  border:1px solid var(--axis);color:var(--muted)}
.chip.good{border-color:var(--good);color:var(--good)}
.chip.warn{border-color:var(--warning);color:var(--warning)}
.chip.crit{border-color:var(--critical);color:var(--critical)}
.reading b{color:var(--ink)}
@media (max-width:620px){.q2{grid-template-columns:1fr}}
.alarm{background:rgba(208,59,59,.09);border:1px solid var(--critical);border-radius:7px;
  padding:11px 13px;margin:10px 0 0;font-size:.9rem}

table.cmp td:last-child,table.cmp th:last-child{text-align:right}
table.cmp tr.r1 td{font-weight:650}
table.cmp tr.r2 td{color:var(--muted)}
table.cmp tr.r0 td{font-style:italic}
.badge.mini{font-size:.58rem;padding:2px 6px}
.b-bm{background:transparent;color:var(--muted);border-color:var(--axis)}
table.port td.sk{width:140px;padding:2px 8px}
table.port td.pos{color:var(--good)}
table.port td.neg{color:var(--critical)}
table.port td.tiny{font-size:.78rem;color:var(--ink2)}
svg.spark{display:block}
.stale{display:flex;gap:12px;align-items:flex-start;border-radius:10px;padding:12px 16px;
  margin:0 0 14px;border:1px solid var(--ring);border-left:5px solid var(--good);
  background:var(--surface)}
.stale.warn{border-left-color:var(--warning)}
.stale.old{border-left-color:var(--critical)}
.stale b{display:block;margin-bottom:2px}
.stale p{margin:0;color:var(--ink2);font-size:.9rem}
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

STALE_JS = """
(function(){
 // A generated page cannot refresh itself, so it must at least be honest about
 // its own age. This reads the build stamp and says how old the data is, every
 // time the page is opened, rather than letting a stale file look current.
 var el=document.getElementById('stale'); if(!el) return;
 var built=new Date(el.getAttribute('data-built'));
 var hrs=(Date.now()-built.getTime())/36e5;
 var txt=el.querySelector('p'), hd=el.querySelector('b');
 var mins=Math.max(1,Math.round(hrs*60));
 var age = hrs<1 ? mins+(mins===1?' minute':' minutes') :
           hrs<48 ? hrs.toFixed(1)+' hours' : (hrs/24).toFixed(1)+' days';
 el.classList.remove('warn','old');
 if(hrs>72){el.classList.add('old');hd.textContent='Stale: rebuild before trusting the live panels';}
 else if(hrs>18){el.classList.add('warn');hd.textContent='Ageing: the live panels are past a trading day old';}
 else {hd.textContent='Fresh';}
 txt.textContent='Built '+age+' ago ('+built.toLocaleString()+'). Market data as of '
   +el.getAttribute('data-asof')+'. Backtested panels do not go stale; the model '
   +'portfolio, the regime gate and the macro readings do. Rebuild: python build_dashboard.py';
})();
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


def sparkline(vals, w=132, h=30, color="var(--s1)"):
    """Single-series mini chart. No axes: it carries shape, and the number beside
    it carries the level. One series, so no legend (the row label names it)."""
    vals = [v for v in vals if v == v]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1.0
    n = len(vals)
    pts = " ".join(f"{i/(n-1)*(w-2)+1:.1f},{h-1-((v-lo)/rng)*(h-2):.1f}"
                   for i, v in enumerate(vals))
    up = vals[-1] >= vals[0]
    c = color if color != "auto" else ("var(--good)" if up else "var(--critical)")
    lx, ly = (n - 1) / (n - 1) * (w - 2) + 1, h - 1 - ((vals[-1] - lo) / rng) * (h - 2)
    return (f'<svg class="spark" viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
            f'aria-hidden="true"><polyline points="{pts}" fill="none" stroke="{c}" '
            f'stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/>'
            f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="2.2" fill="{c}"/></svg>')


def regime_chart(spy, sma, w=720, h=210):
    """SPY against its own 200-day simple moving average: the validated gate.

    Two lines on ONE axis, both in dollars. Shading marks where the gate is on.
    """
    n = min(len(spy), len(sma))
    spy, sma = spy[-n:], sma[-n:]
    if n < 10:
        return ""
    L, R, T, B = 54, 74, 12, 26
    pw, ph = w - L - R, h - T - B
    lo = min(min(spy), min(sma))
    hi = max(max(spy), max(sma))
    pad = (hi - lo) * .10 or 1
    lo, hi = lo - pad, hi + pad

    def X(i):
        return L + i / (n - 1) * pw

    def Y(v):
        return T + (hi - v) / (hi - lo) * ph

    o = [f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="SPY against its '
         f'200-day simple moving average">']
    # shade the risk-on stretches
    run = None
    for i in range(n):
        on = spy[i] > sma[i]
        if on and run is None:
            run = i
        elif not on and run is not None:
            o.append(f'<rect x="{X(run):.1f}" y="{T}" width="{X(i)-X(run):.1f}" '
                     f'height="{ph}" fill="var(--good)" opacity="0.07"/>')
            run = None
    if run is not None:
        o.append(f'<rect x="{X(run):.1f}" y="{T}" width="{X(n-1)-X(run):.1f}" '
                 f'height="{ph}" fill="var(--good)" opacity="0.07"/>')
    for frac in (0, .5, 1):
        v = lo + (hi - lo) * frac
        o.append(f'<line x1="{L}" y1="{Y(v):.1f}" x2="{L+pw}" y2="{Y(v):.1f}" '
                 f'stroke="var(--grid)" stroke-width="1"/>'
                 f'<text x="{L-8}" y="{Y(v)+3.5:.1f}" text-anchor="end" font-size="10.5" '
                 f'fill="var(--muted)">{v:,.0f}</text>')
    for series, col, lab, dash in ((sma, 'var(--s2)', '200d SMA', '5 4'),
                                   (spy, 'var(--s1)', 'SPY', '')):
        pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(series))
        d = f' stroke-dasharray="{dash}"' if dash else ''
        o.append(f'<polyline points="{pts}" fill="none" stroke="{col}" '
                 f'stroke-width="2" stroke-linejoin="round"{d}/>')
        o.append(f'<text x="{X(n-1)+6:.1f}" y="{Y(series[-1])+4:.1f}" font-size="11" '
                 f'font-weight="650" fill="{col}">{lab}</text>')
    o.append(f'<text x="{L}" y="{h-6}" font-size="10.5" fill="var(--muted)">'
             f'about 18 months, shaded where the gate says risk on</text>')
    o.append('</svg>')
    return "".join(o)


def render_card(p, on_res, spy_cagr):
    label, cls, _ = TIERS[p['tier']]
    o = [f'<article class="card t-{cls}">']
    bar = {1: '<span class="evbar"><i class="on"></i><i class="on"></i><i class="on"></i></span>',
           2: '<span class="evbar rej"><i class="on"></i><i class="on"></i><i class="on"></i></span>',
           3: '<span class="evbar uns"><i></i><i></i><i></i></span>',
           4: '<span class="evbar uns"><i></i><i></i><i></i></span>',
           5: '<span class="evbar uns"><i></i><i></i><i></i></span>',
           6: '<span class="evbar"><i class="on"></i><i class="on"></i><i></i></span>'}[p['tier']]
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
    if p.get('comparison'):
        o.append('<table class="cmp"><thead><tr><th>Strategy</th><th>CAGR</th>'
                 '<th>Sharpe</th><th>Max DD</th><th>Tier</th></tr></thead><tbody>')
        tag = {0: ('benchmark', 'bm'), 1: ('VALIDATED', 'validated'),
               2: ('REJECTED', 'rejected')}
        for name, cagr, sh, dd, tr in p['comparison']:
            lab, cls = tag[tr]
            o.append(f'<tr class="r{tr}"><td>{esc(name)}</td><td>{esc(cagr)}</td>'
                     f'<td>{esc(sh)}</td><td>{esc(dd)}</td>'
                     f'<td><span class="badge b-{cls} mini">{lab}</span></td></tr>')
        o.append('</tbody></table>')
    if p.get('pit_note'):
        o.append(f'<p class="note">{esc(p["pit_note"])}</p>')
    if p.get('comparison_pit'):
        o.append('<table class="cmp"><thead><tr><th>Strategy</th><th>CAGR</th>'
                 '<th>Sharpe</th><th>Max DD</th><th>Tier</th></tr></thead><tbody>')
        tag = {0: ('benchmark', 'bm'), 1: ('VALIDATED', 'validated'),
               2: ('REJECTED', 'rejected')}
        for name, cagr, sh, dd, tr in p['comparison_pit']:
            lab, cls = tag[tr]
            o.append(f'<tr class="r{tr}"><td>{esc(name)}</td><td>{esc(cagr)}</td>'
                     f'<td>{esc(sh)}</td><td>{esc(dd)}</td>'
                     f'<td><span class="badge b-{cls} mini">{lab}</span></td></tr>')
        o.append('</tbody></table>')
    if p.get('readings'):
        o.append('<ul class="rules">'
                 + "".join(f'<li>{esc(r)}</li>' for r in p['readings']) + '</ul>')
    if p.get('portfolio'):
        o.append(render_portfolio(p['_port']))
    if p.get('thesis'):
        o.append(render_thesis(p['thesis']))
    if p.get('gate'):
        o.append(render_gate(p['_theses']))
    if p.get('tipverdicts'):
        o.append(render_tips(p['_tips']))
    if p.get('claimledger'):
        o.append(render_claims(p['_tips']))
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
    if p.get('blocker'):
        o.append(f'<div class="blocker"><h4>The blocker</h4>{esc(p["blocker"])}</div>')
    if p.get('caveat'):
        o.append(f'<div class="caveat"><h4>Where this study is weak</h4>'
                 f'{esc(p["caveat"])}</div>')
    o.append(f'<p class="src">{esc(p["source"])}</p></article>')
    return "".join(o)


def render_portfolio(port):
    o = []
    if port['risk_on']:
        o.append(f'<p class="reading"><b>Regime gate: RISK ON.</b> '
                 f'{port["n_eligible"]} of the 65-name pool pass the trend filter; '
                 f'the top {port["top_n"]} by rank would be held, weighted by '
                 f'inverse volatility.</p>')
        o.append('<table class="port"><thead><tr><th>Name</th><th>Weight</th>'
                 '<th>Price</th><th>vs 200d</th><th>Ann. vol</th>'
                 '<th>6 months</th></tr></thead><tbody>')
        for hd in port['holdings']:
            spark = sparkline(port['history'].get(hd['ticker'], []))
            o.append(
                f'<tr><td><b>{esc(hd["ticker"])}</b></td>'
                f'<td>{hd["weight"]:.1f}%</td>'
                f'<td>${hd["price"]:,.2f}</td>'
                f'<td class="{"pos" if hd["above_sma"] >= 0 else "neg"}">'
                f'{hd["above_sma"]:+.1f}%</td>'
                f'<td>{hd["vol"]:.0f}%</td><td class="sk">{spark}</td></tr>')
        o.append('</tbody></table>')
        o.append('<p class="note">Weights are the strategy\'s rule, not advice, '
                 'and they say nothing about how much capital to commit. Position '
                 'sizing in dollars is your decision, not this page\'s and not '
                 'the model\'s.</p>')
    else:
        o.append('<p class="reading"><b>Regime gate: RISK OFF.</b> Strategy C '
                 'holds no equities here. The dynamic sleeve takes gold or long '
                 'Treasuries only while each is above its own 200-day simple '
                 'moving average, otherwise cash.</p>')
        o.append('<table class="port"><thead><tr><th>Sleeve</th><th>Trending?</th>'
                 '<th>Weight</th><th>Price</th><th>vs 200d</th><th>6 months</th>'
                 '</tr></thead><tbody>')
        for hd in port['risk_off']:
            spark = sparkline(port['history'].get(hd['ticker'], []))
            o.append(
                f'<tr><td><b>{esc(hd["ticker"])}</b></td>'
                f'<td>{"yes, hold" if hd["trending"] else "no, skip"}</td>'
                f'<td>{hd["weight"]:.0f}%</td><td>${hd["price"]:,.2f}</td>'
                f'<td class="{"pos" if hd["above_sma"] >= 0 else "neg"}">'
                f'{hd["above_sma"]:+.1f}%</td><td class="sk">{spark}</td></tr>')
        o.append(f'<tr><td><b>Cash</b></td><td>-</td>'
                 f'<td>{port.get("cash_pct", 0):.0f}%</td><td>-</td><td>-</td>'
                 f'<td>-</td></tr>')
        o.append('</tbody></table>')

    hist, sma = port['history'].get('SPY', []), port['history'].get('SPY_SMA200', [])
    if hist and sma:
        o.append('<div class="chartwrap"><div class="lg">'
                 '<span><i style="background:var(--s1)"></i>SPY</span>'
                 '<span><i style="background:var(--s2)"></i>200-day simple moving '
                 'average</span></div>' + regime_chart(hist, sma) + '</div>')
    return "".join(o)


def render_thesis(th):
    o = []
    if th.get('what_must_be_true'):
        o.append('<h4 class="sub4">What would have to be true</h4><ul class="rules">'
                 + "".join(f'<li>{esc(w)}</li>' for w in th['what_must_be_true'])
                 + '</ul>')
    lb = th.get('load_bearing_claim') or {}
    if lb:
        bad = lb['verdict'].startswith(('RETRACTED', 'WRONG', 'UNSUPPORTED'))
        o.append(f'<div class="{"killer" if bad else "priorbox"}">'
                 f'<b>Load-bearing number:</b> {esc(lb["claim"])}<br>'
                 f'<span class="chip {"crit" if bad else "warn"}">'
                 f'{esc(lb["verdict"])}</span> '
                 f'<span class="note">checked against '
                 f'{esc(lb["checked_against"])}</span>'
                 f'<p class="note">{esc(lb.get("note", ""))}</p></div>')
    if th.get('named_companies'):
        o.append('<table class="port"><thead><tr><th>Company</th><th>Role</th>'
                 '<th>Claim</th><th>Reachable from a US brokerage?</th>'
                 '</tr></thead><tbody>')
        for c in th['named_companies']:
            t = c.get('tradeable_us')
            cls, lab = (('good', 'yes') if t is True
                        else ('warn', 'nominal only') if t == 'nominal'
                        else ('crit', 'NO'))
            o.append(f'<tr><td><b>{esc(c["name"])}</b></td>'
                     f'<td>{esc(c["role"])}</td>'
                     f'<td class="tiny">{esc(c.get("claim", ""))} '
                     f'<span class="chip">{esc(c.get("verdict", ""))}</span></td>'
                     f'<td><span class="chip {cls}">{lab}</span> '
                     f'<span class="tiny">{esc(c.get("us_access", ""))}</span>'
                     f'</td></tr>')
        o.append('</tbody></table>')
    fc = th.get('fund_check')
    if fc:
        o.append(f'<div class="killer"><b>{esc(fc["headline"])}</b>'
                 + "".join(f'<p class="note">{esc(x)}</p>' for x in fc['detail'])
                 + f'<p class="src">{esc(fc["source"])}</p></div>')
    if th.get('expression_note'):
        o.append(f'<p class="note">{esc(th["expression_note"])}</p>')
    if th.get('marker_note'):
        o.append(f'<p class="note">Promotional markers '
                 f'({len(th.get("markers", []))}): {esc(th["marker_note"])}</p>')
    return "".join(o)


def render_gate(theses):
    tr = (theses or {}).get('tradability', {})
    if not tr:
        return ""
    o = ['<table class="port"><thead><tr><th>Ticker</th><th>Gate</th>'
         '<th>$ vol/day</th><th>Stale prints</th><th>Home corr</th>'
         '<th>Why</th></tr></thead><tbody>']
    for t in sorted(tr):
        r = tr[t]
        if r.get('status') == 'NO DATA':
            continue
        ok = r['status'] == 'TRADEABLE'
        why = "; ".join(r.get('fails', [])) or "clears every measure"
        hc = ("%.2f" % r["home_corr"]) if r.get("home_corr") is not None else "-"
        o.append(f'<tr><td><b>{esc(t)}</b></td>'
                 f'<td><span class="chip {"good" if ok else "crit"}">'
                 f'{esc(r["status"])}</span></td>'
                 f'<td>${r["dollar_vol"] / 1e6:,.2f}M</td>'
                 f'<td class="{"neg" if r["stale_frac"] > 0.10 else ""}">'
                 f'{r["stale_frac"]:.0%}</td>'
                 f'<td>{hc}</td>'
                 f'<td class="tiny">{esc(why)}</td></tr>')
    o.append('</tbody></table>')
    for t, r in tr.items():
        if r.get('mom_self') is not None:
            o.append(f'<div class="killer"><b>{esc(t)} is why this gate exists.</b> '
                     f'The trend screen reads {r["mom_self"]:+.0%} twelve-month '
                     f'momentum on this line and {r["mom_home"]:+.0%} on '
                     f'{esc(r["home"])} over the same window. Same company, same '
                     f'economics, and the two listings correlate at '
                     f'{r["home_corr"]:.2f}. That verdict would be stale prints '
                     f'catching up, not a read on the business.</div>')
    o.append('<p class="note">A fund is not failed on its own screen volume: '
             'creation and redemption against the underlying basket make a fund '
             'more reachable than a stock trading the same dollars.</p>')
    return "".join(o)


def render_tips(tips):
    counts = tips.get('counts', {})
    where = tips.get('where', {})
    qual = tips.get('quality', {}) or {}
    o = ['<table class="port"><thead><tr><th>Ticker</th>'
         '<th>Trend screen</th><th>Quality screen</th><th>Both?</th>'
         '<th>vs 200d</th><th>Ann. vol</th><th>Valuation</th>'
         '<th>Flags</th></tr></thead><tbody>']
    for r in tips.get('rows', []):
        if r.get('verdict') == 'NO DATA':
            continue
        n = counts.get(r['ticker'], 0)
        q = qual.get(r['ticker'], {})
        flags = []
        if r.get('fragile'):
            flags.append('<span class="chip warn">FRAGILE</span>')
        if n >= 2:
            flags.append(f'<span class="chip crit">RECURS x{n}</span>')
        if q.get('warnings'):
            flags.append('<span class="chip crit">EARNINGS FLAG</span>')
        elig = r['verdict'] == 'ELIGIBLE'
        qlabel = q.get('label', 'not run')
        qgood = qlabel == 'QUALITY'
        # A 5/5 numeric score carrying a warning is not a clean pass.
        if elig and qgood:
            both, bcls = ('FLAGGED', 'warn') if q.get('warnings') else ('YES', 'good')
        else:
            both, bcls = 'no', 'crit'
        o.append(
            f'<tr><td><b>{esc(r["ticker"])}</b></td>'
            f'<td class="{"pos" if elig else "neg"}"><b>{esc(r["verdict"])}</b></td>'
            f'<td class="{"pos" if qgood else "neg"}">{esc(qlabel)}'
            + (f' {q["score"]}/5' if q.get('score') is not None else '') + '</td>'
            f'<td><span class="chip {bcls}">{both}</span></td>'
            f'<td class="{"pos" if r["pct_vs_sma"] >= 0 else "neg"}">'
            f'{r["pct_vs_sma"]:+.0f}%</td>'
            f'<td>{r["vol"]:.0f}%</td>'
            f'<td class="tiny">{esc(q.get("valuation", ""))[:64]}</td>'
            f'<td>{"".join(flags)}</td></tr>')
    o.append('</tbody></table>')
    o.append('<p class="note">Two screens, deliberately separate. The trend screen '
             'answers WHEN and how much risk; the quality screen answers WHAT is '
             'worth owning. A high-conviction name passes both. Income-statement '
             'and cash-flow figures come from SEC filings, not from a data vendor '
             'and not from the video.</p>')
    warned = [(t, q) for t, q in qual.items() if q.get('warnings')]
    for t, q in warned:
        for w in q['warnings']:
            o.append(f'<div class="killer"><b>{esc(t)} earnings quality:</b> '
                     f'{esc(w)}</div>')
    verdicts = [(t, q.get('verdict'), q.get('why')) for t, q in qual.items()
                if q.get('verdict')]
    if verdicts:
        o.append('<table class="port"><thead><tr><th>Ticker</th>'
                 '<th>Provisional fundamental verdict</th><th>Why</th></tr>'
                 '</thead><tbody>')
        for t, v, why in verdicts:
            o.append(f'<tr><td><b>{esc(t)}</b></td><td>{esc(v)}</td>'
                     f'<td class="tiny">{esc(why or "")}</td></tr>')
        o.append('</tbody></table>')

    recur = {t: n for t, n in counts.items() if n >= 2}
    if recur:
        items = ", ".join(f"{t} appears in {n} saved sources" for t, n in recur.items())
        o.append(f'<div class="killer"><b>Recurrence is a CAUTION flag, not a buy '
                 f'signal.</b> {esc(items)}. A name showing up repeatedly across '
                 f'saved content is evidence it is being MARKETED, which is '
                 f'orthogonal to whether it is a good business. Coordinated '
                 f'promotion and organic consensus look identical one video at a '
                 f'time. They only separate when you look at the whole corpus, '
                 f'which is the only reason this row exists.</div>')
    for t, n in recur.items():
        o.append(f'<p class="note">{esc(t)}: {esc(" | ".join(where.get(t, [])))}</p>')
    return "".join(o)


def render_claims(tips):
    style = {'VERIFIED': ('pos', 'good'), 'UNSUPPORTED': ('neg', 'crit'),
             'UNVERIFIED': ('', 'warn'), 'UNVERIFIABLE': ('neg', 'crit'),
             'WRONG': ('neg', 'crit'), 'CONFLATED': ('', 'warn')}
    o = []
    for src in tips.get('sources', []):
        marks = src.get('markers', [])
        total = len(tips.get('markers', {})) or 5
        o.append(f'<p class="reading"><b>{esc(src.get("title", src["id"]))}</b> '
                 f'&mdash; substance {esc(src.get("substance", "?"))}, '
                 f'{len(marks)} of {total} promotional markers present.</p>'
                 .replace("&mdash;", ","))
        if marks:
            o.append('<p class="note">Markers: '
                     + esc(", ".join(m.replace("_", " ") for m in marks)) + '</p>')
        if not src.get('claims'):
            continue
        o.append('<table class="port"><thead><tr><th>Claim</th><th>Check</th>'
                 '<th>Provenance</th></tr></thead><tbody>')
        for c in src['claims']:
            cls, chip = style.get(c['verdict'], ('', 'warn'))
            o.append(f'<tr><td>{esc(c["claim"])}</td>'
                     f'<td class="{cls}"><span class="chip {chip}">'
                     f'{esc(c["verdict"])}</span></td>'
                     f'<td><span class="chip">content sourced</span></td></tr>')
            if c.get('note'):
                o.append(f'<tr><td colspan="3" class="note">{esc(c["note"])}</td></tr>')
        o.append('</tbody></table>')
    o.append('<p class="note">Every row above is content-sourced by definition. '
             'A VERIFIED check means the claim survived comparison against a '
             'filing or a company release, not that the number is promoted to '
             'the validated tier. Nothing from a video ever is.</p>')
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


def render_html(panels, live, macro, on_res, dry_run, dry_src, port=None, tips=None, theses=None):
    spy_cagr = 10.86
    if on_res:
        for r in on_res.get('universe_rows', []):
            if r['label'].startswith('SPY buy and hold'):
                spy_cagr = r['cagr']

    # attach live context and the chart to the panels that need them
    for p in panels:
        if p.get('macro'):
            p['_macro'], p['_live'] = macro, live
        if p.get('portfolio'):
            p['_port'] = port
        if p.get('tipverdicts') or p.get('claimledger'):
            p['_tips'] = tips
        if p.get('gate'):
            p['_theses'] = theses
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

    o.append(f'<div class="stale" id="stale" data-built="{datetime.now().isoformat()}" '
             f'data-asof="{live["asof"]}"><div><b>Checking freshness...</b>'
             f'<p>If this line does not update, JavaScript is blocked; the build '
             f'time is {datetime.now():%Y-%m-%d %H:%M} and market data is as of '
             f'{live["asof"]}.</p></div></div>')

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
        6: ("Tested, execution-blocked", "Survived every test run against it, "
            "including the ones designed to kill it, but cannot be deployed until "
            "a named blocker is resolved. Deliberately not filed as validated: "
            "passing a backtest is not the same as being tradeable."),
    }
    for t in (1, 6, 2, 3, 4, 5):
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
    o.append(f'</div><script>{STALE_JS}{TIP_JS}</script></body></html>')
    return "".join(o)


def render_md(panels, live, macro, on_res, dry_run, dry_src, port=None, tips=None, theses=None):
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
             4: "Not mechanizable", 5: "Watchlist",
             6: "Tested, execution-blocked"}
    for t in (1, 6, 2, 3, 4, 5):
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
            if p.get('comparison'):
                tag = {0: 'benchmark', 1: 'VALIDATED', 2: 'REJECTED'}
                L += ["| Strategy | CAGR | Sharpe | Max DD | Tier |",
                      "|---|---:|---:|---:|---|"]
                L += [f"| {n} | {c} | {s_} | {d} | {tag[t]} |"
                      for n, c, s_, d, t in p['comparison']]
                L.append("")
            if p.get('pit_note'):
                L += [p['pit_note'], ""]
            if p.get('comparison_pit'):
                tag = {0: 'benchmark', 1: 'VALIDATED', 2: 'REJECTED'}
                L += ["| Strategy (point-in-time universe, 2007+) | CAGR | Sharpe | Max DD | Tier |",
                      "|---|---:|---:|---:|---|"]
                L += [f"| {n} | {c} | {s_} | {d} | {tag[t]} |"
                      for n, c, s_, d, t in p['comparison_pit']]
                L.append("")
            if p.get('readings'):
                L += [f"- {r}" for r in p['readings']] + [""]
            if p.get('portfolio') and port:
                if port['risk_on']:
                    L += [f"Regime gate: RISK ON. {port['n_eligible']} of 65 names "
                          f"pass the trend filter; top {port['top_n']} held, "
                          f"inverse-volatility weighted. As of {port['asof']}.", "",
                          "| Name | Weight | Price | vs 200d | Ann. vol |",
                          "|---|---:|---:|---:|---:|"]
                    L += [f"| {h['ticker']} | {h['weight']:.1f}% | "
                          f"${h['price']:,.2f} | {h['above_sma']:+.1f}% | "
                          f"{h['vol']:.0f}% |" for h in port['holdings']]
                else:
                    L += [f"Regime gate: RISK OFF as of {port['asof']}. No equities. "
                          f"Dynamic sleeve holds gold or long Treasuries only while "
                          f"each is above its own 200-day simple moving average.", "",
                          "| Sleeve | Trending | Weight | Price | vs 200d |",
                          "|---|---|---:|---:|---:|"]
                    L += [f"| {h['ticker']} | {'yes' if h['trending'] else 'no'} | "
                          f"{h['weight']:.0f}% | ${h['price']:,.2f} | "
                          f"{h['above_sma']:+.1f}% |" for h in port['risk_off']]
                    L.append(f"| Cash | - | {port.get('cash_pct', 0):.0f}% | - | - |")
                L += ["", "Weights are the strategy's rule, not advice. How much "
                      "capital to commit is your decision.", ""]
            if p.get('metrics'):
                L += ["| Variant | CAGR | Sharpe | Max DD | Calmar |",
                      "|---|---:|---:|---:|---:|"]
                L += ["| " + " | ".join(str(c) for c in row) + " |" for row in p['metrics']]
                L.append("")
            for key in ('metric_note', 'robust', 'prior', 'note'):
                if p.get(key):
                    L += [p[key], ""]
            if p.get('blocker'):
                L += [f"**The blocker:** {p['blocker']}", ""]
            if p.get('caveat'):
                L += [f"**Where this study is weak:** {p['caveat']}", ""]
            if p.get('thesis'):
                th = p['thesis']
                if th.get('what_must_be_true'):
                    L.append("What would have to be true:")
                    L += [f"- {w}" for w in th['what_must_be_true']]
                    L.append("")
                lb = th.get('load_bearing_claim') or {}
                if lb:
                    L += [f"**Load-bearing number:** {lb['claim']}",
                          f"**{lb['verdict']}** (checked against {lb['checked_against']})",
                          "", lb.get('note', ''), ""]
                if th.get('named_companies'):
                    L += ["| Company | Role | Claim | Reachable from a US brokerage? |",
                          "|---|---|---|---|"]
                    for c in th['named_companies']:
                        t = c.get('tradeable_us')
                        lab = ("yes" if t is True
                               else "nominal only" if t == 'nominal' else "NO")
                        L.append(f"| {c['name']} | {c['role']} | "
                                 f"{c.get('claim','')} ({c.get('verdict','')}) | "
                                 f"{lab}: {c.get('us_access','')} |")
                    L.append("")
                fc = th.get('fund_check')
                if fc:
                    L += [f"**{fc['headline']}**", ""]
                    L += [f"- {x}" for x in fc['detail']]
                    L += ["", f"Source: {fc['source']}", ""]
                for k in ('expression_note', 'marker_note'):
                    if th.get(k):
                        L += [th[k], ""]
            if p.get('gate') and theses:
                tr = theses.get('tradability', {})
                L += ["| Ticker | Gate | $ vol/day | Stale prints | Home corr | Why |",
                      "|---|---|---:|---:|---:|---|"]
                for t in sorted(tr):
                    r = tr[t]
                    if r.get('status') == 'NO DATA':
                        continue
                    hc = ("%.2f" % r['home_corr']) if r.get('home_corr') is not None else "-"
                    why = "; ".join(r.get('fails', [])) or "clears every measure"
                    L.append(f"| {t} | {r['status']} | ${r['dollar_vol']/1e6:,.2f}M | "
                             f"{r['stale_frac']:.0%} | {hc} | {why} |")
                L.append("")
                for t, r in tr.items():
                    if r.get('mom_self') is not None:
                        L += [f"**{t} is why this gate exists.** The trend screen reads "
                              f"{r['mom_self']:+.0%} twelve-month momentum on this line "
                              f"and {r['mom_home']:+.0%} on {r['home']} over the same "
                              f"window. Same company, correlating at {r['home_corr']:.2f}.",
                              ""]
            if p.get('tipverdicts') and tips:
                counts = tips.get('counts', {})
                qual = tips.get('quality', {}) or {}
                L += ["| Ticker | Trend | Quality | Both? | vs 200d | Ann. vol | Valuation | Flags |",
                      "|---|---|---|---|---:|---:|---|---|"]
                for r in tips.get('rows', []):
                    if r.get('verdict') == 'NO DATA':
                        continue
                    n = counts.get(r['ticker'], 0)
                    fl = []
                    if r.get('fragile'):
                        fl.append("FRAGILE")
                    if n >= 2:
                        fl.append(f"RECURS x{n}")
                    q = qual.get(r['ticker'], {})
                    if q.get('warnings'):
                        fl.append("EARNINGS FLAG")
                    ql = q.get('label', 'not run')
                    if q.get('score') is not None:
                        ql += f" {q['score']}/5"
                    if r['verdict'] == 'ELIGIBLE' and q.get('label') == 'QUALITY':
                        both = "FLAGGED" if q.get('warnings') else "YES"
                    else:
                        both = "no"
                    L.append(f"| {r['ticker']} | {r['verdict']} | {ql} | {both} | "
                             f"{r['pct_vs_sma']:+.0f}% | {r['vol']:.0f}% | "
                             f"{q.get('valuation','')[:56]} | {', '.join(fl)} |")
                L.append("")
                recur = {t: n for t, n in counts.items() if n >= 2}
                for _tk, q in (tips.get('quality', {}) or {}).items():
                    for w in q.get('warnings', []):
                        L.append(f"- **{_tk} earnings quality:** {w}")
                L.append("")
                L += ["Two screens, deliberately separate. Trend answers WHEN and "
                      "how much risk; quality answers WHAT is worth owning. A "
                      "high-conviction name passes both. Income-statement and "
                      "cash-flow figures come from SEC filings.", ""]
                if recur:
                    L += ["**Recurrence is a CAUTION flag, not a buy signal.** "
                          + ", ".join(f"{t} appears in {n} saved sources"
                                      for t, n in recur.items())
                          + ". A name showing up repeatedly is evidence it is being "
                            "MARKETED, which is orthogonal to whether it is a good "
                            "business.", ""]
            if p.get('claimledger') and tips:
                for src in tips.get('sources', []):
                    total = len(tips.get('markers', {})) or 5
                    L += [f"**{src.get('title', src['id'])}**, substance "
                          f"{src.get('substance', '?')}, {len(src.get('markers', []))} "
                          f"of {total} promotional markers.", ""]
                    if not src.get('claims'):
                        continue
                    L += ["| Claim | Check | Provenance |", "|---|---|---|"]
                    L += [f"| {c['claim']} | {c['verdict']} | content sourced |"
                          for c in src['claims']]
                    L.append("")
                    for c in src['claims']:
                        if c.get('note'):
                            L.append(f"- {c['verdict']}: {c['note']}")
                    L.append("")
                L += ["Every row above is content-sourced by definition. A VERIFIED "
                      "check means the claim survived comparison against a filing, "
                      "not that it is promoted to the validated tier. Nothing from "
                      "a video ever is.", ""]
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
    ap.add_argument('--no-portfolio', action='store_true',
                    help='skip the live model-portfolio panel (faster rebuild)')
    args = ap.parse_args()

    dry_run, dry_src = read_dry_run()
    if dry_run is not True:
        print(f"WARNING: DRY_RUN is not True ({dry_run} via {dry_src}). "
              f"Building anyway and flagging it on the page.")

    print("Fetching live market data...")
    live = fetch_live()
    macro = macro_reading(live)

    port = None
    if not args.no_portfolio:
        print("Computing the current Strategy C model portfolio...")
        try:
            port = current_portfolio()
        except Exception as exc:                      # never fail the whole build
            print(f"WARNING: model portfolio unavailable ({exc.__class__.__name__}: "
                  f"{exc}). The panel will be omitted.")

    theses = None
    th_path = REPO / "thesis_intake.json"
    if th_path.exists():
        theses = json.loads(th_path.read_text(encoding="utf-8"))

    tips = None
    tip_path = REPO / "tip_intake.json"
    if tip_path.exists():
        tips = json.loads(tip_path.read_text(encoding="utf-8"))
    else:
        print("NOTE: tip_intake.json not found. Run: python tip_intake.py "
              "--json tip_intake.json")

    res_path = REPO / "overnight_results.json"
    on_res = json.loads(res_path.read_text()) if res_path.exists() else None
    if on_res is None:
        print("NOTE: overnight_results.json not found. The overnight panel will be "
              "omitted. Run: python overnight_vs_intraday.py --json overnight_results.json")

    panels = build_panels(live, macro, on_res, port, tips, theses)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    html = out / "TRADING-DASHBOARD.html"
    md = out / "TRADING-DASHBOARD.md"
    html.write_text(render_html(panels, live, macro, on_res, dry_run, dry_src, port, tips, theses),
                    encoding="utf-8")
    md.write_text(render_md(panels, live, macro, on_res, dry_run, dry_src, port, tips, theses),
                  encoding="utf-8")
    print(f"Wrote {html}")
    print(f"Wrote {md}")
    print(f"\nRegime: {'RISK ON' if live['regime']['above'] else 'RISK OFF'} | "
          f"gold {macro['gold']}, oil {macro['oil']}, yields {macro['yield']}"
          f"{'  [claimed severe-downside cell]' if macro['severe'] else ''}")


if __name__ == "__main__":
    main()
