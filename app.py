"""
XAZINGA — Prompt Gallery
- PostgreSQL DB (Supabase Postgres)
- Supabase Storage (이미지/영상)
- Flask + Gunicorn
"""
import os
import uuid
import time
import json
import hashlib
import secrets
import requests
from functools import wraps

import psycopg2
from psycopg2.extras import RealDictCursor, Json
from psycopg2.pool import SimpleConnectionPool
from flask import (
    Flask, request, render_template, redirect, url_for,
    jsonify, abort, session, g
)

# =================================================================
# Configuration
# =================================================================
DATABASE_URL = os.environ.get("DATABASE_URL")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
SUPABASE_BUCKET = os.environ.get("SUPABASE_BUCKET", "media")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL env var is required")
if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_KEY env vars are required")

ALLOWED_IMG = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
ALLOWED_VID = {".mp4", ".webm", ".mov"}
ALLOWED_AUDIO = {".mp3", ".wav", ".m4a", ".ogg", ".flac"}

AI_MODELS = [
    "Midjourney", "DALL-E 3", "Stable Diffusion", "Flux", "Nano Banana",
    "Seedream", "Imagen", "Sora", "Veo", "Seedance", "Kling",
    "Runway", "Pika", "Higgsfield", "기타 (직접 입력)"
]

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200MB total request size (covers refs + main file)
app.secret_key = os.environ.get("SECRET_KEY", "dev-" + secrets.token_hex(16))


# =================================================================
# Connection pool
# =================================================================
_pool = SimpleConnectionPool(1, 10, DATABASE_URL)


class _DB:
    def __init__(self):
        self.conn = None
        self.cur = None

    def __enter__(self):
        self.conn = _pool.getconn()
        self.cur = self.conn.cursor(cursor_factory=RealDictCursor)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if exc_type:
                self.conn.rollback()
            else:
                self.conn.commit()
        finally:
            if self.cur:
                self.cur.close()
            if self.conn:
                _pool.putconn(self.conn)

    def execute(self, sql, params=None):
        self.cur.execute(sql, params or ())
        return self.cur

    def fetchone(self, sql, params=None):
        self.execute(sql, params)
        return self.cur.fetchone()

    def fetchall(self, sql, params=None):
        self.execute(sql, params)
        return self.cur.fetchall()


def db():
    return _DB()


