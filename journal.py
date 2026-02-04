import os
import json
import base64
import io
import uuid # For unique Trade IDs
from datetime import datetime
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload

# Security imports
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.backends import default_backend

# --- CONFIGURATION ---
SCOPES = ['https://www.googleapis.com/auth/drive.appdata']
TOKEN_FILE = 'token.json'
SECRETS_FILE = 'secrets.json'
JOURNAL_FILENAME = 'trading_memory_bank.json.enc' 

# --- AUTHENTICATION ---
def authenticate_google():
    """Handles Google OAuth authentication."""
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(SECRETS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, 'w') as token:
            token.write(creds.to_json())
            
    return build('drive', 'v3', credentials=creds)

# --- ENCRYPTION LOGIC ---
def get_encryption_key():
    """
    Generates the key. 
    NOTE: For the bot to run automatically 24/7, we cannot ask for a password input.
    We use a static salt and a hardcoded key (or env variable) for this version.
    """
    salt = b'static_salt_for_trading_bot' # In prod, store this in env variables
    password = b"my_super_secret_bot_password" 
    
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=100000,
        backend=default_backend()
    )
    return base64.urlsafe_b64encode(kdf.derive(password))

# --- CORE IO FUNCTIONS ---
def load_journal(service):
    """Downloads and decrypts the current journal into a JSON list."""
    try:
        key = get_encryption_key()
        fernet = Fernet(key)
        
        query = f"name = '{JOURNAL_FILENAME}' and 'appDataFolder' in parents"
        results = service.files().list(q=query, spaces='appDataFolder').execute()
        files = results.get('files', [])

        if not files:
            return [], None # Returns empty list and None for file_id

        file_id = files[0]['id']
        request = service.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while done is False:
            status, done = downloader.next_chunk()
            
        encrypted_data = fh.getvalue()
        decrypted_data = fernet.decrypt(encrypted_data)
        return json.loads(decrypted_data.decode('utf-8')), file_id

    except Exception as e:
        print(f"Journal Load Info (Starting fresh?): {e}")
        return [], None

def save_journal(service, data, file_id=None):
    """Encrypts and uploads the JSON list."""
    key = get_encryption_key()
    fernet = Fernet(key)
    
    json_str = json.dumps(data, indent=2)
    encrypted_blob = fernet.encrypt(json_str.encode('utf-8'))
    
    media = MediaIoBaseUpload(io.BytesIO(encrypted_blob), mimetype='application/octet-stream', resumable=True)
    
    if file_id:
        service.files().update(fileId=file_id, media_body=media).execute()
    else:
        metadata = {'name': JOURNAL_FILENAME, 'parents': ['appDataFolder']}
        service.files().create(body=metadata, media_body=media).execute()
    print(" >> [MEMORY BANK] Sync Complete.")

# --- BOT FUNCTIONS (For main.py) ---

def log_trade_open(ticker, action, score, reasons, price):
    """Logs the start of a trade (Called by main.py)."""
    service = authenticate_google()
    current_log, file_id = load_journal(service)
    
    trade_entry = {
        "id": str(uuid.uuid4())[:8],
        "type": "BOT_TRADE",
        "timestamp_open": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "ticker": ticker,
        "action": action,
        "entry_price": price,
        "score_at_entry": score,
        "reasons": reasons,
        "status": "OPEN",
        "timestamp_close": None,
        "exit_price": None,
        "profit_loss": None
    }
    
    current_log.append(trade_entry)
    save_journal(service, current_log, file_id)
    return trade_entry['id']

def log_trade_close(trade_id, exit_price):
    """Updates an existing trade with the outcome (Called by main.py)."""
    service = authenticate_google()
    current_log, file_id = load_journal(service)
    
    found = False
    for trade in current_log:
        if trade.get('id') == trade_id:
            trade['status'] = "CLOSED"
            trade['timestamp_close'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            trade['exit_price'] = exit_price
            
            # Simple P/L calc
            if trade['entry_price']:
                if trade['action'] == "BUY":
                    pl = (exit_price - trade['entry_price']) / trade['entry_price']
                else: # SELL/SHORT
                    pl = (trade['entry_price'] - exit_price) / trade['entry_price']
                trade['profit_loss'] = round(pl * 100, 2)
            
            found = True
            print(f" >> Trade {trade_id} Closed. P/L: {trade.get('profit_loss')}%")
            break
            
    if found:
        save_journal(service, current_log, file_id)
    else:
        print(f"Error: Trade ID {trade_id} not found.")

# --- HUMAN FUNCTION (Manual Entry) ---

def manual_entry():
    """Allows you to add a manual note to the JSON database."""
    print("--- Manual Journal Entry ---")
    service = authenticate_google()
    current_log, file_id = load_journal(service)
    
    # Display last 3 entries
    if current_log:
        print(f"\nLast 3 Entries: {json.dumps(current_log[-3:], indent=2)}")

    note = input("\nEnter your note: ")
    if note:
        entry = {
            "id": str(uuid.uuid4())[:8],
            "type": "MANUAL_NOTE",
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "content": note
        }
        current_log.append(entry)
        save_journal(service, current_log, file_id)
        print("Note saved.")

if __name__ == "__main__":
    # If run directly, allow manual entry
    manual_entry()