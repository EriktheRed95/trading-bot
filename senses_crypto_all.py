import ccxt
import pandas as pd
from datetime import datetime

class CryptoSensor:
    def __init__(self):
        self.exchange = ccxt.kraken()
        # Common top coins (excluding stablecoins)
        self.top_altcoins = [
            'BTC/USD', 'ETH/USD', 'SOL/USD', 'XRP/USD', 
            'ADA/USD', 'DOGE/USD', 'AVAX/USD', 'DOT/USD', 
            'LINK/USD', 'MATIC/USD'
        ]

    def get_top_market_snapshot(self):
        """Fetches live prices for all 10 target coins at once."""
        print(f"Scanning market at {datetime.now().strftime('%H:%M:%S')}...")
        results = []
        for symbol in self.top_altcoins:
            try:
                ticker = self.exchange.fetch_ticker(symbol)
                results.append({
                    "symbol": symbol,
                    "price": ticker['last'],
                    "change_24h": ticker['percentage']
                })
            except Exception as e:
                continue
        return pd.DataFrame(results)

if __name__ == "__main__":
    sensor = CryptoSensor()
    df = sensor.get_top_market_snapshot()
    print("\n--- Top 10 Crypto Snapshot ---")
    print(df.to_string(index=False))