"""The alert rules. They read ONLY from the database (never from Ringba) and queue alerts."""
from datetime import timedelta

import alerts
import db
from db import to_dt
from timeutil import fmt, window_start

LOOKBACK_HOURS = 48  # only calls first saved by the live engine in the last 48h can alert

METRIC_SQL = {
    "all": "1=1",
    "connected": "c.connected = TRUE",
    "converted": "c.converted = TRUE",
    "paid": "c.paid = TRUE",
}
METRIC_LABEL = {"all": "all incoming calls", "connected": "connected calls",
                "converted": "converted calls", "paid": "paid calls"}
ENTITY_COL = {"publisher": "publisher_id", "buyer": "buyer_id"}


def parse_thresholds(s):
    out = set()
    for part in str(s or "").replace(";", ",").split(","):
        part = part.strip().rstrip("%")
        if part.isdigit() and 1 <= int(part) <= 1000:
            out.add(int(part))
    return sorted(out) or [80]


def yn(v):
    return "Yes" if v else "No"


def _call_ctx(c):
    return {"time": fmt(c["call_at"]), "publisher": c.get("pub_name") or "-", "buyer": c.get("buyer_name") or "-",
            "did": c.get("did") or "-", "caller": c.get("caller") or "-", "campaign": c.get("campaign_name") or "-",
            "connected": yn(c["connected"]), "converted": yn(c["converted"]), "paid": yn(c["paid"])}


def _recent_live_calls(now, extra_where=""):
    since = now - timedelta(hours=LOOKBACK_HOURS)
    return db.q(f"""SELECT c.call_id, c.call_at, c.publisher_id, c.buyer_id, c.connected, c.converted, c.paid,
                           c.campaign_name, c.caller_number AS caller, p.name AS pub_name, b.name AS buyer_name,
                           d.number AS did
                    FROM calls c
                    LEFT JOIN publishers p ON p.id = c.publisher_id
                    LEFT JOIN buyers b ON b.id = c.buyer_id
                    LEFT JOIN dids d ON d.id = c.did_id
                    WHERE c.source = 'live' AND c.created_at >= :since
                      AND COALESCE(d.never_alert, FALSE) = FALSE {extra_where}
                    ORDER BY c.call_at, c.call_id""", since=since)


def _recent_live_calls_ranked(now):
    """The recent live calls of publishers, each with its position in that publisher's history (call_rank: 1 = the
    publisher's very first call) and the time of the publisher's previous call (prev_call_at). ONE query does it all;
    before, the database was asked two extra questions for every single call (162 calls = 324 round trips = minutes)."""
    since = now - timedelta(hours=LOOKBACK_HOURS)
    return db.q("""WITH recent AS (
                       SELECT c.call_id, c.call_at, c.publisher_id, c.buyer_id, c.connected, c.converted, c.paid,
                              c.campaign_name, c.caller_number AS caller, p.name AS pub_name, b.name AS buyer_name,
                              d.number AS did
                       FROM calls c
                       LEFT JOIN publishers p ON p.id = c.publisher_id
                       LEFT JOIN buyers b ON b.id = c.buyer_id
                       LEFT JOIN dids d ON d.id = c.did_id
                       WHERE c.source = 'live' AND c.created_at >= :since AND c.publisher_id IS NOT NULL
                         AND COALESCE(d.never_alert, FALSE) = FALSE
                   ), ordered AS (
                       SELECT x.call_id,
                              ROW_NUMBER() OVER (PARTITION BY x.publisher_id ORDER BY x.call_at, x.call_id) AS call_rank,
                              LAG(x.call_at) OVER (PARTITION BY x.publisher_id ORDER BY x.call_at, x.call_id) AS prev_call_at
                       FROM calls x
                       WHERE x.publisher_id IN (SELECT publisher_id FROM recent)
                   )
                   SELECT r.*, o.call_rank, o.prev_call_at
                   FROM recent r JOIN ordered o ON o.call_id = r.call_id
                   ORDER BY r.call_at, r.call_id""", since=since)


