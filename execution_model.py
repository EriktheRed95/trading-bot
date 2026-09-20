"""Cash-funded fixed-share accounting shared by historical and paper experiments."""
import math
import pandas as pd


def rebalance(cash, shares, prices, target, cost_rate):
    if not 0 <= cost_rate < .1:
        raise ValueError('Invalid execution cost')
    if any(not math.isfinite(w) or w < 0 for w in target.values()) or sum(target.values()) > 1.00000001:
        raise ValueError('Invalid long-only target')
    symbols = set(shares) | {t for t,w in target.items() if w > 0}
    if any(t not in prices or not math.isfinite(prices[t]) or prices[t] <= 0 for t in symbols):
        raise ValueError('Missing executable price; cannot invent a fill')
    total = cash + sum(q*prices[t] for t,q in shares.items())
    # Solve post-cost NAV. Targets apply to NAV after paying actual dollar turnover.
    low, high = 0., total
    for _ in range(60):
        nav = (low+high)/2
        turnover = sum(abs(nav*target.get(t,0)-shares.get(t,0)*prices[t]) for t in symbols)
        if nav + turnover*cost_rate > total:
            high = nav
        else:
            low = nav
    desired = {t:low*w/prices[t] for t,w in target.items() if w > 0}
    fills = []
    for t in sorted(symbols):
        delta = desired.get(t,0)-shares.get(t,0)
        if abs(delta) > 1e-10:
            fee = abs(delta*prices[t])*cost_rate
            cash -= delta*prices[t]+fee
            fills.append({'ticker':t,'shares':delta,'price':prices[t],'cost':fee})
    if cash < -1e-7:
        raise ValueError('Cash cannot be negative')
    return max(0.,cash), desired, fills


def simulate(close, targets, cost_rate=.0006, initial=10000.):
    """Targets observed at close t fill at close t+1, never earn t->t+1 return.

    Missing held prices invalidate a run. No forward-filled execution prices or
    silently deleted/delisted holdings. Prices are adjusted total-return units.
    Returned equity includes initial cash on the first signal date, hence entry
    fees remain in measured returns. Targets are sparse events, not daily weights.
    """
    close = close.sort_index()
    if close.index.has_duplicates:
        raise ValueError('Duplicate bars')
    cash, shares, pending = initial, {}, None
    equity, weights, fills = {}, {}, []
    for date, row in close.iterrows():
        prices = row.to_dict()
        if any(t not in prices or not math.isfinite(prices[t]) or prices[t] <= 0 for t in shares):
            raise ValueError(f'Missing held price on {date}; history is not valid')
        if pending is not None:
            signal_date, target = pending
            cash, shares, changes = rebalance(cash,shares,prices,target,cost_rate)
            fills.extend(dict(f,asof=str(date),signal_date=str(signal_date)) for f in changes)
            pending = None
        total = cash + sum(q*prices[t] for t,q in shares.items())
        equity[date] = total
        weights[date] = {t:q*prices[t]/total for t,q in shares.items()}
        if date in targets:
            pending = date, targets[date]
    return pd.Series(equity,dtype=float), pd.DataFrame.from_dict(weights,orient='index').reindex(close.index).fillna(0.), fills
