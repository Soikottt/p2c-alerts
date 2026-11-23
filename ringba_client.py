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


def build_keymap(rows, sample=500):
    """Which response key holds each field. Ringba leaves a key OUT of a row when the value is empty (a call that was
    not connected has no 'conversionAmount' ...), so looking only at the first row loses whole columns. Look at many rows."""
    keymap = {f: None for f in KEYS}
    for row in rows[:sample]:
        cols = row.get("columns", row) if isinstance(row, dict) else {}
        for f in KEYS:
            if keymap[f] is None:
                keymap[f] = _find_key(cols, f)
        if all(v is not None for v in keymap.values()):
            break
    return keymap


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
    keymap = build_keymap(raw_rows)
    by_id = {}
    for r in raw_rows:
        rec = row_to_record(r, keymap)
        if rec is None:
            report["skipped"] += 1
        else:
            by_id[rec["call_id"]] = rec
    return list(by_id.values()), report


# ---------- insights report: POST /v2/{accountId}/insights (one day, publisher + campaign + buyer) ----------
INSIGHT_GROUPS = [("publisherName", "Publisher"), ("campaignName", "Campaign"), ("buyer", "Buyer")]
INSIGHT_VALUES = ["callCount", "completedCalls", "connectedCallCount", "payoutCount", "convertedCalls", "nonConnectedCallCount",
                  "duplicateCalls", "blockedCalls", "incompleteCalls", "conversionAmount", "payoutAmount", "profitGross",
                  "totalCost", "callLengthInSeconds"]


def insights_body(start, end, tzname="America/New_York"):
    return {"reportStart": _z(start), "reportEnd": _z(end),
            "groupByColumns": [{"column": c, "displayName": n} for c, n in INSIGHT_GROUPS],
            "valueColumns": [{"column": c, "aggregateFunction": None} for c in INSIGHT_VALUES],
            "orderByColumns": [{"column": "callCount", "direction": "desc"}],
            "formatTimespans": False, "formatPercentages": False, "generateRollups": False,
            "maxResultsPerGroup": 1000, "filters": [], "formatTimeZone": tzname}


def _post_insights(url, headers, body):
    """Retry 429 / 5xx after 2 then 4 seconds (as Ringba advises)."""
    last = None
    for wait in (0, 2, 4):
        if wait:
            _nap(wait)
        _check_time()
        try:
            r = requests.post(url, headers=headers, json=body, timeout=_request_timeout(90))
        except requests.RequestException as e:
            last = f"network error: {e}"
            continue
        if r.status_code == 200:
            return r
        last = f"HTTP {r.status_code}: {r.text[:300]}"
        if r.status_code in (401, 403):
            raise RingbaError(f"Ringba refused the login ({last}). Check RINGBA_API_TOKEN and RINGBA_ACCOUNT_ID.")
        if r.status_code not in (429, 500, 502, 503, 504):
            raise RingbaError(f"Ringba insights failed ({last})")
    raise RingbaError(f"Ringba insights failed after 3 tries: {last}")


def _n(v):
    try:
        return float(v) if v not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


def parse_insights(data):
    """report.records[] -> list of dicts (publisher, campaign, buyer + the numbers)."""
    rp = data.get("report") if isinstance(data, dict) else None
    recs = rp.get("records") if isinstance(rp, dict) else None
    out = []
    for r in recs or []:
        if not isinstance(r, dict):
            continue
        out.append({"publisher": as_str(r.get("publisherName")) or "(none)", "campaign": as_str(r.get("campaignName")) or "(none)",
                    "buyer": as_str(r.get("buyer")) or "(none)",
                    "calls": int(_n(r.get("callCount"))), "completed": int(_n(r.get("completedCalls"))),
                    "connected": int(_n(r.get("connectedCallCount"))), "paid": int(_n(r.get("payoutCount"))),
                    "converted": int(_n(r.get("convertedCalls"))), "not_connected": int(_n(r.get("nonConnectedCallCount"))),
                    "duplicates": int(_n(r.get("duplicateCalls"))), "blocked": int(_n(r.get("blockedCalls"))),
                    "incomplete": int(_n(r.get("incompleteCalls"))),
                    "revenue": round(_n(r.get("conversionAmount")), 2), "payout": round(_n(r.get("payoutAmount")), 2),
                    "profit": round(_n(r.get("profitGross")), 2), "cost": round(_n(r.get("totalCost")), 2),
                    "seconds": int(_n(r.get("callLengthInSeconds")))})
    return out