def rule_new_and_lapsed_publisher(now):
    nr, lr = alerts.get_rule("new_publisher"), alerts.get_rule("lapsed_publisher")
    new_on, lap_on = bool(nr and nr["enabled"]), bool(lr and lr["enabled"])
    if not (new_on or lap_on):
        return 0
    n_first = int((nr or {}).get("param_count") or 10)
    lapse_days = int((lr or {}).get("param_days") or 30)
    rows = _recent_live_calls_ranked(now)
    todo = []                                        # (alert type, key, call, extra info), before skipping what was already alerted
    for c in rows:
        t = to_dt(c["call_at"])
        rank = int(c["call_rank"])
        if new_on and rank <= n_first:
            todo.append(("new_publisher", f"newpub:{c['publisher_id']}:{c['call_id']}", c, {"call_number": rank, "first_n": n_first}))
        if lap_on and rank > 1 and c["prev_call_at"]:
            prev = to_dt(c["prev_call_at"])
            if prev and (t - prev) > timedelta(days=lapse_days):
                todo.append(("lapsed_publisher", f"lapsed:{c['publisher_id']}:{c['call_id']}", c,
                             {"gap_days": (t - prev).days, "last_call_time": fmt(prev)}))
    known = alerts.existing_keys([k for _, k, _, _ in todo])
    made = 0
    for atype, key, c, more in todo:
        if key in known:
            continue
        if alerts.create_alert(atype, key, {**_call_ctx(c), **more}, publisher_id=c["publisher_id"], buyer_id=c["buyer_id"]):
            made += 1
    return made


def rule_watchlist_buyer(now):
    if not alerts.rule_enabled("watchlist_buyer"):
        return 0
    calls = _recent_live_calls(now, "AND c.connected = TRUE AND b.watchlist = TRUE")
    known = alerts.existing_keys([f"watch:{c['call_id']}" for c in calls])
    made = 0
    for c in calls:
        if f"watch:{c['call_id']}" in known:
            continue
        if alerts.create_alert("watchlist_buyer", f"watch:{c['call_id']}", _call_ctx(c),
                               publisher_id=c["publisher_id"], buyer_id=c["buyer_id"]):
            made += 1
    return made


def cap_usage(cap, now, ent_name=None, ins=None):
    """How much of a cap is used right now. Returns dict(usage, pct, start, key, name).
    ent_name / ins (an insights.Usage) are optional: the Cap overview passes them so it does not ask the database again for every cap."""
    start, key = window_start(cap["period"], now)
    col = ENTITY_COL[cap["entity_type"]]
    metric = METRIC_SQL.get(cap["metric"], METRIC_SQL["paid"])
    camp = (cap.get("campaign_name") or "").strip() if cap["entity_type"] == "publisher" else ""
    table = "publishers" if cap["entity_type"] == "publisher" else "buyers"
    ent = {"name": ent_name} if ent_name is not None else db.q1(f"SELECT name FROM {table} WHERE id=:i", i=cap["entity_id"])
    usage = None
    if ent:                                            # the official Ringba insights numbers first (they match Ringba exactly)
        try:
            if ins is not None:
                usage = ins.get(cap, ent["name"], cap["period"])
            else:
                import insights
                usage = insights.usage(cap, ent["name"], cap["period"], now)
        except Exception:
            usage = None
    if usage is None:                                  # not synced yet: count the stored calls
        usage = db.q1(f"SELECT COUNT(*) AS n FROM calls c WHERE c.{col}=:e AND c.call_at >= :s AND {metric}"
                      + (" AND c.campaign_name = :camp" if camp else ""),
                      e=cap["entity_id"], s=start, **({"camp": camp} if camp else {}))["n"]
    pct = usage / cap["cap_value"] * 100 if cap["cap_value"] else 0
    name = ent["name"] if ent else "?"
    return {"usage": usage, "pct": pct, "start": start, "key": key, "name": f"{name} / campaign {camp}" if camp else name}


