from sqlalchemy import create_engine, text
import config
import sys

engine = create_engine(config.DB_URL, pool_pre_ping=True)

def q(sql, **params):
    with engine.begin() as c:
        return c.execute(text(sql), params).mappings().all()

def x(sql, **params):
    with engine.begin() as c:
        return c.execute(text(sql), params)

# --- ADDED THESE TWO FUNCTIONS ---
def get_setting(key, default=None):
    r = q("SELECT value FROM settings WHERE key=:k", k=key)
    return r[0]["value"] if r else default

def set_setting(key, value):
    x("INSERT INTO settings (key,value) VALUES (:k, :v) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", k=key, v=str(value))
# ---------------------------------

if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "set_email":
        email = sys.argv[2]
        set_setting("admin_email", email)
        print(f"✅ Admin email set to {email}")
    else:
        print("Usage: python db.py set_email your@email.com")