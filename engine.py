"""The 5-minute job: read Ringba -> save -> check rules -> send emails.

It can NOT hang: every step prints as it happens (with timing), the steps share a time budget, and a watchdog
stops the run with a clear message if something is still stuck, before GitHub's own 8-minute limit cancels it."""
import faulthandler
import os
import sys
import threading
import time
import traceback
from datetime import timedelta

import alerts
import backup
import db
import dids
import inbox
import ingest
import mailer
import rules
import targets
import setup_db
import ringba_client as rc
from timeutil import fmt

TIME_LIMIT = float(os.getenv("ENGINE_TIME_LIMIT_SECONDS", "400"))   # watchdog: hard stop (GitHub cancels at 480)
SOFT_LIMIT = 300                                                     # optional steps are skipped after this many seconds
CURRENT = {"step": "starting"}


def _emit(msg):
    print(msg, flush=True)


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


def run_once(log=_emit, time_limit=None):
    """Returns (ok, summary_text). `log` is called for every step as it happens."""
    t0 = time.monotonic()
    limit = time_limit or SOFT_LIMIT
    ok = True
    lines = []

    def say(msg):
        line = f"[{time.monotonic() - t0:6.1f}s] {msg}"
        lines.append(line)
        log(line)

    def step(name):
        CURRENT["step"] = name
        say(f"{name} ...")

    def elapsed():
        return time.monotonic() - t0

    now = db.now_utc()
    try:
        if setup_db.ensure_columns():
            say("database upgraded (new columns added)")
    except Exception as ex:
        say(f"database check skipped: {ex}")
    try:                                   # a run that was cancelled half-way leaves a 'running' record behind: close it
        n = db.x("UPDATE sync_runs SET status='error', finished_at=:n, error='Interrupted: the run was stopped before it finished' "
                 "WHERE status='running' AND started_at < :c", n=now, c=now - timedelta(minutes=15))
        if n:
            say(f"closed {n} earlier run(s) that were cut off")
    except Exception as ex:
        say(f"cleanup skipped: {ex}")
    try:
        step("checking the engine heartbeat")
        check_stale(now)
    except Exception as e:
        say(f"heartbeat check failed: {e}")

    # ---- 1. calls from Ringba (essential). At most ~3 minutes, then it carries on at the next run.
    step("reading calls from Ringba")
    rc.set_deadline(min(180, max(limit - elapsed() - 60, 30)))
    try:
        stats = ingest.live_sync()
        say(f"Ringba sync: fetched {stats['fetched']}, new {stats['inserted']}, updated {stats['updated']}")
    except Exception as e:
        ok = False
        say(f"Ringba sync FAILED: {e}")
        traceback.print_exc()
        try:
            alerts.create_alert("sync_error", f"sync_error:{now:%Y%m%d%H}", {"error": str(e)[:600]})
        except Exception:
            pass
    finally:
        rc.set_deadline(None)

    # ---- 2. DID list (optional)
    if elapsed() < limit - 90:
        step("refreshing the DID list from Ringba (once an hour)")
        rc.set_deadline(60)
        try:
            r = dids.sync_if_due(now)
            if r is None:
                say("DID list: not due yet")
            else:
                say(f"DID sync: {r['in_ringba']} numbers in Ringba, {r['added']} new, {r['updated']} updated, {r['retired']} retired"
                    + (f", {r['retire_skipped']} NOT retired (list looked incomplete)" if r["retire_skipped"] else ""))
        except Exception as e:
            say(f"DID sync FAILED (will retry in 30 min): {e}")
            try:
                alerts.create_alert("sync_error", f"numsync_error:{now:%Y%m%d}", {"error": f"DID list sync failed: {str(e)[:500]}"})
            except Exception:
                pass
        finally:
            rc.set_deadline(None)
    else:
        say("DID list: skipped (this run is short of time)")

    # ---- 2b. buyer target caps + live usage from Ringba (optional, at most ~75 s)
    if elapsed() < limit - 120:
        step("reading buyer target caps and usage from Ringba")
        rc.set_deadline(75)
        try:
            r = targets.run()
            if "definitions" in r:
                d = r["definitions"]
                say(f"targets: {d['in_ringba']} in Ringba, {d['added']} new, {d['updated']} updated, {d['removed']} removed; "
                    f"{d['report']['with_daily_cap']} with a daily cap, {d['report']['with_monthly_cap']} with a monthly cap")
            u = r["usage"]
            say(f"target usage: {u['polled']} of {u['total']} read" + (f", {u['failed']} failed" if u["failed"] else "")
                + (" (out of time, the rest next run)" if u.get("out_of_time") else "") + (f" - {u['error']}" if u.get("error") else ""))
        except Exception as e:
            say(f"target caps FAILED (will retry): {e}")
            try:
                alerts.create_alert("sync_error", f"targets_error:{now:%Y%m%d}", {"error": f"Target caps from Ringba failed: {str(e)[:500]}"})
            except Exception:
                pass
        finally:
            rc.set_deadline(None)
    else:
        say("target caps: skipped (this run is short of time)")

    # ---- 3. email replies (optional)
    if elapsed() < limit - 90:
        step("reading email replies from affiliate managers")
        try:
            r = inbox.sync_if_enabled()
            if r is None:
                say("email replies: off, or waiting after a problem")
            elif "error" in r:
                say(f"reading email replies FAILED: {r['error']}")
            else:
                say(f"email replies: {r['saved']} saved as notes ({r['checked']} new messages checked)")
        except Exception as e:
            say(f"reading email replies FAILED: {e}")
    else:
        say("email replies: skipped (this run is short of time)")

    # ---- 4. rules (essential)
    step("checking the alert rules")
    try:
        made = rules.run_all()
        say("alerts created: " + ", ".join(f"{k}={v}" for k, v in made.items()))
    except Exception as e:
        ok = False
        say(f"rules FAILED: {e}")
        traceback.print_exc()

    # ---- 5. send emails (essential): one Gmail login for the batch, at most 25 per run
    step("sending emails")
    try:
        res = mailer.process_outbox(limit=25, max_seconds=max(limit - elapsed() - 20, 20))
        say("emails: " + ", ".join(f"{k}={v}" for k, v in res.items()) +
            ("  (DRY RUN is ON, nothing really sent)" if mailer.is_dry_run() else "") +
            (f"  -> {res['left']} more will go out on the next runs" if res["left"] else ""))
    except Exception as e:
        ok = False
        say(f"sending emails FAILED: {e}")
        traceback.print_exc()

    # ---- 6. weekly backup (optional)
    if elapsed() < limit:
        step("weekly backup check")
        try:
            b = backup.send_if_due(now)
            say(f"backup emailed to the admin ({b})" if b else "backup: not due")
        except Exception as e:
            say(f"backup email FAILED: {e}")
    CURRENT["step"] = "finished"
    say(f"done in {elapsed():.1f}s" + ("" if ok else " (with errors)"))
    return ok, "\n".join(lines)


def start_watchdog(seconds):
    """If the run is still going after `seconds`, say where it is stuck, show what every thread is doing, and exit."""
    def fire():
        _emit(f"WATCHDOG: the engine was still running after {seconds:.0f}s, stuck in step: '{CURRENT['step']}'. "
              f"Stopping now so GitHub does not cancel the whole job. Thread dump follows:")
        faulthandler.dump_traceback(file=sys.stdout, all_threads=True)
        sys.stdout.flush()
        os._exit(3)
    t = threading.Timer(seconds, fire)
    t.daemon = True
    t.start()
    return t


def main():
    wd = start_watchdog(TIME_LIMIT)
    good, _ = run_once()
    wd.cancel()
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())