def fetch_insights(start, end, tzname="America/New_York"):
    """One time window (use one DAY at a time). Returns (rows, raw_record_count)."""
    headers = _auth_headers()
    headers["Accept"] = "application/json"
    r = _post_insights(f"{BASE}/{config.RINGBA_ACCOUNT_ID}/insights", headers, insights_body(start, end, tzname))
    rows = parse_insights(_json(r, "the insights report"))
    return rows


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


_CAP_SKIP = ("sum", "capacity", "concurr", "amount", "revenue", "payout", "hour", "enabled", "isenabled", "capped", "type")


_LIMITISH = re.compile(r"(per_?day|per_?month|daily|monthly)_?(call)?s?_?(cap|limit|max)|(cap|limit|max)_?(calls_?)?(per_?day|per_?month|daily|monthly)|^(calls|max_?calls)_?per_?(day|month)$")


def _cap_num(v):
    """A cap can be a number, a numeric string, or a small object like {"value": 100, "enabled": true}."""
    if isinstance(v, dict):
        if v.get("enabled") is False or v.get("isEnabled") is False:
            return None
        for k in ("value", "cap", "limit", "max", "count", "amount"):
            if k in v:
                return cap_value(v[k])
        return None
    return cap_value(v)


_PERIOD_KEYS = ("period", "type", "captype", "capperiod", "interval", "timeframe", "frequency", "unit", "timeunit", "resetperiod", "duration", "cap_type")
_VALUE_KEYS = ("cap", "value", "limit", "capvalue", "calls", "max", "maximum", "count", "amount", "callcap", "capamount", "total")


def _period_of(obj):
    """'daily' / 'monthly' when this small object says which period it is for (Day, PerDay, Daily, Calls Per Day, Month ...)."""
    low = {re.sub(r"[^a-z]", "", str(k).lower()): k for k in obj}
    for pk in _PERIOD_KEYS:
        k = low.get(re.sub(r"[^a-z]", "", pk))
        if k is None or not isinstance(obj[k], str):
            continue
        v = re.sub(r"[^a-z]", "", obj[k].lower())
        if "month" in v:
            return "monthly"
        if v in ("day", "daily", "perday", "calendarday") or v.endswith("perday") or v.endswith("day") and "hour" not in v:
            return "daily"
    return None


def find_caps(obj, _depth=0, _path=""):
    """Look through a target (any depth) for its daily / monthly call caps. Ringba keeps them under different names
    (dailyCap, schedule.dailyCap, globalCaps.daily ...). Returns ({'daily': n, 'monthly': n}, [(path, value) of every cap-like field])."""
    found, seen = {}, []
    if _depth > 5:
        return found, seen
    if isinstance(obj, dict):
        per = _period_of(obj)
        if per:
            lowk = {re.sub(r"[^a-z]", "", str(k).lower()): k for k in obj}
            raw = next((obj[lowk[vk]] for vk in _VALUE_KEYS if vk in lowk and not isinstance(obj[lowk[vk]], (dict, list))), None)
            if raw is not None:
                off = any(obj.get(k) is False for k in ("enabled", "isEnabled", "active", "isActive"))
                n = None if off else cap_value(raw)
                seen.append((_path or "(object)", str(obj)[:80]))
                if n:
                    found.setdefault(per, n)
                return found, seen
        for k, v in obj.items():
            kl = str(k).lower()
            if any(x in kl for x in ("capacity", "concurr", "sumcap", "sumdaily", "summonthly")):
                continue
            path = f"{_path}.{k}" if _path else str(k)
            if isinstance(v, (dict, list)) and not (isinstance(v, dict) and any(x in v for x in ("value", "cap", "limit"))):
                f2, s2 = find_caps(v, _depth + 1, path)
                for per, n in f2.items():
                    found.setdefault(per, n)
                seen.extend(s2)
                if not ("cap" in kl and not any(x in kl for x in _CAP_SKIP[:3])):
                    continue
            if isinstance(v, list) and "dayofweek" in kl.replace("_", "") and "cap" in kl and "sum" not in kl and len(v) == 7:
                # Ringba's "Daily Cap" with the ADVANCED button: one number per weekday (Sunday first). Today's number is the cap.
                from datetime import datetime
                from zoneinfo import ZoneInfo
                idx = (datetime.now(ZoneInfo("America/New_York")).weekday() + 1) % 7
                n = cap_value(v[idx])
                seen.append((path, v))
                if n:
                    found.setdefault("daily", n)
            elif _LIMITISH.search(kl) and not any(x in kl for x in _CAP_SKIP[:6]) and not isinstance(v, (dict, list)):
                n = cap_value(v)
                seen.append((path, v))
                if n:
                    found.setdefault("monthly" if "month" in kl else "daily", n)
            elif "cap" in kl and not any(x in kl for x in _CAP_SKIP):
                n = _cap_num(v)
                seen.append((path, v if not isinstance(v, (dict, list)) else str(v)[:60]))
                if n:
                    if "month" in kl:
                        found.setdefault("monthly", n)
                    elif "day" in kl or "daily" in kl:
                        found.setdefault("daily", n)
                    elif kl in ("dailycap",):
                        found.setdefault("daily", n)
                    elif _path.lower().endswith(("daily", "day")):
                        found.setdefault("daily", n)
                    elif _path.lower().endswith("monthly"):
                        found.setdefault("monthly", n)
            elif kl in ("daily", "day", "perday") and not isinstance(v, (dict, list)) and "cap" in _path.lower():
                n = cap_value(v)
                seen.append((path, v))
                if n:
                    found.setdefault("daily", n)
            elif kl in ("monthly", "month", "permonth") and not isinstance(v, (dict, list)) and "cap" in _path.lower():
                n = cap_value(v)
                seen.append((path, v))
                if n:
                    found.setdefault("monthly", n)
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:20]):
            f2, s2 = find_caps(v, _depth + 1, f"{_path}[{i}]")
            for per, n in f2.items():
                found.setdefault(per, n)
            seen.extend(s2)
    return found, seen


