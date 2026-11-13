"""Saves Ringba calls into the database (insert new, update changed). Never alerts by itself."""
import time
import traceback
from datetime import timedelta

import db
import ringba_client as rc

CALL_COLS = ["call_id", "call_at", "publisher_id", "buyer_id", "did_id", "campaign_name", "campaign_rid", "target_name",
             "caller_number", "connected", "converted", "paid", "is_live", "is_duplicate", "blocked",
             "incomplete", "duration_sec", "connected_sec", "revenue", "payout", "profit", "telco_cost",
             "source", "created_at", "updated_at"]
UPDATE_COLS = [c for c in CALL_COLS if c not in ("call_id", "source", "created_at")]
CHUNK_PAUSE = 1.0   # seconds between day-chunks during a big backfill (avoids Ringba HTTP 429)


def _check(records, report):
    """Refuse to save data we cannot trust (for example no DID column in Ringba's answer)."""
    if report["missing"]:
        raise rc.RingbaError("Ringba's answer is missing: " + ", ".join(report["missing"])
                             + ". Keys it returned: " + ", ".join(report.get("first_row_keys", [])[:25]))
    if records and not any(r["did"] for r in records):
        raise rc.RingbaError("Ringba returned calls but no dialed number (DID). Press 'Test Ringba connection' "
                             "in Settings and send me the key list.")


def _resolve_entity(table, rid_col, records, rid_key, name_key, now):
    """Find (or create) the publisher/buyer rows for these records. Returns {(rid, name_lower): db_id}."""
    rows = db.q(f"SELECT id, {rid_col} AS rid, name FROM {table}")
    by_rid = {r["rid"]: r for r in rows if r["rid"]}
    by_name = {(r["name"] or "").strip().lower(): r for r in rows}
    seen = {}
    for rec in records:
        rid, name = rec[rid_key], rec[name_key]
        if not rid and not name:
            continue
        k = (rid, (name or "").strip().lower())
        if k not in seen or rec["call_at"] < seen[k][1]:
            seen[k] = (name, rec["call_at"])
    mapping = {}
    for (rid, nlow), (name, first) in seen.items():
        row = by_rid.get(rid) if rid else None
        if row is None and nlow:
            cand = by_name.get(nlow)
            if cand and (not cand["rid"] or not rid):   # a hand-added entry, or Ringba gave no id
                row = cand
        if row is None:
            if table == "publishers":
                new = db.q(f"INSERT INTO publishers (ringba_publisher_id, name, status, first_seen_at, created_at) "
                           f"VALUES (:r,:n,'active',:f,:now) RETURNING id", r=rid, n=name or rid, f=first, now=now)
            else:
                new = db.q(f"INSERT INTO buyers (ringba_buyer_id, name, status, created_at) "
                           f"VALUES (:r,:n,'active',:now) RETURNING id", r=rid, n=name or rid, now=now)
            row = {"id": new[0]["id"], "rid": rid, "name": name}
            if rid:
                by_rid[rid] = row
            by_name.setdefault(nlow, row)
        elif rid and not row["rid"]:
            db.x(f"UPDATE {table} SET {rid_col}=:r WHERE id=:i", r=rid, i=row["id"])  # link hand-added entry
            row["rid"] = rid
            by_rid[rid] = row
        mapping[(rid, nlow)] = row["id"]
    return mapping


def _resolve_dids(records, now):
    rows = db.q("SELECT id, number, ringba_number_id FROM dids")
    by_num = {r["number"]: r for r in rows}
    first = {}
    for rec in records:
        n = rec["did"]
        if n and (n not in first or rec["call_at"] < first[n][0]):
            first[n] = (rec["call_at"], rec["did_rid"])
    mapping = {}
    for num, (when, rid) in first.items():
        row = by_num.get(num)
        if row is None:
            new = db.q("INSERT INTO dids (number, ringba_number_id, status, added_at) VALUES (:n,:r,'active',:a) "
                       "RETURNING id", n=num, r=rid, a=when)
            row = {"id": new[0]["id"], "number": num, "ringba_number_id": rid}
            by_num[num] = row
        elif rid and not row["ringba_number_id"]:
            db.x("UPDATE dids SET ringba_number_id=:r WHERE id=:i", r=rid, i=row["id"])
        mapping[num] = row["id"]
    return mapping


def upsert_calls(records, source="live"):
    """Insert new calls, refresh existing ones. source='live' calls may trigger alerts; 'backfill' never do."""
    now = db.now_utc()
    stats = {"fetched": len(records), "inserted": 0, "updated": 0}
    if not records:
        return stats
    pubs = _resolve_entity("publishers", "ringba_publisher_id", records, "publisher_rid", "publisher_name", now)
    buys = _resolve_entity("buyers", "ringba_buyer_id", records, "buyer_rid", "buyer_name", now)
    dids = _resolve_dids(records, now)

    ids = [r["call_id"] for r in records]
    existing = set()
    for i in range(0, len(ids), 300):
        for r in db.q_in("SELECT call_id FROM calls WHERE call_id IN :ids", "ids", ids[i:i + 300]):
            existing.add(r["call_id"])

    rows = []
    for rec in records:
        pk = (rec["publisher_rid"], (rec["publisher_name"] or "").strip().lower())
        bk = (rec["buyer_rid"], (rec["buyer_name"] or "").strip().lower())
        rows.append({
            "call_id": rec["call_id"], "call_at": rec["call_at"],
            "publisher_id": pubs.get(pk), "buyer_id": buys.get(bk), "did_id": dids.get(rec["did"]),
            "campaign_name": rec["campaign_name"], "campaign_rid": rec.get("campaign_rid"), "target_name": rec["target_name"],
            "caller_number": rec["caller"], "connected": rec["connected"], "converted": rec["converted"],
            "paid": rec["paid"], "is_live": rec["is_live"], "is_duplicate": rec["is_duplicate"],
            "blocked": rec["blocked"], "incomplete": rec["incomplete"],
            "duration_sec": rec["duration_sec"], "connected_sec": rec["connected_sec"],
            "revenue": rec["revenue"], "payout": rec["payout"], "profit": rec["profit"],
            "telco_cost": rec["telco_cost"], "source": source, "created_at": now, "updated_at": now,
        })
    db.bulk_upsert("calls", CALL_COLS, rows, "call_id", UPDATE_COLS)
    stats["inserted"] = sum(1 for r in rows if r["call_id"] not in existing)
    stats["updated"] = len(rows) - stats["inserted"]

    did_ids = sorted({r["did_id"] for r in rows if r["did_id"]})
    if did_ids:
        db.x_in("UPDATE dids SET last_call_at = (SELECT MAX(call_at) FROM calls WHERE calls.did_id = dids.id) "
                "WHERE id IN :ids", "ids", did_ids)
    return stats


