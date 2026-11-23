"""Sends the queued alerts. An alert counts as SENT only after Gmail accepts it."""
import contextlib
import smtplib
import time
from datetime import timedelta
from email.message import EmailMessage
from email.utils import formataddr

import actions
import alerts
import config
import db

MAX_ATTEMPTS = 5


def is_dry_run():
    if config.DRY_RUN_ENV:
        return True
    return str(db.get_setting("dry_run", "true")).strip().lower() == "true"


def sender():
    """(display name, from address). From address defaults to your SMTP login."""
    name = (db.get_setting("from_name", "") or "").strip()
    addr = (db.get_setting("from_email", "") or "").strip() or config.SMTP_USER
    return name, addr


def _build(to_list, cc_list, subject, body, html=None, attachments=None, headers=None):
    name, addr = sender()
    msg = EmailMessage()
    msg["From"] = formataddr((name, addr)) if name else addr
    msg["To"] = ", ".join(to_list)
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)
    reply_to = alerts.parse_emails(db.get_setting("reply_to", ""))
    if reply_to:
        msg["Reply-To"] = ", ".join(reply_to)
    msg["Subject"] = subject
    for k, v in (headers or {}).items():
        if v:
            msg[k] = v
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    for fname, data, maintype, subtype in (attachments or []):
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=fname)
    return msg, addr, list(to_list) + list(cc_list)


def _open(stack):
    """Connect + log in once. The connection is closed when `stack` is closed."""
    if int(config.SMTP_PORT) == 465:
        s = stack.enter_context(smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=30))
    else:
        s = stack.enter_context(smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30))
        s.starttls()
    s.login(config.SMTP_USER, config.SMTP_PASSWORD)
    return s


class Session:
    """One Gmail login reused for a whole batch of emails (logging in again for every email is slow)."""
    def __init__(self):
        self.stack, self.server = None, None

    def get(self):
        if self.server is None:
            self.stack = contextlib.ExitStack()
            try:
                self.server = _open(self.stack)
            except Exception:
                self.close()
                raise
        return self.server

    def close(self):
        if self.stack is not None:
            try:
                self.stack.close()
            except Exception:
                pass
        self.stack, self.server = None, None


def smtp_send(to_list, cc_list, subject, body, html=None, attachments=None, session=None, headers=None, envelope=None):
    if not config.SMTP_USER or not config.SMTP_PASSWORD:
        raise RuntimeError("SMTP_USER / SMTP_PASSWORD are missing in your Secrets.")
    msg, addr, recipients = _build(to_list, cc_list, subject, body, html, attachments, headers)
    if envelope:
        recipients = list(envelope)          # same visible To / Cc, but delivered to this one person only
    if session is not None:
        session.get().send_message(msg, from_addr=addr, to_addrs=recipients)
        return
    with contextlib.ExitStack() as stack:
        _open(stack).send_message(msg, from_addr=addr, to_addrs=recipients)


def thread_headers(a):
    """Message-ID for every alert; a follow-up / answer also points to the first alert of its conversation (In-Reply-To / References),
    so Gmail / Outlook show them as ONE conversation (same subject, same [#A..] code)."""
    _, addr = sender()
    domain = (addr or "localhost").split("@")[-1]
    h = {"Message-ID": f"<p2c-alert-{a['id']}@{domain}>"}
    if a.get("thread_id"):
        import chain
        parent = f"<p2c-alert-{a['thread_id']}@{domain}>"
        root = f"<p2c-alert-{chain.root_of(a['thread_id'])}@{domain}>"
        h["In-Reply-To"] = parent
        h["References"] = root if root == parent else f"{root} {parent}"
    return h


def send_test(to_list, subject="Test email from your Pay-Per-Call alert tool"):
    """Always really sends (ignores dry-run). Used by the 'Send test email' button."""
    smtp_send(to_list, [], subject, "If you can read this, your email setup works.\n")


def _drop_for_retired_dids(rows, res):
    """Last look before sending: a DID alert (inactive / new-no-calls, including its reminders and follow-ups) for a DID that has been
    retired, deleted or set to 'never alert' in the meantime is cancelled, not sent. The alert record is kept for the reports."""
    ids = {}
    for a in rows:
        k = str(a.get("snooze_key") or "")
        if a["alert_type"] in ("did_inactive", "did_new_no_calls") and k.startswith("did:") and k[4:].isdigit():
            ids[a["id"]] = int(k[4:])
    if not ids:
        return rows
    ok = {r["id"] for r in db.q_in("SELECT id FROM dids WHERE id IN :i AND status = 'active' AND never_alert = FALSE", "i", sorted(set(ids.values())))}
    keep = []
    for a in rows:
        if a["id"] in ids and ids[a["id"]] not in ok:
            db.x("UPDATE alerts SET status='skipped', last_error='Cancelled before sending: the DID is retired, deleted or set to never alert' WHERE id=:i", i=a["id"])
            res["cancelled"] = res.get("cancelled", 0) + 1
        else:
            keep.append(a)
    return keep


CLAIM_MINUTES = 10


def _sent_set(a):
    return {x.strip().lower() for x in str(a.get("sent_to") or "").split(",") if x.strip()}


def _claim(alert_id):
    """Only ONE sender may work on an alert: the first to claim it wins (the app button and the 5-minute engine can run at the same time).
    A claim older than CLAIM_MINUTES is treated as a crashed run and can be taken over."""
    now = db.now_utc()
    return db.x("UPDATE alerts SET claimed_at=:n WHERE id=:i AND status='pending' AND (claimed_at IS NULL OR claimed_at < :stale)",
                n=now, i=alert_id, stale=now - timedelta(minutes=CLAIM_MINUTES)) > 0