def _all_time_cap(it):
    """Ringba's "Global Call Cap" (the cap on the whole target for ever). It is stored as schedule.allTimeCap; -1 / 0 = no cap."""
    for src in (it.get("schedule") if isinstance(it.get("schedule"), dict) else {}, it):
        for k, v in src.items():
            if re.sub(r"[^a-z]", "", str(k).lower()) in ("alltimecap", "globalcap", "globalcallcap", "lifetimecap") and not isinstance(v, (dict, list)):
                n = cap_value(v)
                if n:
                    return n
    return None


def parse_target(it):
    """One target from Ringba -> {'rid','name','buyer_rid','buyer_name','enabled','daily_cap','monthly_cap','all_time_cap' (the Global Call Cap)} (or None)."""
    if not isinstance(it, dict):
        return None
    rid = as_str(_pick(it, ["id", "targetId"]))
    if not rid:
        return None
    buyer = it.get("buyer") if isinstance(it.get("buyer"), dict) else {}
    enabled = _pick(it, ["enabled", "isEnabled", "active", "isActive"])
    if enabled is None and isinstance(it.get("state"), str):
        enabled = it["state"].strip().lower() in ("enabled", "active", "on")
    caps, seen = find_caps({k: v for k, v in it.items() if k not in ("buyer",)})
    return {"rid": rid, "name": as_str(_pick(it, ["name", "targetName"])) or rid,
            "buyer_rid": as_str(buyer.get("id") or _pick(it, ["buyerId"])),
            "buyer_name": as_str(buyer.get("name") or _pick(it, ["buyerName"]) or (it.get("buyer") if isinstance(it.get("buyer"), str) else None)),
            "enabled": True if enabled is None else bool(enabled),
            "daily_cap": caps.get("daily"), "monthly_cap": caps.get("monthly"), "all_time_cap": _all_time_cap(it),
            "has_schedule": bool(caps), "cap_fields": seen}


def target_cap_debug(target_id):
    """For the 'check one target' button: every cap-like field Ringba returns for this target, from the list and from the full record."""
    headers = _auth_headers()
    acct = config.RINGBA_ACCOUNT_ID
    out = {"target_id": target_id}
    d = _json(_get(f"{BASE}/{acct}/targets/{target_id}", headers), f"target {target_id}")
    if isinstance(d, dict) and isinstance(d.get("target"), dict):
        d = d["target"]
    t = parse_target(d) if isinstance(d, dict) else None
    out["name"] = t["name"] if t else None
    out["read_as"] = {"daily_cap": t["daily_cap"], "monthly_cap": t["monthly_cap"], "global_cap": t["all_time_cap"], "enabled": t["enabled"]} if t else None
    out["cap_fields"] = [{"field": p, "value": v} for p, v in (t["cap_fields"] if t else [])]
    out["top_level_keys"] = sorted(d)[:60] if isinstance(d, dict) else []
    out["enabled_flags_in_record"] = {k: d[k] for k in d if k in ("enabled", "isEnabled", "active", "isActive", "state", "status")} if isinstance(d, dict) else {}
    out["every_field_that_looks_like_a_cap"] = _capish_fields(d)
    return out