def init_db():
    with db() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id UUID PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                bio TEXT,
                created_at BIGINT NOT NULL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS posts (
                id UUID PRIMARY KEY,
                user_id UUID REFERENCES users(id) ON DELETE SET NULL,
                title TEXT,
                media_path TEXT NOT NULL,
                media_type TEXT NOT NULL,
                prompt TEXT NOT NULL,
                negative_prompt TEXT,
                model TEXT NOT NULL,
                tags TEXT,
                author TEXT DEFAULT 'anonymous',
                likes INTEGER DEFAULT 0,
                views INTEGER DEFAULT 0,
                created_at BIGINT NOT NULL,
                refs JSONB DEFAULT '[]'::jsonb
            )
        """)
        c.execute("ALTER TABLE posts ADD COLUMN IF NOT EXISTS refs JSONB DEFAULT '[]'::jsonb")
        c.execute("CREATE INDEX IF NOT EXISTS idx_posts_created ON posts(created_at DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_posts_model ON posts(model)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_posts_user ON posts(user_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_posts_media_type ON posts(media_type)")


# =================================================================
# Supabase Storage
# =================================================================
def storage_upload(file_storage, save_name: str) -> str:
    upload_url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{save_name}"
    file_storage.stream.seek(0)
    file_bytes = file_storage.stream.read()
    headers = {
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": file_storage.mimetype or "application/octet-stream",
        "x-upsert": "false",
    }
    r = requests.post(upload_url, data=file_bytes, headers=headers, timeout=60)
    if not r.ok:
        raise RuntimeError(f"Storage upload failed: {r.status_code} {r.text}")
    return f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_BUCKET}/{save_name}"


def storage_delete(public_url: str):
    try:
        if not public_url or f"/object/public/{SUPABASE_BUCKET}/" not in public_url:
            return
        path = public_url.split(f"/object/public/{SUPABASE_BUCKET}/", 1)[1]
        del_url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{path}"
        headers = {"Authorization": f"Bearer {SUPABASE_SERVICE_KEY}"}
        requests.delete(del_url, headers=headers, timeout=15)
    except Exception:
        pass


def detect_media_type(ext: str) -> str:
    ext = ext.lower()
    if ext in ALLOWED_IMG: return "image"
    if ext in ALLOWED_VID: return "video"
    if ext in ALLOWED_AUDIO: return "audio"
    return ""


# =================================================================
# Auth helpers
# =================================================================
def hash_password(pw: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.scrypt(pw.encode(), salt=salt.encode(), n=16384, r=8, p=1, dklen=32).hex()
    return f"{salt}${h}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        salt, h = stored.split("$", 1)
        check = hashlib.scrypt(pw.encode(), salt=salt.encode(), n=16384, r=8, p=1, dklen=32).hex()
        return secrets.compare_digest(check, h)
    except Exception:
        return False


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    if hasattr(g, "user"):
        return g.user
    with db() as c:
        row = c.fetchone("SELECT * FROM users WHERE id = %s", (uid,))
    g.user = dict(row) if row else None
    if g.user:
        g.user["id"] = str(g.user["id"])
    return g.user


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kw):
        if not current_user():
            return redirect(url_for("login", next=request.path))
        return fn(*args, **kw)
    return wrapper


@app.context_processor
def inject_user():
    return {"user": current_user()}


def normalize_post(p):
    """Convert DB row to template-friendly dict."""
    p = dict(p)
    p["id"] = str(p["id"])
    if p.get("user_id"):
        p["user_id"] = str(p["user_id"])
    p["tags"] = (p["tags"] or "").split(",") if p["tags"] else []
    refs = p.get("refs") or []
    if isinstance(refs, str):
        try: refs = json.loads(refs)
        except Exception: refs = []
    p["refs"] = refs
    return p


# =================================================================
# Browse routes
# =================================================================
@app.route("/")
def index():
    model = request.args.get("model")
    sort = request.args.get("sort", "recent")
    media_filter = request.args.get("type", "image")
    if media_filter not in ("image", "video", "all"):
        media_filter = "image"

    with db() as c:
        rows = c.fetchall("SELECT media_type, COUNT(*) AS n FROM posts GROUP BY media_type")
        counts = {r["media_type"]: r["n"] for r in rows}
        image_count = counts.get("image", 0)
        video_count = counts.get("video", 0)
        all_count = image_count + video_count

        sql = "SELECT * FROM posts WHERE 1=1"
        params = []
        if media_filter in ("image", "video"):
            sql += " AND media_type = %s"
            params.append(media_filter)
        if model:
            sql += " AND model = %s"
            params.append(model)
        sql += " ORDER BY likes DESC, created_at DESC" if sort == "popular" else " ORDER BY created_at DESC"
        sql += " LIMIT 120"
        posts = [normalize_post(r) for r in c.fetchall(sql, params)]

    return render_template(
        "index.html",
        posts=posts, models=AI_MODELS,
        current_model=model, current_sort=sort, current_type=media_filter,
        image_count=image_count, video_count=video_count, all_count=all_count,
    )


@app.route("/post/<post_id>")
def post_detail(post_id):
    with db() as c:
        row = c.fetchone("SELECT * FROM posts WHERE id = %s", (post_id,))
        if not row:
            abort(404)
        c.execute("UPDATE posts SET views = views + 1 WHERE id = %s", (post_id,))
        post = normalize_post(row)
    return render_template("detail.html", post=post)


@app.route("/post/<post_id>/delete", methods=["POST"])
@login_required
def post_delete(post_id):
    u = current_user()
    with db() as c:
        row = c.fetchone("SELECT * FROM posts WHERE id = %s", (post_id,))
        if not row:
            abort(404)
        if str(row["user_id"]) != str(u["id"]):
            abort(403)
        storage_delete(row["media_path"])
        refs = row.get("refs") or []
        if isinstance(refs, str):
            try: refs = json.loads(refs)
            except Exception: refs = []
        for r in refs:
            storage_delete(r.get("url", ""))
        c.execute("DELETE FROM posts WHERE id = %s", (post_id,))
    return redirect(url_for("user_page", username=u["username"]))


@app.route("/upload", methods=["GET", "POST"])
@login_required
def upload():
    u = current_user()
    if request.method == "POST":
        f = request.files.get("file")
        prompt = (request.form.get("prompt") or "").strip()
        model_choice = request.form.get("model") or "기타 (직접 입력)"
        model_custom = (request.form.get("model_custom") or "").strip()
        title = (request.form.get("title") or "").strip()
        neg = (request.form.get("negative_prompt") or "").strip()
        tags_raw = (request.form.get("tags") or "").strip()

        # Resolve model name
        if model_choice == "기타 (직접 입력)" or model_choice == "기타":
            model = model_custom or "기타"
        else:
            model = model_custom.strip() or model_choice
            # If user typed a custom suffix (e.g. "Seedance 2.0"), prefer their text
            if model_custom:
                model = model_custom

        if not f or not f.filename or not prompt:
            return render_template("upload.html", models=AI_MODELS, error="파일과 프롬프트는 필수입니다"), 400

        ext = os.path.splitext(f.filename)[1].lower()
        media_type = detect_media_type(ext)
        if media_type not in ("image", "video"):
            return render_template("upload.html", models=AI_MODELS, error="지원하지 않는 파일 형식 (이미지/영상만 가능)"), 400

        new_id = str(uuid.uuid4())
        save_name = f"{u['username']}/{new_id}{ext}"
        try:
            public_url = storage_upload(f, save_name)
        except Exception as e:
            return render_template("upload.html", models=AI_MODELS, error=f"업로드 실패: {e}"), 500

        # References (max 10)
        refs = []
        ref_files = request.files.getlist("refs[]")[:10]
        for i, rf in enumerate(ref_files):
            if not rf or not rf.filename:
                continue
            r_ext = os.path.splitext(rf.filename)[1].lower()
            r_type = detect_media_type(r_ext)
            if not r_type:
                continue
            r_name = f"{u['username']}/refs/{new_id}_{i}{r_ext}"
            try:
                r_url = storage_upload(rf, r_name)
                refs.append({"url": r_url, "type": r_type})
            except Exception:
                continue

        tags_clean = ",".join([t.lstrip("#").strip().lower() for t in tags_raw.replace(",", " ").split() if t.strip()])

        with db() as c:
            c.execute(
                """INSERT INTO posts (id, user_id, title, media_path, media_type, prompt, negative_prompt, model, tags, author, created_at, refs)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (new_id, u["id"], title or None, public_url, media_type, prompt,
                 neg or None, model, tags_clean, u["username"], int(time.time()), Json(refs)),
            )
        return redirect(url_for("post_detail", post_id=new_id))
    return render_template("upload.html", models=AI_MODELS)


