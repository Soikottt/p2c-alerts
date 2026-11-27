
"""P2C Alert Center V2: additive database upgrades, search, thread history and audit helpers.
All changes are additive; existing alert, call, DID and follow-up tables are preserved.
"""
from datetime import datetime, timezone
import re

import db


def ensure_schema():
    """Idempotent PostgreSQL migrations for V2. Safe to run on every app start."""
    statements = [
        """CREATE TABLE IF NOT EXISTS email_archive (
            id BIGSERIAL PRIMARY KEY,
            message_id TEXT NOT NULL UNIQUE,
            alert_id INTEGER,
            direction TEXT NOT NULL DEFAULT 'inbound',
            from_email TEXT,
            to_emails TEXT,
            cc_emails TEXT,
            subject TEXT,
            body_text TEXT,
            body_html TEXT,
            in_reply_to TEXT,
            references_header TEXT,
            outcome TEXT,
            received_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",

        # Additive migrations for existing/legacy email_archive tables.
        "ALTER TABLE email_archive ADD COLUMN IF NOT EXISTS body_html TEXT",
        "ALTER TABLE email_archive ADD COLUMN IF NOT EXISTS in_reply_to TEXT",
        "ALTER TABLE email_archive ADD COLUMN IF NOT EXISTS references_header TEXT",
        "ALTER TABLE email_archive ADD COLUMN IF NOT EXISTS to_emails TEXT",
        "ALTER TABLE email_archive ADD COLUMN IF NOT EXISTS cc_emails TEXT",
        "ALTER TABLE email_archive ADD COLUMN IF NOT EXISTS mail_date TIMESTAMPTZ",
        "ALTER TABLE email_archive ADD COLUMN IF NOT EXISTS delivery_status TEXT",
        "ALTER TABLE email_archive ADD COLUMN IF NOT EXISTS error TEXT",
        "ALTER TABLE email_archive ADD COLUMN IF NOT EXISTS attachments TEXT",

        # IMPORTANT: Add received_at before creating its index.
        "ALTER TABLE email_archive ADD COLUMN IF NOT EXISTS received_at TIMESTAMPTZ NOT NULL DEFAULT NOW()",

        # Indexes are created only after the required columns exist.
        "CREATE INDEX IF NOT EXISTS ix_email_archive_alert_received ON email_archive(alert_id, received_at DESC)",

        """CREATE TABLE IF NOT EXISTS audit_events (
            id BIGSERIAL PRIMARY KEY,
            alert_id INTEGER,
            entity_type TEXT NOT NULL DEFAULT 'alert',
            entity_id TEXT,
            event_type TEXT NOT NULL,
            actor TEXT,
            summary TEXT NOT NULL,
            details TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",

        "CREATE INDEX IF NOT EXISTS ix_audit_events_alert_created ON audit_events(alert_id, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS ix_audit_events_entity ON audit_events(entity_type, entity_id, created_at DESC)",

        # merged audit trail and users (see audit.py / auth.py)
        """CREATE TABLE IF NOT EXISTS audit_log (
            id SERIAL PRIMARY KEY, at TIMESTAMPTZ NOT NULL DEFAULT NOW(), actor TEXT, action TEXT NOT NULL,
            entity_type TEXT, entity_id TEXT, alert_id INTEGER, did_id INTEGER, old_value TEXT, new_value TEXT, detail TEXT)""",
        "CREATE INDEX IF NOT EXISTS ix_audit_at ON audit_log (at)",
        "CREATE INDEX IF NOT EXISTS ix_audit_alert ON audit_log (alert_id)",
        "CREATE INDEX IF NOT EXISTS ix_audit_did ON audit_log (did_id)",
        """CREATE TABLE IF NOT EXISTS app_users (
            id SERIAL PRIMARY KEY, name TEXT UNIQUE NOT NULL, pin_hash TEXT NOT NULL, salt TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user', active BOOLEAN NOT NULL DEFAULT TRUE, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
    ]

    for sql in statements:
        db.x(sql)


def audit(event_type, summary, alert_id=None, entity_type='alert', entity_id=None, actor=None, details=None):
    """Kept for the existing call sites: writes to the ONE audit trail (audit.py: who, action, before / after, alert, DID)."""
    try:
        import audit as _audit
        did_id = int(entity_id) if entity_type == 'did' and str(entity_id).isdigit() else None
        text = str(summary) + (f" | {details}" if details else "")
        _audit.log(event_type, entity_type, entity_id, alert_id=alert_id, did_id=did_id, detail=text, who=actor)
    except Exception:
        pass


def strict_family_ids(alert_id):
    """Only explicit thread_id relationships: the root of this alert and everything that descends from it.
    Alerts that merely share a DID are NOT merged."""
    aid = int(alert_id)
    root, seen_up = aid, {aid}
    for _ in range(30):
        row = db.q1("SELECT thread_id FROM alerts WHERE id=:i", i=root)
        if not row or not row.get('thread_id') or int(row['thread_id']) in seen_up:
            break
        root = int(row['thread_id'])
        seen_up.add(root)
    seen, frontier = {root}, [root]
    while frontier:
        rows = db.q_in("SELECT id FROM alerts WHERE thread_id IN :ids", "ids", frontier)
        frontier = [int(r['id']) for r in rows if int(r['id']) not in seen]
        seen.update(frontier)
    return root, sorted(seen)


_CODE = re.compile(r"^\s*#?\s*A?\s*(\d{1,9})\s*$", re.I)


def _esc_like(s):
    return str(s).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search_alerts(query, limit=80):
    """A325, 325, #A325 -> that alert (and its follow-ups). Anything else: DID number, publisher, buyer, e-mail address,
    subject or e-mail text (alerts AND archived replies)."""
    q = (query or '').strip()
    if not q:
        return []
    m = _CODE.match(q)
    exact_id = int(m.group(1)) if m else -1
    like = f"%{_esc_like(q)}%"
    digits = re.sub(r'[^0-9]', '', q)
    dig_like = f"%{digits}%" if len(digits) >= 5 else "%@@no-digits@@%"
    base = """SELECT a.id, a.thread_id, a.alert_type, a.subject, a.status, a.case_status,
                     a.created_at, a.sent_at, a.to_emails, a.cc_emails, a.last_error,
                     d.number AS did, COALESCE(p.name, d.affiliate_name, '') AS publisher
              FROM alerts a
              LEFT JOIN dids d ON d.id = CASE WHEN split_part(COALESCE(a.snooze_key,''), ':', 1)='did'
                   AND split_part(COALESCE(a.snooze_key,''), ':', 2) ~ '^[0-9]+$'
                   THEN split_part(a.snooze_key, ':', 2)::INTEGER ELSE NULL END
              LEFT JOIN publishers p ON p.id=d.publisher_id """
    if m:
        sql = base + """WHERE a.id=:exact OR a.thread_id=:exact ORDER BY CASE WHEN a.id=:exact THEN 0 ELSE 1 END, a.id LIMIT :lim"""
        return db.q(sql, exact=exact_id, lim=int(limit))
    sql = base + """WHERE a.subject ILIKE :like ESCAPE '\\' OR COALESCE(a.body,'') ILIKE :like ESCAPE '\\'
                 OR COALESCE(a.to_emails,'') ILIKE :like ESCAPE '\\' OR COALESCE(a.cc_emails,'') ILIKE :like ESCAPE '\\'
                 OR COALESCE(d.number,'') ILIKE :like ESCAPE '\\' OR COALESCE(p.name,d.affiliate_name,'') ILIKE :like ESCAPE '\\'
                 OR REGEXP_REPLACE(COALESCE(d.number,''), '[^0-9]', '', 'g') LIKE :dig
                 OR a.id IN (SELECT alert_id FROM email_archive WHERE alert_id IS NOT NULL AND
                             (COALESCE(subject,'') ILIKE :like ESCAPE '\\' OR COALESCE(from_email,'') ILIKE :like ESCAPE '\\'
                              OR COALESCE(to_emails,'') ILIKE :like ESCAPE '\\' OR COALESCE(body_text,'') ILIKE :like ESCAPE '\\'))
              ORDER BY a.created_at DESC LIMIT :lim"""
    return db.q(sql, like=like, dig=dig_like, lim=int(limit))


def alert_details(alert_id):
    return db.q1(
        """SELECT a.*, d.number AS did, d.label AS did_label,
                  COALESCE(p.name,d.affiliate_name,'') AS publisher_name,
                  d.status AS did_status
           FROM alerts a
           LEFT JOIN dids d ON d.id = CASE WHEN split_part(COALESCE(a.snooze_key,''), ':', 1)='did'
                AND split_part(COALESCE(a.snooze_key,''), ':', 2) ~ '^[0-9]+$'
                THEN split_part(a.snooze_key, ':', 2)::INTEGER ELSE NULL END
           LEFT JOIN publishers p ON p.id=d.publisher_id
           WHERE a.id=:i""",
        i=int(alert_id)
    )


def _match_mails_to_replies(events, inbound, window_minutes=45):
    """Give each archived incoming e-mail to the Reply note it created: same alert, the closest time (within the window).
    Old replies (no archive) keep NO e-mail, and an e-mail that matches no note becomes its own entry. Never guesses by order."""
    from datetime import timedelta
    replies = [e for e in events if e.get('event') == 'Reply' and 'email' not in e]
    used, loose = set(), []
    for mail in sorted(inbound, key=lambda m: db.to_dt(m['received_at']) or datetime.min.replace(tzinfo=timezone.utc)):
        at = db.to_dt(mail['received_at'])
        best, best_gap = None, None
        for i, ev in enumerate(replies):
            if i in used or ev.get('alert_id') != mail['alert_id'] or not ev.get('when') or not at:
                continue
            gap = abs((ev['when'] - at).total_seconds())
            if gap <= window_minutes * 60 and (best_gap is None or gap < best_gap):
                best, best_gap = i, gap
        if best is None:
            loose.append(mail)
        else:
            used.add(best)
            replies[best]['email'] = mail
    for mail in loose:
        events.append({'when': db.to_dt(mail['received_at']), 'alert_id': mail['alert_id'], 'event': 'Reply',
                       'who': mail.get('from_email') or '', 'kind': 'Incoming email', 'text': (mail.get('body_text') or '')[:500], 'email': mail})


def conversation(alert_id):
    """Return a strict thread's alerts, timeline events (newest first) and the archived incoming messages."""
    import chain
    root, ids = strict_family_ids(alert_id)
    if not ids:
        return root, [], []
    alerts = db.q_in("SELECT * FROM alerts WHERE id IN :ids ORDER BY COALESCE(sent_at,created_at) DESC", "ids", ids)
    events = chain.thread_events(ids)
    inbound = db.q_in("SELECT * FROM email_archive WHERE alert_id IN :ids AND direction IN ('inbound','in') ORDER BY received_at DESC", "ids", ids)
    _match_mails_to_replies(events, inbound)
    for ev in events:                                          # status changes / pauses / retirements recorded in the audit trail
        pass
    try:
        for r in db.q_in("SELECT at, actor, action, detail, old_value, new_value, alert_id FROM audit_log WHERE alert_id IN :ids "
                         "AND action NOT IN ('note_added','status_changed','status_change') ORDER BY id DESC LIMIT 200", "ids", ids):
            events.append({'when': db.to_dt(r['at']), 'alert_id': r['alert_id'], 'event': 'Status', 'who': r['actor'] or '',
                           'kind': str(r['action']).replace('_', ' ').capitalize(),
                           'text': (r['detail'] or '') + (f" ({r['old_value']} → {r['new_value']})" if r['old_value'] or r['new_value'] else '')})
        for r in db.q_in("SELECT at, actor, old_value, new_value, alert_id FROM audit_log WHERE alert_id IN :ids "
                         "AND action IN ('status_changed','status_change') ORDER BY id DESC LIMIT 200", "ids", ids):
            events.append({'when': db.to_dt(r['at']), 'alert_id': r['alert_id'], 'event': 'Status', 'who': r['actor'] or '',
                           'kind': 'Status change', 'text': f"{r['old_value'] or ''} → {r['new_value'] or ''}".strip(' →')})
    except Exception:
        pass
    events.sort(key=lambda e: (e.get('when') or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
    return root, alerts, events


def email_center_rows(limit=500):
    outgoing = db.q(
        """SELECT id, thread_id, subject, to_emails AS sender_to, cc_emails, status,
                  created_at AS happened_at, sent_at, last_error,
                  'outbound' AS direction
           FROM alerts
           WHERE alert_type <> 'alert_response'
           ORDER BY created_at DESC
           LIMIT :n""",
        n=limit
    )

    incoming = db.q(
        """SELECT id, alert_id, subject, from_email AS sender_to, cc_emails,
                  outcome AS status,
                  received_at AS happened_at,
                  received_at AS sent_at,
                  NULL AS last_error,
                  'inbound' AS direction
           FROM email_archive
           WHERE direction IN ('inbound','in')
           ORDER BY received_at DESC
           LIMIT :n""",
        n=limit
    )

    return sorted(
        outgoing + incoming,
        key=lambda r: (
            db.to_dt(r.get('happened_at'))
            or datetime.min.replace(tzinfo=timezone.utc)
        ),
        reverse=True
    )[:limit]


def audit_rows(limit=500):
    return db.q(
        "SELECT * FROM audit_events ORDER BY created_at DESC LIMIT :n",
        n=int(limit)
    )
