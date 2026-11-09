"""Small database helper. Every other file talks to Postgres only through this one."""
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


def get_setting(key, default=None):
    r = q1("SELECT value FROM settings WHERE key=:k", k=key)
    if r is not None and r["value"] is not None:
        return r["value"]
    return default if default is not None else SETTING_DEFAULTS.get(key)


def set_setting(key, value):
    x("INSERT INTO settings (key, value) VALUES (:k, :v) "
      "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", k=key, v=str(value))
