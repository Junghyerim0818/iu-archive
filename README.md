# IU Archive (Flask + Vercel)

아이유 사진을 팬들이 올리고, 관리자가 승인한 뒤 아카이브로 공개하는 간단한 플라스크 앱입니다.

## 요구 환경
- Python 3.10+
- Vercel Python 빌더 (@vercel/python)

## 설정
`.env` 파일 또는 환경 변수로 다음 값을 지정하세요.

- `SECRET_KEY` : 세션용 비밀키
- `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` : 구글 OAuth 클라이언트 값
- `ADMIN_EMAILS` : 관리자 이메일 콤마 구분(예: `admin1@mail.com,admin2@mail.com`)
- `DAILY_UPLOAD_LIMIT` : 1인당 일일 업로드 제한(기본 10)
- `DATABASE_URL` : 선택 사항, 기본 SQLite(`sqlite:///iu_archive.db`)
- `STORAGE_BACKEND` : `local`(기본) 또는 `s3`
- S3 사용 시 `S3_BUCKET`, `S3_REGION`(기본 `ap-northeast-2`), 필요 시 `S3_ENDPOINT`

로컬 실행 시:
```bash
python -m venv .venv
source .venv/bin/activate  # Windows는 .venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

## 배포 (Vercel)
`vercel.json`이 포함되어 있으며 `app.py`를 엔트리로 사용합니다.  
Vercel의 "Environment Variables"에 위 설정값을 등록한 뒤 배포하세요.  
주의: Vercel 서버리스 파일시스템은 일시적이므로 실제 운영 시 `STORAGE_BACKEND=s3`로 설정하고 S3 버킷을 사용하는 것을 권장합니다. 현재 코드는 S3 또는 로컬 파일시스템을 선택적으로 사용합니다.  
데이터베이스를 Neon(PostgreSQL)로 사용할 때는 Vercel에서 제공하는 `DATABASE_URL`(postgres 접두사)을 그대로 설정하면 됩니다. 서버리스 환경에서는 풀을 사용하지 않도록 설정되어 있습니다.

## 기능 요약
- 구글 로그인 전용
- 일일 업로드 제한 (관리자 제외 가능)
- 관리자 승인 워크플로(승인/거절)
- 날짜·행사명 기준 폴더 분류 및 목록 표시


