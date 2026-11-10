"""Creates / upgrades the database. Safe to run again at any time.

Run from a terminal:   python setup_db.py
Or press the button in the app's first-run screen.
"""
import os

import db
from db import SETTING_DEFAULTS

HERE = os.path.dirname(os.path.abspath(__file__))

RULE_SEEDS = [
    # alert_type, label, enabled, days, count, cc_manager, cc_entity_email, cc_entity_extra, description
    ("new_publisher", "New publisher call", True, None, 10, True, True, True,
     "One separate email for each of the first N calls from a publisher. N = 'Count'."),
    ("lapsed_publisher", "Publisher returned after silence", True, 30, None, True, True, True,
     "A publisher sends a call after not calling for N days. N = 'Days'."),
    ("watchlist_buyer", "Watchlist buyer connected", True, None, None, False, True, True,
     "A call connected to a buyer that has Watchlist ticked on the Buyers tab."),
    ("publisher_cap", "Publisher cap warning", True, None, None, True, True, True,
     "Publisher reached a % of its cap. Percentages are set per cap on the Caps tab."),
    ("buyer_cap", "Buyer cap warning", True, None, None, False, True, True,
     "Buyer reached a % of its cap. Percentages are set per cap on the Caps tab."),
    ("did_inactive", "Inactive DID", True, 30, None, True, False, True,
     "A DID had no calls for N days (counted from its last call, or from the day it was added). N = 'Days'."),
    ("engine_stale", "Alert engine was offline", True, None, 45, False, False, False,
     "The 5-minute engine did not run for N minutes. N = 'Count' (minutes)."),
    ("sync_error", "Ringba sync failed", True, None, None, False, False, False,
     "The engine could not read call data from Ringba."),
    ("alert_response", "Someone answered an alert email", True, None, None, False, False, False,
     "Sent to you when someone clicks an answer button in an alert email."),
]

# Answer buttons offered inside each alert email: (alert_type, label, effect, days)
ACTION_SEEDS = [
    ("did_inactive", "Remove this DID", "record", None),
    ("did_inactive", "Keep it - wait 7 more days", "snooze", 7),
    ("did_inactive", "I will contact the publisher first", "snooze", 3),
    ("lapsed_publisher", "Publisher is restarting - all good", "record", None),
    ("lapsed_publisher", "Please investigate this publisher", "record", None),
    ("new_publisher", "Approved - looks fine", "record", None),
    ("new_publisher", "Suspicious - please investigate", "record", None),
    ("publisher_cap", "I will contact the publisher", "snooze", 1),
    ("publisher_cap", "Pause this publisher", "record", None),
    ("buyer_cap", "I will contact the buyer", "snooze", 1),
    ("buyer_cap", "Cap will be increased in Ringba", "record", None),
]

# Columns added after the first release (adding twice is harmless: the error is ignored)
ADD_COLUMNS = [
    ("alert_rules", "template_id", "INTEGER"),
    ("alerts", "body_html", "TEXT"),
    ("alerts", "snooze_key", "TEXT"),
    ("alerts", "response_label", "TEXT"),
    ("alerts", "responded_at", "TIMESTAMPTZ"),
    ("alerts", "responded_by", "TEXT"),
    ("alerts", "response_note", "TEXT"),
    ("dids", "publisher_id", "INTEGER"),
    ("dids", "affiliate_name", "TEXT"),
    ("dids", "next_charge_at", "TIMESTAMPTZ"),
]

# The first wording of the inactive-DID email. Saved templates that still have EXACTLY this text are upgraded.
OLD_DID_BODY = ("DID {did}{label_part} has had no calls for {idle_days} days.\nCreated / added: {added}\n"
                "Last call: {last_call}\nMonthly cost: {monthly_cost}\n\n"
                "Decision needed: release this DID to avoid the monthly charge, or keep it for now?")

OLD_TABLES = ["calls", "dids", "cap_rules", "cc_routing", "alerts_log", "publishers", "buyers"]


