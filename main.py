import time
from system_senses_stream import fetch_stock_data
from system_strategy_evaluator import calculate_score

def run_bot():
    print("Forex & Stock Trading System Active.")
    print("------------------------------------")
    
    # 1. Our current watchlist
    watchlist = ['EURUSD=X', 'GBPUSD=X', 'NVDA', 'AAPL']
    
    # 2. Simulation of current holdings
    # This helps the bot decide if 'HOLD' is actually possible
    my_portfolio = ['AAPL'] 

    for ticker in watchlist:
        print(f"\n[SCANNING] {ticker}...")
        
        # Step A: Fetch Data
        data = fetch_stock_data(ticker)
        
        if data:
            # Step B: Brain (Calculate Score)
            # IMPORTANT: Added 'ticker' here so the evaluator knows which logic to use
            result = calculate_score(data, ticker)
            decision = result['decision']
            
            # Step C: Enhanced Logic for HOLD vs WAIT
            if decision == "HOLD":
                # If we own it, we hold it. If we don't, we just wait.
                if ticker in my_portfolio:
                    print(f" -> Result: HOLD (Maintaining existing position in {ticker})")
                else:
                    print(f" -> Result: WAIT (Market is stable, but no position to hold)")
            
            elif decision == "BUY":
                print(f"*** ALERT: Strong BUY signal for {ticker} ***")
                # Trigger Buy Order logic here later
            
            elif decision == "SELL":
                # Only "Sell" if we actually have something to sell
                if ticker in my_portfolio:
                    print(f"*** ALERT: SELL signal triggered for {ticker} ***")
                else:
                    print(f" -> Result: WAIT (Market bearish, but we don't own {ticker})")
            
            else: # Decision is WAIT
                print(f" -> Result: WAIT (No clear trend detected)")

            # Step D: Print the logic breakdown for transparency
            print(" -> Reasoning:")
            for reason in result['breakdown']:
                print(f"    * {reason}")
            
        else:
            print(f" ! Error: Data could not be retrieved for {ticker}")

        # Brief pause between tickers
        time.sleep(1)

    print("\n------------------------------------")
    print("Cycle Complete. Standing by.")

if __name__ == "__main__":
    run_bot()