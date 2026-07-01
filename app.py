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
MAX_CHARACTERS_PER_USER = 60
MAX_IMAGES_PER_CHARACTER = 30
CHARACTER_IMG_MAX_BYTES = 10 * 1024 * 1024  # 10MB per image (캐릭터시트 합성 이미지 고려)
CHARACTER_CATEGORIES = ["남자", "여자", "동물", "기타"]

NOTICE_CATEGORIES = [
    "업데이트", "공지", "앱 소개", "팁/가이드", "기타"
]

CONTENT_CATEGORIES = [
    "액션", "드라마", "쇼츠", "광고", "뷰티", "패션", "제품", "푸드", "기타"
]

# Portfolio
PORTFOLIO_IMG_MAX_BYTES = 5 * 1024 * 1024       # 이미지 1장당 5MB
PORTFOLIO_IMG_MAX_COUNT = 8                       # 게시물당 이미지 최대 8장
PORTFOLIO_VID_MAX_BYTES = 50 * 1024 * 1024       # 비디오 1개당 50MB
PORTFOLIO_VID_MAX_COUNT = 1                       # 게시물당 비디오 최대 1개
PORTFOLIO_POST_MAX_BYTES = 50 * 1024 * 1024      # 게시물당 총 50MB
PORTFOLIO_USER_MAX_BYTES = 5 * 1024 * 1024 * 1024  # 1인당 총 5GB
PORTFOLIO_CATEGORIES = ["사진", "영상", "AI"]

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024
app.secret_key = os.environ.get("SECRET_KEY", "dev-" + secrets.token_hex(16))


@app.after_request
def add_no_cache_headers(resp):
    """HTML 응답에 캐싱 방지 헤더 — 코드 업데이트가 즉시 반영되도록."""
    ct = resp.headers.get("Content-Type", "")
    if "text/html" in ct:
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp


