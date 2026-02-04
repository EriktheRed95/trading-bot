import pandas as pd

def evaluate_forex_strategy(data):
    """
    Strategy: Mean Reversion using Bollinger Bands + RSI
    """
    df = pd.DataFrame(data)
    closes = df['close']
    current_price = closes.iloc[-1]

    # 1. Calculate Indicators
    # Bollinger Bands (20 SMA, 2 Std Dev)
    sma_20 = closes.rolling(window=20).mean()
    std_dev = closes.rolling(window=20).std()
    upper_band = (sma_20 + (std_dev * 2)).iloc[-1]
    lower_band = (sma_20 - (std_dev * 2)).iloc[-1]
    
    # RSI
    delta = closes.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs.iloc[-1]))

    score = 0
    breakdown = []

    # 2. Logic
    # Buy if price touches Lower Band AND RSI is low (Bounce Up)
    if current_price <= lower_band:
        score += 3
        breakdown.append(f"Price touching Lower Band ({lower_band:.4f})")
    
    if current_price >= upper_band:
        score -= 3
        breakdown.append(f"Price touching Upper Band ({upper_band:.4f})")

    if rsi < 30:
        score += 2
        breakdown.append(f"RSI Oversold ({rsi:.1f})")
    elif rsi > 70:
        score -= 2
        breakdown.append(f"RSI Overbought ({rsi:.1f})")

    # 3. Decision
    if score >= 4: decision = "BUY"
    elif score <= -4: decision = "SELL"
    elif score > 0: decision = "HOLD" # Bias is up, keep holding
    else: decision = "WAIT"

    return {"final_score": score, "decision": decision, "breakdown": breakdown}