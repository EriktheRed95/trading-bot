import yfinance as yf
import pandas as pd

def calculate_rsi(data, window=14):
    """ Calculates the Relative Strength Index (RSI) manually """
    delta = data['Close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=window).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=window).mean()
    
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

def fetch_stock_data(ticker_symbol):
    print(f"--- SENSES: Fetching history for {ticker_symbol}... ---")
    
    try:
        stock = yf.Ticker(ticker_symbol)
        
        # 1. Get Basic Info (Fundamentals)
        info = stock.info
        current_price = info.get('currentPrice')
        
        # 2. Get History (Technical Analysis needs past data)
        # We need at least 260 days to calculate a 200-day moving average accurately
        hist = stock.history(period="1y")
        
        if hist.empty:
            return None

        # 3. Calculate Indicators
        # Calculate 50-Day SMA
        hist['50_SMA'] = hist['Close'].rolling(window=50).mean()
        # Calculate 200-Day SMA
        hist['200_SMA'] = hist['Close'].rolling(window=200).mean()
        # Calculate RSI
        hist['RSI'] = calculate_rsi(hist)

        # Get the latest values (the last row of the table)
        latest = hist.iloc[-1]
        
        data = {
            'symbol': ticker_symbol,
            'current_price': current_price,
            'pe_ratio': info.get('forwardPE', None),
            'sector': info.get('sector', 'Unknown'),
            
            # New Technical Data
            'sma_50': latest['50_SMA'],
            'sma_200': latest['200_SMA'],
            'rsi': latest['RSI']
        }
        
        return data

    except Exception as e:
        print(f"Error fetching data: {e}")
        return None