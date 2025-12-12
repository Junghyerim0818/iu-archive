import os
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from functools import wraps
from pathlib import Path
from typing import Optional

from authlib.integrations.flask_client import OAuth
from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
    send_from_directory,
    jsonify,
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.pool import NullPool
from sqlalchemy import inspect, text
from werkzeug.utils import secure_filename
from google.cloud import storage
from google.oauth2 import service_account
import re

BASE_DIR = Path(__file__).parent
IS_SERVERLESS = bool(os.getenv("VERCEL") or os.getenv("AWS_LAMBDA_FUNCTION_NAME"))

# 서버리스에서는 /tmp 만 쓰기 가능하므로 기본 DB 경로를 /tmp로 변경
default_sqlite = (
    f"sqlite:////tmp/iu_archive.db" if IS_SERVERLESS else f"sqlite:///{BASE_DIR / 'iu_archive.db'}"
)

app = Flask(__name__)
engine_options = {}
postgres_uri = os.getenv("DATABASE_URL", default_sqlite)
if postgres_uri.startswith("postgres") and IS_SERVERLESS:
    # 서버리스 + Postgres(Neon 등)에서는 연결 풀을 끄고 pre_ping을 켭니다.
    engine_options = {"poolclass": NullPool, "pool_pre_ping": True}

app.config.update(
    SECRET_KEY=os.getenv("SECRET_KEY", "dev-secret"),
    SQLALCHEMY_DATABASE_URI=postgres_uri,
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    SQLALCHEMY_ENGINE_OPTIONS=engine_options,
    UPLOAD_FOLDER=BASE_DIR / "uploads",
    DAILY_UPLOAD_LIMIT=int(os.getenv("DAILY_UPLOAD_LIMIT", "10")),
    STORAGE_BACKEND=os.getenv("STORAGE_BACKEND", "local"),  # local or gcs
    GCS_BUCKET=os.getenv("GCS_BUCKET"),
    GCS_PROJECT=os.getenv("GCS_PROJECT"),
    GCS_CREDENTIALS=os.getenv("GCS_CREDENTIALS"),  # JSON 키 파일 경로 또는 JSON 문자열
)

db = SQLAlchemy(app)
gcs_client = None
gcs_bucket = None

# 백그라운드 작업을 위한 스레드 풀
upload_executor = ThreadPoolExecutor(max_workers=4)
if app.config["STORAGE_BACKEND"] == "gcs":
    try:
        # 서비스 계정 키 파일 경로 또는 JSON 문자열로 인증
        if app.config["GCS_CREDENTIALS"]:
            # JSON 문자열인지 파일 경로인지 확인
            if app.config["GCS_CREDENTIALS"].startswith("{"):
                # JSON 문자열
                credentials_info = json.loads(app.config["GCS_CREDENTIALS"])
                credentials = service_account.Credentials.from_service_account_info(credentials_info)
            else:
                # 파일 경로
                credentials = service_account.Credentials.from_service_account_file(
                    app.config["GCS_CREDENTIALS"]
                )
            gcs_client = storage.Client(project=app.config["GCS_PROJECT"], credentials=credentials)
        else:
            # 환경 변수에서 자동으로 인증 (GOOGLE_APPLICATION_CREDENTIALS 등)
            gcs_client = storage.Client(project=app.config["GCS_PROJECT"])
        
        gcs_bucket = gcs_client.bucket(app.config["GCS_BUCKET"])
    except Exception as e:
        app.logger.error(f"Google Cloud Storage 초기화 실패: {e}")
        gcs_client = None
        gcs_bucket = None


def ensure_schema():
    """
    DB 스키마 보장.
    - SQLite: 경로 생성 후 create_all
    - 그 외(Postgres/Neon 등): create_all만 수행
    """
    uri = app.config["SQLALCHEMY_DATABASE_URI"]
    if uri.startswith("sqlite:"):
        db_path = Path(uri.replace("sqlite:///", ""))
        db_path.parent.mkdir(parents=True, exist_ok=True)
    
    try:
        app.logger.info("스키마 생성 시작... (DB: %s)", "PostgreSQL" if uri.startswith("postgres") else "SQLite")
        db.create_all()
        app.logger.info("스키마 생성 완료")
    except Exception as exc:
        app.logger.error("스키마 생성 실패: %s", exc, exc_info=True)
        raise


# 스키마 초기화 플래그 (한 번만 실행)
_schema_initialized = False


def check_tables_exist():
    """필수 테이블(User, Photo)이 존재하는지 확인 - 실제 쿼리로 확인"""
    try:
        # 간단한 쿼리로 테이블 존재 확인
        db.session.execute(text('SELECT 1 FROM "user" LIMIT 1'))
        db.session.execute(text("SELECT 1 FROM photo LIMIT 1"))
        db.session.rollback()  # 쿼리만 확인하고 롤백
        return True
    except Exception as exc:
        db.session.rollback()
        # 테이블이 없으면 ProgrammingError 또는 OperationalError 발생
        return False