def _capish_fields(obj, path="", depth=0, acc=None):
    """Every field (any depth) whose name mentions cap / limit / day / month / global / schedule, with its value. For the 'check one target' button."""
    acc = [] if acc is None else acc
    if depth > 7 or len(acc) > 120:
        return acc
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else str(k)
            if re.search(r"cap|limit|day|month|global|schedule|quota|max", str(k), re.I):
                acc.append({"field": p, "value": (v if not isinstance(v, (dict, list)) else json.dumps(v)[:300])})
            if isinstance(v, (dict, list)):
                _capish_fields(v, p, depth + 1, acc)
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:25]):
            _capish_fields(v, f"{path}[{i}]", depth + 1, acc)
    return acc


def _target_detail(headers, acct, rid, it):
    """One target's full record -> parsed target, or None when it failed (the caller keeps the list version)."""
    d = _json(_get(f"{BASE}/{acct}/targets/{rid}", headers), f"target {rid}")
    if isinstance(d, dict) and isinstance(d.get("target"), dict):
        d = d["target"]
    return parse_target({**it, **d}) if isinstance(d, dict) else None


def fetch_targets(max_details=2000, skip_details=None, workers=6):
    """Every buyer target on the account with its daily / monthly cap. Returns (list, report).
    Targets whose list entry shows no cap are asked for in full, several at the same time. `skip_details` = ids already known to
    have no cap (not asked again). When the time budget ends, the targets not yet checked are listed in report['unchecked'] and
    report['partial'] is True: the caller must keep their old caps. Nothing is lost, the next run continues."""
    from concurrent.futures import ThreadPoolExecutor
    headers = _auth_headers()
    acct = config.RINGBA_ACCOUNT_ID
    data = _json(_get(f"{BASE}/{acct}/targets", headers), "the targets list")
    items = extract_target_items(data)
    skip_details = set(skip_details or ())
    parsed, bad = [], 0
    for it in items:
        t = parse_target(it)
        if t is None:
            bad += 1
            continue
        parsed.append((it, t))
    todo = [(i, it, t) for i, (it, t) in enumerate(parsed) if not (t["daily_cap"] or t["monthly_cap"] or t["all_time_cap"]) and t["rid"] not in skip_details]
    todo = todo[:max_details]
    out = [t for _, t in parsed]
    unchecked, no_cap_checked, details = [], [], 0

    def work(job):
        i, it, t = job
        try:
            _check_time()
            return job, _target_detail(headers, acct, t["rid"], it), None
        except RingbaError as e:
            return job, None, e
        except Exception as e:                      # one odd record must not stop the sync
            return job, None, e

    if todo:
        with ThreadPoolExecutor(max_workers=max(1, int(workers))) as ex:
            for (i, it, t), full, err in ex.map(work, todo):
                if err is not None:
                    if isinstance(err, RingbaError) and "ran out of time" in str(err):
                        unchecked.append(t["rid"])
                    continue                        # failed once: keep the list version, try again next sync
                details += 1
                if full:
                    out[i] = full
                    t = full
                if not (t["daily_cap"] or t["monthly_cap"] or t["all_time_cap"]):
                    no_cap_checked.append(t["rid"])
    keys = sorted({k for it in items if isinstance(it, dict) for k in it})[:40]
    report = {"count": len(items), "parsed": len(out), "invalid": bad, "detail_calls": details,
              "with_daily_cap": sum(1 for t in out if t["daily_cap"]), "with_monthly_cap": sum(1 for t in out if t["monthly_cap"]),
              "with_global_cap": sum(1 for t in out if t["all_time_cap"]),
              "first_item_keys": keys, "partial": bool(unchecked), "unchecked": unchecked, "no_cap_checked": no_cap_checked}
    if items and not out:
        raise RingbaError("Ringba returned targets but I could not find their id field. Fields it returned: " + ", ".join(keys))
    return out, report


