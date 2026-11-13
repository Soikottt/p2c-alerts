"""Talks to Ringba using the standard JSON call-log endpoint:

    POST https://api.ringba.com/v2/{accountId}/calllogs

 - valueColumns = list of {"column": "..."} dictionaries
 - formatTimeZone = America/New_York
 - callDt comes back as a 13-digit unix-millisecond number (divided by 1000 -> timezone-aware datetime)
 - rows live in response["callLog"]["data"]  OR  response["report"]["records"]
 - PAGING: Ringba ignores "page"/"pageSize" and returns only 20 rows. The working way is "offset" + "size":
   ask for `size` rows at `offset`, move the offset forward, stop when a request returns no rows.
   A seen_ids set stops the loop if Ringba ever ignores the offset and repeats the same rows.
 - time.sleep(1) between requests; HTTP 429 is retried with a pause

Numbers (DIDs):  GET https://api.ringba.com/v2/{accountId}/numbers  -> JSON array of every tracking number on the account
"""
import csv
import hashlib
import io
import json
import re
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
from dateutil.parser import isoparse

import config

NY = ZoneInfo("America/New_York")
BASE = "https://api.ringba.com/v2"
PAGE_SIZE = 100          # rows asked for per request ("size")
PAGE_PAUSE = 1.0         # seconds between requests (avoids HTTP 429)
MAX_REQUESTS = 5000      # hard stop so a misbehaving API can never loop forever


class RingbaError(Exception):
    pass


# ---- time budget: the 5-minute engine must never run past its slot. Backfills set no budget.
_budget = {"deadline": None}


def set_deadline(seconds=None):
    """Everything below stops (with a clear RingbaError) when this many seconds have passed. None = no limit."""
    _budget["deadline"] = (time.monotonic() + seconds) if seconds else None


def time_left():
    d = _budget["deadline"]
    return None if d is None else d - time.monotonic()


def _check_time():
    left = time_left()
    if left is not None and left <= 0:
        raise RingbaError("This run ran out of time talking to Ringba. It will continue on the next run.")


def _nap(seconds):
    """sleep, but never beyond the budget"""
    left = time_left()
    if left is not None:
        if left <= 0:
            _check_time()
        seconds = min(seconds, max(left, 0))
    time.sleep(seconds)


def _request_timeout(default):
    left = time_left()
    return default if left is None else max(5, min(default, left))


# The nine columns you required ...
REQUIRED_COLUMNS = ["callDt", "inboundCallId", "publisherId", "publisherName", "buyerId", "buyer",
                    "inboundPhoneNumber", "hasConnected", "connectedCallLengthInSeconds"]
# ... plus the ones the alerts and caps need. IMPORTANT: inboundPhoneNumber is the CALLER's number.
# The DID (the number that was dialed) is tag:DialedNumber:Number / number / numberId.
CORE_EXTRA = ["tag:DialedNumber:Number", "number", "numberId", "hasConverted", "hasPayout"]
FULL_EXTRA = ["targetName", "campaignName", "campaignId", "isLive", "isDuplicate", "wasBlocked", "isIncomplete",
              "callLengthInSeconds", "conversionAmount", "payoutAmount", "profitNet", "telcoCost"]
COLUMN_SETS = [REQUIRED_COLUMNS + CORE_EXTRA + FULL_EXTRA, REQUIRED_COLUMNS + CORE_EXTRA, REQUIRED_COLUMNS]
_working_set = {"i": 0}   # remembers which set Ringba accepted