def ensure_photo_columns():
    """Photo 테이블에 필요한 컬럼이 있는지 확인하고 없으면 추가"""
    try:
        uri = app.config["SQLALCHEMY_DATABASE_URI"]
        columns_to_check = ['file_size', 'tags', 'download_count']
        
        if uri.startswith("postgres"):
            # PostgreSQL: 컬럼 존재 여부 확인
            for col_name in columns_to_check:
                result = db.session.execute(text(f"""
                    SELECT column_name 
                    FROM information_schema.columns 
                    WHERE table_name = 'photo' AND column_name = '{col_name}'
                """))
                if not result.fetchone():
                    if col_name == 'file_size':
                        db.session.execute(text("ALTER TABLE photo ADD COLUMN file_size INTEGER"))
                    elif col_name == 'tags':
                        db.session.execute(text("ALTER TABLE photo ADD COLUMN tags VARCHAR(512)"))
                    elif col_name == 'download_count':
                        db.session.execute(text("ALTER TABLE photo ADD COLUMN download_count INTEGER DEFAULT 0"))
                    db.session.commit()
                    app.logger.info(f"Photo 테이블에 {col_name} 컬럼 추가 완료")
        elif uri.startswith("sqlite"):
            # SQLite: 컬럼 존재 여부 확인
            result = db.session.execute(text("PRAGMA table_info(photo)"))
            columns = [row[1] for row in result.fetchall()]
            for col_name in columns_to_check:
                if col_name not in columns:
                    if col_name == 'file_size':
                        db.session.execute(text("ALTER TABLE photo ADD COLUMN file_size INTEGER"))
                    elif col_name == 'tags':
                        db.session.execute(text("ALTER TABLE photo ADD COLUMN tags VARCHAR(512)"))
                    elif col_name == 'download_count':
                        db.session.execute(text("ALTER TABLE photo ADD COLUMN download_count INTEGER DEFAULT 0"))
                    db.session.commit()
                    app.logger.info(f"Photo 테이블에 {col_name} 컬럼 추가 완료")
    except Exception as e:
        db.session.rollback()
        app.logger.warning(f"컬럼 확인/추가 실패 (이미 존재할 수 있음): {e}")


