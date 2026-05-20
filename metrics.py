"""Risk/return metrics computed from an equity curve.

Everything here works on a pandas Series of portfolio value indexed by
timestamp, so it's bar-resolution-agnostic: the annualization factor is
inferred from the timestamps (hourly, daily, weekly all just work).

"Winning" against buy-and-hold is judged here on risk-adjusted terms
(Sharpe, max drawdown, Calmar) — not raw total return alone.
"""
import numpy as np
import pandas as pd

TRADING_DAYS = 252


def periods_per_year(index):
    """Infer how many bars make up a year from the timestamp spacing."""
    idx = pd.DatetimeIndex(index)
    if len(idx) < 2:
        return TRADING_DAYS
    span_years = (idx[-1] - idx[0]).total_seconds() / (365.25 * 24 * 3600)
    if span_years <= 0:
        return TRADING_DAYS
    return (len(idx) - 1) / span_years


def drawdown_series(equity):
    """Underwater curve: fractional drop from the running peak (<= 0)."""
    equity = pd.Series(equity).astype(float)
    return equity / equity.cummax() - 1.0


def compute_metrics(equity, rf_annual=0.0):
    """Return a dict of summary stats for an equity curve.

    Keys: total_return, cagr, ann_vol, sharpe, max_drawdown, calmar, years
    (all percentages except sharpe, calmar, years).
    """
    equity = pd.Series(equity).astype(float).dropna()
    if len(equity) < 2 or equity.iloc[0] <= 0:
        return dict(total_return=0.0, cagr=0.0, ann_vol=0.0, sharpe=0.0,
                    max_drawdown=0.0, calmar=0.0, years=0.0)

    ppy = periods_per_year(equity.index)
    returns = equity.pct_change().dropna()

    total_return = (equity.iloc[-1] / equity.iloc[0] - 1.0) * 100
    years = max((pd.DatetimeIndex(equity.index)[-1]
                 - pd.DatetimeIndex(equity.index)[0]).total_seconds()
                / (365.25 * 24 * 3600), 1e-9)
    cagr = ((equity.iloc[-1] / equity.iloc[0]) ** (1.0 / years) - 1.0) * 100

    std = returns.std()
    ann_vol = std * np.sqrt(ppy) * 100
    if std and not np.isnan(std):
        rf_per = rf_annual / ppy
        sharpe = (returns.mean() - rf_per) / std * np.sqrt(ppy)
    else:
        sharpe = 0.0

    max_dd = drawdown_series(equity).min() * 100
    calmar = cagr / abs(max_dd) if max_dd else 0.0

    return dict(total_return=total_return, cagr=cagr, ann_vol=ann_vol,
                sharpe=float(sharpe), max_drawdown=max_dd,
                calmar=float(calmar), years=years)
