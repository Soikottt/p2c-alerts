"""Follow-up notes and status for every alert (open / follow-up / done). Nothing is ever deleted: this is your history."""
import db

# What the team has done about an alert. 'follow_up' is set automatically when an affiliate manager answers.
STATUSES = ["open", "follow_up", "pending", "in_progress", "waiting", "done", "no_action"]
STATUS_LABEL = {
    "open": "🆕 Open (no answer yet)",
    "follow_up": "💬 Answered - needs action",
    "pending": "⏳ Pending (work not started)",
    "in_progress": "🔧 In progress",
    "waiting": "🕒 Waiting for publisher / AM",
    "done": "✅ Done (action completed)",
    "no_action": "🚫 No action needed",
}
LABEL_STATUS = {v: k for k, v in STATUS_LABEL.items()}


def label(status):
    return STATUS_LABEL.get(status or "open", str(status))


def add_note(alert_id, source, author, text, status=None):
    """source: 'answer' (button on the email page), 'email' (a reply), 'team' (written in the app)."""
    now = db.now_utc()
    db.x("INSERT INTO alert_notes (alert_id, source, author, text, status_after, created_at) VALUES (:a,:s,:w,:t,:st,:n)",
         a=alert_id, s=source, w=(author or "").strip() or None, t=(text or "").strip(), st=status, n=now)
    if status:
        set_status(alert_id, status, now)


def set_status(alert_id, status, now=None):
    if status in STATUSES:
        db.x("UPDATE alerts SET case_status=:s, case_updated_at=:n WHERE id=:i", s=status, n=now or db.now_utc(), i=alert_id)


def mark_follow_up_if_open(alert_id):
    r = db.q1("SELECT case_status FROM alerts WHERE id=:i", i=alert_id)
    return "follow_up" if (not r or (r["case_status"] or "open") == "open") else None


def timeline(alert_id):
    return db.q("SELECT created_at, source, author, text, status_after FROM alert_notes WHERE alert_id=:i ORDER BY id", i=alert_id)


def _did_id(key):
    try:
        return int(str(key).split(":")[1]) if str(key).startswith("did:") else None
    except (ValueError, IndexError):
        return None


def latest_by_did(sources=("answer", "email")):
    """{did_id: {'text', 'author', 'when', 'status', 'alert_id'}} using the newest note written by the affiliate manager."""
    out = {}
    rows = db.q("""SELECT n.id, n.alert_id, n.source, n.author, n.text, n.created_at, a.snooze_key, a.case_status
                   FROM alert_notes n JOIN alerts a ON a.id = n.alert_id
                   WHERE a.alert_type = 'did_inactive' ORDER BY n.id""")
    for r in rows:
        did = _did_id(r["snooze_key"])
        if did and r["source"] in sources:
            out[did] = {"text": r["text"], "author": r["author"], "when": db.to_dt(r["created_at"]),
                        "status": r["case_status"], "alert_id": r["alert_id"]}
    return out


def status_by_did():
    """{did_id: status of the newest inactive-DID alert}"""
    out = {}
    for r in db.q("SELECT snooze_key, case_status FROM alerts WHERE alert_type='did_inactive' AND status<>'skipped' ORDER BY id"):
        did = _did_id(r["snooze_key"])
        if did:
            out[did] = r["case_status"] or "open"
    return out


def one_line(note, tzfmt=None):
    """'publisher paused (Mia, 2026-11-02)'"""
    if not note:
        return ""
    when = tzfmt(note["when"], False)[:10] if tzfmt and note.get("when") else ""
    who = note.get("author") or "AM"
    return f"{note['text']} ({who}{', ' + when if when else ''})"
