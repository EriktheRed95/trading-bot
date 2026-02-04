import yfinance as yf
import pandas as pd
import numpy as np

def calculate_rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def fetch_stock_data(ticker):
    print(f"--- SENSES: Fetching history for {ticker}... ---")
    try:
        # 1. Fetch History (We need this for SMA, RSI, and fallback price)
        stock = yf.Ticker(ticker)
        # Fetch 1 year to ensure we have enough for SMA 200
        hist = stock.history(period="1y")
        
        if hist.empty:
            return None

        # 2. robust Price Check
        # Try to get live price first
        price = stock.info.get('regularMarketPrice') or stock.info.get('currentPrice')
        
        # FALLBACK: If live price is None (common for Crypto/Forex), use last close
        if price is None:
            price = hist['Close'].iloc[-1]

        # 3. Calculate Indicators
        hist['sma_50'] = hist['Close'].rolling(window=50).mean()
        hist['sma_200'] = hist['Close'].rolling(window=200).mean()
        hist['rsi'] = calculate_rsi(hist['Close'])

        # 4. Prepare the Package
        # We return a DICTIONARY of values
        data = {
            'symbol': ticker,
            'current_price': price,
            'pe_ratio': stock.info.get('trailingPE'), # Might be None for crypto
            'sector': stock.info.get('sector', 'Unknown'),
            'sma_50': hist['sma_50'].iloc[-1],
            'sma_200': hist['sma_200'].iloc[-1],
            'rsi': hist['rsi'].iloc[-1],
            # CRITICAL: We pass the history DataFrame too so the Dashboard can graph it!
            'history': hist 
        }
        
        return data

    except Exception as e:
        print(f"Error fetching {ticker}: {e}")
        return None