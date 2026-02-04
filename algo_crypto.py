class CryptoAlgo:
    def evaluate(self, data):
        score = 0
        rsi = data.get('rsi', 50)
        price = data.get('price', 0)
        sma_200 = data.get('sma_200', 0)
        volume_change = data.get('volume_24h_change', 0) # New metric

        # 1. Momentum Surge (Aggressive RSI)
        # Crypto can stay overbought longer than stocks.
        if rsi < 25: 
            score += 40 
        elif rsi > 80: 
            score -= 40

        # 2. Volume Confirmation
        # If price is rising but volume is dropping, it's a "Trap."
        if volume_change > 10: # Volume up 10%
            score += 15
        elif volume_change < -20:
            score -= 15

        # 3. The "Moon" Guardrail
        # Even in crypto, the 200 SMA is respected by big "Whales."
        if price > sma_200:
            score += 10
        else:
            score -= 20

        return score