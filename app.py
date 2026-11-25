"""Pay-Per-Call Alert Center  -  the screen you work in."""
import re
from datetime import datetime, timedelta, timezone
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
    known = actions.identity(token, st.query_params.get("r", ""), st.query_params.get("s", ""))
    with st.form("answer_form"):
        if known:
            st.markdown(f"Answering as **{known}**")        # personal link from the email: nothing to type
            who = mail = ""
        else:
            who = st.text_input("Your name", key="ans_who")
            mail = st.text_input("Your email address", key="ans_mail", help="So the team can see exactly who answered.")
        days = None
        if row["effect"] == "snooze_custom":
            days = st.number_input("Pause this alert for how many days?", 1, 365, int(row["effect_days"] or 7), key="ans_days",
                                   help="For example the number of days the DID owner asked for.")
        note = st.text_area("Note for the team (optional)", key="ans_note")
        go = st.form_submit_button("✅ Confirm my answer")
    if go and known:
        res = actions.respond(token, known, note, days)
        (st.success if res["ok"] else st.error)(res["msg"])
    elif go:
        if not who.strip() or not alerts.EMAIL_RE.match(mail.strip()):
            st.error("Please type your name and a valid email address, then confirm again.")
        else:
            res = actions.respond(token, f"{who.strip()} <{mail.strip().lower()}>", note, days)
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
        db.q1("SELECT thread_id FROM alerts LIMIT 1")
        db.q1("SELECT source, ringba_key FROM caps LIMIT 1")
        db.q1("SELECT title, alert_time, repeat_days, repeat_rule, last_alert_at, alert_count FROM tasks LIMIT 1")
        db.q1("SELECT day, paid FROM daily_stats LIMIT 1")
        db.q1("SELECT day FROM insight_days LIMIT 1")
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


def column_widths(name):
    try:
        w = json.loads(db.get_setting(f"colwidth_{name}") or "{}")
        return {k: int(v) for k, v in w.items() if str(v).isdigit() or isinstance(v, int)}
    except (ValueError, TypeError):
        return {}


def view_controls(name, df, column_config):
    """'Arrange columns' box for one table: move a column to any position and set its width in pixels. Both are saved
    (per table) and come back after you close the app. Returns (column order, column_config with the saved widths)."""
    shown, order = column_order(name, df, column_config)
    widths = column_widths(name)
    label = lambda c: str(c).replace("_", " ")
    with st.expander("↔️ Arrange columns / column width"):
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
        w1, w2, w3, w4 = st.columns([3, 2, 1, 1])
        wcol = w1.selectbox("Column to resize", order, format_func=label, key=f"w_col_{name}")
        wpx = w2.number_input("Width in pixels (small = 70, normal = 150, wide = 300)", 40, 900, int(widths.get(wcol, 150)), step=10,
                              key=f"w_px_{name}_{wcol}")
        w3.write(""); w4.write("")
        if w3.button("Set width", key=f"w_go_{name}"):
            widths[wcol] = int(wpx)
            db.set_setting(f"colwidth_{name}", json.dumps(widths))
            st.rerun()
        if w4.button("Reset", key=f"w_reset_{name}"):
            db.set_setting(f"colwidth_{name}", "{}")
            st.rerun()
        st.caption("Order now: " + "  ›  ".join(label(c) for c in order))
        if widths:
            st.caption("Saved widths: " + ", ".join(f"{label(c)} {px}px" for c, px in widths.items() if c in order))
    cfg = dict(column_config)
    for c, px in widths.items():
        if c in order:
            base = cfg.get(c)
            cfg[c] = (st.column_config.Column(base, width=px) if isinstance(base, str)
                      else {**base, "width": px} if isinstance(base, dict) else st.column_config.Column(width=px))
    return order, cfg


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
    df, fsig = filter_bar(name, df)
    order, column_config = view_controls(name, df, column_config)
    with st.form(f"form_{name}"):
        edited = st.data_editor(df, key=f"ed_{name}_{ver}_{fsig}", column_config=column_config, hide_index=True, column_order=order,
                                num_rows="dynamic" if dynamic else "fixed", disabled=list(disabled))
        submitted = st.form_submit_button("💾 Save changes")
    csv_button(name, df)
    if submitted:
        st.session_state[f"msg_{name}"] = save_table(table, df, edited, display_cols, pk=pk, transform=transform,
                                                     required=required, allow_delete=allow_delete,
                                                     insert_extra=insert_extra)
        st.session_state[f"ver_{name}"] = ver + 1
        st.rerun()


def frame(rows, columns):
    return pd.DataFrame(rows, columns=columns)


def filter_bar(name, df, skip=(), max_unique=40):
    """A search box + pick-lists for the columns that have few different values (status, type, publisher ...).
    Returns (the filtered table, a short code of the current filter). Nothing selected = no filter."""
    import hashlib
    if df is None or len(df) == 0:
        return df, "0"
    # text version of every column (empty cells -> ""), used for searching and the pick-lists
    txt = df.astype(object).where(df.notna(), "").astype(str)
    cand = []
    for c in df.columns:
        if c in skip or str(c).startswith("_") or str(c).lower() == "id":
            continue
        try:
            n = txt[c].nunique()
        except Exception:
            continue
        if 2 <= n <= max_unique:
            cand.append(c)
    active = sum(1 for c in cand if st.session_state.get(f"fl_{name}_{c}"))
    q = st.session_state.get(f"fq_{name}", "")

    def reset():
        st.session_state[f"fq_{name}"] = ""
        for c in cand:
            st.session_state[f"fl_{name}_{c}"] = []
    a, b = st.columns([4, 1])
    q = a.text_input("🔍 Search", key=f"fq_{name}", placeholder="Type any word: name, number, status ...", label_visibility="collapsed")
    b.button("✖ Clear filters", key=f"fr_{name}", on_click=reset, disabled=not (q or active), use_container_width=True)
    picks = {}
    if cand:
        with st.expander(f"🎛️ Filters" + (f" ({active} active)" if active else ""), expanded=bool(active)):
            cols = st.columns(min(len(cand), 4))
            for i, c in enumerate(cand):
                vals = sorted(txt[c].unique())
                picks[c] = cols[i % len(cols)].multiselect(str(c), vals, key=f"fl_{name}_{c}")
    out = df
    if q:
        hay = txt.apply(lambda col: col.str.contains(q, case=False, regex=False)).any(axis=1)
        out = out[hay]
    for c, sel in picks.items():
        if sel:
            out = out[txt[c].isin(sel)]
    sig = hashlib.md5((q + "|" + "|".join(f"{c}={sorted(v)}" for c, v in picks.items() if v)).encode()).hexdigest()[:8]
    if len(out) != len(df):
        st.caption(f"Showing {len(out):,} of {len(df):,} rows.")
    return out.reset_index(drop=True), sig


def mail_preview(html_body, plain_body=None):
    """Show an e-mail the way a mail app does: white page, dark text, whatever theme (dark / light) the app uses.
    Fixes text that had the same colour as the background."""
    import html as _h
    import streamlit.components.v1 as components
    page = ('<div style="background:#ffffff;color:#202124;font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.5;'
            'padding:16px 18px;border:1px solid #dadce0;border-radius:8px">' + (html_body or "") + "</div>")
    n = len(re.sub(r"<[^>]+>", "", html_body or "")) 
    components.html(page, height=min(900, max(180, 140 + int(n / 90) * 22 + (html_body or "").count("<li") * 8 + (html_body or "").count("<tr") * 30)),
                    scrolling=True)
    if plain_body is not None:
        with st.expander("Plain-text version (for mail apps that do not show formatting)"):
            lines = str(plain_body).count("\n") + 1
            components.html('<pre style="background:#ffffff;color:#202124;font-family:Consolas,Menlo,monospace;font-size:13px;'
                            'white-space:pre-wrap;word-wrap:break-word;margin:0;padding:12px 14px;border:1px solid #dadce0;border-radius:8px">'
                            + _h.escape(str(plain_body)) + "</pre>", height=min(700, max(120, 40 + lines * 20)), scrolling=True)


def csv_button(name, df, label="⬇️ Download CSV"):
    """CSV of the table exactly as filtered on screen (all rows, all columns)."""
    if df is None or len(df) == 0:
        return
    try:
        data = df.drop(columns=[c for c in df.columns if str(c).startswith("_")]).to_csv(index=False)
    except Exception:
        data = df.to_csv(index=False)
    st.download_button(f"{label} ({len(df):,} rows)", data, f"{name}.csv", "text/csv", key=f"dlcsv_{name}")


def show_df(name, df, **kw):
    """st.dataframe with the search / filter bar, the 'Arrange columns' box and a CSV download."""
    df2, _ = filter_bar(name, df)
    cfg = kw.pop("column_config", None) or {}
    kw.pop("column_order", None)
    if df2 is not None and len(df2.columns):
        order, cfg = view_controls(name, df2, cfg)
        kw["column_order"] = order
    kw["column_config"] = cfg
    st.dataframe(df2, hide_index=True, **kw)
    csv_button(name, df2)
    return df2


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
    import insights as _ins
    _t = _ins.today(now)
    _off = {}
    for _lab, _a, _b in (("Today", _t, _t), ("Yesterday", _t - timedelta(days=1), _t - timedelta(days=1)), ("Last 7 days", _t - timedelta(days=6), _t)):
        if _ins.covered(_a, _b, now):
            _off[_lab] = _ins.totals(_a, _b)
    if len(_off) == 3:                                   # official Ringba numbers
        for col, label in zip(st.columns(3), ["Today", "Yesterday", "Last 7 days"]):
            t_ = _off[label]
            col.markdown(f"**{label}**")
            col.metric("Calls", f"{int(t_['calls']):,}")
            col.caption(f"Connected {int(t_['connected'])} · Converted {int(t_['converted'])} · Paid {int(t_['paid'])} · Profit ${float(t_['profit']):,.2f}")
        st.caption("Official numbers from the Ringba insights report.")
    else:
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
        links = didtools.active_links()
        for c in caps:
            if not didtools.cap_has_active_did(c, links):
                continue                              # a publisher / campaign without an active DID is not shown or counted
            u = rules.cap_usage(c, now)
            out.append({"Type": c["entity_type"], "Name": u["name"], "Period": c["period"], "Counts": c["metric"],
                        "Used": u["usage"], "Cap": c["cap_value"], "Percent": round(u["pct"], 1)})
        show_df("dash_caps", pd.DataFrame(out).sort_values("Percent", ascending=False))
    tg_rows = db.q("SELECT * FROM targets WHERE active = TRUE AND (daily_cap IS NOT NULL OR monthly_cap IS NOT NULL OR all_time_cap IS NOT NULL) AND counts_at IS NOT NULL")
    if tg_rows:
        st.subheader("Buyer target caps right now (from Ringba)")
        out = []
        bnames = {b["id"]: b["name"] for b in db.q("SELECT id, name FROM buyers")}
        for t in tg_rows:
            for per, cap, used in (("daily", t["daily_cap"], t["count_day"]), ("monthly", t["monthly_cap"], t["count_month"]),
                                   ("global", t.get("all_time_cap"), t.get("count_total"))):
                if cap:
                    out.append({"Buyer": t["buyer_name"] or bnames.get(t["buyer_id"]) or "", "Target": t["name"], "Period": per, "Used": used or 0, "Cap": cap,
                                "Percent": round((used or 0) / cap * 100, 1)})
        show_df("dash_tcaps", pd.DataFrame(out).sort_values("Percent", ascending=False))

    st.subheader("Latest alerts")
    rows = db.q("SELECT created_at, alert_type, subject, status, response_label FROM alerts "
                "WHERE status<>'skipped' ORDER BY id DESC LIMIT 15")
    if rows:
        d = pd.DataFrame(rows)
        d["created_at"] = d["created_at"].map(fmt)
        show_df("dash_alerts", d)
    else:
        st.caption("No alerts yet.")


