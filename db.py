"""Postgres backend for Godown Stock server edition.

Free Supabase project (no card). Tables:
  rolls   - one row per fabric roll
  history - every creation / cutting / adjustment event
  users   - login accounts (passwords hashed via werkzeug)
  meta    - key/value settings (lowKg, labelPreset, roll_seq)

Run schema.sql once in the Supabase SQL editor to create the tables.
"""
import os
import csv
import io
from contextlib import contextmanager

from werkzeug.security import generate_password_hash, check_password_hash


class ConfigError(RuntimeError):
    pass


_pool = None


def _get_pool():
    """Lazily create a small threaded connection pool."""
    global _pool
    if _pool is None:
        import psycopg2
        from psycopg2 import pool as pgpool
        url = os.environ.get("DATABASE_URL", "").strip()
        if not url:
            raise ConfigError("Set the DATABASE_URL env var (see SETUP.md).")
        _pool = pgpool.ThreadedConnectionPool(1, 8, url)
    return _pool


@contextmanager
def _conn():
    p = _get_pool()
    c = p.getconn()
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        p.putconn(c)


def _rows(cur):
    from psycopg2.extras import RealDictCursor  # noqa
    return cur.fetchall()


def _q(c, sql, params=()):
    import psycopg2.extras
    cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(sql, params)
    return cur


# ---------------------------------------------------------------- shapes
def _roll(d):
    return {
        "id": d["id"],
        "fabricType": d.get("fabric_type") or "",
        "color": d.get("color") or "",
        "dia": d.get("dia") or "",
        "gsm": d.get("gsm") or "",
        "weight": float(d.get("weight") or 0),
        "currentWeight": float(d.get("current_weight") or 0),
        "manufacturer": d.get("manufacturer") or "",
        "createdDate": d.get("created_date") or "",
        "status": d.get("status") or "in-stock",
        "styles": [s for s in (d.get("styles") or "").split(",") if s],
        "lastUsedDate": d.get("last_used_date") or None,
        "notes": d.get("notes") or "",
    }


def _hist(d):
    return {
        "hid": d["hid"],
        "rollId": d.get("roll_id") or "",
        "date": d.get("date") or "",
        "type": d.get("type") or "",
        "style": d.get("style") or "",
        "weightUsed": float(d.get("weight_used") or 0),
        "remaining": float(d.get("remaining") or 0),
        "notes": d.get("notes") or "",
    }