def rule_caps(now):
    import dids as didtools
    made = 0
    links = didtools.active_links()
    for cap in db.q("SELECT * FROM caps WHERE active = TRUE"):
        atype = f"{cap['entity_type']}_cap"
        if not alerts.rule_enabled(atype):
            continue
        if not didtools.cap_has_active_did(cap, links):
            continue                                   # no active DID for this publisher / campaign: nothing is counted or alerted
        u = cap_usage(cap, now)
        reached = [t for t in parse_thresholds(cap["thresholds"]) if u["pct"] >= t]
        if not reached:
            continue
        top = max(reached)
        pub_id = cap["entity_id"] if cap["entity_type"] == "publisher" else None
        buy_id = cap["entity_id"] if cap["entity_type"] == "buyer" else None
        ctx = {"entity_type": cap["entity_type"].title(), "name": u["name"], "percent": f"{u['pct']:.0f}", "level": top,
               "usage": u["usage"], "cap": cap["cap_value"], "remaining": max(cap["cap_value"] - u["usage"], 0),
               "period": cap["period"], "metric_label": METRIC_LABEL.get(cap["metric"], cap["metric"]),
               "window_start": fmt(u["start"])}
        key = f"cap:{cap['id']}:{cap['cap_value']}:{u['key']}:{top}"
        if alerts.create_alert(atype, key, ctx, publisher_id=pub_id, buyer_id=buy_id, snooze_key=f"cap:{cap['id']}"):
            made += 1
            for lower in reached:      # lower levels crossed at the same moment: mark done, never email later
                if lower != top:
                    alerts.create_alert(atype, f"cap:{cap['id']}:{cap['cap_value']}:{u['key']}:{lower}",
                                        status="skipped", subject="(skipped)", body="skipped")
    return made


def next_step_text(days, remind_days, remind_max, reminders_sent, next_cycle_idle):
    """The sentence at the bottom of every inactive-DID email: what happens if nobody answers."""
    if remind_days > 0 and reminders_sent < remind_max:
        return (f"If nobody answers this email: a reminder is sent in {remind_days} day(s) "
                f"(reminder {reminders_sent + 1} of {remind_max}). If you click a 'wait' button, this DID is paused for those days "
                f"and you get a follow-up email when the pause ends. The alert is never paused for ever.")
    if remind_days > 0:
        return (f"This was the last reminder. If nobody answers, nothing more is sent for this DID until it reaches "
                f"{next_cycle_idle} days without calls, then a new alert starts. The alert is never paused for ever.")
    return (f"If nobody answers this email: no reminder is sent. A new alert for this DID starts when it reaches "
            f"{next_cycle_idle} days without calls. If you click a 'wait' button, you get a follow-up email when the pause ends.")


def rule_target_caps(now):
    """Buyer target caps (daily, monthly AND the global call cap). Limits and live usage come from Ringba (see targets.py)."""
    if not alerts.rule_enabled("target_cap"):
        return 0
    import targets as tg
    made = 0
    for t in db.q("SELECT * FROM targets WHERE active = TRUE AND alerts_on = TRUE AND counts_at IS NOT NULL"):
        at = to_dt(t["counts_at"])
        if now - at > timedelta(minutes=tg.STALE_MINUTES):
            continue                                   # usage is old (the poll did not get through): never alert on old numbers
        for period, cap, used in (("daily", t["daily_cap"], t["count_day"]), ("monthly", t["monthly_cap"], t["count_month"]),
                                  ("global", t.get("all_time_cap"), t.get("count_total"))):
            if not cap:
                continue
            used = int(used or 0)
            pct = used / cap * 100
            reached = [x for x in parse_thresholds(t["thresholds"]) if pct >= x]
            if not reached:
                continue
            top = max(reached)
            if period == "global":                     # the Global Call Cap has no window: one alert per level for this cap size
                start, wkey = at, "all"
            else:
                start, wkey = window_start(period, at)     # the window the numbers were read in, so midnight cannot cause a repeat
            ctx = {"target": t["name"] or t["ringba_target_id"], "buyer": t["buyer_name"] or "-", "period": period,
                   "percent": f"{pct:.0f}", "level": top, "usage": used, "cap": cap, "remaining": max(cap - used, 0),
                   "window_start": fmt(start)}
            base = f"target:{t['id']}:{period}:{cap}:{wkey}"
            if alerts.create_alert("target_cap", f"{base}:{top}", ctx, buyer_id=t["buyer_id"], snooze_key=f"target:{t['id']}:{period}"):
                made += 1
                for lower in reached:
                    if lower != top:
                        alerts.create_alert("target_cap", f"{base}:{lower}", status="skipped", subject="(skipped)", body="skipped")
    return made


