"""Strategy C — trend-following, regime-gated, long-only ranking allocator.

The answer to "what would actually make the bot win?": flip the edge from
mean-reversion (which fights trends) to trend-following, never short, hold by
default, and only sit out when the regime turns. One ranking brain combines:

  1. MARKET REGIME GATE   — if SPY is below its 200d SMA, leave equities.
  2. PER-NAME TREND FILTER — eligible only if above its own 200d SMA AND has
                             positive 12-1 month momentum.
  3. RANKING BRAIN        — eligible names scored by a z-blend of 12-1 momentum,
                             6-1 momentum, and trend strength (% above 200d).
  4. SELECTION            — hold the top-N scored names.
  5. INVERSE-VOL SIZING   — weight by 1/recent-vol to reduce standalone volatility exposure (not equal correlated risk).
  6. RISK-OFF SLEEVE      — when out of equities, park in cash / gold / Treasuries
                             (configurable) instead of earning nothing.
  7. MONTHLY REBALANCE    — low turnover; costs charged on weight changes.

Universe is a broad ~65-name large-cap pool: the ranker does the selecting, so
this is far less hand-picked than a curated winners' list (a pragmatic step
toward survivorship-fairness; the fully-correct fix is point-in-time index
membership data, which yfinance does not provide).
"""
import numpy as np
import pandas as pd
import yfinance as yf

from metrics import compute_metrics
from strategy_config import COMMISSION_BPS, SLIPPAGE_BPS

COST_PER_SIDE = (SLIPPAGE_BPS + COMMISSION_BPS) / 10_000.0
MARKET = 'SPY'

# Broad large-cap pool across sectors. Newer names join when they have history.
BROAD_UNIVERSE = [
    # tech / semis
    'AAPL', 'MSFT', 'NVDA', 'AMD', 'INTC', 'AVGO', 'QCOM', 'TXN', 'MU', 'AMAT', 'ADI', 'LRCX',
    # software / internet
    'ORCL', 'CRM', 'ADBE', 'CSCO', 'IBM', 'GOOGL', 'META', 'AMZN', 'NFLX', 'UBER',
    # communications
    'T', 'VZ', 'CMCSA', 'DIS',
    # consumer
    'KO', 'PEP', 'PG', 'WMT', 'COST', 'MCD', 'NKE', 'SBUX', 'HD', 'LOW', 'TGT',
    # financials
    'JPM', 'BAC', 'WFC', 'C', 'GS', 'MS', 'AXP', 'BRK-B',
    # health
    'JNJ', 'PFE', 'MRK', 'ABT', 'UNH', 'LLY', 'ABBV', 'AMGN', 'GILD', 'BMY',
    # industrial / energy
    'GE', 'CAT', 'BA', 'HON', 'XOM', 'CVX', 'UNP',
    # autos
    'TSLA', 'F', 'GM',
]
RISK_OFF_TICKERS = ['GLD', 'TLT']  # gold ETF (2004+), long Treasuries ETF (2002+)


def load_prices(tickers, period="max", interval="1d"):
    """Adjusted daily closes, one column per ticker."""
    tickers = sorted(set(tickers) | {MARKET})
    data = yf.download(tickers, period=period, interval=interval,
                       auto_adjust=True, progress=False)
    close = data['Close'] if isinstance(data.columns, pd.MultiIndex) else data[['Close']]
    close.index = pd.to_datetime(close.index).tz_localize(None)
    return close.sort_index()


def _zscore(s):
    sd = s.std()
    return (s - s.mean()) / sd if sd and not np.isnan(sd) else s * 0.0


