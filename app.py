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
    jsonify, abort, session, g, make_response
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
    "Seedance", "Grok", "Kling",
    "Midjourney", "DALL-E 3", "Stable Diffusion", "Flux", "Nano Banana",
    "Seedream", "Imagen", "Sora", "Veo",
    "Runway", "Pika", "Higgsfield", "기타 (직접 입력)"
]

PARTIAL_LIMIT = 200            # chars visible before blur
PRIVATE_POST_LIMIT = 50        # max private posts per user
COMMENT_MAX_LEN = 1000
MESSAGE_MAX_LEN = 2000
APP_CONTENT_MAX = 20000
PROCESS_TEXT_MAX = 5000
PROCESS_IMAGES_MAX = 8
AVATAR_MAX_BYTES = 5 * 1024 * 1024

# Characters
MAX_CHARACTERS_PER_USER = 30
MAX_IMAGES_PER_CHARACTER = 30
CHARACTER_IMG_MAX_BYTES = 10 * 1024 * 1024  # 10MB per image (캐릭터시트 합성 이미지 고려)
CHARACTER_CATEGORIES = ["남자", "여자", "동물", "기타"]

APP_CATEGORIES = [
    "생산성", "디자인", "AI 도구", "개발", "영상/사진",
    "음악", "게임", "교육", "라이프스타일", "유틸리티", "기타"
]

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


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kw):
        u = current_user()
        if not u:
            return redirect(url_for("login", next=request.path))
        if not u.get("is_admin"):
            abort(403)
        return fn(*args, **kw)
    return wrapper


def get_blocked_ids(viewer_id):
    """양방향 차단 — 내가 차단한 사람 + 나를 차단한 사람."""
    if not viewer_id:
        return set()
    with db() as c:
        rows = c.fetchall(
            "SELECT blocked_id AS uid FROM blocks WHERE blocker_id = %s "
            "UNION SELECT blocker_id AS uid FROM blocks WHERE blocked_id = %s",
            (viewer_id, viewer_id)
        )
        return {r["uid"] for r in rows}


def get_muted_ids(viewer_id):
    """일방향 숨김 — 내가 숨긴 사용자만 (피드에서만 안 보임, 메시지/댓글은 그대로)."""
    if not viewer_id:
        return set()
    with db() as c:
        rows = c.fetchall(
            "SELECT muted_id AS uid FROM user_mutes WHERE muter_id = %s",
            (viewer_id,)
        )
        return {r["uid"] for r in rows}


def get_hidden_ids(viewer_id):
    """피드 + 댓글에서 숨길 모든 사용자 = 차단 + 숨김 합집합."""
    if not viewer_id:
        return set()
    return get_blocked_ids(viewer_id) | get_muted_ids(viewer_id)


@app.context_processor
def inject_user():
    u = current_user()
    unread = 0
    pending_apps = 0
    blocked_ids = set()
    muted_ids = set()
    settings = {}
    if u:
        with db() as c:
            row = c.fetchone(
                "SELECT COUNT(*) AS n FROM messages WHERE recipient_id = %s AND read_at IS NULL",
                (u["id"],),
            )
            unread = row["n"] if row else 0
            if u.get("is_admin"):
                row = c.fetchone(
                    "SELECT COUNT(*) AS n FROM app_posts WHERE status = 'pending'"
                )
                pending_apps = row["n"] if row else 0
            rows = c.fetchall("SELECT blocked_id FROM blocks WHERE blocker_id = %s", (u["id"],))
            blocked_ids = {r["blocked_id"] for r in rows}
            rows = c.fetchall("SELECT muted_id FROM user_mutes WHERE muter_id = %s", (u["id"],))
            muted_ids = {r["muted_id"] for r in rows}
    try:
        with db() as c:
            rows = c.fetchall("SELECT key, value FROM site_settings", ())
            settings = {r["key"]: r["value"] for r in rows}
    except Exception:
        pass
    return {
        "user": u,
        "unread_count": unread,
        "pending_apps": pending_apps,
        "blocked_ids": blocked_ids,
        "muted_ids": muted_ids,
        "app_categories": APP_CATEGORIES,
        "site": settings,
    }


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
        if diff < 86400: return f"{diff // 3600}���간 전"
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

    pimgs = p.get("process_images") or []
    if isinstance(pimgs, str):
        try: pimgs = json.loads(pimgs)
        except Exception: pimgs = []
    p["process_images"] = pimgs

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
def start():
    """첫 방문에만 시작 페이지를 보여줌. 이후엔 자동으로 갤러리로."""
    # 이미 시작 페이지를 본 적 있거나, 명시적으로 /?force=1 이 아니면 갤러리로
    if request.cookies.get("seen_start") and not request.args.get("force"):
        return redirect("/explore")

    with db() as c:
        rows = c.fetchall("SELECT key, value FROM site_settings", ())
        s = {r["key"]: r["value"] for r in rows}
    cat_url = s.get("start_cat_url") or s.get("logo_url") or \
              "https://d2ol7oe51mr4n9.cloudfront.net/user_34ctFqCBIuenEEsMCqyrHXc4WX1/c598945a-597e-4392-bb17-a70a218961ae.jpg"

    resp = make_response(render_template("start.html", start_cat_url=cat_url))
    # 1년간 시작 페이지 다시 안 봐도 됨
    resp.set_cookie("seen_start", "1", max_age=60*60*24*365, samesite="Lax")
    return resp


