"""Official numbers from the Ringba insights report (POST /v2/{accountId}/insights).

One request per DAY (publisher + campaign + buyer), cut at midnight of the account timezone (Settings -> timezone, default
America/New_York). Saved in `daily_stats`. Today is refreshed on every 5-minute run, yesterday once an hour, and missing days of
the last 35 are filled in (a few per run). Cap alerts and the report numbers use these figures, because they match Ringba exactly."""
from datetime import date, datetime, time, timedelta, timezone

import db
import ringba_client as rc
from timeutil import tz

FRESH_MINUTES = 20          # today's numbers older than this are not trusted for cap alerts (then the stored calls are used)
KEEP_DAYS = 35
METRIC_COL = {"all": "calls", "connected": "connected", "converted": "converted", "paid": "paid"}
SUM_COLS = ["calls", "completed", "connected", "paid", "converted", "not_connected", "duplicates", "blocked", "incomplete",
            "revenue", "payout", "profit", "cost", "seconds"]


def day_bounds(day):
    """(start, end) UTC of one local day; end is the last second of the day (as Ringba's example)."""
    z = tz()
    start = datetime.combine(day, time.min, tzinfo=z).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=z).astimezone(timezone.utc) - timedelta(seconds=1)
    return start, end


def today(now=None):
    return (now or db.now_utc()).astimezone(tz()).date()


def sync_day(day, now=None):
    """Download one day and replace what is stored for it. Returns the number of rows."""
    now = now or db.now_utc()
    s, e = day_bounds(day)
    rows = rc.fetch_insights(s, e, tz().key if hasattr(tz(), "key") else "America/New_York")
    db.x("DELETE FROM daily_stats WHERE day=:d", d=day)
    merged = {}
    for r in rows:                                   # same names twice (rare) are added together
        k = (r["publisher"], r["campaign"], r["buyer"])
        m = merged.setdefault(k, {c: 0 for c in SUM_COLS})
        for c in SUM_COLS:
            m[c] += r[c]
    for (pub, camp, buy), m in merged.items():
        db.x("INSERT INTO daily_stats (day, publisher, campaign, buyer, " + ", ".join(SUM_COLS) + ") VALUES (:day,:p,:c,:b,"
             + ",".join(":" + c for c in SUM_COLS) + ")", day=day, p=pub, c=camp, b=buy, **m)
    db.x("DELETE FROM insight_days WHERE day=:d", d=day)
    db.x("INSERT INTO insight_days (day, synced_at, row_count) VALUES (:d,:n,:r)", d=day, n=now, r=len(merged))
    db._memo.clear()
    return len(merged)


def run(now=None, max_days=4):
    """Called by the engine. Returns {'synced': [days], 'failed': [..]} ."""
    now = now or db.now_utc()
    t = today(now)
    have = {_d(r["day"]): db.to_dt(r["synced_at"]) for r in db.q("SELECT day, synced_at FROM insight_days")}
    todo = [t]
    y = t - timedelta(days=1)
    if y not in have or (now - have[y]) > timedelta(hours=1):
        todo.append(y)
    for i in range(2, KEEP_DAYS):
        d = t - timedelta(days=i)
        if d not in have and len(todo) < max_days:
            todo.append(d)
    res = {"synced": [], "failed": []}
    for d in todo:
        try:
            sync_day(d, now)
            res["synced"].append(d.isoformat())
        except rc.RingbaError as e:
            res["failed"].append(f"{d}: {str(e)[:150]}")
            if "ran out of time" in str(e) or "refused" in str(e):
                break
        rc._nap(0.5)
    db.x("DELETE FROM daily_stats WHERE day < :d", d=t - timedelta(days=400))
    return res


