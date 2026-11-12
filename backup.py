"""A complete copy of your decisions and history as CSV files in one zip.
Download it any time (Alert Log page) or get it by email every N days (Settings)."""
import csv
import io
import zipfile
from datetime import timedelta

import alerts
import config
import db

TABLES = [
    ("alerts", "SELECT id, created_at, alert_type, subject, body, to_emails, cc_emails, status, sent_at, response_label, "
               "responded_by, responded_at, response_note, case_status, case_updated_at, snooze_key FROM alerts ORDER BY id"),
    ("alert_notes", "SELECT id, alert_id, created_at, source, author, text, status_after FROM alert_notes ORDER BY id"),
    ("dids", "SELECT d.id, d.number, d.label, d.status, d.monthly_cost, d.notes, d.added_at, d.last_call_at, d.next_charge_at, "
             "d.affiliate_name, d.campaign_id, d.ringba_number_id FROM dids d ORDER BY d.number"),
    ("publishers", "SELECT id, name, ringba_publisher_id, status, publisher_email, cc_publisher, extra_cc, notes FROM publishers ORDER BY name"),
    ("buyers", "SELECT id, name, ringba_buyer_id, status, watchlist, buyer_email, cc_buyer, extra_cc, notes FROM buyers ORDER BY name"),
    ("affiliate_managers", "SELECT id, name, email, active FROM affiliate_managers ORDER BY name"),
    ("publisher_managers", "SELECT publisher_id, manager_id FROM publisher_managers"),
    ("caps", "SELECT id, entity_type, entity_id, period, metric, cap_value, thresholds, active, notes FROM caps ORDER BY id"),
    ("email_templates", "SELECT id, alert_type, name, subject, body FROM email_templates ORDER BY id"),
    ("alert_action_defs", "SELECT id, alert_type, label, effect, effect_days, active, sort_order FROM alert_action_defs ORDER BY id"),
]


def build_zip():
    """Returns (filename, bytes)."""
    now = db.now_utc()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, sql in TABLES:
            rows = db.q(sql)
            out = io.StringIO()
            if rows:
                w = csv.DictWriter(out, fieldnames=list(rows[0].keys()))
                w.writeheader()
                for r in rows:
                    w.writerow({k: ("" if v is None else v) for k, v in r.items()})
            z.writestr(f"{name}.csv", out.getvalue())
        z.writestr("README.txt", f"Backup made {now:%Y-%m-%d %H:%M} UTC.\n"
                                 "alerts.csv = every alert ever created, with answers and case status.\n"
                                 "alert_notes.csv = follow-up / done notes (source: answer, email, team).\n")
    return f"p2c-backup-{now:%Y-%m-%d}.zip", buf.getvalue()


def send_if_due(now=None, force=False):
    """Email the backup to the admin addresses every N days. Returns 'sent', 'dry_run', or None if not due."""
    import mailer
    now = now or db.now_utc()
    days = int(float(db.get_setting("backup_email_days", "7") or 0))
    if days <= 0 and not force:
        return None
    nxt = db.get_setting("backup_next_run", "")
    if not force:
        if not nxt:                                           # first time: schedule, do not send yet
            db.set_setting("backup_next_run", (now + timedelta(days=days)).isoformat())
            return None
        if now < db.to_dt(nxt):
            return None
    to = alerts.admin_to()
    if not to:
        return None
    name, data = build_zip()
    body = "Your Pay-Per-Call backup is attached (alerts, follow-up notes, DIDs, publishers, buyers, caps, templates).\n"
    if mailer.is_dry_run():
        out = "dry_run"
    else:
        mailer.smtp_send(to, [], f"{db.get_setting('subject_prefix', '')} Backup {now:%Y-%m-%d}".strip(), body,
                         attachments=[(name, data, "application", "zip")])
        out = "sent"
    db.set_setting("backup_next_run", (now + timedelta(days=max(days, 1))).isoformat())
    return out
