# system_strategy_evaluator.py

# Import the specific logic functions from our new specialist files
from algo_stocks import evaluate_stock_strategy
from algo_forex import evaluate_forex_strategy
from algo_crypto import evaluate_crypto_strategy

# Inside system_strategy_evaluator.py
import pandas as pd # <--- Add this import at the top

# ... (keep imports) ...

def calculate_score(data, ticker):
    """
    Routes the data to the correct Specialist Brain.
    """
    # FIX: Allow List OR DataFrame
    if not isinstance(data, list) and not isinstance(data, pd.DataFrame):
        print(f"Error: Data for {ticker} is not in list/DataFrame format.")
        return {"decision": "WAIT", "final_score": 0, "breakdown": ["Data Error"]}

    # If it's a DataFrame, convert it to list of dicts for consistency, 
    # OR ensure the sub-algos handle DataFrames (which they do).
    # Our updated algos expect 'data' to be passed into pd.DataFrame(data), 
    # so passing a DataFrame directly works fine!

    # 1. Forex Logic
    if ticker.endswith('=X'):
        return evaluate_forex_strategy(data)
    
    # 2. Crypto Logic
    elif ticker.endswith('-USD'):
        return evaluate_crypto_strategy(data)
    
    # 3. Stock Logic
    else:
        return evaluate_stock_strategy(data)