def _split_sql(text_):
    return [s.strip() for s in text_.split(";") if s.strip() and not all(
        ln.strip().startswith("--") or not ln.strip() for ln in s.splitlines())]


def _old_layout_present():
    rows = db.q("SELECT column_name FROM information_schema.columns "
                "WHERE table_name='calls' AND column_name='ringba_call_id'")
    return bool(rows)


def _rename_old_tables(log):
    for t in OLD_TABLES:
        exists = db.q("SELECT 1 AS x FROM information_schema.tables WHERE table_name=:t "
                      "AND table_schema=current_schema()", t=t)
        if exists:
            db.x(f"ALTER TABLE {t} RENAME TO old_{t}")
            log(f"  renamed old table {t} -> old_{t} (kept as a backup)")


def _copy_old_data(log):
    def has(t):
        return bool(db.q("SELECT 1 AS x FROM information_schema.tables WHERE table_name=:t "
                         "AND table_schema=current_schema()", t=t))
    if has("old_publishers"):
        n = db.x("""INSERT INTO publishers (ringba_publisher_id, name, status, publisher_email,
                       cc_publisher, notes, first_seen_at, created_at)
                    SELECT ringba_publisher_id, COALESCE(NULLIF(name,''), ringba_publisher_id),
                           COALESCE(status,'active'), alert_email, COALESCE(cc_on_alerts, FALSE),
                           notes, first_call_at, NOW()
                    FROM old_publishers
                    WHERE ringba_publisher_id IS NOT NULL
                      AND ringba_publisher_id NOT IN ('', 'None', 'none')
                    ON CONFLICT (ringba_publisher_id) DO NOTHING""")
        log(f"  copied {n} publishers (names, emails, CC switches)")
    if has("old_buyers"):
        n = db.x("""INSERT INTO buyers (ringba_buyer_id, name, status, watchlist, buyer_email,
                       cc_buyer, notes, created_at)
                    SELECT ringba_buyer_id, COALESCE(NULLIF(name,''), ringba_buyer_id),
                           COALESCE(status,'active'), COALESCE(watchlist, FALSE), alert_email,
                           COALESCE(cc_on_alerts, FALSE), notes, NOW()
                    FROM old_buyers
                    WHERE ringba_buyer_id IS NOT NULL
                      AND ringba_buyer_id NOT IN ('', 'None', 'none')
                    ON CONFLICT (ringba_buyer_id) DO NOTHING""")
        log(f"  copied {n} buyers (names, watchlist, emails)")
    if has("old_cap_rules") and has("old_publishers"):
        n = db.x("""INSERT INTO caps (entity_type, entity_id, period, metric, cap_value, thresholds, active)
                    SELECT 'publisher', p.id, r.period, 'converted', r.cap_value,
                           r.alert_threshold_pct::text || ',100', COALESCE(r.active, TRUE)
                    FROM old_cap_rules r
                    JOIN old_publishers op ON op.id = r.entity_id AND r.entity_type = 'publisher'
                    JOIN publishers p ON p.ringba_publisher_id = op.ringba_publisher_id""")
        log(f"  copied {n} publisher caps (counting converted calls)")
    if has("old_cap_rules") and has("old_buyers"):
        n = db.x("""INSERT INTO caps (entity_type, entity_id, period, metric, cap_value, thresholds, active)
                    SELECT 'buyer', b.id, r.period, 'paid', r.cap_value,
                           r.alert_threshold_pct::text || ',100', COALESCE(r.active, TRUE)
                    FROM old_cap_rules r
                    JOIN old_buyers ob ON ob.id = r.entity_id AND r.entity_type = 'buyer'
                    JOIN buyers b ON b.ringba_buyer_id = ob.ringba_buyer_id""")
        log(f"  copied {n} buyer caps (counting paid calls)")
    old_admin = db.q1("SELECT value FROM settings WHERE key='admin_email'")
    if old_admin and old_admin["value"]:
        db.x("INSERT INTO settings (key, value) VALUES ('admin_emails', :v) ON CONFLICT (key) DO NOTHING",
             v=old_admin["value"])
        log("  copied your admin email")


