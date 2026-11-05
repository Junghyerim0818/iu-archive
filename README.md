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
| `DERIVATIVE_TIME_BUDGET` | | 한 요청에서 썸네일 생성에 쓸 최대 초 (기본 8) |
| `DERIVATIVE_WORKERS` | | 썸네일 동시 생성 스레드 수 (기본 4) |

## 이미지 제공 방식 (썸네일 + CDN)

원본은 그대로 비공개로 두고, 화면에는 WebP 파생 이미지를 씁니다.

| 종류 | 크기 | 주소 | 용도 |
| --- | --- | --- | --- |
| thumb | 짧은 변 480px | `/photos/<id>/thumb?v=1` | 갤러리, 관리자/프로필 목록 |
| display | 긴 변 1920px | `/photos/<id>/display?v=1` | 확대 보기 |
| 원본 | 그대로 | `/photos/<id>/download` | 다운로드 (서명 URL로 리디렉션) |

- 파생 이미지는 GCS `derived/v1/...` 에 저장됩니다. 업로드 완료/승인 시점에 시간 예산 안에서 만들고, 남은 건 처음 조회될 때 만들어집니다.
- **승인된 사진**의 파생 이미지는 `Cache-Control: public, max-age=31536000, s-maxage=31536000, immutable` 로 응답하므로
  Vercel CDN과 브라우저에 캐시됩니다. 서버 함수와 GCS는 (배포별) 캐시 미스 때만 호출됩니다.
  이 응답에서는 세션을 읽지 않아 `Vary: Cookie`/`Set-Cookie`가 붙지 않습니다.
- 승인 전 사진은 `private` 로만 응답합니다.
- 업로드는 **JPG / PNG / WebP / GIF / HEIC**만 허용합니다. 브라우저가 보낸 타입이 아니라 파일 앞부분(매직 넘버)으로 서버에서 다시 검사합니다.
- **HEIC는 서버에서 JPG(품질 90)로 변환**해 원본으로 보관하고 HEIC 파일은 지웁니다. 다운로드도 JPG로 나갑니다.
  변환한 JPG에는 EXIF(GPS 위치 등)를 넣지 않고, 색 정보(ICC 프로파일)는 유지합니다. (`pillow-heif` 사용)
- 그래도 손상된 파일 등으로 변환에 실패하면 `derive_failed`로 표시되고 원본으로 대체됩니다.
- 기존 사진은 관리자 화면의 **썸네일 미리 만들기** 버튼이나 `flask --app app backfill-derivatives`로 미리 만들 수 있습니다.
- 썸네일 규칙을 바꾸면 `media.py`의 `DERIVATIVE_VERSION`을 올리세요. 경로/URL이 바뀌어 캐시가 자연스럽게 갱신됩니다.

## 개인정보: 원본 메타데이터 제거

휴대폰 사진 원본에는 촬영 위치(GPS), 기기 정보, 촬영 시각이 EXIF/XMP로 들어 있고, 원본은 누구나 다운로드할 수 있으므로 제거합니다.

- JPEG·PNG·WebP는 **메타데이터 블록만 잘라내고 픽셀은 재압축하지 않습니다**(화질 손실 없음). JPEG의 회전 정보와 ICC 색 프로파일은 유지합니다.
  JPEG 끝에 붙은 보조 이미지(MPF, 자체 EXIF 포함)도 제거합니다. HEIC는 JPG 변환 시 메타데이터를 넣지 않습니다.
- 정리는 썸네일 생성과 같은 처리 과정에서 하며, 원본 파일을 같은 경로에 덮어씁니다.
- **원본을 내보내는 모든 경로**(`/photos/<id>/download`, `/photos/<id>/image`, `/files/...`, 썸네일 실패 시 대체)는
  정리가 끝난 원본(`meta_clean`)만 내보냅니다. 처리 중이면 `503 + Retry-After`, 정리할 수 없는 파일이면 404.
- 기존 사진은 관리자 화면의 **지금 정리하기** 버튼(또는 `flask --app app backfill-derivatives`)으로 한 번에 정리하세요.
- ⚠️ GCS 버킷에 **객체 버전 관리(Object Versioning)** 가 켜져 있으면 덮어쓰기 전 버전(위치 정보 포함)이 남습니다.
  켜져 있다면 끄거나, 이전 버전을 삭제하는 수명 주기 규칙을 추가하세요.

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
