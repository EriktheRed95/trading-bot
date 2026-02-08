import pandas as pd
import numpy as np

# --- HELPER FUNCTIONS ---
def calculate_rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).fillna(0)
    loss = (-delta.where(delta < 0, 0)).fillna(0)
    avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calculate_macd(series, fast=12, slow=26, signal=9):
    k = series.ewm(span=fast, adjust=False).mean()
    d = series.ewm(span=slow, adjust=False).mean()
    macd = k - d
    sig = macd.ewm(span=signal, adjust=False).mean()
    return macd, sig

def calculate_bbands(series, length=20, std=2):
    sma = series.rolling(window=length).mean()
    std_dev = series.rolling(window=length).std()
    upper = sma + (std * std_dev)
    lower = sma - (std * std_dev)
    return upper, lower

# --- STRATEGY LOGIC ---
def evaluate_stock_strategy(df):
    if len(df) < 200:
        return {"final_score": 0, "breakdown": ["Not enough data"]}

    score = 0
    reasons = []
    
    close = df['close'].iloc[-1]
    
    # --- INDICATORS ---
    sma50 = df['close'].rolling(window=50).mean().iloc[-1]
    sma200 = df['close'].rolling(window=200).mean().iloc[-1]
    
    rsi_series = calculate_rsi(df['close'])
    current_rsi = rsi_series.iloc[-1]
    
    macd, signal = calculate_macd(df['close'])
    macd_val = macd.iloc[-1]
    sig_val = signal.iloc[-1]

    # --- SCORING LOGIC (BALANCED) ---
    
    # 1. TREND FILTER
    in_major_uptrend = close > sma200

    if in_major_uptrend:
        score += 3
        reasons.append("STRONG TREND: Price > SMA200")
    elif close > sma50:
        score += 1
        reasons.append("RECOVERY: Price > SMA50 (Potential Reversal)")
    else:
        score -= 5
        reasons.append("WEAKNESS: Price < SMA50 & SMA200")
        
    # 2. RSI FILTER (Dip OR Momentum)
    if current_rsi < 35:
        score += 3
        reasons.append(f"Deep Value (RSI {current_rsi:.1f})")
    elif current_rsi < 50:
        score += 1
        reasons.append(f"Buy the Dip (RSI {current_rsi:.1f})")
    elif 50 <= current_rsi <= 70:
        score += 1  # Reward Momentum
        reasons.append(f"Momentum Strength (RSI {current_rsi:.1f})")
    elif current_rsi > 75:
        score -= 2
        reasons.append(f"Overbought (RSI {current_rsi:.1f})")
    
    # 3. MACD (Confirmation)
    if macd_val > sig_val:
        score += 1
        reasons.append("MACD Bullish")
    else:
        score -= 1
        reasons.append("MACD Bearish")

    # --- 🛡️ THE SHORT-SELLING SHIELD ---
    # If the stock is in a major uptrend, we forbid a negative score.
    # This prevents us from betting against "Rocket Ships" like NVDA or PLTR.
    if in_major_uptrend and score < 0:
        score = 0 
        reasons.append("🛡️ SHORT BLOCKED: Don't fight the SMA200 Trend!")

    return {
        "final_score": score,
        "breakdown": reasons
    }