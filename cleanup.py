import os
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# Configuration
SCOPES = ['https://www.googleapis.com/auth/drive.appdata']
OLD_FILENAME = 'secure_journal.txt' # The unencrypted file we want to remove

def authenticate():
    creds = None
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    return build('drive', 'v3', credentials=creds)

def delete_old_journal():
    service = authenticate()
    print(f"Searching for old file: '{OLD_FILENAME}'...")

    # Find the file
    query = f"name = '{OLD_FILENAME}' and 'appDataFolder' in parents and trashed = false"
    results = service.files().list(q=query, spaces='appDataFolder', fields="files(id, name)").execute()
    items = results.get('files', [])

    if not items:
        print("No unencrypted journal file found. You are clean!")
        return

    # Delete the file(s) - loop in case you ran the old script multiple times
    for item in items:
        print(f"Found file ID: {item['id']}. Deleting...")
        service.files().delete(fileId=item['id']).execute()
        print("Deleted.")

    print("\nCleanup complete! Your cloud folder now only contains the encrypted journal.")

if __name__ == '__main__':
    delete_old_journal()