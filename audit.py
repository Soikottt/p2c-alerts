"""Audit trail: who did what, previous value, new value, when, and the related alert / DID.
Never raises: a problem with the log must never stop the real action."""
import db


def actor():
    try:
        import streamlit as st
        u = st.session_state.get("user")
        if u:
            return str(u)
    except Exception:
        pass
    return "system"


def log(action, entity_type=None, entity_id=None, alert_id=None, did_id=None, old=None, new=None, detail=None, who=None):
    try:
        db.x("INSERT INTO audit_log (at, actor, action, entity_type, entity_id, alert_id, did_id, old_value, new_value, detail) "
             "VALUES (:at,:a,:ac,:et,:ei,:al,:d,:o,:n,:de)",
             at=db.now_utc(), a=who or actor(), ac=str(action)[:80], et=entity_type, ei=None if entity_id is None else str(entity_id),
             al=alert_id, d=did_id, o=None if old is None else str(old)[:500], n=None if new is None else str(new)[:500],
             de=None if detail is None else str(detail)[:1000])
        return True
    except Exception:
        return False


def recent(limit=500, alert_id=None, did_id=None, actor_name=None, action=None, entity_type=None):
    where, p = [], {"n": int(limit)}
    if alert_id:
        where.append("alert_id = :al"); p["al"] = alert_id
    if did_id:
        where.append("did_id = :d"); p["d"] = did_id
    if actor_name:
        where.append("actor = :a"); p["a"] = actor_name
    if action:
        where.append("action = :ac"); p["ac"] = action
    if entity_type:
        where.append("entity_type = :et"); p["et"] = entity_type
    sql = "SELECT * FROM audit_log" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT :n"
    return db.q(sql, **p)
