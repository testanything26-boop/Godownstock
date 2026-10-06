"""Godown Stock - server edition (Flask + Postgres).

Free Render web service. Data lives in a free Supabase Postgres database.
Login with admin / staff roles. Serves the single-page app from templates/.
"""
import io
import csv
import os
import time
from functools import wraps
from threading import Lock

from flask import Flask, request, jsonify, session, Response

import db

# Serializes roll mutations inside this worker (two phones cutting the SAME
# roll can't interleave their read-modify-write). render.yaml runs 1 worker.
write_lock = Lock()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me")
APP_URL = os.environ.get("APP_URL", "").rstrip("/")


def me():
    return session.get("user")


def login_required(f):
    @wraps(f)
    def w(*a, **kw):
        if not me():
            return jsonify({"error": "login"}), 401
        return f(*a, **kw)
    return w


def admin_required(f):
    @wraps(f)
    def w(*a, **kw):
        u = me()
        if not u:
            return jsonify({"error": "login"}), 401
        if u.get("role") != "admin":
            return jsonify({"error": "forbidden - admin only"}), 403
        return f(*a, **kw)
    return w


def with_db(f):
    """Turn a missing database setup into a readable API error."""
    @wraps(f)
    def w(*a, **kw):
        try:
            return f(*a, **kw)
        except db.ConfigError as e:
            return jsonify({"error": "Server storage not configured: " + str(e)}), 500
    return w


@app.route("/")
def index():
    # Works whether index.html was uploaded to the repo root (GitHub web
    # upload can't do folders) or into templates/.
    base = os.path.dirname(os.path.abspath(__file__))
    for p in (os.path.join(base, "index.html"),
              os.path.join(base, "templates", "index.html")):
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return f.read().replace("{{ app_url }}", APP_URL)
    return ("index.html is missing — upload it to the repo root "
            "(Add file → Upload files)."), 500


@app.route("/api/login", methods=["POST"])
@with_db
def login():
    d = request.get_json(force=True)
    u = db.verify_user(d.get("username", ""), d.get("password", ""))
    if not u:
        return jsonify({"error": "Invalid username or password"}), 401
    session["user"] = {"username": u["username"], "role": u["role"]}
    return jsonify({"ok": True, "user": session["user"]})


@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/state")
@login_required
@with_db
def state():
    return jsonify({
        "user": me(),
        "rolls": db.get_rolls(),
        "locations": db.list_locations(),
        "recentHistory": db.get_recent_history(),
        "settings": db.get_settings(),
    })


# ---------------------------------------------------------------- rolls
@app.route("/api/rolls/next-id")
@login_required
@with_db
def next_id():
    return jsonify({"id": db.peek_next_id()})


@app.route("/api/rolls", methods=["POST"])
@login_required
@with_db
def create_roll():
    with write_lock:
        try:
            return jsonify(db.create_roll(request.get_json(force=True)))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400


@app.route("/api/rolls/bulk", methods=["POST"])
@login_required
@with_db
def bulk_create():
    with write_lock:
        try:
            return jsonify({"ids": db.bulk_create(request.get_json(force=True))})
        except ValueError as e:
            return jsonify({"error": str(e)}), 400


@app.route("/api/rolls/<rid>/history")
@login_required
@with_db
def roll_history(rid):
    return jsonify(db.get_roll_history(rid))


@app.route("/api/rolls/<rid>", methods=["PUT"])
@login_required
@with_db
def update_roll(rid):
    with write_lock:
        try:
            return jsonify(db.update_roll(rid, request.get_json(force=True)))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400


@app.route("/api/rolls/<rid>/cut", methods=["POST"])
@login_required
@with_db
def log_cut(rid):
    with write_lock:
        try:
            return jsonify(db.log_cut(rid, request.get_json(force=True)))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400


