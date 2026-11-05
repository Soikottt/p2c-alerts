import os
from dotenv import load_dotenv

# Try to load local .env file (for running on your computer)
load_dotenv()

# A helper to get secrets from either local .env or Streamlit Cloud
def get_secret(key, default=None):
    # 1. Try local environment variable first
    val = os.getenv(key)
    if val:
        return val
    # 2. Try Streamlit secrets (for the cloud)
    try:
        import streamlit as st
        return st.secrets.get(key, default)
    except Exception:
        return default

DB_URL = get_secret("DATABASE_URL")
RINGBA_TOKEN = get_secret("RINGBA_API_TOKEN")
RINGBA_ACCT = get_secret("RINGBA_ACCOUNT_ID")
SMTP_HOST = get_secret("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(get_secret("SMTP_PORT", 587))
SMTP_USER = get_secret("SMTP_USER")
SMTP_PASS = get_secret("SMTP_PASSWORD")
DRY_RUN = str(get_secret("DRY_RUN", "true")).lower() == "true"
