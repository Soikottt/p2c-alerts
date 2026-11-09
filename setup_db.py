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
    ("did_inactive", "Inactive DID", True, 30, None, False, False, False,
     "A DID had no calls for N days (counted from its last call, or from the day it was added). N = 'Days'."),
    ("engine_stale", "Alert engine was offline", True, None, 45, False, False, False,
     "The 5-minute engine did not run for N minutes. N = 'Count' (minutes)."),
    ("sync_error", "Ringba sync failed", True, None, None, False, False, False,
     "The engine could not read call data from Ringba."),
]

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


def seed(log=print):
    for k, v in SETTING_DEFAULTS.items():
        db.x("INSERT INTO settings (key, value) VALUES (:k, :v) ON CONFLICT (key) DO NOTHING", k=k, v=v)
    for t, label, en, days, cnt, ccm, cce, ccx, desc in RULE_SEEDS:
        db.x("""INSERT INTO alert_rules (alert_type, label, enabled, param_days, param_count,
                   cc_manager, cc_entity_email, cc_entity_extra, extra_cc, description)
                VALUES (:t,:l,:e,:d,:c,:m,:ce,:cx,'',:desc) ON CONFLICT (alert_type) DO NOTHING""",
             t=t, l=label, e=en, d=days, c=cnt, m=ccm, ce=cce, cx=ccx, desc=desc)


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
    log("  tables are ready")
    if migrated:
        log("Copying your useful old settings into the new tables ...")
        _copy_old_data(log)
    seed(log)
    log("  default alert rules and settings are in place")
    log("DONE. Database is ready.")


if __name__ == "__main__":
    run()
