"""Sends the queued alerts. An alert counts as SENT only after Gmail accepts it."""
import contextlib
import smtplib
import time
from email.message import EmailMessage
from email.utils import formataddr

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


def _build(to_list, cc_list, subject, body, html=None, attachments=None):
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


def smtp_send(to_list, cc_list, subject, body, html=None, attachments=None, session=None):
    if not config.SMTP_USER or not config.SMTP_PASSWORD:
        raise RuntimeError("SMTP_USER / SMTP_PASSWORD are missing in your Secrets.")
    msg, addr, recipients = _build(to_list, cc_list, subject, body, html, attachments)
    if session is not None:
        session.get().send_message(msg, from_addr=addr, to_addrs=recipients)
        return
    with contextlib.ExitStack() as stack:
        _open(stack).send_message(msg, from_addr=addr, to_addrs=recipients)


def send_test(to_list, subject="Test email from your Pay-Per-Call alert tool"):
    """Always really sends (ignores dry-run). Used by the 'Send test email' button."""
    smtp_send(to_list, [], subject, "If you can read this, your email setup works.\n")


def process_outbox(limit=25, max_seconds=None):
    """Send pending alerts (oldest first), at most `limit` per run and within `max_seconds`. The rest waits for the next run."""
    res = {"sent": 0, "dry_run": 0, "failed": 0, "waiting": 0, "left": 0}
    t0 = time.monotonic()
    rows = db.q("SELECT * FROM alerts WHERE status='pending' ORDER BY id LIMIT :n", n=limit)
    total = db.q1("SELECT COUNT(*) AS n FROM alerts WHERE status='pending'")["n"]
    res["left"] = max(total - len(rows), 0)
    dry = is_dry_run()
    session = Session()
    try:
        for idx, a in enumerate(rows):
            if max_seconds is not None and time.monotonic() - t0 > max_seconds:
                res["left"] += len(rows) - idx
                break
            to = alerts.parse_emails(a["to_emails"]) or alerts.admin_to()   # admin list may have been added later
            cc = alerts.parse_emails(a["cc_emails"])
            if not to:
                # no admin email set yet: keep it pending, do not use up attempts
                db.x("UPDATE alerts SET last_error='No admin email set (Emails page)' WHERE id=:i", i=a["id"])
                res["waiting"] += 1
                continue
            if dry:
                print(f"--- DRY RUN (not sent) ---\nTo: {', '.join(to)}\nCC: {', '.join(cc)}\n"
                      f"Subject: {a['subject']}\n{a['body']}\n", flush=True)
                db.x("UPDATE alerts SET status='dry_run', last_error=NULL WHERE id=:i", i=a["id"])
                res["dry_run"] += 1
                continue
            try:
                smtp_send(to, cc, a["subject"], a["body"], a.get("body_html"), session=session)
                db.x("UPDATE alerts SET status='sent', sent_at=:n, attempts=attempts+1, last_error=NULL WHERE id=:i",
                     n=db.now_utc(), i=a["id"])
                res["sent"] += 1
            except Exception as e:
                session.close()                     # next email starts from a fresh connection
                attempts = a["attempts"] + 1
                status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
                db.x("UPDATE alerts SET status=:s, attempts=:a, last_error=:e WHERE id=:i",
                     s=status, a=attempts, e=str(e)[:500], i=a["id"])
                res["failed"] += 1
                print(f"Email failed for alert {a['id']}: {e}", flush=True)
    finally:
        session.close()
    return res
