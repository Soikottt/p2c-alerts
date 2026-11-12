"""Email wording. Subject and message are editable in the app; {placeholders} are filled in per alert."""
import html
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
                     ("campaign", "Campaign this DID belongs to"),
                     ("idle_days", "Days without calls"), ("last_call", "Time of last call, or 'never'"),
                     ("calls_30d", "Total calls on this DID in the last 30 days"),
                     ("last_note", "The latest follow-up note from the affiliate manager, or 'none yet'"),
                     ("added", "When the DID was created in Ringba"), ("next_charge", "Date Ringba charges for it next"),
                     ("monthly_cost", "Monthly cost or 'not set'")],
    "engine_stale": [("gap_minutes", "Minutes the engine was offline"), ("last_sync", "Time of the last good sync")],
    "sync_error": [("error", "The error message")],
    "alert_response": [("responder", "Name of the person who answered"), ("choice", "The option they clicked"),
                       ("note", "Their note"), ("original_subject", "Subject of the original alert"),
                       ("effect_text", "What the system did automatically")],
}

_CALL_BODY = ("- **Time:** {time}\n- **Publisher:** {publisher}\n- **Buyer:** {buyer}\n- **DID (number dialed):** {did}\n"
              "- **Campaign:** {campaign}\n- **Connected:** {connected}   **Converted:** {converted}   **Paid:** {paid}")
_CAP_BODY = ("**{entity_type} {name}** has used **{percent}%** of its {period} cap.\n\n"
             "- **Usage:** {usage} of {cap} ({metric_label})\n- **Remaining:** {remaining}\n"
             "- **Window started:** {window_start}\n- **Alert level:** {level}%\n\n"
             "**Action:** contact the {entity_type} or set up another route before calls are wasted.")

DEFAULTS = {
    "new_publisher": ("New publisher {publisher} - call #{call_number} of {first_n}",
                      "A call arrived from publisher **{publisher}**.\nThis is **call #{call_number}** of the first {first_n} from this publisher.\n\n" + _CALL_BODY),
    "lapsed_publisher": ("Publisher {publisher} is back after {gap_days} days",
                         "Publisher **{publisher}** just sent a call after **{gap_days} days** of silence (previous call: {last_call_time}).\n\n" + _CALL_BODY),
    "watchlist_buyer": ("Watchlist buyer {buyer} connected a call",
                        "A call **CONNECTED** to watchlist buyer **{buyer}**.\n\n" + _CALL_BODY),
    "publisher_cap": ("CAP {percent}%: {name} ({period}, {metric_label})", _CAP_BODY),
    "buyer_cap": ("CAP {percent}%: {name} ({period}, {metric_label})", _CAP_BODY),
    "did_inactive": ("Inactive DID {did} - {idle_days} days without calls",
                     "**DID {did}{label_part}** has had **no calls for {idle_days} days**.\n\n"
                     "- **Publisher:** {publisher}\n- **Campaign:** {campaign}\n- **Last call on this DID:** {last_call}\n"
                     "- **Total calls in the last 30 days:** {calls_30d}\n- **Created in Ringba:** {added}\n"
                     "- **Next charge:** {next_charge}\n- **Monthly cost:** {monthly_cost}\n"
                     "- **Last follow-up note:** {last_note}\n\n"
                     "**Decision needed:** release this DID to avoid the next charge, or keep it for now?"),
    "engine_stale": ("Alert engine was offline for {gap_minutes} minutes",
                     "The last successful Ringba sync was at **{last_sync}** ({gap_minutes} minutes ago).\n"
                     "Calls in that gap are caught up now, but alerts were late. Check GitHub Actions if this repeats."),
    "sync_error": ("Ringba sync failed", "The engine could not read calls from Ringba.\n\n**Error:** {error}"),
    "alert_response": ("Answer received: {choice} ({original_subject})",
                       "**{responder}** answered an alert.\n\n- **Alert:** {original_subject}\n- **Answer:** {choice}\n"
                       "- **Note for the team:** {note}\n- **What the system did:** {effect_text}"),
}

