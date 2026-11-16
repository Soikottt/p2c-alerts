"""My Tasks: a simple personal to-do list with a daily e-mail.
A task has a work date (when you must do it) and an alert date (when you want to be reminded; can be earlier).
Every change is written to task_log, so you can see when something was done or changed."""
import calendar
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import alerts
import db

REPEATS = ["none", "daily", "weekdays", "weekly", "monthly", "custom", "pick_weekdays", "pick_monthdays", "pick_dates"]
REPEAT_LABEL = {"none": "Does not repeat", "daily": "Every day", "weekdays": "Every weekday (Mon-Fri)",
                "weekly": "Every week", "monthly": "Every month", "custom": "Every N days (you choose N)",
                "pick_weekdays": "On the weekdays I choose (e.g. every Monday and Tuesday)",
                "pick_monthdays": "On the days of the month I choose (e.g. the 15th and 20th)",
                "pick_dates": "On the exact dates I choose"}
REPEAT_SHORT = {"none": "No repeat", "daily": "Every day", "weekdays": "Mon-Fri", "weekly": "Every week", "monthly": "Every month",
                "custom": "Every N days", "pick_weekdays": "Chosen weekdays", "pick_monthdays": "Chosen month days", "pick_dates": "Chosen dates"}
PICK = ("pick_weekdays", "pick_monthdays", "pick_dates")
REPEATING = ("daily", "weekdays", "weekly", "monthly", "custom") + PICK
WD_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MONTH_DAY_CHOICES = [str(i) for i in range(1, 32)] + ["last"]


# ---- "my own days": chosen weekdays / days of the month / exact dates
def parse_rule(repeat, value):
    """What the person typed or picked -> the saved rule text. Raises ValueError with a plain message."""
    import re
    parts = [str(x) for x in value] if isinstance(value, (list, tuple, set)) else re.split(r"[,\s;]+", str(value or "").strip())
    parts = [p for p in parts if p]
    if not parts:
        raise ValueError({"pick_weekdays": "Choose at least one weekday.", "pick_monthdays": "Choose at least one day of the month.",
                          "pick_dates": "Type at least one date, like 2026-12-15."}[repeat])
    if repeat == "pick_weekdays":
        names = [n.lower() for n in WD_NAMES]
        idx = set()
        for p in parts:
            if p.lower()[:3] not in names:
                raise ValueError(f"'{p}' is not a weekday (use Mon, Tue, Wed ...).")
            idx.add(names.index(p.lower()[:3]))
        return ",".join(str(i) for i in sorted(idx))
    if repeat == "pick_monthdays":
        days = set()
        for p in parts:
            if p.lower() in ("last", "end", "99"):
                days.add(99)
            elif p.isdigit() and 1 <= int(p) <= 31:
                days.add(int(p))
            else:
                raise ValueError(f"'{p}' is not a day of the month (use 1 to 31, or last).")
        return ",".join(str(i) for i in sorted(days))
    ds = set()
    for p in parts:
        d = pdate(p) if len(p) >= 10 else None
        if not d:
            raise ValueError(f"'{p}' is not a date. Write it like 2026-12-15.")
        ds.add(d.isoformat())
    return ",".join(sorted(ds))


def rule_text(repeat, rule):
    """The saved rule in plain words: 'Mon, Tue' / '15, 20, last' / '2026-12-01, 2026-12-15'."""
    items = [x for x in str(rule or "").split(",") if x]
    if repeat == "pick_weekdays":
        return ", ".join(WD_NAMES[int(i)] for i in items)
    if repeat == "pick_monthdays":
        return ", ".join("last" if i == "99" else i for i in items)
    return ", ".join(items)


def matches(d, repeat, rule):
    items = [x for x in str(rule or "").split(",") if x]
    if repeat == "pick_weekdays":
        return str(d.weekday()) in items
    if repeat == "pick_monthdays":
        last = calendar.monthrange(d.year, d.month)[1]
        return d.day in {last if int(i) == 99 else min(int(i), last) for i in items}
    return d.isoformat() in items


def next_after(repeat, rule, after):
    """The first chosen date later than `after`, or None (all chosen dates are over)."""
    if repeat == "pick_dates":
        later = sorted(x for x in str(rule or "").split(",") if x and x > after.isoformat())
        return date.fromisoformat(later[0]) if later else None
    for i in range(1, 800):
        d = after + timedelta(days=i)
        if matches(d, repeat, rule):
            return d
    return None


def first_on_or_after(repeat, rule, d):
    return d if matches(d, repeat, rule) else next_after(repeat, rule, d)


