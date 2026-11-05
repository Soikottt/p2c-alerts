import os
from dotenv import load_dotenv

load_dotenv()

def get_secret(key, default=None):
    # 1. Try local environment variable
    val = os.getenv(key)
    if val:
        return val
    # 2. Try Streamlit secrets
    try:
        import streamlit as st
        if hasattr(st, "secrets") and key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return default

DB_URL = get_secret("DATABASE_URL")
RINGBA_TOKEN = get_secret("RINGBA_API_TOKEN")
RINGBA_ACCT = get_secret("RINGBA_ACCOUNT_ID")
SMTP_HOST = get_secret("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(get_secret("SMTP_PORT", 587))
SMTP_USER = get_secret("SMTP_USER")
SMTP_PASS = get_secret("SMTP_PASSWORD")
DRY_RUN = str(get_secret("DRY_RUN", "true")).lower() == "true"

# Show a clear error if the DB_URL is missing
if not DB_URL:
    raise ValueError("❌ DATABASE_URL is missing! Please check your Streamlit Secrets or .env file.")