def cancel_pending_for_inactive_dids(alert_type):
    """Emails of this type that are still waiting to be sent, for a DID that is retired / deleted / 'never alert': cancelled.
    The alert record stays (status 'skipped', with the reason) so reports and history are kept."""
    return db.x("UPDATE alerts SET status='skipped', last_error='Cancelled: the DID is retired, deleted or set to never alert' "
                "WHERE status='pending' AND alert_type=:t AND snooze_key IN "
                "(SELECT 'did:' || CAST(id AS TEXT) FROM dids WHERE status <> 'active' OR never_alert = TRUE)", t=alert_type)


_live = {"done": False}


def did_still_needs_follow_up(d, now, why=""):
    """Last check before a FOLLOW-UP or REMINDER email is made for DID `d`: ask Ringba (at most every 5 minutes) whether the number still
    exists, then read the DID's status again. False = the DID was retired / deleted / set to never alert, so no email is sent."""
    import dids as didtools
    if not _live["done"]:
        _live["done"] = True
        rc_mod = None
        try:
            import ringba_client as rc_mod
            rc_mod.set_deadline(25)
            didtools.check_live_status(now)
        except Exception:
            pass                                     # Ringba not reachable: the status saved by the hourly sync is used
        finally:
            if rc_mod is not None:
                rc_mod.set_deadline(None)
    cur = db.q1("SELECT status, never_alert FROM dids WHERE id=:i", i=d["id"])
    return bool(cur and cur["status"] == "active" and not cur["never_alert"])


def rule_inactive_dids(now):
    """One alert per DID per idle period. If nobody answers, it is repeated as a REMINDER every `did_reminder_days`
    days (at most `did_reminder_max` times). If somebody clicked 'wait N days', a FOLLOW-UP goes out when the pause ends.
    DIDs ticked 'Never alert' are skipped completely."""
    rule = alerts.get_rule("did_inactive")
    if not rule or not rule["enabled"]:
        return 0
    days = int(rule["param_days"] or 30)
    remind_days = max(int(float(db.get_setting("did_reminder_days", "3") or 0)), 0)
    remind_max = max(int(float(db.get_setting("did_reminder_max", "3") or 0)), 0)
    # a DID that became 'never alert' or was retired / deleted must not send an email that was still waiting in the queue
    cancel_pending_for_inactive_dids("did_inactive")
    idle_dids = []
    for d in db.q("SELECT * FROM dids WHERE status = 'active' AND COALESCE(never_alert, FALSE) = FALSE"):
        ref = to_dt(d["last_call_at"]) or to_dt(d["added_at"])
        if ref and (now - ref).days >= days:
            idle_dids.append((d, ref, (now - ref).days))
    if not idle_dids:
        return 0

    chains = {}       # base key -> that idle period's alerts, oldest first (the alert, its reminders, its follow-ups)
    for a in db.q("SELECT id, dedupe_key, status, created_at, sent_at, response_label, responded_at, case_status, case_updated_at FROM alerts "
                  "WHERE alert_type='did_inactive' AND status <> 'skipped' AND dedupe_key LIKE 'did:%' ORDER BY id"):
        parts = a["dedupe_key"].split(":")
        if len(parts) >= 4:
            chains.setdefault(":".join(parts[:4]), []).append(a)
    snoozes = {r["entity_key"]: to_dt(r["snooze_until"]) for r in db.q("SELECT entity_key, snooze_until FROM snoozes WHERE entity_key LIKE 'did:%'")}

    import cases
    import dids as didtools
    extra = {}

    def load_extra():                      # the heavy lookups are only done when an email is really going to be made
        if not extra:
            active = [d for d, _, _ in idle_dids]
            extra.update(pub={p["id"]: p["name"] for p in db.q("SELECT id, name FROM publishers")},
                         camp=didtools.campaign_names(active), calls30=didtools.calls_last_30d(now),
                         notes=cases.latest_by_did())
        return extra

    def make_ctx(d, idle, cycle, reminders_sent):
        e = load_extra()
        return {"did": d["number"], "label": d["label"] or "", "label_part": f" ({d['label']})" if d["label"] else "",
                "idle_days": idle, "last_call": fmt(d["last_call_at"]) if d["last_call_at"] else "never",
                "added": fmt(d["added_at"]), "publisher": e["pub"].get(d["publisher_id"]) or d["affiliate_name"] or "none",
                "next_charge": fmt(d["next_charge_at"], False) if d["next_charge_at"] else "unknown",
                "monthly_cost": f"${float(d['monthly_cost']):.2f}" if d["monthly_cost"] is not None else "not set",
                "campaign": e["camp"].get(d["id"]) or "unknown", "calls_30d": e["calls30"].get(d["id"], 0),
                "last_note": cases.one_line(e["notes"].get(d["id"]), fmt) or "none yet",
                "next_step": next_step_text(days, remind_days, remind_max, reminders_sent, (cycle + 1) * days)}

    import chain as chainlib
    made = 0
    for d, ref, idle in idle_dids:
        cycle = idle // days
        base = f"did:{d['id']}:{ref.date()}:{cycle}"
        chain = chains.get(base, [])
        ev = chainlib.evaluate(chain, now, snoozes.get(f"did:{d['id']}"), remind_days, remind_max, base)
        key, tag, reminders_sent = base, "", 0
        thread_root, history = None, None
        if chain:
            # the chain judges every alert and answer of THIS DID (not only the newest e-mail), see chain.py
            if ev["state"] not in ("reminder", "followup_due") or ev["next_at"] is None or ev["next_at"] > now:
                continue
            key, tag, reminders_sent = ev["key"], ev["tag"], ev["reminders"] + (1 if ev["state"] == "reminder" else 0)
            thread_root = chain[0]["id"]                  # the follow-up is an answer in the SAME e-mail conversation: same [#A..] code
        if tag and not did_still_needs_follow_up(d, now):          # a follow-up / reminder: check Ringba first
            alerts.create_alert("did_inactive", f"{key}:cancelled", status="skipped", subject="(follow-up cancelled: DID retired)",
                                body="This follow-up was cancelled because the DID was deleted / retired in Ringba before it was sent.",
                                publisher_id=d["publisher_id"], snooze_key=f"did:{d['id']}")
            continue
        ctx = make_ctx(d, idle, cycle, reminders_sent)
        if thread_root:
            ctx["history"] = chainlib.history_text(thread_root)
        if alerts.create_alert("did_inactive", key, ctx, publisher_id=d["publisher_id"], snooze_key=f"did:{d['id']}",
                               subject_tag=tag, thread_id=thread_root):
            made += 1
    return made


