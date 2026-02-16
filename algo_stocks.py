import pandas as pd
import numpy as np

# --- HELPER FUNCTIONS ---
def calculate_rsi(series, period=14):
    delta = series.diff()
    
    # 1. Separate Gains and Losses
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    
    # 2. Calculate Smoothed Averages (Wilder's Method)
    avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
    
    # 3. Calculate RS and RSI (Safe Division)
    # Add a tiny epsilon (1e-9) to avoid Division by Zero errors
    rs = avg_gain / (avg_loss + 1e-9)
    rsi = 100 - (100 / (1 + rs))
    
    return rsi.fillna(50)

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

    # --- SCORING ---
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

    # SHORT SHIELD
    if in_major_uptrend and score < 0:
        score = 0 
        reasons.append("SHORT BLOCKED")

    return {
        "final_score": score,
        "breakdown": reasons
    }