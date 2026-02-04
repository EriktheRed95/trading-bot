import yfinance as yf
import pandas as pd

def fetch_macro_data():
    """
    Fetches key macro-economic indicators.
    """
    try:
        # Fetch data
        tickers = ['^TNX', '^VIX']
        # 'progress=False' hides the ugly download bars in your dashboard
        data = yf.download(tickers, period="5d", progress=False)['Close']
        
        # Check if data is empty (prevents crashes)
        if data.empty:
            return None

        # Extract TNX (Treasury Yield)
        current_tnx = data['^TNX'].iloc[-1]
        prev_tnx = data['^TNX'].iloc[-2]
        # Calculate percentage change safely
        tnx_change = 0.0
        if prev_tnx != 0:
            tnx_change = ((current_tnx - prev_tnx) / prev_tnx) * 100

        # Extract VIX (Volatility)
        current_vix = data['^VIX'].iloc[-1]

        return {
            "tnx_yield": float(current_tnx),
            "tnx_change_pct": float(tnx_change),
            "vix": float(current_vix)
        }
    except Exception as e:
        print(f"Error fetching macro data: {e}")
        return None

def analyze_market_regime(macro_data):
    """
    Returns a 'Market Regime' modifier based on macro data.
    """
    # 1. Safety Check: If data is missing, return a neutral "Safe Mode"
    if not macro_data:
        return {
            "score_modifier": 0,
            "regime_reasons": ["Data Unavailable - Neutral"]
        }

    score_modifier = 0
    reasons = []

    # 2. VIX Analysis (Fear)
    try:
        vix = macro_data.get('vix', 0)
        if vix > 30:
            score_modifier -= 20
            reasons.append(f"EXTREME FEAR (VIX: {vix:.2f})")
        elif vix > 20:
            score_modifier -= 10
            reasons.append(f"High Volatility (VIX: {vix:.2f})")
        else:
            score_modifier += 5
            reasons.append("Volatility Stable (Risk On)")

        # 3. Bond Yield Analysis (Gravity)
        tnx_change = macro_data.get('tnx_change_pct', 0)
        if tnx_change > 3.0:
            score_modifier -= 15
            reasons.append(f"Yield Spike Alert (+{tnx_change:.2f}%)")
        elif tnx_change < -2.0:
            score_modifier += 5
            reasons.append("Yields Cooling Off")

    except Exception as e:
        reasons.append(f"Calculation Error: {e}")

    # 4. FINAL RETURN (Must be at the root indentation level)
    return {
        "score_modifier": score_modifier,
        "regime_reasons": reasons
    }