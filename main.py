import time
# 1. The Senses
from system_senses_stream import fetch_stock_data
from senses_macro import fetch_macro_data, analyze_market_regime

# 2. The Brain
from system_strategy_evaluator import calculate_score

# 3. The Hands
from system_execution_client import get_schwab_client, get_current_positions, execute_trade

# 4. The Black Box
from journal import log_trade_open

# Shared with backtest_engine.py — keeps live and backtest thresholds in sync.
from strategy_config import THRESHOLDS

# --- CONFIGURATION ---
DRY_RUN = True  # <--- SAFETY LOCK IS ON
WATCHLIST = ['EURUSD=X', 'GBPUSD=X', 'NVDA', 'AAPL', 'BTC-USD']

# Live watchlist mixes forex / crypto / stocks, so we use the DEFAULT bucket here.
# Per-identity thresholds (ROCKET/GRINDER/FORTRESS) are applied by the backtester.
_LIVE = THRESHOLDS['DEFAULT']
ENTRY_LONG = _LIVE['entry_long']
EXIT_LONG = _LIVE['exit_long']

# --- EXIT SETTINGS ---
TAKE_PROFIT_PCT = 0.02  # +2.0% (Base Target)
STOP_LOSS_PCT = -0.01   # -1.0% (Hard Floor)

def run_bot():
    print(f"Forex & Stock Trading System: DYNAMIC MODE")
    print(f"Real Trading: DISABLED")
    print(f"Base Strategy: TP > {TAKE_PROFIT_PCT*100}% | SL < {STOP_LOSS_PCT*100}%")
    print("---------------------------------------------")
    
    # A. Wake up the components
    schwab_client = get_schwab_client()
    
    # Local Memory for Simulation
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

        # C. Check Wallet
        print(f"[PORTFOLIO] Simulated Holdings: {list(sim_portfolio.keys())}")

        # D. Analyze Tickers
        for ticker in WATCHLIST:
            print(f"\n[ANALYZING] {ticker}...")
            
            # Determine Asset Class
            is_forex = '=X' in ticker
            is_volatile = not is_forex # Stocks and Crypto are "Volatile"
            
            # 1. Fetch Price Data
            data = fetch_stock_data(ticker)
            if not data:
                print(" ! Error: No data.")
                continue

            current_price = data.get('current_price', 0.0)
            if current_price is None or current_price == 0:
                print(" ! Error: Invalid price.")
                continue

            # 2. Get Technical Score
            tech_result = calculate_score(data, ticker)
            tech_score = tech_result['final_score']
            total_score = tech_score + macro_score

            # --- THE STRATEGIST (Dynamic Exit Logic) ---
            final_action = "WAIT"
            pnl_pct = 0.0
            
            if ticker in sim_portfolio:
                entry_price = sim_portfolio[ticker]
                pnl_pct = (current_price - entry_price) / entry_price
                print(f" -> P/L: {pnl_pct*100:.2f}% (Entry: ${entry_price:.2f})")

                # 1. STOP LOSS (Always active for safety)
                if pnl_pct <= STOP_LOSS_PCT:
                    final_action = "SELL_SL"
                    print(" -> Hit Stop Loss. Exiting.")

                # 2. DYNAMIC TAKE PROFIT (The "Let it Run" Logic)
                elif pnl_pct >= TAKE_PROFIT_PCT:
                    if is_forex:
                        # Forex: Take the money immediately
                        final_action = "SELL_TP"
                        print(" -> Forex Target Hit. Taking Profit.")
                    elif is_volatile:
                        # Stocks/Crypto: Check Momentum
                        if total_score >= ENTRY_LONG:
                            final_action = "HOLD_RUNNER"
                            print(f" -> Target Hit ({pnl_pct*100:.2f}%) but Score is STRONG ({total_score}). Letting it run!")
                        else:
                            final_action = "SELL_TP"
                            print(f" -> Target Hit and Momentum Fading (Score: {total_score}). Securing Bag.")

                # 3. TECHNICAL BREAKDOWN (Protect Unrealized Gains)
                # If the score drops to the exit threshold, get out even if we haven't hit TP yet.
                elif total_score <= EXIT_LONG:
                    final_action = "SELL_SIGNAL"
                    print(" -> Technical Breakdown detected. Exiting position.")

            else:
                # We don't own it yet. Should we buy?
                if total_score >= ENTRY_LONG:
                    final_action = "BUY"
                elif -2 <= total_score <= 2:
                    final_action = "WAIT"

            # --- EXECUTION & LOGGING ---
            if final_action in ["BUY", "SELL_TP", "SELL_SL", "SELL_SIGNAL"]:
                
                print(f"!!! SIGNAL GENERATED: {final_action} {ticker} @ ${current_price} (Score: {total_score}) !!!")
                
                if "BUY" in final_action:
                    print(f"    [PAPER TRADE] BUY executed. Tracking entry at ${current_price}")
                    sim_portfolio[ticker] = current_price
                    log_trade_open(ticker, f"SIM_{final_action}", total_score, tech_result['breakdown'], current_price)
                
                elif "SELL" in final_action:
                    print(f"    [PAPER TRADE] SELL executed ({final_action}). Closing position.")
                    if ticker in sim_portfolio:
                        del sim_portfolio[ticker]
                    log_trade_open(ticker, f"SIM_{final_action}", total_score, [f"P/L: {pnl_pct*100:.2f}%"], current_price)

            elif final_action == "HOLD_RUNNER":
                # Log nothing, just print
                print(f" -> 🚀 HOLDING RUNNER: {ticker} is up {pnl_pct*100:.2f}% and still strong.")

            else:
                print(f" -> Result: {final_action} (Score: {total_score})")

            time.sleep(1) 

        print("\nCycle Complete. Sleeping 60s...")
        time.sleep(60)

if __name__ == "__main__":
    run_bot()