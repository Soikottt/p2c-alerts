import streamlit as st
import pandas as pd
import db
import alert_engine
from datetime import datetime, timezone, timedelta
import requests
# Set up the page
st.set_page_config(page_title="P2C Alert Console", layout="wide")
st.title("📞 Pay-Per-Call Alert Console")

# --- HELPER FUNCTION TO SAVE TABLE EDITS ---
def save_table(df, table_name, columns):
    for _, row in df.iterrows():
        # Prepare the parameters (convert NaN/NaT to None)
        params = {c: (None if pd.isna(row[c]) else row[c]) for c in columns}
        
        if pd.isna(row.get("id")):
            # Insert new row
            keys = ", ".join(params.keys())
            vals = ", ".join(f":{k}" for k in params.keys())
            db.x(f"INSERT INTO {table_name} ({keys}) VALUES ({vals})", **params)
        else:
            # Update existing row
            sets = ", ".join(f"{k}=:{k}" for k in params.keys())
            db.x(f"UPDATE {table_name} SET {sets} WHERE id=:id", **params, id=int(row["id"]))
    st.success(f"✅ {table_name.title()} saved successfully!")
    st.rerun()

# --- CREATE TABS ---
tabs = st.tabs(["Dashboard", "Publishers", "Buyers", "DIDs", "Cap Rules", "CC Routing", "Alert Log", "Settings"])

# ─── TAB 1: DASHBOARD ───
with tabs[0]:
    st.header("Overview")
    col1, col2, col3, col4 = st.columns(4)
    
    calls_today = db.q("SELECT COUNT(*) n FROM calls WHERE started_at >= CURRENT_DATE")[0]["n"]
    conn_today = db.q("SELECT COUNT(*) n FROM calls WHERE started_at >= CURRENT_DATE AND connected=TRUE")[0]["n"]
    watchlist_buyers = db.q("SELECT COUNT(*) n FROM buyers WHERE watchlist=TRUE")[0]["n"]
    active_pubs = db.q("SELECT COUNT(*) n FROM publishers WHERE status='active'")[0]["n"]
    
    col1.metric("Calls Today", calls_today)
    col2.metric("Connected Today", conn_today)
    col3.metric("Watchlist Buyers", watchlist_buyers)
    col4.metric("Active Publishers", active_pubs)
    
    st.subheader("Recent Calls (Last 100)")
    recent = db.q("""
        SELECT c.started_at as Time, p.name as Publisher, b.name as Buyer, d.number as DID, 
               c.connected as Connected, c.duration_sec as Duration
        FROM calls c
        LEFT JOIN publishers p ON p.id = c.publisher_id
        LEFT JOIN buyers b ON b.id = c.buyer_id
        LEFT JOIN dids d ON d.id = c.did_id
        ORDER BY c.started_at DESC LIMIT 100
    """)
    if recent:
        st.dataframe(pd.DataFrame([dict(r) for r in recent]), use_container_width=True)
    else:
        st.info("No calls found in the database yet.")

# ─── TAB 2: PUBLISHERS ───
with tabs[1]:
    st.header("Publishers")
    st.caption("Set each publisher's alert email and toggle CC.")
    
    pubs = db.q("SELECT id, name, alert_email, cc_on_alerts, status, total_calls, last_call_at FROM publishers ORDER BY name")
    df_pubs = pd.DataFrame([dict(r) for r in pubs])
    
    if df_pubs.empty:
        st.info("No publishers found yet. They will appear here automatically once calls come in.")
    else:
        edited_pubs = st.data_editor(
            df_pubs, 
            num_rows="dynamic", 
            use_container_width=True,
            disabled=["total_calls", "last_call_at"],
            column_config={
                "cc_on_alerts": st.column_config.CheckboxColumn("CC Alerts"),
                "status": st.column_config.SelectboxColumn(options=["active", "paused", "terminated"])
            }
        )
        if st.button("Save Publishers"):
            save_table(edited_pubs, "publishers", ["name", "alert_email", "cc_on_alerts", "status"])

