import yfinance as yf
import pandas as pd
from datetime import datetime

class StockSensor:
    def __init__(self):
        self.watch_list = ["AAPL", "TSLA", "NVDA", "AMD"]
        self.indices = {
            "S&P500": "^GSPC",
            "NASDAQ": "^IXIC",
            "DOW": "^DJI"
        }
        print("Stock & Index Sensor initialized.")

    def get_market_mood(self):
        """Checks if the major indices are up or down (YTD)."""
        mood = {}
        for name, ticker in self.indices.items():
            try:
                data = yf.Ticker(ticker).fast_info
                mood[name] = round(data['year_to_date_change'] * 100, 2)
            except:
                mood[name] = "Data Unavailable"
        return mood

    def get_price(self, symbol="AAPL"):
        """Fetches the current stock price, bid, and ask."""
        try:
            ticker = yf.Ticker(symbol)
            data = ticker.fast_info
            return {
                "symbol": symbol,
                "price": round(data['last_price'], 2),
                "bid": round(data.get('bid', 0), 2), # Some stocks don't show live bids
                "ask": round(data.get('ask', 0), 2),
                "timestamp": datetime.now().strftime("%H:%M:%S")
            }
        except Exception as e:
            return {"error": str(e)}

    def get_historical_data(self, symbol="AAPL", interval="1m", period="1d"):
        """Fetches historical candles."""
        try:
            data = yf.download(symbol, period=period, interval=interval, progress=False)
            return data.tail(5)
        except Exception as e:
            print(f"Error fetching stock history: {e}")
            return None

# --- This is where we test the code ---
if __name__ == "__main__":
    sensor = StockSensor()
    
    print("\n1. Checking Market Mood:")
    print(sensor.get_market_mood())
    
    print("\n2. Checking Apple (AAPL) Price Details:")
    print(sensor.get_price("AAPL"))