from indicators import calculate_rsi, calculate_macd


def evaluate_crypto_strategy(df):
    """Crypto: trend filter on SMA200 plus RSI/MACD confirmation."""
    if len(df) < 200:
        return {"final_score": 0, "breakdown": ["Not enough data (Need 200+ candles)"]}

    score = 0
    reasons = []

    close = df['close'].iloc[-1]
    sma200 = df['close'].rolling(window=200).mean().iloc[-1]
    current_rsi = calculate_rsi(df['close']).iloc[-1]
    macd_line, signal_line = calculate_macd(df['close'])
    macd_val = macd_line.iloc[-1]
    sig_val = signal_line.iloc[-1]

    # Trend filter — below SMA200, effectively ban buying.
    if close < sma200:
        score -= 10
        reasons.append(f"BEARISH TREND: Price ${close:.2f} < SMA200 ${sma200:.2f}")
    else:
        score += 2
        reasons.append("BULLISH TREND: Price > SMA200")

        if current_rsi < 35:
            score += 3
            reasons.append(f"Oversold Dip (RSI {current_rsi:.1f})")
        elif current_rsi > 75:
            score -= 2
            reasons.append(f"Overbought (RSI {current_rsi:.1f})")

        if macd_val > sig_val:
            score += 2
            reasons.append("MACD Bullish Cross")
        elif macd_val < sig_val:
            score -= 1
            reasons.append("MACD Bearish Cross")

    return {"final_score": score, "breakdown": reasons}