@app.route("/_build")
def build_info():
    """빌드 추적용 — 어떤 커밋이 서빙 중인지 확인."""
    return {
        "commit": os.environ.get("RENDER_GIT_COMMIT", "local"),
        "build": "google-oauth-v1",
        "char_max_mb": CHARACTER_IMG_MAX_BYTES // (1024*1024),
        "google_oauth": bool(os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET")),
    }


# =================================================================
# SEO: sitemap.xml, robots.txt
# =================================================================
@app.route("/sitemap.xml")
def sitemap():
    from flask import Response
    pages = []
    base = "https://www.xazinga.com"
    # 고정 페이지
    for path in ["/", "/explore", "/portfolio", "/apps", "/skills"]:
        pages.append(f'  <url><loc>{base}{path}</loc><changefreq>daily</changefreq><priority>0.8</priority></url>')
    # 게시물
    try:
        with db() as c:
            rows = c.fetchall("SELECT id, created_at FROM posts WHERE visibility = 'public' ORDER BY created_at DESC LIMIT 500")
            for r in rows:
                pages.append(f'  <url><loc>{base}/post/{r["id"]}</loc><priority>0.6</priority></url>')
            # 유저 프로필
            rows = c.fetchall("SELECT username FROM users ORDER BY created_at DESC LIMIT 200")
            for r in rows:
                pages.append(f'  <url><loc>{base}/u/{r["username"]}</loc><priority>0.5</priority></url>')
            # 포트폴리오
            rows = c.fetchall(
                "SELECT u.username FROM portfolio_members pm "
                "JOIN users u ON u.id = pm.user_id WHERE pm.status = 'approved'"
            )
            for r in rows:
                pages.append(f'  <url><loc>{base}/portfolio/u/{r["username"]}</loc><priority>0.5</priority></url>')
    except Exception:
        pass
    xml = '<?xml version="1.0" encoding="UTF-8"?>\n'
    xml += '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    xml += '\n'.join(pages)
    xml += '\n</urlset>'
    return Response(xml, mimetype='application/xml')


@app.route("/robots.txt")
def robots():
    from flask import Response
    txt = """User-agent: *
Allow: /
Disallow: /admin
Disallow: /messages
Disallow: /characters
Disallow: /api/
Sitemap: https://www.xazinga.com/sitemap.xml"""
    return Response(txt, mimetype='text/plain')


# =================================================================
# 다국어 지원
# =================================================================
TRANSLATIONS = {
    "ko": {
        "upload": "업로드", "login": "로그인", "signup": "회원가입", "logout": "로그아웃",
        "search": "검색", "like": "좋아요", "comment": "댓글", "delete": "삭제",
        "edit": "수정", "save": "저장", "cancel": "취소", "submit": "제출",
        "my_page": "MY PAGE", "admin": "관리자", "follow": "팔로우",
        "portfolio_apply": "포트폴리오 사용 신청", "pending": "승인 대기 중이에요",
        "no_posts": "아직 게시물이 없어요.", "write": "글 쓰기",
        "title_required": "제목은 필수예요", "login_required": "로그인이 필요해요",
        "copied": "복사 완료!", "back": "돌아가기",
        "hero_desc": "AI로 만든 작품과 사용한 프롬프트를 함께 아카이브하는 갤러리",
        # 로그인/회원가입
        "welcome_back": "다시 오신 것을 환영합니다", "welcome": "환영합니다",
        "username": "아이디", "password": "비밀번호", "password_confirm": "비밀번호 확인",
        "or_google": "Google 계정으로 계속하기", "no_account": "계정이 없으신가요?",
        "has_account": "이미 계정이 있으신가요?",
        # 업로드
        "upload_title": "작품 올리기", "title": "제목", "prompt": "프롬프트",
        "negative_prompt": "네거티브 프롬프트", "model": "모델", "tags": "태그",
        "media": "미디어", "visibility": "공개 설정",
        "public": "공개", "partial": "일부 공개", "private": "비공개",
        "source_url": "원본 URL", "process_text": "작업 과정",
        "drag_drop": "클릭하거나 드래그해서 파일 추가",
        # 갤러리
        "all": "전체", "recent": "최신순", "popular": "인기순", "images": "이미지", "videos": "영상",
        "filter_model": "모델 필터", "sort_by": "정렬",
        # 프로필
        "posts": "게시물", "likes": "좋아요", "views": "조회수",
        "edit_profile": "프로필 수정", "new_post": "새 게시물", "bio": "소개",
        "send_message": "메시지 보내기", "block": "차단", "mute": "숨기기",
        "manage": "관리",
        # 포트폴리오
        "portfolio_desc": "크리에이터들의 작품 포트폴리오를 만나보세요.",
        "my_portfolio": "내 포트폴리오", "upload_work": "작품 올리기",
        "usage": "사용량", "works": "작품",
        # NOTICE
        "notice_desc": "업데이트 소식, 공지사항, 추천 앱 소개를 확인하세요.",
        # 스킬
        "skills": "스킬", "add": "추가", "no_skills": "아직 등록된 스킬이 없어요.",
        # 공통
        "confirm_delete": "삭제할까요?", "loading": "로딩 중...",
        "no_results": "결과가 없어요.", "view_all": "전체 보기",
        "enter": "입장", "messages": "메시지",
        "characters": "캐릭터", "first_creator": "첫 번째 크리에이터가 되어보세요!",
    },
    "en": {
        "upload": "Upload", "login": "Login", "signup": "Sign Up", "logout": "Logout",
        "search": "Search", "like": "Like", "comment": "Comment", "delete": "Delete",
        "edit": "Edit", "save": "Save", "cancel": "Cancel", "submit": "Submit",
        "my_page": "MY PAGE", "admin": "Admin", "follow": "Follow",
        "portfolio_apply": "Apply for Portfolio", "pending": "Pending approval",
        "no_posts": "No posts yet.", "write": "Write",
        "title_required": "Title is required", "login_required": "Login required",
        "copied": "Copied!", "back": "Back",
        "hero_desc": "Gallery for archiving AI-generated art with prompts",
        "welcome_back": "Welcome back", "welcome": "Welcome",
        "username": "Username", "password": "Password", "password_confirm": "Confirm password",
        "or_google": "Continue with Google", "no_account": "Don't have an account?",
        "has_account": "Already have an account?",
        "upload_title": "Upload Work", "title": "Title", "prompt": "Prompt",
        "negative_prompt": "Negative Prompt", "model": "Model", "tags": "Tags",
        "media": "Media", "visibility": "Visibility",
        "public": "Public", "partial": "Partial", "private": "Private",
        "source_url": "Source URL", "process_text": "Process",
        "drag_drop": "Click or drag to add files",
        "all": "All", "recent": "Recent", "popular": "Popular", "images": "Images", "videos": "Videos",
        "filter_model": "Model Filter", "sort_by": "Sort by",
        "posts": "Posts", "likes": "Likes", "views": "Views",
        "edit_profile": "Edit Profile", "new_post": "New Post", "bio": "Bio",
        "send_message": "Message", "block": "Block", "mute": "Mute",
        "manage": "Manage",
        "portfolio_desc": "Discover creators' portfolios.",
        "my_portfolio": "My Portfolio", "upload_work": "Upload Work",
        "usage": "Usage", "works": "Works",
        "notice_desc": "Updates, announcements, and app recommendations.",
        "skills": "Skills", "add": "Add", "no_skills": "No skills registered yet.",
        "confirm_delete": "Delete?", "loading": "Loading...",
        "no_results": "No results.", "view_all": "View All",
        "enter": "Enter", "messages": "Messages",
        "characters": "Characters", "first_creator": "Be the first creator!",
    },
    "ja": {
        "upload": "アップロード", "login": "ログイン", "signup": "会員登録", "logout": "ログアウト",
        "search": "検索", "like": "いいね", "comment": "コメント", "delete": "削除",
        "edit": "編集", "save": "保存", "cancel": "キャンセル", "submit": "送信",
        "my_page": "MY PAGE", "admin": "管理者", "follow": "フォロー",
        "portfolio_apply": "ポートフォリオ申請", "pending": "承認待ち",
        "no_posts": "まだ投稿がありません。", "write": "投稿する",
        "title_required": "タイトルは必須です", "login_required": "ログインが必要です",
        "copied": "コピー完了！", "back": "戻る",
        "hero_desc": "AIで作った作品とプロンプトをアーカイブするギャラリー",
        "welcome_back": "おかえりなさい", "welcome": "ようこそ",
        "username": "ユーザー名", "password": "パスワード", "password_confirm": "パスワード確認",
        "or_google": "Googleアカウントで続ける", "no_account": "アカウントをお持ちでないですか？",
        "has_account": "すでにアカウントをお持ちですか？",
        "upload_title": "作品を投稿", "title": "タイトル", "prompt": "プロンプト",
        "negative_prompt": "ネガティブプロンプト", "model": "モデル", "tags": "タグ",
        "media": "メディア", "visibility": "公開設定",
        "public": "公開", "partial": "一部公開", "private": "非公開",
        "source_url": "ソースURL", "process_text": "制作過程",
        "drag_drop": "クリックまたはドラッグでファイルを追加",
        "all": "全て", "recent": "新着順", "popular": "人気順", "images": "画像", "videos": "動画",
        "filter_model": "モデルフィルター", "sort_by": "並び替え",
        "posts": "投稿", "likes": "いいね", "views": "閲覧数",
        "edit_profile": "プロフィール編集", "new_post": "新規投稿", "bio": "自己紹介",
        "send_message": "メッセージ", "block": "ブロック", "mute": "ミュート",
        "manage": "管理",
        "portfolio_desc": "クリエイターのポートフォリオをご覧ください。",
        "my_portfolio": "マイポートフォリオ", "upload_work": "作品を投稿",
        "usage": "使用量", "works": "作品",
        "notice_desc": "アップデート情報、お知らせ、おすすめアプリ。",
        "skills": "スキル", "add": "追加", "no_skills": "まだスキルが登録されていません。",
        "confirm_delete": "削除しますか？", "loading": "読み込み中...",
        "no_results": "結果がありません。", "view_all": "全て見る",
        "enter": "入場", "messages": "メッセージ",
        "characters": "キャラクター", "first_creator": "最初のクリエイターになりましょう！",
    }
}



# 한국어 텍스트 → 영어/일본어 매핑 (한국어가 키, _() 함수에서 사용)
I18N = {
    "en": {
        # 공통
        "돌아오신 걸 환영해요": "Welcome back",
        "환영합니다": "Welcome",
        "계정이 없으신가요?": "Don't have an account?",
        "이미 계정이 있으신가요?": "Already have an account?",
        "Google 계정으로 로그인": "Continue with Google",
        "Google 계정으로 가입": "Continue with Google",
        "아이디": "Username",
        "비밀번호": "Password",
        "비밀번호 확인": "Confirm Password",
        "로그인이 필요해요": "Login required",
        "제목은 필수예요": "Title is required",
        # 업로드
        "작품 올리기": "Upload Work",
        "미디어 파일": "Media File",
        "클릭하거나 드래그해서 파일 추가": "Click or drag to add files",
        "제목": "Title",
        "프롬프트": "Prompt",
        "네거티브 프롬프트": "Negative Prompt",
        "모델 선택": "Select Model",
        "태그": "Tags",
        "공개 설정": "Visibility",
        "공개": "Public",
        "일부 공개": "Partial",
        "비공개": "Private",
        "원본 URL (선택)": "Source URL (optional)",
        "작업 과정 (선택)": "Process Notes (optional)",
        "작업 과정 이미지": "Process Images",
        "참고 자료": "References",
        "업로드 중...": "Uploading...",
        # 갤러리
        "전체": "All",
        "최신순": "Recent",
        "인기순": "Popular",
        "이미지": "Images",
        "영상": "Videos",
        # 프로필
        "게시물": "Posts",
        "좋아요": "Likes",
        "조회수": "Views",
        "프로필 수정": "Edit Profile",
        "소개": "Bio",
        "저장": "Save",
        "메시지 보내기": "Send Message",
        "차단": "Block",
        "차단 해제": "Unblock",
        "숨기기": "Mute",
        "숨기기 해제": "Unmute",
        "관리": "Manage",
        "아바타 변경": "Change Avatar",
        # 게시물 상세
        "댓글": "Comments",
        "댓글을 남겨보세요": "Leave a comment",
        "댓글 작성": "Post Comment",
        "수정": "Edit",
        "삭제": "Delete",
        "이 게시물을 삭제할까요?": "Delete this post?",
        "이 작품을 삭제할까요?": "Delete this work?",
        "이 글을 삭제할까요?": "Delete this post?",
        "목록으로": "Back to List",
        "프롬프트 복사": "Copy Prompt",
        "복사 완료!": "Copied!",
        # 포트폴리오
        "크리에이터들의 작품 포트폴리오를 만나보세요.": "Discover creators portfolios.",
        "포트폴리오 사용 신청": "Apply for Portfolio",
        "승인 대기 중이에요": "Pending approval",
        "내 포트폴리오": "My Portfolio",
        "아직 포트폴리오가 없어요.": "No portfolios yet.",
        "첫 번째 포트폴리오의 주인공이 되어보세요!": "Be the first portfolio creator!",
        "작품 올리기": "Upload Work",
        "아직 작품이 없어요.": "No works yet.",
        "첫 번째 작품을 올려보세요!": "Upload your first work!",
        "포트폴리오로 돌아가기": "Back to Portfolio",
        "사용량": "Usage",
        # NOTICE
        "업데이트 소식, 공지사항, 추천 앱 소개를 확인하세요.": "Updates, announcements, and app recommendations.",
        "글 쓰기": "Write",
        "아직 게시물이 없어요.": "No posts yet.",
        "첫 번째 글을 작성해보세요!": "Write the first post!",
        "아직 등록된 스킬이 없어요.": "No skills registered yet.",
        # 앱/노트 작성
        "업데이트 소식, 공지, 앱 소개 등 자유롭게 작성하세요.": "Write about updates, announcements, or app recommendations.",
        "제출 완료!": "Submitted!",
        "주인장이 확인 후 승인하면 NOTICE 페이지에 노출됩니다.": "It will appear on the NOTICE page after admin approval.",
        "NOTICE 목록으로 돌아가기 →": "Back to NOTICE →",
        "작성하면 바로 발행됩니다.": "Published immediately.",
        "가입자가 작성한 글은 주인장 승인 후 노출돼요.": "Posts by members require admin approval.",
        "제목 *": "Title *",
        "앱/서비스 이름 *": "App/Service Name *",
        "카테고리": "Category",
        "— 선택 —": "— Select —",
        "관련 URL (선택)": "Related URL (optional)",
        "썸네일 이미지": "Thumbnail Image",
        "16:9 권장 (없으면 기본 아이콘 표시)": "16:9 recommended (default icon if empty)",
        "본문 *": "Content *",
        "업데이트 내용, 공지사항, 앱 소개 등을 자유롭게 적어주세요...": "Write about updates, announcements, or apps...",
        "장점 / 하이라이트": "Highlights",
        "알려진 이슈 / 참고": "Known Issues / Notes",
        "평점 (앱 소개 시, 선택)": "Rating (optional)",
        "선택 안 함": "None",
        # 캐릭터
        "캐릭터 이름": "Character Name",
        "캐릭터당 이미지": "Images per Character",
        "새 캐릭터": "New Character",
        "이미지 추가": "Add Images",
        "이미지를 드래그앤드롭하세요": "Drag and drop images here",
        "이 캐릭터를 삭제할까요?": "Delete this character?",
        "이 이미지를 삭제할까요?": "Delete this image?",
        # 메시지
        "받은 메시지함": "Inbox",
        "메시지 보내기": "Send Message",
        "메시지를 입력하세요": "Type your message",
        "보내기": "Send",
        "아직 메시지가 없어요.": "No messages yet.",
        # 검색
        "검색어를 입력하세요": "Enter search query",
        "검색 결과": "Search Results",
        "결과가 없어요.": "No results found.",
        # 시작 페이지
        "입장": "Enter",
        # 스킬
        "AI 영상/이미지 제작에 유용한 스킬과 프롬프트 모음": "Collection of useful skills and prompt templates for AI creation",
        "스킬 추가": "Add Skill",
        "이름과 내용은 필수예요": "Name and content are required",
        "이 스킬이 무엇을 하는지 간단히": "Brief description of this skill",
        "스킬 내용, 프롬프트 템플릿, 사용법 등을 적어주세요...": "Write skill content, prompt templates, usage guide...",
        "프롬프트 템플릿, 워크플로우, 가이드 등 자유롭게 작성": "Prompt templates, workflows, guides etc.",
        "이 스킬을 삭제할까요?": "Delete this skill?",
        # 포트폴리오 폼
        "작품 제목": "Work Title",
        "이미지 추가": "Add Images",
        "클릭하거나 드래그해서 이미지 추가": "Click or drag to add images",
        "클릭하거나 드래그해서 영상 추가": "Click or drag to add video",
        # 어드민
        "사이트 설정과 승인 대기 게시물을 관리하세요.": "Manage site settings and pending posts.",
        "승인 대기 중인 글이 없어요.": "No pending posts.",
        "포트폴리오 승인 대기가 없어요.": "No pending portfolio requests.",
        "포트폴리오 사용 신청": "Portfolio application",
        "거절 사유?": "Rejection reason?",
    },
    "ja": {
        "돌아오신 걸 환영해요": "おかえりなさい",
        "환영합니다": "ようこそ",
        "계정이 없으신가요?": "アカウントをお持ちでないですか？",
        "이미 계정이 있으신가요?": "すでにアカウントをお持ちですか？",
        "Google 계정으로 로그인": "Googleで続ける",
        "Google 계정으로 가입": "Googleで続ける",
        "아이디": "ユーザー名",
        "비밀번호": "パスワード",
        "비밀번호 확인": "パスワード確認",
        "로그인이 필요해요": "ログインが必要です",
        "제목은 필수예요": "タイトルは必須です",
        "작품 올리기": "作品を投稿",
        "미디어 파일": "メディアファイル",
        "클릭하거나 드래그해서 파일 추가": "クリックまたはドラッグでファイルを追加",
        "제목": "タイトル",
        "프롬프트": "プロンプト",
        "네거티브 프롬프트": "ネガティブプロンプト",
        "모델 선택": "モデル選択",
        "태그": "タグ",
        "공개 설정": "公開設定",
        "공개": "公開",
        "일부 공개": "一部公開",
        "비공개": "非公開",
        "원본 URL (선택)": "ソースURL（任意）",
        "작업 과정 (선택)": "制作過程（任意）",
        "작업 과정 이미지": "制作過程の画像",
        "참고 자료": "参考資料",
        "업로드 중...": "アップロード中...",
        "전체": "全て",
        "최신순": "新着順",
        "인기순": "人気順",
        "이미지": "画像",
        "영상": "動画",
        "게시물": "投稿",
        "좋아요": "いいね",
        "조회수": "閲覧数",
        "프로필 수정": "プロフィール編集",
        "소개": "自己紹介",
        "저장": "保存",
        "메시지 보내기": "メッセージを送る",
        "차단": "ブロック",
        "차단 해제": "ブロック解除",
        "숨기기": "ミュート",
        "숨기기 해제": "ミュート解除",
        "관리": "管理",
        "아바타 변경": "アバター変更",
        "댓글": "コメント",
        "댓글을 남겨보세요": "コメントを残す",
        "댓글 작성": "コメントする",
        "수정": "編集",
        "삭제": "削除",
        "이 게시물을 삭제할까요?": "この投稿を削除しますか？",
        "이 작품을 삭제할까요?": "この作品を削除しますか？",
        "이 글을 삭제할까요?": "この投稿を削除しますか？",
        "목록으로": "一覧へ",
        "프롬프트 복사": "プロンプトをコピー",
        "복사 완료!": "コピー完了！",
        "크리에이터들의 작품 포트폴리오를 만나보세요.": "クリエイターのポートフォリオをご覧ください。",
        "포트폴리오 사용 신청": "ポートフォリオ申請",
        "승인 대기 중이에요": "承認待ち",
        "내 포트폴리오": "マイポートフォリオ",
        "아직 포트폴리오가 없어요.": "まだポートフォリオがありません。",
        "첫 번째 포트폴리오의 주인공이 되어보세요!": "最初のポートフォリオを作成しましょう！",
        "아직 작품이 없어요.": "まだ作品がありません。",
        "첫 번째 작품을 올려보세요!": "最初の作品を投稿しましょう！",
        "포트폴리오로 돌아가기": "ポートフォリオに戻る",
        "사용량": "使用量",
        "업데이트 소식, 공지사항, 추천 앱 소개를 확인하세요.": "アップデート情報、お知らせ、おすすめアプリ。",
        "글 쓰기": "投稿する",
        "아직 게시물이 없어요.": "まだ投稿がありません。",
        "첫 번째 글을 작성해보세요!": "最初の投稿を作成しましょう！",
        "아직 등록된 스킬이 없어요.": "まだスキルが登録されていません。",
        "업데이트 소식, 공지, 앱 소개 등 자유롭게 작성하세요.": "アップデート、お知らせ、アプリ紹介を自由に作成。",
        "제출 완료!": "送信完了！",
        "주인장이 확인 후 승인하면 NOTICE 페이지에 노출됩니다.": "管理者の承認後、NOTICEページに表示されます。",
        "NOTICE 목록으로 돌아가기 →": "NOTICEに戻る →",
        "작성하면 바로 발행됩니다.": "すぐに公開されます。",
        "가입자가 작성한 글은 주인장 승인 후 노출돼요.": "メンバーの投稿は管理者の承認が必要です。",
        "제목 *": "タイトル *",
        "앱/서비스 이름 *": "アプリ/サービス名 *",
        "카테고리": "カテゴリ",
        "— 선택 —": "— 選択 —",
        "관련 URL (선택)": "関連URL（任意）",
        "썸네일 이미지": "サムネイル画像",
        "16:9 권장 (없으면 기본 아이콘 표시)": "16:9推奨（なければデフォルトアイコン）",
        "본문 *": "本文 *",
        "업데이트 내용, 공지사항, 앱 소개 등을 자유롭게 적어주세요...": "アップデート、お知らせ、アプリ紹介を自由に...",
        "장점 / 하이라이트": "ハイライト",
        "알려진 이슈 / 참고": "既知の問題 / 参考",
        "평점 (앱 소개 시, 선택)": "評価（任意）",
        "선택 안 함": "なし",
        "캐릭터 이름": "キャラクター名",
        "새 캐릭터": "新規キャラクター",
        "이미지 추가": "画像を追加",
        "이미지를 드래그앤드롭하세요": "画像をドラッグ＆ドロップ",
        "이 캐릭터를 삭제할까요?": "このキャラクターを削除しますか？",
        "이 이미지를 삭제할까요?": "この画像を削除しますか？",
        "받은 메시지함": "受信箱",
        "메시지를 입력하세요": "メッセージを入力",
        "보내기": "送信",
        "아직 메시지가 없어요.": "まだメッセージがありません。",
        "검색어를 입력하세요": "検索ワードを入力",
        "검색 결과": "検索結果",
        "결과가 없어요.": "結果がありません。",
        "입장": "入場",
        "AI 영상/이미지 제작에 유용한 스킬과 프롬프트 모음": "AI制作に役立つスキルとプロンプト集",
        "스킬 추가": "スキル追加",
        "이름과 내용은 필수예요": "名前と内容は必須です",
        "이 스킬이 무엇을 하는지 간단히": "このスキルの簡単な説明",
        "스킬 내용, 프롬프트 템플릿, 사용법 등을 적어주세요...": "スキル内容、プロンプトテンプレート、使い方...",
        "프롬프트 템플릿, 워크플로우, 가이드 등 자유롭게 작성": "テンプレート、ワークフロー、ガイドなど",
        "이 스킬을 삭제할까요?": "このスキルを削除しますか？",
        "작품 제목": "作品タイトル",
        "클릭하거나 드래그해서 이미지 추가": "クリックまたはドラッグで画像を追加",
        "클릭하거나 드래그해서 영상 추가": "クリックまたはドラッグで動画を追加",
        "사이트 설정과 승인 대기 게시물을 관리하세요.": "サイト設定と承認待ち投稿を管理。",
        "승인 대기 중인 글이 없어요.": "承認待ちの投稿はありません。",
        "포트폴리오 승인 대기가 없어요.": "ポートフォリオの承認待ちはありません。",
        "거절 사유?": "却下理由？",
    }
}

@app.route("/set-lang/<lang_code>")
def set_lang(lang_code):
    if lang_code not in TRANSLATIONS:
        lang_code = "ko"
    resp = redirect(request.referrer or "/explore")
    resp.set_cookie("lang", lang_code, max_age=365*24*3600, samesite="Lax")
    return resp


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
        c.execute("ALTER TABLE posts ADD COLUMN IF NOT EXISTS category TEXT")
        c.execute("CREATE INDEX IF NOT EXISTS idx_posts_category ON posts(category)")
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

        # Portfolio
        c.execute("""
            CREATE TABLE IF NOT EXISTS portfolio_members (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE UNIQUE,
                status TEXT NOT NULL DEFAULT 'pending',
                total_bytes BIGINT NOT NULL DEFAULT 0,
                created_at BIGINT NOT NULL,
                approved_at BIGINT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_pm_status ON portfolio_members(status)")
        c.execute("""
            CREATE TABLE IF NOT EXISTS portfolio_posts (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                images JSONB DEFAULT '[]'::jsonb,
                video JSONB,
                total_bytes BIGINT NOT NULL DEFAULT 0,
                created_at BIGINT NOT NULL,
                updated_at BIGINT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_pp_user ON portfolio_posts(user_id, created_at DESC)")
        c.execute("ALTER TABLE portfolio_posts ADD COLUMN IF NOT EXISTS category TEXT")
        c.execute("ALTER TABLE portfolio_posts ADD COLUMN IF NOT EXISTS sort_order INT DEFAULT 0")

        # Skills
        c.execute("""
            CREATE TABLE IF NOT EXISTS skills (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                description TEXT,
                content TEXT NOT NULL,
                category TEXT,
                created_at BIGINT NOT NULL,
                updated_at BIGINT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_skills_created ON skills(created_at DESC)")

        # 번역 API 일일 사용량 추적 (레이트 리밋용)
        c.execute("""
            CREATE TABLE IF NOT EXISTS translation_usage (
                identifier TEXT NOT NULL,
                used_date DATE NOT NULL,
                count INT NOT NULL DEFAULT 0,
                PRIMARY KEY (identifier, used_date)
            )
        """)

        # 범용 레이트 리밋 (로그인 brute-force, 회원가입 남용 방지 등)
        c.execute("""
            CREATE TABLE IF NOT EXISTS rate_limits (
                id BIGSERIAL PRIMARY KEY,
                bucket TEXT NOT NULL,
                created_at BIGINT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_rate_limits_bucket ON rate_limits(bucket, created_at)")


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


def get_client_ip():
    """프록시(Render) 뒤에서 실제 클라이언트 IP 추출."""
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or "unknown"


def rate_limit_check(bucket, max_count, window_seconds):
    """슬라이딩 윈도우 레이트 리밋. 한도 내면 True(기록함), 초과면 False.
    bucket 예: 'login:1.2.3.4'. window_seconds 안에서 max_count회까지 허용."""
    now = int(time.time())
    window_start = now - window_seconds
    try:
        with db() as c:
            # 가끔 오래된 레코드 청소 (윈도우의 2배 이전 것)
            if now % 20 == 0:
                c.execute("DELETE FROM rate_limits WHERE created_at < %s", (now - window_seconds * 2,))
            row = c.fetchone(
                "SELECT COUNT(*) AS n FROM rate_limits WHERE bucket = %s AND created_at >= %s",
                (bucket, window_start),
            )
            if row and row["n"] >= max_count:
                return False
            c.execute(
                "INSERT INTO rate_limits (bucket, created_at) VALUES (%s, %s)",
                (bucket, now),
            )
        return True
    except Exception:
        # rate limit 시스템 장애 시 정상 동작 우선 (서비스 막지 않음)
        return True


def rate_limit_clear(bucket):
    """성공 시 해당 버킷 기록 삭제 (예: 로그인 성공하면 실패 카운트 리셋)."""
    try:
        with db() as c:
            c.execute("DELETE FROM rate_limits WHERE bucket = %s", (bucket,))
    except Exception:
        pass


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
    lang = request.cookies.get("lang", "ko")
    if lang not in TRANSLATIONS:
        lang = "ko"
    t = TRANSLATIONS[lang]
    def _(text):
        if lang == "ko":
            return text
        return I18N.get(lang, {}).get(text, text)
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
                pf_row = c.fetchone(
                    "SELECT COUNT(*) AS n FROM portfolio_members WHERE status = 'pending'"
                )
                pending_apps += pf_row["n"] if pf_row else 0
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
    lang = request.cookies.get("lang", "ko")
    if lang not in TRANSLATIONS:
        lang = "ko"
    t = TRANSLATIONS[lang]
    return {
        "user": u,
        "unread_count": unread,
        "pending_apps": pending_apps,
        "blocked_ids": blocked_ids,
        "muted_ids": muted_ids,
        "app_categories": NOTICE_CATEGORIES,
        "site": settings,
        "lang": lang,
        "t": t,
        "_": _,
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
    """매번 시작 페이지(ENTER 화면)를 보여줌. ENTER 누르면 /explore로 이동."""
    with db() as c:
        rows = c.fetchall("SELECT key, value FROM site_settings", ())
        s = {r["key"]: r["value"] for r in rows}
    cat_url = s.get("start_cat_url") or s.get("logo_url") or \
              "https://d2ol7oe51mr4n9.cloudfront.net/user_34ctFqCBIuenEEsMCqyrHXc4WX1/c598945a-597e-4392-bb17-a70a218961ae.jpg"

    return render_template("start.html", start_cat_url=cat_url)


@app.route("/explore")
def index():
    model = request.args.get("model")
    category = request.args.get("category")
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
            sql += " AND (model = %s OR model ILIKE %s)"
            params.append(model)
            params.append(model + ' %')
        if category:
            sql += " AND category = %s"
            params.append(category)
        if blocked:
            sql += " AND user_id != ALL(%s)"
            params.append(list(blocked))
        sql += " ORDER BY likes DESC, created_at DESC" if sort == "popular" else " ORDER BY created_at DESC"
        sql += " LIMIT 120"
        posts = [normalize_post(r, viewer_id) for r in c.fetchall(sql, params)]

        liked_ids = set()
        if viewer_id and posts:
            pids = [str(p["id"]) for p in posts]
            liked_rows = c.fetchall(
                "SELECT post_id FROM post_likes WHERE user_id = %s AND post_id = ANY(%s::uuid[])",
                (viewer_id, pids)
            )
            liked_ids = {str(r["post_id"]) for r in liked_rows}

    return render_template(
        "index.html",
        posts=posts, models=AI_MODELS,
        content_categories=CONTENT_CATEGORIES,
        current_model=model, current_category=category,
        current_sort=sort, current_type=media_filter,
        image_count=image_count, video_count=video_count, all_count=all_count,
        liked_ids=liked_ids,
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


@app.route("/post/<post_id>/edit", methods=["GET", "POST"])
@login_required
def post_edit(post_id):
    """게시물 수정 — 소유자만. 미디어 파일 자체는 교체 불가 (재업로드 권장).
    수정 가능 항목: title, prompt, negative_prompt, model, tags, visibility, source_url, process_text."""
    u = current_user()
    with db() as c:
        row = c.fetchone("SELECT * FROM posts WHERE id = %s", (post_id,))
        if not row:
            abort(404)
        if str(row["user_id"]) != str(u["id"]):
            abort(403)
    post = dict(row)
    post["id"] = str(post["id"])

    if request.method == "POST":
        title = (request.form.get("title") or "").strip()
        prompt = (request.form.get("prompt") or "").strip()
        model_choice = request.form.get("model") or "기타 (직접 입력)"
        model_custom = (request.form.get("model_custom") or "").strip()
        neg = (request.form.get("negative_prompt") or "").strip()
        tags_raw = (request.form.get("tags") or "").strip()
        visibility = (request.form.get("visibility") or "public").strip()
        source_url = (request.form.get("source_url") or "").strip()
        process_text = (request.form.get("process_text") or "").strip()[:PROCESS_TEXT_MAX]
        category = (request.form.get("category") or "").strip()
        if category and category not in CONTENT_CATEGORIES:
            category = None

        if visibility not in ("public", "partial", "private"):
            visibility = "public"
        if model_choice.startswith("기타"):
            model = model_custom or "기타"
        else:
            model = model_custom or model_choice
        if not prompt:
            return render_template("post_edit.html", post=post, models=AI_MODELS,
                content_categories=CONTENT_CATEGORIES, error="프롬프트는 필수입니다"), 400

        if source_url:
            if not (source_url.startswith("http://") or source_url.startswith("https://")):
                source_url = "https://" + source_url
            if len(source_url) > 500:
                source_url = source_url[:500]

        # 비공개로 바꾸려면 개수 제한 확인 (현재 비공개가 아닌 경우만)
        if visibility == "private" and (post.get("visibility") or "public") != "private":
            with db() as c:
                cnt = c.fetchone(
                    "SELECT COUNT(*) AS n FROM posts WHERE user_id = %s AND visibility = 'private'",
                    (u["id"],),
                )
                if cnt and cnt["n"] >= PRIVATE_POST_LIMIT:
                    return render_template(
                        "post_edit.html", post=post, models=AI_MODELS,
                        error=f"비공개는 최대 {PRIVATE_POST_LIMIT}개까지만 가능합니다. (현재 {cnt['n']}개)"
                    ), 400

        with db() as c:
            c.execute(
                """UPDATE posts SET title = %s, prompt = %s, negative_prompt = %s, model = %s,
                   tags = %s, visibility = %s, source_url = %s, process_text = %s, category = %s
                   WHERE id = %s""",
                (title or None, prompt, neg or None, model, tags_raw or None,
                 visibility, source_url or None, process_text or None, category or None, post_id)
            )
        return redirect(url_for("post_detail", post_id=post_id))

    return render_template("post_edit.html", post=post, models=AI_MODELS,
        content_categories=CONTENT_CATEGORIES)


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
        model_choice = request.form.get("model") or "기타 (직접 ���력)"
        model_custom = (request.form.get("model_custom") or "").strip()
        title = (request.form.get("title") or "").strip()
        neg = (request.form.get("negative_prompt") or "").strip()
        tags_raw = (request.form.get("tags") or "").strip()
        visibility = (request.form.get("visibility") or "public").strip()
        source_url = (request.form.get("source_url") or "").strip()
        process_text = (request.form.get("process_text") or "").strip()[:PROCESS_TEXT_MAX]
        category = (request.form.get("category") or "").strip()
        if category and category not in CONTENT_CATEGORIES:
            category = None
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
            return render_template("upload.html", models=AI_MODELS, content_categories=CONTENT_CATEGORIES, error="파일과 프롬프트는 필수입니다"), 400

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
            return render_template("upload.html", models=AI_MODELS, content_categories=CONTENT_CATEGORIES, error="지원하지 않는 파일 형식 (이미지/영상만 가능)"), 400

        new_id = str(uuid.uuid4())
        save_name = f"{u['username']}/{new_id}{ext}"
        try:
            public_url = storage_upload(f, save_name)
        except Exception as e:
            return render_template("upload.html", models=AI_MODELS, content_categories=CONTENT_CATEGORIES, error=f"업로드 실패: {e}"), 500

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
                """INSERT INTO posts (id, user_id, title, media_path, media_type, prompt, negative_prompt, model, tags, author, created_at, refs, visibility, source_url, process_text, process_images, category)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (new_id, u["id"], title or None, public_url, media_type, prompt,
                 neg or None, model, tags_clean, u["username"], int(time.time()),
                 Json(refs), visibility, source_url or None,
                 process_text or None, Json(process_images), category or None),
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
        content_categories=CONTENT_CATEGORIES,
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
        # 봇 대량 수집 방지: 같은 IP 분당 30회 (정상 사용엔 영향 없음)
        ip = get_client_ip()
        if not rate_limit_check(f"search:{ip}", max_count=30, window_seconds=60):
            return render_template("search.html", posts=[], q=q, current_type=media_filter,
                                   error="검색이 너무 많아요. 잠시 후 다시 시도해주세요."), 429
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
    """좋아요 토글 — 로그인 필수. 같은 게시물에 두 번 누르면 해제."""
    u = current_user()
    if not u:
        return jsonify({"error": "login_required"}), 401
    viewer_id = u["id"]
    with db() as c:
        row = c.fetchone("SELECT user_id, visibility FROM posts WHERE id = %s", (post_id,))
        if not row:
            return jsonify({"likes": 0}), 404
        if (row.get("visibility") or "public") == "private":
            if str(viewer_id) != str(row["user_id"]):
                return jsonify({"likes": 0}), 403
        # 토글
        existing = c.fetchone(
            "SELECT 1 FROM post_likes WHERE post_id = %s AND user_id = %s",
            (post_id, viewer_id)
        )
        if existing:
            c.execute("DELETE FROM post_likes WHERE post_id = %s AND user_id = %s", (post_id, viewer_id))
            c.execute("UPDATE posts SET likes = GREATEST(likes - 1, 0) WHERE id = %s", (post_id,))
            liked = False
        else:
            c.execute("INSERT INTO post_likes (post_id, user_id) VALUES (%s, %s)", (post_id, viewer_id))
            c.execute("UPDATE posts SET likes = likes + 1 WHERE id = %s", (post_id,))
            liked = True
        cnt = c.fetchone("SELECT likes FROM posts WHERE id = %s", (post_id,))
    return jsonify({"likes": cnt["likes"] if cnt else 0, "liked": liked})


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
        ip = get_client_ip()
        # 봇 대량 계정 생성 방지: 같은 IP에서 하루 3개까지
        if not rate_limit_check(f"signup:{ip}", max_count=3, window_seconds=86400):
            return render_template(
                "signup.html",
                error="가입 시도가 너무 많아요. 24시간 후 다시 시도해주세요."
            ), 429
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
        ip = get_client_ip()
        # brute-force 방지: 같은 IP에서 15분에 10회 실패하면 차단
        if not rate_limit_check(f"login:{ip}", max_count=10, window_seconds=900):
            return render_template(
                "login.html",
                error="로그인 시도가 너무 많아요. 15분 후 다시 시도해주세요."
            ), 429
        username = (request.form.get("username") or "").strip().lower()
        password = request.form.get("password") or ""
        with db() as c:
            row = c.fetchone("SELECT * FROM users WHERE username = %s", (username,))
        # 비밀번호 없는 구글 가입자는 일반 로그인 차단
        if not row or not row.get("password_hash") or not verify_password(password, row["password_hash"]):
            return render_template("login.html", error="사용자명 또는 비밀번호가 잘못되었습니다"), 400
        # 로그인 성공 → 해당 IP의 실패 카운트 리셋
        rate_limit_clear(f"login:{ip}")
        session["user_id"] = str(row["id"])
        return redirect(request.args.get("next") or url_for("index"))
    return render_template("login.html")


@app.route("/logout", methods=["POST", "GET"])
def logout():
    session.clear()
    return redirect(url_for("index"))


# =================================================================
# Google OAuth
# =================================================================
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI", "https://www.xazinga.com/auth/google/callback")


def _google_oauth_enabled():
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


def _unique_username_from_email(email):
    """email 앞부분을 username으로 변환, 중복 시 숫자 붙임."""
    base = (email.split("@")[0] if email else "user").lower()
    base = "".join(c for c in base if c.isalnum() or c in "_-")[:20] or "user"
    candidate = base
    i = 1
    with db() as c:
        while c.fetchone("SELECT 1 FROM users WHERE username = %s", (candidate,)):
            i += 1
            candidate = f"{base}{i}"
            if i > 9999:
                candidate = base + secrets.token_hex(3)
                break
    return candidate


@app.route("/auth/google/login")
def google_login():
    if not _google_oauth_enabled():
        return "Google OAuth가 설정되지 않았습니다. 관리자에게 문의하세요.", 503
    # CSRF state
    state = secrets.token_urlsafe(24)
    session["google_oauth_state"] = state
    session["google_oauth_next"] = request.args.get("next") or url_for("index")
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "online",
        "prompt": "select_account",
        "state": state,
    }
    qs = "&".join(f"{k}={requests.utils.quote(str(v), safe='')}" for k, v in params.items())
    return redirect(f"https://accounts.google.com/o/oauth2/v2/auth?{qs}")


@app.route("/auth/google/callback")
def google_callback():
    if not _google_oauth_enabled():
        return "Google OAuth가 설정되지 않았습니다.", 503

    error = request.args.get("error")
    if error:
        return render_template("login.html", error=f"Google 로그인 실패: {error}"), 400

    code = request.args.get("code")
    state = request.args.get("state")
    expected_state = session.pop("google_oauth_state", None)
    next_url = session.pop("google_oauth_next", "/")
    if not code or not state or state != expected_state:
        return render_template("login.html", error="잘못된 인증 요청입니다 (state 불일치)"), 400

    # 1) code → access token
    try:
        tok = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uri": GOOGLE_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
            timeout=15,
        )
        tok.raise_for_status()
        access_token = tok.json().get("access_token")
    except Exception as e:
        return render_template("login.html", error=f"토큰 교환 실패: {str(e)[:120]}"), 400

    # 2) userinfo 호출
    try:
        ui = requests.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
        ui.raise_for_status()
        info = ui.json()
    except Exception as e:
        return render_template("login.html", error=f"사용자 정보 조회 실패: {str(e)[:120]}"), 400

    google_id = info.get("sub")
    email = (info.get("email") or "").lower()
    name = info.get("name") or ""
    picture = info.get("picture") or ""

    if not google_id:
        return render_template("login.html", error="Google 계정 정보가 부족합니다"), 400

    # 3) 기존 유저 찾기: google_id 우선, 없으면 email 매칭
    with db() as c:
        row = c.fetchone("SELECT * FROM users WHERE google_id = %s", (google_id,))
        if not row and email:
            # 이메일 일치 시 기존 계정에 google_id 연결
            row = c.fetchone("SELECT * FROM users WHERE email = %s", (email,))
            if row:
                c.execute("UPDATE users SET google_id = %s WHERE id = %s", (google_id, row["id"]))
        if not row:
            # 신규 가입 — username 자동 생성
            uname = _unique_username_from_email(email or name)
            uid = str(uuid.uuid4())
            c.execute(
                """INSERT INTO users (id, username, password_hash, google_id, email, avatar_url, created_at)
                   VALUES (%s, %s, NULL, %s, %s, %s, %s)""",
                (uid, uname, google_id, email, picture or None, int(time.time()))
            )
            row = c.fetchone("SELECT * FROM users WHERE id = %s", (uid,))

    session["user_id"] = str(row["id"])
    # 안전한 redirect (외부 URL 차단)
    if not next_url or not next_url.startswith("/"):
        next_url = "/"
    return redirect(next_url)


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
        pf_pending = c.fetchall(
            "SELECT pm.*, u.username, u.avatar_url FROM portfolio_members pm "
            "JOIN users u ON u.id = pm.user_id "
            "WHERE pm.status = 'pending' ORDER BY pm.created_at ASC", ()
        )
        pf_pending = [dict(r) for r in pf_pending]
        for p in pf_pending:
            p["id"] = str(p["id"])

        # 용량 측정 (에러 시 기본값)
        try:
            db_size_row = c.fetchone("SELECT pg_database_size(current_database()) AS db_bytes")
            db_bytes = db_size_row["db_bytes"] if db_size_row else 0
        except Exception:
            db_bytes = 0

        try:
            storage_row = c.fetchone("""
                SELECT
                    COALESCE(SUM(CASE WHEN media_type = 'image' THEN 1 ELSE 0 END), 0) AS img_count,
                    COALESCE(SUM(CASE WHEN media_type = 'video' THEN 1 ELSE 0 END), 0) AS vid_count
                FROM posts
            """)
        except Exception:
            storage_row = {"img_count": 0, "vid_count": 0}

        try:
            extra = c.fetchone("""
                SELECT
                    (SELECT COUNT(*) FROM characters) AS char_count,
                    (SELECT COALESCE(SUM(total_bytes), 0) FROM portfolio_members) AS pf_total,
                    (SELECT COUNT(*) FROM portfolio_members WHERE status = 'approved') AS pf_members
            """)
        except Exception:
            extra = {"char_count": 0, "pf_total": 0, "pf_members": 0}

        usage = {
            "db_bytes": db_bytes,
            "db_gb": round(db_bytes / (1024**3), 2),
            "db_limit_gb": 8,
            "pf_bytes": extra["pf_total"] if extra else 0,
            "pf_gb": round((extra["pf_total"] or 0) / (1024**3), 2) if extra else 0,
            "img_count": storage_row["img_count"] if storage_row else 0,
            "vid_count": storage_row["vid_count"] if storage_row else 0,
            "char_count": extra["char_count"] if extra else 0,
            "pf_members": extra["pf_members"] if extra else 0,
        }

    return render_template("admin.html", settings=settings, pending=pending,
                           stats=stats, pf_pending=pf_pending, usage=usage)


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
# Mute / Unmute (가벼�� 숨김 — 피드에서만 안 보임)
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
# =================================================================
# Seedance 2.0 Guide Page
# =================================================================
SEEDANCE_CASES = [
    {
        "id": 1, "category": "캐릭터 애니메이션",
        "title": "이미지 한 장에서 캐릭터가 움직이는 영상",
        "inputs": "이미지 1장 (캐릭터 전신/반신)",
        "prompt": "소녀는 우아하게 빨래를 널고 있다. 건조가 끝난 뒤 양동이에서 새 빨래를 꺼내 힘차게 털어낸다. 카메라는 미디엄 샷, 자연광.",
        "tip": "@이미지1의 캐릭터 외모(얼굴, 옷, 체형)가 영상에서 그대로 유지됩니다. 동작은 시간순으로 나열하면 자연스러워요.",
        "lang": "ZH"
    },
    {
        "id": 2, "category": "캐릭터 애니메이션",
        "title": "퇴근 후 집에 도착하는 남자",
        "inputs": "이미지 1장 (남자 캐릭터)",
        "prompt": "@이미지1의 남자는 퇴근 후 피곤에 지쳐 복도를 걷다가 점점 느려지더니 집 문 앞에 멈춰 선다. 얼굴 클로즈업. 심호흡을 하고 문을 연다. 카메라는 천천히 push-in.",
        "tip": "감정을 '피곤하다'로 쓰지 말고, '속도가 느려진다', '심호흡' 같은 물리적 동작으로 표현하세요.",
        "lang": "ZH"
    },
    {
        "id": 3, "category": "카메라 워크 복제",
        "title": "영상의 카메라만 빌려서 다른 장면 만들기",
        "inputs": "비디오 1개 (카메라 레퍼런스)",
        "prompt": "@비디오1의 소녀를 오페라 배우로 교체. 장면은 화려한 무대. @비디오1의 카메라 움직임과 전환 효과를 참조하되 캐릭터와 배경은 복사하지 않음.",
        "tip": "반드시 '카메라 움직임만 참조'라고 명시하세요. 안 쓰면 캐릭터까지 복사됩니다.",
        "lang": "ZH"
    },
    {
        "id": 4, "category": "카메라 워크 복제",
        "title": "체스 게임에서 자갈길로 전환되는 원샷",
        "inputs": "비디오 1개 (전환 레퍼런스)",
        "prompt": "@비디오1의 모든 전환과 카메라 움직임을 처음부터 끝까지 참조. 체스 게임으로 시작하고, 카메라가 왼쪽으로 이동하며 바닥의 노란 자갈을 비추고, 점차 넓은 풍경으로 확대. 원샷.",
        "tip": "원샷(one-take)을 지정하면 중간에 컷이 끊기지 않는 연속 영상이 됩니다.",
        "lang": "ZH"
    },
    {
        "id": 5, "category": "캐릭터 교체",
        "title": "영상 속 인물만 다른 사람으로 교체",
        "inputs": "이미지 1장 + 비디오 1개",
        "prompt": "@비디오1의 캐릭터를 @이미지1로 교체. 첫 프레임을 @이미지1로 설정. @비디오1의 움직임과 표정 변화를 완전히 참조. 카메라 컷 없음.",
        "tip": "'완전히 참조'가 동작 복제의 핵심입니다. 첫 프레임을 이미지로 고정하면 캐릭터 일관성이 올라가요.",
        "lang": "ZH"
    },
    {
        "id": 6, "category": "캐릭터 교체",
        "title": "밴드 보컬을 다른 사람으로 교체",
        "inputs": "이미지 1장 (새 보컬) + 비디오 1개 (원본 공연)",
        "prompt": "@비디오1의 여성 리드 싱어를 @이미지1의 남성 싱어로 교체. 동작은 원본 영상에서 완전히 모방. 카메라 컷 없이 원샷. 밴드가 음악을 연주하는 장면.",
        "tip": "음악 장면에서는 '카메라 컷 없이'를 명시하지 않으면 Seedance가 임의로 컷을 넣어서 어색해질 수 있어요.",
        "lang": "ZH"
    },
    {
        "id": 7, "category": "상품 광고",
        "title": "가방 상업 사진 전시 영상",
        "inputs": "이미지 3장 (제품 여러 각도)",
        "prompt": "@이미지2 가방의 상업 사진 전시. 가방 측면은 @이미지1 참조, 표면 재질은 @이미지3 참조. 모든 디테일이 표시되어야 하며 카메라가 천천히 회전하면서 각도를 보여줌. 스튜디오 조명.",
        "tip": "여러 이미지로 제품의 다양한 각도/질감을 참조시키면 AI가 더 정확한 제품을 렌더링합니다.",
        "lang": "ZH"
    },
    {
        "id": 8, "category": "상품 광고",
        "title": "슈퍼카 다이나믹 주행 광고",
        "inputs": "이미지 1장 (차량) + 비디오 1개 (카메라 레퍼런스)",
        "prompt": "@비디오1의 카메라 움직임과 화면 전환 리듬을 참고. @이미지1의 빨간색 슈퍼카를 재현. 다이나믹한 주행 장면, 속도감 있는 편집.",
        "tip": "자동차 광고는 비디오 레퍼런스에서 카메라 리듬을 빌려오면 프로 느낌이 나요.",
        "lang": "ZH"
    },
    {
        "id": 9, "category": "멀티 이미지 합성",
        "title": "여러 이미지로 VR 공간 구성",
        "inputs": "이미지 4장 + 비디오 1개 (카메라 무빙)",
        "prompt": "@이미지1을 첫 프레임, 1인칭 관점으로. @비디오1의 카메라 이동 효과를 참조. 위 장면은 @이미지2, 왼쪽 장면은 @이미지3, 오른쪽 장면은 @이미지4. 엘리베이터 안에서 주변을 둘러보는 느낌.",
        "tip": "방향(위/아래/왼쪽/오른쪽)을 명확히 지정하면 이미지가 올바른 위치에 배치됩니다.",
        "lang": "ZH"
    },
    {
        "id": 10, "category": "멀티 이미지 합성",
        "title": "엘리베이터 안의 캐릭터 (배경+인물+공간 분리)",
        "inputs": "이미지 3장 + 비디오 1개",
        "prompt": "@이미지1의 남자를 참조. @이미지2의 엘리베이터에 있음. @비디오1의 모든 카메라 효과와 주인공의 표정을 완전하게 참조. 카메라가 천천히 pull-back.",
        "tip": "캐릭터(이미지1), 공간(이미지2), 동작(비디오1)을 각각 다른 소재로 분리 참조하면 합성 정확도가 올라가요.",
        "lang": "ZH"
    },
    {
        "id": 11, "category": "영상 연장",
        "title": "카페 영상을 15초 뒤로 연장",
        "inputs": "비디오 1개 (원본)",
        "prompt": "15초 연장. 0-5초: 빛과 그림자가 블라인드를 뚫고 나무 테이블 위로 천천히 미끄러짐. 나뭇가지가 숨결처럼 흔들림. 5-10초: 컵에서 김이 올라옴. 카메라 천천히 push-in. 10-15초: 손이 프레임에 들어와 컵을 집어 듦.",
        "tip": "연장은 시간 구간별로 반드시 나눠야 합니다. 안 나누면 Seedance가 중간을 알아서 채워서 어색해져요.",
        "lang": "ZH"
    },
    {
        "id": 12, "category": "영상 연장",
        "title": "스케이트보드 영상을 앞으로 10초 연장",
        "inputs": "비디오 1개 (원본)",
        "prompt": "10초를 앞으로 연장. 따뜻한 오후 햇살 속에서 카메라는 길모퉁이의 차양에서부터 시작하여 천천히 벽 바닥에 숨어있는 해바라기를 비추고, 점점 원본 영상의 시작 지점으로 이동.",
        "tip": "'앞으로 연장'은 프리퀄처럼 현재 영상 시작 전에 일어나는 일을 묘사하세요.",
        "lang": "ZH"
    },
    {
        "id": 13, "category": "스타일 전환",
        "title": "수묵화 스타일 태극권",
        "inputs": "이미지 1장 + 비디오 1개 (동작 레퍼런스)",
        "prompt": "흑백 잉크 스타일. @이미지1의 캐릭터는 @비디오1의 특수 효과와 동작을 참조하여 잉크 태극권 쿵푸 장면을 연출. 수묵 질감, 번지는 효과.",
        "tip": "스타일 키워드를 프롬프트 맨 앞에 넣으면 전체 분위기에 적용됩니다.",
        "lang": "ZH"
    },
    {
        "id": 14, "category": "스타일 전환",
        "title": "장미 꽃잎이 자라는 마술 효과",
        "inputs": "이미지 2장 + 비디오 1개",
        "prompt": "@비디오1의 첫 프레임 캐릭터를 @이미지1로 교체. @비디오1의 특수 효과와 움직임을 완전히 참조. 손바닥 위에서 장미 꽃잎이 자라나며 카메라 앞으로 확산.",
        "tip": "특수 효과(파티클, 꽃잎, 불꽃 등)는 별도로 명시해야 Seedance가 생성합니다.",
        "lang": "ZH"
    },
    {
        "id": 15, "category": "스토리보드 → 영상",
        "title": "만화 컷을 영상으로 변환",
        "inputs": "이미지 1장 (만화 페이지)",
        "prompt": "@이미지1을 왼쪽에서 오른쪽, 위에서 아래 순서로 만화 해석. 캐릭터 대사를 그림과 일치하게 유지. 스토리보드 전환과 주요 장면에 특수 효과 적용.",
        "tip": "만화/스토리보드는 읽는 순서(좌→우, 상→하)를 반드시 명시해야 Seedance가 올바른 순서로 해석합니다.",
        "lang": "ZH"
    },
    {
        "id": 16, "category": "스토리보드 → 영상",
        "title": "어린 시절의 사계절 힐링 영상",
        "inputs": "이미지 1장 (스토리보드 대본)",
        "prompt": "@이미지1의 스토리보드 대본을 참고. 풍경, 움직임, 그래픽, 텍스트를 참고하여 '어린 시절의 사계절'에 대한 15초 힐링 영상 제작. 부드러운 전환.",
        "tip": "스토리보드에 텍스트가 포함되어 있으면 Seedance가 읽고 해석합니다. 다만 한국어보다 중국어/영어 인식률이 더 높아요.",
        "lang": "ZH"
    },
    {
        "id": 17, "category": "액션 / 전투",
        "title": "단풍숲에서의 무기 대결",
        "inputs": "이미지 5장 (캐릭터 2명 + 배경) + 비디오 1개 (동작)",
        "prompt": "@이미지1과 @이미지2는 창 캐릭터, @이미지3과 @이미지4는 쌍검 캐릭터를 참고. @비디오1의 행동을 모방하여 @이미지5의 단풍잎 숲에서 전투.",
        "tip": "캐릭터는 3명 이하로 유지하세요. 4명 이상이면 Seedance가 캐릭터를 합치거나 사라지게 합니다.",
        "lang": "ZH"
    },
    {
        "id": 18, "category": "액션 / 전투",
        "title": "별빛 아래 에너지 파동 대결",
        "inputs": "이미지 2장 (캐릭터) + 비디오 2개 (동작 + 카메라)",
        "prompt": "@비디오1의 캐릭터 움직임과 @비디오2의 카메라 언어를 참조. @이미지1과 @이미지2의 싸움 장면. 별이 빛나는 밤. 충돌할 때 에너지 파동이 사방으로 퍼짐.",
        "tip": "비디오에서 동작을, 이미지에서 외모를 분리 참조하면 원하는 캐릭터가 원하는 동작을 합니다.",
        "lang": "ZH"
    },
    {
        "id": 19, "category": "대사 / 립싱크",
        "title": "동물 캐릭터 토크쇼",
        "inputs": "이미지 1장 (캐릭터들)",
        "prompt": "고양이 진행자(털을 핥고 눈을 굴림): '우리 가족은 내 옆에 앉기를 거부해!' 강아지 손님(고개를 끄덕이며): '저도요.' 토크쇼 무대 세트. 고정 카메라.",
        "tip": "대사는 15초에 25-30단어가 한계입니다. 넘으면 핵심 대사만 남기고 나머지는 신체 행동으로 변환하세요.",
        "lang": "ZH"
    },
    {
        "id": 20, "category": "대사 / 립싱크",
        "title": "군인 지휘 장면",
        "inputs": "이미지 1장 (캐릭터들)",
        "prompt": "고정 렌즈. 서 있는 강한 남자(대장)가 주먹을 쥐고 팔을 휘두르며 '3분 안에 공격하라!'라고 말한다. 옆의 팀원은 총기를 점검하고, 금발 팀원은 칼을 뽑아 준비.",
        "tip": "대사와 함께 신체 행동을 반드시 같이 묘사하세요. 입만 움직이면 어색해집니다.",
        "lang": "ZH"
    },
    {
        "id": 21, "category": "1인칭 POV",
        "title": "파쿠르 원샷 추적",
        "inputs": "이미지 5장 (경유지 장면들)",
        "prompt": "@이미지1~@이미지5. 거리에서 주자를 따라 계단 위로, 복도를 통과하여 지붕까지 올라가 마침내 도시를 내려다보는 원샷 추적. 1인칭 주관적 관점.",
        "tip": "여러 이미지를 경유지로 사용하면 경로가 풍부해집니다. '1인칭', 'POV'를 명확히 명시하세요.",
        "lang": "ZH"
    },
    {
        "id": 22, "category": "1인칭 POV",
        "title": "롤러코스터 스릴 체험",
        "inputs": "이미지 5장 (경유지)",
        "prompt": "@이미지1~@이미지5 주관적 시각으로 촬영한 스릴 넘치는 롤러코스터. 속도는 점점 빨라지고, 급커브와 급하강이 반복.",
        "tip": "속도 변화를 서술하면 리듬감이 살아납니다. '점점 빨라진다', '급하강' 같은 표현이 효과적.",
        "lang": "ZH"
    },
    {
        "id": 23, "category": "로고 / 텍스트 애니메이션",
        "title": "퍼즐 깨기 효과로 텍스트 등장",
        "inputs": "이미지 2장 + 비디오 1개 (효과 레퍼런스)",
        "prompt": "@이미지1의 천장부터 시작. @비디오1의 퍼즐 깨기 효과를 참조하여 전환. 텍스트가 파편에서 조립되듯 나타남. @이미지2의 글꼴 스타일 참조.",
        "tip": "비디오 레퍼런스에서 전환 효과만 빌려오면 로고 애니메이션 퀄리티가 크게 올라갑니다.",
        "lang": "ZH"
    },
    {
        "id": 24, "category": "로고 / 텍스트 애니메이션",
        "title": "황금 파티클로 브랜드 로고 형성",
        "inputs": "이미지 1장 (로고) + 비디오 1개 (파티클 효과)",
        "prompt": "검은 화면에서 시작. @비디오1의 입자 효과와 재질을 참고. 황금색 입자가 화면 왼쪽에서 떠올라 오른쪽으로 퍼지며 @이미지1의 로고 형태를 구성.",
        "tip": "파티클 효과 영상을 레퍼런스로 넣으면 Seedance가 같은 느낌의 입자를 생성합니다.",
        "lang": "ZH"
    },
    {
        "id": 25, "category": "패션 / 의상 쇼케이스",
        "title": "모델이 여러 의상을 번갈아 입는 영상",
        "inputs": "이미지 6장 (얼굴 1장 + 의상 5장) + 비디오 1개 (리듬)",
        "prompt": "@이미지1의 모델 얼굴 특징을 참고. @이미지2~@이미지6의 의상을 입고 카메라에 가까이 다가가며 장난꾸러기, 차가운, 귀여운, 놀란, 강렬한 표정을 차례로 연기. @비디오1의 전환 리듬 참조.",
        "tip": "첫 이미지는 얼굴 전용, 나머지에 각각 다른 의상을 배치하면 자동으로 옷이 바뀌는 효과가 나요.",
        "lang": "ZH"
    },
    {
        "id": 26, "category": "패션 / 의상 쇼케이스",
        "title": "의상 + 가방 브랜드 쇼케이스",
        "inputs": "이미지 4장 (의상 + 가방) + 비디오 1개 (리듬)",
        "prompt": "포스터 속 소녀가 끊임없이 옷을 갈아입음. 의상은 @이미지1과 @이미지2의 스타일. 손에 @이미지3 가방을 들고 있음. 영상 리듬은 @비디오1 참조. @이미지4의 배경.",
        "tip": "소품(가방, 액세서리)도 별도 이미지로 참조시키면 더 정확하게 렌더링됩니다.",
        "lang": "ZH"
    },
    {
        "id": 27, "category": "줄거리 반전",
        "title": "로맨스에서 서스펜스로 반전",
        "inputs": "비디오 1개 (원본 로맨스 장면)",
        "prompt": "@비디오1의 줄거리를 뒤집음. 남자의 눈빛이 갑자기 온화함에서 차갑고 맹렬하게 바뀜. 준비가 안 된 순간 갑자기 여주인공을 다리에서 밀어냄. 카메라가 빠르게 아래를 비춤.",
        "tip": "감정 전환점을 시간으로 명확히 지정하면 더 드라마틱한 반전이 됩니다.",
        "lang": "ZH"
    },
    {
        "id": 28, "category": "줄거리 반전",
        "title": "바에서의 서스펜스 반전",
        "inputs": "비디오 1개",
        "prompt": "@비디오1 전체 줄거리 전복. 0-3초: 양복 남자가 차분한 표정으로 바에 앉아 와인 잔을 듦. 카메라 천천히 전진. 3-8초: 갑자기 와인 잔을 바닥에 던지고 일어남. 빛과 그림자가 급변. 8-15초: 뒤에서 누군가 다가옴.",
        "tip": "카메라와 조명 변화도 감정 전환과 동기화시키면 영화 느낌이 나요.",
        "lang": "ZH"
    },
    {
        "id": 29, "category": "부동산 / 공간 소개",
        "title": "사무실 건물 시네마틱 소개",
        "inputs": "이미지 3-5장 (공간 사진) + 비디오 1개 (카메라 무빙)",
        "prompt": "제공된 사무실 건물 사진을 바탕으로 2.35:1 와이드스크린, 24fps, 섬세한 영상 스타일로 15초짜리 영화 수준의 부동산 소개 영상 제작. @비디오1의 카메라 무빙 참조. 드론 샷에서 시작해 내부로 진입.",
        "tip": "2.35:1 비율 + 24fps를 지정하면 자동으로 영화적 느낌이 적용됩니다.",
        "lang": "ZH"
    },
    {
        "id": 30, "category": "제품 가전 광고",
        "title": "레인지후드 광고",
        "inputs": "이미지 4장 (제품 + 장면)",
        "prompt": "@이미지1 첫 프레임에서 여성이 우아하게 요리 중. 연기 없음. 카메라가 @이미지2의 레인지후드를 촬영하기 위해 빠르게 위로 이동. @이미지3의 필터 클로즈업. @이미지4에서 제품 전체 모습. 깨끗한 주방 배경.",
        "tip": "가전 광고는 '문제 상황 → 제품 등장 → 해결' 순서로 프롬프트를 구성하면 효과적이에요.",
        "lang": "ZH"
    },
]


@app.route("/guide/seedance")
def seedance_guide():
    """Seedance 2.0 가이드 페이지."""
    category = request.args.get("category")
    categories = list(dict.fromkeys([c["category"] for c in SEEDANCE_CASES]))
    if category:
        cases = [c for c in SEEDANCE_CASES if c["category"] == category]
    else:
        cases = SEEDANCE_CASES
    return render_template("seedance_guide.html", cases=cases,
                           categories=categories, current_category=category)


# APP 카테고리 → Notice
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

    # 사이트 소개 글 맨 위 고정
    pinned = [a for a in apps if "사이트 소개" in (a.get("title") or "")]
    regular = [a for a in apps if a not in pinned]

    # 스킬 목록
    with db() as c:
        try:
            sk_rows = c.fetchall(
                "SELECT s.*, u.username, u.avatar_url, u.is_admin "
                "FROM skills s JOIN users u ON u.id = s.user_id "
                "ORDER BY s.created_at DESC LIMIT 20"
            )
            skills = [dict(r) for r in sk_rows]
            for s in skills:
                s["id"] = str(s["id"])
        except Exception:
            skills = []

    return render_template("apps_index.html", apps=regular, pinned=pinned,
                           current_category=category, skills=skills)


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
    """이미지 추가 — 캐릭터 디테일 페이지 또는 목록에서 드래그앤드롭."""
    u = current_user()
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    with db() as c:
        row = c.fetchone(
            "SELECT * FROM characters WHERE id = %s AND user_id = %s",
            (char_id, u["id"])
        )
        if not row:
            if is_ajax:
                return jsonify({"ok": False, "error": "캐릭터를 찾을 수 없어요."}), 404
            abort(404)
    existing = _parse_char_images(row.get("images"))
    if len(existing) >= MAX_IMAGES_PER_CHARACTER:
        if is_ajax:
            return jsonify({"ok": False, "error": f"이미 최대 {MAX_IMAGES_PER_CHARACTER}장이에요."}), 400
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
    if is_ajax:
        return jsonify({
            "ok": added > 0,
            "added": added,
            "total": len(existing),
            "skipped": [{"name": n, "reason": r} for n, r in skipped],
        })
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


@app.route("/dl")
@login_required
def force_download():
    """이미지 URL을 받아서 서버 경유로 강제 다운로드시키는 프록시."""
    url = request.args.get("u", "").strip()
    fname = request.args.get("n", "image").strip() or "image"
    if not url.startswith("http"):
        abort(400)
    try:
        r = requests.get(url, timeout=15, stream=True)
        if r.status_code != 200:
            abort(404)
        ct = r.headers.get("Content-Type", "application/octet-stream")
        resp = Response(r.content, mimetype=ct)
        resp.headers["Content-Disposition"] = f'attachment; filename="{fname}"'
        return resp
    except Exception:
        abort(500)


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


# =================================================================
# Skills (스킬 저장소)
# =================================================================

SKILL_CATEGORIES = ["영상 제작", "이미지 생성", "마케팅", "글쓰기", "개발", "기타"]

@app.route("/skills")
def skills_index():
    """스킬 저장소 목록."""
    category = request.args.get("category")
    sql = """
        SELECT s.*, u.username, u.avatar_url, u.is_admin
        FROM skills s JOIN users u ON u.id = s.user_id
    """
    params = []
    if category:
        sql += " WHERE s.category = %s"
        params.append(category)
    sql += " ORDER BY s.created_at DESC LIMIT 100"

    with db() as c:
        rows = c.fetchall(sql, params)
    skills = [dict(r) for r in rows]
    for s in skills:
        s["id"] = str(s["id"])

    return render_template("skills_index.html", skills=skills,
                           skill_categories=SKILL_CATEGORIES,
                           current_category=category)


@app.route("/skills/<skill_id>")
def skill_detail(skill_id):
    """스킬 상세."""
    with db() as c:
        row = c.fetchone(
            "SELECT s.*, u.username, u.avatar_url, u.is_admin "
            "FROM skills s JOIN users u ON u.id = s.user_id "
            "WHERE s.id = %s", (skill_id,)
        )
    if not row:
        abort(404)
    skill = dict(row)
    skill["id"] = str(skill["id"])
    u = current_user()
    is_mine = u and (str(u["id"]) == str(skill["user_id"]) or u.get("is_admin"))
    return render_template("skill_detail.html", skill=skill, is_mine=is_mine)


@app.route("/skills/new", methods=["GET", "POST"])
@admin_required
def skill_new():
    """스킬 추가 (어드민 전용)."""
    u = current_user()
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()[:200]
        description = (request.form.get("description") or "").strip()[:500]
        content = (request.form.get("content") or "").strip()[:20000]
        category = (request.form.get("category") or "").strip()

        if not (name and content):
            return render_template("skill_form.html",
                skill_categories=SKILL_CATEGORIES,
                error="이름과 내용은 필수예요"), 400

        with db() as c:
            c.execute(
                "INSERT INTO skills (user_id, name, description, content, category, created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (u["id"], name, description, content, category or None, int(time.time()))
            )
        return redirect("/skills")

    return render_template("skill_form.html", skill_categories=SKILL_CATEGORIES)


@app.route("/skills/<skill_id>/edit", methods=["GET", "POST"])
@admin_required
def skill_edit(skill_id):
    """스킬 수정."""
    u = current_user()
    with db() as c:
        row = c.fetchone("SELECT * FROM skills WHERE id = %s", (skill_id,))
    if not row:
        abort(404)
    skill = dict(row)
    skill["id"] = str(skill["id"])

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()[:200]
        description = (request.form.get("description") or "").strip()[:500]
        content = (request.form.get("content") or "").strip()[:20000]
        category = (request.form.get("category") or "").strip()

        if not (name and content):
            return render_template("skill_form.html",
                skill=skill, skill_categories=SKILL_CATEGORIES,
                error="이름과 내용은 필수예요"), 400

        with db() as c:
            c.execute(
                "UPDATE skills SET name=%s, description=%s, content=%s, category=%s, updated_at=%s "
                "WHERE id=%s",
                (name, description, content, category or None, int(time.time()), skill_id)
            )
        return redirect(f"/skills/{skill_id}")

    return render_template("skill_form.html", skill=skill, skill_categories=SKILL_CATEGORIES)


@app.route("/skills/<skill_id>/delete", methods=["POST"])
@admin_required
def skill_delete(skill_id):
    with db() as c:
        c.execute("DELETE FROM skills WHERE id = %s", (skill_id,))
    return redirect("/skills")


# =================================================================
# Portfolio
# =================================================================

def _portfolio_status(user_id):
    """유저의 포트폴리오 멤버 상태 반환. None이면 미가입."""
    with db() as c:
        row = c.fetchone(
            "SELECT * FROM portfolio_members WHERE user_id = %s", (user_id,)
        )
    return dict(row) if row else None


@app.route("/portfolio")
def portfolio_index():
    """승인된 포트폴리오 멤버 목록."""
    u = current_user()
    viewer_id = u["id"] if u else None
    blocked = get_hidden_ids(viewer_id) if viewer_id else set()

    sql = """
        SELECT pm.*, u.username, u.avatar_url, u.bio,
               (SELECT COUNT(*) FROM portfolio_posts pp WHERE pp.user_id = pm.user_id) AS post_count
        FROM portfolio_members pm
        JOIN users u ON u.id = pm.user_id
        WHERE pm.status = 'approved'
    """
    params = []
    if blocked:
        sql += " AND pm.user_id != ALL(%s)"
        params.append(list(blocked))
    sql += " ORDER BY pm.approved_at DESC NULLS LAST"

    with db() as c:
        rows = c.fetchall(sql, params)
    members = [dict(r) for r in rows]

    my_status = None
    if u:
        my_status = _portfolio_status(u["id"])

    return render_template("portfolio_index.html", members=members, my_status=my_status)


@app.route("/portfolio/apply", methods=["POST"])
@login_required
def portfolio_apply():
    """포트폴리오 사용 신청."""
    u = current_user()
    existing = _portfolio_status(u["id"])
    if existing:
        return redirect("/portfolio")

    status = "approved" if u.get("username") == "xazinga" else "pending"
    with db() as c:
        c.execute(
            "INSERT INTO portfolio_members (user_id, status, created_at, approved_at) "
            "VALUES (%s, %s, %s, %s)",
            (u["id"], status, int(time.time()),
             int(time.time()) if status == "approved" else None)
        )
    return redirect("/portfolio")


@app.route("/portfolio/u/<username>")
def portfolio_user(username):
    """특정 유저의 포트폴리오 페이지."""
    u = current_user()
    category = request.args.get("category")
    with db() as c:
        owner = c.fetchone("SELECT * FROM users WHERE username = %s", (username,))
        if not owner:
            abort(404)
        mem = c.fetchone(
            "SELECT * FROM portfolio_members WHERE user_id = %s AND status = 'approved'",
            (owner["id"],)
        )
        if not mem:
            abort(404)
        if category:
            posts = c.fetchall(
                "SELECT * FROM portfolio_posts WHERE user_id = %s AND category = %s ORDER BY sort_order ASC, created_at DESC",
                (owner["id"], category)
            )
        else:
            posts = c.fetchall(
                "SELECT * FROM portfolio_posts WHERE user_id = %s ORDER BY sort_order ASC, created_at DESC",
                (owner["id"],)
            )
    posts = [dict(p) for p in posts]
    for p in posts:
        p["id"] = str(p["id"])
        if isinstance(p.get("images"), str):
            try: p["images"] = json.loads(p["images"])
            except: p["images"] = []
        if isinstance(p.get("video"), str):
            try: p["video"] = json.loads(p["video"])
            except: p["video"] = None

    is_mine = u and str(u["id"]) == str(owner["id"])
    return render_template("portfolio_user.html", owner=owner, posts=posts,
                           member=dict(mem), is_mine=is_mine,
                           pf_categories=PORTFOLIO_CATEGORIES,
                           current_category=category)


@app.route("/portfolio/new", methods=["GET", "POST"])
@login_required
def portfolio_new():
    """포트폴리오 게시물 작성."""
    u = current_user()
    mem = _portfolio_status(u["id"])
    if not mem or mem["status"] != "approved":
        return redirect("/portfolio")

    if request.method == "POST":
        title = (request.form.get("title") or "").strip()[:200]
        category = (request.form.get("category") or "").strip()
        if not title:
            return render_template("portfolio_form.html",
                pf_categories=PORTFOLIO_CATEGORIES, error="제목은 필수예요"), 400

        # 용량 계산
        post_bytes = 0
        images_data = []
        video_data = None

        # 이미지 처리
        img_files = request.files.getlist("images")
        img_limit = 20 if u.get("is_admin") else PORTFOLIO_IMG_MAX_COUNT
        for i, f in enumerate(img_files[:img_limit]):
            if not f or not f.filename:
                continue
            ext = os.path.splitext(f.filename)[1].lower()
            if ext not in ALLOWED_IMG:
                continue
            f.stream.seek(0, 2)
            size = f.stream.tell()
            f.stream.seek(0)
            img_size_limit = 15 * 1024 * 1024 if u.get("is_admin") else PORTFOLIO_IMG_MAX_BYTES
            if size > img_size_limit:
                return render_template("portfolio_form.html",
                    error=f"이미지 '{f.filename}'이(가) {size/1024/1024:.1f}MB — 최대 5MB"), 400
            post_bytes += size

        # 비디오 처리
        vid_file = request.files.get("video")
        vid_size = 0
        if vid_file and vid_file.filename:
            ext = os.path.splitext(vid_file.filename)[1].lower()
            if ext not in ALLOWED_VID:
                return render_template("portfolio_form.html",
                    error="지원하지 않는 영상 형식이에요 (mp4/webm/mov만 가능)"), 400
            vid_file.stream.seek(0, 2)
            vid_size = vid_file.stream.tell()
            vid_file.stream.seek(0)
            if vid_size > PORTFOLIO_VID_MAX_BYTES:
                return render_template("portfolio_form.html",
                    error=f"영상이 {vid_size/1024/1024:.1f}MB — 최대 50MB"), 400
            post_bytes += vid_size

        if post_bytes > PORTFOLIO_POST_MAX_BYTES:
            return render_template("portfolio_form.html",
                error=f"게시물 총 용량 {post_bytes/1024/1024:.1f}MB — 최대 50MB"), 400

        # 유저 총 용량 체크 (어드민은 10GB)
        max_bytes = 10 * 1024 * 1024 * 1024 if u.get("is_admin") else PORTFOLIO_USER_MAX_BYTES
        user_total = mem.get("total_bytes", 0) or 0
        if user_total + post_bytes > max_bytes:
            remain = max(0, max_bytes - user_total)
            return render_template("portfolio_form.html",
                error=f"포트폴리오 총 용량 초과 (남은 공간: {remain/1024/1024:.0f}MB / 최대 5GB)"), 400

        # 실제 업로드
        post_id = str(uuid.uuid4())
        img_limit = 20 if u.get("is_admin") else PORTFOLIO_IMG_MAX_COUNT
        for i, f in enumerate(img_files[:img_limit]):
            if not f or not f.filename:
                continue
            ext = os.path.splitext(f.filename)[1].lower()
            if ext not in ALLOWED_IMG:
                continue
            f.stream.seek(0, 2)
            sz = f.stream.tell()
            f.stream.seek(0)
            img_sz_limit = 15 * 1024 * 1024 if u.get("is_admin") else PORTFOLIO_IMG_MAX_BYTES
            if sz > img_sz_limit:
                continue
            name = f"portfolio/{u['id']}/{post_id}/img-{i}{ext}"
            try:
                url = storage_upload(f, name)
                images_data.append({"url": url, "id": str(uuid.uuid4()), "size": sz})
            except Exception:
                continue

        if vid_file and vid_file.filename:
            ext = os.path.splitext(vid_file.filename)[1].lower()
            name = f"portfolio/{u['id']}/{post_id}/video{ext}"
            try:
                url = storage_upload(vid_file, name)
                video_data = {"url": url, "id": str(uuid.uuid4()), "size": vid_size, "ext": ext}
            except Exception:
                pass

        actual_bytes = sum(im.get("size", 0) for im in images_data) + (video_data.get("size", 0) if video_data else 0)

        with db() as c:
            c.execute(
                "INSERT INTO portfolio_posts (id, user_id, title, category, images, video, total_bytes, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (post_id, u["id"], title, category or None, Json(images_data),
                 Json(video_data) if video_data else None,
                 actual_bytes, int(time.time()))
            )
            c.execute(
                "UPDATE portfolio_members SET total_bytes = total_bytes + %s WHERE user_id = %s",
                (actual_bytes, u["id"])
            )
        return redirect(f"/portfolio/u/{u['username']}")

    return render_template("portfolio_form.html", pf_categories=PORTFOLIO_CATEGORIES)


@app.route("/portfolio/post/<post_id>")
def portfolio_post_detail(post_id):
    """포트폴리오 게시물 상세."""
    u = current_user()
    with db() as c:
        p = c.fetchone(
            "SELECT pp.*, u.username, u.avatar_url "
            "FROM portfolio_posts pp JOIN users u ON u.id = pp.user_id "
            "WHERE pp.id = %s", (post_id,)
        )
    if not p:
        abort(404)
    p = dict(p)
    p["id"] = str(p["id"])
    if isinstance(p.get("images"), str):
        try: p["images"] = json.loads(p["images"])
        except: p["images"] = []
    if isinstance(p.get("video"), str):
        try: p["video"] = json.loads(p["video"])
        except: p["video"] = None
    is_mine = u and str(u["id"]) == str(p["user_id"])
    return render_template("portfolio_detail.html", post=p, is_mine=is_mine)


@app.route("/portfolio/post/<post_id>/edit", methods=["GET", "POST"])
@login_required
def portfolio_post_edit(post_id):
    """포트폴리오 게시물 수정."""
    u = current_user()
    with db() as c:
        row = c.fetchone("SELECT * FROM portfolio_posts WHERE id = %s", (post_id,))
    if not row:
        abort(404)
    if str(row["user_id"]) != str(u["id"]) and not u.get("is_admin"):
        abort(403)
    post = dict(row)
    post["id"] = str(post["id"])

    if request.method == "POST":
        title = (request.form.get("title") or "").strip()[:200]
        category = (request.form.get("category") or "").strip()
        if not title:
            return render_template("portfolio_edit.html", post=post,
                pf_categories=PORTFOLIO_CATEGORIES, error="제목은 필수예요"), 400

        # 새 이미지 추가
        img_files = request.files.getlist("images")
        images = post.get("images") or []
        if isinstance(images, str):
            try: images = json.loads(images)
            except: images = []

        img_limit = 20 if u.get("is_admin") else PORTFOLIO_IMG_MAX_COUNT
        added_bytes = 0
        for i, f in enumerate(img_files):
            if len(images) >= img_limit:
                break
            if not f or not f.filename:
                continue
            ext = os.path.splitext(f.filename)[1].lower()
            if ext not in ALLOWED_IMG:
                continue
            f.stream.seek(0, 2)
            sz = f.stream.tell()
            f.stream.seek(0)
            img_sz_limit = 15 * 1024 * 1024 if u.get("is_admin") else PORTFOLIO_IMG_MAX_BYTES
            if sz > img_sz_limit:
                continue
            name = f"portfolio/{u['id']}/{post_id}/img-{len(images)}{ext}"
            try:
                url = storage_upload(f, name)
                images.append({"url": url, "id": str(uuid.uuid4()), "size": sz})
                added_bytes += sz
            except Exception:
                continue

        # 삭제할 이미지
        remove_ids = request.form.getlist("remove_images")
        removed_bytes = 0
        if remove_ids:
            new_images = []
            for img in images:
                if img.get("id") in remove_ids:
                    try: storage_delete(img.get("url", ""))
                    except: pass
                    removed_bytes += img.get("size", 0)
                else:
                    new_images.append(img)
            images = new_images

        net_bytes = added_bytes - removed_bytes
        with db() as c:
            c.execute(
                "UPDATE portfolio_posts SET title=%s, category=%s, images=%s, "
                "total_bytes = GREATEST(0, total_bytes + %s), updated_at=%s WHERE id=%s",
                (title, category or None, Json(images), net_bytes, int(time.time()), post_id)
            )
            if net_bytes != 0:
                c.execute(
                    "UPDATE portfolio_members SET total_bytes = GREATEST(0, total_bytes + %s) WHERE user_id = %s",
                    (net_bytes, row["user_id"])
                )
        owner_username = u["username"]
        with db() as c:
            owner = c.fetchone("SELECT username FROM users WHERE id = %s", (row["user_id"],))
            if owner:
                owner_username = owner["username"]
        return redirect(f"/portfolio/u/{owner_username}")

    if isinstance(post.get("images"), str):
        try: post["images"] = json.loads(post["images"])
        except: post["images"] = []
    return render_template("portfolio_edit.html", post=post, pf_categories=PORTFOLIO_CATEGORIES)


@app.route("/api/portfolio/reorder", methods=["POST"])
@login_required
def portfolio_reorder():
    """포트폴리오 게시물 순서 변경 API."""
    u = current_user()
    data = request.get_json(silent=True) or {}
    order = data.get("order", [])
    if not order:
        return jsonify({"ok": False}), 400
    with db() as c:
        for i, pid in enumerate(order):
            c.execute(
                "UPDATE portfolio_posts SET sort_order = %s WHERE id = %s AND user_id = %s",
                (i, pid, u["id"])
            )
    return jsonify({"ok": True})


@app.route("/portfolio/post/<post_id>/delete", methods=["POST"])
@login_required
def portfolio_post_delete(post_id):
    """포트폴리오 게시물 삭제."""
    u = current_user()
    with db() as c:
        row = c.fetchone("SELECT * FROM portfolio_posts WHERE id = %s", (post_id,))
        if not row:
            abort(404)
        if str(row["user_id"]) != str(u["id"]) and not u.get("is_admin"):
            abort(403)
        # 스토리지에서 파일 삭제
        images = row.get("images") or []
        if isinstance(images, str):
            try: images = json.loads(images)
            except: images = []
        for img in images:
            try: storage_delete(img.get("url", ""))
            except: pass
        video = row.get("video")
        if isinstance(video, str):
            try: video = json.loads(video)
            except: video = None
        if video and video.get("url"):
            try: storage_delete(video["url"])
            except: pass
        # DB 삭제 + 용량 업데이트
        freed = row.get("total_bytes", 0) or 0
        c.execute("DELETE FROM portfolio_posts WHERE id = %s", (post_id,))
        c.execute(
            "UPDATE portfolio_members SET total_bytes = GREATEST(0, total_bytes - %s) WHERE user_id = %s",
            (freed, row["user_id"])
        )
    owner_username = u["username"]
    with db() as c:
        owner = c.fetchone("SELECT username FROM users WHERE id = %s", (row["user_id"],))
        if owner:
            owner_username = owner["username"]
    return redirect(f"/portfolio/u/{owner_username}")


@app.route("/portfolio/<member_id>/approve", methods=["POST"])
@admin_required
def portfolio_approve(member_id):
    with db() as c:
        c.execute(
            "UPDATE portfolio_members SET status = 'approved', approved_at = %s WHERE id = %s",
            (int(time.time()), member_id)
        )
    return redirect("/admin")


@app.route("/portfolio/<member_id>/reject", methods=["POST"])
@admin_required
def portfolio_reject(member_id):
    with db() as c:
        c.execute("DELETE FROM portfolio_members WHERE id = %s AND status = 'pending'", (member_id,))
    return redirect("/admin")


@app.route("/portfolio/bulk", methods=["GET", "POST"])
@admin_required
def portfolio_bulk():
    """포트폴리오 일괄 업로드 (어드민 전용)."""
    u = current_user()
    # 자동 승인 확인
    mem = _portfolio_status(u["id"])
    if not mem or mem["status"] != "approved":
        # 어드민이면 자동 생성
        with db() as c:
            existing = c.fetchone("SELECT id FROM portfolio_members WHERE user_id = %s", (u["id"],))
            if not existing:
                c.execute(
                    "INSERT INTO portfolio_members (user_id, status, created_at, approved_at) "
                    "VALUES (%s, 'approved', %s, %s)",
                    (u["id"], int(time.time()), int(time.time()))
                )
        mem = _portfolio_status(u["id"])

    if request.method == "POST":
        results = []
        errors = []
        max_idx = 50
        for idx in range(max_idx):
            title = request.form.get(f"title_{idx}")
            if title is None:
                continue
            title = title.strip()[:200]
            if not title:
                continue

            img_files = request.files.getlist(f"images_{idx}")
            if not img_files or not any(f.filename for f in img_files):
                errors.append(f"#{idx}: '{title}' — 이미지 없음")
                continue

            post_id = str(uuid.uuid4())
            images_data = []
            post_bytes = 0
            skipped = []

            img_limit = 20 if u.get("is_admin") else PORTFOLIO_IMG_MAX_COUNT
            img_sz_limit = 15 * 1024 * 1024 if u.get("is_admin") else PORTFOLIO_IMG_MAX_BYTES
            for i, f in enumerate(img_files[:img_limit]):
                if not f or not f.filename:
                    continue
                ext = os.path.splitext(f.filename)[1].lower()
                if ext not in ALLOWED_IMG:
                    skipped.append(f"{f.filename}: 지원하지 않는 형식")
                    continue
                f.stream.seek(0, 2)
                sz = f.stream.tell()
                f.stream.seek(0)
                if sz > img_sz_limit:
                    skipped.append(f"{f.filename}: {sz/1024/1024:.1f}MB 초과 (최대 {img_sz_limit/1024/1024:.0f}MB)")
                    continue
                name = f"portfolio/{u['id']}/{post_id}/img-{i}{ext}"
                try:
                    url = storage_upload(f, name)
                    images_data.append({"url": url, "id": str(uuid.uuid4()), "size": sz})
                    post_bytes += sz
                except Exception as e:
                    skipped.append(f"{f.filename}: 업로드 실패 ({str(e)[:50]})")
                    continue

            if not images_data:
                errors.append(f"#{idx}: '{title}' — 저장된 이미지 0장 (건너뜀: {', '.join(skipped[:3])})")
                continue

            with db() as c:
                category = request.form.get(f"category_{idx}", "").strip()
                c.execute(
                    "INSERT INTO portfolio_posts (id, user_id, title, category, images, video, total_bytes, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (post_id, u["id"], title, category or None, Json(images_data), None, post_bytes, int(time.time()))
                )
                c.execute(
                    "UPDATE portfolio_members SET total_bytes = total_bytes + %s WHERE user_id = %s",
                    (post_bytes, u["id"])
                )
            results.append({"title": title, "count": len(images_data), "skipped": skipped})

        return render_template("portfolio_bulk.html", results=results, errors=errors, done=True)

    return render_template("portfolio_bulk.html")


# 프롬프트 번역 API — Google Translate 공개 엔드포인트 사용 (무료, 키 불필요)
@app.route("/api/seed-notice", methods=["POST"])
@admin_required
def seed_notice():
    """첫 NOTICE 게시글 시드 (1회용)."""
    u = current_user()
    with db() as c:
        existing = c.fetchone(
            "SELECT id FROM app_posts WHERE user_id = %s AND title LIKE %s",
            (u["id"], "%XAZINGA 업데이트 히스토리%")
        )
        if existing:
            return jsonify({"ok": False, "msg": "이미 존재합니다"}), 400

        content = """XAZINGA가 처음 만들어진 날부터 지금까지의 모든 업데이트를 기록합니다.

━━━━━━━━━━━━━━━━━━━━━━━━━━

v1.0 — 초기 런칭
• 갤러리(PROMPT) 페이지 오픈
• 게시물 업로드 (이미지/영상 + 프롬프트 + 모델 태그)
• 좋아요, 댓글
• 회원가입/로그인 시스템
• 마이페이지 (아바타, BIO)

v1.1 — 커뮤니티 기능
• DM (1:1 메시지)
• 차단 / 숨김 분리
• 검색 기능 (Cmd+K 모달)
• 게시물 수정/삭제
• 게시물 공개/일부공개/비공개 설정

v1.2 — APP 카테고리 & 어드민
• APP 카테고리 (어드민 승인제)
• 어드민 대시보드
• 사이트 설정 (로고, 태그라인, 히어로 이미지)

v1.3 — 캐릭터 저장소
• MY CHARACTERS (캐릭터당 30장, 계정당 60개)
• 드래그앤드롭 이미지 업로드
• 캐릭터 시트 페이지

v1.4 — 디자인 & UX 개선
• 시작 페이지 (ENTER 화면)
• Higgsfield 스타일 masonry 레이아웃
• 카드 호버 시 메타 정보 50% 투명도
• 유저 프로필 SNS 링크 (인스타, X, 유튜브, 틱톡)

v1.5 — 인증 & 번역
• Google OAuth 로그인/가입
• 프롬프트 번역 기능 (한국어/영어/중국어)
• 작업 프로세스 이미지 업로드

v1.6 — 포트폴리오 & NOTICE ← 지금!
• PORTFOLIO 카테고리 추가 (승인제)
  — 이미지 8장(장당 5MB) + 영상 1개(50MB)
  — 1인당 총 5GB 제한
• APP → NOTICE로 전환 (업데이트/공지/앱소개 통합)

━━━━━━━━━━━━━━━━━━━━━━━━━━

앞으로도 계속 업데이트됩니다."""

        c.execute(
            "INSERT INTO app_posts "
            "(user_id, title, app_name, app_url, category, thumbnail_url, "
            " content, pros, cons, rating, status, approved_at, approved_by) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),%s) RETURNING id",
            (u["id"], "XAZINGA 업데이트 히스토리 — v1.0 ~ v1.6",
             "XAZINGA", "https://www.xazinga.com", "업데이트", None,
             content,
             "• 지속적인 기능 추가\n• 미니멀 디자인 유지\n• 커뮤니티 기반 성장",
             "• 아직 알림 시스템 미구현\n• 팔로우 기능 예정",
             0, "approved", u["id"])
        )
        row = c.fetchone("SELECT 1")
    return jsonify({"ok": True, "msg": "첫 NOTICE 게시글 생성 완료!"})


@app.route("/api/seed-about", methods=["POST"])
@admin_required
def seed_about():
    """사이트 소개 공지글 시드 (1회용)."""
    u = current_user()
    with db() as c:
        existing = c.fetchone(
            "SELECT id FROM app_posts WHERE user_id = %s AND title LIKE %s",
            (u["id"], "%XAZINGA에 오신 것을 환영합니다%")
        )
        if existing:
            return jsonify({"ok": False, "msg": "이미 존재합니다"}), 400

        content = """XAZINGA는 AI로 만든 이미지와 영상을 프롬프트와 함께 아카이브하는 갤러리 커뮤니티입니다.

━━━━━━━━━━━━━━━━━━━━━━━━━━

XAZINGA는 뭐하는 곳인가요?

요즘 Midjourney, DALL-E, Stable Diffusion, Seedance, Kling 같은 AI 도구로 놀라운 이미지와 영상을 만드는 분들이 정말 많아졌습니다. 그런데 막상 만들고 나면 프롬프트는 어디에 저장하셨나요?

XAZINGA는 그 문제를 해결합니다.

작품을 올리면서 사용한 프롬프트, 네거티브 프롬프트, 모델명, 작업 과정까지 함께 기록할 수 있습니다. 나중에 다시 찾아보기도 쉽고, 다른 사람들의 프롬프트를 참고해서 새로운 영감을 얻을 수도 있어요.

━━━━━━━━━━━━━━━━━━━━━━━━━━

주요 기능

PROMPT — 메인 갤러리
AI로 만든 이미지/영상을 프롬프트와 함께 업로드할 수 있는 핵심 공간이에요. 모델 태그, 네거티브 프롬프트, 작업 과정 이미지, 참고 자료까지 한 게시물에 담을 수 있습니다. 공개/일부공개/비공개 설정도 가능해서 아직 완성되지 않은 작업도 안심하고 저장할 수 있어요.

PORTFOLIO — 크리에이터 포트폴리오
자신의 작품을 체계적으로 모아두는 공간입니다. 이미지 최대 8장(장당 5MB)과 영상 1개(50MB)를 하나의 게시물로 올릴 수 있고, 1인당 총 5GB까지 사용 가능합니다. 신청 후 승인을 받으면 사용할 수 있어요.

MY CHARACTERS — 캐릭터 저장소
AI 이미지 생성에 자주 쓰는 캐릭터의 프롬프트와 레퍼런스 이미지를 저장하는 본인 전용 공간이에요. 캐릭터당 30장, 계정당 60개까지 만들 수 있고 드래그앤드롭으로 편하게 이미지를 추가할 수 있습니다.

NOTICE — 공지 & 앱 소개
지금 보고 계신 이 공간입니다. 사이트 업데이트 내역, 공지사항, 유용한 AI 도구 소개 등을 올립니다.

━━━━━━━━━━━━━━━━━━━━━━━━━━

커뮤니티 기능

DM — 마음에 드는 크리에이터에게 1:1 메시지를 보낼 수 있어요.
좋아요 — 마음에 드는 작품에 하트를 눌러주세요.
댓글 — 작품에 대한 피드백이나 질문을 남길 수 있어요.
검색 — Cmd+K (Ctrl+K)를 누르면 어디서든 검색할 수 있습니다.
프롬프트 번역 — 한국어/영어/중국어 간 번역을 지원합니다.

━━━━━━━━━━━━━━━━━━━━━━━━━━

지원하는 AI 모델

현재 프롬프트 업로드 시 선택할 수 있는 모델은 다음과 같습니다.

Seedance · Grok · Kling · Midjourney · DALL-E 3 · Stable Diffusion · Flux · Nano Banana · Seedream · Imagen · Sora · Veo · Runway · Pika · Higgsfield · 기타 (직접 입력)

목록에 없는 모델은 '기타'를 선택하고 직접 입력하시면 됩니다.

━━━━━━━━━━━━━━━━━━━━━━━━━━

이용 안내

가입은 일반 회원가입 또는 Google 계정으로 할 수 있습니다. 가입 즉시 프롬프트 갤러리 업로드, 댓글, DM 등 대부분의 기능을 사용할 수 있어요.

포트폴리오는 신청 후 승인제로 운영됩니다. 승인 요청을 보내시면 빠르게 확인하겠습니다.

현재 XAZINGA는 초기 단계이며 지속적으로 기능을 추가하고 있습니다. 불편한 점이나 건의사항이 있으시면 DM으로 편하게 보내주세요.

━━━━━━━━━━━━━━━━━━━━━━━━━━

만든 사람: @xazinga
사이트: https://www.xazinga.com"""

        c.execute(
            "INSERT INTO app_posts "
            "(user_id, title, app_name, app_url, category, thumbnail_url, "
            " content, pros, cons, rating, status, approved_at, approved_by) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),%s) RETURNING id",
            (u["id"], "XAZINGA에 오신 것을 환영합니다 — 사이트 소개",
             "XAZINGA", "https://www.xazinga.com", "공지", None,
             content, None, None, 0, "approved", u["id"])
        )
    return jsonify({"ok": True, "msg": "사이트 소개 공지글 생성 완료!"})


@app.route("/api/seed-seedance-guide", methods=["POST"])
@admin_required
def seed_seedance_guide():
    """Seedance 2.0 프롬프트 가이드 공지글 시드."""
    u = current_user()
    with db() as c:
        existing = c.fetchone(
            "SELECT id FROM app_posts WHERE user_id = %s AND title LIKE %s",
            (u["id"], "%Seedance 2.0 영상 프롬프트 작성 가이드%")
        )
        if existing:
            return jsonify({"ok": False, "msg": "이미 존재합니다"}), 400

        content = """Seedance 2.0으로 AI 영상을 만들 때, 프롬프트를 어떻게 써야 원하는 결과가 나오는지 정리한 가이드입니다.

━━━━━━━━━━━━━━━━━━━━━━━━━━

Seedance 2.0이란?

ByteDance에서 만든 멀티모달 AI 영상 생성 모델입니다. 텍스트, 이미지, 영상, 오디오를 함께 입력해서 4초~15초 분량의 AI 영상을 만들 수 있어요. 단순히 "멋진 영상 만들어줘"가 아니라, 무엇을 보여줄지 / 카메라는 어떻게 움직일지 / 몇 초에 무슨 일이 일어날지를 구체적으로 지시해야 좋은 결과가 나옵니다.

━━━━━━━━━━━━━━━━━━━━━━━━━━

입력 제한

이미지: 최대 9장 (jpeg, png, webp, bmp, tiff, gif / 각 30MB)
영상: 최대 3개 (mp4, mov / 각 50MB, 길이 2~15초)
오디오: 최대 3개 (mp3, wav / 총 길이 15초 이하)
텍스트: 자연어 프롬프트 (제한 없음)
총 파일 수: 최대 12개 (이미지+영상+오디오 합산)

주의: 실사 사람 얼굴이 포함된 이미지/영상은 플랫폼 정책상 차단될 수 있습니다.

━━━━━━━━━━━━━━━━━━━━━━━━━━

핵심 문법: @ 레퍼런스 시스템

업로드한 자료를 @Image1, @Video1, @Audio1 같은 이름으로 부릅니다. 중요한 건 단순히 "참고해줘"가 아니라 역할을 명확히 지정하는 거예요.

좋은 예시:
@Image1's character as the main subject.
@Image2 as the first frame.
@Image3 as the last frame.
Reference @Video1's camera movement and action choreography.
BGM references @Audio1.

나쁜 예시:
Reference @Video1.
→ 카메라를 참고하란 건지, 동작인지, 효과인지 불명확합니다.

━━━━━━━━━━━━━━━━━━━━━━━━━━

프롬프트 기본 구조

1. 주인공 / 피사체 설정
2. 장소 / 배경 설정
3. 행동 / 움직임 설명
4. 카메라 움직임
5. 시간대별 연출 (타임라인)
6. 전환 / 효과
7. 사운드 / 음악 / 효과음
8. 스타일 / 분위기
9. 금지 사항

━━━━━━━━━━━━━━━━━━━━━━━━━━

10초 이상은 시간 분할이 핵심

15초 영상 구조 예시:
0~3초: 시작 상태. 주인공 등장, 카메라 천천히 push in.
3~6초: 준비 동작. 주변을 살피며 긴장감 상승.
6~10초: 핵심 사건. 빛이 터지고 대상이 움직이기 시작.
10~13초: 반응. 주인공이 놀라며 카메라가 따라감.
13~15초: 안정된 끝. 다음 장면으로 이어질 수 있는 구도.

기본 흐름: 시작 → 준비/접근 → 핵심 사건 → 결과/반응 → 안정된 끝

━━━━━━━━━━━━━━━━━━━━━━━━━━

카메라 표현 정리

기본 움직임:
slow push in — 피사체 쪽으로 천천히 접근
pull back — 카메라가 뒤로 빠짐
pan left/right — 좌우 회전
tilt up/down — 상하 회전
tracking shot — 피사체를 따라가는 카메라
orbit shot — 피사체 주변을 도는 카메라
one-take — 컷 없이 이어지는 롱테이크

고급 표현:
dolly zoom — 배경 왜곡 압박감 줌
fisheye lens — 초광각 왜곡
low angle / high angle — 앙각/부감
bird's-eye view — 완전 탑뷰
first-person POV — 1인칭 시점
whip pan — 빠른 좌우 전환
crane shot — 위아래 크게 이동

샷 크기:
extreme close-up → close-up → medium shot → full shot → wide shot → establishing shot

━━━━━━━━━━━━━━━━━━━━━━━━━━

자주 쓰는 패턴

캐릭터 일관성 유지:
@Image1's character as the main subject.
Keep the same face shape, hairstyle, outfit, body proportions throughout.

카메라 움직임 복제:
Reference @Video1's camera movement only.
Do not copy the character or background from @Video1.

영상 연장:
Extend @Video1 by 15 seconds.
0~5초: @Video1 마지막 프레임에서 이어서...

음악 비트 매칭:
Reference @Audio1's rhythm and beat structure.
Cut timing, camera movement should match the beat.

상품 광고:
@Image1 as the hero product.
0~3초: 깔끔한 스튜디오에서 천천히 회전
3~7초: 질감, 소재, 로고 클로즈업
7~11초: 라이프스타일 사용 장면
11~15초: 드라마틱 조명의 히어로 샷

━━━━━━━━━━━━━━━━━━━━━━━━━━

스타일 강화 문구 (프롬프트 끝에 추가)

실사/영화풍:
Photorealistic, cinematic quality, shallow depth of field, natural lighting, film grain, 24fps.

애니메이션:
Anime style, clean linework, cinematic lighting, expressive motion, smooth animation.

판타지:
Epic fantasy atmosphere, volumetric light, magical particles, grand scale.

네온 도시:
High saturation neon colors, wet asphalt reflections, cyberpunk atmosphere.

브이로그/POV:
First-person POV, handheld camera feeling, natural breathing, travel vlog style.

━━━━━━━━━━━━━━━━━━━━━━━━━━

자주 하는 실수

1. 레퍼런스 역할 불명확 — "Reference @Video1" 대신 구체적으로 무엇을 참고할지 적기
2. 짧은 시간에 너무 많은 행동 — 4초에 전투+대사+폭발 넣으면 불안정
3. 카메라 지시 충돌 — "Static camera + fast orbit" 같은 모순 피하기
4. 사운드 미지정 — 원하는 소리/원하지 않는 소리 명확히 적기

━━━━━━━━━━━━━━━━━━━━━━━━━━

핵심 요약

@레퍼런스는 반드시 역할을 지정한다.
15초 영상은 시간 구간으로 나눈다.
한 구간에는 하나의 핵심 행동만 넣는다.
카메라 움직임을 명확히 쓴다.
사운드까지 지시한다.
마지막에 금지사항을 넣는다.

가장 안정적인 기본 구조:
@Image1 as the first frame.
@Image2 as the last frame.
@Image3 as the character reference.
→ 0~3초: 시작 / 3~6초: 접근 / 6~10초: 핵심 / 10~13초: 반응 / 13~15초: 끝
→ Camera / Sound / Style / Constraints 순으로 마무리

━━━━━━━━━━━━━━━━━━━━━━━━━━

원본 출처: https://javaexpert.tistory.com/1763"""

        c.execute(
            "INSERT INTO app_posts "
            "(user_id, title, app_name, app_url, category, thumbnail_url, "
            " content, pros, cons, rating, status, approved_at, approved_by) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),%s) RETURNING id",
            (u["id"], "Seedance 2.0 영상 프롬프트 작성 가이드",
             "Seedance 2.0", "https://javaexpert.tistory.com/1763", "팁/가이드", None,
             content,
             "• @ 레퍼런스 역할 지정이 핵심\n• 15초 영상은 시간 구간 분할\n• 카메라/사운드까지 명확히 지시",
             "• 실사 얼굴 업로드 시 차단 가능\n• 한국어 립싱크 불안정할 수 있음",
             0, "approved", u["id"])
        )
    return jsonify({"ok": True, "msg": "Seedance 가이드 공지글 생성 완료!"})


@app.route("/api/seed-website-cloner", methods=["POST"])
@admin_required
def seed_website_cloner():
    """AI 웹사이트 클로너 오픈소스 추천 게시글 시드."""
    u = current_user()
    with db() as c:
        existing = c.fetchone(
            "SELECT id FROM app_posts WHERE user_id = %s AND title LIKE %s",
            (u["id"], "%웹사이트를 통째로 복제%"),
        )
        if existing:
            return jsonify({"ok": False, "msg": "이미 존재합니다"}), 400

        content = """URL 하나만 넣으면 그 웹사이트를 통째로 뜯어서 최신 Next.js 코드로 재구성해주는 오픈소스 템플릿이에요. AI 코딩 에이전트(Claude Code 등)와 함께 씁니다. 깃허브 별점 18,000개가 넘는 화제의 프로젝트예요.

━━━━━━━━━━━━━━━━━━━━━━━━━━

무엇을 하는 도구인가

타겟 사이트 주소를 지정하고 명령 한 줄(/clone-website)을 실행하면, AI 에이전트가 알아서:
· 사이트를 스크린샷 찍고 뜯어봄
· 폰트·색상 같은 디자인 토큰과 이미지·영상 에셋을 추출
· 각 섹션의 컴포넌트 명세서를 작성
· 여러 빌더 에이전트를 병렬로 돌려서 섹션별로 재구성
· 마지막에 원본과 비교(비주얼 diff)해서 QA

핵심은 "추측하지 않는다"는 점이에요. 각 빌더가 실제 computed CSS 값, 상태별 콘텐츠, 반응형 브레이크포인트, 에셋 경로까지 그대로 받아서 만듭니다.

━━━━━━━━━━━━━━━━━━━━━━━━━━

동작 방식 (5단계 파이프라인)

1. 정찰 — 스크린샷, 디자인 토큰 추출, 스크롤·클릭·호버·반응형 훑기
2. 기반 구축 — 폰트/색상/전역 스타일 갱신, 모든 에셋 다운로드
3. 컴포넌트 명세 — 정확한 CSS 값·상태·동작·콘텐츠를 담은 스펙 파일 작성
4. 병렬 빌드 — git worktree에서 섹션별 빌더 에이전트 동시 실행
5. 조립 & QA — worktree 병합, 페이지 연결, 원본과 시각 비교

━━━━━━━━━━━━━━━━━━━━━━━━━━

기술 스택

· Next.js 16 (App Router, React 19, TypeScript strict)
· shadcn/ui (Radix + Tailwind CSS v4)
· Tailwind CSS v4 (oklch 디자인 토큰)
· Lucide React 아이콘

지원 에이전트: Claude Code(권장), Cursor, Windsurf, Codex CLI, GitHub Copilot, Gemini CLI, Cline, Aider 등 다양하게 호환됩니다.

━━━━━━━━━━━━━━━━━━━━━━━━━━

이럴 때 유용해요

· 플랫폼 이전 — 내가 소유한 WordPress/Webflow/Squarespace 사이트를 최신 Next.js로 재구축
· 소스코드 유실 — 사이트는 살아있는데 코드가 없거나 개발자가 떠났을 때, 최신 포맷으로 코드를 되살림
· 학습 — 실제 프로덕션 사이트가 특정 레이아웃·애니메이션·반응형을 어떻게 구현했는지 진짜 코드로 뜯어보며 공부

━━━━━━━━━━━━━━━━━━━━━━━━━━

주의사항 (반드시 지키세요)

· 피싱·사칭 금지 — 남을 속이거나 사칭하는 용도, 불법 행위에 쓰면 안 됩니다.
· 남의 디자인 도용 금지 — 로고, 브랜드 에셋, 원본 문구는 그 주인의 것입니다.
· 이용약관 위반 금지 — 스크래핑·복제를 금지하는 사이트가 있으니 먼저 확인하세요.

본인이 소유했거나 학습 목적으로 쓰는 게 원칙이에요.

━━━━━━━━━━━━━━━━━━━━━━━━━━

깃허브: https://github.com/JCodesMore/ai-website-cloner-template
라이선스: MIT (무료, 상업적 이용 가능)

"Use this template" 버튼으로 본인 저장소를 만들어서 쓰는 걸 권장합니다."""

        c.execute(
            "INSERT INTO app_posts "
            "(user_id, title, app_name, app_url, category, thumbnail_url, "
            " content, pros, cons, rating, status, approved_at, approved_by) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),%s) RETURNING id",
            (u["id"], "웹사이트를 통째로 복제하는 AI 오픈소스 — AI Website Cloner",
             "AI Website Cloner Template",
             "https://github.com/JCodesMore/ai-website-cloner-template",
             "앱 소개", None,
             content,
             "• 명령 한 줄로 사이트 전체를 Next.js 코드로 재구성\n• Claude Code 등 대부분의 AI 에이전트 호환\n• 별점 18k+, MIT 라이선스로 무료\n• 디자인 토큰·에셋·반응형까지 정밀 추출",
             "• 남의 사이트 무단 복제는 저작권·약관 위반 위험\n• Node.js 24+ 및 AI 코딩 에이전트 필요\n• 완벽 복제가 아니라 이후 수정 보완 필요",
             0, "approved", u["id"])
        )
    return jsonify({"ok": True, "msg": "AI 웹사이트 클로너 추천 게시글 생성 완료!"})


@app.route("/api/seed-skills", methods=["POST"])
@admin_required
def seed_skills():
    """스킬 3개 일괄 시드."""
    u = current_user()
    skills_data = [
        {
            "name": "Seedance 디렉터",
            "category": "영상 제작",
            "description": "장면 설명을 Seedance 2.0에 최적화된 영어+중국어 이중 언어 영상 프롬프트로 변환. 액션/일반/대화 장면 모두 지원.",
            "content": """Seedance 2.0 유니버셜 디렉터

이 스킬은 "한국어로 장면을 설명하면 Seedance가 이해하는 형태로 바꿔주는 번역기"입니다. 단순 번역이 아니라, Seedance 엔진이 잘 렌더링하는 구조로 재구성해 줍니다.

━━━ 왜 이 스킬이 필요한가? ━━━

Seedance 2.0은 영어와 중국어 프롬프트에서 가장 좋은 결과를 냅니다. 한국어로 "멋진 액션 장면 만들어줘"라고 쓰면 모호한 결과가 나오지만, 이 스킬이 변환한 구조화된 EN+ZH 프롬프트를 넣으면 카메라 움직임, 조명, 동작 타이밍까지 의도대로 나올 확률이 크게 올라갑니다.

중국어를 따로 넣는 이유는 Seedance가 ByteDance 제품이라 중국어 학습 데이터가 풍부하기 때문입니다. 같은 장면이라도 ZH 프롬프트가 더 정확한 결과를 내는 경우가 많습니다.

━━━ 장면 유형별 아키타입 ━━━

장면 유형에 따라 카메라와 공간 설계가 자동으로 달라집니다.

[액션 장면]

추격(Pursuit): 쫓기는 자가 프레임 앞, 쫓는 자가 뒤. 길이 좁아지거나 넓어지면서 긴장감 조절.
→ 왜? 시청자가 거리가 좁혀지는 느낌을 직관적으로 느끼려면 프레임 안 위치가 중요합니다.

결투(Duel): 카메라가 우세한 쪽에 낮게 위치. 우세가 반드시 교차.
→ 신경 쓸 점: 한쪽만 이기면 반복적 동작만 렌더링됨. 우세 교차가 시각적 다양성을 만듭니다.

충격(Impact): 빌드업 느리게 → 충돌 빠르게 → 여파 다시 느리게.
→ 왜? 속도 대비가 있어야 충격의 무게감이 살아납니다.

[일반 장면]

여정(Journey): 피사체가 공간을 이동. 배경이 바뀌면서 시간 흐름 표현.
분위기(Atmosphere): 아무것도 안 변함. 무드 자체가 콘텐츠. 느린 push-in 또는 정적 홀드.
→ 왜? 변화 없는 장면은 카메라를 최소화해야 분위기가 유지됩니다. 카메라가 바쁘면 분위기가 깨집니다.
공개(Reveal): 안 보이던 것이 보임. 문이 열리거나, 안개가 걷히거나.

[대화 장면]

대립(Confrontation): 둘 다 밀어붙임. 파워가 교차할 때 카메라가 축을 넘김.
심문(Interrogation): 비대칭. 질문자에 로우앵글, 침묵에 push-in.
→ 대사 한계: 15초에 25-30단어. 넘으면 핵심만 남기고 나머지는 신체 행동으로.
협상(Negotiation): 균형. 대칭 프레이밍, 동일한 샷 사이즈.

━━━ Seedance 엔진 규칙 — 어기면 깨진 영상 ━━━

1. 액션은 의도 + 기술명으로만
   ✅ "회전 돌려차기가 명중한다"
   ❌ "왼팔이 45도 회전하며 훅을 손목 높이에서 막는다"
   → Seedance는 관절 단위 묘사를 해석 못 합니다.

2. 프레임 밖 = 존재하지 않음
   화면 밖 캐릭터를 언급하면 유령 같은 형태가 렌더링됩니다.

3. 반사(거울, 물웅덩이, 칼날) 금지
   반사 장면은 공간 구조를 깨뜨립니다.

4. 감정 레이블 대신 물리 현상
   ❌ "분노한 표정" → ✅ "턱이 조이고, 콧구멍이 벌어진다"
   → "분노"는 추상적, 근육 움직임은 구체적이라 일관된 결과.

5. 한 샷에 퇴장 + 재입장 금지
   프레임 벗어나면 그 샷에서 끝. 다시 나오려면 컷 전환.

6. 동시 추적 캐릭터 3명 이하
   4명 이상이면 캐릭터가 합쳐지거나 사라집니다.

━━━ 출력 형식 ━━━

[{{"lang":"en","prompt":"Style & Mood: ... Dynamic Description: ... Static Description: ..."}},
 {{"lang":"zh","prompt":"风格与氛围: ... 动态描述: ... 静态描述: ..."}}]

ZH는 번역이 아닌 네이티브 중국어 재작성 (1,800자 이하)
레퍼런스 이미지 사용 시 <<<image_n>>> 범례 포함

━━━ 카메라 표현 사전 ━━━

앵글: low-angle/仰拍, high-angle/俯拍, dutch angle/荷兰角, bird's-eye/鸟瞰, OTS/过肩
렌즈: wide 14-24mm/广角, standard 35-50mm/标准, telephoto 85-200mm/长焦
무브: tracking/跟拍, dolly-in/推镜头, dolly-out/拉镜头, crane/摇臂, orbit/环绕, handheld/手持
시간: slow-motion/升格, speed ramp/变速, freeze frame/定格
전환: smash cut/硬切, match cut/匹配剪辑, whip-pan transition/甩镜转场"""
        },
        {
            "name": "비디오 프롬프트 빌더",
            "category": "영상 제작",
            "description": "크리에이티브 브리프 하나로 4섹션 구조의 프로급 영상 프롬프트를 생성. 이펙트 타임라인, 인벤토리, 밀도 맵, 에너지 아크 포함.",
            "content": """비디오 프롬프트 빌더 (Seedance 2.0)

"나이키 느낌의 러닝 광고, 산, 15초" 같은 한 줄 브리프만 주면 프로 에디터가 작성한 것 같은 상세한 샷별 프롬프트를 만들어 줍니다.

━━━ 왜 이 스킬이 필요한가? ━━━

AI 영상 생성에서 가장 흔한 실수는 "분위기만 전달하고 구체적인 시각 설계를 안 하는 것"입니다. "영화같은 러닝 광고"라고만 쓰면 카메라가 랜덤, 이펙트 없이 밋밋, 에너지가 일정해서 지루합니다.

이 스킬은 매 초마다 무슨 이펙트가, 어떤 카메라로, 어떤 속도로 일어나는지를 전부 설계해서 전달합니다.

━━━ 출력 4섹션 — 각각 왜 필요한지 ━━━

[1. SHOT-BY-SHOT EFFECTS TIMELINE] — 핵심

SHOT 1 (0:00-0:03) — 러너의 발이 지면을 찍는 순간
EFFECT: 속도 램프 (감속 → 가속) + 얕은 DOF
발이 젖은 흙을 밟는 순간 80%에서 20% 속도로 급감속. 진흙 입자가 슬로모션으로 튀어오른다. 0.5초 후 100% 속도 복귀.
카메라: 지면 5cm 초접사, 15mm 광각, 미세 핸드헬드
전환: 모션 블러 스미어로 다음 샷 진입

→ 왜 이렇게 상세하게? Seedance는 "슬로모션 러닝" 같은 모호한 지시보다 "80%에서 20% 감속, 진흙 입자 슬로모션" 같은 수치에 훨씬 정확하게 반응합니다.

→ 신경 쓸 점:
한 샷은 1-4초. 너무 길면 중간을 임의로 채움
이펙트가 겹칠 때 전부 나열
전환 방식 반드시 명시. 안 쓰면 하드컷 처리

[2. MASTER EFFECTS INVENTORY] — 밸런스 체크

전체 이펙트를 한눈에 정리. 같은 이펙트 3회 이상 반복되면 하나를 교체하는 게 좋습니다. 너무 다양해도 산만해지고, 너무 반복돼도 단조로워집니다.

예: 속도 램프 — 3회 (Shot 1, 5, 9) — 임팩트 순간 강조
    달리 인 — 2회 (Shot 3, 7) — 감정적 절정 접근

[3. EFFECTS DENSITY MAP] — 가장 중요한 인사이트

타임라인 구간별 이펙트 밀도:
0:00-0:04 = HIGH (4 effects) | 0:04-0:08 = LOW (1 effect) | 0:08-0:12 = HIGH (3 effects)

→ 왜 중요한가? HIGH → HIGH → HIGH = 시청자 피로. LOW → LOW → LOW = 지루.
HIGH ↔ LOW 교차가 리듬감을 만들고 하이라이트를 돋보이게 합니다.
프로 뮤직비디오나 광고를 분석해보면 거의 모든 영상이 이 패턴을 따릅니다.

[4. ENERGY ARC] — 전체 흐름

Act 1 (0-5초): 훅. 첫 2초에 스크롤을 멈추지 않으면 끝.
Act 2 (5-11초): 시그니처 비주얼 + 브랜드 노출. 가장 임팩트 있는 샷.
Act 3 (11-15초): 착지. 갑자기 끝나면 안 됨. 의도적으로 멈추는 느낌 필요.

이펙트를 아무리 멋지게 써도 전체 흐름이 없으면 "대단한데 뭔지 모르겠다"가 됩니다.

━━━ 핵심 크리에이티브 원칙 ━━━

1. 대비가 임팩트를 만든다 — 슬로모션 후 속도 램프가 2배 강력. 연속 속도 램프는 효과 반감.
2. 시그니처 모먼트 필수 — "이 영상 하면 떠오르는 그 장면" 1개. 없으면 기억에 안 남음.
3. 전환은 무대다 — 위프 팬, 블룸 플래시, 모션 블러 스미어 = 퍼포먼스.
4. 구체성 > 모호함 — "약 20-25% 속도" > "슬로모션". "프레임 시계방향 15-20도" > "카메라 틸트".
5. 에너지는 반드시 해소 — 이펙트 예산이 떨어진 것처럼 보이면 안 됨.

━━━ 길이별 설계 ━━━

5-10초: 4-7샷. 숏폼(릴스, 틱톡). 첫 샷이 곧 훅.
10-20초: 8-14샷. 인스타 광고, 유튜브 프리롤.
20-30초: 12-20샷. 브랜드 필름, TV CF.
기본값: 15-20초."""
        },
        {
            "name": "Seedance 프롬프팅 가이드",
            "category": "영상 제작",
            "description": "Seedance 2.0 프롬프트의 5블록 구조, @소스 바인딩, 타임라인 분할, 카메라 키워드 총정리. 실전 예시와 자주 하는 실수 포함.",
            "content": """Seedance 2.0 프롬프팅 가이드

처음 쓰는 분도, 이미 쓰고 있는 분도 "왜 결과가 의도와 다른지" 이해하고 개선할 수 있는 가이드입니다.

━━━ 핵심 3원칙 — 이것만 지켜도 결과가 달라집니다 ━━━

[원칙 1] 모든 소재에는 역할이 있어야 한다

이미지 5장을 올렸는데 "참고해줘"만 쓰면?
→ Seedance가 랜덤으로 1-2장만 반영하고 나머지 무시.

반드시 각 소재에 구체적 역할 지정:
@이미지1을 첫 번째 프레임으로.
@이미지2를 마지막 프레임으로.
@이미지3의 캐릭터를 주인공으로.
@비디오1의 카메라 움직임만 참조.
@오디오1의 리듬에 맞춰 컷 전환.

→ 왜? 각 입력이 영상의 어느 부분에 영향을 줘야 하는지 명시하지 않으면 모델이 추측합니다. 추측 = 의도와 다른 결과.

→ 특히 주의: "@비디오1 참조"만 쓰면 카메라인지, 동작인지, 색감인지, 리듬인지 불명확. "카메라 움직임만" 처럼 범위를 좁혀야 합니다.

[원칙 2] 감정은 이름이 아니라 물리 현상으로 쓴다

❌ "슬프다", "긴장감 있다", "행복한 표정"
✅ "눈물이 뺨을 따라 흐르고, 입가가 미세하게 떨린다"
✅ "손가락이 테이블 모서리를 반복적으로 두드린다"

→ 왜? "슬프다"는 100가지 표현이 가능한 추상어. Seedance는 물리적 묘사에서 훨씬 일관되고 사실적인 결과를 냅니다. 이건 모든 AI 생성 도구의 공통 원칙입니다.

[원칙 3] 10초 이상이면 타임라인을 3초 단위로 쪼갠다

❌ "15초 동안 여자가 숲을 걸으며 슬퍼한다"
✅ 0-3초: 숲 입구, 천천히 걷기. 카메라 와이드.
   3-6초: 나뭇가지를 손으로 만지며 멈춤. 카메라 미디엄.
   6-10초: 하늘을 올려다봄. 빛이 나뭇잎 사이로. 클로즈업.
   10-13초: 눈을 감고 심호흡. 바람에 머리카락.
   13-15초: 다시 걷기. 카메라 뒤에서 따라감.

→ 왜? 분할 안 하면 Seedance가 0-5초만 신경 쓰고 6초 이후는 알아서 채움. 이게 "뒷부분이 이상해요" 문제의 원인.
→ 한 구간에 핵심 동작 하나만! "달리면서 뒤돌아보고 넘어지고 일어나서 외친다" = 4개를 3초에? 전부 뭉개짐.

━━━ 5블록 프롬프트 구조 ━━━

어떤 영상이든 이 5블록을 순서대로 채우면 됩니다. 하나라도 빠지면 빈 부분을 랜덤으로 채웁니다.

① 소재 바인딩 — 뭘 참고할지
→ 이게 없으면 업로드한 이미지가 무시될 수 있음

② 피사체 + 공간 — 누가 어디에
→ "예쁜 여자" 같은 건 의미 없음. 구체적 외모와 배경 필수

③ 타임라인 동작 — 언제 뭘 하는지
→ 가장 중요한 블록. 이게 없으면 정적인 영상

④ 카메라 + 오디오 — 어떻게 찍고 어떤 소리
→ 카메라 안 쓰면 랜덤 앵글. 사운드 안 쓰면 무음 또는 노이즈

⑤ 스타일 + 제약 — 어떤 느낌, 뭘 하지 말지
→ 제약이 중요. "얼굴 변형 없음, 손가락 왜곡 없음" 같은 네거티브 지시가 안정성을 크게 높임

━━━ 자주 하는 실수 TOP 5 ━━━

1. 레퍼런스 역할 불명확 (가장 흔함)
   ❌ "Reference @Video1."
   ✅ "Reference @Video1's camera movement only."

2. 짧은 시간에 너무 많은 행동
   4초에 5개 동작 = 전부 깨짐
   → 4-5초 = 핵심 1개. 15초 = 5개 구간 x 1개씩.

3. 카메라 지시 충돌
   ❌ "정적 카메라 + 빠른 오빗 + 핸드헬드" (모순)
   ✅ "0-3초: 정적. 3-8초: 느린 push-in." 시간별 분리.

4. 사운드 미지정
   원하지 않는 소리도 명시:
   ✅ "Sound: 발자국, 바람. No background music, no narration."

5. 실사 얼굴 업로드
   정책상 차단될 수 있음. 캐릭터 시트나 비실사 스타일 권장.

━━━ 입력 제한 ━━━

이미지: 최대 9장 (각 30MB)
영상: 최대 3개 (각 50MB, 2-15초)
오디오: 최대 3개 (총 15초 이하)
총 파일: 최대 12개 / 출력: 4-15초

━━━ 실전 패턴 모음 ━━━

캐릭터 일관성:
Keep same face, hairstyle, outfit, body proportions throughout.
No face morphing, no extra characters.

카메라 복제:
Reference @Video1's camera movement only.
Do not copy characters or background.

영상 연장:
Extend @Video1 by 15 seconds.
0-5초: Continue from final frame...

비트 매칭:
Reference @Audio1's rhythm.
Cut timing matches the beat.
Stronger motion on downbeats.

상품 광고:
@Image1 as hero product.
0-3초: 스튜디오 회전 / 3-7초: 질감 클로즈업 / 7-11초: 라이프스타일 / 11-15초: 히어로 샷

스타일 문구:
실사: Photorealistic, cinematic, shallow DOF, film grain, 24fps.
애니: Anime style, clean linework, smooth animation.
판타지: Epic fantasy, volumetric light, magical particles.
네온: Neon colors, wet asphalt reflections, cyberpunk.
브이로그: First-person POV, handheld, travel vlog style."""
        }
    ]

    created = 0
    try:
        with db() as c:
            for sk in skills_data:
                existing = c.fetchone(
                    "SELECT id FROM skills WHERE user_id = %s AND name = %s",
                    (u["id"], sk["name"])
                )
                if existing:
                    continue
                c.execute(
                    "INSERT INTO skills (user_id, name, description, content, category, created_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s)",
                    (u["id"], sk["name"], sk["description"], sk["content"],
                     sk["category"], int(time.time()))
                )
                created += 1
        return jsonify({"ok": True, "msg": f"스킬 {created}개 생성 완료!"})
    except Exception as e:
        import traceback
        return jsonify({"ok": False, "error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/api/seed-threads-guide", methods=["POST"])
@admin_required
def seed_threads_guide():
    """스레드 AI 워크플로우 가이드 공지글 시드."""
    u = current_user()
    with db() as c:
        existing = c.fetchone(
            "SELECT id FROM app_posts WHERE user_id = %s AND title LIKE %s",
            (u["id"], "%스레드 떡상하게 만드는 AI 워크플로우%")
        )
        if existing:
            return jsonify({"ok": False, "msg": "이미 존재합니다"}), 400

        content = """뉴스레터나 블로그 글 하나를 스레드 감성의 포스트 10개로 자동 변환하는 실전 AI 시스템 가이드입니다.

━━━━━━━━━━━━━━━━━━━━━━━━━━

왜 지금 스레드인가

스레드는 인스타그램 인프라 위에서 텍스트 중심 소통을 표방하는 플랫폼입니다. 2023년 출시 이후 가장 빠르게 성장한 SNS 중 하나이고, 2026년 현재 월간 활성 사용자가 4억 명을 넘었습니다.

핵심은 이겁니다: 스레드는 아직 알고리즘이 공정합니다.

팔로워 수보다 콘텐츠 자체의 공명이 도달 범위를 결정합니다. 유튜브나 인스타처럼 초기 영향력이 없어도, 좋은 글 하나가 수만 명에게 닿을 수 있습니다. 지금이 골든타임인 이유죠.

텍스트 기반 저진입장벽 — 영상이나 이미지 없이도 팔로워를 쌓을 수 있다
팔로워보다 공명 우선 — 초보자에게 공평한 경쟁 환경
브랜딩의 최적지 — 사색과 통찰이 환영받는 공간

━━━━━━━━━━━━━━━━━━━━━━━━━━

콘텐츠 리퍼포징이란

리퍼포징(Repurposing)은 이미 만든 콘텐츠를 다른 형식이나 플랫폼에 맞게 재가공하는 전략입니다.

블로그 글 하나 → 유튜브 영상으로 → 영상 자막을 뉴스레터로 → 뉴스레터를 스레드 포스트로

이 사이클을 반복하면 같은 노력으로 10배의 노출을 만들 수 있습니다.

크리에이터의 가장 큰 병목은 아이디어가 아니라 실행입니다. 매일 새 콘텐츠를 만드는 건 불가능에 가깝지만, 잘 쓴 글 하나를 여러 플랫폼에 맞게 재해석하는 것은 AI의 도움으로 충분히 가능합니다.

워크플로우:
INPUT: 긴 뉴스레터/블로그 글 (500~3000자)
PROCESS: AI가 핵심 인사이트 추출 + 스레드 감성 변환
OUTPUT: 스레드용 포스트 10개 (각 300~500자)
ACTION: 매일 1개씩 예약 발행 → 10일치 콘텐츠 확보

━━━━━━━━━━━━━━━━━━━━━━━━━━

줄글 감성의 해부

스레드에는 뚜렷한 문화적 문체가 있습니다. "줄글 감성"이라 불리는 이 스타일은 단순히 짧은 문장이 아닙니다. 호흡, 여백, 공감의 배치가 만들어내는 리듬입니다.

줄글 감성의 5가지 특징:

1. 짧은 문장 — 한 문장이 하나의 생각. 마침표 뒤에 호흡한다.
2. 여백의 활용 — 빈 줄이 없으면 스레드 감성이 아니다. 여백이 리듬을 만든다.
3. 설명 대신 느낌 — 독자에게 설명하지 않는다. 느끼게 한다. "왜냐하면"은 쓰지 않는다.
4. 공감 우선 — 독자가 "이게 나 얘기잖아"라고 생각하게 만든다.
5. 열린 결말 — 모든 것을 설명하지 않는다. 여지를 남긴다.

나쁜 예시 (설명체):
"브랜딩은 매우 중요한데, 그 이유는 사람들이 당신을 기억하는 방식이 결국 당신의 비즈니스 성과에 직결되기 때문입니다."

좋은 예시 (줄글 감성):
"브랜딩은 결국 기억이다.
사람들이 당신을 떠올릴 때
어떤 단어가 먼저 오는지—
그게 전부다."

체크리스트:
□ 한 문장이 2줄을 넘지 않는가?
□ 빈 줄이 3문장에 1번 이상 있는가?
□ "왜냐하면", "따라서", "그러므로"를 안 썼는가?
□ "설명"이 아니라 "느낌"이 먼저 오는가?
□ 마지막 문장이 여운을 남기는가?

━━━━━━━━━━━━━━━━━━━━━━━━━━

AI 프롬프트 설계 원리

AI에게 "요약해줘"라고 하면 스레드 감성이 나오지 않습니다. 4개 레이어로 설계해야 합니다.

Layer 1 — 페르소나 설정: AI가 "스레드 전문 작가" 역할을 갖게 한다
Layer 2 — 스타일 가이드: 줄글 감성 규칙을 구체적으로 나열 + 예시 포스트 제공
Layer 3 — 제약 조건: 포스트 수(10개), 글자 수(300~500자), 독립성(원본 몰라도 이해 가능)
Layer 4 — 출력 형식: [POST_1]~[POST_10] 파싱 가능한 구조

실제 프롬프트:

"당신은 스레드(Threads) 콘텐츠 전문 작가입니다.
아래 원본 글을 분석해 포스트 10개를 만들어주세요.

## 스타일 가이드: 사색형 줄글
- 문장은 짧게. 마침표로 호흡 조절.
- 설명하지 말고, 느끼게 한다.
- 여백(빈 줄)을 적극 활용.
- 해시태그 없이, 또는 1개만.

## 규칙
1. 각 포스트는 300~500자 이내
2. 원본을 몰라도 이해 가능해야 함
3. [POST_1] ~ [POST_10] 형식으로 출력

## 원본 글
{여기에 원본 글 붙여넣기}"

━━━━━━━━━━━━━━━━━━━━━━━━━━

4주 실행 로드맵

Week 1 — 콘텐츠 씨앗 뿌리기
기존 글 5개를 AI에 넣어 포스트 50개 생성. 30개 선별해서 한 달치 발행 큐. 매일 1~2개 일정한 시간에 발행.

Week 2 — 반응 패턴 분석
어떤 포스트에 좋아요/댓글/리포스트가 많은지 기록. 잘 되는 주제와 문체의 공통점 찾기. 반응 좋은 스타일을 프롬프트 예시로 추가.

Week 3 — 프롬프트 고도화
톤 변형 시도 (사색형 → 인사이트형 → 스토리형). 댓글 피드백을 다음 포스트 주제로 활용.

Week 4 — 시스템화
워크플로우 자체를 콘텐츠로 공유 (과정 공유 = 신뢰 구축). 주간 루틴 확정: 월요일 원본 선정 → 화요일 AI 생성 → 수~일 발행.

━━━━━━━━━━━━━━━━━━━━━━━━━━

도구 비교

ChatGPT (Plus $20/월) — 설치 불필요, 바로 사용 가능, 파일 업로드 가능
Claude (Pro $20/월) — 긴 글 처리에 강함, 한국어 품질 우수
OpenAI API + HTML 툴 — 자동화 가능, 글 1개 처리 비용 약 $0.01~0.03
Google Gemini — 무료로 시작 가능, 출력 품질 편차 있음

입문자는 ChatGPT 또는 Claude 대화창에서 프롬프트를 복사-붙여넣기하는 것으로 시작하세요.

━━━━━━━━━━━━━━━━━━━━━━━━━━

출처: 박현서, 「스레드 떡상하게 만드는 AI 워크플로우」 (2026)"""

        c.execute(
            "INSERT INTO app_posts "
            "(user_id, title, app_name, app_url, category, thumbnail_url, "
            " content, pros, cons, rating, status, approved_at, approved_by) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),%s) RETURNING id",
            (u["id"], "스레드 떡상하게 만드는 AI 워크플로우 — 콘텐츠 리퍼포징 가이드",
             "Threads", None, "팁/가이드", None,
             content,
             "• 글 1개 → 스레드 포스트 10개 자동 변환\n• 줄글 감성 5가지 특징 + 체크리스트\n• 4주 실행 로드맵 포함",
             "• 자동화하려면 API 설정 필요\n• 스레드 알고리즘은 계속 변할 수 있음",
             0, "approved", u["id"])
        )
    return jsonify({"ok": True, "msg": "스레드 가이드 공지글 생성 완료!"})


@app.route("/api/seed-seedance-recipes", methods=["POST"])
@admin_required
def seed_seedance_recipes():
    """Seedance 2.0 실전 레시피북 공지글 시드."""
    u = current_user()
    with db() as c:
        existing = c.fetchone(
            "SELECT id FROM app_posts WHERE user_id = %s AND title LIKE %s",
            (u["id"], "%Seedance 2.0 실전 프롬프트 레시피북%")
        )
        if existing:
            return jsonify({"ok": False, "msg": "이미 존재합니다"}), 400

        content = """Seedance 2.0으로 실제 영상을 만들 때 바로 쓸 수 있는 프롬프트 레시피 모음입니다. 카테고리별로 정리했으니 필요한 상황에 맞는 레시피를 찾아서 응용하세요.

━━━━━━━━━━━━━━━━━━━━━━━━━━

레시피 1: 이미지 → 캐릭터 애니메이션

사진 한 장에서 캐릭터가 움직이는 영상을 만드는 가장 기본적인 패턴입니다.

프롬프트 구조:
@이미지1의 캐릭터를 주인공으로 사용.
[캐릭터가 하는 동작을 구체적으로 서술].
[카메라 움직임].
[배경/환경 묘사].

실전 예시:
@이미지1의 남자가 퇴근 후 피곤한 표정으로 복도를 걷다가 속도가 느려지며 집 문 앞에 멈춰 선다. 얼굴 클로즈업. 심호흡을 하고 문을 연다. 카메라는 천천히 push-in.

핵심 포인트:
- @이미지1의 캐릭터 외모(얼굴, 옷, 체형)가 영상에서 유지됨
- 동작은 시간순으로 나열하면 자연스러움
- 표정 변화를 물리적으로 묘사 ("심호흡", "눈을 감는다" 등)

━━━━━━━━━━━━━━━━━━━━━━━━━━

레시피 2: 비디오 카메라 워크 복제

기존 영상의 카메라 움직임만 빌려서 완전히 다른 장면을 만드는 패턴입니다.

프롬프트 구조:
@비디오1의 카메라 움직임과 전환 효과를 참조.
@비디오1의 캐릭터나 배경은 복사하지 마세요.
[새로운 캐릭터/장면 설명].

실전 예시:
@비디오1의 소녀를 오페라 배우로 교체. 장면은 화려한 무대. @비디오1의 카메라 움직임과 전환 효과를 참조하고, 캐릭터의 움직임에 맞춰 렌즈를 조정.

핵심 포인트:
- 반드시 "카메라 움직임만"이라고 명시해야 함
- 캐릭터/배경을 복사하지 말라는 네거티브 지시 필수
- 카메라 리듬 + 전환 타이밍이 그대로 적용됨

━━━━━━━━━━━━━━━━━━━━━━━━━━

레시피 3: 캐릭터 교체 (비디오 속 인물 바꾸기)

기존 영상의 동작과 연기는 유지하면서 캐릭터만 바꾸는 패턴입니다.

프롬프트 구조:
@비디오1의 캐릭터를 @이미지1로 교체.
첫 번째 프레임을 @이미지1로 설정.
@비디오1의 움직임과 표정을 완전히 참조.

실전 예시:
@비디오1의 여성 리드 싱어를 @이미지1의 남성 싱어로 교체. 동작은 원본 영상에서 완전히 모방. 카메라 컷 없이 원샷. 밴드가 음악을 연주하는 장면.

핵심 포인트:
- "완전히 참조"라는 표현이 동작 복제의 핵심
- 첫 프레임을 이미지로 고정하면 캐릭터 일관성 향상
- 원샷 지정하면 중간에 캐릭터가 바뀌는 문제 방지

━━━━━━━━━━━━━━━━━━━━━━━━━━

레시피 4: 상품 광고 영상

제품 이미지에서 상업 광고 수준의 영상을 만드는 패턴입니다.

프롬프트 구조:
@이미지1의 [제품명]을 히어로 제품으로 사용.
@이미지2에서 제품 측면 참조. @이미지3에서 표면 질감 참조.
제품의 모든 디테일이 표시되어야 하며, [카메라/조명/배경 설명].

실전 예시 (가방 광고):
@이미지2 가방의 상업 사진 전시. 가방 측면은 @이미지1, 표면 재질은 @이미지3을 참조. 제품의 모든 디테일이 표시되어야 하며, 카메라가 천천히 회전하면서 가방의 각도를 보여줌.

실전 예시 (자동차 광고):
@비디오1의 카메라 움직임과 화면 전환 리듬을 참고. @이미지1의 빨간색 슈퍼카를 재현. 다이나믹한 주행 장면.

핵심 포인트:
- 여러 이미지로 제품의 다양한 각도/질감을 참조시키면 정확도 향상
- 광고는 조명 묘사가 특히 중요 ("스튜디오 조명", "드라마틱 역광" 등)
- 제품이 화면 중앙에 오도록 명시

━━━━━━━━━━━━━━━━━━━━━━━━━━

레시피 5: 멀티 이미지 합성 (공간 구성)

여러 이미지를 조합해서 하나의 공간을 만드는 패턴입니다.

프롬프트 구조:
@이미지1을 첫 번째 프레임 (1인칭 관점).
@비디오1의 카메라 이동 효과를 참조.
위쪽 장면은 @이미지2, 왼쪽은 @이미지3, 오른쪽은 @이미지4.

실전 예시 (VR 공간):
@이미지1을 첫 프레임, 1인칭 관점으로. @비디오1의 카메라 이동 효과를 참조. 위 장면은 @이미지2, 왼쪽은 @이미지3, 오른쪽은 @이미지4. 엘리베이터 안에서 주변을 둘러보는 느낌.

핵심 포인트:
- 방향을 명확히 (위/아래/왼쪽/오른쪽)
- 1인칭 관점 지정하면 몰입감 있는 공간 구성
- 이미지당 역할이 명확해야 함 (배경/캐릭터/소품 분리)

━━━━━━━━━━━━━━━━━━━━━━━━━━

레시피 6: 영상 연장 (앞으로/뒤로)

기존 영상을 앞이나 뒤로 확장하는 패턴입니다.

프롬프트 구조 (뒤로 연장):
@비디오1을 15초 연장.
0-5초: [마지막 프레임에서 이어지는 장면].
5-10초: [다음 전개].
10-15초: [마무리].

프롬프트 구조 (앞으로 연장):
10초를 앞으로 연장.
[현재 영상 시작 전에 일어나는 장면을 시간순으로 서술].

실전 예시:
@비디오1을 15초 연장. 0-5초: 빛과 그림자가 블라인드를 뚫고 테이블 위로 천천히 미끄러짐. 나뭇가지가 살짝 흔들림. 5-10초: 컵에서 김이 올라옴. 카메라 천천히 push-in. 10-15초: 손이 들어와서 컵을 집어 듦.

핵심 포인트:
- 시간 구간별로 반드시 나눠야 함
- 마지막/첫 프레임과의 연결이 자연스러워야 함
- "앞으로 연장"은 프리퀄 느낌으로 작성

━━━━━━━━━━━━━━━━━━━━━━━━━━

레시피 7: 스타일 전환

같은 동작을 완전히 다른 비주얼 스타일로 바꾸는 패턴입니다.

프롬프트 구조:
[스타일] 스타일. @이미지1의 캐릭터가 @비디오1의 동작을 참조하여 [장면 설명].

실전 예시 (수묵화 스타일):
흑백 잉크 스타일. @이미지1의 캐릭터는 @비디오1의 특수 효과와 동작을 참조하여 잉크 태극권 쿵푸 장면을 연출.

사용 가능한 스타일 키워드:
- 수묵화: 흑백 잉크 스타일, 수묵 질감
- 애니메이션: 애니메이션 스타일, 깨끗한 선화, 부드러운 모션
- 클레이: 클레이모션 스타일, 스톱모션 질감
- 레트로: 80년대 VHS 필름 질감, 노이즈 그레인
- 네온: 네온 컬러, 사이버펑크 분위기
- 미니어처: 틸트시프트 렌즈, 미니어처 스케일

━━━━━━━━━━━━━━━━━━━━━━━━━━

레시피 8: 스토리보드/만화 → 영상

그림이나 만화 컷을 영상으로 변환하는 패턴입니다.

프롬프트 구조:
@이미지1을 왼쪽에서 오른쪽, 위에서 아래 순서로 해석.
캐릭터 대사를 그림과 일치하게 유지.
스토리보드 전환과 주요 장면 해석에 특수 효과 적용.

실전 예시 (긴 스크롤 스토리보드):
@이미지1의 스토리보드 대본을 참고. 스토리보드의 풍경, 움직임, 그래픽, 카피를 참고하여 15초 힐링 영상 제작. 각 컷의 전환은 부드러운 디졸브.

핵심 포인트:
- 읽는 순서를 명시 (좌→우, 상→하)
- 대사가 있으면 립싱크와 일치하도록 언급
- 각 컷 사이의 전환 방식 지정

━━━━━━━━━━━━━━━━━━━━━━━━━━

레시피 9: 액션/전투 장면

두 캐릭터 이상이 격투하는 장면을 만드는 패턴입니다.

프롬프트 구조:
@이미지1, @이미지2의 캐릭터 참조.
@비디오1의 행동을 모방하여 [장소]에서 전투.
[카메라/특수효과 설명].

실전 예시:
@이미지1(창 캐릭터)과 @이미지2(쌍검 캐릭터)가 @비디오1의 행동을 모방하여 @이미지5의 단풍잎 숲에서 전투. 카메라는 두 캐릭터를 따라가며, 검이 부딪힐 때 불꽃 이펙트.

실전 예시 (특수효과 전투):
@비디오1의 캐릭터 움직임과 @비디오2의 카메라 언어를 참조. @이미지1과 @이미지2의 싸움 장면. 별이 빛나는 밤. 싸우는 동안 에너지 파동이 퍼짐.

핵심 포인트:
- 캐릭터 3명 이하로 유지
- 비디오에서 동작을, 이미지에서 외모를 분리 참조
- 특수효과(불꽃, 에너지 파동 등)는 별도로 명시

━━━━━━━━━━━━━━━━━━━━━━━━━━

레시피 10: 대사/립싱크 장면

캐릭터가 실제로 말하는 장면을 만드는 패턴입니다.

프롬프트 구조:
[캐릭터 설명]. [환경 설명].
[캐릭터]가 [언어]로 "[대사 내용]"이라고 말한다.
[카메라/표정 변화].

실전 예시 (동물 토크쇼):
고양이 진행자가 털을 핥고 눈을 굴리며: "우리 가족은 내 옆에 앉아 있기를 거부해!" 강아지 손님이 고개를 끄덕이며: "저도요, 산책 갈 때만 반겨줘요." 토크쇼 무대 세트.

실전 예시 (군인 장면):
고정 렌즈. 서 있는 강한 남자(대장)가 주먹을 불끈 쥐고 팔을 휘두르며 스페인어로 "3분 안에 공격하라!"라고 말한다. 옆의 팀원은 총기를 점검한다.

핵심 포인트:
- 대사는 15초에 25-30단어가 한계
- 대사와 함께 신체 행동을 같이 묘사
- 감정은 물리적으로 ("주먹을 쥐고", "눈을 굴리며")

━━━━━━━━━━━━━━━━━━━━━━━━━━

레시피 11: 1인칭 POV / 롤러코스터 시점

주관적 시점의 몰입형 영상을 만드는 패턴입니다.

프롬프트 구조:
@이미지1부터 @이미지N까지 주관적 시점(1인칭)으로 촬영.
[경로/움직임 설명].

실전 예시 (파쿠르):
@이미지1~@이미지5. 거리에서 주자를 따라 계단 위로, 복도를 통과해 지붕까지 올라가 마침내 도시를 내려다보는 원샷 추적. 1인칭 관점.

실전 예시 (놀이공원):
@이미지1~@이미지5. 주관적 시각의 스릴 넘치는 롤러코스터. 속도는 점점 빨라짐. 1인칭 POV.

핵심 포인트:
- 여러 이미지를 경유지로 사용하면 경로가 풍부해짐
- "1인칭", "주관적 시점", "POV"를 명확히 명시
- 속도 변화를 서술하면 리듬감 향상

━━━━━━━━━━━━━━━━━━━━━━━━━━

레시피 12: 텍스트/로고 애니메이션

로고나 텍스트가 등장하는 인트로/아웃트로 만드는 패턴입니다.

프롬프트 구조:
@이미지1의 [배경/텍스처]에서 시작.
@비디오1의 [특수효과]를 참조하여 전환.
"[텍스트]" 글꼴이 나타남.

실전 예시:
@이미지1의 천장부터 시작. @비디오1의 퍼즐 깨기 효과를 참조하여 전환. "BELIEVE" 텍스트를 "Seedance"로 교체. @이미지2의 글꼴 스타일 참조.

실전 예시 (파티클 효과):
검은 화면에서 시작. @비디오1의 입자 효과를 참고. 황금색 입자가 화면 왼쪽에서 떠올라 오른쪽으로 퍼지며 브랜드 로고가 형성됨.

핵심 포인트:
- 비디오 레퍼런스에서 전환 효과를 빌려오면 퀄리티 향상
- 텍스트가 "나타나는 과정"을 묘사해야 함
- 배경 텍스처를 이미지로 지정하면 일관성 유지

━━━━━━━━━━━━━━━━━━━━━━━━━━

레시피 13: 패션/의상 쇼케이스

모델이 여러 의상을 번갈아 입는 패션 영상 패턴입니다.

프롬프트 구조:
@이미지1의 모델 얼굴 특징을 참조.
@이미지2~@이미지N의 의상을 입고 카메라에 다가감.
@비디오1의 리듬 참조.

실전 예시:
@이미지1의 모델 얼굴. 모델은 @이미지2~@이미지6의 의상을 입고 카메라에 가까이 다가가며 장난꾸러기, 차가운, 귀여운, 놀란 표정, 강렬한 표정을 차례로 보여줌. @비디오1의 전환 리듬 참조.

핵심 포인트:
- 첫 번째 이미지는 얼굴/외모 참조 전용
- 나머지 이미지에 각각 다른 의상 배치
- 비디오에서 전환 리듬을 빌려오면 뮤직비디오 느낌

━━━━━━━━━━━━━━━━━━━━━━━━━━

레시피 14: 줄거리 반전 (비디오 리믹스)

기존 영상의 스토리를 뒤집는 패턴입니다.

프롬프트 구조:
@비디오1의 줄거리를 뒤집으면서 [새로운 전개 설명].
[시간 구간별 새로운 스토리].

실전 예시:
@비디오1의 줄거리를 뒤집음. 남자의 눈빛이 갑자기 온화함에서 차갑게 바뀜. 준비가 안 된 순간, 갑자기 여주인공을 다리에서 밀어냄. 카메라가 빠르게 아래를 비춤.

실전 예시 (서스펜스):
@비디오1 전체 줄거리 전복. 0-3초: 양복 남자가 차분한 표정으로 바에 앉아 와인 잔을 듦. 카메라 천천히 전진. 3-8초: 갑자기 와인 잔을 바닥에 던지고 일어남. 빛과 그림자가 급변. 8-15초: 뒤에서 누군가 다가옴.

핵심 포인트:
- 원본 영상의 분위기를 반전시키는 묘사가 핵심
- 감정 전환점을 시간으로 명확히 지정
- 카메라/조명 변화도 감정 전환과 동기화

━━━━━━━━━━━━━━━━━━━━━━━━━━

레시피 15: 부동산/공간 소개 영상

건물이나 공간을 시네마틱하게 소개하는 패턴입니다.

프롬프트 구조:
제공된 [공간] 사진을 바탕으로 2.35:1 와이드스크린, 24fps, 섬세한 영상 스타일.
15초짜리 영화 수준의 [공간 종류] 소개 영상 제작.
@비디오1의 카메라 무빙 참조.

핵심 포인트:
- 2.35:1 비율 + 24fps 지정하면 영화 느낌
- 공간 사진 여러 장을 넣으면 다양한 앵글 연출
- 드론 샷, 크레인 샷 키워드 활용

━━━━━━━━━━━━━━━━━━━━━━━━━━

조합 팁

위 레시피들은 서로 조합할 수 있습니다:
- 레시피 1(캐릭터) + 레시피 7(스타일) = 수묵화 스타일의 캐릭터 애니메이션
- 레시피 4(상품) + 레시피 12(로고) = 상품 소개 + 브랜드 인트로
- 레시피 2(카메라) + 레시피 9(액션) = 참조 영상의 카메라 워크로 전투 장면
- 레시피 6(연장) + 레시피 14(반전) = 기존 영상을 연장하면서 스토리 반전

━━━━━━━━━━━━━━━━━━━━━━━━━━

참고 자료: Seedance 2.0 공식 사용 설명서
https://www.seedance2prompt.com/ko/blog/seedance-2-user-guide"""

        c.execute(
            "INSERT INTO app_posts "
            "(user_id, title, app_name, app_url, category, thumbnail_url, "
            " content, pros, cons, rating, status, approved_at, approved_by) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),%s) RETURNING id",
            (u["id"], "Seedance 2.0 실전 프롬프트 레시피북 — 15가지 카테고리별 활용법",
             "Seedance 2.0", "https://www.seedance2prompt.com/ko/blog/seedance-2-user-guide",
             "팁/가이드", None,
             content,
             "• 15가지 카테고리별 실전 프롬프트 템플릿\n• 조합 팁으로 무한 응용 가능\n• 각 레시피마다 핵심 포인트 정리",
             "• 실사 얼굴 업로드 시 정책상 차단 가능\n• 결과는 매번 다를 수 있음 (반복 생성 권장)",
             0, "approved", u["id"])
        )
    return jsonify({"ok": True, "msg": "Seedance 레시피북 생성 완료!"})


@app.route("/api/seed-v17-update", methods=["POST"])
@admin_required
def seed_v17_update():
    """v1.7 업데이트 노트 시드."""
    u = current_user()
    with db() as c:
        existing = c.fetchone(
            "SELECT id FROM app_posts WHERE user_id = %s AND title LIKE %s",
            (u["id"], "%v1.7 업데이트%")
        )
        if existing:
            return jsonify({"ok": False, "msg": "이미 존재합니다"}), 400

        content = """XAZINGA v1.7 업데이트가 적용되었습니다.

━━━━━━━━━━━━━━━━━━━━━━━━━━

다국어 지원 (KO / EN / JA)

사이트 전체가 한국어, 영어, 일본어를 지원합니다. 헤더 오른쪽의 KO / EN / JA 버튼을 클릭하면 언어가 전환됩니다.

적용 범위:
- 헤더 버튼 (업로드, 로그인, 회원가입, 로그아웃)
- 로그인 / 회원가입 페이지
- 갤러리 필터 및 정렬
- 포트폴리오 페이지 (신청, 작품 올리기, 상태 표시)
- NOTICE 페이지
- 스킬 저장소
- 프로필 페이지 (수정, 관리 등)
- 공통 UI (삭제 확인, 로딩, 빈 상태 메시지)

선택한 언어는 쿠키에 저장되어 다음 방문 시에도 유지됩니다.

━━━━━━━━━━━━━━━━━━━━━━━━━━

SEO 전면 강화

Google 검색에 XAZINGA가 노출되도록 전면적인 SEO 최적화를 적용했습니다.

sitemap.xml 자동 생성
- 고정 페이지, 모든 공개 게시물, 유저 프로필, 포트폴리오가 자동으로 사이트맵에 포함
- 새 게시물이 올라오면 자동으로 사이트맵에 추가
- Google Search Console에 등록 완료 (67페이지 발견)

robots.txt
- 검색 엔진 크롤링 규칙 설정
- DM, 캐릭터 저장소, 어드민 페이지는 검색에서 제외
- API 엔드포인트도 검색 차단

페이지별 메타태그 자동 생성
- 게시물: 작품 제목 + 작성자 + 모델명이 검색 결과 타이틀에 표시
- 프롬프트가 검색 결과 설명(description)에 자동 삽입
- 프로필: "@username의 프로필 — XAZINGA"
- 갤러리: "AI 프롬프트 갤러리 — XAZINGA"

OG 이미지 자동 설정
- 게시물 공유 시 해당 작품 이미지가 카카오톡/X/디스코드 미리보기로 표시
- 프로필 공유 시 아바타가 미리보기 이미지로 표시

구조화 데이터 (JSON-LD)
- 게시물에 ImageObject/VideoObject 스키마 삽입
- Google 이미지 검색, Google 비디오 검색에서 직접 노출 가능

canonical URL
- 중복 URL로 인한 SEO 페널티 방지

━━━━━━━━━━━━━━━━━━━━━━━━━━

NOTICE 개편

기존 APP 카테고리가 NOTICE로 전환되었습니다.

변경 사항:
- 카테고리: 업데이트, 공지, 앱 소개, 팁/가이드, 기타
- 가로 카드 레이아웃 + 내용 미리보기
- 사이트 소개 글 상단 고정 (PINNED 배지)
- 스킬 저장소 섹션 추가 (글 쓰기 버튼 아래)

━━━━━━━━━━━━━━━━━━━━━━━━━━

스킬 저장소

NOTICE 페이지 안에 스킬 저장소가 추가되었습니다. AI 영상/이미지 제작에 유용한 스킬, 프롬프트 템플릿, 워크플로우를 카테고리별로 모아볼 수 있습니다.

카테고리: 영상 제작, 이미지 생성, 마케팅, 글쓰기, 개발, 기타
현재 등록된 스킬: Seedance 디렉터, 비디오 프롬프트 빌더, Seedance 프롬프팅 가이드
COPY 버튼으로 스킬 내용을 클립보드에 복사 가능
어드민이 추가/수정/삭제 가능

━━━━━━━━━━━━━━━━━━━━━━━━━━

포트폴리오 카테고리

크리에이터 포트폴리오 기능이 추가되었습니다.

제한 사양:
- 이미지: 장당 5MB, 게시물당 최대 8장
- 영상: 50MB, 게시물당 1개
- 게시물당 총 50MB
- 1인당 총 5GB

승인제로 운영됩니다. xazinga 계정은 자동 승인, 그 외는 어드민 승인 필요.

━━━━━━━━━━━━━━━━━━━━━━━━━━

캐릭터 저장소 변경

캐릭터 수와 컷 수가 조정되었습니다.

변경 전: 캐릭터 30개, 캐릭터당 60장
변경 후: 캐릭터 60개, 캐릭터당 30장

━━━━━━━━━━━━━━━━━━━━━━━━━━

어드민 대시보드 강화

실시간 사용량 모니터링이 추가되었습니다.

표시 항목:
- DATABASE: 현재 사용량 / 8GB (프로그레스 바)
- IMAGES / VIDEOS: 프롬프트 갤러리 미디어 수
- CHARACTERS: 전체 캐릭터 수
- PORTFOLIO MEMBERS: 승인된 포트폴리오 멤버 수
- PORTFOLIO STORAGE: 포트폴리오 총 용량

━━━━━━━━━━━━━━━━━━━━━━━━━━

보안 강화

Supabase RLS (Row Level Security)를 모든 테이블에 적용했습니다. DB URL을 직접 아는 외부인이 데이터에 접근할 수 없도록 차단됩니다. 사이트 일반 사용에는 영향 없습니다.

━━━━━━━━━━━━━━━━━━━━━━━━━━

콘텐츠 추가

NOTICE에 다음 가이드가 추가되었습니다:
- XAZINGA 업데이트 히스토리 (v1.0 ~ v1.6)
- XAZINGA 사이트 소개
- Seedance 2.0 영상 프롬프트 작성 가이드
- Seedance 2.0 실전 프롬프트 레시피북 (15가지 카테고리)
- 스레드 떡상하게 만드는 AI 워크플로우"""

        c.execute(
            "INSERT INTO app_posts "
            "(user_id, title, app_name, app_url, category, thumbnail_url, "
            " content, pros, cons, rating, status, approved_at, approved_by) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),%s) RETURNING id",
            (u["id"], "v1.7 업데이트 — 다국어 지원, SEO 강화, 스킬 저장소, 포트폴리오",
             "XAZINGA", "https://www.xazinga.com", "업데이트", None,
             content,
             "• 다국어 UI (한/영/일)\n• Google 검색 노출 (SEO)\n• 스킬 저장소 + 포트폴리오\n• 보안 강화 (RLS)",
             None,
             0, "approved", u["id"])
        )
        return jsonify({"ok": True, "msg": "v1.7 업데이트 노트 생성 완료!"})


@app.route("/api/seed-video-ref-guide", methods=["POST"])
@admin_required
def seed_video_ref_guide():
    """Seedance 비디오 레퍼런스 트러블슈팅 게시글 시드."""
    u = current_user()
    with db() as c:
        existing = c.fetchone(
            "SELECT id FROM app_posts WHERE user_id = %s AND title LIKE %s",
            (u["id"], "%비디오 레퍼런스%왜 자꾸 실패%")
        )
        if existing:
            return jsonify({"ok": False, "msg": "이미 존재합니다"}), 400

        content = """파일 스펙은 멀쩡한데 생성만 누르면 에러. 직접 부딪히면서 알아낸 실패 원인과, 실제로 통과시킨 해결법 정리.

스펙이 완벽해도 통과 안 되는 이유는 거의 하나다. 실사 인물 얼굴 감지. 한 프레임이라도 잡히면 그걸로 끝.

━━━━━━━━━━━━━━━━━━━━━━━━━━

01 — 시스템 레벨에서 그냥 막아버리는 것들

파일을 받기도 전에 차단되는 케이스.

실사 얼굴: 한 프레임이라도 얼굴 감지되면 차단. 유명인 아니어도 동일. 무술 레퍼런스가 거의 무조건 걸리는 이유.
파일 크기: 비디오 50MB / 이미지 30MB / 오디오 15MB 초과 시 자동 차단.
비디오 길이: 2초 미만 또는 15초 초과. 여러 개 올릴 때는 합산 15초가 상한.
파일 개수: 이미지 + 비디오 + 오디오 합산 12개를 넘기면 안 됨.
포맷: 비디오는 mp4, mov만. mkv, avi 등은 받지 않음.

━━━━━━━━━━━━━━━━━━━━━━━━━━

02 — 비디오만 단독으로 올릴 때 자주 걸리는 함정

스펙 검사 통과한 뒤에도 실패하는 케이스.

해상도 미스매치: 480p~720p 권장 범위를 벗어나면 처리 실패. 4K나 1080p 원본을 그대로 올리면 막힌다. 720p로 다운스케일해서 올리는 게 안전.

HEVC 코덱: 같은 mp4라도 H.265(HEVC)면 실패하는 경우가 있음. H.264로 재인코딩 필요.

총 길이 합산 초과: 비디오 3개를 동시에 올릴 때 합산이 15초를 넘으면 차단. 8초 + 8초 = 16초도 막힌다.

현장 팁: 클로드 파일 업로드 파이프라인은 이미지를 자동으로 재압축한다. 클로드에서 확인한 파일 크기로 "이미지가 너무 작아서 실패" 같은 진단 내리면 거의 틀린다. 진단은 항상 로컬 원본 기준.

━━━━━━━━━━━━━━━━━━━━━━━━━━

03 — 그래서 어떻게 통과시킬 것인가

실사 인물 얼굴 문제에 대한 실전 해결법 3가지.

1. 얼굴 없는 구간만 잘라서 쓰기
하체 동작, 손 동작, 뒷모습 구간만 크롭해서 업로드. 가장 간단하고 빠른 우회. (FFmpeg, CapCut)

2. 실루엣으로 변환
얼굴 인식을 피하면서 동작은 그대로 전달. 오히려 동작 참조 품질이 더 좋게 나올 때도 있다. CapCut 실루엣 필터 한 번이면 충분.

3. 애니메이션 / 게임 클립으로 교체
가장 확실한 방법. 얼굴 감지가 아예 안 걸리고 동작 참조도 잘 잡힌다. 귀멸의 칼날, 블루 아이 사무라이 같은 애니, Ghost of Tsushima, Nioh 같은 게임 컷씬이 검술 레퍼런스로 쓸 만하다.

━━━━━━━━━━━━━━━━━━━━━━━━━━

04 — 플랫폼 옮기면 해결되나? — 안 됨

씨댄스의 구조 문제라 다른 플랫폼으로 도망쳐도 똑같다.

Seedance 2.0: 동작 레퍼런스 가능, 실사 인물 얼굴 차단
Higgsfield: 비디오 동작 레퍼런스 구조 자체가 없음
Runway Gen-4: 이미지만 가능, 비디오 레퍼런스 없음

힉스필드와 런웨이는 비디오를 동작 레퍼런스로 받는 구조 자체가 없다. 옮겨봐야 해결 안 됨. 답은 레퍼런스 소스를 바꾸는 것뿐.

━━━━━━━━━━━━━━━━━━━━━━━━━━

05 — 실사 → 애니 변환 워크플로우

실사 무술 영상을 그대로 못 쓸 때 우회 경로.

1순위 — Kling Video to Video
애니/일러스트 스타일 전환이 가장 강력하고, 복잡한 동작에서도 프레임 간 일관성이 높다. 레퍼런스 영상의 동작, 프레이밍, 카메라 언어를 보존하면서 재렌더링이 가능해서 "일본 애니 스타일로, 검 동작과 줌은 유지" 같은 프롬프트가 잘 먹힌다.

2순위 — DomoAI
일본 애니메이션 모델 특화. 영상 전체에서 스타일이 흔들리지 않고 일관되게 유지된다. 30개 이상 스타일을 지원하고, 애니화 품질만 따지면 Kling보다 낫다는 평도 있다.

워크플로우: 실사 무술 영상 → Kling/DomoAI로 애니화 → 그 클립을 씨댄스 레퍼런스로 투입. 이 경로면 얼굴 감지 우회와 동작 보존이 둘 다 잡힌다.

━━━━━━━━━━━━━━━━━━━━━━━━━━

06 — 비디오 레퍼런스 필수 프롬프트

비디오를 올리고 프롬프트를 안 쓰거나 대충 쓰면 Seedance가 알아서 해석한다. 결과는 랜덤. 아래 문구를 상황에 맞게 조합해서 넣어야 의도대로 나온다.

동작 고정 (가장 중요)
@비디오1의 모든 동작과 움직임을 완전히 참조.
→ 이게 없으면 Seedance가 동작을 무시하고 새로 만든다.

카메라만 복제 (동작 말고 카메라만 빌릴 때)
@비디오1의 카메라 움직임과 전환 효과만 참조. 캐릭터와 배경은 복사하지 않음.
→ "만"이 핵심. 안 쓰면 캐릭터까지 따라옴.

캐릭터 교체 (영상 속 인물을 바꿀 때)
@비디오1의 캐릭터를 @이미지1로 교체. 첫 프레임을 @이미지1로 설정. @비디오1의 움직임과 표정을 완전히 참조.
→ "첫 프레임 설정"이 캐릭터 일관성의 핵심.

리듬/타이밍 복제 (뮤직비디오, 광고)
@비디오1의 전환 리듬과 컷 타이밍을 참조. 다운비트에 강한 모션, 조용한 구간에 부드러운 전환.
→ 비트 매칭할 때 필수.

동작 + 특수효과 분리 참조
@비디오1의 캐릭터 움직임을 참조. @비디오2의 카메라 언어와 특수 효과를 참조.
→ 비디오 2개를 각각 다른 역할로 쓸 때.

네거티브 지시 (안 쓰면 망하는 것들)
얼굴 변형 없음. 손가락 왜곡 없음. 추가 인물 없음. 카메라 컷 없음.
→ 원하지 않는 걸 명시적으로 막아야 한다.

원샷 지정
원샷. 중간에 컷이나 장면 전환 없이 하나의 연속 촬영.
→ 안 쓰면 Seedance가 임의로 컷을 넣는다.

━━━━━━━━━━━━━━━━━━━━━━━━━━

07 — 조합 예시

검술 장면 (애니 레퍼런스 사용):
@비디오1의 모든 동작과 움직임을 완전히 참조. @이미지1의 캐릭터 외모를 주인공으로. 벚꽃이 흩날리는 밤의 신사 앞. 카메라는 로우앵글에서 시작해 검이 부딪히는 순간 클로즈업. 원샷. 추가 인물 없음.

댄스 영상 (카메라만 복제):
@비디오1의 카메라 움직임과 전환 효과만 참조. 캐릭터와 배경은 복사하지 않음. @이미지1의 댄서가 네온 조명의 스튜디오에서 공연. 카메라 컷 없음. 얼굴 변형 없음.

상품 광고 (리듬 복제):
@비디오1의 전환 리듬과 컷 타이밍을 참조. @이미지1의 제품을 히어로 오브젝트로. 스튜디오 조명, 천천히 360도 회전에서 시작. 다운비트에 클로즈업 전환.

━━━━━━━━━━━━━━━━━━━━━━━━━━

08 — 업로드 전 체크리스트

생성 누르기 전에 한 번씩 보고 가는 용도.

포맷: mp4 또는 mov
코덱: H.264 (HEVC 금지)
해상도: 720p 권장 (480p 가능)
길이: 2~15초, 여러 개면 합산 15초 이내
파일 크기: 50MB 이하
실사 인물 얼굴 없음
비디오 최대 3개

— 직접 부딪히면서 정리한 메모."""

        c.execute(
            "INSERT INTO app_posts "
            "(user_id, title, app_name, app_url, category, thumbnail_url, "
            " content, pros, cons, rating, status, approved_at, approved_by) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),%s) RETURNING id",
            (u["id"], "Seedance 2.0 비디오 레퍼런스, 왜 자꾸 실패할까",
             "Seedance 2.0", None, "팁/가이드", None,
             content,
             "• 실사 얼굴 감지 우회법 3가지\n• 실사→애니 변환 워크플로우\n• 업로드 전 체크리스트",
             None,
             0, "approved", u["id"])
        )
    return jsonify({"ok": True, "msg": "비디오 레퍼런스 가이드 생성 완료!"})


def _do_translate(text, target):
    """Google Translate 무료 엔드포인트로 번역. 성공 시 문자열, 실패 시 None."""
    if not text or not text.strip():
        return ""
    if len(text) > 5000:
        text = text[:5000]
    try:
        r = requests.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": "auto", "tl": target, "dt": "t", "q": text},
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        r.raise_for_status()
        data = r.json()
        translated = "".join(seg[0] for seg in data[0] if seg and len(seg) > 0 and seg[0])
        return translated
    except Exception:
        return None


@app.route("/api/translate", methods=["POST"])
def api_translate():
    """텍스트 실시간 번역. POST {text, target} — 회원 전용, 하루 5회 제한."""
    from datetime import datetime, timezone, timedelta

    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    target = (data.get("target") or "ko").strip()
    if target not in ("ko", "en", "zh-CN"):
        return jsonify({"error": "지원하지 않는 언어"}), 400
    if not text:
        return jsonify({"translated": ""})

    # 로그인 필수 — 회원만 번역 사용 가능
    u = current_user()
    if not u:
        return jsonify({"error": "번역은 로그인 후 이용할 수 있어요.", "login_required": True}), 401

    # 레이트 리밋: 로그인 유저 id 기준 / KST 날짜 기준 하루 5회
    DAILY_LIMIT = 5
    identifier = "user:" + str(u["id"])
    today_kst = (datetime.now(timezone.utc) + timedelta(hours=9)).date()

    with db() as c:
        row = c.fetchone(
            "SELECT count FROM translation_usage WHERE identifier = %s AND used_date = %s",
            (identifier, today_kst),
        )
        used = row["count"] if row else 0
        if used >= DAILY_LIMIT:
            return jsonify({
                "error": f"번역은 하루 {DAILY_LIMIT}회까지만 가능해요. 내일 다시 시도해주세요.",
                "limit_reached": True, "used": used, "limit": DAILY_LIMIT,
            }), 429

    translated = _do_translate(text, target)
    if translated is None:
        return jsonify({"error": "번역 실패"}), 500

    # 번역 성공 시에만 카운트 증가
    try:
        with db() as c:
            c.execute(
                "INSERT INTO translation_usage (identifier, used_date, count) VALUES (%s, %s, 1) "
                "ON CONFLICT (identifier, used_date) DO UPDATE SET count = translation_usage.count + 1",
                (identifier, today_kst),
            )
    except Exception:
        pass

    remaining = max(0, DAILY_LIMIT - (used + 1))
    return jsonify({"translated": translated, "remaining": remaining})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)





































