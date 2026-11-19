"""Cap overview: every publisher (with an active DID) and every active buyer target with its total / global cap and its usage."""
import db
import dids
import rules


def _pct(used, cap):
    return round(used / cap * 100, 1) if cap else None


def publisher_caps(now=None):
    """Returns (summary rows, detail rows).
    detail  = one row per publisher + campaign + period (only caps of publishers that have an ACTIVE DID in that campaign).
    summary = one row per publisher with an active DID: the TOTAL of its campaign caps per period, and what is used of them.
              A cap for 'all campaigns' is shown on its own (it is not added to the campaign total, so nothing is counted twice)."""
    now = now or db.now_utc()
    links = dids.active_links()
    names = {p["id"]: p["name"] for p in db.q("SELECT id, name FROM publishers")}
    n_dids = {r["publisher_id"]: r["n"] for r in db.q("SELECT publisher_id, COUNT(*) AS n FROM dids WHERE status='active' AND publisher_id IS NOT NULL GROUP BY publisher_id")}
    detail = []
    import insights
    ins = insights.Usage(now)                                   # all usage numbers in 3-4 queries, not 3 per cap
    for c in db.q("SELECT * FROM caps WHERE entity_type='publisher' AND active = TRUE"):
        if not dids.cap_has_active_did(c, links):
            continue
        u = rules.cap_usage(c, now, ent_name=names.get(c["entity_id"]), ins=ins)
        detail.append({"publisher": names.get(c["entity_id"], "?"), "pid": c["entity_id"], "campaign": c["campaign_name"] or "(all campaigns)",
                       "period": c["period"], "counts": c["metric"], "cap": c["cap_value"], "used": u["usage"], "pct": _pct(u["usage"], c["cap_value"]),
                       "source": "Ringba" if c.get("source") == "ringba" else "typed by you"})
    summary = {}
    for pid in links[0]:
        summary[pid] = {"publisher": names.get(pid, "?"), "active_dids": n_dids.get(pid, 0), "campaigns": set(),
                        "daily_cap": 0, "daily_used": 0, "monthly_cap": 0, "monthly_used": 0,
                        "all_daily_cap": None, "all_daily_used": None, "all_monthly_cap": None, "all_monthly_used": None}
    for d in detail:
        s = summary.get(d["pid"])
        if s is None:
            continue
        if d["campaign"] == "(all campaigns)":
            s[f"all_{d['period']}_cap"], s[f"all_{d['period']}_used"] = d["cap"], d["used"]
        else:
            s["campaigns"].add(d["campaign"])
            s[f"{d['period']}_cap"] += d["cap"]
            s[f"{d['period']}_used"] += d["used"]
    rows = []
    for s in summary.values():
        rows.append({"publisher": s["publisher"], "active_dids": s["active_dids"], "campaigns_with_cap": len(s["campaigns"]),
                     "daily_cap_total": s["daily_cap"] or None, "used_today": s["daily_used"] if s["daily_cap"] else None, "today_pct": _pct(s["daily_used"], s["daily_cap"]),
                     "monthly_cap_total": s["monthly_cap"] or None, "used_month": s["monthly_used"] if s["monthly_cap"] else None, "month_pct": _pct(s["monthly_used"], s["monthly_cap"]),
                     "all_campaigns_daily_cap": s["all_daily_cap"], "all_campaigns_daily_used": s["all_daily_used"],
                     "all_campaigns_monthly_cap": s["all_monthly_cap"], "all_campaigns_monthly_used": s["all_monthly_used"],
                     "has_cap": bool(s["daily_cap"] or s["monthly_cap"] or s["all_daily_cap"] or s["all_monthly_cap"])})
    rows.sort(key=lambda r: (not r["has_cap"], -(max(r["today_pct"] or 0, r["month_pct"] or 0)), r["publisher"].lower()))
    detail.sort(key=lambda d: (d["publisher"].lower(), d["campaign"], d["period"]))
    return rows, detail


def target_caps(include_disabled=False):
    """One row per buyer target (active ones, or all with include_disabled): its global (whole-target) daily and monthly cap and usage, from Ringba."""
    out = []
    for t in db.q("SELECT * FROM targets" + ("" if include_disabled else " WHERE active = TRUE") + " ORDER BY buyer_name, name"):
        dc, mc = t["daily_cap"], t["monthly_cap"]
        out.append({"buyer": t["buyer_name"] or "", "target": t["name"] or t["ringba_target_id"], "target_id": t["ringba_target_id"],
                    "daily_cap": dc, "used_today": (t["count_day"] or 0) if dc else None, "today_pct": _pct(t["count_day"] or 0, dc),
                    "monthly_cap": mc, "used_month": (t["count_month"] or 0) if mc else None, "month_pct": _pct(t["count_month"] or 0, mc),
                    "alerts_on": bool(t["alerts_on"]), "usage_read": t["counts_at"], "has_cap": bool(dc or mc), "enabled": bool(t["active"])})
    return out


def buyer_totals(target_rows):
    """Per buyer: how many active targets, how many have a cap, and the total of their caps."""
    b = {}
    for r in target_rows:
        s = b.setdefault(r["buyer"] or "(no buyer)", {"buyer": r["buyer"] or "(no buyer)", "active_targets": 0, "targets_with_cap": 0,
                                                       "daily_cap_total": 0, "used_today": 0, "monthly_cap_total": 0, "used_month": 0})
        s["active_targets"] += 1
        s["targets_with_cap"] += 1 if r["has_cap"] else 0
        if r["daily_cap"]:
            s["daily_cap_total"] += r["daily_cap"]; s["used_today"] += r["used_today"] or 0
        if r["monthly_cap"]:
            s["monthly_cap_total"] += r["monthly_cap"]; s["used_month"] += r["used_month"] or 0
    rows = []
    for s in b.values():
        rows.append({**s, "daily_cap_total": s["daily_cap_total"] or None, "used_today": s["used_today"] if s["daily_cap_total"] else None,
                     "today_pct": _pct(s["used_today"], s["daily_cap_total"]),
                     "monthly_cap_total": s["monthly_cap_total"] or None, "used_month": s["used_month"] if s["monthly_cap_total"] else None,
                     "month_pct": _pct(s["used_month"], s["monthly_cap_total"])})
    return sorted(rows, key=lambda r: r["buyer"].lower())
