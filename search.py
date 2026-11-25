"""Universal search: alert code (A325, 325, #A325), DID number, publisher, buyer, e-mail address, e-mail subject / text."""
import re

import db

CODE = re.compile(r"^\s*#?\s*A?\s*(\d{1,9})\s*$", re.I)


def _like(q):
    return "%" + q.lower().replace("%", "").replace("_", "") + "%"


def search(q, limit=40):
    q = (q or "").strip()
    out = {"alerts": [], "dids": [], "publishers": [], "buyers": [], "emails": [], "kind": ""}
    if len(q) < 2 and not CODE.match(q):
        return out
    m = CODE.match(q)
    cols = "id, thread_id, alert_type, subject, to_emails, cc_emails, status, case_status, created_at"
    if m:
        n = int(m.group(1))
        out["kind"] = "code"
        out["alerts"] = db.q(f"SELECT {cols} FROM alerts WHERE id=:n OR thread_id=:n ORDER BY id DESC LIMIT :l", n=n, l=limit)
        return out
    out["kind"] = "text"
    lk = _like(q)
    digits = re.sub(r"\D", "", q)
    ids = {}
    for r in db.q(f"SELECT {cols} FROM alerts WHERE LOWER(subject) LIKE :q OR LOWER(COALESCE(to_emails,'')) LIKE :q "
                  f"OR LOWER(COALESCE(cc_emails,'')) LIKE :q OR LOWER(body) LIKE :q ORDER BY id DESC LIMIT :l", q=lk, l=limit):
        ids[r["id"]] = r
    if len(digits) >= 7:
        for d in db.q("SELECT id, number, label, status FROM dids WHERE REPLACE(REPLACE(REPLACE(REPLACE(number,'+',''),'-',''),' ',''),'(','') LIKE :q LIMIT 20",
                      q="%" + digits + "%"):
            out["dids"].append(d)
            for r in db.q(f"SELECT {cols} FROM alerts WHERE snooze_key=:k ORDER BY id DESC LIMIT :l", k=f"did:{d['id']}", l=limit):
                ids.setdefault(r["id"], r)
    else:
        out["dids"] = db.q("SELECT id, number, label, status FROM dids WHERE LOWER(number) LIKE :q OR LOWER(COALESCE(label,'')) LIKE :q LIMIT 20", q=lk)
    out["publishers"] = db.q("SELECT id, name FROM publishers WHERE LOWER(name) LIKE :q ORDER BY name LIMIT 20", q=lk)
    out["buyers"] = db.q("SELECT id, name FROM buyers WHERE LOWER(name) LIKE :q ORDER BY name LIMIT 20", q=lk)
    em = db.q("SELECT alert_id, direction, subject, from_email, mail_date FROM email_archive WHERE alert_id IS NOT NULL AND "
              "(LOWER(COALESCE(subject,'')) LIKE :q OR LOWER(COALESCE(from_email,'')) LIKE :q OR LOWER(COALESCE(to_emails,'')) LIKE :q "
              "OR LOWER(COALESCE(body_text,'')) LIKE :q) ORDER BY id DESC LIMIT :l", q=lk, l=limit)
    out["emails"] = em
    missing = [e["alert_id"] for e in em if e["alert_id"] not in ids]
    if missing:
        for r in db.q_in(f"SELECT {cols} FROM alerts WHERE id IN :i", "i", sorted(set(missing))):
            ids[r["id"]] = r
    out["alerts"] = sorted(ids.values(), key=lambda r: -r["id"])[:limit]
    return out
