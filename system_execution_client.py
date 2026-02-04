import json
from schwab.auth import client_from_token_file
from schwab.orders.equities import equity_buy_market, equity_sell_market

# --- Configuration ---
SECRETS_FILE = 'schwab_keys.json'
TOKEN_FILE = 'token.json'
ACCOUNT_ID = 12345678  # <--- REPLACE THIS with your actual 8-digit Schwab Account ID

# Inside system_execution_client.py

def get_schwab_client():
    """Authenticates with Schwab and returns a client object."""
    try:
        with open(SECRETS_FILE, 'r') as f:
            secrets = json.load(f)
            
        print("Attempting to connect to Schwab...")
        # FIX: Removed 'callback_url' from this specific call
        client = client_from_token_file(
            token_path=TOKEN_FILE,
            api_key=secrets['app_key'],
            app_secret=secrets['app_secret']
        )
        print(">> Connection Successful! 'The Hands' are ready.")
        return client
    except Exception as e:
        print(f"Error connecting to Schwab: {e}")
        return None

def get_current_positions(client):
    """
    Step 1: Dynamic Portfolio Sync
    Fetches the list of tickers you currently own in your Schwab account.
    """
    if not client:
        return []

    print("Checking current portfolio holdings...")
    try:
        # Fetch account details including positions
        # Note: 'fields' parameter might vary based on API version, keeping it simple
        resp = client.account_details(ACCOUNT_ID, fields=client.Account.Fields.POSITIONS)
        
        # Parse the response to get a list of symbols
        # This structure depends on the exact Schwab API response format
        positions = []
        if resp.ok:
            data = resp.json()
            # Navigate the JSON response (example structure)
            acct_positions = data.get('securitiesAccount', {}).get('positions', [])
            for p in acct_positions:
                symbol = p.get('instrument', {}).get('symbol')
                if symbol:
                    positions.append(symbol)
        
        print(f"Verified Holdings: {positions}")
        return positions

    except Exception as e:
        print(f"Error fetching positions: {e}")
        return []

def execute_trade(client, ticker, decision):
    """
    Step 2: 'Hands' Activation
    Executes Market Buy/Sell orders based on the decision.
    """
    if client is None:
        print(f"!! Execution Failed: No active Schwab client for {ticker}.")
        return

    # 1. Handle BUY
    if decision == "BUY":
        print(f"*** EXECUTING BUY ORDER FOR {ticker} ***")
        try:
            # Uncomment the next two lines to go LIVE
            # resp = client.place_order(ACCOUNT_ID, equity_buy_market(ticker, 1))
            # print(f"Order Status: {resp.status_code}")
            print(f" -> [PAPER TRADE] Bought 1 share of {ticker} (Simulation)")
        except Exception as e:
            print(f" ! Order Failed: {e}")

    # 2. Handle SELL
    elif decision == "SELL":
        print(f"*** EXECUTING SELL ORDER FOR {ticker} ***")
        try:
            # Uncomment the next two lines to go LIVE
            # resp = client.place_order(ACCOUNT_ID, equity_sell_market(ticker, 1))
            # print(f"Order Status: {resp.status_code}")
            print(f" -> [PAPER TRADE] Sold 1 share of {ticker} (Simulation)")
        except Exception as e:
            print(f" ! Order Failed: {e}")

    # 3. Handle HOLD or WAIT
    else:
        # We generally don't send orders for HOLD/WAIT
        pass