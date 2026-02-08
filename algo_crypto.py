import pandas as pd
import numpy as np

# --- HELPER FUNCTIONS (Native Math) ---
def calculate_rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).fillna(0)
    loss = (-delta.where(delta < 0, 0)).fillna(0)
    
    # Use exponential moving average (Wilder's method approximation)
    avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
    
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

def calculate_macd(series, fast=12, slow=26, signal=9):
    k = series.ewm(span=fast, adjust=False).mean()
    d = series.ewm(span=slow, adjust=False).mean()
    macd_line = k - d
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line, signal_line

# --- STRATEGY LOGIC ---
def evaluate_crypto_strategy(df):
    """
    Crypto Logic: High Volatility Protection + Trend Following.
    Dependency-free version.
    """
    # 1. Ensure we have enough data
    if len(df) < 200:
        return {
            "final_score": 0,
            "breakdown": ["Not enough data (Need 200+ candles)"]
        }

    score = 0
    reasons = []
    
    # Get latest values
    close = df['close'].iloc[-1]
    
    # --- INDICATORS ---
    # 1. SMA 200 (The Trend Guard)
    sma200 = df['close'].rolling(window=200).mean().iloc[-1]

    # 2. RSI (Native Calculation)
    rsi_series = calculate_rsi(df['close'])
    current_rsi = rsi_series.iloc[-1]

    # 3. MACD (Native Calculation)
    macd_line_series, signal_line_series = calculate_macd(df['close'])
    macd_line = macd_line_series.iloc[-1]
    signal_line = signal_line_series.iloc[-1]

    # --- SCORING LOGIC ---

    # A. TREND FILTER (The Crash Protector)
    # If BTC is below the 200 SMA, it's often a "falling knife".
    if close < sma200:
        score -= 10  # Massive penalty. Effectively bans buying.
        reasons.append(f"BEARISH TREND: Price ${close:.2f} < SMA200 ${sma200:.2f}")
    else:
        score += 2
        reasons.append("BULLISH TREND: Price > SMA200")

        # B. RSI Checks
        if current_rsi < 35:
            score += 3  # Strong buy signal if trend is UP
            reasons.append(f"Oversold Dip (RSI {current_rsi:.1f})")
        elif current_rsi > 75:
            score -= 2
            reasons.append(f"Overbought (RSI {current_rsi:.1f})")
        
        # C. MACD Checks
        if macd_line > signal_line:
            score += 2
            reasons.append("MACD Bullish Cross")
        elif macd_line < signal_line:
            score -= 1
            reasons.append("MACD Bearish Cross")

    # --- FINAL VERDICT ---
    return {
        "final_score": score,
        "breakdown": reasons
    }