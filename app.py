"""Pay-Per-Call Alert Center  -  the screen you work in."""
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

st.set_page_config(page_title="Pay-Per-Call Alert Center", page_icon="📞", layout="wide")

import config

if not config.DB_URL:
    st.error("DATABASE_URL is missing. In Streamlit Cloud open: App → Settings → Secrets, and add it "
             "(see SETUP_GUIDE.md).")
    st.stop()

import actions
import alerts
import db
import dids as didtools
import engine
import ingest
import mailer
import reports
import ringba_client as rc
import rules
import setup_db
import templates
from timeutil import fmt, tz, window_start


# ------------------------------------------------------------------ answer link opened from an email
def action_page(token):
    st.title("📞 Pay-Per-Call Alert Center")
    try:
        row = actions.lookup(token)
    except Exception:
        st.error("The system is not ready yet. Please try again in a minute.")
        return
    if not row:
        st.error("This link is not valid or has expired.")
        return
    if row["response_label"]:
        st.info(f"This alert was already answered: **{row['response_label']}**"
                f"{' by ' + row['responded_by'] if row['responded_by'] else ''}. Nothing more to do.")
        return
    st.subheader(row["alert_subject"])
    st.markdown(f"Your answer: **{row['label']}**")
    with st.form("answer_form"):
        who = st.text_input("Your name", key="ans_who")
        note = st.text_area("Note for the team (optional)", key="ans_note")
        go = st.form_submit_button("✅ Confirm my answer")
    if go:
        res = actions.respond(token, who, note)
        (st.success if res["ok"] else st.error)(res["msg"])


_token = st.query_params.get("a") if hasattr(st, "query_params") else None
if _token:
    action_page(_token)
    st.stop()

# ------------------------------------------------------------------ password (optional but recommended)
_pw = config.get_secret("APP_PASSWORD")
if _pw and not st.session_state.get("authed"):
    st.title("📞 Pay-Per-Call Alert Center")
    entered = st.text_input("Password", type="password")
    if entered:
        if entered == str(_pw):
            st.session_state["authed"] = True
            st.rerun()
        else:
            st.error("Wrong password")
    st.stop()


# ------------------------------------------------------------------ first-run / upgrade database setup
def schema_ready():
    try:
        db.q1("SELECT call_id FROM calls LIMIT 1")
        db.q1("SELECT template_id FROM alert_rules LIMIT 1")
        db.q1("SELECT body_html, response_label, snooze_key FROM alerts LIMIT 1")
        db.q1("SELECT publisher_id FROM publisher_managers LIMIT 1")
        db.q1("SELECT subject FROM email_templates LIMIT 1")
        db.q1("SELECT token FROM alert_actions LIMIT 1")
        db.q1("SELECT entity_key FROM snoozes LIMIT 1")
        return True
    except Exception:
        return False


if not st.session_state.get("schema_ok"):
    if schema_ready():
        st.session_state["schema_ok"] = True
    else:
        st.title("📞 Pay-Per-Call Alert Center")
        st.info("The database needs to be set up or upgraded. Press the button once. Your existing data is kept: "
                "old tables are NOT deleted (they are renamed old_*), and your publishers, buyers, emails and caps "
                "are copied across.")
        if st.button("🛠️ Set up / upgrade the database now", type="primary"):
            logs = []
            try:
                setup_db.run(log=logs.append)
                st.code("\n".join(logs))
                st.session_state["schema_ok"] = True
                st.success("Done. Reloading ...")
                st.rerun()
            except Exception as e:
                st.code("\n".join(logs))
                st.error(f"Setup failed: {e}")
        st.stop()


# ------------------------------------------------------------------ helpers
EMAIL_TOKEN = re.compile(r"[,;\s]+")


def validate_emails(s, field="email"):
    for tok in EMAIL_TOKEN.split(s or ""):
        if tok and not alerts.EMAIL_RE.match(tok):
            raise ValueError(f"'{tok}' is not a valid {field}")
    return s


def clean(v):
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(v, "item"):
        v = v.item()
    if isinstance(v, pd.Timestamp):
        v = v.date()
    elif isinstance(v, datetime):
        v = v.date()
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return v


def same(a, b):
    if a is None and b is None:
        return True
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return a == b


def save_table(table, orig, edited, display_cols, pk="id", transform=None, required=(),
               allow_delete=False, insert_extra=None):
    """Compare what you edited with what was there; INSERT new rows, UPDATE changed rows, DELETE removed rows."""
    done = {"added": 0, "changed": 0, "deleted": 0, "errors": []}
    orig_rows = {}
    for _, r in orig.iterrows():
        orig_rows[clean(r[pk])] = {c: clean(r[c]) for c in display_cols}
    seen = set()
    for _, row in edited.iterrows():
        disp = {c: clean(row[c]) for c in display_cols}
        pkv = clean(row[pk]) if pk in edited.columns else None
        label = disp.get("name") or disp.get("number") or disp.get("label") or disp.get("email") or str(pkv or "new row")
        try:
            if pkv is None:
                if all(v is None or v is False for v in disp.values()):
                    continue                                   # empty new row
                for rq in required:
                    if disp.get(rq) is None:
                        raise ValueError(f"'{rq}' is required")
                vals = transform(disp) if transform else dict(disp)
                vals.update(insert_extra or {})
                cols = list(vals)
                db.x(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join(':' + c for c in cols)})", **vals)
                done["added"] += 1
            else:
                seen.add(pkv)
                before = orig_rows.get(pkv)
                if before is not None and all(same(disp[c], before[c]) for c in display_cols):
                    continue
                for rq in required:
                    if disp.get(rq) is None:
                        raise ValueError(f"'{rq}' is required")
                vals = transform(disp) if transform else dict(disp)
                sets = ",".join(f"{c}=:{c}" for c in vals)
                db.x(f"UPDATE {table} SET {sets} WHERE {pk}=:__pk", __pk=pkv, **vals)
                done["changed"] += 1
        except Exception as e:
            msg = str(e).split("\n")[0][:200]
            if "unique" in msg.lower() or "duplicate" in msg.lower():
                msg = "that value already exists"
            done["errors"].append(f"{label}: {msg}")
    if allow_delete:
        for pkv in set(orig_rows) - seen:
            try:
                db.x(f"DELETE FROM {table} WHERE {pk}=:p", p=pkv)
                done["deleted"] += 1
            except Exception as e:
                done["errors"].append(f"{orig_rows[pkv].get('name') or pkv}: cannot delete ({str(e)[:80]}) - set its status instead")
    return done


def table_editor(name, df, column_config, display_cols, table, transform=None, required=(), allow_delete=False,
                 dynamic=True, disabled=(), pk="id", insert_extra=None):
    """An editable table inside a form: nothing is sent or reloaded until you press Save."""
    msg = st.session_state.pop(f"msg_{name}", None)
    if msg:
        parts = [f"{msg['added']} added", f"{msg['changed']} changed"] + ([f"{msg['deleted']} deleted"] if msg["deleted"] else [])
        (st.success if not msg["errors"] else st.warning)("Saved: " + ", ".join(parts))
        for e in msg["errors"]:
            st.error(e)
    ver = st.session_state.get(f"ver_{name}", 0)
    with st.form(f"form_{name}"):
        edited = st.data_editor(df, key=f"ed_{name}_{ver}", column_config=column_config, hide_index=True,
                                num_rows="dynamic" if dynamic else "fixed", disabled=list(disabled))
        submitted = st.form_submit_button("💾 Save changes")
    if submitted:
        st.session_state[f"msg_{name}"] = save_table(table, df, edited, display_cols, pk=pk, transform=transform,
                                                     required=required, allow_delete=allow_delete,
                                                     insert_extra=insert_extra)
        st.session_state[f"ver_{name}"] = ver + 1
        st.rerun()


