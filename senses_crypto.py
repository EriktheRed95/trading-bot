import ccxt
import time
import pandas as pd
from datetime import datetime

class CryptoSensor:
    def __init__(self):
        # We use Kraken because it doesn't require an API key for public data fetching
        self.exchange = ccxt.kraken()
        print(f"Crypto Sensor initialized: Connected to {self.exchange.name}")

    def get_price(self, symbol="BTC/USD"):
        """Fetches the current bid/ask price for a crypto pair."""
        try:
            ticker = self.exchange.fetch_ticker(symbol)
            return {
                "symbol": symbol,
                "price": ticker['last'],
                "bid": ticker['bid'],
                "ask": ticker['ask'],
                "timestamp": datetime.now().strftime("%H:%M:%S")
            }
        except Exception as e:
            return {"error": str(e)}

    def get_historical_data(self, symbol="BTC/USD", timeframe='1m', limit=5):
        """Fetches the last N candles (Open, High, Low, Close)."""
        try:
            ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            # Convert to readable DataFrame
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            return df
        except Exception as e:
            print(f"Error fetching history: {e}")
            return None

# --- Quick Test Block ---
if __name__ == "__main__":
    sensor = CryptoSensor()
    
    # 1. Test Live Price
    print("\n--- Checking Live Bitcoin Price ---")
    price_data = sensor.get_price("BTC/USD")
    print(f"Current Price: ${price_data['price']}")
    
    # 2. Test Recent History
    print("\n--- Checking Last 5 Minutes of Data ---")
    history = sensor.get_historical_data("BTC/USD")
    print(history[['timestamp', 'close']])