# ================================================================== REPORTS
@st.cache_data(ttl=120, show_spinner=False)
def cached_calls(s, e, pid, bid):
    return reports.load_calls(s, e, pid, bid)


def official_block(a, b, pf="All", bf="All", key="off"):
    """Numbers straight from the Ringba insights report (they match Ringba exactly)."""
    import insights
    pub = None if pf == "All" else pf
    buy = None if bf == "All" else bf
    now = db.now_utc()
    if not insights.covered(a, b, now):
        days = (b - a).days + 1
        st.warning("The official Ringba numbers for these dates are not all loaded yet (the engine loads them automatically, a few days per run).")
        if days <= 62 and st.button("⬇️ Load official numbers from Ringba now", key=f"{key}_load"):
            n_ok, bad = 0, []
            bar = st.progress(0.0)
            d = a
            try:
                rc.set_deadline(240)
                while d <= b:
                    try:
                        insights.sync_day(d, now)
                        n_ok += 1
                    except Exception as e:
                        bad.append(f"{d}: {str(e)[:120]}")
                        break
                    bar.progress(min(1.0, (d - a).days / max(days - 1, 1)))
                    d += timedelta(days=1)
            finally:
                rc.set_deadline(None)
            set_flash("flash_off", "err" if bad else "ok", f"Loaded {n_ok} day(s)." + (" Problem: " + "; ".join(bad) if bad else ""))
            st.rerun()
        flash("flash_off")
        return False
    t = insights.totals(a, b, publisher=pub, buyer=buy)
    calls, conn, rev = int(t["calls"]), int(t["connected"]), float(t["revenue"])
    for col, k, v in zip(st.columns(6), ["Calls", "Connected", "Converted", "Paid", "Blocked", "Duplicates"],
                         [calls, conn, int(t["converted"]), int(t["paid"]), int(t["blocked"]), int(t["duplicates"])]):
        col.metric(k, f"{v:,}")
    vals = [f"{conn / calls * 100:.1f}%" if calls else "0.0%", f"${rev:,.2f}", f"${float(t['payout']):,.2f}", f"${float(t['profit']):,.2f}",
            f"{float(t['cost']):,.2f}".join(["$", ""]), f"{int(t['seconds'] / conn) if conn else 0}",
            f"{float(t['profit']) / rev * 100:.1f}%" if rev else "-", f"${rev / calls:,.2f}" if calls else "-"]
    labels = ["Connect %", "Revenue", "Payout", "Profit", "Total cost", "Avg talk (sec)", "Margin", "Revenue / call"]
    for col, k, v in zip(st.columns(8), labels, vals):
        col.metric(k, v)
    tabs = st.tabs(["By publisher", "By campaign", "By buyer", "By day"])
    for tab, by, nm in zip(tabs, ["publisher", "campaign", "buyer", "day"], ["Publisher", "Campaign", "Buyer", "Day"]):
        with tab:
            rows = insights.breakdown(a, b, by)
            if pub and by != "publisher":
                pass
            df = pd.DataFrame(rows)
            if df.empty:
                st.caption("No calls.")
                continue
            df = df.rename(columns={"name": nm, "calls": "Calls", "connected": "Connected", "converted": "Converted", "paid": "Paid",
                                    "blocked": "Blocked", "duplicates": "Duplicates", "revenue": "Revenue", "payout": "Payout", "profit": "Profit", "cost": "Cost"})
            df["Connect %"] = (df["Connected"] / df["Calls"].where(df["Calls"] > 0) * 100).round(1)
            df["Margin %"] = (df["Profit"] / df["Revenue"].where(df["Revenue"] > 0) * 100).round(1)
            show_df(f"{key}_{by}", df[[nm, "Calls", "Connected", "Converted", "Paid", "Blocked", "Duplicates", "Connect %", "Revenue", "Payout", "Profit", "Margin %", "Cost"]])
    return True


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

    st.subheader("Official numbers (Ringba insights report)")
    st.caption("Straight from Ringba, one day at a time, cut at midnight in your timezone. Cap alerts use these numbers.")
    official_block(a, b, pf, bf, key="off_rep")
    st.subheader("From the stored calls")
    st.caption("Built from the individual calls saved in the database. If this differs from the official numbers above, the stored calls are "
               "out of date: use Sync & Import → Backfill to re-read them.")
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
            show_df("recon", res)
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
            bd = show_df(f"rep_{by}", bd)
            st.download_button("⬇️ Download CSV", bd.to_csv(index=False), f"{fn}_{a}_{b}.csv", "text/csv", key=f"dl_{fn}")
    with t_raw:
        raw = df.drop(columns=["day"]).copy()
        raw["call_at"] = raw["call_at"].dt.strftime("%Y-%m-%d %H:%M:%S")
        st.caption(f"All {len(raw):,} calls, newest first.")
        show_df("rep_raw", raw.iloc[::-1].reset_index(drop=True))
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
    show_all = st.checkbox("Also show publishers that have no active DID (to add a new publisher by hand, tick this)", key="pubs_show_nodid")
    rows = db.q("""SELECT p.id, p.name, p.ringba_publisher_id, p.status, p.publisher_email, p.cc_publisher, p.extra_cc, p.notes,
                          (SELECT MAX(call_at) FROM calls WHERE publisher_id = p.id) AS last_call,
                          (SELECT COUNT(*) FROM calls WHERE publisher_id = p.id AND call_at >= :s) AS calls_30d,
                          (SELECT COUNT(*) FROM dids WHERE publisher_id = p.id AND status = 'active') AS active_dids
                   FROM publishers p ORDER BY p.name""", s=since)
    total = len(rows)
    if not show_all:
        rows = [r for r in rows if r["active_dids"]]
        st.caption(f"Showing the {len(rows)} publishers that have at least one **active DID**" + (f" ({total - len(rows)} without one are hidden)." if total > len(rows) else "."))
    for r in rows:
        r["managers"] = ", ".join(sorted(links.get(r["id"], [])))
    cols = ["id", "name", "ringba_publisher_id", "status", "managers", "publisher_email", "cc_publisher", "extra_cc",
            "notes", "last_call", "calls_30d", "active_dids"]
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
           "calls_30d": st.column_config.NumberColumn("Calls (30d)"),
           "active_dids": st.column_config.NumberColumn("Active DIDs")}
    table_editor("pub", df, cfg, ["name", "ringba_publisher_id", "status", "publisher_email", "cc_publisher", "extra_cc", "notes"],
                 "publishers", tf, required=("name",), disabled=["managers", "last_call", "calls_30d", "active_dids"],
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
    t_buy, t_tg = st.tabs(["🏢 Buyers (buyer watchlist)", "🎯 Target watchlist"])
    with t_buy:
        _buyers_table()
    with t_tg:
        _target_watchlist()


def _target_watchlist():
    st.caption("Tick **Watchlist** on a target to get an email every time a call CONNECTS to that target. Only **active targets** "
               "of **active buyers** are listed. Targets come from Ringba (they refresh by themselves). The e-mail shows the target name, "
               "the target ID and the buyer.")
    since = db.now_utc() - timedelta(days=30)
    rows = db.q("""SELECT t.id, t.ringba_target_id AS target_id, t.name, COALESCE(t.buyer_name, b.name) AS buyer, t.watchlist
                   FROM targets t LEFT JOIN buyers b ON b.id = t.buyer_id
                   WHERE t.active = TRUE AND (t.buyer_id IS NULL OR COALESCE(b.status, 'active') = 'active')
                   ORDER BY buyer, t.name""")
    stat = {}
    for r in db.q("""SELECT LOWER(TRIM(target_name)) AS k, COUNT(*) AS n, SUM(CASE WHEN converted THEN 1 ELSE 0 END) AS conv,
                            MAX(call_at) AS last_call FROM calls WHERE call_at >= :s AND target_name IS NOT NULL
                     GROUP BY LOWER(TRIM(target_name))""", s=since):
        stat[r["k"]] = r
    names = {}
    for r in rows:
        names[(r["name"] or "").strip().lower()] = names.get((r["name"] or "").strip().lower(), 0) + 1
    for r in rows:
        k = (r["name"] or "").strip().lower()
        r["calls_30d"] = int((stat.get(k) or {}).get("n") or 0)
        r["converted_30d"] = int((stat.get(k) or {}).get("conv") or 0)
        lc = (stat.get(k) or {}).get("last_call")
        r["last_call"] = fmt(lc) if lc else "none in 30d"
    if not rows:
        st.info("No active targets yet. They appear after the engine reads your Ringba targets (Caps page, target caps).")
        return
    dup = [n for n, c in names.items() if c > 1]
    if dup:
        st.warning("Two active targets share the same name (" + ", ".join(dup[:5]) + "). Calls only carry the target name, so both are "
                   "matched by that name. Give them different names in Ringba to tell them apart.")
    cols = ["id", "target_id", "name", "buyer", "watchlist", "calls_30d", "converted_30d", "last_call"]
    df = frame(rows, cols)
    df["watchlist"] = df["watchlist"].fillna(False).astype(bool)
    n_on = int(df["watchlist"].sum())
    st.caption(f"{n_on} of {len(df)} active targets are on the watchlist.")

    def tf(v):
        return {"watchlist": bool(v["watchlist"])}

    cfg = {"id": None, "target_id": st.column_config.TextColumn("Target ID"), "name": st.column_config.TextColumn("Target name"),
           "buyer": st.column_config.TextColumn("Buyer"), "watchlist": st.column_config.CheckboxColumn("⭐ Watchlist"),
           "calls_30d": st.column_config.NumberColumn("Calls 30d"), "converted_30d": st.column_config.NumberColumn("Converted 30d", help="Calls in the last 30 days that converted."),
           "last_call": st.column_config.TextColumn("Last call")}
    table_editor("tgwatch", df, cfg, ["watchlist"], "targets", tf, dynamic=False,
                 disabled=["target_id", "name", "buyer", "calls_30d", "converted_30d", "last_call"])


def _buyers_table():
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
    conn30 = didtools.connected_last_30d(now)
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
                     "calls_30d": calls30.get(r["id"], 0), "conn_30d": conn30.get(r["id"], 0),
                     "follow_up": cases.one_line(am_notes.get(r["id"]), fmt),
                     "case": cases.label(case_state[r["id"]]) if r["id"] in case_state else "",
                     "status": r["status"], "never_alert": bool(r["never_alert"]), "label": r["label"],
                     "monthly_cost": float(r["monthly_cost"]) if r["monthly_cost"] is not None else None, "notes": r["notes"],
                     "idle_days": (now - ref).days if ref else None,
                     "_retired": 1 if r["status"] == "retired" else 0})
    cols = ["id", "campaign", "publisher", "created_added", "next_charge", "number", "last_call", "calls_30d", "conn_30d", "follow_up",
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
           "campaign": st.column_config.TextColumn("Campaign"),
           "publisher": st.column_config.TextColumn("Publisher", help="The publisher this number is assigned to in Ringba."),
           "created_added": st.column_config.TextColumn("Created", help="Ringba creation (allocation) time. For hand-added DIDs: the day you added it."),
           "next_charge": st.column_config.TextColumn("Next charge", help="When Ringba bills this number next."),
           "number": st.column_config.TextColumn("DID", required=True),
           "last_call": st.column_config.TextColumn("Last call"),
           "calls_30d": st.column_config.NumberColumn("Calls 30d", help="All calls to this DID in the last 30 days."),
           "conn_30d": st.column_config.NumberColumn("Connected 30d", help="Calls in the last 30 days that connected to a buyer."),
           "follow_up": st.column_config.TextColumn("AM note", width="large",
                                                    help="The latest answer or email reply from the affiliate manager. Full history: 'History of a DID' below."),
           "case": st.column_config.TextColumn("Case", help="Open / Follow-up / Done, for the newest inactive-DID alert."),
           "status": st.column_config.SelectboxColumn("Status", options=["active", "retired"]),
           "never_alert": st.column_config.CheckboxColumn("Never alert", help="Tick = this DID NEVER gets any alert (no inactive-DID emails, "
                                                          "and its calls do not trigger new-publisher / returning-publisher / watchlist emails). "
                                                          "Use it for DIDs you keep on purpose. It stays ticked through every Ringba sync."),
           "label": st.column_config.TextColumn("Label"),
           "monthly_cost": st.column_config.NumberColumn("Monthly cost $", min_value=0.0, format="%.2f"),
           "notes": st.column_config.TextColumn("Team notes"),
           "idle_days": st.column_config.NumberColumn("Idle days", help="Days without calls")}
    table_editor("did", df, cfg, ["number", "label", "status", "never_alert", "monthly_cost", "notes"], "dids", tf, required=("number",),
                 disabled=["campaign", "publisher", "created_added", "next_charge", "last_call", "calls_30d", "conn_30d", "follow_up", "case", "idle_days"],
                 insert_extra={"added_at": db.now_utc()})

    with st.expander("📜 History of a DID (every alert, answer and note, saved for later review)"):
        flash("flash_didnote")
        nums = {r["number"]: r["id"] for r in rows}
        if not nums:
            st.caption("No DIDs yet.")
        else:
            pick = st.selectbox("DID", sorted(nums), key="did_hist_pick")
            did_id = nums[pick]
            hist = db.q("SELECT id, created_at, subject, status, case_status FROM alerts WHERE alert_type IN ('did_inactive','did_new_no_calls') "
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
                    show_df("did_notes", d[["created_at", "source", "author", "text", "status_after"]])
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
    show_all = False
    links = None
    if by_campaign:
        show_all = st.checkbox("Also show caps of publishers / campaigns without an active DID (they are never counted or alerted)",
                               key="caps_show_nodid")
        links = didtools.active_links()
    ents = db.q(f"SELECT id, name FROM {table} ORDER BY name")
    if by_campaign and not show_all:
        ents = [e for e in ents if e["id"] in links[0]]
    name2id = {e["name"]: e["id"] for e in ents}
    ALL = "(all campaigns)"
    campaigns = [ALL] + [r["campaign_name"] for r in db.q(
        "SELECT DISTINCT campaign_name FROM calls WHERE campaign_name IS NOT NULL AND call_at >= :s ORDER BY campaign_name",
        s=db.now_utc() - timedelta(days=120))]
    rows = db.q(f"""SELECT c.id, c.entity_id, e.name AS name, c.campaign_name, c.period, c.metric, c.cap_value, c.thresholds, c.active, c.notes, c.source, c.ringba_key
                    FROM caps c JOIN {table} e ON e.id = c.entity_id WHERE c.entity_type = :t ORDER BY e.name, c.campaign_name""", t=entity_type)
    if by_campaign:
        hidden = 0
        keep = []
        for r in rows:
            if show_all or didtools.cap_has_active_did({"entity_type": "publisher", "entity_id": r["entity_id"], "campaign_name": r["campaign_name"],
                                                    "ringba_key": r.get("ringba_key")}, links):
                keep.append(r)
            else:
                hidden += 1
        rows = keep
        if hidden:
            st.caption(f"{hidden} cap(s) are hidden because that publisher has no active DID in that campaign.")
    now = db.now_utc()
    for r in rows:
        u = rules.cap_usage({"entity_type": entity_type, "entity_id": r["entity_id"], "period": r["period"],
                             "metric": r["metric"], "cap_value": r["cap_value"], "campaign_name": r["campaign_name"]}, now)
        r["used_now"], r["percent_now"] = u["usage"], round(u["pct"], 1)
        r["campaign"] = r["campaign_name"] or ALL
        r["from_"] = "Ringba" if r["source"] == "ringba" else "typed by you"
        if r["campaign_name"] and r["campaign_name"] not in campaigns:
            campaigns.append(r["campaign_name"])           # a campaign with no recent calls must stay selectable
    cols = ["id", "name"] + (["campaign"] if by_campaign else []) + ["period", "metric", "cap_value", "thresholds", "active", "notes", "used_now", "percent_now"] + (["from_"] if by_campaign else [])
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
           "from_": st.column_config.TextColumn("Cap from", help="Ringba = read automatically (it is updated on every read). typed by you = never changed by Ringba."),
           "percent_now": st.column_config.NumberColumn("% now")}
    table_editor(f"cap_{entity_type}", df, cfg, editable, "caps", tf, required=("name", "cap_value"), allow_delete=True,
                 disabled=["used_now", "percent_now"] + (["from_"] if by_campaign else []))


