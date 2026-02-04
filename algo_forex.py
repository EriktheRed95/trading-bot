import pandas as pd

def evaluate_forex_strategy(data):
    """
    Technical analysis specifically tuned for Forex pairs.
    Uses RSI for momentum and SMA for trend confirmation.
    """
    # Convert data to a Pandas Series for easy math
    prices = pd.Series([float(d['close']) for d in data])
    current_price = prices.iloc[-1]
    
    # 1. Calculate 14-period SMA
    sma_period = 14
    sma = prices.rolling(window=sma_period).mean().iloc[-1]
    
    # 2. Calculate RSI (Relative Strength Index)
    delta = prices.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=sma_period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=sma_period).mean()
    
    # Avoid division by zero
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs.iloc[-1]))

    score = 0
    breakdown = []

    # --- RSI LOGIC ---
    if rsi < 30:
        score += 3
        breakdown.append(f"RSI is Oversold ({rsi:.2f}) - Bullish")
    elif rsi > 70:
        score -= 3
        breakdown.append(f"RSI is Overbought ({rsi:.2f}) - Bearish")
    else:
        breakdown.append(f"RSI is Neutral ({rsi:.2f})")

    # --- SMA LOGIC ---
    if current_price > sma:
        score += 2
        breakdown.append(f"Price is above SMA ({sma:.5f}) - Uptrend")
    else:
        score -= 2
        breakdown.append(f"Price is below SMA ({sma:.5f}) - Downtrend")

    # --- DECISION MAPPING ---
    # We define HOLD as a slight positive/neutral bias 
    # where we aren't ready to buy fresh, but wouldn't exit yet.
    if score >= 4:
        decision = "BUY"
    elif score <= -4:
        decision = "SELL"
    elif -1 <= score <= 2:
        decision = "HOLD"
    else:
        decision = "WAIT"

    return {
        "final_score": score,
        "decision": decision,
        "breakdown": breakdown
    }