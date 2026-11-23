"""One place that understands an alert's CHAIN: the first alert, its reminders, its follow-ups and every answer, reply and note.
The engine (rules.py), the Follow-ups page and the Alert Log all use this, so they can never disagree.

family_ids(alert_id)   -> (root id, [all alert ids of that e-mail conversation])   (same [#A123] code, same thread)
thread_events(ids)     -> the full history (alerts sent, answers, replies, notes), oldest first
kind_of(alert)         -> 'New alert' / 'Follow-up email (reminder)' / 'Follow-up email (pause ended)' / 'Manager response' ...
evaluate(chain, ...)   -> what the engine must do next for one DID chain
parse_code(text)       -> alert id from '#A306', 'A306', '[#A306]' or '306'
"""
import re
from datetime import datetime, timedelta, timezone

import db
from db import to_dt

CLOSED = ("done", "no_action")
_MIN = datetime(1970, 1, 1, tzinfo=timezone.utc)


def parse_code(text):
    m = re.search(r"#?\s*A?\s*(\d+)", str(text or "").strip(), re.I)
    return int(m.group(1)) if m else None


def kind_of(a):
    """Plain-English label of one alert row: what kind of email it was."""
    t, key = a.get("alert_type") or "", str(a.get("dedupe_key") or "")
    if t == "alert_response":
        return "Manager response (notice)"
    if key.endswith(":cancelled"):
        return "Follow-up CANCELLED"
    if t == "did_inactive":
        parts = key.split(":")
        sub = parts[4] if len(parts) > 4 else ""
        if sub.startswith("f"):
            return "Follow-up email (pause ended)"
        if sub.startswith("r"):
            return "Follow-up email (reminder)"
        return "New alert"
    if t == "did_new_no_calls":
        parts = key.split(":")
        n = parts[2] if len(parts) > 2 else "1"
        return "Follow-up email (repeat)" if n.isdigit() and int(n) > 1 else "New alert"
    return "New alert"


def root_of(alert_id):
    """The first alert of this conversation (follow-ups point to it with thread_id)."""
    cur, seen = int(alert_id), set()
    for _ in range(25):
        if cur in seen:
            break
        seen.add(cur)
        r = db.q1("SELECT thread_id FROM alerts WHERE id=:i", i=cur)
        if not r or not r["thread_id"]:
            break
        cur = int(r["thread_id"])
    return cur


def _base_of(key):
    p = str(key or "").split(":")
    if p and p[0] == "did" and len(p) >= 4:
        return ":".join(p[:4])
    if p and p[0] == "newdid" and len(p) >= 2:
        return ":".join(p[:2])
    return None


def family_ids(alert_id):
    """(root id, ids of every alert of the conversation, oldest first): the root, everything that points to it with thread_id
    (follow-ups, answer notices) and - for older follow-ups made before threading existed - the alerts of the same DID chain."""
    root = root_of(alert_id)
    ids, frontier = {root}, [root]
    while frontier:
        kids = db.q_in("SELECT id FROM alerts WHERE thread_id IN :p", "p", frontier)
        frontier = [k["id"] for k in kids if k["id"] not in ids]
        ids.update(frontier)
    r = db.q1("SELECT dedupe_key FROM alerts WHERE id=:i", i=root)
    base = _base_of(r["dedupe_key"]) if r else None
    if base:
        like = base + ":%" if base.startswith("did:") else base + ":%"
        more = db.q("SELECT id FROM alerts WHERE (dedupe_key = :b OR dedupe_key LIKE :l) AND dedupe_key NOT LIKE :nc", b=base, l=like, nc="%:cancelled")
        for m in more:
            ids.add(m["id"])
        frontier = [m["id"] for m in more]
        while frontier:
            kids = db.q_in("SELECT id FROM alerts WHERE thread_id IN :p", "p", frontier)
            frontier = [k["id"] for k in kids if k["id"] not in ids]
            ids.update(frontier)
    return root, sorted(ids)


