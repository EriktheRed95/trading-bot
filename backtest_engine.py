import warnings

import numpy as np
import pandas as pd
import yfinance as yf

warnings.simplefilter(action='ignore', category=FutureWarning)

from indicators import calculate_adx
from metrics import compute_metrics
from senses_macro import analyze_market_regime
from strategy_config import (
    COMMISSION_BPS,
    CORE_ALLOCATION,
    GRINDER_VOL_MIN,
    OPTION_BLOWOUT_THRESHOLD,
    OPTION_LEVERAGE,
    OPTION_THETA_PER_HOUR,
    ROCKET_ADX_MIN,
    ROCKET_VOL_MIN,
    SLIPPAGE_BPS,
    THRESHOLDS,
)
from system_strategy_evaluator import calculate_score

# Cost paid as a fraction of notional on each side of a trade.
_COST_PER_SIDE = (SLIPPAGE_BPS + COMMISSION_BPS) / 10_000.0

# Hourly bars used to warm up indicators AND classify the asset before trading begins.
# A bigger warm-up gives the classifier ~6 weeks of data instead of just SMA200's 200 hours.
WARMUP_BARS = 1000

BATCH_TICKERS = [
    'NVDA', 'PLTR', 'SMCI',
    'TSLA', 'RIVN', 'AMD',
    'F', 'INTC', 'PYPL',
    'KO', 'JPM', 'WMT',
    'SPY', 'QQQ',
]


def calculate_gamma_proxy(prices):
    """Acceleration of price (a rough gamma proxy)."""
    return prices.diff().diff()


def classify_asset_at(df, end_idx, lookback_bars=WARMUP_BARS):
    """Classify using only df.iloc[max(0, end_idx-lookback):end_idx].

    This is the key fix for look-ahead bias: the original classified using df.tail(1000)
    and a daily resample of the *entire* dataframe, so the bot "knew" the future
    volatility/trend of the stock when it set its identity at bar 0.
    """
    window = df.iloc[max(0, end_idx - lookback_bars):end_idx]
    if window.empty or 'adx' not in window.columns:
        return "FORTRESS", "Insufficient data", CORE_ALLOCATION['FORTRESS']

    avg_adx = window['adx'].mean()
    daily = window.set_index('Datetime')['close'].resample('D').last().pct_change().dropna()
    if len(daily) < 5:
        return "FORTRESS", "Insufficient daily data", CORE_ALLOCATION['FORTRESS']

    volatility = daily.std() * np.sqrt(252) * 100
    stats = f"Vol:{volatility:.0f}% ADX:{avg_adx:.0f}"

    if volatility > ROCKET_VOL_MIN and avg_adx > ROCKET_ADX_MIN:
        return "ROCKET", stats, CORE_ALLOCATION['ROCKET']
    if volatility > GRINDER_VOL_MIN:
        return "GRINDER", stats, CORE_ALLOCATION['GRINDER']
    return "FORTRESS", stats, CORE_ALLOCATION['FORTRESS']


def fetch_historical_data(ticker, period="729d", interval="1h"):
    try:
        df = yf.download(ticker, period=period, interval=interval, progress=False)
        if df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.reset_index(inplace=True)
        df.rename(columns={'Open': 'open', 'High': 'high', 'Low': 'low',
                           'Close': 'close', 'Volume': 'volume'}, inplace=True)
        col = 'Datetime' if 'Datetime' in df.columns else 'Date'
        df.rename(columns={col: 'Datetime'}, inplace=True)
        df['Datetime'] = pd.to_datetime(df['Datetime']).dt.tz_localize(None)

        df['adx'] = calculate_adx(df)
        df['gamma'] = calculate_gamma_proxy(df['close'])

        # Macro: VIX + TNX daily, plus pre-computed TNX % change so the backtester
        # can call analyze_market_regime(...) per bar without recomputing.
        macro = yf.download(['^VIX', '^TNX'], period=period, interval="1d", progress=False)['Close']
        macro.reset_index(inplace=True)
        macro.rename(columns={'^VIX': 'vix', '^TNX': 'tnx_yield', 'Date': 'Datetime'}, inplace=True)
        macro['Datetime'] = pd.to_datetime(macro['Datetime']).dt.tz_localize(None)
        macro['tnx_change_pct'] = macro['tnx_yield'].pct_change() * 100

        merged = pd.merge_asof(
            df.sort_values('Datetime'),
            macro.sort_values('Datetime'),
            on='Datetime',
            direction='backward',
        ).ffill().fillna(0)
        return merged
    except Exception as e:
        print(f"fetch_historical_data({ticker}) failed: {e}")
        return None


