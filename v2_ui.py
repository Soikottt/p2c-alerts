"""Streamlit UI pages for P2C Alert Center V2 (merged). Every table is paged and has a CSV download."""
import html
import re
from datetime import timedelta

import pandas as pd
import streamlit as st

import cases
import db
import v2_features as v2
from timeutil import fmt

PAGE_SIZE = 50
PERMANENT = timedelta(days=3650)


# ---------------------------------------------------------------- small helpers
def _me():
    return str(st.session_state.get("user") or "").strip()


def _open_alert_center(alert_id):
    """Navigate to Alert Center and select the requested alert before widgets render."""
    st.session_state["v2_selected_alert_id"] = int(alert_id)
    st.session_state["page_v2"] = "🔎 Alert Center"


def _colour(status):
    """Streamlit only knows these colours for :colour[text]."""
    return {'sent': 'green', 'received': 'green', 'saved': 'green', 'failed': 'red', 'pending': 'orange',
            'retrying': 'orange', 'dry_run': 'blue'}.get(str(status or '').lower(), 'gray')


def _status_badge(status):
    return _colour(status)


def _safe_html(raw):
    """Outgoing HTML (made by this app): remove anything active."""
    raw = str(raw or '')
    raw = re.sub(r'(?is)<(script|iframe|object|embed|form|meta|base)[^>]*>.*?</\1\s*>', '', raw)
    raw = re.sub(r'(?is)<(script|iframe|object|embed|form|meta|base)[^>]*/?\s*>', '', raw)
    raw = re.sub(r'(?i)\s+on[a-z]+\s*=\s*("[^"]*"|\'[^\']*\'|[^\s>]+)', '', raw)
    raw = re.sub(r'(?i)(href|src)\s*=\s*(["\'])\s*javascript:[^"\']*\2', r'\1="#"', raw)
    return raw[:200000]


def _inbound_html(raw):
    """Incoming HTML comes from outside: only the allow-list of the task editor (no scripts, styles, images, forms)."""
    try:
        import tasks
        return tasks.clean_html(raw)
    except Exception:
        return "<pre>" + html.escape(re.sub(r'(?s)<[^>]+>', ' ', str(raw or ''))[:20000]) + "</pre>"


def _white(body_html):
    return f"<div style='background:#fff;color:#111;font-family:Arial,sans-serif;padding:10px'>{body_html}</div>"


def _show_html(raw, height=380, outgoing=True):
    import streamlit.components.v1 as components
    components.html(_white(_safe_html(raw) if outgoing else _inbound_html(raw)), height=height, scrolling=True)


