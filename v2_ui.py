"""Streamlit UI pages for P2C Alert Center V2. Additive UI; legacy pages remain available."""
import html
import re

import pandas as pd
import streamlit as st

import cases
import db
import v2_features as v2
from timeutil import fmt


def _status_badge(status):
    return {'sent': 'success', 'received': 'success', 'saved': 'success', 'failed': 'danger',
            'pending': 'warning', 'dry_run': 'info', 'skipped': 'secondary'}.get(str(status or '').lower(), 'secondary')


def _safe_html(raw):
    """Conservative email HTML sanitizer before displaying untrusted inbound email content."""
    raw = str(raw or '')
    raw = re.sub(r'(?is)<(script|iframe|object|embed|form|meta|base)[^>]*>.*?</\1\s*>', '', raw)
    raw = re.sub(r'(?is)<(script|iframe|object|embed|form|meta|base)[^>]*/\s*>', '', raw)
    raw = re.sub(r'(?i)\s+on[a-z]+\s*=\s*("[^"]*"|\'[^\']*\'|[^\s>]+)', '', raw)
    raw = re.sub(r'(?is)<(style|link)[^>]*>.*?</\1\s*>', '', raw)
    raw = re.sub(r'(?is)<(img|link)[^>]*>', '', raw)  # avoid remote tracking pixels / stylesheets
    raw = re.sub(r'(?i)\s+style\s*=\s*("[^"]*"|\'[^\']*\')', '', raw)
    raw = re.sub(r'(?i)(href|src)\s*=\s*(["\'])\s*javascript:[^"\']*\2', r'\1="#"', raw)
    return raw[:200000]


def _email_card(a, expanded_key):
    status = a.get('status') or 'unknown'
    label = 'Follow-up' if 'Follow-up' in str(a.get('alert_type')) or ':r' in str(a.get('dedupe_key') or '') else 'Alert email'
    with st.container(border=True):
        left, right = st.columns([5, 1])
        left.markdown(f"**{label} · #{a['id']}**")
        icon = {'sent':'🟢','received':'🟢','saved':'🟢','failed':'🔴','pending':'🟡','dry_run':'🧪','skipped':'⚪'}.get(str(status).lower(),'⚪')
        right.markdown(f"{icon} **{status.replace('_',' ').title()}**")
        st.caption(f"{fmt(a.get('sent_at') or a.get('created_at'))} · To: {a.get('to_emails') or '—'}")
        st.markdown(f"**{a.get('subject') or '(no subject)'}**")
        key = f"v2_mail_{expanded_key}_{a['id']}"
        if st.button("Collapse email" if st.session_state.get(key) else "View full email", key=f"btn_{key}", use_container_width=True):
            st.session_state[key] = not st.session_state.get(key, False)
        if st.session_state.get(key):
            st.caption(f"CC: {a.get('cc_emails') or '—'}")
            if a.get('body_html'):
                import streamlit.components.v1 as components
                components.html(_safe_html(a['body_html']), height=420, scrolling=True)
            else:
                st.code(a.get('body') or '', language=None)
            if a.get('last_error'):
                st.error(f"Delivery error: {a['last_error']}")


def _inbound_card(mail, key):
    with st.container(border=True):
        st.markdown("**Incoming email reply**  ·  :green[Received]" )
        st.caption(f"{fmt(mail.get('received_at'))} · From: {mail.get('from_email') or 'Unknown'}")
        st.markdown(f"**{mail.get('subject') or '(no subject)'}**")
        with st.expander("View complete original reply", expanded=False):
            st.caption(f"To: {mail.get('to_emails') or '—'} · CC: {mail.get('cc_emails') or '—'}")
            if mail.get('body_html'):
                import streamlit.components.v1 as components
                components.html(_safe_html(mail['body_html']), height=360, scrolling=True)
            else:
                st.text(mail.get('body_text') or '(No readable message body)')
            if mail.get('in_reply_to'):
                st.caption(f"In-Reply-To: {mail['in_reply_to']}")


