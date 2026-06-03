"""
gmail_auth.py — One-time Gmail OAuth2 setup.

Run this ONCE from your local machine (not the server) after downloading
your credentials JSON from Google Cloud Console:

  1. Go to: https://console.cloud.google.com/
  2. Create a project → Enable Gmail API
  3. Create OAuth 2.0 credentials (Desktop app type)
  4. Download the JSON → save as gmail_credentials.json
  5. Run: python bot/gmail_auth.py

This opens a browser to approve access, then saves gmail_token.json
next to your credentials file. Upload both files to the server:

  scp gmail_credentials.json gmail_token.json trade-server:/root/

Then set in config.py:
  GMAIL_CREDENTIALS_FILE = "/root/gmail_credentials.json"
"""

from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

SCOPES       = ["https://www.googleapis.com/auth/gmail.send"]
CREDS_FILE   = Path("gmail_credentials.json")
TOKEN_FILE   = Path("gmail_token.json")

if not CREDS_FILE.exists():
    raise FileNotFoundError(
        f"Credentials file not found: {CREDS_FILE}\n"
        "Download from Google Cloud Console → APIs & Services → Credentials"
    )

creds = None
if TOKEN_FILE.exists():
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

if not creds or not creds.valid:
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_FILE), SCOPES)
        creds = flow.run_local_server(port=0)
    TOKEN_FILE.write_text(creds.to_json())

print(f"✓ Gmail authenticated successfully. Token saved to {TOKEN_FILE}")
print(f"  Upload both files to the server:")
print(f"    scp {CREDS_FILE} {TOKEN_FILE} trade-server:/root/")
print(f"  Then set in config.py:")
print(f'    GMAIL_CREDENTIALS_FILE = "/root/gmail_credentials.json"')
