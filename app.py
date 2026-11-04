import hmac
import json
import os
import random
import re
import secrets
import threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import date, datetime, time, timedelta, timezone
from functools import wraps
from time import monotonic
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo

from authlib.integrations.flask_client import OAuth
from flask import (
    Flask,
    Response,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from flask_sqlalchemy import SQLAlchemy
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import func, inspect, literal, or_, text
from sqlalchemy.pool import NullPool
from werkzeug.utils import secure_filename

from media import (
    DERIVATIVE_VERSION,
    SNIFF_BYTES,
    SUPPORTED_EXTENSIONS,
    SUPPORTED_FORMATS,
    SUPPORTED_MIME_TYPES,
    ImageProcessingError,
    make_derivatives,
    sniff_image_format,
    unsupported_reason,
)

try:
    from dotenv import load_dotenv

    load_dotenv()  # 로컬 개발 시 .env 로드 (운영 환경 변수는 덮어쓰지 않음)
except ImportError:
    pass

BASE_DIR = Path(__file__).parent
IS_SERVERLESS = bool(os.getenv("VERCEL") or os.getenv("AWS_LAMBDA_FUNCTION_NAME"))
IS_PRODUCTION = IS_SERVERLESS or os.getenv("FLASK_ENV") == "production"

# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------

# 서버리스에서는 /tmp 만 쓰기 가능 (단, 인스턴스마다 따로이고 언제든 사라짐)
default_sqlite = (
    "sqlite:////tmp/iu_archive.db" if IS_SERVERLESS else f"sqlite:///{BASE_DIR / 'iu_archive.db'}"
)
database_uri = os.getenv("DATABASE_URL", default_sqlite)
if database_uri.startswith("postgres://"):
    # SQLAlchemy 2.x는 postgres:// 스킴을 지원하지 않음
    database_uri = "postgresql://" + database_uri[len("postgres://"):]

engine_options = {}
if database_uri.startswith("postgres") and IS_SERVERLESS:
    # 서버리스 + Postgres(Neon 등)에서는 연결 풀을 끄고 pre_ping을 켭니다.
    engine_options = {"poolclass": NullPool, "pool_pre_ping": True}

secret_key = os.getenv("SECRET_KEY")
if not secret_key:
    if IS_PRODUCTION:
        # 기본값으로 운영하면 누구나 세션 쿠키를 위조해 관리자로 로그인할 수 있음
        raise RuntimeError("SECRET_KEY 환경변수가 설정되지 않았습니다. 운영 환경에서는 필수입니다.")
    secret_key = "dev-secret-local-only"

app = Flask(__name__)
app.config.update(
    SECRET_KEY=secret_key,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=IS_PRODUCTION,
    SQLALCHEMY_DATABASE_URI=database_uri,
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    SQLALCHEMY_ENGINE_OPTIONS=engine_options,
    MAX_CONTENT_LENGTH=int(os.getenv("MAX_REQUEST_MB", "110")) * 1024 * 1024,
    UPLOAD_FOLDER=BASE_DIR / "uploads",
    DAILY_UPLOAD_LIMIT=int(os.getenv("DAILY_UPLOAD_LIMIT", "20")),  # 하루 최대 업로드 장수
    DAILY_UPLOAD_MB=int(os.getenv("DAILY_UPLOAD_MB", "100")),  # 하루 최대 업로드 용량
    MAX_FILE_MB=int(os.getenv("MAX_FILE_MB", "10")),  # 파일 1개 최대 크기
    MAX_FILES_PER_REQUEST=int(os.getenv("MAX_FILES_PER_REQUEST", "200")),
    SIGNED_URL_HOURS=int(os.getenv("SIGNED_URL_HOURS", "6")),
    # 썸네일 생성에 한 요청이 쓸 수 있는 최대 시간(초). 남은 건 다음 요청/백필에서 처리
    DERIVATIVE_TIME_BUDGET=float(os.getenv("DERIVATIVE_TIME_BUDGET", "8")),
    DERIVATIVE_WORKERS=int(os.getenv("DERIVATIVE_WORKERS", "4")),
    APP_TIMEZONE=os.getenv("APP_TIMEZONE", "Asia/Seoul"),
    STORAGE_BACKEND=os.getenv("STORAGE_BACKEND", "local"),  # local or gcs
    GCS_BUCKET=os.getenv("GCS_BUCKET"),
    GCS_PROJECT=os.getenv("GCS_PROJECT"),
    GCS_CREDENTIALS=os.getenv("GCS_CREDENTIALS"),  # JSON 키 파일 경로 또는 JSON 문자열
)
APP_TZ = ZoneInfo(app.config["APP_TIMEZONE"])
MB = 1024 * 1024

REJECTED_PLACEHOLDER = (
    "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='200' height='200' "
    "viewBox='0 0 200 200'%3E%3Crect fill='%231a1a1a' width='200' height='200'/%3E%3Ctext "
    "fill='%23888888' font-family='sans-serif' font-size='14' x='50%25' y='50%25' "
    "text-anchor='middle' dominant-baseline='middle'%3E%EA%B1%B0%EC%A0%88%EB%90%9C%20"
    "%EC%82%AC%EC%A7%84%3C/text%3E%3C/svg%3E"
)
MAX_EVENT_NAME_LEN = 100
MAX_TAG_LEN = 30
MAX_TAGS = 20

db = SQLAlchemy(app)
upload_token_serializer = URLSafeTimedSerializer(app.config["SECRET_KEY"], salt="upload-token")
UPLOAD_TOKEN_MAX_AGE = 2 * 60 * 60  # presigned URL(1시간)보다 여유 있게

if IS_SERVERLESS and app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite"):
    app.logger.warning("서버리스 환경에서 SQLite(/tmp)를 사용 중입니다. 데이터가 유지되지 않습니다. DATABASE_URL을 설정하세요.")
if IS_SERVERLESS and app.config["STORAGE_BACKEND"] != "gcs":
    app.logger.warning("서버리스 환경에서 로컬 스토리지를 사용 중입니다. 업로드 파일이 유지되지 않습니다.")

# ---------------------------------------------------------------------------
# Google Cloud Storage
# ---------------------------------------------------------------------------

gcs_client = None
gcs_bucket = None
if app.config["STORAGE_BACKEND"] == "gcs":
    try:
        from google.cloud import storage
        from google.oauth2 import service_account

        creds_value = app.config["GCS_CREDENTIALS"]
        if creds_value:
            if creds_value.lstrip().startswith("{"):
                credentials = service_account.Credentials.from_service_account_info(json.loads(creds_value))
            else:
                credentials = service_account.Credentials.from_service_account_file(creds_value)
            gcs_client = storage.Client(project=app.config["GCS_PROJECT"], credentials=credentials)
        else:
            # 환경 변수에서 자동으로 인증 (GOOGLE_APPLICATION_CREDENTIALS 등)
            gcs_client = storage.Client(project=app.config["GCS_PROJECT"])
        gcs_bucket = gcs_client.bucket(app.config["GCS_BUCKET"])
    except Exception as e:
        app.logger.error(f"Google Cloud Storage 초기화 실패: {e}")
        gcs_client = None
        gcs_bucket = None


def use_gcs() -> bool:
    return app.config["STORAGE_BACKEND"] == "gcs"


# ---------------------------------------------------------------------------
# 모델
# ---------------------------------------------------------------------------


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    name = db.Column(db.String(255))
    picture = db.Column(db.String(512))
    is_admin = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Photo(db.Model):
    __table_args__ = (
        db.Index("ix_photo_status_taken_date", "status", "taken_date"),
        db.Index("ix_photo_user_created_at", "user_id", "created_at"),
        db.Index("ix_photo_relative_path", "relative_path"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    relative_path = db.Column(db.String(512), nullable=False)
    original_name = db.Column(db.String(255))
    event_name = db.Column(db.String(255))  # 사진명/행사명
    taken_date = db.Column(db.Date, nullable=False)  # 촬영일
    tags = db.Column(db.String(512))  # 태그 (공백 없이 쉼표로 구분, 정렬됨)
    status = db.Column(db.String(32), default="pending")  # pending/approved/rejected
    file_size = db.Column(db.Integer)  # 파일 크기 (bytes)
    download_count = db.Column(db.Integer, default=0)  # 다운로드 횟수
    created_at = db.Column(db.DateTime, default=datetime.utcnow)  # UTC
    # 파생 이미지 (WebP). 없으면 아직 생성 전
    thumb_path = db.Column(db.String(600))  # 갤러리 썸네일
    display_path = db.Column(db.String(600))  # 확대 보기용
    width = db.Column(db.Integer)  # 원본 크기 (회전 보정 후)
    height = db.Column(db.Integer)
    derive_failed = db.Column(db.Boolean, default=False)  # 처리할 수 없는 형식 (HEIC 등) → 원본 사용

    user = db.relationship("User", backref="photos")


# ---------------------------------------------------------------------------
# 스키마 초기화 (프로세스당 1회)
# ---------------------------------------------------------------------------

PHOTO_EXTRA_COLUMNS = {
    "file_size": "INTEGER",
    "tags": "VARCHAR(512)",
    "download_count": "INTEGER DEFAULT 0",
    "thumb_path": "VARCHAR(600)",
    "display_path": "VARCHAR(600)",
    "width": "INTEGER",
    "height": "INTEGER",
    "derive_failed": "BOOLEAN DEFAULT FALSE",
}
PHOTO_INDEXES = {
    "ix_photo_status_taken_date": "photo (status, taken_date)",
    "ix_photo_user_created_at": "photo (user_id, created_at)",
    "ix_photo_relative_path": "photo (relative_path)",
}


def ensure_schema():
    """테이블 생성 + 예전 DB에 빠진 컬럼/인덱스 추가."""
    uri = app.config["SQLALCHEMY_DATABASE_URI"]
    if uri.startswith("sqlite:///"):
        Path(uri.replace("sqlite:///", "")).parent.mkdir(parents=True, exist_ok=True)

    db.create_all()

    existing = {col["name"] for col in inspect(db.engine).get_columns("photo")}
    with db.engine.begin() as conn:
        for name, ddl in PHOTO_EXTRA_COLUMNS.items():
            if name not in existing:
                conn.execute(text(f"ALTER TABLE photo ADD COLUMN {name} {ddl}"))
                app.logger.info(f"Photo 테이블에 {name} 컬럼 추가")
        for name, target in PHOTO_INDEXES.items():
            conn.execute(text(f"CREATE INDEX IF NOT EXISTS {name} ON {target}"))


_schema_ready = False
_schema_lock = threading.Lock()


@app.before_request
def ensure_schema_once():
    global _schema_ready
    if _schema_ready or os.getenv("AUTO_DDL", "1") == "0":
        return
    with _schema_lock:
        if _schema_ready:
            return
        try:
            ensure_schema()
            _schema_ready = True
        except Exception as exc:
            # 다음 요청에서 다시 시도
            app.logger.error("스키마 초기화 실패: %s", exc, exc_info=True)


# ---------------------------------------------------------------------------
# 공통 유틸
# ---------------------------------------------------------------------------


def wants_json() -> bool:
    return (
        request.is_json
        or request.path.startswith("/api/")
        or request.headers.get("X-Requested-With") == "XMLHttpRequest"
    )


def json_error(message: str, status: int = 400, **extra):
    return jsonify({"success": False, "error": message, **extra}), status


def local_today() -> date:
    return datetime.now(APP_TZ).date()


def today_bounds_utc() -> tuple[datetime, datetime]:
    """서비스 시간대 기준 '오늘'을 UTC naive datetime 구간으로 반환 (created_at이 UTC naive)."""
    start_local = datetime.combine(local_today(), time.min, tzinfo=APP_TZ)
    start_utc = start_local.astimezone(timezone.utc).replace(tzinfo=None)
    return start_utc, start_utc + timedelta(days=1)


def normalize_event_name(value) -> str:
    value = str(value or "").strip()
    return value[:MAX_EVENT_NAME_LEN] or "기타"


def normalize_tags(value) -> Optional[str]:
    if isinstance(value, (list, tuple)):
        raw = value
    else:
        raw = str(value or "").split(",")
    tags = sorted({str(t).strip()[:MAX_TAG_LEN] for t in raw if str(t).strip()})
    tags = tags[:MAX_TAGS]
    joined = ",".join(tags)
    return joined[:512] or None


def split_tags(value: Optional[str]) -> list[str]:
    return [t.strip() for t in (value or "").split(",") if t.strip()]


def parse_date(value: Optional[str], default: Optional[date] = None) -> Optional[date]:
    if not value:
        return default
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def is_allowed_image_type(content_type: Optional[str]) -> bool:
    """썸네일을 만들 수 있는 형식(JPG/PNG/WebP/GIF)만 허용. SVG·HEIC 등은 거부."""
    content_type = (content_type or "").split(";")[0].strip().lower()
    return content_type in SUPPORTED_MIME_TYPES


def is_supported_content(head: bytes) -> bool:
    """실제 파일 내용(앞부분)이 허용 형식인지. Content-Type은 클라이언트가 속일 수 있음."""
    return sniff_image_format(head) in SUPPORTED_FORMATS


def escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def tag_exact_match(tag: str):
    """쉼표 구분 문자열에서 태그를 '정확히' 일치시키는 조건 ('IU'가 'IU콘서트'와 매칭되지 않음)."""
    wrapped = literal(",") + Photo.tags + literal(",")
    return wrapped.like(f"%,{escape_like(tag)},%", escape="\\")


def parse_id_list(values: Iterable, limit: int = 1000) -> list[int]:
    ids = []
    for value in values:
        try:
            ids.append(int(str(value).strip()))
        except (TypeError, ValueError):
            continue
    return list(dict.fromkeys(ids))[:limit]


def sanitize_storage_path(path: str) -> str:
    path = path.replace("\\", "/")
    path = re.sub(r"/+", "/", path)
    return path.strip("/")


def make_relative_path(taken_date: date, filename: str, idx: int, base_ts: datetime) -> str:
    safe_name = secure_filename(filename or "")
    if not safe_name or "." not in safe_name:
        ext = Path(filename or "").suffix.lower()
        ext = ext if re.fullmatch(r"\.[a-z0-9]{1,5}", ext or "") else ".jpg"
        safe_name = f"file{ext}" if not safe_name else f"{safe_name}{ext}"
    stamp = base_ts.strftime("%Y%m%d%H%M%S%f")
    return sanitize_storage_path(f"{taken_date}/{stamp}_{idx}_{secrets.token_hex(3)}_{safe_name}")


def ensure_upload_folder() -> Path:
    upload_root = Path(app.config["UPLOAD_FOLDER"])
    try:
        upload_root.mkdir(parents=True, exist_ok=True)
    except OSError:
        # 읽기 전용 파일 시스템인 경우 /tmp 사용
        upload_root = Path("/tmp/uploads")
        upload_root.mkdir(parents=True, exist_ok=True)
    return upload_root


def local_file_path(relative_path: str) -> Optional[Path]:
    root = ensure_upload_folder().resolve()
    candidate = (root / relative_path).resolve()
    if root not in candidate.parents:
        return None
    return candidate


def save_file(relative_path: str, data: bytes, content_type: str):
    if use_gcs():
        if gcs_bucket is None:
            raise RuntimeError("GCS가 초기화되지 않았습니다.")
        blob = gcs_bucket.blob(relative_path)
        blob.upload_from_string(data, content_type=content_type)
    else:
        file_path = local_file_path(relative_path)
        if file_path is None:
            raise ValueError("잘못된 파일 경로입니다.")
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(data)


def read_file(relative_path: str) -> bytes:
    if use_gcs():
        if gcs_bucket is None:
            raise RuntimeError("GCS가 초기화되지 않았습니다.")
        return gcs_bucket.blob(relative_path).download_as_bytes()
    file_path = local_file_path(relative_path)
    if file_path is None or not file_path.exists():
        raise FileNotFoundError(relative_path)
    return file_path.read_bytes()


def delete_file(relative_path: str) -> bool:
    """스토리지에서 파일 삭제. 성공(또는 이미 없음) 시 True."""
    try:
        if use_gcs():
            if gcs_bucket is None:
                return False
            blob = gcs_bucket.blob(relative_path)
            try:
                blob.delete()
            except Exception as exc:
                if getattr(exc, "code", None) != 404:
                    raise
        else:
            file_path = local_file_path(relative_path)
            if file_path and file_path.exists():
                file_path.unlink()
                try:
                    if not any(file_path.parent.iterdir()):
                        file_path.parent.rmdir()
                except OSError:
                    pass
        return True
    except Exception as exc:
        app.logger.error(f"파일 삭제 실패 ({relative_path}): {exc}")
        return False


def reject_photo(photo: Photo) -> bool:
    ok = delete_file(photo.relative_path)
    for path in (photo.thumb_path, photo.display_path):
        if path:
            delete_file(path)
    photo.thumb_path = photo.display_path = None
    photo.status = "rejected"
    return ok


# ---------------------------------------------------------------------------
# 파생 이미지 (썸네일 / 확대 보기용 WebP)
# ---------------------------------------------------------------------------
#
# - 업로드 완료·승인 시점에 시간 예산(DERIVATIVE_TIME_BUDGET) 안에서 생성
# - 못 만든 건 /photos/<id>/thumb 를 처음 요청할 때 생성 (예전 사진도 자동으로 채워짐)
# - 승인된 사진의 파생 이미지는 고정 URL + `Cache-Control: public, immutable` 로 내려가
#   Vercel CDN과 브라우저에 캐시됨 → 함수 호출/GCS 전송은 사진당 사실상 한 번

VARIANT_COLUMNS = {"thumb": "thumb_path", "display": "display_path"}
PUBLIC_IMMUTABLE = "public, max-age=31536000, s-maxage=31536000, immutable"
_derive_executor = ThreadPoolExecutor(max_workers=app.config["DERIVATIVE_WORKERS"])


def derivative_path(relative_path: str, kind: str) -> str:
    return f"derived/v{DERIVATIVE_VERSION}/{kind}/{relative_path}.webp"


def _derive_job(relative_path: str, data: Optional[bytes]) -> dict:
    """(스레드에서 실행) 원본을 읽어 파생 이미지를 만들고 저장. DB는 건드리지 않음."""
    result = make_derivatives(data if data is not None else read_file(relative_path))
    paths = {}
    for kind, content in result.files.items():
        path = derivative_path(relative_path, kind)
        save_file(path, content, "image/webp")
        paths[VARIANT_COLUMNS[kind]] = path
    return {**paths, "width": result.width, "height": result.height}


def needs_derivatives(photo: Photo) -> bool:
    return photo.status != "rejected" and not photo.thumb_path and not photo.derive_failed


def generate_derivatives(photos: Iterable[Photo], budget: Optional[float] = None,
                         data_by_path: Optional[dict] = None) -> int:
    """시간 예산 안에서 파생 이미지를 만들고 DB에 반영. 처리(성공+영구 실패)한 개수를 반환."""
    targets = [p for p in photos if needs_derivatives(p)]
    if not targets:
        return 0
    budget = app.config["DERIVATIVE_TIME_BUDGET"] if budget is None else budget
    data_by_path = data_by_path or {}
    futures = {
        _derive_executor.submit(_derive_job, p.relative_path, data_by_path.get(p.relative_path)): p
        for p in targets
    }
    done, not_done = wait(futures, timeout=budget)
    handled = 0
    for future in done:
        photo = futures[future]
        exc = future.exception()
        if exc is None:
            for key, value in future.result().items():
                setattr(photo, key, value)
            photo.derive_failed = False
            handled += 1
        elif isinstance(exc, ImageProcessingError):
            app.logger.warning(f"파생 이미지 생성 불가 (photo {photo.id}): {exc}")
            photo.derive_failed = True
            handled += 1
        else:
            # 네트워크 오류 등 일시적인 문제 → 다음에 다시 시도
            app.logger.error(f"파생 이미지 생성 실패 (photo {photo.id}): {exc}")
    if not_done:
        app.logger.info(f"파생 이미지 {len(not_done)}개는 시간 예산 초과로 다음에 처리")
    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        app.logger.error(f"파생 이미지 정보 저장 실패: {exc}")
        return 0
    return handled


def safe_generate_derivatives(photos, **kwargs) -> int:
    """업로드/승인 흐름을 절대 깨뜨리지 않도록 감싼 버전."""
    try:
        return generate_derivatives(photos, **kwargs)
    except Exception as exc:
        db.session.rollback()
        app.logger.error(f"파생 이미지 생성 중 오류: {exc}", exc_info=True)
        return 0


def variant_url(photo: Photo, kind: str) -> str:
    if photo.status == "rejected":
        return REJECTED_PLACEHOLDER
    return url_for("photo_variant", photo_id=photo.id, kind=kind, v=DERIVATIVE_VERSION)


def missing_derivatives_query():
    return Photo.query.filter(
        Photo.status != "rejected",
        Photo.thumb_path.is_(None),
        or_(Photo.derive_failed.is_(None), Photo.derive_failed.is_(False)),
    )


def signed_url(relative_path: str, method: str = "GET", **kwargs) -> str:
    blob = gcs_bucket.blob(relative_path)
    return blob.generate_signed_url(
        version="v4",
        expiration=timedelta(hours=app.config["SIGNED_URL_HOURS"]) if method == "GET" else timedelta(hours=1),
        method=method,
        **kwargs,
    )


def build_file_url(photo: Photo) -> str:
    # 거절된 사진은 스토리지에서 삭제되므로 기본 이미지 반환
    if photo.status == "rejected":
        return REJECTED_PLACEHOLDER
    if use_gcs():
        if gcs_bucket is None:
            return REJECTED_PLACEHOLDER
        return signed_url(photo.relative_path)
    return url_for("files", filename=photo.relative_path)


def download_filename(photo: Photo) -> str:
    ext = Path(photo.relative_path).suffix or ".jpg"
    base = re.sub(r'[\\/:*?"<>|\r\n]+', "_", photo.event_name or "photo").strip() or "photo"
    return f"{base}_{photo.id}{ext}"


# ---------------------------------------------------------------------------
# 인증 / 권한
# ---------------------------------------------------------------------------


def current_user() -> Optional[User]:
    uid = session.get("user_id")
    if not uid:
        return None
    return db.session.get(User, uid)


def _deny(message: str, status: int, category: str):
    if wants_json():
        return jsonify({"success": False, "error": message, "redirect": url_for("home")}), status
    flash(message, category)
    return redirect(url_for("home"))


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user():
            return _deny("세션이 만료되었습니다. 다시 로그인해주세요.", 401, "warning")
        return fn(*args, **kwargs)

    return wrapper


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user:
            return _deny("세션이 만료되었습니다. 다시 로그인해주세요.", 401, "warning")
        if not user.is_admin:
            return _deny("관리자만 접근 가능합니다.", 403, "error")
        return fn(*args, **kwargs)

    return wrapper


def can_view_photo(photo: Optional[Photo], user: Optional[User]) -> bool:
    if photo is None or photo.status == "rejected":
        return False
    if photo.status == "approved":
        return True
    return bool(user and (user.is_admin or user.id == photo.user_id))


# --- CSRF -------------------------------------------------------------------

CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}


def get_csrf_token() -> str:
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


@app.before_request
def csrf_protect():
    if request.method in CSRF_SAFE_METHODS or app.config.get("CSRF_DISABLED"):
        return None
    expected = session.get("_csrf_token")
    sent = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
    if expected and sent and hmac.compare_digest(str(sent), str(expected)):
        return None
    message = "보안 토큰이 만료되었습니다. 페이지를 새로고침한 뒤 다시 시도해주세요."
    if wants_json():
        # 401/403은 프론트에서 '세션 만료'로 처리하므로 400 사용
        return json_error(message, 400, error_type="csrf")
    flash(message, "error")
    return redirect(request.referrer or url_for("home"))


@app.context_processor
def inject_helpers():
    return {
        "file_url": build_file_url,
        "thumb_url": lambda photo: variant_url(photo, "thumb"),
        "display_url": lambda photo: variant_url(photo, "display"),
        "csrf_token": get_csrf_token,
    }


# ---------------------------------------------------------------------------
# 업로드 한도
# ---------------------------------------------------------------------------


def limits() -> dict:
    return {
        "max_files": app.config["DAILY_UPLOAD_LIMIT"],
        "max_day_bytes": app.config["DAILY_UPLOAD_MB"] * MB,
        "max_file_bytes": app.config["MAX_FILE_MB"] * MB,
    }


def get_today_usage(user: User) -> tuple[int, int]:
    """(오늘 업로드한 장수, 오늘 업로드한 총 바이트)"""
    start, end = today_bounds_utc()
    rows = (
        db.session.query(Photo.file_size, Photo.relative_path)
        .filter(Photo.user_id == user.id, Photo.created_at >= start, Photo.created_at < end)
        .all()
    )
    total = 0
    for file_size, relative_path in rows:
        if file_size:
            total += file_size
        elif not use_gcs():
            path = local_file_path(relative_path)
            if path and path.exists():
                total += path.stat().st_size
    return len(rows), total


def quota_error(user: User, usage: tuple[int, int], sizes: list[int], names: list[str]):
    """한도를 넘으면 (json 응답, status)를, 아니면 None을 반환."""
    if user.is_admin:
        return None
    lim = limits()
    today_count, today_size = usage

    if today_count + len(sizes) > lim["max_files"]:
        return json_error(
            f"오늘 업로드 한도에 도달했습니다. (최대 {lim['max_files']}장)",
            error_type="file_limit_exceeded",
            today_count=today_count,
            max_files=lim["max_files"],
            remaining=max(0, lim["max_files"] - today_count),
        )

    for size, name in zip(sizes, names):
        if size > lim["max_file_bytes"]:
            return json_error(
                f"파일 크기가 너무 큽니다. (최대 {app.config['MAX_FILE_MB']}MB)",
                error_type="file_size_exceeded",
                filename=name,
                file_size_mb=round(size / MB, 2),
                max_size_mb=app.config["MAX_FILE_MB"],
            )

    new_size = sum(sizes)
    if today_size + new_size > lim["max_day_bytes"]:
        remaining_mb = max(0, lim["max_day_bytes"] - today_size) / MB
        return json_error(
            f"오늘 업로드 용량 한도에 도달했습니다. (남은 용량: {remaining_mb:.1f}MB)",
            error_type="size_limit_exceeded",
            today_total_size_mb=round(today_size / MB, 2),
            new_size_mb=round(new_size / MB, 2),
            max_size_mb=app.config["DAILY_UPLOAD_MB"],
            remaining_mb=round(remaining_mb, 2),
        )
    return None


# ---------------------------------------------------------------------------
# 페이지
# ---------------------------------------------------------------------------


@app.route("/")
def home():
    return render_template("home.html", user=current_user())


@app.route("/archive")
def archive():
    approved = (
        Photo.query.filter_by(status="approved")
        .order_by(Photo.taken_date.desc(), Photo.created_at.desc())
        .all()
    )
    seven_days_ago = datetime.utcnow() - timedelta(days=7)
    new_count = sum(1 for p in approved if p.created_at and p.created_at >= seven_days_ago)

    # 년도 > 월 > 일 계층 구조로 그룹화
    years_dict: dict = {}
    for photo in approved:
        d = photo.taken_date
        years_dict.setdefault(d.year, {}).setdefault(d.month, {}).setdefault(d.day, []).append(photo)

    return render_template(
        "archive.html",
        years_dict=years_dict,
        user=current_user(),
        total_count=len(approved),
        new_count=new_count,
    )


@app.route("/upload", methods=["GET"])
@login_required
def upload():
    user = current_user()
    today_count, today_total_size = get_today_usage(user)
    lim = limits()
    if user.is_admin:
        remaining_files = remaining_size_mb = "무제한"
    else:
        remaining_files = max(0, lim["max_files"] - today_count)
        remaining_size_mb = max(0, lim["max_day_bytes"] - today_total_size) / MB

    return render_template(
        "upload.html",
        user=user,
        today_count=today_count,
        today_total_size=today_total_size,
        used_size_mb=today_total_size / MB,
        remaining_files=remaining_files,
        remaining_size_mb=remaining_size_mb,
        max_files=lim["max_files"],
        storage_backend=app.config["STORAGE_BACKEND"],
        max_size_mb=app.config["DAILY_UPLOAD_MB"],
        max_file_mb=app.config["MAX_FILE_MB"],
        accept_types=",".join(sorted(SUPPORTED_MIME_TYPES) + list(SUPPORTED_EXTENSIONS)),
        today=local_today(),
    )


@app.route("/admin/pending", methods=["GET", "POST"])
@admin_required
def admin_pending():
    if request.method == "POST":
        action = request.form.get("action")

        if action in ("batch_approve", "batch_reject"):
            photo_ids = parse_id_list(request.form.get("photo_ids", "").split(","))
            if not photo_ids:
                flash("선택한 사진이 없습니다.", "error")
                return redirect(url_for("admin_pending"))
            photos = Photo.query.filter(Photo.id.in_(photo_ids), Photo.status == "pending").all()
        elif action in ("approve", "reject"):
            photo = db.session.get(Photo, request.form.get("photo_id", type=int) or 0)
            photos = [photo] if photo and photo.status == "pending" else []
        else:
            flash("알 수 없는 요청입니다.", "error")
            return redirect(url_for("admin_pending"))

        if not photos:
            flash("처리할 사진을 찾을 수 없습니다.", "error")
            return redirect(url_for("admin_pending"))

        failed_deletes = 0
        for photo in photos:
            if action in ("approve", "batch_approve"):
                photo.status = "approved"
            elif not reject_photo(photo):
                failed_deletes += 1
        db.session.commit()
        if action in ("approve", "batch_approve"):
            # 업로드 때 못 만든 썸네일이 있으면 승인하면서 채움 (남은 건 첫 조회 때 생성)
            safe_generate_derivatives(photos)

        verb = "승인" if action in ("approve", "batch_approve") else "거절"
        flash(f"{len(photos)}개의 사진이 {verb}되었습니다.", "success")
        if failed_deletes:
            flash(f"{failed_deletes}개 파일은 스토리지에서 삭제하지 못했습니다. 로그를 확인해주세요.", "error")
        return redirect(url_for("admin_pending"))

    pending_photos = (
        Photo.query.filter_by(status="pending").order_by(Photo.created_at.desc()).all()
    )
    user_ids = {photo.user_id for photo in pending_photos}
    users_dict = {u.id: u for u in User.query.filter(User.id.in_(user_ids)).all()} if user_ids else {}

    # 업로드 날짜(서비스 시간대 기준)별 > 사용자별 그룹화
    photos_by_date = defaultdict(lambda: defaultdict(list))
    for photo in pending_photos:
        photos_by_date[to_local_date(photo.created_at)][photo.user_id].append(photo)

    organized_photos = []
    for upload_date in sorted(photos_by_date.keys(), reverse=True):
        group = {"date": upload_date, "users": [], "total_count": 0}
        for user_id, photos in photos_by_date[upload_date].items():
            if user_id in users_dict:
                group["users"].append({"user": users_dict[user_id], "photos": photos})
                group["total_count"] += len(photos)
        organized_photos.append(group)

    return render_template(
        "admin_pending.html",
        organized_photos=organized_photos,
        total_count=len(pending_photos),
        user=current_user(),
        missing_derivatives=missing_derivatives_query().count(),
    )


@app.route("/admin/derivatives", methods=["GET", "POST"])
@admin_required
def admin_derivatives():
    """썸네일이 없는 사진(예전 사진)을 조금씩 채우는 백필. POST 한 번에 최대 20개."""
    processed = 0
    if request.method == "POST":
        batch = missing_derivatives_query().order_by(Photo.id).limit(20).all()
        processed = generate_derivatives(batch)
    return jsonify({
        "success": True,
        "processed": processed,
        "remaining": missing_derivatives_query().count(),
        "failed": Photo.query.filter(Photo.derive_failed.is_(True), Photo.status != "rejected").count(),
    })


@app.cli.command("backfill-derivatives")
def backfill_derivatives_command():
    """flask --app app backfill-derivatives : 썸네일이 없는 모든 사진 처리"""
    ensure_schema()
    while True:
        batch = missing_derivatives_query().order_by(Photo.id).limit(50).all()
        if not batch:
            break
        handled = generate_derivatives(batch, budget=600)
        print(f"처리 {handled}개, 남은 {missing_derivatives_query().count()}개")
        if handled == 0:
            break


def to_local_date(value: Optional[datetime]) -> date:
    if not value:
        return local_today()
    return value.replace(tzinfo=timezone.utc).astimezone(APP_TZ).date()


@app.route("/profile")
@login_required
def profile():
    user = current_user()
    my_photos = Photo.query.filter_by(user_id=user.id).order_by(Photo.created_at.desc()).all()

    photos_by_date = defaultdict(list)
    for photo in my_photos:
        photos_by_date[to_local_date(photo.created_at)].append(photo)

    organized_photos = [
        {"date": d, "photos": photos_by_date[d], "total_count": len(photos_by_date[d])}
        for d in sorted(photos_by_date.keys(), reverse=True)
    ]
    return render_template(
        "profile.html", user=user, organized_photos=organized_photos, total_count=len(my_photos)
    )


# ---------------------------------------------------------------------------
# 파일 제공
# ---------------------------------------------------------------------------


@app.route("/files/<path:filename>")
def files(filename):
    """로컬 스토리지 전용. 승인된 사진 또는 본인/관리자만 접근 가능."""
    if use_gcs():
        abort(404)
    photo = Photo.query.filter_by(relative_path=filename).first()
    if not can_view_photo(photo, current_user()):
        abort(404)
    return send_from_directory(ensure_upload_folder(), photo.relative_path)


@app.route("/photos/<int:photo_id>/image")
def photo_image(photo_id):
    """항상 유효한 이미지 주소. 요청 시점에 새 서명 URL로 리디렉션 (만료 문제 없음)."""
    photo = db.session.get(Photo, photo_id)
    if not can_view_photo(photo, current_user()):
        abort(404)
    if use_gcs():
        if gcs_bucket is None:
            abort(503)
        resp = redirect(signed_url(photo.relative_path))
        resp.headers["Cache-Control"] = "private, max-age=1800"
        return resp
    return send_from_directory(ensure_upload_folder(), photo.relative_path)


def original_response(photo: Photo):
    """파생 이미지를 쓸 수 없을 때 원본으로 대체 (캐시 금지: 서명 URL은 만료됨)."""
    if use_gcs():
        if gcs_bucket is None:
            abort(503)
        resp = redirect(signed_url(photo.relative_path))
    else:
        resp = send_from_directory(ensure_upload_folder(), photo.relative_path)
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


@app.route("/photos/<int:photo_id>/<any(thumb, display):kind>")
def photo_variant(photo_id, kind):
    """썸네일/확대 이미지 (WebP).

    승인된 사진은 세션을 전혀 읽지 않고 public/immutable 로 응답한다.
    (세션을 읽으면 Flask가 `Vary: Cookie`를 붙여 CDN이 사용자마다 따로 캐시하게 됨)
    """
    photo = db.session.get(Photo, photo_id)
    if photo is None or photo.status == "rejected":
        abort(404)
    is_public = photo.status == "approved"
    if not is_public and not can_view_photo(photo, current_user()):
        abort(404)

    if needs_derivatives(photo):
        safe_generate_derivatives([photo])

    path = getattr(photo, VARIANT_COLUMNS[kind])
    if not path:
        return original_response(photo)
    try:
        data = read_file(path)
    except Exception as exc:
        # 파생 파일이 사라졌으면 다음 요청에서 다시 만들도록 초기화
        app.logger.warning(f"파생 이미지 읽기 실패 ({path}): {exc}")
        photo.thumb_path = photo.display_path = None
        db.session.commit()
        return original_response(photo)

    resp = Response(data, mimetype="image/webp")
    resp.headers["Cache-Control"] = PUBLIC_IMMUTABLE if is_public else "private, max-age=3600"
    return resp


@app.route("/photos/<int:photo_id>/download")
def photo_download(photo_id):
    """다운로드 카운트 증가 후 첨부파일(Content-Disposition: attachment)로 내려줌."""
    photo = db.session.get(Photo, photo_id)
    if photo is None or photo.status != "approved":
        abort(404)

    # 같은 세션에서 같은 사진을 반복 다운로드해도 한 번만 카운트
    recent = session.get("dl", [])
    if photo.id not in recent:
        Photo.query.filter_by(id=photo.id).update(
            {Photo.download_count: func.coalesce(Photo.download_count, 0) + 1},
            synchronize_session=False,
        )
        db.session.commit()
        session["dl"] = (recent + [photo.id])[-100:]

    name = download_filename(photo)
    if use_gcs():
        if gcs_bucket is None:
            abort(503)
        return redirect(
            signed_url(
                photo.relative_path,
                response_disposition=f"attachment; filename*=UTF-8''{quote(name)}",
            )
        )
    return send_from_directory(
        ensure_upload_folder(), photo.relative_path, as_attachment=True, download_name=name
    )


# ---------------------------------------------------------------------------
# 아카이브 API
# ---------------------------------------------------------------------------


def approved_query_with_tags():
    query = Photo.query.filter(Photo.status == "approved")
    tags = split_tags(request.args.get("tags"))
    if tags:
        # 선택한 태그 중 하나라도 포함된 사진 (OR)
        query = query.filter(or_(*[tag_exact_match(tag) for tag in tags]))
    return query


@app.route("/api/archive/photos")
def api_archive_photos():
    """무한 스크롤용 사진 API. sort=random 일 때는 seed로 순서를 고정해 페이지 간 중복/누락이 없음."""
    page = max(1, request.args.get("page", 1, type=int) or 1)
    per_page = min(100, max(1, request.args.get("per_page", 30, type=int) or 30))
    sort_by = request.args.get("sort", "random")
    if sort_by not in ("random", "date", "downloads"):
        sort_by = "random"
    seed = request.args.get("seed", type=int)

    query = approved_query_with_tags()
    start = (page - 1) * per_page

    if sort_by == "random":
        if seed is None:
            seed = random.randrange(1, 2**31)
        ids = [row[0] for row in query.with_entities(Photo.id).order_by(Photo.id).all()]
        random.Random(seed).shuffle(ids)
        total = len(ids)
        page_ids = ids[start:start + per_page]
        by_id = {p.id: p for p in Photo.query.filter(Photo.id.in_(page_ids)).all()} if page_ids else {}
        photos = [by_id[i] for i in page_ids if i in by_id]
    else:
        if sort_by == "date":
            order = (Photo.taken_date.desc(), Photo.created_at.desc(), Photo.id.desc())
        else:
            order = (func.coalesce(Photo.download_count, 0).desc(), Photo.taken_date.desc(), Photo.id.desc())
        total = query.count()
        photos = query.order_by(*order).offset(start).limit(per_page).all()

    return jsonify({
        "photos": [
            {
                "id": photo.id,
                "url": variant_url(photo, "display"),  # 확대 보기용
                "thumb_url": variant_url(photo, "thumb"),  # 갤러리 그리드용
                "width": photo.width,
                "height": photo.height,
                "event_name": photo.event_name or "기타",
                "taken_date": photo.taken_date.isoformat() if photo.taken_date else None,
                "tags": split_tags(photo.tags),
                "download_count": photo.download_count or 0,
            }
            for photo in photos
        ],
        "has_more": start + per_page < total,
        "page": page,
        "total": total,
        "seed": seed,
    })


@app.route("/api/archive/tags")
def api_archive_tags():
    """승인된 사진의 태그 목록과 태그별 사진 수 (쿼리 1번)."""
    rows = (
        db.session.query(Photo.tags)
        .filter(Photo.status == "approved", Photo.tags.isnot(None))
        .all()
    )
    counter = Counter()
    for (tags,) in rows:
        counter.update(set(split_tags(tags)))
    sorted_tags = sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    return jsonify({"tags": [{"name": tag, "count": count} for tag, count in sorted_tags]})


# ---------------------------------------------------------------------------
# 업로드 API
# ---------------------------------------------------------------------------


def visible_photos_on(target_date: date, user: User):
    """추천 목록용: 승인된 사진 + 내가 올린 사진만 (다른 사람의 미승인 정보 노출 방지)."""
    return db.session.query(Photo.event_name, Photo.tags).filter(
        Photo.taken_date == target_date,
        or_(Photo.status == "approved", Photo.user_id == user.id),
    )


@app.route("/api/upload/event-names")
@login_required
def api_upload_event_names():
    target_date = parse_date(request.args.get("date"))
    if not target_date:
        return jsonify({"event_names": []}), 400
    names = {
        name.strip()
        for name, _ in visible_photos_on(target_date, current_user()).all()
        if name and name.strip()
    }
    return jsonify({"event_names": sorted(names), "date": target_date.isoformat()})


@app.route("/api/upload/tags")
@login_required
def api_upload_tags():
    target_date = parse_date(request.args.get("date"))
    if not target_date:
        return jsonify({"tags": []}), 400
    all_tags = set()
    for _, tags in visible_photos_on(target_date, current_user()).all():
        all_tags.update(split_tags(tags))
    return jsonify({"tags": sorted(all_tags), "date": target_date.isoformat()})


@app.route("/api/upload/presigned", methods=["POST"])
@login_required
def api_upload_presigned():
    """GCS 직접 업로드용 presigned URL + 서명된 업로드 토큰 발급."""
    if not use_gcs() or gcs_bucket is None:
        return json_error("GCS를 사용하지 않는 환경에서는 presigned URL을 사용할 수 없습니다.")

    user = current_user()
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return json_error("요청 데이터가 없습니다.")

    taken_date_value = parse_date(data.get("taken_date"), default=local_today())
    if not taken_date_value:
        return json_error("날짜 형식이 올바르지 않습니다.")
    event_name = normalize_event_name(data.get("event_name"))
    tags = normalize_tags(data.get("tags"))

    files_info = data.get("files")
    if not isinstance(files_info, list) or not files_info:
        return json_error("파일 정보가 없습니다.")
    if len(files_info) > app.config["MAX_FILES_PER_REQUEST"]:
        return json_error(f"한 번에 최대 {app.config['MAX_FILES_PER_REQUEST']}개까지 업로드할 수 있습니다.")

    cleaned = []
    for idx, info in enumerate(files_info):
        if not isinstance(info, dict):
            return json_error("잘못된 파일 정보입니다.")
        name = str(info.get("name") or f"file_{idx}")[:255]
        try:
            size = int(info.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        content_type = str(info.get("type") or "").lower()
        if size <= 0:
            return json_error(f"빈 파일은 업로드할 수 없습니다. ({name})")
        if not is_allowed_image_type(content_type):
            return json_error(
                f"{unsupported_reason(name=name, content_type=content_type)} ({name})",
                error_type="invalid_type",
            )
        cleaned.append((name, size, content_type))

    # 선검사 (최종 검증은 /api/upload/complete 에서 실제 파일 크기로 다시 함)
    error = quota_error(user, get_today_usage(user), [c[1] for c in cleaned], [c[0] for c in cleaned])
    if error:
        return error

    base_ts = datetime.utcnow()
    presigned_urls = []
    for idx, (name, size, content_type) in enumerate(cleaned):
        relative_path = make_relative_path(taken_date_value, name, idx, base_ts)
        url = signed_url(relative_path, method="PUT", content_type=content_type)
        token = upload_token_serializer.dumps({
            "u": user.id,
            "p": relative_path,
            "n": name,
            "t": content_type,
            "e": event_name,
            "d": taken_date_value.isoformat(),
            "g": tags,
        })
        presigned_urls.append({
            "url": url,
            "token": token,
            "path": relative_path,
            "filename": name,
            "size": size,
            "content_type": content_type,
        })

    return jsonify({
        "success": True,
        "presigned_urls": presigned_urls,
        "event_name": event_name,
        "taken_date": taken_date_value.isoformat(),
        "tags": tags,
    })


@app.route("/api/upload/complete", methods=["POST"])
@login_required
def api_upload_complete():
    """GCS 업로드 완료 처리.

    클라이언트가 보낸 경로/크기는 믿지 않는다:
    - 서버가 서명한 토큰만 받고 (본인 것인지, 만료되지 않았는지 확인)
    - GCS에서 실제 파일 크기/타입을 읽어 한도를 다시 검사하고
    - 한도를 넘거나 이미지가 아니면 GCS에서 삭제한다.
    """
    if not use_gcs() or gcs_bucket is None:
        return json_error("GCS를 사용하지 않는 환경입니다.")

    user = current_user()
    data = request.get_json(silent=True) or {}
    tokens = data.get("tokens")
    if not isinstance(tokens, list) or not tokens:
        return json_error("업로드된 파일 정보가 없습니다.")
    if len(tokens) > app.config["MAX_FILES_PER_REQUEST"]:
        return json_error("한 번에 처리할 수 있는 파일 수를 초과했습니다.")

    lim = limits()
    today_count, today_size = get_today_usage(user)
    seen_paths = set()
    accepted, rejected = [], []

    for token in tokens:
        try:
            payload = upload_token_serializer.loads(str(token), max_age=UPLOAD_TOKEN_MAX_AGE)
        except SignatureExpired:
            rejected.append({"filename": None, "reason": "업로드 시간이 만료되었습니다."})
            continue
        except BadSignature:
            rejected.append({"filename": None, "reason": "잘못된 업로드 정보입니다."})
            continue

        path, name = payload.get("p"), payload.get("n")
        if payload.get("u") != user.id or not path or path in seen_paths:
            rejected.append({"filename": name, "reason": "잘못된 업로드 정보입니다."})
            continue
        seen_paths.add(path)

        if Photo.query.filter_by(relative_path=path).first():
            rejected.append({"filename": name, "reason": "이미 등록된 파일입니다."})
            continue

        blob = gcs_bucket.get_blob(path)
        if blob is None:
            rejected.append({"filename": name, "reason": "업로드된 파일을 찾을 수 없습니다."})
            continue

        size = int(blob.size or 0)
        reason = None
        try:
            head = blob.download_as_bytes(start=0, end=SNIFF_BYTES - 1)
        except Exception as exc:
            app.logger.warning(f"업로드 파일 형식 확인 실패 ({path}): {exc}")
            head = b""
        if not is_allowed_image_type(blob.content_type) or not is_supported_content(head):
            reason = unsupported_reason(sniff_image_format(head), name, blob.content_type or "")
        elif not user.is_admin:
            if size > lim["max_file_bytes"]:
                reason = f"파일 크기가 {app.config['MAX_FILE_MB']}MB를 초과합니다."
            elif today_count + 1 > lim["max_files"]:
                reason = "오늘 업로드 장수 한도를 초과했습니다."
            elif today_size + size > lim["max_day_bytes"]:
                reason = "오늘 업로드 용량 한도를 초과했습니다."
        if reason:
            delete_file(path)
            rejected.append({"filename": name, "reason": reason})
            continue

        today_count += 1
        today_size += size
        accepted.append(Photo(
            user_id=user.id,
            relative_path=path,
            original_name=name,
            event_name=normalize_event_name(payload.get("e")),
            taken_date=parse_date(payload.get("d"), default=local_today()),
            tags=normalize_tags(payload.get("g")),
            status="pending",
            file_size=size,
        ))

    if accepted:
        db.session.add_all(accepted)
        db.session.commit()
        # 관리자 화면이 원본 대신 가벼운 썸네일을 보도록 바로 생성 (시간 예산 내에서만)
        safe_generate_derivatives(accepted)

    if not accepted:
        return json_error(
            rejected[0]["reason"] if rejected else "업로드된 파일이 없습니다.",
            error_type="upload_rejected",
            rejected=rejected,
        )

    message = f"{len(accepted)}개의 파일이 업로드되었습니다."
    if rejected:
        message += f" ({len(rejected)}개는 처리되지 않았습니다: {rejected[0]['reason']})"
    return jsonify({"success": True, "message": message, "count": len(accepted), "rejected": rejected})


@app.route("/api/upload", methods=["POST"])
@login_required
def api_upload():
    """서버 경유 업로드 (로컬 스토리지용). 저장이 끝난 뒤에 응답한다."""
    user = current_user()

    taken_date_value = parse_date(request.form.get("taken_date"), default=local_today())
    if not taken_date_value:
        return json_error("날짜 형식이 올바르지 않습니다.")
    event_name = normalize_event_name(request.form.get("event_name"))
    tags = normalize_tags(request.form.get("tags"))

    files = [f for f in request.files.getlist("photos") if f and f.filename]
    if not files:
        return json_error("파일을 선택해주세요.")
    if len(files) > app.config["MAX_FILES_PER_REQUEST"]:
        return json_error(f"한 번에 최대 {app.config['MAX_FILES_PER_REQUEST']}개까지 업로드할 수 있습니다.")

    file_items = []
    for f in files:
        data = f.read()
        if not data:
            return json_error(f"빈 파일은 업로드할 수 없습니다. ({f.filename})")
        if not is_allowed_image_type(f.mimetype) or not is_supported_content(data[:SNIFF_BYTES]):
            reason = unsupported_reason(sniff_image_format(data[:SNIFF_BYTES]), f.filename, f.mimetype or "")
            return json_error(f"{reason} ({f.filename})", error_type="invalid_type")
        file_items.append((f.filename, data, f.mimetype))

    error = quota_error(
        user, get_today_usage(user), [len(d) for _, d, _ in file_items], [n for n, _, _ in file_items]
    )
    if error:
        return error

    base_ts = datetime.utcnow()
    saved_paths = []
    new_photos = []
    data_by_path = {}
    try:
        for idx, (name, data, content_type) in enumerate(file_items):
            relative_path = make_relative_path(taken_date_value, name, idx, base_ts)
            save_file(relative_path, data, content_type)
            saved_paths.append(relative_path)
            data_by_path[relative_path] = data
            new_photos.append(Photo(
                user_id=user.id,
                relative_path=relative_path,
                original_name=name[:255],
                event_name=event_name,
                taken_date=taken_date_value,
                tags=tags,
                status="pending",
                file_size=len(data),
            ))
        db.session.add_all(new_photos)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        for path in saved_paths:
            delete_file(path)
        app.logger.error(f"업로드 실패: {exc}", exc_info=True)
        return json_error("파일 저장 중 오류가 발생했습니다.", 500)

    safe_generate_derivatives(new_photos, data_by_path=data_by_path)

    return jsonify({
        "success": True,
        "message": f"{len(file_items)}개의 파일이 업로드되었습니다.",
        "count": len(file_items),
    })


@app.errorhandler(413)
def too_large(_):
    if wants_json():
        return json_error("요청 크기가 너무 큽니다.", 413, error_type="request_too_large")
    flash("요청 크기가 너무 큽니다.", "error")
    return redirect(url_for("home"))


# ---------------------------------------------------------------------------
# 로그인
# ---------------------------------------------------------------------------

oauth = OAuth(app)
oauth.register(
    name="google",
    client_id=os.getenv("GOOGLE_CLIENT_ID"),
    client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)


@app.route("/login")
def login():
    redirect_uri = url_for("auth_callback", _external=True)
    return oauth.google.authorize_redirect(redirect_uri)


@app.route("/auth/callback")
def auth_callback():
    try:
        token = oauth.google.authorize_access_token()
    except Exception as exc:
        app.logger.warning(f"OAuth 콜백 실패: {exc}")
        flash("구글 로그인에 실패했습니다. 다시 시도해주세요.", "error")
        return redirect(url_for("home"))

    userinfo = token.get("userinfo")
    if not userinfo or not userinfo.get("email") or not userinfo.get("email_verified", False):
        flash("구글 로그인에 실패했습니다. (인증된 이메일이 필요합니다)", "error")
        return redirect(url_for("home"))

    email = userinfo["email"].strip()
    admin_emails = {e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()}

    user = User.query.filter(func.lower(User.email) == email.lower()).first()
    if not user:
        user = User(
            email=email,
            name=userinfo.get("name"),
            picture=userinfo.get("picture"),
            is_admin=email.lower() in admin_emails,
        )
        db.session.add(user)
    else:
        user.name = userinfo.get("name")
        user.picture = userinfo.get("picture")
        # 관리자 권한은 환경변수가 설정되어 있을 때만 동기화 (없으면 기존 값 유지)
        if admin_emails:
            user.is_admin = email.lower() in admin_emails
    db.session.commit()

    # 세션 고정 공격 방지: 로그인 시 세션 초기화
    session.clear()
    session["user_id"] = user.id
    flash("로그인되었습니다.", "success")
    return redirect(url_for("home"))


@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    flash("로그아웃되었습니다.", "success")
    return redirect(url_for("home"))


if __name__ == "__main__":
    with app.app_context():
        ensure_schema()
    app.run(host="0.0.0.0", port=8000, debug=True)