def time_options():
    """'12:00 AM', '12:15 AM' ... '11:45 PM'"""
    return [f"{(h % 12) or 12:02d}:{m:02d} {'AM' if h < 12 else 'PM'}" for h in range(24) for m in range(0, 60, 15)]


def fmt_time(hhmm):
    """'21:30' -> '09:30 PM'"""
    try:
        h, m = str(hhmm).split(":")[:2]
        h, m = int(h), int(m)
        return f"{(h % 12) or 12:02d}:{m:02d} {'AM' if h < 12 else 'PM'}"
    except (ValueError, AttributeError):
        return ""


def parse_time(v):
    """'09:30 PM' (or '21:30') -> '21:30'. Returns None when empty / not a time."""
    v = (v or "").strip().upper()
    if not v:
        return None
    try:
        if v.endswith(("AM", "PM")):
            t = datetime.strptime(v.replace(" ", ""), "%I:%M%p")
        else:
            t = datetime.strptime(v, "%H:%M")
        return f"{t.hour:02d}:{t.minute:02d}"
    except ValueError:
        return None


def fmt_dt(dt):
    """A stored (UTC) time shown in YOUR time zone, AM/PM:  Mon 16 Nov 2026, 07:56 PM"""
    if not dt:
        return ""
    return db.to_dt(dt).astimezone(local_tz()).strftime("%a %d %b %Y, %I:%M %p")


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


def add_task(title, notes="", work_date=None, alert_date=None, repeat="none", alert_time=None, repeat_days=None, repeat_rule=None, now=None):
    title = (title or "").strip()
    if not title:
        raise ValueError("Please write what the task is.")
    wd = pdate(work_date) or today(now)
    ad = pdate(alert_date) or wd
    if repeat not in REPEATS:
        repeat = "none"
    rule = None
    if repeat in PICK:
        rule = parse_rule(repeat, repeat_rule)
        first = first_on_or_after(repeat, rule, wd)
        if first is None:
            raise ValueError("All the dates you chose are already in the past.")
        ad, wd = first - (wd - ad), first               # the first work date is the first chosen day; the alert keeps its distance
    at = parse_time(alert_time) or default_time()
    rd = max(int(repeat_days), 1) if (repeat == "custom" and repeat_days) else (1 if repeat == "custom" else None)
    rows = db.q("INSERT INTO tasks (title, notes, work_date, alert_date, alert_time, repeat, repeat_days, repeat_rule, status, alert_count, created_at, updated_at) "
                "VALUES (:t,:n,:w,:a,:at,:r,:rd,:rr,'open',0,:now,:now) RETURNING id",
                t=title, n=(notes or "").strip(), w=iso(wd), a=iso(ad), at=at, r=repeat, rd=rd, rr=rule, now=db.now_utc())
    tid = rows[0]["id"]
    log(tid, title, "created", f"work {iso(wd)}, alert {iso(ad)} {fmt_time(at)}, {repeat_label(repeat, rd, rule)}")
    return tid


def default_time():
    return parse_time(db.get_setting("tasks_default_time", "09:00")) or "09:00"


def repeat_label(repeat, days=None, rule=None):
    if repeat == "custom":
        return f"Every {int(days or 1)} day(s)"
    if repeat in PICK:
        return {"pick_weekdays": "Every ", "pick_monthdays": "On day ", "pick_dates": "On "}[repeat] + rule_text(repeat, rule)
    return REPEAT_LABEL.get(repeat, repeat)


