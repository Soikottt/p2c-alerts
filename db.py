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
    "read_replies": "true",        # read email replies from affiliate managers (needs IMAP on the mailbox)
    "imap_host": "",               # empty = guessed from SMTP_HOST (smtp.gmail.com -> imap.gmail.com)
    "inbox_last_ok": "",           # (internal)
    "inbox_last_error": "",        # (internal)
    "inbox_next_run": "",          # (internal) after a failure, wait 30 minutes
    "did_reminder_days": "3",      # nobody answered an inactive-DID email: remind again after N days (0 = never remind)
    "did_reminder_max": "3",       # at most this many reminders per DID alert
    "targets_sync_minutes": "60",  # how often the buyer targets and their caps are re-read from Ringba (usage is read every run)
    "targets_next_run": "",        # (internal)
    "targets_last_ok": "",         # (internal)
    "targets_last_error": "",      # (internal)
    "refresh_days": "35",          # past days that are re-read from Ringba (one 24 h window per run) so late conversions / payouts are picked up; 0 = off
    "refresh_cursor": "0",         # (internal) which past day is next
    "backup_email_days": "7",      # email a backup (CSV zip) to the admin every N days. 0 = never
    "backup_next_run": "",         # (internal)
}


PRE_PING = True          # the app keeps this on; the short-lived engine turns it off (saves one round trip per query, the retry in _run covers dead connections)
MEMO_TTL = 0             # seconds; the engine sets it so rules / templates are not re-read for every single alert (0 = off)
STATS = {"queries": 0, "seconds": 0.0}
_memo = {}
STATEMENT_TIMEOUT_MS = 30000     # no single query may run longer than 30 s
_IS_PG = False


def get_engine():
    global _engine, _IS_PG
    if _engine is None:
        if not config.DB_URL:
            raise RuntimeError("DATABASE_URL is missing. Add it to your Secrets (or .env file).")
        kwargs = {"pool_pre_ping": PRE_PING, "pool_recycle": 240}
        _IS_PG = str(config.DB_URL).startswith("postgresql")
        if _IS_PG:
            # Neon puts idle databases to sleep and silently drops idle connections. A dead connection then looks alive
            # and the "ping" before every query waits for ever. These settings make a dead connection fail in ~15 s.
            kwargs["connect_args"] = {
                "connect_timeout": 15,          # opening a connection
                "keepalives": 1, "keepalives_idle": 20, "keepalives_interval": 5, "keepalives_count": 3,
                "tcp_user_timeout": 15000,      # data not acknowledged for 15 s -> the connection is declared dead
            }
        _engine = create_engine(config.DB_URL, **kwargs)
    return _engine


def set_engine(engine):  # used by tests
    global _engine
    _engine = engine
    clear_settings_cache()


_DEAD_CONNECTION_HINTS = ("server closed the connection", "ssl syscall", "connection already closed", "terminating connection",
                          "timeout expired", "could not connect", "connection timed out", "eof detected", "connection reset",
                          "connection refused", "could not receive data", "ssl connection has been closed")


def _is_connection_problem(e):
    """True only for 'the connection died / could not be opened' errors (worth another try).
    Real SQL mistakes, duplicate columns, slow queries (statement timeout) are NOT retried."""
    if getattr(e, "connection_invalidated", False):
        return True
    msg = str(e).lower()
    return any(h in msg for h in _DEAD_CONNECTION_HINTS)


def memo(key, fn):
    """Remember fn()'s answer for MEMO_TTL seconds (only when the engine switched it on)."""
    if not MEMO_TTL:
        return fn()
    hit = _memo.get(key)
    now = time.monotonic()
    if hit and now - hit[0] < MEMO_TTL:
        return hit[1]
    v = fn()
    _memo[key] = (now, v)
    return v


def stats_text():
    n, sec = STATS["queries"], STATS["seconds"]
    return f"database: {n} queries, {sec:.1f}s spent waiting for it" + (f" (average {sec / n * 1000:.0f} ms each)" if n else "")


def _run(fn):
    """Run fn(connection) in one transaction. If the connection turns out to be dead, throw the pool away and retry
    (twice) instead of hanging or failing the whole 5-minute run on one bad connection."""
    last = None
    for attempt in range(3):
        t_q = time.monotonic()
        STATS["queries"] += 1
        try:
            eng = get_engine()
            with eng.begin() as c:
                if _IS_PG:
                    c.exec_driver_sql(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}")   # works through Neon's pooler too
                out = fn(c)
                STATS["seconds"] += time.monotonic() - t_q
                return out
        except Exception as e:
            STATS["seconds"] += time.monotonic() - t_q
            if attempt < 2 and _is_connection_problem(e):
                last = e
                try:
                    get_engine().dispose()
                except Exception:
                    pass
                time.sleep(1 + attempt)
                continue
            raise
    raise last


def q(sql, **params):
    """Run a SELECT (or INSERT ... RETURNING) and return a list of dicts."""
    return _run(lambda c: [dict(r) for r in c.execute(text(sql), params).mappings().all()])


def q1(sql, **params):
    rows = q(sql, **params)
    return rows[0] if rows else None


def x(sql, **params):
    """Run a statement that changes data. Returns number of rows touched."""
    return _run(lambda c: c.execute(text(sql), params).rowcount)


def q_in(sql, list_name, values, **params):
    """SELECT with an IN :list_name clause."""
    stmt = text(sql).bindparams(bindparam(list_name, expanding=True))
    return _run(lambda c: [dict(r) for r in c.execute(stmt, {list_name: list(values), **params}).mappings().all()])


def x_in(sql, list_name, values, **params):
    stmt = text(sql).bindparams(bindparam(list_name, expanding=True))
    return _run(lambda c: c.execute(stmt, {list_name: list(values), **params}).rowcount)


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
