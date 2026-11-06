import requests
import config
from datetime import datetime, timezone
from dateutil.parser import isoparse

def fetch_calls(start_time, end_time):
    url = f"https://api.ringba.com/v2/{config.RINGBA_ACCT}/calllogs"
    
    headers = {
        "Authorization": f"Token {config.RINGBA_TOKEN}",
        "Content-Type": "application/json"
    }
    
    all_parsed_calls = []
    page = 1
    
    while True:
        body = {
            "reportStart": start_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "reportEnd": end_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "valueColumns": [
                {"column": "callDt"},
                {"column": "inboundCallId"},
                {"column": "publisherId"},
                {"column": "publisherName"},
                {"column": "buyerId"},
                {"column": "buyer"},
                {"column": "inboundPhoneNumber"},
                {"column": "hasConnected"},
                {"column": "connectedCallLengthInSeconds"}
            ],
            "orderByColumns": [{"column": "callDt", "direction": "asc"}],
            "formatTimeZone": "America/New_York",
            "pagination": {"page": page, "pageSize": 500}
        }
        
        response = requests.post(url, headers=headers, json=body, timeout=30)
        if response.status_code != 200:
            raise Exception(f"Ringba API Error {response.status_code}: {response.text}")
            
        data = response.json()
        
        # --- THE FIX: Ringba now uses "report" -> "records" instead of "callLog" -> "data" ---
        raw_calls = []
        if "callLog" in data and "data" in data["callLog"]:
            raw_calls = data["callLog"]["data"]
        elif "report" in data and "records" in data["report"]:
            raw_calls = data["report"]["records"]
            
        if not raw_calls:
            break # No more calls
            
        for row in raw_calls:
            # The data might be flat or nested under "columns"
            cols = row.get("columns", row)
            
            # Sometimes callDt is a timestamp number, sometimes a string. We handle both.
            dt_val = cols.get("callDt")
            if isinstance(dt_val, (int, float)):
                call_dt = datetime.fromtimestamp(dt_val / 1000, tz=timezone.utc)
            elif isinstance(dt_val, str):
                call_dt = isoparse(dt_val)
            else:
                call_dt = datetime.now(timezone.utc)
            
            all_parsed_calls.append({
                "call_id": str(cols.get("inboundCallId", "")),
                "started_at": call_dt,
                "publisher_id": str(cols.get("publisherId", "")),
                "publisher_name": cols.get("publisherName", "Unknown Publisher"),
                "buyer_id": str(cols.get("buyerId", "")),
                "buyer_name": cols.get("buyer", "Unknown Buyer"),
                "did": str(cols.get("inboundPhoneNumber", "")),
                "connected": bool(cols.get("hasConnected", False)),
                "duration": int(cols.get("connectedCallLengthInSeconds", 0) or 0)
            })
            
        if len(raw_calls) < 500:
            break # Last page reached
            
        page += 1
        
    return all_parsed_calls