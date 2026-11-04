# IU Archive

Flask + SQLAlchemy 기반 사진 아카이브. Vercel(서버리스)에 배포하고, DB는 Postgres(Neon 등), 파일은 Google Cloud Storage를 사용합니다.

## 환경 변수

| 이름 | 필수 | 설명 |
| --- | --- | --- |
| `SECRET_KEY` | ✅ (운영) | 세션 서명 키. 운영 환경(Vercel)에서 없으면 앱이 시작되지 않습니다. `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `DATABASE_URL` | ✅ (운영) | Postgres 접속 문자열. 없으면 SQLite (Vercel에서는 `/tmp`라 데이터가 사라짐) |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | ✅ | Google OAuth |
| `ADMIN_EMAILS` | | 쉼표로 구분한 관리자 이메일 |
| `STORAGE_BACKEND` | | `gcs` 또는 `local`(기본) |
| `GCS_BUCKET` / `GCS_PROJECT` / `GCS_CREDENTIALS` | gcs일 때 | 버킷, 프로젝트, 서비스 계정 JSON(문자열 또는 파일 경로) |
| `DAILY_UPLOAD_LIMIT` | | 하루 업로드 장수 (기본 20) |
| `DAILY_UPLOAD_MB` | | 하루 업로드 용량 MB (기본 100) |
| `MAX_FILE_MB` | | 파일 1개 최대 MB (기본 10) |
| `SIGNED_URL_HOURS` | | 이미지 서명 URL 유효 시간 (기본 6) |
| `APP_TIMEZONE` | | '오늘' 기준 시간대 (기본 `Asia/Seoul`) |
| `AUTO_DDL` | | `0`이면 시작 시 테이블/컬럼/인덱스 자동 생성을 건너뜀 |

## 로컬 실행

```bash
pip install -r requirements-dev.txt
# .env 파일에 위 환경 변수 작성 (SECRET_KEY 없으면 로컬 전용 기본값 사용)
python app.py          # http://localhost:8000
```

## 테스트

```bash
python -m pytest -q
```
