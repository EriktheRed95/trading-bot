import pandas as pd
import yfinance as yf
import numpy as np
import warnings

# --- 1. CLEANUP ---
warnings.simplefilter(action='ignore', category=FutureWarning)

# --- IMPORT YOUR BRAIN ---
from system_strategy_evaluator import calculate_score
from senses_macro import analyze_market_regime

# --- CONFIGURATION ---
BATCH_TICKERS = [
    'NVDA', 'PLTR', 'SMCI', # ROCKETS
    'TSLA', 'RIVN', 'AMD',  # CHAOS
    'F', 'INTC', 'PYPL',    # FALLEN
    'KO', 'JPM', 'WMT',     # FORTRESS
    'SPY', 'QQQ'            # INDEXES
]

def calculate_adx_proper(df, period=14):
    """ Standard Wilder's ADX (Verified). """
    df = df.copy()
    df['h-l'] = df['high'] - df['low']
    df['h-pc'] = abs(df['high'] - df['close'].shift(1))
    df['l-pc'] = abs(df['low'] - df['close'].shift(1))
    df['tr'] = df[['h-l', 'h-pc', 'l-pc']].max(axis=1)
    df['up_move'] = df['high'] - df['high'].shift(1)
    df['down_move'] = df['low'].shift(1) - df['low']
    df['plus_dm'] = np.where((df['up_move'] > df['down_move']) & (df['up_move'] > 0), df['up_move'], 0)
    df['minus_dm'] = np.where((df['down_move'] > df['up_move']) & (df['down_move'] > 0), df['down_move'], 0)
    alpha = 1/period
    tr = df['tr'].ewm(alpha=alpha, adjust=False).mean()
    plus_dm = df['plus_dm'].ewm(alpha=alpha, adjust=False).mean()
    minus_dm = df['minus_dm'].ewm(alpha=alpha, adjust=False).mean()
    tr = tr.replace(0, 1e-9) 
    plus_di = 100 * (plus_dm / tr)
    minus_di = 100 * (minus_dm / tr)
    denom = plus_di + minus_di
    denom = denom.replace(0, 1e-9)
    dx = 100 * abs(plus_di - minus_di) / denom
    adx = dx.ewm(alpha=alpha, adjust=False).mean()
    return adx.fillna(20)

def classify_asset(df):
    """
    Identifies Stock Type.
    """
    recent = df.tail(1000)
    avg_adx = recent['adx'].mean()
    daily = df.set_index('Datetime')['close'].resample('D').last().pct_change().dropna()
    volatility = daily.std() * np.sqrt(252) * 100
    
    stats = f"Vol:{volatility:.0f}% ADX:{avg_adx:.0f}"
    
    if volatility > 35:
        if avg_adx > 25: 
            return "ROCKET", stats, 0.80 # 80% Core (More room for Turbo Scalping)
        else:            
            return "GRINDER", stats, 0.00 # 0% Core (Don't bag hold chaos)
    elif volatility > 20:
        return "GRINDER", stats, 0.00 # 0% Core
    else:
        return "FORTRESS", stats, 0.90 # 90% Core

def fetch_historical_data(ticker, period="729d", interval="1h"):
    try:
        df = yf.download(ticker, period=period, interval=interval, progress=False)
        if df.empty: return None
        if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
        df.reset_index(inplace=True)
        df.rename(columns={'Open':'open','High':'high','Low':'low','Close':'close','Volume':'volume'}, inplace=True)
        col = 'Datetime' if 'Datetime' in df.columns else 'Date'
        df.rename(columns={col: 'Datetime'}, inplace=True)
        df['Datetime'] = pd.to_datetime(df['Datetime']).dt.tz_localize(None)
        df['adx'] = calculate_adx_proper(df)
        df_macro = yf.download(['^VIX', '^TNX'], period=period, interval="1d", progress=False)['Close']
        df_macro.reset_index(inplace=True)
        df_macro.rename(columns={'^VIX':'vix','^TNX':'tnx_yield','Date':'Datetime'}, inplace=True)
        df_macro['Datetime'] = pd.to_datetime(df_macro['Datetime']).dt.tz_localize(None)
        return pd.merge_asof(df.sort_values('Datetime'), df_macro.sort_values('Datetime'), on='Datetime', direction='backward').ffill().fillna(0)
    except: return None

