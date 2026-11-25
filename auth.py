"""Simple name + PIN login. PINs are stored as salted hashes, never as plain text.
Login is only demanded once at least one user exists (or ADMIN_PIN is set), so you can never lock yourself out:
the secret ADMIN_PIN always opens the user 'admin'."""
import hashlib
import hmac
import os

import config
import db

ROUNDS = 120_000


def _hash(pin, salt):
    return hashlib.pbkdf2_hmac("sha256", str(pin).encode(), bytes.fromhex(salt), ROUNDS).hex()


def valid_pin(pin):
    pin = str(pin or "").strip()
    return len(pin) >= 4


def create_user(name, pin, role="user"):
    name = (name or "").strip()
    if not name:
        raise ValueError("Write a name.")
    if not valid_pin(pin):
        raise ValueError("The PIN needs at least 4 characters.")
    if db.q1("SELECT 1 AS x FROM app_users WHERE LOWER(name)=LOWER(:n)", n=name):
        raise ValueError("That name already exists.")
    salt = os.urandom(16).hex()
    db.x("INSERT INTO app_users (name, pin_hash, salt, role, active, created_at) VALUES (:n,:h,:s,:r,TRUE,:now)",
         n=name, h=_hash(pin, salt), s=salt, r="admin" if role == "admin" else "user", now=db.now_utc())


def set_pin(name, pin):
    if not valid_pin(pin):
        raise ValueError("The PIN needs at least 4 characters.")
    salt = os.urandom(16).hex()
    return db.x("UPDATE app_users SET pin_hash=:h, salt=:s WHERE LOWER(name)=LOWER(:n)", h=_hash(pin, salt), s=salt, n=name)


def set_active(name, active):
    return db.x("UPDATE app_users SET active=:a WHERE LOWER(name)=LOWER(:n)", a=bool(active), n=name)


def delete_user(name):
    return db.x("DELETE FROM app_users WHERE LOWER(name)=LOWER(:n)", n=name)


def users():
    return db.q("SELECT id, name, role, active, created_at FROM app_users ORDER BY name")


def admin_pin():
    p = config.get_secret("ADMIN_PIN")
    return str(p).strip() if p else ""


def login_required():
    if admin_pin():
        return True
    def load():
        try:
            return bool(db.q1("SELECT 1 AS x FROM app_users WHERE active = TRUE"))
        except Exception:
            return False                  # table not created yet: never block the app
    return db.memo(("login_required",), load)


def forget_cache():
    try:
        db._memo.clear()
    except Exception:
        pass


def check(name, pin):
    """Returns the user's name (as stored) and role, or None."""
    name, pin = (name or "").strip(), str(pin or "")
    if not name or not pin:
        return None
    ap = admin_pin()
    if ap and name.lower() == "admin" and hmac.compare_digest(pin, ap):
        return {"name": "admin", "role": "admin"}
    u = db.q1("SELECT name, pin_hash, salt, role, active FROM app_users WHERE LOWER(name)=LOWER(:n)", n=name)
    if not u or not u["active"]:
        return None
    if hmac.compare_digest(_hash(pin, u["salt"]), u["pin_hash"]):
        return {"name": u["name"], "role": u["role"]}
    return None
