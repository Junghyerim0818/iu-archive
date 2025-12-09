import os
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
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.pool import NullPool
from sqlalchemy import inspect, text
from werkzeug.utils import secure_filename
import boto3
from botocore.client import Config

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
    STORAGE_BACKEND=os.getenv("STORAGE_BACKEND", "local"),  # local or s3
    S3_BUCKET=os.getenv("S3_BUCKET"),
    S3_REGION=os.getenv("S3_REGION", "ap-northeast-2"),
    S3_ENDPOINT=os.getenv("S3_ENDPOINT"),  # 옵션: 커스텀 엔드포인트
)

db = SQLAlchemy(app)
s3_client = None
if app.config["STORAGE_BACKEND"] == "s3":
    session_kwargs = {}
    if app.config["S3_ENDPOINT"]:
        session_kwargs["endpoint_url"] = app.config["S3_ENDPOINT"]
    s3_client = boto3.client(
        "s3",
        region_name=app.config["S3_REGION"],
        config=Config(signature_version="s3v4"),
        **session_kwargs,
    )


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
    """필수 테이블(User, Photo, Visitor)이 존재하는지 확인 - 실제 쿼리로 확인"""
    try:
        # 간단한 쿼리로 테이블 존재 확인
        db.session.execute(text('SELECT 1 FROM "user" LIMIT 1'))
        db.session.execute(text("SELECT 1 FROM photo LIMIT 1"))
        db.session.execute(text("SELECT 1 FROM visitor LIMIT 1"))
        db.session.rollback()  # 쿼리만 확인하고 롤백
        return True
    except Exception as exc:
        db.session.rollback()
        # 테이블이 없으면 ProgrammingError 또는 OperationalError 발생
        return False


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
    event_name = db.Column(db.String(255))
    taken_date = db.Column(db.Date, nullable=False)
    status = db.Column(db.String(32), default="pending")  # pending/approved/rejected
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User", backref="photos")


class Visitor(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    ip_address = db.Column(db.String(45))  # IPv6 지원
    session_id = db.Column(db.String(255))
    visited_at = db.Column(db.DateTime, default=datetime.utcnow)
    visited_date = db.Column(db.Date, default=date.today)


def current_user() -> Optional[User]:
    uid = session.get("user_id")
    if not uid:
        return None
    return db.session.get(User, uid)


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user():
            flash("로그인이 필요합니다.", "warning")
            return redirect(url_for("home"))
        return fn(*args, **kwargs)

    return wrapper


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user or not user.is_admin:
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


def ensure_upload_folder() -> Path:
    """로컬 스토리지를 사용할 때만 폴더를 생성합니다."""
    if app.config["STORAGE_BACKEND"] == "s3":
        # S3를 사용할 때는 임시 경로만 반환 (실제로는 사용되지 않음)
        return Path("/tmp/uploads")
    
    upload_root = Path(app.config["UPLOAD_FOLDER"])
    try:
        upload_root.mkdir(parents=True, exist_ok=True)
    except OSError:
        # 읽기 전용 파일 시스템인 경우 /tmp 사용
        upload_root = Path("/tmp/uploads")
        upload_root.mkdir(parents=True, exist_ok=True)
    return upload_root


@app.route("/")
def home():
    # 방문자 기록 (중복 방지: 같은 세션에서는 하루에 한 번만 기록)
    session_id = session.get("_id", session.get("session_id"))
    if not session_id:
        import secrets
        session_id = secrets.token_hex(16)
        session["_id"] = session_id
    
    ip_address = request.remote_addr or request.environ.get("HTTP_X_FORWARDED_FOR", "").split(",")[0]
    today = date.today()
    
    # 방문자 기록 시도 (테이블이 없을 수 있으므로 예외 처리)
    try:
        # 오늘 이미 방문한 세션이 아니면 기록
        existing_visit = Visitor.query.filter_by(
            session_id=session_id,
            visited_date=today
        ).first()
        
        if not existing_visit:
            visitor = Visitor(
                ip_address=ip_address,
                session_id=session_id,
                visited_date=today
            )
            db.session.add(visitor)
            db.session.commit()
    except Exception as exc:
        # visitor 테이블이 없거나 다른 오류 발생 시 스키마 생성 시도
        app.logger.warning("방문자 기록 실패, 스키마 확인: %s", exc)
        try:
            db.create_all()
        except Exception:
            pass
        db.session.rollback()
    
    approved_recent = (
        Photo.query.filter_by(status="approved")
        .order_by(Photo.created_at.desc())
        .limit(12)
        .all()
    )
    
    # 통계 데이터
    total_photos = Photo.query.count()
    approved_photos = Photo.query.filter_by(status="approved").count()
    pending_photos = Photo.query.filter_by(status="pending").count()
    
    # 방문자 통계 (테이블이 없을 수 있으므로 예외 처리)
    try:
        total_visitors = Visitor.query.count()
        today_visitors = Visitor.query.filter_by(visited_date=today).count()
    except Exception:
        total_visitors = 0
        today_visitors = 0
    
    stats = {
        "total_photos": total_photos,
        "approved_photos": approved_photos,
        "pending_photos": pending_photos,
        "total_visitors": total_visitors,
        "today_visitors": today_visitors,
    }
    
    return render_template("home.html", photos=approved_recent, user=current_user(), stats=stats)


@app.route("/archive")
def archive():
    approved = (
        Photo.query.filter_by(status="approved")
        .order_by(Photo.taken_date.desc(), Photo.created_at.desc())
        .all()
    )

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
        "archive.html", years_dict=years_dict, user=current_user()
    )