def _apply_cost(amount):
    """Subtract one side of slippage+commission from a cash amount."""
    return amount * (1.0 - _COST_PER_SIDE)


def _rsi_last(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / (avg_loss + 1e-9)
    return (100 - (100 / (1 + rs))).iloc[-1]


def run_smart_backtest(ticker, silent=False, period="729d", interval="1h",
                       warmup_bars=WARMUP_BARS, theta_per_bar=OPTION_THETA_PER_HOUR,
                       return_curves=False):
    df = fetch_historical_data(ticker, period=period, interval=interval)
    if df is None or len(df) <= warmup_bars + 1:
        return None

    # Classify using ONLY the warm-up window — no future data leakage.
    start_idx = warmup_bars
    identity, stats, core_allocation = classify_asset_at(df, start_idx, lookback_bars=warmup_bars)

    initial_cash = 10_000.00
    core_equity = initial_cash * core_allocation
    sat_equity = initial_cash * (1 - core_allocation)

    # Core position: buy-and-hold from start_idx, costs apply on entry.
    start_price = df.iloc[start_idx]['close']
    core_shares = _apply_cost(core_equity) / start_price

    sat_cash = sat_equity
    sat_units = 0.0
    sat_position = "NONE"  # NONE | CALL | PUT | STOCK_LONG | STOCK_SHORT
    sat_entry_price = 0.0

    trades = 0
    hours_in_market = 0

    def _sat_value(p):
        """Mark-to-market value of the satellite sleeve at price p."""
        if sat_position in ("CALL", "PUT"):
            return sat_units
        if sat_position == "STOCK_LONG":
            return sat_units * p
        if sat_position == "STOCK_SHORT":
            return sat_units * sat_entry_price + (sat_entry_price - p) * sat_units
        return sat_cash

    # Per-bar equity curves for the bot and the buy-and-hold benchmark.
    times, bot_equity, hold_equity = [], [], []

    th = THRESHOLDS.get(identity, THRESHOLDS['DEFAULT'])

    for i in range(start_idx + 1, len(df)):
        current_slice = df.iloc[i - 200: i + 1]['close']
        price = df.iloc[i]['close']
        prev_price = df.iloc[i - 1]['close']
        pct_change = (price - prev_price) / prev_price if prev_price else 0.0

        # Mark-to-market open synthetic options first (so we can detect blow-outs).
        if sat_position in ("CALL", "PUT"):
            if sat_position == "CALL":
                change = (pct_change * OPTION_LEVERAGE) - theta_per_bar
            else:
                change = (-pct_change * OPTION_LEVERAGE) - theta_per_bar
            sat_units = sat_units * (1 + change)
            if sat_units < sat_entry_price * OPTION_BLOWOUT_THRESHOLD:
                sat_units = 0.0
                sat_position = "NONE"

        sma50 = current_slice.iloc[-50:].mean()
        sma200 = current_slice.mean()
        rsi_val = _rsi_last(current_slice)
        gamma_val = df.iloc[i]['gamma']

        if sat_position != "NONE":
            hours_in_market += 1

        tech_score = calculate_score(df.iloc[i - 200: i + 1], ticker)['final_score']

        # Macro regime modifier — same code path the live bot uses.
        macro_modifier = analyze_market_regime({
            'vix': df.iloc[i]['vix'],
            'tnx_yield': df.iloc[i]['tnx_yield'],
            'tnx_change_pct': df.iloc[i]['tnx_change_pct'],
        })['score_modifier']
        total_score = tech_score + macro_modifier

        # ROCKET — synthetic options on the satellite portion.
        if identity == "ROCKET":
            if sat_position == "NONE":
                if total_score >= th['entry_long'] or (rsi_val < 50 and gamma_val > 0):
                    sat_units = _apply_cost(sat_cash)
                    sat_entry_price = sat_units
                    sat_cash = 0.0
                    sat_position = "CALL"
                    trades += 1
            elif sat_position == "CALL":
                if rsi_val > th['rsi_take'] or (total_score <= th['exit_long'] and price < sma50):
                    sat_cash = _apply_cost(sat_units)
                    sat_units = 0.0
                    sat_position = "NONE"
                    trades += 1

        # GRINDER — stock only, longs in uptrend / shorts in death trend.
        elif identity == "GRINDER":
            is_death_trend = price < sma200

            if sat_position == "NONE":
                if is_death_trend and total_score <= th['entry_short']:
                    effective_price = price * (1 - _COST_PER_SIDE)  # short fills slightly worse
                    sat_units = sat_cash / effective_price
                    sat_entry_price = effective_price
                    sat_cash = 0.0
                    sat_position = "STOCK_SHORT"
                    trades += 1
                elif (not is_death_trend) and total_score >= th['entry_long']:
                    effective_price = price * (1 + _COST_PER_SIDE)  # long fills slightly worse
                    sat_units = sat_cash / effective_price
                    sat_entry_price = effective_price
                    sat_cash = 0.0
                    sat_position = "STOCK_LONG"
                    trades += 1

            elif sat_position == "STOCK_LONG":
                if total_score <= th['exit_long'] or is_death_trend:
                    sat_cash = _apply_cost(sat_units * price)
                    sat_units = 0.0
                    sat_position = "NONE"
                    trades += 1

            elif sat_position == "STOCK_SHORT":
                if total_score >= th['exit_short'] or not is_death_trend:
                    profit = (sat_entry_price - price) * sat_units
                    sat_cash = _apply_cost(sat_units * sat_entry_price + profit)
                    sat_units = 0.0
                    sat_position = "NONE"
                    trades += 1

        # FORTRESS — safety; only deploys satellite cash on strong dips.
        elif identity == "FORTRESS":
            if sat_position == "NONE":
                if total_score >= th['entry_long']:
                    effective_price = price * (1 + _COST_PER_SIDE)
                    sat_units = sat_cash / effective_price
                    sat_cash = 0.0
                    sat_position = "STOCK_LONG"
                    trades += 1
            elif sat_position == "STOCK_LONG":
                if total_score <= th['exit_long']:
                    sat_cash = _apply_cost(sat_units * price)
                    sat_units = 0.0
                    sat_position = "NONE"
                    trades += 1

        # Record this bar's mark-to-market equity (after any trades).
        times.append(df.iloc[i]['Datetime'])
        bot_equity.append(core_shares * price + _sat_value(price))
        hold_equity.append(initial_cash * price / start_price)

    # Mark to market.
    final_price = df.iloc[-1]['close']
    total_final = core_shares * final_price + _sat_value(final_price)
    bot_ret = ((total_final - initial_cash) / initial_cash) * 100
    hold_ret = ((final_price - start_price) / start_price) * 100
    exposure_pct = (hours_in_market / (len(df) - start_idx)) * 100

    idx = pd.DatetimeIndex(times)
    bot_curve = pd.Series(bot_equity, index=idx)
    hold_curve = pd.Series(hold_equity, index=idx)
    bm = compute_metrics(bot_curve)
    hm = compute_metrics(hold_curve)

    result = {
        'ticker': ticker, 'id': identity, 'stats': stats,
        'bot_ret': bot_ret, 'hold_ret': hold_ret,
        'trades': trades, 'exp': exposure_pct,
        'winner': "BOT" if bot_ret > hold_ret else "HOLD",
        'bars': len(df) - start_idx,
        'start_date': str(df.iloc[start_idx]['Datetime'])[:10],
        'sharpe': bm['sharpe'], 'max_dd': bm['max_drawdown'], 'cagr': bm['cagr'],
        'ann_vol': bm['ann_vol'], 'calmar': bm['calmar'],
        'hold_sharpe': hm['sharpe'], 'hold_max_dd': hm['max_drawdown'],
        'hold_cagr': hm['cagr'],
    }
    if return_curves:
        result['bot_curve'] = bot_curve
        result['hold_curve'] = hold_curve
    return result


def run_batch_test():
    print(f"\nOPTIONS & GREEKS BATTLE  (slippage {SLIPPAGE_BPS}bps + commission {COMMISSION_BPS}bps per side)")
    print(f"{'TICKER':<6} | {'ID':<8} | {'STATS':<16} | {'BOT %':<8} | {'HOLD %':<8} | {'TRD':<3} | {'WIN'}")
    print("-" * 80)

    results = []
    for ticker in BATCH_TICKERS:
        res = run_smart_backtest(ticker, silent=True)
        if res:
            print(f"{res['ticker']:<6} | {res['id']:<8} | {res['stats']:<16} | "
                  f"{res['bot_ret']:>8.0f}% | {res['hold_ret']:>8.0f}% | "
                  f"{res['trades']:>3} | {res['winner']}")
            results.append(res)

    print("-" * 80)
    wins = sum(1 for r in results if r['winner'] == "BOT")
    print(f"SCORE: Bot wins {wins}/{len(results)}")


if __name__ == "__main__":
    run_batch_test()
