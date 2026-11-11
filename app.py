"""Small owner-only file sharing service with atomic, verified uploads."""

import hashlib
import hmac
import os
import secrets
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, render_template_string, request, send_file, session, url_for
from werkzeug.security import check_password_hash


def create_app(config=None):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get("SESSION_SECRET"),
        OWNER_PASSWORD_HASH=os.environ.get("OWNER_PASSWORD_HASH"),
        UPLOAD_TOKEN_SHA256=os.environ.get("UPLOAD_TOKEN_SHA256"),
        PUBLIC_BASE_URL=os.environ.get("PUBLIC_BASE_URL", "").rstrip("/"),
        DATA_DIR=os.environ.get("DATA_DIR", "/data"),
        MAX_UPLOAD_BYTES=int(os.environ.get("MAX_UPLOAD_BYTES", str(250 * 1024 * 1024))),
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )
    if config:
        app.config.update(config)
    for key in ("SECRET_KEY", "OWNER_PASSWORD_HASH", "UPLOAD_TOKEN_SHA256", "PUBLIC_BASE_URL"):
        if not app.config[key]:
            raise RuntimeError(f"Missing required configuration: {key}")
    if not app.config["PUBLIC_BASE_URL"].startswith("https://") and not app.testing:
        raise RuntimeError("PUBLIC_BASE_URL must use HTTPS")

    data_dir = Path(app.config["DATA_DIR"])
    files_dir = data_dir / "files"
    temp_dir = data_dir / "tmp"
    files_dir.mkdir(parents=True, exist_ok=True)
    temp_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "shares.sqlite3"
    app.config["MAX_CONTENT_LENGTH"] = app.config["MAX_UPLOAD_BYTES"] + 1024 * 1024

    @contextmanager
    def db():
        conn = sqlite3.connect(db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    with db() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""CREATE TABLE IF NOT EXISTS shares (
            id TEXT PRIMARY KEY, filename TEXT NOT NULL, size INTEGER NOT NULL,
            sha256 TEXT NOT NULL, created_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1
        )""")

    def is_owner():
        return session.get("owner") is True

    def is_api_user():
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return False
        supplied = hashlib.sha256(header[7:].encode()).hexdigest()
        return hmac.compare_digest(supplied, app.config["UPLOAD_TOKEN_SHA256"])

    def csrf_ok():
        supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token", "")
        return is_owner() and hmac.compare_digest(supplied, session.get("csrf_token", ""))

    def share_url(share_id):
        return f"{app.config['PUBLIC_BASE_URL']}/s/{share_id}"

    @app.after_request
    def security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(413)
    def too_large(_):
        return jsonify(error="size_limit", message="Upload exceeds the configured size limit"), 413

    @app.get("/healthz")
    def health():
        return jsonify(status="ok")

    @app.get("/")
    def home():
        if not is_owner():
            return render_template_string(LOGIN_HTML, error=None)
        with db() as conn:
            rows = conn.execute("SELECT * FROM shares WHERE active=1 ORDER BY created_at DESC").fetchall()
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_urlsafe(32)
        shares = [dict(row, url=share_url(row["id"])) for row in rows]
        return render_template_string(HOME_HTML, shares=shares, csrf=session["csrf_token"], max_mb=app.config["MAX_UPLOAD_BYTES"] // 1048576)

    @app.post("/login")
    def login():
        password = request.form.get("password", "")
        if not check_password_hash(app.config["OWNER_PASSWORD_HASH"], password):
            return render_template_string(LOGIN_HTML, error="Incorrect password"), 401
        session.clear()
        session["owner"] = True
        session["csrf_token"] = secrets.token_urlsafe(32)
        return redirect(url_for("home"))

    @app.post("/logout")
    def logout():
        if not csrf_ok():
            abort(403)
        session.clear()
        return redirect(url_for("home"))

    @app.post("/api/uploads")
    def upload():
        if not (is_api_user() or csrf_ok()):
            return jsonify(error="unauthorized", message="Upload credential required"), 401
        incoming = request.files.get("file")
        if incoming is None or not incoming.filename:
            return jsonify(error="validation", message="A file and filename are required"), 400
        filename = incoming.filename.replace("\\", "/").split("/")[-1]
        if not filename or len(filename.encode("utf-8")) > 255 or any(ord(c) < 32 for c in filename):
            return jsonify(error="validation", message="Invalid filename"), 400
        client_sha = request.headers.get("X-Content-SHA256")
        if client_sha and (len(client_sha) != 64 or any(c not in "0123456789abcdefABCDEF" for c in client_sha)):
            return jsonify(error="validation", message="Invalid SHA-256"), 400

        fd, temp_path = tempfile.mkstemp(prefix="upload-", dir=temp_dir)
        size = 0
        digest = hashlib.sha256()
        try:
            with os.fdopen(fd, "wb") as output:
                while chunk := incoming.stream.read(1024 * 1024):
                    size += len(chunk)
                    if size > app.config["MAX_UPLOAD_BYTES"]:
                        return jsonify(error="size_limit", message="File exceeds size limit"), 413
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if client_sha and not hmac.compare_digest(client_sha.lower(), digest.hexdigest()):
                return jsonify(error="validation", message="SHA-256 mismatch"), 422
            share_id = secrets.token_urlsafe(32)
            final_path = files_dir / share_id
            os.replace(temp_path, final_path)
            try:
                with db() as conn:
                    conn.execute("INSERT INTO shares (id, filename, size, sha256, created_at) VALUES (?, ?, ?, ?, ?)",
                                 (share_id, filename, size, digest.hexdigest(), datetime.now(timezone.utc).isoformat()))
            except Exception:
                final_path.unlink(missing_ok=True)
                raise
            return jsonify(id=share_id, url=share_url(share_id), filename=filename, size=size, sha256=digest.hexdigest()), 201
        finally:
            Path(temp_path).unlink(missing_ok=True)

    @app.get("/s/<share_id>")
    def download(share_id):
        with db() as conn:
            row = conn.execute("SELECT filename FROM shares WHERE id=? AND active=1", (share_id,)).fetchone()
        path = files_dir / share_id
        if row is None or not path.is_file():
            abort(404)
        return send_file(path, mimetype="application/octet-stream", as_attachment=True,
                         download_name=row["filename"], conditional=False)

    @app.delete("/api/shares/<share_id>")
    def api_delete(share_id):
        if not (is_api_user() or csrf_ok()):
            return jsonify(error="unauthorized"), 401
        if not disable_share(share_id):
            return jsonify(error="not_found"), 404
        return "", 204

    @app.post("/shares/<share_id>/delete")
    def browser_delete(share_id):
        if not csrf_ok():
            abort(403)
        disable_share(share_id)
        return redirect(url_for("home"))

    def disable_share(share_id):
        with db() as conn:
            changed = conn.execute("UPDATE shares SET active=0 WHERE id=? AND active=1", (share_id,)).rowcount
        if changed:
            (files_dir / share_id).unlink(missing_ok=True)
        return bool(changed)

    return app


LOGIN_HTML = """<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>
<title>Fileshare sign in</title><style>body{font:16px system-ui;background:#f5f7fb;color:#17243b;margin:0;display:grid;place-items:center;min-height:100vh}main{background:white;padding:2rem;border-radius:14px;box-shadow:0 8px 30px #1232;max-width:360px;width:80%}input,button{box-sizing:border-box;width:100%;padding:.8rem;font:inherit;margin:.5rem 0;border-radius:8px}button{background:#2156bd;color:white;border:0;cursor:pointer}.error{color:#a20}</style>
<main><h1>Fileshare</h1><p>Owner sign in</p>{% if error %}<p class=error>{{ error }}</p>{% endif %}<form method=post action=/login><label>Password<input name=password type=password autocomplete=current-password required></label><button>Sign in</button></form></main></html>"""

HOME_HTML = """<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>
<title>Fileshare</title><style>body{font:16px system-ui;background:#f5f7fb;color:#17243b;margin:0}main{max-width:850px;margin:3rem auto;padding:0 1rem}header{display:flex;justify-content:space-between;align-items:center}section{background:white;padding:1.5rem;margin:1.5rem 0;border-radius:14px;box-shadow:0 5px 20px #1231}button{background:#2156bd;color:white;border:0;border-radius:8px;padding:.7rem 1rem;cursor:pointer}input[type=file]{display:block;margin:1rem 0}input.url{width:100%;box-sizing:border-box;padding:.6rem;margin:.4rem 0}article{border-top:1px solid #ddd;padding:1rem 0}small{color:#52627a}.danger{background:#a22}.error{color:#a22}</style>
<main><header><h1>Fileshare</h1><form method=post action=/logout><input type=hidden name=csrf_token value='{{ csrf }}'><button>Sign out</button></form></header>
<section><h2>Upload a file</h2><p>Maximum {{ max_mb }} MB. Anyone with the link can download the file.</p><form id=upload><input type=file name=file required><button>Upload</button></form><p id=status role=status></p><div id=result hidden><input class=url id=share-url readonly aria-label='Share URL'><button id=copy>Copy link</button></div></section>
<section><h2>Active shares</h2>{% for s in shares %}<article><strong>{{ s.filename }}</strong><br><small>{{ s.size }} bytes · SHA-256 {{ s.sha256 }}</small><input class=url readonly value='{{ s.url }}' aria-label='Share URL for {{ s.filename }}'><form method=post action='/shares/{{ s.id }}/delete'><input type=hidden name=csrf_token value='{{ csrf }}'><button class=danger>Remove share</button></form></article>{% else %}<p>No active shares.</p>{% endfor %}</section></main>
<script>const f=document.querySelector('#upload'),s=document.querySelector('#status'),r=document.querySelector('#result'),u=document.querySelector('#share-url');f.addEventListener('submit',async e=>{e.preventDefault();s.textContent='Uploading…';r.hidden=true;try{const x=await fetch('/api/uploads',{method:'POST',headers:{'X-CSRF-Token':'{{ csrf }}'},body:new FormData(f)}),j=await x.json();if(!x.ok)throw Error(j.message||'Upload failed');u.value=j.url;r.hidden=false;s.textContent='Upload complete: '+j.size+' bytes · SHA-256 '+j.sha256}catch(err){s.textContent=err.message}});document.querySelector('#copy').addEventListener('click',()=>navigator.clipboard.writeText(u.value));</script></html>"""


app = create_app()