def thread_events(ids):
    """Everything that happened in these alerts, oldest first:
    [{'when','alert_id','event','who','text','kind'}]. Events are: 'Email' (an alert / follow-up was sent), 'Answer' (button), 'Reply' (e-mail reply), 'Note' (team)."""
    ids = sorted({int(i) for i in ids})
    if not ids:
        return []
    al = db.q_in("SELECT id, alert_type, dedupe_key, status, created_at, sent_at, subject, response_label, responded_at, responded_by, response_note "
                 "FROM alerts WHERE id IN :i", "i", ids)
    notes = db.q_in("SELECT alert_id, created_at, source, author, text, status_after FROM alert_notes WHERE alert_id IN :i ORDER BY id", "i", ids)
    answered_notes = {n["alert_id"] for n in notes if n["source"] == "answer"}
    ev = []
    for a in al:
        if a["alert_type"] == "alert_response":
            continue
        k = kind_of(a)
        if k == "Follow-up CANCELLED":
            ev.append({"when": to_dt(a["created_at"]), "alert_id": a["id"], "event": "Cancelled", "who": "", "kind": k, "text": "Follow-up cancelled (DID retired / deleted)"})
            continue
        st = {"sent": "sent", "pending": "waiting to be sent", "dry_run": "test only (not really sent)", "failed": "FAILED to send"}.get(a["status"], a["status"])
        ev.append({"when": to_dt(a["sent_at"] or a["created_at"]), "alert_id": a["id"], "event": "Email", "who": "", "kind": k, "text": f"{k} - {st}"})
        if a["response_label"] and a["id"] not in answered_notes:
            ev.append({"when": to_dt(a["responded_at"]), "alert_id": a["id"], "event": "Answer", "who": a["responded_by"] or "", "kind": "Manager response",
                       "text": a["response_label"] + (f" - {a['response_note']}" if a["response_note"] else "")})
    kind_by_id = {a["id"]: kind_of(a) for a in al}
    for n in notes:
        ev.append({"when": to_dt(n["created_at"]), "alert_id": n["alert_id"],
                   "event": {"answer": "Answer", "email": "Reply", "team": "Note"}.get(n["source"], n["source"]), "who": n["author"] or "",
                   "kind": {"answer": "Manager response", "email": "Manager reply (email)", "team": "Team note"}.get(n["source"], n["source"]),
                   "text": (n["text"] or "") + (f"   [status: {n['status_after']}]" if n["status_after"] else "")})
    ev.sort(key=lambda e: (e["when"] or _MIN, e["alert_id"]))
    return ev


def history_text(alert_id, limit=40):
    """Plain-text history of the whole conversation (for e-mails): every alert sent and every answer / reply / note."""
    from timeutil import fmt
    _, ids = family_ids(alert_id)
    lines = []
    for e in thread_events(ids)[-limit:]:
        who = f" - {e['who']}" if e["who"] else ""
        what = {"Email": "Email sent", "Cancelled": "Cancelled"}.get(e["event"], e["kind"])
        lines.append(f"- {fmt(e['when'])}{who} ({what}): " + " ".join((e["text"] or "").split())[:400])
    return "\n".join(lines) or "- (nothing yet)"


def latest_answer(ids):
    """Newest answer of the whole conversation: {'label','by','at','note'} or None."""
    best = None
    for a in db.q_in("SELECT id, response_label, responded_at, responded_by, response_note FROM alerts WHERE id IN :i AND response_label IS NOT NULL", "i", list(ids) or [0]):
        at = to_dt(a["responded_at"]) or _MIN
        if best is None or at > best["at"]:
            best = {"label": a["response_label"], "by": a["responded_by"] or "", "at": at, "note": a["response_note"] or "", "alert_id": a["id"]}
    return best


def closed_by_team(chain):
    """True when the newest status the team gave this chain is 'done' / 'no action needed'."""
    touched = [a for a in chain if (a.get("case_status") or "open") != "open"]
    if not touched:
        return False
    newest = max(touched, key=lambda a: to_dt(a.get("case_updated_at")) or _MIN)
    return (newest.get("case_status") or "open") in CLOSED


