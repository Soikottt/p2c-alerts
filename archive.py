"""Full e-mail archive in ONE table (email_archive): every incoming reply and every outgoing alert e-mail, complete
(text + HTML + headers + attachment info + delivery status). Never raises: a problem here must never stop sending or reading mail.
direction is 'inbound' or 'outbound' (older rows from an earlier version may say 'in' / 'out': IN_DIRS / OUT_DIRS cover both)."""
import json
import re
from email.header import decode_header, make_header
from email.utils import getaddresses, parsedate_to_datetime

import db

MAX_BODY = 1_000_000
IN_DIRS = ("inbound", "in")
OUT_DIRS = ("outbound", "out")


def _dec(v):
    try:
        return str(make_header(decode_header(v or "")))
    except Exception:
        return str(v or "")


def _addrs(v):
    return ", ".join(a for _, a in getaddresses([v or ""]) if a)


def _utc(s):
    try:
        d = parsedate_to_datetime(s)
        if d.tzinfo is None:
            from datetime import timezone
            d = d.replace(tzinfo=timezone.utc)
        return d
    except Exception:
        return None


def clean_text(s):
    """PostgreSQL cannot store a NUL character; some e-mails contain one."""
    return str(s or "").replace("\x00", "")


def out_message_id(alert_id):
    """The same Message-ID the mailer puts on the e-mail (replies point to it)."""
    try:
        import mailer
        _, addr = mailer.sender()
        domain = (addr or "localhost").split("@")[-1]
    except Exception:
        domain = "localhost"
    return f"<p2c-alert-{alert_id}@{domain}>"


def record_out(a, status, error=None, message_id=None):
    """One row per alert e-mail. Called when it is sent, fails or is a dry run; later calls update the delivery status."""
    try:
        mid = message_id or out_message_id(a["id"])
        when = a.get("sent_at") or a.get("created_at") or db.now_utc()
        db.x("INSERT INTO email_archive (message_id, direction, alert_id, subject, from_email, to_emails, cc_emails, body_text, body_html, "
             "received_at, mail_date, delivery_status, error) "
             "VALUES (:m,'outbound',:al,:s,:f,:t,:c,:bt,:bh,:dt,:dt,:st,:er) "
             "ON CONFLICT (message_id) DO UPDATE SET delivery_status=:st, error=:er, direction='outbound', "
             "to_emails=COALESCE(:t, email_archive.to_emails), cc_emails=COALESCE(:c, email_archive.cc_emails)",
             m=mid, al=a["id"], s=a.get("subject"), f=a.get("from_email"), t=a.get("to_emails"), c=a.get("cc_emails"),
             bt=clean_text(a.get("body")), bh=clean_text(a.get("body_html")) or None, dt=when,
             st=status, er=(str(error)[:500] if error else None))
        return True
    except Exception:
        return False


def parse_message(msg):
    """(text, html, [attachment info]) of a full e-mail message, whatever its structure."""
    text, html, atts = [], [], []
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        fn = _dec(part.get_filename()) if part.get_filename() else ""
        disp = str(part.get("Content-Disposition") or "").lower()
        if fn or "attachment" in disp:
            payload = part.get_payload(decode=True) or b""
            atts.append({"name": fn or "(no name)", "type": ctype, "size": len(payload)})
            continue
        if ctype in ("text/plain", "text/html"):
            raw = part.get_payload(decode=True) or b""
            cs = part.get_content_charset() or "utf-8"
            try:
                s = raw.decode(cs, errors="replace")
            except LookupError:
                s = raw.decode("utf-8", errors="replace")
            (text if ctype == "text/plain" else html).append(s)
    t = "\n".join(text).strip()
    h = "\n".join(html).strip()
    if not t and h:
        t = re.sub(r"\s+\n", "\n", re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(script|style).*?</\1>", "", h)))
        t = re.sub(r"[ \t]+", " ", t).strip()
    return clean_text(t)[:MAX_BODY], clean_text(h)[:MAX_BODY], atts


def record_in(msg, mid, alert_id, outcome, received=None):
    """Store a complete incoming e-mail. `msg` is an email.message.Message (full). Returns True / False, never raises."""
    try:
        t, h, atts = parse_message(msg)
        db.x("INSERT INTO email_archive (message_id, direction, alert_id, subject, from_email, to_emails, cc_emails, body_text, body_html, "
             "in_reply_to, references_header, mail_date, delivery_status, attachments, outcome, received_at) "
             "VALUES (:m,'inbound',:al,:s,:f,:t,:c,:bt,:bh,:irt,:rf,:dt,'received',:at,:o,:rc) "
             "ON CONFLICT (message_id) DO UPDATE SET outcome=:o, alert_id=:al",
             m=mid, al=alert_id, s=clean_text(_dec(msg.get("Subject"))), f=_addrs(msg.get("From")), t=_addrs(msg.get("To")), c=_addrs(msg.get("Cc")),
             bt=t, bh=h, irt=clean_text((msg.get("In-Reply-To") or "").strip()) or None, rf=clean_text((msg.get("References") or "").strip()) or None,
             dt=_utc(msg.get("Date")), at=json.dumps(atts) if atts else None, o=outcome, rc=received or db.now_utc())
        return True
    except Exception:
        return False


def for_alert(alert_id):
    return db.q("SELECT * FROM email_archive WHERE alert_id=:a ORDER BY received_at DESC, id DESC", a=alert_id)


def backfill(batch=500):
    """One-time (safe to repeat): copy every alert e-mail that was already sent into the archive. Old replies cannot be recovered."""
    if db.get_setting("archive_backfilled", "") == "1":
        return 0
    n = 0
    last = 0
    have = {r["alert_id"] for r in db.q("SELECT alert_id FROM email_archive WHERE direction IN ('outbound','out') AND alert_id IS NOT NULL")}
    while True:
        rows = db.q("SELECT id, subject, to_emails, cc_emails, body, body_html, status, last_error, sent_at, created_at FROM alerts "
                    "WHERE id > :l ORDER BY id LIMIT :n", l=last, n=batch)
        if not rows:
            break
        for a in rows:
            last = a["id"]
            if a["id"] in have or a["status"] not in ("sent", "failed", "dry_run", "pending"):
                continue
            if record_out(a, a["status"], a.get("last_error")):
                n += 1
    db.set_setting("archive_backfilled", "1")
    return n