def pubcaps_section():
    import pubcaps
    st.caption("Read from Ringba by itself: the **daily** and **monthly** cap of every publisher in every campaign (from the campaign's payout rules; "
               "a publisher without its own payout rule uses the campaign's default payout). Re-read every "
               f"{int(float(db.get_setting('pubcaps_sync_minutes') or 60))} minutes. Usage is counted from your own call data (PAID calls). "
               "A cap you type below for the same publisher + campaign + period is never replaced by Ringba.")
    flash("flash_pubcaps")
    ok_t, err = db.get_setting("pubcaps_last_ok", ""), db.get_setting("pubcaps_last_error", "")
    c1, c2 = st.columns([1, 3])
    if c1.button("🔄 Read publisher caps from Ringba now", key="pc_sync"):
        try:
            rc.set_deadline(120)
            r = pubcaps.run(force=True)
            msg = (f"Ringba has {r['campaigns']} campaigns with {r['with_caps']} publisher caps: {r['added']} new, {r['updated']} changed, "
                   f"{r['removed']} removed, {r['skipped_manual']} skipped (you typed your own).")
            if r["unmatched"]:
                msg += " Publishers not in this tool yet (they appear after their first call): " + ", ".join(r["unmatched"][:15])
            if not r["with_caps"]:
                msg += f" No cap found. Campaign fields Ringba returned: {', '.join(r['report']['campaign_keys'])}. Publisher fields: {', '.join(r['report']['affiliate_keys'])}."
            set_flash("flash_pubcaps", "ok" if r["with_caps"] else "err", msg)
        except Exception as e:
            set_flash("flash_pubcaps", "err", f"Could not read publisher caps: {e}")
        finally:
            rc.set_deadline(None)
        st.rerun()
    c2.caption((f"Last successful read: {ago(ok_t)}. " if ok_t else "Not read yet. ") + (f"Last problem: {err}" if err else ""))


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
            rc.set_deadline(240)
            r = tg.run(force=True)
            d, u = r["definitions"], r["usage"]
            msg = (f"Ringba has {d['in_ringba']} targets: {d['report']['with_daily_cap']} with a daily cap, "
                   f"{d['report']['with_monthly_cap']} with a monthly cap, {d['report'].get('with_global_cap', 0)} with a global cap. Usage read for {u['polled']} of {u['total']}.")
            if d.get("partial"):
                msg += (f" {d['unchecked']} targets are still waiting (Ringba is slow right now). Nothing was lost: they are read automatically "
                        "on the next 5-minute run, or press the button again.")
            set_flash("flash_targets", "ok", msg)
        except Exception as e:
            set_flash("flash_targets", "err", f"Could not read targets: {e}")
        finally:
            rc.set_deadline(None)
        st.rerun()
    c2.caption((f"Last successful read: {ago(ok_t)}. " if ok_t else "Not read yet. ") + (f"Last problem: {err}" if err else ""))
    rows = db.q("SELECT * FROM targets WHERE active = TRUE AND (daily_cap IS NOT NULL OR monthly_cap IS NOT NULL OR all_time_cap IS NOT NULL) ORDER BY buyer_name, name")
    if not rows:
        st.info("No target with a cap yet. Press the button above (Ringba must have a daily or monthly cap on the target).")
        return
    recs = []
    bnames = {b["id"]: b["name"] for b in db.q("SELECT id, name FROM buyers")}
    for r in rows:
        dp = round((r["count_day"] or 0) / r["daily_cap"] * 100, 1) if r["daily_cap"] else None
        mp = round((r["count_month"] or 0) / r["monthly_cap"] * 100, 1) if r["monthly_cap"] else None
        gp = round((r.get("count_total") or 0) / r["all_time_cap"] * 100, 1) if r.get("all_time_cap") else None
        recs.append({"id": r["id"], "buyer": r["buyer_name"] or bnames.get(r["buyer_id"]) or "", "target": r["name"],
                     "daily_cap": r["daily_cap"], "used_today": r["count_day"] if r["daily_cap"] else None, "today_pct": dp,
                     "monthly_cap": r["monthly_cap"], "used_month": r["count_month"] if r["monthly_cap"] else None, "month_pct": mp,
                     "global_cap": r.get("all_time_cap"), "used_global": r.get("count_total") if r.get("all_time_cap") else None, "global_pct": gp,
                     "alerts_on": bool(r["alerts_on"]), "thresholds": r["thresholds"], "usage_read": ago(r["counts_at"]) if r["counts_at"] else "never"})
    cols = ["id", "buyer", "target", "daily_cap", "used_today", "today_pct", "monthly_cap", "used_month", "month_pct", "global_cap", "used_global", "global_pct", "alerts_on", "thresholds", "usage_read"]
    df = frame(recs, cols)
    df["alerts_on"] = df["alerts_on"].astype(bool)

    def tf(v):
        th = ",".join(str(t) for t in rules.parse_thresholds(v["thresholds"] or "80,100"))
        return {"alerts_on": bool(v["alerts_on"]), "thresholds": th}
    cfg = {"id": None, "buyer": st.column_config.TextColumn("Buyer"), "target": st.column_config.TextColumn("Target"),
           "daily_cap": st.column_config.NumberColumn("Daily cap"), "used_today": st.column_config.NumberColumn("Used today"),
           "today_pct": st.column_config.NumberColumn("Today %"), "monthly_cap": st.column_config.NumberColumn("Monthly cap"),
           "used_month": st.column_config.NumberColumn("Used this month"), "month_pct": st.column_config.NumberColumn("Month %"),
           "global_cap": st.column_config.NumberColumn("Global cap", help="Ringba 'Global Call Cap': the cap on the whole target for ever"),
           "used_global": st.column_config.NumberColumn("Used (global)"), "global_pct": st.column_config.NumberColumn("Global %"),
           "alerts_on": st.column_config.CheckboxColumn("Send alerts", help="Untick to stop alerts for this target."),
           "thresholds": st.column_config.TextColumn("Alert at % (comma-separated)", help="Example: 80,100. Applies to the daily, the monthly and the global cap."),
           "usage_read": st.column_config.TextColumn("Usage read")}
    table_editor("targets", df, cfg, ["alerts_on", "thresholds"], "targets", tf, dynamic=False,
                 disabled=["buyer", "target", "daily_cap", "used_today", "today_pct", "monthly_cap", "used_month", "month_pct", "global_cap", "used_global", "global_pct", "usage_read"])