def frame(rows, columns):
    return pd.DataFrame(rows, columns=columns)


def ago(dt):
    if not dt:
        return "never"
    s = (db.now_utc() - db.to_dt(dt)).total_seconds()
    return f"{int(s // 60)} min ago" if s < 7200 else (f"{int(s // 3600)} h ago" if s < 172800 else f"{int(s // 86400)} days ago")


def progress_bar_cb(label="Loading"):
    bar = st.progress(0.0, text=label)

    def cb(i, n, day, total):
        bar.progress(i / n, text=f"{label}: {day}  ({i}/{n} days, {total['fetched']} calls so far)")
    return cb


def flash(key):
    m = st.session_state.pop(key, None)
    if m:
        (st.success if m[0] == "ok" else st.error)(m[1])


def set_flash(key, kind, text):
    st.session_state[key] = (kind, text)


# ================================================================== DASHBOARD
def page_dashboard():
    now = db.now_utc()
    ok_run = db.q1("SELECT MAX(finished_at) AS t FROM sync_runs WHERE kind='live' AND status='ok'")
    ok_t = db.to_dt(ok_run["t"]) if ok_run and ok_run["t"] else None
    last = db.q1("SELECT status, error FROM sync_runs WHERE kind='live' ORDER BY id DESC LIMIT 1")
    first, _, n = reports.coverage()
    c1, c2, c3 = st.columns(3)
    c1.metric("Last good sync", ago(ok_t))
    c2.metric("Calls stored", f"{n:,}")
    c3.metric("History starts", fmt(first, False) if first else "-")
    if ok_t is None:
        st.info("The live engine has not run yet. After setup, GitHub Actions runs it every 5 minutes "
                "(or press 'Run engine now' on the Sync & Import page).")
    elif (now - ok_t) > timedelta(minutes=30):
        st.error(f"⚠️ No successful sync for {int((now - ok_t).total_seconds() // 60)} minutes. Check GitHub Actions.")
    if last and last["status"] == "error":
        st.error(f"Last sync error: {last['error']}")
    if db.get_setting("numbers_last_error", ""):
        st.warning(f"DID list sync problem: {db.get_setting('numbers_last_error')}")

    st.subheader("Calls")
    a, b = reports.preset_range("Last 7 days")
    s, e = reports.utc_bounds(a, b)
    df = reports.load_calls(s, e)
    today = reports.today_local()
    for col, label, sub in zip(st.columns(3), ["Today", "Yesterday", "Last 7 days"],
                               [df[df["day"] == today] if not df.empty else df,
                                df[df["day"] == today - timedelta(days=1)] if not df.empty else df, df]):
        sm = reports.summary(sub)
        col.markdown(f"**{label}**")
        col.metric("Calls", sm["Calls"])
        col.caption(f"Connected {sm['Connected']} · Converted {sm['Converted']} · Paid {sm['Paid']}")

    st.subheader("Caps right now")
    caps = db.q("SELECT * FROM caps WHERE active = TRUE")
    if not caps:
        st.caption("No caps yet. Add them on the Caps page.")
    else:
        out = []
        for c in caps:
            u = rules.cap_usage(c, now)
            out.append({"Type": c["entity_type"], "Name": u["name"], "Period": c["period"], "Counts": c["metric"],
                        "Used": u["usage"], "Cap": c["cap_value"], "Percent": round(u["pct"], 1)})
        st.dataframe(pd.DataFrame(out).sort_values("Percent", ascending=False), hide_index=True)

    st.subheader("Latest alerts")
    rows = db.q("SELECT created_at, alert_type, subject, status, response_label FROM alerts "
                "WHERE status<>'skipped' ORDER BY id DESC LIMIT 15")
    if rows:
        d = pd.DataFrame(rows)
        d["created_at"] = d["created_at"].map(fmt)
        st.dataframe(d, hide_index=True)
    else:
        st.caption("No alerts yet.")


# ================================================================== REPORTS
@st.cache_data(ttl=120, show_spinner=False)
def cached_calls(s, e, pid, bid):
    return reports.load_calls(s, e, pid, bid)


def page_reports():
    st.caption("Reads from your stored calls. Times are in your timezone (Settings). Pick options, then press Show report.")
    pubs = db.q("SELECT id, name FROM publishers ORDER BY name")
    buys = db.q("SELECT id, name FROM buyers ORDER BY name")
    t = reports.today_local()
    with st.form("report_form"):
        c1, c2, c3 = st.columns(3)
        preset = c1.selectbox("Date range", reports.PRESETS, key="rep_preset")
        custom = c2.date_input("Custom dates (only used for 'Custom range')", value=(t - timedelta(days=6), t), key="rep_custom")
        pf = c3.selectbox("Publisher", ["All"] + [p["name"] for p in pubs], key="rep_pub")
        bf = c3.selectbox("Buyer", ["All"] + [p["name"] for p in buys], key="rep_buy")
        go = st.form_submit_button("▶️ Show report")
    if go or "rep_params" not in st.session_state:
        if go:
            st.cache_data.clear()
        st.session_state["rep_params"] = (preset if go else "Today", custom, pf if go else "All", bf if go else "All")
    preset, custom, pf, bf = st.session_state["rep_params"]
    if preset == "Custom range":
        if not (isinstance(custom, (list, tuple)) and len(custom) == 2):
            st.info("Pick both a start and an end date, then press Show report.")
            return
        a, b = custom
    else:
        a, b = reports.preset_range(preset)
        st.markdown(f"**{preset}: {a:%b %d, %Y} → {b:%b %d, %Y}**  ·  {reports.PRESET_HELP[preset]}")
    pid = next((p["id"] for p in pubs if p["name"] == pf), None)
    bid = next((p["id"] for p in buys if p["name"] == bf), None)
    s, e = reports.utc_bounds(a, b)

    first, _, n = reports.coverage()
    if n == 0 or s < first - timedelta(days=1):
        have = f"Your stored history starts {fmt(first, False)}." if first else "No calls are stored yet."
        st.warning(f"{have} Part of this range is not in your database.")
        if st.button("⬇️ Load the missing part from Ringba", key="rep_load"):
            res = ingest.backfill_range(s, min(e, first) if first else e, progress_bar_cb("Loading"))
            st.cache_data.clear()
            st.success(f"Loaded {res['inserted']} new calls.")
            if res["failures"]:
                st.error("Some days failed: " + "; ".join(res["failures"][:5]))

    df = cached_calls(s, e, pid, bid)
    sm = reports.summary(df)
    for col, k in zip(st.columns(6), ["Calls", "Connected", "Converted", "Paid", "Blocked", "Duplicates"]):
        col.metric(k, f"{sm[k]:,}")
    for col, k, fp in zip(st.columns(5), ["Connect %", "Revenue", "Payout", "Profit", "Avg talk (sec)"],
                          ["{:.1f}%", "${:,.2f}", "${:,.2f}", "${:,.2f}", "{}"]):
        col.metric(k, fp.format(sm[k]))
    if df.empty:
        st.info("No calls in this range.")
        return
    t_day, t_pub, t_buy, t_did, t_camp, t_raw = st.tabs(["By day", "By publisher", "By buyer", "By DID", "By campaign", "All calls"])
    for tab, by, fn in [(t_day, "day", "by_day"), (t_pub, "publisher", "by_publisher"), (t_buy, "buyer", "by_buyer"),
                        (t_did, "did", "by_did"), (t_camp, "campaign", "by_campaign")]:
        with tab:
            bd = reports.breakdown(df, by)
            st.dataframe(bd, hide_index=True)
            st.download_button("⬇️ Download CSV", bd.to_csv(index=False), f"{fn}_{a}_{b}.csv", "text/csv", key=f"dl_{fn}")
    with t_raw:
        raw = df.drop(columns=["day"]).copy()
        raw["call_at"] = raw["call_at"].dt.strftime("%Y-%m-%d %H:%M:%S")
        st.caption(f"Showing the newest 2,000 of {len(raw):,} calls. The download has all of them.")
        st.dataframe(raw.tail(2000).iloc[::-1], hide_index=True)
        st.download_button("⬇️ Download all calls CSV", raw.to_csv(index=False), f"calls_{a}_{b}.csv", "text/csv", key="dl_raw")