def paged_table(key, df, csv_name, page_size=PAGE_SIZE, empty="Nothing to show."):
    """A table with page buttons and a CSV download of ALL rows (not only the page)."""
    if df is None or len(df) == 0:
        st.info(empty)
        return 0
    n = len(df)
    pages = max((n + page_size - 1) // page_size, 1)
    c1, c2 = st.columns([3, 1])
    with c2:
        page = int(st.number_input("Page", 1, pages, 1, key=f"{key}_pg", help=f"{n:,} rows, {page_size} per page")) if pages > 1 else 1
    with c1:
        st.caption(f"{n:,} rows" + (f" · page {page} of {pages}" if pages > 1 else ""))
    st.dataframe(df.iloc[(page - 1) * page_size: page * page_size], use_container_width=True, hide_index=True)
    st.download_button("⬇ CSV (all rows)", df.to_csv(index=False).encode("utf-8-sig"), file_name=csv_name, mime="text/csv", key=f"{key}_csv")
    return n


def _since_choice(key, default="30 days"):
    opts = {"7 days": 7, "30 days": 30, "90 days": 90, "365 days": 365, "All time": 36500}
    pick = st.selectbox("Period", list(opts), index=list(opts).index(default), key=key)
    return db.now_utc() - timedelta(days=opts[pick]), pick


# ---------------------------------------------------------------- Alert Center
def _email_card(a, expanded_key):
    status = a.get('status') or 'unknown'
    label = 'Follow-up' if 'Follow-up' in str(a.get('alert_type')) or ':r' in str(a.get('dedupe_key') or '') else 'Alert email'
    with st.container(border=True):
        left, right = st.columns([5, 1])
        left.markdown(f"**{label} · #{a['id']}**")
        right.markdown(f":{_colour(status)}[**{str(status).replace('_', ' ').title()}**]")
        st.caption(f"{fmt(a.get('sent_at') or a.get('created_at'))} · To: {a.get('to_emails') or '—'}")
        st.markdown(f"**{a.get('subject') or '(no subject)'}**")
        key = f"v2_mail_{expanded_key}_{a['id']}"
        if st.button("Collapse email" if st.session_state.get(key) else "View full email", key=f"btn_{key}", use_container_width=True):
            st.session_state[key] = not st.session_state.get(key, False)
            st.rerun()
        if st.session_state.get(key):
            st.caption(f"CC: {a.get('cc_emails') or '—'}")
            if a.get('body_html'):
                _show_html(a['body_html'], 420)
            else:
                st.code(a.get('body') or '', language=None)
            if a.get('last_error'):
                st.error(f"Delivery error: {a['last_error']}")


def _inbound_card(mail, key):
    with st.container(border=True):
        st.markdown("**Incoming email reply**  ·  :green[Received]")
        st.caption(f"{fmt(mail.get('received_at'))} · From: {mail.get('from_email') or 'Unknown'}")
        st.markdown(f"**{mail.get('subject') or '(no subject)'}**")
        with st.expander("View complete original reply", expanded=False):
            st.caption(f"To: {mail.get('to_emails') or '—'} · CC: {mail.get('cc_emails') or '—'}")
            if mail.get('body_html'):
                _show_html(mail['body_html'], 360, outgoing=False)
            else:
                st.text(mail.get('body_text') or '(No readable message body)')
            if mail.get('in_reply_to'):
                st.caption(f"In-Reply-To: {mail['in_reply_to']}")


def _row_from_details(a):
    return {'id': a['id'], 'thread_id': a.get('thread_id'), 'alert_type': a.get('alert_type'), 'subject': a.get('subject'),
            'status': a.get('status'), 'case_status': a.get('case_status'), 'created_at': a.get('created_at'),
            'sent_at': a.get('sent_at'), 'did': a.get('did'), 'publisher': a.get('publisher_name')}


def page_alert_center():
    st.title("Alert Center")
    st.caption("Search any alert (A325, 325, #A325, DID, publisher, buyer, e-mail address, subject or words inside an e-mail) "
               "and read its whole conversation on the right.")
    q1, q2 = st.columns([5, 1])
    with q1:
        query = st.text_input("Universal search", value=st.session_state.get('v2_alert_query', ''),
                              placeholder="A325 · DID · publisher · buyer · email · subject…", key="v2_alert_query")
    with q2:
        st.write("")
        st.write("")
        st.button("Search", key="v2_search_button", use_container_width=True)
    if query.strip():
        results = v2.search_alerts(query, 100)
    else:
        results = db.q("""SELECT a.id,a.thread_id,a.alert_type,a.subject,a.status,a.case_status,a.created_at,a.sent_at,
                            a.to_emails,a.cc_emails,a.last_error,d.number AS did,COALESCE(p.name,d.affiliate_name,'') AS publisher
                     FROM alerts a LEFT JOIN dids d ON d.id=CASE WHEN split_part(COALESCE(a.snooze_key,''),':',1)='did' AND split_part(COALESCE(a.snooze_key,''),':',2) ~ '^[0-9]+$' THEN split_part(a.snooze_key,':',2)::INTEGER ELSE NULL END
                     LEFT JOIN publishers p ON p.id=d.publisher_id ORDER BY a.created_at DESC LIMIT 100""")
    selected = st.session_state.get('v2_selected_alert_id')
    selected = int(selected) if selected else None
    ids = [int(r['id']) for r in results]
    if selected is not None and selected not in ids:
        # an alert opened from another page must stay open even when the current search does not list it
        one = v2.alert_details(selected)
        if one:
            results = [_row_from_details(one)] + list(results)
            ids = [selected] + ids
        else:
            selected = None
    if not results:
        st.info("No matching alerts. Try an alert ID, phone number, publisher name or subject keyword.")
        return
    if selected is None:
        selected = ids[0]
    left, right = st.columns([1.25, 1], gap="large")
    with left:
        st.subheader(f"Alerts ({len(results)})")
        for r in results:
            active = int(r['id']) == selected
            with st.container(border=True):
                c1, c2 = st.columns([5, 1])
                c1.markdown(f"**A{r['id']} · {r.get('publisher') or r.get('alert_type') or 'Alert'}**")
                c1.caption(f"{r.get('did') or 'No DID'} · {fmt(r.get('created_at'))}")
                c1.write(r.get('subject') or '')
                c2.markdown(f":{_colour(r.get('status'))}[{str(r.get('status') or '').replace('_', ' ').title()}]")
                if c2.button("Open" if not active else "Opened", key=f"v2_open_{r['id']}", disabled=active, use_container_width=True):
                    st.session_state['v2_selected_alert_id'] = int(r['id'])
                    st.rerun()
    with right:
        a = v2.alert_details(selected)
        if not a:
            st.warning("This alert could not be found.")
            return
        root, mails, events = v2.conversation(selected)
        st.markdown("### Conversation history")
        st.caption(f"Thread #A{root} · {len(mails)} email records · {len(events)} activities")
        st.markdown(f"**{a.get('publisher_name') or a.get('alert_type')}**")
        st.caption(f"Alert A{a['id']} · DID: {a.get('did') or '—'} · DID status: {a.get('did_status') or '—'}")
        m1, m2, m3 = st.columns(3)
        m1.metric("Emails", len([x for x in mails if x.get('alert_type') != 'alert_response']))
        m2.metric("Activities", len(events))
        m3.metric("Team status", cases.label(a.get('case_status')))
        tab_all, tab_emails, tab_replies, tab_notes = st.tabs(["All activity", "Emails", "Replies", "Notes & status"])
        cap = st.session_state.get(f"v2_tl_cap_{root}", 40)

        def show_timeline(mode='all'):
            shown = 0
            for ev in events:
                kind = ev.get('event') or ''
                if mode == 'emails' and kind != 'Email':
                    continue
                if mode == 'replies' and kind != 'Reply':
                    continue
                if mode == 'notes' and kind not in ('Note', 'Answer', 'Cancelled', 'Status'):
                    continue
                shown += 1
                if shown > cap:
                    break
                with st.container(border=True):
                    c1, c2 = st.columns([4, 1])
                    c1.markdown(f"**{ev.get('kind') or kind}**")
                    c1.caption(f"{fmt(ev.get('when'))} · {ev.get('who') or 'System'} · A{ev.get('alert_id')}")
                    c2.caption(kind)
                    st.write(ev.get('text') or '')
                    archived = ev.get('email')
                    if archived:
                        _inbound_card(archived, f"{root}_{ev.get('alert_id')}")
                    elif kind == 'Email':
                        record = next((x for x in mails if int(x['id']) == int(ev.get('alert_id') or -1)), None)
                        if record:
                            _email_card(record, f"{root}")
            if shown > cap and st.button("Show more", key=f"v2_more_{mode}_{root}"):
                st.session_state[f"v2_tl_cap_{root}"] = cap + 40
                st.rerun()

        with tab_all:
            show_timeline('all')
        with tab_emails:
            for mail in mails:
                if mail.get('alert_type') != 'alert_response':
                    _email_card(mail, f"{root}_emails")
        with tab_replies:
            if not any(e.get('event') == 'Reply' for e in events):
                st.info("No replies yet. Replies received from now on are stored with their complete original text. "
                        "Older replies stay short, as they were saved before.")
            show_timeline('replies')
        with tab_notes:
            show_timeline('notes')
        with st.expander("Add internal note / update status"):
            with st.form(f"v2_note_{selected}"):
                who = st.text_input("Your name", value=_me(), key=f"v2_note_who_{selected}")
                status_values = cases.STATUSES
                current = a.get('case_status') or 'open'
                status = st.selectbox("Team status", status_values, index=status_values.index(current) if current in status_values else 0,
                                      format_func=cases.label, key=f"v2_note_status_{selected}")
                note = st.text_area("Internal note", key=f"v2_note_text_{selected}")
                save = st.form_submit_button("Save activity", type="primary")
            if save:
                if not note.strip() and status == current:
                    st.warning("Add a note or change the status first.")
                else:
                    cases.add_note(selected, 'team', who, note.strip() or f"Status changed to {cases.label(status)}", status if status != current else None)
                    st.success("Saved.")
                    st.rerun()


# ---------------------------------------------------------------- Email Center
def _mail_df(rows):
    return pd.DataFrame([{'Date': fmt(r.get('happened_at') or r.get('sent_at')), 'Direction': r.get('direction'),
                          'Alert': f"A{r.get('alert_id') or r.get('id')}", 'Subject': r.get('subject'),
                          'To / From': r.get('sender_to'), 'Status': r.get('status'), 'Error': r.get('last_error') or ''} for r in rows])


def page_email_center():
    st.title("Email Center")
    st.caption("Everything that was sent or received, with safe retry for failed e-mails.")
    q = st.text_input("Search emails", placeholder="Subject, address, alert ID…", key="v2_email_search").lower().strip()
    rows = v2.email_center_rows(1500)
    if q:
        rows = [r for r in rows if q in ' '.join(str(r.get(k) or '') for k in ('subject', 'sender_to', 'cc_emails', 'id', 'alert_id')).lower()]
    outbox = [r for r in rows if r['direction'] == 'outbound' and r.get('status') in ('pending', 'retrying')]
    sent = [r for r in rows if r['direction'] == 'outbound' and r.get('status') in ('sent', 'dry_run')]
    failed = [r for r in rows if r['direction'] == 'outbound' and r.get('status') == 'failed']
    incoming = [r for r in rows if r['direction'] == 'inbound']
    t_out, t_sent, t_fail, t_in, t_tpl, t_prev, t_hist = st.tabs(
        [f"Outbox ({len(outbox)})", f"Sent ({len(sent)})", f"Failed ({len(failed)})", f"Incoming replies ({len(incoming)})",
         "Templates", "Preview", "Delivery history"])
    with t_out:
        st.caption("Waiting to be sent by the engine (runs every 5 minutes).")
        paged_table("v2_ob", _mail_df(outbox), "outbox.csv", empty="The outbox is empty.")
    with t_sent:
        paged_table("v2_sent", _mail_df(sent), "sent_emails.csv", empty="No sent e-mails.")
    with t_fail:
        st.caption("Retry puts a FAILED e-mail back in the queue. A sent e-mail can never be retried, so nobody gets it twice.")
        paged_table("v2_fail", _mail_df(failed), "failed_emails.csv", empty="No failed e-mails. 🎉")
        if failed:
            pick = st.selectbox("Failed e-mail", range(len(failed)), key="v2_retry_pick",
                                format_func=lambda i: f"A{failed[i]['id']} · {failed[i].get('subject') or ''} · {(failed[i].get('last_error') or '')[:60]}")
            if st.button("Retry this e-mail", key="v2_retry_btn", type="primary"):
                import mailer
                if mailer.retry_failed(failed[pick]['id']):
                    st.success("Back in the queue. The engine sends it within 5 minutes.")
                else:
                    st.warning("It is not in the failed state any more, so nothing was changed.")
    with t_in:
        paged_table("v2_in", _mail_df(incoming), "incoming_replies.csv", empty="No archived replies yet.")
    with t_tpl:
        st.info("Edit the alert wording on the **📝 Email templates** page in the left menu.")
        st.button("Go to Email templates", key="v2_goto_tpl", on_click=lambda: st.session_state.__setitem__("page_v2", "📝 Email templates"))
    with t_prev:
        pool = [r for r in rows if r['direction'] == 'outbound']
        if not pool:
            st.info("No e-mails to preview yet.")
        else:
            i = st.selectbox("E-mail", range(min(len(pool), 200)), key="v2_prev_pick",
                             format_func=lambda i: f"A{pool[i]['id']} · {pool[i].get('subject') or ''}")
            a = v2.alert_details(pool[i]['id'])
            if a:
                st.caption(f"To: {a.get('to_emails') or '—'} · CC: {a.get('cc_emails') or '—'}")
                if a.get('body_html'):
                    _show_html(a['body_html'], 480)
                else:
                    st.code(a.get('body') or '', language=None)
    with t_hist:
        try:
            hist = db.q("SELECT alert_id, direction, delivery_status, error, subject, to_emails, from_email, received_at FROM email_archive "
                        "ORDER BY received_at DESC NULLS LAST LIMIT 3000")
        except Exception:
            hist = []
        df = pd.DataFrame([{'Date': fmt(h['received_at']), 'Alert': f"A{h['alert_id']}" if h['alert_id'] else '', 'Direction': h['direction'],
                            'Delivery': h.get('delivery_status') or '', 'Subject': h['subject'], 'Address': h.get('to_emails') or h.get('from_email') or '',
                            'Error': h.get('error') or ''} for h in hist])
        paged_table("v2_hist", df, "delivery_history.csv", empty="No delivery history yet.")
    openable = [r for r in rows if r.get('alert_id') or r.get('direction') == 'outbound'][:300]
    if openable:
        st.divider()
        choice = st.selectbox("Open an email's alert conversation", range(len(openable)),
                              format_func=lambda i: f"A{openable[i].get('alert_id') or openable[i].get('id')} · {openable[i].get('subject') or '(no subject)'}",
                              key="v2_email_open_pick")
        st.button("Open conversation", key="v2_email_open_btn", on_click=_open_alert_center,
                  args=(int(openable[choice].get('alert_id') or openable[choice].get('id')),))


# ---------------------------------------------------------------- Audit log
def page_audit_log():
    st.title("Audit Log")
    st.caption("Who changed what, when, and what it was before and after. Entries from before this version are not invented.")
    q = st.text_input("Filter", placeholder="Alert ID (A325), DID, person, action or word…", key="v2_audit_search").strip()
    where, p = "", {}
    m = re.match(r"^#?A?(\d{1,9})$", q, re.I)
    if m:
        where, p = "WHERE alert_id=:n OR did_id=:n", {"n": int(m.group(1))}
    elif q:
        where, p = ("WHERE actor ILIKE :l OR action ILIKE :l OR entity_type ILIKE :l OR COALESCE(entity_id,'') ILIKE :l OR "
                    "COALESCE(detail,'') ILIKE :l OR COALESCE(old_value,'') ILIKE :l OR COALESCE(new_value,'') ILIKE :l"), {"l": f"%{q}%"}
    try:
        rows = db.q(f"SELECT at, actor, action, entity_type, entity_id, alert_id, did_id, old_value, new_value, detail FROM audit_log {where} ORDER BY id DESC LIMIT 5000", **p)
    except Exception as e:
        st.error(f"The audit table is not ready yet. Run 'Set up database' once. ({e})")
        return
    df = pd.DataFrame([{'When': fmt(r['at']), 'Who': r['actor'] or 'system', 'Action': r['action'],
                        'What': f"{r['entity_type']}:{r['entity_id'] or ''}", 'Alert': f"A{r['alert_id']}" if r['alert_id'] else '',
                        'DID': r['did_id'] or '', 'Before': r['old_value'] or '', 'After': r['new_value'] or '', 'Details': r['detail'] or ''} for r in rows])
    paged_table("v2_audit", df, "audit_log.csv", empty="No audit entries match.")
    with st.expander("Older V2 audit events (read only)"):
        try:
            old = v2.audit_rows(500)
        except Exception:
            old = []
        paged_table("v2_audit_old", pd.DataFrame([{'When': fmt(r['created_at']), 'Event': r['event_type'], 'Alert': f"A{r['alert_id']}" if r['alert_id'] else '',
                                                   'Who': r['actor'] or 'System', 'Summary': r['summary'], 'Details': r['details'] or ''} for r in old]),
                    "audit_events_old.csv", empty="None.")


# ---------------------------------------------------------------- Follow-up control center
def _did_key(did_id):
    return f"did:{int(did_id)}"


def _audit(action, did_id, old, new, detail):
    try:
        import audit
        audit.log(action, "did", did_id, did_id=int(did_id), old=old, new=new, detail=detail)
    except Exception:
        pass


def _control(row, key):
    """Pause / reschedule / resume / cancel the follow-ups of one DID. Uses the snooze mechanism the engine already respects."""
    did = int(row["did_id"])
    k = _did_key(did)
    cur = db.q1("SELECT snooze_until, reason FROM snoozes WHERE entity_key=:k", k=k)
    old = f"{fmt(cur['snooze_until'])} ({cur['reason']})" if cur else "not paused"
    c1, c2 = st.columns(2)
    with c1:
        days = st.number_input("Days", 1, 365, 7, key=f"{key}_d")
        if st.button("⏸ Pause for these days", key=f"{key}_pause", use_container_width=True):
            until = db.now_utc() + timedelta(days=int(days))
            db.x("INSERT INTO snoozes (entity_key, snooze_until, reason) VALUES (:k,:u,:r) ON CONFLICT (entity_key) DO UPDATE SET snooze_until=EXCLUDED.snooze_until, reason=EXCLUDED.reason",
                 k=k, u=until, r=f"Paused by {_me() or 'team'}")
            _audit("followup_paused", did, old, fmt(until), f"DID {row['did']} paused {days} days")
            st.rerun()
        if st.button("▶ Resume now", key=f"{key}_resume", use_container_width=True):
            db.x("DELETE FROM snoozes WHERE entity_key=:k", k=k)
            _audit("followup_resumed", did, old, "not paused", f"DID {row['did']} resumed")
            st.rerun()
    with c2:
        when = st.date_input("New follow-up date", value=(db.now_utc() + timedelta(days=3)).date(), key=f"{key}_date")
        if st.button("🗓 Reschedule", key=f"{key}_resched", use_container_width=True):
            from datetime import datetime, timezone
            until = datetime(when.year, when.month, when.day, 9, 0, tzinfo=timezone.utc)
            if until <= db.now_utc():
                st.warning("Pick a date in the future.")
            else:
                db.x("INSERT INTO snoozes (entity_key, snooze_until, reason) VALUES (:k,:u,:r) ON CONFLICT (entity_key) DO UPDATE SET snooze_until=EXCLUDED.snooze_until, reason=EXCLUDED.reason",
                     k=k, u=until, r=f"Rescheduled by {_me() or 'team'}")
                _audit("followup_rescheduled", did, old, fmt(until), f"DID {row['did']} follow-up moved")
                st.rerun()
        if st.button("🛑 Cancel follow-ups", key=f"{key}_cancel", use_container_width=True,
                     help="No more follow-ups for this DID until you press Resume."):
            until = db.now_utc() + PERMANENT
            db.x("INSERT INTO snoozes (entity_key, snooze_until, reason) VALUES (:k,:u,:r) ON CONFLICT (entity_key) DO UPDATE SET snooze_until=EXCLUDED.snooze_until, reason=EXCLUDED.reason",
                 k=k, u=until, r=f"Cancelled follow-ups by {_me() or 'team'}")
            _audit("followup_cancelled", did, old, "cancelled", f"DID {row['did']} follow-ups cancelled")
            st.rerun()


def page_followup_center():
    import followups
    st.title("Follow-up Control Center")
    st.caption("See every follow-up, and pause, move, cancel or resume it. Retired or deleted DIDs never get new follow-ups.")
    now = db.now_utc()
    try:
        rows = followups.upcoming(now)
    except Exception as e:
        st.error(f"Could not load follow-ups: {e}")
        return
    def cancelled(r):
        return str(r.get("status") or "").lower().startswith("cancel") or (r.get("paused_until") and r["paused_until"] > now + timedelta(days=1000))
    active = [r for r in rows if not cancelled(r) and "retired" not in str(r.get("will_send")).lower()]
    upcoming_rows = [r for r in active if r.get("scheduled") and r.get("next_followup") and r["next_followup"] >= now]
    overdue = [r for r in active if r.get("scheduled") and r.get("next_followup") and r["next_followup"] < now]
    gone = [r for r in rows if cancelled(r)]
    c = st.columns(4)
    c[0].metric("Upcoming", len(upcoming_rows)); c[1].metric("Overdue", len(overdue))
    c[2].metric("Cancelled", len(gone)); c[3].metric("All tracked DIDs", len(rows))
    t_up, t_over, t_done, t_can = st.tabs(["Upcoming", "Overdue", "Completed", "Cancelled"])

    def table(rs, name):
        return pd.DataFrame([{'Code': r['code'], 'DID': r['did'], 'Publisher': r['publisher'], 'Kind': r['kind'], 'Next follow-up': fmt(r['next_followup']) if r['next_followup'] else '-',
                              'Will send': r['will_send'], 'Status': r['status'], 'Last answer': r['previous_response']} for r in rs])

    def controls(rs, key):
        if rs:
            i = st.selectbox("Choose a DID to manage", range(len(rs)), key=f"{key}_pick",
                             format_func=lambda i: f"{rs[i]['code']} · {rs[i]['did']} · {rs[i]['publisher']}")
            with st.expander("Pause · reschedule · cancel · resume", expanded=False):
                _control(rs[i], f"{key}_c")
            st.button("Open alert conversation", key=f"{key}_open", on_click=_open_alert_center, args=(int(rs[i]['extra']['root_id']),))

    with t_up:
        paged_table("v2_fu_up", table(upcoming_rows, "up"), "followups_upcoming.csv", empty="No upcoming follow-ups.")
        controls(upcoming_rows, "v2_fu_up")
    with t_over:
        st.caption("The date has passed. The engine sends these on its next run unless the DID is paused, retired or answered.")
        paged_table("v2_fu_over", table(overdue, "over"), "followups_overdue.csv", empty="Nothing is overdue.")
        controls(overdue, "v2_fu_over")
    with t_done:
        try:
            h = followups.history(500)
        except Exception:
            h = []
        h = [x for x in h if "CANCELLED" not in x["kind"]]
        paged_table("v2_fu_done", pd.DataFrame([{'When': fmt(x['when']), 'Code': x['code'], 'Kind': x['kind'], 'DID': x['did'], 'Publisher': x['publisher'],
                                                 'E-mail': x['email_status'], 'Answer': x['response'], 'Team status': cases.label(x['case'])} for x in h]),
                    "followups_completed.csv", empty="No follow-ups have been made yet.")
    with t_can:
        try:
            h = [x for x in followups.history(500) if "CANCELLED" in x["kind"]]
        except Exception:
            h = []
        paged_table("v2_fu_can", table(gone, "can"), "followups_cancelled.csv", empty="No cancelled follow-ups.")
        controls(gone, "v2_fu_can")
        if h:
            st.caption("Cancelled automatically (DID retired / deleted):")
            paged_table("v2_fu_can2", pd.DataFrame([{'When': fmt(x['when']), 'Code': x['code'], 'DID': x['did'], 'Publisher': x['publisher'], 'Note': x['note']} for x in h]),
                        "followups_auto_cancelled.csv")


# ---------------------------------------------------------------- DID lifecycle
def lifecycle_state(d, snooze_until, last_alert_at, now, idle_days=30):
    """One plain word for where a DID is in its life."""
    st_ = str(d.get("status") or "active").lower()
    if st_ == "deleted":
        return "Deleted"
    if st_ != "active":
        return "Retired"
    if d.get("never_alert"):
        return "Never alert"
    if snooze_until and snooze_until > now + timedelta(days=1000):
        return "Follow-ups cancelled"
    if snooze_until and snooze_until > now:
        return "Paused"
    last_call = db.to_dt(d.get("last_call_at"))
    if last_call is None:
        return "New (no calls yet)"
    if last_alert_at and last_call > last_alert_at:
        return "Recovered"
    if (now - last_call).days >= idle_days:
        return "Inactive"
    return "Active"


def page_did_lifecycle():
    st.title("DID Lifecycle")
    st.caption("New → Active → Inactive → Paused → Recovered → Retired / Deleted, worked out from calls, alerts and pauses.")
    now = db.now_utc()
    rule = db.q1("SELECT param_days FROM alert_rules WHERE alert_type='did_inactive'") or {}
    idle = int(rule.get("param_days") or 30)
    dids = db.q("SELECT d.*, COALESCE(p.name, d.affiliate_name, '') AS pub FROM dids d LEFT JOIN publishers p ON p.id=d.publisher_id ORDER BY d.number")
    snz = {r["entity_key"]: db.to_dt(r["snooze_until"]) for r in db.q("SELECT entity_key, snooze_until FROM snoozes WHERE entity_key LIKE 'did:%'")}
    last_alert = {r["snooze_key"]: db.to_dt(r["m"]) for r in db.q(
        "SELECT snooze_key, MAX(created_at) AS m FROM alerts WHERE alert_type IN ('did_inactive','did_new_no_calls') AND status <> 'skipped' AND snooze_key LIKE 'did:%' GROUP BY snooze_key")}
    out = []
    for d in dids:
        k = _did_key(d["id"])
        out.append({**d, "state": lifecycle_state(d, snz.get(k), last_alert.get(k), now, idle), "last_alert": last_alert.get(k)})
    counts = pd.Series([o["state"] for o in out]).value_counts() if out else pd.Series(dtype=int)
    cols = st.columns(min(max(len(counts), 1), 6))
    for col, (name, n) in zip(cols, counts.items()):
        col.metric(name, int(n))
    states = ["All"] + sorted(counts.index.tolist())
    pick = st.selectbox("Show", states, key="v2_lc_state")
    q = st.text_input("Find DID / publisher", key="v2_lc_q").strip().lower()
    view = [o for o in out if (pick == "All" or o["state"] == pick) and (not q or q in f"{o['number']} {o['pub']} {o.get('label') or ''}".lower())]
    df = pd.DataFrame([{'DID': o['number'], 'Label': o.get('label') or '', 'Publisher': o['pub'], 'State': o['state'], 'DID status': o.get('status'),
                        'Last call': fmt(o['last_call_at']) if o.get('last_call_at') else '-', 'Last alert': fmt(o['last_alert']) if o['last_alert'] else '-',
                        'Monthly cost': o.get('monthly_cost')} for o in view])
    paged_table("v2_lc", df, "did_lifecycle.csv", empty="No DIDs match.")
    if view:
        i = st.selectbox("DID history", range(min(len(view), 500)), key="v2_lc_pick", format_func=lambda i: f"{view[i]['number']} · {view[i]['pub']} · {view[i]['state']}")
        d = view[i]
        alerts_ = db.q("SELECT id, alert_type, subject, status, created_at, case_status FROM alerts WHERE snooze_key=:k ORDER BY id DESC LIMIT 200", k=_did_key(d["id"]))
        log = db.q("SELECT at, actor, action, old_value, new_value, detail FROM audit_log WHERE did_id=:i OR (entity_type='did' AND entity_id=:s) ORDER BY id DESC LIMIT 200",
                   i=int(d["id"]), s=str(d["id"]))
        st.markdown("**Alerts for this DID**")
        paged_table("v2_lc_al", pd.DataFrame([{'Alert': f"A{a['id']}", 'Date': fmt(a['created_at']), 'Type': a['alert_type'], 'Subject': a['subject'],
                                               'E-mail': a['status'], 'Team': cases.label(a['case_status'])} for a in alerts_]), f"did_{d['number']}_alerts.csv", empty="No alerts yet.")
        st.markdown("**Changes (audit trail)**")
        paged_table("v2_lc_log", pd.DataFrame([{'When': fmt(r['at']), 'Who': r['actor'] or 'system', 'Action': r['action'], 'Before': r['old_value'] or '',
                                                'After': r['new_value'] or '', 'Details': r['detail'] or ''} for r in log]), f"did_{d['number']}_history.csv", empty="No recorded changes.")
        if alerts_:
            st.button("Open latest alert conversation", key="v2_lc_open", on_click=_open_alert_center, args=(int(alerts_[0]['id']),))
    deleted = db.q("SELECT at, actor, entity_id, detail FROM audit_log WHERE entity_type='did' AND action ILIKE '%delet%' ORDER BY id DESC LIMIT 200")
    if deleted:
        with st.expander(f"Deleted DIDs on record ({len(deleted)})"):
            paged_table("v2_lc_del", pd.DataFrame([{'When': fmt(r['at']), 'Who': r['actor'] or 'system', 'DID id': r['entity_id'], 'Details': r['detail'] or ''} for r in deleted]),
                        "deleted_dids.csv")


# ---------------------------------------------------------------- Analytics
def _hours(a, b):
    a, b = db.to_dt(a), db.to_dt(b)
    return (b - a).total_seconds() / 3600 if a and b and b >= a else None


def page_analytics():
    st.title("Operations Analytics")
    since, label = _since_choice("v2_an_period")
    pubs = db.q("SELECT id, name FROM publishers ORDER BY name")
    pf = st.selectbox("Publisher", [None] + pubs, format_func=lambda p: "All publishers" if p is None else p["name"], key="v2_an_pub")
    tabs = st.tabs(["Overview", "Publishers", "Buyers", "DIDs", "Alerts", "Managers", "Follow-ups", "E-mail delivery", "DID recovery", "DID cost"])
    pwhere, pp = ("AND c.publisher_id=:pid", {"pid": pf["id"]}) if pf else ("", {})

    with tabs[0]:
        t = db.q1("SELECT COUNT(*) AS n FROM alerts WHERE status <> 'skipped' AND created_at>=:s", s=since)['n']
        s_ = db.q1("SELECT COUNT(*) AS n FROM alerts WHERE status='sent' AND created_at>=:s", s=since)['n']
        f_ = db.q1("SELECT COUNT(*) AS n FROM alerts WHERE status='failed' AND created_at>=:s", s=since)['n']
        a_ = db.q1("SELECT COUNT(*) AS n FROM dids WHERE status='active'")['n']
        r_ = db.q1("SELECT COUNT(*) AS n FROM dids WHERE status<>'active'")['n']
        for col, lab, v in zip(st.columns(5), [f'Alerts ({label})', 'Sent', 'Failed', 'Active DIDs', 'Retired/deleted DIDs'], [t, s_, f_, a_, r_]):
            col.metric(lab, f"{int(v or 0):,}")

    with tabs[1]:
        rows = db.q(f"""SELECT COALESCE(p.name,'(none)') AS name, COUNT(*) AS calls,
                        SUM(CASE WHEN c.connected THEN 1 ELSE 0 END) AS connected, SUM(CASE WHEN c.converted THEN 1 ELSE 0 END) AS converted,
                        COALESCE(SUM(c.payout),0) AS payout, COALESCE(SUM(c.profit),0) AS profit
                        FROM calls c LEFT JOIN publishers p ON p.id=c.publisher_id WHERE c.call_at>=:s {pwhere}
                        GROUP BY COALESCE(p.name,'(none)') ORDER BY calls DESC LIMIT 500""", s=since, **pp)
        paged_table("v2_an_pub_t", pd.DataFrame([{'Publisher': r['name'], 'Calls': r['calls'], 'Connected': r['connected'], 'Converted': r['converted'],
                                                  'Connect %': round(100 * (r['connected'] or 0) / r['calls'], 1) if r['calls'] else 0,
                                                  'Convert %': round(100 * (r['converted'] or 0) / r['calls'], 1) if r['calls'] else 0,
                                                  'Payout': round(float(r['payout'] or 0), 2), 'Profit': round(float(r['profit'] or 0), 2)} for r in rows]),
                    "publisher_performance.csv", empty="No calls in this period.")

    with tabs[2]:
        rows = db.q(f"""SELECT COALESCE(b.name,'(none)') AS name, COUNT(*) AS calls,
                        SUM(CASE WHEN c.connected THEN 1 ELSE 0 END) AS connected, SUM(CASE WHEN c.converted THEN 1 ELSE 0 END) AS converted,
                        COALESCE(SUM(c.revenue),0) AS revenue, COALESCE(SUM(c.profit),0) AS profit
                        FROM calls c LEFT JOIN buyers b ON b.id=c.buyer_id WHERE c.call_at>=:s {pwhere}
                        GROUP BY COALESCE(b.name,'(none)') ORDER BY calls DESC LIMIT 500""", s=since, **pp)
        paged_table("v2_an_buy_t", pd.DataFrame([{'Buyer': r['name'], 'Calls': r['calls'], 'Connected': r['connected'], 'Converted': r['converted'],
                                                  'Connect %': round(100 * (r['connected'] or 0) / r['calls'], 1) if r['calls'] else 0,
                                                  'Revenue': round(float(r['revenue'] or 0), 2), 'Profit': round(float(r['profit'] or 0), 2)} for r in rows]),
                    "buyer_performance.csv", empty="No calls in this period.")

    with tabs[3]:
        rows = db.q(f"""SELECT d.number, COALESCE(p.name,d.affiliate_name,'') AS pub, d.status, COUNT(c.id) AS calls,
                        SUM(CASE WHEN c.converted THEN 1 ELSE 0 END) AS converted
                        FROM dids d LEFT JOIN publishers p ON p.id=d.publisher_id
                        LEFT JOIN calls c ON c.did_id=d.id AND c.call_at>=:s
                        WHERE 1=1 {'AND d.publisher_id=:pid' if pf else ''} GROUP BY d.number, COALESCE(p.name,d.affiliate_name,''), d.status
                        ORDER BY calls DESC LIMIT 2000""", s=since, **pp)
        paged_table("v2_an_did_t", pd.DataFrame([{'DID': r['number'], 'Publisher': r['pub'], 'Status': r['status'], 'Calls': r['calls'], 'Converted': r['converted'] or 0} for r in rows]),
                    "did_performance.csv", empty="No DIDs.")

    alerts_rows = db.q("SELECT id, alert_type, status, created_at, sent_at, responded_at, responded_by, response_label, case_status FROM alerts "
                       "WHERE status <> 'skipped' AND created_at>=:s ORDER BY id DESC LIMIT 20000", s=since)
    with tabs[4]:
        g = {}
        for a in alerts_rows:
            x = g.setdefault(a['alert_type'], {'n': 0, 'sent': 0, 'failed': 0, 'answered': 0, 'hrs': []})
            x['n'] += 1; x['sent'] += a['status'] == 'sent'; x['failed'] += a['status'] == 'failed'; x['answered'] += bool(a['response_label'])
            h = _hours(a['sent_at'] or a['created_at'], a['responded_at'])
            if h is not None:
                x['hrs'].append(h)
        paged_table("v2_an_al_t", pd.DataFrame([{'Alert type': k, 'Alerts': v['n'], 'Sent': v['sent'], 'Failed': v['failed'], 'Answered': v['answered'],
                                                 'Answer %': round(100 * v['answered'] / v['n'], 1), 'Avg hours to answer': round(sum(v['hrs']) / len(v['hrs']), 1) if v['hrs'] else None}
                                                for k, v in sorted(g.items(), key=lambda kv: -kv[1]['n'])]), "alert_performance.csv", empty="No alerts in this period.")

    with tabs[5]:
        g = {}
        for a in alerts_rows:
            if not a['responded_by']:
                continue
            x = g.setdefault(a['responded_by'], [])
            h = _hours(a['sent_at'] or a['created_at'], a['responded_at'])
            if h is not None:
                x.append(h)
        paged_table("v2_an_mgr_t", pd.DataFrame([{'Manager': k, 'Answers': len(v), 'Avg hours to respond': round(sum(v) / len(v), 1) if v else None,
                                                  'Fastest (h)': round(min(v), 1) if v else None, 'Slowest (h)': round(max(v), 1) if v else None}
                                                 for k, v in sorted(g.items(), key=lambda kv: -len(kv[1]))]), "manager_response_time.csv", empty="No answered alerts in this period.")

    with tabs[6]:
        fu = [a for a in alerts_rows if a['alert_type'] in ('did_inactive', 'did_new_no_calls')]
        threads = db.q("SELECT thread_id, COUNT(*) AS n FROM alerts WHERE thread_id IS NOT NULL AND created_at>=:s GROUP BY thread_id", s=since)
        closed = len([a for a in fu if a['case_status'] in ('done', 'no_action')])
        c1, c2, c3 = st.columns(3)
        c1.metric("DID alerts", len(fu)); c2.metric("Closed by the team", closed)
        c3.metric("Avg follow-ups per thread", f"{(sum(t['n'] for t in threads) / len(threads)):.1f}" if threads else "0")
        st.caption("Closed = status Done or No action needed. Threads = alerts that have follow-ups or replies.")

    with tabs[7]:
        by = db.q("SELECT status, COUNT(*) AS n FROM alerts WHERE created_at>=:s GROUP BY status ORDER BY n DESC", s=since)
        paged_table("v2_an_del_t", pd.DataFrame([{'Status': r['status'], 'E-mails': r['n']} for r in by]), "email_delivery.csv", empty="No e-mails.")
        try:
            inc = db.q1("SELECT COUNT(*) AS n FROM email_archive WHERE direction IN ('inbound','in') AND received_at>=:s", s=since)['n']
            st.metric("Replies received", int(inc))
        except Exception:
            pass

    with tabs[8]:
        st.caption("Recovered = the DID had a call after the alert. A simple indicator, not proof the alert caused it.")
        rows = db.q("""SELECT a.id, a.created_at, d.number, COALESCE(p.name,d.affiliate_name,'') AS publisher,
                             CASE WHEN EXISTS(SELECT 1 FROM calls c WHERE c.did_id=d.id AND c.call_at>a.created_at) THEN TRUE ELSE FALSE END AS recovered
                      FROM alerts a JOIN dids d ON d.id=CASE WHEN split_part(COALESCE(a.snooze_key,''),':',1)='did' AND split_part(COALESCE(a.snooze_key,''),':',2) ~ '^[0-9]+$' THEN split_part(a.snooze_key,':',2)::INTEGER ELSE NULL END
                      LEFT JOIN publishers p ON p.id=d.publisher_id
                      WHERE a.alert_type IN ('did_inactive','did_new_no_calls') AND a.status <> 'skipped' AND a.created_at>=:s """ + ("AND d.publisher_id=:pid " if pf else "") +
                     "ORDER BY a.created_at DESC LIMIT 5000", s=since, **pp)
        if rows:
            rec = sum(1 for r in rows if r['recovered'])
            st.metric("Observed DID recovery rate", f"{rec / len(rows) * 100:.1f}%", help=f"{rec} of {len(rows)} alerts have a later call.")
            paged_table("v2_an_rec_t", pd.DataFrame([{'Alert': f"A{r['id']}", 'DID': r['number'], 'Publisher': r['publisher'], 'Alert date': fmt(r['created_at']),
                                                      'Later call': 'Yes' if r['recovered'] else 'No'} for r in rows]), "did_recovery.csv")
        else:
            st.info("No DID alerts in this period.")

    with tabs[9]:
        rows = db.q("SELECT COALESCE(p.name,d.affiliate_name,'(none)') AS pub, COUNT(*) AS n, COALESCE(SUM(d.monthly_cost),0) AS cost "
                    "FROM dids d LEFT JOIN publishers p ON p.id=d.publisher_id WHERE d.status='active' GROUP BY COALESCE(p.name,d.affiliate_name,'(none)') ORDER BY cost DESC")
        total = sum(float(r['cost'] or 0) for r in rows)
        st.metric("Monthly DID cost (active)", f"{total:,.2f}")
        idle = db.q1("SELECT COALESCE(SUM(monthly_cost),0) AS c FROM dids WHERE status='active' AND last_call_at IS NOT NULL AND last_call_at < :t", t=db.now_utc() - timedelta(days=30))
        st.metric("Cost of DIDs with no call for 30+ days", f"{float(idle['c'] or 0):,.2f}")
        paged_table("v2_an_cost_t", pd.DataFrame([{'Publisher': r['pub'], 'Active DIDs': r['n'], 'Monthly cost': round(float(r['cost'] or 0), 2)} for r in rows]), "did_cost.csv")


# ---------------------------------------------------------------- Publisher / buyer profiles
def page_entity_profiles(entity='publisher'):
    title = 'Publisher Profiles' if entity == 'publisher' else 'Buyer Profiles'
    table = 'publishers' if entity == 'publisher' else 'buyers'
    st.title(title)
    records = db.q(f"SELECT * FROM {table} ORDER BY name")
    if not records:
        st.info(f"No {entity} records available yet.")
        return
    pick = st.selectbox(f"Select {entity}", records, format_func=lambda r: f"{r['name']} · {r['status']}", key=f"v2_{entity}_profile")
    st.subheader(pick['name'])
    c1, c2, c3 = st.columns(3)
    if entity == 'publisher':
        c1.metric('Active DIDs', db.q1("SELECT COUNT(*) AS n FROM dids WHERE publisher_id=:i AND status='active'", i=pick['id'])['n'])
        c2.metric('Calls', db.q1("SELECT COUNT(*) AS n FROM calls WHERE publisher_id=:i", i=pick['id'])['n'])
        c3.metric('Alerts', db.q1("SELECT COUNT(*) AS n FROM alerts a JOIN dids d ON d.id=CASE WHEN split_part(COALESCE(a.snooze_key,''),':',1)='did' AND split_part(COALESCE(a.snooze_key,''),':',2) ~ '^[0-9]+$' THEN split_part(a.snooze_key,':',2)::INTEGER ELSE NULL END WHERE d.publisher_id=:i", i=pick['id'])['n'])
        detail = db.q("SELECT number,label,status,last_call_at,monthly_cost FROM dids WHERE publisher_id=:i ORDER BY status,number", i=pick['id'])
        st.markdown('**Assigned DIDs**')
        paged_table(f"v2_{entity}_dids", pd.DataFrame(detail), f"{pick['name']}_dids.csv", empty="No DIDs.")
        calls = db.q("SELECT call_at,campaign_name,connected,converted,paid,duration_sec,payout,profit FROM calls WHERE publisher_id=:i ORDER BY call_at DESC LIMIT 1000", i=pick['id'])
        related_alerts = db.q("""SELECT a.id,a.created_at,a.subject,a.status,a.case_status FROM alerts a
                                 JOIN dids d ON d.id=CASE WHEN split_part(COALESCE(a.snooze_key,''),':',1)='did' AND split_part(COALESCE(a.snooze_key,''),':',2) ~ '^[0-9]+$' THEN split_part(a.snooze_key,':',2)::INTEGER ELSE NULL END
                                 WHERE d.publisher_id=:i ORDER BY a.id DESC LIMIT 500""", i=pick['id'])
    else:
        c1.metric('Calls', db.q1("SELECT COUNT(*) AS n FROM calls WHERE buyer_id=:i", i=pick['id'])['n'])
        c2.metric('Connected', db.q1("SELECT COUNT(*) AS n FROM calls WHERE buyer_id=:i AND connected=TRUE", i=pick['id'])['n'])
        c3.metric('Targets', db.q1("SELECT COUNT(*) AS n FROM targets WHERE buyer_id=:i", i=pick['id'])['n'])
        calls = db.q("SELECT call_at,campaign_name,connected,converted,paid,duration_sec,revenue,profit FROM calls WHERE buyer_id=:i ORDER BY call_at DESC LIMIT 1000", i=pick['id'])
        related_alerts = db.q("SELECT id,created_at,subject,status,case_status FROM alerts WHERE subject ILIKE :q OR body ILIKE :q ORDER BY id DESC LIMIT 500", q=f"%{pick['name']}%")
    st.markdown('**Recent calls**')
    paged_table(f"v2_{entity}_calls", pd.DataFrame(calls), f"{pick['name']}_calls.csv", empty="No calls.")
    if pick.get('notes'):
        st.info(pick['notes'])
    contact = pick.get('publisher_email') if entity == 'publisher' else pick.get('buyer_email')
    st.caption(f"Contact: {contact or 'Not set'}")
    st.markdown('**Related alert history**')
    if related_alerts:
        paged_table(f"v2_{entity}_alerts", pd.DataFrame([{'Alert': f"A{r['id']}", 'Date': fmt(r['created_at']), 'Subject': r['subject'], 'Email status': r['status'],
                                                          'Team status': cases.label(r['case_status'])} for r in related_alerts]), f"{pick['name']}_alerts.csv")
        _ids = [int(r['id']) for r in related_alerts]
        _selected = st.selectbox('Open related alert', _ids, format_func=lambda i: f"A{i} · {next((r['subject'] for r in related_alerts if int(r['id']) == i), '')}", key=f'v2_{entity}_alert_pick')
        st.button('Open alert history', key=f'v2_{entity}_alert_open', on_click=_open_alert_center, args=(_selected,))
    else:
        st.caption('No matching alerts found.')