def _start_run(kind, start, end):
    return db.q("INSERT INTO sync_runs (kind, started_at, window_start, window_end, status) "
                "VALUES (:k,:s,:ws,:we,'running') RETURNING id", k=kind, s=db.now_utc(), ws=start, we=end)[0]["id"]


def _finish_run(run_id, status, stats=None, error=None):
    stats = stats or {}
    db.x("UPDATE sync_runs SET finished_at=:f, status=:st, rows_fetched=:a, rows_inserted=:b, rows_updated=:c, error=:e "
         "WHERE id=:i", f=db.now_utc(), st=status, a=stats.get("fetched", 0), b=stats.get("inserted", 0),
         c=stats.get("updated", 0), e=(error or None) and str(error)[:1500], i=run_id)


def live_sync(hours=None):
    """Re-read the last few hours from Ringba. New calls may alert; old ones are just refreshed."""
    hours = hours or float(db.get_setting("live_lookback_hours", "6"))
    end = db.now_utc()
    start = end - timedelta(hours=hours)
    run_id = _start_run("live", start, end)
    try:
        records, report = rc.fetch_range(start, end)
        _check(records, report)
        stats = upsert_calls(records, source="live")
        _finish_run(run_id, "ok", stats)
        return stats
    except Exception as e:
        _finish_run(run_id, "error", error=f"{e}")
        raise


def refresh_one_day(now=None):
    """Ringba keeps changing a call after it ended (conversion, payout, revenue, duplicate / blocked flags). live_sync only
    re-reads the last few hours, so older calls would keep their first, incomplete values. Every run therefore re-reads ONE
    24-hour window, moving one window further back each time (last 24 h, the 24 h before, ...), and starts over after `refresh_days` (default 35) days.
    Missing calls found this way are saved as 'backfill' (they never alert). Returns stats + which window."""
    now = now or db.now_utc()
    days = max(int(float(db.get_setting("refresh_days", "35") or 0)), 0)
    if days <= 0:
        return None
    k = int(float(db.get_setting("refresh_cursor", "0") or 0))
    if k < 0 or k >= days:
        k = 0
    start, end = now - timedelta(hours=24 * (k + 1)), now - timedelta(hours=24 * k)      # k=0: the last 24 h, k=1: the 24 h before ...
    records, report = rc.fetch_range(start, end)
    _check(records, report)
    stats = upsert_calls(records, source="backfill")
    db.set_setting("refresh_cursor", k + 1 if k + 1 < days else 0)
    stats["window"] = f"{start:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M} UTC"
    stats["day_back"] = k
    return stats


def backfill_range(start, end, progress=None, source="backfill", step_hours=24):
    """Load history day-by-day. Safe to run again: existing calls are just updated. Never sends alerts."""
    run_id = _start_run("backfill", start, end)
    total = {"fetched": 0, "inserted": 0, "updated": 0}
    failures = []
    chunks = []
    cur = start
    while cur < end:
        nxt = min(cur + timedelta(hours=step_hours), end)
        chunks.append((cur, nxt))
        cur = nxt
    for i, (a, b) in enumerate(chunks, 1):
        try:
            records, report = rc.fetch_range(a, b)
            _check(records, report)
            s = upsert_calls(records, source=source)
            for k in total:
                total[k] += s[k]
        except Exception as e:
            failures.append(f"{a:%Y-%m-%d}: {e}")
            traceback.print_exc()
        if progress:
            progress(i, len(chunks), f"{b:%Y-%m-%d}", total)
        time.sleep(CHUNK_PAUSE)
    status = "ok" if not failures else ("partial" if total["fetched"] else "error")
    _finish_run(run_id, status, total, error="; ".join(failures)[:1500] if failures else None)
    total["failures"] = failures
    return total


def backfill_days(days=90, progress=None):
    end = db.now_utc()
    return backfill_range(end - timedelta(days=days), end, progress)


def import_csv_text(text, source="backfill"):
    """Load a CSV you downloaded from Ringba by hand (same columns as the API export)."""
    records, report = rc.parse_csv(text)
    if report["missing"]:
        return None, report
    run_id = _start_run("csv_import", None, None)
    try:
        stats = upsert_calls(records, source=source)
        _finish_run(run_id, "ok", stats)
    except Exception as e:
        _finish_run(run_id, "error", error=e)
        raise
    return stats, report
