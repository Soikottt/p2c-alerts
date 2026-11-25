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
        "ALTER TABLE email_archive ADD COLUMN IF NOT EXISTS body_html TEXT",
        "ALTER TABLE email_archive ADD COLUMN IF NOT EXISTS in_reply_to TEXT",
        "ALTER TABLE email_archive ADD COLUMN IF NOT EXISTS references_header TEXT",
        "ALTER TABLE email_archive ADD COLUMN IF NOT EXISTS to_emails TEXT",
        "ALTER TABLE email_archive ADD COLUMN IF NOT EXISTS cc_emails TEXT",
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
    ]
    for sql in statements:
        db.x(sql)


def audit(event_type, summary, alert_id=None, entity_type='alert', entity_id=None, actor=None, details=None):
    try:
        db.x("""INSERT INTO audit_events(alert_id, entity_type, entity_id, event_type, actor, summary, details, created_at)
               VALUES(:a,:et,:ei,:t,:actor,:s,:d,:n)""",
             a=alert_id, et=entity_type, ei=str(entity_id) if entity_id is not None else None,
             t=event_type, actor=actor, s=str(summary)[:500], d=details, n=db.now_utc())
    except Exception:
        # Audit must never break the primary operation.
        pass


def strict_family_ids(alert_id):
    """Resolve only explicit thread_id relationships; do not merge unrelated alerts merely sharing a DID."""
    aid = int(alert_id)
    seen, frontier = {aid}, [aid]
    while frontier:
        rows = db.q_in("SELECT id FROM alerts WHERE thread_id IN :ids", "ids", frontier)
        frontier = [int(r['id']) for r in rows if int(r['id']) not in seen]
        seen.update(frontier)
    # Walk upward to the root, then collect all descendants from that root.
    root = aid
    for _ in range(30):
        row = db.q1("SELECT thread_id FROM alerts WHERE id=:i", i=root)
        if not row or not row.get('thread_id'):
            break
        nxt = int(row['thread_id'])
        if nxt in seen:
            break
        seen.add(nxt)
        root = nxt
    frontier = [root]
    seen = {root}
    while frontier:
        rows = db.q_in("SELECT id FROM alerts WHERE thread_id IN :ids", "ids", frontier)
        frontier = [int(r['id']) for r in rows if int(r['id']) not in seen]
        seen.update(frontier)
    return root, sorted(seen)


def search_alerts(query, limit=80):
    q = (query or '').strip()
    if not q:
        return []
    like = f"%{q}%"
    numeric = re.sub(r'[^0-9]', '', q)
    exact_id = int(numeric) if numeric and len(numeric) <= 12 else -1
    sql = """SELECT a.id, a.thread_id, a.alert_type, a.subject, a.status, a.case_status,
                     a.created_at, a.sent_at, a.to_emails, a.cc_emails, a.last_error,
                     d.number AS did, COALESCE(p.name, d.affiliate_name, '') AS publisher
              FROM alerts a
              LEFT JOIN dids d ON d.id = CASE WHEN split_part(COALESCE(a.snooze_key,''), ':', 1)='did'
                   AND split_part(COALESCE(a.snooze_key,''), ':', 2) ~ '^[0-9]+$'
                   THEN split_part(a.snooze_key, ':', 2)::INTEGER ELSE NULL END
              LEFT JOIN publishers p ON p.id=d.publisher_id
              WHERE a.id=:exact OR COALESCE(a.thread_id, -1)=:exact
                 OR a.subject ILIKE :like OR COALESCE(a.body,'') ILIKE :like
                 OR COALESCE(a.to_emails,'') ILIKE :like OR COALESCE(a.cc_emails,'') ILIKE :like
                 OR COALESCE(d.number,'') ILIKE :like OR COALESCE(p.name,d.affiliate_name,'') ILIKE :like
              ORDER BY CASE WHEN a.id=:exact THEN 0 WHEN a.thread_id=:exact THEN 1 ELSE 2 END,
                       a.created_at DESC LIMIT :lim"""
    return db.q(sql, exact=exact_id, like=like, lim=int(limit))


def alert_details(alert_id):
    return db.q1("""SELECT a.*, d.number AS did, d.label AS did_label,
                         COALESCE(p.name,d.affiliate_name,'') AS publisher_name,
                         d.status AS did_status
                  FROM alerts a
                  LEFT JOIN dids d ON d.id = CASE WHEN split_part(COALESCE(a.snooze_key,''), ':', 1)='did'
                       AND split_part(COALESCE(a.snooze_key,''), ':', 2) ~ '^[0-9]+$'
                       THEN split_part(a.snooze_key, ':', 2)::INTEGER ELSE NULL END
                  LEFT JOIN publishers p ON p.id=d.publisher_id
                  WHERE a.id=:i""", i=int(alert_id))


def conversation(alert_id):
    """Return a strict thread's alerts, timeline events and archived inbound messages."""
    import chain
    root, ids = strict_family_ids(alert_id)
    if not ids:
        return root, [], []
    alerts = db.q_in("SELECT * FROM alerts WHERE id IN :ids ORDER BY COALESCE(sent_at,created_at) DESC", "ids", ids)
    events = chain.thread_events(ids)
    archived = db.q_in("SELECT * FROM email_archive WHERE alert_id IN :ids ORDER BY received_at DESC", "ids", ids)
    by_alert = {}
    for mail in archived:
        by_alert.setdefault(mail['alert_id'], []).append(mail)
    # Enrich reply events with the original full email archive record.
    for ev in events:
        if ev.get('event') == 'Reply':
            candidates = by_alert.get(ev.get('alert_id'), [])
            if candidates:
                mail = candidates.pop(0)
                ev['email'] = mail
    # Include archived replies even when the old note was not created (e.g. an ignored or legacy import).
    known = {(e.get('alert_id'), e.get('when')) for e in events if e.get('event') == 'Reply'}
    for mail_list in by_alert.values():
        for mail in mail_list:
            events.append({'when': db.to_dt(mail['received_at']), 'alert_id': mail['alert_id'], 'event': 'Reply',
                           'who': mail.get('from_email') or '', 'kind': 'Incoming email',
                           'text': (mail.get('body_text') or '')[:500], 'email': mail})
    events.sort(key=lambda e: (e.get('when') or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
    return root, alerts, events


def email_center_rows(limit=500):
    outgoing = db.q("""SELECT id, thread_id, subject, to_emails AS sender_to, cc_emails, status,
                            created_at AS happened_at, sent_at, last_error, 'outbound' AS direction
                     FROM alerts WHERE alert_type <> 'alert_response' ORDER BY created_at DESC LIMIT :n""", n=limit)
    incoming = db.q("""SELECT id, alert_id, subject, from_email AS sender_to, cc_emails, outcome AS status,
                            received_at AS happened_at, received_at AS sent_at, NULL AS last_error, 'inbound' AS direction
                     FROM email_archive ORDER BY received_at DESC LIMIT :n""", n=limit)
    return sorted(outgoing + incoming, key=lambda r: db.to_dt(r.get('happened_at')) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)[:limit]


def audit_rows(limit=500):
    return db.q("SELECT * FROM audit_events ORDER BY created_at DESC LIMIT :n", n=int(limit))
