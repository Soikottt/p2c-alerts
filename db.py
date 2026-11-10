"""Small database helper. Every other file talks to Postgres only through this one."""
import time
from datetime import datetime, timezone

from dateutil.parser import isoparse
from sqlalchemy import bindparam, create_engine, text

import config

_engine = None

SETTING_DEFAULTS = {
    "timezone": "America/New_York",
    "day_start": "00:00",          # daily caps reset here (00:00 = day ends 11:59 PM)
    "live_lookback_hours": "6",    # each 5-minute run re-reads this many hours
    "subject_prefix": "[P2C Alert]",
    "dry_run": "true",             # true = print emails instead of sending
    "admin_emails": "",
    "from_name": "",               # shown as the sender name, e.g. "Acme Call Alerts"
    "from_email": "",              # empty = use the SMTP login address
    "reply_to": "",                # where replies go (e.g. your team inbox)
    "app_url": "",                 # your Streamlit app link, used for the answer buttons in emails
    "numbers_sync_minutes": "60",  # how often the DID list is refreshed from Ringba
    "numbers_auto_retire": "true", # DIDs that vanish from Ringba's list are marked retired
    "numbers_next_run": "",        # (internal) when the next DID sync is due
    "numbers_last_ok": "",         # (internal) last successful DID sync
    "numbers_last_error": "",      # (internal) last DID sync problem
}


def get_engine():
    global _engine
    if _engine is None:
        if not config.DB_URL:
            raise RuntimeError("DATABASE_URL is missing. Add it to your Secrets (or .env file).")
        _engine = create_engine(config.DB_URL, pool_pre_ping=True, pool_recycle=300)
    return _engine


def set_engine(engine):  # used by tests
    global _engine
    _engine = engine
    clear_settings_cache()


def q(sql, **params):
    """Run a SELECT (or INSERT ... RETURNING) and return a list of dicts."""
    with get_engine().begin() as c:
        return [dict(r) for r in c.execute(text(sql), params).mappings().all()]


def q1(sql, **params):
    rows = q(sql, **params)
    return rows[0] if rows else None


def x(sql, **params):
    """Run a statement that changes data. Returns number of rows touched."""
    with get_engine().begin() as c:
        return c.execute(text(sql), params).rowcount


def q_in(sql, list_name, values, **params):
    """SELECT with an IN :list_name clause."""
    stmt = text(sql).bindparams(bindparam(list_name, expanding=True))
    with get_engine().begin() as c:
        return [dict(r) for r in c.execute(stmt, {list_name: list(values), **params}).mappings().all()]


def x_in(sql, list_name, values, **params):
    stmt = text(sql).bindparams(bindparam(list_name, expanding=True))
    with get_engine().begin() as c:
        return c.execute(stmt, {list_name: list(values), **params}).rowcount


def bulk_upsert(table, columns, rows, conflict_col, update_cols, chunk=300):
    """Insert many rows in few round-trips; on duplicate key, update `update_cols`."""
    if not rows:
        return
    sets = ", ".join(f"{c}=EXCLUDED.{c}" for c in update_cols)
    for i in range(0, len(rows), chunk):
        part = rows[i:i + chunk]
        params, values = {}, []
        for n, row in enumerate(part):
            ph = []
            for c in columns:
                k = f"{c}_{n}"
                params[k] = row.get(c)
                ph.append(f":{k}")
            values.append("(" + ",".join(ph) + ")")
        sql = (f"INSERT INTO {table} ({','.join(columns)}) VALUES {','.join(values)} "
               f"ON CONFLICT ({conflict_col}) DO UPDATE SET {sets}")
        x(sql, **params)


def now_utc():
    return datetime.now(timezone.utc)


def to_dt(v):
    """Turn whatever the database gives back into a timezone-aware UTC datetime."""
    if v is None:
        return None
    if isinstance(v, str):
        v = isoparse(v)
    if v.tzinfo is None:
        v = v.replace(tzinfo=timezone.utc)
    return v.astimezone(timezone.utc)


_settings_cache = {"t": 0.0, "data": {}}
SETTINGS_TTL = 15.0   # seconds. Settings are read all the time; this keeps pages fast.


def _all_settings():
    now = time.monotonic()
    if now - _settings_cache["t"] > SETTINGS_TTL or not _settings_cache["data"]:
        _settings_cache["data"] = {r["key"]: r["value"] for r in q("SELECT key, value FROM settings")}
        _settings_cache["t"] = now
    return _settings_cache["data"]


def clear_settings_cache():
    _settings_cache["t"] = 0.0
    _settings_cache["data"] = {}


def get_setting(key, default=None):
    v = _all_settings().get(key)
    if v is not None:
        return v
    return default if default is not None else SETTING_DEFAULTS.get(key)


def set_setting(key, value):
    x("INSERT INTO settings (key, value) VALUES (:k, :v) "
      "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", k=key, v=str(value))
    clear_settings_cache()