# Earlier built-in wordings. A saved template that still has EXACTLY one of these is upgraded to the new look.
OLD_BODIES = {
    'new_publisher': ('A call arrived from publisher {publisher}.\nThis is call #{call_number} of the first {first_n} from this publisher.\n\nTime: {time}\nPublisher: {publisher}\nBuyer: {buyer}\nDID (number dialed): {did}\nCampaign: {campaign}\nConnected: {connected}   Converted: {converted}   Paid: {paid}',),
    'lapsed_publisher': ('Publisher {publisher} just sent a call after {gap_days} days of silence (previous call: {last_call_time}).\n\nTime: {time}\nPublisher: {publisher}\nBuyer: {buyer}\nDID (number dialed): {did}\nCampaign: {campaign}\nConnected: {connected}   Converted: {converted}   Paid: {paid}',),
    'watchlist_buyer': ('A call CONNECTED to watchlist buyer {buyer}.\n\nTime: {time}\nPublisher: {publisher}\nBuyer: {buyer}\nDID (number dialed): {did}\nCampaign: {campaign}\nConnected: {connected}   Converted: {converted}   Paid: {paid}',),
    'publisher_cap': ('{entity_type} {name} has used {percent}% of its {period} cap.\n\nUsage: {usage} of {cap} ({metric_label})\nRemaining: {remaining}\nWindow started: {window_start}\nAlert level: {level}%\n\nAction: contact the {entity_type} or set up another route before calls are wasted.',),
    'buyer_cap': ('{entity_type} {name} has used {percent}% of its {period} cap.\n\nUsage: {usage} of {cap} ({metric_label})\nRemaining: {remaining}\nWindow started: {window_start}\nAlert level: {level}%\n\nAction: contact the {entity_type} or set up another route before calls are wasted.',),
    'did_inactive': ('DID {did}{label_part} has had no calls for {idle_days} days.\n\nPublisher: {publisher}\nCampaign: {campaign}\nLast call on this DID: {last_call}\nTotal calls in the last 30 days: {calls_30d}\nCreated in Ringba: {added}\nNext charge: {next_charge}\nMonthly cost: {monthly_cost}\nLast follow-up note: {last_note}\n\nDecision needed: release this DID to avoid the next charge, or keep it for now?', 'DID {did}{label_part} has had no calls for {idle_days} days.\nCreated / added: {added}\nLast call: {last_call}\nMonthly cost: {monthly_cost}\n\nDecision needed: release this DID to avoid the monthly charge, or keep it for now?', 'DID {did}{label_part} has had no calls for {idle_days} days.\nPublisher: {publisher}\nCreated in Ringba: {added}\nLast call: {last_call}\nNext charge: {next_charge}\nMonthly cost: {monthly_cost}\n\nDecision needed: release this DID to avoid the next charge, or keep it for now?',),
    'engine_stale': ('The last successful Ringba sync was at {last_sync} ({gap_minutes} minutes ago).\nCalls in that gap are caught up now, but alerts were late. Check GitHub Actions if this repeats.',),
    'sync_error': ('The engine could not read calls from Ringba.\n\nError: {error}',),
    'alert_response': ('{responder} answered an alert.\n\nAlert: {original_subject}\nAnswer: {choice}\nNote: {note}\n\nWhat the system did: {effect_text}',),
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
                          "campaign": "Rehab", "calls_30d": "0", "last_note": "Mia (Nov 2): publisher paused, will restart next week",
                          "idle_days": "34", "last_call": "2026-10-06 04:41 PM EDT", "added": "2026-06-12 09:30 AM EDT",
                          "next_charge": "2026-11-28", "monthly_cost": "$2.50"}
SAMPLE["engine_stale"] = {"gap_minutes": "95", "last_sync": "2026-11-09 01:10 PM EST"}
SAMPLE["sync_error"] = {"error": "HTTP 429: Rate limit exceeded"}
SAMPLE["alert_response"] = {"responder": "Mia", "choice": "Remove this DID", "note": "Publisher confirmed they stopped",
                            "original_subject": "Inactive DID +16143505503 - 34 days without calls",
                            "effect_text": "Nothing changed automatically. Your team decides."}


# ---------------------------------------------------------------- formatting (what you type -> a nice email)
#   **bold**   *italic*   __underline__   # Heading   - bullet   1. numbered   [text](https://link)   --- line
#   indent a bullet by two spaces for a sub-bullet. A blank line starts a new paragraph.
FORMAT_HELP = [
    ("**bold text**", "bold text"), ("*italic text*", "italic text"), ("__underlined__", "underlined"),
    ("# Big heading   /   ## Smaller heading", "heading"),
    ("- a bullet   (or  * a bullet)", "bullet list"), ("  - a sub-bullet (2 spaces first)", "nested bullet"),
    ("1. first step   2. second step", "numbered list"), ("[click here](https://example.com)", "a link"),
    ("---", "a dividing line"), ("(empty line)", "new paragraph"),
]
_BULLET = re.compile(r"^(\s*)[-*\u2022]\s+(.*)$")
_NUMBER = re.compile(r"^(\s*)\d+[.)]\s+(.*)$")
_HEAD = re.compile(r"^\s*(#{1,3})\s+(.*)$")
_HR = re.compile(r"^\s*(-{3,}|_{3,}|\*{3,})\s*$")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+|mailto:[^)\s]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_UNDER = re.compile(r"__(.+?)__")
_ITAL = re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])")


