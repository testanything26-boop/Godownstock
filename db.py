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


def _meta_set(c, key, value):
    """Portable meta upsert (UPDATE, INSERT if absent).

    The previous INSERT ... ON CONFLICT form silently depends on the live
    meta table having a primary key on `key`; where it does not, Postgres
    raises a non-ValueError and the caller dies with a bare HTTP 500 (this
    broke bulk roll entry). UPDATE-then-INSERT works either way. Safe
    under the app's single worker + write lock."""
    cur = _q(c, "UPDATE meta SET value=%s WHERE key=%s", (str(value), key))
    if cur.rowcount == 0:
        _q(c, "INSERT INTO meta(key, value) VALUES (%s, %s)", (key, str(value)))


def _ensure_location_schema(c):
    """Idempotent migration for the multi-godown feature (v2.1): locations
    table + rolls.location_id. Runs on every login via ensure_seed, so
    existing deployments upgrade without touching the SQL editor."""
    _q(c, "CREATE TABLE IF NOT EXISTS locations"
          " (id serial primary key, name text unique not null)")
    _q(c, "ALTER TABLE rolls ADD COLUMN IF NOT EXISTS"
          " location_id integer REFERENCES locations(id)")


def _ensure_history_schema(c):
    """Idempotent migration for cutting-weight returns (v2.16): history
    gains ref_id, linking a 'return' entry to the cutting entry it
    returns weight against."""
    _q(c, "ALTER TABLE history ADD COLUMN IF NOT EXISTS ref_id integer")


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
        "refId": d.get("ref_id"),
    }


# ---------------------------------------------------------------- seed / users
def ensure_seed():
    au = os.environ.get("ADMIN_USER", "admin").strip() or "admin"
    ap = os.environ.get("ADMIN_PASS", "").strip()
    with _conn() as c:
        _ensure_location_schema(c)
        _ensure_wastage_schema(c)
        _ensure_history_schema(c)
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
def _date_prefix(created_date):
    """Roll ID prefix from the inward date (v2.12): 'R' + DDMMYY, e.g.
    inward date 2026-10-29 -> 'R291026'. A missing/unreadable date falls
    back to today, so an ID can always be formed."""
    import datetime
    s = (created_date or "").strip()
    d = None
    if s:
        try:
            d = datetime.date.fromisoformat(s[:10])
        except ValueError:
            d = None
    if d is None:
        d = datetime.date.today()
    return "R%02d%02d%02d" % (d.day, d.month, d.year % 100)


def _max_seq_for(c, prefix):
    """Highest sequence number already used for one date prefix
    (ids look like R291026-7). Scans the real rows, so numbering always
    matches what exists — no separate counter to drift or crash."""
    import re
    pat = re.compile(r"^" + re.escape(prefix) + r"-(\d+)$")
    m = 0
    for row in _q(c, "SELECT id FROM rolls WHERE id LIKE %s",
                  (prefix + "-%",)).fetchall():
        mm = pat.match((row["id"] or "").upper())
        if mm:
            m = max(m, int(mm.group(1)))
    return m


def peek_next_id(created_date=None):
    """The ID the next auto-created roll inwarded on `created_date`
    (default: today) will get (informational only; the real assignment
    happens at save time)."""
    prefix = _date_prefix(created_date)
    with _conn() as c:
        return "%s-%d" % (prefix, _max_seq_for(c, prefix) + 1)


def _next_id(c, created_date=None):
    prefix = _date_prefix(created_date)
    seq = _max_seq_for(c, prefix)
    while True:
        seq += 1
        rid = "%s-%d" % (prefix, seq)
        if not _q(c, "SELECT 1 FROM rolls WHERE id=%s", (rid,)).fetchone():
            break
    return rid, seq


def _bump_seq_for(c, rid):
    # Kept for API compatibility with create_roll's custom-ID path.
    # Date-based numbering is derived from the rows themselves (see
    # _max_seq_for), so a caller-supplied ID needs no counter upkeep.
    return


def _get_roll(c, rid):
    r = _q(c, "SELECT r.*, l.name AS location_name FROM rolls r"
              " LEFT JOIN locations l ON l.id=r.location_id WHERE r.id=%s",
           (rid,)).fetchone()
    return _roll(r) if r else None


