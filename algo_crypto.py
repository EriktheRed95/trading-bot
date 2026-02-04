import pandas as pd

def evaluate_crypto_strategy(data):
    """
    Strategy: Momentum Chasing (MACD + RSI) with Whale Guardrails
    """
    df = pd.DataFrame(data)
    closes = df['close']
    
    # 1. Calculate Indicators
    # MACD (12, 26, 9)
    exp1 = closes.ewm(span=12, adjust=False).mean()
    exp2 = closes.ewm(span=26, adjust=False).mean()
    macd_line = exp1 - exp2
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    
    macd_val = macd_line.iloc[-1]
    signal_val = signal_line.iloc[-1]
    
    # RSI
    delta = closes.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs.iloc[-1]))

    sma_200 = closes.rolling(window=200).mean().iloc[-1]
    current_price = closes.iloc[-1]

    score = 0
    breakdown = []

    # 2. Logic
    # A. MACD Crossover (Strong Momentum Signal)
    if macd_val > signal_val:
        score += 3
        breakdown.append(f"MACD Bullish Cross ({macd_val:.2f} > {signal_val:.2f})")
    else:
        score -= 3
        breakdown.append("MACD Bearish Trend")

    # B. RSI Filter (Don't buy the top)
    if rsi > 80:
        score -= 10 # Hard stop: Too dangerous
        breakdown.append("RSI Danger Zone (>80)")
    elif rsi < 30:
        score += 2
        breakdown.append("RSI Dip Buy Opportunity")

    # C. Whale Guardrail (Trend Filter)
    if current_price > sma_200:
        score += 1
        breakdown.append("Above 200 SMA (Bull Market)")
    else:
        score -= 2
        breakdown.append("Below 200 SMA (Bear Market - Be Careful)")

    # 3. Decision
    if score >= 4: decision = "BUY"
    elif score <= -4: decision = "SELL"
    elif 0 <= score < 4: decision = "HOLD"
    else: decision = "WAIT"

    return {"final_score": score, "decision": decision, "breakdown": breakdown}