# ================================================================== PUBLISHERS
def page_publishers():
    st.caption("Change anything in the table, then press **Save changes** once. Add a publisher by hand (name only) and it "
               "links automatically when its first call arrives. Create affiliate managers on the 'Emails' page.")
    mgrs = db.q("SELECT id, name FROM affiliate_managers WHERE active = TRUE ORDER BY name")
    mname = {m["id"]: m["name"] for m in mgrs}
    links = {}
    for r in db.q("SELECT publisher_id, manager_id FROM publisher_managers"):
        if r["manager_id"] in mname:
            links.setdefault(r["publisher_id"], []).append(mname[r["manager_id"]])
    since = db.now_utc() - timedelta(days=30)
    rows = db.q("""SELECT p.id, p.name, p.ringba_publisher_id, p.status, p.publisher_email, p.cc_publisher, p.extra_cc, p.notes,
                          (SELECT MAX(call_at) FROM calls WHERE publisher_id = p.id) AS last_call,
                          (SELECT COUNT(*) FROM calls WHERE publisher_id = p.id AND call_at >= :s) AS calls_30d
                   FROM publishers p ORDER BY p.name""", s=since)
    for r in rows:
        r["managers"] = ", ".join(sorted(links.get(r["id"], [])))
    cols = ["id", "name", "ringba_publisher_id", "status", "managers", "publisher_email", "cc_publisher", "extra_cc",
            "notes", "last_call", "calls_30d"]
    df = frame(rows, cols)
    df["last_call"] = df["last_call"].map(lambda v: "never" if pd.isna(v) else fmt(v))
    df["cc_publisher"] = df["cc_publisher"].fillna(False).astype(bool)

    def tf(v):
        validate_emails(v["publisher_email"], "publisher email")
        validate_emails(v["extra_cc"], "CC email")
        return {"name": v["name"], "ringba_publisher_id": v["ringba_publisher_id"], "status": v["status"] or "active",
                "publisher_email": v["publisher_email"], "cc_publisher": bool(v["cc_publisher"]),
                "extra_cc": v["extra_cc"], "notes": v["notes"]}

    cfg = {"id": None,
           "name": st.column_config.TextColumn("Publisher name", required=True),
           "ringba_publisher_id": st.column_config.TextColumn("Ringba ID", help="Leave empty for hand-added publishers."),
           "status": st.column_config.SelectboxColumn("Status", options=["active", "paused", "terminated"]),
           "managers": st.column_config.TextColumn("Affiliate managers (CC)", help="Change them with the form below the table."),
           "publisher_email": st.column_config.TextColumn("Publisher's own email"),
           "cc_publisher": st.column_config.CheckboxColumn("CC the publisher?", help="Also needs the alert's 'CC publisher/buyer' switch on."),
           "extra_cc": st.column_config.TextColumn("Extra CC emails", help="Comma-separated. Always copied on this publisher's alerts."),
           "notes": st.column_config.TextColumn("Notes"),
           "last_call": st.column_config.TextColumn("Last call"),
           "calls_30d": st.column_config.NumberColumn("Calls (30d)")}
    table_editor("pub", df, cfg, ["name", "ringba_publisher_id", "status", "publisher_email", "cc_publisher", "extra_cc", "notes"],
                 "publishers", tf, required=("name",), disabled=["managers", "last_call", "calls_30d"],
                 insert_extra={"created_at": db.now_utc()})

    st.subheader("Affiliate managers on CC")
    st.caption("Give a publisher 1, 2, 3 or more affiliate managers. You can select many publishers at once.")
    flash("flash_assign")
    pname = {r["name"]: r["id"] for r in rows}
    with st.form("assign_form"):
        sel_p = st.multiselect("Publishers", list(pname), key="as_pubs")
        sel_m = st.multiselect("Affiliate managers", [m["name"] for m in mgrs], key="as_mgrs")
        mode = st.radio("What to do", ["Add these managers (keep the existing ones)", "Replace all managers with these",
                                       "Remove these managers"], key="as_mode")
        go = st.form_submit_button("Apply")
    if go:
        if not sel_p or not sel_m and not mode.startswith("Replace"):
            set_flash("flash_assign", "err", "Pick at least one publisher and one manager.")
        else:
            m_ids = [m["id"] for m in mgrs if m["name"] in sel_m]
            for p in sel_p:
                pid = pname[p]
                if mode.startswith("Replace"):
                    db.x("DELETE FROM publisher_managers WHERE publisher_id=:p", p=pid)
                for mid in m_ids:
                    if mode.startswith("Remove"):
                        db.x("DELETE FROM publisher_managers WHERE publisher_id=:p AND manager_id=:m", p=pid, m=mid)
                    else:
                        db.x("INSERT INTO publisher_managers (publisher_id, manager_id) VALUES (:p,:m) "
                             "ON CONFLICT DO NOTHING", p=pid, m=mid)
            set_flash("flash_assign", "ok", f"Done for {len(sel_p)} publisher(s).")
        st.rerun()


