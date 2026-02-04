import time
# 1. The Senses
from system_senses_stream import fetch_stock_data
from senses_macro import fetch_macro_data, analyze_market_regime

# 2. The Brain
from system_strategy_evaluator import calculate_score

# 3. The Hands 
# We import these to check the connection, but we won't use execute_trade
from system_execution_client import get_schwab_client, get_current_positions, execute_trade

# 4. The Black Box
from journal import log_trade_open

# --- CONFIGURATION ---
DRY_RUN = True  # <--- SAFETY LOCK IS ON
WATCHLIST = ['EURUSD=X', 'GBPUSD=X', 'NVDA', 'AAPL', 'BTC-USD']

def run_bot():
    print(f"Forex & Stock Trading System: SIMULATION MODE")
    print(f"Real Trading: DISABLED")
    print("---------------------------------------------")
    
    # A. Wake up the components
    schwab_client = get_schwab_client()
    
    while True:
        print("\n--- NEW SCAN CYCLE ---")
        
        # B. Check Global Weather (Macro)
        macro_data = fetch_macro_data()
        regime = analyze_market_regime(macro_data)
        
        macro_score = 0
        if regime:
            macro_score = regime.get('score_modifier', 0)
            print(f"[MACRO] Regime: {regime['regime_reasons']} (Modifier: {macro_score})")
        else:
            print("[MACRO] Data unavailable. Proceeding with caution.")

        # C. Check Our Wallet
        # We still look at the wallet to see if we "simulate" holding something
        if schwab_client:
            my_portfolio = get_current_positions(schwab_client)
        else:
            my_portfolio = [] 
        print(f"[PORTFOLIO] Holding: {my_portfolio}")

        # D. Analyze Tickers
        for ticker in WATCHLIST:
            print(f"\n[ANALYZING] {ticker}...")
            
            # 1. Fetch Price Data
            data = fetch_stock_data(ticker)
            if not data:
                print(" ! Error: No data.")
                continue

            # 2. Get Technical Score
            tech_result = calculate_score(data, ticker)
            tech_score = tech_result['final_score']
            
            # 3. Calculate TOTAL Score
            total_score = tech_score + macro_score
            
            # 4. Determine Action
            final_action = "WAIT"
            if total_score >= 4:
                final_action = "BUY"
            elif total_score <= -4:
                final_action = "SELL"
            elif -2 <= total_score <= 2:
                final_action = "HOLD"
            
            # 5. Portfolio Logic (Simulation)
            if final_action == "BUY" and ticker in my_portfolio:
                final_action = "HOLD" 
                print(" -> Signal BUY, but already own it.")
            elif final_action == "SELL" and ticker not in my_portfolio:
                final_action = "WAIT"
                print(" -> Signal SELL, but don't own it.")

            # 6. Execution & Logging
            if final_action in ["BUY", "SELL"]:
                current_price = data.get('current_price', 0.0)
                
                print(f"!!! SIGNAL GENERATED: {final_action} {ticker} @ ${current_price} (Score: {total_score}) !!!")
                print(f"    Reasons: {tech_result['breakdown']}")
                
                # --- SAFETY LOCK ENABLED ---
                # The "else" block that used to contain real trading is REMOVED.
                # It is now impossible for this script to spend money.
                
                print(f"    [PAPER TRADE] Simulating {final_action} order for {ticker}...")
                
                # We log it with a "SIM_" prefix so your journal knows it wasn't real
                log_trade_open(ticker, f"SIM_{final_action}", total_score, tech_result['breakdown'], current_price)

            else:
                print(f" -> Result: {final_action} (Score: {total_score})")

            time.sleep(1) 

        print("\nCycle Complete. Sleeping 60s...")
        time.sleep(60)

if __name__ == "__main__":
    run_bot()