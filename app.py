"""Pay-Per-Call Alert Center  -  the screen you work in."""
import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

st.set_page_config(page_title="Pay-Per-Call Alert Center", page_icon="📞", layout="wide")

import config

if not config.DB_URL:
    st.error("DATABASE_URL is missing. In Streamlit Cloud open: App → Settings → Secrets, and add it "
             "(see SETUP_GUIDE.md, Step 5).")
    st.stop()

import alerts
import db
import engine
import ingest
import mailer
import reports
import ringba_client as rc
import rules
import setup_db
from timeutil import fmt, tz, window_start

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


# ------------------------------------------------------------------ first-run database setup
def schema_ready():
    try:
        db.q1("SELECT call_id FROM calls LIMIT 1")
        db.q1("SELECT alert_type FROM alert_rules LIMIT 1")
        db.q1("SELECT id FROM caps LIMIT 1")
        return True
    except Exception:
        return False


if not schema_ready():
    st.title("📞 Pay-Per-Call Alert Center")
    st.info("First time here (or the database needs the new layout). Press the button once. "
            "Your old tables are NOT deleted: they are kept as a backup named old_*, and your publisher / buyer "
            "emails, watchlist, caps and admin email are copied across.")
    if st.button("🛠️ Set up / upgrade the database now", type="primary"):
        logs = []
        try:
            setup_db.run(log=logs.append)
            st.code("\n".join(logs))
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
        label = disp.get("name") or disp.get("number") or disp.get("email") or str(pkv or "new row")
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
                done["errors"].append(f"{orig_rows[pkv].get('name') or pkv}: cannot delete ({str(e)[:80]}) - set status instead")
    return done


def table_editor(name, df, column_config, display_cols, table, transform=None, required=(), allow_delete=False,
                 dynamic=True, disabled=(), pk="id", insert_extra=None):
    msg = st.session_state.pop(f"msg_{name}", None)
    if msg:
        parts = [f"{msg['added']} added", f"{msg['changed']} changed"] + ([f"{msg['deleted']} deleted"] if msg["deleted"] else [])
        (st.success if not msg["errors"] else st.warning)("Saved: " + ", ".join(parts))
        for e in msg["errors"]:
            st.error(e)
    ver = st.session_state.get(f"ver_{name}", 0)
    edited = st.data_editor(df, key=f"ed_{name}_{ver}", column_config=column_config, hide_index=True,
                            num_rows="dynamic" if dynamic else "fixed", disabled=list(disabled))
    if st.button("💾 Save changes", key=f"save_{name}"):
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


# ------------------------------------------------------------------ banner
if mailer.is_dry_run():
    st.warning("🧪 DRY RUN is ON: alerts are created but emails are NOT really sent. "
               "Turn it off in the Settings tab when you are ready.")

st.title("📞 Pay-Per-Call Alert Center")
tabs = st.tabs(["📊 Dashboard", "📑 Reports", "🧑‍💼 Publishers", "🏢 Buyers", "📞 DIDs", "🚦 Caps",
                "✉️ Emails & Alerts", "📜 Alert Log", "🔄 Sync & Import", "⚙️ Settings"])


