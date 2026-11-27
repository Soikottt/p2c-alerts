"""'Answer by email' buttons. Each alert email can carry options such as
'Remove this DID' / 'Keep it, wait 7 days'. Clicking opens your app, the person confirms, the answer is saved
and your team gets a notification email."""
import secrets
from datetime import timedelta
from html import escape

import config
import db

EFFECTS = {"record": "Only record the answer (your team decides)",
           "snooze": "Record + pause this alert for N days",
           "snooze_custom": "Record + pause for the number of days the person types (Days = the suggested number)",
           "retire_did": "Record + mark the DID as retired"}


def app_url():
    u = (db.get_setting("app_url", "") or config.get_secret("APP_URL") or "").strip()
    return u.rstrip("/")


def defs_for(alert_type):
    return db.memo(("defs", alert_type), lambda: db.q("SELECT label, effect, effect_days FROM alert_action_defs WHERE alert_type=:t "
                                                      "AND active = TRUE ORDER BY sort_order, id", t=alert_type))


def plain_to_html(text):
    return ('<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.5">'
            + escape(text or "").replace("\n", "<br>") + "</div>")


def build_html(body_html, links):
    """The formatted email body followed by one button per answer option."""
    btn = ('display:inline-block;padding:10px 16px;margin:4px 6px 4px 0;background:#1a73e8;color:#ffffff;'
           'border-radius:6px;text-decoration:none;font-weight:bold')
    buttons = "".join(f'<a href="{escape(u)}" style="{btn}">{escape(l)}</a>' for l, u in links)
    return (f'{body_html}<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px">'
            f'<p style="margin-top:18px"><b>Please choose one:</b></p><p>{buttons}</p>'
            '<p style="color:#888;font-size:12px">Your answer is saved and the team is notified.</p></div>')


def attach(alert_id, alert_type, body, body_html=None):
    """Create one secret link per option. Returns (text_body, html_body). Without buttons the body is returned as it is."""
    url, defs = app_url(), defs_for(alert_type)
    if not url or not defs:
        return body, body_html
    links, values, params = [], [], {"a": alert_id, "n": db.now_utc()}
    for i, d in enumerate(defs):                       # ONE insert for all the buttons of this email
        tok = secrets.token_urlsafe(16)
        values.append(f"(:t{i},:a,:l{i},:e{i},:d{i},:n)")
        params.update({f"t{i}": tok, f"l{i}": d["label"], f"e{i}": d["effect"] or "record", f"d{i}": d["effect_days"]})
        links.append((d["label"], f"{url}/?a={tok}"))
    db.x("INSERT INTO alert_actions (token, alert_id, label, effect, effect_days, created_at) VALUES " + ", ".join(values), **params)
    text = body + "\n\nPlease choose one (click a link):\n" + "\n".join(f"  * {l}: {u}" for l, u in links)
    return text, build_html(body_html or plain_to_html(body), links)


def _key():
    import hashlib
    return hashlib.sha256(f"{config.DB_URL}|{config.SMTP_PASSWORD}|p2c-answer".encode()).digest()


def sign(token, email):
    import hmac
    return hmac.new(_key(), f"{token}|{email.lower()}".encode(), "sha256").hexdigest()[:20]


def personalize(text, html, email):
    """Give ONE recipient his own copy of the answer links: the link carries his e-mail address (signed, so it cannot be
    changed by hand). When he clicks, the app already knows who he is: he does not type his name."""
    import base64
    import re
    email = (email or "").strip().lower()
    r = base64.urlsafe_b64encode(email.encode()).decode().rstrip("=")

    def add(sep):
        return lambda m: f"{m.group(0)}{sep}r={r}{sep}s={sign(m.group(1), email)}"
    pat = r"\?a=([\w-]+)"
    return (re.sub(pat, add("&"), text or ""),
            re.sub(pat, add("&amp;"), html) if html else html)