def _inline_html(s):
    s = html.escape(s, quote=True)
    s = _LINK.sub(lambda m: f'<a href="{m.group(2)}" style="color:#1a73e8">{m.group(1)}</a>', s)
    s = _BOLD.sub(r"<b>\1</b>", s)
    s = _UNDER.sub(r"<u>\1</u>", s)
    return _ITAL.sub(r"<i>\1</i>", s)


def _inline_text(s):
    s = _LINK.sub(lambda m: f"{m.group(1)} ({m.group(2)})", s)
    s = _BOLD.sub(r"\1", s)
    s = _UNDER.sub(r"\1", s)
    return _ITAL.sub(r"\1", s)


def _value_html(v):
    return html.escape(str(v), quote=True).replace("\n", "<br>")


def to_html(text, ctx=None):
    """The template as an HTML email body. {placeholders} are filled with HTML-escaped values."""
    out, para, stack = [], [], []          # stack = open lists: ["ul"|"ol", ...]

    def close_lists(to=0):
        while len(stack) > to:
            out.append(f"</li></{stack.pop()}>")

    def flush_para():
        if para:
            out.append('<p style="margin:0 0 10px 0">' + "<br>".join(para) + "</p>")
            para.clear()

    for raw in (text or "").replace("\r\n", "\n").split("\n"):
        line = raw.rstrip()
        if not line.strip():
            flush_para(); close_lists()
            continue
        if _HR.match(line):
            flush_para(); close_lists()
            out.append('<hr style="border:0;border-top:1px solid #ddd;margin:12px 0">')
            continue
        h = _HEAD.match(line)
        if h:
            flush_para(); close_lists()
            size = {1: "20px", 2: "17px", 3: "15px"}[len(h.group(1))]
            out.append(f'<div style="font-size:{size};font-weight:bold;margin:12px 0 6px 0">{_inline_html(h.group(2))}</div>')
            continue
        b, n = _BULLET.match(line), _NUMBER.match(line)
        if b or n:
            flush_para()
            m = b or n
            kind = "ul" if b else "ol"
            level = min(len(m.group(1).replace("\t", "  ")) // 2, 3)
            while len(stack) > level + 1:
                out.append(f"</li></{stack.pop()}>")
            if len(stack) == level + 1:
                if stack[-1] != kind:
                    out.append(f"</li></{stack.pop()}>")
                    out.append(f"<{kind} style='margin:4px 0 8px 22px;padding:0'>"); stack.append(kind)
                else:
                    out.append("</li>")
            while len(stack) < level + 1:
                out.append(f"<{kind} style='margin:4px 0 8px 22px;padding:0'>"); stack.append(kind)
            out.append(f"<li style='margin:2px 0'>{_inline_html(m.group(2))}")
            continue
        close_lists()
        para.append(_inline_html(line))
    flush_para(); close_lists()
    body = "".join(out)
    body = re.sub(r"\{(\w+)\}", lambda m: _value_html(ctx[m.group(1)]) if ctx and m.group(1) in ctx else m.group(0), body)
    return ('<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.5;color:#222">' + body + "</div>")


def to_text(text, ctx=None):
    """The same template as clean plain text (for mail apps that do not show HTML, and for the Alert Log)."""
    out = []
    for raw in (text or "").replace("\r\n", "\n").split("\n"):
        line = raw.rstrip()
        if _HR.match(line):
            out.append("-" * 30); continue
        h = _HEAD.match(line)
        if h:
            out.append(_inline_text(h.group(2)).upper() if len(h.group(1)) == 1 else _inline_text(h.group(2))); continue
        b, n = _BULLET.match(line), _NUMBER.match(line)
        if b:
            pad = "  " * min(len(b.group(1).replace("\t", "  ")) // 2, 3)
            out.append(f"{pad}{chr(8226) if not pad else chr(9702)} {_inline_text(b.group(2))}"); continue
        if n:
            out.append(re.sub(r"^(\s*)(\d+[.)])\s+", r"\1\2 ", _inline_text(line))); continue
        out.append(_inline_text(line))
    plain = "\n".join(out)
    return re.sub(r"\{(\w+)\}", lambda m: str(ctx[m.group(1)]) if ctx and m.group(1) in ctx else m.group(0), plain)


def render_body(text, ctx=None):
    """(plain_text, html) for an email body."""
    return to_text(text, ctx), to_html(text, ctx)


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
