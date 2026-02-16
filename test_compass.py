import pandas as pd
import yfinance as yf
import numpy as np

def calculate_adx_debug(df, period=14):
    """
    Strict, step-by-step ADX calculation with debug prints.
    """
    df = df.copy()
    print(f"\n--- DEBUGGING ADX MATH (First 5 Valid Rows) ---")
    
    # 1. True Range (TR)
    df['h-l'] = df['high'] - df['low']
    df['h-pc'] = abs(df['high'] - df['close'].shift(1))
    df['l-pc'] = abs(df['low'] - df['close'].shift(1))
    df['tr'] = df[['h-l', 'h-pc', 'l-pc']].max(axis=1)
    
    # 2. Directional Movement (THE CRITICAL FIX)
    # UpMove = High - PrevHigh
    # DownMove = PrevLow - Low (Ensures positive magnitude for drops)
    df['up_move'] = df['high'] - df['high'].shift(1)
    df['down_move'] = df['low'].shift(1) - df['low']
    
    # If move is negative (e.g. price went down on up_move), set to 0
    df['plus_dm'] = np.where((df['up_move'] > df['down_move']) & (df['up_move'] > 0), df['up_move'], 0)
    df['minus_dm'] = np.where((df['down_move'] > df['up_move']) & (df['down_move'] > 0), df['down_move'], 0)
    
    # 3. SMOOTHING (Wilder's Method)
    alpha = 1 / period
    
    # Use EWM to smooth. 
    # Note: We must handle the first values carefully in a real engine, 
    # but for this test EWM with adjust=False is sufficient approximation.
    df['tr_smooth'] = df['tr'].ewm(alpha=alpha, adjust=False).mean()
    df['plus_dm_smooth'] = df['plus_dm'].ewm(alpha=alpha, adjust=False).mean()
    df['minus_dm_smooth'] = df['minus_dm'].ewm(alpha=alpha, adjust=False).mean()
    
    # 4. Directional Indicators (+DI, -DI)
    # Avoid Division by Zero
    tr_safe = df['tr_smooth'].replace(0, np.nan)
    df['plus_di'] = 100 * (df['plus_dm_smooth'] / tr_safe)
    df['minus_di'] = 100 * (df['minus_dm_smooth'] / tr_safe)
    
    # 5. DX (The Directional Index)
    sum_di = df['plus_di'] + df['minus_di']
    diff_di = abs(df['plus_di'] - df['minus_di'])
    df['dx'] = 100 * (diff_di / sum_di.replace(0, np.nan))
    
    # 6. ADX (Smoothed DX)
    df['adx'] = df['dx'].ewm(alpha=alpha, adjust=False).mean()
    
    # DEBUG OUTPUT
    cols = ['plus_dm', 'minus_dm', 'plus_di', 'minus_di', 'dx', 'adx']
    # Show rows 20-25 (to skip the initial NaNs)
    print(df[cols].iloc[20:25]) 
    
    return df['adx']

# --- RUN TEST ---
print("⏳ Fetching PYPL data...")
df = yf.download('PYPL', period='1y', interval='1d', progress=False)

# Clean Columns
if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
df.rename(columns={'Open':'open','High':'high','Low':'low','Close':'close'}, inplace=True)

# Run Calculation
adx = calculate_adx_debug(df)

print(f"\n✅ FINAL ADX CHECK:")
print(f"Min: {adx.min():.4f}")
print(f"Max: {adx.max():.4f}")
print(f"Current: {adx.iloc[-1]:.4f}")

if adx.max() > 100 or adx.min() < 0:
    print("\n❌ FAIL: ADX IS STILL BROKEN")
else:
    print("\n✅ SUCCESS: ADX IS WITHIN 0-100 RANGE")