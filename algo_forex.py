import pandas as pd

from indicators import calculate_rsi, calculate_bbands


def evaluate_forex_strategy(data):
    """Forex: Bollinger-band mean reversion with RSI confirmation."""
    df = pd.DataFrame(data)
    closes = df['close']
    current_price = closes.iloc[-1]

    upper_band, lower_band = calculate_bbands(closes)
    upper = upper_band.iloc[-1]
    lower = lower_band.iloc[-1]
    rsi = calculate_rsi(closes).iloc[-1]

    score = 0
    breakdown = []

    if current_price <= lower:
        score += 3
        breakdown.append(f"Price touching Lower Band ({lower:.4f})")
    if current_price >= upper:
        score -= 3
        breakdown.append(f"Price touching Upper Band ({upper:.4f})")

    if rsi < 30:
        score += 2
        breakdown.append(f"RSI Oversold ({rsi:.1f})")
    elif rsi > 70:
        score -= 2
        breakdown.append(f"RSI Overbought ({rsi:.1f})")

    if score >= 4:
        decision = "BUY"
    elif score <= -4:
        decision = "SELL"
    elif score > 0:
        decision = "HOLD"
    else:
        decision = "WAIT"

    return {"final_score": score, "decision": decision, "breakdown": breakdown}
