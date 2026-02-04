import os
import json
import base64
from datetime import datetime
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
import io

# Security imports
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

# --- CONFIGURATION ---
SCOPES = ['https://www.googleapis.com/auth/drive.appdata']
TOKEN_FILE = 'credentials.json'
SECRETS_FILE = 'secrets.json'
JOURNAL_FILENAME = 'secure_trading_journal.enc' # .enc indicates encrypted

def authenticate_google():
    """Handles Google OAuth authentication."""
    creds = None
    if os.path.exists(TOKEN_FILE):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        except Exception:
            print("Token expired or invalid. Re-authenticating...")

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(SECRETS_FILE):
                print(f"Error: {SECRETS_FILE} not found. Please download OAuth client secrets.")
                return None
            flow = InstalledAppFlow.from_client_secrets_file(SECRETS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        
        with open(TOKEN_FILE, 'w') as token:
            token.write(creds.to_json())
            
    return build('drive', 'v3', credentials=creds)

def get_encryption_key(password, salt=None):
    """
    Derives a secure URL-safe base64-encoded key from the user's password.
    If salt is None, generates a new one (for new journals).
    Returns (fernet_key, salt).
    """
    if salt is None:
        salt = os.urandom(16) # Generate new salt for fresh setup
    
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=480000,
    )
    key = base64.urlsafe_b64encode(kdf.derive(password.encode()))
    return key, salt

def main():
    print("--- Secure Cloud Trading Journal ---")
    service = authenticate_google()
    if not service:
        return

    # 1. Check for existing journal in AppData folder
    results = service.files().list(
        q=f"name='{JOURNAL_FILENAME}' and 'appDataFolder' in parents",
        spaces='appDataFolder',
        fields="files(id, name)"
    ).execute()
    
    files = results.get('files', [])
    
    current_data = ""
    file_id = None
    salt = None

    if not files:
        # --- NEW JOURNAL SETUP ---
        print("\nNo existing journal found.")
        print("Initializing secure setup...")
        
        while True:
            pwd = input("Create a password for this journal: ")
            pwd_confirm = input("Confirm password: ")
            if pwd == pwd_confirm and len(pwd) > 0:
                break
            print("Passwords did not match or were empty. Try again.")
        
        # Generate key and new salt
        key, salt = get_encryption_key(pwd)
        fernet = Fernet(key)
        print("Password set. Encryption enabled.")
        
    else:
        # --- EXISTING JOURNAL LOGIN ---
        file_id = files[0]['id']
        print("\nExisting encrypted journal found.")
        
        # Download the file to memory
        request = service.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while done is False:
            status, done = downloader.next_chunk()
            
        file_content = fh.getvalue()
        
        # Extract Salt (first 16 bytes) and Encrypted Data
        salt = file_content[:16]
        encrypted_data = file_content[16:]
        
        # Authenticate
        while True:
            pwd = input("Enter your journal password: ")
            try:
                key, _ = get_encryption_key(pwd, salt)
                fernet = Fernet(key)
                decrypted_bytes = fernet.decrypt(encrypted_data)
                current_data = decrypted_bytes.decode('utf-8')
                print("Access Granted. Decryption successful.")
                break
            except Exception:
                print("Incorrect password. Access Denied. Try again.")

    # 2. Display previous entries (Optional - currently shows last 500 chars)
    if current_data:
        print("\n--- Last Entry Snippet ---")
        print(current_data[-500:] + "..." if len(current_data) > 500 else current_data)
        print("--------------------------")

    # 3. Get User Input
    new_entry = input("\nEnter your new journal entry: ")
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    formatted_entry = f"\n[{timestamp}] {new_entry}"
    
    updated_content = current_data + formatted_entry
    
    # 4. Encrypt Everything
    print("Encrypting data...")
    encrypted_blob = fernet.encrypt(updated_content.encode('utf-8'))
    
    # Combine Salt + Encrypted Data (we need the salt to unlock it next time)
    final_payload = salt + encrypted_blob
    
    # 5. Upload/Overwrite to Cloud
    media = MediaIoBaseUpload(io.BytesIO(final_payload), mimetype='application/octet-stream', resumable=True)
    
    if file_id:
        service.files().update(fileId=file_id, media_body=media).execute()
        print("Journal updated securely.")
    else:
        file_metadata = {
            'name': JOURNAL_FILENAME,
            'parents': ['appDataFolder']
        }
        service.files().create(body=file_metadata, media_body=media, fields='id').execute()
        print("New secure journal created.")

    print("Success! Memory flushed.")

if __name__ == "__main__":
    main()