"""Creates alert records (the 'outbox') and decides who gets To / CC."""
import re

import db

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


def is_snoozed(key):
    r = db.q1("SELECT snooze_until FROM snoozes WHERE entity_key=:k", k=key)
    return bool(r and db.to_dt(r["snooze_until"]) > db.now_utc())


def _entity_cc(rule, entity_type, entity_id):
    cc = []
    if entity_type == "publisher":
        if rule["cc_manager"]:
            for m in db.q("""SELECT m.email FROM publisher_managers pm JOIN affiliate_managers m ON m.id = pm.manager_id
                             WHERE pm.publisher_id=:i AND m.active = TRUE ORDER BY m.name""", i=entity_id):
                cc += parse_emails(m["email"])
        r = db.q1("SELECT publisher_email, cc_publisher, extra_cc FROM publishers WHERE id=:i", i=entity_id)
        if r:
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


def create_alert(alert_type, dedupe_key, ctx=None, publisher_id=None, buyer_id=None, status="pending",
                 snooze_key=None, subject=None, body=None):
    """Queue one email. Returns True if it was new (the same dedupe_key is never queued twice).
    The wording comes from the alert's template; ctx fills the {placeholders}."""
    import actions
    import templates
    rule = get_rule(alert_type)
    if not rule or not rule["enabled"]:
        return False
    if snooze_key and is_snoozed(snooze_key):
        return False
    body_html = None
    if subject is None:
        subj_t, body_t = templates.get_template(alert_type, rule)
        subject = templates.render(subj_t, ctx)
        body, body_html = templates.render_body(body_t, ctx)      # plain text + formatted HTML version
    to, cc = build_recipients(rule, publisher_id, buyer_id)
    prefix = db.get_setting("subject_prefix", "[P2C Alert]")
    rows = db.q("""INSERT INTO alerts (alert_type, dedupe_key, subject, body, to_emails, cc_emails, status, attempts,
                                       created_at, snooze_key)
                   VALUES (:t,:k,:s,:b,:to,:cc,:st,0,:now,:sk) ON CONFLICT (dedupe_key) DO NOTHING RETURNING id""",
                t=alert_type, k=dedupe_key, s=f"{prefix} {subject}".strip(), b=body, to=", ".join(to),
                cc=", ".join(cc), st=status, now=db.now_utc(), sk=snooze_key)
    if not rows:
        return False
    if status == "skipped":
        return True
    alert_id = rows[0]["id"]
    full_subject = f"{prefix} {subject}".strip() + f" [#A{alert_id}]"     # replies keep this tag, so they can be matched
    text, html = (actions.attach(alert_id, alert_type, body, body_html) if status == "pending" else (body, body_html))
    db.x("UPDATE alerts SET subject=:s, body=:b, body_html=:h WHERE id=:i", s=full_subject, b=text, h=html, i=alert_id)
    return True
