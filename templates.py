"""Email wording. Subject and message are editable in the app; {placeholders} are filled in per alert."""
import re

import db

CALL_PH = [("publisher", "Publisher name"), ("buyer", "Buyer name"), ("did", "DID (number dialed)"),
           ("caller", "Caller's number"), ("campaign", "Campaign"), ("time", "Time of the call"),
           ("connected", "Yes / No"), ("converted", "Yes / No"), ("paid", "Yes / No")]
CAP_PH = [("entity_type", "publisher or buyer"), ("name", "Publisher / buyer name"), ("percent", "Percent used now"),
          ("level", "The alert level that was reached (e.g. 80)"), ("usage", "Calls used"), ("cap", "Cap size"),
          ("remaining", "Calls left"), ("period", "daily / monthly"), ("metric_label", "What counts (paid calls ...)"),
          ("window_start", "When this cap window started")]

PLACEHOLDERS = {
    "new_publisher": [("call_number", "1 for the first call, 2 for the second ..."), ("first_n", "The N in 'first N calls'")] + CALL_PH,
    "lapsed_publisher": [("gap_days", "Days of silence"), ("last_call_time", "Time of the previous call")] + CALL_PH,
    "watchlist_buyer": CALL_PH,
    "publisher_cap": CAP_PH,
    "buyer_cap": CAP_PH,
    "did_inactive": [("did", "The DID"), ("label", "Your label for it"), ("label_part", "' (label)' or empty"),
                     ("publisher", "Publisher this DID is assigned to in Ringba, or 'none'"),
                     ("idle_days", "Days without calls"), ("last_call", "Time of last call, or 'never'"),
                     ("added", "When the DID was created in Ringba"), ("next_charge", "Date Ringba charges for it next"),
                     ("monthly_cost", "Monthly cost or 'not set'")],
    "engine_stale": [("gap_minutes", "Minutes the engine was offline"), ("last_sync", "Time of the last good sync")],
    "sync_error": [("error", "The error message")],
    "alert_response": [("responder", "Name of the person who answered"), ("choice", "The option they clicked"),
                       ("note", "Their note"), ("original_subject", "Subject of the original alert"),
                       ("effect_text", "What the system did automatically")],
}

_CALL_BODY = ("Time: {time}\nPublisher: {publisher}\nBuyer: {buyer}\nDID (number dialed): {did}\n"
              "Campaign: {campaign}\nConnected: {connected}   Converted: {converted}   Paid: {paid}")
_CAP_BODY = ("{entity_type} {name} has used {percent}% of its {period} cap.\n\n"
             "Usage: {usage} of {cap} ({metric_label})\nRemaining: {remaining}\nWindow started: {window_start}\n"
             "Alert level: {level}%\n\nAction: contact the {entity_type} or set up another route before calls are wasted.")

DEFAULTS = {
    "new_publisher": ("New publisher {publisher} - call #{call_number} of {first_n}",
                      "A call arrived from publisher {publisher}.\nThis is call #{call_number} of the first {first_n} from this publisher.\n\n" + _CALL_BODY),
    "lapsed_publisher": ("Publisher {publisher} is back after {gap_days} days",
                         "Publisher {publisher} just sent a call after {gap_days} days of silence (previous call: {last_call_time}).\n\n" + _CALL_BODY),
    "watchlist_buyer": ("Watchlist buyer {buyer} connected a call",
                        "A call CONNECTED to watchlist buyer {buyer}.\n\n" + _CALL_BODY),
    "publisher_cap": ("CAP {percent}%: {name} ({period}, {metric_label})", _CAP_BODY),
    "buyer_cap": ("CAP {percent}%: {name} ({period}, {metric_label})", _CAP_BODY),
    "did_inactive": ("Inactive DID {did} - {idle_days} days without calls",
                     "DID {did}{label_part} has had no calls for {idle_days} days.\nPublisher: {publisher}\n"
                     "Created in Ringba: {added}\nLast call: {last_call}\nNext charge: {next_charge}\n"
                     "Monthly cost: {monthly_cost}\n\n"
                     "Decision needed: release this DID to avoid the next charge, or keep it for now?"),
    "engine_stale": ("Alert engine was offline for {gap_minutes} minutes",
                     "The last successful Ringba sync was at {last_sync} ({gap_minutes} minutes ago).\n"
                     "Calls in that gap are caught up now, but alerts were late. Check GitHub Actions if this repeats."),
    "sync_error": ("Ringba sync failed", "The engine could not read calls from Ringba.\n\nError: {error}"),
    "alert_response": ("Answer received: {choice} ({original_subject})",
                       "{responder} answered an alert.\n\nAlert: {original_subject}\nAnswer: {choice}\n"
                       "Note: {note}\n\nWhat the system did: {effect_text}"),
}

SAMPLE = {
    "call": {"time": "2026-11-09 03:15 PM EST", "publisher": "Acme Media", "buyer": "Best Rehab Co", "did": "+16143505503",
             "caller": "+15125550123", "campaign": "Rehab", "connected": "Yes", "converted": "No", "paid": "No"},
    "cap": {"entity_type": "buyer", "name": "Best Rehab Co", "percent": "82", "level": "80", "usage": "41", "cap": "50",
            "remaining": "9", "period": "daily", "metric_label": "paid calls", "window_start": "2026-11-09 12:00 AM EST"},
}
SAMPLE["new_publisher"] = {**SAMPLE["call"], "call_number": "3", "first_n": "10"}
SAMPLE["lapsed_publisher"] = {**SAMPLE["call"], "gap_days": "41", "last_call_time": "2026-09-29 11:02 AM EDT"}
SAMPLE["watchlist_buyer"] = SAMPLE["call"]
SAMPLE["publisher_cap"] = {**SAMPLE["cap"], "entity_type": "publisher", "name": "Acme Media", "metric_label": "converted calls"}
SAMPLE["buyer_cap"] = SAMPLE["cap"]
SAMPLE["did_inactive"] = {"did": "+16143505503", "label": "Rehab line", "label_part": " (Rehab line)", "publisher": "Acme Media",
                          "idle_days": "34", "last_call": "2026-10-06 04:41 PM EDT", "added": "2026-06-12 09:30 AM EDT",
                          "next_charge": "2026-11-28", "monthly_cost": "$2.50"}
SAMPLE["engine_stale"] = {"gap_minutes": "95", "last_sync": "2026-11-09 01:10 PM EST"}
SAMPLE["sync_error"] = {"error": "HTTP 429: Rate limit exceeded"}
SAMPLE["alert_response"] = {"responder": "Mia", "choice": "Remove this DID", "note": "Publisher confirmed they stopped",
                            "original_subject": "Inactive DID +16143505503 - 34 days without calls",
                            "effect_text": "Nothing changed automatically. Your team decides."}


def render(text, ctx):
    """Fill {placeholders}. Unknown ones are left visible so typos are easy to spot."""
    ctx = ctx or {}
    return re.sub(r"\{(\w+)\}", lambda m: str(ctx[m.group(1)]) if m.group(1) in ctx else m.group(0), text or "")


def get_template(alert_type, rule=None):
    """(subject, body) from the template chosen for this alert, else the first saved one, else built-in."""
    tid = (rule or {}).get("template_id")
    row = db.q1("SELECT subject, body FROM email_templates WHERE id=:i AND alert_type=:t", i=tid, t=alert_type) if tid else None
    if row is None:
        row = db.q1("SELECT subject, body FROM email_templates WHERE alert_type=:t ORDER BY id LIMIT 1", t=alert_type)
    if row is not None:
        return row["subject"], row["body"]
    return DEFAULTS[alert_type]
