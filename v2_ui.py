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



# ---------------------------------------------------------------- Filters, saved views
def _apply_filters(prefix, f):
    import workq
    f = {**workq.DEFAULT_FILTERS, **(f or {})}
    st.session_state[f"{prefix}_case"] = list(f["case"])
    st.session_state[f"{prefix}_email"] = f["email"]
    st.session_state[f"{prefix}_asg"] = f["asg"]
    st.session_state[f"{prefix}_ovd"] = bool(f["ovd"])
    st.session_state[f"{prefix}_open"] = bool(f["open_only"])
    st.session_state[f"{prefix}_days"] = int(f["days"])


def _apply_view(prefix):
    import json
    import workq
    vid = st.session_state.get(f"{prefix}_viewpick")
    for r in workq.views_list(_me()):
        if r["id"] == vid:
            try:
                _apply_filters(prefix, json.loads(r["filters"]))
            except Exception:
                pass


def filter_bar(prefix):
    """Quick presets + filters + saved views. Returns the filters dict (see workq.filters_sql)."""
    import json
    import workq
    me = _me()
    st.session_state.setdefault(f"{prefix}_case", [])
    st.session_state.setdefault(f"{prefix}_email", "")
    st.session_state.setdefault(f"{prefix}_asg", "Anyone")
    st.session_state.setdefault(f"{prefix}_ovd", False)
    st.session_state.setdefault(f"{prefix}_open", False)
    st.session_state.setdefault(f"{prefix}_days", 0)
    cols = st.columns(len(workq.PRESETS) + 1)
    for col, (name, preset) in zip(cols, workq.PRESETS.items()):
        col.button(name, key=f"{prefix}_pre_{name}", use_container_width=True, on_click=_apply_filters, args=(prefix, preset))
    cols[-1].button("Clear", key=f"{prefix}_pre_clear", use_container_width=True, on_click=_apply_filters, args=(prefix, workq.DEFAULT_FILTERS))
    people = ["Anyone", "Me", "Unassigned"] + workq.people()
    cur = st.session_state.get(f"{prefix}_asg")
    if cur and cur not in people:
        people.append(cur)
    with st.expander("Filters and saved views", expanded=False):
        c1, c2, c3 = st.columns(3)
        c1.multiselect("Team status", cases.STATUSES, format_func=cases.label, key=f"{prefix}_case")
        c2.selectbox("E-mail status", ["", "pending", "sent", "failed", "dry_run"], format_func=lambda s: s or "Any", key=f"{prefix}_email")
        c3.selectbox("Assigned to", people, key=f"{prefix}_asg")
        d1, d2, d3 = st.columns(3)
        d1.checkbox("Overdue only", key=f"{prefix}_ovd")
        d2.checkbox("Hide closed (done / no action)", key=f"{prefix}_open")
        d3.selectbox("Created", [0, 1, 7, 30, 90], format_func=lambda n: "Any time" if not n else f"Last {n} days", key=f"{prefix}_days")
        views = workq.views_list(me)
        if views:
            v1, v2c, v3 = st.columns([3, 1, 1])
            v1.selectbox("Saved views", [r["id"] for r in views], format_func=lambda i: next((r["name"] + (" (shared)" if not r["owner"] else "") for r in views if r["id"] == i), ""),
                         key=f"{prefix}_viewpick")
            v2c.write(""); v2c.write("")
            v2c.button("Apply", key=f"{prefix}_viewapply", on_click=_apply_view, args=(prefix,), use_container_width=True)
            v3.write(""); v3.write("")
            if v3.button("Delete", key=f"{prefix}_viewdel", use_container_width=True):
                workq.view_delete(st.session_state.get(f"{prefix}_viewpick"), me)
                st.rerun()
    flt = {"case": st.session_state[f"{prefix}_case"], "email": st.session_state[f"{prefix}_email"], "asg": st.session_state[f"{prefix}_asg"],
           "ovd": st.session_state[f"{prefix}_ovd"], "open_only": st.session_state[f"{prefix}_open"], "days": st.session_state[f"{prefix}_days"]}
    with st.expander("Save this filter as a view"):
        n1, n2, n3 = st.columns([3, 1, 1])
        vname = n1.text_input("View name", key=f"{prefix}_vname", label_visibility="collapsed", placeholder="Name, e.g. 'Rehab overdue'")
        shared = n2.checkbox("Share", key=f"{prefix}_vshare", help="Everyone can use it")
        n3.write("")
        if n3.button("Save view", key=f"{prefix}_vsave", use_container_width=True):
            if workq.view_save(me, vname, flt, shared):
                st.success("Saved. Pick it under 'Filters and saved views'.")
            else:
                st.warning("Give the view a name first.")
    return flt


