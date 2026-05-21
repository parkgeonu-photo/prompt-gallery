"""Seed sample posts into Supabase (DB + Storage)."""
import os
import sys
import uuid
import time
import hashlib
import secrets
import urllib.request
import requests
import psycopg2
from psycopg2.extras import RealDictCursor

DATABASE_URL = os.environ["DATABASE_URL"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
BUCKET = "media"


def hash_password(pw):
    salt = secrets.token_hex(16)
    h = hashlib.scrypt(pw.encode(), salt=salt.encode(), n=16384, r=8, p=1, dklen=32).hex()
    return f"{salt}${h}"


SAMPLES = [
    ("https://d8j0ntlcm91z4.cloudfront.net/user_34ctFqCBIuenEEsMCqyrHXc4WX1/hf_20260521_021450_1b64e590-8b35-47a7-98c3-4dd57c36690d.png",
     "사이버펑크 도쿄의 밤",
     "Cyberpunk Tokyo street at night, neon signs reflecting on wet pavement, cinematic lighting, rain, ultra detailed",
     "Nano Banana", "사이버펑크,야경,도쿄,네온,비", 42),
    ("https://d8j0ntlcm91z4.cloudfront.net/user_34ctFqCBIuenEEsMCqyrHXc4WX1/hf_20260521_021455_e8479e09-f4a6-4464-b5e9-7d7ee0577022.png",
     "햇살 가득한 다락방 독서공간",
     "Cozy reading nook in a sunlit attic, warm afternoon light streaming through round window, stacks of vintage books, watercolor illustration style",
     "Nano Banana", "수채화,일러스트,책,따뜻한,인테리어", 28),
    ("https://d8j0ntlcm91z4.cloudfront.net/user_34ctFqCBIuenEEsMCqyrHXc4WX1/hf_20260521_021449_4b6d66d5-4f3c-47a5-9d22-11da7db16bc6.png",
     "산정상의 눈표범",
     "Majestic snow leopard sitting on a mountain ridge at sunset, golden hour, photo-realistic wildlife photography, telephoto lens",
     "Nano Banana", "야생동물,사진,눈표범,산,골든아워", 67),
    ("https://d8j0ntlcm91z4.cloudfront.net/user_34ctFqCBIuenEEsMCqyrHXc4WX1/hf_20260521_021453_d8f80d9d-1bfe-42cc-b013-0ba7a683dca4.png",
     "구름 위 떠다니는 섬",
     "Surreal floating islands with waterfalls cascading into clouds, fantasy landscape, dramatic sky, Studio Ghibli inspired",
     "Nano Banana", "판타지,지브리,풍경,초현실", 89),
    ("https://d8j0ntlcm91z4.cloudfront.net/user_34ctFqCBIuenEEsMCqyrHXc4WX1/hf_20260521_021452_5a95e9de-2ff5-49c3-88b9-ecb0b3f684a6.png",
     "북유럽풍 미니멀 카페",
     "Modern minimalist coffee shop interior, scandinavian design, soft morning light, plants, warm wood tones, architectural photography",
     "Nano Banana", "카페,인테리어,미니멀,북유럽,건축", 35),
    ("https://d8j0ntlcm91z4.cloudfront.net/user_34ctFqCBIuenEEsMCqyrHXc4WX1/hf_20260521_021456_17f9b1a0-42f9-47a9-bb9d-43320286b41c.png",
     "우주를 떠도는 우주비행사",
     "Astronaut floating in deep space, Earth reflected in helmet visor, stars, nebula in background, hyper realistic",
     "Nano Banana", "우주,SF,우주비행사,지구,초실사", 124),
]


def upload_to_storage(file_bytes, save_name, mime):
    url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{save_name}"
    headers = {
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": mime,
        "x-upsert": "true",
    }
    r = requests.post(url, data=file_bytes, headers=headers, timeout=60)
    r.raise_for_status()
    return f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/{save_name}"


def main():
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor(cursor_factory=RealDictCursor)

    # Schema bootstrap (idempotent)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id UUID PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            bio TEXT,
            created_at BIGINT NOT NULL
        );
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
            created_at BIGINT NOT NULL
        );
    """)
    conn.commit()

    # demo user
    cur.execute("SELECT id FROM users WHERE username = %s", ("demo",))
    row = cur.fetchone()
    if row:
        demo_id = str(row["id"])
        print(f"User 'demo' exists: {demo_id}")
    else:
        demo_id = str(uuid.uuid4())
        cur.execute(
            "INSERT INTO users (id, username, password_hash, created_at) VALUES (%s,%s,%s,%s)",
            (demo_id, "demo", hash_password("demo123"), int(time.time())),
        )
        conn.commit()
        print(f"Created user 'demo' (pw: demo123): {demo_id}")

    # posts
    cur.execute("SELECT COUNT(*) AS n FROM posts WHERE user_id = %s", (demo_id,))
    if cur.fetchone()["n"] > 0:
        print("Posts already seeded. Done.")
        return

    now = int(time.time())
    for i, (url, title, prompt, model, tags, likes) in enumerate(SAMPLES):
        print(f"[{i+1}/{len(SAMPLES)}] Downloading {title}...")
        with urllib.request.urlopen(url, timeout=30) as resp:
            data = resp.read()
        post_id = str(uuid.uuid4())
        save_name = f"demo/{post_id}.png"
        public = upload_to_storage(data, save_name, "image/png")
        cur.execute(
            """INSERT INTO posts (id, user_id, title, media_path, media_type, prompt, model, tags, author, likes, created_at)
               VALUES (%s,%s,%s,%s,'image',%s,%s,%s,'demo',%s,%s)""",
            (post_id, demo_id, title, public, prompt, model, tags, likes, now - i * 3600),
        )
        conn.commit()
        print(f"   -> uploaded: {public}")

    print(f"\nSeeded {len(SAMPLES)} posts.")


if __name__ == "__main__":
    main()

