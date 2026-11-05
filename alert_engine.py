from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import config
import db
import emailer
from ringba_client import fetch_calls

NY = ZoneInfo("America/New_York")
RESET_TIME = timedelta(hours=23, minutes=30) # 11:30 PM EST

def get_period_key(period):
    """Calculates the current period key (e.g., '2026-11-05' or '2026-11')."""
    now = datetime.now(NY)
    if period == "daily":
        return (now - RESET_TIME).strftime("%Y-%m-%d")
    return now.strftime("%Y-%m")

def get_period_start(period):
    """Calculates the exact datetime when the current period started."""
    now = datetime.now(NY)
    if period == "daily":
        return (now - RESET_TIME).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)

def process_call(c):
    # 1. Check if we already processed this call
    if db.q("SELECT 1 FROM calls WHERE ringba_call_id=:k", k=c["call_id"]):
        return

    # 2. Ensure Publisher, Buyer, and DID exist in DB
    if c["publisher_id"]:
        db.x("INSERT INTO publishers (ringba_publisher_id, name) VALUES (:r, :n) ON CONFLICT (ringba_publisher_id) DO UPDATE SET name=EXCLUDED.name", r=c["publisher_id"], n=c["publisher_name"])
    if c["buyer_id"]:
        db.x("INSERT INTO buyers (ringba_buyer_id, name) VALUES (:r, :n) ON CONFLICT (ringba_buyer_id) DO UPDATE SET name=EXCLUDED.name", r=c["buyer_id"], n=c["buyer_name"])
    if c["did"]:
        db.x("INSERT INTO dids (number) VALUES (:n) ON CONFLICT (number) DO NOTHING", n=c["did"])

    # Get their DB IDs
    pub = db.q("SELECT * FROM publishers WHERE ringba_publisher_id=:r", r=c["publisher_id"])[0] if c["publisher_id"] else None
    buy = db.q("SELECT * FROM buyers WHERE ringba_buyer_id=:r", r=c["buyer_id"])[0] if c["buyer_id"] else None
    did = db.q("SELECT * FROM dids WHERE number=:n", n=c["did"])[0] if c["did"] else None

    # 3. Save the call
    db.x("""INSERT INTO calls (ringba_call_id, publisher_id, buyer_id, did_id, started_at, connected, duration_sec)
            VALUES (:k, :p, :b, :d, :s, :c, :dur)""",
         k=c["call_id"], p=pub["id"] if pub else None, b=buy["id"] if buy else None, 
         d=did["id"] if did else None, s=c["started_at"], c=c["connected"], dur=c["duration"])

    if did:
        db.x("UPDATE dids SET last_call_at=:s WHERE id=:i", s=c["started_at"], i=did["id"])

    # RULE 1: New Publisher (first 10 calls)
    if pub:
        first_call = pub["first_call_at"]
        calls_since = pub["calls_since_first"]
        
        if first_call is None or calls_since < 10:
            new_count = 1 if first_call is None else calls_since + 1
            _send_alert("new_publisher", f"new_pub:{pub['id']}:{c['call_id']}", 
                        f"New Publisher Call #{new_count}: {pub['name']}", 
                        f"Publisher: {pub['name']}\nCall #{new_count} of first 10\nDID: {c['did']}\nConnected: {c['connected']}",
                        publisher=pub)
        else:
            new_count = calls_since

        db.x("""UPDATE publishers SET first_call_at=COALESCE(first_call_at, :s), 
                last_call_at=:s, total_calls=total_calls+1, calls_since_first=:n WHERE id=:i""",
             s=c["started_at"], n=new_count, i=pub["id"])

    # RULE 2: Publisher Reactivated (no connected call in 30 days)
    if pub and c["connected"]:
        last_conn = pub["last_connected_call_at"]
        if last_conn and (c["started_at"] - last_conn) > timedelta(days=30):
            _send_alert("publisher_lapsed", f"lapsed_pub:{pub['id']}:{c['started_at'].date()}", 
                        f"Publisher Reactivated: {pub['name']}", 
                        f"{pub['name']} sent a connected call after 30+ days.\nDID: {c['did']}",
                        publisher=pub)
        db.x("UPDATE publishers SET last_connected_call_at=:s WHERE id=:i", s=c["started_at"], i=pub["id"])

    # RULE 3: Watchlist Buyer
    if buy and buy["watchlist"] and c["connected"]:
        _send_alert("watchlist_connect", f"watch_connect:{c['call_id']}", 
                    f"Watchlist Buyer Connected: {buy['name']}", 
                    f"Buyer: {buy['name']}\nPublisher: {c['publisher_name']}\nDID: {c['did']}\nDuration: {c['duration']}s",
                    buyer=buy)

