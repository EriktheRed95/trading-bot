import pandas as pd
import yfinance as yf
import numpy as np

# --- IMPORT YOUR BRAIN ---
from system_strategy_evaluator import calculate_score
from senses_macro import analyze_market_regime

# --- CONFIGURATION ---
BATCH_TICKERS = [
    'NVDA', 'TSLA', 'AMD', 'AAPL', 'AMZN', 
    'GOOGL', 'MSFT', 'META', 'JPM', 'KO',
    'MU', 'PLTR', 'RIVN'
]

def fetch_historical_data(ticker, period="729d", interval="1h"):
    try:
        df = yf.download(ticker, period=period, interval=interval, progress=False)
        if df.empty: return None
        if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
        df.reset_index(inplace=True)
        df.rename(columns={'Open':'open','High':'high','Low':'low','Close':'close','Volume':'volume'}, inplace=True)
        
        # Date Handling
        if 'Datetime' in df.columns: 
            df['Datetime'] = pd.to_datetime(df['Datetime']).dt.tz_localize(None)
        elif 'Date' in df.columns: 
            df.rename(columns={'Date': 'Datetime'}, inplace=True)
            df['Datetime'] = pd.to_datetime(df['Datetime']).dt.tz_localize(None)
        
        # Macro
        df_macro = yf.download(['^VIX', '^TNX'], period=period, interval="1d", progress=False)['Close']
        df_macro.reset_index(inplace=True)
        df_macro.rename(columns={'^VIX':'vix','^TNX':'tnx_yield','Date':'Datetime'}, inplace=True)
        df_macro['Datetime'] = pd.to_datetime(df_macro['Datetime']).dt.tz_localize(None)
        
        return pd.merge_asof(df.sort_values('Datetime'), df_macro.sort_values('Datetime'), on='Datetime', direction='backward').ffill().fillna(0)
    except: return None

def run_dynamic_backtest(ticker, silent=False):
    if not silent: print(f"⏳ Analyzing {ticker} Strategy...")
    df = fetch_historical_data(ticker)
    if df is None: return None

    # --- 1. INITIALIZATION (The Fix) ---
    initial_cash = 10000.00
    
    # Get start price and trend
    start_idx = 200
    if len(df) < start_idx: return None
    
    start_row = df.iloc[start_idx]
    start_price = start_row['close']
    start_slice = df.iloc[0 : start_idx+1]
    sma200_start = start_slice['close'].mean()
    
    # Determine Initial Allocation
    if start_price > sma200_start:
        core_pct = 0.80
    else:
        core_pct = 0.20
        
    # Buy Initial Positions
    core_equity = initial_cash * core_pct
    sat_equity = initial_cash * (1 - core_pct)
    
    core_shares = core_equity / start_price
    sat_cash = sat_equity
    
    sat_shares = 0
    sat_position = "NONE"
    sat_entry_price = 0.0
    
    # --- 2. THE LOOP ---
    for i in range(start_idx + 1, len(df)):
        current_slice = df.iloc[i-200 : i+1]
        current_row = df.iloc[i]
        price = current_row['close']
        
        # A. TREND CHECK
        sma200 = current_slice['close'].mean()
        sma50 = current_slice['close'].iloc[-50:].mean()
        in_major_uptrend = price > sma200
        
        # B. DYNAMIC REBALANCING
        # Target
        target_core_pct = 0.80 if in_major_uptrend else 0.20
        
        # Current Valuation
        val_core = core_shares * price
        
        val_sat = sat_cash
        if sat_position == "LONG": 
            val_sat = sat_shares * price
        elif sat_position == "SHORT": 
            # Equity = Initial Value + Profit
            # Initial Value of short was (shares * entry)
            # Profit is (entry - price) * shares
            val_sat = (sat_shares * sat_entry_price) + ((sat_entry_price - price) * sat_shares)
            
        curr_total = val_core + val_sat
        
        # Calc Deviation
        if curr_total > 0:
            curr_core_pct = val_core / curr_total
            
            # Only rebalance if drift is > 5% (Buffer)
            if abs(curr_core_pct - target_core_pct) > 0.05:
                desired_core = curr_total * target_core_pct
                diff = desired_core - val_core
                
                # Execute Rebalance
                # If diff > 0: We need more Core. Buy it using Sat Cash.
                # If diff < 0: We have too much Core. Sell it and add to Sat Cash.
                
                # Note: If Sat is in a trade, we might not have cash. 
                # For simplicity in this backtest, we assume Sat has liquidity or we treat sat_cash as the funding pool.
                # If sat_cash goes negative here, it implies leverage (margin), which is acceptable for this simulation.
                
                core_shares += diff / price
                sat_cash -= diff 

        # C. SATELLITE TRADING (The Sniper)
        macro_snapshot = {'vix': current_row['vix'], 'tnx_yield': current_row['tnx_yield'], 'tnx_change_pct': 0}
        regime = analyze_market_regime(macro_snapshot)
        tech_score = calculate_score(current_slice, ticker)['final_score']
        total_score = tech_score + regime.get('score_modifier', 0)

        if sat_position == "NONE":
            # Only buy if we have cash (or allowed leverage)
            if sat_cash > 0: 
                if total_score >= 4 and price > sma50:
                    sat_shares = sat_cash / price
                    sat_cash = 0
                    sat_position = "LONG"
                elif total_score <= -4 and price < sma200:
                    sat_shares = sat_cash / price
                    sat_entry_price = price
                    sat_cash = 0
                    sat_position = "SHORT"

        elif sat_position == "LONG":
            if total_score <= -3 or price < sma50:
                sat_cash = sat_shares * price
                sat_shares = 0
                sat_position = "NONE"
                
        elif sat_position == "SHORT":
            if total_score >= 3 or price > sma50:
                profit = (sat_entry_price - price) * sat_shares
                sat_cash = (sat_shares * sat_entry_price) + profit
                sat_shares = 0
                sat_position = "NONE"

    # --- FINAL TALLY ---
    final_price = df.iloc[-1]['close']
    
    val_core = core_shares * final_price
    val_sat = sat_cash
    if sat_position == "LONG": val_sat = sat_shares * final_price
    elif sat_position == "SHORT": val_sat = (sat_shares * sat_entry_price) + ((sat_entry_price - final_price) * sat_shares)
    
    total_final = val_core + val_sat
    
    return {
        'ticker': ticker,
        'dynamic_return': ((total_final - 10000) / 10000) * 100,
        'hold_return': ((final_price - start_price) / start_price) * 100
    }

def run_batch_test():
    print(f"\n🚀 HYBRID BATTLE: DYNAMIC CORE SCALING")
    print(f"Rule: Bull=80% Core | Bear=20% Core")
    print("-" * 65)
    print(f"{'TICKER':<8} | {'DYN RET':<12} | {'HOLD RET':<12} | {'WINNER'}")
    print("-" * 65)
    results = []
    for ticker in BATCH_TICKERS:
        res = run_dynamic_backtest(ticker, silent=True)
        if res:
            winner = "💎 DYNAMIC" if res['dynamic_return'] > res['hold_return'] else "📈 HOLD"
            print(f"{res['ticker']:<8} | {res['dynamic_return']:>10.2f}% | {res['hold_return']:>10.2f}% | {winner}")
            results.append(res)
            
    wins = sum(1 for r in results if r['dynamic_return'] > r['hold_return'])
    print("-" * 65)
    print(f"\n🏆 FINAL SCORE: Dynamic Strategy wins {wins} out of {len(results)} matches.")

if __name__ == "__main__":
    run_batch_test()