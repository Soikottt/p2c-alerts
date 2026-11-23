"""Upcoming DID follow-up report: which DID alert will send another email, and when. Read-only: nothing here sends or changes anything.
It follows the same rules as rules.rule_inactive_dids / rules.rule_new_dids_no_calls (3-day reminders, 'wait N days' answers).

upcoming(now)  -> one row per DID that has a follow-up / reminder / repeat alert coming (or that was cancelled because the DID is retired)
preview(row)   -> (subject, plain text, html) of the email that will be sent next
history(limit) -> follow-ups, reminders and cancelled follow-ups that already happened
"""
from datetime import datetime, timedelta, timezone

import db
from db import to_dt
from timeutil import fmt

_MIN = datetime(1970, 1, 1, tzinfo=timezone.utc)
TYPES = ("did_inactive", "did_new_no_calls")


def _did_id(key):
    k = str(key or "")
    return int(k.split(":")[1]) if k.startswith(("did:", "newdid:")) and k.split(":")[1].isdigit() else None


def _settings():
    g = lambda k, d: max(int(float(db.get_setting(k, d) or 0)), 0)
    return {"remind_days": g("did_reminder_days", "3"), "remind_max": g("did_reminder_max", "3")}


def _status_text(d):
    if d["status"] != "active":
        return "Retired / not in Ringba"
    if d["never_alert"]:
        return "Active (never alert)"
    return "Active"


def _row(kind, d, pub, camp, chain_len, last_email, resp, snooze, next_when, note, will, alert_type, extra):
    days = None
    if resp and resp.get("responded_at") and snooze and to_dt(snooze["snooze_until"]) and to_dt(resp["responded_at"]) and to_dt(snooze["snooze_until"]) > to_dt(resp["responded_at"]):
        days = round((to_dt(snooze["snooze_until"]) - to_dt(resp["responded_at"])).total_seconds() / 86400)
    return {"kind": kind, "did_id": d["id"], "did": d["number"], "label": d["label"] or "", "publisher": pub.get(d["publisher_id"]) or d["affiliate_name"] or "",
            "campaign": camp.get(d["id"]) or "", "previous_response": (resp or {}).get("response_label") or "",
            "responded_by": (resp or {}).get("responded_by") or "", "responded_at": (resp or {}).get("responded_at"),
            "response_note": (resp or {}).get("response_note") or "", "snoozed_days": days, "last_email": last_email,
            "next_followup": next_when, "followups_sent": max(chain_len - 1, 0), "did_status": _status_text(d), "will_send": will,
            "why": note, "alert_type": alert_type, "extra": extra}


def _answers_by_did():
    """{did id: {'response_label','responded_by','responded_at','response_note'}} - the newest answer in any alert of the DID."""
    out = {}
    for a in db.q("SELECT snooze_key, response_label, responded_at, responded_by, response_note FROM alerts "
                  "WHERE alert_type IN ('did_inactive','did_new_no_calls') AND response_label IS NOT NULL ORDER BY id"):
        did = _did_id(a["snooze_key"])
        if did and (did not in out or (to_dt(a["responded_at"]) or _MIN) >= (to_dt(out[did]["responded_at"]) or _MIN)):
            out[did] = a
    return out