def check_caps():
    rules = db.q("SELECT * FROM cap_rules WHERE active=TRUE")
    for r in rules:
        ent = db.q(f"SELECT * FROM {r['entity_type']}s WHERE id=:i", i=r["entity_id"])
        if not ent: continue
        ent = ent[0]
        
        pstart = get_period_start(r["period"])
        usage = db.q(f"SELECT COUNT(*) AS n FROM calls WHERE {r['entity_type']}_id=:i AND started_at>=:s", i=r["entity_id"], s=pstart)[0]["n"]
        
        pct = (usage / r["cap_value"] * 100) if r["cap_value"] else 0
        
        if pct >= r["alert_threshold_pct"]:
            key = f"cap:{r['id']}:{get_period_key(r['period'])}:{r['alert_threshold_pct']}"
            atype = f"{r['entity_type']}_cap"
            _send_alert(atype, key, f"CAP ALERT: {ent['name']} at {pct:.0f}%",
                        f"{r['entity_type'].title()}: {ent['name']}\nUsage: {usage}/{r['cap_value']} ({r['period']})",
                        buyer=ent if r['entity_type']=='buyer' else None,
                        publisher=ent if r['entity_type']=='publisher' else None)

def check_inactive_dids():
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    rows = db.q("SELECT * FROM dids WHERE status='active' AND (last_call_at IS NULL OR last_call_at < :c)", c=cutoff)
    for d in rows:
        key = f"did_inactive:{d['id']}"
        if not db.q("SELECT 1 FROM alerts_log WHERE dedupe_key=:k AND sent_at > NOW() - INTERVAL '30 days'", k=key):
            _send_alert("did_inactive", key, f"Inactive DID: {d['number']}", f"No calls in 30 days.\nDID: {d['number']}")

def _send_alert(alert_type, dedupe_key, subject, body, buyer=None, publisher=None):
    if db.q("SELECT 1 FROM alerts_log WHERE dedupe_key=:k", k=dedupe_key):
        return # Already sent
    
    admin = db.get_setting("admin_email")
    if not admin: return

    # Get CC routing
    route = db.q("SELECT * FROM cc_routing WHERE alert_type=:t", t=alert_type)
    route = route[0] if route else {"cc_buyer": False, "cc_publisher": False}
    
    cc = []
    if route["cc_buyer"] and buyer and buyer.get("cc_on_alerts") and buyer.get("alert_email"):
        cc.append(buyer["alert_email"])
    if route["cc_publisher"] and publisher and publisher.get("cc_on_alerts") and publisher.get("alert_email"):
        cc.append(publisher["alert_email"])

    emailer.send_email(admin, subject, body, cc)
    db.log_alert(alert_type, dedupe_key, str([admin]+cc), body[:200])

def run():
    print(f"[{datetime.now()}] Starting Alert Engine...")
    
    # 1. Fetch new calls (lookback 1 hour for safety)
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=1)
    
    try:
        calls = fetch_calls(start, now)
        print(f"Fetched {len(calls)} calls from Ringba.")
        for c in sorted(calls, key=lambda x: x["started_at"]):
            process_call(c)
    except Exception as e:
        print("Error fetching calls:", e)

    # 2. Check Caps
    check_caps()
    
    # 3. Check Inactive DIDs (runs every time, deduplication handles spam)
    check_inactive_dids()
    
    print("Done.")

def backfill_history(days=180):
    """Pulls historical calls to populate the database without sending emails."""
    print(f"Starting backfill for last {days} days...")
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    
    # Temporarily disable email sending
    global _send_alert
    original_send_alert = _send_alert
    def dummy_send_alert(*args, **kwargs): pass
    _send_alert = dummy_send_alert
    
    try:
        calls = fetch_calls(start, now)
        print(f"Fetched {len(calls)} total calls from Ringba. Processing...")
        for c in sorted(calls, key=lambda x: x["started_at"]):
            process_call(c)
        print("Backfill complete.")
    except Exception as e:
        print("Backfill error:", e)
    finally:
        # Restore email sending
        _send_alert = original_send_alert

if __name__ == "__main__":
    run()