def run_allocator(close, holdable, top_n=10, sma_window=200, mom_lookback=252,
                  mom_skip=21, mom_mid=126, vol_lookback=63, risk_off=None,
                  risk_off_candidates=('GLD', 'TLT'), holdable_fn=None,
                  sentiment_df=None, sentiment_weight=0.0, orthogonalize=False,
                  evaluation_start=None, rebalance_schedule='first'):
    """Simulate the allocator. risk_off is one of:
        None            -> sit in cash when defensive
        {ticker: wt}    -> fixed sleeve (e.g. {'GLD':0.5,'TLT':0.5})
        'dynamic'       -> flight-to-what's-working: hold each candidate only
                           while it's above its own 200d SMA, else cash
    holdable_fn(date) -> set of tickers eligible to hold as of that date
        (point-in-time index membership); falls back to the static `holdable`.
    Returns (equity Series, stats dict, weights DataFrame)."""
    close = close.sort_index().copy()
    holdable = [t for t in holdable if t in close.columns]
    dynamic = (risk_off == 'dynamic')
    if dynamic:
        riskoff_cols = [t for t in risk_off_candidates if t in close.columns]
    else:
        riskoff = risk_off or {}
        riskoff_cols = [t for t in riskoff if t in close.columns]
    cols = list(dict.fromkeys(holdable + riskoff_cols))
    rets = close.pct_change(fill_method=None)

    sma = close.rolling(sma_window).mean()
    mom_long = close.shift(mom_skip) / close.shift(mom_lookback) - 1.0   # 12-1 month
    mom_mid_s = close.shift(mom_skip) / close.shift(mom_mid) - 1.0       # 6-1 month
    trend = close / sma - 1.0                                            # % above 200d
    vol = rets.rolling(vol_lookback).std() * np.sqrt(252)
    risk_on = close[MARKET] > close[MARKET].rolling(sma_window).mean()

    # Match live monthly planning: first observed session, after full warm-up.
    grouped=close.iloc[mom_lookback:].groupby(close.iloc[mom_lookback:].index.to_period('M'))
    if rebalance_schedule not in ('first','last'):
        raise ValueError('Unknown monthly schedule')
    rb_days=(grouped.head(1) if rebalance_schedule=='first' else grouped.tail(1)).index

    targets, turnover = {}, pd.Series(0.0, index=close.index)
    prev = pd.Series(0.0, index=cols)
    for d in rb_days:
        if evaluation_start is not None and d < pd.Timestamp(evaluation_start):
            continue
        if not np.isfinite(close.at[d, MARKET]):
            raise ValueError('Missing market gate price')
        target = pd.Series(0.0, index=cols)
        invested = False
        if bool(risk_on.loc[d]):
            hold_today = holdable_fn(d) if holdable_fn else holdable
            elig = [t for t in hold_today
                    if t in close.columns
                    and close.at[d, t] > sma.at[d, t]
                    and mom_long.at[d, t] > 0
                    and close[t].loc[:d].tail(mom_lookback+1).notna().all()
                    and not np.isnan(vol.at[d, t]) and vol.at[d, t] > 0]
            if elig:
                comp = (_zscore(mom_long.loc[d, elig])
                        + _zscore(mom_mid_s.loc[d, elig])
                        + _zscore(trend.loc[d, elig]))
                if sentiment_df is not None and sentiment_weight:
                    # Use the PREVIOUS completed month's sentiment (no look-ahead).
                    pm = str(d.to_period('M') - 1)
                    srow = sentiment_df.loc[pm] if pm in sentiment_df.index else None
                    svec = pd.Series(
                        [srow.get(t, 0.0) if srow is not None else 0.0 for t in elig],
                        index=elig).fillna(0.0)
                    sz = _zscore(svec)
                    if orthogonalize:
                        # Residualize sentiment against the momentum/trend score:
                        # only the part NOT explained by momentum tilts the rank.
                        cz = _zscore(comp)
                        denom = float((cz * cz).sum())
                        beta = float((sz * cz).sum()) / denom if denom else 0.0
                        sz = sz - beta * cz
                    comp = comp + sentiment_weight * sz
                picks = comp.sort_values(ascending=False).head(top_n).index
                inv = 1.0 / vol.loc[d, picks]
                target.loc[picks] = (inv / inv.sum()).values
                invested = True
        if not invested and not bool(risk_on.loc[d]):
            if dynamic:
                # Hold each risk-off candidate only while it's trending up.
                trending = [c for c in riskoff_cols
                            if not np.isnan(close.at[d, c]) and close.at[d, c] > sma.at[d, c]]
                for c in trending:
                    target.loc[c] = 1.0 / len(trending)   # else all cash
            else:
                for t, wt in riskoff.items():
                    if t in riskoff_cols and not np.isnan(close.at[d, t]):
                        target.loc[t] = wt
        turnover.loc[d] = (target - prev).abs().sum()
        targets[d] = target
        prev = target

    from execution_model import simulate
    valuation = close[cols] if evaluation_start is None else close.loc[evaluation_start:,cols]
    equity, weights, fills = simulate(valuation,
        {d:{t:float(w) for t,w in target.items() if w > 0} for d,target in targets.items()},
        cost_rate=COST_PER_SIDE)
    turns=[]
    for date in sorted({f['asof'] for f in fills}):
        batch=[f for f in fills if f['asof']==date]
        before=equity.loc[pd.Timestamp(date)]+sum(f['cost'] for f in batch)
        turns.append(sum(abs(f['shares']*f['price']) for f in batch)/before)
    stats = {'rebalances': len(targets), 'fills':len(fills),
             'costs':sum(f['cost'] for f in fills),
             'avg_turnover':float(np.mean(turns)) if turns else 0.,
             'accounting':'fixed shares, next observed close, post-cost cash-funded targets'}
    return equity, stats, weights