def rule_new_dids_no_calls(now):
    """A recently added DID that never got a call: alert after N days, then again every N days, until its first call.
    A pause (answer button, any number of days) stops it; when the pause ends the alert comes back. Never-alert DIDs are skipped."""
    rule = alerts.get_rule("did_new_no_calls")
    if not rule or not rule["enabled"]:
        return 0
    every = max(int(rule["param_days"] or 3), 1)
    window = int(rule["param_count"] or 0)          # a DID is 'new' for this many days after it was added (0 = always)
    sql = ("SELECT d.* FROM dids d WHERE d.status='active' AND COALESCE(d.never_alert, FALSE)=FALSE AND d.last_call_at IS NULL "
           "AND NOT EXISTS (SELECT 1 FROM calls c WHERE c.did_id = d.id)")
    p = {}
    if window > 0:
        sql += " AND d.added_at >= :since"
        p["since"] = now - timedelta(days=window)
    cancel_pending_for_inactive_dids("did_new_no_calls")
    db.x("UPDATE alerts SET status='skipped', last_error='DID got its first call: nothing to follow up' WHERE status='pending' AND alert_type='did_new_no_calls' AND snooze_key IN "
         "(SELECT 'did:' || CAST(id AS TEXT) FROM dids WHERE last_call_at IS NOT NULL)")
    cands = [(d, to_dt(d["added_at"])) for d in db.q(sql, **p)]
    cands = [(d, a) for d, a in cands if a and (now - a) >= timedelta(days=every)]
    if not cands:
        return 0
    last = {}      # did id -> (number of alerts so far, time of the newest, is it still waiting to be sent)
    handled = set()   # DIDs whose newest alert the team closed ('done' / 'no action'): no more repeat alerts
    import chain as chainlib
    newchains, first_id = {}, {}
    for a in db.q("SELECT id, dedupe_key, status, created_at, sent_at, case_status, case_updated_at FROM alerts WHERE alert_type='did_new_no_calls' "
                  "AND status <> 'skipped' AND dedupe_key LIKE 'newdid:%' ORDER BY id"):
        did = a["dedupe_key"].split(":")[1]
        n = last.get(did, (0,))[0] + 1
        last[did] = (n, to_dt(a["sent_at"] or a["created_at"]), a["status"] == "pending")
        newchains.setdefault(did, []).append(a)
        first_id.setdefault(did, a["id"])
    for did, ch in newchains.items():          # closed by the team (newest status done / no action): no more repeat alerts
        if chainlib.closed_by_team(ch):
            handled.add(did)
    snoozes = {r["entity_key"]: to_dt(r["snooze_until"]) for r in db.q("SELECT entity_key, snooze_until FROM snoozes WHERE entity_key LIKE 'did:%'")}
    import dids as didtools
    pub = {x["id"]: x["name"] for x in db.q("SELECT id, name FROM publishers")}
    camp = didtools.campaign_names([d for d, _ in cands])
    made = 0
    for d, added in cands:
        n, when, waiting = last.get(str(d["id"]), (0, None, False))
        if waiting or str(d["id"]) in handled:
            continue
        until = snoozes.get(f"did:{d['id']}")
        if until and until > now:
            continue                                   # paused (maybe the DID owner asked for time)
        if when and now - when < timedelta(days=every):
            continue
        if n and not did_still_needs_follow_up(d, now):          # a repeat alert: check Ringba first
            alerts.create_alert("did_new_no_calls", f"newdid:{d['id']}:{n + 1}:cancelled", status="skipped",
                                subject="(follow-up cancelled: DID retired)", snooze_key=f"did:{d['id']}",
                                body="This follow-up was cancelled because the DID was deleted / retired in Ringba before it was sent.",
                                publisher_id=d["publisher_id"])
            continue
        ctx = {"did": d["number"], "label": d["label"] or "", "label_part": f" ({d['label']})" if d["label"] else "",
               "age_days": (now - added).days, "added": fmt(d["added_at"]), "alert_number": n + 1, "repeat_days": every,
               "publisher": pub.get(d["publisher_id"]) or d["affiliate_name"] or "none", "campaign": camp.get(d["id"]) or "unknown",
               "monthly_cost": f"${float(d['monthly_cost']):.2f}" if d["monthly_cost"] is not None else "not set",
               "next_charge": fmt(d["next_charge_at"], False) if d["next_charge_at"] else "unknown",
               "next_step": (f"If nobody answers this email: another alert is sent in {every} day(s), until this DID gets its first call. "
                             f"If you click Pause, nothing is sent for the days you choose, then the alerts continue.")}
        root = first_id.get(str(d["id"]))
        if root:                                # a repeat alert is an answer in the SAME e-mail conversation (same [#A..] code) + the history
            ctx["history"] = chainlib.history_text(root)
        if alerts.create_alert("did_new_no_calls", f"newdid:{d['id']}:{n + 1}", ctx, publisher_id=d["publisher_id"],
                               snooze_key=f"did:{d['id']}", subject_tag="REMINDER: " if n else "", thread_id=root):
            made += 1
    return made