def _add_history(c, rid, date, typ, style, used, remaining, notes, ref_id=None):
    _q(c, "INSERT INTO history(roll_id, date, type, style, weight_used, remaining, notes, ref_id)"
          " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
       (rid, date or "", typ, style or "", used, remaining, notes or "", ref_id))


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
            rid, _ = _next_id(c, data.get("createdDate"))
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
    # Validate every weight BEFORE touching the database, naming the roll
    # position so one bad line in a pasted list is easy to find and fix.
    weights = []
    for i, r in enumerate(rows, 1):
        try:
            w = round(float(r.get("weight") or 0), 2)
        except (TypeError, ValueError):
            raise ValueError("Roll %d: weight is not a number" % i)
        if w <= 0:
            raise ValueError("Roll %d: every roll needs a weight above 0" % i)
        weights.append(w)
    with _conn() as c:
        # The bulk form sends locationId on every roll; the CSV importer
        # sends it at the top level. Accept either.
        loc_src = (data or {}).get("locationId")
        if loc_src is None and rows:
            loc_src = rows[0].get("locationId")
        loc = _location_id(c, loc_src)
        # Fast batch path (v2.11, date IDs v2.12): allocate ALL ids in one
        # pass — for each inward date in the batch, read that date's
        # highest used sequence once, then hand out numbers in memory —
        # then ONE multi-row INSERT for the rolls and ONE for their
        # history rows. The old loop used ~5 database round trips per
        # roll, so a large inward batch crawled and risked a timeout.
        seq_by_prefix = {}
        ids = []
        for r in rows:
            prefix = _date_prefix(r.get("createdDate"))
            if prefix not in seq_by_prefix:
                seq_by_prefix[prefix] = _max_seq_for(c, prefix)
            seq_by_prefix[prefix] += 1
            ids.append("%s-%d" % (prefix, seq_by_prefix[prefix]))
        roll_sql = ("INSERT INTO rolls(id, fabric_type, color, dia, gsm, weight, current_weight,"
                    " manufacturer, created_date, status, styles, last_used_date, notes, location_id) VALUES "
                    + ",".join("(%s,%s,%s,%s,%s,%s,%s,%s,%s,'in-stock','','',%s,%s)" for _ in rows))
        params = []
        for rid, r, w in zip(ids, rows, weights):
            params += [rid, r.get("fabricType", ""), r.get("color", ""), r.get("dia", ""),
                       r.get("gsm", ""), w, w, r.get("manufacturer", ""),
                       r.get("createdDate", ""), r.get("notes", ""), loc]
        _q(c, roll_sql, tuple(params))
        hist_sql = ("INSERT INTO history(roll_id, date, type, style, weight_used, remaining, notes) VALUES "
                    + ",".join("(%s,%s,'created','',0,%s,'Roll created (bulk inward)')" for _ in rows))
        hparams = []
        for rid, r, w in zip(ids, rows, weights):
            hparams += [rid, r.get("createdDate", ""), w]
        _q(c, hist_sql, tuple(hparams))
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


_BULK_EDIT_COLS = {
    "manufacturer": "manufacturer",
    "color": "color",
    "fabricType": "fabric_type",
    "dia": "dia",
    "gsm": "gsm",
}
_BULK_EDIT_LABELS = {
    "manufacturer": "Company", "color": "Colour", "fabricType": "Fabric",
    "dia": "Dia", "gsm": "GSM", "locationId": "Godown",
}


def bulk_update(ids, fields):
    """Apply the same field changes to many rolls at once (v2.15 bulk edit).

    Only the whitelisted fields are touched; each roll keeps every other
    value. One history entry per roll records old -> new for the audit
    trail. All-or-nothing: any bad id/field aborts before any write.
    """
    ids = [str(i) for i in (ids or [])]
    fields = fields or {}
    if not ids:
        raise ValueError("No rolls selected")
    keys = [k for k in fields if k in _BULK_EDIT_COLS or k == "locationId"]
    if not keys:
        raise ValueError("Nothing to change")
    import datetime
    today = datetime.date.today().isoformat()
    with _conn() as c:
        new_loc = _location_id(c, fields.get("locationId")) if "locationId" in keys else None
        loc_names = {}
        for lid in {new_loc}:
            if lid is not None:
                row = _q(c, "SELECT name FROM locations WHERE id=%s", (lid,)).fetchone()
                loc_names[lid] = row["name"] if row else str(lid)
        # Validate everything first so the batch never half-applies.
        rows = {}
        for rid in ids:
            r = _q(c, "SELECT * FROM rolls WHERE id=%s", (rid,)).fetchone()
            if not r:
                raise ValueError("Roll not found: " + rid)
            rows[rid] = r
            if r["location_id"] is not None and r["location_id"] not in loc_names:
                row = _q(c, "SELECT name FROM locations WHERE id=%s", (r["location_id"],)).fetchone()
                loc_names[r["location_id"]] = row["name"] if row else str(r["location_id"])
        for rid in ids:
            r = rows[rid]
            sets, params, notes = [], [], []
            for k in keys:
                if k == "locationId":
                    sets.append("location_id=%s")
                    params.append(new_loc)
                    notes.append("Godown %s -> %s" % (loc_names.get(r["location_id"], "—"),
                                                      loc_names.get(new_loc, "—")))
                else:
                    sets.append(_BULK_EDIT_COLS[k] + "=%s")
                    params.append(fields[k])
                    notes.append("%s %s -> %s" % (_BULK_EDIT_LABELS[k], r[_BULK_EDIT_COLS[k]] or "—", fields[k]))
            params.append(rid)
            _q(c, "UPDATE rolls SET " + ", ".join(sets) + " WHERE id=%s", tuple(params))
            _add_history(c, rid, today, "edit", "", 0, float(r["current_weight"] or 0),
                         "Bulk edit: " + "; ".join(notes))
        return {"updated": len(ids)}


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


