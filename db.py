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
        url = os.environ.get("DATABASE_URL", "").strip()
        if not url:
            raise ConfigError("Set the DATABASE_URL env var (see SETUP.md).")
        try:
            import psycopg2  # noqa
            from psycopg2 import pool as pgpool
        except ImportError:
            raise ConfigError("Database driver not installed — check the Render build logs.")
        try:
            _pool = pgpool.ThreadedConnectionPool(1, 8, url)
        except Exception as e:
            raise ConfigError(
                "Could not reach the database. Check DATABASE_URL in Render: use the "
                "port-6543 pooler string, replace [YOUR-PASSWORD] with the real password, "
                "and URL-encode special characters in the password "
                "(@ → %40, # → %23, / → %2F, ? → %3F).") from e
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


def _ensure_location_schema(c):
    """Idempotent migration for the multi-godown feature (v2.1): locations
    table + rolls.location_id. Runs on every login via ensure_seed, so
    existing deployments upgrade without touching the SQL editor."""
    _q(c, "CREATE TABLE IF NOT EXISTS locations"
          " (id serial primary key, name text unique not null)")
    _q(c, "ALTER TABLE rolls ADD COLUMN IF NOT EXISTS"
          " location_id integer REFERENCES locations(id)")


def _ensure_wastage_schema(c):
    """Idempotent migration for wastage bags (v2.2)."""
    _q(c, "CREATE TABLE IF NOT EXISTS wastage_bags ("
          "id text primary key, weight numeric default 0, "
          "created_date text default '', status text default 'in-stock', "
          "buyer_name text default '', buyer_phone text default '', "
          "sold_date text default '', notes text default '')")
    _q(c, "ALTER TABLE wastage_bags ADD COLUMN IF NOT EXISTS"
          " rate_per_kg numeric default 0")
    _q(c, "ALTER TABLE wastage_bags ADD COLUMN IF NOT EXISTS"
          " total_amount numeric default 0")


def _location_id(c, lid):
    """Validate an incoming location id; returns int or None."""
    if lid in (None, "", 0, "0"):
        return None
    try:
        lid = int(lid)
    except (TypeError, ValueError):
        raise ValueError("Unknown godown location")
    _ensure_location_schema(c)
    if not _q(c, "SELECT 1 FROM locations WHERE id=%s", (lid,)).fetchone():
        raise ValueError("Unknown godown location")
    return lid


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
        "locationId": d.get("location_id"),
        "location": d.get("location_name") or "",
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
        _ensure_location_schema(c)
        _ensure_wastage_schema(c)
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
def peek_next_id():
    """The ID the next auto-created roll will get (informational only;
    the real assignment happens at save time)."""
    import re
    with _conn() as c:
        r = _q(c, "SELECT value FROM meta WHERE key='roll_seq'").fetchone()
        seq = int(r["value"]) if r else 0
        m = 0
        for row in _q(c, "SELECT id FROM rolls WHERE id LIKE 'R-%%'").fetchall():
            mm = re.match(r"^R-(\d+)$", (row["id"] or "").upper())
            if mm:
                m = max(m, int(mm.group(1)))
        return "R-%04d" % (max(seq, m) + 1)


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
    r = _q(c, "SELECT r.*, l.name AS location_name FROM rolls r"
              " LEFT JOIN locations l ON l.id=r.location_id WHERE r.id=%s",
           (rid,)).fetchone()
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
              " manufacturer, created_date, status, styles, last_used_date, notes, location_id)"
              " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'in-stock','','',%s,%s)",
           (rid, data.get("fabricType", ""), data.get("color", ""), data.get("dia", ""),
            data.get("gsm", ""), weight, weight, data.get("manufacturer", ""),
            data.get("createdDate", ""), data.get("notes", ""),
            _location_id(c, data.get("locationId"))))
        _add_history(c, rid, data.get("createdDate", ""), "created", "", 0, weight, "Roll created")
        return _get_roll(c, rid)


