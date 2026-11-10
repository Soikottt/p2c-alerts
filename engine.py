"""The 5-minute job: read Ringba -> save -> check rules -> send emails."""
import sys
import traceback
from datetime import timedelta

import alerts
import db
import dids
import ingest
import mailer
import rules
from timeutil import fmt


def check_stale(now):
    rule = alerts.get_rule("engine_stale")
    if not rule or not rule["enabled"]:
        return
    minutes = int(rule["param_count"] or 45)
    last = db.q1("SELECT MAX(finished_at) AS t FROM sync_runs WHERE kind='live' AND status='ok'")
    last = db.to_dt(last["t"]) if last and last["t"] else None
    if last and now - last > timedelta(minutes=minutes):
        gap = int((now - last).total_seconds() // 60)
        alerts.create_alert("engine_stale", f"stale:{last.isoformat()}", {"gap_minutes": gap, "last_sync": fmt(last)})


def run_once(log=print):
    """Returns (ok, summary_text)."""
    ok = True
    lines = []
    now = db.now_utc()
    try:
        check_stale(now)
    except Exception as e:
        lines.append(f"stale check failed: {e}")
    try:
        stats = ingest.live_sync()
        lines.append(f"Ringba sync: fetched {stats['fetched']}, new {stats['inserted']}, updated {stats['updated']}")
    except Exception as e:
        ok = False
        lines.append(f"Ringba sync FAILED: {e}")
        traceback.print_exc()
        try:
            alerts.create_alert("sync_error", f"sync_error:{now:%Y%m%d%H}", {"error": str(e)[:600]})
        except Exception:
            pass
    try:                                   # DID list: refreshed from Ringba about once an hour (non-critical)
        r = dids.sync_if_due(now)
        if r is not None:
            lines.append(f"DID sync: {r['in_ringba']} numbers in Ringba, {r['added']} new, {r['updated']} updated, "
                         f"{r['retired']} retired" + (f", {r['retire_skipped']} NOT retired (list looked incomplete)" if r["retire_skipped"] else ""))
    except Exception as e:
        lines.append(f"DID sync FAILED (will retry in 30 min): {e}")
        try:
            alerts.create_alert("sync_error", f"numsync_error:{now:%Y%m%d}", {"error": f"DID list sync failed: {str(e)[:500]}"})
        except Exception:
            pass
    try:
        made = rules.run_all()
        lines.append("Alerts created: " + ", ".join(f"{k}={v}" for k, v in made.items()))
    except Exception as e:
        ok = False
        lines.append(f"Rules FAILED: {e}")
        traceback.print_exc()
    try:
        res = mailer.process_outbox()
        lines.append("Emails: " + ", ".join(f"{k}={v}" for k, v in res.items()) +
                     ("  (DRY RUN is ON - nothing really sent)" if mailer.is_dry_run() else ""))
    except Exception as e:
        ok = False
        lines.append(f"Email sending FAILED: {e}")
        traceback.print_exc()
    for ln in lines:
        log(ln)
    return ok, "\n".join(lines)


if __name__ == "__main__":
    good, _ = run_once()
    sys.exit(0 if good else 1)
