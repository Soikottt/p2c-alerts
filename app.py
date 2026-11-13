"""Pay-Per-Call Alert Center  -  the screen you work in."""
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import json
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
import backup
import cases
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
        mail = st.text_input("Your email address", key="ans_mail", help="So the team can see exactly who answered.")
        note = st.text_area("Note for the team (optional)", key="ans_note")
        go = st.form_submit_button("✅ Confirm my answer")
    if go:
        if not who.strip() or not alerts.EMAIL_RE.match(mail.strip()):
            st.error("Please type your name and a valid email address, then confirm again.")
        else:
            res = actions.respond(token, f"{who.strip()} <{mail.strip().lower()}>", note)
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
        db.q1("SELECT never_alert FROM dids LIMIT 1")
        db.q1("SELECT campaign_name FROM caps LIMIT 1")
        db.q1("SELECT ringba_target_id FROM targets LIMIT 1")
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


def column_order(name, df, column_config):
    """(shown columns, saved order). The order is saved per table in the settings, so it stays after you close the app."""
    shown = [c for c in df.columns if column_config.get(c, True) is not None and not str(c).startswith("_")]
    try:
        saved = json.loads(db.get_setting(f"colorder_{name}") or "[]")
    except ValueError:
        saved = []
    return shown, [c for c in saved if c in shown] + [c for c in shown if c not in saved]