# ================================================================== DASHBOARD
def tab_dashboard():
    now = db.now_utc()
    last = db.q1("SELECT * FROM sync_runs WHERE kind='live' ORDER BY id DESC LIMIT 1")
    ok_run = db.q1("SELECT MAX(finished_at) AS t FROM sync_runs WHERE kind='live' AND status='ok'")
    ok_t = db.to_dt(ok_run["t"]) if ok_run and ok_run["t"] else None
    c1, c2, c3 = st.columns(3)
    c1.metric("Last good sync", ago(ok_t))
    if ok_t is None:
        st.info("The live engine has not run yet. After setup, GitHub Actions runs it every 5 minutes "
                "(or press 'Run engine now' in Sync & Import).")
    elif (now - ok_t) > timedelta(minutes=30):
        st.error(f"⚠️ No successful sync for {int((now - ok_t).total_seconds() // 60)} minutes. Check GitHub Actions.")
    if last and last["status"] == "error":
        st.error(f"Last sync error: {last['error']}")
    first, lastcall, n = reports.coverage()
    c2.metric("Calls stored", f"{n:,}")
    c3.metric("History starts", fmt(first, False) if first else "-")

    st.subheader("Calls")
    cols = st.columns(3)
    for col, preset in zip(cols, ["Today", "Yesterday", "Last 7 days"]):
        a, b = reports.preset_range(preset)
        s, e = reports.utc_bounds(a, b)
        sm = reports.summary(reports.load_calls(s, e))
        col.markdown(f"**{preset}**")
        col.metric("Calls", sm["Calls"])
        col.caption(f"Connected {sm['Connected']} · Converted {sm['Converted']} · Paid {sm['Paid']}")

    st.subheader("Caps right now")
    caps = db.q("SELECT * FROM caps WHERE active = TRUE")
    if not caps:
        st.caption("No caps yet. Add them in the Caps tab.")
    else:
        out = []
        for c in caps:
            u = rules.cap_usage(c, now)
            out.append({"Type": c["entity_type"], "Name": u["name"], "Period": c["period"],
                        "Counts": c["metric"], "Used": u["usage"], "Cap": c["cap_value"], "Percent": round(u["pct"], 1)})
        d = pd.DataFrame(out).sort_values("Percent", ascending=False)
        st.dataframe(d, hide_index=True)

    st.subheader("Latest alerts")
    rows = db.q("SELECT created_at, alert_type, subject, status FROM alerts WHERE status<>'skipped' ORDER BY id DESC LIMIT 15")
    if rows:
        d = pd.DataFrame(rows)
        d["created_at"] = d["created_at"].map(fmt)
        st.dataframe(d, hide_index=True)
    else:
        st.caption("No alerts yet.")


# ================================================================== REPORTS
def tab_reports():
    st.caption("Reads from your stored calls. Times are in your timezone (Settings).")
    c1, c2, c3 = st.columns([2, 2, 2])
    preset = c1.selectbox("Date range", reports.PRESETS, index=0, key="rep_preset")
    if preset == "Custom range":
        t = reports.today_local()
        rng = c2.date_input("From – To", value=(t - timedelta(days=6), t), key="rep_custom")
        if isinstance(rng, (list, tuple)) and len(rng) == 2:
            a, b = rng
        else:
            st.info("Pick both a start and an end date.")
            return
    else:
        a, b = reports.preset_range(preset)
        c2.markdown(f"**{a:%b %d, %Y} → {b:%b %d, %Y}**")
        c2.caption(reports.PRESET_HELP[preset])
    pubs = db.q("SELECT id, name FROM publishers ORDER BY name")
    buys = db.q("SELECT id, name FROM buyers ORDER BY name")
    pf = c3.selectbox("Publisher", ["All"] + [p["name"] for p in pubs], key="rep_pub")
    bf = c3.selectbox("Buyer", ["All"] + [p["name"] for p in buys], key="rep_buy")
    pid = next((p["id"] for p in pubs if p["name"] == pf), None)
    bid = next((p["id"] for p in buys if p["name"] == bf), None)

    s, e = reports.utc_bounds(a, b)
    first, last, n = reports.coverage()
    if n == 0 or s < first - timedelta(days=1):
        have = f"Your stored history starts {fmt(first, False)}." if first else "No calls are stored yet."
        st.warning(f"{have} Part of this range is not in your database.")
        if st.button("⬇️ Load the missing part from Ringba", key="rep_load"):
            end_load = min(e, first) if first else e
            res = ingest.backfill_range(s, end_load, progress_bar_cb("Loading"))
            st.success(f"Loaded {res['inserted']} new calls.")
            if res["failures"]:
                st.error("Some days failed: " + "; ".join(res["failures"][:5]))
            st.rerun()

    df = reports.load_calls(s, e, pid, bid)
    sm = reports.summary(df)
    m = st.columns(6)
    for col, k in zip(m, ["Calls", "Connected", "Converted", "Paid", "Blocked", "Duplicates"]):
        col.metric(k, f"{sm[k]:,}")
    m = st.columns(5)
    for col, k, fmtp in zip(m, ["Connect %", "Revenue", "Payout", "Profit", "Avg talk (sec)"],
                            ["{:.1f}%", "${:,.2f}", "${:,.2f}", "${:,.2f}", "{}"]):
        col.metric(k, fmtp.format(sm[k]))
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
def tab_publishers():
    st.caption("Add a publisher by hand (name only) and it links automatically when its first call arrives. "
               "Affiliate managers are created in the 'Emails & Alerts' tab.")
    mgrs = db.q("SELECT id, name FROM affiliate_managers WHERE active = TRUE ORDER BY name")
    name2id = {m["name"]: m["id"] for m in mgrs}
    since = db.now_utc() - timedelta(days=30)
    rows = db.q("""SELECT p.id, p.name, p.ringba_publisher_id, p.status, m.name AS manager, p.publisher_email,
                          p.cc_publisher, p.extra_cc, p.notes,
                          (SELECT MAX(call_at) FROM calls WHERE publisher_id = p.id) AS last_call,
                          (SELECT COUNT(*) FROM calls WHERE publisher_id = p.id AND call_at >= :s) AS calls_30d
                   FROM publishers p LEFT JOIN affiliate_managers m ON m.id = p.manager_id
                   ORDER BY p.name""", s=since)
    cols = ["id", "name", "ringba_publisher_id", "status", "manager", "publisher_email", "cc_publisher",
            "extra_cc", "notes", "last_call", "calls_30d"]
    df = frame(rows, cols)
    df["last_call"] = df["last_call"].map(lambda v: "never" if pd.isna(v) else fmt(v))
    df["cc_publisher"] = df["cc_publisher"].fillna(False).astype(bool)

    def tf(v):
        validate_emails(v["publisher_email"], "publisher email")
        validate_emails(v["extra_cc"], "CC email")
        return {"name": v["name"], "ringba_publisher_id": v["ringba_publisher_id"], "status": v["status"] or "active",
                "manager_id": name2id.get(v["manager"]), "publisher_email": v["publisher_email"],
                "cc_publisher": bool(v["cc_publisher"]), "extra_cc": v["extra_cc"], "notes": v["notes"]}

    cfg = {"id": None,
           "name": st.column_config.TextColumn("Publisher name", required=True),
           "ringba_publisher_id": st.column_config.TextColumn("Ringba ID", help="Leave empty for hand-added publishers."),
           "status": st.column_config.SelectboxColumn("Status", options=["active", "paused", "terminated"]),
           "manager": st.column_config.SelectboxColumn("Affiliate manager (gets CC)", options=list(name2id)),
           "publisher_email": st.column_config.TextColumn("Publisher's own email"),
           "cc_publisher": st.column_config.CheckboxColumn("CC the publisher?", help="Also needs the alert's 'CC publisher/buyer' switch on."),
           "extra_cc": st.column_config.TextColumn("Extra CC emails", help="Comma-separated. Always copied on this publisher's alerts."),
           "notes": st.column_config.TextColumn("Notes"),
           "last_call": st.column_config.TextColumn("Last call"),
           "calls_30d": st.column_config.NumberColumn("Calls (30d)")}
    table_editor("pub", df, cfg, ["name", "ringba_publisher_id", "status", "manager", "publisher_email",
                                  "cc_publisher", "extra_cc", "notes"], "publishers", tf, required=("name",),
                 disabled=["last_call", "calls_30d"], insert_extra={"created_at": db.now_utc()})


