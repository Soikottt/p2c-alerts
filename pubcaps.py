"""Publisher caps per campaign, read from Ringba (the payout rules of each campaign: dailyCap / monthlyCap).
Rows are saved in the same `caps` table the manual caps use (source = 'ringba'), so the usual cap alerts work unchanged.
Your own settings are never overwritten: the alert levels, the Active tick and the notes. A cap you typed yourself for the same
publisher + campaign + period always wins (no second row is made)."""
from datetime import timedelta

import db
import ringba_client as rc

DEFAULT_THRESHOLDS = "80,100"


def sync(now=None):
    """Read the campaigns from Ringba and update the publisher caps. Returns stats."""
    now = now or db.now_utc()
    items, report = rc.fetch_publisher_caps()
    by_rid, by_name = {}, {}
    for p in db.q("SELECT id, name, ringba_publisher_id FROM publishers"):
        if p["ringba_publisher_id"]:
            by_rid[str(p["ringba_publisher_id"]).lower()] = p["id"]
        by_name[(p["name"] or "").strip().lower()] = p["id"]
    existing = {r["ringba_key"]: r for r in db.q("SELECT id, ringba_key, cap_value, active, notes FROM caps WHERE source='ringba'")}
    manual = {(r["entity_id"], r["campaign_name"] or "", r["period"]) for r in
              db.q("SELECT entity_id, campaign_name, period FROM caps WHERE entity_type='publisher' AND COALESCE(source,'manual') <> 'ringba'")}
    added = updated = skipped_manual = 0
    unmatched, seen = [], set()
    for it in items:
        pid = by_rid.get((it["pub_rid"] or "").lower()) or by_name.get((it["pub_name"] or "").strip().lower())
        if pid is None:
            unmatched.append(it["pub_name"] or it["pub_rid"] or "?")
            continue
        for period, cap in (("daily", it["daily_cap"]), ("monthly", it["monthly_cap"])):
            if not cap:
                continue
            key = f"{it['campaign_id']}:{it['pub_rid'] or it['pub_name']}:{period}"
            seen.add(key)
            if (pid, it["campaign_name"] or "", period) in manual:
                skipped_manual += 1
                continue
            row = existing.get(key)
            if row is None:
                note = "From Ringba" + (" (campaign default payout)" if it["inherited"] else "") + \
                       (f" - {it['rule_count']} payout rules have a cap, the first is used" if it["rule_count"] > 1 else "")
                db.x("INSERT INTO caps (entity_type, entity_id, campaign_name, period, metric, cap_value, thresholds, active, notes, source, ringba_key) "
                     "VALUES ('publisher',:e,:c,:p,'paid',:v,:t,TRUE,:n,'ringba',:k)",
                     e=pid, c=it["campaign_name"], p=period, v=cap, t=DEFAULT_THRESHOLDS, n=note, k=key)
                added += 1
            else:
                sets, params = [], {"i": row["id"]}
                if int(row["cap_value"]) != int(cap):
                    sets.append("cap_value=:v"); params["v"] = cap
                if (row["notes"] or "") == "Ringba: cap removed":      # the cap is back
                    sets += ["active=TRUE", "notes='From Ringba'"]
                if sets:
                    db.x(f"UPDATE caps SET {', '.join(sets)} WHERE id=:i", **params)
                    updated += 1
    removed = 0
    if report["with_caps"]:                    # an empty answer is more likely a glitch than 'every cap was removed'
        for key, row in existing.items():
            if key not in seen and row["active"]:
                db.x("UPDATE caps SET active=FALSE, notes='Ringba: cap removed' WHERE id=:i", i=row["id"])
                removed += 1
    return {"campaigns": report["campaigns"], "with_caps": report["with_caps"], "added": added, "updated": updated,
            "removed": removed, "skipped_manual": skipped_manual, "unmatched": sorted(set(unmatched)), "report": report}


def run(now=None, force=False):
    """Called by the engine. Returns stats, or None when it is not time yet."""
    now = now or db.now_utc()
    nxt = db.get_setting("pubcaps_next_run", "")
    if not force and nxt and now < db.to_dt(nxt):
        return None
    minutes = max(5, int(float(db.get_setting("pubcaps_sync_minutes", "60") or 60)))
    try:
        out = sync(now)
    except Exception as e:
        db.set_setting("pubcaps_last_error", f"{now:%Y-%m-%d %H:%M} UTC: {str(e)[:300]}")
        db.set_setting("pubcaps_next_run", (now + timedelta(minutes=30)).isoformat())
        raise
    db.set_setting("pubcaps_last_ok", now.isoformat())
    db.set_setting("pubcaps_last_error", "")
    db.set_setting("pubcaps_next_run", (now + timedelta(minutes=minutes)).isoformat())
    return out