def _evaluate_core(chain, now, snooze_until, remind_days, remind_max, base):
    """What happens next for one DID chain (alerts oldest first). Returns a dict:
       state  : 'queued' | 'closed' | 'reminder' | 'reminders_done' | 'paused' | 'followup_due' | 'answered' | 'engaged'
       next_at: when the next email is due (None = nothing scheduled)
       key    : dedupe-key suffix for the next email ('' = the chain has no email yet)
       tag    : text in front of the follow-up
       reminders: reminders sent since the last real alert / follow-up
       answer : the newest answered alert of the chain (or None)
       note   : explanation in plain English
    Every chain is judged on its OWN alerts and answers, so one DID never inherits another DID's schedule."""
    out = {"state": "new", "next_at": None, "key": base, "tag": "", "reminders": 0, "answer": None, "note": ""}
    if not chain:
        return out
    last = chain[-1]
    last_sent = to_dt(last.get("sent_at") or last.get("created_at"))
    for a in reversed(chain):                         # reminders since the newest real alert / follow-up
        if ":r" in str(a["dedupe_key"])[len(base):]:
            out["reminders"] += 1
        else:
            break
    answered = [a for a in chain if a.get("response_label")]
    out["answer"] = max(answered, key=lambda a: to_dt(a.get("responded_at")) or _MIN) if answered else None
    if last["status"] == "pending":
        out.update(state="queued", next_at=now, note="Waiting in the send queue")
        return out
    if closed_by_team(chain):
        out.update(state="closed", note="Closed by the team (done / no action needed): no more follow-ups")
        return out
    # an answer that came AFTER the newest email: the manager decides what comes next
    fresh = [a for a in answered if (to_dt(a.get("responded_at")) or _MIN) >= (last_sent or _MIN)]
    if fresh:
        ans = max(fresh, key=lambda a: to_dt(a.get("responded_at")) or _MIN)
        resp_at = to_dt(ans["responded_at"])
        if snooze_until and resp_at and snooze_until > resp_at:
            if snooze_until > now:
                out.update(state="paused", next_at=snooze_until, tag="FOLLOW-UP (pause ended): ", key=f"{base}:f{last['id']}", reminders=0,
                           note="Paused by the manager's answer - follow-up when the pause ends")
            else:
                out.update(state="followup_due", next_at=snooze_until, tag="FOLLOW-UP (pause ended): ", key=f"{base}:f{last['id']}", reminders=0,
                           note="The pause ended - follow-up is due")
        else:
            out.update(state="answered", note="Answered, no pause set: nothing scheduled")
        return out
    # nobody answered the newest email. An e-mail reply / the team working on it (after that email) also counts as 'somebody is on it'.
    engaged = [a for a in chain if (a.get("case_status") or "open") in ("follow_up", "in_progress", "waiting", "pending")
               and (to_dt(a.get("case_updated_at")) or _MIN) >= (last_sent or _MIN)]
    if engaged:
        out.update(state="engaged", note="An affiliate manager replied / the team is working on it: no automatic reminder")
        return out
    if remind_days > 0 and out["reminders"] < remind_max:
        due = last_sent + timedelta(days=remind_days)
        out.update(state="reminder", next_at=due, key=f"{base}:r{last['id']}", tag=f"REMINDER {out['reminders'] + 1}/{remind_max}: ",
                   note=f"Reminder {out['reminders'] + 1} of {remind_max} if nobody answers")
    else:
        out.update(state="reminders_done", note="No more reminders for this idle period")
    return out


def evaluate(chain, now, snooze_until, remind_days, remind_max, base):
    """See _evaluate_core. One extra rule, the same the engine uses when it creates an email (alerts.create_alert -> is_snoozed):
    while a manager's pause is running, NOTHING is sent. So the next email of that DID is the end of the pause, whatever the e-mails
    before it look like. (Before, the page could show a reminder date inside a running pause that the engine would never send.)"""
    out = _evaluate_core(chain, now, snooze_until, remind_days, remind_max, base)
    if chain and snooze_until and snooze_until > now and out["state"] not in ("new", "queued", "closed", "paused"):
        out.update(state="paused", next_at=snooze_until, tag="FOLLOW-UP (pause ended): ", key=f"{base}:f{chain[-1]['id']}", reminders=0,
                   note="Paused by the manager's answer - follow-up when the pause ends")
    return out