FIELDS = ("title", "notes", "work_date", "alert_date", "alert_time", "repeat", "repeat_days", "repeat_rule")
RESET_ALERT_ON = ("work_date", "alert_date", "alert_time", "repeat", "repeat_days", "repeat_rule")     # a changed schedule starts the alert again


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
        elif f == "alert_time":
            v = parse_time(v) or default_time()
        elif f == "repeat_days":
            v = max(int(v), 1) if v not in (None, "") else None
        elif f == "repeat_rule":
            eff = new.get("repeat") or old["repeat"]
            v = parse_rule(eff, v) if eff in PICK else None
        else:
            v = (v or "").strip()
            if f == "title" and not v:
                continue
        cur = old[f] or ("" if f in ("title", "notes") else None)
        if f in ("work_date", "alert_date"):
            cur = iso(cur)
        if f == "alert_time":
            cur = cur or default_time()
        if f == "repeat_days" and old["repeat"] != "custom" and (new.get("repeat") or old["repeat"]) != "custom":
            continue                                   # the number only matters for 'Every N days'
        if (cur or None) != (v or None):
            changes.append(f"{f}: '{cur or ''}' -> '{v or ''}'")
            sets.append(f"{f}=:{f}")
            params[f] = v
    eff_rep = params.get("repeat", old["repeat"])
    eff_rule = params.get("repeat_rule", old["repeat_rule"])
    if eff_rep in PICK and not eff_rule:
        raise ValueError("Choose the days for this repeat.")
    if eff_rep not in PICK and old["repeat_rule"] and "repeat_rule" not in params:
        sets.append("repeat_rule=NULL")                 # switched to a normal repeat: forget the chosen days
    if eff_rep in PICK and eff_rule and sets:
        wd = pdate(params.get("work_date", old["work_date"]))
        ad = pdate(params.get("alert_date", old["alert_date"])) or wd
        if wd and not matches(wd, eff_rep, eff_rule):     # the work date must be one of the chosen days: move it to the next one
            nxt = next_after(eff_rep, eff_rule, wd)
            if nxt:
                params["work_date"], params["alert_date"] = iso(nxt), iso(nxt - (wd - ad))
                for f in ("work_date", "alert_date"):
                    if f"{f}=:{f}" not in sets:
                        sets.append(f"{f}=:{f}")
                changes.append(f"work_date: moved to the next chosen day {params['work_date']}")
    if sets:
        if any(c.split(":")[0] in RESET_ALERT_ON for c in changes):
            sets += ["alert_count=0", "last_alert_at=NULL"]
        db.x(f"UPDATE tasks SET {', '.join(sets)}, updated_at=:now WHERE id=:i", **params)
        log(task_id, params.get("title") or old["title"], "changed", "; ".join(changes))
    return [c.split(":")[0] for c in changes]


def _add_months(d, n):
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def next_work_date(work, repeat, after, every=1):
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
        elif repeat == "custom":
            d = d + timedelta(days=max(int(every or 1), 1))
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
    if t["repeat"] in REPEATING:
        wd = pdate(t["work_date"]) or today(now)
        ad = pdate(t["alert_date"]) or wd
        offset = max((wd - ad).days, 0)
        if t["repeat"] in PICK:
            nxt = next_after(t["repeat"], t["repeat_rule"], max(today(now), wd))
            if nxt is None:                               # the last chosen date: the task is finished for good
                db.x("UPDATE tasks SET status='done', done_at=:n, updated_at=:n WHERE id=:i", n=n, i=task_id)
                log(task_id, t["title"], "done", "last chosen date reached: task finished")
                return
        else:
            nxt = next_work_date(wd, t["repeat"], max(today(now), wd), t["repeat_days"])
        db.x("UPDATE tasks SET work_date=:w, alert_date=:a, alert_count=0, last_alert_at=NULL, updated_at=:n WHERE id=:i",
             w=iso(nxt), a=iso(nxt - timedelta(days=offset)), n=n, i=task_id)
        log(task_id, t["title"], "done", f"repeats: next work date {iso(nxt)}")
    else:
        db.x("UPDATE tasks SET status='done', done_at=:n, updated_at=:n WHERE id=:i", n=n, i=task_id)
        log(task_id, t["title"], "done", "")


def reopen(task_id):
    t = db.q1("SELECT * FROM tasks WHERE id=:i", i=task_id)
    if t and t["status"] == "done":
        db.x("UPDATE tasks SET status='open', done_at=NULL, alert_count=0, last_alert_at=NULL, updated_at=:n WHERE id=:i", n=db.now_utc(), i=task_id)
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


# ------------------------------------------------------------------ real-time alerts and follow-ups
def alert_dt(t):
    """When this task's alert is due: alert date + alert time in YOUR time zone (an aware datetime), or None."""
    d = pdate(t["alert_date"]) or pdate(t["work_date"])
    if not d:
        return None
    hh, mm = (parse_time(t.get("alert_time")) or default_time()).split(":")
    return datetime(d.year, d.month, d.day, int(hh), int(mm), tzinfo=local_tz())


def followup_minutes():
    try:
        return max(int(float(db.get_setting("tasks_followup_minutes", "30") or 0)), 0)
    except ValueError:
        return 30


def followup_max():
    try:
        return max(int(float(db.get_setting("tasks_followup_max", "10") or 0)), 0)
    except ValueError:
        return 10


def in_quiet_hours(now=None):
    """True when follow-ups must wait (default 11:00 PM - 06:00 AM, your time)."""
    a, b = parse_time(db.get_setting("tasks_quiet_from", "23:00")), parse_time(db.get_setting("tasks_quiet_to", "06:00"))
    if not a or not b or a == b:
        return False
    cur = local_now(now).strftime("%H:%M")
    return (a <= cur < b) if a < b else (cur >= a or cur < b)