# ================================================================== BUYERS
def page_buyers():
    st.caption("Tick **Watchlist** to get an email every time a call CONNECTS to that buyer. Change anything, then press "
               "**Save changes** once. Add a buyer by name before it has calls and it links automatically later.")
    since = db.now_utc() - timedelta(days=30)
    rows = db.q("""SELECT b.id, b.name, b.ringba_buyer_id, b.status, b.watchlist, b.buyer_email, b.cc_buyer, b.extra_cc, b.notes,
                          (SELECT MAX(call_at) FROM calls WHERE buyer_id = b.id) AS last_call,
                          (SELECT COUNT(*) FROM calls WHERE buyer_id = b.id AND call_at >= :s) AS calls_30d,
                          (SELECT COUNT(*) FROM calls WHERE buyer_id = b.id AND call_at >= :s AND paid = TRUE) AS paid_30d
                   FROM buyers b ORDER BY b.name""", s=since)
    cols = ["id", "name", "ringba_buyer_id", "status", "watchlist", "buyer_email", "cc_buyer", "extra_cc", "notes",
            "last_call", "calls_30d", "paid_30d"]
    df = frame(rows, cols)
    df["last_call"] = df["last_call"].map(lambda v: "never" if pd.isna(v) else fmt(v))
    for c in ("watchlist", "cc_buyer"):
        df[c] = df[c].fillna(False).astype(bool)

    def tf(v):
        validate_emails(v["buyer_email"], "buyer email")
        validate_emails(v["extra_cc"], "CC email")
        return {"name": v["name"], "ringba_buyer_id": v["ringba_buyer_id"], "status": v["status"] or "active",
                "watchlist": bool(v["watchlist"]), "buyer_email": v["buyer_email"], "cc_buyer": bool(v["cc_buyer"]),
                "extra_cc": v["extra_cc"], "notes": v["notes"]}

    cfg = {"id": None,
           "name": st.column_config.TextColumn("Buyer name", required=True),
           "ringba_buyer_id": st.column_config.TextColumn("Ringba ID", help="Leave empty for hand-added buyers."),
           "status": st.column_config.SelectboxColumn("Status", options=["active", "paused", "terminated"]),
           "watchlist": st.column_config.CheckboxColumn("⭐ Watchlist"),
           "buyer_email": st.column_config.TextColumn("Buyer's own email"),
           "cc_buyer": st.column_config.CheckboxColumn("CC the buyer?"),
           "extra_cc": st.column_config.TextColumn("Extra CC emails", help="Comma-separated."),
           "notes": st.column_config.TextColumn("Notes"),
           "last_call": st.column_config.TextColumn("Last call"),
           "calls_30d": st.column_config.NumberColumn("Calls (30d)"),
           "paid_30d": st.column_config.NumberColumn("Paid (30d)")}
    table_editor("buy", df, cfg, ["name", "ringba_buyer_id", "status", "watchlist", "buyer_email", "cc_buyer", "extra_cc", "notes"],
                 "buyers", tf, required=("name",), disabled=["last_call", "calls_30d", "paid_30d"],
                 insert_extra={"created_at": db.now_utc()})


# ================================================================== DIDs
def page_dids():
    st.info("**DIDs update by themselves.** Every hour the engine reads all numbers on your Ringba account (including newly created ones, "
            "with their Ringba creation time). A number that was deleted in Ringba is marked **retired** automatically. "
            "You can also press the sync button, add numbers by hand, or set a DID to **retired** yourself (retired DIDs never alert).")
    flash("flash_didsync")
    ok_t, err = db.get_setting("numbers_last_ok", ""), db.get_setting("numbers_last_error", "")
    c1, c2 = st.columns([1, 3])
    if c1.button("🔄 Sync DIDs from Ringba now"):
        r = None
        try:
            r = didtools.sync_if_due(force=True)
        except Exception as e:
            st.error(f"DID sync failed: {e}")
        if r is not None:
            rep = r["report"]
            used = ", ".join(f"{k}='{v}'" for k, v in rep["mapped"].items())
            msg = (f"Ringba has {r['in_ringba']} numbers: {r['added']} new, {r['updated']} updated, {r['retired']} marked retired, "
                   f"{r['reactivated']} re-activated. {rep['with_created_date']} had a creation date, {rep['with_publisher']} are assigned to a publisher"
                   f"{', ' + str(rep['released']) + ' already released by Ringba (ignored)' if rep.get('released') else ''}. Fields used: {used}.")
            if r["retire_skipped"]:
                msg += f" NOT retired: {r['retire_skipped']} DIDs are missing from Ringba's list, which looks too many, so I left them alone."
            set_flash("flash_didsync", "ok", msg)
            st.rerun()
    c2.caption((f"Last successful sync: {ago(ok_t)}. " if ok_t else "Not synced yet. ") + (f"Last problem: {err}" if err else ""))
    now = db.now_utc()
    since = now - timedelta(days=30)
    pubn = {p["id"]: p["name"] for p in db.q("SELECT id, name FROM publishers")}
    rows = db.q("""SELECT d.id, d.number, d.label, d.status, d.monthly_cost, d.notes, d.added_at, d.last_call_at,
                          d.publisher_id, d.affiliate_name, d.next_charge_at,
                          (SELECT COUNT(*) FROM calls WHERE did_id = d.id AND call_at >= :s) AS calls_30d
                   FROM dids d""", s=since)
    recs = []
    for r in rows:
        ref = db.to_dt(r["last_call_at"]) or db.to_dt(r["added_at"])
        recs.append({"id": r["id"], "number": r["number"], "label": r["label"], "status": r["status"],
                     "monthly_cost": float(r["monthly_cost"]) if r["monthly_cost"] is not None else None, "notes": r["notes"],
                     "publisher": pubn.get(r["publisher_id"]) or r["affiliate_name"] or "",
                     "next_charge": fmt(r["next_charge_at"], False)[:10] if r["next_charge_at"] else "",
                     "created_added": fmt(r["added_at"]) if r["added_at"] else "-",
                     "last_call": fmt(r["last_call_at"]) if r["last_call_at"] else "never",
                     "idle_days": (now - ref).days if ref else None, "calls_30d": r["calls_30d"],
                     "_retired": 1 if r["status"] == "retired" else 0})
    cols = ["id", "number", "label", "status", "monthly_cost", "notes", "publisher", "next_charge", "created_added", "last_call",
            "idle_days", "calls_30d"]
    df = frame(recs, cols + ["_retired"])
    df = df.sort_values(["_retired", "idle_days"], ascending=[True, False], na_position="last").drop(columns="_retired").reset_index(drop=True)

    def tf(v):
        num = rc.normalize_phone(v["number"])
        if not num:
            raise ValueError("number is not valid")
        return {"number": num, "label": v["label"], "status": v["status"] or "active",
                "monthly_cost": v["monthly_cost"], "notes": v["notes"]}

    cfg = {"id": None,
           "number": st.column_config.TextColumn("DID number", required=True),
           "label": st.column_config.TextColumn("Label"),
           "status": st.column_config.SelectboxColumn("Status", options=["active", "retired"]),
           "monthly_cost": st.column_config.NumberColumn("Monthly cost $", min_value=0.0, format="%.2f"),
           "notes": st.column_config.TextColumn("Notes"),
           "publisher": st.column_config.TextColumn("Publisher (from Ringba)", help="The publisher this number is assigned to in Ringba. Their affiliate managers can be copied on the inactive-DID email."),
           "next_charge": st.column_config.TextColumn("Next charge", help="When Ringba bills this number next."),
           "created_added": st.column_config.TextColumn("Created in Ringba", help="Ringba creation time (allocation date). For hand-added DIDs: the day you added it."),
           "last_call": st.column_config.TextColumn("Last call"),
           "idle_days": st.column_config.NumberColumn("Days without calls"),
           "calls_30d": st.column_config.NumberColumn("Calls (30d)")}
    table_editor("did", df, cfg, ["number", "label", "status", "monthly_cost", "notes"], "dids", tf, required=("number",),
                 disabled=["publisher", "next_charge", "created_added", "last_call", "idle_days", "calls_30d"], insert_extra={"added_at": db.now_utc()})

    with st.expander("➕ Add / import DIDs (including numbers that never got a call)"):
        flash("flash_did_import")
        st.markdown("**Option 1: paste numbers.** One per line. Optional label and Ringba creation date after commas, e.g. "
                    "`+16143505503, Rehab line, 2026-06-12 09:30`. Numbers that exist already just get the date / label filled in.")
        with st.form("did_paste_form"):
            txt = st.text_area("Numbers", height=130, key="did_paste_txt")
            go1 = st.form_submit_button("Import pasted numbers")
        if go1:
            items, bad = didtools.parse_lines(txt)
            r = didtools.upsert_dids(items)
            set_flash("flash_did_import", "ok", f"Added {r['added']}, updated {r['updated']}, unchanged {r['skipped']}; {bad} lines were not valid numbers.")
            st.rerun()
        st.markdown("**Option 2: upload the numbers list exported from Ringba** (Numbers page → export). "
                    "I look for the number, created-date and name columns by myself.")
        with st.form("did_file_form"):
            up = st.file_uploader("Ringba numbers CSV", type=["csv"], key="did_file")
            go2 = st.form_submit_button("Import this file")
        if go2 and up is not None:
            items, bad, info = didtools.parse_export(up.getvalue().decode("utf-8-sig", errors="replace"))
            if isinstance(info, dict) and info.get("error"):
                set_flash("flash_did_import", "err", f"{info['error']} Columns found: {', '.join(info.get('headers', []))}")
            else:
                r = didtools.upsert_dids(items)
                set_flash("flash_did_import", "ok", f"Added {r['added']}, updated {r['updated']}, unchanged {r['skipped']}; {bad} rows skipped. "
                                                    f"Columns used: number='{info['number']}', created='{info['created']}', label='{info['label']}'.")
            st.rerun()