# For each thing we store: the response keys we accept (first match wins).
KEYS = {
    "call_id": ["inboundCallId", "Inbound Call ID"],
    "call_at": ["callDt", "Call Date"],
    "call_iso": ["tag:Date:ISODate"],
    "publisher_id": ["publisherId", "Publisher ID"],
    "publisher_name": ["publisherName", "Publisher"],
    "buyer_id": ["buyerId", "Buyer ID"],
    "buyer_name": ["buyer", "buyerName", "Buyer"],
    "target_name": ["targetName", "Target"],
    "campaign_name": ["campaignName", "Campaign"],
    "campaign_id": ["campaignId", "Campaign ID"],
    "caller": ["inboundPhoneNumber", "Caller ID"],
    "did": ["tag:DialedNumber:Number", "number", "dialedNumber", "Number"],
    "did_id": ["numberId", "Number ID"],
    "connected": ["hasConnected", "Connected"],
    "converted": ["hasConverted", "Converted"],
    "paid": ["hasPayout", "Paid Out"],
    "is_live": ["isLive", "Is Live"],
    "duplicate": ["isDuplicate", "Is Duplicate"],
    "blocked": ["wasBlocked", "Blocked"],
    "incomplete": ["isIncomplete", "Incomplete"],
    "duration": ["callLengthInSeconds", "Duration", "tag:CallLength:Total"],
    "connected_len": ["connectedCallLengthInSeconds", "Connected Call Length"],
    "revenue": ["conversionAmount", "Revenue"],
    "payout": ["payoutAmount", "Payout"],
    "profit": ["profitNet", "Profit Net Telco"],
    "telco_cost": ["telcoCost", "Telco Cost"],
}


# ---------- value helpers ----------
def norm(h):
    return re.sub(r"[^a-z0-9]", "", (h or "").lower())


def as_str(v):
    if v is None:
        return None
    s = str(v).strip()
    return None if s == "" or s.lower() in ("none", "null", "nan") else s


def as_bool(v):
    return str(v if v is not None else "").strip().lower() in ("true", "1", "yes", "y", "t")


def as_num(v):
    s = as_str(v)
    if s is None:
        return None
    s = s.replace("$", "").replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def as_secs(v):
    s = as_str(v)
    if s is None:
        return 0
    if ":" in s:
        try:
            parts = [int(float(p)) for p in s.split(":")]
            while len(parts) < 3:
                parts.insert(0, 0)
            return parts[-3] * 3600 + parts[-2] * 60 + parts[-1]
        except ValueError:
            return 0
    try:
        return int(float(s))
    except ValueError:
        return 0


def normalize_phone(v):
    """+16143505503 style. Accepts '(614) 350-5503', '16143505503', etc."""
    s = as_str(v)
    if not s:
        return None
    digits = re.sub(r"\D", "", s)
    if not digits:
        return None
    if len(digits) == 10:
        digits = "1" + digits
    return "+" + digits


def parse_when(iso, raw):
    """callDt: 13-digit unix milliseconds -> timezone-aware UTC datetime (also accepts text dates)."""
    if iso:
        try:
            d = isoparse(str(iso))
            return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
        except ValueError:
            pass
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        n = float(raw)
        return datetime.fromtimestamp(n / 1000.0 if n > 1e11 else n, tz=timezone.utc)
    raw = as_str(raw)
    if not raw:
        return None
    if re.fullmatch(r"\d{12,14}(\.0+)?", raw):
        return datetime.fromtimestamp(float(raw) / 1000.0, tz=timezone.utc)
    for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=NY).astimezone(timezone.utc)
        except ValueError:
            continue
    try:
        d = isoparse(raw)
        return (d if d.tzinfo else d.replace(tzinfo=NY)).astimezone(timezone.utc)
    except ValueError:
        return None


# ---------- row dict -> record ----------
def _find_key(row, field):
    for k in KEYS[field]:
        if k in row:
            return k
    low = {norm(k): k for k in row}
    for k in KEYS[field]:
        if norm(k) in low:
            return low[norm(k)]
    return None


