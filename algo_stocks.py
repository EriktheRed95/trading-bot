import pandas as pd

def evaluate_stock_strategy(data):
    """
    Strategy: SMA Trend Following + RSI Momentum + Volume Confirmation
    """
    # 1. Prepare Data
    df = pd.DataFrame(data)
    if len(df) < 200:
        return {"final_score": 0, "decision": "WAIT", "breakdown": ["Not enough data for SMA 200"]}
    
    closes = df['close']
    volumes = df['volume']
    current_price = closes.iloc[-1]
    current_volume = volumes.iloc[-1]

    # 2. Calculate Indicators
    sma_50 = closes.rolling(window=50).mean().iloc[-1]
    sma_200 = closes.rolling(window=200).mean().iloc[-1]
    avg_volume = volumes.rolling(window=20).mean().iloc[-1]

    # RSI Calculation
    delta = closes.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs.iloc[-1]))

    # 3. Logic Evaluation
    score = 0
    breakdown = []

    # A. Trend (Golden Cross / Alignment)
    if current_price > sma_200:
        score += 2
        breakdown.append(f"Price > SMA 200 (Bullish Trend)")
    else:
        score -= 2
        breakdown.append(f"Price < SMA 200 (Bearish Trend)")

    if sma_50 > sma_200:
        score += 1
        breakdown.append("Golden Cross Active (SMA 50 > 200)")

    # B. Momentum (RSI)
    if rsi < 30:
        score += 3
        breakdown.append(f"RSI Oversold ({rsi:.1f})")
    elif rsi > 70:
        score -= 3
        breakdown.append(f"RSI Overbought ({rsi:.1f})")

    # C. Volume Confirmation (New)
    if current_volume > (avg_volume * 1.2):
        score += 1
        breakdown.append("High Volume detected (Validating move)")

    # 4. Decision
    if score >= 4: decision = "BUY"
    elif score <= -4: decision = "SELL"
    elif 1 <= score <= 3: decision = "HOLD"
    else: decision = "WAIT"

    return {"final_score": score, "decision": decision, "breakdown": breakdown}