def _drop_for_paused_dids(rows, res):
    """Last look before sending (2): an e-mail of a DID that an affiliate manager paused in the meantime (answered 'wait N days' after this
    e-mail was queued) is cancelled, not sent. The record is kept."""
    keys = {}
    for a in rows:
        k = str(a.get("snooze_key") or "")
        if a["alert_type"] in ("did_inactive", "did_new_no_calls") and k.startswith("did:"):
            keys[a["id"]] = k
    if not keys:
        return rows
    now = db.now_utc()
    paused = {r["entity_key"]: db.to_dt(r["snooze_until"]) for r in db.q_in("SELECT entity_key, snooze_until FROM snoozes WHERE entity_key IN :k", "k", sorted(set(keys.values())))}
    keep = []
    for a in rows:
        until = paused.get(keys.get(a["id"]))
        if a["id"] in keys and until and until > now:
            db.x("UPDATE alerts SET status='skipped', last_error=:e WHERE id=:i", i=a["id"],
                 e=f"Cancelled before sending: the affiliate manager paused this DID until {until:%Y-%m-%d %H:%M} UTC")
            res["cancelled"] = res.get("cancelled", 0) + 1
        else:
            keep.append(a)
    return keep


def process_outbox(limit=25, max_seconds=None):
    """Send pending alerts (oldest first), at most `limit` per run and within `max_seconds`. The rest waits for the next run.
    Every alert is claimed first and every finished recipient is written down (sent_to), so a second sender or a retry after a
    half-failed send can never e-mail the same person the same alert twice."""
    res = {"sent": 0, "dry_run": 0, "failed": 0, "waiting": 0, "left": 0, "busy": 0}
    t0 = time.monotonic()
    stale = db.now_utc() - timedelta(minutes=CLAIM_MINUTES)
    rows = db.q("SELECT * FROM alerts WHERE status='pending' AND (claimed_at IS NULL OR claimed_at < :s) ORDER BY id LIMIT :n", n=limit, s=stale)
    total = db.q1("SELECT COUNT(*) AS n FROM alerts WHERE status='pending' AND (claimed_at IS NULL OR claimed_at < :s)", s=stale)["n"]
    res["left"] = max(total - len(rows), 0)
    dry = is_dry_run()
    rows = _drop_for_retired_dids(rows, res)
    rows = _drop_for_paused_dids(rows, res)
    session = Session()
    try:
        for idx, a in enumerate(rows):
            if max_seconds is not None and time.monotonic() - t0 > max_seconds:
                res["left"] += len(rows) - idx
                break
            if not _claim(a["id"]):
                res["busy"] += 1                    # another sender has this alert right now
                continue
            to = alerts.parse_emails(a["to_emails"]) or alerts.admin_to()   # admin list may have been added later
            cc = alerts.parse_emails(a["cc_emails"])
            if not to:
                # no admin email set yet: keep it pending, do not use up attempts
                db.x("UPDATE alerts SET last_error='No admin email set (Emails page)', claimed_at=NULL WHERE id=:i", i=a["id"])
                res["waiting"] += 1
                continue
            if dry:
                print(f"--- DRY RUN (not sent) ---\nTo: {', '.join(to)}\nCC: {', '.join(cc)}\n"
                      f"Subject: {a['subject']}\n{a['body']}\n", flush=True)
                db.x("UPDATE alerts SET status='dry_run', last_error=NULL, claimed_at=NULL WHERE id=:i", i=a["id"])
                res["dry_run"] += 1
                continue
            done = _sent_set(a)
            try:
                hdr = thread_headers(a)
                if "/?a=" in (a["body"] or ""):
                    # an alert with answer buttons: everybody gets his OWN links, so the app knows who answered
                    for rcpt in dict.fromkeys(to + cc):
                        if rcpt.lower() in done:
                            continue                # this person already got it (an earlier try died half way)
                        t, h = actions.personalize(a["body"], a.get("body_html"), rcpt)
                        smtp_send(to, cc, a["subject"], t, h, session=session, headers=hdr, envelope=[rcpt])
                        done.add(rcpt.lower())
                        db.x("UPDATE alerts SET sent_to=:s WHERE id=:i", s=",".join(sorted(done)), i=a["id"])   # written down at once
                elif "*" not in done:
                    smtp_send(to, cc, a["subject"], a["body"], a.get("body_html"), session=session, headers=hdr)
                    done.add("*")
                db.x("UPDATE alerts SET status='sent', sent_at=:n, attempts=attempts+1, last_error=NULL, sent_to=:s WHERE id=:i",
                     n=db.now_utc(), s=",".join(sorted(done)), i=a["id"])
                res["sent"] += 1
            except Exception as e:
                session.close()                     # next email starts from a fresh connection
                attempts = a["attempts"] + 1
                status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
                db.x("UPDATE alerts SET status=:s, attempts=:a, last_error=:e, claimed_at=NULL, sent_to=:t WHERE id=:i",
                     s=status, a=attempts, e=str(e)[:500], t=",".join(sorted(done)) or None, i=a["id"])
                res["failed"] += 1
                print(f"Email failed for alert {a['id']}: {e}", flush=True)
    finally:
        session.close()
    return res