# ================================================================== CAPS
def caps_section(entity_type, default_metric):
    table = "publishers" if entity_type == "publisher" else "buyers"
    ents = db.q(f"SELECT id, name FROM {table} ORDER BY name")
    name2id = {e["name"]: e["id"] for e in ents}
    rows = db.q(f"""SELECT c.id, c.entity_id, e.name AS name, c.period, c.metric, c.cap_value, c.thresholds, c.active, c.notes
                    FROM caps c JOIN {table} e ON e.id = c.entity_id WHERE c.entity_type = :t ORDER BY e.name""", t=entity_type)
    now = db.now_utc()
    for r in rows:
        u = rules.cap_usage({"entity_type": entity_type, "entity_id": r["entity_id"], "period": r["period"],
                             "metric": r["metric"], "cap_value": r["cap_value"]}, now)
        r["used_now"], r["percent_now"] = u["usage"], round(u["pct"], 1)
    cols = ["id", "name", "period", "metric", "cap_value", "thresholds", "active", "notes", "used_now", "percent_now"]
    df = frame(rows, cols)
    df["active"] = df["active"].fillna(True).astype(bool)

    def tf(v):
        if v["name"] not in name2id:
            raise ValueError(f"pick a {entity_type} from the list")
        cap = int(float(v["cap_value"] or 0))
        if cap <= 0:
            raise ValueError("cap must be more than 0")
        th = ",".join(str(t) for t in rules.parse_thresholds(v["thresholds"] or "80,100"))
        return {"entity_type": entity_type, "entity_id": name2id[v["name"]], "period": v["period"] or "daily",
                "metric": v["metric"] or default_metric, "cap_value": cap, "thresholds": th,
                "active": bool(v["active"]), "notes": v["notes"]}

    cfg = {"id": None,
           "name": st.column_config.SelectboxColumn(entity_type.title(), options=list(name2id), required=True),
           "period": st.column_config.SelectboxColumn("Period", options=["daily", "monthly"], required=True),
           "metric": st.column_config.SelectboxColumn("Counts", options=["paid", "converted", "connected", "all"],
                                                      help="Which calls use up the cap"),
           "cap_value": st.column_config.NumberColumn("Cap (calls)", min_value=1, step=1, required=True),
           "thresholds": st.column_config.TextColumn("Alert at % (comma-separated)", help="Example: 80,100"),
           "active": st.column_config.CheckboxColumn("Active"),
           "notes": st.column_config.TextColumn("Notes"),
           "used_now": st.column_config.NumberColumn("Used now"),
           "percent_now": st.column_config.NumberColumn("% now")}
    table_editor(f"cap_{entity_type}", df, cfg, ["name", "period", "metric", "cap_value", "thresholds", "active", "notes"],
                 "caps", tf, required=("name", "cap_value"), allow_delete=True, disabled=["used_now", "percent_now"])


def page_caps():
    start, _ = window_start("daily", db.now_utc())
    st.caption(f"Copy your caps from Ringba here. The tool counts calls and emails you when usage reaches the % you choose. "
               f"Today's window started {fmt(start)}. Daily caps reset at the 'Day starts at' time in Settings "
               f"(default 00:00, i.e. the day ends at 11:59 PM). Monthly caps reset on the 1st. Remove a row to delete a cap.")
    st.subheader("🏢 Buyer caps")
    st.caption("Default: counts PAID calls.")
    caps_section("buyer", "paid")
    st.subheader("🧑‍💼 Publisher caps")
    st.caption("Default: counts CONVERTED calls.")
    caps_section("publisher", "converted")


