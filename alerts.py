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
    return db.memo(("rule", alert_type), lambda: db.q1("SELECT * FROM alert_rules WHERE alert_type=:t", t=alert_type))


# A run must never spend minutes creating a flood of alerts (for example right after the test alerts were deleted):
# at most `max_new` per run and `seconds` of time. What is left is simply created by the next run.
_budget = {"max": None, "created": 0, "deadline": None}


def set_budget(max_new=None, seconds=None):
    import time
    _budget.update(max=max_new, created=0, deadline=(time.monotonic() + seconds) if seconds else None)


def over_budget():
    import time
    return bool((_budget["max"] is not None and _budget["created"] >= _budget["max"])
                or (_budget["deadline"] and time.monotonic() > _budget["deadline"]))


def rule_enabled(alert_type):
    r = get_rule(alert_type)
    return bool(r and r["enabled"])


def admin_to():
    return parse_emails(db.get_setting("admin_emails", ""))


def is_snoozed(key):
    r = db.q1("SELECT snooze_until FROM snoozes WHERE entity_key=:k", k=key)
    return bool(r and db.to_dt(r["snooze_until"]) > db.now_utc())


def _entity_cc(rule, entity_type, entity_id):
    return db.memo(("cc", rule["alert_type"], entity_type, entity_id), lambda: _entity_cc_uncached(rule, entity_type, entity_id))


def _entity_cc_uncached(rule, entity_type, entity_id):
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


def existing_keys(keys):
    """Which of these dedupe keys already have an alert? One question per 500 keys. The rules use it to skip
    everything that was already alerted, instead of rebuilding (and re-checking) the same emails every 5 minutes."""
    keys = list(set(keys))
    found = set()
    for i in range(0, len(keys), 500):
        found |= {r["dedupe_key"] for r in db.q_in("SELECT dedupe_key FROM alerts WHERE dedupe_key IN :k", "k", keys[i:i + 500])}
    return found


def create_alert(alert_type, dedupe_key, ctx=None, publisher_id=None, buyer_id=None, status="pending",
                 snooze_key=None, subject=None, body=None, subject_tag=""):
    """Queue one email. Returns True if it was new (the same dedupe_key is never queued twice).
    The wording comes from the alert's template; ctx fills the {placeholders}."""
    import actions
    import templates
    rule = get_rule(alert_type)
    if not rule or not rule["enabled"]:
        return False
    if status == "pending" and over_budget():
        return False                                   # not recorded: the next run creates it
    if snooze_key and is_snoozed(snooze_key):
        return False
    body_html = None
    if subject is None:
        subj_t, body_t = templates.get_template(alert_type, rule)
        if ctx and ctx.get("next_step") and "{next_step}" not in body_t:
            body_t = body_t + "\n\n{next_step}"        # a template you edited earlier still gets the 'if nobody answers' note
        subject = subject_tag + templates.render(subj_t, ctx)
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
    _budget["created"] += 1
    return True


def delete_alerts(ids, also_resume_pauses=True):
    """Permanently delete alerts (for example test alerts) with their answer buttons and notes.
    The engine can then create a fresh alert for the same DID / cap, because the old one no longer blocks it.
    Returns how many alerts were deleted."""
    ids = sorted({int(i) for i in ids})
    deleted = 0
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        keys = [r["snooze_key"] for r in db.q_in("SELECT DISTINCT snooze_key FROM alerts WHERE id IN :ids AND snooze_key IS NOT NULL",
                                                 "ids", part)]
        db.x_in("DELETE FROM alert_actions WHERE alert_id IN :ids", "ids", part)
        db.x_in("DELETE FROM alert_notes WHERE alert_id IN :ids", "ids", part)
        deleted += db.x_in("DELETE FROM alerts WHERE id IN :ids", "ids", part)
        if also_resume_pauses and keys:
            db.x_in("DELETE FROM snoozes WHERE entity_key IN :ks", "ks", keys)
    return deleted