def run_smart_backtest(ticker, silent=False):
    df = fetch_historical_data(ticker)
    if df is None: return None
    
    identity, stats, core_allocation = classify_asset(df)
    
    initial_cash = 10000.00
    start_idx = 200
    start_price = df.iloc[start_idx]['close']
    
    core_equity = initial_cash * core_allocation
    sat_equity = initial_cash * (1 - core_allocation)
    
    core_shares = core_equity / start_price
    sat_cash = sat_equity
    sat_shares = 0
    sat_position = "NONE"
    sat_entry_price = 0.0
    
    trades = 0
    hours_in_market = 0

    # RSI helper for Turbo Scalping
    def get_rsi(slice):
        delta = slice.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(alpha=1/14, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/14, adjust=False).mean()
        rs = avg_gain / (avg_loss + 1e-9)
        return 100 - (100 / (1 + rs))

    for i in range(start_idx + 1, len(df)):
        current_slice = df.iloc[i-200 : i+1]['close']
        price = df.iloc[i]['close']
        
        sma50 = current_slice.iloc[-50:].mean()
        sma200 = current_slice.mean()
        rsi_val = get_rsi(current_slice).iloc[-1]
        
        if sat_position != "NONE": hours_in_market += 1
        
        tech_score = calculate_score(df.iloc[i-200 : i+1], ticker)['final_score']
        total_score = tech_score # Simplified for clarity

        # --- STRATEGY EXECUTION ---
        
        # 🚀 ROCKET (Turbo Scalper)
        if identity == "ROCKET":
            if sat_position == "NONE":
                # Buy Aggressively on Momentum or Dip
                if total_score >= 3 or rsi_val < 50: 
                    sat_shares = sat_cash / price
                    sat_cash = 0
                    sat_position = "LONG"
                    trades += 1
            elif sat_position == "LONG":
                # TURBO SCALP: Take profit if Overbought (>75), Re-enter later
                # OR Panic Exit if Trend Breaks (< SMA50)
                if rsi_val > 75 or (total_score <= -4 and price < sma50):
                    sat_cash = sat_shares * price
                    sat_shares = 0
                    sat_position = "NONE"
                    trades += 1

        # ⚔️ GRINDER (0% Core, 100% Active Grim Reaper)
        elif identity == "GRINDER":
            is_death_trend = price < sma200
            
            if sat_position == "NONE":
                if is_death_trend:
                    if total_score <= -2: # Short
                        sat_shares = sat_cash / price
                        sat_entry_price = price
                        sat_cash = 0
                        sat_position = "SHORT"
                        trades += 1
                else:
                    if total_score >= 3: # Buy
                        sat_shares = sat_cash / price
                        sat_cash = 0
                        sat_position = "LONG"
                        trades += 1
            elif sat_position == "LONG":
                # WIDER STOP: Exit at -4 (Chill Pill) or Death Trend
                if total_score <= -4 or is_death_trend:
                    sat_cash = sat_shares * price
                    sat_shares = 0
                    sat_position = "NONE"
                    trades += 1
            elif sat_position == "SHORT":
                # WIDER STOP: Cover at +4 (Chill Pill) or Bull Trend
                if total_score >= 4 or not is_death_trend:
                    profit = (sat_entry_price - price) * sat_shares
                    sat_cash = (sat_shares * sat_entry_price) + profit
                    sat_shares = 0
                    sat_position = "NONE"
                    trades += 1

        # 🛡️ FORTRESS (Safety)
        elif identity == "FORTRESS":
            if sat_position == "NONE":
                if total_score >= 5: 
                    sat_shares = sat_cash / price
                    sat_cash = 0
                    sat_position = "LONG"
                    trades += 1
            elif sat_position == "LONG":
                if total_score <= -2:
                    sat_cash = sat_shares * price
                    sat_shares = 0
                    sat_position = "NONE"
                    trades += 1

    # --- RESULTS ---
    final_price = df.iloc[-1]['close']
    val_core = core_shares * final_price
    val_sat = sat_cash
    if sat_position == "LONG": val_sat = sat_shares * final_price
    elif sat_position == "SHORT": val_sat = (sat_shares * sat_entry_price) + ((sat_entry_price - final_price) * sat_shares)
    
    total_final = val_core + val_sat
    bot_ret = ((total_final - 10000) / 10000) * 100
    hold_ret = ((final_price - start_price) / start_price) * 100
    exposure_pct = (hours_in_market / (len(df) - start_idx)) * 100
    
    return {
        'ticker': ticker, 'id': identity, 'stats': stats,
        'bot_ret': bot_ret, 'hold_ret': hold_ret,
        'trades': trades, 'exp': exposure_pct,
        'winner': "BOT" if bot_ret > hold_ret else "HOLD"
    }

def run_batch_test():
    print(f"\n🚀 ALPHA BATTLE: NO BAGS, TURBO JETS")
    print(f"{'TICKER':<6} | {'ID':<8} | {'STATS':<16} | {'BOT %':<7} | {'HOLD %':<7} | {'TRD':<3} | {'EXP%':<4} | {'WIN'}")
    print("-" * 85)
    
    results = []
    for ticker in BATCH_TICKERS:
        res = run_smart_backtest(ticker, silent=True)
        if res:
            print(f"{res['ticker']:<6} | {res['id']:<8} | {res['stats']:<16} | {res['bot_ret']:>7.0f}% | {res['hold_ret']:>7.0f}% | {res['trades']:>3} | {res['exp']:>3.0f}% | {res['winner']}")
            results.append(res)
    
    print("-" * 85)
    wins = sum(1 for r in results if r['winner'] == "BOT")
    print(f"🏆 SCORE: Bot wins {wins}/{len(results)}")

if __name__ == "__main__":
    run_batch_test()