def state_of(t, now=None):
    """What you should do with this open task:
    'action'   the alert was sent and the task is still not done (or its work date is over)
    'due'      the alert time has come, the e-mail goes out at the next engine run (within 5 minutes)
    'upcoming' the alert time is later"""
    now = now or db.now_utc()
    wd = pdate(t["work_date"])
    if (t.get("alert_count") or 0) > 0 or (wd and wd < today(now)):
        return "action"
    adt = alert_dt(t)
    if adt is not None and adt <= now:
        return "due"
    return "upcoming"


STATE_LABEL = {"action": "🔴 ACTION NEEDED", "due": "🟠 DUE NOW", "upcoming": "⚪ Upcoming"}


def next_alert_text(t, now=None):
    """Plain words for the 'Next alert' column."""
    now = now or db.now_utc()
    n = t.get("alert_count") or 0
    adt = alert_dt(t)
    if n == 0:
        if adt is None:
            return ""
        return ("sending within 5 min" if adt <= now else adt.strftime("%a %d %b, %I:%M %p"))
    fm = followup_minutes()
    if fm == 0:
        return f"alerted {n}x (no follow-ups)"
    if n - 1 >= followup_max() > 0:
        return f"alerted {n}x - follow-ups finished"
    last = db.to_dt(t["last_alert_at"]) if t.get("last_alert_at") else None
    if not last:
        return ""
    due = (last + timedelta(minutes=fm)).astimezone(local_tz())
    return f"follow-up #{n} at {due.strftime('%I:%M %p')}"


def _task_lines(items, now, followup):
    tz = local_tz()
    out = []
    for i, t in enumerate(items, 1):
        wd, adt = pdate(t["work_date"]), alert_dt(t)
        bits = []
        if wd:
            bits.append(f"work date {wd:%a %d %b}")
        if adt:
            bits.append(f"alert {adt:%a %d %b, %I:%M %p}")
        if followup:
            bits.append(f"alerted {t['alert_count']}x already")
        line = f"{i}. {t['title']}"
        if bits:
            line += "\n   " + " | ".join(bits)
        if (t["notes"] or "").strip():
            line += "\n   Note: " + " ".join(t["notes"].split())[:300]
        out.append(line)
    return "\n\n".join(out)          # a blank line between tasks, so they are easy to tell apart in the e-mail


def _send_group(items, now, heading, followup, key):
    app_url = (db.get_setting("app_url", "") or "").strip()
    fm, fmax = followup_minutes(), followup_max()
    note = (f"If you do not tick Done in the app, a follow-up comes every {fm} minutes" + (f" (up to {fmax} times)." if fmax else ".")) if fm else ""
    ctx = {"heading": heading, "count": str(len(items)), "first_title": items[0]["title"],
           "now": local_now(now).strftime("%a %d %b %Y, %I:%M %p") + f" ({db.get_setting('tasks_timezone', 'Asia/Dhaka')})",
           "task_list": _task_lines(items, now, followup), "app_url": app_url or "(set the app address in Settings)",
           "followup_note": note}
    if not alerts.create_alert("task_alert", key, ctx):
        return 0
    ids = [t["id"] for t in items]
    db.x_in("UPDATE tasks SET alert_count = alert_count + 1, last_alert_at = :n WHERE id IN :ids", "ids", ids, n=now)
    for t in items:
        log(t["id"], t["title"], "follow-up sent" if followup else "alert sent", f"#{(t['alert_count'] or 0) + 1}")
    return len(items)


def run_alerts(now=None):
    """Called by the engine (every 5 minutes). Sends ONE e-mail for the tasks whose alert time has come, and ONE follow-up e-mail for
    tasks that were alerted but are still not done after the follow-up time. Returns (new alerts, follow-ups)."""
    now = now or db.now_utc()
    firsts, follows = [], []
    fm, fmax, quiet = followup_minutes(), followup_max(), None
    for t in open_tasks():
        adt = alert_dt(t)
        if adt is None or adt > now:
            continue
        n = t["alert_count"] or 0
        if n == 0:
            firsts.append(t)
            continue
        if fm == 0 or (fmax and n - 1 >= fmax):
            continue
        last = db.to_dt(t["last_alert_at"]) if t["last_alert_at"] else None
        if last is None or now - last >= timedelta(minutes=fm):
            if quiet is None:
                quiet = in_quiet_hours(now)
            if not quiet:
                follows.append(t)
    stamp = f"{now:%Y%m%d%H%M}"
    a = _send_group(firsts, now, "Task alert", False, f"taskalert:{stamp}") if firsts else 0
    f = _send_group(follows, now, "Follow-up: still not done", True, f"taskfollow:{stamp}") if follows else 0
    return a, f
