from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import db


def tz():
    try:
        return ZoneInfo(db.get_setting("timezone", "America/New_York"))
    except Exception:
        return ZoneInfo("America/New_York")


def fmt(dt, with_tz=True):
    """Show a time in your timezone, e.g. 2026-11-06 03:15 PM EST."""
    if dt is None:
        return "-"
    return db.to_dt(dt).astimezone(tz()).strftime("%Y-%m-%d %I:%M %p" + (" %Z" if with_tz else ""))


def _day_start_hm():
    s = (db.get_setting("day_start", "00:00") or "00:00").strip()
    try:
        h, m = s.split(":")
        return int(h), int(m)
    except Exception:
        return 0, 0


def window_start(period, now):
    """When the current daily / monthly cap window began (UTC). Resets at the 'day_start' setting."""
    z = tz()
    h, m = _day_start_hm()
    local = now.astimezone(z)
    if period == "daily":
        boundary = local.replace(hour=h, minute=m, second=0, microsecond=0)
        if local < boundary:
            boundary -= timedelta(days=1)
        return boundary.astimezone(ZoneInfo("UTC")), boundary.date().isoformat()
    first = local.replace(day=1, hour=h, minute=m, second=0, microsecond=0)
    if local < first:
        prev = (first - timedelta(days=1)).replace(day=1)
        first = prev
    return first.astimezone(ZoneInfo("UTC")), f"{first.year}-{first.month:02d}"
