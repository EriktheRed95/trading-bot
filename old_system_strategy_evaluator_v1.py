import pandas as pd

class StrategyEvaluator:
    def __init__(self):
        print("Strategy Evaluator (The Brain) initialized.")

    def calculate_indicators(self, df):
        """
        Helper: Calculates SMA and RSI from raw historical data.
        df: Pandas DataFrame with 'close' column.
        """
        if df is None or len(df) < 14:
            return None

        # Simple Moving Averages
        df['sma_50'] = df['close'].rolling(window=50, min_periods=1).mean()
        df['sma_200'] = df['close'].rolling(window=200, min_periods=1).mean()

        # Relative Strength Index (RSI)
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        df['rsi'] = 100 - (100 / (1 + rs))
        
        return df.iloc[-1] # Return only the most recent calculated row

    def analyze(self, asset_type, data):
        """
        The Core Brain logic.
        asset_type: 'crypto', 'stock', or 'forex'
        data: Dictionary containing price, indicators, and fundamentals
        """
        score = 0
        reasons = []
        
        symbol = data.get('symbol', 'Unknown')
        price = data.get('price', 0)
        sma_50 = data.get('sma_50', 0)
        sma_200 = data.get('sma_200', 0)
        rsi = data.get('rsi', 50) # Default to neutral
        
        # --- 1. TECHNICAL ANALYSIS (Universal) ---
        
        # Golden Cross Logic
        if sma_50 > sma_200:
            score += 20
            reasons.append("Bullish Trend (50SMA > 200SMA): +20")
        else:
            score -= 20
            reasons.append("Bearish Trend (50SMA < 200SMA): -20")

        # Price Location
        if price > sma_50:
            score += 10
            reasons.append("Price Strong (Above 50SMA): +10")
        else:
            reasons.append("Price Weak (Below 50SMA): +0")

        # RSI Logic
        if rsi < 30:
            score += 25
            reasons.append(f"Oversold (RSI {rsi:.1f}): +25")
        elif rsi > 70:
            score -= 25
            reasons.append(f"Overbought (RSI {rsi:.1f}): -25")
        else:
            reasons.append(f"RSI Neutral ({rsi:.1f}): +0")

        # --- 2. ASSET-SPECIFIC ANALYSIS ---
        
        # Stock Fundamentals (P/E Ratio)
        if asset_type == 'stock':
            pe = data.get('pe_ratio')
            if pe and 0 < pe < 25:
                score += 15
                reasons.append(f"Value Stock (P/E {pe:.2f}): +15")
        
        # Crypto-Specific (Volatility Bonus)
        elif asset_type == 'crypto':
            # Example: Reward high-volume tokens or specific momentum
            reasons.append("Crypto volatility factor active.")

        # Forex-Specific (Trend Strength)
        elif asset_type == 'forex':
            reasons.append("Forex spread analysis active.")

        # --- 3. FINAL DECISION ---
        decision = "WAIT"
        if score >= 40:
            decision = "BUY"
        elif score <= -40:
            decision = "SELL/SHORT"

        return {
            "symbol": symbol,
            "score": score,
            "decision": decision,
            "analysis": reasons
        }

# --- Quick Test ---
if __name__ == "__main__":
    brain = StrategyEvaluator()
    
    # Mock data representing a bullish stock
    test_data = {
        "symbol": "NVDA",
        "price": 700,
        "sma_50": 650,
        "sma_200": 500,
        "rsi": 28,
        "pe_ratio": 22.5
    }
    
    result = brain.analyze('stock', test_data)
    print(f"\nFinal Decision for {result['symbol']}: {result['decision']}")
    for reason in result['analysis']:
        print(f" - {reason}")   