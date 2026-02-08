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

# --- EXIT STRATEGY SETTINGS ---
TAKE_PROFIT_PCT = 0.02  # +2.0%
STOP_LOSS_PCT = -0.01   # -1.0%

def run_bot():
    print(f"Forex & Stock Trading System: SIMULATION MODE")
    print(f"Real Trading: DISABLED")
    print(f"Exit Strategy: TP @ {TAKE_PROFIT_PCT*100}% | SL @ {STOP_LOSS_PCT*100}%")
    print("---------------------------------------------")
    
    # A. Wake up the components
    schwab_client = get_schwab_client()
    
    # Local Memory for Simulation (The "Accountant")
    # Format: {'NVDA': 105.50, 'BTC-USD': 45000.00}
    sim_portfolio = {} 
    
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

        # C. Check Real Wallet (Just for display)
        if schwab_client:
            real_holdings = get_current_positions(schwab_client)
        else:
            real_holdings = []
        
        print(f"[PORTFOLIO] Simulated Holdings: {list(sim_portfolio.keys())}")

        # D. Analyze Tickers
        for ticker in WATCHLIST:
            print(f"\n[ANALYZING] {ticker}...")
            
            # 1. Fetch Price Data
            data = fetch_stock_data(ticker)
            if not data:
                print(" ! Error: No data.")
                continue

            current_price = data.get('current_price', 0.0)
            if current_price is None or current_price == 0:
                print(" ! Error: Invalid price.")
                continue

            # --- THE ACCOUNTANT (Profit/Loss Logic) ---
            final_action = "WAIT"
            pnl_pct = 0.0
            
            if ticker in sim_portfolio:
                entry_price = sim_portfolio[ticker]
                pnl_pct = (current_price - entry_price) / entry_price
                
                print(f" -> P/L: {pnl_pct*100:.2f}% (Entry: ${entry_price:.2f})")

                # Check Hard Exits
                if pnl_pct >= TAKE_PROFIT_PCT:
                    final_action = "SELL_TP"
                elif pnl_pct <= STOP_LOSS_PCT:
                    final_action = "SELL_SL"
            
            # --- THE ANALYST (Technical Score Logic) ---
            # Only run the brain if the Accountant hasn't already decided to sell
            tech_result = calculate_score(data, ticker)
            tech_score = tech_result['final_score']
            total_score = tech_score + macro_score
            
            if final_action == "WAIT":
                # Determine Technical Action
                if total_score >= 4:
                    if ticker not in sim_portfolio:
                        final_action = "BUY"
                    else:
                        final_action = "HOLD" # Already have it
                elif total_score <= -4:
                    if ticker in sim_portfolio:
                        final_action = "SELL_SIGNAL"
                    else:
                        final_action = "WAIT" # Don't have it, can't sell
                elif -2 <= total_score <= 2:
                    final_action = "HOLD"

            # --- EXECUTION & LOGGING ---
            if final_action in ["BUY", "SELL_TP", "SELL_SL", "SELL_SIGNAL"]:
                
                print(f"!!! SIGNAL GENERATED: {final_action} {ticker} @ ${current_price} (Score: {total_score}) !!!")
                
                # SIMULATION LOGIC
                if "BUY" in final_action:
                    print(f"    [PAPER TRADE] BUY executed. Tracking entry at ${current_price}")
                    sim_portfolio[ticker] = current_price
                    log_trade_open(ticker, f"SIM_{final_action}", total_score, tech_result['breakdown'], current_price)
                
                elif "SELL" in final_action:
                    print(f"    [PAPER TRADE] SELL executed ({final_action}). Closing position.")
                    if ticker in sim_portfolio:
                        del sim_portfolio[ticker]
                    # Log the close
                    log_trade_open(ticker, f"SIM_{final_action}", total_score, [f"P/L: {pnl_pct*100:.2f}%"], current_price)

            else:
                print(f" -> Result: {final_action} (Score: {total_score})")

            time.sleep(1) 

        print("\nCycle Complete. Sleeping 60s...")
        time.sleep(60)

if __name__ == "__main__":
    run_bot()