@app.route("/search")
def search():
    q = (request.args.get("q") or "").strip()
    media_filter = request.args.get("type", "all")
    if media_filter not in ("image", "video", "all"):
        media_filter = "all"
    posts = []
    if q:
        with db() as c:
            sql = "SELECT * FROM posts WHERE (prompt ILIKE %s OR title ILIKE %s OR tags ILIKE %s OR author ILIKE %s OR model ILIKE %s)"
            like = f"%{q}%"
            params = [like, like, like, like, like]
            if media_filter in ("image", "video"):
                sql += " AND media_type = %s"
                params.append(media_filter)
            sql += " ORDER BY created_at DESC LIMIT 120"
            posts = [normalize_post(r) for r in c.fetchall(sql, params)]
    return render_template("search.html", posts=posts, q=q, current_type=media_filter)


@app.route("/like/<post_id>", methods=["POST"])
def like(post_id):
    with db() as c:
        c.execute("UPDATE posts SET likes = likes + 1 WHERE id = %s", (post_id,))
        row = c.fetchone("SELECT likes FROM posts WHERE id = %s", (post_id,))
    return jsonify({"likes": row["likes"] if row else 0})


@app.route("/u/<username>")
def user_page(username):
    with db() as c:
        u = c.fetchone("SELECT * FROM users WHERE username = %s", (username,))
        if not u:
            abort(404)
        rows = c.fetchall("SELECT * FROM posts WHERE user_id = %s ORDER BY created_at DESC", (u["id"],))
        posts = [normalize_post(r) for r in rows]
        total_likes = sum(p["likes"] for p in posts)
        total_views = sum(p["views"] for p in posts)
        profile = dict(u)
        profile["id"] = str(profile["id"])
    return render_template("profile.html", profile=profile, posts=posts,
                           total_likes=total_likes, total_views=total_views)


# =================================================================
# Character Sheet generator (standalone embedded page)
# =================================================================
@app.route("/character-sheet")
def character_sheet():
    return render_template("character_sheet.html")


# =================================================================
# Auth routes
# =================================================================
@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip().lower()
        password = request.form.get("password") or ""
        if not username or len(username) < 2:
            return render_template("signup.html", error="사용자명은 2자 이상이어야 합니다"), 400
        if not username.replace("_", "").replace("-", "").isalnum():
            return render_template("signup.html", error="사용자명은 영문/숫자/_/- 만 가능합니다"), 400
        if len(password) < 6:
            return render_template("signup.html", error="비밀번호는 6자 이상이어야 합니다"), 400
        with db() as c:
            existing = c.fetchone("SELECT id FROM users WHERE username = %s", (username,))
            if existing:
                return render_template("signup.html", error="이미 사용 중인 사용자명입니다"), 400
            uid = str(uuid.uuid4())
            c.execute(
                "INSERT INTO users (id, username, password_hash, created_at) VALUES (%s, %s, %s, %s)",
                (uid, username, hash_password(password), int(time.time())),
            )
        session["user_id"] = uid
        return redirect(request.args.get("next") or url_for("index"))
    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip().lower()
        password = request.form.get("password") or ""
        with db() as c:
            row = c.fetchone("SELECT * FROM users WHERE username = %s", (username,))
        if not row or not verify_password(password, row["password_hash"]):
            return render_template("login.html", error="사용자명 또는 비밀번호가 잘못되었습니다"), 400
        session["user_id"] = str(row["id"])
        return redirect(request.args.get("next") or url_for("index"))
    return render_template("login.html")


@app.route("/logout", methods=["POST", "GET"])
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/health")
def health():
    return {"ok": True}


# =================================================================
# Bootstrap
# =================================================================
init_db()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)

