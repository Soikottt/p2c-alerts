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


def _call_lines(c):
    return (f"Time: {fmt(c['call_at'])}\nPublisher: {c.get('pub_name') or '-'}\n"
            f"Buyer: {c.get('buyer_name') or '-'}\nDID (number dialed): {c.get('did') or '-'}\n"
            f"Campaign: {c.get('campaign_name') or '-'}\nConnected: {'Yes' if c['connected'] else 'No'}   "
            f"Converted: {'Yes' if c['converted'] else 'No'}   Paid: {'Yes' if c['paid'] else 'No'}")


def _recent_live_calls(now, extra_where=""):
    since = now - timedelta(hours=LOOKBACK_HOURS)
    return db.q(f"""SELECT c.call_id, c.call_at, c.publisher_id, c.buyer_id, c.connected, c.converted, c.paid,
                           c.campaign_name, p.name AS pub_name, b.name AS buyer_name, d.number AS did
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
    for c in _recent_live_calls(now):
        if not c["publisher_id"]:
            continue
        t = to_dt(c["call_at"])
        rank = db.q1("SELECT COUNT(*) AS n FROM calls WHERE publisher_id=:p AND "
                     "(call_at < :t OR (call_at = :t AND call_id <= :cid))",
                     p=c["publisher_id"], t=t, cid=c["call_id"])["n"]
        if new_on and rank <= n_first:
            body = (f"A call arrived from publisher {c['pub_name']}.\n"
                    f"This is call #{rank} of the first {n_first} from this publisher.\n\n{_call_lines(c)}\n")
            if alerts.create_alert("new_publisher", f"newpub:{c['publisher_id']}:{c['call_id']}",
                                   f"New publisher {c['pub_name']} - call #{rank} of {n_first}", body,
                                   publisher_id=c["publisher_id"], buyer_id=c["buyer_id"]):
                made += 1
        if lap_on and rank > 1:
            prev = db.q1("SELECT MAX(call_at) AS t FROM calls WHERE publisher_id=:p AND "
                         "(call_at < :t OR (call_at = :t AND call_id < :cid))",
                         p=c["publisher_id"], t=t, cid=c["call_id"])["t"]
            prev = to_dt(prev)
            if prev and (t - prev) > timedelta(days=lapse_days):
                gap = (t - prev).days
                body = (f"Publisher {c['pub_name']} just sent a call after {gap} days of silence "
                        f"(previous call: {fmt(prev)}).\n\n{_call_lines(c)}\n")
                if alerts.create_alert("lapsed_publisher", f"lapsed:{c['publisher_id']}:{c['call_id']}",
                                       f"Publisher {c['pub_name']} is back after {gap} days", body,
                                       publisher_id=c["publisher_id"], buyer_id=c["buyer_id"]):
                    made += 1
    return made


def rule_watchlist_buyer(now):
    if not alerts.rule_enabled("watchlist_buyer"):
        return 0
    made = 0
    calls = _recent_live_calls(now, "AND c.connected = TRUE AND b.watchlist = TRUE")
    for c in calls:
        body = f"A call CONNECTED to watchlist buyer {c['buyer_name']}.\n\n{_call_lines(c)}\n"
        if alerts.create_alert("watchlist_buyer", f"watch:{c['call_id']}",
                               f"Watchlist buyer {c['buyer_name']} connected a call", body,
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
        label = METRIC_LABEL.get(cap["metric"], cap["metric"])
        for t in reached:
            key = f"cap:{cap['id']}:{cap['cap_value']}:{u['key']}:{t}"
            if t == top:
                remaining = max(cap["cap_value"] - u["usage"], 0)
                body = (f"{cap['entity_type'].title()} {u['name']} has used {u['pct']:.0f}% of its "
                        f"{cap['period']} cap.\n\nUsage: {u['usage']} of {cap['cap_value']} ({label})\n"
                        f"Remaining: {remaining}\nWindow started: {fmt(u['start'])}\nAlert level: {t}%\n\n"
                        f"Action: contact the {cap['entity_type']} or set up another route before calls are wasted.\n")
                if alerts.create_alert(atype, key, f"CAP {u['pct']:.0f}%: {u['name']} ({cap['period']}, {label})",
                                       body, publisher_id=pub_id, buyer_id=buy_id):
                    made += 1
                    # lower levels crossed at the same moment: mark as done so they never email later
                    for lower in reached:
                        if lower != top:
                            lk = f"cap:{cap['id']}:{cap['cap_value']}:{u['key']}:{lower}"
                            alerts.create_alert(atype, lk, f"(skipped) {u['name']} {lower}%", "skipped",
                                                status="skipped")
    return made


def rule_inactive_dids(now):
    rule = alerts.get_rule("did_inactive")
    if not rule or not rule["enabled"]:
        return 0
    days = int(rule["param_days"] or 30)
    made = 0
    for d in db.q("SELECT * FROM dids WHERE status = 'active'"):
        ref = to_dt(d["last_call_at"]) or to_dt(d["added_at"])
        if not ref:
            continue
        idle = (now - ref).days
        if idle < days:
            continue
        cycle = idle // days
        cost = f"\nMonthly cost: ${float(d['monthly_cost']):.2f}" if d["monthly_cost"] is not None else ""
        last = fmt(d["last_call_at"]) if d["last_call_at"] else f"never (added {fmt(d['added_at'])})"
        body = (f"DID {d['number']}{' (' + d['label'] + ')' if d['label'] else ''} has had no calls for {idle} days.\n"
                f"Last call: {last}{cost}\n\nAction: release this number to avoid the monthly charge, "
                f"or mark it 'retired' on the DIDs tab to stop these alerts.\n")
        if alerts.create_alert("did_inactive", f"did:{d['id']}:{ref.date()}:{cycle}",
                               f"Inactive DID {d['number']} - {idle} days without calls", body):
            made += 1
    return made


def run_all(now=None):
    now = now or db.now_utc()
    out = {}
    for name, fn in [("publishers", rule_new_and_lapsed_publisher), ("watchlist", rule_watchlist_buyer),
                     ("caps", rule_caps), ("dids", rule_inactive_dids)]:
        out[name] = fn(now)
    return out