# ================================================================== BUYERS
def tab_buyers():
    st.caption("Tick **Watchlist** to get an email every time a call CONNECTS to that buyer. "
               "Add a buyer by name before it has any calls and it links automatically later.")
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
    table_editor("buy", df, cfg, ["name", "ringba_buyer_id", "status", "watchlist", "buyer_email", "cc_buyer",
                                  "extra_cc", "notes"], "buyers", tf, required=("name",),
                 disabled=["last_call", "calls_30d", "paid_30d"], insert_extra={"created_at": db.now_utc()})


# ================================================================== DIDs
def tab_dids():
    st.caption("A DID is the tracking number that was dialed. You get an email when an active DID has had no calls for the "
               "number of days set in 'Emails & Alerts' (default 30), so you can release it and stop the monthly charge. "
               "A DID that never got a call is counted from the day it was added. Set a DID to **retired** to stop its alerts.")
    since = db.now_utc() - timedelta(days=30)
    now = db.now_utc()
    rows = db.q("""SELECT d.id, d.number, d.label, d.status, d.monthly_cost, d.added_at, d.notes, d.last_call_at,
                          (SELECT COUNT(*) FROM calls WHERE did_id = d.id AND call_at >= :s) AS calls_30d
                   FROM dids d ORDER BY d.number""", s=since)
    z = tz()
    recs = []
    for r in rows:
        ref = db.to_dt(r["last_call_at"]) or db.to_dt(r["added_at"])
        recs.append({"id": r["id"], "number": r["number"], "label": r["label"], "status": r["status"],
                     "monthly_cost": float(r["monthly_cost"]) if r["monthly_cost"] is not None else None,
                     "added_on": db.to_dt(r["added_at"]).astimezone(z).date() if r["added_at"] else None,
                     "notes": r["notes"], "last_call": fmt(r["last_call_at"]) if r["last_call_at"] else "never",
                     "idle_days": (now - ref).days if ref else None, "calls_30d": r["calls_30d"]})
    cols = ["id", "number", "label", "status", "monthly_cost", "added_on", "notes", "last_call", "idle_days", "calls_30d"]
    df = frame(recs, cols).sort_values("idle_days", ascending=False, na_position="last").reset_index(drop=True)

    def tf(v):
        num = rc.normalize_phone(v["number"])
        if not num:
            raise ValueError("number is not valid")
        d = v["added_on"]
        added = datetime.combine(d, time.min, tzinfo=z).astimezone(timezone.utc) if d else db.now_utc()
        return {"number": num, "label": v["label"], "status": v["status"] or "active",
                "monthly_cost": v["monthly_cost"], "added_at": added, "notes": v["notes"]}

    cfg = {"id": None,
           "number": st.column_config.TextColumn("DID number", required=True),
           "label": st.column_config.TextColumn("Label"),
           "status": st.column_config.SelectboxColumn("Status", options=["active", "retired"]),
           "monthly_cost": st.column_config.NumberColumn("Monthly cost $", min_value=0.0, format="%.2f"),
           "added_on": st.column_config.DateColumn("Added on", help="Used for the 30-day rule if the DID never got a call."),
           "notes": st.column_config.TextColumn("Notes"),
           "last_call": st.column_config.TextColumn("Last call"),
           "idle_days": st.column_config.NumberColumn("Days without calls"),
           "calls_30d": st.column_config.NumberColumn("Calls (30d)")}
    table_editor("did", df, cfg, ["number", "label", "status", "monthly_cost", "added_on", "notes"], "dids", tf,
                 required=("number",), disabled=["last_call", "idle_days", "calls_30d"])

    with st.expander("➕ Import my full list of DIDs (including numbers that never got a call)"):
        st.write("Paste one number per line. Optional label after a comma, e.g. `+16143505503, Rehab line`. "
                 "Numbers that already exist are skipped. New numbers start their 30-day clock **today**.")
        txt = st.text_area("Numbers", height=150, key="did_import_txt")
        if st.button("Import numbers", key="did_import_btn"):
            added = skipped = bad = 0
            have = {r["number"] for r in db.q("SELECT number FROM dids")}
            for line in (txt or "").splitlines():
                if not line.strip():
                    continue
                parts = line.split(",", 1)
                num = rc.normalize_phone(parts[0])
                if not num:
                    bad += 1
                    continue
                if num in have:
                    skipped += 1
                    continue
                db.x("INSERT INTO dids (number, label, status, added_at) VALUES (:n,:l,'active',:a)",
                     n=num, l=(parts[1].strip() if len(parts) > 1 else None), a=db.now_utc())
                have.add(num)
                added += 1
            st.success(f"Added {added}, skipped {skipped} that already existed, {bad} lines were not valid numbers.")


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