@app.before_request
def ensure_schema_before_request():
    """모든 요청 전에 스키마가 존재하는지 확인하고 없으면 생성"""
    global _schema_initialized
    
    # AUTO_DDL=0이면 스키마 생성을 건너뜀
    if os.getenv("AUTO_DDL", "1") == "0":
        if not _schema_initialized:
            _schema_initialized = True
        return
    
    # 이미 초기화되었고 테이블이 존재하면 건너뜀
    if _schema_initialized:
        try:
            if check_tables_exist():
                return
            else:
                # 테이블이 없으면 다시 초기화 시도
                app.logger.warning("테이블이 없어 재초기화 시도")
                _schema_initialized = False
        except Exception:
            pass
    
    try:
        # 테이블이 이미 존재하는지 확인
        if not check_tables_exist():
            app.logger.info("테이블이 없어 스키마 생성 시작...")
            ensure_schema()
            # 생성 후 다시 확인
            if check_tables_exist():
                app.logger.info("DB 스키마가 성공적으로 생성되었습니다.")
                _schema_initialized = True
            else:
                app.logger.error("스키마 생성 후에도 테이블이 없습니다.")
                _schema_initialized = False  # 재시도 가능하도록
        else:
            app.logger.debug("테이블이 이미 존재합니다.")
            # 기존 테이블에 필요한 컬럼이 있는지 확인하고 없으면 추가
            ensure_photo_columns()
            _schema_initialized = True
    except Exception as exc:
        app.logger.error("요청 전 스키마 생성 실패: %s", exc, exc_info=True)
        _schema_initialized = False  # 실패 시 재시도 가능하도록


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    name = db.Column(db.String(255))
    picture = db.Column(db.String(512))
    is_admin = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Photo(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    relative_path = db.Column(db.String(512), nullable=False)
    original_name = db.Column(db.String(255))
    event_name = db.Column(db.String(255))  # 파일명 저장 (하위 호환성 유지)
    taken_date = db.Column(db.Date, nullable=False)  # 업로드일 저장 (하위 호환성 유지)
    tags = db.Column(db.String(512))  # 태그 (쉼표로 구분)
    status = db.Column(db.String(32), default="pending")  # pending/approved/rejected
    file_size = db.Column(db.Integer)  # 파일 크기 (bytes)
    download_count = db.Column(db.Integer, default=0)  # 다운로드 횟수
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User", backref="photos")


# Visitor 모델 제거됨 - 방문자 기록 기능 비활성화


def current_user() -> Optional[User]:
    uid = session.get("user_id")
    if not uid:
        return None
    return db.session.get(User, uid)


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user():
            # AJAX 요청인 경우 JSON 응답 반환
            if request.is_json or request.path.startswith("/api/") or request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return jsonify({"success": False, "error": "세션이 만료되었습니다.", "redirect": url_for("home")}), 401
            # 일반 요청인 경우 홈으로 리디렉션
            flash("세션이 만료되었습니다. 다시 로그인해주세요.", "warning")
            return redirect(url_for("home"))
        return fn(*args, **kwargs)

    return wrapper


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user:
            # AJAX 요청인 경우 JSON 응답 반환
            if request.is_json or request.path.startswith("/api/") or request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return jsonify({"success": False, "error": "세션이 만료되었습니다.", "redirect": url_for("home")}), 401
            # 일반 요청인 경우 홈으로 리디렉션
            flash("세션이 만료되었습니다. 다시 로그인해주세요.", "warning")
            return redirect(url_for("home"))
        if not user.is_admin:
            # AJAX 요청인 경우 JSON 응답 반환
            if request.is_json or request.path.startswith("/api/") or request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return jsonify({"success": False, "error": "관리자만 접근 가능합니다.", "redirect": url_for("home")}), 403
            flash("관리자만 접근 가능합니다.", "error")
            return redirect(url_for("home"))
        return fn(*args, **kwargs)

    return wrapper


oauth = OAuth(app)
oauth.register(
    name="google",
    client_id=os.getenv("GOOGLE_CLIENT_ID"),
    client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)


def sanitize_gcs_path(path: str) -> str:
    """GCS blob 경로를 안전하게 정리하는 함수"""
    # 백슬래시를 슬래시로 변환
    path = path.replace("\\", "/")
    # 연속된 슬래시 제거
    path = re.sub(r"/+", "/", path)
    # 앞뒤 슬래시 제거
    path = path.strip("/")
    # GCS에서 허용되지 않는 문자 제거 또는 변환
    # GCS blob 이름은 UTF-8 문자를 지원하지만, 일부 특수 문자는 문제가 될 수 있음
    # 안전하게 URL 인코딩된 문자만 허용
    return path


def ensure_upload_folder() -> Path:
    """로컬 스토리지를 사용할 때만 폴더를 생성합니다."""
    if app.config["STORAGE_BACKEND"] == "gcs":
        # GCS를 사용할 때는 임시 경로만 반환 (실제로는 사용되지 않음)
        return Path("/tmp/uploads")
    
    upload_root = Path(app.config["UPLOAD_FOLDER"])
    try:
        upload_root.mkdir(parents=True, exist_ok=True)
    except OSError:
        # 읽기 전용 파일 시스템인 경우 /tmp 사용
        upload_root = Path("/tmp/uploads")
        upload_root.mkdir(parents=True, exist_ok=True)
    return upload_root


def process_file_upload_async(file_data: bytes, filename: str, relative_path: str, 
                              user_id: int, event_name: str, taken_date: date, 
                              tags: Optional[str], file_size: int, content_type: str):
    """백그라운드에서 파일을 실제로 저장하는 함수"""
    try:
        with app.app_context():
            if app.config["STORAGE_BACKEND"] == "gcs":
                # GCS 업로드
                blob_name = str(relative_path).replace("\\", "/")
                blob_name = sanitize_gcs_path(blob_name)
                
                # blob 이름 검증
                if not blob_name or len(blob_name) > 1024:
                    raise ValueError(f"Invalid blob name: {blob_name}")
                
                blob = gcs_bucket.blob(blob_name)
                blob.content_type = content_type or "application/octet-stream"
                from io import BytesIO
                blob.upload_from_file(BytesIO(file_data))
            else:
                # 로컬 스토리지 업로드
                upload_root = ensure_upload_folder()
                file_path = upload_root / relative_path
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_bytes(file_data)
            
            # DB에 기록
            photo_data = {
                "user_id": user_id,
                "relative_path": relative_path.replace("\\", "/"),
                "original_name": filename,
                "event_name": event_name,
                "taken_date": taken_date,
                "tags": tags,
                "status": "pending",
                "file_size": file_size,
            }
            photo = Photo(**photo_data)
            db.session.add(photo)
            db.session.commit()
            app.logger.info(f"파일 업로드 완료: {filename}")
    except Exception as e:
        error_msg = str(e)
        app.logger.error(f"파일 업로드 실패: {filename}, 오류: {error_msg}", exc_info=True)
        # GCS 관련 오류인 경우 더 자세한 정보 로깅
        if "pattern" in error_msg.lower() or "invalid" in error_msg.lower():
            app.logger.error(f"GCS blob 경로 문제 가능성: relative_path={relative_path}")
        db.session.rollback()


@app.route("/")
def home():
    # 랜덤 사진들을 모자이크용으로 가져오기
    import random
    all_approved = Photo.query.filter_by(status="approved").all()
    # 랜덤으로 최대 50개 선택 (없으면 빈 리스트)
    if len(all_approved) > 0:
        random_photos = random.sample(all_approved, min(len(all_approved), 50))
    else:
        random_photos = []
    
    return render_template("home.html", random_photos=random_photos, user=current_user())


@app.route("/archive")
def archive():
    approved = (
        Photo.query.filter_by(status="approved")
        .order_by(Photo.taken_date.desc(), Photo.created_at.desc())
        .all()
    )

    # 통계 계산
    total_count = len(approved)
    
    # 신규 사진: 최근 7일 내에 승인된 사진 (created_at 기준)
    from datetime import timedelta
    seven_days_ago = datetime.utcnow() - timedelta(days=7)
    new_count = Photo.query.filter_by(status="approved").filter(
        Photo.created_at >= seven_days_ago
    ).count()

    # 년도 > 월 > 일 계층 구조로 그룹화
    years_dict = {}
    for photo in approved:
        year = photo.taken_date.year
        month = photo.taken_date.month
        day = photo.taken_date.day
        
        if year not in years_dict:
            years_dict[year] = {}
        if month not in years_dict[year]:
            years_dict[year][month] = {}
        if day not in years_dict[year][month]:
            years_dict[year][month][day] = []
        
        years_dict[year][month][day].append(photo)

    return render_template(
        "archive.html", 
        years_dict=years_dict, 
        user=current_user(),
        total_count=total_count,
        new_count=new_count
    )


@app.route("/api/archive/photos")
def api_archive_photos():
    """무한 스크롤을 위한 사진 API (정렬 지원)"""
    import random
    from sqlalchemy import or_
    
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 30, type=int)
    tags_param = request.args.get("tags")  # 여러 태그 필터 (쉼표로 구분)
    sort_by = request.args.get("sort", "random")  # random, date, downloads
    
    # 모든 승인된 사진 가져오기
    query = Photo.query.filter_by(status="approved")
    
    # 태그 필터 적용 (여러 태그 중 하나라도 포함된 사진)
    if tags_param:
        tags_list = [tag.strip() for tag in tags_param.split(",") if tag.strip()]
        if tags_list:
            # OR 조건: 선택한 태그 중 하나라도 포함된 사진
            tag_filters = [Photo.tags.contains(tag) for tag in tags_list]
            query = query.filter(or_(*tag_filters))
    
    # 정렬 적용
    if sort_by == "date":
        # 날짜순 (최신순)
        query = query.order_by(Photo.taken_date.desc(), Photo.created_at.desc())
    elif sort_by == "downloads":
        # 다운로드순 (다운로드 많은 순)
        query = query.order_by(Photo.download_count.desc(), Photo.taken_date.desc())
    else:
        # 랜덤 (기본값)
        pass
    
    if sort_by == "random":
        all_approved = query.all()
        # 매번 랜덤으로 섞기
        shuffled = all_approved.copy()
        random.shuffle(shuffled)
        all_approved = shuffled
    else:
        all_approved = query.all()
    
    if not all_approved:
        return jsonify({
            "photos": [],
            "has_more": False,
            "page": page
        })
    
    # 페이지네이션
    start = (page - 1) * per_page
    end = start + per_page
    photos_page = all_approved[start:end]
    
    # 사진 데이터 포맷팅
    photos_data = []
    for photo in photos_page:
        tags_list = photo.tags.split(",") if photo.tags else []
        photos_data.append({
            "id": photo.id,
            "url": build_file_url(photo),
            "event_name": photo.event_name or "기타",
            "taken_date": photo.taken_date.isoformat() if photo.taken_date else None,
            "tags": tags_list,
            "download_count": photo.download_count or 0
        })
    
    has_more = end < len(all_approved)
    
    return jsonify({
        "photos": photos_data,
        "has_more": has_more,
        "page": page,
        "total": len(all_approved)
    })


