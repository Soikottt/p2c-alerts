"""Reads replies that affiliate managers send to an alert email and saves them as follow-up notes.

How it finds the alert: every alert subject ends with a tag like  [#A123]. A reply keeps that tag.
It reads the same mailbox the alerts are sent from (SMTP_USER) over IMAP. Only replies from people who were
on the alert's To / CC list are accepted."""
import email
import hashlib
import imaplib
import re
import time
from datetime import timedelta
from email.header import decode_header, make_header
from email.utils import parseaddr

import alerts
import cases
import config
import db

TAG = re.compile(r"\[#A(\d+)\]")


def imap_host():
    h = (db.get_setting("imap_host", "") or config.get_secret("IMAP_HOST") or "").strip()
    if h:
        return h
    smtp = (config.SMTP_HOST or "").lower()
    if "office365" in smtp or "outlook" in smtp:
        return "outlook.office365.com"
    return smtp.replace("smtp.", "imap.", 1) if smtp.startswith("smtp.") else smtp


def clean_reply(text):
    """Keep only what the person wrote, not the quoted original."""
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    t = re.split(r"\n\s*On [^\n]{0,200}(?:\n[^\n]{0,200})?wrote:", "\n" + t, maxsplit=1)[0]
    t = re.split(r"\n\s*-{2,}\s*Original Message\s*-{2,}|\n\s*_{10,}|\n\s*From:\s[^\n]+\n\s*Sent:", t, maxsplit=1, flags=re.I)[0]
    lines = [ln for ln in t.split("\n") if not ln.strip().startswith(">")]
    return "\n".join(lines).strip()[:1500]


def _text_of(msg):
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition", "")):
                return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="replace")
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                html = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="replace")
                return re.sub(r"<[^>]+>", " ", html)
        return ""
    payload = msg.get_payload(decode=True)
    return payload.decode(msg.get_content_charset() or "utf-8", errors="replace") if payload else ""


def _headers_batch(M, ids):
    """One round trip: Subject / From / Message-ID of many messages. Returns {id: email.message.Message (headers only)}."""
    out = {}
    if not ids:
        return out
    typ, data = M.fetch(b",".join(ids), "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM MESSAGE-ID)])")
    for item in data or []:
        if isinstance(item, tuple) and len(item) >= 2:
            num = item[0].split()[0]
            out[num] = email.message_from_bytes(item[1])
    return out


def sync_replies(days=14, scan=300, max_seconds=45):
    """Returns {'saved': n, 'ignored': n, 'checked': n}. Raises on login problems.
    It looks at the headers of the recent messages itself and picks the ones whose subject has an alert tag [#A123]
    (asking the mail server to search for the tag is unreliable: Gmail only matches whole words)."""
    if not config.SMTP_USER or not config.SMTP_PASSWORD:
        raise RuntimeError("SMTP_USER / SMTP_PASSWORD are missing in your Secrets.")
    res = {"checked": 0, "saved": 0, "ignored": 0}
    t0 = time.monotonic()
    M = imaplib.IMAP4_SSL(imap_host(), 993, timeout=20)
    try:
        M.login(config.SMTP_USER, config.SMTP_PASSWORD)
        M.select("INBOX", readonly=True)
        since = (db.now_utc() - timedelta(days=days)).strftime("%d-%b-%Y")
        typ, data = M.search(None, "SINCE", since)
        ids = (data[0] or b"").split()[-scan:]
        heads = _headers_batch(M, ids)
        for num in ids:
            if time.monotonic() - t0 > max_seconds:
                break                                   # the rest is read on the next run
            head = heads.get(num)
            if head is None:
                continue
            subj = str(make_header(decode_header(head.get("Subject", ""))))
            m = TAG.search(subj)
            if not m:
                continue                                # not a reply to one of our alerts
            mid = (head.get("Message-ID") or "").strip()
            if not mid:
                mid = hashlib.sha1((subj + str(head.get("From", ""))).encode()).hexdigest()
            if db.q1("SELECT 1 AS x FROM inbound_emails WHERE message_id=:m", m=mid):
                continue
            typ, md = M.fetch(num, "(BODY.PEEK[])")
            raw = md[0][1] if md and md[0] else b""
            if not raw:
                continue
            msg = email.message_from_bytes(raw)
            res["checked"] += 1
            name, addr = parseaddr(msg.get("From", ""))
            addr = (addr or "").lower()
            outcome, aid = "ignored", int(m.group(1))
            if addr and addr != (config.SMTP_USER or "").lower():
                a = db.q1("SELECT id, subject, to_emails, cc_emails FROM alerts WHERE id=:i", i=aid)
                allowed = {e.lower() for e in alerts.parse_emails(a["to_emails"]) + alerts.parse_emails(a["cc_emails"])} if a else set()
                text = clean_reply(_text_of(msg))
                if a and addr in allowed and text:
                    who = str(make_header(decode_header(name))) if name else addr
                    cases.add_note(aid, "email", who, text, cases.mark_follow_up_if_open(aid))
                    alerts.create_alert("alert_response", f"resp-mail:{aid}:{hashlib.sha1(mid.encode()).hexdigest()[:10]}",
                                        {"responder": who, "choice": "Replied by email", "note": text[:400],
                                         "original_subject": TAG.sub("", a["subject"]).strip(),
                                         "effect_text": "Saved as a follow-up note on the alert."})
                    outcome = "saved"
            db.x("INSERT INTO inbound_emails (message_id, alert_id, from_email, outcome, received_at) VALUES (:m,:a,:f,:o,:n)",
                 m=mid, a=aid, f=addr, o=outcome, n=db.now_utc())
            res["saved" if outcome == "saved" else "ignored"] += 1
    finally:
        try:
            M.logout()
        except Exception:
            pass
    return res


def sync_if_enabled():
    """Called by the engine. Never raises: a mailbox problem must not stop alerts."""
    if str(db.get_setting("read_replies", "true")).lower() != "true" or not (config.SMTP_USER and config.SMTP_PASSWORD):
        return None
    now = db.now_utc()
    nxt = db.get_setting("inbox_next_run", "")
    if nxt and now < db.to_dt(nxt):
        return None                      # a recent login failure: wait, so the mailbox is not hammered with bad logins
    try:
        res = sync_replies()
        db.set_setting("inbox_last_ok", now.isoformat())
        db.set_setting("inbox_last_error", "")
        db.set_setting("inbox_next_run", "")
        return res
    except Exception as e:
        db.set_setting("inbox_last_error", f"{now:%Y-%m-%d %H:%M} UTC: {str(e)[:250]}")
        db.set_setting("inbox_next_run", (now + timedelta(minutes=30)).isoformat())
        return {"error": str(e)[:250]}