def page_caps():
    start, _ = window_start("daily", db.now_utc())
    st.caption(f"The tool emails you when usage reaches the % you choose. "
               f"Today's window started {fmt(start)}. Daily caps reset at the 'Day starts at' time in Settings "
               f"(default 00:00, i.e. the day ends at 11:59 PM). Monthly caps reset on the 1st. Remove a row to delete a cap.")
    st.subheader("🎯 Buyer target caps (automatic from Ringba)")
    targets_section()
    st.subheader("🧑‍💼 Publisher caps per campaign (automatic from Ringba)")
    pubcaps_section()
    st.caption("You can also type a cap yourself (one row per campaign). Rows marked 'Ringba' are filled in automatically; you can change the "
               "alert levels, the Active tick and the notes, but the cap size itself follows Ringba.")
    caps_section("publisher", "paid")
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
    st.caption("Add a manager (name + email + Active). A manager gets e-mail copies in **two** ways: "
               "**(a)** tick **Gets ALL alerts** here: copied on every alert (publisher, buyer, target, DID, watchlist ...), "
               "or **(b)** give the manager to publishers on the Publishers page: copied only on alerts of those publishers, "
               "and only for alert types that have **CC affiliate managers** ticked in section 4. "
               "A manager with neither gets nothing. Remove a row to delete a manager.")
    rows = db.q("""SELECT m.id, m.name, m.email, m.active, m.all_alerts,
                          (SELECT COUNT(*) FROM publisher_managers pm WHERE pm.manager_id = m.id) AS publishers
                   FROM affiliate_managers m ORDER BY m.name""")
    df = frame(rows, ["id", "name", "email", "active", "all_alerts", "publishers"])
    df["active"] = df["active"].fillna(True).astype(bool)
    df["all_alerts"] = df["all_alerts"].fillna(False).astype(bool)
    for r in rows:
        if r["active"] and not r["all_alerts"] and not r["publishers"]:
            st.warning(f"**{r['name']}** is active but has no publishers and 'Gets ALL alerts' is off, so no e-mail is sent to this manager. "
                       "Tick **Gets ALL alerts** or give the manager to publishers on the Publishers page.")

    def tf(v):
        validate_emails(v["email"], "email")
        if not alerts.EMAIL_RE.match((v["email"] or "").split(",")[0].strip()):
            raise ValueError("email is required")
        return {"name": v["name"], "email": v["email"], "active": bool(v["active"]) if v["active"] is not None else True,
                "all_alerts": bool(v["all_alerts"]) if v["all_alerts"] is not None else False}

    cfg = {"id": None, "name": st.column_config.TextColumn("Manager name", required=True),
           "email": st.column_config.TextColumn("Email", required=True),
           "active": st.column_config.CheckboxColumn("Active"),
           "all_alerts": st.column_config.CheckboxColumn("Gets ALL alerts", help="Copied on every alert, no publisher needed."),
           "publishers": st.column_config.NumberColumn("Publishers given", help="How many publishers this manager is assigned to.")}
    table_editor("mgr", df, cfg, ["name", "email", "active", "all_alerts"], "affiliate_managers", tf, required=("name", "email"),
                 allow_delete=True, disabled=["publishers"])

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
            "- **Managers with 'Gets ALL alerts':** copied on every alert of every type (except your own task / system e-mails), whatever the switches below.\n"
            "- **CC publisher / buyer own email:** only if the alert switch here is on **and** 'CC the publisher/buyer?' is ticked on their row.\n"
            "- **CC their extra emails:** the 'Extra CC emails' on that publisher or buyer row.\n"
            "- **Always CC these emails:** extra addresses for that alert type.\n"
            "- Changes apply to alerts created from now on.")


# ================================================================== TEMPLATES & ANSWER BUTTONS
def page_templates():
    import tasks
    import richnote
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
    with st.expander("✍️ Formatting: use the buttons and shortcuts in the message box"):
        st.markdown("The message box works like an email editor (bold, colours, bullets, numbers, links, headings). "
                    "Press the ⌨ button in the box to see the shortcuts (Ctrl+B, Ctrl+I, Ctrl+U, Ctrl+K link, Ctrl+Shift+8 bullets ...).")
        st.caption("Type placeholders like {campaign} as normal text. You can make them bold or coloured too. "
                   "Press Preview to see exactly how the email will look. Old templates are converted when you open them; they keep working until you save.")
    ver = st.session_state.get("tplver", 0)
    kk = f"{atype}_{t['id']}_{ver}"
    with st.form(f"tpl_form_{kk}"):
        name = st.text_input("Template name", t["name"], key=f"tpl_name_{kk}")
        subject = st.text_input("Subject", t["subject"], key=f"tpl_subj_{kk}")
        st.caption("Message (write it like an email; type placeholders such as {campaign} where the real value should go)")
        body_in = richnote.rich_note(templates.to_editor_html(t["body"]), key=f"tpl_body_{kk}", height=340)
        body = richnote.value(f"tpl_body_{kk}") or body_in or ""
        c1, c2, c3 = st.columns(3)
        save = c1.form_submit_button("💾 Save changes")
        save_new = c2.form_submit_button("➕ Save as NEW template")
        preview = c3.form_submit_button("👁 Preview")
    now = db.now_utc()
    if save or save_new:
        body = tasks.clean_note(body)                    # safe HTML (empty when nothing was typed)
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
        defs = actions.defs_for(atype)
        mail_preview(html_p, plain_p)
        if defs:
            st.markdown("**Please choose one:**  " + "   ".join(f"`[ {d['label']} ]`" for d in defs))
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
        if eff in ("snooze", "snooze_custom") and not days:
            raise ValueError(f"'{eff}' needs the number of Days (for snooze_custom it is the suggested number)")
        if eff == "retire_did" and atype not in ("did_inactive", "did_new_no_calls"):
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
def show_thread_history(aid, key):
    """The whole e-mail conversation of an alert code: every alert / follow-up that was sent, every answer, reply and team note."""
    import chain as chainlib
    a0 = db.q1("SELECT id FROM alerts WHERE id=:i", i=int(aid))
    if not a0:
        st.warning(f"No alert with the code #A{aid} was found.")
        return None
    root, ids = chainlib.family_ids(aid)
    al = db.q_in("SELECT id, alert_type, dedupe_key, subject, status, created_at, sent_at, to_emails, cc_emails, body, thread_id FROM alerts WHERE id IN :i ORDER BY id", "i", ids)
    mails = [a for a in al if a["alert_type"] != "alert_response" and not str(a["dedupe_key"]).endswith(":cancelled")]
    st.markdown(f"#### 🧵 Conversation **#A{root}** - {len(mails)} email(s) in this thread")
    ev = chainlib.thread_events(ids)
    if ev:
        df = pd.DataFrame([{"when": fmt(e["when"]), "what": e["kind"], "who": e["who"], "details": e["text"], "email_code": f"#A{e['alert_id']}"} for e in ev])
        show_df(f"thread_{key}", df, column_config={"when": "When", "what": "What happened", "who": "Who", "details": st.column_config.TextColumn("Details", width="large"),
                                                      "email_code": st.column_config.TextColumn("Email id", help="The id of the single email in the Alert Log")})
    st.caption("All emails of this conversation use the same subject and the same code, so Gmail / Outlook show them as one thread.")
    for a in mails:
        with st.expander(f"{chainlib.kind_of(a)} - #A{a['id']} - {fmt(a['sent_at'] or a['created_at'])} - {a['status']}"):
            st.text(f"To: {a['to_emails']}\nCC: {a['cc_emails']}\nSubject: {a['subject']}\n\n{a['body']}")
    return set(ids)


def log_state(r, has_buttons):
    """(group, label) for one alert row, so you can see at a glance what needs you."""
    s, c = r["status"], (r["case_status"] or "open")
    if s == "failed":
        return "needs", "🔴 E-MAIL FAILED"
    if s == "pending":
        return "wait", "🟡 QUEUED - not sent yet"
    if s == "skipped":
        return "info", "⚪ Skipped"
    if s == "dry_run":
        return "info", "🧪 TEST - not really sent"
    return {"follow_up": ("needs", "🔴 NEEDS ACTION - answered"), "pending": ("needs", "🔴 NEEDS ACTION - not started"),
            "in_progress": ("wait", "🔧 IN PROGRESS"), "waiting": ("wait", "🟡 WAITING for publisher / AM"),
            "done": ("done", "🟢 DONE"), "no_action": ("done", "🟢 NO ACTION NEEDED")}.get(
        c, ("wait", "🟡 WAITING for an answer") if has_buttons else ("info", "⚪ INFO only"))


def chainlib_parse(text):
    import chain
    return chain.parse_code(text)


