import os
from dotenv import load_dotenv

# Load the .env file
load_dotenv()

DB_URL = os.getenv("DATABASE_URL")
RINGBA_TOKEN = os.getenv("RINGBA_API_TOKEN")
RINGBA_ACCT = os.getenv("RINGBA_ACCOUNT_ID")
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASSWORD")
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"