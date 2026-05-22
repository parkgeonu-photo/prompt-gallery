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

PARTIAL_LIMIT = 200            # chars visible before blur
PRIVATE_POST_LIMIT = 50        # max private posts per user
COMMENT_MAX_LEN = 1000
MESSAGE_MAX_LEN = 2000

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024
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
                refs JSONB DEFAULT '[]'::jsonb,
                visibility TEXT DEFAULT 'public',
                source_url TEXT
            )
        """)
        c.execute("ALTER TABLE posts ADD COLUMN IF NOT EXISTS refs JSONB DEFAULT '[]'::jsonb")
        c.execute("ALTER TABLE posts ADD COLUMN IF NOT EXISTS visibility TEXT DEFAULT 'public'")
        c.execute("ALTER TABLE posts ADD COLUMN IF NOT EXISTS source_url TEXT")
        c.execute("CREATE INDEX IF NOT EXISTS idx_posts_created ON posts(created_at DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_posts_model ON posts(model)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_posts_user ON posts(user_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_posts_media_type ON posts(media_type)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_posts_visibility ON posts(visibility)")
        c.execute("""
            CREATE TABLE IF NOT EXISTS comments (
                id UUID PRIMARY KEY,
                post_id UUID NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
                user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                author TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at BIGINT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_comments_post ON comments(post_id, created_at DESC)")
        c.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id UUID PRIMARY KEY,
                sender_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                recipient_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                body TEXT NOT NULL,
                read_at BIGINT,
                created_at BIGINT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_msg_recipient ON messages(recipient_id, created_at DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_msg_sender ON messages(sender_id, created_at DESC)")


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
    u = current_user()
    unread = 0
    if u:
        with db() as c:
            row = c.fetchone(
                "SELECT COUNT(*) AS n FROM messages WHERE recipient_id = %s AND read_at IS NULL",
                (u["id"],),
            )
            unread = row["n"] if row else 0
    return {"user": u, "unread_count": unread}


@app.template_filter("datetimeformat")
def _datetimeformat(ts):
    """Unix ts -> '2026-05-21 14:30' (KST)."""
    if not ts:
        return ""
    try:
        from datetime import datetime, timezone, timedelta
        kst = timezone(timedelta(hours=9))
        dt = datetime.fromtimestamp(int(ts), tz=kst)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


@app.template_filter("relativetime")
def _relativetime(ts):
    """Unix ts -> '3분 전' / '2시간 전' / '4일 전' style."""
    if not ts:
        return ""
    try:
        diff = int(time.time()) - int(ts)
        if diff < 60: return "방금 전"
        if diff < 3600: return f"{diff // 60}분 전"
        if diff < 86400: return f"{diff // 3600}시간 전"
        if diff < 86400 * 7: return f"{diff // 86400}일 전"
        from datetime import datetime, timezone, timedelta
        kst = timezone(timedelta(hours=9))
        dt = datetime.fromtimestamp(int(ts), tz=kst)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return ""


def normalize_post(p, viewer_id=None):
    """DB row -> template-friendly dict with visibility-aware masking."""
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
    p["visibility"] = p.get("visibility") or "public"

    is_owner = viewer_id and p.get("user_id") and str(viewer_id) == str(p["user_id"])
    p["is_owner"] = is_owner

    if p["visibility"] == "partial" and not is_owner:
        full = p["prompt"] or ""
        if len(full) > PARTIAL_LIMIT:
            p["prompt_visible"] = full[:PARTIAL_LIMIT]
            p["prompt_hidden"] = full[PARTIAL_LIMIT:]
            p["prompt_truncated"] = True
        else:
            p["prompt_visible"] = full
            p["prompt_hidden"] = ""
            p["prompt_truncated"] = False
        p["negative_prompt_hidden"] = bool(p.get("negative_prompt"))
        if p["negative_prompt_hidden"]:
            p["negative_prompt"] = None
    else:
        p["prompt_visible"] = p["prompt"]
        p["prompt_hidden"] = ""
        p["prompt_truncated"] = False
        p["negative_prompt_hidden"] = False

    return p


# =================================================================
# Browse routes
# =================================================================
@app.route("/")
def index():
    model = request.args.get("model")
    sort = request.args.get("sort", "recent")
    media_filter = request.args.get("type", "all")
    if media_filter not in ("image", "video", "all"):
        media_filter = "all"

    u = current_user()
    viewer_id = u["id"] if u else None

    with db() as c:
        if viewer_id:
            cnt_rows = c.fetchall(
                "SELECT media_type, COUNT(*) AS n FROM posts WHERE visibility != 'private' OR user_id = %s GROUP BY media_type",
                (viewer_id,),
            )
        else:
            cnt_rows = c.fetchall(
                "SELECT media_type, COUNT(*) AS n FROM posts WHERE visibility != 'private' GROUP BY media_type"
            )
        counts = {r["media_type"]: r["n"] for r in cnt_rows}
        image_count = counts.get("image", 0)
        video_count = counts.get("video", 0)
        all_count = image_count + video_count

        sql = "SELECT * FROM posts WHERE 1=1"
        params = []
        if viewer_id:
            sql += " AND (visibility != 'private' OR user_id = %s)"
            params.append(viewer_id)
        else:
            sql += " AND visibility != 'private'"
        if media_filter in ("image", "video"):
            sql += " AND media_type = %s"
            params.append(media_filter)
        if model:
            sql += " AND model = %s"
            params.append(model)
        sql += " ORDER BY likes DESC, created_at DESC" if sort == "popular" else " ORDER BY created_at DESC"
        sql += " LIMIT 120"
        posts = [normalize_post(r, viewer_id) for r in c.fetchall(sql, params)]

    return render_template(
        "index.html",
        posts=posts, models=AI_MODELS,
        current_model=model, current_sort=sort, current_type=media_filter,
        image_count=image_count, video_count=video_count, all_count=all_count,
    )


@app.route("/post/<post_id>")
def post_detail(post_id):
    u = current_user()
    viewer_id = u["id"] if u else None
    with db() as c:
        row = c.fetchone("SELECT * FROM posts WHERE id = %s", (post_id,))
        if not row:
            abort(404)
        if (row.get("visibility") or "public") == "private":
            if not viewer_id or str(viewer_id) != str(row["user_id"]):
                abort(404)
        c.execute("UPDATE posts SET views = views + 1 WHERE id = %s", (post_id,))
        post = normalize_post(row, viewer_id)
        comments = c.fetchall(
            "SELECT * FROM comments WHERE post_id = %s ORDER BY created_at ASC",
            (post_id,),
        )
        comments = [dict(c_) for c_ in comments]
        for c_ in comments:
            c_["id"] = str(c_["id"])
            c_["user_id"] = str(c_["user_id"])
            c_["is_mine"] = (viewer_id and str(viewer_id) == c_["user_id"])
    return render_template("detail.html", post=post, comments=comments)


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
        visibility = (request.form.get("visibility") or "public").strip()
        source_url = (request.form.get("source_url") or "").strip()
        if visibility not in ("public", "partial", "private"):
            visibility = "public"

        # Resolve model
        if model_choice.startswith("기타"):
            model = model_custom or "기타"
        else:
            model = model_custom.strip() or model_choice
            if model_custom:
                model = model_custom

        # Source URL light validation
        if source_url:
            if not (source_url.startswith("http://") or source_url.startswith("https://")):
                source_url = "https://" + source_url
            if len(source_url) > 500:
                source_url = source_url[:500]

        if not f or not f.filename or not prompt:
            return render_template("upload.html", models=AI_MODELS, error="파일과 프롬프트는 필수입니다"), 400

        # Private post limit check
        if visibility == "private":
            with db() as c:
                cnt = c.fetchone(
                    "SELECT COUNT(*) AS n FROM posts WHERE user_id = %s AND visibility = 'private'",
                    (u["id"],),
                )
                if cnt and cnt["n"] >= PRIVATE_POST_LIMIT:
                    return render_template(
                        "upload.html", models=AI_MODELS,
                        error=f"비공개 게시물은 최대 {PRIVATE_POST_LIMIT}개까지만 가능합니다. (현재 {cnt['n']}개) 기존 비공개 게시물을 정리하거나 공개/일부공개로 올리세요."
                    ), 400

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
                """INSERT INTO posts (id, user_id, title, media_path, media_type, prompt, negative_prompt, model, tags, author, created_at, refs, visibility, source_url)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (new_id, u["id"], title or None, public_url, media_type, prompt,
                 neg or None, model, tags_clean, u["username"], int(time.time()),
                 Json(refs), visibility, source_url or None),
            )
        return redirect(url_for("post_detail", post_id=new_id))

    # GET — also expose remaining private slots
    private_used = 0
    with db() as c:
        row = c.fetchone(
            "SELECT COUNT(*) AS n FROM posts WHERE user_id = %s AND visibility = 'private'",
            (u["id"],),
        )
        private_used = row["n"] if row else 0
    return render_template(
        "upload.html",
        models=AI_MODELS,
        private_used=private_used,
        private_limit=PRIVATE_POST_LIMIT,
    )


@app.route("/search")
def search():
    q = (request.args.get("q") or "").strip()
    media_filter = request.args.get("type", "all")
    if media_filter not in ("image", "video", "all"):
        media_filter = "all"
    u = current_user()
    viewer_id = u["id"] if u else None
    posts = []
    if q:
        with db() as c:
            sql = "SELECT * FROM posts WHERE (prompt ILIKE %s OR title ILIKE %s OR tags ILIKE %s OR author ILIKE %s OR model ILIKE %s)"
            like = f"%{q}%"
            params = [like, like, like, like, like]
            if viewer_id:
                sql += " AND (visibility != 'private' OR user_id = %s)"
                params.append(viewer_id)
            else:
                sql += " AND visibility != 'private'"
            if media_filter in ("image", "video"):
                sql += " AND media_type = %s"
                params.append(media_filter)
            sql += " ORDER BY created_at DESC LIMIT 120"
            posts = [normalize_post(r, viewer_id) for r in c.fetchall(sql, params)]
    return render_template("search.html", posts=posts, q=q, current_type=media_filter)


@app.route("/like/<post_id>", methods=["POST"])
def like(post_id):
    u = current_user()
    viewer_id = u["id"] if u else None
    with db() as c:
        row = c.fetchone("SELECT user_id, visibility FROM posts WHERE id = %s", (post_id,))
        if not row:
            return jsonify({"likes": 0}), 404
        if (row.get("visibility") or "public") == "private":
            if not viewer_id or str(viewer_id) != str(row["user_id"]):
                return jsonify({"likes": 0}), 403
        c.execute("UPDATE posts SET likes = likes + 1 WHERE id = %s", (post_id,))
        cnt = c.fetchone("SELECT likes FROM posts WHERE id = %s", (post_id,))
    return jsonify({"likes": cnt["likes"] if cnt else 0})


# =================================================================
# Comments
# =================================================================
@app.route("/post/<post_id>/comment", methods=["POST"])
@login_required
def add_comment(post_id):
    u = current_user()
    body = (request.form.get("body") or "").strip()
    if not body:
        return redirect(url_for("post_detail", post_id=post_id))
    if len(body) > COMMENT_MAX_LEN:
        body = body[:COMMENT_MAX_LEN]

    with db() as c:
        post = c.fetchone("SELECT user_id, visibility FROM posts WHERE id = %s", (post_id,))
        if not post:
            abort(404)
        # Disallow commenting on private posts unless owner
        if (post.get("visibility") or "public") == "private":
            if str(post["user_id"]) != str(u["id"]):
                abort(403)
        cid = str(uuid.uuid4())
        c.execute(
            "INSERT INTO comments (id, post_id, user_id, author, body, created_at) VALUES (%s,%s,%s,%s,%s,%s)",
            (cid, post_id, u["id"], u["username"], body, int(time.time())),
        )
    return redirect(url_for("post_detail", post_id=post_id) + f"#c-{cid}")


@app.route("/comment/<comment_id>/delete", methods=["POST"])
@login_required
def delete_comment(comment_id):
    u = current_user()
    with db() as c:
        row = c.fetchone("SELECT post_id, user_id FROM comments WHERE id = %s", (comment_id,))
        if not row:
            abort(404)
        # Allow comment author OR post owner to delete
        post = c.fetchone("SELECT user_id FROM posts WHERE id = %s", (row["post_id"],))
        is_comment_owner = str(row["user_id"]) == str(u["id"])
        is_post_owner = post and str(post["user_id"]) == str(u["id"])
        if not (is_comment_owner or is_post_owner):
            abort(403)
        c.execute("DELETE FROM comments WHERE id = %s", (comment_id,))
        post_id = str(row["post_id"])
    return redirect(url_for("post_detail", post_id=post_id))


# =================================================================
# Direct Messages
# =================================================================
@app.route("/messages")
@login_required
def messages_inbox():
    u = current_user()
    with db() as c:
        # Latest message per thread (other user)
        rows = c.fetchall("""
            SELECT DISTINCT ON (other_id)
                other_id, other_username, body, created_at, read_at, sender_id, recipient_id, mid
            FROM (
                SELECT m.id AS mid, m.body, m.created_at, m.read_at, m.sender_id, m.recipient_id,
                       CASE WHEN m.sender_id = %s THEN m.recipient_id ELSE m.sender_id END AS other_id,
                       CASE WHEN m.sender_id = %s THEN ru.username ELSE su.username END AS other_username
                FROM messages m
                JOIN users su ON su.id = m.sender_id
                JOIN users ru ON ru.id = m.recipient_id
                WHERE m.sender_id = %s OR m.recipient_id = %s
            ) t
            ORDER BY other_id, created_at DESC
        """, (u["id"], u["id"], u["id"], u["id"]))
        # Sort by latest activity overall
        threads = sorted(
            [dict(r) for r in rows],
            key=lambda r: r["created_at"],
            reverse=True,
        )
        for t in threads:
            t["other_id"] = str(t["other_id"])
            t["sender_id"] = str(t["sender_id"])
            t["recipient_id"] = str(t["recipient_id"])
            t["mine"] = t["sender_id"] == u["id"]
            t["unread"] = (not t["mine"]) and (t["read_at"] is None)
    return render_template("messages_inbox.html", threads=threads)


@app.route("/messages/<username>", methods=["GET", "POST"])
@login_required
def messages_thread(username):
    u = current_user()
    with db() as c:
        other = c.fetchone("SELECT * FROM users WHERE username = %s", (username,))
        if not other:
            abort(404)
        other_id = str(other["id"])
        if other_id == u["id"]:
            return redirect(url_for("messages_inbox"))

        if request.method == "POST":
            body = (request.form.get("body") or "").strip()
            if body:
                if len(body) > MESSAGE_MAX_LEN:
                    body = body[:MESSAGE_MAX_LEN]
                c.execute(
                    "INSERT INTO messages (id, sender_id, recipient_id, body, created_at) VALUES (%s,%s,%s,%s,%s)",
                    (str(uuid.uuid4()), u["id"], other_id, body, int(time.time())),
                )
            return redirect(url_for("messages_thread", username=username))

        # Mark received messages as read
        c.execute(
            "UPDATE messages SET read_at = %s WHERE recipient_id = %s AND sender_id = %s AND read_at IS NULL",
            (int(time.time()), u["id"], other_id),
        )

        rows = c.fetchall("""
            SELECT * FROM messages
            WHERE (sender_id = %s AND recipient_id = %s)
               OR (sender_id = %s AND recipient_id = %s)
            ORDER BY created_at ASC
            LIMIT 500
        """, (u["id"], other_id, other_id, u["id"]))
        msgs = []
        for r in rows:
            d = dict(r)
            d["id"] = str(d["id"])
            d["sender_id"] = str(d["sender_id"])
            d["recipient_id"] = str(d["recipient_id"])
            d["mine"] = d["sender_id"] == u["id"]
            msgs.append(d)
        other_profile = dict(other)
        other_profile["id"] = str(other_profile["id"])
    return render_template("messages_thread.html", other=other_profile, messages=msgs)


# =================================================================
# Profile / Auth
# =================================================================
@app.route("/u/<username>")
def user_page(username):
    u = current_user()
    viewer_id = u["id"] if u else None
    with db() as c:
        target = c.fetchone("SELECT * FROM users WHERE username = %s", (username,))
        if not target:
            abort(404)
        is_self = viewer_id and str(viewer_id) == str(target["id"])
        if is_self:
            rows = c.fetchall(
                "SELECT * FROM posts WHERE user_id = %s ORDER BY created_at DESC",
                (target["id"],),
            )
        else:
            rows = c.fetchall(
                "SELECT * FROM posts WHERE user_id = %s AND visibility != 'private' ORDER BY created_at DESC",
                (target["id"],),
            )
        posts = [normalize_post(r, viewer_id) for r in rows]
        total_likes = sum(p["likes"] for p in posts)
        total_views = sum(p["views"] for p in posts)
        profile = dict(target)
        profile["id"] = str(profile["id"])
    return render_template("profile.html", profile=profile, posts=posts,
                           total_likes=total_likes, total_views=total_views,
                           is_self=is_self)


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


@app.route("/character-sheet")
def character_sheet():
    return render_template("character_sheet.html")


@app.route("/health")
def health():
    return {"ok": True}


# =================================================================
# Bootstrap
# =================================================================
init_db()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)


