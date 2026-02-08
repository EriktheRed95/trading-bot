import pandas as pd
import yfinance as yf
import numpy as np

# --- IMPORT YOUR BRAIN ---
from system_strategy_evaluator import calculate_score
from senses_macro import analyze_market_regime

# --- CONFIGURATION ---
BATCH_TICKERS = ['NVDA', 'TSLA', 'AMD', 'AAPL', 'AMZN', 'GOOGL', 'MSFT', 'META', 'JPM', 'KO', 'MU', 'PLTR', 'RIVN']
CORE_ALLOCATION = 0.70  
SATELLITE_ALLOCATION = 0.30 

def fetch_historical_data(ticker, period="729d", interval="1h"):
    try:
        df = yf.download(ticker, period=period, interval=interval, progress=False)
        if df.empty: return None
        if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
        df.reset_index(inplace=True)
        df.rename(columns={'Open':'open','High':'high','Low':'low','Close':'close','Volume':'volume'}, inplace=True)
        if 'Datetime' in df.columns: df['Datetime'] = pd.to_datetime(df['Datetime']).dt.tz_localize(None)
        elif 'Date' in df.columns: 
            df.rename(columns={'Date': 'Datetime'}, inplace=True)
            df['Datetime'] = pd.to_datetime(df['Datetime']).dt.tz_localize(None)
        
        df_macro = yf.download(['^VIX', '^TNX'], period=period, interval="1d", progress=False)['Close']
        df_macro.reset_index(inplace=True)
        df_macro.rename(columns={'^VIX':'vix','^TNX':'tnx_yield','Date':'Datetime'}, inplace=True)
        df_macro['Datetime'] = pd.to_datetime(df_macro['Datetime']).dt.tz_localize(None)
        
        return pd.merge_asof(df.sort_values('Datetime'), df_macro.sort_values('Datetime'), on='Datetime', direction='backward').ffill().fillna(0)
    except: return None

def run_hybrid_backtest(ticker, silent=False):
    df = fetch_historical_data(ticker)
    if df is None: return None

    initial_total = 10000.00
    core_cash = initial_total * CORE_ALLOCATION
    start_price = df.iloc[200]['close']
    core_shares = core_cash / start_price
    
    sat_balance = initial_total * SATELLITE_ALLOCATION
    sat_position = "NONE" 
    sat_shares = 0
    sat_entry_price = 0.0

    for i in range(200, len(df)):
        current_slice = df.iloc[i-200 : i+1]
        current_row = df.iloc[i]
        price = current_row['close']
        
        # SATELLITE SENSES
        sma50 = current_slice['close'].iloc[-50:].mean() # Short-term trend
        sma200 = current_slice['close'].mean()         # Long-term trend
        
        macro_snapshot = {'vix': current_row['vix'], 'tnx_yield': current_row['tnx_yield'], 'tnx_change_pct': 0}
        regime = analyze_market_regime(macro_snapshot)
        tech_score = calculate_score(current_slice, ticker)['final_score']
        total_score = tech_score + regime.get('score_modifier', 0)

        # --- REVISED SATELLITE LOGIC ---
        if sat_position == "NONE":
            if total_score >= 4 and price > sma50:
                sat_shares = sat_balance / price
                sat_balance = 0
                sat_position = "LONG"
            elif total_score <= -4 and price < sma200:
                # Active Shorting only if long-term trend is actually broken
                sat_shares = sat_balance / price
                sat_entry_price = price
                sat_balance = 0
                sat_position = "SHORT"

        elif sat_position == "LONG":
            # EXIT FASTER: If the short-term trend (SMA50) breaks OR score is bad
            if total_score <= -3 or price < sma50:
                sat_balance = sat_shares * price
                sat_shares = 0
                sat_position = "NONE"
                
        elif sat_position == "SHORT":
            # COVER FASTER: If short-term trend recovers
            if total_score >= 3 or price > sma50:
                profit = (sat_entry_price - price) * sat_shares
                sat_balance = (sat_shares * sat_entry_price) + profit
                sat_shares = 0
                sat_position = "NONE"

    # --- FINAL TALLY ---
    final_price = df.iloc[-1]['close']
    final_core_val = core_shares * final_price
    final_sat_val = sat_balance
    if sat_position == "LONG": final_sat_val = sat_shares * final_price
    elif sat_position == "SHORT": 
        final_sat_val = (sat_shares * sat_entry_price) + ((sat_entry_price - final_price) * sat_shares)
    
    total_final = final_core_val + final_sat_val
    return {
        'ticker': ticker,
        'hybrid_return': ((total_final - initial_total) / initial_total) * 100,
        'hold_return': ((final_price - start_price) / start_price) * 100
    }

def run_batch_test():
    print(f"\n🚀 HYBRID BATTLE: ACTIVE DEFENSE MODE")
    print("-" * 65)
    print(f"{'TICKER':<8} | {'HYBRID RET':<12} | {'HOLD RET':<12} | {'WINNER'}")
    print("-" * 65)
    results = []
    for ticker in BATCH_TICKERS:
        res = run_hybrid_backtest(ticker, silent=True)
        if res:
            winner = "💎 HYBRID" if res['hybrid_return'] > res['hold_return'] else "📈 HOLD"
            print(f"{res['ticker']:<8} | {res['hybrid_return']:>10.2f}% | {res['hold_return']:>10.2f}% | {winner}")
            results.append(res)
    print("-" * 65)

if __name__ == "__main__":
    run_batch_test()