# ---------------------------------------------------------------- seed / users
def ensure_seed():
    au = os.environ.get("ADMIN_USER", "admin").strip() or "admin"
    ap = os.environ.get("ADMIN_PASS", "").strip()
    with _conn() as c:
        n = _q(c, "SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        if n == 0:
            if not ap:
                raise ConfigError("Set ADMIN_USER and ADMIN_PASS env vars for the first admin account.")
            _q(c, "INSERT INTO users(username, pass_hash, role, created_at) VALUES (%s,%s,'admin',CURRENT_DATE)",
               (au, generate_password_hash(ap)))


def verify_user(username, password):
    ensure_seed()
    with _conn() as c:
        u = _q(c, "SELECT * FROM users WHERE LOWER(username)=LOWER(%s)", (username,)).fetchone()
    if u and check_password_hash(u["pass_hash"], password or ""):
        return {"username": u["username"], "role": u["role"]}
    return None


def create_user(username, password, role="staff"):
    username = (username or "").strip()
    role = "admin" if role == "admin" else "staff"
    if not username or not password:
        raise ValueError("Username and password are required")
    with _conn() as c:
        ex = _q(c, "SELECT 1 FROM users WHERE LOWER(username)=LOWER(%s)", (username,)).fetchone()
        if ex:
            raise ValueError("Username already exists")
        _q(c, "INSERT INTO users(username, pass_hash, role, created_at) VALUES (%s,%s,%s,CURRENT_DATE)",
           (username, generate_password_hash(password), role))


def list_users():
    with _conn() as c:
        rs = _q(c, "SELECT username, role, created_at FROM users ORDER BY username").fetchall()
    return [{"username": r["username"], "role": r["role"],
             "createdAt": str(r["created_at"] or "")} for r in rs]


def delete_user(username):
    with _conn() as c:
        _q(c, "DELETE FROM users WHERE LOWER(username)=LOWER(%s)", (username,))


# ---------------------------------------------------------------- rolls
def _next_id(c):
    r = _q(c, "SELECT value FROM meta WHERE key='roll_seq'").fetchone()
    seq = int(r["value"]) if r else 0
    while True:
        seq += 1
        rid = "R-%04d" % seq
        if not _q(c, "SELECT 1 FROM rolls WHERE id=%s", (rid,)).fetchone():
            break
    _q(c, "INSERT INTO meta(key, value) VALUES ('roll_seq', %s) "
           "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", (str(seq),))
    # if the caller supplied a custom id like R-0100, keep the counter ahead
    return rid, seq


def _bump_seq_for(c, rid):
    import re
    m = re.match(r"^R-(\d+)$", (rid or "").upper())
    if not m:
        return
    n = int(m.group(1))
    r = _q(c, "SELECT value FROM meta WHERE key='roll_seq'").fetchone()
    if not r or int(r["value"]) < n:
        _q(c, "INSERT INTO meta(key, value) VALUES ('roll_seq', %s) "
               "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", (str(n),))


def _get_roll(c, rid):
    r = _q(c, "SELECT * FROM rolls WHERE id=%s", (rid,)).fetchone()
    return _roll(r) if r else None


def _add_history(c, rid, date, typ, style, used, remaining, notes):
    _q(c, "INSERT INTO history(roll_id, date, type, style, weight_used, remaining, notes)"
          " VALUES (%s,%s,%s,%s,%s,%s,%s)",
       (rid, date or "", typ, style or "", used, remaining, notes or ""))


def create_roll(data):
    data = data or {}
    weight = round(float(data.get("weight") or 0), 2)
    if weight <= 0:
        raise ValueError("Weight must be above 0")
    with _conn() as c:
        rid = (data.get("id") or "").strip().upper()
        if rid:
            if _q(c, "SELECT 1 FROM rolls WHERE id=%s", (rid,)).fetchone():
                raise ValueError("Roll ID %s already exists" % rid)
            _bump_seq_for(c, rid)
        else:
            rid, _ = _next_id(c)
        _q(c, "INSERT INTO rolls(id, fabric_type, color, dia, gsm, weight, current_weight,"
              " manufacturer, created_date, status, styles, last_used_date, notes)"
              " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'in-stock','','',%s)",
           (rid, data.get("fabricType", ""), data.get("color", ""), data.get("dia", ""),
            data.get("gsm", ""), weight, weight, data.get("manufacturer", ""),
            data.get("createdDate", ""), data.get("notes", "")))
        _add_history(c, rid, data.get("createdDate", ""), "created", "", 0, weight, "Roll created")
        return _get_roll(c, rid)


def bulk_create(data):
    rows = (data or {}).get("rolls") or []
    if not 1 <= len(rows) <= 500:
        raise ValueError("Give 1–500 rolls")
    ids = []
    with _conn() as c:
        for r in rows:
            weight = round(float(r.get("weight") or 0), 2)
            if weight <= 0:
                raise ValueError("Every roll needs a weight above 0")
            rid, _ = _next_id(c)
            _q(c, "INSERT INTO rolls(id, fabric_type, color, dia, gsm, weight, current_weight,"
                  " manufacturer, created_date, status, styles, last_used_date, notes)"
                  " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'in-stock','','',%s)",
               (rid, r.get("fabricType", ""), r.get("color", ""), r.get("dia", ""),
                r.get("gsm", ""), weight, weight, r.get("manufacturer", ""),
                r.get("createdDate", ""), r.get("notes", "")))
            _add_history(c, rid, r.get("createdDate", ""), "created", "", 0, weight, "Roll created (bulk inward)")
            ids.append(rid)
    return ids


def update_roll(rid, data):
    data = data or {}
    with _conn() as c:
        r = _q(c, "SELECT * FROM rolls WHERE id=%s", (rid,)).fetchone()
        if not r:
            raise ValueError("Roll not found")
        old_w = float(r["weight"] or 0)
        new_w = round(float(data.get("weight", old_w) or 0), 2)
        _q(c, "UPDATE rolls SET fabric_type=%s, color=%s, dia=%s, gsm=%s,"
              " manufacturer=%s, created_date=%s, notes=%s WHERE id=%s",
           (data.get("fabricType", r["fabric_type"]), data.get("color", r["color"]),
            data.get("dia", r["dia"]), data.get("gsm", r["gsm"]),
            data.get("manufacturer", r["manufacturer"]),
            data.get("createdDate", r["created_date"]),
            data.get("notes", r["notes"]), rid))
        if new_w != old_w:
            import datetime
            diff = new_w - old_w
            cur = max(0.0, round(float(r["current_weight"] or 0) + diff, 2))
            status = "exhausted" if cur <= 0.01 else ("in-use" if r["status"] == "exhausted" else r["status"])
            _q(c, "UPDATE rolls SET weight=%s, current_weight=%s, status=%s WHERE id=%s",
               (new_w, cur, status, rid))
            _add_history(c, rid, datetime.date.today().isoformat(), "adjustment", "", 0, cur,
                         "Weight corrected %.2f → %.2f kg" % (old_w, new_w))
        return _get_roll(c, rid)


def log_cut(rid, data):
    data = data or {}
    used = round(float(data.get("weightUsed") or 0), 2)
    if used <= 0:
        raise ValueError("Enter the weight used")
    with _conn() as c:
        r = _q(c, "SELECT * FROM rolls WHERE id=%s", (rid,)).fetchone()
        if not r:
            raise ValueError("Roll not found")
        cur = float(r["current_weight"] or 0)
        if used > cur + 0.001:
            raise ValueError("Only %.2f kg left on this roll" % cur)
        import datetime
        date = data.get("date") or datetime.date.today().isoformat()
        style = (data.get("style") or "").strip()
        remaining = round(max(0.0, cur - used), 2)
        styles = [s for s in (r["styles"] or "").split(",") if s]
        if style and style not in styles:
            styles.append(style)
        last_used = r["last_used_date"] or ""
        if not last_used or date > last_used:
            last_used = date
        status = "exhausted" if remaining <= 0.01 else "in-use"
        _q(c, "UPDATE rolls SET current_weight=%s, status=%s, styles=%s,"
              " last_used_date=%s WHERE id=%s",
           (remaining, status, ",".join(styles), last_used, rid))
        _add_history(c, rid, date, "cutting", style, used, remaining, data.get("notes", ""))
        return _get_roll(c, rid)


def delete_roll(rid):
    with _conn() as c:
        _q(c, "DELETE FROM history WHERE roll_id=%s", (rid,))
        _q(c, "DELETE FROM rolls WHERE id=%s", (rid,))


def get_rolls():
    with _conn() as c:
        rs = _q(c, "SELECT * FROM rolls ORDER BY id").fetchall()
    return [_roll(r) for r in rs]


def get_history(frm=None, to=None):
    sql = ("SELECT h.*, r.fabric_type, r.color FROM history h"
           " LEFT JOIN rolls r ON r.id=h.roll_id WHERE 1=1")
    params = []
    if frm:
        sql += " AND h.date >= %s"; params.append(frm)
    if to:
        sql += " AND h.date <= %s"; params.append(to)
    sql += " ORDER BY h.date DESC, h.hid DESC"
    with _conn() as c:
        rs = _q(c, sql, params).fetchall()
    out = []
    for r in rs:
        d = _hist(r)
        d["fabricType"] = r.get("fabric_type") or ""
        d["color"] = r.get("color") or ""
        out.append(d)
    return out


# ---------------------------------------------------------------- import
def import_csv(text):
    reader = csv.DictReader(io.StringIO(text or ""))
    rows = []
    for ln, row in enumerate(reader, start=2):
        w = (row.get("weight") or "").strip()
        if not w:
            continue
        try:
            weight = round(float(w), 2)
        except ValueError:
            raise ValueError("Line %d: weight '%s' is not a number" % (ln, w))
        if weight <= 0:
            raise ValueError("Line %d: weight must be above 0" % ln)
        rows.append({
            "fabricType": (row.get("fabricType") or "").strip(),
            "color": (row.get("colour") or row.get("color") or "").strip(),
            "dia": (row.get("dia") or "").strip(),
            "gsm": (row.get("gsm") or "").strip(),
            "weight": weight,
            "manufacturer": (row.get("manufacturer") or "").strip(),
            "createdDate": (row.get("createdDate") or "").strip(),
            "notes": (row.get("notes") or "").strip(),
        })
    if not rows:
        raise ValueError("No data rows found")
    if len(rows) > 500:
        raise ValueError("Max 500 rows per import")
    return bulk_create({"rolls": rows})


# ---------------------------------------------------------------- settings
def get_settings():
    with _conn() as c:
        rs = _q(c, "SELECT key, value FROM meta").fetchall()
    m = {r["key"]: r["value"] for r in rs}
    try:
        low = float(m.get("lowKg", 5))
    except ValueError:
        low = 5
    return {"lowKg": low, "labelPreset": m.get("labelPreset", "8")}


def set_settings(d):
    d = d or {}
    with _conn() as c:
        if "lowKg" in d:
            _q(c, "INSERT INTO meta(key, value) VALUES ('lowKg', %s)"
                   " ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
               (str(float(d["lowKg"] or 5)),))
        if "labelPreset" in d:
            _q(c, "INSERT INTO meta(key, value) VALUES ('labelPreset', %s)"
                   " ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
               (str(d["labelPreset"]),))