@app.route("/files/<path:filename>")
def files(filename):
    if app.config["STORAGE_BACKEND"] == "s3":
        # 로컬 라우팅 불가, 아카이브에서 presigned URL을 직접 사용합니다.
        flash("파일은 S3에서 제공됩니다.")
        return redirect(url_for("archive"))
    upload_root = ensure_upload_folder()
    return send_from_directory(upload_root, filename)


@app.route("/upload", methods=["GET", "POST"])
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
        if app.config["STORAGE_BACKEND"] == "s3":
            # S3를 사용할 때는 데이터베이스의 file_size 컬럼 사용
            for photo in today_photos:
                if hasattr(photo, 'file_size') and photo.file_size:
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
    
    # 제한 설정
    MAX_FILES_PER_DAY = 20
    MAX_SIZE_PER_DAY = 100 * 1024 * 1024  # 100MB
    
    if request.method == "POST":
        if not user.is_admin:
            # 파일 개수 체크
            if today_count >= MAX_FILES_PER_DAY:
                flash(f"오늘 업로드 한도에 도달했습니다. (최대 {MAX_FILES_PER_DAY}장)", "error")
                return redirect(url_for("upload"))
        
        file = request.files.get("photo")
        event_name = request.form.get("event_name", "").strip() or "기타"
        taken_date_str = request.form.get("taken_date") or str(today)
        try:
            taken_date_value = datetime.strptime(taken_date_str, "%Y-%m-%d").date()
        except ValueError:
            flash("날짜 형식이 올바르지 않습니다.", "error")
            return redirect(url_for("upload"))

        if not file or file.filename == "":
            flash("파일을 선택해주세요.", "error")
            return redirect(url_for("upload"))
        
        # 파일 크기 체크
        file.seek(0, 2)  # 파일 끝으로 이동
        file_size = file.tell()
        file.seek(0)  # 다시 시작으로
        
        if not user.is_admin:
            if file_size > MAX_SIZE_PER_DAY:
                flash(f"파일 크기가 너무 큽니다. (최대 100MB)", "error")
                return redirect(url_for("upload"))
            
            if today_total_size + file_size > MAX_SIZE_PER_DAY:
                remaining_mb = (MAX_SIZE_PER_DAY - today_total_size) / (1024 * 1024)
                flash(f"오늘 업로드 용량 한도에 도달했습니다. (남은 용량: {remaining_mb:.1f}MB)", "error")
                return redirect(url_for("upload"))

        safe_name = secure_filename(file.filename)
        if not safe_name:
            flash("파일 이름이 올바르지 않습니다.", "error")
            return redirect(url_for("upload"))

        folder_name = f"{taken_date_value}_{event_name.replace(' ', '_')}"
        timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
        relative_path = Path(folder_name) / f"{timestamp}_{safe_name}"

        if app.config["STORAGE_BACKEND"] == "s3":
            key = str(relative_path).replace("\\", "/")
            file.stream.seek(0)
            s3_client.upload_fileobj(
                file.stream,
                app.config["S3_BUCKET"],
                key,
                ExtraArgs={"ContentType": file.mimetype or "application/octet-stream"},
            )
        else:
            upload_root = ensure_upload_folder()
            target_dir = upload_root / folder_name
            target_dir.mkdir(parents=True, exist_ok=True)
            file.save(target_dir / relative_path.name)

        photo_data = {
            "user_id": user.id,
            "relative_path": str(relative_path).replace("\\", "/"),
            "original_name": file.filename,
            "event_name": event_name,
            "taken_date": taken_date_value,
            "status": "pending",
        }
        # file_size 컬럼이 있으면 추가
        if hasattr(Photo, 'file_size'):
            photo_data["file_size"] = file_size
        
        photo = Photo(**photo_data)
        db.session.add(photo)
        db.session.commit()

        flash("업로드가 접수되었습니다. 관리자의 승인 후 아카이브에 노출됩니다.", "success")
        return redirect(url_for("upload"))

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
        max_size_mb=100,
        today=date.today(),
    )


@app.route("/admin/pending", methods=["GET", "POST"])
@admin_required
def admin_pending():
    if request.method == "POST":
        action = request.form.get("action")
        photo_id = request.form.get("photo_id")
        photo = db.session.get(Photo, photo_id)
        if photo:
            if action == "approve":
                photo.status = "approved"
            elif action == "reject":
                photo.status = "rejected"
            db.session.commit()
            flash("처리되었습니다.", "success")
        return redirect(url_for("admin_pending"))

    pending = Photo.query.filter_by(status="pending").order_by(Photo.created_at).all()
    return render_template(
        "admin_pending.html",
        photos=pending,
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
        .limit(20)
        .all()
    )
    return render_template("profile.html", user=user, photos=my_photos)


def build_file_url(photo: Photo) -> str:
    if app.config["STORAGE_BACKEND"] == "s3":
        return s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": app.config["S3_BUCKET"], "Key": photo.relative_path},
            ExpiresIn=3600,
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
        user.is_admin = email.lower() in admin_emails
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