@app.route("/api/upload/event-names")
@login_required
def api_upload_event_names():
    """특정 날짜에 등록된 행사명 목록 반환"""
    date_str = request.args.get("date")
    if not date_str:
        return jsonify({"event_names": []}), 400
    
    try:
        target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"event_names": []}), 400
    
    # 해당 날짜에 등록된 사진들의 행사명 목록 가져오기 (중복 제거)
    photos = Photo.query.filter(
        Photo.taken_date == target_date
    ).all()
    
    # 행사명 추출 및 중복 제거
    event_names = set()
    for photo in photos:
        if photo.event_name and photo.event_name.strip():
            event_names.add(photo.event_name.strip())
    
    # 정렬하여 반환
    event_names_list = sorted(list(event_names))
    
    return jsonify({
        "event_names": event_names_list,
        "date": target_date.isoformat()
    })


@app.route("/api/upload/tags")
@login_required
def api_upload_tags():
    """특정 날짜에 등록된 태그 목록 반환"""
    date_str = request.args.get("date")
    if not date_str:
        return jsonify({"tags": []}), 400
    
    try:
        target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"tags": []}), 400
    
    # 해당 날짜에 등록된 사진들의 태그 목록 가져오기
    photos = Photo.query.filter(
        Photo.taken_date == target_date
    ).all()
    
    # 태그 추출 및 중복 제거
    all_tags = set()
    for photo in photos:
        if photo.tags and photo.tags.strip():
            tags_list = [tag.strip() for tag in photo.tags.split(",") if tag.strip()]
            all_tags.update(tags_list)
    
    # 정렬하여 반환
    tags_list = sorted(list(all_tags))
    
    return jsonify({
        "tags": tags_list,
        "date": target_date.isoformat()
    })


@app.route("/api/archive/photos/download", methods=["POST"])
def api_archive_photos_download():
    """사진 다운로드 시 카운트 증가"""
    data = request.get_json()
    if not data or not data.get("photo_ids"):
        return jsonify({"success": False, "error": "사진 ID가 없습니다."}), 400
    
    photo_ids = data.get("photo_ids", [])
    if not isinstance(photo_ids, list):
        return jsonify({"success": False, "error": "잘못된 요청 형식입니다."}), 400
    
    try:
        # 각 사진의 다운로드 카운트 증가
        photos = Photo.query.filter(Photo.id.in_(photo_ids)).all()
        for photo in photos:
            photo.download_count = (photo.download_count or 0) + 1
        
        db.session.commit()
        return jsonify({"success": True, "count": len(photos)})
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"다운로드 카운트 증가 실패: {e}")
        return jsonify({"success": False, "error": "다운로드 카운트 증가 실패"}), 500


@app.route("/api/archive/tags")
def api_archive_tags():
    """사용 가능한 모든 태그 목록 반환"""
    # 승인된 사진의 모든 태그 수집
    approved_photos = Photo.query.filter_by(status="approved").filter(Photo.tags.isnot(None)).all()
    all_tags = set()
    
    for photo in approved_photos:
        if photo.tags:
            tags_list = [tag.strip() for tag in photo.tags.split(",") if tag.strip()]
            all_tags.update(tags_list)
    
    # 태그별 사진 개수 계산
    tag_counts = {}
    for tag in all_tags:
        count = Photo.query.filter_by(status="approved").filter(Photo.tags.contains(tag)).count()
        tag_counts[tag] = count
    
    # 사진 개수 순으로 정렬
    sorted_tags = sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)
    
    return jsonify({
        "tags": [{"name": tag, "count": count} for tag, count in sorted_tags]
    })


@app.route("/files/<path:filename>")
def files(filename):
    if app.config["STORAGE_BACKEND"] == "gcs":
        # 로컬 라우팅 불가, 아카이브에서 presigned URL을 직접 사용합니다.
        flash("파일은 Google Cloud Storage에서 제공됩니다.")
        return redirect(url_for("archive"))
    upload_root = ensure_upload_folder()
    return send_from_directory(upload_root, filename)