# Sector map for the broad pool (used by the optional sector cap).
SECTORS = {}
for _grp, _names in {
    'tech': ['AAPL', 'MSFT', 'NVDA', 'AMD', 'INTC', 'AVGO', 'QCOM', 'TXN', 'MU',
             'AMAT', 'ADI', 'LRCX', 'ORCL', 'CRM', 'ADBE', 'CSCO', 'IBM'],
    'internet': ['GOOGL', 'META', 'AMZN', 'NFLX', 'UBER'],
    'comm': ['T', 'VZ', 'CMCSA', 'DIS'],
    'consumer': ['KO', 'PEP', 'PG', 'WMT', 'COST', 'MCD', 'NKE', 'SBUX', 'HD', 'LOW', 'TGT'],
    'financials': ['JPM', 'BAC', 'WFC', 'C', 'GS', 'MS', 'AXP', 'BRK-B'],
    'health': ['JNJ', 'PFE', 'MRK', 'ABT', 'UNH', 'LLY', 'ABBV', 'AMGN', 'GILD', 'BMY'],
    'industrial_energy': ['GE', 'CAT', 'BA', 'HON', 'XOM', 'CVX', 'UNP'],
    'auto': ['TSLA', 'F', 'GM'],
}.items():
    for _t in _names:
        SECTORS[_t] = _grp


def _apply_caps(weights, max_weight=None, sector_map=None, sector_cap=None):
    """Cap individual (and optionally per-sector) weights. Excess that can't be
    redistributed under the caps stays in cash (sum may fall below 1)."""
    w = dict(weights)
    if max_weight:
        for _ in range(50):
            over = {t: v for t, v in w.items() if v > max_weight + 1e-9}
            if not over:
                break
            excess = sum(v - max_weight for v in over.values())
            for t in over:
                w[t] = max_weight
            under = [t for t in w if w[t] < max_weight - 1e-9]
            pool = sum(w[t] for t in under)
            if not under or pool <= 0:
                break
            for t in under:
                w[t] += excess * (w[t] / pool)
    if sector_map and sector_cap:
        sec_tot = {}
        for t, v in w.items():
            sec_tot[sector_map.get(t, '?')] = sec_tot.get(sector_map.get(t, '?'), 0) + v
        for s, tot in sec_tot.items():
            if tot > sector_cap + 1e-9:
                scale = sector_cap / tot
                for t in [x for x in w if sector_map.get(x, '?') == s]:
                    w[t] *= scale          # trimmed sector weight -> remainder cash
    return w