# ------------------------------------------------------------ publisher caps (payout rules of each campaign)
def _items(data, keys):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in keys:
            v = data.get(k)
            if isinstance(v, list):
                return v
            if isinstance(v, dict):
                inner = _items(v, keys)
                if inner:
                    return inner
    return []


def payout_rules(v):
    """All payout rules inside a Ringba value (a list, or one rule, or a wrapper). A rule = a dict with '...Cap' fields."""
    out = []
    if isinstance(v, dict):
        if any(norm(k).endswith("cap") for k in v):
            return [v]
        for x in v.values():
            if isinstance(x, (list, dict)):
                out += payout_rules(x)
    elif isinstance(v, list):
        for x in v:
            out += payout_rules(x)
    return out


def _first_cap(rules, field):
    """The first rule that has a cap in this field -> (cap, number of rules with a cap there)."""
    caps = [cap_value(_pick(r, [field])) for r in rules]
    caps = [c for c in caps if c]
    return (caps[0] if caps else None), len(caps)


def parse_campaign_caps(camp):
    """One campaign (as Ringba returns it) -> list of {'campaign_id','campaign_name','pub_rid','pub_name','daily_cap','monthly_cap',
    'inherited','rule_count'}: one entry per publisher (affiliate) that has a cap in this campaign. Caps are counts of payouts."""
    if not isinstance(camp, dict):
        return []
    cid = as_str(_pick(camp, ["id", "campaignId"]))
    cname = as_str(_pick(camp, ["name", "campaignName"])) or cid
    defaults = [r for r in payout_rules(_pick(camp, ["defaultPayouts", "defaultPayout"])) if not as_bool(_pick(r, ["isGlobal"]))]
    out = []
    for a in (_pick(camp, ["affiliates", "publishers"]) or []):
        if not isinstance(a, dict):
            continue
        own = payout_rules(_pick(a, ["payoutConversionSettings", "payoutSettings"]))
        rules, inherited = (own, False) if own else (defaults, True)      # no own rules: the publisher uses the campaign's default payouts
        daily, nd = _first_cap(rules, "dailyCap")
        monthly, nm = _first_cap(rules, "monthlyCap")
        if not (daily or monthly):
            continue
        out.append({"campaign_id": cid, "campaign_name": cname,
                    "pub_rid": as_str(_pick(a, ["affiliateId", "publisherId", "id"])),
                    "pub_name": as_str(_pick(a, ["affiliateName", "publisherName", "name"])),
                    "daily_cap": daily, "monthly_cap": monthly, "inherited": inherited, "rule_count": max(nd, nm)})
    return out


def fetch_publisher_caps(max_details=100):
    """Publisher caps of every campaign, read from the campaigns' payout rules. Returns (list, report)."""
    headers = _auth_headers()
    acct = config.RINGBA_ACCOUNT_ID
    data = _json(_get(f"{BASE}/{acct}/campaigns", headers), "the campaigns list")
    camps = _items(data, ("campaigns", "data", "items", "result", "results", "records"))
    out, details, no_aff = [], 0, 0
    for c in camps:
        if not isinstance(c, dict):
            continue
        if _pick(c, ["affiliates", "publishers"]) is None and details < max_details:     # the list had no publisher info: ask for this campaign in full
            cid = as_str(_pick(c, ["id", "campaignId"]))
            if cid:
                details += 1
                _nap(0.2)
                d = _json(_get(f"{BASE}/{acct}/campaigns/{cid}", headers), f"campaign {cid}")
                if isinstance(d, dict) and isinstance(d.get("campaign"), dict):
                    d = d["campaign"]
                if isinstance(d, dict):
                    c = {**c, **d}
        if _pick(c, ["affiliates", "publishers"]) is None:
            no_aff += 1
        out += parse_campaign_caps(c)
    first = next((c for c in camps if isinstance(c, dict)), {})
    aff = next((a for c in camps if isinstance(c, dict) for a in (_pick(c, ["affiliates", "publishers"]) or []) if isinstance(a, dict)), {})
    report = {"campaigns": len(camps), "with_caps": len(out), "detail_calls": details, "without_publisher_list": no_aff,
              "campaign_keys": sorted(first)[:40], "affiliate_keys": sorted(aff)[:40]}
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