@app.route("/api/upload/presigned", methods=["POST"])
@login_required
def api_upload_presigned():
    """GCS presigned URL 생성 API (클라이언트에서 직접 업로드용)"""
    user = current_user()
    today = date.today()
    
    # 오늘 업로드한 파일 개수와 총 크기 계산
    today_photos = Photo.query.filter(
        Photo.user_id == user.id,
        db.func.date(Photo.created_at) == today,
    ).all()
    
    today_count = len(today_photos)
    today_total_size = 0
    if not user.is_admin:
        if app.config["STORAGE_BACKEND"] == "gcs":
            for photo in today_photos:
                if photo.file_size:
                    today_total_size += photo.file_size
        else:
            upload_root = ensure_upload_folder()
            for photo in today_photos:
                try:
                    photo_path = upload_root / photo.relative_path
                    if photo_path.exists():
                        today_total_size += photo_path.stat().st_size
                except Exception:
                    pass
    
    # 제한 설정
    MAX_FILES_PER_DAY = 20
    MAX_SIZE_PER_DAY = 100 * 1024 * 1024  # 100MB
    MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
    
    # 요청 데이터 받기
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "error": "요청 데이터가 없습니다."}), 400
    
    event_name = data.get("event_name", "").strip() or "기타"
    taken_date_str = data.get("taken_date") or str(today)
    try:
        taken_date_value = datetime.strptime(taken_date_str, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"success": False, "error": "날짜 형식이 올바르지 않습니다."}), 400
    
    tags_str = data.get("tags", "").strip()
    tags_list = [tag.strip() for tag in tags_str.split(",") if tag.strip()]
    tags = ",".join(sorted(set(tags_list))) if tags_list else None
    
    files_info = data.get("files", [])  # [{"name": "...", "size": 1234, "type": "image/jpeg"}, ...]
    
    if not files_info:
        return jsonify({"success": False, "error": "파일 정보가 없습니다."}), 400
    
    # 할당량 체크
    if not user.is_admin:
        if today_count + len(files_info) > MAX_FILES_PER_DAY:
            remaining = max(0, MAX_FILES_PER_DAY - today_count)
            return jsonify({
                "success": False,
                "error": f"오늘 업로드 한도에 도달했습니다. (최대 {MAX_FILES_PER_DAY}장)",
                "error_type": "file_limit_exceeded",
                "today_count": today_count,
                "max_files": MAX_FILES_PER_DAY,
                "remaining": remaining
            }), 400
        
        total_new_size = sum(f.get("size", 0) for f in files_info)
        
        # 파일 크기 체크
        oversized_files = [f for f in files_info if f.get("size", 0) > MAX_FILE_SIZE]
        if oversized_files:
            return jsonify({
                "success": False,
                "error": f"파일 크기가 너무 큽니다. (최대 {MAX_FILE_SIZE / (1024 * 1024):.0f}MB)",
                "error_type": "file_size_exceeded",
                "filename": oversized_files[0].get("name", "알 수 없음"),
                "file_size_mb": round(oversized_files[0].get("size", 0) / (1024 * 1024), 2),
                "max_size_mb": MAX_FILE_SIZE / (1024 * 1024)
            }), 400
        
        # 총 용량 체크
        if today_total_size + total_new_size > MAX_SIZE_PER_DAY:
            remaining_mb = (MAX_SIZE_PER_DAY - today_total_size) / (1024 * 1024)
            return jsonify({
                "success": False,
                "error": f"오늘 업로드 용량 한도에 도달했습니다. (남은 용량: {remaining_mb:.1f}MB)",
                "error_type": "size_limit_exceeded",
                "today_total_size_mb": round(today_total_size / (1024 * 1024), 2),
                "new_size_mb": round(total_new_size / (1024 * 1024), 2),
                "max_size_mb": MAX_SIZE_PER_DAY / (1024 * 1024),
                "remaining_mb": round(remaining_mb, 2)
            }), 400
    
    # GCS를 사용하지 않으면 기존 방식으로 폴백
    if app.config["STORAGE_BACKEND"] != "gcs":
        return jsonify({"success": False, "error": "GCS를 사용하지 않는 환경에서는 presigned URL을 사용할 수 없습니다."}), 400
    
    # Presigned URL 생성
    base_timestamp = datetime.utcnow()
    presigned_urls = []
    
    for idx, file_info in enumerate(files_info):
        filename = file_info.get("name", f"file_{idx}")
        file_size = file_info.get("size", 0)
        content_type = file_info.get("type", "application/octet-stream")
        
        safe_name = secure_filename(filename)
        if not safe_name:
            safe_name = f"file_{idx}.jpg"
        
        folder_name = str(taken_date_value)
        timestamp = base_timestamp.strftime("%Y%m%d%H%M%S%f") + f"_{idx}"
        relative_path = Path(folder_name) / f"{timestamp}_{safe_name}"
        relative_path_str = str(relative_path).replace("\\", "/")
        relative_path_str = sanitize_gcs_path(relative_path_str)
        
        blob = gcs_bucket.blob(relative_path_str)
        blob.content_type = content_type
        
        # Presigned URL 생성 (업로드용, 1시간 유효)
        # 파일 크기 제한은 서버 측에서 이미 검증하므로 헤더에 포함하지 않음
        from datetime import timedelta
        url = blob.generate_signed_url(
            version="v4",
            expiration=timedelta(hours=1),
            method="PUT",
            content_type=content_type
        )
        
        presigned_urls.append({
            "url": url,
            "path": relative_path_str,
            "filename": filename,
            "size": file_size,
            "content_type": content_type
        })
    
    return jsonify({
        "success": True,
        "presigned_urls": presigned_urls,
        "event_name": event_name,
        "taken_date": taken_date_value.isoformat(),
        "tags": tags,
        "user_id": user.id
    })


@app.route("/api/upload/complete", methods=["POST"])
@login_required
def api_upload_complete():
    """업로드 완료 후 DB에 기록하는 API"""
    user = current_user()
    data = request.get_json()
    
    if not data or not data.get("paths"):
        return jsonify({"success": False, "error": "업로드된 파일 경로가 없습니다."}), 400
    
    uploaded_paths = data.get("paths", [])  # [{"path": "...", "filename": "...", "size": 1234}, ...]
    event_name = data.get("event_name", "기타")
    taken_date_str = data.get("taken_date")
    tags = data.get("tags")
    
    try:
        taken_date_value = datetime.strptime(taken_date_str, "%Y-%m-%d").date() if taken_date_str else date.today()
    except ValueError:
        return jsonify({"success": False, "error": "날짜 형식이 올바르지 않습니다."}), 400
    
    # DB에 기록
    uploaded_count = 0
    for path_info in uploaded_paths:
        photo_data = {
            "user_id": user.id,
            "relative_path": path_info["path"],
            "original_name": path_info.get("filename", "unknown"),
            "event_name": event_name,
            "taken_date": taken_date_value,
            "tags": tags,
            "status": "pending",
            "file_size": path_info.get("size", 0),
        }
        photo = Photo(**photo_data)
        db.session.add(photo)
        uploaded_count += 1
    
    db.session.commit()
    
    return jsonify({
        "success": True,
        "message": f"{uploaded_count}개의 파일이 업로드되었습니다.",
        "count": uploaded_count
    })