def run_with_overlay(close, holdable, top_n=10, sma_window=200, mom_lookback=252,
                     mom_skip=21, mom_mid=126, vol_lookback=63, risk_off='dynamic',
                     risk_off_candidates=('GLD', 'TLT'), holdable_fn=None,
                     max_weight=None, trailing_stop=None, sector_map=None, sector_cap=None,
                     stop_below_sma=False, require_both=False):
    """Daily holdings simulation with a name-level risk overlay:
    intra-month stop + per-name / per-sector position caps.

    Stop options (applied only to equity holdings, intra-month):
      trailing_stop   -> exit if drawdown from peak exceeds this (magnitude)
      stop_below_sma  -> exit if price falls below its own 200d SMA (trend break)
      require_both    -> with both set, only exit when BOTH fire (disaster-only:
                         a confirmed downtrend AND a big drawdown, so normal
                         pullbacks in healthy uptrends are ignored)
    Overlay params default off -> a plain monthly-rebalanced baseline.
    Returns (equity Series, stats dict)."""
    close = close.sort_index().copy()
    rets = close.pct_change(fill_method=None)
    sma = close.rolling(sma_window).mean()
    mom_long = close.shift(mom_skip) / close.shift(mom_lookback) - 1.0
    mom_mid_s = close.shift(mom_skip) / close.shift(mom_mid) - 1.0
    trend = close / sma - 1.0
    vol = rets.rolling(vol_lookback).std() * np.sqrt(252)
    mkt = close[MARKET].values
    mkt_sma = close[MARKET].rolling(sma_window).mean().values

    holdable = [t for t in holdable if t in close.columns]
    dynamic = (risk_off == 'dynamic')
    if dynamic:
        riskoff_cols = [t for t in risk_off_candidates if t in close.columns]
    elif isinstance(risk_off, dict):
        riskoff_cols = [t for t in risk_off if t in close.columns]
    else:
        riskoff_cols = []

    cols = set(holdable + riskoff_cols)
    CL = {t: close[t].values for t in cols}
    SM = {t: sma[t].values for t in cols}
    ML = {t: mom_long[t].values for t in holdable}
    MM = {t: mom_mid_s[t].values for t in holdable}
    TR = {t: trend[t].values for t in holdable}
    VL = {t: vol[t].values for t in holdable}
    RV = {t: rets[t].values for t in cols}

    dates = close.index
    warm=close.iloc[mom_lookback:]
    rb_set=set(warm.groupby(warm.index.to_period('M')).head(1).index)

    def _z(a):
        sd = a.std()
        return (a - a.mean()) / sd if sd else a * 0.0

    def target(i, d):
        if mkt[i] > mkt_sma[i]:                       # risk-on
            today = holdable_fn(d) if holdable_fn else holdable
            elig = [t for t in today if t in CL
                    and CL[t][i] > SM[t][i] and ML[t][i] > 0
                    and not np.isnan(VL[t][i]) and VL[t][i] > 0]
            if elig:
                comp = (_z(np.array([ML[t][i] for t in elig]))
                        + _z(np.array([MM[t][i] for t in elig]))
                        + _z(np.array([TR[t][i] for t in elig])))
                order = np.argsort(comp)[::-1][:top_n]
                picks = [elig[j] for j in order]
                inv = np.array([1.0 / VL[t][i] for t in picks])
                w = dict(zip(picks, inv / inv.sum()))
                return _apply_caps(w, max_weight, sector_map, sector_cap)
        if dynamic:
            up = [c for c in riskoff_cols if not np.isnan(CL[c][i]) and CL[c][i] > SM[c][i]]
            return {c: 1.0 / len(up) for c in up} if up else {}
        if isinstance(risk_off, dict):
            return {t: wt for t, wt in risk_off.items()
                    if t in CL and not np.isnan(CL[t][i])}
        return {}

    from execution_model import rebalance
    cash, shares, peak, pending = 10000., {}, {}, None
    values, turns = {}, []
    for i,d in enumerate(dates):
        prices=close.loc[d].to_dict()
        if any(not np.isfinite(prices[t]) or prices[t]<=0 for t in shares):
            raise ValueError(f'Missing held price on {d}')
        if pending is not None:
            before=cash+sum(q*prices[t] for t,q in shares.items())
            cash,shares,fills=rebalance(cash,shares,prices,pending,COST_PER_SIDE)
            turns.append(sum(abs(f['shares']*f['price']) for f in fills)/before)
            peak={t:peak.get(t,prices[t]) for t in shares}
            pending=None
        for t in shares:peak[t]=max(peak.get(t,prices[t]),prices[t])
        total=cash+sum(q*prices[t] for t,q in shares.items())
        values[d]=total
        exits=[]
        for t in shares:
            if t not in holdable:continue
            dd_hit=bool(trailing_stop) and prices[t]<=peak[t]*(1-trailing_stop)
            sma_hit=stop_below_sma and prices[t]<SM[t][i]
            if ((dd_hit and sma_hit) if require_both else (dd_hit or sma_hit)):exits.append(t)
        if d in rb_set:
            pending=target(i,d)
            for t in exits:pending.pop(t,None)
        elif exits:
            # Only exits are requested; remaining allocations are based on
            # observed values. Execution occurs at the NEXT observed close.
            pending={t:q*prices[t]/total for t,q in shares.items() if t not in exits}
    return pd.Series(values), {'avg_turnover':float(np.mean(turns)) if turns else 0.,
                              'accounting':'fixed shares; next-close orders and costed exits'}