def _add_columns():
    for table, col, typ in ADD_COLUMNS:
        try:
            db.x(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        except Exception:
            pass   # column already exists


def _seed_templates_and_actions():
    import templates
    now = db.now_utc()
    for atype, (subj, body) in templates.DEFAULTS.items():
        if not db.q1("SELECT 1 AS x FROM email_templates WHERE alert_type=:t", t=atype):
            db.x("INSERT INTO email_templates (alert_type, name, subject, body, created_at, updated_at) "
                 "VALUES (:t,'Default',:s,:b,:n,:n)", t=atype, s=subj, b=body, n=now)
        db.x("UPDATE alert_rules SET template_id=(SELECT MIN(id) FROM email_templates WHERE alert_type=:t) "
             "WHERE alert_type=:t AND template_id IS NULL", t=atype)
    seeded = {r["alert_type"] for r in db.q("SELECT DISTINCT alert_type FROM alert_action_defs")}
    old = db.q1("SELECT id, body FROM email_templates WHERE alert_type='did_inactive' AND name='Default'")
    if old and old["body"] == OLD_DID_BODY:
        subj, body = templates.DEFAULTS["did_inactive"]
        db.x("UPDATE email_templates SET subject=:s, body=:b, updated_at=:n WHERE id=:i", s=subj, b=body, n=now, i=old["id"])
    for i, (atype, label, effect, days) in enumerate(ACTION_SEEDS):
        if atype in seeded:
            continue
        db.x("INSERT INTO alert_action_defs (alert_type, label, effect, effect_days, active, sort_order) "
             "VALUES (:t,:l,:e,:d,TRUE,:o)", t=atype, l=label, e=effect, d=days, o=i)


def _migrate_managers():
    """Older layout had ONE manager per publisher (publishers.manager_id). Now it is many: publisher_managers."""
    db.x("INSERT INTO publisher_managers (publisher_id, manager_id) SELECT id, manager_id FROM publishers "
         "WHERE manager_id IS NOT NULL ON CONFLICT DO NOTHING")
    db.x("UPDATE publishers SET manager_id = NULL WHERE manager_id IS NOT NULL")


def seed(log=print):
    for k, v in SETTING_DEFAULTS.items():
        db.x("INSERT INTO settings (key, value) VALUES (:k, :v) ON CONFLICT (key) DO NOTHING", k=k, v=v)
    for t, label, en, days, cnt, ccm, cce, ccx, desc in RULE_SEEDS:
        db.x("""INSERT INTO alert_rules (alert_type, label, enabled, param_days, param_count,
                   cc_manager, cc_entity_email, cc_entity_extra, extra_cc, description)
                VALUES (:t,:l,:e,:d,:c,:m,:ce,:cx,'',:desc) ON CONFLICT (alert_type) DO NOTHING""",
             t=t, l=label, e=en, d=days, c=cnt, m=ccm, ce=cce, cx=ccx, desc=desc)
    _seed_templates_and_actions()


def run(log=print, schema_file=None, migrate=True):
    log("Setting up the database ...")
    migrated = False
    if migrate:
        try:
            if _old_layout_present():
                log("Found your OLD tables. Keeping them as a backup (old_*) and building the new layout.")
                _rename_old_tables(log)
                migrated = True
        except Exception as e:  # non-Postgres (tests) or no permission
            log(f"  (skipped old-table check: {e})")
    sql = open(schema_file or os.path.join(HERE, "schema.sql"), encoding="utf-8").read()
    for stmt in _split_sql(sql):
        db.x(stmt)
    _add_columns()
    _migrate_managers()
    log("  tables are ready")
    if migrated:
        log("Copying your useful old settings into the new tables ...")
        _copy_old_data(log)
    seed(log)
    log("  default alert rules and settings are in place")
    log("DONE. Database is ready.")


if __name__ == "__main__":
    run()
