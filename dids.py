"""Add / update DIDs in bulk: pasted lines or a number list exported from Ringba."""
import csv
import io
import re
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo

from dateutil.parser import parse as parse_date

import db
import ringba_client as rc

NY = ZoneInfo("America/New_York")


def parse_when(s):
    s = (s or "").strip()
    if not s:
        return None
    try:
        d = parse_date(s)
    except (ValueError, OverflowError):
        return None
    return (d if d.tzinfo else d.replace(tzinfo=NY)).astimezone(timezone.utc)


def parse_lines(text):
    """'+16145550100, Rehab line, 2026-06-12 09:30' per line (label and date optional)."""
    items, bad = [], 0
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split(",")]
        num = rc.normalize_phone(parts[0])
        if not num:
            bad += 1
            continue
        items.append({"number": num, "label": parts[1] if len(parts) > 1 and parts[1] else None,
                      "created_at": parse_when(parts[2]) if len(parts) > 2 else None, "rid": None})
    return items, bad


def parse_export(text):
    """A CSV exported from Ringba's Numbers page. Finds the number / created / name columns by themselves."""
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    heads = reader.fieldnames or []
    low = {h: rc.norm(h) for h in heads}

    def pick(exact, contains):
        for h, n in low.items():
            if n in exact:
                return h
        for h, n in low.items():
            if any(c in n for c in contains):
                return h
        return None
    c_num = pick({"number", "phonenumber", "did", "trackingnumber", "e164", "numbere164"}, ["number", "phone"])
    c_created = pick({"created", "createdat", "createddate", "createdon", "datecreated", "createdtime", "added"}, ["creat", "added", "date"])
    c_label = pick({"name", "label", "numbername", "description"}, ["name", "label"])
    c_id = pick({"id", "numberid"}, ["numberid"])
    if c_label == c_num:
        c_label = None
    items, bad = [], 0
    if not c_num:
        return [], len(list(reader)), {"error": "I could not find a phone number column.", "headers": heads}
    for r in reader:
        num = rc.normalize_phone(r.get(c_num))
        if not num:
            bad += 1
            continue
        items.append({"number": num, "label": rc.as_str(r.get(c_label)) if c_label else None,
                      "created_at": parse_when(r.get(c_created)) if c_created else None,
                      "rid": rc.as_str(r.get(c_id)) if c_id else None})
    return items, bad, {"number": c_num, "created": c_created, "label": c_label, "id": c_id}


def _publisher_lookup():
    pubs = db.q("SELECT id, ringba_publisher_id, name FROM publishers")
    return ({p["ringba_publisher_id"]: p["id"] for p in pubs if p["ringba_publisher_id"]},
            {(p["name"] or "").strip().lower(): p["id"] for p in pubs})


def upsert_dids(items):
    """New numbers are added (clock starts at their created date, or now). Existing ones get created date, label,
    publisher and next-charge date kept up to date. Items may come from Ringba, a pasted list or a file."""
    now = db.now_utc()
    have = {r["number"]: r for r in db.q("SELECT id, number, label, ringba_number_id, added_at, publisher_id, "
                                         "affiliate_name, next_charge_at FROM dids")}
    by_rid, by_name = _publisher_lookup()
    added = updated = skipped = 0
    for it in items:
        aff_id, aff_name, nxt = it.get("affiliate_id"), it.get("affiliate_name"), it.get("next_charge")
        pid = by_rid.get(aff_id) or by_name.get((aff_name or "").strip().lower()) if (aff_id or aff_name) else None
        row = have.get(it["number"])
        if row is None:
            db.x("INSERT INTO dids (number, ringba_number_id, label, status, added_at, publisher_id, affiliate_name, next_charge_at) "
                 "VALUES (:n,:r,:l,'active',:a,:p,:an,:nc)", n=it["number"], r=it["rid"], l=it["label"],
                 a=it["created_at"] or now, p=pid, an=aff_name, nc=nxt)
            have[it["number"]] = {"id": 0, "number": it["number"], "label": it["label"], "ringba_number_id": it["rid"],
                                  "added_at": it["created_at"] or now, "publisher_id": pid, "affiliate_name": aff_name,
                                  "next_charge_at": nxt}
            added += 1
            continue
        cur = db.to_dt(row["added_at"])
        new_date = bool(it["created_at"]) and (cur is None or abs((it["created_at"] - cur).total_seconds()) > 1)
        new_label = bool(it["label"]) and not row["label"]
        new_rid = bool(it["rid"]) and not row["ringba_number_id"]
        from_ringba = "affiliate_id" in it                      # only a Ringba sync knows who the DID is assigned to
        new_pub = from_ringba and (pid != row["publisher_id"] or (aff_name or None) != (row["affiliate_name"] or None))
        cur_nc = db.to_dt(row["next_charge_at"]) if row["next_charge_at"] else None
        new_nc = bool(nxt) and (cur_nc is None or abs((nxt - cur_nc).total_seconds()) > 1)
        if new_date or new_label or new_rid or new_pub or new_nc:
            db.x("UPDATE dids SET added_at=COALESCE(:a, added_at), label=COALESCE(label, :l), "
                 "ringba_number_id=COALESCE(ringba_number_id, :r), next_charge_at=COALESCE(:nc, next_charge_at) WHERE number=:n",
                 a=it["created_at"] if new_date else None, l=it["label"], r=it["rid"],
                 nc=nxt if new_nc else None, n=it["number"])
            if new_pub:
                db.x("UPDATE dids SET publisher_id=:p, affiliate_name=:an WHERE number=:n", p=pid, an=aff_name, n=it["number"])
            updated += 1
        else:
            skipped += 1
    return {"added": added, "updated": updated, "skipped": skipped}


