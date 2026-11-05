import requests
import config
from datetime import datetime, timezone
from dateutil.parser import isoparse

def fetch_calls(start_time, end_time):
    """Fetches ALL calls from Ringba between two datetime objects (handles pagination)."""
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
        raw_calls = data.get("callLog", {}).get("data", [])
        
        if not raw_calls:
            break # No more calls
            
        for row in raw_calls:
            cols = row.get("columns", {})
            call_dt = isoparse(cols.get("callDt")) if cols.get("callDt") else datetime.now(timezone.utc)
            
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
            break # We reached the last page
            
        page += 1
        
    return all_parsed_calls
