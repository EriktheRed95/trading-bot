"""Shared, completed-session Strategy C targets. No broker, credentials or orders."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import math
import numpy as np
import pandas as pd
from strategy_c import BROAD_UNIVERSE, RISK_OFF_TICKERS, MARKET, load_prices, _zscore
from paper_book import session_close, SESSION_CLOSE_BASIS


class DataUnavailable(ValueError):
    pass


def signal_snapshot(close=None, *, now=None, top_n=10):
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(ZoneInfo('America/New_York'))
    if close is None:
        close = load_prices(BROAD_UNIVERSE + list(RISK_OFF_TICKERS) + ['QQQ','AGG','BIL'], period='2y')
    close = close.copy().sort_index()
    close.index = pd.to_datetime(close.index).tz_localize(None).normalize()
    close = close[~close.index.duplicated(keep='last')]
    # Before the close has settled, today's daily candle is not a completed bar.
    today = pd.Timestamp(local.date())
    close = close.loc[close.index <= today]
    if (local.hour, local.minute) < (16, 15):
        close = close.loc[close.index < today]
    if MARKET not in close or close.empty:
        raise DataUnavailable('No completed SPY session is available.')
    market = close[MARKET].dropna()
    if len(market) < 253:
        raise DataUnavailable('SPY needs at least 253 completed observations.')
    date = market.index[-1]
    if (today - date).days > 4:
        raise DataUnavailable('Market data is stale; paper orders are held.')
    close = close.loc[close.index <= date].reindex(market.index)
    close = close.where(np.isfinite(close) & (close > 0))
    # No forward filling: missing observations must not create tradable prices.
    valid = [t for t in close if close[t].iloc[-253:].notna().all()]
    if MARKET not in valid:
        raise DataUnavailable('SPY has missing or invalid price observations.')
    prices = {t: float(close[t].iloc[-1]) for t in close if pd.notna(close[t].iloc[-1])}
    sma = close.rolling(200).mean()
    returns = close.pct_change(fill_method=None)
    vol = returns.rolling(63).std() * np.sqrt(252)
    long = close.shift(21) / close.shift(252) - 1
    mid = close.shift(21) / close.shift(126) - 1
    trend = close / sma - 1
    eligible = [t for t in BROAD_UNIVERSE if t in valid
                and close.at[date,t] > sma.at[date,t] and long.at[date,t] > 0
                and math.isfinite(vol.at[date,t]) and vol.at[date,t] > 0]
    risk_on = bool(close.at[date,MARKET] > sma.at[date,MARKET])
    weights = {}
    if risk_on and eligible:
        score = _zscore(long.loc[date,eligible]) + _zscore(mid.loc[date,eligible]) + _zscore(trend.loc[date,eligible])
        picks = score.sort_values(ascending=False).head(top_n).index
        inv = 1 / vol.loc[date,picks]
        weights = {t: float(v) for t,v in (inv / inv.sum()).items()}
    elif not risk_on:
        # A missing defensive asset is not silently interpreted as a downtrend.
        if any(t not in valid for t in RISK_OFF_TICKERS):
            raise DataUnavailable('Defensive sleeve data is incomplete; orders are held.')
        picks = [t for t in RISK_OFF_TICKERS if close.at[date,t] > sma.at[date,t]]
        weights = {t: 1/len(picks) for t in picks}
    missing = [t for t in BROAD_UNIVERSE if t not in valid]
    if risk_on and len(set(valid).intersection(BROAD_UNIVERSE)) < max(10, math.ceil(len(BROAD_UNIVERSE)*0.8)):
        raise DataUnavailable('Less than 80% of the stock universe has complete data; orders are held.')
    # Provenance: the bar's fail-closed completion time (13:00 New York, the
    # earliest regular close; no exchange calendar is available) and the actual
    # observation time. The paper book fills a queued signal only on a bar that
    # completed after the signal's fetched_at, and never trusts a later bar_end.
    return {'asof': str(date.date()), 'fetched_at': now.isoformat(),
            'bar_end': session_close(str(date.date())).isoformat(), 'bar_end_basis': SESSION_CLOSE_BASIS, 'risk_on': risk_on,
            'strategy': 'Strategy C / paper v1', 'target_weights': weights, 'prices': prices,
            'eligible': len(eligible), 'excluded': missing,
            'holdings': [{'ticker':t, 'weight':w*100, 'price':prices[t],
                          'vol':float(vol.at[date,t])*100,
                          'above_sma':float(trend.at[date,t])*100, 'score':None}
                         for t,w in weights.items()],
            'spy': prices[MARKET], 'sma200':float(sma.at[date,MARKET]),
            'cash_pct':(1-sum(weights.values()))*100}
