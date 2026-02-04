import pandas as pd
import json
from journal import authenticate_google, load_journal

def decode_trading_history():
    print("--- 🔓 Journal Decoder: Analyzing Simulated Trades ---")
    
    # 1. Connect to Google Drive
    service = authenticate_google()
    if not service:
        print("Error: Could not connect to Google Drive.")
        return

    # 2. Load and Decrypt the Memory Bank
    # This uses your existing decryption logic from journal.py
    data, _ = load_journal(service)
    
    if not data:
        print("No trade data found in the memory bank.")
        return

    # 3. Convert to a Pandas DataFrame for analysis
    df = pd.DataFrame(data)

    # 4. Filter for actual trades (ignore manual notes for now)
    if 'type' in df.columns:
        trades_df = df[df['type'] == 'BOT_TRADE'].copy()
    else:
        # Fallback if the 'type' field is missing
        trades_df = df.copy()

    if trades_df.empty:
        print("No bot trades found. Maybe you only have manual notes?")
        return

    # 5. Calculate Basic Stats
    total_trades = len(trades_df)
    closed_trades = trades_df[trades_df['status'] == 'CLOSED']
    
    # Simple Win Rate calculation based on Profit/Loss
    if not closed_trades.empty and 'profit_loss' in closed_trades.columns:
        wins = len(closed_trades[closed_trades['profit_loss'] > 0])
        win_rate = (wins / len(closed_trades)) * 100
        avg_pl = closed_trades['profit_loss'].mean()
    else:
        win_rate = 0
        avg_pl = 0

    # 6. Display Summary to Terminal
    print("\n" + "="*50)
    print(f"📊 PERFORMANCE SUMMARY")
    print("-" * 50)
    print(f"Total Simulated Trades: {total_trades}")
    print(f"Closed Trades:         {len(closed_trades)}")
    print(f"Avg. Profit/Loss:      {avg_pl:.2f}%")
    print(f"Win Rate:              {win_rate:.1f}%")
    print("="*50 + "\n")

    # 7. Show Last 10 Trades in Terminal
    # We select the most relevant columns for display
    cols_to_show = ['timestamp_open', 'ticker', 'action', 'entry_price', 'status', 'profit_loss']
    # Filter only columns that actually exist to avoid errors
    existing_cols = [c for c in cols_to_show if c in trades_df.columns]
    
    print("🕒 RECENT ACTIVITY (Last 10 Trades):")
    print(trades_df[existing_cols].tail(10).to_string(index=False))

    # 8. Export to CSV for Excel/Google Sheets
    csv_filename = "trade_history_export.csv"
    trades_df.to_csv(csv_filename, index=False)
    print(f"\n✅ Full history exported to: {csv_filename}")

if __name__ == "__main__":
    decode_trading_history()