def _d(v):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def covered(first_day, last_day, now=None):
    """True when every day in the range was synced, and the last one (if it is today) recently."""
    now = now or db.now_utc()
    rows = {_d(r["day"]): db.to_dt(r["synced_at"]) for r in db.q("SELECT day, synced_at FROM insight_days WHERE day >= :a AND day <= :b", a=first_day, b=last_day)}
    d = first_day
    while d <= last_day:
        if d not in rows:
            return False
        d += timedelta(days=1)
    if last_day >= today(now) and (now - rows[last_day]) > timedelta(minutes=FRESH_MINUTES):
        return False
    return True


def totals(first_day, last_day, publisher=None, campaign=None, buyer=None):
    """Sum of the numbers in a date range (names compared without case)."""
    sql = "SELECT " + ", ".join(f"COALESCE(SUM({c}),0) AS {c}" for c in SUM_COLS) + " FROM daily_stats WHERE day >= :a AND day <= :b"
    args = {"a": first_day, "b": last_day}
    for col, v in (("publisher", publisher), ("campaign", campaign), ("buyer", buyer)):
        if v:
            sql += f" AND LOWER(TRIM({col})) = :{col}"
            args[col] = str(v).strip().lower()
    return db.q1(sql, **args)


def usage(cap, name, period, now=None):
    """Cap usage from the official numbers, or None when they do not cover the window (then the stored calls are used)."""
    now = now or db.now_utc()
    if (db.get_setting("day_start", "00:00") or "00:00").strip() not in ("00:00", "0:00"):
        return None                                   # a custom day start does not line up with Ringba's days
    t = today(now)
    first = t if period == "daily" else t.replace(day=1)
    if not covered(first, t, now):
        return None
    camp = (cap.get("campaign_name") or "").strip() if cap["entity_type"] == "publisher" else ""
    col = METRIC_COL.get(cap["metric"], "paid")
    if cap["entity_type"] == "publisher":
        r = totals(first, t, publisher=name, campaign=camp or None)
    else:
        r = totals(first, t, buyer=name)
    return int(r[col] or 0)


def breakdown(first_day, last_day, by):
    """Table by 'publisher' | 'campaign' | 'buyer' | 'day'."""
    cols = ", ".join(f"COALESCE(SUM({c}),0) AS {c}" for c in SUM_COLS)
    return db.q(f"SELECT {by} AS name, {cols} FROM daily_stats WHERE day >= :a AND day <= :b GROUP BY {by} ORDER BY SUM(calls) DESC",
                a=first_day, b=last_day)


class Usage:
    """Cap usage for MANY caps at once. usage() asks the database 3 times for every single cap; this asks 4 times in total, whatever the
    number of caps (the Cap overview page was slow because of that). get() gives the same number as usage(), or None when the official
    numbers do not cover the window (then the caller counts the stored calls)."""
    def __init__(self, now=None):
        self.now = now or db.now_utc()
        self.ok = (db.get_setting("day_start", "00:00") or "00:00").strip() in ("00:00", "0:00")
        self.win = {}
        if not self.ok:
            return
        t = today(self.now)
        for period, first in (("daily", t), ("monthly", t.replace(day=1))):
            if not covered(first, t, self.now):
                continue
            rows = db.q("SELECT publisher, campaign, buyer, " + ", ".join(f"COALESCE(SUM({c}),0) AS {c}" for c in SUM_COLS)
                        + " FROM daily_stats WHERE day >= :a AND day <= :b GROUP BY publisher, campaign, buyer", a=first, b=t)
            self.win[period] = rows

    @staticmethod
    def _n(v):
        return str(v or "").strip().lower()

    def get(self, cap, name, period):
        rows = self.win.get(period)
        if rows is None:
            return None
        col = METRIC_COL.get(cap["metric"], "paid")
        nm = self._n(name)
        total = 0
        if cap["entity_type"] == "publisher":
            camp = (cap.get("campaign_name") or "").strip().lower()
            for r in rows:
                if self._n(r["publisher"]) == nm and (not camp or self._n(r["campaign"]) == camp):
                    total += int(r[col] or 0)
        else:
            for r in rows:
                if self._n(r["buyer"]) == nm:
                    total += int(r[col] or 0)
        return total
