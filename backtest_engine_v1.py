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
    'MU', 'PLTR', 'RIVN' # Added some smaller/volatile names
]

def fetch_historical_data(ticker, period="2y", interval="1h"):
    try:
        df_asset = yf.download(ticker, period=period, interval=interval, progress=False)
    except: return None

    if df_asset.empty: return None
    
    if isinstance(df_asset.columns, pd.MultiIndex):
        df_asset.columns = df_asset.columns.get_level_values(0)
    
    df_asset.reset_index(inplace=True)
    df_asset.rename(columns={'Open': 'open', 'High': 'high', 'Low': 'low', 'Close': 'close', 'Volume': 'volume'}, inplace=True)

    # Date handling
    if 'Datetime' in df_asset.columns:
        if df_asset['Datetime'].dt.tz is not None:
            df_asset['Datetime'] = df_asset['Datetime'].dt.tz_localize(None)
    elif 'Date' in df_asset.columns:
        df_asset.rename(columns={'Date': 'Datetime'}, inplace=True)
        df_asset['Datetime'] = pd.to_datetime(df_asset['Datetime'])

    # Macro
    try:
        df_macro = yf.download(['^VIX', '^TNX'], period=period, interval="1d", progress=False)['Close']
    except: return None
        
    df_macro.reset_index(inplace=True)
    df_macro.rename(columns={'^VIX': 'vix', '^TNX': 'tnx_yield', 'Date': 'Datetime'}, inplace=True)
    if df_macro['Datetime'].dt.tz is not None:
        df_macro['Datetime'] = df_macro['Datetime'].dt.tz_localize(None)
    df_macro['Datetime'] = pd.to_datetime(df_macro['Datetime'])
    df_macro['tnx_change_pct'] = df_macro['tnx_yield'].pct_change() * 100
    
    df_merged = pd.merge_asof(
        df_asset.sort_values('Datetime'), df_macro.sort_values('Datetime'),
        on='Datetime', direction='backward'
    )
    df_merged['vix'] = df_merged['vix'].ffill()
    df_merged['tnx_yield'] = df_merged['tnx_yield'].ffill()
    df_merged['tnx_change_pct'] = df_merged['tnx_change_pct'].fillna(0)
    return df_merged

def run_single_backtest(ticker, silent=False):
    if not silent: print(f"⏳ Testing {ticker}...")
    df = fetch_historical_data(ticker, period="729d", interval="1h")
    if df is None: return None

    # Sim Params
    balance = 10000.00
    position = "NONE" # NONE, LONG, SHORT
    shares = 0
    entry_price = 0.0
    trades_count = 0
    
    start_index = 200
    if len(df) < start_index: return None
    
    # Buy & Hold Calc
    start_price = df.iloc[start_index]['close']
    end_price = df.iloc[-1]['close']
    buy_hold_return = ((end_price - start_price) / start_price) * 100

    for i in range(start_index, len(df)):
        current_slice = df.iloc[i-200 : i+1].copy()
        current_row = df.iloc[i]
        price = current_row['close']
        
        # Macro & Tech
        macro_snapshot = {
            'vix': current_row['vix'],
            'tnx_yield': current_row['tnx_yield'],
            'tnx_change_pct': current_row['tnx_change_pct']
        }
        regime = analyze_market_regime(macro_snapshot)
        macro_score = regime.get('score_modifier', 0)
        tech_result = calculate_score(current_slice, ticker) 
        total_score = tech_result['final_score'] + macro_score
        
        # LOGIC: LONG / SHORT / CASH
        action = "WAIT"
        
        if position == "NONE":
            if total_score >= 4:
                action = "ENTER_LONG"
            elif total_score <= -4:
                action = "ENTER_SHORT"
                
        elif position == "LONG":
            # Exit Long if trend breaks
            if total_score <= -2: 
                action = "EXIT_LONG"
            # Or Flip Short immediately if very bearish
            elif total_score <= -5:
                action = "FLIP_SHORT"

        elif position == "SHORT":
            # Exit Short if trend recovers
            if total_score >= 2:
                action = "EXIT_SHORT"
            # Or Flip Long immediately if very bullish
            elif total_score >= 5:
                action = "FLIP_LONG"

        # EXECUTION
        if action == "ENTER_LONG":
            shares = balance / price
            entry_price = price
            balance = 0
            position = "LONG"
            
        elif action == "ENTER_SHORT":
            shares = balance / price
            entry_price = price
            balance = 0 # Collateral locked
            position = "SHORT"
            
        elif action == "EXIT_LONG":
            balance = shares * price
            shares = 0
            position = "NONE"
            trades_count += 1
            
        elif action == "EXIT_SHORT":
            # Profit = (Entry - Exit) * Shares
            # Equity = Initial_Collateral + Profit
            # Initial_Collateral was (Shares * Entry)
            initial_val = shares * entry_price
            profit = (entry_price - price) * shares
            balance = initial_val + profit
            shares = 0
            position = "NONE"
            trades_count += 1
            
        elif action == "FLIP_SHORT": # Sell Long, Go Short
            # 1. Close Long
            balance = shares * price
            trades_count += 1
            # 2. Open Short
            shares = balance / price
            entry_price = price
            balance = 0
            position = "SHORT"
            
        elif action == "FLIP_LONG": # Cover Short, Go Long
            # 1. Close Short
            initial_val = shares * entry_price
            profit = (entry_price - price) * shares
            balance = initial_val + profit
            trades_count += 1
            # 2. Open Long
            shares = balance / price
            entry_price = price
            balance = 0
            position = "LONG"

    # Final tally
    final_equity = balance
    if position == "LONG":
        final_equity = shares * df.iloc[-1]['close']
    elif position == "SHORT":
        entry_val = shares * entry_price
        current_val = shares * df.iloc[-1]['close']
        profit = entry_val - current_val
        final_equity = entry_val + profit

    bot_return = ((final_equity - 10000) / 10000) * 100
    
    return {
        'ticker': ticker,
        'bot_return': bot_return,
        'hold_return': buy_hold_return,
        'trades': trades_count,
        'beat_market': bot_return > buy_hold_return
    }

def run_batch_test():
    print("\n🚀 STARTING MARKET-WIDE BATTLE (Long/Short Strategy)")
    print("Mode: 2 Years / Hourly")
    print("-" * 75)
    print(f"{'TICKER':<8} | {'BOT (L/S)':<12} | {'HOLD':<12} | {'TRADES':<6} | {'WINNER'}")
    print("-" * 75)
    
    results = []
    
    for ticker in BATCH_TICKERS:
        res = run_single_backtest(ticker, silent=True)
        if res:
            winner = "🤖 BOT" if res['beat_market'] else "📈 HOLD"
            print(f"{res['ticker']:<8} | {res['bot_return']:>10.2f}% | {res['hold_return']:>10.2f}% | {res['trades']:>6} | {winner}")
            results.append(res)
        else:
            print(f"{ticker:<8} |    ERROR     |      --      |   --   |   --")
            
    print("-" * 75)
    bot_wins = sum(1 for r in results if r['beat_market'])
    print(f"\n🏆 FINAL SCORE: Bot wins {bot_wins} out of {len(results)} matches.")

if __name__ == "__main__":
    choice = input("Run (S)ingle stock or (B)atch? [S/B]: ").strip().upper()
    if choice == 'B':
        run_batch_test()
    else:
        t = input("Enter ticker: ")
        res = run_single_backtest(t)
        if res:
            print(f"\nFinal Result for {t}:")
            print(f"Bot (Long/Short): {res['bot_return']:.2f}%")
            print(f"Buy & Hold:       {res['hold_return']:.2f}%")