# ─── TAB 3: BUYERS ───
with tabs[2]:
    st.header("Buyers")
    st.caption("Toggle 'Watchlist' to get an alert when they connect. Set email to CC them on buyer caps.")
    
    buys = db.q("SELECT id, name, alert_email, cc_on_alerts, watchlist, status FROM buyers ORDER BY name")
    df_buys = pd.DataFrame([dict(r) for r in buys])
    
    if df_buys.empty:
        st.info("No buyers found yet.")
    else:
        edited_buys = st.data_editor(
            df_buys, 
            num_rows="dynamic", # Allows you to manually add a buyer if needed
            use_container_width=True,
            column_config={
                "watchlist": st.column_config.CheckboxColumn("Watchlist"),
                "cc_on_alerts": st.column_config.CheckboxColumn("CC Alerts"),
                "status": st.column_config.SelectboxColumn(options=["active", "paused", "terminated"])
            }
        )
        if st.button("Save Buyers"):
            save_table(edited_buys, "buyers", ["name", "alert_email", "cc_on_alerts", "watchlist", "status"])

# ─── TAB 4: DIDs ───
with tabs[3]:
    st.header("DIDs")
    
    dids = db.q("SELECT id, number, monthly_cost, status, last_call_at FROM dids ORDER BY number")
    df_dids = pd.DataFrame([dict(r) for r in dids])
    
    if df_dids.empty:
        st.info("No DIDs found yet.")
    else:
        edited_dids = st.data_editor(
            df_dids, 
            num_rows="fixed", 
            use_container_width=True,
            disabled=["number", "last_call_at"],
            column_config={
                "status": st.column_config.SelectboxColumn(options=["active", "inactive"])
            }
        )
        if st.button("Save DIDs"):
            save_table(edited_dids, "dids", ["monthly_cost", "status"])

# ─── TAB 5: CAP RULES ───
with tabs[4]:
    st.header("Cap Rules")
    st.caption("Metric is always 'incoming_calls'. Daily resets at 11:30 PM EST.")
    
    rules = db.q("SELECT id, entity_type, entity_id, period, cap_value, alert_threshold_pct, active FROM cap_rules")
    df_rules = pd.DataFrame([dict(r) for r in rules])
    
    if df_rules.empty:
        # Create empty dataframe with correct columns if none exist
        df_rules = pd.DataFrame(columns=["id", "entity_type", "entity_id", "period", "cap_value", "alert_threshold_pct", "active"])
        
    edited_rules = st.data_editor(
        df_rules, 
        num_rows="dynamic", 
        use_container_width=True,
        column_config={
            "entity_type": st.column_config.SelectboxColumn(options=["publisher", "buyer"], required=True),
            "entity_id": st.column_config.NumberColumn("Entity ID", required=True, help="You must enter the exact DB ID number from the Publishers or Buyers tab."),
            "period": st.column_config.SelectboxColumn(options=["daily", "monthly"], required=True),
            "cap_value": st.column_config.NumberColumn(min_value=1, required=True),
            "alert_threshold_pct": st.column_config.NumberColumn(min_value=1, max_value=100, default=80),
            "active": st.column_config.CheckboxColumn(default=True)
        },
        disabled=["id"]
    )
    if st.button("Save Cap Rules"):
        # Need to handle the 'metric' column which isn't shown in the UI but exists in DB
        for _, row in edited_rules.iterrows():
            if pd.isna(row["id"]):
                db.x("INSERT INTO cap_rules (entity_type, entity_id, period, metric, cap_value, alert_threshold_pct, active) VALUES (:et, :ei, :p, 'incoming_calls', :cv, :th, :a)",
                     et=row["entity_type"], ei=int(row["entity_id"]), p=row["period"], cv=int(row["cap_value"]), th=int(row["alert_threshold_pct"]), a=bool(row["active"]))
            else:
                db.x("UPDATE cap_rules SET entity_type=:et, entity_id=:ei, period=:p, cap_value=:cv, alert_threshold_pct=:th, active=:a WHERE id=:id",
                     et=row["entity_type"], ei=int(row["entity_id"]), p=row["period"], cv=int(row["cap_value"]), th=int(row["alert_threshold_pct"]), a=bool(row["active"]), id=int(row["id"]))
        st.success("✅ Cap Rules saved!")
        st.rerun()

