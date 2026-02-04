import yfinance as yf
from datetime import datetime

class ForexSensor:
    def __init__(self):
        print("Forex Sensor initialized")

    def get_exchange_rate(self, base="EUR", quote="USD"):
        """Fetches exchange rate for currency pairs."""
        symbol = f"{base}{quote}=X"
        try:
            ticker = yf.Ticker(symbol)
            rate = ticker.fast_info['last_price']
            return {
                "pair": f"{base}/{quote}",
                "rate": round(rate, 5),
                "timestamp": datetime.now().strftime("%H:%M:%S")
            }
        except Exception as e:
            return {"error": str(e)}

if __name__ == "__main__":
    sensor = ForexSensor()
    print("\n--- Checking EUR/USD Rate ---")
    print(sensor.get_exchange_rate("EUR", "USD"))