@app.route("/api/rolls/<rid>", methods=["DELETE"])
@admin_required
@with_db
def delete_roll(rid):
    with write_lock:
        db.delete_roll(rid)
        return jsonify({"ok": True})


# ---------------------------------------------------------------- locations (admin)
@app.route("/api/locations")
@login_required
@with_db
def list_locations():
    return jsonify(db.list_locations())


@app.route("/api/locations", methods=["POST"])
@admin_required
@with_db
def create_location():
    with write_lock:
        try:
            return jsonify(db.create_location((request.get_json(force=True) or {}).get("name")))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400


@app.route("/api/locations/<int:lid>", methods=["PUT"])
@admin_required
@with_db
def rename_location(lid):
    with write_lock:
        try:
            return jsonify(db.rename_location(lid, (request.get_json(force=True) or {}).get("name")))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400


@app.route("/api/locations/<int:lid>", methods=["DELETE"])
@admin_required
@with_db
def delete_location(lid):
    with write_lock:
        db.delete_location(lid)
        return jsonify({"ok": True})


# ---------------------------------------------------------------- users (admin)
@app.route("/api/users")
@admin_required
@with_db
def list_users():
    return jsonify(db.list_users())


@app.route("/api/users", methods=["POST"])
@admin_required
@with_db
def create_user():
    with write_lock:
        d = request.get_json(force=True)
        try:
            db.create_user(d.get("username", ""), d.get("password", ""), d.get("role", "staff"))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"ok": True})


@app.route("/api/users/<username>", methods=["DELETE"])
@admin_required
@with_db
def delete_user(username):
    with write_lock:
        if username == me()["username"]:
            return jsonify({"error": "You cannot delete your own account"}), 400
        db.delete_user(username)
        return jsonify({"ok": True})


# ---------------------------------------------------------------- settings
@app.route("/api/settings", methods=["PUT"])
@login_required
@with_db
def save_settings():
    with write_lock:
        db.set_settings(request.get_json(force=True))
        return jsonify({"ok": True})


# ---------------------------------------------------------------- import
@app.route("/api/import", methods=["POST"])
@admin_required
@with_db
def import_csv():
    with write_lock:
        f = request.files.get("file")
        if not f:
            return jsonify({"error": "No file uploaded"}), 400
        try:
            ids = db.import_csv(f.read().decode("utf-8-sig"))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"ids": ids, "count": len(ids)})


# ---------------------------------------------------------------- reports
@app.route("/api/reports/stock.csv")
@login_required
@with_db
def rep_stock():
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Roll ID", "Fabric", "Colour", "Dia", "GSM", "Weight kg",
                "In stock kg", "Manufacturer", "Godown", "Created", "Last used",
                "Status", "Styles"])
    for r in db.get_rolls():
        w.writerow([r["id"], r["fabricType"], r["color"], r["dia"], r["gsm"],
                    r["weight"], r["currentWeight"], r["manufacturer"],
                    r["location"], r["createdDate"], r["lastUsedDate"], r["status"],
                    "; ".join(r["styles"])])
    return Response(
        buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition":
                 "attachment; filename=godown-stock-%s.csv" % time.strftime("%Y%m%d")})


@app.route("/api/reports/cutting.csv")
@login_required
@with_db
def rep_cut():
    frm = request.args.get("from", "")
    to = request.args.get("to", "")
    hist = db.get_history(frm, to)
    rolls = {r["id"]: r for r in db.get_rolls()}
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Date", "Roll ID", "Fabric", "Colour", "Action", "Style",
                "Used kg", "Left kg", "Notes"])
    for h in hist:
        r = rolls.get(h["rollId"], {})
        w.writerow([h["date"], h["rollId"], r.get("fabricType", ""),
                    r.get("color", ""), h["type"], h["style"], h["weightUsed"],
                    h["remaining"], h["notes"]])
    return Response(
        buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition":
                 "attachment; filename=godown-cutting-%s.csv" % time.strftime("%Y%m%d")})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