def bulk_create(data):
    rows = (data or {}).get("rolls") or []
    if not 1 <= len(rows) <= 500:
        raise ValueError("Give 1–500 rolls")
    ids = []
    with _conn() as c:
        loc = _location_id(c, (data or {}).get("locationId"))
        for r in rows:
            weight = round(float(r.get("weight") or 0), 2)
            if weight <= 0:
                raise ValueError("Every roll needs a weight above 0")
            rid, _ = _next_id(c)
            _q(c, "INSERT INTO rolls(id, fabric_type, color, dia, gsm, weight, current_weight,"
                  " manufacturer, created_date, status, styles, last_used_date, notes, location_id)"
                  " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'in-stock','','',%s,%s)",
               (rid, r.get("fabricType", ""), r.get("color", ""), r.get("dia", ""),
                r.get("gsm", ""), weight, weight, r.get("manufacturer", ""),
                r.get("createdDate", ""), r.get("notes", ""), loc))
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
              " manufacturer=%s, created_date=%s, notes=%s, location_id=%s WHERE id=%s",
           (data.get("fabricType", r["fabric_type"]), data.get("color", r["color"]),
            data.get("dia", r["dia"]), data.get("gsm", r["gsm"]),
            data.get("manufacturer", r["manufacturer"]),
            data.get("createdDate", r["created_date"]),
            data.get("notes", r["notes"]),
            _location_id(c, data.get("locationId")), rid))
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
        _ensure_location_schema(c)
        rs = _q(c, "SELECT r.*, l.name AS location_name FROM rolls r"
                   " LEFT JOIN locations l ON l.id=r.location_id"
                   " ORDER BY r.id").fetchall()
    return [_roll(r) for r in rs]


def get_roll_history(rid):
    with _conn() as c:
        rs = _q(c, "SELECT * FROM history WHERE roll_id=%s ORDER BY date DESC, hid DESC",
                (rid,)).fetchall()
    return [_hist(r) for r in rs]


def get_recent_history(days=60):
    """History entries from the last N days, newest first — powers the
    dashboard KPIs and recent-activity feed."""
    import datetime
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    with _conn() as c:
        rs = _q(c, "SELECT * FROM history WHERE date >= %s ORDER BY date DESC, hid DESC",
                (cutoff,)).fetchall()
    return [_hist(r) for r in rs]


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


# ---------------------------------------------------------------- locations
def list_locations():
    with _conn() as c:
        _ensure_location_schema(c)
        rs = _q(c, "SELECT id, name FROM locations ORDER BY name").fetchall()
    return [{"id": r["id"], "name": r["name"]} for r in rs]


def create_location(name):
    name = (name or "").strip()
    if not name:
        raise ValueError("Location name is required")
    with _conn() as c:
        _ensure_location_schema(c)
        ex = _q(c, "SELECT 1 FROM locations WHERE LOWER(name)=LOWER(%s)", (name,)).fetchone()
        if ex:
            raise ValueError("A godown with that name already exists")
        r = _q(c, "INSERT INTO locations(name) VALUES (%s) RETURNING id, name", (name,)).fetchone()
    return {"id": r["id"], "name": r["name"]}


def rename_location(lid, name):
    name = (name or "").strip()
    if not name:
        raise ValueError("Location name is required")
    with _conn() as c:
        _ensure_location_schema(c)
        ex = _q(c, "SELECT 1 FROM locations WHERE LOWER(name)=LOWER(%s) AND id<>%s",
                (name, lid)).fetchone()
        if ex:
            raise ValueError("A godown with that name already exists")
        cur = _q(c, "UPDATE locations SET name=%s WHERE id=%s RETURNING id, name",
                 (name, lid)).fetchone()
        if not cur:
            raise ValueError("Location not found")
    return {"id": cur["id"], "name": cur["name"]}


def delete_location(lid):
    with _conn() as c:
        _ensure_location_schema(c)
        _q(c, "UPDATE rolls SET location_id=NULL WHERE location_id=%s", (lid,))
        _q(c, "DELETE FROM locations WHERE id=%s", (lid,))


# ---------------------------------------------------------------- wastage bags
def _wastage(d):
    return {
        "id": d["id"],
        "weight": float(d.get("weight") or 0),
        "createdDate": d.get("created_date") or "",
        "status": d.get("status") or "in-stock",
        "buyerName": d.get("buyer_name") or "",
        "buyerPhone": d.get("buyer_phone") or "",
        "ratePerKg": float(d.get("rate_per_kg") or 0),
        "totalAmount": float(d.get("total_amount") or 0),
        "soldDate": d.get("sold_date") or "",
        "notes": d.get("notes") or "",
    }


def _next_wastage_id(c):
    import re
    r = _q(c, "SELECT value FROM meta WHERE key='wastage_seq'").fetchone()
    seq = int(r["value"]) if r else 0
    m = 0
    for row in _q(c, "SELECT id FROM wastage_bags WHERE id LIKE 'W-%%'").fetchall():
        mm = re.match(r"^W-(\d+)$", (row["id"] or "").upper())
        if mm:
            m = max(m, int(mm.group(1)))
    seq = max(seq, m) + 1
    while True:
        bid = "W-%04d" % seq
        if not _q(c, "SELECT 1 FROM wastage_bags WHERE id=%s", (bid,)).fetchone():
            break
        seq += 1
    _q(c, "INSERT INTO meta(key, value) VALUES ('wastage_seq', %s) "
           "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", (str(seq),))
    return bid