def ew_index(close, universe):
    """Legacy frictionless daily-rebalanced comparator; NOT buy-and-hold or bias-free.
    Use validation_report.py for costed matched-window benchmarks.
    """
    cols = [c for c in universe if c in close.columns]
    rets = close[cols].pct_change()
    avail = close[cols].notna() & close[cols].shift(1).notna()
    return 10_000 * (1 + rets.where(avail).mean(axis=1).fillna(0.0)).cumprod()


def _rebased(series, start):
    s = series.loc[start:].dropna()
    return s / s.iloc[0] * 10_000


if __name__ == "__main__":
    print("Loading max history for broad universe...")
    close = load_prices(BROAD_UNIVERSE + RISK_OFF_TICKERS)
    print(f"Data: {close.index[0].date()} -> {close.index[-1].date()}  "
          f"({len(close)} days, {close.shape[1]} tickers)")

    start = close[MARKET].dropna().index[200]
    configs = {
        'Cash': None,
        'Gold (GLD)': {'GLD': 1.0},
        'Treasuries (TLT)': {'TLT': 1.0},
        'Gold+Tsy 50/50': {'GLD': 0.5, 'TLT': 0.5},
        'Dynamic (trend)': 'dynamic',
    }

    print(f"\nWindow: {start.date()} -> {close.index[-1].date()}\n")
    print(f"{'Strategy C risk-off':<22} {'CAGR':>6} {'Sharpe':>7} {'maxDD':>7} "
          f"{'Calmar':>7} {'Inv%':>6}")
    print("-" * 60)
    curves, wts = {}, {}
    for name, ro in configs.items():
        eq, stats, w = run_allocator(close, BROAD_UNIVERSE, top_n=10, risk_off=ro)
        eqw = _rebased(eq, start)
        inv = (w[[c for c in BROAD_UNIVERSE if c in w.columns]]
               .loc[start:].sum(axis=1) > 1e-9).mean() * 100
        m = compute_metrics(eqw)
        curves[name], wts[name] = eqw, w
        print(f"{name:<22} {m['cagr']:>5.1f}% {m['sharpe']:>7.2f} "
              f"{m['max_drawdown']:>6.0f}% {m['calmar']:>7.2f} {inv:>5.0f}%")

    spy = _rebased(close[MARKET], start)
    ewb = _rebased(ew_index(close, BROAD_UNIVERSE), start)
    for label, c in [('SPY buy & hold', spy), ('EW-hold broad pool', ewb)]:
        m = compute_metrics(c)
        print(f"{label:<22} {m['cagr']:>5.1f}% {m['sharpe']:>7.2f} "
              f"{m['max_drawdown']:>6.0f}% {m['calmar']:>7.2f}      -")

    # What did the DYNAMIC sleeve actually choose, and how often?
    w = wts['Dynamic (trend)'].loc[start:]
    hold_cols = [c for c in BROAD_UNIVERSE if c in w.columns]
    defensive = w[hold_cols].sum(axis=1) <= 1e-9
    n = int(defensive.sum())
    gold = (w['GLD'] > 0) if 'GLD' in w else pd.Series(False, index=w.index)
    tsy = (w['TLT'] > 0) if 'TLT' in w else pd.Series(False, index=w.index)
    print(f"\nDynamic sleeve - of the {defensive.mean()*100:.0f}% of time spent defensive:")
    print(f"  gold+tsy {(gold & tsy & defensive).sum()/n*100:>3.0f}%   "
          f"gold only {(gold & ~tsy & defensive).sum()/n*100:>3.0f}%   "
          f"tsy only {(tsy & ~gold & defensive).sum()/n*100:>3.0f}%   "
          f"cash {(defensive & ~gold & ~tsy).sum()/n*100:>3.0f}%")

    curves['Dynamic (trend)'].to_csv("strategy_c_equity.csv")
    from plot_equity import plot_equity
    plot_equity(curves['Dynamic (trend)'], spy,
                title=(f"Strategy C (broad pool, dynamic risk-off) vs benchmarks\n"
                       f"{start.date()} -> {close.index[-1].date()}"),
                out_path="strategy_c.png",
                extras=[(ewb, "EW-hold broad pool", "#e8710a"),
                        (curves['Gold+Tsy 50/50'], "Static Gold+Tsy 50/50", "#9c27b0")])