def identity(token, r, s):
    """'Name <email>' for a valid personal link, else None (then the page asks for name and email)."""
    import base64
    try:
        email = base64.urlsafe_b64decode(r + "=" * (-len(r) % 4)).decode().lower()
    except Exception:
        return None
    import hmac
    if not (s and hmac.compare_digest(sign(token, email), s)):
        return None
    m = db.q1("SELECT name FROM affiliate_managers WHERE LOWER(email)=:e LIMIT 1", e=email)
    name = (m["name"] if m else "").strip() or email.split("@")[0].replace(".", " ").replace("_", " ").title()
    return f"{name} <{email}>"


def lookup(token):
    return db.q1("""SELECT a.token, a.alert_id, a.label, a.effect, a.effect_days, al.subject AS alert_subject,
                           al.snooze_key, al.response_label, al.responded_by, al.responded_at
                    FROM alert_actions a JOIN alerts al ON al.id = a.alert_id WHERE a.token = :t""", t=token)


def respond(token, who, note, days=None):
    """Save the answer. First answer wins. Returns {'ok': bool, 'msg': str}."""
    import alerts
    row = lookup(token)
    if not row:
        return {"ok": False, "msg": "This link is not valid or has expired."}
    if row["response_label"]:
        return {"ok": False, "msg": f"This alert was already answered: '{row['response_label']}'"
                                    f"{' by ' + row['responded_by'] if row['responded_by'] else ''}."}
    now = db.now_utc()
    if row["effect"] == "snooze_custom":        # the person typed how many days to pause
        try:
            days = int(days if days is not None else row["effect_days"] or 7)
        except (TypeError, ValueError):
            days = int(row["effect_days"] or 7)
        days = min(max(days, 1), 365)
        row = {**row, "effect": "snooze", "effect_days": days, "label": f"{row['label']} ({days} days)"}
    n = db.x("UPDATE alerts SET response_label=:l, responded_at=:n, responded_by=:w, response_note=:note "
             "WHERE id=:i AND response_label IS NULL", l=row["label"], n=now, w=(who or "").strip() or None,
             note=(note or "").strip() or None, i=row["alert_id"])
    if n == 0:
        return {"ok": False, "msg": "Someone just answered this alert."}
    import cases
    cases.add_note(row["alert_id"], "answer", who, f"{row['label']}" + (f" - {note.strip()}" if (note or "").strip() else ""),
                   cases.mark_follow_up_if_open(row["alert_id"]))
    effect_text = "Nothing changed automatically. Your team decides."
    key, eff = row["snooze_key"], row["effect"]
    if eff == "snooze" and key and row["effect_days"]:
        until = now + timedelta(days=int(row["effect_days"]))
        db.x("INSERT INTO snoozes (entity_key, snooze_until, reason) VALUES (:k,:u,:r) "
             "ON CONFLICT (entity_key) DO UPDATE SET snooze_until=EXCLUDED.snooze_until, reason=EXCLUDED.reason",
             k=key, u=until, r=row["label"])
        effect_text = f"This alert is paused for {row['effect_days']} days."
        import audit
        audit.log("followup_paused", "snooze", key, alert_id=row["alert_id"], did_id=int(key[4:]) if key.startswith("did:") and key[4:].isdigit() else None,
                  new=f"until {until:%Y-%m-%d %H:%M} UTC", detail=row["label"], who=(who or "").strip() or None)
    elif eff == "retire_did" and key and key.startswith("did:"):
        db.x("UPDATE dids SET status='retired' WHERE id=:i", i=int(key.split(":")[1]))
        effect_text = "The DID was marked as retired (no more alerts for it)."
        import audit
        audit.log("did_retired", "did", key.split(":")[1], alert_id=row["alert_id"], did_id=int(key.split(":")[1]), new="retired",
                  detail="answered: " + str(row["label"]), who=(who or "").strip() or None)
    ctx = {"responder": (who or "").strip() or "Someone", "choice": row["label"],
           "note": (note or "").strip() or "-", "original_subject": row["alert_subject"], "effect_text": effect_text,
           "history": alerts.answer_history(row["alert_id"])}
    from email.utils import parseaddr
    alerts.create_alert("alert_response", f"resp:{row['alert_id']}", ctx, thread_id=row["alert_id"],
                        cc_extra=[parseaddr(who or "")[1]])       # the person who answered gets his own answer + history by e-mail
    return {"ok": True, "msg": f"Thank you. Your answer '{row['label']}' was saved and the team was notified."}