def chainlib_kind(row):
    import chain
    return chain.kind_of(row)


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
    code_q = st.text_input("🔎 Search / open an alert by its code (for example #A306)", key="log_code",
                           help="Shows the whole history of that code: the first alert, every follow-up, every answer, reply and note.")
    code_ids = None
    if (code_q or "").strip():
        cid = chainlib_parse(code_q)
        if cid:
            code_ids = show_thread_history(cid, "code")
        else:
            st.warning("Type a code like #A306 or just the number 306.")
    with st.expander("More filters (e-mail status, team status)"):
        f1, f2 = st.columns(2)
        show = f1.multiselect("Show status", ["pending", "sent", "dry_run", "failed", "skipped"], default=["pending", "sent", "dry_run", "failed"])
        case_f = f2.multiselect("Team status", cases.STATUSES, default=cases.STATUSES, format_func=cases.label)
    if show and case_f:
        rows = db.q_in("SELECT id, created_at, alert_type, dedupe_key, thread_id, subject, to_emails, cc_emails, status, attempts, last_error, case_status, "
                       "response_label, response_note, responded_by, responded_at FROM alerts WHERE status IN :s ORDER BY id DESC LIMIT 20000",
                       "s", show)
        rows = [r for r in rows if (r["case_status"] or "open") in case_f]
        if code_ids is not None:
            rows = [r for r in rows if r["id"] in code_ids]
        if not rows:
            st.info("Nothing to show.")
        else:
            with_buttons = {r["alert_id"] for r in db.q_in("SELECT DISTINCT alert_id FROM alert_actions WHERE alert_id IN :i", "i", [r["id"] for r in rows])}
            for r in rows:
                r["_cat"], r["_state"] = log_state(r, r["id"] in with_buttons)
            cnt = {k: sum(1 for r in rows if r["_cat"] == k) for k in ("needs", "wait", "done", "info")}
            k1, k2, k3, k4 = st.columns(4)
            k1.metric("🔴 Needs action", cnt["needs"], help="An AM answered and the work is not done, work not started, or the e-mail failed")
            k2.metric("🟡 Waiting", cnt["wait"], help="Queued to send, waiting for an answer, or work in progress")
            k3.metric("🟢 Done", cnt["done"])
            k4.metric("⚪ Info", cnt["info"], help="Information only, test e-mails and skipped alerts")
            pick_cat = st.radio("Show", ["All", f"🔴 Needs action ({cnt['needs']})", f"🟡 Waiting ({cnt['wait']})", f"🟢 Done ({cnt['done']})", f"⚪ Info ({cnt['info']})"],
                                horizontal=True, key="log_cat", label_visibility="collapsed")
            want = {"🔴": "needs", "🟡": "wait", "🟢": "done", "⚪": "info"}.get(pick_cat[:1])
            rank = {"needs": 0, "wait": 1, "info": 2, "done": 3}
            rows = sorted([r for r in rows if want is None or r["_cat"] == want], key=lambda r: (rank[r["_cat"]], -r["id"]))
            last_note, replied_by = {}, {}
            for n in db.q("SELECT alert_id, source, author, text FROM alert_notes ORDER BY id"):
                who_ = n["author"] or {"answer": "AM", "email": "AM", "team": "Team"}.get(n["source"], "")
                last_note[n["alert_id"]] = f"{n['text']}  - {who_} ({n['source']})"
                if n["source"] == "email":
                    replied_by[n["alert_id"]] = who_
            # a "Manager response" row is only a notice that somebody answered: show WHAT was answered, and the manager's note only when there is one
            resp_ids = [int(str(r["dedupe_key"]).split(":")[1]) for r in rows if r["alert_type"] == "alert_response" and str(r["dedupe_key"]).startswith("resp")
                        and str(r["dedupe_key"]).split(":")[1].isdigit()]
            parents = {p["id"]: p for p in db.q_in("SELECT id, response_label, response_note, responded_by FROM alerts WHERE id IN :i", "i", resp_ids)} if resp_ids else {}
            def _resp_parent(r):
                k = str(r["dedupe_key"]).split(":")
                return parents.get(int(k[1])) if r["alert_type"] == "alert_response" and k[0].startswith("resp") and len(k) > 1 and k[1].isdigit() else None
            recs = [{"state": r["_state"], "id": r["id"], "code": f"#A{r['thread_id'] or r['id']}", "created": fmt(r["created_at"]),
                     "type": (chainlib_kind(r) + (" - email reply" if str(r["dedupe_key"]).startswith("resp-mail") else "")), "subject": r["subject"],
                     "team_status": cases.label(r["case_status"]),
                     "am_answer": (f"{r['response_label']} - {r['responded_by'] or 'name not given'}" if r["response_label"]
                                   else (f"{_resp_parent(r)['response_label']} - {_resp_parent(r)['responded_by'] or 'name not given'}" if _resp_parent(r) and _resp_parent(r)["response_label"] else "")),
                     "note_for_team": (r["response_note"] or (_resp_parent(r) or {}).get("response_note") or "") if r["alert_type"] != "alert_response" else ((_resp_parent(r) or {}).get("response_note") or ""), "latest_note": last_note.get(r["id"], ""), "add_note": "",
                     "answered_by": r["responded_by"] or (f"(email reply) {replied_by[r['id']]}" if r["id"] in replied_by else ""), "answered_at": fmt(r["responded_at"]) if r["responded_at"] else "",
                     "sent": r["status"], "to": r["to_emails"], "cc": r["cc_emails"], "error": r["last_error"] or "", "_cat": r["_cat"]} for r in rows]
            cols = ["state", "id", "code", "created", "type", "subject", "team_status", "am_answer", "note_for_team", "latest_note", "add_note",
                    "answered_by", "answered_at", "sent", "to", "cc", "error", "_cat"]
            df = frame(recs, cols)
            df, fsig = filter_bar("alertlog_f", df, skip=("subject", "note_for_team", "latest_note", "add_note", "answered_at", "to", "cc", "error", "state", "am_answer"))
            st.caption("🔴 **NEEDS ACTION** = an AM answered (do what the answer says), work not started, or the e-mail failed · "
                       "🟡 **WAITING** = queued, waiting for an answer, or work in progress · 🟢 **DONE** · ⚪ **INFO** only. "
                       "Red rows are sorted to the top.")
            st.caption("**Team status** is the dropdown in the table: change it (for example to *Done* once you did what the answer says), "
                       "type a note in **Add a note** if you like, then press **Save status / notes** once. Every change is kept in the history.")
            ver = st.session_state.get("logver", 0)
            cfg = {"_cat": None, "state": st.column_config.TextColumn("State", width="medium", help="What to do with this alert"),
                   "id": st.column_config.NumberColumn("#", width="small"),
                   "code": st.column_config.TextColumn("Code", help="The [#A..] code of the e-mail conversation. Follow-ups keep the code of the first alert."),
                   "created": st.column_config.TextColumn("Created"),
                   "type": st.column_config.TextColumn("What is this", help="New alert / Follow-up email / Manager response"),
                   "subject": st.column_config.TextColumn("Subject", width="large"),
                   "team_status": st.column_config.SelectboxColumn("Team status", options=[cases.STATUS_LABEL[s] for s in cases.STATUSES], width="medium"),
                   "am_answer": st.column_config.TextColumn("AM answer (button)"),
                   "note_for_team": st.column_config.TextColumn("Note for the team (from the answer page)", width="large"),
                   "latest_note": st.column_config.TextColumn("Latest note / email reply", width="large"),
                   "add_note": st.column_config.TextColumn("Add a note", width="large"),
                   "answered_by": st.column_config.TextColumn("Answered by"), "answered_at": st.column_config.TextColumn("Answered at"),
                   "sent": st.column_config.TextColumn("Email status"), "to": st.column_config.TextColumn("To"),
                   "cc": st.column_config.TextColumn("CC"), "error": st.column_config.TextColumn("Error")}
            order, cfg = view_controls("alertlog2", df, cfg)
            tint = {"needs": "rgba(255,75,75,0.20)", "wait": "rgba(255,196,0,0.18)", "done": "rgba(40,180,90,0.16)", "info": ""}
            try:
                shown = df.style.apply(lambda r: [f"background-color: {tint[r['_cat']]}" if tint[r["_cat"]] else ""] * len(r), axis=1) \
                                .map(lambda v: "font-weight: 700", subset=["state"])
            except Exception:
                shown = df
            with st.form("log_table_form"):
                try:
                    edited = st.data_editor(shown, key=f"ed_logtbl_{ver}_{fsig}", column_config=cfg, hide_index=True, num_rows="fixed", column_order=order,
                                            disabled=[c for c in cols if c not in ("team_status", "add_note")])
                except Exception:        # an older Streamlit that cannot colour the rows: show it plain
                    edited = st.data_editor(df, key=f"ed_logtbl_{ver}_{fsig}_plain", column_config=cfg, hide_index=True, num_rows="fixed", column_order=order,
                                            disabled=[c for c in cols if c not in ("team_status", "add_note")])
                who_t = st.text_input("Your name (saved with your changes)", key="log_who_table")
                go_t = st.form_submit_button("💾 Save status / notes")
            csv_button("alert_log", df)
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

            pick = st.selectbox("Open one alert (id)", [int(x) for x in df["id"]] or [r["id"] for r in rows], key="log_pick")
            a = db.q1("SELECT * FROM alerts WHERE id=:i", i=pick)
            if a:
                ans = ""
                if a["response_label"]:
                    ans = (f"\nANSWER: {a['response_label']}  (by {a['responded_by'] or '?'})"
                           + (f"\nNOTE FOR THE TEAM: {a['response_note']}" if a["response_note"] else ""))
                st.text(f"To: {a['to_emails']}\nCC: {a['cc_emails']}\nSubject: {a['subject']}\nEmail status: {a['status']}  {a['last_error'] or ''}{ans}\n\n{a['body']}")
                st.markdown("**History of this alert's whole conversation (every email, answer, reply and note - saved permanently)**")
                show_thread_history(pick, "pick")
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
        show_df("paused", pd.DataFrame(sn).rename(columns={"what": "paused", "campaign": "campaign name", "publisher": "publisher name"})
                [["paused", "campaign name", "publisher name", "until", "reason"]])
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
        recent = db.q("SELECT id, created_at, alert_type, subject, status FROM alerts ORDER BY id DESC LIMIT 5000")
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
        show_df("syncruns", d)


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
def page_tasks():
    import tasks
    import richnote
    flash("flash_tasks")
    now = db.now_utc()
    td = tasks.today(now)
    tzn = db.get_setting("tasks_timezone", "Asia/Dhaka")
    st.caption(f"Your own to-do list. It is **{tasks.local_now(now):%A %d %b %Y, %I:%M %p}** now ({tzn}). An e-mail is sent at each task's "
               "**alert date and time**. If you do not tick **Done**, a follow-up e-mail comes "
               f"{'every ' + str(tasks.followup_minutes()) + ' minutes' if tasks.followup_minutes() else '(follow-ups are OFF)'} until you do. "
               "The engine checks every 5 minutes, so an e-mail can be up to 5 minutes late.")
    opts = tasks.time_options()
    dflt = tasks.fmt_time(tasks.default_time())
    ver = st.session_state.get("task_ver", 0)
    note_key = f"task_note_{ver}"

    def draft_from_state():
        ss = st.session_state
        rep = ss.get("task_rep", "none")
        rule = {"pick_weekdays": ss.get("task_wdays"), "pick_monthdays": ss.get("task_mdays"), "pick_dates": ss.get("task_dates"),
                "slots": ss.get("task_slots")}.get(rep)
        return rep, rule, {"title": ss.get("task_title"), "notes": ss.get(note_key) or "", "work_date": ss.get("task_wd"),
                           "alert_date": ss.get("task_ad"), "alert_time": ss.get("task_at"), "repeat": rep, "cc_emails": ss.get("task_cc") or ""}

    def add_task_cb():
        ss = st.session_state
        rep, rule, d = draft_from_state()
        try:
            tid = tasks.add_task(d["title"], d["notes"], d["work_date"], d["alert_date"], rep, d["alert_time"],
                                 ss.get("task_rdays") if rep == "custom" else None, rule, cc_emails=d["cc_emails"])
            row = db.q1("SELECT * FROM tasks WHERE id=:i", i=tid)
            late = tasks.alert_dt(row) <= db.now_utc()
            first = tasks.pdate(row["alert_date"])
            set_flash("flash_tasks", "ok", "Task added. " + ("Its alert time has already passed, so the e-mail goes out within 5 minutes."
                      if late else f"First e-mail: {first:%a %d %b %Y} at {tasks.fmt_time(row['alert_time'])}.") +
                      (f" It repeats: {tasks.repeat_label(rep, ss.get('task_rdays'), row['repeat_rule'])}." if rep != "none" else ""))
            ss["task_title"] = ""
            ss["task_cc"] = ""
            ss["task_ver"] = ss.get("task_ver", 0) + 1          # a fresh, empty details box
            ss.pop("task_preview", None)
        except ValueError as e:
            ss["task_err"] = str(e)

    def preview_cb():
        ss = st.session_state
        ss["task_preview"] = draft_from_state()[2]

    err_msg = st.session_state.pop("task_err", None)
    if err_msg:
        st.error(err_msg)
    st.subheader("➕ New task")
    with st.form("task_add_form", border=True):
        st.text_input("Task subject (this becomes the e-mail subject)", key="task_title", placeholder="For example: Call Rehab buyer about the cap")
        st.markdown("**Details: what to do** (optional)")
        note_now = richnote.rich_note("", key=note_key, height=320)
        st.text_input("CC e-mails for this task (optional)", key="task_cc", placeholder="john@company.com, mary@company.com",
                      help="These people get a copy of this task's alert and follow-up e-mails. Separate with commas. You can change it later.")
        c1, c2, c3 = st.columns(3)
        c1.date_input("Work date (do it on)", td, key="task_wd")
        c2.date_input("Alert date (e-mail me on)", td, key="task_ad", help="Can be earlier than the work date.")
        c3.selectbox("Alert time (AM/PM)", opts, index=opts.index(dflt) if dflt in opts else 36, key="task_at")
        st.selectbox("Repeat", tasks.REPEATS, format_func=lambda r: tasks.REPEAT_LABEL[r], key="task_rep",
                     help="When you tick Done, a repeating task moves to its next date and the alert comes again.")
        with st.expander("Repeat options (only if you chose a repeat above)"):
            st.caption("Only the option that matches your **Repeat** choice is used.")
            st.text_area("Several alert times in a circle (choose Repeat = 'Several alert times in a circle')", key="task_slots",
                         value="Mon 10:00 AM\nFri 03:00 PM", height=110,
                         help="One alert time per line (or separated by commas): a weekday or a day of the month, then the time. "
                              "Examples: Mon 10:00 AM  |  Sun 10am  |  15 03:00 AM  |  last 09:00 PM. "
                              "When you tick Done, only the alert that just came moves on: Mon -> Fri -> Mon -> ... (a circle). "
                              "The Work date / Alert date / Alert time boxes above are not used for this repeat: the first alert is the next time of the circle.")
            r1, r2 = st.columns(2)
            r1.number_input("Every N days: how many days?", 1, 365, 7, key="task_rdays")
            r2.multiselect("Chosen weekdays: which days?", tasks.WD_NAMES, default=["Mon"], key="task_wdays",
                           help="Pick one or more. Example: Mon and Tue = every Monday and every Tuesday.")
            r3, r4 = st.columns(2)
            r3.multiselect("Chosen month days: which days?", tasks.MONTH_DAY_CHOICES, default=["15"], key="task_mdays",
                           help="Pick one or more, for example 15 and 20. 'last' = the last day of the month. A short month uses its last day.")
            r4.text_input("Chosen dates (YYYY-MM-DD, separated by commas)", key="task_dates", placeholder="2026-12-01, 2026-12-15",
                          help="Any dates you like. After the last one the task is finished.")
        b1, b2 = st.columns(2)
        b1.form_submit_button("👁 Preview e-mail", on_click=preview_cb)
        b2.form_submit_button("➕ Add task", type="primary", on_click=add_task_cb)
    pv = st.session_state.get("task_preview")
    if pv:
        pv = dict(pv)
        if not tasks.note_text(pv.get("notes")) and tasks.note_text(note_now):
            pv["notes"] = note_now                     # the box value that arrived with this page run
        subj, html_p, plain_p = tasks.preview_email([pv])
        with st.container(border=True):
            st.markdown("**👁 This is how your alert e-mail will look**")
            if not tasks.note_text(pv.get("notes")):
                st.warning("The Details box is EMPTY in this preview. If you wrote details, click inside the box, then press "
                           "**Preview e-mail** again. (If it is still empty, tell me.)")
            st.markdown(f"**Subject:** {subj}")
            mail_preview(html_p, plain_p)
            if st.button("Close preview", key="task_pv_close"):
                st.session_state.pop("task_preview", None)
                st.rerun()

    rows = tasks.open_tasks()
    state = {r["id"]: tasks.state_of(r, now) for r in rows}
    n_act = sum(1 for v in state.values() if v == "action")
    n_due = sum(1 for v in state.values() if v == "due")
    n_up = sum(1 for v in state.values() if v == "upcoming")
    done_today = db.q1("SELECT COUNT(*) AS n FROM task_log WHERE event='done' AND at >= :s", s=tasks.local_now(now).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc))["n"]
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("🔴 Action needed", n_act, help="Alert sent and not done yet, or the work date has passed")
    k2.metric("🟠 Due now", n_due, help="Alert time has come: the e-mail goes out within 5 minutes")
    k3.metric("⚪ Upcoming", n_up)
    k4.metric("✅ Done today", done_today)
    view = st.radio("Show", ["🔴 Action needed + due now", "⚪ Upcoming", "All open"], horizontal=True, key="task_view")
    shown = [r for r in rows if (view == "All open" or (view.startswith("⚪") and state[r["id"]] == "upcoming")
                                  or (view.startswith("🔴") and state[r["id"]] in ("action", "due")))]
    rank = {"action": 0, "due": 1, "upcoming": 2}
    shown.sort(key=lambda r: (rank[state[r["id"]]], tasks.alert_dt(r) or now))
    st.subheader(f"To do ({len(shown)})")
    if not shown:
        st.info("Nothing here." if rows else "Nothing open. Add your first task above.")
    else:
        df = pd.DataFrame([{"id": r["id"], "Done": False, "Status": tasks.STATE_LABEL[state[r["id"]]], "Task": r["title"], "Created": tasks.fmt_dt(r["created_at"]),
                            "Work date": tasks.pdate(r["work_date"]), "Alert date": tasks.pdate(r["alert_date"]),
                            "Alert time": tasks.fmt_time(r["alert_time"] or tasks.default_time()), "Repeat": tasks.REPEAT_SHORT[r["repeat"]],
                            "Repeat details": (str(r["repeat_days"] or "") if r["repeat"] == "custom" else tasks.rule_text(r["repeat"], r["repeat_rule"])),
                            "Next e-mail": tasks.next_alert_text(r, now), "CC": r.get("cc_emails") or "",
                            "Details": " / ".join(x.strip() for x in tasks.note_text(r).split("\n") if x.strip())[:3000], "Remove": False} for r in shown])
        df, sig = filter_bar("tasks", df, skip=("Task", "Details", "Next e-mail", "id", "Repeat details", "Created"))
        default_order = ["id", "Done", "Status", "Created", "Task", "Details", "Work date", "Alert date", "Alert time", "Repeat", "Repeat details", "Next e-mail", "CC", "Remove"]
        df = df[[c for c in default_order if c in df.columns]]          # starting order; your own saved order (below) wins
        cfg = {"id": None, "Done": st.column_config.CheckboxColumn("Done", width="small"),
               "Status": st.column_config.TextColumn("Status", width="medium"),
               "Task": st.column_config.TextColumn("Task subject"), "Created": st.column_config.TextColumn("Created (Dhaka)"),
               "Details": st.column_config.TextColumn("Details (double-click to read all)", width="large", help="Double-click a cell to read the full note. To change it, use Edit a task below."),
               "Work date": st.column_config.DateColumn("Work date", format="ddd DD MMM YYYY"),
               "Alert date": st.column_config.DateColumn("Alert date", format="ddd DD MMM YYYY"),
               "Alert time": st.column_config.SelectboxColumn("Alert time", options=opts, required=True),
               "Repeat": st.column_config.SelectboxColumn("Repeat", options=list(tasks.REPEAT_SHORT.values()), required=True),
               "Repeat details": st.column_config.TextColumn("Repeat details", help="Every N days: the number. Chosen weekdays: Mon, Tue. "
                                                             "Chosen month days: 15, 20, last. Chosen dates: 2026-12-01, 2026-12-15. "
                                                             "Several times (circle): Mon 10:00 AM, Fri 03:00 PM, 15 03:00 AM."),
               "CC": st.column_config.TextColumn("CC e-mails", help="Extra people who get this task's e-mails. Separate with commas."),
               "Remove": st.column_config.CheckboxColumn("Remove", width="small", help="Deletes the task (the history stays)")}
        order, cfg = view_controls("tasks", df, cfg)                     # same "Arrange columns / column width" box as the other tables
        styled = df.style.apply(lambda r: [{"🔴": "background-color: rgba(255,75,75,0.18)", "🟠": "background-color: rgba(255,170,0,0.18)"}
                                            .get(str(r["Status"])[:1], "")] * len(r), axis=1) if len(df) else df
        with st.form("task_edit_form", border=False):
            try:
                ed = st.data_editor(styled, hide_index=True, use_container_width=True, key=f"task_editor_{sig}",
                                    disabled=["id", "Status", "Next e-mail", "Created", "Details"], column_order=order, column_config=cfg)
            except Exception:
                ed = st.data_editor(df, hide_index=True, use_container_width=True, key=f"task_editor_{sig}_plain",
                                    disabled=["id", "Status", "Next e-mail", "Created", "Details"], column_order=order, column_config=cfg)
            save_tasks = st.form_submit_button("💾 Save changes", type="primary")
        csv_button("tasks", df)
        if save_tasks:
            done = changed = removed = 0
            errs = []
            back = {v: k for k, v in tasks.REPEAT_SHORT.items()}
            for i in range(len(df)):
                o, n = df.iloc[i], ed.iloc[i]
                tid = int(o["id"])
                if n["Remove"]:
                    tasks.delete_task(tid); removed += 1
                    continue
                rep_k, det = back.get(n["Repeat"], "none"), str(n["Repeat details"] or "").strip()
                try:
                    kw = {}
                    if rep_k == "custom":
                        if not det.isdigit() or int(det) < 1:
                            raise ValueError("Repeat details: type how many days, like 4.")
                        kw["repeat_days"] = int(det)
                    elif rep_k in tasks.PICK or rep_k == "slots":
                        kw["repeat_rule"] = det
                    if tasks.update_task(tid, title=n["Task"],
                                         work_date=n["Work date"] if pd.notna(n["Work date"]) else None,
                                         alert_date=n["Alert date"] if pd.notna(n["Alert date"]) else None,
                                         alert_time=n["Alert time"], repeat=rep_k,
                                         cc_emails=str(n["CC"] or ""), **kw):
                        changed += 1
                except ValueError as e:
                    errs.append(f"{n['Task']}: {e}")
                    continue
                if n["Done"]:
                    tasks.mark_done(tid); done += 1
            if errs:
                st.session_state["task_err"] = "Not saved: " + " | ".join(errs)
            set_flash("flash_tasks", "ok", f"Saved. Done: {done}, changed: {changed}, removed: {removed}.")
            st.rerun()
        st.caption("Tick **Done** and press **Save changes**. You can also change the subject, dates, time and repeat in the table. "
                   "Changing the alert date or time makes the alert start again. To change the details text, use **Edit a task** below.")

    with st.expander("✏️ Edit a task (subject and details with bold, bullets, tick boxes, colours)"):
        if not rows:
            st.write("No open tasks.")
        else:
            pick = st.selectbox("Which task?", [r["id"] for r in rows], format_func=lambda i: next(r["title"] for r in rows if r["id"] == i), key="task_edit_pick")
            cur = next(r for r in rows if r["id"] == pick)
            ev = st.session_state.get("task_edit_ver", 0)
            init = tasks.note_html(cur) if tasks.note_text(cur) else ""
            nk = f"task_edit_note_{pick}_{ev}"

            def edit_cb():
                ss = st.session_state
                try:
                    tasks.update_task(pick, title=ss.get(f"task_edit_title_{pick}_{ev}"), notes=ss.get(nk) or "",
                                      cc_emails=ss.get(f"task_edit_cc_{pick}_{ev}") or "")
                    set_flash("flash_tasks", "ok", "Task saved.")
                    ss["task_edit_ver"] = ev + 1
                    ss.pop("task_edit_preview", None)
                except ValueError as e:
                    ss["task_err"] = str(e)

            def edit_pv_cb():
                ss = st.session_state
                ss["task_edit_preview"] = {"title": ss.get(f"task_edit_title_{pick}_{ev}"), "notes": ss.get(nk) or "", "work_date": cur["work_date"],
                                           "alert_date": cur["alert_date"], "alert_time": cur["alert_time"], "repeat": cur["repeat"]}

            with st.form(f"task_edit_one_{pick}_{ev}", border=False):
                st.text_input("Task subject", cur["title"], key=f"task_edit_title_{pick}_{ev}")
                st.text_input("CC e-mails for this task (empty = nobody)", cur.get("cc_emails") or "", key=f"task_edit_cc_{pick}_{ev}",
                              help="Separate with commas. Applies to the next alert and follow-ups of this task.")
                note_now_e = richnote.rich_note(init, key=nk, height=320)
                e1, e2 = st.columns(2)
                e1.form_submit_button("👁 Preview e-mail", on_click=edit_pv_cb)
                e2.form_submit_button("💾 Save this task", type="primary", on_click=edit_cb)
            ep = st.session_state.get("task_edit_preview")
            if ep:
                ep = dict(ep)
                if not tasks.note_text(ep.get("notes")) and tasks.note_text(note_now_e):
                    ep["notes"] = note_now_e
                subj, html_p, plain_p = tasks.preview_email([ep])
                with st.container(border=True):
                    if not tasks.note_text(ep.get("notes")):
                        st.warning("The Details box is EMPTY in this preview. Click inside the box and press Preview again.")
                    st.markdown(f"**Subject:** {subj}")
                    mail_preview(html_p, plain_p)

    with st.expander("✅ Done recently (tick Reopen to bring a task back)"):
        dn = db.q("SELECT id, title, done_at FROM tasks WHERE status='done' ORDER BY done_at DESC LIMIT 5000")
        if dn:
            ddf = pd.DataFrame([{"id": r["id"], "Reopen": False, "Task": r["title"], "Done at": tasks.fmt_dt(r["done_at"])} for r in dn])
            with st.form("task_reopen_form", border=False):
                ded = st.data_editor(ddf, hide_index=True, use_container_width=True, key="task_done_editor", disabled=["id", "Task", "Done at"],
                                     column_config={"id": None})
                reopen_go = st.form_submit_button("Reopen ticked tasks")
            if reopen_go:
                for i in range(len(ddf)):
                    if ded.iloc[i]["Reopen"]:
                        tasks.reopen(int(ddf.iloc[i]["id"]))
                st.rerun()
        else:
            st.write("No finished tasks yet. (Repeating tasks do not appear here: they move to their next date. See History.)")

    with st.expander("🕘 History (when tasks were created, alerted, changed or done)"):
        lg = db.q("SELECT at, task_title, event, detail FROM task_log ORDER BY id DESC LIMIT 5000")
        hdf = frame([{"When": tasks.fmt_dt(r["at"]), "Task": r["task_title"], "What happened": r["event"], "Details": r["detail"]} for r in lg],
                    ["When", "Task", "What happened", "Details"])
        show_df("taskhist", hdf)

    with st.expander("⏰ E-mail settings (alert time, follow-ups, quiet hours)"):
        qf, qt = tasks.fmt_time(db.get_setting("tasks_quiet_from", "23:00")), tasks.fmt_time(db.get_setting("tasks_quiet_to", "06:00"))
        with st.form("task_settings"):
            c1, c2, c3 = st.columns(3)
            dt_ = c1.selectbox("Default alert time", opts, index=opts.index(dflt) if dflt in opts else 36, key="tk_dt")
            fm = c2.number_input("Follow-up every (minutes, 0 = never)", 0, 1440, tasks.followup_minutes(), key="tk_fm")
            fx = c3.number_input("Follow-ups per alert (0 = no limit)", 0, 200, tasks.followup_max(), key="tk_fx")
            c4, c5, c6 = st.columns(3)
            q1_ = c4.selectbox("No follow-ups from", opts, index=opts.index(qf) if qf in opts else 92, key="tk_q1")
            q2_ = c5.selectbox("...until", opts, index=opts.index(qt) if qt in opts else 24, key="tk_q2")
            tz_ = c6.text_input("Your time zone", tzn, key="tk_tz")
            st.markdown("**Optional morning summary** (one e-mail with your whole list, in addition to the alerts above)")
            c7, c8, c9 = st.columns(3)
            on = c7.checkbox("Send the morning summary", value=str(db.get_setting("tasks_digest_on", "false")).lower() == "true", key="tk_on")
            tm = c8.selectbox("Summary time", opts, index=opts.index(tasks.fmt_time(db.get_setting("tasks_send_time", "08:00"))) if tasks.fmt_time(db.get_setting("tasks_send_time", "08:00")) in opts else 32, key="tk_time")
            wk = c9.checkbox("Also on Saturday and Sunday", value=str(db.get_setting("tasks_weekends", "false")).lower() == "true", key="tk_wk")
            if st.form_submit_button("💾 Save"):
                try:
                    ZoneInfo(tz_)
                except Exception:
                    st.error("Time zone must look like Asia/Dhaka.")
                else:
                    db.set_setting("tasks_default_time", tasks.parse_time(dt_))
                    db.set_setting("tasks_followup_minutes", str(int(fm)))
                    db.set_setting("tasks_followup_max", str(int(fx)))
                    db.set_setting("tasks_quiet_from", tasks.parse_time(q1_))
                    db.set_setting("tasks_quiet_to", tasks.parse_time(q2_))
                    db.set_setting("tasks_timezone", tz_)
                    db.set_setting("tasks_digest_on", "true" if on else "false")
                    db.set_setting("tasks_send_time", tasks.parse_time(tm))
                    db.set_setting("tasks_weekends", "true" if wk else "false")
                    set_flash("flash_tasks", "ok", "Saved.")
                    st.rerun()
        st.caption("E-mails go to the admin e-mail address(es) in Settings. You can change the wording on the Email templates tab "
                   "(\"My tasks: alert and follow-up\"). First alerts are always sent at their time; only follow-ups wait for quiet hours to end.")
        if st.button("📧 Check and send task e-mails now", key="task_now"):
            a, f = tasks.run_alerts()
            res = mailer.process_outbox() if (a or f) else None
            st.success(f"{a} task(s) alerted, {f} follow-up(s). " + (f"E-mail: {res}" if res else "Nothing is due right now."))