# ================================================================== EMAILS
def page_emails():
    flash("flash_admin")
    st.subheader("1. Who always gets the alerts (To:)")
    cur = ", ".join(alerts.admin_to())
    with st.form("admin_form"):
        txt = st.text_area("Admin emails (one per line, or separated by commas)", value=cur.replace(", ", "\n"), height=100, key="admin_txt")
        save = st.form_submit_button("💾 Save admin emails")
    if save:
        toks = [t for t in EMAIL_TOKEN.split(txt or "") if t]
        bad = [t for t in toks if not alerts.EMAIL_RE.match(t)]
        if bad:
            set_flash("flash_admin", "err", "Not valid: " + ", ".join(bad))
        else:
            db.set_setting("admin_emails", ", ".join(alerts.parse_emails(txt)))
            set_flash("flash_admin", "ok", "Saved.")
        st.rerun()
    if st.button("📨 Send a test email now"):
        to = alerts.admin_to()
        if not to:
            st.error("Save at least one admin email first.")
        else:
            try:
                mailer.send_test(to)
                st.success("Test email sent to " + ", ".join(to) + ". Check your inbox (and spam).")
            except Exception as e:
                st.error(f"Could not send: {e}")

    st.subheader("2. Sender (who the email comes from)")
    st.caption("Emails are sent through the mail account in your Secrets (SMTP_USER). To send from your company address, put the company "
               "mailbox and its app password in the Secrets (Google Workspace / Microsoft 365 both work). Here you can change the sender "
               "NAME and where replies go. A different From address only works if your mail provider allows it (for Gmail: Settings → "
               "'Send mail as').")
    name, addr = mailer.sender()
    with st.form("sender_form"):
        c1, c2, c3 = st.columns(3)
        f_name = c1.text_input("Sender name", db.get_setting("from_name", ""), key="snd_name")
        f_mail = c2.text_input("From address (empty = your SMTP login)", db.get_setting("from_email", ""), key="snd_mail")
        f_reply = c3.text_input("Reply-to address(es)", db.get_setting("reply_to", ""), key="snd_reply")
        sv = st.form_submit_button("💾 Save sender")
    st.caption(f"Emails currently go out as: {name + ' ' if name else ''}<{addr or 'SMTP_USER not set'}>")
    if sv:
        try:
            validate_emails(f_mail, "From address")
            validate_emails(f_reply, "reply-to address")
            db.set_setting("from_name", f_name.strip())
            db.set_setting("from_email", f_mail.strip())
            db.set_setting("reply_to", f_reply.strip())
            set_flash("flash_admin", "ok", "Sender saved.")
        except ValueError as e:
            set_flash("flash_admin", "err", str(e))
        st.rerun()

    st.subheader("3. Affiliate managers")
    st.caption("Create managers here, then give them to publishers on the Publishers page (a publisher can have several). "
               "Remove a row to delete a manager.")
    rows = db.q("SELECT id, name, email, active FROM affiliate_managers ORDER BY name")
    df = frame(rows, ["id", "name", "email", "active"])
    df["active"] = df["active"].fillna(True).astype(bool)

    def tf(v):
        validate_emails(v["email"], "email")
        if not alerts.EMAIL_RE.match((v["email"] or "").split(",")[0].strip()):
            raise ValueError("email is required")
        return {"name": v["name"], "email": v["email"], "active": bool(v["active"]) if v["active"] is not None else True}

    cfg = {"id": None, "name": st.column_config.TextColumn("Manager name", required=True),
           "email": st.column_config.TextColumn("Email", required=True),
           "active": st.column_config.CheckboxColumn("Active")}
    table_editor("mgr", df, cfg, ["name", "email", "active"], "affiliate_managers", tf, required=("name", "email"),
                 allow_delete=True)

    st.subheader("4. Each alert type: on/off, numbers, and who is copied")
    st.caption("The wording of each email and its answer buttons are on the 'Email templates' page.")
    rows = db.q("""SELECT alert_type, label, enabled, param_days, param_count, cc_manager, cc_entity_email,
                          cc_entity_extra, extra_cc, description FROM alert_rules ORDER BY label""")
    cols = ["alert_type", "label", "enabled", "param_days", "param_count", "cc_manager", "cc_entity_email",
            "cc_entity_extra", "extra_cc", "description"]
    df = frame(rows, cols)
    for c in ("enabled", "cc_manager", "cc_entity_email", "cc_entity_extra"):
        df[c] = df[c].fillna(False).astype(bool)

    def tf2(v):
        validate_emails(v["extra_cc"], "CC email")
        return {"enabled": bool(v["enabled"]), "param_days": None if v["param_days"] is None else int(v["param_days"]),
                "param_count": None if v["param_count"] is None else int(v["param_count"]),
                "cc_manager": bool(v["cc_manager"]), "cc_entity_email": bool(v["cc_entity_email"]),
                "cc_entity_extra": bool(v["cc_entity_extra"]), "extra_cc": v["extra_cc"]}

    cfg = {"alert_type": None,
           "label": st.column_config.TextColumn("Alert"),
           "enabled": st.column_config.CheckboxColumn("On"),
           "param_days": st.column_config.NumberColumn("Days", min_value=1, step=1),
           "param_count": st.column_config.NumberColumn("Count", min_value=1, step=1),
           "cc_manager": st.column_config.CheckboxColumn("CC affiliate managers"),
           "cc_entity_email": st.column_config.CheckboxColumn("CC publisher / buyer own email"),
           "cc_entity_extra": st.column_config.CheckboxColumn("CC their extra emails"),
           "extra_cc": st.column_config.TextColumn("Always CC these emails"),
           "description": st.column_config.TextColumn("What it does", width="large")}
    table_editor("rules", df, cfg, ["enabled", "param_days", "param_count", "cc_manager", "cc_entity_email",
                                    "cc_entity_extra", "extra_cc"], "alert_rules", tf2, dynamic=False,
                 disabled=["label", "description"], pk="alert_type")
    with st.expander("How the CC logic works"):
        st.markdown(
            "- **To:** always the admin emails above.\n"
            "- **CC affiliate managers:** ALL active managers given to that publisher (publisher alerts only).\n"
            "- **CC publisher / buyer own email:** only if the alert switch here is on **and** 'CC the publisher/buyer?' is ticked on their row.\n"
            "- **CC their extra emails:** the 'Extra CC emails' on that publisher or buyer row.\n"
            "- **Always CC these emails:** extra addresses for that alert type.\n"
            "- Changes apply to alerts created from now on.")


