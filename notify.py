"""Telegram alerts + daily summary e-mail. Runs inside the 5-minute engine. Never raises out of run()."""
import json
import urllib.parse
import urllib.request
from datetime import timedelta

import config
import db
from timeutil import fmt, tz


# ---------------------------------------------------------------- Telegram
def telegram_configured():
    return bool(config.get_secret("TELEGRAM_BOT_TOKEN") and config.get_secret("TELEGRAM_CHAT_ID"))


def send_telegram(text):
    """Returns (ok, message). Uses only the standard library."""
    token, chat = config.get_secret("TELEGRAM_BOT_TOKEN"), config.get_secret("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return False, "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are missing in your Secrets."
    try:
        data = urllib.parse.urlencode({"chat_id": str(chat), "text": text[:3900], "disable_web_page_preview": "true"}).encode()
        with urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data), timeout=15) as r:
            body = json.loads(r.read().decode("utf-8", "replace"))
        return (True, "sent") if body.get("ok") else (False, str(body.get("description") or body)[:200])
    except Exception as e:
        return False, str(e).replace(str(token), "***")[:200]


def _on(key):
    return str(db.get_setting(key, "true")).strip().lower() == "true"


def _pointer(key, current_max):
    """Last handled id. First time: start from now, so old history does not flood the chat."""
    v = db.get_setting(key, "")
    if v in (None, ""):
        db.set_setting(key, int(current_max))
        return int(current_max)
    return int(float(v))


def _telegram_events(now, say):
    if not telegram_configured():
        return 0
    sent = 0
    lines = []

    def push(icon, text):
        lines.append(f"{icon} {text}")

    if _on("tg_on_failed"):
        mx = (db.q1("SELECT COALESCE(MAX(id),0) AS m FROM alerts") or {}).get("m", 0)
        last = _pointer("tg_last_failed_id", mx)
        for r in db.q("SELECT id, subject, last_error FROM alerts WHERE status='failed' AND id > :i ORDER BY id LIMIT 20", i=last):
            push("🔴", f"E-mail FAILED A{r['id']}: {r['subject'] or ''} ({str(r.get('last_error') or '')[:80]})")
        db.set_setting("tg_last_failed_id", int(mx))
    if _on("tg_on_reply"):
        mx = (db.q1("SELECT COALESCE(MAX(id),0) AS m FROM alert_notes") or {}).get("m", 0)
        last = _pointer("tg_last_note_id", mx)
        for r in db.q("SELECT n.id, n.alert_id, n.author FROM alert_notes n WHERE n.source='email' AND n.id > :i ORDER BY n.id LIMIT 20", i=last):
            push("💬", f"New reply on A{r['alert_id']} from {r.get('author') or 'someone'}")
        db.set_setting("tg_last_note_id", int(mx))
    if _on("tg_on_engine"):
        mx = (db.q1("SELECT COALESCE(MAX(id),0) AS m FROM alerts") or {}).get("m", 0)
        last = _pointer("tg_last_sys_id", mx)
        for r in db.q("SELECT id, alert_type, subject FROM alerts WHERE alert_type IN ('engine_stale','sync_error') AND id > :i ORDER BY id LIMIT 10", i=last):
            push("⚠️", f"{r['subject'] or r['alert_type']}")
        db.set_setting("tg_last_sys_id", int(mx))
    if _on("tg_on_due"):
        for r in db.q("""SELECT id, assigned_to, due_at FROM alerts WHERE due_at IS NOT NULL AND due_at < :n AND due_notified_at IS NULL
                         AND COALESCE(case_status,'open') NOT IN ('done','no_action') ORDER BY due_at LIMIT 20""", n=now):
            push("⏰", f"A{r['id']} is overdue (assigned to {r.get('assigned_to') or 'nobody'}, was due {fmt(r['due_at'])})")
            db.x("UPDATE alerts SET due_notified_at=:n WHERE id=:i", n=now, i=r["id"])
    if lines:
        text = "\n".join(lines[:10]) + (f"\n… and {len(lines) - 10} more" if len(lines) > 10 else "")
        ok, msg = send_telegram("P2C Alert Center\n" + text)
        say(f"Telegram: {len(lines)} event(s) -> {'sent' if ok else 'FAILED: ' + msg}")
        sent = len(lines) if ok else 0
    return sent