def upcoming(now=None):
    """Rows sorted by next date. 'will_send' says Yes / No + the reason, so you see at once what the engine will skip."""
    import dids as didtools
    now = now or db.now_utc()
    cfg = _settings()
    rule = db.q1("SELECT param_days FROM alert_rules WHERE alert_type='did_inactive'") or {}
    rule_new = db.q1("SELECT param_days, param_count FROM alert_rules WHERE alert_type='did_new_no_calls'") or {}
    idle_days = int(rule.get("param_days") or 30)
    every = max(int(rule_new.get("param_days") or 3), 1)
    dids = {d["id"]: d for d in db.q("SELECT * FROM dids")}
    pub = {p["id"]: p["name"] for p in db.q("SELECT id, name FROM publishers")}
    camp = didtools.campaign_names(list(dids.values())) if dids else {}
    snoozes = {r["entity_key"]: r for r in db.q("SELECT entity_key, snooze_until, reason FROM snoozes WHERE entity_key LIKE 'did:%'")}
    rows = []

    # ---- inactive DIDs: reminders and 'wait N days' follow-ups. Every DID chain is judged by chain.evaluate (the same code the engine uses).
    import chain as chainlib
    chains = {}                                     # base key -> alerts of that idle period, oldest first
    for a in db.q("SELECT id, dedupe_key, status, created_at, sent_at, response_label, responded_at, responded_by, response_note, case_status, case_updated_at "
                  "FROM alerts WHERE alert_type='did_inactive' AND status <> 'skipped' AND dedupe_key LIKE 'did:%' ORDER BY id"):
        parts = a["dedupe_key"].split(":")
        if len(parts) >= 4 and _did_id(a["dedupe_key"]) in dids:
            chains.setdefault(":".join(parts[:4]), []).append(a)
    newest = {}                                     # did id -> its newest chain (older idle periods are history, not follow-ups)
    for base, ch in chains.items():
        did = _did_id(base)
        if did not in newest or ch[0]["id"] > newest[did][1][0]["id"]:
            newest[did] = (base, ch)
    answers = _answers_by_did()                     # the newest answer of the DID in ANY of its alerts (older chains too)
    for did, (base, chain) in newest.items():
        d = dids[did]
        sn = snoozes.get(f"did:{did}")
        until = to_dt(sn["snooze_until"]) if sn else None
        ev = chainlib.evaluate(chain, now, until, cfg["remind_days"], cfg["remind_max"], base)
        last = chain[-1]
        when = to_dt(last["sent_at"] or last["created_at"])
        if ev["state"] == "closed":
            continue                                # the team finished it: nothing to follow
        resp = answers.get(did)
        scheduled = ev["next_at"] is not None
        nxt = ev["next_at"]
        will = "Yes"
        ref = to_dt(d["last_call_at"]) or to_dt(d["added_at"])
        if d["status"] != "active":
            will = "No - the DID is retired / deleted in Ringba"
        elif d["never_alert"]:
            will = "No - DID is set to never alert"
        elif not scheduled:
            will = "No - nothing scheduled (" + ev["note"].split(":")[0].lower() + ")"
        elif ref and nxt and (nxt - ref).days < idle_days and nxt > now:
            will = "Only if it still has no calls then"
        sent_n = sum(1 for a in chain if a["status"] != "pending")
        r = _row("Inactive DID", d, pub, camp, len(chain), when, resp, sn, nxt, ev["note"], will, "did_inactive",
                 {"tag": ev["tag"], "reminders": ev["reminders"], "base": base, "idle_ref": ref, "root_id": chain[0]["id"], "state": ev["state"]})
        r["scheduled"], r["state"], r["code"] = scheduled, ev["state"], f"#A{chain[0]['id']}"
        r["paused_until"] = until if ev["state"] in ("paused", "followup_due") else None
        rows.append(r)

    # ---- new DIDs without a first call: repeat alert every N days
    groups = {}
    ans_all = _answers_by_did()
    for a in db.q("SELECT id, dedupe_key, status, created_at, sent_at, response_label, responded_at, responded_by, response_note, case_status, case_updated_at "
                  "FROM alerts WHERE alert_type='did_new_no_calls' AND status <> 'skipped' AND dedupe_key LIKE 'newdid:%' ORDER BY id"):
        did = _did_id(a["dedupe_key"])
        if did in dids:
            groups.setdefault(did, []).append(a)
    for did, chain in groups.items():
        d = dids[did]
        last = chain[-1]
        if d["last_call_at"] is not None:
            continue                                # first call arrived: nothing more to follow up
        when = to_dt(last["sent_at"] or last["created_at"])
        import chain as chainlib
        if chainlib.closed_by_team(chain):
            continue
        sn = snoozes.get(f"did:{did}")
        until = to_dt(sn["snooze_until"]) if sn else None
        nxt = when + timedelta(days=every)
        note = f"Repeat alert every {every} day(s) until the first call"
        if until and until > nxt:
            nxt, note = until, "Alert comes back when the pause ends"
        resp = next((a for a in reversed(chain) if a["response_label"]), None) or ans_all.get(did)
        will = "Yes"
        if d["status"] != "active":
            will = "No - the DID is retired / deleted in Ringba"
        elif d["never_alert"]:
            will = "No - DID is set to never alert"
        r = _row("New DID, no calls yet", d, pub, camp, len(chain), when, resp, sn, nxt, note, will, "did_new_no_calls",
                 {"tag": "REMINDER: ", "n": len(chain), "root_id": chain[0]["id"]})
        r["scheduled"], r["state"], r["code"] = True, "repeat", f"#A{chain[0]['id']}"
        r["paused_until"] = until if until and until > now else None
        rows.append(r)
    rows.sort(key=lambda r: (not r["scheduled"], r["will_send"] != "Yes", r["next_followup"] or now))
    return rows


