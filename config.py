"""Reads your secrets from (1) environment / .env file, or (2) Streamlit Cloud secrets."""
import os
from dotenv import load_dotenv

load_dotenv()


def get_secret(key, default=None):
    val = os.getenv(key)
    if val not in (None, ""):
        return val
    try:
        import streamlit as st
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return default


def _fix_db_url(url):
    """Neon gives 'postgresql://...'; SQLAlchemy wants the driver name in it."""
    if not url:
        return url
    url = str(url).strip().strip('"').strip("'")
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg2://" + url[len("postgresql://"):]
    return url


DB_URL = _fix_db_url(get_secret("DATABASE_URL"))
RINGBA_TOKEN = get_secret("RINGBA_API_TOKEN")
RINGBA_ACCOUNT_ID = get_secret("RINGBA_ACCOUNT_ID")
SMTP_HOST = get_secret("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(get_secret("SMTP_PORT", 587))
SMTP_USER = get_secret("SMTP_USER")
SMTP_PASSWORD = get_secret("SMTP_PASSWORD")

# Extra safety switch. The real on/off switch lives inside the app (Settings tab).
# If DRY_RUN=true here, this machine NEVER sends real email, whatever the app says.
DRY_RUN_ENV = str(get_secret("DRY_RUN", "false")).strip().lower() == "true"