def page_alert_center():
    st.title("Alert Center")
    st.caption("Search any alert and inspect its complete email conversation without leaving the alert list.")
    q1, q2 = st.columns([5, 1])
    with q1:
        query = st.text_input("Universal search", value=st.session_state.get('v2_alert_query',''),
                              placeholder="Alert ID, DID, publisher, buyer, email or subject…", key="v2_alert_query")
    with q2:
        st.write("")
        st.write("")
        st.button("Search", key="v2_search_button", use_container_width=True)
    results = v2.search_alerts(query, 100) if query.strip() else db.q("""SELECT a.id,a.thread_id,a.alert_type,a.subject,a.status,a.case_status,a.created_at,a.sent_at,
                            a.to_emails,a.cc_emails,a.last_error,d.number AS did,COALESCE(p.name,d.affiliate_name,'') AS publisher
                     FROM alerts a LEFT JOIN dids d ON d.id=CASE WHEN split_part(COALESCE(a.snooze_key,''),':',1)='did' AND split_part(COALESCE(a.snooze_key,''),':',2) ~ '^[0-9]+$' THEN split_part(a.snooze_key,':',2)::INTEGER ELSE NULL END
                     LEFT JOIN publishers p ON p.id=d.publisher_id ORDER BY a.created_at DESC LIMIT 100""")
    if not results:
        st.info("No matching alerts. Try an alert ID, phone number, publisher name or subject keyword.")
        return
    ids = [int(r['id']) for r in results]
    selected = int(st.session_state.get('v2_selected_alert_id') or ids[0])
    if selected not in ids:
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
                c2.markdown(f":{_status_badge(r.get('status'))}[{str(r.get('status') or '').title()}]")
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
        def show_timeline(mode='all'):
            for ev in events:
                kind = ev.get('event') or ''
                if mode == 'emails' and kind != 'Email':
                    continue
                if mode == 'replies' and kind != 'Reply':
                    continue
                if mode == 'notes' and kind not in ('Note','Answer','Cancelled'):
                    continue
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
        with tab_all:
            show_timeline('all')
        with tab_emails:
            for mail in mails:
                if mail.get('alert_type') != 'alert_response':
                    _email_card(mail, f"{root}_emails")
        with tab_replies:
            if not any(e.get('event') == 'Reply' for e in events):
                st.info("No archived email replies yet. New replies will be stored with their complete original content after the V2 upgrade.")
            show_timeline('replies')
        with tab_notes:
            show_timeline('notes')
        with st.expander("Add internal note / update status"):
            with st.form(f"v2_note_{selected}"):
                who = st.text_input("Your name", key=f"v2_note_who_{selected}")
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


def page_email_center():
    st.title("Email Center")
    st.caption("Outgoing alert emails and archived incoming replies in one searchable mailbox view.")
    rows = v2.email_center_rows(500)
    if not rows:
        st.info("No email records yet.")
        return
    q = st.text_input("Search emails", placeholder="Subject, address, alert ID…", key="v2_email_search").lower().strip()
    if q:
        rows = [r for r in rows if q in ' '.join(str(r.get(k) or '') for k in ('subject','sender_to','cc_emails','id','alert_id')).lower()]
    df = pd.DataFrame([{'Date': fmt(r.get('happened_at')), 'Direction': r.get('direction'), 'Alert': f"A{r.get('alert_id') or r.get('id')}",
                        'Subject': r.get('subject'), 'Address': r.get('sender_to'), 'Status': r.get('status'), 'Email record': r.get('id')} for r in rows])
    st.dataframe(df, use_container_width=True, hide_index=True)
    st.caption("Outgoing full content is preserved in alerts.body/body_html. Incoming full content is preserved in email_archive.")
    openable = [r for r in rows if r.get('alert_id') or r.get('direction') == 'outbound']
    if openable:
        choice = st.selectbox("Open an email's alert conversation", range(len(openable)),
                              format_func=lambda i: f"A{openable[i].get('alert_id') or openable[i].get('id')} · {openable[i].get('subject') or '(no subject)'}",
                              key="v2_email_open_pick")
        if st.button("Open conversation", key="v2_email_open_btn"):
            st.session_state['v2_selected_alert_id'] = int(openable[choice].get('alert_id') or openable[choice].get('id'))
            st.session_state['page_v2'] = '🔎 Alert Center'
            st.rerun()