def row_to_record(row, keymap=None):
    cols = row.get("columns", row) if isinstance(row, dict) else {}
    keymap = keymap or {f: _find_key(cols, f) for f in KEYS}

    def g(field):
        k = keymap.get(field)
        return cols.get(k) if k else None

    cid = as_str(g("call_id"))
    when = parse_when(as_str(g("call_iso")), g("call_at"))
    if not cid or not when:
        return None
    return {
        "call_id": cid, "call_at": when,
        "publisher_rid": as_str(g("publisher_id")), "publisher_name": as_str(g("publisher_name")),
        "buyer_rid": as_str(g("buyer_id")), "buyer_name": as_str(g("buyer_name")),
        "target_name": as_str(g("target_name")), "campaign_name": as_str(g("campaign_name")),
        "campaign_rid": as_str(g("campaign_id")),
        "caller": as_str(g("caller")), "did": normalize_phone(g("did")), "did_rid": as_str(g("did_id")),
        "connected": as_bool(g("connected")), "converted": as_bool(g("converted")), "paid": as_bool(g("paid")),
        "is_live": as_bool(g("is_live")), "is_duplicate": as_bool(g("duplicate")),
        "blocked": as_bool(g("blocked")), "incomplete": as_bool(g("incomplete")),
        "duration_sec": as_secs(g("duration")), "connected_sec": as_secs(g("connected_len")),
        "revenue": as_num(g("revenue")), "payout": as_num(g("payout")),
        "profit": as_num(g("profit")), "telco_cost": as_num(g("telco_cost")),
    }


def extract_rows(data):
    """Rows are in response['callLog']['data'] OR response['report']['records']."""
    if isinstance(data, dict):
        cl = data.get("callLog")
        if isinstance(cl, dict) and isinstance(cl.get("data"), list):
            return cl["data"]
        rp = data.get("report")
        if isinstance(rp, dict) and isinstance(rp.get("records"), list):
            return rp["records"]
    return []


def _check_first_row(rows):
    """Which required things can we find in the first row? Returns (missing, mapped)."""
    if not rows:
        return [], {}
    cols = rows[0].get("columns", rows[0]) if isinstance(rows[0], dict) else {}
    keymap = {f: _find_key(cols, f) for f in KEYS}
    missing = []
    if not keymap["call_id"]:
        missing.append("inboundCallId")
    if not (keymap["call_at"] or keymap["call_iso"]):
        missing.append("callDt")
    if not (keymap["publisher_id"] or keymap["publisher_name"]):
        missing.append("publisherId / publisherName")
    if not keymap["did"]:
        missing.append("DID (tag:DialedNumber:Number / number)")
    return missing, {f: k for f, k in keymap.items() if k}


# ---------- manual CSV upload (fallback, no API call) ----------
def parse_csv(text):
    """Parse a call-log CSV you downloaded from Ringba yourself. Returns (records, report)."""
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    rows = list(reader)
    missing, mapped = _check_first_row(rows)
    report = {"headers": len(reader.fieldnames or []), "mapped": mapped, "missing": missing,
              "rows": len(rows), "skipped": 0}
    if missing:
        return [], report
    keymap = {f: _find_key(rows[0], f) for f in KEYS}
    by_id = {}
    for r in rows:
        rec = row_to_record(r, keymap)
        if rec is None:
            report["skipped"] += 1
        else:
            by_id[rec["call_id"]] = rec
    return list(by_id.values()), report