def page_cap_overview():
    import capview
    import targets as tg
    now = db.now_utc()
    st.caption("One place to see **every cap**: the TOTAL cap of each publisher (only publishers with an active DID) and the GLOBAL daily / monthly cap of "
               "each active buyer target, with how much is used. Caps are set on the Caps tab (publisher caps and target caps are read from Ringba automatically).")
    pub_tab, tgt_tab = st.tabs(["🧑‍💼 Publishers (active DID)", "🎯 Buyer targets (active)"])
    prog = lambda label: st.column_config.ProgressColumn(label, min_value=0, max_value=100, format="%.0f%%")
    with pub_tab:
        rows, detail = capview.publisher_caps(now)
        with_cap = [r for r in rows if r["has_cap"]]
        k1, k2, k3 = st.columns(3)
        k1.metric("Publishers with an active DID", len(rows))
        k2.metric("...of them with a cap", len(with_cap))
        k3.metric("Without any cap", len(rows) - len(with_cap))
        if not rows:
            st.info("No publisher has an active DID yet. Press 'Read DIDs from Ringba' on the Sync & Import tab.")
        else:
            st.markdown("**Total cap per publisher** (sum of its campaign caps)")
            inc = st.checkbox("Also show publishers without a cap", key="ov_pub_nocap")
            df = pd.DataFrame([{k: v for k, v in r.items() if k != "has_cap"} for r in (rows if inc else with_cap)])
            if df.empty:
                st.info("No publisher cap yet. On the Caps tab press 'Read publisher caps from Ringba now'.")
            else:
                show_df("ov_pub", df, column_config={
                    "publisher": "Publisher", "active_dids": st.column_config.NumberColumn("Active DIDs"),
                    "campaigns_with_cap": st.column_config.NumberColumn("Campaigns with a cap"),
                    "daily_cap_total": st.column_config.NumberColumn("Daily cap (total)"), "used_today": st.column_config.NumberColumn("Used today"),
                    "today_pct": prog("Today %"), "monthly_cap_total": st.column_config.NumberColumn("Monthly cap (total)"),
                    "used_month": st.column_config.NumberColumn("Used this month"), "month_pct": prog("Month %"),
                    "all_campaigns_daily_cap": st.column_config.NumberColumn("Daily cap, all campaigns", help="A cap on the publisher as a whole (not added to the total)"),
                    "all_campaigns_daily_used": st.column_config.NumberColumn("Used today (all)"),
                    "all_campaigns_monthly_cap": st.column_config.NumberColumn("Monthly cap, all campaigns"),
                    "all_campaigns_monthly_used": st.column_config.NumberColumn("Used this month (all)")})
            with st.expander(f"Every publisher cap by campaign ({len(detail)})", expanded=False):
                if detail:
                    show_df("ov_pub_detail", pd.DataFrame(detail).drop(columns=["pid"]), column_config={
                        "publisher": "Publisher", "campaign": "Campaign", "period": "Period", "counts": "Counts", "cap": st.column_config.NumberColumn("Cap"),
                        "used": st.column_config.NumberColumn("Used"), "pct": prog("Used %"), "source": "Cap from"})
                else:
                    st.caption("No caps.")
    with tgt_tab:
        all_t = capview.target_caps(include_disabled=True)
        trows = [r for r in all_t if r["enabled"]]
        ok_t, err = db.get_setting("targets_last_ok", ""), db.get_setting("targets_last_error", "")
        c1, c2 = st.columns([1, 3])
        if c1.button("🔄 Read targets from Ringba now", key="ov_tg_sync"):
            try:
                rc.set_deadline(240)
                r = tg.run(force=True)
                d = r["definitions"]
                msg = f"Ringba has {d['in_ringba']} targets: {d['report']['with_daily_cap']} with a daily cap, {d['report']['with_monthly_cap']} with a monthly cap, {d['report'].get('with_global_cap', 0)} with a global cap."
                if d.get("partial"):
                    msg += f" {d['unchecked']} targets are still waiting; they are read on the next 5-minute run (or press the button again)."
                set_flash("flash_ov", "ok", msg)
            except Exception as e:
                set_flash("flash_ov", "err", f"Could not read targets: {e}")
            finally:
                rc.set_deadline(None)
            st.rerun()
        c2.caption((f"Last successful read: {ago(ok_t)}. " if ok_t else "Not read yet. ") + (f"Last problem: {err}" if err else ""))
        flash("flash_ov")
        with st.expander("🔎 Check one target (shows what Ringba returns for its caps)"):
            with st.form("ov_tg_check"):
                tid = st.text_input("Target ID", placeholder="TAcad4568899b34ccca7bace74eca7b3f4", key="ov_tg_id")
                go_c = st.form_submit_button("Check")
            if go_c and tid.strip():
                try:
                    rc.set_deadline(60)
                    dbg = rc.target_cap_debug(tid.strip())
                    st.json(dbg)
                    st.caption("Copy everything above and send it to me if the caps are not read correctly.")
                except Exception as e:
                    st.error(f"Could not read it: {e}")
                finally:
                    rc.set_deadline(None)
        with_cap = [r for r in trows if r["has_cap"]]
        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Targets saved", len(all_t))
        k2.metric("Active targets", len(trows))
        k3.metric("...with a daily or monthly cap", len(with_cap))
        k4.metric("Disabled in Ringba", len(all_t) - len(trows))
        if not all_t:
            st.info("No target is saved yet. Press the button above to read the targets from Ringba.")
        else:
            st.markdown("**Global cap of each active target** (the cap on the whole target, whoever sends the calls)")
            o1, o2 = st.columns(2)
            inc = o1.checkbox("Also show targets without a cap", key="ov_tg_nocap")
            inc_off = o2.checkbox("Also show disabled targets", key="ov_tg_off")
            base = all_t if inc_off else trows
            df = pd.DataFrame([{k: v for k, v in r.items() if k not in ("has_cap",)} for r in (base if inc else [r for r in base if r["has_cap"]])])
            if df.empty:
                st.info("None of the targets shown has a daily or monthly cap saved. If a target has a cap in Ringba, use "
                        "'Check one target' above with its ID and send me what it shows.")
            else:
                df["usage_read"] = df["usage_read"].map(lambda v: ago(v) if v else "never")
                show_df("ov_tg", df.sort_values(["today_pct", "month_pct", "global_pct"], ascending=False, na_position="last"), column_config={
                    "buyer": "Buyer", "target": "Target", "target_id": "Target ID", "enabled": st.column_config.CheckboxColumn("Enabled in Ringba"),
                    "daily_cap": st.column_config.NumberColumn("Daily cap"),
                    "used_today": st.column_config.NumberColumn("Used today"), "today_pct": prog("Today %"),
                    "monthly_cap": st.column_config.NumberColumn("Monthly cap"), "used_month": st.column_config.NumberColumn("Used this month"),
                    "month_pct": prog("Month %"), "global_cap": st.column_config.NumberColumn("Global cap"), "used_global": st.column_config.NumberColumn("Used (global)"),
                    "global_pct": prog("Global %"), "alerts_on": st.column_config.CheckboxColumn("Alerts on"), "usage_read": "Usage read"})
            bt = capview.buyer_totals(trows)
            with st.expander("Total per buyer (sum of its target caps)"):
                show_df("ov_buyers", pd.DataFrame(bt), column_config={
                    "buyer": "Buyer", "active_targets": "Active targets", "targets_with_cap": "Targets with a cap",
                    "daily_cap_total": "Daily cap (total)", "used_today": "Used today", "today_pct": prog("Today %"),
                    "monthly_cap_total": "Monthly cap (total)", "used_month": "Used this month", "month_pct": prog("Month %")})


