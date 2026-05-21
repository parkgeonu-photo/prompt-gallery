"""Seed the demo gallery with AI-generated sample posts."""
import sqlite3
import urllib.request
import uuid
import time
from pathlib import Path

BASE = Path(__file__).parent
DB_PATH = BASE / "gallery.db"
UPLOAD_DIR = BASE / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

SAMPLES = [
    {
        "url": "https://d8j0ntlcm91z4.cloudfront.net/user_34ctFqCBIuenEEsMCqyrHXc4WX1/hf_20260521_021450_1b64e590-8b35-47a7-98c3-4dd57c36690d.png",
        "title": "사이버펑크 도쿄의 밤",
        "prompt": "Cyberpunk Tokyo street at night, neon signs reflecting on wet pavement, cinematic lighting, rain, ultra detailed",
        "model": "Nano Banana",
        "tags": "사이버펑크,야경,도쿄,네온,비",
        "author": "neon_dreamer",
        "likes": 42,
    },
    {
        "url": "https://d8j0ntlcm91z4.cloudfront.net/user_34ctFqCBIuenEEsMCqyrHXc4WX1/hf_20260521_021455_e8479e09-f4a6-4464-b5e9-7d7ee0577022.png",
        "title": "햇살 가득한 다락방 독서공간",
        "prompt": "Cozy reading nook in a sunlit attic, warm afternoon light streaming through round window, stacks of vintage books, watercolor illustration style",
        "model": "Nano Banana",
        "tags": "수채화,일러스트,책,따뜻한,인테리어",
        "author": "book_lover",
        "likes": 28,
    },
    {
        "url": "https://d8j0ntlcm91z4.cloudfront.net/user_34ctFqCBIuenEEsMCqyrHXc4WX1/hf_20260521_021449_4b6d66d5-4f3c-47a5-9d22-11da7db16bc6.png",
        "title": "산정상의 눈표범",
        "prompt": "Majestic snow leopard sitting on a mountain ridge at sunset, golden hour, photo-realistic wildlife photography, telephoto lens",
        "model": "Nano Banana",
        "tags": "야생동물,사진,눈표범,산,골든아워",
        "author": "wild_capture",
        "likes": 67,
    },
    {
        "url": "https://d8j0ntlcm91z4.cloudfront.net/user_34ctFqCBIuenEEsMCqyrHXc4WX1/hf_20260521_021453_d8f80d9d-1bfe-42cc-b013-0ba7a683dca4.png",
        "title": "구름 위 떠다니는 섬",
        "prompt": "Surreal floating islands with waterfalls cascading into clouds, fantasy landscape, dramatic sky, Studio Ghibli inspired",
        "model": "Nano Banana",
        "tags": "판타지,지브리,풍경,초현실",
        "author": "ghibli_fan",
        "likes": 89,
    },
    {
        "url": "https://d8j0ntlcm91z4.cloudfront.net/user_34ctFqCBIuenEEsMCqyrHXc4WX1/hf_20260521_021452_5a95e9de-2ff5-49c3-88b9-ecb0b3f684a6.png",
        "title": "북유럽풍 미니멀 카페",
        "prompt": "Modern minimalist coffee shop interior, scandinavian design, soft morning light, plants, warm wood tones, architectural photography",
        "model": "Nano Banana",
        "tags": "카페,인테리어,미니멀,북유럽,건축",
        "author": "cafe_designer",
        "likes": 35,
    },
    {
        "url": "https://d8j0ntlcm91z4.cloudfront.net/user_34ctFqCBIuenEEsMCqyrHXc4WX1/hf_20260521_021456_17f9b1a0-42f9-47a9-bb9d-43320286b41c.png",
        "title": "우주를 떠도는 우주비행사",
        "prompt": "Astronaut floating in deep space, Earth reflected in helmet visor, stars, nebula in background, hyper realistic",
        "model": "Nano Banana",
        "tags": "우주,SF,우주비행사,지구,초실사",
        "author": "space_artist",
        "likes": 124,
    },
]


def main():
    # Init DB
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS posts (
        id TEXT PRIMARY KEY,
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
        created_at INTEGER NOT NULL
    )
    """)
    conn.commit()

    # Skip if already seeded
    count = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    if count > 0:
        print(f"DB already has {count} posts. Skipping seed.")
        return

    now = int(time.time())
    for i, s in enumerate(SAMPLES):
        post_id = str(uuid.uuid4())
        filename = f"{post_id}.png"
        target = UPLOAD_DIR / filename
        print(f"Downloading {s['title']}...")
        urllib.request.urlretrieve(s["url"], target)

        conn.execute(
            """INSERT INTO posts (id, title, media_path, media_type, prompt, model, tags, author, likes, created_at)
               VALUES (?, ?, ?, 'image', ?, ?, ?, ?, ?, ?)""",
            (post_id, s["title"], filename, s["prompt"], s["model"], s["tags"], s["author"], s["likes"], now - i * 3600),
        )
    conn.commit()
    conn.close()
    print(f"Seeded {len(SAMPLES)} posts.")


if __name__ == "__main__":
    main()

