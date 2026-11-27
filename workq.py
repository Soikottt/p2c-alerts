"""Work queue helpers: filters, assignment, bulk actions, saved views, reply templates. No Streamlit in here."""
import json
from datetime import timedelta

import audit
import cases
import db

CLOSED = ("done", "no_action")

PRESETS = {
    "My open": {"case": [], "email": "", "asg": "Me", "ovd": False, "days": 0, "open_only": True},
    "Overdue": {"case": [], "email": "", "asg": "Anyone", "ovd": True, "days": 0, "open_only": True},
    "Failed e-mails": {"case": [], "email": "failed", "asg": "Anyone", "ovd": False, "days": 7, "open_only": False},
    "Unassigned": {"case": ["open", "follow_up", "pending"], "email": "", "asg": "Unassigned", "ovd": False, "days": 0, "open_only": False},
    "Needs reply": {"case": ["open", "follow_up", "pending"], "email": "", "asg": "Anyone", "ovd": False, "days": 30, "open_only": False},
    "Waiting": {"case": ["waiting"], "email": "", "asg": "Anyone", "ovd": False, "days": 0, "open_only": False},
}
DEFAULT_FILTERS = {"case": [], "email": "", "asg": "Anyone", "ovd": False, "days": 0, "open_only": False}


def filters_sql(f, me, now):
    """-> (' AND ...', params) for the alerts table aliased as a. Portable SQL."""
    f = {**DEFAULT_FILTERS, **(f or {})}
    sql, p = " AND a.alert_type <> 'alert_response'", {}
    cs = [c for c in (f.get("case") or []) if c]
    if cs:
        names = []
        for i, c in enumerate(cs):
            p[f"f_c{i}"] = c
            names.append(f":f_c{i}")
        sql += f" AND COALESCE(a.case_status,'open') IN ({','.join(names)})"
    if f.get("open_only"):
        sql += " AND COALESCE(a.case_status,'open') NOT IN ('done','no_action')"
    if f.get("email"):
        sql += " AND a.status = :f_email"
        p["f_email"] = f["email"]
    asg = f.get("asg") or "Anyone"
    if asg == "Me":
        sql += " AND a.assigned_to = :f_me"
        p["f_me"] = me or "@@nobody@@"
    elif asg == "Unassigned":
        sql += " AND (a.assigned_to IS NULL OR a.assigned_to = '')"
    elif asg != "Anyone":
        sql += " AND a.assigned_to = :f_asg"
        p["f_asg"] = asg
    if f.get("ovd"):
        sql += " AND a.due_at IS NOT NULL AND a.due_at < :f_now AND COALESCE(a.case_status,'open') NOT IN ('done','no_action')"
        p["f_now"] = now
    if int(f.get("days") or 0) > 0:
        sql += " AND a.created_at >= :f_since"
        p["f_since"] = now - timedelta(days=int(f["days"]))
    return sql, p


# ---------------------------------------------------------------- people
def people():
    names = set()
    try:
        for r in db.q("SELECT name FROM app_users WHERE active=TRUE"):
            names.add(r["name"])
    except Exception:
        pass
    try:
        for r in db.q("SELECT name FROM affiliate_managers WHERE active=TRUE"):
            names.add(r["name"])
    except Exception:
        pass
    return sorted(n for n in names if n)


# ---------------------------------------------------------------- assignment + bulk
def assign(alert_ids, person, due_at, who):
    person = (person or "").strip() or None
    n = 0
    for aid in alert_ids:
        old = db.q1("SELECT assigned_to, due_at FROM alerts WHERE id=:i", i=int(aid))
        if old is None:
            continue
        db.x("UPDATE alerts SET assigned_to=:p, due_at=:d, due_notified_at=NULL WHERE id=:i", p=person, d=due_at, i=int(aid))
        audit.log("assigned", "alert", aid, alert_id=int(aid), old=str(old.get("assigned_to") or ""), new=str(person or ""),
                  detail=f"due {due_at:%Y-%m-%d %H:%M} UTC" if due_at else "no due time", who=who)
        n += 1
    return n


def bulk_status(alert_ids, status, who, note=None):
    n = 0
    for aid in alert_ids:
        cases.add_note(int(aid), "team", who, (note or "").strip() or f"Status set to {cases.label(status)} (bulk)", status)
        n += 1
    return n


def bulk_note(alert_ids, who, note, status=None):
    for aid in alert_ids:
        cases.add_note(int(aid), "team", who, note, status)
    return len(alert_ids)


# ---------------------------------------------------------------- saved views
def views_list(owner):
    return db.q("SELECT id, name, owner, filters FROM saved_views WHERE owner=:o OR owner='' ORDER BY name", o=owner or "")


def view_save(owner, name, filters, shared=False):
    name = (name or "").strip()
    if not name:
        return False
    own = "" if shared else (owner or "")
    db.x("DELETE FROM saved_views WHERE owner=:o AND name=:n", o=own, n=name)
    db.x("INSERT INTO saved_views (owner, name, filters, created_at) VALUES (:o,:n,:f,:t)", o=own, n=name, f=json.dumps(filters), t=db.now_utc())
    audit.log("view_saved", "view", name, new=json.dumps(filters)[:300], who=owner)
    return True


def view_delete(view_id, who=None):
    db.x("DELETE FROM saved_views WHERE id=:i", i=int(view_id))
    audit.log("view_deleted", "view", view_id, who=who)


# ---------------------------------------------------------------- reply templates
DEFAULT_REPLIES = [
    ("Pausing as requested", "Thanks. We will pause {did} as requested and check it again at the next follow-up.", "waiting"),
    ("Traffic is back", "{publisher} confirmed traffic is flowing again on {did}.", "done"),
    ("Need more information", "We need more details about {did}. Please tell us why the traffic stopped and when it will restart.", "waiting"),
    ("No longer needed", "{did} is no longer needed. No more action required.", "no_action"),
    ("Escalated to buyer", "Escalated to the buyer team for a decision on {did}.", "in_progress"),
]


def replies_list():
    rows = db.q("SELECT id, name, body, set_status FROM reply_templates ORDER BY name")
    if not rows:
        for n, b, s in DEFAULT_REPLIES:
            db.x("INSERT INTO reply_templates (name, body, set_status, created_at) VALUES (:n,:b,:s,:t)", n=n, b=b, s=s, t=db.now_utc())
        rows = db.q("SELECT id, name, body, set_status FROM reply_templates ORDER BY name")
    return rows


def reply_save(name, body, status, who=None, template_id=None):
    name, body = (name or "").strip(), (body or "").strip()
    if not name or not body:
        return False
    if template_id:
        db.x("UPDATE reply_templates SET name=:n, body=:b, set_status=:s WHERE id=:i", n=name, b=body, s=status or None, i=int(template_id))
    else:
        db.x("INSERT INTO reply_templates (name, body, set_status, created_at) VALUES (:n,:b,:s,:t)", n=name, b=body, s=status or None, t=db.now_utc())
    audit.log("reply_template_saved", "reply_template", name, who=who)
    return True


def reply_delete(template_id, who=None):
    db.x("DELETE FROM reply_templates WHERE id=:i", i=int(template_id))
    audit.log("reply_template_deleted", "reply_template", template_id, who=who)


def reply_render(body, did="", publisher="", alert_id=""):
    return (str(body or "").replace("{did}", str(did or "the DID")).replace("{publisher}", str(publisher or "the publisher"))
            .replace("{alert}", f"A{alert_id}" if alert_id else "this alert"))