def rule_task_digest(now):
    """My Tasks: real-time alerts (at each task's own date + time), follow-ups while a task is not done, and the optional morning summary."""
    import tasks
    if not alerts.rule_enabled("task_alert"):
        return tasks.run_digest(now)
    a, f = tasks.run_alerts(now)
    return a + f + tasks.run_digest(now)


def run_all(now=None, log=None, max_new=None, seconds=None):
    """Every rule once. `max_new` / `seconds` limit how many alerts one run may create (the rest comes next run)."""
    import time
    now = now or db.now_utc()
    out = {}
    _live["done"] = False                              # the live Ringba check for follow-ups runs at most once per run
    alerts.set_budget(max_new, seconds)
    try:
        for name, fn in [("publishers", rule_new_and_lapsed_publisher), ("watchlist", rule_watchlist_buyer),
                         ("caps", rule_caps), ("targets", rule_target_caps), ("dids", rule_inactive_dids), ("new dids", rule_new_dids_no_calls), ("tasks", rule_task_digest)]:
            t0 = time.monotonic()
            out[name] = fn(now)
            if log:
                log(f"  rule {name}: {out[name]} new alert(s), {time.monotonic() - t0:.1f}s")
        if alerts.over_budget() and log:
            log("  (limit for one run reached: the remaining alerts are created on the next runs)")
    finally:
        alerts.set_budget(None, None)
    return out
