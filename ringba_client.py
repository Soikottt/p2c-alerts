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
        raw_calls = data.get("callLog", {}).get("data", data.get("report", {}).get("records", []))
            
        if not raw_calls:
            break
            
        for row in raw_calls:
            cols = row.get("columns", row)
            
            # --- FIX: Handle millisecond timestamps AND strings ---
            dt_val = cols.get("callDt")
            if isinstance(dt_val, (int, float)):
                call_dt = datetime.fromtimestamp(dt_val / 1000.0, tz=timezone.utc)
            elif isinstance(dt_val, str):
                call_dt = isoparse(dt_val)
            else:
                call_dt = datetime.now(timezone.utc)
            
            # --- FIX: Handle None for connected status ---
            connected_raw = cols.get("hasConnected")
            is_connected = bool(connected_raw) if connected_raw is not None else False

            # Handle None for duration
            dur_raw = cols.get("connectedCallLengthInSeconds")
            duration = int(dur_raw) if dur_raw is not None else 0

            all_parsed_calls.append({
                "call_id": str(cols.get("inboundCallId", "")),
                "started_at": call_dt,
                "publisher_id": str(cols.get("publisherId", "")),
                "publisher_name": cols.get("publisherName", "Unknown Publisher"),
                "buyer_id": str(cols.get("buyerId", "")),
                "buyer_name": cols.get("buyer", "Unknown Buyer"),
                "did": str(cols.get("inboundPhoneNumber", "")),
                "connected": is_connected,
                "duration": duration
            })
            
        if len(raw_calls) < 500:
            break
            
        page += 1
        
    return all_parsed_calls