def tab_caps():
    start, _ = window_start("daily", db.now_utc())
    st.caption(f"Copy your caps from Ringba here. The tool counts calls and emails you when usage reaches the % you choose. "
               f"Today's window started {fmt(start)}. Daily caps reset at the 'Day starts at' time in Settings "
               f"(default 00:00, i.e. the day ends at 11:59 PM). Monthly caps reset on the 1st.")
    st.subheader("🏢 Buyer caps")
    st.caption("Default: counts PAID calls.")
    caps_section("buyer", "paid")
    st.subheader("🧑‍💼 Publisher caps")
    st.caption("Default: counts CONVERTED calls.")
    caps_section("publisher", "converted")


# ================================================================== EMAILS & ALERTS
def tab_emails():
    st.subheader("1. Who always gets the alerts (To:)")
    cur = ", ".join(alerts.admin_to())
    txt = st.text_area("Admin emails (one per line, or separated by commas)", value=cur.replace(", ", "\n"), height=100)
    c1, c2 = st.columns(2)
    if c1.button("💾 Save admin emails"):
        toks = [t for t in EMAIL_TOKEN.split(txt or "") if t]
        bad = [t for t in toks if not alerts.EMAIL_RE.match(t)]
        if bad:
            st.error("Not valid: " + ", ".join(bad))
        else:
            db.set_setting("admin_emails", ", ".join(alerts.parse_emails(txt)))
            st.success("Saved.")
            st.rerun()
    if c2.button("📨 Send a test email now"):
        to = alerts.admin_to()
        if not to:
            st.error("Save at least one admin email first.")
        else:
            try:
                mailer.send_test(to)
                st.success("Test email sent to " + ", ".join(to) + ". Check your inbox (and spam).")
            except Exception as e:
                st.error(f"Could not send: {e}")

    st.subheader("2. Affiliate managers")
    st.caption("Create managers here, then pick one for each publisher on the Publishers tab. They are copied (CC) on that publisher's alerts. "
               "Remove a row to delete a manager (publishers keep working, just without a manager).")
    rows = db.q("SELECT id, name, email, active FROM affiliate_managers ORDER BY name")
    df = frame(rows, ["id", "name", "email", "active"])
    df["active"] = df["active"].fillna(True).astype(bool)

    def tf(v):
        validate_emails(v["email"], "email")
        if not alerts.EMAIL_RE.match(v["email"] or ""):
            raise ValueError("email is required")
        return {"name": v["name"], "email": v["email"], "active": bool(v["active"]) if v["active"] is not None else True}

    cfg = {"id": None, "name": st.column_config.TextColumn("Manager name", required=True),
           "email": st.column_config.TextColumn("Email", required=True),
           "active": st.column_config.CheckboxColumn("Active")}
    table_editor("mgr", df, cfg, ["name", "email", "active"], "affiliate_managers", tf, required=("name", "email"),
                 allow_delete=True)

    st.subheader("3. Each alert type: on/off, numbers, and who is copied")
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
           "cc_manager": st.column_config.CheckboxColumn("CC affiliate manager"),
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
            "- **CC affiliate manager:** the manager chosen for that publisher (publisher alerts only).\n"
            "- **CC publisher / buyer own email:** only if the alert type switch here is on **and** the 'CC the publisher/buyer?' box "
            "is ticked on their row. Both must be on, so outside parties are never emailed by accident.\n"
            "- **CC their extra emails:** the 'Extra CC emails' on that publisher or buyer row.\n"
            "- **Always CC these emails:** extra addresses for that alert type (for example a partner or a team inbox).\n"
            "- Changes apply to alerts created from now on.")