def _get_wastage(c, bid):
    r = _q(c, "SELECT * FROM wastage_bags WHERE id=%s", (bid,)).fetchone()
    return _wastage(r) if r else None


def get_wastage_bags():
    with _conn() as c:
        _ensure_wastage_schema(c)
        rs = _q(c, "SELECT * FROM wastage_bags ORDER BY id").fetchall()
    return [_wastage(r) for r in rs]


def create_wastage_bag(data):
    data = data or {}
    weight = round(float(data.get("weight") or 0), 2)
    if weight <= 0:
        raise ValueError("Weight must be above 0")
    with _conn() as c:
        _ensure_wastage_schema(c)
        bid = (data.get("id") or "").strip().upper()
        if bid:
            if _q(c, "SELECT 1 FROM wastage_bags WHERE id=%s", (bid,)).fetchone():
                raise ValueError("Bag ID %s already exists" % bid)
        else:
            bid = _next_wastage_id(c)
        _q(c, "INSERT INTO wastage_bags(id, weight, created_date, status, notes)"
              " VALUES (%s,%s,%s,'in-stock',%s)",
           (bid, weight, data.get("createdDate", ""), data.get("notes", "")))
        return _get_wastage(c, bid)


def update_wastage_bag(bid, data):
    data = data or {}
    with _conn() as c:
        _ensure_wastage_schema(c)
        r = _q(c, "SELECT * FROM wastage_bags WHERE id=%s", (bid,)).fetchone()
        if not r:
            raise ValueError("Bag not found")
        if r["status"] == "sold":
            raise ValueError("Bag is already sold — it cannot be edited")
        weight = round(float(data.get("weight", r["weight"]) or 0), 2)
        if weight <= 0:
            raise ValueError("Weight must be above 0")
        _q(c, "UPDATE wastage_bags SET weight=%s, created_date=%s, notes=%s WHERE id=%s",
           (weight, data.get("createdDate", r["created_date"]),
            data.get("notes", r["notes"]), bid))
        return _get_wastage(c, bid)


def sell_wastage_bag(bid, data):
    data = data or {}
    buyer = (data.get("buyerName") or "").strip()
    if not buyer:
        raise ValueError("Buyer name is required")
    with _conn() as c:
        _ensure_wastage_schema(c)
        r = _q(c, "SELECT * FROM wastage_bags WHERE id=%s", (bid,)).fetchone()
        if not r:
            raise ValueError("Bag not found")
        if r["status"] == "sold":
            raise ValueError("Bag is already sold")
        import datetime
        sold_date = data.get("soldDate") or datetime.date.today().isoformat()
        rate = round(float(data.get("ratePerKg") or 0), 2)
        total = round(float(r["weight"] or 0) * rate, 2)
        _q(c, "UPDATE wastage_bags SET status='sold', buyer_name=%s,"
              " rate_per_kg=%s, total_amount=%s,"
              " sold_date=%s, notes=%s WHERE id=%s",
           (buyer, rate, total, sold_date,
            data.get("notes", r["notes"]), bid))
        return _get_wastage(c, bid)


def delete_wastage_bag(bid):
    with _conn() as c:
        _ensure_wastage_schema(c)
        _q(c, "DELETE FROM wastage_bags WHERE id=%s", (bid,))


# ---------------------------------------------------------------- settings
def get_settings():
    with _conn() as c:
        rs = _q(c, "SELECT key, value FROM meta").fetchall()
    m = {r["key"]: r["value"] for r in rs}
    try:
        low = float(m.get("lowKg", 5))
    except ValueError:
        low = 5

    def _f(key, default):
        try:
            return float(m.get(key, default))
        except (ValueError, TypeError):
            return default

    def _i(key, default):
        try:
            return int(float(m.get(key, default)))
        except (ValueError, TypeError):
            return default

    return {"lowKg": low, "labelPreset": m.get("labelPreset", "8"),
            "companyName": m.get("companyName", "Chakra Production"),
            "labelCustomW": _f("labelCustomW", 100),
            "labelCustomH": _f("labelCustomH", 20),
            "labelCustomCols": _i("labelCustomCols", 2),
            "labelQrMm": _f("labelQrMm", 0)}


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
        if "companyName" in d:
            _q(c, "INSERT INTO meta(key, value) VALUES ('companyName', %s)"
                   " ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
               (str(d["companyName"] or "").strip() or "Chakra Production",))
        for _k in ("labelCustomW", "labelCustomH", "labelCustomCols",
                   "labelQrMm"):
            if _k in d:
                _q(c, "INSERT INTO meta(key, value) VALUES (%s, %s)"
                       " ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                   (_k, str(d[_k])))