def preview(row, now=None):
    """(subject, plain text, html) of the email this row will send next, built from your template with today's DID data."""
    import alerts
    import rules
    import templates
    now = now or db.now_utc()
    d = db.q1("SELECT * FROM dids WHERE id=:i", i=row["did_id"])
    if not d:
        return "", "", ""
    import dids as didtools
    cfg = _settings()
    rule = alerts.get_rule(row["alert_type"])
    if not rule:
        return "", "", ""
    nxt = row["next_followup"] or now
    pub = row["publisher"] or "none"
    campaign = row["campaign"] or "unknown"
    base = {"did": d["number"], "label": d["label"] or "", "label_part": f" ({d['label']})" if d["label"] else "", "publisher": pub, "campaign": campaign,
            "added": fmt(d["added_at"]), "next_charge": fmt(d["next_charge_at"], False) if d["next_charge_at"] else "unknown",
            "monthly_cost": f"${float(d['monthly_cost']):.2f}" if d["monthly_cost"] is not None else "not set"}
    if row["alert_type"] == "did_inactive":
        days = int(rule.get("param_days") or 30)
        ref = row["extra"].get("idle_ref") or to_dt(d["added_at"])
        idle = max((nxt - ref).days, 0) if ref else days
        import cases
        ctx = {**base, "idle_days": idle, "last_call": fmt(d["last_call_at"]) if d["last_call_at"] else "never",
               "calls_30d": didtools.calls_last_30d(now).get(d["id"], 0),
               "last_note": cases.one_line(cases.latest_by_did().get(d["id"]), fmt) or "none yet",
               "next_step": rules.next_step_text(days, cfg["remind_days"], cfg["remind_max"], row["extra"].get("reminders", 0) + (0 if row["extra"].get("tag", "").startswith("FOLLOW") else 1), (idle // days + 1) * days)}
        tag = row["extra"].get("tag", "")
    else:
        every = max(int(rule.get("param_days") or 3), 1)
        ctx = {**base, "age_days": max((nxt - to_dt(d["added_at"])).days, 0), "alert_number": row["extra"].get("n", 1) + 1, "repeat_days": every,
               "next_step": (f"If nobody answers this email: another alert is sent in {every} day(s), until this DID gets its first call. "
                             f"If you click Pause, nothing is sent for the days you choose, then the alerts continue.")}
        tag = "REMINDER: "
    subj_t, body_t = templates.get_template(row["alert_type"], rule)
    if ctx.get("next_step") and "{next_step}" not in body_t:
        body_t = body_t + "\n\n{next_step}"
    root = row["extra"].get("root_id")
    if root:                       # a follow-up is an answer in the SAME e-mail conversation: same subject, same [#A..] code, plus the history
        import chain as chainlib
        ctx["history"] = chainlib.history_text(root)
        if "{history}" not in body_t:
            body_t = body_t + "\n\nHistory of answers on this alert:\n{history}"
        parent = db.q1("SELECT subject FROM alerts WHERE id=:i", i=int(root))
        base_subject = parent["subject"] if parent else ""
        subject = base_subject if base_subject.lower().startswith("re:") else ("Re: " + base_subject if base_subject else "")
        text, html = templates.render_body(body_t, ctx)
        if tag.strip():
            text = f"{tag.strip()}\n\n{text}"
            html = f"<p><b>{tag.strip()}</b></p>{html}" if html else html
        return subject, text, html
    subject = f"{db.get_setting('subject_prefix', '[P2C Alert]')} {tag}{templates.render(subj_t, ctx)}".strip()
    text, html = templates.render_body(body_t, ctx)
    return subject, text, html


def history(limit=300):
    """Follow-ups, reminders and repeat alerts that were made, and follow-ups that were cancelled because the DID is retired. Newest first."""
    rows = db.q("SELECT id, alert_type, dedupe_key, status, created_at, sent_at, response_label, responded_by, case_status, snooze_key, last_error, thread_id "
                "FROM alerts WHERE alert_type IN ('did_inactive','did_new_no_calls') ORDER BY id DESC LIMIT :n", n=int(limit) * 3)
    dids = {d["id"]: d for d in db.q("SELECT id, number, label, status, publisher_id FROM dids")}
    pub = {p["id"]: p["name"] for p in db.q("SELECT id, name FROM publishers")}
    out = []
    for a in rows:
        key = a["dedupe_key"]
        tail = key.split(":", 4)
        cancelled = key.endswith(":cancelled")
        if a["alert_type"] == "did_inactive":
            sub = ":".join(key.split(":")[4:]) if len(key.split(":")) > 4 else ""
            kind = "Follow-up (pause ended)" if sub.startswith("f") else "Reminder (no answer)" if sub.startswith("r") else ""
            if cancelled:
                kind = "Follow-up CANCELLED"
        else:
            n = key.split(":")[2] if len(key.split(":")) > 2 else "1"
            kind = "Repeat alert" if (n.isdigit() and int(n) > 1) else ""
            if cancelled:
                kind = "Follow-up CANCELLED"
        if not kind:
            continue
        d = dids.get(_did_id(a["snooze_key"] or key))
        out.append({"alert_id": a["id"], "code": f"#A{a['thread_id'] or a['id']}", "when": to_dt(a["sent_at"] or a["created_at"]), "kind": kind, "did": d["number"] if d else "?",
                    "label": (d["label"] if d else "") or "", "publisher": pub.get(d["publisher_id"], "") if d else "",
                    "email_status": a["status"], "response": a["response_label"] or "", "case": a["case_status"] or "open",
                    "did_now": _status_text({**d, "never_alert": False}) if d else "?", "note": a["last_error"] or ""})
        if len(out) >= limit:
            break
    return out
