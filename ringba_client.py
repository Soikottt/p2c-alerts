"""Talks to Ringba and turns the call-log JSON into clean Python dictionaries."""
import json
import os
import re
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
from dateutil.parser import isoparse

import config

NY = ZoneInfo("America/New_York")
BASE = "https://api.ringba.com/v2"
COLUMNS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ringba_columns.json")


class RingbaError(Exception):
    pass


# For each thing we store: header names we accept (CSV display name first, then API column id).
ALIASES = {
    "call_id": ["Inbound Call ID", "inboundCallId"],
    "call_iso": ["tag:Date:ISODate"],
    "call_date": ["Call Date", "callDt"],
    "publisher_id": ["Publisher ID", "publisherId", "tag:Publisher:Id"],
    "publisher_name": ["Publisher", "publisherName", "tag:Publisher:Name"],
    "buyer_id": ["Buyer ID", "buyerId"],
    "buyer_name": ["Buyer", "buyer"],
    "target_name": ["Target", "targetName"],
    "campaign_name": ["Campaign", "campaignName", "tag:Campaign:Name"],
    "caller": ["Caller ID", "inboundPhoneNumber"],
    # THE DID = the number that was dialed (tracking number), NOT the caller's number.
    "did": ["tag:DialedNumber:Number", "Number", "number", "inboundPhoneNumber"],
    "did_id": ["Number ID", "numberId"],
    "connected": ["Connected", "hasConnected"],
    "converted": ["Converted", "hasConverted"],
    "paid": ["Paid Out", "hasPayout"],
    "is_live": ["Is Live", "isLive"],
    "duplicate": ["Is Duplicate", "isDuplicate"],
    "blocked": ["Blocked", "wasBlocked"],
    "incomplete": ["Incomplete", "isIncomplete"],
    "duration": ["Duration", "callLengthInSeconds", "tag:CallLength:Total"],
    "connected_len": ["Connected Call Length", "connectedCallLengthInSeconds"],
    "revenue": ["Revenue", "conversionAmount"],
    "payout": ["Payout", "payoutAmount"],
    "profit": ["Profit Net Telco", "profitNet"],
    "telco_cost": ["Telco Cost", "telcoCost"],
}


# ---------- value helpers ----------
def norm(h):
    return re.sub(r"[^a-z0-9]", "", (h or "").lower())


def as_str(v):
    s = (v or "").strip() if isinstance(v, str) or v is None else str(v).strip()
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
    if iso:
        try:
            d = isoparse(iso)
            return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
        except ValueError:
            pass
    raw = as_str(raw)
    if not raw:
        return None
    if re.fullmatch(r"\d{12,14}", raw):  # unix milliseconds
        return datetime.fromtimestamp(int(raw) / 1000.0, tz=timezone.utc)
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


# ---------- JSON Extraction Helpers ----------
def extract_field(row, field):
    """Safely gets a field from the JSON row based on ALIASES."""
    cols = row.get("columns", row)
    if field not in ALIASES:
        return None
    # Try exact match
    for key in ALIASES[field]:
        if key in cols:
            return cols[key]
    # Try case-insensitive match
    cols_lower = {k.lower(): v for k, v in cols.items()}
    for key in ALIASES[field]:
        if key.lower() in cols_lower:
            return cols_lower[key.lower()]
    return None