@app.route("/explore")
def index():
    model = request.args.get("model")
    sort = request.args.get("sort", "recent")
    media_filter = request.args.get("type", "all")
    if media_filter not in ("image", "video", "all"):
        media_filter = "all"

    u = current_user()
    viewer_id = u["id"] if u else None
    blocked = get_hidden_ids(viewer_id)

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
            # 정확히 일치 OR 모델명으로 시작 (예: 'Midjourney' -> 'Midjourney v7'도 잡음)
            sql += " AND (model = %s OR model ILIKE %s)"
            params.append(model)
            params.append(model + ' %')
        if blocked:
            sql += " AND user_id != ALL(%s)"
            params.append(list(blocked))
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
    blocked = get_hidden_ids(viewer_id)
    with db() as c:
        row = c.fetchone("SELECT * FROM posts WHERE id = %s", (post_id,))
        if not row:
            abort(404)
        if (row.get("visibility") or "public") == "private":
            if not viewer_id or str(viewer_id) != str(row["user_id"]):
                abort(404)
        if blocked and row["user_id"] in blocked:
            abort(404)
        c.execute("UPDATE posts SET views = views + 1 WHERE id = %s", (post_id,))
        post = normalize_post(row, viewer_id)
        if blocked:
            comments = c.fetchall(
                "SELECT * FROM comments WHERE post_id = %s AND user_id != ALL(%s) ORDER BY created_at ASC",
                (post_id, list(blocked)),
            )
        else:
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
        # 본인 또는 어드민만 삭제 가능
        is_owner = str(row["user_id"]) == str(u["id"])
        is_admin = u.get("is_admin", False)
        if not (is_owner or is_admin):
            abort(403)
        try:
            storage_delete(row["media_path"])
        except Exception:
            pass
        refs = row.get("refs") or []
        if isinstance(refs, str):
            try: refs = json.loads(refs)
            except Exception: refs = []
        for r in refs:
            try:
                storage_delete(r.get("url", ""))
            except Exception:
                pass
        c.execute("DELETE FROM posts WHERE id = %s", (post_id,))
    # 어드민이 남의 글 삭제하면 갤러리로, 본인 글 삭제하면 마이페이지로
    if is_owner:
        return redirect(url_for("user_page", username=u["username"]))
    return redirect("/explore")


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
        process_text = (request.form.get("process_text") or "").strip()[:PROCESS_TEXT_MAX]
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

        # 작업프로세스 이미지 업로드 (최대 PROCESS_IMAGES_MAX)
        process_images = []
        for proc_f in request.files.getlist("process_images"):
            if not proc_f or not proc_f.filename:
                continue
            if len(process_images) >= PROCESS_IMAGES_MAX:
                break
            try:
                p_ext = os.path.splitext(proc_f.filename)[1].lower()
                if p_ext not in ALLOWED_IMG:
                    continue
                p_name = f"process/{new_id}-{len(process_images)}{p_ext}"
                p_url = storage_upload(proc_f, p_name)
                process_images.append({"url": p_url})
            except Exception:
                continue

        tags_clean = ",".join([t.lstrip("#").strip().lower() for t in tags_raw.replace(",", " ").split() if t.strip()])

        with db() as c:
            c.execute(
                """INSERT INTO posts (id, user_id, title, media_path, media_type, prompt, negative_prompt, model, tags, author, created_at, refs, visibility, source_url, process_text, process_images)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (new_id, u["id"], title or None, public_url, media_type, prompt,
                 neg or None, model, tags_clean, u["username"], int(time.time()),
                 Json(refs), visibility, source_url or None,
                 process_text or None, Json(process_images)),
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
        blocked = get_blocked_ids(u["id"])
        if blocked:
            blocked_str = {str(b) for b in blocked}
            threads = [t for t in threads if t["other_id"] not in blocked_str]
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

        blocked = get_blocked_ids(u["id"])
        if other["id"] in blocked:
            return render_template("messages_thread.html",
                                   other=dict(other), messages=[], blocked=True)

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
        # social_links가 JSON 문자열이면 파싱
        sl = profile.get("social_links") or {}
        if isinstance(sl, str):
            try: sl = json.loads(sl)
            except Exception: sl = {}
        profile["social_links"] = sl

        # 본인 페이지일 때만 최근 메시지 8개
        my_threads = []
        if is_self:
            blocked = get_blocked_ids(u["id"])
            rows = c.fetchall("""
                SELECT DISTINCT ON (other_id)
                    other_id, other_username, other_avatar, body, created_at, read_at, sender_id, recipient_id
                FROM (
                    SELECT m.body, m.created_at, m.read_at, m.sender_id, m.recipient_id,
                           CASE WHEN m.sender_id = %s THEN m.recipient_id ELSE m.sender_id END AS other_id,
                           CASE WHEN m.sender_id = %s THEN ru.username ELSE su.username END AS other_username,
                           CASE WHEN m.sender_id = %s THEN ru.avatar_url ELSE su.avatar_url END AS other_avatar
                    FROM messages m
                    JOIN users su ON su.id = m.sender_id
                    JOIN users ru ON ru.id = m.recipient_id
                    WHERE m.sender_id = %s OR m.recipient_id = %s
                ) t
                ORDER BY other_id, created_at DESC
            """, (u["id"], u["id"], u["id"], u["id"], u["id"]))
            tlist = []
            for r in rows:
                d = dict(r)
                d["other_id"] = str(d["other_id"])
                d["sender_id"] = str(d["sender_id"])
                d["mine"] = d["sender_id"] == u["id"]
                d["unread"] = (not d["mine"]) and (d["read_at"] is None)
                tlist.append(d)
            tlist.sort(key=lambda r: r["created_at"], reverse=True)
            blocked_str = {str(b) for b in blocked} if blocked else set()
            my_threads = [t for t in tlist if t["other_id"] not in blocked_str][:8]

    # 본인 마이페이지에서 캐릭터 미리보기 (최대 6개)
    my_chars_preview = []
    my_chars_total = 0
    if is_self:
        with db() as c:
            rows = c.fetchall(
                "SELECT id, name, category, images FROM characters WHERE user_id = %s ORDER BY created_at DESC LIMIT 6",
                (u["id"],)
            )
            for r in rows:
                d = dict(r)
                d["id"] = str(d["id"])
                d["images"] = _parse_char_images(d.get("images"))
                my_chars_preview.append(d)
            cnt = c.fetchone("SELECT COUNT(*) AS n FROM characters WHERE user_id = %s", (u["id"],))
            my_chars_total = cnt["n"] if cnt else 0

    return render_template("profile.html", profile=profile, posts=posts,
                           total_likes=total_likes, total_views=total_views,
                           is_self=is_self, my_threads=my_threads,
                           my_chars_preview=my_chars_preview,
                           my_chars_total=my_chars_total)


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip().lower()
        password = request.form.get("password") or ""
        if not username or len(username) < 2:
            return render_template("signup.html", error="사용자명은 2����� 이상이어야 합��다"), 400
        if not username.replace("_", "").replace("-", "").isalnum():
            return render_template("signup.html", error="사용자명은 영문/숫자/_/- 만 가능합니다"), 400
        if len(password) < 6:
            return render_template("signup.html", error="비밀번호는 6자 ���상���어야 합니다"), 400
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


# =================================================================
# Admin
# =================================================================
@app.route("/admin")
@admin_required
def admin_dashboard():
    with db() as c:
        rows = c.fetchall("SELECT key, value FROM site_settings", ())
        settings = {r["key"]: r["value"] for r in rows}
        pending = c.fetchall(
            "SELECT ap.*, u.username FROM app_posts ap "
            "JOIN users u ON u.id = ap.user_id "
            "WHERE ap.status = 'pending' ORDER BY ap.created_at ASC",
            ()
        )
        pending = [dict(p) for p in pending]
        for p in pending:
            p["id"] = str(p["id"])
        stats = c.fetchone("""
            SELECT
              (SELECT COUNT(*) FROM users) AS users_n,
              (SELECT COUNT(*) FROM posts) AS posts_n,
              (SELECT COUNT(*) FROM app_posts WHERE status = 'approved') AS apps_n,
              (SELECT COUNT(*) FROM app_posts WHERE status = 'pending') AS pending_n
        """, ())
    return render_template("admin.html", settings=settings, pending=pending, stats=stats)


@app.route("/admin/settings", methods=["POST"])
@admin_required
def admin_settings_update():
    fields = {
        "site_name": (request.form.get("site_name") or "").strip()[:80],
        "site_tagline": (request.form.get("site_tagline") or "").strip()[:500],
        "hero_enabled": "true" if request.form.get("hero_enabled") else "false",
    }
    # 파일 업로드가 있으면 그게 우선 (URL 입력칸은 무시)
    logo_uploaded = False
    logo_file = request.files.get("logo_file")
    if logo_file and logo_file.filename:
        ext = os.path.splitext(logo_file.filename)[1].lower()
        if ext in ALLOWED_IMG:
            name = f"branding/logo-{int(time.time())}{ext}"
            try:
                url = storage_upload(logo_file, name)
                fields["logo_url"] = url
                logo_uploaded = True
            except Exception as e:
                app.logger.error(f"Logo upload failed: {e}")

    hero_uploaded = False
    hero_file = request.files.get("hero_file")
    if hero_file and hero_file.filename:
        ext = os.path.splitext(hero_file.filename)[1].lower()
        if ext in ALLOWED_IMG | ALLOWED_VID:
            name = f"branding/hero-{int(time.time())}{ext}"
            try:
                url = storage_upload(hero_file, name)
                fields["hero_image_url"] = url
                hero_uploaded = True
            except Exception as e:
                app.logger.error(f"Hero upload failed: {e}")

    # 파일 업로드가 없을 때만 URL 입력칸 사용 (그리고 비어있지 않을 때만)
    if not logo_uploaded:
        lu = (request.form.get("logo_url") or "").strip()
        if lu:
            fields["logo_url"] = lu
    if not hero_uploaded:
        hu = (request.form.get("hero_image_url") or "").strip()
        if hu:
            fields["hero_image_url"] = hu

    with db() as c:
        for k, v in fields.items():
            c.execute(
                "INSERT INTO site_settings (key, value, updated_at) VALUES (%s, %s, NOW()) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()",
                (k, v)
            )
    return redirect("/admin?saved=1")


# =================================================================
# Profile editing
# =================================================================
@app.route("/settings", methods=["GET", "POST"])
@login_required
def user_settings():
    """기존 SETTINGS 페이지는 호환 유지. 새 편집은 마이페이지 인라인에서."""
    u = current_user()
    if request.method == "POST":
        bio = (request.form.get("bio") or "").strip()[:500]
        avatar_url = u.get("avatar_url")
        avatar_file = request.files.get("avatar_file")
        if avatar_file and avatar_file.filename:
            ext = os.path.splitext(avatar_file.filename)[1].lower()
            if ext in ALLOWED_IMG:
                name = f"avatars/{u['id']}-{int(time.time())}{ext}"
                avatar_url = storage_upload(avatar_file, name)
        with db() as c:
            c.execute(
                "UPDATE users SET bio = %s, avatar_url = %s WHERE id = %s",
                (bio, avatar_url, u["id"])
            )
        return redirect(f"/u/{u['username']}")
    return render_template("settings.html")


@app.route("/profile/avatar", methods=["POST"])
@login_required
def profile_avatar_update():
    """마이페이지에서 아바타 클릭하면 바로 업로드. AJAX 호환."""
    u = current_user()
    avatar_file = request.files.get("avatar_file")
    if not avatar_file or not avatar_file.filename:
        if request.headers.get("Accept", "").startswith("application/json"):
            return jsonify({"error": "파일이 없습니다"}), 400
        return redirect(f"/u/{u['username']}")
    ext = os.path.splitext(avatar_file.filename)[1].lower()
    if ext not in ALLOWED_IMG:
        if request.headers.get("Accept", "").startswith("application/json"):
            return jsonify({"error": "이미지 파일만 가능합니다"}), 400
        return redirect(f"/u/{u['username']}")
    name = f"avatars/{u['id']}-{int(time.time())}{ext}"
    avatar_url = storage_upload(avatar_file, name)
    with db() as c:
        c.execute("UPDATE users SET avatar_url = %s WHERE id = %s", (avatar_url, u["id"]))
    if request.headers.get("Accept", "").startswith("application/json"):
        return jsonify({"avatar_url": avatar_url})
    return redirect(f"/u/{u['username']}")


@app.route("/profile/edit", methods=["POST"])
@login_required
def profile_edit():
    """BIO + 외부 링크 5종 인라인 저장."""
    u = current_user()
    bio = (request.form.get("bio") or "").strip()[:500]

    def _norm(url):
        url = (url or "").strip()[:300]
        if url and not url.startswith(("http://", "https://")):
            url = "https://" + url
        return url or None

    links = {
        "website":   _norm(request.form.get("link_website")),
        "instagram": _norm(request.form.get("link_instagram")),
        "twitter":   _norm(request.form.get("link_twitter")),
        "youtube":   _norm(request.form.get("link_youtube")),
        "threads":   _norm(request.form.get("link_threads")),
    }
    links = {k: v for k, v in links.items() if v}

    with db() as c:
        c.execute(
            "UPDATE users SET bio = %s, social_links = %s WHERE id = %s",
            (bio, Json(links), u["id"])
        )
    return redirect(f"/u/{u['username']}")


# =================================================================
# Block / Unblock
# =================================================================
@app.route("/block/<username>", methods=["POST"])
@login_required
def block_user(username):
    u = current_user()
    with db() as c:
        target = c.fetchone("SELECT id FROM users WHERE username = %s", (username,))
        if not target:
            abort(404)
        if str(target["id"]) == str(u["id"]):
            return jsonify({"error": "자기 자신을 차단할 수 없습니다"}), 400
        c.execute(
            "INSERT INTO blocks (blocker_id, blocked_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (u["id"], target["id"])
        )
    return redirect(request.referrer or "/")


@app.route("/unblock/<username>", methods=["POST"])
@login_required
def unblock_user(username):
    u = current_user()
    with db() as c:
        target = c.fetchone("SELECT id FROM users WHERE username = %s", (username,))
        if not target:
            abort(404)
        c.execute(
            "DELETE FROM blocks WHERE blocker_id = %s AND blocked_id = %s",
            (u["id"], target["id"])
        )
    return redirect(request.referrer or "/")


@app.route("/blocked")
@login_required
def blocked_list():
    u = current_user()
    with db() as c:
        rows = c.fetchall(
            "SELECT u.username, u.avatar_url, b.created_at "
            "FROM blocks b JOIN users u ON u.id = b.blocked_id "
            "WHERE b.blocker_id = %s ORDER BY b.created_at DESC",
            (u["id"],)
        )
    return render_template("blocked.html", blocked=[dict(r) for r in rows])


# =================================================================
# Mute / Unmute (가벼운 숨김 — 피드에서만 안 보임)
# =================================================================
@app.route("/mute/<username>", methods=["POST"])
@login_required
def mute_user(username):
    u = current_user()
    with db() as c:
        target = c.fetchone("SELECT id FROM users WHERE username = %s", (username,))
        if not target:
            abort(404)
        if str(target["id"]) == str(u["id"]):
            return jsonify({"error": "자기 자신을 숨길 수 없습니다"}), 400
        c.execute(
            "INSERT INTO user_mutes (muter_id, muted_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (u["id"], target["id"])
        )
    return redirect(request.referrer or "/")


@app.route("/unmute/<username>", methods=["POST"])
@login_required
def unmute_user(username):
    u = current_user()
    with db() as c:
        target = c.fetchone("SELECT id FROM users WHERE username = %s", (username,))
        if not target:
            abort(404)
        c.execute(
            "DELETE FROM user_mutes WHERE muter_id = %s AND muted_id = %s",
            (u["id"], target["id"])
        )
    return redirect(request.referrer or "/")


@app.route("/muted")
@login_required
def muted_list():
    u = current_user()
    with db() as c:
        rows = c.fetchall(
            "SELECT u.username, u.avatar_url, u.bio, m.created_at "
            "FROM user_mutes m JOIN users u ON u.id = m.muted_id "
            "WHERE m.muter_id = %s ORDER BY m.created_at DESC",
            (u["id"],)
        )
    return render_template("muted.html", muted=[dict(r) for r in rows])


# =================================================================
# APP 카테고리
# =================================================================
@app.route("/apps")
def apps_index():
    u = current_user()
    viewer_id = u["id"] if u else None
    blocked = get_hidden_ids(viewer_id)
    category = request.args.get("category")

    sql = """
      SELECT ap.*, u.username, u.avatar_url, u.is_admin
      FROM app_posts ap JOIN users u ON u.id = ap.user_id
      WHERE ap.status = 'approved'
    """
    params = []
    if blocked:
        sql += " AND ap.user_id != ALL(%s)"
        params.append(list(blocked))
    if category:
        sql += " AND ap.category = %s"
        params.append(category)
    sql += " ORDER BY u.is_admin DESC, ap.created_at DESC LIMIT 60"

    with db() as c:
        rows = c.fetchall(sql, params)
        apps = [dict(r) for r in rows]
        for a in apps:
            a["id"] = str(a["id"])

    return render_template("apps_index.html", apps=apps, current_category=category)


@app.route("/apps/new", methods=["GET", "POST"])
@login_required
def app_new():
    u = current_user()
    if request.method == "POST":
        title = (request.form.get("title") or "").strip()[:200]
        app_name = (request.form.get("app_name") or "").strip()[:120]
        app_url = (request.form.get("app_url") or "").strip()[:500]
        category = (request.form.get("category") or "").strip()
        content = (request.form.get("content") or "").strip()[:APP_CONTENT_MAX]
        pros = (request.form.get("pros") or "").strip()[:2000]
        cons = (request.form.get("cons") or "").strip()[:2000]
        try:
            rating = int(request.form.get("rating") or 0)
        except ValueError:
            rating = 0
        rating = max(0, min(5, rating))

        if not (title and app_name and content):
            return render_template("app_new.html", error="제목/앱이름/본��은 필수예요"), 400

        if app_url and not app_url.startswith(("http://", "https://")):
            app_url = "https://" + app_url

        thumb_url = None
        thumb_file = request.files.get("thumbnail")
        if thumb_file and thumb_file.filename:
            ext = os.path.splitext(thumb_file.filename)[1].lower()
            if ext in ALLOWED_IMG:
                name = f"apps/{u['id']}-{int(time.time())}{ext}"
                thumb_url = storage_upload(thumb_file, name)

        status = "approved" if u.get("is_admin") else "pending"
        approved_at_sql = "NOW()" if status == "approved" else "NULL"
        approved_by = u["id"] if status == "approved" else None

        with db() as c:
            row = c.fetchone(
                f"INSERT INTO app_posts "
                f"(user_id, title, app_name, app_url, category, thumbnail_url, "
                f" content, pros, cons, rating, status, approved_at, approved_by) "
                f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,{approved_at_sql},%s) RETURNING id",
                (u["id"], title, app_name, app_url, category, thumb_url,
                 content, pros, cons, rating, status, approved_by)
            )
        if status == "approved":
            return redirect(f"/apps/{row['id']}")
        return render_template("app_new.html", submitted=True)
    return render_template("app_new.html")


@app.route("/apps/<app_id>")
def app_detail(app_id):
    u = current_user()
    viewer_id = u["id"] if u else None
    with db() as c:
        row = c.fetchone(
            "SELECT ap.*, u.username, u.avatar_url, u.is_admin "
            "FROM app_posts ap JOIN users u ON u.id = ap.user_id "
            "WHERE ap.id = %s", (app_id,)
        )
        if not row:
            abort(404)
        if row["status"] != "approved":
            if not viewer_id:
                abort(404)
            if str(viewer_id) != str(row["user_id"]) and not (u and u.get("is_admin")):
                abort(404)
        c.execute("UPDATE app_posts SET view_count = view_count + 1 WHERE id = %s", (app_id,))
    a = dict(row)
    a["id"] = str(a["id"])
    a["is_mine"] = viewer_id and str(viewer_id) == str(a["user_id"])
    return render_template("app_detail.html", app_post=a)


@app.route("/apps/<app_id>/approve", methods=["POST"])
@admin_required
def app_approve(app_id):
    u = current_user()
    with db() as c:
        c.execute(
            "UPDATE app_posts SET status = 'approved', approved_at = NOW(), approved_by = %s, reject_reason = NULL "
            "WHERE id = %s",
            (u["id"], app_id)
        )
    return redirect("/admin")


@app.route("/apps/<app_id>/reject", methods=["POST"])
@admin_required
def app_reject(app_id):
    reason = (request.form.get("reason") or "").strip()[:500]
    with db() as c:
        c.execute(
            "UPDATE app_posts SET status = 'rejected', reject_reason = %s WHERE id = %s",
            (reason, app_id)
        )
    return redirect("/admin")


@app.route("/apps/<app_id>/delete", methods=["POST"])
@login_required
def app_delete(app_id):
    u = current_user()
    with db() as c:
        row = c.fetchone("SELECT user_id FROM app_posts WHERE id = %s", (app_id,))
        if not row:
            abort(404)
        if str(row["user_id"]) != str(u["id"]) and not u.get("is_admin"):
            abort(403)
        c.execute("DELETE FROM app_posts WHERE id = %s", (app_id,))
    return redirect("/apps")




# =================================================================
# 캐릭터 저장소 (마이페이지 안, 본인만)
# =================================================================
def _parse_char_images(raw):
    """images JSONB → list[{url, id, created_at}]."""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try: return json.loads(raw) or []
        except Exception: return []
    return raw or []


@app.route("/characters")
@login_required
def my_characters():
    u = current_user()
    category = request.args.get("category")
    with db() as c:
        if category and category in CHARACTER_CATEGORIES:
            rows = c.fetchall(
                "SELECT * FROM characters WHERE user_id = %s AND category = %s ORDER BY created_at DESC",
                (u["id"], category)
            )
        else:
            rows = c.fetchall(
                "SELECT * FROM characters WHERE user_id = %s ORDER BY created_at DESC",
                (u["id"],)
            )
        characters = []
        for r in rows:
            d = dict(r)
            d["id"] = str(d["id"])
            d["images"] = _parse_char_images(d.get("images"))
            characters.append(d)
        total = c.fetchone(
            "SELECT COUNT(*) AS n FROM characters WHERE user_id = %s", (u["id"],)
        )
    return render_template(
        "characters.html",
        characters=characters,
        current_category=category,
        total_count=total["n"] if total else 0,
        max_chars=MAX_CHARACTERS_PER_USER,
    )


@app.route("/characters/new", methods=["GET", "POST"])
@login_required
def character_new():
    u = current_user()

    # 카운트 체크
    with db() as c:
        row = c.fetchone("SELECT COUNT(*) AS n FROM characters WHERE user_id = %s", (u["id"],))
        cur_count = row["n"] if row else 0
    if cur_count >= MAX_CHARACTERS_PER_USER:
        return render_template(
            "character_form.html", char=None, error=f"캐릭터는 최대 {MAX_CHARACTERS_PER_USER}개까지 저장 가능해요. (현재 {cur_count}개)",
            cur_count=cur_count
        ), 400

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()[:80]
        category = (request.form.get("category") or "기타").strip()
        if category not in CHARACTER_CATEGORIES:
            category = "기타"
        prompt = (request.form.get("prompt") or "").strip()[:5000]

        if not name:
            return render_template("character_form.html", char=None, error="캐릭터 이름은 필수예요", cur_count=cur_count), 400

        # 이미지 업로드
        char_id = str(uuid.uuid4())
        images = []
        skipped = []  # [(filename, reason), ...]
        for f in request.files.getlist("images"):
            if not f or not f.filename:
                continue
            if len(images) >= MAX_IMAGES_PER_CHARACTER:
                skipped.append((f.filename, f"최대 {MAX_IMAGES_PER_CHARACTER}장 초과"))
                break
            ext = os.path.splitext(f.filename)[1].lower()
            if ext not in ALLOWED_IMG:
                skipped.append((f.filename, f"지원하지 않는 형식 ({ext or '확장자 없음'})"))
                continue
            # 크기 체크
            f.stream.seek(0, 2)
            size = f.stream.tell()
            f.stream.seek(0)
            if size > CHARACTER_IMG_MAX_BYTES:
                skipped.append((f.filename, f"{size/1024/1024:.1f}MB — 최대 10MB"))
                continue
            try:
                name_path = f"characters/{u['id']}/{char_id}/{len(images)}{ext}"
                url = storage_upload(f, name_path)
                images.append({"url": url, "id": str(uuid.uuid4())})
            except Exception as e:
                skipped.append((f.filename, f"업로드 실패: {str(e)[:80]}"))
                continue

        with db() as c:
            c.execute(
                "INSERT INTO characters (id, user_id, name, category, prompt, images) VALUES (%s,%s,%s,%s,%s,%s)",
                (char_id, u["id"], name, category, prompt or None, Json(images))
            )

        if skipped:
            # flash로 다음 페이지에 표시
            session["char_upload_skipped"] = skipped
        return redirect(url_for("character_detail", char_id=char_id))

    return render_template("character_form.html", char=None, cur_count=cur_count)


@app.route("/characters/<char_id>")
@login_required
def character_detail(char_id):
    u = current_user()
    with db() as c:
        row = c.fetchone(
            "SELECT * FROM characters WHERE id = %s AND user_id = %s",
            (char_id, u["id"])
        )
        if not row:
            abort(404)
    char = dict(row)
    char["id"] = str(char["id"])
    char["images"] = _parse_char_images(char.get("images"))
    skipped = session.pop("char_upload_skipped", None)
    return render_template("character_detail.html", char=char,
                           max_images=MAX_IMAGES_PER_CHARACTER,
                           skipped=skipped)


@app.route("/characters/<char_id>/edit", methods=["GET", "POST"])
@login_required
def character_edit(char_id):
    u = current_user()
    with db() as c:
        row = c.fetchone(
            "SELECT * FROM characters WHERE id = %s AND user_id = %s",
            (char_id, u["id"])
        )
        if not row:
            abort(404)
    char = dict(row)
    char["id"] = str(char["id"])
    char["images"] = _parse_char_images(char.get("images"))

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()[:80] or char["name"]
        category = (request.form.get("category") or char["category"]).strip()
        if category not in CHARACTER_CATEGORIES:
            category = "기타"
        prompt = (request.form.get("prompt") or "").strip()[:5000]

        with db() as c:
            c.execute(
                "UPDATE characters SET name = %s, category = %s, prompt = %s, updated_at = NOW() WHERE id = %s",
                (name, category, prompt or None, char_id)
            )
        return redirect(url_for("character_detail", char_id=char_id))

    return render_template("character_form.html", char=char, cur_count=None)


@app.route("/characters/<char_id>/images/add", methods=["POST"])
@login_required
def character_images_add(char_id):
    """이미지 추가 — 캐릭터 디테일 페이지에서 드래그앤드롭."""
    u = current_user()
    with db() as c:
        row = c.fetchone(
            "SELECT * FROM characters WHERE id = %s AND user_id = %s",
            (char_id, u["id"])
        )
        if not row:
            abort(404)
    existing = _parse_char_images(row.get("images"))
    if len(existing) >= MAX_IMAGES_PER_CHARACTER:
        return redirect(url_for("character_detail", char_id=char_id))

    added = 0
    skipped = []
    for f in request.files.getlist("images"):
        if not f or not f.filename:
            continue
        if len(existing) + added >= MAX_IMAGES_PER_CHARACTER:
            skipped.append((f.filename, f"최대 {MAX_IMAGES_PER_CHARACTER}장 초과"))
            break
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in ALLOWED_IMG:
            skipped.append((f.filename, f"지원하지 않는 형식 ({ext or '확장자 없음'})"))
            continue
        f.stream.seek(0, 2)
        size = f.stream.tell()
        f.stream.seek(0)
        if size > CHARACTER_IMG_MAX_BYTES:
            skipped.append((f.filename, f"{size/1024/1024:.1f}MB — 최대 10MB"))
            continue
        try:
            idx = len(existing) + added
            name_path = f"characters/{u['id']}/{char_id}/{idx}-{int(time.time())}{ext}"
            url = storage_upload(f, name_path)
            existing.append({"url": url, "id": str(uuid.uuid4())})
            added += 1
        except Exception as e:
            skipped.append((f.filename, f"업로드 실패: {str(e)[:80]}"))
            continue

    if added > 0:
        with db() as c:
            c.execute(
                "UPDATE characters SET images = %s, updated_at = NOW() WHERE id = %s",
                (Json(existing), char_id)
            )
    if skipped:
        session["char_upload_skipped"] = skipped
    return redirect(url_for("character_detail", char_id=char_id))


@app.route("/characters/<char_id>/images/<image_id>/delete", methods=["POST"])
@login_required
def character_image_delete(char_id, image_id):
    u = current_user()
    with db() as c:
        row = c.fetchone(
            "SELECT * FROM characters WHERE id = %s AND user_id = %s",
            (char_id, u["id"])
        )
        if not row:
            abort(404)
        images = _parse_char_images(row.get("images"))
        target = next((i for i in images if i.get("id") == image_id), None)
        if target:
            try:
                storage_delete(target.get("url", ""))
            except Exception:
                pass
            images = [i for i in images if i.get("id") != image_id]
            c.execute(
                "UPDATE characters SET images = %s, updated_at = NOW() WHERE id = %s",
                (Json(images), char_id)
            )
    return redirect(url_for("character_detail", char_id=char_id))


@app.route("/characters/<char_id>/delete", methods=["POST"])
@login_required
def character_delete(char_id):
    u = current_user()
    with db() as c:
        row = c.fetchone(
            "SELECT * FROM characters WHERE id = %s AND user_id = %s",
            (char_id, u["id"])
        )
        if not row:
            abort(404)
        images = _parse_char_images(row.get("images"))
        for img in images:
            try:
                storage_delete(img.get("url", ""))
            except Exception:
                pass
        c.execute("DELETE FROM characters WHERE id = %s", (char_id,))
    return redirect(url_for("my_characters"))


# 업로드 페이지 "내 캐릭터 불러오기" 모달용 JSON API
@app.route("/api/my-characters")
@login_required
def api_my_characters():
    u = current_user()
    with db() as c:
        rows = c.fetchall(
            "SELECT id, name, category, images FROM characters WHERE user_id = %s ORDER BY created_at DESC",
            (u["id"],)
        )
    out = []
    for r in rows:
        d = dict(r)
        d["id"] = str(d["id"])
        d["images"] = _parse_char_images(d.get("images"))
        out.append(d)
    return jsonify({"characters": out})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)



