# ---------- talking to Ringba ----------
def _z(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _post(url, headers, body, retries=6):
    """POST with patience: 429 (rate limit) and 5xx are retried after a pause (never longer than the time budget)."""
    last = None
    for attempt in range(1, retries + 1):
        _check_time()
        try:
            r = requests.post(url, headers=headers, json=body, timeout=_request_timeout(120))
        except requests.RequestException as e:
            last = f"network error: {e}"
            _nap(2 * attempt)
            continue
        if r.status_code == 200:
            return r
        last = f"HTTP {r.status_code}: {r.text[:300]}"
        if r.status_code in (401, 403):
            raise RingbaError(f"Ringba refused the login ({last}). Check RINGBA_API_TOKEN and RINGBA_ACCOUNT_ID.")
        if r.status_code == 404:
            raise RingbaError(f"Ringba says not found ({last}). Check RINGBA_ACCOUNT_ID.")
        if r.status_code == 400:
            raise RingbaError(f"BAD_REQUEST {last}")
        if r.status_code == 429:
            wait = r.headers.get("Retry-After")
            _nap(min(float(wait), 90) if wait and wait.replace(".", "").isdigit() else min(10 * attempt, 90))
        else:
            _nap(3 * attempt)
    raise RingbaError(f"Ringba request failed after {retries} tries: {last}")


def _row_call_id(row, key):
    """The call id; if a row has none, a fingerprint of the whole row, so repeated rows are still recognised."""
    cols = row.get("columns", row) if isinstance(row, dict) else {}
    cid = as_str(cols.get(key)) if key else None
    if cid is None:
        cid = "row:" + hashlib.md5(json.dumps(row, sort_keys=True, default=str).encode()).hexdigest()
    return cid


def _fetch_pages(url, headers, start, end, columns):
    """Download EVERY call in [start, end] with offset/size paging. Returns (rows, number_of_requests)."""
    all_rows, seen_ids = [], set()
    offset, requests_made, call_id_key = 0, 0, None
    while requests_made < MAX_REQUESTS:
        _check_time()
        body = {
            "reportStart": _z(start), "reportEnd": _z(end),
            "valueColumns": [{"column": c} for c in columns],
            "orderByColumns": [{"column": "callDt", "direction": "asc"}],
            "formatTimeZone": "America/New_York",
            "offset": offset,
            "size": PAGE_SIZE,
        }
        r = _post(url, headers, body)
        requests_made += 1
        try:
            data = r.json()
        except ValueError:
            raise RingbaError(f"Ringba did not return JSON: {r.text[:300]}")
        raw_calls = extract_rows(data)
        if not raw_calls:
            break                                   # nothing left
        if call_id_key is None:
            first = raw_calls[0].get("columns", raw_calls[0]) if isinstance(raw_calls[0], dict) else {}
            call_id_key = _find_key(first, "call_id")
        new_this_page = 0
        for row in raw_calls:
            cid = _row_call_id(row, call_id_key)
            if cid not in seen_ids:
                seen_ids.add(cid)
                new_this_page += 1
                all_rows.append(row)
        if new_this_page == 0:
            break                                   # safety: Ringba ignored the offset and repeated itself
        # Move forward by the rows we really received. Same as "+ size" when Ringba returns `size` rows,
        # but never skips calls if it returns fewer (for example its default of 20).
        offset += len(raw_calls)
        _nap(PAGE_PAUSE)                            # be gentle: avoids HTTP 429
    return all_rows, requests_made


def fetch_range(start, end):
    """Download + parse one time window. Returns (records, report)."""
    if not config.RINGBA_TOKEN or not config.RINGBA_ACCOUNT_ID:
        raise RingbaError("RINGBA_API_TOKEN or RINGBA_ACCOUNT_ID is missing in your Secrets.")
    url = f"{BASE}/{config.RINGBA_ACCOUNT_ID}/calllogs"
    headers = {"Authorization": f"Token {config.RINGBA_TOKEN}", "Content-Type": "application/json"}
    raw_rows, n_requests = None, 0
    for i in range(_working_set["i"], len(COLUMN_SETS)):
        try:
            raw_rows, n_requests = _fetch_pages(url, headers, start, end, COLUMN_SETS[i])
            _working_set["i"] = i
            break
        except RingbaError as e:
            if str(e).startswith("BAD_REQUEST") and i < len(COLUMN_SETS) - 1:
                continue          # Ringba did not like one of the extra columns: try a smaller set
            raise RingbaError(str(e).replace("BAD_REQUEST ", "Ringba rejected the request: "))
    missing, mapped = _check_first_row(raw_rows)
    report = {"headers": len(mapped), "mapped": mapped, "missing": missing, "rows": len(raw_rows),
              "skipped": 0, "requests": n_requests, "column_set": ["full", "core", "minimal"][_working_set["i"]],
              "first_row_keys": sorted((raw_rows[0].get("columns", raw_rows[0]) if raw_rows else {}).keys())}
    if missing or not raw_rows:
        return [], report
    keymap = {f: _find_key(raw_rows[0].get("columns", raw_rows[0]), f) for f in KEYS}
    by_id = {}
    for r in raw_rows:
        rec = row_to_record(r, keymap)
        if rec is None:
            report["skipped"] += 1
        else:
            by_id[rec["call_id"]] = rec
    return list(by_id.values()), report


# ---------- numbers (DIDs): GET /v2/{accountId}/numbers ----------
NUM_KEYS = {
    "number": ["number", "phoneNumber", "numberE164", "e164", "trackingNumber", "did", "dialedNumber", "phone", "tollFreeNumber"],
    "id": ["numberId", "id", "phoneNumberId"],
    "label": ["name", "label", "numberName", "friendlyName", "description"],
    "created": ["allocationDT", "createdAt", "createdDate", "createDate", "dateCreated", "created", "createdOn", "creationDate",
                "createdDt", "createdTime", "dateAdded", "addedAt", "purchaseDate", "createdUtc"],
}


def _pick(d, cands):
    low = {norm(k): k for k in d}
    for c in cands:
        if c in d:
            return d[c]
    for c in cands:
        if norm(c) in low:
            return d[low[norm(c)]]
    return None


def _when_any(v):
    """Created date: epoch milliseconds / seconds, ISO text, or a normal date string."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)) or (isinstance(v, str) and re.fullmatch(r"\d{9,14}(\.\d+)?", v.strip())):
        n = float(v)
        return datetime.fromtimestamp(n / 1000.0 if n > 1e11 else n, tz=timezone.utc)
    if str(v).startswith(("0001-", "0000-")):
        return None                                   # '0001-01-01...' means "not set"
    try:
        d = parse_when(None, str(v))
    except (ValueError, OverflowError):
        return None
    return d if d and d.year > 1900 else None


def extract_number_items(data):
    """The answer is a JSON array. Some APIs wrap it ({'numbers': [...]}), so that is accepted too."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("numbers", "data", "items", "result", "results", "records", "phoneNumbers"):
            v = data.get(k)
            if isinstance(v, list):
                return v
            if isinstance(v, dict):
                inner = extract_number_items(v)
                if inner:
                    return inner
    return []


def _get(url, headers, retries=6):
    last = None
    for attempt in range(1, retries + 1):
        _check_time()
        try:
            r = requests.get(url, headers=headers, timeout=_request_timeout(60))
        except requests.RequestException as e:
            last = f"network error: {e}"
            _nap(2 * attempt)
            continue
        if r.status_code == 200:
            return r
        last = f"HTTP {r.status_code}: {r.text[:300]}"
        if r.status_code in (401, 403):
            raise RingbaError(f"Ringba refused the login ({last}). Check RINGBA_API_TOKEN and RINGBA_ACCOUNT_ID.")
        if r.status_code == 404:
            raise RingbaError(f"Ringba says not found ({last}). Check RINGBA_ACCOUNT_ID.")
        if r.status_code == 429:
            wait = r.headers.get("Retry-After")
            _nap(min(float(wait), 90) if wait and wait.replace(".", "").isdigit() else min(10 * attempt, 90))
        else:
            _nap(3 * attempt)
    raise RingbaError(f"Ringba request failed after {retries} tries: {last}")


def _released(it):
    """A number Ringba has already released (deallocated) is no longer ours."""
    if it.get("deallocFlag") is True:
        return True
    return _when_any(it.get("deAllocationDT")) is not None


def parse_numbers(data):
    """JSON answer -> ([{'number','label','created_at','rid','affiliate_id','affiliate_name','next_charge'}], report).
    Shape (from Ringba): {"transactionId": ..., "numbers": [{"phoneNumber", "allocationDT", "nextChargeDT",
    "deAllocationDT", "id", "name", "affiliate": {"id", "name", "isSelf"}, ...}]}"""
    items = extract_number_items(data)
    out, invalid, released, keys = [], 0, 0, set()
    mapped = {}
    for it in items:
        aff_id = aff_name = next_charge = campaign_id = None
        if isinstance(it, str):
            num, label, rid, created = normalize_phone(it), None, None, None
        elif isinstance(it, dict):
            keys.update(it.keys())
            if _released(it):
                released += 1
                continue
            num = normalize_phone(_pick(it, NUM_KEYS["number"]))
            label = as_str(_pick(it, NUM_KEYS["label"]))
            rid = as_str(_pick(it, NUM_KEYS["id"]))
            created = _when_any(_pick(it, NUM_KEYS["created"]))
            next_charge = _when_any(it.get("nextChargeDT"))
            campaign_id = as_str(it.get("campaignId"))
            aff = it.get("affiliate")
            if isinstance(aff, dict) and not aff.get("isSelf"):      # "You" = your own account, not a publisher
                aff_id, aff_name = as_str(aff.get("id")), as_str(aff.get("name"))
            for f, cands in NUM_KEYS.items():
                for cnd in cands:
                    if cnd in it or norm(cnd) in {norm(k) for k in it}:
                        mapped.setdefault(f, cnd)
                        break
        else:
            invalid += 1
            continue
        if not num:
            invalid += 1
            continue
        out.append({"number": num, "label": label, "created_at": created, "rid": rid,
                    "affiliate_id": aff_id, "affiliate_name": aff_name, "next_charge": next_charge,
                    "campaign_id": campaign_id})
    report = {"count": len(items), "parsed": len(out), "invalid": invalid, "released": released, "mapped": mapped,
              "first_item_keys": sorted(keys)[:40], "with_created_date": sum(1 for o in out if o["created_at"]),
              "with_publisher": sum(1 for o in out if o["affiliate_name"])}
    if items and not out and not released:
        raise RingbaError("Ringba returned numbers but I could not find the phone number field. Fields it returned: "
                          + ", ".join(sorted(keys)[:40]))
    return out, report


def fetch_numbers():
    """Every tracking number currently on your account (including newly created ones)."""
    if not config.RINGBA_TOKEN or not config.RINGBA_ACCOUNT_ID:
        raise RingbaError("RINGBA_API_TOKEN or RINGBA_ACCOUNT_ID is missing in your Secrets.")
    url = f"{BASE}/{config.RINGBA_ACCOUNT_ID}/numbers"
    headers = {"Authorization": f"Token {config.RINGBA_TOKEN}", "Content-Type": "application/json"}
    r = _get(url, headers)
    try:
        data = r.json()
    except ValueError:
        raise RingbaError(f"Ringba did not return JSON for the numbers list: {r.text[:300]}")
    return parse_numbers(data)


# ---------- buyer targets and their caps ----------
#   GET /v2/{accountId}/targets                       -> the targets (each has a 'schedule' with hourlyCap / dailyCap / monthlyCap ...)
#   GET /v2/{accountId}/targets/{targetId}            -> one target in full (used when the list has no 'schedule')
#   GET /v2/{accountId}/targets/{targetId}/Counts     -> live counters: {"stats": {"<targetId>": {"currentDay": 8, "currentMonth": 120, ...}}}
# A cap of -1 means "no cap set".
def _auth_headers():
    if not config.RINGBA_TOKEN or not config.RINGBA_ACCOUNT_ID:
        raise RingbaError("RINGBA_API_TOKEN or RINGBA_ACCOUNT_ID is missing in your Secrets.")
    return {"Authorization": f"Token {config.RINGBA_TOKEN}", "Content-Type": "application/json"}


def _json(r, what):
    try:
        return r.json()
    except ValueError:
        raise RingbaError(f"Ringba did not return JSON for {what}: {r.text[:300]}")


def cap_value(v):
    """Ringba cap -> a whole number of calls, or None when no cap is set (-1, 0, empty, not a number)."""
    if v is None or isinstance(v, bool):
        return None
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return int(n) if n > 0 else None


def extract_target_items(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("targets", "data", "items", "result", "results", "records"):
            v = data.get(k)
            if isinstance(v, list):
                return v
            if isinstance(v, dict):
                inner = extract_target_items(v)
                if inner:
                    return inner
    return []


def parse_target(it):
    """One target from Ringba -> {'rid','name','buyer_rid','buyer_name','enabled','daily_cap','monthly_cap'} (or None)."""
    if not isinstance(it, dict):
        return None
    rid = as_str(_pick(it, ["id", "targetId"]))
    if not rid:
        return None
    sched = it.get("schedule") if isinstance(it.get("schedule"), dict) else {}
    buyer = it.get("buyer") if isinstance(it.get("buyer"), dict) else {}
    enabled = _pick(it, ["enabled", "isEnabled", "active", "isActive"])
    if enabled is None and isinstance(it.get("state"), str):
        enabled = it["state"].strip().lower() in ("enabled", "active", "on")
    return {"rid": rid, "name": as_str(_pick(it, ["name", "targetName"])) or rid,
            "buyer_rid": as_str(buyer.get("id") or _pick(it, ["buyerId"])),
            "buyer_name": as_str(buyer.get("name") or _pick(it, ["buyerName"]) or (it.get("buyer") if isinstance(it.get("buyer"), str) else None)),
            "enabled": True if enabled is None else bool(enabled),
            "daily_cap": cap_value(_pick(sched, ["dailyCap"]) if sched else None),
            "monthly_cap": cap_value(_pick(sched, ["monthlyCap"]) if sched else None),
            "has_schedule": bool(sched)}


def fetch_targets(max_details=300):
    """Every buyer target on the account with its daily / monthly cap. Returns (list, report)."""
    headers = _auth_headers()
    acct = config.RINGBA_ACCOUNT_ID
    data = _json(_get(f"{BASE}/{acct}/targets", headers), "the targets list")
    items = extract_target_items(data)
    out, details, bad = [], 0, 0
    for it in items:
        t = parse_target(it)
        if t is None:
            bad += 1
            continue
        if not t["has_schedule"] and details < max_details:         # the list had no cap info: ask for this target in full
            details += 1
            _nap(0.2)
            d = _json(_get(f"{BASE}/{acct}/targets/{t['rid']}", headers), f"target {t['rid']}")
            if isinstance(d, dict) and isinstance(d.get("target"), dict):
                d = d["target"]
            full = parse_target({**it, **d}) if isinstance(d, dict) else None
            t = full or t
        out.append(t)
    keys = sorted({k for it in items if isinstance(it, dict) for k in it})[:40]
    report = {"count": len(items), "parsed": len(out), "invalid": bad, "detail_calls": details,
              "with_daily_cap": sum(1 for t in out if t["daily_cap"]), "with_monthly_cap": sum(1 for t in out if t["monthly_cap"]),
              "first_item_keys": keys}
    if items and not out:
        raise RingbaError("Ringba returned targets but I could not find their id field. Fields it returned: " + ", ".join(keys))
    return out, report


def parse_counts(data, target_id):
    """Live counters of one target. The id in the answer may be written with other capitalisation."""
    stats = data.get("stats") if isinstance(data, dict) else None
    if not isinstance(stats, dict) or not stats:
        return None
    row = next((v for k, v in stats.items() if str(k).lower() == str(target_id).lower()), None)
    if row is None and len(stats) == 1:
        row = next(iter(stats.values()))
    if not isinstance(row, dict):
        return None
    def n(k):
        try:
            return int(float(row.get(k) or 0))
        except (TypeError, ValueError):
            return 0
    return {"day": n("currentDay"), "month": n("currentMonth"), "hour": n("currentHour"), "live": n("currentLive"), "total": n("total")}


def fetch_target_counts(target_id):
    data = _json(_get(f"{BASE}/{config.RINGBA_ACCOUNT_ID}/targets/{target_id}/Counts", _auth_headers(), retries=3), f"counts of {target_id}")
    return parse_counts(data, target_id)


def test_connection(hours=3):
    """Used by the Settings button. Returns a plain dict you can show on screen."""
    from datetime import timedelta
    end = datetime.now(timezone.utc)
    recs, rep = fetch_range(end - timedelta(hours=hours), end)
    return {"records": len(recs), "report": rep, "sample": recs[:3]}