# ================================================================== TEMPLATES & ANSWER BUTTONS
def page_templates():
    flash("flash_tpl")
    rules_ = db.q("SELECT alert_type, label, description, template_id FROM alert_rules ORDER BY label")
    by_label = {r["label"]: r for r in rules_}
    choice = st.selectbox("Alert type", list(by_label), key="tpl_alert")
    rule = by_label[choice]
    atype = rule["alert_type"]
    st.caption(rule["description"])
    if not actions.app_url():
        st.warning("Answer buttons are not added to emails yet: put your app link in Settings → 'Your app link'.")

    tpls = db.q("SELECT id, name, subject, body FROM email_templates WHERE alert_type=:t ORDER BY id", t=atype)
    if not tpls:
        subj, body = templates.DEFAULTS[atype]
        db.x("INSERT INTO email_templates (alert_type, name, subject, body, created_at, updated_at) VALUES (:t,'Default',:s,:b,:n,:n)",
             t=atype, s=subj, b=body, n=db.now_utc())
        st.rerun()
    labels = [f"{t['name']}" + ("  ✅ in use" if t["id"] == rule["template_id"] else "") for t in tpls]
    idx = st.selectbox("Template", range(len(tpls)), format_func=lambda i: labels[i], key=f"tpl_pick_{atype}")
    t = tpls[idx]

    with st.expander("Placeholders you can use in the subject and message"):
        st.markdown("\n".join(f"- `{{{k}}}`: {d}" for k, d in templates.PLACEHOLDERS[atype]))
    ver = st.session_state.get("tplver", 0)
    kk = f"{atype}_{t['id']}_{ver}"
    with st.form(f"tpl_form_{kk}"):
        name = st.text_input("Template name", t["name"], key=f"tpl_name_{kk}")
        subject = st.text_input("Subject", t["subject"], key=f"tpl_subj_{kk}")
        body = st.text_area("Message / note", t["body"], height=280, key=f"tpl_body_{kk}")
        c1, c2, c3 = st.columns(3)
        save = c1.form_submit_button("💾 Save changes")
        save_new = c2.form_submit_button("➕ Save as NEW template")
        preview = c3.form_submit_button("👁 Preview")
    now = db.now_utc()
    if save or save_new:
        if not (subject or "").strip() or not (body or "").strip():
            set_flash("flash_tpl", "err", "Subject and message cannot be empty.")
        elif save:
            db.x("UPDATE email_templates SET name=:n, subject=:s, body=:b, updated_at=:u WHERE id=:i",
                 n=name.strip() or t["name"], s=subject, b=body, u=now, i=t["id"])
            set_flash("flash_tpl", "ok", "Template saved. It is used for the next alerts.")
        else:
            nm = name.strip() or "New template"
            if any(x["name"] == nm for x in tpls):
                nm += " (copy)"
            db.x("INSERT INTO email_templates (alert_type, name, subject, body, created_at, updated_at) VALUES (:t,:n,:s,:b,:u,:u)",
                 t=atype, n=nm, s=subject, b=body, u=now)
            set_flash("flash_tpl", "ok", f"Saved as new template '{nm}'. Pick it in the list, then press 'Use this template'.")
        st.session_state["tplver"] = ver + 1
        st.rerun()
    if preview:
        sample = templates.SAMPLE[atype]
        st.markdown("**Preview (with example data):**")
        st.text(f"Subject: {db.get_setting('subject_prefix', '')} {templates.render(subject, sample)}\n\n{templates.render(body, sample)}")
        defs = actions.defs_for(atype)
        if defs:
            st.text("Please choose one (click a link):\n" + "\n".join(f"  * {d['label']}" for d in defs))
    c1, c2 = st.columns(2)
    if c1.button(f"✅ Use this template for '{choice}'", key=f"use_{atype}_{t['id']}"):
        db.x("UPDATE alert_rules SET template_id=:t WHERE alert_type=:a", t=t["id"], a=atype)
        set_flash("flash_tpl", "ok", f"'{t['name']}' will be used for the next '{choice}' alerts.")
        st.rerun()
    if c2.button("🗑 Delete this template", key=f"del_{atype}_{t['id']}"):
        if t["id"] == rule["template_id"] or len(tpls) <= 1:
            st.error("You cannot delete the template in use (or the only one). Choose another one first.")
        else:
            db.x("DELETE FROM email_templates WHERE id=:i", i=t["id"])
            set_flash("flash_tpl", "ok", "Deleted.")
            st.rerun()

    st.subheader("Answer buttons inside this email")
    st.caption("Each button becomes a secret link in the email. When someone (for example an affiliate manager) clicks it and confirms, "
               "the answer is saved, shown in the Alert Log, and your admin emails are notified. Effect: **record** = only record it, your team "
               "decides; **snooze** = also pause this alert for N days; **retire_did** = also mark the DID retired (DID alerts only). "
               "Remove all rows to send this alert without buttons.")
    rows = db.q("SELECT id, label, effect, effect_days, active, sort_order FROM alert_action_defs WHERE alert_type=:t "
                "ORDER BY sort_order, id", t=atype)
    df = frame(rows, ["id", "label", "effect", "effect_days", "active", "sort_order"])
    df["active"] = df["active"].fillna(True).astype(bool)

    def tf(v):
        eff = v["effect"] or "record"
        days = None if v["effect_days"] is None else int(v["effect_days"])
        if eff == "snooze" and not days:
            raise ValueError("'snooze' needs the number of Days")
        if eff == "retire_did" and atype != "did_inactive":
            raise ValueError("'retire_did' only works for Inactive DID alerts")
        return {"label": v["label"], "effect": eff, "effect_days": days, "active": bool(v["active"]),
                "sort_order": int(v["sort_order"] or 0)}

    cfg = {"id": None, "label": st.column_config.TextColumn("Button text", required=True, width="large"),
           "effect": st.column_config.SelectboxColumn("Effect", options=list(actions.EFFECTS)),
           "effect_days": st.column_config.NumberColumn("Days", min_value=1, step=1),
           "active": st.column_config.CheckboxColumn("Shown"),
           "sort_order": st.column_config.NumberColumn("Order", step=1)}
    table_editor(f"act_{atype}", df, cfg, ["label", "effect", "effect_days", "active", "sort_order"], "alert_action_defs", tf,
                 required=("label",), allow_delete=True, insert_extra={"alert_type": atype})


# ================================================================== ALERT LOG
def page_log():
    flash("flash_log")
    counts = {r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) AS n FROM alerts GROUP BY status")}
    st.write(" · ".join(f"**{k}**: {v}" for k, v in sorted(counts.items())) or "No alerts yet.")
    c1, c2, c3 = st.columns(3)
    if c1.button("📤 Send pending alerts now"):
        st.success(str(mailer.process_outbox()))
    if c2.button("🔁 Retry failed alerts"):
        n = db.x("UPDATE alerts SET status='pending', attempts=0 WHERE status='failed'")
        st.success(f"{n} alerts put back in the queue. Press 'Send pending alerts now'.")
    show = c3.multiselect("Show status", ["pending", "sent", "dry_run", "failed", "skipped"],
                          default=["pending", "sent", "dry_run", "failed"])
    if show:
        rows = db.q_in("SELECT id, created_at, alert_type, subject, to_emails, cc_emails, status, attempts, last_error, "
                       "response_label, responded_by, responded_at FROM alerts WHERE status IN :s ORDER BY id DESC LIMIT 300",
                       "s", show)
        if not rows:
            st.info("Nothing to show.")
        else:
            d = pd.DataFrame(rows)
            d["created_at"] = d["created_at"].map(fmt)
            d["responded_at"] = d["responded_at"].map(lambda v: "" if pd.isna(v) else fmt(v))
            st.dataframe(d, hide_index=True)
            pick = st.selectbox("Read one alert (id)", [r["id"] for r in rows])
            a = db.q1("SELECT * FROM alerts WHERE id=:i", i=pick)
            if a:
                ans = f"\nANSWER: {a['response_label']} (by {a['responded_by'] or '?'}) {a['response_note'] or ''}" if a["response_label"] else ""
                st.text(f"To: {a['to_emails']}\nCC: {a['cc_emails']}\nSubject: {a['subject']}\nStatus: {a['status']}  {a['last_error'] or ''}{ans}\n\n{a['body']}")

    st.subheader("Paused (snoozed) alerts")
    sn = [r for r in db.q("SELECT entity_key, snooze_until, reason FROM snoozes") if db.to_dt(r["snooze_until"]) > db.now_utc()]
    if not sn:
        st.caption("Nothing is paused.")
    else:
        for r in sn:
            if r["entity_key"].startswith("did:"):
                d = db.q1("SELECT number FROM dids WHERE id=:i", i=int(r["entity_key"].split(":")[1]))
                r["what"] = f"DID {d['number']}" if d else r["entity_key"]
            else:
                r["what"] = r["entity_key"]
            r["until"] = fmt(r["snooze_until"])
        st.dataframe(pd.DataFrame(sn)[["what", "until", "reason"]], hide_index=True)
        with st.form("unsnooze_form"):
            pick = st.multiselect("Resume alerts for", [r["what"] for r in sn], key="unsnooze_pick")
            go = st.form_submit_button("Resume now")
        if go:
            for r in sn:
                if r["what"] in pick:
                    db.x("DELETE FROM snoozes WHERE entity_key=:k", k=r["entity_key"])
            set_flash("flash_log", "ok", f"Resumed {len(pick)}.")
            st.rerun()


