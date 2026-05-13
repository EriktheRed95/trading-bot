from indicators import calculate_rsi, calculate_macd, calculate_bbands  # noqa: F401


def evaluate_stock_strategy(df):
    if len(df) < 200:
        return {"final_score": 0, "breakdown": ["Not enough data"]}

    score = 0
    reasons = []

    close = df['close'].iloc[-1]
    sma50 = df['close'].rolling(window=50).mean().iloc[-1]
    sma200 = df['close'].rolling(window=200).mean().iloc[-1]

    current_rsi = calculate_rsi(df['close']).iloc[-1]
    macd, signal = calculate_macd(df['close'])
    macd_val = macd.iloc[-1]
    sig_val = signal.iloc[-1]

    in_major_uptrend = close > sma200

    # Trend
    if in_major_uptrend:
        score += 3
        reasons.append("STRONG TREND")
    elif close > sma50:
        score += 1
        reasons.append("RECOVERY")
    else:
        score -= 5
        reasons.append("WEAKNESS")

    # RSI
    if current_rsi < 35:
        score += 3
        reasons.append(f"OVERSOLD ({current_rsi:.0f})")
    elif current_rsi < 50:
        score += 1
        reasons.append(f"DIP ({current_rsi:.0f})")
    elif current_rsi > 75:
        score -= 2
        reasons.append(f"OVERBOUGHT ({current_rsi:.0f})")

    # MACD
    if macd_val > sig_val:
        score += 1
        reasons.append("MACD BULL")
    else:
        score -= 1
        reasons.append("MACD BEAR")

    # Don't short in a major uptrend.
    if in_major_uptrend and score < 0:
        score = 0
        reasons.append("SHORT BLOCKED")

    return {"final_score": score, "breakdown": reasons}