# ─── TAB 6: CC ROUTING ───
with tabs[5]:
    st.header("CC Routing")
    st.caption("Control which alerts CC the buyer or publisher. (They must also have CC enabled on their profile).")
    
    route = db.q("SELECT alert_type, cc_buyer, cc_publisher FROM cc_routing ORDER BY alert_type")
    df_route = pd.DataFrame([dict(r) for r in route])
    
    edited_route = st.data_editor(
        df_route, 
        num_rows="fixed", 
        use_container_width=True,
        disabled=["alert_type"]
    )
    if st.button("Save CC Routing"):
        for _, row in edited_route.iterrows():
            db.x("UPDATE cc_routing SET cc_buyer=:b, cc_publisher=:p WHERE alert_type=:t",
                 b=bool(row["cc_buyer"]), p=bool(row["cc_publisher"]), t=row["alert_type"])
        st.success("✅ CC Routing saved!")
        st.rerun()

# ─── TAB 7: ALERT LOG ───
with tabs[6]:
    st.header("Alert Log")
    st.caption("History of all alerts sent by the engine.")
    
    logs = db.q("SELECT sent_at, alert_type, recipients, details FROM alerts_log ORDER BY sent_at DESC LIMIT 500")
    if logs:
        st.dataframe(pd.DataFrame([dict(r) for r in logs]), use_container_width=True)
    else:
        st.info("No alerts sent yet.")