# ================================================================== ALERT LOG
def tab_log():
    counts = {r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) AS n FROM alerts GROUP BY status")}
    st.write(" · ".join(f"**{k}**: {v}" for k, v in sorted(counts.items())) or "No alerts yet.")
    c1, c2, c3 = st.columns(3)
    if c1.button("📤 Send pending alerts now"):
        res = mailer.process_outbox()
        st.success(str(res))
    if c2.button("🔁 Retry failed alerts"):
        n = db.x("UPDATE alerts SET status='pending', attempts=0 WHERE status='failed'")
        st.success(f"{n} alerts put back in the queue. Press 'Send pending alerts now'.")
    show = c3.multiselect("Show status", ["pending", "sent", "dry_run", "failed", "skipped"],
                          default=["pending", "sent", "dry_run", "failed"])
    if not show:
        return
    rows = db.q_in("SELECT id, created_at, alert_type, subject, to_emails, cc_emails, status, attempts, last_error "
                   "FROM alerts WHERE status IN :s ORDER BY id DESC LIMIT 300", "s", show)
    if not rows:
        st.info("Nothing to show.")
        return
    d = pd.DataFrame(rows)
    d["created_at"] = d["created_at"].map(fmt)
    st.dataframe(d, hide_index=True)
    pick = st.selectbox("Read one alert (id)", [r["id"] for r in rows])
    a = db.q1("SELECT * FROM alerts WHERE id=:i", i=pick)
    if a:
        st.text(f"To: {a['to_emails']}\nCC: {a['cc_emails']}\nSubject: {a['subject']}\nStatus: {a['status']}  {a['last_error'] or ''}\n\n{a['body']}")