def page_audit_log():
    st.title("Audit Log")
    st.caption("A chronological record of V2-tracked actions. Existing historical actions are not fabricated or backfilled.")
    rows = v2.audit_rows(1000)
    if not rows:
        st.info("No V2 audit events have been recorded yet. New notes, replies and status updates will appear here as tracking is added.")
        return
    q = st.text_input("Filter audit events", placeholder="Alert ID, actor, event or keyword…", key="v2_audit_search").lower().strip()
    if q:
        rows = [r for r in rows if q in ' '.join(str(r.get(k) or '') for k in ('alert_id','actor','event_type','summary','details','entity_id')).lower()]
    df = pd.DataFrame([{'When': fmt(r['created_at']), 'Event': r['event_type'], 'Alert': f"A{r['alert_id']}" if r['alert_id'] else '',
                        'Entity': f"{r['entity_type']}:{r['entity_id'] or ''}", 'Actor': r['actor'] or 'System', 'Summary': r['summary'], 'Details': r['details'] or ''} for r in rows])
    st.dataframe(df, use_container_width=True, hide_index=True)


def page_analytics():
    st.title("Operations Analytics")
    st.caption("Operational health and DID recovery metrics. Counts are calculated from the existing alert and call records.")
    total = db.q1("SELECT COUNT(*) AS n FROM alerts WHERE status <> 'skipped'")['n']
    sent = db.q1("SELECT COUNT(*) AS n FROM alerts WHERE status='sent'")['n']
    failed = db.q1("SELECT COUNT(*) AS n FROM alerts WHERE status='failed'")['n']
    active_dids = db.q1("SELECT COUNT(*) AS n FROM dids WHERE status='active'")['n']
    retired_dids = db.q1("SELECT COUNT(*) AS n FROM dids WHERE status='retired'")['n']
    c = st.columns(5)
    for col, label, value in zip(c, ['Alerts','Sent emails','Failed emails','Active DIDs','Retired DIDs'], [total,sent,failed,active_dids,retired_dids]):
        col.metric(label, f"{int(value or 0):,}")
    st.subheader("DID alert recovery")
    st.caption("A DID alert is counted as recovered when a later call exists for the DID after that alert was created. This is a simple operational indicator, not a causal attribution.")
    rows = db.q("""SELECT a.id, a.created_at, d.number, COALESCE(p.name,d.affiliate_name,'') AS publisher,
                         CASE WHEN EXISTS(SELECT 1 FROM calls c WHERE c.did_id=d.id AND c.call_at>a.created_at) THEN TRUE ELSE FALSE END AS recovered
                  FROM alerts a JOIN dids d ON d.id=CASE WHEN split_part(COALESCE(a.snooze_key,''),':',1)='did' AND split_part(COALESCE(a.snooze_key,''),':',2) ~ '^[0-9]+$' THEN split_part(a.snooze_key,':',2)::INTEGER ELSE NULL END
                  LEFT JOIN publishers p ON p.id=d.publisher_id
                  WHERE a.alert_type IN ('did_inactive','did_new_no_calls') AND a.status <> 'skipped'
                  ORDER BY a.created_at DESC LIMIT 2000""")
    if rows:
        recovered = sum(1 for r in rows if r['recovered'])
        rate = recovered / len(rows) * 100
        st.metric("Observed DID recovery rate", f"{rate:.1f}%", help=f"{recovered} of {len(rows)} DID alert records have a later call. Repeated alerts may represent the same DID.")
        st.dataframe(pd.DataFrame([{'Alert':f"A{r['id']}",'DID':r['number'],'Publisher':r['publisher'],'Alert date':fmt(r['created_at']),'Later call detected': 'Yes' if r['recovered'] else 'No'} for r in rows]), use_container_width=True, hide_index=True)
    else:
        st.info("No DID alert records available for this report.")


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
    c1,c2,c3 = st.columns(3)
    if entity == 'publisher':
        c1.metric('Active DIDs', db.q1("SELECT COUNT(*) AS n FROM dids WHERE publisher_id=:i AND status='active'", i=pick['id'])['n'])
        c2.metric('Calls', db.q1("SELECT COUNT(*) AS n FROM calls WHERE publisher_id=:i", i=pick['id'])['n'])
        c3.metric('Alerts', db.q1("SELECT COUNT(*) AS n FROM alerts WHERE body ILIKE :q OR subject ILIKE :q", q=f"%{pick['name']}%")['n'])
        detail = db.q("SELECT number,label,status,last_call_at,monthly_cost FROM dids WHERE publisher_id=:i ORDER BY status,number", i=pick['id'])
        st.markdown('**Assigned DIDs**')
        if detail: st.dataframe(pd.DataFrame(detail), use_container_width=True, hide_index=True)
        calls = db.q("SELECT call_at,campaign_name,connected,converted,paid,duration_sec,payout,profit FROM calls WHERE publisher_id=:i ORDER BY call_at DESC LIMIT 100", i=pick['id'])
    else:
        c1.metric('Calls', db.q1("SELECT COUNT(*) AS n FROM calls WHERE buyer_id=:i", i=pick['id'])['n'])
        c2.metric('Connected', db.q1("SELECT COUNT(*) AS n FROM calls WHERE buyer_id=:i AND connected=TRUE", i=pick['id'])['n'])
        c3.metric('Targets', db.q1("SELECT COUNT(*) AS n FROM targets WHERE buyer_id=:i", i=pick['id'])['n'])
        calls = db.q("SELECT call_at,campaign_name,connected,converted,paid,duration_sec,revenue,profit FROM calls WHERE buyer_id=:i ORDER BY call_at DESC LIMIT 100", i=pick['id'])
    st.markdown('**Recent calls**')
    if calls: st.dataframe(pd.DataFrame(calls), use_container_width=True, hide_index=True)
    if pick.get('notes'): st.info(pick['notes'])
    contact = pick.get('publisher_email') if entity == 'publisher' else pick.get('buyer_email')
    st.caption(f"Contact: {contact or 'Not set'}")
    related_alerts = db.q("SELECT id,created_at,subject,status,case_status FROM alerts WHERE subject ILIKE :q OR body ILIKE :q ORDER BY id DESC LIMIT 100", q=f"%{pick['name']}%")
    st.markdown('**Related alert history**')
    if related_alerts:
        st.dataframe(pd.DataFrame([{'Alert':f"A{r['id']}",'Date':fmt(r['created_at']),'Subject':r['subject'],'Email status':r['status'],'Team status':cases.label(r['case_status'])} for r in related_alerts]), use_container_width=True, hide_index=True)
        _ids = [int(r['id']) for r in related_alerts]
        _selected = st.selectbox('Open related alert', _ids, format_func=lambda i: f"A{i} · {next((r['subject'] for r in related_alerts if int(r['id'])==i), '')}", key=f'v2_{entity}_alert_pick')
        if st.button('Open alert history', key=f'v2_{entity}_alert_open'):
            st.session_state['v2_selected_alert_id'] = _selected
            st.session_state['page_v2'] = '🔎 Alert Center'
            st.rerun()
    else:
        st.caption('No matching alerts found in stored subject/body text.')
