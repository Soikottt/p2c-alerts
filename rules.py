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
                    WHERE c.source = 'live' AND c.created_at >= :since {extra_where}
                    ORDER BY c.call_at, c.call_id""", since=since)


def rule_new_and_lapsed_publisher(now):
    nr, lr = alerts.get_rule("new_publisher"), alerts.get_rule("lapsed_publisher")
    new_on, lap_on = bool(nr and nr["enabled"]), bool(lr and lr["enabled"])
    if not (new_on or lap_on):
        return 0
    
    n_first = int((nr or {}).get("param_count") or 10)
    lapse_days = int((lr or {}).get("param_days") or 30)
    made = 0
    
    recent_calls = _recent_live_calls(now)
    if not recent_calls:
        return 0

    # 1. Get all unique publisher IDs from the recent calls
    pub_ids = list({c["publisher_id"] for c in recent_calls if c["publisher_id"]})
    if not pub_ids:
        return 0

    # 2. Fetch ALL historical calls for these publishers in ONE query
    hist_calls = db.q_in("SELECT call_id, publisher_id, call_at FROM calls WHERE publisher_id IN :ids ORDER BY call_at", "ids", pub_ids)
    
    # 3. Group the history by publisher ID
    from collections import defaultdict
    pub_history = defaultdict(list)
    for hc in hist_calls:
        pub_history[hc["publisher_id"]].append(hc)

    # 4. Process the recent calls using the pre-loaded history
    for c in recent_calls:
        if not c["publisher_id"]:
            continue
        t = to_dt(c["call_at"])
        
        # Find the rank in Python (much faster than querying the database)
        history = pub_history[c["publisher_id"]]
        prev_calls = [h for h in history if h["call_at"] < t or (h["call_at"] == t and h["call_id"] <= c["call_id"])]
        rank = len(prev_calls)

        if new_on and rank <= n_first:
            ctx = {**_call_ctx(c), "call_number": rank, "first_n": n_first}
            if alerts.create_alert("new_publisher", f"newpub:{c['publisher_id']}:{c['call_id']}", ctx,
                                   publisher_id=c["publisher_id"], buyer_id=c["buyer_id"]):
                made += 1

        if lap_on and rank > 1:
            prev_call = prev_calls[-2]
            prev_t = to_dt(prev_call["call_at"])
            if prev_t and (t - prev_t) > timedelta(days=lapse_days):
                ctx = {**_call_ctx(c), "gap_days": (t - prev_t).days, "last_call_time": fmt(prev_t)}
                if alerts.create_alert("lapsed_publisher", f"lapsed:{c['publisher_id']}:{c['call_id']}", ctx,
                                       publisher_id=c["publisher_id"], buyer_id=c["buyer_id"]):
                    made += 1
    return made


def rule_watchlist_buyer(now):
    if not alerts.rule_enabled("watchlist_buyer"):
        return 0
    made = 0
    for c in _recent_live_calls(now, "AND c.connected = TRUE AND b.watchlist = TRUE"):
        if alerts.create_alert("watchlist_buyer", f"watch:{c['call_id']}", _call_ctx(c),
                               publisher_id=c["publisher_id"], buyer_id=c["buyer_id"]):
            made += 1
    return made


def cap_usage(cap, now):
    """How much of a cap is used right now. Returns dict(usage, pct, start, key, name)."""
    start, key = window_start(cap["period"], now)
    col = ENTITY_COL[cap["entity_type"]]
    metric = METRIC_SQL.get(cap["metric"], METRIC_SQL["paid"])
    usage = db.q1(f"SELECT COUNT(*) AS n FROM calls c WHERE c.{col}=:e AND c.call_at >= :s AND {metric}",
                  e=cap["entity_id"], s=start)["n"]
    table = "publishers" if cap["entity_type"] == "publisher" else "buyers"
    ent = db.q1(f"SELECT name FROM {table} WHERE id=:i", i=cap["entity_id"])
    pct = usage / cap["cap_value"] * 100 if cap["cap_value"] else 0
    return {"usage": usage, "pct": pct, "start": start, "key": key, "name": ent["name"] if ent else "?"}


def rule_caps(now):
    made = 0
    for cap in db.q("SELECT * FROM caps WHERE active = TRUE"):
        atype = f"{cap['entity_type']}_cap"
        if not alerts.rule_enabled(atype):
            continue
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


def rule_inactive_dids(now):
    rule = alerts.get_rule("did_inactive")
    if not rule or not rule["enabled"]:
        return 0
    days = int(rule["param_days"] or 30)
    made = 0
    import cases
    import dids as didtools
    pub_names = {p["id"]: p["name"] for p in db.q("SELECT id, name FROM publishers")}
    active = db.q("SELECT * FROM dids WHERE status = 'active'")
    camp = didtools.campaign_names(active)
    calls30 = didtools.calls_last_30d(now)
    notes = cases.latest_by_did()
    for d in active:
        ref = to_dt(d["last_call_at"]) or to_dt(d["added_at"])
        if not ref:
            continue
        idle = (now - ref).days
        if idle < days:
            continue
        cycle = idle // days
        ctx = {"did": d["number"], "label": d["label"] or "", "label_part": f" ({d['label']})" if d["label"] else "",
               "idle_days": idle, "last_call": fmt(d["last_call_at"]) if d["last_call_at"] else "never",
               "added": fmt(d["added_at"]), "publisher": pub_names.get(d["publisher_id"]) or d["affiliate_name"] or "none",
               "next_charge": fmt(d["next_charge_at"], False) if d["next_charge_at"] else "unknown",
               "monthly_cost": f"${float(d['monthly_cost']):.2f}" if d["monthly_cost"] is not None else "not set",
               "campaign": camp.get(d["id"]) or "unknown", "calls_30d": calls30.get(d["id"], 0),
               "last_note": cases.one_line(notes.get(d["id"]), fmt) or "none yet"}
        if alerts.create_alert("did_inactive", f"did:{d['id']}:{ref.date()}:{cycle}", ctx, publisher_id=d["publisher_id"],
                               snooze_key=f"did:{d['id']}"):
            made += 1
    return made


def run_all(now=None):
    now = now or db.now_utc()
    out = {}
    for name, fn in [("publishers", rule_new_and_lapsed_publisher), ("watchlist", rule_watchlist_buyer),
                     ("caps", rule_caps), ("dids", rule_inactive_dids)]:
        out[name] = fn(now)
    return out