# ================================================================== SYNC & IMPORT
def page_sync():
    first, last, n = reports.coverage()
    st.write(f"**{n:,} calls stored**" + (f", from {fmt(first, False)} to {fmt(last)}." if n else "."))

    st.subheader("1. Load history from Ringba")
    st.caption("Loading history NEVER sends alerts. It is safe to run again: calls are updated, never duplicated. It pauses 1 second "
               "between requests so Ringba does not block you, so 90 days takes several minutes. Keep this tab open, or use the "
               "'Load history' workflow on GitHub (Actions tab) to run it in the background.")
    days = st.number_input("How many days back?", 1, 400, 90, key="bf_days")
    if st.button("⬇️ Load history now", type="primary"):
        res = ingest.backfill_days(int(days), progress_bar_cb("Loading history"))
        st.success(f"Finished. {res['inserted']} new calls, {res['updated']} refreshed.")
        if res["failures"]:
            st.error("These days failed (run again to retry them):\n" + "\n".join(res["failures"][:10]))

    st.subheader("2. Or upload a CSV you exported from Ringba")
    up = st.file_uploader("Ringba call log CSV", type=["csv"])
    if up is not None and st.button("Import this CSV"):
        stats, rep = ingest.import_csv_text(up.getvalue().decode("utf-8-sig", errors="replace"))
        if stats is None:
            st.error("This file is missing: " + ", ".join(rep["missing"]))
        else:
            st.success(f"Imported: {stats['inserted']} new calls, {stats['updated']} already there.")

    st.subheader("3. Run the engine once, right now")
    st.caption("Does exactly what the 5-minute job does: read recent calls, check rules, send emails (respecting DRY RUN).")
    if st.button("▶️ Run engine now"):
        ok, summary = engine.run_once(log=lambda m: None)
        (st.success if ok else st.error)("Finished" if ok else "Finished with errors")
        st.code(summary)

    st.subheader("Recent sync runs")
    rows = db.q("SELECT id, kind, started_at, status, rows_fetched, rows_inserted, rows_updated, error FROM sync_runs "
                "ORDER BY id DESC LIMIT 15")
    if rows:
        d = pd.DataFrame(rows)
        d["started_at"] = d["started_at"].map(fmt)
        st.dataframe(d, hide_index=True)


# ================================================================== SETTINGS
def page_settings():
    flash("flash_settings")
    st.subheader("General")
    with st.form("settings_form"):
        c1, c2 = st.columns(2)
        tzname = c1.text_input("Timezone", db.get_setting("timezone"), key="set_tz")
        day_start = c2.text_input("Day starts at (HH:MM)", db.get_setting("day_start"), key="set_day",
                                  help="00:00 means a daily cap resets at midnight, i.e. the day ends at 11:59 PM.")
        look = c1.number_input("Re-read the last N hours every run", 1, 48, int(float(db.get_setting("live_lookback_hours"))), key="set_look")
        prefix = c2.text_input("Email subject prefix", db.get_setting("subject_prefix"), key="set_prefix")
        nm = c1.number_input("Refresh the DID list from Ringba every N minutes", 5, 1440, int(float(db.get_setting("numbers_sync_minutes") or 60)), key="set_nm")
        auto_ret = c2.checkbox("Mark DIDs that disappear from Ringba as retired", value=str(db.get_setting("numbers_auto_retire")).lower() == "true", key="set_autoret")
        app_url = st.text_input("Your app link (used for the answer buttons in emails)", db.get_setting("app_url", ""), key="set_url",
                                help="Example: https://your-app-name.streamlit.app")
        real = st.checkbox("✅ Send REAL emails (untick = DRY RUN, nothing is sent)", value=not mailer.is_dry_run(), key="set_real",
                           disabled=config.DRY_RUN_ENV,
                           help="DRY RUN is forced on by the DRY_RUN=true secret." if config.DRY_RUN_ENV else None)
        saved = st.form_submit_button("💾 Save settings")
    if saved:
        try:
            ZoneInfo(tzname)
            hh, mm = day_start.split(":")
            assert 0 <= int(hh) < 24 and 0 <= int(mm) < 60
            assert not app_url.strip() or app_url.strip().startswith("http")
        except Exception:
            set_flash("flash_settings", "err", "Timezone must look like America/New_York, the time like 00:00, and the app link must start with https://")
        else:
            db.set_setting("timezone", tzname)
            db.set_setting("day_start", f"{int(hh):02d}:{int(mm):02d}")
            db.set_setting("live_lookback_hours", int(look))
            db.set_setting("subject_prefix", prefix)
            db.set_setting("app_url", app_url.strip().rstrip("/"))
            db.set_setting("numbers_sync_minutes", int(nm))
            db.set_setting("numbers_auto_retire", "true" if auto_ret else "false")
            if not config.DRY_RUN_ENV:
                db.set_setting("dry_run", "false" if real else "true")
            set_flash("flash_settings", "ok", "Saved.")
        st.rerun()

    st.subheader("Connections")
    st.write({"Database": "✅ connected", "Ringba token": "✅ set" if config.RINGBA_TOKEN else "❌ missing",
              "Ringba account ID": "✅ set" if config.RINGBA_ACCOUNT_ID else "❌ missing",
              "Mail login (SMTP_USER)": "✅ set" if config.SMTP_USER else "❌ missing",
              "Mail app password": "✅ set" if config.SMTP_PASSWORD else "❌ missing",
              "App password (optional)": "✅ set" if config.get_secret("APP_PASSWORD") else "⚠️ not set - anyone with the link can open this app"})
    if st.button("🔌 Test Ringba connection"):
        try:
            res = rc.test_connection()
            rep = res["report"]
            if rep["missing"]:
                st.error("Connected, but Ringba's answer is missing: " + ", ".join(rep["missing"]))
                st.write({"Keys Ringba returned": rep.get("first_row_keys", [])})
            elif res["records"] == 0:
                st.warning("Connected, but there were no calls in the last 3 hours. Try again later or load history.")
            else:
                st.success(f"Ringba works. Read {res['records']} calls from the last 3 hours (column set: {rep['column_set']}).")
            st.write({"Fields found": rep["mapped"]})
            if res["sample"]:
                s0 = res["sample"][0]
                st.caption("First call read (the DID must be the number dialed, not the caller's number):")
                st.write({"DID": s0["did"], "Caller": s0["caller"], "Publisher": s0["publisher_name"], "Buyer": s0["buyer_name"],
                          "Connected": s0["connected"], "Time": fmt(s0["call_at"])})
        except Exception as e:
            st.error(str(e))

    st.subheader("Database size")
    rows = [{"table": t, "rows": db.q1(f"SELECT COUNT(*) AS n FROM {t}")["n"]}
            for t in ["calls", "publishers", "buyers", "dids", "caps", "alerts", "sync_runs"]]
    st.dataframe(pd.DataFrame(rows), hide_index=True)


# ================================================================== navigation: only the chosen page is loaded (fast)
PAGES = {"📊 Dashboard": page_dashboard, "📑 Reports": page_reports, "🧑‍💼 Publishers": page_publishers,
         "🏢 Buyers": page_buyers, "📞 DIDs": page_dids, "🚦 Caps": page_caps, "✉️ Emails": page_emails,
         "📝 Email templates": page_templates, "📜 Alert Log": page_log, "🔄 Sync & Import": page_sync,
         "⚙️ Settings": page_settings}

if mailer.is_dry_run():
    st.warning("🧪 DRY RUN is ON: alerts are created but emails are NOT really sent. Turn it off in Settings when you are ready.")
st.title("📞 Pay-Per-Call Alert Center")
page = st.radio("Menu", list(PAGES), horizontal=True, label_visibility="collapsed", key="page")
try:
    PAGES[page]()
except Exception as e:  # show errors on screen instead of a blank page
    st.error(f"Something went wrong on this page: {e}")
    st.exception(e)
