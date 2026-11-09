"""Talks to Ringba and turns the call-log CSV into clean Python dictionaries."""
import csv
import io
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
    "did": ["tag:DialedNumber:Number", "Number", "number"],
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
REQUIRED = ["call_id", "did", "publisher_id|publisher_name"]  # plus a date (checked separately)


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


# ---------- CSV -> records ----------
def map_headers(headers):
    """Returns {field: actual_header_in_file}."""
    exact = {h: h for h in headers}
    normed = {}
    for h in headers:
        normed.setdefault(norm(h), h)
    out = {}
    for field, names in ALIASES.items():
        for n in names:
            if n in exact:
                out[field] = exact[n]
                break
            if norm(n) in normed:
                out[field] = normed[norm(n)]
                break
    return out


def parse_csv(text):
    """Returns (records, report). report tells you what was found / missing."""
    text = text.lstrip("\ufeff")
    reader = csv.DictReader(io.StringIO(text))
    headers = reader.fieldnames or []
    m = map_headers(headers)
    missing = []
    if "call_id" not in m:
        missing.append("Inbound Call ID")
    if "did" not in m:
        missing.append("Dialed number / DID (tag:DialedNumber:Number or Number)")
    if "publisher_id" not in m and "publisher_name" not in m:
        missing.append("Publisher / Publisher ID")
    if "call_iso" not in m and "call_date" not in m:
        missing.append("Call date (tag:Date:ISODate or Call Date)")
    report = {"headers": len(headers), "mapped": m, "missing": missing, "rows": 0, "skipped": 0}
    if missing:
        return [], report

    def g(row, field):
        h = m.get(field)
        return row.get(h) if h else None

    by_id = {}
    for row in reader:
        report["rows"] += 1
        cid = as_str(g(row, "call_id"))
        when = parse_when(as_str(g(row, "call_iso")), g(row, "call_date"))
        if not cid or not when:
            report["skipped"] += 1
            continue
        by_id[cid] = {
            "call_id": cid,
            "call_at": when,
            "publisher_rid": as_str(g(row, "publisher_id")),
            "publisher_name": as_str(g(row, "publisher_name")),
            "buyer_rid": as_str(g(row, "buyer_id")),
            "buyer_name": as_str(g(row, "buyer_name")),
            "target_name": as_str(g(row, "target_name")),
            "campaign_name": as_str(g(row, "campaign_name")),
            "caller": as_str(g(row, "caller")),
            "did": normalize_phone(g(row, "did")),
            "did_rid": as_str(g(row, "did_id")),
            "connected": as_bool(g(row, "connected")),
            "converted": as_bool(g(row, "converted")),
            "paid": as_bool(g(row, "paid")),
            "is_live": as_bool(g(row, "is_live")),
            "is_duplicate": as_bool(g(row, "duplicate")),
            "blocked": as_bool(g(row, "blocked")),
            "incomplete": as_bool(g(row, "incomplete")),
            "duration_sec": as_secs(g(row, "duration")),
            "connected_sec": as_secs(g(row, "connected_len")),
            "revenue": as_num(g(row, "revenue")),
            "payout": as_num(g(row, "payout")),
            "profit": as_num(g(row, "profit")),
            "telco_cost": as_num(g(row, "telco_cost")),
        }
    return list(by_id.values()), report


# ---------- talking to Ringba ----------
def _z(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_csv_text(start, end, retries=3):
    if not config.RINGBA_TOKEN or not config.RINGBA_ACCOUNT_ID:
        raise RingbaError("RINGBA_API_TOKEN or RINGBA_ACCOUNT_ID is missing in your Secrets.")
    url = f"{BASE}/{config.RINGBA_ACCOUNT_ID}/calllogs/export/csv"
    headers = {"Authorization": f"Token {config.RINGBA_TOKEN}", "Content-Type": "application/json"}
    with open(COLUMNS_FILE, encoding="utf-8") as f:
        columns = json.load(f)
    body = {
        "reportStart": _z(start), "reportEnd": _z(end),
        "valueColumns": columns,
        "orderByColumns": [{"column": "callDt", "direction": "asc"}],
        "formatTimespans": True, "formatPercentages": True, "formatDateTime": True,
        "generateRollups": False, "filters": [], "formatTimeZone": "America/New_York",
    }
    last = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.post(url, headers=headers, json=body, timeout=180)
        except requests.RequestException as e:
            last = f"network error: {e}"
        else:
            if r.status_code == 200:
                text = r.content.decode("utf-8-sig", errors="replace")
                if text.lstrip().startswith(("{", "[")):
                    try:
                        j = json.loads(text)
                        link = next((j[k] for k in ("url", "downloadUrl", "fileUrl") if isinstance(j, dict) and j.get(k)), None)
                    except ValueError:
                        link = None
                    if link:
                        r2 = requests.get(link, timeout=180)
                        return r2.content.decode("utf-8-sig", errors="replace")
                    raise RingbaError(f"Ringba returned JSON instead of a CSV: {text[:300]}")
                return text
            last = f"HTTP {r.status_code}: {r.text[:300]}"
            if r.status_code in (401, 403):
                raise RingbaError(f"Ringba refused the login ({last}). Check RINGBA_API_TOKEN and RINGBA_ACCOUNT_ID.")
            if r.status_code == 404:
                raise RingbaError(f"Ringba says not found ({last}). Check RINGBA_ACCOUNT_ID.")
        time.sleep(2 * attempt)
    raise RingbaError(f"Ringba request failed after {retries} tries: {last}")


def fetch_range(start, end):
    """Download + parse one time window. Returns (records, report)."""
    text = fetch_csv_text(start, end)
    return parse_csv(text)


def test_connection(hours=3):
    """Used by the Settings button. Returns a plain dict you can show on screen."""
    from datetime import timedelta
    end = datetime.now(timezone.utc)
    recs, rep = fetch_range(end - timedelta(hours=hours), end)
    return {"records": len(recs), "report": rep, "sample": recs[:3]}
