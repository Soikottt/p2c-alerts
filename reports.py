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


# ---------------------------------------------------------------- compare with a Ringba report
SUMMARY_METRICS = {"Incoming": "calls", "Connected": "connected", "Paid": "paid", "Converted": "converted",
                   "Duplicate": "duplicates", "Blocked": "blocked"}


def _key(name):
    return " ".join(str(name).lower().split())


def parse_ringba_summary(text):
    """A Ringba 'by publisher' report (CSV). Returns (rows_df, totals_dict). The unnamed total row is split off."""
    import io
    df = pd.read_csv(io.StringIO(text.lstrip("\ufeff")))
    if "Publisher" not in df.columns or "Incoming" not in df.columns:
        raise ValueError("This does not look like a Ringba report by publisher. I need the columns 'Publisher' and 'Incoming'.")
    for col in SUMMARY_METRICS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col].astype(str).str.replace(",", ""), errors="coerce").fillna(0).astype(int)
    pub = df["Publisher"].astype(str).str.strip()
    is_total = df["Publisher"].isna() | pub.eq("") | pub.str.lower().isin(["nan", "total", "totals", "grand total"])
    totals = {SUMMARY_METRICS[c]: int(df.loc[is_total, c].sum()) for c in SUMMARY_METRICS if c in df.columns}
    return df.loc[~is_total].reset_index(drop=True), totals


def reconcile(summary_df, tool_df):
    """Publisher by publisher: what Ringba says vs what this tool stored. Diff = tool minus Ringba."""
    metrics = [(c, v) for c, v in SUMMARY_METRICS.items() if c in summary_df.columns]
    ring = {}
    for _, r in summary_df.iterrows():
        ring[_key(r["Publisher"])] = {"name": str(r["Publisher"]).strip(), **{v: int(r[c]) for c, v in metrics}}
    tool = {}
    if tool_df is not None and not tool_df.empty:
        t = tool_df.copy()
        t["publisher"] = t["publisher"].fillna("(none)")
        for name, g in t.groupby("publisher"):
            tool[_key(name)] = {"name": str(name), "calls": len(g), "connected": int(g["connected"].sum()),
                                "paid": int(g["paid"].sum()), "converted": int(g["converted"].sum()),
                                "duplicates": int(g["is_duplicate"].sum()), "blocked": int(g["blocked"].sum())}
    rows = []
    for k in sorted(set(ring) | set(tool), key=lambda k: -(ring.get(k, {}).get("calls", 0))):
        a, b = ring.get(k, {}), tool.get(k, {})
        row = {"Publisher": a.get("name") or b.get("name")}
        bad = False
        for _, v in metrics:
            ra, tb = int(a.get(v, 0)), int(b.get(v, 0))
            label = {"calls": "Calls", "connected": "Connected", "paid": "Paid", "converted": "Converted",
                     "duplicates": "Duplicates", "blocked": "Blocked"}[v]
            row[f"Ringba {label}"], row[f"Tool {label}"], row[f"Diff {label}"] = ra, tb, tb - ra
            bad = bad or (tb != ra)
        row["Result"] = "✅ match" if not bad else ("⚠️ missing in tool" if k not in tool else ("⚠️ not in Ringba report" if k not in ring else "⚠️ differs"))
        rows.append(row)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- find days that look incomplete
def day_counts(first_day, last_day):
    """{local date: number of stored calls} for every day in the range (days without calls count as 0)."""
    s, e = utc_bounds(first_day, last_day)
    rows = db.q("SELECT call_at FROM calls WHERE call_at >= :s AND call_at < :e", s=s, e=e)
    out = {first_day + timedelta(days=i): 0 for i in range((last_day - first_day).days + 1)}
    if rows:
        ts = pd.to_datetime([r["call_at"] for r in rows], utc=True, format="mixed").tz_convert(tz())
        for d, n in pd.Series(ts.date).value_counts().items():
            out[d] = int(n)
    return out


def suspicious_days(counts, today=None):
    """Days that look cut short (for example the old 20-calls-per-request limit): zero, <= 25 calls,
    or under a quarter of the typical day. Today is never flagged because it is still going."""
    today = today or today_local()
    past = {d: n for d, n in counts.items() if d < today}
    vals = sorted(n for n in past.values() if n > 0)
    med = vals[len(vals) // 2] if vals else 0
    return sorted(d for d, n in past.items() if n == 0 or n <= 25 or (med >= 100 and n < 0.25 * med))