# ================================================================== SYNC & IMPORT
def tab_sync():
    first, last, n = reports.coverage()
    st.write(f"**{n:,} calls stored**" + (f", from {fmt(first, False)} to {fmt(last)}." if n else "."))

    st.subheader("1. Load history from Ringba")
    st.caption("Loading history NEVER sends alerts. It is safe to run again: calls are updated, never duplicated. "
               "It can take a few minutes. Keep this tab open, or use the 'Backfill' button on GitHub (Actions tab) to run it in the background.")
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
    st.caption("Does exactly what the 5-minute job does: read recent calls, check rules, send emails "
               "(respecting DRY RUN).")
    if st.button("▶️ Run engine now"):
        ok, summary = engine.run_once(log=lambda m: None)
        (st.success if ok else st.error)("Finished" if ok else "Finished with errors")
        st.code(summary)

    st.subheader("Recent sync runs")
    rows = db.q("SELECT id, kind, started_at, status, rows_fetched, rows_inserted, rows_updated, error "
                "FROM sync_runs ORDER BY id DESC LIMIT 15")
    if rows:
        d = pd.DataFrame(rows)
        d["started_at"] = d["started_at"].map(fmt)
        st.dataframe(d, hide_index=True)


# ================================================================== SETTINGS
def tab_settings():
    st.subheader("General")
    c1, c2 = st.columns(2)
    tzname = c1.text_input("Timezone", db.get_setting("timezone"))
    day_start = c2.text_input("Day starts at (HH:MM)", db.get_setting("day_start"),
                              help="00:00 means a daily cap resets at midnight, i.e. the day ends at 11:59 PM.")
    look = c1.number_input("Re-read the last N hours every run", 1, 48, int(float(db.get_setting("live_lookback_hours"))))
    prefix = c2.text_input("Email subject prefix", db.get_setting("subject_prefix"))
    real = st.checkbox("✅ Send REAL emails (untick = DRY RUN, nothing is sent)", value=not mailer.is_dry_run(),
                       disabled=config.DRY_RUN_ENV,
                       help="DRY RUN is forced on by the DRY_RUN=true secret." if config.DRY_RUN_ENV else None)
    if st.button("💾 Save settings"):
        try:
            ZoneInfo(tzname)
            hh, mm = day_start.split(":")
            assert 0 <= int(hh) < 24 and 0 <= int(mm) < 60
        except Exception:
            st.error("Timezone must look like America/New_York and the time like 00:00")
        else:
            db.set_setting("timezone", tzname)
            db.set_setting("day_start", f"{int(hh):02d}:{int(mm):02d}")
            db.set_setting("live_lookback_hours", int(look))
            db.set_setting("subject_prefix", prefix)
            if not config.DRY_RUN_ENV:
                db.set_setting("dry_run", "false" if real else "true")
            st.success("Saved.")
            st.rerun()

    st.subheader("Connections")
    st.write({"Database": "✅ connected", "Ringba token": "✅ set" if config.RINGBA_TOKEN else "❌ missing",
              "Ringba account ID": "✅ set" if config.RINGBA_ACCOUNT_ID else "❌ missing",
              "Gmail user": "✅ set" if config.SMTP_USER else "❌ missing",
              "Gmail app password": "✅ set" if config.SMTP_PASSWORD else "❌ missing",
              "App password (optional)": "✅ set" if config.get_secret("APP_PASSWORD") else "⚠️ not set - anyone with the link can open this app"})
    if st.button("🔌 Test Ringba connection"):
        try:
            res = rc.test_connection()
            rep = res["report"]
            if rep["missing"]:
                st.error("Connected, but the file is missing columns: " + ", ".join(rep["missing"]))
            else:
                st.success(f"Ringba works. Read {res['records']} calls from the last 3 hours "
                           f"(file has {rep['headers']} columns).")
            st.write({k: v for k, v in rep["mapped"].items()})
            if res["sample"]:
                s = res["sample"][0]
                st.caption("First call read (check the DID is the number dialed, not the caller's number):")
                st.write({"DID": s["did"], "Caller": s["caller"], "Publisher": s["publisher_name"], "Buyer": s["buyer_name"],
                          "Connected": s["connected"], "Time": fmt(s["call_at"])})
        except Exception as e:
            st.error(str(e))

    st.subheader("Database size")
    rows = []
    for t in ["calls", "publishers", "buyers", "dids", "caps", "alerts", "sync_runs"]:
        rows.append({"table": t, "rows": db.q1(f"SELECT COUNT(*) AS n FROM {t}")["n"]})
    st.dataframe(pd.DataFrame(rows), hide_index=True)


for tab, fn in zip(tabs, [tab_dashboard, tab_reports, tab_publishers, tab_buyers, tab_dids, tab_caps, tab_emails,
                          tab_log, tab_sync, tab_settings]):
    with tab:
        try:
            fn()
        except Exception as e:  # show errors on screen instead of a blank page
            st.error(f"Something went wrong on this tab: {e}")
            st.exception(e)