@app.route("/api/upload", methods=["POST"])
@login_required
def api_upload():
    """기존 업로드 API (로컬 스토리지용 또는 폴백)"""
    user = current_user()
    today = date.today()
    
    # 오늘 업로드한 파일 개수와 총 크기 계산
    today_photos = Photo.query.filter(
        Photo.user_id == user.id,
        db.func.date(Photo.created_at) == today,
    ).all()
    
    today_count = len(today_photos)
    today_total_size = 0
    if not user.is_admin:
        if app.config["STORAGE_BACKEND"] == "gcs":
            for photo in today_photos:
                if photo.file_size:
                    today_total_size += photo.file_size
        else:
            upload_root = ensure_upload_folder()
            for photo in today_photos:
                try:
                    photo_path = upload_root / photo.relative_path
                    if photo_path.exists():
                        today_total_size += photo_path.stat().st_size
                except Exception:
                    pass
    
    # 제한 설정
    MAX_FILES_PER_DAY = 20
    MAX_SIZE_PER_DAY = 100 * 1024 * 1024  # 100MB
    MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
    
    # 폼 데이터 받기
    event_name = request.form.get("event_name", "").strip() or "기타"
    taken_date_str = request.form.get("taken_date") or str(today)
    try:
        taken_date_value = datetime.strptime(taken_date_str, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"success": False, "error": "날짜 형식이 올바르지 않습니다."}), 400
    
    # 태그 처리 (선택사항)
    tags_str = request.form.get("tags", "").strip()
    # 태그 정리 (쉼표로 구분, 공백 제거, 중복 제거)
    tags_list = [tag.strip() for tag in tags_str.split(",") if tag.strip()]
    tags = ",".join(sorted(set(tags_list))) if tags_list else None
    
    # 여러 파일 받기
    files = request.files.getlist("photos")
    if not files or all(f.filename == "" for f in files):
        return jsonify({"success": False, "error": "파일을 선택해주세요."}), 400
    
    # 파일 개수 체크
    if not user.is_admin and today_count + len(files) > MAX_FILES_PER_DAY:
        remaining = MAX_FILES_PER_DAY - today_count
        return jsonify({
            "success": False, 
            "error": f"오늘 업로드 한도에 도달했습니다. (최대 {MAX_FILES_PER_DAY}장)",
            "error_type": "file_limit_exceeded",
            "today_count": today_count,
            "max_files": MAX_FILES_PER_DAY,
            "remaining": max(0, remaining)
        }), 400
    
    # 파일 크기 체크 및 총 용량 계산
    total_new_size = 0
    valid_files = []
    
    for file in files:
        if file.filename == "":
            continue
        
        # 파일 크기 확인
        file.seek(0, 2)
        file_size = file.tell()
        file.seek(0)
        
        if not user.is_admin:
            if file_size > MAX_FILE_SIZE:
                return jsonify({
                    "success": False, 
                    "error": f"파일 크기가 너무 큽니다. ({file.filename}: 최대 {MAX_FILE_SIZE / (1024 * 1024):.0f}MB)",
                    "error_type": "file_size_exceeded",
                    "filename": file.filename,
                    "file_size_mb": round(file_size / (1024 * 1024), 2),
                    "max_size_mb": MAX_FILE_SIZE / (1024 * 1024)
                }), 400
        
        total_new_size += file_size
        valid_files.append((file, file_size))
    
    # 하루 총 용량 체크
    if not user.is_admin and today_total_size + total_new_size > MAX_SIZE_PER_DAY:
        remaining_mb = (MAX_SIZE_PER_DAY - today_total_size) / (1024 * 1024)
        return jsonify({
            "success": False, 
            "error": f"오늘 업로드 용량 한도에 도달했습니다. (남은 용량: {remaining_mb:.1f}MB)",
            "error_type": "size_limit_exceeded",
            "today_total_size_mb": round(today_total_size / (1024 * 1024), 2),
            "new_size_mb": round(total_new_size / (1024 * 1024), 2),
            "max_size_mb": MAX_SIZE_PER_DAY / (1024 * 1024),
            "remaining_mb": round(remaining_mb, 2)
        }), 400
    
    # 파일들을 백그라운드로 처리
    # 파일 데이터를 먼저 모두 읽어서 메모리에 저장한 후, 즉시 응답 반환
    base_timestamp = datetime.utcnow()
    file_tasks = []
    
    for idx, (file, file_size) in enumerate(valid_files):
        # 파일명이 없거나 secure_filename이 빈 문자열을 반환하는 경우 처리
        original_filename = file.filename or f"file_{idx}"
        safe_name = secure_filename(original_filename)
        if not safe_name:
            # secure_filename이 빈 문자열을 반환하면 기본 파일명 사용
            safe_name = f"file_{idx}.jpg"
        
        # 촬영일 기준으로 폴더 생성
        folder_name = str(taken_date_value)
        # 마이크로초 단위 타임스탬프 + 인덱스로 고유성 보장
        # 마이크로초는 6자리이므로 그대로 사용
        timestamp = base_timestamp.strftime("%Y%m%d%H%M%S%f") + f"_{idx}"
        relative_path = Path(folder_name) / f"{timestamp}_{safe_name}"
        
        # GCS 경로 정리
        if app.config["STORAGE_BACKEND"] == "gcs":
            relative_path_str = str(relative_path).replace("\\", "/")
            relative_path_str = sanitize_gcs_path(relative_path_str)
            relative_path = Path(relative_path_str)
        
        # 파일 데이터 읽기 (메모리에 저장)
        file_data = file.read()
        content_type = file.mimetype or "application/octet-stream"
        
        # 백그라운드 작업으로 추가 (즉시 실행되지 않음)
        file_tasks.append({
            'file_data': file_data,
            'filename': file.filename,
            'relative_path': str(relative_path).replace("\\", "/"),
            'user_id': user.id,
            'event_name': event_name,
            'taken_date': taken_date_value,
            'tags': tags,
            'file_size': file_size,
            'content_type': content_type
        })
    
    # 모든 파일 데이터를 메모리에 저장한 후, 백그라운드로 처리 시작
    # 이렇게 하면 응답을 즉시 반환할 수 있음
    for task in file_tasks:
        upload_executor.submit(
            process_file_upload_async,
            task['file_data'],
            task['filename'],
            task['relative_path'],
            task['user_id'],
            task['event_name'],
            task['taken_date'],
            task['tags'],
            task['file_size'],
            task['content_type']
        )
    
    uploaded_count = len(file_tasks)
    return jsonify({
        "success": True,
        "message": f"{uploaded_count}개의 파일이 업로드되었습니다.",
        "count": uploaded_count
    })


