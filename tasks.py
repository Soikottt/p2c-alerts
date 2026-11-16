"""My Tasks: a simple personal to-do list with a daily e-mail.
A task has a work date (when you must do it) and an alert date (when you want to be reminded; can be earlier).
Every change is written to task_log, so you can see when something was done or changed."""
import calendar
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import alerts
import db

REPEATS = ["none", "daily", "weekdays", "weekly", "monthly"]
REPEAT_LABEL = {"none": "Does not repeat", "daily": "Every day", "weekdays": "Every weekday (Mon-Fri)",
                "weekly": "Every week", "monthly": "Every month"}


def local_tz():
    try:
        return ZoneInfo(db.get_setting("tasks_timezone", "Asia/Dhaka") or "Asia/Dhaka")
    except Exception:
        return ZoneInfo("Asia/Dhaka")


def local_now(now=None):
    return (now or db.now_utc()).astimezone(local_tz())


def today(now=None):
    return local_now(now).date()


def pdate(v):
    """'2026-11-16' (or a date / datetime) -> date, or None."""
    if not v:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def iso(d):
    d = pdate(d)
    return d.isoformat() if d else None


def log(task_id, title, event, detail=""):
    db.x("INSERT INTO task_log (task_id, task_title, at, event, detail) VALUES (:i,:t,:n,:e,:d)",
         i=task_id, t=title, n=db.now_utc(), e=event, d=detail)


def add_task(title, notes="", work_date=None, alert_date=None, repeat="none", now=None):
    title = (title or "").strip()
    if not title:
        raise ValueError("Please write what the task is.")
    wd = pdate(work_date) or today(now)
    ad = pdate(alert_date) or wd
    if repeat not in REPEATS:
        repeat = "none"
    rows = db.q("INSERT INTO tasks (title, notes, work_date, alert_date, repeat, status, created_at, updated_at) "
                "VALUES (:t,:n,:w,:a,:r,'open',:now,:now) RETURNING id",
                t=title, n=(notes or "").strip(), w=iso(wd), a=iso(ad), r=repeat, now=db.now_utc())
    tid = rows[0]["id"]
    log(tid, title, "created", f"work {iso(wd)}, alert {iso(ad)}, {REPEAT_LABEL[repeat]}")
    return tid


FIELDS = ("title", "notes", "work_date", "alert_date", "repeat")


def update_task(task_id, **new):
    """Save changed fields only, and write what changed to the history. Returns the list of changed field names."""
    old = db.q1("SELECT * FROM tasks WHERE id=:i", i=task_id)
    if not old:
        return []
    changes, sets, params = [], [], {"i": task_id, "now": db.now_utc()}
    for f in FIELDS:
        if f not in new:
            continue
        v = new[f]
        if f in ("work_date", "alert_date"):
            v = iso(v)
        elif f == "repeat":
            v = v if v in REPEATS else "none"
        else:
            v = (v or "").strip()
            if f == "title" and not v:
                continue
        cur = old[f] or ("" if f in ("title", "notes") else None)
        if f in ("work_date", "alert_date"):
            cur = iso(cur)
        if (cur or None) != (v or None):
            changes.append(f"{f}: '{cur or ''}' -> '{v or ''}'")
            sets.append(f"{f}=:{f}")
            params[f] = v
    if sets:
        db.x(f"UPDATE tasks SET {', '.join(sets)}, updated_at=:now WHERE id=:i", **params)
        log(task_id, params.get("title") or old["title"], "changed", "; ".join(changes))
    return [c.split(":")[0] for c in changes]


def _add_months(d, n):
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def next_work_date(work, repeat, after):
    """The first repeat date that is later than `after`."""
    d, k = work, 0
    while d <= after and k < 800:
        k += 1
        if repeat == "daily":
            d = d + timedelta(days=1)
        elif repeat == "weekdays":
            d = d + timedelta(days=1)
            while d.weekday() >= 5:
                d = d + timedelta(days=1)
        elif repeat == "weekly":
            d = d + timedelta(days=7)
        elif repeat == "monthly":
            d = _add_months(work, k)
        else:
            break
    return d


