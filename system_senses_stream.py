import yfinance as yf

from indicators import calculate_rsi


def fetch_stock_data(ticker):
    print(f"--- SENSES: Fetching history for {ticker}... ---")
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period="1y")
        if hist.empty:
            return None

        # Prefer live price; fall back to last close (forex/crypto often have no live quote).
        price = stock.info.get('regularMarketPrice') or stock.info.get('currentPrice')
        if price is None:
            price = hist['Close'].iloc[-1]

        hist['sma_50'] = hist['Close'].rolling(window=50).mean()
        hist['sma_200'] = hist['Close'].rolling(window=200).mean()
        hist['rsi'] = calculate_rsi(hist['Close'])

        return {
            'symbol': ticker,
            'current_price': price,
            'pe_ratio': stock.info.get('trailingPE'),
            'sector': stock.info.get('sector', 'Unknown'),
            'sma_50': hist['sma_50'].iloc[-1],
            'sma_200': hist['sma_200'].iloc[-1],
            'rsi': hist['rsi'].iloc[-1],
            'history': hist,
        }
    except Exception as e:
        print(f"Error fetching {ticker}: {e}")
        return None
