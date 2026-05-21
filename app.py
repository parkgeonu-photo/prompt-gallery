import os
import sqlite3
import uuid
import time
import hashlib
import secrets
from pathlib import Path
from functools import wraps
from flask import Flask, request, render_template, redirect, url_for, send_from_directory, jsonify, abort, session, g

BASE = Path(__file__).parent
# Render 등 영구 디스크 마운트 경로 우선, 없으면 로컬 경로
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "gallery.db"
UPLOAD_DIR = DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

ALLOWED_IMG = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
ALLOWED_VID = {".mp4", ".webm", ".mov"}

AI_MODELS = [
    "Midjourney", "DALL-E 3", "Stable Diffusion", "Flux", "Nano Banana",
    "Seedream", "Imagen", "Sora", "Veo", "Seedance", "Kling",
    "Runway", "Pika", "Higgsfield", "기타"
]

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024
app.secret_key = os.environ.get("SECRET_KEY", "dev-" + secrets.token_hex(16))


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            bio TEXT,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS posts (
            id TEXT PRIMARY KEY,
            user_id TEXT,
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
            created_at INTEGER NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS idx_created ON posts(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_model ON posts(model);
        CREATE INDEX IF NOT EXISTS idx_user ON posts(user_id);
        """)
        # 기존 테이블에 user_id 컬럼이 없으면 추가 (마이그레이션)
        cols = [r["name"] for r in c.execute("PRAGMA table_info(posts)").fetchall()]
        if "user_id" not in cols:
            c.execute("ALTER TABLE posts ADD COLUMN user_id TEXT")


# ============== Auth helpers ==============

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
        row = c.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    g.user = dict(row) if row else None
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


# ============== Routes ==============

@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)


@app.route("/")
def index():
    model = request.args.get("model")
    sort = request.args.get("sort", "recent")
    media_filter = request.args.get("type", "image")  # 'image' | 'video' | 'all'
    if media_filter not in ("image", "video", "all"):
        media_filter = "image"

    with db() as c:
        # Counts per media type for tab badges
        counts = {row["media_type"]: row["c"] for row in c.execute(
            "SELECT media_type, COUNT(*) AS c FROM posts GROUP BY media_type"
        ).fetchall()}
        image_count = counts.get("image", 0)
        video_count = counts.get("video", 0)
        all_count = image_count + video_count

        sql = "SELECT * FROM posts WHERE 1=1"
        params = []
        if media_filter in ("image", "video"):
            sql += " AND media_type = ?"
            params.append(media_filter)
        if model:
            sql += " AND model = ?"
            params.append(model)
        sql += " ORDER BY likes DESC, created_at DESC" if sort == "popular" else " ORDER BY created_at DESC"
        sql += " LIMIT 120"
        posts = [dict(r) for r in c.execute(sql, params).fetchall()]
        for p in posts:
            p["tags"] = (p["tags"] or "").split(",") if p["tags"] else []

    return render_template(
        "index.html",
        posts=posts,
        models=AI_MODELS,
        current_model=model,
        current_sort=sort,
        current_type=media_filter,
        image_count=image_count,
        video_count=video_count,
        all_count=all_count,
    )


@app.route("/post/<post_id>")
def post_detail(post_id):
    with db() as c:
        row = c.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
        if not row:
            abort(404)
        c.execute("UPDATE posts SET views = views + 1 WHERE id = ?", (post_id,))
        c.commit()
        post = dict(row)
        post["tags"] = (post["tags"] or "").split(",") if post["tags"] else []
    return render_template("detail.html", post=post)


@app.route("/post/<post_id>/delete", methods=["POST"])
@login_required
def post_delete(post_id):
    u = current_user()
    with db() as c:
        row = c.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
        if not row:
            abort(404)
        if row["user_id"] != u["id"]:
            abort(403)
        # 파일도 삭제
        try:
            (UPLOAD_DIR / row["media_path"]).unlink()
        except FileNotFoundError:
            pass
        c.execute("DELETE FROM posts WHERE id = ?", (post_id,))
        c.commit()
    return redirect(url_for("user_page", username=u["username"]))


@app.route("/upload", methods=["GET", "POST"])
@login_required
def upload():
    u = current_user()
    if request.method == "POST":
        f = request.files.get("file")
        prompt = (request.form.get("prompt") or "").strip()
        model = request.form.get("model") or "기타"
        title = (request.form.get("title") or "").strip()
        neg = (request.form.get("negative_prompt") or "").strip()
        tags_raw = (request.form.get("tags") or "").strip()

        if not f or not f.filename or not prompt:
            return render_template("upload.html", models=AI_MODELS, error="파일과 프롬프트는 필수입니다"), 400

        ext = os.path.splitext(f.filename)[1].lower()
        if ext in ALLOWED_IMG:
            media_type = "image"
        elif ext in ALLOWED_VID:
            media_type = "video"
        else:
            return render_template("upload.html", models=AI_MODELS, error="지원하지 않는 파일 형식"), 400

        new_id = str(uuid.uuid4())
        save_name = f"{new_id}{ext}"
        f.save(UPLOAD_DIR / save_name)

        tags_clean = ",".join([t.lstrip("#").strip().lower() for t in tags_raw.replace(",", " ").split() if t.strip()])

        with db() as c:
            c.execute(
                """INSERT INTO posts (id, user_id, title, media_path, media_type, prompt, negative_prompt, model, tags, author, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (new_id, u["id"], title or None, save_name, media_type, prompt, neg or None, model, tags_clean, u["username"], int(time.time())),
            )
            c.commit()
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
            sql = "SELECT * FROM posts WHERE (prompt LIKE ? OR title LIKE ? OR tags LIKE ? OR author LIKE ?)"
            params = [f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%"]
            if media_filter in ("image", "video"):
                sql += " AND media_type = ?"
                params.append(media_filter)
            sql += " ORDER BY created_at DESC LIMIT 120"
            rows = c.execute(sql, params).fetchall()
            posts = [dict(r) for r in rows]
            for p in posts:
                p["tags"] = (p["tags"] or "").split(",") if p["tags"] else []
    return render_template("search.html", posts=posts, q=q, current_type=media_filter)


@app.route("/like/<post_id>", methods=["POST"])
def like(post_id):
    with db() as c:
        c.execute("UPDATE posts SET likes = likes + 1 WHERE id = ?", (post_id,))
        c.commit()
        row = c.execute("SELECT likes FROM posts WHERE id = ?", (post_id,)).fetchone()
    return jsonify({"likes": row["likes"] if row else 0})


# ============== User pages ==============

@app.route("/u/<username>")
def user_page(username):
    with db() as c:
        u = c.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if not u:
            abort(404)
        rows = c.execute("SELECT * FROM posts WHERE user_id = ? ORDER BY created_at DESC", (u["id"],)).fetchall()
        posts = [dict(r) for r in rows]
        for p in posts:
            p["tags"] = (p["tags"] or "").split(",") if p["tags"] else []
        # stats
        total_likes = sum(p["likes"] for p in posts)
        total_views = sum(p["views"] for p in posts)
    return render_template("profile.html", profile=dict(u), posts=posts, total_likes=total_likes, total_views=total_views)


# ============== Auth routes ==============

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
            existing = c.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
            if existing:
                return render_template("signup.html", error="이미 사용 중인 사용자명입니다"), 400
            uid = str(uuid.uuid4())
            c.execute(
                "INSERT INTO users (id, username, password_hash, created_at) VALUES (?, ?, ?, ?)",
                (uid, username, hash_password(password), int(time.time())),
            )
            c.commit()
        session["user_id"] = uid
        return redirect(request.args.get("next") or url_for("index"))
    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip().lower()
        password = request.form.get("password") or ""
        with db() as c:
            row = c.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if not row or not verify_password(password, row["password_hash"]):
            return render_template("login.html", error="사용자명 또는 비밀번호가 잘못되었습니다"), 400
        session["user_id"] = row["id"]
        return redirect(request.args.get("next") or url_for("index"))
    return render_template("login.html")


@app.route("/logout", methods=["POST", "GET"])
def logout():
    session.clear()
    return redirect(url_for("index"))


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=False)