# ---------------------------------------------------------------- automatic sync from Ringba
AUTO_MARK = "[auto-retired"


def _retire_missing(seen):
    """DIDs that are no longer in Ringba's list stop alerting. Guard: never retire a suspiciously large share."""
    active = db.q("SELECT id, number, notes FROM dids WHERE status = 'active'")
    gone = [d for d in active if d["number"] not in seen]
    if not gone:
        return 0, 0
    if len(gone) > max(2, 0.5 * len(active)):
        return 0, len(gone)          # looks like a broken / partial answer, do nothing
    stamp = f"{AUTO_MARK}: not in Ringba's number list on {db.now_utc():%Y-%m-%d}]"
    for d in gone:
        notes = f"{d['notes']} {stamp}" if d["notes"] else stamp
        db.x("UPDATE dids SET status='retired', notes=:n WHERE id=:i", n=notes, i=d["id"])
    return len(gone), 0


def _reactivate(seen):
    """A DID that this sync retired and that is back in Ringba becomes active again (hand-retired ones stay retired)."""
    n = 0
    for d in db.q("SELECT id, number, notes FROM dids WHERE status = 'retired'"):
        if d["number"] in seen and d["notes"] and AUTO_MARK in d["notes"]:
            cleaned = re.sub(r"\s*\[auto-retired[^\]]*\]", "", d["notes"]).strip() or None
            db.x("UPDATE dids SET status='active', notes=:n WHERE id=:i", n=cleaned, i=d["id"])
            n += 1
    return n


def sync_from_ringba(retire=None):
    """Read every number on the Ringba account and bring the DID list up to date. Returns a stats dict."""
    import ringba_client as rcl
    items, report = rcl.fetch_numbers()
    res = upsert_dids(items)
    seen = {i["number"] for i in items}
    do_retire = (str(db.get_setting("numbers_auto_retire", "true")).lower() == "true") if retire is None else retire
    res.update(retired=0, retire_skipped=0, reactivated=0)
    if items:
        res["reactivated"] = _reactivate(seen)
        if do_retire:
            res["retired"], res["retire_skipped"] = _retire_missing(seen)
    res["in_ringba"] = len(items)
    res["report"] = report
    return res


def sync_if_due(now=None, force=False):
    """Called by the 5-minute engine. Runs at most once per 'numbers_sync_minutes'. Returns stats, or None if not due."""
    now = now or db.now_utc()
    nxt = db.get_setting("numbers_next_run", "")
    if not force and nxt:
        try:
            if now < db.to_dt(nxt):
                return None
        except Exception:
            pass
    minutes = max(5, int(float(db.get_setting("numbers_sync_minutes", "60") or 60)))
    try:
        res = sync_from_ringba()
    except Exception as e:
        db.set_setting("numbers_last_error", f"{now:%Y-%m-%d %H:%M} UTC: {str(e)[:300]}")
        db.set_setting("numbers_next_run", (now + timedelta(minutes=30)).isoformat())
        raise
    db.set_setting("numbers_last_ok", now.isoformat())
    db.set_setting("numbers_last_error", "")
    db.set_setting("numbers_next_run", (now + timedelta(minutes=minutes)).isoformat())
    return res
