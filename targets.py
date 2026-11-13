"""Buyer targets and their caps, read from Ringba.

 * every `targets_sync_minutes` (60): the list of targets with their daily / monthly cap  -> table `targets`
 * every 5-minute run: the live usage ("Counts": calls today / this month) of every target that has a cap
The alert rule (rules.rule_target_caps) then compares usage with the cap. Your own choices (alerts on/off, the % levels)
are never overwritten by a sync."""
from datetime import timedelta

import db
import ringba_client as rc

STALE_MINUTES = 30        # usage older than this is not used for alerts (the poll did not get through)


def _buyer_lookup():
    buyers = db.q("SELECT id, ringba_buyer_id, name FROM buyers")
    return ({b["ringba_buyer_id"]: b["id"] for b in buyers if b["ringba_buyer_id"]},
            {(b["name"] or "").strip().lower(): b["id"] for b in buyers})


def _buyer_from_calls(now):
    """{target name (lower): buyer_id} from recent calls, for targets whose Ringba record does not say who the buyer is."""
    out = {}
    for r in db.q("SELECT target_name, buyer_id, MAX(call_at) AS t FROM calls WHERE target_name IS NOT NULL AND buyer_id IS NOT NULL "
                  "AND call_at >= :s GROUP BY target_name, buyer_id", s=now - timedelta(days=60)):
        key = r["target_name"].strip().lower()
        t = db.to_dt(r["t"])
        if key not in out or t > out[key][0]:
            out[key] = (t, r["buyer_id"])
    return {k: v[1] for k, v in out.items()}


def sync_definitions(now=None):
    """Read every target + its caps from Ringba and save them. Returns stats."""
    now = now or db.now_utc()
    items, report = rc.fetch_targets()
    by_rid, by_name = _buyer_lookup()
    from_calls = None
    have = {r["ringba_target_id"]: r for r in db.q("SELECT id, ringba_target_id, buyer_id FROM targets")}
    added = updated = 0
    seen = set()
    for t in items:
        seen.add(t["rid"])
        bid = by_rid.get(t["buyer_rid"]) or by_name.get((t["buyer_name"] or "").strip().lower())
        if bid is None:
            if from_calls is None:
                from_calls = _buyer_from_calls(now)
            bid = from_calls.get((t["name"] or "").strip().lower())
        row = have.get(t["rid"])
        if row is None:
            db.x("INSERT INTO targets (ringba_target_id, name, buyer_id, buyer_name, active, daily_cap, monthly_cap, synced_at) "
                 "VALUES (:r,:n,:b,:bn,:a,:d,:m,:s)", r=t["rid"], n=t["name"], b=bid, bn=t["buyer_name"], a=t["enabled"],
                 d=t["daily_cap"], m=t["monthly_cap"], s=now)
            added += 1
        else:
            db.x("UPDATE targets SET name=:n, buyer_id=COALESCE(:b, buyer_id), buyer_name=COALESCE(:bn, buyer_name), active=:a, "
                 "daily_cap=:d, monthly_cap=:m, synced_at=:s WHERE id=:i", n=t["name"], b=bid, bn=t["buyer_name"], a=t["enabled"],
                 d=t["daily_cap"], m=t["monthly_cap"], s=now, i=row["id"])
            updated += 1
    gone = 0
    if items:                                           # a target deleted in Ringba stops alerting
        for rid, row in have.items():
            if rid not in seen:
                gone += db.x("UPDATE targets SET active = FALSE WHERE id=:i AND active = TRUE", i=row["id"])
    return {"in_ringba": len(items), "added": added, "updated": updated, "removed": gone, "report": report}


def poll_counts(now=None, max_errors=5):
    """Live usage of every active target that has a cap. Stops quietly when the time budget runs out
    (the least recently read targets are read first next time). Returns stats."""
    now = now or db.now_utc()
    rows = db.q("SELECT id, ringba_target_id FROM targets WHERE active = TRUE AND (daily_cap IS NOT NULL OR monthly_cap IS NOT NULL) "
                "ORDER BY counts_at IS NOT NULL, counts_at")
    res = {"polled": 0, "failed": 0, "total": len(rows)}
    errors = 0
    for r in rows:
        try:
            c = rc.fetch_target_counts(r["ringba_target_id"])
        except rc.RingbaError as e:
            if "ran out of time" in str(e):
                res["out_of_time"] = True
                break
            res["failed"] += 1
            errors += 1
            if errors >= max_errors:
                res["error"] = str(e)[:200]
                break
            continue
        if c is None:
            res["failed"] += 1
            continue
        db.x("UPDATE targets SET count_day=:d, count_month=:m, counts_at=:n WHERE id=:i", d=c["day"], m=c["month"], n=db.now_utc(), i=r["id"])
        res["polled"] += 1
        rc._nap(0.2)
    return res


def run(now=None, force=False):
    """Called by the engine. Returns a stats dict, or None when there is nothing to do."""
    now = now or db.now_utc()
    out = {}
    nxt = db.get_setting("targets_next_run", "")
    due = force or not nxt or now >= db.to_dt(nxt)
    if due:
        minutes = max(5, int(float(db.get_setting("targets_sync_minutes", "60") or 60)))
        try:
            out["definitions"] = sync_definitions(now)
        except Exception as e:
            db.set_setting("targets_last_error", f"{now:%Y-%m-%d %H:%M} UTC: {str(e)[:300]}")
            db.set_setting("targets_next_run", (now + timedelta(minutes=30)).isoformat())
            raise
        db.set_setting("targets_last_ok", now.isoformat())
        db.set_setting("targets_last_error", "")
        db.set_setting("targets_next_run", (now + timedelta(minutes=minutes)).isoformat())
    out["usage"] = poll_counts(now)
    return out