@app.route("/upload", methods=["GET"])
@login_required
def upload():
    user = current_user()
    today = date.today()
    
    # 오늘 업로드한 파일 개수와 총 크기 계산
    today_photos = Photo.query.filter(
        Photo.user_id == user.id,
        db.func.date(Photo.created_at) == today,
    ).all()
    
    today_count = len(today_photos)
    # 오늘 업로드한 파일의 총 크기 계산
    today_total_size = 0
    if not user.is_admin:
        if app.config["STORAGE_BACKEND"] == "gcs":
            # GCS를 사용할 때는 데이터베이스의 file_size 컬럼 사용
            for photo in today_photos:
                if photo.file_size:
                    today_total_size += photo.file_size
        else:
            # 로컬 스토리지를 사용할 때는 파일 시스템에서 크기 확인
            upload_root = ensure_upload_folder()
            for photo in today_photos:
                try:
                    photo_path = upload_root / photo.relative_path
                    if photo_path.exists():
                        today_total_size += photo_path.stat().st_size
                except Exception:
                    pass
    
    # 남은 용량 계산
    MAX_FILES_PER_DAY = 20
    MAX_SIZE_PER_DAY = 100 * 1024 * 1024  # 100MB
    remaining_files = MAX_FILES_PER_DAY - today_count if not user.is_admin else "무제한"
    remaining_size_mb = (MAX_SIZE_PER_DAY - today_total_size) / (1024 * 1024) if not user.is_admin else "무제한"
    used_size_mb = today_total_size / (1024 * 1024)
    
    return render_template(
        "upload.html",
        user=user,
        today_count=today_count,
        today_total_size=today_total_size,
        used_size_mb=used_size_mb,
        remaining_files=remaining_files,
        remaining_size_mb=remaining_size_mb,
        max_files=MAX_FILES_PER_DAY,
        storage_backend=app.config["STORAGE_BACKEND"],
        max_size_mb=100,
        today=date.today(),
    )


@app.route("/admin/pending", methods=["GET", "POST"])
@admin_required
def admin_pending():
    if request.method == "POST":
        action = request.form.get("action")
        
        # 일괄 처리
        if action in ("batch_approve", "batch_reject"):
            photo_ids_str = request.form.get("photo_ids", "")
            if not photo_ids_str:
                flash("선택한 사진이 없습니다.", "error")
                return redirect(url_for("admin_pending"))
            
            photo_ids = [int(id.strip()) for id in photo_ids_str.split(",") if id.strip()]
            photos = Photo.query.filter(Photo.id.in_(photo_ids), Photo.status == "pending").all()
            
            if not photos:
                flash("처리할 사진을 찾을 수 없습니다.", "error")
                return redirect(url_for("admin_pending"))
            
            approved_count = 0
            rejected_count = 0
            
            for photo in photos:
                if action == "batch_approve":
                    photo.status = "approved"
                    approved_count += 1
                elif action == "batch_reject":
                    # 거부 시 파일 삭제
                    try:
                        if app.config["STORAGE_BACKEND"] == "gcs":
                            blob = gcs_bucket.blob(photo.relative_path)
                            if blob.exists():
                                blob.delete()
                                app.logger.info(f"GCS 파일 삭제 완료: {photo.relative_path}")
                        else:
                            upload_root = ensure_upload_folder()
                            file_path = upload_root / photo.relative_path
                            if file_path.exists():
                                file_path.unlink()
                                app.logger.info(f"로컬 파일 삭제 완료: {photo.relative_path}")
                                try:
                                    folder_path = file_path.parent
                                    if not any(folder_path.iterdir()):
                                        folder_path.rmdir()
                                except:
                                    pass
                    except Exception as e:
                        app.logger.error(f"파일 삭제 실패: {e}")
                    
                    photo.status = "rejected"
                    rejected_count += 1
            
            db.session.commit()
            
            if action == "batch_approve":
                flash(f"{approved_count}개의 사진이 승인되었습니다.", "success")
            else:
                flash(f"{rejected_count}개의 사진이 거절되었습니다.", "success")
            
            return redirect(url_for("admin_pending"))
        
        # 단일 처리 (기존 로직)
        photo_id = request.form.get("photo_id")
        photo = db.session.get(Photo, photo_id)
        if photo:
            if action == "approve":
                photo.status = "approved"
                db.session.commit()
                flash("승인되었습니다.", "success")
            elif action == "reject":
                # 거부 시 파일 삭제
                try:
                    if app.config["STORAGE_BACKEND"] == "gcs":
                        # Google Cloud Storage에서 파일 삭제
                        blob = gcs_bucket.blob(photo.relative_path)
                        if blob.exists():
                            blob.delete()
                            app.logger.info(f"GCS 파일 삭제 완료: {photo.relative_path}")
                    else:
                        # 로컬 스토리지에서 파일 삭제
                        upload_root = ensure_upload_folder()
                        file_path = upload_root / photo.relative_path
                        if file_path.exists():
                            file_path.unlink()
                            app.logger.info(f"로컬 파일 삭제 완료: {photo.relative_path}")
                            # 빈 디렉토리 정리
                            try:
                                folder_path = file_path.parent
                                if not any(folder_path.iterdir()):
                                    folder_path.rmdir()
                            except:
                                pass
                except Exception as e:
                    app.logger.error(f"파일 삭제 실패: {e}")
                    flash("파일 삭제 중 오류가 발생했습니다.", "error")
                
                photo.status = "rejected"
                db.session.commit()
                flash("거부되었습니다.", "success")
        return redirect(url_for("admin_pending"))

    # 업로드 날짜별로 그룹화
    from collections import defaultdict
    # 사용자 정보를 함께 로드하여 N+1 쿼리 방지
    pending_photos = (
        Photo.query.filter_by(status="pending")
        .order_by(Photo.created_at.desc())
        .all()
    )
    
    # 사용자 정보를 한 번에 로드
    user_ids = {photo.user_id for photo in pending_photos}
    users_dict = {u.id: u for u in User.query.filter(User.id.in_(user_ids)).all()}
    
    # 업로드 날짜별로 그룹화 (created_at의 날짜 부분만)
    photos_by_date = defaultdict(lambda: defaultdict(list))
    
    for photo in pending_photos:
        upload_date = photo.created_at.date() if photo.created_at else date.today()
        user_id = photo.user_id
        # URL을 미리 생성하여 성능 최적화
        photo.file_url_cache = build_file_url(photo)
        photos_by_date[upload_date][user_id].append(photo)
    
    # 날짜별로 정렬 (최신순)
    sorted_dates = sorted(photos_by_date.keys(), reverse=True)
    
    # 날짜별, 사용자별로 정리된 데이터 구조 생성
    organized_photos = []
    for upload_date in sorted_dates:
        date_group = {
            'date': upload_date,
            'users': [],
            'total_count': 0
        }
        
        # 각 날짜의 사용자별로 정리
        for user_id, photos in photos_by_date[upload_date].items():
            user = users_dict.get(user_id)
            if user:
                date_group['users'].append({
                    'user': user,
                    'photos': photos
                })
                date_group['total_count'] += len(photos)
        
        organized_photos.append(date_group)
    
    return render_template(
        "admin_pending.html",
        organized_photos=organized_photos,
        total_count=len(pending_photos),
        user=current_user(),
        upload_root="files",
    )


