class StockAlgo:
    def evaluate(self, data):
        score = 0
        price = data.get('price', 0)
        sma_50 = data.get('sma_50', 0)
        sma_200 = data.get('sma_200', 0) # Added
        rsi = data.get('rsi', 50)
        pe = data.get('pe_ratio')

        # 1. Macro Trend (The SMA 200 Filter)
        if price > sma_200:
            score += 15
        else:
            score -= 25 # Heavy penalty for being in a downtrend

        # 2. Medium Trend (Golden Cross potential)
        if sma_50 > sma_200:
            score += 10

        # 3. Fundamental Value (P/E)
        if pe and 0 < pe < 20: 
            score += 15

        # 4. Momentum (RSI)
        if rsi < 30: score += 20
        elif rsi > 70: score -= 20

        return score