def log_return(rid, data):
    """Add returned cutting weight back onto a roll (v2.16).

    The return is recorded against ONE cutting entry (data.historyId)
    as its own 'return' history entry — the cutting entry itself is
    never changed. Per his rule, the total returned against a cutting
    entry can never exceed that entry's used kg. An exhausted roll
    comes back to 'in-use' when weight returns.
    """
    data = data or {}
    ret = round(float(data.get("weightReturned") or 0), 2)
    if ret <= 0:
        raise ValueError("Enter the returned weight")
    try:
        hid = int(data.get("historyId"))
    except (TypeError, ValueError):
        raise ValueError("Pick the cutting entry this return belongs to")
    with _conn() as c:
        _ensure_history_schema(c)
        r = _q(c, "SELECT * FROM rolls WHERE id=%s", (rid,)).fetchone()
        if not r:
            raise ValueError("Roll not found")
        e = _q(c, "SELECT * FROM history WHERE hid=%s AND roll_id=%s", (hid, rid)).fetchone()
        if not e or e["type"] != "cutting":
            raise ValueError("Cutting entry not found")
        already = _q(c, "SELECT COALESCE(SUM(weight_used),0) AS s FROM history"
                        " WHERE ref_id=%s AND type='return'", (hid,)).fetchone()["s"]
        max_ret = round(float(e["weight_used"] or 0) - float(already or 0), 2)
        if ret > max_ret + 0.001:
            raise ValueError("Only %.2f kg can still be returned against this cutting" % max_ret)
        import datetime
        date = data.get("date") or datetime.date.today().isoformat()
        remaining = round(min(float(r["weight"] or 0), float(r["current_weight"] or 0) + ret), 2)
        status = "in-use" if remaining > 0.01 else r["status"]
        _q(c, "UPDATE rolls SET current_weight=%s, status=%s WHERE id=%s",
           (remaining, status, rid))
        notes = (data.get("notes") or "").strip() or (
            "Returned against the %s cutting" % (e["date"] or "earlier"))
        _add_history(c, rid, date, "return", e["style"] or "", ret, remaining, notes, ref_id=hid)
        return _get_roll(c, rid)


def delete_roll(rid):
    with _conn() as c:
        _q(c, "DELETE FROM history WHERE roll_id=%s", (rid,))
        _q(c, "DELETE FROM rolls WHERE id=%s", (rid,))


def erase_preview():
    with _conn() as c:
        return {
            "rolls": _q(c, "SELECT COUNT(*) AS n FROM rolls").fetchone()["n"],
            "history": _q(c, "SELECT COUNT(*) AS n FROM history").fetchone()["n"],
            "bags": _q(c, "SELECT COUNT(*) AS n FROM wastage_bags").fetchone()["n"],
            "locations": _q(c, "SELECT COUNT(*) AS n FROM locations").fetchone()["n"],
        }


def erase_all_data():
    # Wipe every transactional record for a fresh start. Keeps users (so the
    # admin can still log in) and all other meta settings (company name,
    # label preset, low-stock threshold). Roll numbering is date-based
    # (v2.12) and derived from the rolls themselves, so with every roll
    # deleted it restarts on its own: the first roll inwarded on a date
    # gets R<DDMMYY>-1 for that date. Deleting the wastage_seq key resets
    # bag numbering: next bag is W-0001.
    with _conn() as c:
        counts = {
            "rolls": _q(c, "SELECT COUNT(*) AS n FROM rolls").fetchone()["n"],
            "history": _q(c, "SELECT COUNT(*) AS n FROM history").fetchone()["n"],
            "bags": _q(c, "SELECT COUNT(*) AS n FROM wastage_bags").fetchone()["n"],
            "locations": _q(c, "SELECT COUNT(*) AS n FROM locations").fetchone()["n"],
        }
        _q(c, "DELETE FROM history")
        _q(c, "DELETE FROM rolls")
        _q(c, "DELETE FROM wastage_bags")
        _q(c, "DELETE FROM locations")
        _q(c, "DELETE FROM meta WHERE key IN ('roll_seq', 'wastage_seq')")
        return counts


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
    _meta_set(c, "wastage_seq", seq)
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
            "labelCustomRows": _i("labelCustomRows", 0),
            "labelQrMm": _f("labelQrMm", 0)}


def set_settings(d):
    d = d or {}
    with _conn() as c:
        if "lowKg" in d:
            _meta_set(c, "lowKg", float(d["lowKg"] or 5))
        if "labelPreset" in d:
            _meta_set(c, "labelPreset", d["labelPreset"])
        if "companyName" in d:
            _meta_set(c, "companyName",
                      str(d["companyName"] or "").strip() or "Chakra Production")
        for _k in ("labelCustomW", "labelCustomH", "labelCustomCols",
                   "labelCustomRows", "labelQrMm"):
            if _k in d:
                _meta_set(c, _k, d[_k])