# ---------------------------------------------------------------- daily summary
def summary_data(now):
    d = {}
    d["open"] = db.q("""SELECT COALESCE(case_status,'open') AS s, COUNT(*) AS n FROM alerts WHERE alert_type <> 'alert_response'
                        AND COALESCE(case_status,'open') NOT IN ('done','no_action') GROUP BY COALESCE(case_status,'open') ORDER BY n DESC""")
    d["failed"] = (db.q1("SELECT COUNT(*) AS n FROM alerts WHERE status='failed' AND created_at > :t", t=now - timedelta(days=7)) or {}).get("n", 0)
    d["replies"] = (db.q1("SELECT COUNT(*) AS n FROM alert_notes WHERE source='email' AND created_at > :t", t=now - timedelta(hours=24)) or {}).get("n", 0)
    d["did_open"] = (db.q1("""SELECT COUNT(*) AS n FROM alerts WHERE alert_type IN ('did_inactive','did_new_no_calls') AND status='sent'
                              AND COALESCE(case_status,'open') IN ('open','follow_up')""") or {}).get("n", 0)
    d["due"] = db.q("""SELECT COALESCE(assigned_to,'(unassigned)') AS who, COUNT(*) AS n FROM alerts WHERE due_at IS NOT NULL AND due_at < :n
                       AND COALESCE(case_status,'open') NOT IN ('done','no_action') GROUP BY COALESCE(assigned_to,'(unassigned)') ORDER BY n DESC""", n=now)
    try:
        import followups
        d["overdue_fu"] = len([r for r in followups.upcoming(now) if r.get("scheduled") and r.get("next_followup") and r["next_followup"] < now
                               and not (r.get("paused_until") and r["paused_until"] > now)])
    except Exception:
        d["overdue_fu"] = 0
    return d


def summary_text(now):
    d = summary_data(now)
    lines = [f"P2C Alert Center: daily summary ({fmt(now)})", ""]
    lines.append("Open alerts: " + (", ".join(f"{r['s'].replace('_', ' ')} {r['n']}" for r in d["open"]) or "none"))
    lines.append(f"Failed e-mails (7 days): {d['failed']}")
    lines.append(f"Replies in the last 24 hours: {d['replies']}")
    lines.append(f"Overdue follow-ups: {d['overdue_fu']}")
    lines.append(f"DID alerts without an answer: {d['did_open']}")
    if d["due"]:
        lines.append("Overdue assigned work: " + ", ".join(f"{r['who']} {r['n']}" for r in d["due"]))
    url = (db.get_setting("app_url", "") or "").strip()
    if url:
        lines += ["", f"Open the app: {url}"]
    return "\n".join(lines)


def _recipients():
    raw = (db.get_setting("summary_to", "") or db.get_setting("admin_emails", "") or "")
    return [x.strip() for x in raw.replace(";", ",").split(",") if "@" in x]


def send_summary(now, log=print):
    """Send it now (also used by the test button). Returns (ok, message)."""
    import mailer
    text = summary_text(now)
    to = _recipients()
    if not to:
        return False, "No recipient: fill 'Send the daily summary to' in Settings (or the admin e-mails)."
    if mailer.is_dry_run():
        log("DRY RUN summary:\n" + text)
        return True, "DRY RUN is ON: the summary was only printed in the log."
    html_body = "<div style='font-family:Arial,sans-serif;font-size:14px;color:#111'>" + "<br>".join(
        (l.replace("&", "&amp;").replace("<", "&lt;") or "&nbsp;") for l in text.splitlines()) + "</div>"
    mailer.smtp_send(to, [], f"{db.get_setting('subject_prefix', '[P2C Alert]')} Daily summary", text, html=html_body)
    if telegram_configured() and _on("tg_on_summary"):
        send_telegram(text)
    return True, f"Sent to {', '.join(to)}"


def summary_if_due(now, say):
    if str(db.get_setting("summary_on", "false")).lower() != "true":
        return
    local = now.astimezone(tz())
    try:
        hour = int(float(db.get_setting("summary_hour", "9") or 9))
    except Exception:
        hour = 9
    today = local.strftime("%Y-%m-%d")
    if local.hour < hour or db.get_setting("summary_last_date", "") == today:
        return
    db.set_setting("summary_last_date", today)          # first: so a failed send cannot repeat every 5 minutes
    ok, msg = send_summary(now, log=say)
    say(f"daily summary: {msg}")


def run(now, say=print):
    try:
        _telegram_events(now, say)
    except Exception as e:
        say(f"Telegram step failed: {e}")
    try:
        summary_if_due(now, say)
    except Exception as e:
        say(f"daily summary failed: {e}")