# ─── TAB 8: SETTINGS ───
with tabs[7]:
    st.header("Settings")
    
    current_admin = db.get_setting("admin_email", "")
    new_admin = st.text_input("Admin Email (receives all alerts as 'To:')", value=current_admin)
    
    if st.button("Save Settings"):
        if new_admin:
            db.x("INSERT INTO settings (key, value) VALUES ('admin_email', :e) ON CONFLICT (key) DO UPDATE SET value = :e", e=new_admin)
            st.success("✅ Settings saved!")
            st.rerun()

    st.divider()
    st.subheader("Sync Historical Data")
    st.caption("Pull past calls from Ringba to populate your dashboard. This will NOT send emails.")
    
    col1, col2 = st.columns(2)
    with col1:
        start_date = st.date_input("Start Date", value=datetime.date.today() - datetime.timedelta(days=30))
    with col2:
        end_date = st.date_input("End Date", value=datetime.date.today())
        
    if st.button("⬇️ Sync Historical Data from Ringba"):
        if start_date > end_date:
            st.error("❌ Start date must be before end date.")
        else:
            start_dt = datetime.datetime.combine(start_date, datetime.time.min).replace(tzinfo=datetime.timezone.utc)
            end_dt = datetime.datetime.combine(end_date, datetime.time.max).replace(tzinfo=datetime.timezone.utc)
            
            with st.spinner(f"Pulling data from {start_date} to {end_date}..."):
                result_msg = alert_engine.backfill_history(start_dt, end_dt)
            st.success(result_msg)
            st.rerun()  
            
    st.divider()
    st.subheader("Manual Engine Run")
    st.caption("Click this to run the alert engine immediately instead of waiting for the 5-minute timer.")
    
    if st.button("▶️ Run Alert Engine Now"):
        with st.spinner("Running engine..."):
            alert_engine.run()
        st.success("✅ Engine finished! Check the Alert Log tab.")
        
    st.divider()
    st.subheader("API Connection Test")
    if st.button("🧪 Test Ringba Connection"):
        from datetime import datetime, timezone, timedelta
        import requests
        import config
        
        # 1. Ask for just the last 24 hours
        now = datetime.now(timezone.utc)
        start = now - timedelta(hours=24) 
        
        start_str = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        
        url = f"https://api.ringba.com/v2/{config.RINGBA_ACCT}/calllogs"
        headers = {"Authorization": f"Token {config.RINGBA_TOKEN}", "Content-Type": "application/json"}
        
        body = {
            "reportStart": start_str,
            "reportEnd": end_str,
            "valueColumns": [{"column": "callDt"}],
            "pagination": {"page": 1, "pageSize": 5}
        }
        
        with st.spinner("Testing..."):
            try:
                res = requests.post(url, headers=headers, json=body, timeout=15)
                
                st.write(f"**Status Code:** {res.status_code}")
                
                # PRINT THE EXACT TOKEN (just the last 5 characters) so we can verify it matches
                st.write(f"**Token ends in:** `...{str(config.RINGBA_TOKEN)[-5:]}`")
                st.write(f"**Account ID:** `{config.RINGBA_ACCT}`")
                
                if res.status_code == 200:
                    data = res.json()
                    total = data.get("totalCount", 0)
                    if total == 0:
                        st.warning(f"⚠️ Ringba connected, but found 0 calls in the last 24 hours.")
                        st.write("Here is the exact response from Ringba:")
                        st.json(data) # Show the FULL response, not just the data
                    else:
                        st.success(f"✅ Connected! Ringba says there are {total} calls in the last 24 hours.")
                else:
                    st.error("❌ Failed to connect. Here is the exact error from Ringba:")
                    st.code(res.text)
            except Exception as e:
                st.error(f"❌ Python Error: {e}")
                
    st.divider()
    st.subheader("Accuracy Test")
    if st.button("🔍 Run Accuracy Test"):
        from datetime import datetime, timezone
        from dateutil.parser import isoparse
        import requests
        import config
        
        st.write("Fetching 5 most recent calls directly from Ringba...")
        
        url = f"https://api.ringba.com/v2/{config.RINGBA_ACCT}/calllogs"
        headers = {"Authorization": f"Token {config.RINGBA_TOKEN}", "Content-Type": "application/json"}
        
        body = {
            "reportStart": (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "reportEnd": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "valueColumns": [
                {"column": "callDt"}, 
                {"column": "inboundCallId"},
                {"column": "hasConnected"},
                {"column": "connectedCallLengthInSeconds"}
            ],
            "orderByColumns": [{"column": "callDt", "direction": "desc"}],
            "pagination": {"page": 1, "pageSize": 5}
        }
        
        try:
            res = requests.post(url, headers=headers, json=body, timeout=15)
            data = res.json()
            
            # Handle both old and new Ringba response formats
            raw_calls = data.get("callLog", {}).get("data", data.get("report", {}).get("records", []))
            
            if not raw_calls:
                st.warning("Ringba returned 0 calls to compare.")
            else:
                st.write("### 1. What Ringba says (Raw API):")
                for row in raw_calls:
                    cols = row.get("columns", row)
                    st.write(f"- Call ID: `{cols.get('inboundCallId')}` | Ringba Date Raw: `{cols.get('callDt')}` | Connected: `{cols.get('hasConnected')}`")
                
                st.write("### 2. What our Database stored:")
                # Grab those exact same 5 Call IDs from our database
                call_ids = [str(row.get("columns", row).get("inboundCallId")) for row in raw_calls]
                
                # Build a dynamic query for the 5 IDs
                placeholders = ", ".join([f":id{i}" for i in range(len(call_ids))])
                params = {f"id{i}": cid for i, cid in enumerate(call_ids)}
                
                db_calls = db.q(f"""
                    SELECT ringba_call_id, started_at, connected, duration_sec 
                    FROM calls 
                    WHERE ringba_call_id IN ({placeholders})
                """, **params)
                
                for row in db_calls:
                    st.write(f"- Call ID: `{row['ringba_call_id']}` | DB Date: `{row['started_at']}` | Connected: `{row['connected']}` | Duration: `{row['duration_sec']}s`")
                
                st.info("Compare the Ringba Date Raw to the DB Date. If they are off by 4 or 5 hours, we have a timezone bug in our parsing.")
        except Exception as e:
            st.error(f"Error: {e}")