def page_followups():
    """Upcoming DID follow-ups: which DID will get another email, when, and which ones the engine will skip."""
    import followups
    now = db.now_utc()
    st.caption("Every DID alert that will send **another email** (a 3-day reminder when nobody answered, or a follow-up when a 'wait N days' pause ends, "
               "or a repeat alert for a new DID without calls). Before each of these emails the engine checks Ringba: a DID that was deleted or retired "
               "gets **no email**. Nothing is ever deleted here: answers and history stay.")
    try:
        rows = followups.upcoming(now)
    except Exception as e:
        st.error(f"Could not build the report: {e}")
        return
    is_yes = lambda r: r["will_send"] == "Yes" or r["will_send"].startswith("Only if")
    will = [r for r in rows if is_yes(r)]
    paused = [r for r in rows if r.get("paused_until") and is_yes(r)]
    due = [r for r in will if r["next_followup"] and r["next_followup"] <= now + timedelta(days=1) and not r.get("paused_until")]
    notsent = [r for r in rows if not is_yes(r)]
    live = db.get_setting("numbers_live_checked_at") or db.get_setting("numbers_last_ok")
    # ---- checks: mistakes must never go unseen
    try:
        probs = followups.health(now)
    except Exception as e:
        probs = [{"level": "bad", "title": "The checks could not run", "detail": str(e)[:200]}]
    bad = [p for p in probs if p["level"] == "bad"]
    warn = [p for p in probs if p["level"] == "warn"]
    head = "🔴 " + f"{len(bad)} problem(s) need your attention" if bad else ("🟡 " + f"{len(warn)} thing(s) to look at" if warn else "🟢 All checks are fine")
    with st.expander(f"🩺 Checks: {head}", expanded=bool(bad)):
        for p in probs:
            icon = {"bad": "🔴", "warn": "🟡", "ok": "🟢"}[p["level"]]
            st.markdown(f"{icon} **{p['title']}**" + (f"  \n<small>{p['detail']}</small>" if p["detail"] else ""), unsafe_allow_html=True)
        st.caption("These checks run every time you open this tab. A red or yellow line means something may not work the way your team expects.")
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("🔴 Due in the next 24 hours", len(due), help="Emails the engine will send within 24 hours (or should have sent already). Pick 'Due in 24 hours' below to see them.")
    k2.metric("⏸️ Paused by a manager", len(paused), help="An affiliate manager asked for more time. Nothing is sent until the pause ends; then a follow-up goes out ONLY if the DID still has no calls.")
    k3.metric("🟢 Scheduled later", len(will) - len(due) - len(paused))
    k4.metric("⛔ Will not be sent / nothing scheduled", len(notsent), help="Retired DIDs, DIDs that got a call, or answered with no pause.")
    st.caption(f"Last live check of Ringba: {ago(live) if live else 'never'}. Times are shown in EST.")
    up_tab, hist_tab = st.tabs(["📅 Upcoming follow-ups", "🕘 Previous follow-ups"])
    with up_tab:
        views = {"🔴 Due in 24 hours": due, "⏸️ Paused by a manager": paused, "📋 Everything": rows, "⛔ Will not be sent": notsent}
        names = [f"{k} ({len(v)})" for k, v in views.items()]
        if "fu_view" not in st.session_state:
            st.session_state["fu_view"] = names[0] if due else names[2]
        if st.session_state["fu_view"] not in names:            # the number in the name changed: keep the same view
            st.session_state["fu_view"] = next((n for n in names if n.split(" (")[0] == str(st.session_state["fu_view"]).split(" (")[0]), names[2])
        view = st.radio("Show", names, horizontal=True, key="fu_view")
        shown = list(views.values())[names.index(view)]
        shown = sorted(shown, key=lambda r: (r["next_followup"] is None, r["next_followup"] or now))
        if not shown:
            st.success("Nothing here. 🎉" if view.startswith("🔴") else "Nothing in this list right now.")
        else:
            recs = [{"status": r["status"], "next_email": fmt(r["next_followup"]) if r["next_followup"] else "", "did": r["did"], "label": r["label"],
                     "publisher": r["publisher"], "campaign": r["campaign"], "what_happens": r["why"],
                     "previous_response": r["previous_response"], "answered_by": r["responded_by"],
                     "answered_at": fmt(r["responded_at"]) if r["responded_at"] else "", "note": r["response_note"],
                     "paused_days": r["snoozed_days"], "last_email": fmt(r["last_email"]) if r["last_email"] else "",
                     "followups_sent": r["followups_sent"], "code": r["code"], "type": r["kind"], "did_status": r["did_status"]} for r in shown]
            df = pd.DataFrame(recs)
            show_df("followups", df, column_config={
                "status": st.column_config.TextColumn("What is happening", width="large", help="🔴 due / overdue · 🟡 within 24 hours · 🟢 later · ⏸️ paused by a manager · ⛔ will not be sent"),
                "next_email": "Next email (EST)", "did": "DID", "label": "Label", "publisher": "Publisher", "campaign": "Campaign",
                "what_happens": st.column_config.TextColumn("What happens", width="large"),
                "previous_response": st.column_config.TextColumn("AM's previous response", width="large"), "answered_by": "Answered by",
                "answered_at": "Answered at", "note": "AM's note", "paused_days": st.column_config.NumberColumn("Paused for (days)", help="How many days the previous answer paused this DID"),
                "last_email": "Last email sent", "followups_sent": st.column_config.NumberColumn("Follow-ups so far", help="Reminders / follow-ups already sent for this idle period"),
                "code": st.column_config.TextColumn("Alert code", help="The [#A..] code in the e-mail subject. Search it in the Alert Log to see the whole history."),
                "type": "Alert type", "did_status": "Current DID status"})
            st.markdown("**Preview of the next email**")
            labels = [f"{r['did']}  {('(' + r['label'] + ') ') if r['label'] else ''}- {fmt(r['next_followup']) if r['next_followup'] else 'no date'}" for r in shown]
            pick = st.selectbox("Choose a DID", range(len(shown)), format_func=lambda i: labels[i], key="fu_pick")
            row = shown[min(pick, len(shown) - 1)]
            st.info(row["status"])
            try:
                subject, text, html = followups.preview(row, now)
                st.markdown(f"**Subject:** {subject}")
                mail_preview(html, text)
                st.caption("The preview uses today's data. The real email is made when it is due, with the numbers of that day.")
            except Exception as e:
                st.error(f"Could not build the preview: {e}")
    with hist_tab:
        try:
            hist = followups.history()
        except Exception as e:
            hist, _e = [], e
            st.error(f"Could not read the history: {e}")
        if not hist:
            st.info("No reminder or follow-up has been made yet.")
        else:
            hdf = pd.DataFrame([{"when": fmt(r["when"]), "code": r["code"], "kind": r["kind"], "did": r["did"], "label": r["label"], "publisher": r["publisher"],
                                 "email": r["email_status"], "response": r["response"], "team_status": cases.label(r["case"]), "did_now": r["did_now"],
                                 "note": r["note"]} for r in hist])
            show_df("followups_hist", hdf, column_config={"when": "When", "code": "Alert code", "kind": "What", "did": "DID", "label": "Label", "publisher": "Publisher",
                                                            "email": "Email status", "response": "AM's response", "team_status": "Team status",
                                                            "did_now": "DID status now", "note": "Note"})


