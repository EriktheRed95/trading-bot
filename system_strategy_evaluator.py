# ... imports ...
from algo_stocks import evaluate_stock_strategy
from algo_forex import evaluate_forex_strategy
from algo_crypto import evaluate_crypto_strategy  # <--- Make sure this is imported

def calculate_score(data, ticker):
    # ... (data checking code) ...

    # ROUTING LOGIC
    if "=X" in ticker:
        return evaluate_forex_strategy(data)
    elif "BTC" in ticker or "ETH" in ticker:     # <--- Check for Crypto
        return evaluate_crypto_strategy(data)    # <--- Route to Crypto Algo
    else:
        return evaluate_stock_strategy(data)