import pandas as pd

def calculate_score(data):
    """
    Analyzes historical data to produce a score and decision.
    Logic: 
    - BUY: RSI < 30 (Oversold) + Price > SMA
    - SELL: RSI > 70 (Overbought) + Price < SMA
    - HOLD: RSI between 45-55 (Consolidating) or trending slightly
    - WAIT: No clear signal
    """
    prices = pd.Series([float(d['close']) for d in data])
    current_price = prices.iloc[-1]
    
    # 1. Simple Moving Average (SMA) - 14 period
    sma = prices.rolling(window=14).mean().iloc[-1]
    
    # 2. Relative Strength Index (RSI) - Simple implementation
    delta = prices.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs.iloc[-1]))

    # Decision Logic
    score = 0
    breakdown = []
    
    if rsi < 35:
        score += 3
        breakdown.append(f"RSI is Oversold ({rsi:.2f})")
    elif rsi > 65:
        score -= 3
        breakdown.append(f"RSI is Overbought ({rsi:.2f})")
    
    if current_price > sma:
        score += 2
        breakdown.append(f"Price is above SMA ({sma:.4f})")
    else:
        score -= 2
        breakdown.append(f"Price is below SMA ({sma:.4f})")

    # Determine Decision
    if score >= 4:
        decision = "BUY"
    elif score <= -4:
        decision = "SELL"
    elif -1 <= score <= 1:
        # Neutral zone: If you have it, keep it. If not, don't enter.
        decision = "HOLD"
    else:
        decision = "WAIT"

    return {
        "final_score": score,
        "decision": decision,
        "breakdown": breakdown
    }
from algo_forex import evaluate_forex_strategy

def calculate_score(data, ticker):
    """
    This is the 'Traffic Controller'. 
    It checks the ticker name and sends the data to the right 'Brain'.
    """
    
    # Check if the ticker is a Forex pair (usually ends in =X on Yahoo)
    if ticker.endswith('=X'):
        return evaluate_forex_strategy(data)
    
    # Otherwise, use the standard Stock Logic
    else:
        return calculate_stock_score(data)

def calculate_stock_score(data):
    """
    Your original stock logic lives here.
    (You can keep your existing logic from the previous version)
    """
    # Simple example logic for stocks if you want to keep it basic:
    prices = [float(d['close']) for d in data]
    current_price = prices[-1]
    prev_price = prices[-2]
    
    score = 0
    breakdown = []
    
    if current_price > prev_price:
        score += 2
        breakdown.append("Price is trending up")
    else:
        score -= 2
        breakdown.append("Price is trending down")
        
    decision = "WAIT"
    if score >= 2: decision = "BUY"
    elif score <= -2: decision = "SELL"
    
    return {
        "final_score": score,
        "decision": decision,
        "breakdown": breakdown
    }