def arrange_columns(name, shown, order):
    """Move a column to any position. (Streamlit tables cannot remember a drag, so this is the saved way to do it.)"""
    with st.expander("↔️ Arrange columns"):
        label = lambda c: str(c).replace("_", " ")
        c1, c2, c3, c4 = st.columns([3, 2, 1, 1])
        col = c1.selectbox("Column", order, format_func=label, key=f"mv_col_{name}")
        pos = c2.number_input("Move to position (1 = first)", 1, len(order), min(order.index(col) + 1, len(order)),
                              key=f"mv_pos_{name}_{col}")
        c3.write(""); c4.write("")
        if c3.button("Move", key=f"mv_go_{name}"):
            new = [c for c in order if c != col]
            new.insert(int(pos) - 1, col)
            db.set_setting(f"colorder_{name}", json.dumps(new))
            st.rerun()
        if c4.button("Reset", key=f"mv_reset_{name}"):
            db.set_setting(f"colorder_{name}", "[]")
            st.rerun()
        st.caption("Now: " + "  ›  ".join(label(c) for c in order))


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
    shown, order = column_order(name, df, column_config)
    arrange_columns(name, shown, order)
    with st.form(f"form_{name}"):
        edited = st.data_editor(df, key=f"ed_{name}_{ver}", column_config=column_config, hide_index=True, column_order=order,
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
    if db.get_setting("inbox_last_error", ""):
        st.warning(f"Could not read email replies: {db.get_setting('inbox_last_error')}")

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
    tg_rows = db.q("SELECT * FROM targets WHERE active = TRUE AND (daily_cap IS NOT NULL OR monthly_cap IS NOT NULL) AND counts_at IS NOT NULL")
    if tg_rows:
        st.subheader("Buyer target caps right now (from Ringba)")
        out = []
        for t in tg_rows:
            for per, cap, used in (("daily", t["daily_cap"], t["count_day"]), ("monthly", t["monthly_cap"], t["count_month"])):
                if cap:
                    out.append({"Buyer": t["buyer_name"] or "", "Target": t["name"], "Period": per, "Used": used or 0, "Cap": cap,
                                "Percent": round((used or 0) / cap * 100, 1)})
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
    with st.expander("🔍 Compare with a Ringba report (check that the numbers match)"):
        st.caption(f"Download **Report by publisher** from Ringba for the SAME dates as above ({a} → {b}) and upload it here. "
                   "Ringba must be set to your account timezone. A difference of 0 means this tool matches Ringba.")
        with st.form("recon_form"):
            up = st.file_uploader("Ringba report CSV", type=["csv"], key="recon_file")
            go_r = st.form_submit_button("Compare")
        if go_r and up is not None:
            try:
                sdf, totals = reports.parse_ringba_summary(up.getvalue().decode("utf-8-sig", errors="replace"))
                st.session_state["recon"] = (reports.reconcile(sdf, cached_calls(s, e, None, None)), totals, f"{a} → {b}")
            except Exception as ex:
                st.session_state["recon"] = None
                st.error(f"Could not read that file: {ex}")
        rec = st.session_state.get("recon")
        if rec:
            res, totals, rng = rec
            tool_total = int(res["Tool Calls"].sum()) if "Tool Calls" in res else 0
            ring_total = totals.get("calls", int(res["Ringba Calls"].sum()))
            (st.success if tool_total == ring_total else st.warning)(
                f"Range compared: {rng}. Ringba says **{ring_total:,}** calls, this tool has **{tool_total:,}** "
                f"(difference {tool_total - ring_total:+,}).")
            bad_rows = res[res["Result"] != "✅ match"]
            st.caption(f"{len(res) - len(bad_rows)} publishers match, {len(bad_rows)} differ. If the tool is LOWER, load the missing days "
                       "(next box). If the day totals look shifted, check the timezone in Settings.")
            st.dataframe(res, hide_index=True)
            st.download_button("⬇️ Download comparison", res.to_csv(index=False), "comparison.csv", "text/csv", key="dl_recon")
    with st.expander("🩺 Check for missing or incomplete days"):
        st.caption("Looks for days with no calls, 25 calls or fewer, or far below a normal day (for example days loaded before the "
                   "20-calls-per-request fix). You can then reload exactly those days from Ringba.")
        if st.button("Check my stored history", key="chk_days"):
            f_, l_, n_ = reports.coverage()
            if n_ == 0:
                st.session_state["bad_days"] = []
            else:
                counts = reports.day_counts(f_.astimezone(tz()).date(), reports.today_local())
                st.session_state["bad_days"] = reports.suspicious_days(counts)
                st.session_state["day_counts"] = counts
        bad = st.session_state.get("bad_days")
        if bad is not None:
            if not bad:
                st.success("Every stored day looks complete.")
            else:
                cnt = st.session_state.get("day_counts", {})
                st.warning(f"{len(bad)} day(s) look incomplete.")
                st.dataframe(pd.DataFrame({"day": [str(d) for d in bad], "calls stored": [cnt.get(d, 0) for d in bad]}), hide_index=True)
                if st.button(f"⬇️ Reload these {len(bad)} days from Ringba", key="reload_days"):
                    bar = st.progress(0.0, text="Reloading")
                    tot = {"inserted": 0, "updated": 0}
                    fails = []
                    for i, d_ in enumerate(bad, 1):
                        ds, de = reports.utc_bounds(d_, d_)
                        r_ = ingest.backfill_range(ds, de)
                        tot["inserted"] += r_["inserted"]; tot["updated"] += r_["updated"]; fails += r_["failures"]
                        bar.progress(i / len(bad), text=f"Reloaded {d_} ({i}/{len(bad)})")
                    st.cache_data.clear()
                    st.session_state["bad_days"] = None
                    (st.error if fails else st.success)(f"Added {tot['inserted']} missing calls, refreshed {tot['updated']}." +
                                                         (" Some days failed: " + "; ".join(fails[:3]) if fails else ""))
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
    pubn = {p["id"]: p["name"] for p in db.q("SELECT id, name FROM publishers")}
    rows = db.q("""SELECT d.id, d.number, d.label, d.status, d.monthly_cost, d.notes, d.added_at, d.last_call_at,
                          d.publisher_id, d.affiliate_name, d.next_charge_at, d.campaign_id, d.never_alert
                   FROM dids d""")
    camp = didtools.campaign_names(rows)
    calls30 = didtools.calls_last_30d(now)
    am_notes = cases.latest_by_did()
    case_state = cases.status_by_did()
    recs = []
    for r in rows:
        ref = db.to_dt(r["last_call_at"]) or db.to_dt(r["added_at"])
        recs.append({"id": r["id"], "campaign": camp.get(r["id"], ""),
                     "publisher": pubn.get(r["publisher_id"]) or r["affiliate_name"] or "",
                     "created_added": fmt(r["added_at"]) if r["added_at"] else "-",
                     "next_charge": fmt(r["next_charge_at"], False)[:10] if r["next_charge_at"] else "",
                     "number": r["number"],
                     "last_call": fmt(r["last_call_at"]) if r["last_call_at"] else "never",
                     "calls_30d": calls30.get(r["id"], 0),
                     "follow_up": cases.one_line(am_notes.get(r["id"]), fmt),
                     "case": cases.label(case_state[r["id"]]) if r["id"] in case_state else "",
                     "status": r["status"], "never_alert": bool(r["never_alert"]), "label": r["label"],
                     "monthly_cost": float(r["monthly_cost"]) if r["monthly_cost"] is not None else None, "notes": r["notes"],
                     "idle_days": (now - ref).days if ref else None,
                     "_retired": 1 if r["status"] == "retired" else 0})
    cols = ["id", "campaign", "publisher", "created_added", "next_charge", "number", "last_call", "calls_30d", "follow_up",
            "case", "status", "never_alert", "label", "monthly_cost", "notes", "idle_days"]
    df = frame(recs, cols + ["_retired"])
    df["never_alert"] = df["never_alert"].fillna(False).astype(bool)
    df = df.sort_values(["_retired", "idle_days"], ascending=[True, False], na_position="last").drop(columns="_retired").reset_index(drop=True)

    def tf(v):
        num = rc.normalize_phone(v["number"])
        if not num:
            raise ValueError("number is not valid")
        return {"number": num, "label": v["label"], "status": v["status"] or "active", "never_alert": bool(v["never_alert"]),
                "monthly_cost": v["monthly_cost"], "notes": v["notes"]}

    cfg = {"id": None,
           "campaign": st.column_config.TextColumn("Campaign name"),
           "publisher": st.column_config.TextColumn("Publisher Name", help="The publisher this number is assigned to in Ringba."),
           "created_added": st.column_config.TextColumn("DID Created Date", help="Ringba creation (allocation) time. For hand-added DIDs: the day you added it."),
           "next_charge": st.column_config.TextColumn("Next charge", help="When Ringba bills this number next."),
           "number": st.column_config.TextColumn("DID", required=True),
           "last_call": st.column_config.TextColumn("Last Call Date"),
           "calls_30d": st.column_config.NumberColumn("Total calls last 30 days"),
           "follow_up": st.column_config.TextColumn("Follow up note from AM", width="large",
                                                    help="The latest answer or email reply from the affiliate manager. Full history: 'History of a DID' below."),
           "case": st.column_config.TextColumn("Case", help="Open / Follow-up / Done, for the newest inactive-DID alert."),
           "status": st.column_config.SelectboxColumn("Status", options=["active", "retired"]),
           "never_alert": st.column_config.CheckboxColumn("Never alert", help="Tick = this DID NEVER gets any alert (no inactive-DID emails, "
                                                          "and its calls do not trigger new-publisher / returning-publisher / watchlist emails). "
                                                          "Use it for DIDs you keep on purpose. It stays ticked through every Ringba sync."),
           "label": st.column_config.TextColumn("Label"),
           "monthly_cost": st.column_config.NumberColumn("Monthly cost $", min_value=0.0, format="%.2f"),
           "notes": st.column_config.TextColumn("Team notes"),
           "idle_days": st.column_config.NumberColumn("Days without calls")}
    table_editor("did", df, cfg, ["number", "label", "status", "never_alert", "monthly_cost", "notes"], "dids", tf, required=("number",),
                 disabled=["campaign", "publisher", "created_added", "next_charge", "last_call", "calls_30d", "follow_up", "case", "idle_days"],
                 insert_extra={"added_at": db.now_utc()})

    with st.expander("📜 History of a DID (every alert, answer and note, saved for later review)"):
        flash("flash_didnote")
        nums = {r["number"]: r["id"] for r in rows}
        if not nums:
            st.caption("No DIDs yet.")
        else:
            pick = st.selectbox("DID", sorted(nums), key="did_hist_pick")
            did_id = nums[pick]
            hist = db.q("SELECT id, created_at, subject, status, case_status FROM alerts WHERE alert_type='did_inactive' "
                        "AND snooze_key=:k AND status<>'skipped' ORDER BY id DESC", k=f"did:{did_id}")
            if not hist:
                st.caption("No inactive-DID alert has been created for this DID yet.")
            for h in hist:
                st.markdown(f"**Alert #{h['id']}** · {fmt(h['created_at'])} · {cases.label(h['case_status'])}")
                st.caption(h["subject"])
                tl = cases.timeline(h["id"])
                if tl:
                    d = pd.DataFrame(tl)
                    d["created_at"] = d["created_at"].map(fmt)
                    st.dataframe(d[["created_at", "source", "author", "text", "status_after"]], hide_index=True)
                else:
                    st.caption("No notes yet.")
            if hist:
                with st.form(f"did_note_form_{did_id}"):
                    who = st.text_input("Your name", key=f"did_note_who_{did_id}")
                    stt = st.selectbox("Mark the newest alert as", cases.STATUSES, format_func=cases.label,
                                       index=1, key=f"did_note_st_{did_id}")
                    txt = st.text_area("Follow-up / done note", key=f"did_note_txt_{did_id}")
                    go = st.form_submit_button("💾 Save note")
                if go:
                    if not (txt or "").strip():
                        set_flash("flash_didnote", "err", "Write a note first.")
                    else:
                        cases.add_note(hist[0]["id"], "team", who, txt, stt)
                        set_flash("flash_didnote", "ok", "Note saved.")
                    st.rerun()

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
    by_campaign = entity_type == "publisher"
    ents = db.q(f"SELECT id, name FROM {table} ORDER BY name")
    name2id = {e["name"]: e["id"] for e in ents}
    ALL = "(all campaigns)"
    campaigns = [ALL] + [r["campaign_name"] for r in db.q(
        "SELECT DISTINCT campaign_name FROM calls WHERE campaign_name IS NOT NULL AND call_at >= :s ORDER BY campaign_name",
        s=db.now_utc() - timedelta(days=120))]
    rows = db.q(f"""SELECT c.id, c.entity_id, e.name AS name, c.campaign_name, c.period, c.metric, c.cap_value, c.thresholds, c.active, c.notes
                    FROM caps c JOIN {table} e ON e.id = c.entity_id WHERE c.entity_type = :t ORDER BY e.name, c.campaign_name""", t=entity_type)
    now = db.now_utc()
    for r in rows:
        u = rules.cap_usage({"entity_type": entity_type, "entity_id": r["entity_id"], "period": r["period"],
                             "metric": r["metric"], "cap_value": r["cap_value"], "campaign_name": r["campaign_name"]}, now)
        r["used_now"], r["percent_now"] = u["usage"], round(u["pct"], 1)
        r["campaign"] = r["campaign_name"] or ALL
        if r["campaign_name"] and r["campaign_name"] not in campaigns:
            campaigns.append(r["campaign_name"])           # a campaign with no recent calls must stay selectable
    cols = ["id", "name"] + (["campaign"] if by_campaign else []) + ["period", "metric", "cap_value", "thresholds", "active", "notes", "used_now", "percent_now"]
    df = frame(rows, cols)
    df["active"] = df["active"].fillna(True).astype(bool)
    editable = ["name"] + (["campaign"] if by_campaign else []) + ["period", "metric", "cap_value", "thresholds", "active", "notes"]

    def tf(v):
        if v["name"] not in name2id:
            raise ValueError(f"pick a {entity_type} from the list")
        cap = int(float(v["cap_value"] or 0))
        if cap <= 0:
            raise ValueError("cap must be more than 0")
        th = ",".join(str(t) for t in rules.parse_thresholds(v["thresholds"] or "80,100"))
        out = {"entity_type": entity_type, "entity_id": name2id[v["name"]], "period": v["period"] or "daily",
               "metric": v["metric"] or default_metric, "cap_value": cap, "thresholds": th,
               "active": bool(v["active"]), "notes": v["notes"]}
        if by_campaign:
            out["campaign_name"] = None if (v["campaign"] in (None, ALL)) else v["campaign"]
        return out

    cfg = {"id": None,
           "name": st.column_config.SelectboxColumn(entity_type.title(), options=list(name2id), required=True),
           "campaign": st.column_config.SelectboxColumn("Campaign", options=campaigns,
                                                        help="This cap counts only calls of this campaign. '(all campaigns)' = every campaign of the publisher."),
           "period": st.column_config.SelectboxColumn("Period", options=["daily", "monthly"], required=True),
           "metric": st.column_config.SelectboxColumn("Counts", options=["paid", "converted", "connected", "all"],
                                                      help="Which calls use up the cap"),
           "cap_value": st.column_config.NumberColumn("Cap (calls)", min_value=1, step=1, required=True),
           "thresholds": st.column_config.TextColumn("Alert at % (comma-separated)", help="Example: 80,100"),
           "active": st.column_config.CheckboxColumn("Active"),
           "notes": st.column_config.TextColumn("Notes"),
           "used_now": st.column_config.NumberColumn("Used now"),
           "percent_now": st.column_config.NumberColumn("% now")}
    table_editor(f"cap_{entity_type}", df, cfg, editable, "caps", tf, required=("name", "cap_value"), allow_delete=True,
                 disabled=["used_now", "percent_now"])


def targets_section():
    import targets as tg
    st.caption("Read from Ringba by itself: every target's **daily** and **monthly** cap, and its live usage (the same counters Ringba's routing "
               "uses). You get an email when usage reaches the levels below. Targets are re-read from Ringba every "
               f"{int(float(db.get_setting('targets_sync_minutes') or 60))} minutes; usage on every 5-minute run.")
    flash("flash_targets")
    ok_t, err = db.get_setting("targets_last_ok", ""), db.get_setting("targets_last_error", "")
    c1, c2 = st.columns([1, 3])
    if c1.button("🔄 Read targets from Ringba now", key="tg_sync"):
        try:
            rc.set_deadline(120)
            r = tg.run(force=True)
            d, u = r["definitions"], r["usage"]
            set_flash("flash_targets", "ok", f"Ringba has {d['in_ringba']} targets: {d['report']['with_daily_cap']} with a daily cap, "
                      f"{d['report']['with_monthly_cap']} with a monthly cap. Usage read for {u['polled']} of {u['total']}.")
        except Exception as e:
            set_flash("flash_targets", "err", f"Could not read targets: {e}")
        finally:
            rc.set_deadline(None)
        st.rerun()
    c2.caption((f"Last successful read: {ago(ok_t)}. " if ok_t else "Not read yet. ") + (f"Last problem: {err}" if err else ""))
    rows = db.q("SELECT * FROM targets WHERE active = TRUE AND (daily_cap IS NOT NULL OR monthly_cap IS NOT NULL) ORDER BY buyer_name, name")
    if not rows:
        st.info("No target with a cap yet. Press the button above (Ringba must have a daily or monthly cap on the target).")
        return
    recs = []
    for r in rows:
        dp = round((r["count_day"] or 0) / r["daily_cap"] * 100, 1) if r["daily_cap"] else None
        mp = round((r["count_month"] or 0) / r["monthly_cap"] * 100, 1) if r["monthly_cap"] else None
        recs.append({"id": r["id"], "buyer": r["buyer_name"] or "", "target": r["name"],
                     "daily_cap": r["daily_cap"], "used_today": r["count_day"] if r["daily_cap"] else None, "today_pct": dp,
                     "monthly_cap": r["monthly_cap"], "used_month": r["count_month"] if r["monthly_cap"] else None, "month_pct": mp,
                     "alerts_on": bool(r["alerts_on"]), "thresholds": r["thresholds"], "usage_read": ago(r["counts_at"]) if r["counts_at"] else "never"})
    cols = ["id", "buyer", "target", "daily_cap", "used_today", "today_pct", "monthly_cap", "used_month", "month_pct", "alerts_on", "thresholds", "usage_read"]
    df = frame(recs, cols)
    df["alerts_on"] = df["alerts_on"].astype(bool)

    def tf(v):
        th = ",".join(str(t) for t in rules.parse_thresholds(v["thresholds"] or "80,100"))
        return {"alerts_on": bool(v["alerts_on"]), "thresholds": th}
    cfg = {"id": None, "buyer": st.column_config.TextColumn("Buyer"), "target": st.column_config.TextColumn("Target"),
           "daily_cap": st.column_config.NumberColumn("Daily cap"), "used_today": st.column_config.NumberColumn("Used today"),
           "today_pct": st.column_config.NumberColumn("Today %"), "monthly_cap": st.column_config.NumberColumn("Monthly cap"),
           "used_month": st.column_config.NumberColumn("Used this month"), "month_pct": st.column_config.NumberColumn("Month %"),
           "alerts_on": st.column_config.CheckboxColumn("Send alerts", help="Untick to stop alerts for this target."),
           "thresholds": st.column_config.TextColumn("Alert at % (comma-separated)", help="Example: 80,100. Applies to the daily and the monthly cap."),
           "usage_read": st.column_config.TextColumn("Usage read")}
    table_editor("targets", df, cfg, ["alerts_on", "thresholds"], "targets", tf, dynamic=False,
                 disabled=["buyer", "target", "daily_cap", "used_today", "today_pct", "monthly_cap", "used_month", "month_pct", "usage_read"])


def page_caps():
    start, _ = window_start("daily", db.now_utc())
    st.caption(f"The tool emails you when usage reaches the % you choose. "
               f"Today's window started {fmt(start)}. Daily caps reset at the 'Day starts at' time in Settings "
               f"(default 00:00, i.e. the day ends at 11:59 PM). Monthly caps reset on the 1st. Remove a row to delete a cap.")
    st.subheader("🎯 Buyer target caps (automatic from Ringba)")
    targets_section()
    st.subheader("🧑‍💼 Publisher caps (per campaign)")
    st.caption("Ringba has no cap data for publishers, so type these in. Pick the **campaign** when the publisher has a different cap on each "
               "campaign (one row per campaign). Usage is counted from your own call data. Default: counts CONVERTED calls.")
    caps_section("publisher", "converted")
    st.subheader("🏢 Buyer caps (manual, whole buyer)")
    st.caption("Only for a cap on the buyer as a whole. For caps on a buyer's targets use the automatic section above. Default: counts PAID calls.")
    caps_section("buyer", "paid")


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
    if db.get_setting("reply_to", "").strip():
        st.warning("A reply-to address is set, so email replies go to THAT mailbox, which this tool cannot read. For replies to be saved as "
                   "follow-up notes on the DID page, leave Reply-to empty (replies then come back to the alert mailbox).")
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
    with st.expander("✍️ Formatting: bold, bullets, numbers, links (type these in the message)"):
        st.markdown("The message is turned into a proper email. Type the left column; the email shows the right column.")
        st.markdown("\n".join(f"- `{a_}`  →  {b_}" for a_, b_ in templates.FORMAT_HELP))
        st.caption("Example:  `**Decision needed:** release {did}?`  then a line  `- **Publisher:** {publisher}`.  "
                   "Press Preview to see exactly how it will look.")
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
        plain_p, html_p = templates.render_body(body, sample)
        st.markdown("**Preview of the email (with example data):**")
        st.markdown(f"**Subject:** {db.get_setting('subject_prefix', '')} {templates.render(subject, sample)}")
        st.markdown(html_p, unsafe_allow_html=True)
        defs = actions.defs_for(atype)
        if defs:
            st.markdown("**Please choose one:**  " + "   ".join(f"`[ {d['label']} ]`" for d in defs))
        with st.expander("Plain-text version (for mail apps that do not show formatting)"):
            st.text(plain_p)
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
    err = db.get_setting("inbox_last_error", "")
    st.caption("Email replies from affiliate managers are read automatically." + (f"  ⚠️ Problem reading the mailbox: {err}" if err else
               (f"  Last checked: {ago(db.get_setting('inbox_last_ok'))}." if db.get_setting("inbox_last_ok") else "")))
    c1, c2, c3, c4 = st.columns(4)
    if c1.button("📤 Send pending alerts now"):
        st.success(str(mailer.process_outbox()))
    if c2.button("🔁 Retry failed alerts"):
        n = db.x("UPDATE alerts SET status='pending', attempts=0 WHERE status='failed'")
        st.success(f"{n} alerts put back in the queue. Press 'Send pending alerts now'.")
    if c3.button("🗄️ Prepare backup (ZIP)"):
        st.session_state["backup_zip"] = backup.build_zip()
    if st.session_state.get("backup_zip"):
        fname, data = st.session_state["backup_zip"]
        c4.download_button("⬇️ Download " + fname, data, fname, "application/zip", key="dl_backup")
    f1, f2 = st.columns(2)
    show = f1.multiselect("Show status", ["pending", "sent", "dry_run", "failed", "skipped"], default=["pending", "sent", "dry_run", "failed"])
    case_f = f2.multiselect("Team status", cases.STATUSES, default=cases.STATUSES, format_func=cases.label)
    if show and case_f:
        rows = db.q_in("SELECT id, created_at, alert_type, subject, to_emails, cc_emails, status, attempts, last_error, case_status, "
                       "response_label, response_note, responded_by, responded_at FROM alerts WHERE status IN :s ORDER BY id DESC LIMIT 400",
                       "s", show)
        rows = [r for r in rows if (r["case_status"] or "open") in case_f]
        if not rows:
            st.info("Nothing to show.")
        else:
            last_note, replied_by = {}, {}
            for n in db.q("SELECT alert_id, source, author, text FROM alert_notes ORDER BY id"):
                who_ = n["author"] or {"answer": "AM", "email": "AM", "team": "Team"}.get(n["source"], "")
                last_note[n["alert_id"]] = f"{n['text']}  - {who_} ({n['source']})"
                if n["source"] == "email":
                    replied_by[n["alert_id"]] = who_
            recs = [{"id": r["id"], "created": fmt(r["created_at"]), "type": r["alert_type"], "subject": r["subject"],
                     "team_status": cases.label(r["case_status"]), "am_answer": r["response_label"] or "",
                     "note_for_team": r["response_note"] or "", "latest_note": last_note.get(r["id"], ""), "add_note": "",
                     "answered_by": r["responded_by"] or (f"(email reply) {replied_by[r['id']]}" if r["id"] in replied_by else ""), "answered_at": fmt(r["responded_at"]) if r["responded_at"] else "",
                     "sent": r["status"], "to": r["to_emails"], "cc": r["cc_emails"], "error": r["last_error"] or ""} for r in rows]
            cols = ["id", "created", "type", "subject", "team_status", "am_answer", "note_for_team", "latest_note", "add_note",
                    "answered_by", "answered_at", "sent", "to", "cc", "error"]
            df = frame(recs, cols)
            st.caption("**Team status** is the dropdown in the table: change it (for example to *Done* once you did what the answer says), "
                       "type a note in **Add a note** if you like, then press **Save status / notes** once. Every change is kept in the history.")
            ver = st.session_state.get("logver", 0)
            cfg = {"id": st.column_config.NumberColumn("#"),
                   "created": st.column_config.TextColumn("Created"), "type": st.column_config.TextColumn("Alert type"),
                   "subject": st.column_config.TextColumn("Subject", width="large"),
                   "team_status": st.column_config.SelectboxColumn("Team status", options=[cases.STATUS_LABEL[s] for s in cases.STATUSES], width="medium"),
                   "am_answer": st.column_config.TextColumn("AM answer (button)"),
                   "note_for_team": st.column_config.TextColumn("Note for the team (from the answer page)", width="large"),
                   "latest_note": st.column_config.TextColumn("Latest note / email reply", width="large"),
                   "add_note": st.column_config.TextColumn("Add a note", width="large"),
                   "answered_by": st.column_config.TextColumn("Answered by"), "answered_at": st.column_config.TextColumn("Answered at"),
                   "sent": st.column_config.TextColumn("Email status"), "to": st.column_config.TextColumn("To"),
                   "cc": st.column_config.TextColumn("CC"), "error": st.column_config.TextColumn("Error")}
            with st.form("log_table_form"):
                edited = st.data_editor(df, key=f"ed_logtbl_{ver}", column_config=cfg, hide_index=True, num_rows="fixed",
                                        disabled=[c for c in cols if c not in ("team_status", "add_note")])
                who_t = st.text_input("Your name (saved with your changes)", key="log_who_table")
                go_t = st.form_submit_button("💾 Save status / notes")
            if go_t:
                orig_by = {int(r["id"]): r for _, r in df.iterrows()}
                changed = 0
                for _, r in edited.iterrows():
                    aid = int(r["id"])
                    new_status = cases.LABEL_STATUS.get(clean(r["team_status"]))
                    note_txt = clean(r["add_note"])
                    status_changed = bool(new_status) and new_status != cases.LABEL_STATUS.get(orig_by[aid]["team_status"])
                    if note_txt or status_changed:
                        cases.add_note(aid, "team", who_t, note_txt or f"Status changed to {cases.STATUS_LABEL[new_status]}",
                                       new_status if status_changed else None)
                        changed += 1
                st.session_state["logver"] = ver + 1
                set_flash("flash_log", "ok", f"Saved {changed} update(s)." if changed else "Nothing was changed.")
                st.rerun()

            pick = st.selectbox("Open one alert (id)", [r["id"] for r in rows], key="log_pick")
            a = db.q1("SELECT * FROM alerts WHERE id=:i", i=pick)
            if a:
                ans = ""
                if a["response_label"]:
                    ans = (f"\nANSWER: {a['response_label']}  (by {a['responded_by'] or '?'})"
                           + (f"\nNOTE FOR THE TEAM: {a['response_note']}" if a["response_note"] else ""))
                st.text(f"To: {a['to_emails']}\nCC: {a['cc_emails']}\nSubject: {a['subject']}\nEmail status: {a['status']}  {a['last_error'] or ''}{ans}\n\n{a['body']}")
                st.markdown("**History: answers, email replies and team notes (saved permanently)**")
                tl = cases.timeline(pick)
                if tl:
                    t = pd.DataFrame(tl)
                    t["created_at"] = t["created_at"].map(fmt)
                    st.dataframe(t[["created_at", "source", "author", "text", "status_after"]], hide_index=True)
                else:
                    st.caption("No notes yet.")
                cur = a["case_status"] or "open"
                with st.form(f"case_form_{pick}"):
                    who = st.text_input("Your name", key=f"case_who_{pick}")
                    stt = st.selectbox("Team status", cases.STATUSES, index=cases.STATUSES.index(cur) if cur in cases.STATUSES else 0,
                                       format_func=cases.label, key=f"case_st_{pick}")
                    txt = st.text_area("Note", key=f"case_txt_{pick}")
                    go = st.form_submit_button("💾 Save note")
                if go:
                    if not (txt or "").strip() and stt == cur:
                        set_flash("flash_log", "err", "Write a note or change the status.")
                    else:
                        cases.add_note(pick, "team", who, (txt or "").strip() or f"Status changed to {cases.label(stt)}", stt)
                        set_flash("flash_log", "ok", "Saved.")
                    st.rerun()

    log_cleanup()

    st.subheader("Paused (snoozed) alerts")
    sn = [r for r in db.q("SELECT entity_key, snooze_until, reason FROM snoozes") if db.to_dt(r["snooze_until"]) > db.now_utc()]
    if not sn:
        st.caption("Nothing is paused.")
    else:
        pubn = {p["id"]: p["name"] for p in db.q("SELECT id, name FROM publishers")}
        buyn = {b["id"]: b["name"] for b in db.q("SELECT id, name FROM buyers")}
        did_ids = [int(r["entity_key"].split(":")[1]) for r in sn if r["entity_key"].startswith("did:")]
        did_rows = {d["id"]: d for d in db.q_in("SELECT id, number, publisher_id, affiliate_name, campaign_id FROM dids WHERE id IN :i", "i", did_ids)} if did_ids else {}
        camp = didtools.campaign_names(list(did_rows.values())) if did_rows else {}
        caps_by_id = {c["id"]: c for c in db.q("SELECT id, entity_type, entity_id, campaign_name FROM caps")}
        tgt_by_id = {t["id"]: t for t in db.q("SELECT id, name FROM targets")}
        seen_labels = {}
        for r in sn:
            kind, _, ident = r["entity_key"].partition(":")
            r["campaign"], r["publisher"] = "", ""
            if kind == "did" and ident.isdigit() and int(ident) in did_rows:
                d = did_rows[int(ident)]
                r["what"] = f"DID {d['number']}"
                r["campaign"] = camp.get(d["id"], "")
                r["publisher"] = pubn.get(d["publisher_id"]) or d["affiliate_name"] or ""
            elif kind == "cap" and ident.isdigit() and int(ident) in caps_by_id:
                c = caps_by_id[int(ident)]
                if c["entity_type"] == "publisher":
                    r["publisher"] = pubn.get(c["entity_id"], "")
                    r["campaign"] = c["campaign_name"] or ""
                    r["what"] = f"Publisher cap: {r['publisher']}" + (f" / {c['campaign_name']}" if c["campaign_name"] else "")
                else:
                    r["what"] = f"Buyer cap: {buyn.get(c['entity_id'], '?')}"
            elif kind == "target":
                tid, _, per = ident.partition(":")
                trow = tgt_by_id.get(int(tid)) if tid.isdigit() else None
                r["what"] = f"Target cap ({per}): {trow['name']}" if trow else r["entity_key"]
                r["campaign"] = ""
                r["publisher"] = ""
            else:
                r["what"] = r["entity_key"]
            n = seen_labels[r["what"]] = seen_labels.get(r["what"], 0) + 1
            if n > 1:
                r["what"] = f"{r['what']} ({n})"
            r["until"] = fmt(r["snooze_until"])
        st.dataframe(pd.DataFrame(sn).rename(columns={"what": "paused", "campaign": "campaign name", "publisher": "publisher name"})
                     [["paused", "campaign name", "publisher name", "until", "reason"]], hide_index=True)
        with st.form("unsnooze_form"):
            pick = st.multiselect("Resume alerts for", [r["what"] for r in sn], key="unsnooze_pick")
            go = st.form_submit_button("Resume now")
        if go:
            for r in sn:
                if r["what"] in pick:
                    db.x("DELETE FROM snoozes WHERE entity_key=:k", k=r["entity_key"])
            set_flash("flash_log", "ok", f"Resumed {len(pick)}.")
            st.rerun()


def log_cleanup():
    """Delete test alerts. An alert that already exists blocks a new one for the same DID / cap, so test alerts must go
    before you launch for real."""
    with st.expander("🗑️ Delete test alerts (do this before you go live)"):
        st.caption("A DID that already has an alert (even a test one, even in DRY RUN) will NOT get a new alert for the same idle period. "
                   "Delete the test alerts here and the engine creates fresh, real ones on its next run (25 emails go out per run).")
        flash("flash_cleanup")
        also = st.checkbox("Also resume paused (snoozed) DIDs / caps that belong to the deleted alerts", value=True, key="clean_resume")
        recent = db.q("SELECT id, created_at, alert_type, subject, status FROM alerts ORDER BY id DESC LIMIT 300")
        labels = {f"#{r['id']} · {fmt(r['created_at'])} · {r['status']} · {r['subject']}"[:160]: r["id"] for r in recent}
        with st.form("clean_pick_form"):
            pick = st.multiselect("1. Pick the alerts to delete", list(labels), key="clean_pick")
            go1 = st.form_submit_button("🗑️ Delete the picked alerts")
        if go1:
            n = alerts.delete_alerts([labels[k] for k in pick], also) if pick else 0
            set_flash("flash_cleanup", "ok", f"Deleted {n} alert(s).")
            st.rerun()
        n_dry = db.q1("SELECT COUNT(*) AS n FROM alerts WHERE status='dry_run'")["n"]
        if st.button(f"2. Delete all {n_dry} DRY-RUN alerts (never sent)", disabled=not n_dry, key="clean_dry"):
            ids = [r["id"] for r in db.q("SELECT id FROM alerts WHERE status='dry_run'")]
            set_flash("flash_cleanup", "ok", f"Deleted {alerts.delete_alerts(ids, also)} dry-run alert(s).")
            st.rerun()
        types = [r["alert_type"] for r in db.q("SELECT DISTINCT alert_type FROM alerts ORDER BY alert_type")]
        with st.form("clean_all_form"):
            t = st.selectbox("3. Start fresh: delete ALL alerts of this type", ["(all types)"] + types, key="clean_type")
            word = st.text_input("Type DELETE to confirm", key="clean_word")
            go3 = st.form_submit_button("🧨 Delete them")
        if go3:
            if word.strip() != "DELETE":
                set_flash("flash_cleanup", "err", "Nothing deleted: type DELETE (capital letters) to confirm.")
            else:
                rows = db.q("SELECT id FROM alerts") if t == "(all types)" else db.q("SELECT id FROM alerts WHERE alert_type=:t", t=t)
                set_flash("flash_cleanup", "ok", f"Deleted {alerts.delete_alerts([r['id'] for r in rows], also)} alert(s).")
            st.rerun()
        if mailer.is_dry_run():
            st.info("DRY RUN is still ON. When you are ready, untick it in ⚙️ Settings ('Send REAL emails').")


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
        replies = c1.checkbox("Read email replies from affiliate managers", value=str(db.get_setting("read_replies")).lower() == "true", key="set_replies",
                              help="Reads the mailbox the alerts are sent from (IMAP must be on). Replies are saved as follow-up notes.")
        imap = c2.text_input("Mailbox server (IMAP). Empty = automatic", db.get_setting("imap_host", ""), key="set_imap")
        bk = c1.number_input("Email me a backup every N days (0 = never)", 0, 365, int(float(db.get_setting("backup_email_days") or 7)), key="set_bk")
        rem_d = c1.number_input("Inactive DID: nobody answered -> remind again after N days (0 = never remind)", 0, 60,
                                int(float(db.get_setting("did_reminder_days") or 3)), key="set_remd",
                                help="Also printed at the bottom of every inactive-DID email, so people know what happens next.")
        rem_m = c2.number_input("Inactive DID: send at most N reminders", 0, 20, int(float(db.get_setting("did_reminder_max") or 3)), key="set_remm")
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
            db.set_setting("read_replies", "true" if replies else "false")
            db.set_setting("imap_host", imap.strip())
            db.set_setting("backup_email_days", int(bk))
            db.set_setting("did_reminder_days", int(rem_d))
            db.set_setting("did_reminder_max", int(rem_m))
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