@app.route("/profile")
@login_required
def profile():
    user = current_user()
    my_photos = (
        Photo.query.filter_by(user_id=user.id)
        .order_by(Photo.created_at.desc())
        .all()
    )
    
    # 업로드 날짜별로 그룹화 및 URL 미리 생성
    from collections import defaultdict
    photos_by_date = defaultdict(list)
    
    for photo in my_photos:
        upload_date = photo.created_at.date() if photo.created_at else date.today()
        # URL을 미리 생성하여 성능 최적화
        photo.file_url_cache = build_file_url(photo)
        photos_by_date[upload_date].append(photo)
    
    # 날짜별로 정렬 (최신순)
    sorted_dates = sorted(photos_by_date.keys(), reverse=True)
    
    # 날짜별로 정리된 데이터 구조 생성
    organized_photos = []
    for upload_date in sorted_dates:
        organized_photos.append({
            'date': upload_date,
            'photos': photos_by_date[upload_date],
            'total_count': len(photos_by_date[upload_date])
        })
    
    return render_template("profile.html", user=user, organized_photos=organized_photos, total_count=len(my_photos))


def build_file_url(photo: Photo) -> str:
    # 거절된 사진은 스토리지에서 삭제되므로 기본 이미지 반환
    if photo.status == "rejected":
        # 기본 이미지 (SVG data URI)
        return "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='200' height='200' viewBox='0 0 200 200'%3E%3Crect fill='%231a1a1a' width='200' height='200'/%3E%3Ctext fill='%23888888' font-family='sans-serif' font-size='14' x='50%25' y='50%25' text-anchor='middle' dominant-baseline='middle'%3E%EA%B1%B0%EC%A0%88%EB%90%9C%20%EC%82%AC%EC%A7%84%3C/text%3E%3C/svg%3E"
    
    if app.config["STORAGE_BACKEND"] == "gcs":
        from datetime import timedelta

        blob = gcs_bucket.blob(photo.relative_path)
        # 1시간 유효한 서명 URL (V4) 생성
        return blob.generate_signed_url(
            version="v4",
            expiration=timedelta(hours=1),
            method="GET",
        )
    return url_for("files", filename=photo.relative_path)


@app.context_processor
def inject_helpers():
    return {"file_url": build_file_url}


@app.route("/login")
def login():
    # 리디렉션 URI 명시적 설정
    redirect_uri = url_for("auth_callback", _external=True)
    app.logger.info(f"OAuth redirect URI: {redirect_uri}")
    return oauth.google.authorize_redirect(redirect_uri)


@app.route("/auth/callback")
def auth_callback():
    token = oauth.google.authorize_access_token()
    userinfo = token.get("userinfo")
    if not userinfo:
        flash("구글 로그인에 실패했습니다.", "error")
        return redirect(url_for("home"))

    email = userinfo["email"]
    admin_emails = set(
        e.strip().lower()
        for e in os.getenv("ADMIN_EMAILS", "").split(",")
        if e.strip()
    )

    user = User.query.filter_by(email=email).first()
    if not user:
        # 새 사용자: 환경변수 기준으로 관리자 권한 설정
        user = User(
            email=email,
            name=userinfo.get("name"),
            picture=userinfo.get("picture"),
            is_admin=email.lower() in admin_emails,
        )
        db.session.add(user)
    else:
        # 기존 사용자: 이름과 사진만 업데이트
        user.name = userinfo.get("name")
        user.picture = userinfo.get("picture")
        # 관리자 권한은 환경변수에 이메일이 있을 때만 업데이트 (기존 권한 보호)
        if admin_emails:  # 환경변수가 설정되어 있을 때만
            user.is_admin = email.lower() in admin_emails
        # 환경변수가 없으면 기존 is_admin 값 유지
    db.session.commit()

    session["user_id"] = user.id
    flash("로그인되었습니다.", "success")
    return redirect(url_for("home"))


@app.route("/logout")
def logout():
    session.clear()
    flash("로그아웃되었습니다.", "success")
    return redirect(url_for("home"))


if __name__ == "__main__":
    with app.app_context():
        ensure_schema()
    app.run(host="0.0.0.0", port=8000, debug=True)