def _assigned_chip(r):
    who = r.get("assigned_to")
    if not who:
        return ""
    due = db.to_dt(r.get("due_at")) if r.get("due_at") else None
    late = bool(due and due < db.now_utc())
    return f"{who}" + (f" · due {fmt(due, False)}" if due else "") + (" ⚠️" if late else "")


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
            'sent_at': a.get('sent_at'), 'did': a.get('did'), 'publisher': a.get('publisher_name'),
            'assigned_to': a.get('assigned_to'), 'due_at': a.get('due_at')}


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
    flt = filter_bar('ac')
    results = v2.search_alerts(query, 100, filters=flt, me=_me())
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
                if r.get('assigned_to'):
                    c1.caption("👤 " + _assigned_chip(r))
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
        st.caption("👤 " + (_assigned_chip(a) or "Not assigned"))
        with st.expander("Assign / due time"):
            _assign_form(a)
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
            import workq
            tpls = workq.replies_list()
            st.selectbox("Reply template (fills the note)", [""] + [t["name"] for t in tpls], format_func=lambda s: s or "No template", key=f"v2_tplpick_{selected}",
                         on_change=_use_reply, args=(selected, a.get('did'), a.get('publisher_name')))
            current = a.get('case_status') or 'open'
            status_values = cases.STATUSES
            st.session_state.setdefault(f"v2_note_status_{selected}", current if current in status_values else status_values[0])
            st.session_state.setdefault(f"v2_note_text_{selected}", "")
            with st.form(f"v2_note_{selected}"):
                who = st.text_input("Your name", value=_me(), key=f"v2_note_who_{selected}")
                status = st.selectbox("Team status", status_values, format_func=cases.label, key=f"v2_note_status_{selected}")
                note = st.text_area("Internal note", key=f"v2_note_text_{selected}")
                save = st.form_submit_button("Save activity", type="primary")
            if save:
                if not note.strip() and status == current:
                    st.warning("Add a note or change the status first.")
                else:
                    cases.add_note(selected, 'team', who, note.strip() or f"Status changed to {cases.label(status)}", status if status != current else None)
                    st.session_state[f"v2_note_text_{selected}"] = ""
                    st.success("Saved.")
                    st.rerun()


def _use_reply(alert_id, did, publisher):
    import workq
    pick = st.session_state.get(f"v2_tplpick_{alert_id}")
    t = next((r for r in workq.replies_list() if r["name"] == pick), None)
    if t:
        st.session_state[f"v2_note_text_{alert_id}"] = workq.reply_render(t["body"], did, publisher, alert_id)
        if t.get("set_status") in cases.STATUSES:
            st.session_state[f"v2_note_status_{alert_id}"] = t["set_status"]


def _assign_form(a):
    import workq
    from datetime import datetime, time as dtime
    from timeutil import tz
    aid = int(a["id"])
    names = [""] + workq.people()
    if a.get("assigned_to") and a["assigned_to"] not in names:
        names.append(a["assigned_to"])
    with st.form(f"v2_assign_{aid}"):
        person = st.selectbox("Assign to", names, index=names.index(a["assigned_to"]) if a.get("assigned_to") in names else 0, format_func=lambda s: s or "Nobody",
                              key=f"v2_asg_p_{aid}")
        has_due = st.checkbox("Set a due date", value=bool(a.get("due_at")), key=f"v2_asg_h_{aid}")
        cur = db.to_dt(a["due_at"]).astimezone(tz()) if a.get("due_at") else (db.now_utc() + timedelta(days=1)).astimezone(tz())
        d1, d2 = st.columns(2)
        day = d1.date_input("Due date", value=cur.date(), key=f"v2_asg_d_{aid}")
        hour = d2.time_input("Due time", value=cur.time().replace(second=0, microsecond=0), key=f"v2_asg_t_{aid}")
        go = st.form_submit_button("Save assignment", type="primary")
    if go:
        due = datetime.combine(day, hour, tzinfo=tz()).astimezone(db.now_utc().tzinfo) if has_due else None
        workq.assign([aid], person, due, _me())
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
        st.divider()
        reply_templates_manager()
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
    want = st.session_state.pop(f"v2_pref_{entity}", None)
    if want:
        hit = next((r for r in records if r["name"] == want), None)
        if hit:
            st.session_state[f"v2_{entity}_profile"] = hit
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