# ---------- talking to Ringba ----------
def _z(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_range(start, end):
    """Download + parse one time window using the JSON endpoint with pagination."""
    if not config.RINGBA_TOKEN or not config.RINGBA_ACCOUNT_ID:
        raise RingbaError("RINGBA_API_TOKEN or RINGBA_ACCOUNT_ID is missing in your Secrets.")

    url = f"{BASE}/{config.RINGBA_ACCOUNT_ID}/calllogs"
    headers = {"Authorization": f"Token {config.RINGBA_TOKEN}", "Content-Type": "application/json"}

    try:
        with open(COLUMNS_FILE, encoding="utf-8") as f:
            columns = json.load(f)
    except FileNotFoundError:
        columns = [{"column": c} for c in [
            "callDt", "inboundCallId", "publisherId", "publisherName", 
            "buyerId", "buyer", "inboundPhoneNumber", "hasConnected", 
            "connectedCallLengthInSeconds"
        ]]

    all_records = []
    seen_ids = set() # Safety: prevents infinite loops
    page = 1

    while True:
        body = {
            "reportStart": _z(start),
            "reportEnd": _z(end),
            "valueColumns": columns,
            "orderByColumns": [{"column": "callDt", "direction": "asc"}],
            "formatTimeZone": "America/New_York",
            "pagination": {"page": page, "pageSize": 100}
        }

        # Retry logic for Rate Limits (429)
        max_retries = 3
        response = None
        for attempt in range(max_retries):
            try:
                response = requests.post(url, headers=headers, json=body, timeout=60)
                if response.status_code == 429:
                    print(f"Rate limited (429). Waiting 10 seconds...")
                    time.sleep(10)
                else:
                    break
            except requests.RequestException as e:
                if attempt == max_retries - 1:
                    raise RingbaError(f"Network error: {e}")
                time.sleep(5)

        if response.status_code == 401 or response.status_code == 403:
            raise RingbaError(f"Ringba refused the login (HTTP {response.status_code}). Check your Token and Account ID.")
        if response.status_code != 200:
            raise RingbaError(f"Ringba API Error {response.status_code}: {response.text[:300]}")

        data = response.json()
        raw_calls = data.get("callLog", {}).get("data", data.get("report", {}).get("records", []))
            
        if not raw_calls:
            break

        new_records_this_page = 0
        for row in raw_calls:
            cid = as_str(extract_field(row, "call_id"))
            if not cid or cid in seen_ids:
                continue
            
            when = parse_when(as_str(extract_field(row, "call_iso")), extract_field(row, "call_date"))
            if not when:
                continue

            seen_ids.add(cid)
            new_records_this_page += 1
            
            all_records.append({
                "call_id": cid,
                "call_at": when,
                "publisher_rid": as_str(extract_field(row, "publisher_id")),
                "publisher_name": as_str(extract_field(row, "publisher_name")),
                "buyer_rid": as_str(extract_field(row, "buyer_id")),
                "buyer_name": as_str(extract_field(row, "buyer_name")),
                "target_name": as_str(extract_field(row, "target_name")),
                "campaign_name": as_str(extract_field(row, "campaign_name")),
                "caller": as_str(extract_field(row, "caller")),
                "did": normalize_phone(extract_field(row, "did")),
                "did_rid": as_str(extract_field(row, "did_id")),
                "connected": as_bool(extract_field(row, "connected")),
                "converted": as_bool(extract_field(row, "converted")),
                "paid": as_bool(extract_field(row, "paid")),
                "is_live": as_bool(extract_field(row, "is_live")),
                "is_duplicate": as_bool(extract_field(row, "duplicate")),
                "blocked": as_bool(extract_field(row, "blocked")),
                "incomplete": as_bool(extract_field(row, "incomplete")),
                "duration_sec": as_secs(extract_field(row, "duration")),
                "connected_sec": as_secs(extract_field(row, "connected_len")),
                "revenue": as_num(extract_field(row, "revenue")),
                "payout": as_num(extract_field(row, "payout")),
                "profit": as_num(extract_field(row, "profit")),
                "telco_cost": as_num(extract_field(row, "telco_cost")),
            })

        # If the API returns the exact same calls (ignoring pagination), stop to prevent infinite loop
        if new_records_this_page == 0:
            break
            
        page += 1
        time.sleep(1) # Prevent rate limits between pages

    report = {"rows": len(all_records), "skipped": 0, "mapped": list(ALIASES.keys()), "missing": []}
    return all_records, report


def test_connection(hours=3):
    """Used by the Settings button. Returns a plain dict you can show on screen."""
    from datetime import timedelta
    end = datetime.now(timezone.utc)
    recs, rep = fetch_range(end - timedelta(hours=hours), end)
    return {"records": len(recs), "report": rep, "sample": recs[:3]}
