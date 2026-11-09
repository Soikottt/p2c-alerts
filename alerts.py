"""Creates alert records (the 'outbox') and decides who gets To / CC."""
import re

import db
from timeutil import fmt  # noqa: F401  (re-exported for rules)

EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")


def parse_emails(s):
    """'a@x.com, b@y.com; c@z.com' -> ['a@x.com','b@y.com','c@z.com'] (no duplicates)."""
    out, seen = [], set()
    for part in re.split(r"[,;\s]+", s or ""):
        p = part.strip()
        if p and EMAIL_RE.match(p) and p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return out


def get_rule(alert_type):
    return db.q1("SELECT * FROM alert_rules WHERE alert_type=:t", t=alert_type)


def rule_enabled(alert_type):
    r = get_rule(alert_type)
    return bool(r and r["enabled"])


def admin_to():
    return parse_emails(db.get_setting("admin_emails", ""))


def _entity_cc(rule, entity_type, entity_id):
    cc = []
    if entity_type == "publisher":
        r = db.q1("""SELECT p.publisher_email, p.cc_publisher, p.extra_cc,
                            m.email AS manager_email, m.active AS manager_active
                     FROM publishers p LEFT JOIN affiliate_managers m ON m.id = p.manager_id
                     WHERE p.id=:i""", i=entity_id)
        if r:
            if rule["cc_manager"] and r["manager_email"] and r["manager_active"]:
                cc += parse_emails(r["manager_email"])
            if rule["cc_entity_email"] and r["cc_publisher"] and r["publisher_email"]:
                cc += parse_emails(r["publisher_email"])
            if rule["cc_entity_extra"]:
                cc += parse_emails(r["extra_cc"])
    elif entity_type == "buyer":
        r = db.q1("SELECT buyer_email, cc_buyer, extra_cc FROM buyers WHERE id=:i", i=entity_id)
        if r:
            if rule["cc_entity_email"] and r["cc_buyer"] and r["buyer_email"]:
                cc += parse_emails(r["buyer_email"])
            if rule["cc_entity_extra"]:
                cc += parse_emails(r["extra_cc"])
    return cc


def build_recipients(rule, publisher_id=None, buyer_id=None):
    to = admin_to()
    cc = parse_emails(rule.get("extra_cc"))
    if publisher_id:
        cc += _entity_cc(rule, "publisher", publisher_id)
    if buyer_id:
        cc += _entity_cc(rule, "buyer", buyer_id)
    seen = {e.lower() for e in to}
    final = []
    for e in cc:
        if e.lower() not in seen:
            seen.add(e.lower())
            final.append(e)
    return to, final


def create_alert(alert_type, dedupe_key, subject, body, publisher_id=None, buyer_id=None, status="pending"):
    """Queue one email. Returns True if it was new (same dedupe_key is never queued twice)."""
    rule = get_rule(alert_type)
    if not rule or not rule["enabled"]:
        return False
    to, cc = build_recipients(rule, publisher_id, buyer_id)
    prefix = db.get_setting("subject_prefix", "[P2C Alert]")
    n = db.x("""INSERT INTO alerts (alert_type, dedupe_key, subject, body, to_emails, cc_emails, status, attempts, created_at)
                VALUES (:t,:k,:s,:b,:to,:cc,:st,0,:now) ON CONFLICT (dedupe_key) DO NOTHING""",
             t=alert_type, k=dedupe_key, s=f"{prefix} {subject}".strip(), b=body,
             to=", ".join(to), cc=", ".join(cc), st=status, now=db.now_utc())
    return n == 1