def tasks_tz_now():
    """Current time in the tasks time zone (Dhaka by default), AM/PM, for the 'Page loaded' line."""
    import tasks
    return tasks.local_now().strftime("%a %d %b %Y, %I:%M:%S %p") + f" ({db.get_setting('tasks_timezone', 'Asia/Dhaka')})"


def page_schedule():
    """One place to see everything that is coming (tasks, task follow-ups, DID follow-ups) and what already happened."""
    import followups
    import tasks
    now = db.now_utc()
    ln = tasks.local_now(now)
    st.caption(f"Everything that will be e-mailed to you, so you can be ready before it comes. It is **{ln:%A %d %b %Y, %I:%M %p}** now "
               f"({db.get_setting('tasks_timezone', 'Asia/Dhaka')}). Task follow-ups come every {tasks.followup_minutes()} minutes until you tick Done "
               "(change it in My Tasks > E-mail settings).")
    c1, c2 = st.columns([1, 3])
    days = c1.selectbox("Look ahead", [1, 3, 7, 14, 30], index=2, format_func=lambda d: f"{d} day(s)", key="sched_days")
    rows = [{"when": r["when"], "type": r["kind"], "what": r["title"], "details": r["detail"], "code": f"task #{r['task_id']}"}
            for r in tasks.upcoming_alerts(now, days=days)]
    try:
        for r in followups.upcoming(now):
            if r.get("scheduled") and r["next_followup"] and r["will_send"] == "Yes" and r["next_followup"] <= now + timedelta(days=days):
                rows.append({"when": r["next_followup"].astimezone(tasks.local_tz()), "type": "DID follow-up: " + r["kind"],
                             "what": f"{r['did']} - {r['publisher']}", "details": r["why"], "code": r.get("code", "")})
    except Exception as e:
        st.warning(f"The DID follow-ups could not be loaded now: {str(e)[:120]}")
    rows.sort(key=lambda r: r["when"])
    end_today = ln.replace(hour=23, minute=59, second=59)
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("In the next hour", sum(1 for r in rows if r["when"] <= ln + timedelta(hours=1)))
    k2.metric("Today", sum(1 for r in rows if r["when"] <= end_today))
    k3.metric("Next 24 hours", sum(1 for r in rows if r["when"] <= ln + timedelta(hours=24)))
    k4.metric(f"Next {days} day(s)", len(rows))
    t_up, t_prev = st.tabs(["⏭️ Upcoming alerts", "🕘 Previous (what already happened)"])
    with t_up:
        if not rows:
            st.info("Nothing is scheduled in this time.")
        else:
            def _in(w):
                m = int((w - ln).total_seconds() // 60)
                if m <= 0:
                    return "now / next run"
                return (f"{m // 1440}d " if m >= 1440 else "") + (f"{(m % 1440) // 60}h " if m >= 60 else "") + f"{m % 60}m"
            df = pd.DataFrame([{"when": r["when"].strftime("%a %d %b %Y, %I:%M %p"), "in": _in(r["when"]), "type": r["type"], "what": r["what"],
                                "details": r["details"], "code": r["code"]} for r in rows])
            show_df("sched_up", df, column_config={"when": "When (your time)", "in": "Comes in", "type": "Type", "what": st.column_config.TextColumn("What", width="large"),
                                                   "details": st.column_config.TextColumn("Details", width="large"), "code": "Task / alert code"})
    with t_prev:
        h = tasks.history(300)
        try:
            for r in followups.history(100):
                h.append({"when": r["when"], "task": f"{r['did']}", "event": "DID e-mail: " + r["kind"], "detail": f"{r['code']} - {r.get('response') or r.get('email_status') or ''}", "task_id": ""})
        except Exception:
            pass
        h.sort(key=lambda r: r["when"] or now, reverse=True)
        if not h:
            st.info("Nothing yet.")
        else:
            df = pd.DataFrame([{"when": tasks.fmt_dt(r["when"]), "task": r["task"], "event": r["event"], "details": r["detail"]} for r in h])
            show_df("sched_prev", df, column_config={"when": "When (your time)", "task": st.column_config.TextColumn("Task / DID", width="large"),
                                                     "event": "What happened", "details": st.column_config.TextColumn("Details", width="large")})


PAGES = {"📊 Dashboard": page_dashboard, "🎛️ Cap overview": page_cap_overview, "📅 Follow-ups": page_followups, "🗓️ Schedule": page_schedule, "📑 Reports": page_reports, "🧑‍💼 Publishers": page_publishers,
         "🏢 Buyers": page_buyers, "📞 DIDs": page_dids, "🚦 Caps": page_caps, "✅ My Tasks": page_tasks, "✉️ Emails": page_emails,
         "📝 Email templates": page_templates, "📜 Alert Log": page_log, "🔄 Sync & Import": page_sync,
         "⚙️ Settings": page_settings}

if mailer.is_dry_run():
    st.warning("🧪 DRY RUN is ON: alerts are created but emails are NOT really sent. Turn it off in Settings when you are ready.")
st.title("📞 Pay-Per-Call Alert Center")
mc, rc_ = st.columns([8, 1])
page = mc.radio("Menu", list(PAGES), horizontal=True, label_visibility="collapsed", key="page")
if rc_.button("🔄 Refresh", key="refresh_page", help="Reload this page with the newest data", use_container_width=True):
    try:
        db._memo.clear()
        st.cache_data.clear()
    except Exception:
        pass
    st.rerun()
st.caption(f"Page loaded {tasks_tz_now()}")
try:
    PAGES[page]()
except Exception as e:  # show errors on screen instead of a blank page
    st.error(f"Something went wrong on this page: {e}")
    st.exception(e)
