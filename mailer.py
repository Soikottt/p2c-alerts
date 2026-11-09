"""Sends the queued alerts. An alert counts as SENT only after Gmail accepts it."""
import smtplib
from email.message import EmailMessage

import alerts
import config
import db

MAX_ATTEMPTS = 5


def is_dry_run():
    if config.DRY_RUN_ENV:
        return True
    return str(db.get_setting("dry_run", "true")).strip().lower() == "true"


def smtp_send(to_list, cc_list, subject, body):
    if not config.SMTP_USER or not config.SMTP_PASSWORD:
        raise RuntimeError("SMTP_USER / SMTP_PASSWORD are missing in your Secrets.")
    msg = EmailMessage()
    msg["From"] = config.SMTP_USER
    msg["To"] = ", ".join(to_list)
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as s:
        s.starttls()
        s.login(config.SMTP_USER, config.SMTP_PASSWORD)
        s.send_message(msg, to_addrs=list(to_list) + list(cc_list))


def send_test(to_list, subject="Test email from your Pay-Per-Call alert tool"):
    """Always really sends (ignores dry-run). Used by the 'Send test email' button."""
    smtp_send(to_list, [], subject, "If you can read this, your email setup works.\n")


def process_outbox(limit=100):
    """Send every pending alert. Returns counts."""
    res = {"sent": 0, "dry_run": 0, "failed": 0, "waiting": 0}
    rows = db.q("SELECT * FROM alerts WHERE status='pending' ORDER BY id LIMIT :n", n=limit)
    dry = is_dry_run()
    for a in rows:
        to = alerts.parse_emails(a["to_emails"]) or alerts.admin_to()   # admin list may have been added later
        cc = alerts.parse_emails(a["cc_emails"])
        if not to:
            # no admin email set yet: keep it pending, do not use up attempts
            db.x("UPDATE alerts SET last_error='No admin email set (Emails & Alerts tab)' WHERE id=:i", i=a["id"])
            res["waiting"] += 1
            continue
        if dry:
            print(f"--- DRY RUN (not sent) ---\nTo: {', '.join(to)}\nCC: {', '.join(cc)}\n"
                  f"Subject: {a['subject']}\n{a['body']}\n")
            db.x("UPDATE alerts SET status='dry_run', last_error=NULL WHERE id=:i", i=a["id"])
            res["dry_run"] += 1
            continue
        try:
            smtp_send(to, cc, a["subject"], a["body"])
            db.x("UPDATE alerts SET status='sent', sent_at=:n, attempts=attempts+1, last_error=NULL WHERE id=:i",
                 n=db.now_utc(), i=a["id"])
            res["sent"] += 1
        except Exception as e:
            attempts = a["attempts"] + 1
            status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
            db.x("UPDATE alerts SET status=:s, attempts=:a, last_error=:e WHERE id=:i",
                 s=status, a=attempts, e=str(e)[:500], i=a["id"])
            res["failed"] += 1
            print(f"Email failed for alert {a['id']}: {e}")
    return res
