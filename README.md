# Prompt Gallery

AI로 만든 이미지/영상과 사용한 프롬프트를 함께 아카이브하는 커뮤니티 사이트.

## 기능

- 이미지/영상 업로드 + 프롬프트 함께 저장
- 회원가입/로그인 (개인 페이지)
- IMAGE / VIDEO / ALL 카테고리
- 모델별 필터, 키워드 검색, 좋아요
- 프롬프트 원클릭 복사
- Higgsfield 스타일 다크 테마 + 마소너리 레이아웃

## 기술 스택

- **Backend**: Flask (Python 3.11)
- **DB**: SQLite
- **Frontend**: Vanilla HTML/CSS/JS, Jinja2 템플릿
- **Server**: Gunicorn
- **배포**: Render.com (무료 티어)

## 로컬 실행

```bash
pip install -r requirements.txt
python app.py
```

http://localhost:5000

## 배포 (Render)

1. https://render.com 가입 후 GitHub 연결
2. New + → Web Service → 이 저장소 선택
3. 자동으로 `render.yaml` 인식하고 배포
4. 환경변수 `SECRET_KEY` 자동 생성됨

## 디렉터리

```
app.py              # Flask 앱
templates/          # HTML 템플릿
  base.html
  index.html
  detail.html
  upload.html
  search.html
  profile.html
  signup.html
  login.html
seed.py             # 초기 데이터 생성 스크립트
migrate.py          # DB 마이그레이션
requirements.txt    # 의존성
render.yaml         # Render 배포 설정
```

