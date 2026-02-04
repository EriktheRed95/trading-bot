import json
from schwab.auth import client_from_token_file
from schwab.orders.common import OrderStrategyType, Session, Duration
from schwab.orders.equity import equity_buy_market, equity_sell_market

# Define where our files are located
SECRETS_FILE = 'secrets.json'
TOKEN_FILE = 'token.json'

def get_schwab_client():
    """Authenticates with Schwab and returns a client object."""
    try:
        with open(SECRETS_FILE, 'r') as f:
            secrets = json.load(f)
            
        client = client_from_token_file(
            token_path=TOKEN_FILE,
            api_key=secrets['app_key'],
            app_secret=secrets['app_secret'],
            callback_url=secrets['callback_url']
        )
        return client
    except Exception as e:
        print(f"Error connecting to Schwab: {e}")
        return None

def execute_trade(client, ticker, decision):
    """
    The 'Hands' in action. 
    Takes the client and the brain's decision to place orders.
    """
    if client is None:
        print("!! Execution Failed: No active Schwab client.")
        return

    # 1. Handle BUY Signal
    if decision == "BUY":
        print(f"*** SENDING BUY ORDER FOR {ticker} ***")
        # For simplicity, we are buying 1 share at Market price
        # Note: Forex pairs might require 'fx_buy_market' depending on Schwab's API version
        response = client.place_order(
            # This is your account ID (usually found in your Schwab portal)
            account_id=12345678, 
            order_spec=equity_buy_market(ticker, 1)
        )
        print(f"Order Status: {response.status_code}")

    # 2. Handle SELL Signal
    elif decision == "SELL":
        print(f"*** SENDING SELL ORDER FOR {ticker} ***")
        response = client.place_order(
            account_id=12345678,
            order_spec=equity_sell_market(ticker, 1)
        )
        print(f"Order Status: {response.status_code}")

    # 3. Handle HOLD Signal
    elif decision == "HOLD":
        print(f"--- HOLD: Decision for {ticker} is to maintain current position. No order sent. ---")

    # 4. Handle WAIT (Neutral)
    else:
        print(f"--- WAIT: No actionable signal for {ticker}. ---")

if __name__ == "__main__":
    client = get_schwab_client()