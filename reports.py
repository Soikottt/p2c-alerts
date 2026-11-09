"""Date presets + call report tables. Reads from the database."""
from datetime import date, datetime, time, timedelta, timezone

import pandas as pd
from dateutil.relativedelta import relativedelta

import db
from timeutil import tz

PRESETS = ["Today", "Yesterday", "Last 7 days", "This week", "Last week", "Last 30 days", "Last month",
           "Last 3 months", "Last 6 months", "This year", "Last year", "Custom range"]

PRESET_HELP = {
    "Today": "From midnight until now",
    "Yesterday": "The full previous day",
    "Last 7 days": "Today and the 6 days before it",
    "This week": "Monday until today",
    "Last week": "Monday to Sunday of the previous week",
    "Last 30 days": "Today and the 29 days before it",
    "Last month": "The full previous calendar month",
    "Last 3 months": "Same day 3 months ago until today",
    "Last 6 months": "Same day 6 months ago until today",
    "This year": "1 January until today",
    "Last year": "The full previous calendar year",
}


def today_local():
    return datetime.now(tz()).date()


def preset_range(name, today=None):
    """Returns (first_day, last_day) inclusive, in your timezone."""
    t = today or today_local()
    monday = t - timedelta(days=t.weekday())
    if name == "Today":
        return t, t
    if name == "Yesterday":
        return t - timedelta(days=1), t - timedelta(days=1)
    if name == "Last 7 days":
        return t - timedelta(days=6), t
    if name == "This week":
        return monday, t
    if name == "Last week":
        return monday - timedelta(days=7), monday - timedelta(days=1)
    if name == "Last 30 days":
        return t - timedelta(days=29), t
    if name == "Last month":
        first_this = t.replace(day=1)
        return first_this - relativedelta(months=1), first_this - timedelta(days=1)
    if name == "Last 3 months":
        return t - relativedelta(months=3), t
    if name == "Last 6 months":
        return t - relativedelta(months=6), t
    if name == "This year":
        return date(t.year, 1, 1), t
    if name == "Last year":
        return date(t.year - 1, 1, 1), date(t.year - 1, 12, 31)
    raise ValueError(name)


def utc_bounds(first_day, last_day):
    """[start, end) in UTC for whole local days."""
    z = tz()
    start = datetime.combine(first_day, time.min, tzinfo=z).astimezone(timezone.utc)
    end = datetime.combine(last_day + timedelta(days=1), time.min, tzinfo=z).astimezone(timezone.utc)
    return start, end


def coverage():
    r = db.q1("SELECT MIN(call_at) AS first, MAX(call_at) AS last, COUNT(*) AS n FROM calls")
    return (db.to_dt(r["first"]), db.to_dt(r["last"]), r["n"]) if r and r["n"] else (None, None, 0)


BOOL_COLS = ["connected", "converted", "paid", "is_duplicate", "blocked"]
NUM_COLS = ["revenue", "payout", "profit", "duration_sec", "connected_sec"]


def load_calls(start_utc, end_utc, publisher_id=None, buyer_id=None):
    sql = """SELECT c.call_id, c.call_at, p.name AS publisher, b.name AS buyer, d.number AS did,
                    c.campaign_name AS campaign, c.target_name AS target, c.caller_number AS caller,
                    c.connected, c.converted, c.paid, c.is_duplicate, c.blocked,
                    c.duration_sec, c.connected_sec, c.revenue, c.payout, c.profit
             FROM calls c
             LEFT JOIN publishers p ON p.id = c.publisher_id
             LEFT JOIN buyers b ON b.id = c.buyer_id
             LEFT JOIN dids d ON d.id = c.did_id
             WHERE c.call_at >= :s AND c.call_at < :e"""
    params = {"s": start_utc, "e": end_utc}
    if publisher_id:
        sql += " AND c.publisher_id = :p"
        params["p"] = publisher_id
    if buyer_id:
        sql += " AND c.buyer_id = :b"
        params["b"] = buyer_id
    rows = db.q(sql + " ORDER BY c.call_at", **params)
    df = pd.DataFrame(rows, columns=["call_id", "call_at", "publisher", "buyer", "did", "campaign", "target",
                                     "caller", "connected", "converted", "paid", "is_duplicate", "blocked",
                                     "duration_sec", "connected_sec", "revenue", "payout", "profit"])
    if df.empty:
        return df
    df["call_at"] = pd.to_datetime(df["call_at"], utc=True, format="mixed").dt.tz_convert(tz())
    for c in BOOL_COLS:
        df[c] = df[c].fillna(False).astype(bool)
    for c in NUM_COLS:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    df["day"] = df["call_at"].dt.date
    return df


def summary(df):
    n = len(df)
    conn = int(df["connected"].sum()) if n else 0
    conv = int(df["converted"].sum()) if n else 0
    paid = int(df["paid"].sum()) if n else 0
    return {
        "Calls": n, "Connected": conn, "Converted": conv, "Paid": paid,
        "Blocked": int(df["blocked"].sum()) if n else 0,
        "Duplicates": int(df["is_duplicate"].sum()) if n else 0,
        "Connect %": round(conn / n * 100, 1) if n else 0.0,
        "Revenue": round(float(df["revenue"].sum()), 2) if n else 0.0,
        "Payout": round(float(df["payout"].sum()), 2) if n else 0.0,
        "Profit": round(float(df["profit"].sum()), 2) if n else 0.0,
        "Avg talk (sec)": int(df.loc[df["connected_sec"] > 0, "connected_sec"].mean()) if n and (df["connected_sec"] > 0).any() else 0,
    }


def breakdown(df, by):
    if df.empty:
        return pd.DataFrame()
    d = df.copy()
    d[by] = d[by].fillna("(none)").astype(str)
    g = d.groupby(by).agg(Calls=("call_id", "count"), Connected=("connected", "sum"), Converted=("converted", "sum"),
                          Paid=("paid", "sum"), Blocked=("blocked", "sum"), Revenue=("revenue", "sum"),
                          Payout=("payout", "sum"), Profit=("profit", "sum")).reset_index()
    g["Connect %"] = (g["Connected"] / g["Calls"] * 100).round(1)
    for c in ("Revenue", "Payout", "Profit"):
        g[c] = g[c].round(2)
    sort_col = by if by == "day" else "Calls"
    return g.sort_values(sort_col, ascending=(by == "day")).reset_index(drop=True)