# ---------------------------------------------------------------- Needs Attention (home)
CHIP = {"green": ("#e7f6ee", "#14724a"), "red": ("#fdeaea", "#b42318"), "orange": ("#fff4e0", "#b54708"),
        "blue": ("#e8f0fe", "#1d4ed8"), "gray": ("#eef1f5", "#475467")}


def chip(text, colour="gray"):
    bg, fg = CHIP.get(colour, CHIP["gray"])
    return (f"<span style='background:{bg};color:{fg};padding:3px 10px;border-radius:999px;font-size:12px;"
            f"font-weight:600;white-space:nowrap'>{html.escape(str(text))}</span>")


def _go(page_name, alert_id=None):
    if alert_id:
        st.session_state["v2_selected_alert_id"] = int(alert_id)
    st.session_state["page_v2"] = page_name


def _section(title, count, colour):
    st.markdown(f"<div style='display:flex;align-items:center;gap:10px;margin:2px 0 6px'><span style='font-weight:700;font-size:16px'>{html.escape(title)}</span>"
                f"{chip(count, colour if count else 'green')}</div>", unsafe_allow_html=True)


def page_home():
    import followups
    import mailer
    now = db.now_utc()
    st.markdown("## Needs attention")
    st.caption("Everything that is waiting for you, in one place. Each line has one button.")
    # health strip
    ok = db.q1("SELECT MAX(finished_at) AS t FROM sync_runs WHERE kind='live' AND status='ok'")
    t = db.to_dt(ok["t"]) if ok and ok["t"] else None
    mins = int((now - t).total_seconds() // 60) if t else None
    engine = chip("Engine: never ran", "red") if t is None else (chip(f"Engine OK · {mins} min ago", "green") if mins <= 15 else chip(f"Engine late · {mins} min", "red"))
    ringba = chip("Ringba needs attention", "red") if db.get_setting('numbers_last_error', '') else chip("Ringba OK", "green")
    inbox = chip("Inbox needs attention", "red") if db.get_setting('inbox_last_error', '') else chip("Inbox OK", "green")
    mode = chip("DRY RUN: no real e-mails", "orange") if mailer.is_dry_run() else chip("Live sending", "green")
    st.markdown(f"<div style='display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px'>{engine}{ringba}{inbox}{mode}</div>", unsafe_allow_html=True)

    failed = db.q("SELECT id, subject, to_emails, last_error, created_at FROM alerts WHERE status='failed' AND created_at > :t ORDER BY id DESC LIMIT 10", t=now - timedelta(days=7))
    replies = db.q("""SELECT a.id, a.subject, MAX(n.created_at) AS last_reply, MAX(n.author) AS who FROM alert_notes n JOIN alerts a ON a.id=n.alert_id
                      WHERE n.source='email' AND n.created_at > :t AND a.case_status IN ('open','follow_up','pending')
                      GROUP BY a.id, a.subject ORDER BY MAX(n.created_at) DESC LIMIT 10""", t=now - timedelta(days=3))
    try:
        overdue = [r for r in followups.upcoming(now) if r.get("scheduled") and r.get("next_followup") and r["next_followup"] < now
                   and not (r.get("paused_until") and r["paused_until"] > now)][:10]
    except Exception:
        overdue = []
    silent = db.q("""SELECT id, subject, created_at, case_status FROM alerts WHERE alert_type IN ('did_inactive','did_new_no_calls')
                     AND status='sent' AND case_status IN ('open','follow_up') ORDER BY id DESC LIMIT 10""")
    c = st.columns(4)
    c[0].metric("Failed e-mails", len(failed)); c[1].metric("Replies to read", len(replies))
    c[2].metric("Overdue follow-ups", len(overdue)); c[3].metric("DIDs without answer", len(silent))

    me = _me()
    mine = db.q("""SELECT id, subject, due_at, case_status FROM alerts WHERE assigned_to=:m AND alert_type <> 'alert_response'
                   AND COALESCE(case_status,'open') NOT IN ('done','no_action') ORDER BY due_at IS NULL, due_at LIMIT 10""", m=me) if me else []
    if mine:
        with st.container(border=True):
            _section("Assigned to me", len(mine), "blue")
            for r in mine:
                a, b = st.columns([5, 1])
                late = bool(r.get("due_at") and db.to_dt(r["due_at"]) < now)
                a.markdown(f"**A{r['id']}** · {r['subject'] or ''}")
                a.caption((f"due {fmt(r['due_at'])}" + (" ⚠️ overdue" if late else "")) if r.get("due_at") else "no due date")
                b.button("Open", key=f"home_my_{r['id']}", use_container_width=True, on_click=_go, args=("🔎 Alert Center", int(r['id'])))
    left, right = st.columns(2, gap="large")
    with left:
        with st.container(border=True):
            _section("Failed e-mails", len(failed), "red")
            if not failed:
                st.caption("All e-mails were delivered. 🎉")
            for r in failed:
                a, b = st.columns([4, 1])
                a.markdown(f"**A{r['id']}** · {r['subject'] or ''}")
                a.caption(f"{fmt(r['created_at'])} · {str(r.get('last_error') or '')[:90]}")
                if b.button("Retry", key=f"home_retry_{r['id']}", use_container_width=True):
                    mailer.retry_failed(r['id'])
                    st.rerun()
        with st.container(border=True):
            _section("Overdue follow-ups", len(overdue), "orange")
            if not overdue:
                st.caption("Nothing is late.")
            for r in overdue:
                a, b = st.columns([4, 1])
                a.markdown(f"**{r['code']}** · {r['did']} · {r['publisher']}")
                a.caption(f"was due {fmt(r['next_followup'])}")
                b.button("Open", key=f"home_fu_{r['did_id']}", use_container_width=True, on_click=_go, args=("🔎 Alert Center", int(r['extra']['root_id'])))
            st.button("Manage all follow-ups", key="home_fu_all", on_click=_go, args=("🎚️ Follow-up Control",))
    with right:
        with st.container(border=True):
            _section("New replies to read", len(replies), "blue")
            if not replies:
                st.caption("No unread replies.")
            for r in replies:
                a, b = st.columns([4, 1])
                a.markdown(f"**A{r['id']}** · {r['subject'] or ''}")
                a.caption(f"{fmt(r['last_reply'])} · {r.get('who') or ''}")
                b.button("Open", key=f"home_rp_{r['id']}", use_container_width=True, on_click=_go, args=("🔎 Alert Center", int(r['id'])))
        with st.container(border=True):
            _section("DID alerts without an answer", len(silent), "orange")
            if not silent:
                st.caption("All DID alerts have an answer.")
            for r in silent:
                a, b = st.columns([4, 1])
                a.markdown(f"**A{r['id']}** · {r['subject'] or ''}")
                a.caption(f"{fmt(r['created_at'])} · {cases.label(r['case_status'])}")
                b.button("Open", key=f"home_sl_{r['id']}", use_container_width=True, on_click=_go, args=("🔎 Alert Center", int(r['id'])))


# ---------------------------------------------------------------- Work Queue (bulk actions)
def page_work_queue():
    import workq
    from datetime import datetime
    from timeutil import tz
    st.markdown("## Work Queue")
    st.caption("Tick alerts, then change many at once: status, person, due date, note or a reply template.")
    q = st.text_input("Search", key="wq_q", placeholder="A325 · DID · publisher · e-mail · subject…")
    flt = filter_bar("wq")
    rows = v2.search_alerts(q, 300, filters=flt, me=_me())
    st.caption(f"{len(rows):,} alerts" + (" (showing the newest 200)" if len(rows) > 200 else ""))
    rows = rows[:200]
    if not rows:
        st.info("No alerts match. Change the filters or press Clear.")
        return
    all_sel = st.checkbox("Select all shown", key="wq_all")
    df = pd.DataFrame([{"Select": bool(all_sel), "Alert": f"A{r['id']}", "Subject": r.get("subject"), "DID": r.get("did") or "", "Publisher": r.get("publisher") or "",
                        "Team": cases.label(r.get("case_status")), "E-mail": r.get("status"), "Assigned": r.get("assigned_to") or "",
                        "Due": fmt(r["due_at"], False) if r.get("due_at") else "", "Created": fmt(r.get("created_at"), False), "_id": int(r["id"])} for r in rows])
    sig = f"wq_ed_{int(bool(all_sel))}_{len(df)}_{rows[0]['id']}"
    edited = st.data_editor(df, hide_index=True, use_container_width=True, key=sig, column_config={"Select": st.column_config.CheckboxColumn("Select"), "_id": None},
                            disabled=[c for c in df.columns if c != "Select"], height=420)
    st.download_button("⬇ CSV (all shown rows)", df.drop(columns=["Select", "_id"]).to_csv(index=False).encode("utf-8-sig"), file_name="work_queue.csv", mime="text/csv", key="wq_csv")
    chosen = [int(i) for i in edited.loc[edited["Select"], "_id"].tolist()]
    with st.container(border=True):
        st.markdown(f"**Bulk action** · {len(chosen)} selected")
        action = st.selectbox("Action", ["Set team status", "Assign to a person", "Set due date only", "Add a note", "Use a reply template"], key="wq_action")
        who = _me()
        payload = {}
        if action == "Set team status":
            payload["status"] = st.selectbox("New status", cases.STATUSES, format_func=cases.label, key="wq_status")
            payload["note"] = st.text_input("Note (optional)", key="wq_note1")
        elif action in ("Assign to a person", "Set due date only"):
            if action == "Assign to a person":
                payload["person"] = st.selectbox("Person", [""] + workq.people(), format_func=lambda s: s or "Nobody", key="wq_person")
            has_due = st.checkbox("Set a due date", value=True, key="wq_hasdue")
            d1, d2 = st.columns(2)
            day = d1.date_input("Due date", value=(db.now_utc() + timedelta(days=1)).astimezone(tz()).date(), key="wq_dday")
            hr = d2.time_input("Due time", value=datetime(2000, 1, 1, 9, 0).time(), key="wq_dtime")
            payload["due"] = datetime.combine(day, hr, tzinfo=tz()).astimezone(db.now_utc().tzinfo) if has_due else None
        elif action == "Add a note":
            payload["note"] = st.text_area("Note", key="wq_note2")
        else:
            tpls = workq.replies_list()
            payload["tpl"] = st.selectbox("Template", [t["id"] for t in tpls], format_func=lambda i: next(t["name"] for t in tpls if t["id"] == i), key="wq_tpl")
            st.caption("{did}, {publisher} and {alert} are filled in for each alert.")
        sure = st.checkbox(f"Yes, apply to all {len(chosen)} selected alerts", key="wq_sure") if len(chosen) > 20 else True
        if st.button("Apply to selected", type="primary", key="wq_apply", disabled=not chosen or not sure):
            n = 0
            if action == "Set team status":
                n = workq.bulk_status(chosen, payload["status"], who, payload.get("note"))
            elif action == "Assign to a person":
                n = workq.assign(chosen, payload["person"], payload["due"], who)
            elif action == "Set due date only":
                for aid in chosen:
                    cur = db.q1("SELECT assigned_to FROM alerts WHERE id=:i", i=aid) or {}
                    n += workq.assign([aid], cur.get("assigned_to"), payload["due"], who)
            elif action == "Add a note":
                if not (payload.get("note") or "").strip():
                    st.warning("Write the note first.")
                else:
                    n = workq.bulk_note(chosen, who, payload["note"].strip())
            else:
                t = next(t for t in tpls if t["id"] == payload["tpl"])
                by_id = {int(r["id"]): r for r in rows}
                for aid in chosen:
                    r = by_id.get(aid, {})
                    cases.add_note(aid, "team", who, workq.reply_render(t["body"], r.get("did"), r.get("publisher"), aid), t.get("set_status") if t.get("set_status") in cases.STATUSES else None)
                    n += 1
            if n:
                audit_msg = f"{action}: {n} alerts"
                try:
                    import audit
                    audit.log("bulk_action", "alert", f"{n} alerts", detail=audit_msg, who=who)
                except Exception:
                    pass
                st.success(f"Done: {n} alerts updated.")
                st.rerun()


# ---------------------------------------------------------------- Reply templates manager
def reply_templates_manager():
    import workq
    tpls = workq.replies_list()
    st.markdown("**Reply templates** (used in Alert Center notes and the Work Queue)")
    st.caption("Use {did}, {publisher} and {alert}; they are filled in automatically.")
    ids = [0] + [t["id"] for t in tpls]
    pick = st.selectbox("Template", ids, format_func=lambda i: "➕ New template" if i == 0 else next(t["name"] for t in tpls if t["id"] == i), key="rt_pick")
    cur = next((t for t in tpls if t["id"] == pick), {})
    with st.form(f"rt_form_{pick}"):
        name = st.text_input("Name", value=cur.get("name", ""))
        body = st.text_area("Text", value=cur.get("body", ""), height=110)
        opts = [""] + cases.STATUSES
        status = st.selectbox("Also set team status to", opts, index=opts.index(cur.get("set_status")) if cur.get("set_status") in opts else 0,
                              format_func=lambda s: "Do not change" if not s else cases.label(s))
        c1, c2 = st.columns(2)
        save = c1.form_submit_button("Save template", type="primary")
        delete = c2.form_submit_button("Delete", disabled=not pick)
    if save:
        if workq.reply_save(name, body, status, _me(), pick or None):
            st.success("Saved.")
            st.rerun()
        else:
            st.warning("A name and a text are needed.")
    if delete and pick:
        workq.reply_delete(pick, _me())
        st.rerun()


# ---------------------------------------------------------------- Settings: Telegram + daily summary
def settings_notifications():
    import notify
    st.subheader("Notifications")
    tg = notify.telegram_configured()
    st.markdown("**Telegram alerts** " + (":green[connected]" if tg else ":orange[not connected]"))
    if not tg:
        st.caption("1) In Telegram open @BotFather, send /newbot and copy the token.  2) Send any message to your new bot, then open "
                   "https://api.telegram.org/bot<TOKEN>/getUpdates and copy the number after \"chat\":{\"id\":.  "
                   "3) Add two secrets in Streamlit AND in GitHub (Settings → Secrets → Actions): TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.  "
                   "4) Reload this page. (Never paste the token in code or in a chat.)")
    c = st.columns(5)
    flags = [("tg_on_failed", "Failed e-mails"), ("tg_on_reply", "New replies"), ("tg_on_engine", "Engine / Ringba problems"),
             ("tg_on_due", "Overdue assigned work"), ("tg_on_summary", "Also send the daily summary")]
    new = {}
    for col, (k, lab) in zip(c, flags):
        new[k] = col.checkbox(lab, value=str(db.get_setting(k, "true")).lower() == "true", key=f"set_{k}")
    st.markdown("**Daily summary e-mail**")
    d1, d2, d3 = st.columns([1, 1, 3])
    on = d1.checkbox("Send every day", value=str(db.get_setting("summary_on", "false")).lower() == "true", key="set_summary_on")
    hour = d2.number_input("At hour (0-23, your timezone)", 0, 23, int(float(db.get_setting("summary_hour", "9") or 9)), key="set_summary_hour")
    to = d3.text_input("Send the daily summary to (comma separated; empty = admin e-mails)", db.get_setting("summary_to", ""), key="set_summary_to")
    b1, b2, b3 = st.columns(3)
    if b1.button("💾 Save notification settings", key="set_notif_save"):
        for k, val in new.items():
            db.set_setting(k, "true" if val else "false")
        db.set_setting("summary_on", "true" if on else "false")
        db.set_setting("summary_hour", int(hour))
        db.set_setting("summary_to", to.strip())
        st.success("Saved.")
    if b2.button("Send a Telegram test", key="set_tg_test", disabled=not tg):
        ok, msg = notify.send_telegram("P2C Alert Center: test message ✅")
        (st.success if ok else st.error)(msg if not ok else "Sent. Check Telegram.")
    if b3.button("Send the summary now (test)", key="set_sum_test"):
        try:
            ok, msg = notify.send_summary(db.now_utc())
            (st.success if ok else st.warning)(msg)
        except Exception as e:
            st.error(f"Could not send: {e}")
    with st.expander("Preview of the summary"):
        st.text(notify.summary_text(db.now_utc()))
    st.caption("WhatsApp is not included: it needs a paid Meta business account. Telegram is free and works the same way.")