def mark_done(task_id, now=None):
    """A normal task becomes 'done'. A repeating task stays open and moves to its next date."""
    t = db.q1("SELECT * FROM tasks WHERE id=:i", i=task_id)
    if not t or t["status"] == "done":
        return
    n = db.now_utc() if now is None else now
    if t["repeat"] in ("daily", "weekdays", "weekly", "monthly"):
        wd = pdate(t["work_date"]) or today(now)
        ad = pdate(t["alert_date"]) or wd
        offset = max((wd - ad).days, 0)
        nxt = next_work_date(wd, t["repeat"], max(today(now), wd))
        db.x("UPDATE tasks SET work_date=:w, alert_date=:a, updated_at=:n WHERE id=:i",
             w=iso(nxt), a=iso(nxt - timedelta(days=offset)), n=n, i=task_id)
        log(task_id, t["title"], "done", f"repeats: next work date {iso(nxt)}")
    else:
        db.x("UPDATE tasks SET status='done', done_at=:n, updated_at=:n WHERE id=:i", n=n, i=task_id)
        log(task_id, t["title"], "done", "")


def reopen(task_id):
    t = db.q1("SELECT * FROM tasks WHERE id=:i", i=task_id)
    if t and t["status"] == "done":
        db.x("UPDATE tasks SET status='open', done_at=NULL, updated_at=:n WHERE id=:i", n=db.now_utc(), i=task_id)
        log(task_id, t["title"], "reopened", "")


def delete_task(task_id):
    t = db.q1("SELECT title FROM tasks WHERE id=:i", i=task_id)
    if t:
        db.x("DELETE FROM tasks WHERE id=:i", i=task_id)
        log(task_id, t["title"], "deleted", "")


def open_tasks():
    return db.q("SELECT * FROM tasks WHERE status='open' ORDER BY work_date, id")


def status_of(t, td):
    wd, ad = pdate(t["work_date"]), pdate(t["alert_date"])
    if wd and wd < td:
        return "Overdue"
    if wd and wd == td:
        return "Today"
    if ad and ad <= td:
        return "Reminder"
    return "Later"


def _line(t, td):
    wd = pdate(t["work_date"])
    s = f"- {t['title']}"
    if wd and wd < td:
        s += f" (was due {wd:%a %d %b})"
    elif wd and wd > td:
        s += f" (work on {wd:%a %d %b})"
    if (t["notes"] or "").strip():
        s += "\n  Note: " + " ".join(t["notes"].split())[:300]
    return s


def digest_text(td, tasks=None):
    """Returns (text, count). Only open tasks whose alert date or work date has come."""
    tasks = open_tasks() if tasks is None else tasks
    groups = {"Overdue": [], "Today": [], "Reminder": []}
    for t in tasks:
        st = status_of(t, td)
        if st in groups:
            groups[st].append(t)
    parts = []
    for key, head in (("Overdue", "OVERDUE"), ("Today", "DUE TODAY"), ("Reminder", "REMINDERS (work is later)")):
        if groups[key]:
            parts.append(head + "\n" + "\n".join(_line(t, td) for t in groups[key]))
    return "\n\n".join(parts), sum(len(v) for v in groups.values())


def _send_time():
    try:
        hh, mm = (db.get_setting("tasks_send_time", "08:00") or "08:00").split(":")
        return int(hh), int(mm)
    except Exception:
        return 8, 0


def run_digest(now=None, force=False):
    """Queue today's list (once per day, after the chosen time). Returns 1 if an e-mail was queued."""
    on = str(db.get_setting("tasks_digest_on", "true")).lower() == "true"
    if not force and not on:
        return 0
    ln = local_now(now)
    td = ln.date()
    if not force:
        hh, mm = _send_time()
        if (ln.hour, ln.minute) < (hh, mm):
            return 0
        if ln.weekday() >= 5 and str(db.get_setting("tasks_weekends", "false")).lower() != "true":
            return 0
    text, count = digest_text(td)
    if not count:
        return 0
    ctx = {"date": f"{td:%a %d %b %Y}", "count": str(count), "task_list": text}
    key = f"tasks:{td.isoformat()}" + (f":now{int((now or db.now_utc()).timestamp())}" if force else "")
    return 1 if alerts.create_alert("task_digest", key, ctx) else 0
