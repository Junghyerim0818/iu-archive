# IU Archive (Flask + Vercel)

아이유 사진을 팬들이 올리고, 관리자가 승인한 뒤 아카이브로 공개하는 간단한 플라스크 앱입니다.

## 요구 환경
- Python 3.10+
- Vercel Python 빌더 (@vercel/python)

## 설정
`.env` 파일 또는 환경 변수로 다음 값을 지정하세요.

### 필수 설정
- `SECRET_KEY` : 세션용 비밀키 (랜덤 문자열 생성 권장)
- `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` : 구글 OAuth 클라이언트 값
- `ADMIN_EMAILS` : 관리자 이메일 콤마 구분(예: `admin1@mail.com,admin2@mail.com`)

### 선택 설정
- `DAILY_UPLOAD_LIMIT` : 1인당 일일 업로드 제한(기본 10)
- `DATABASE_URL` : **Neon PostgreSQL 권장** (예: `postgresql://user:pass@host/db?sslmode=require`)
  - 설정하지 않으면 SQLite 사용 (서버리스 환경에서는 권장하지 않음)
- `STORAGE_BACKEND` : `local`(기본, 로컬 개발용) 또는 `s3`(프로덕션 권장)
- S3 사용 시: `S3_BUCKET`, `S3_REGION`(기본 `ap-northeast-2`), 필요 시 `S3_ENDPOINT`
- `AUTO_DDL` : 스키마 자동 생성 (기본 `1`, `0`으로 설정하면 비활성화)

로컬 실행 시:
```bash
python -m venv .venv
source .venv/bin/activate  # Windows는 .venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

## 배포 (Vercel)

### 1. Vercel 프로젝트 설정
`vercel.json`이 포함되어 있으며 `app.py`를 엔트리로 사용합니다.

### 2. 환경 변수 설정
Vercel 대시보드 → 프로젝트 → Settings → Environment Variables에서 다음을 설정하세요:

**필수:**
- `SECRET_KEY`: 랜덤 문자열 (예: `openssl rand -hex 32`로 생성)
- `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`: Google OAuth 설정
- `ADMIN_EMAILS`: 관리자 이메일 (콤마 구분)
- `DATABASE_URL`: Neon PostgreSQL 연결 문자열
  - 예: `postgresql://user:password@host/dbname?sslmode=require`

**선택 (S3 사용 시):**
- `STORAGE_BACKEND=s3`
- `S3_BUCKET`: S3 버킷 이름
- `S3_REGION`: 리전 (기본: `ap-northeast-2`)
- `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`: AWS 자격증명

### 3. 배포
환경 변수 설정 후 배포하면 자동으로 DB 스키마가 생성됩니다.

**주의사항:**
- Vercel 서버리스 파일시스템은 일시적이므로 프로덕션에서는 반드시 `STORAGE_BACKEND=s3` 사용 권장
- Neon PostgreSQL 사용 시 서버리스 환경에 최적화된 연결 풀 설정이 자동 적용됩니다

## 기능 요약
- 구글 로그인 전용
- 일일 업로드 제한 (관리자 제외 가능)
- 관리자 승인 워크플로(승인/거절)
- 날짜·행사명 기준 폴더 분류 및 목록 표시


