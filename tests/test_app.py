import os
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def app_module(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    sys.modules.pop("app", None)
    import app as module

    module.app.config.update(TESTING=True, UPLOAD_FOLDER=tmp_path / "uploads")
    with module.app.app_context():
        module.ensure_schema()
    yield module
    with module.app.app_context():
        module.db.session.remove()
        module.db.engine.dispose()
    sys.modules.pop("app", None)


@pytest.fixture()
def client(app_module):
    return app_module.app.test_client()


CSRF = "test-csrf-token"
JPEG_MAGIC = b"\xff\xd8\xff\xe0"
HEIC_HEAD = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic"


def make_user(m, email="user@example.com", is_admin=False):
    with m.app.app_context():
        user = m.User(email=email, name=email.split("@")[0], is_admin=is_admin)
        m.db.session.add(user)
        m.db.session.commit()
        return user.id


def login(client, user_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["_csrf_token"] = CSRF


def add_photo(m, user_id, status="approved", tags=None, event_name="공연", taken=date(2024, 5, 1), **kw):
    with m.app.app_context():
        photo = m.Photo(
            user_id=user_id,
            relative_path=kw.pop("relative_path", f"{taken}/{datetime.utcnow().timestamp()}_{os.urandom(3).hex()}.jpg"),
            event_name=event_name,
            taken_date=taken,
            tags=tags,
            status=status,
            **kw,
        )
        m.db.session.add(photo)
        m.db.session.commit()
        return photo.id


# --- 보안 -------------------------------------------------------------------


def test_secret_key_required_on_serverless(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "SECRET_KEY"}
    env.update(VERCEL="1", DATABASE_URL=f"sqlite:///{tmp_path / 'x.db'}")
    result = subprocess.run(
        [sys.executable, "-c", "import app"], cwd=ROOT, env=env, capture_output=True, text=True
    )
    assert result.returncode != 0
    assert "SECRET_KEY" in result.stderr


def test_post_without_csrf_token_is_rejected(app_module, client):
    uid = make_user(app_module)
    login(client, uid)
    resp = client.post("/api/upload", data={})
    assert resp.status_code == 400
    assert resp.get_json()["error_type"] == "csrf"


def test_admin_form_without_csrf_is_rejected(app_module, client):
    admin = make_user(app_module, "admin@example.com", is_admin=True)
    pid = add_photo(app_module, admin, status="pending")
    login(client, admin)
    client.post("/admin/pending", data={"action": "approve", "photo_id": pid})
    with app_module.app.app_context():
        assert app_module.db.session.get(app_module.Photo, pid).status == "pending"
    client.post("/admin/pending", data={"action": "approve", "photo_id": pid, "csrf_token": CSRF})
    with app_module.app.app_context():
        assert app_module.db.session.get(app_module.Photo, pid).status == "approved"


def test_admin_page_does_not_inline_event_name_in_js(app_module, client):
    admin = make_user(app_module, "admin@example.com", is_admin=True)
    payload = "x');alert(1);//"
    add_photo(app_module, admin, status="pending", event_name=payload)
    login(client, admin)
    html = client.get("/admin/pending").get_data(as_text=True)
    assert "openImageModal('" not in html
    assert 'data-alt="x&#39;);alert(1);//"' in html


def test_pending_photo_not_visible_to_others(app_module, client):
    owner = make_user(app_module)
    pid = add_photo(app_module, owner, status="pending")
    assert client.get(f"/photos/{pid}/image").status_code == 404
    other = make_user(app_module, "other@example.com")
    login(client, other)
    assert client.get(f"/photos/{pid}/image").status_code == 404


# --- 아카이브 API ----------------------------------------------------------------


def test_tag_filter_is_exact_match(app_module, client):
    uid = make_user(app_module)
    a = add_photo(app_module, uid, tags="IU")
    b = add_photo(app_module, uid, tags="IU콘서트")
    c = add_photo(app_module, uid, tags="50%,IU")
    data = client.get("/api/archive/photos?sort=date&tags=IU").get_json()
    assert {p["id"] for p in data["photos"]} == {a, c}
    data = client.get("/api/archive/photos?sort=date&tags=50%25").get_json()
    assert {p["id"] for p in data["photos"]} == {c}

    tags = {t["name"]: t["count"] for t in client.get("/api/archive/tags").get_json()["tags"]}
    assert tags == {"IU": 2, "IU콘서트": 1, "50%": 1}
    assert b


def test_random_pagination_has_no_duplicates_or_gaps(app_module, client):
    uid = make_user(app_module)
    ids = {add_photo(app_module, uid) for _ in range(23)}
    seen = []
    page = 1
    while True:
        data = client.get(f"/api/archive/photos?sort=random&seed=42&per_page=5&page={page}").get_json()
        seen += [p["id"] for p in data["photos"]]
        if not data["has_more"]:
            break
        page += 1
    assert len(seen) == len(set(seen)) == 23
    assert set(seen) == ids


def test_per_page_is_capped(app_module, client):
    uid = make_user(app_module)
    for _ in range(3):
        add_photo(app_module, uid)
    data = client.get("/api/archive/photos?per_page=100000&page=-5").get_json()
    assert data["page"] == 1
    assert len(data["photos"]) == 3


def test_download_counts_once_per_session(app_module, client, tmp_path):
    uid = make_user(app_module)
    pid = add_photo(app_module, uid, relative_path="2024-05-01/a.jpg")
    f = tmp_path / "uploads" / "2024-05-01" / "a.jpg"
    f.parent.mkdir(parents=True)
    f.write_bytes(real_jpeg((300, 200)))
    for _ in range(3):
        resp = client.get(f"/photos/{pid}/download")
        assert resp.status_code == 200
        assert "attachment" in resp.headers["Content-Disposition"]
    with app_module.app.app_context():
        assert app_module.db.session.get(app_module.Photo, pid).download_count == 1


# --- 업로드 (로컬) ------------------------------------------------------------


def test_local_upload_saves_synchronously_and_rejects_non_images(app_module, client, tmp_path):
    import io

    uid = make_user(app_module)
    login(client, uid)
    headers = {"X-CSRF-Token": CSRF}

    resp = client.post(
        "/api/upload",
        data={"photos": (io.BytesIO(b"<svg onload=alert(1)>"), "x.svg", "image/svg+xml")},
        headers=headers,
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400

    resp = client.post(
        "/api/upload",
        data={
            "event_name": "콘서트",
            "tags": "b, a ,a",
            "taken_date": "2024-05-01",
            "photos": (io.BytesIO(JPEG_MAGIC + b"jpegdata"), "사진.jpg", "image/jpeg"),
        },
        headers=headers,
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200, resp.get_json()
    with app_module.app.app_context():
        photo = app_module.Photo.query.one()
        assert photo.tags == "a,b"
        assert photo.file_size == len(JPEG_MAGIC + b"jpegdata")
        assert (tmp_path / "uploads" / photo.relative_path).exists()


def test_local_upload_enforces_daily_count(app_module, client):
    import io

    app_module.app.config["DAILY_UPLOAD_LIMIT"] = 2
    uid = make_user(app_module)
    login(client, uid)
    files = [(io.BytesIO(JPEG_MAGIC + b"abc"), f"{i}.jpg", "image/jpeg") for i in range(3)]
    resp = client.post(
        "/api/upload",
        data={"photos": files},
        headers={"X-CSRF-Token": CSRF},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert resp.get_json()["error_type"] == "file_limit_exceeded"


# --- 업로드 (GCS presigned) ---------------------------------------------------


class FakeBlob:
    def __init__(self, bucket, name):
        self.bucket, self.name = bucket, name
        self.size, self.content_type = None, None

    def generate_signed_url(self, **kwargs):
        return f"https://storage.test/{self.name}?method={kwargs.get('method')}"

    def upload_from_string(self, data, content_type=None):
        self.data = data
        self.size, self.content_type = len(data), content_type
        self.bucket.store[self.name] = self

    def download_as_bytes(self, start=None, end=None):
        if self.name not in self.bucket.store:
            raise FileNotFoundError(self.name)
        if start is not None:
            return self.data[start:(end + 1 if end is not None else None)]
        return self.data

    def delete(self):
        self.bucket.store.pop(self.name, None)


class FakeBucket:
    def __init__(self):
        self.store = {}

    def blob(self, name):
        return self.store.get(name) or FakeBlob(self, name)

    def get_blob(self, name):
        return self.store.get(name)

    def put(self, name, size=None, content_type="image/jpeg", data=None):
        if data is None:
            data = JPEG_MAGIC + b"x" * max(0, size - len(JPEG_MAGIC))
        self.blob(name).upload_from_string(data, content_type)


@pytest.fixture()
def gcs(app_module, monkeypatch):
    bucket = FakeBucket()
    monkeypatch.setattr(app_module, "gcs_bucket", bucket)
    app_module.app.config["STORAGE_BACKEND"] = "gcs"
    return bucket


def presign(client, files, **extra):
    body = {"event_name": "공연", "taken_date": "2024-05-01", "tags": "IU", "files": files, **extra}
    return client.post("/api/upload/presigned", json=body, headers={"X-CSRF-Token": CSRF})


def complete(client, tokens):
    return client.post("/api/upload/complete", json={"tokens": tokens}, headers={"X-CSRF-Token": CSRF})


def test_complete_requires_server_signed_token(app_module, client, gcs):
    uid = make_user(app_module)
    login(client, uid)
    # 예전 방식처럼 임의 경로/크기를 보내면 거부
    resp = client.post(
        "/api/upload/complete",
        json={"paths": [{"path": "someone/else.jpg", "size": 0}]},
        headers={"X-CSRF-Token": CSRF},
    )
    assert resp.status_code == 400
    resp = complete(client, ["forged.token"])
    assert resp.status_code == 400
    with app_module.app.app_context():
        assert app_module.Photo.query.count() == 0


def test_complete_rejects_other_users_token(app_module, client, gcs):
    a = make_user(app_module, "a@example.com")
    b = make_user(app_module, "b@example.com")
    login(client, a)
    item = presign(client, [{"name": "a.jpg", "size": 100, "type": "image/jpeg"}]).get_json()["presigned_urls"][0]
    gcs.put(item["path"], 100)
    login(client, b)
    assert complete(client, [item["token"]]).status_code == 400


def test_complete_uses_real_size_and_deletes_oversized(app_module, client, gcs):
    uid = make_user(app_module)
    login(client, uid)
    # 클라이언트는 작은 크기라고 주장
    item = presign(client, [{"name": "big.jpg", "size": 100, "type": "image/jpeg"}]).get_json()["presigned_urls"][0]
    gcs.put(item["path"], 11 * 1024 * 1024)  # 실제로는 11MB
    resp = complete(client, [item["token"]])
    assert resp.status_code == 400
    assert item["path"] not in gcs.store  # 스토리지에서도 삭제됨
    with app_module.app.app_context():
        assert app_module.Photo.query.count() == 0


def test_complete_success_and_token_reuse(app_module, client, gcs):
    uid = make_user(app_module)
    login(client, uid)
    result = presign(client, [{"name": "a.jpg", "size": 100, "type": "image/jpeg"}]).get_json()
    item = result["presigned_urls"][0]
    gcs.put(item["path"], 1234)
    resp = complete(client, [item["token"]])
    assert resp.status_code == 200, resp.get_json()
    with app_module.app.app_context():
        photo = app_module.Photo.query.one()
        assert photo.file_size == 1234  # 클라이언트가 말한 100이 아니라 실제 크기
        assert photo.tags == "IU"
    # 같은 토큰으로 다시 등록 불가
    assert complete(client, [item["token"]]).status_code == 400


def test_complete_enforces_daily_count_with_real_rows(app_module, client, gcs):
    app_module.app.config["DAILY_UPLOAD_LIMIT"] = 2
    uid = make_user(app_module)
    login(client, uid)
    items = []
    for i in range(2):  # presigned 단계는 각각 통과
        items += presign(client, [{"name": f"{i}.jpg", "size": 10, "type": "image/jpeg"}]).get_json()["presigned_urls"]
    items += presign(client, [{"name": "x.jpg", "size": 10, "type": "image/jpeg"}]).get_json()["presigned_urls"]
    for it in items:
        gcs.put(it["path"], 10)
    resp = complete(client, [it["token"] for it in items])
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["count"] == 2 and len(body["rejected"]) == 1


def test_presigned_rejects_non_image(app_module, client, gcs):
    uid = make_user(app_module)
    login(client, uid)
    resp = presign(client, [{"name": "a.html", "size": 10, "type": "text/html"}])
    assert resp.status_code == 400


# --- 썸네일 / CDN 캐시 --------------------------------------------------------


def real_jpeg(size=(2000, 1500)):
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", size, (10, 120, 200)).save(buf, "JPEG")
    return buf.getvalue()


def write_local(tmp_path, relative_path, data):
    f = tmp_path / "uploads" / relative_path
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(data)


def test_local_upload_generates_derivatives(app_module, client, tmp_path):
    import io

    uid = make_user(app_module)
    login(client, uid)
    resp = client.post(
        "/api/upload",
        data={"photos": (io.BytesIO(real_jpeg()), "a.jpg", "image/jpeg")},
        headers={"X-CSRF-Token": CSRF},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    with app_module.app.app_context():
        photo = app_module.Photo.query.one()
        assert photo.thumb_path and photo.display_path
        assert (photo.width, photo.height) == (2000, 1500)
        assert (tmp_path / "uploads" / photo.thumb_path).exists()


def test_approved_thumb_is_publicly_cacheable_even_when_logged_in(app_module, client, tmp_path):
    uid = make_user(app_module)
    pid = add_photo(app_module, uid, relative_path="2024-05-01/a.jpg")
    write_local(tmp_path, "2024-05-01/a.jpg", real_jpeg())
    login(client, uid)  # 쿠키가 있어도 공유 캐시 가능해야 함

    resp = client.get(f"/photos/{pid}/thumb?v=1")  # 첫 요청에서 지연 생성
    assert resp.status_code == 200
    assert resp.mimetype == "image/webp"
    assert "public" in resp.headers["Cache-Control"] and "immutable" in resp.headers["Cache-Control"]
    assert "Cookie" not in resp.headers.get("Vary", "")
    assert "Set-Cookie" not in resp.headers
    with app_module.app.app_context():
        assert app_module.db.session.get(app_module.Photo, pid).thumb_path

    resp = client.get(f"/photos/{pid}/display")
    assert resp.status_code == 200 and resp.mimetype == "image/webp"


def test_pending_thumb_is_private(app_module, client, tmp_path):
    owner = make_user(app_module)
    pid = add_photo(app_module, owner, status="pending", relative_path="2024-05-01/p.jpg")
    write_local(tmp_path, "2024-05-01/p.jpg", real_jpeg())
    assert client.get(f"/photos/{pid}/thumb").status_code == 404
    login(client, owner)
    resp = client.get(f"/photos/{pid}/thumb")
    assert resp.status_code == 200
    assert resp.headers["Cache-Control"].startswith("private")


def test_unrecognized_file_is_never_served_as_original(app_module, client, tmp_path):
    # 형식을 알 수 없는 파일은 메타데이터를 정리했다고 보장할 수 없으므로 원본을 내보내지 않음
    uid = make_user(app_module)
    pid = add_photo(app_module, uid, relative_path="2024-05-01/x.bin")
    write_local(tmp_path, "2024-05-01/x.bin", b"unknown-format")
    assert client.get(f"/photos/{pid}/thumb").status_code == 404
    assert client.get(f"/photos/{pid}/image").status_code == 404
    assert client.get(f"/photos/{pid}/download").status_code == 404
    with app_module.app.app_context():
        photo = app_module.db.session.get(app_module.Photo, pid)
        assert photo.derive_failed is True and not photo.meta_clean


def test_truncated_jpeg_is_cleaned_and_still_shown(app_module, client, tmp_path):
    # 중간에서 잘린 JPEG도 위치 정보를 지우고 (끝 표시를 붙여) 보여줌
    from io import BytesIO

    from PIL import Image

    import media

    buf = BytesIO()
    Image.effect_noise((800, 600), 60).convert("RGB").save(buf, "JPEG", exif=gps_exif())
    data = buf.getvalue()
    uid = make_user(app_module)
    pid = add_photo(app_module, uid, relative_path="2024-05-01/c.jpg")
    write_local(tmp_path, "2024-05-01/c.jpg", data[: len(data) // 2])
    assert client.get(f"/photos/{pid}/thumb").status_code == 200
    cleaned = (tmp_path / "uploads" / "2024-05-01/c.jpg").read_bytes()
    assert not media.has_private_metadata(cleaned)


def test_archive_api_does_not_sign_originals(app_module, client, gcs):
    uid = make_user(app_module)
    pid = add_photo(app_module, uid)
    photo = client.get("/api/archive/photos?sort=date").get_json()["photos"][0]
    assert photo["thumb_url"].startswith(f"/photos/{pid}/thumb")
    assert photo["url"].startswith(f"/photos/{pid}/display")
    assert "storage.test" not in str(photo)


def test_gcs_complete_generates_thumbnails_and_reject_deletes_them(app_module, client, gcs):
    admin = make_user(app_module, "admin@example.com", is_admin=True)
    login(client, admin)
    item = presign(client, [{"name": "a.jpg", "size": 100, "type": "image/jpeg"}]).get_json()["presigned_urls"][0]
    gcs.put(item["path"], data=real_jpeg())
    assert complete(client, [item["token"]]).status_code == 200
    with app_module.app.app_context():
        photo = app_module.Photo.query.one()
        thumb, display, pid = photo.thumb_path, photo.display_path, photo.id
    assert thumb in gcs.store and display in gcs.store
    assert gcs.store[thumb].content_type == "image/webp"

    client.post("/admin/pending", data={"action": "reject", "photo_id": pid, "csrf_token": CSRF})
    assert thumb not in gcs.store and display not in gcs.store and item["path"] not in gcs.store


def test_admin_backfill_endpoint(app_module, client, tmp_path):
    admin = make_user(app_module, "admin@example.com", is_admin=True)
    for i in range(3):
        add_photo(app_module, admin, relative_path=f"2024-05-01/{i}.jpg")
        write_local(tmp_path, f"2024-05-01/{i}.jpg", real_jpeg((600, 400)))
    add_photo(app_module, admin, relative_path="2024-05-01/bad.jpg")
    write_local(tmp_path, "2024-05-01/bad.jpg", b"broken")
    login(client, admin)

    assert client.get("/admin/derivatives").get_json()["remaining"] == 4
    data = client.post("/admin/derivatives", headers={"X-CSRF-Token": CSRF}).get_json()
    assert data == {"success": True, "processed": 4, "remaining": 0, "failed": 1}
    html = client.get("/admin/pending").get_data(as_text=True)
    assert 'id="backfillBtn"' not in html


# --- 업로드 형식: JPG/PNG/WebP/GIF + HEIC(서버에서 JPG로 변환) -------------------


def real_heic(size=(1200, 900), gps=True):
    from io import BytesIO

    import pillow_heif  # noqa: F401  (opener 등록은 media 모듈이 함)
    from PIL import Image

    img = Image.new("RGB", size, (220, 40, 90))
    exif = Image.Exif()
    if gps:
        exif[0x8825] = {1: "N", 2: (37.0, 33.0, 0.0)}  # GPSInfo
    buf = BytesIO()
    img.save(buf, "HEIF", quality=80, exif=exif.tobytes())
    return buf.getvalue()


def test_local_upload_converts_heic_to_jpeg(app_module, client, tmp_path):
    import io

    from PIL import Image

    uid = make_user(app_module)
    login(client, uid)
    # Windows 브라우저처럼 타입이 비어 있어도 내용으로 판별
    resp = client.post(
        "/api/upload",
        data={"photos": (io.BytesIO(real_heic()), "IMG_0001.HEIC", "application/octet-stream")},
        headers={"X-CSRF-Token": CSRF},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200, resp.get_json()
    with app_module.app.app_context():
        photo = app_module.Photo.query.one()
        assert photo.relative_path.endswith(".jpg")
        assert photo.original_name == "IMG_0001.HEIC"
        assert (photo.width, photo.height) == (1200, 900)
        assert photo.thumb_path and photo.display_path
        converted = tmp_path / "uploads" / photo.relative_path
        assert photo.file_size == converted.stat().st_size
    jpg = Image.open(converted)
    assert jpg.format == "JPEG" and jpg.size == (1200, 900)
    assert 0x8825 not in jpg.getexif()  # GPS 위치 정보 제거
    assert not list((tmp_path / "uploads").rglob("*.HEIC"))  # 원본 HEIC는 삭제


def test_unsupported_format_is_still_rejected(app_module, client):
    import io

    uid = make_user(app_module)
    login(client, uid)
    resp = client.post(
        "/api/upload",
        data={"photos": (io.BytesIO(b"BM" + b"\x00" * 60), "a.bmp", "image/bmp")},
        headers={"X-CSRF-Token": CSRF},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert resp.get_json()["error_type"] == "invalid_type"


def test_presigned_accepts_heic_and_infers_type_from_extension(app_module, client, gcs):
    uid = make_user(app_module)
    login(client, uid)
    resp = presign(client, [{"name": "IMG_0001.HEIC", "size": 10, "type": ""}])
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["presigned_urls"][0]["content_type"] == "image/heic"


def test_gcs_heic_is_converted_and_original_removed(app_module, client, gcs):
    uid = make_user(app_module)
    login(client, uid)
    item = presign(client, [{"name": "IMG.HEIC", "size": 100, "type": "image/heic"}]).get_json()["presigned_urls"][0]
    gcs.put(item["path"], data=real_heic(), content_type="image/heic")
    assert complete(client, [item["token"]]).status_code == 200
    with app_module.app.app_context():
        photo = app_module.Photo.query.one()
        assert photo.relative_path.endswith(".jpg")
        assert gcs.store[photo.relative_path].content_type == "image/jpeg"
        assert photo.thumb_path in gcs.store
    assert item["path"] not in gcs.store


def test_complete_rejects_content_that_is_not_an_image(app_module, client, gcs):
    uid = make_user(app_module)
    login(client, uid)
    item = presign(client, [{"name": "a.jpg", "size": 100, "type": "image/jpeg"}]).get_json()["presigned_urls"][0]
    gcs.put(item["path"], data=b"<html>not an image</html>", content_type="image/jpeg")
    assert complete(client, [item["token"]]).status_code == 400
    assert item["path"] not in gcs.store


def test_download_converts_pending_heic_first(app_module, client, tmp_path):
    uid = make_user(app_module)
    pid = add_photo(app_module, uid, relative_path="2024-05-01/x.heic")
    write_local(tmp_path, "2024-05-01/x.heic", real_heic(gps=False))
    resp = client.get(f"/photos/{pid}/download")
    assert resp.status_code == 200
    assert ".jpg" in resp.headers["Content-Disposition"]
    assert resp.data[:3] == b"\xff\xd8\xff"


def test_upload_page_accept_attribute_lists_only_supported_types(app_module, client):
    uid = make_user(app_module)
    login(client, uid)
    html = client.get("/upload").get_data(as_text=True)
    assert 'accept="image/*"' not in html
    assert "image/heic" in html and ".heic" in html and "image/webp" in html
    assert "image/bmp" not in html


# --- 원본 메타데이터(GPS 등) 제거 ----------------------------------------------


def gps_exif(orientation=None):
    from PIL import Image

    exif = Image.Exif()
    exif[0x010F] = "Apple"
    exif[0x0110] = "iPhone 15 Pro"
    exif[0x8825] = {1: "N", 2: (37.0, 33.0, 12.5), 3: "E", 4: (126.0, 58.0, 41.0)}
    if orientation:
        exif[0x0112] = orientation
    return exif.tobytes()


def jpeg_with_gps(size=(1200, 900), orientation=6):
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", size, (90, 90, 200)).save(buf, "JPEG", quality=90, exif=gps_exif(orientation))
    return buf.getvalue()


def test_local_upload_removes_gps_but_keeps_orientation(app_module, client, tmp_path):
    import io

    from PIL import Image

    import media

    uid = make_user(app_module)
    login(client, uid)
    original = jpeg_with_gps()
    assert media.has_private_metadata(original)
    resp = client.post(
        "/api/upload",
        data={"photos": (io.BytesIO(original), "a.jpg", "image/jpeg")},
        headers={"X-CSRF-Token": CSRF},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    with app_module.app.app_context():
        photo = app_module.Photo.query.one()
        assert photo.meta_clean is True
        stored = (tmp_path / "uploads" / photo.relative_path).read_bytes()
        assert photo.file_size == len(stored)
        assert (photo.width, photo.height) == (900, 1200)  # 회전 반영
    assert not media.has_private_metadata(stored)
    assert Image.open(io.BytesIO(stored)).getexif().get(0x0112) == 6


def test_gcs_upload_overwrites_original_with_clean_copy(app_module, client, gcs):
    import media

    uid = make_user(app_module)
    login(client, uid)
    item = presign(client, [{"name": "a.jpg", "size": 100, "type": "image/jpeg"}]).get_json()["presigned_urls"][0]
    gcs.put(item["path"], data=jpeg_with_gps(), content_type="image/jpeg")
    assert complete(client, [item["token"]]).status_code == 200
    stored = gcs.store[item["path"]]
    assert not media.has_private_metadata(stored.data)
    assert stored.content_type == "image/jpeg"


def test_existing_photo_is_cleaned_before_download_even_if_thumb_exists(app_module, client, tmp_path):
    """이번 변경 전에 썸네일까지 만들어진 예전 사진도 다운로드 직전에 정리됨."""
    import media

    uid = make_user(app_module)
    pid = add_photo(app_module, uid, relative_path="2024-05-01/old.jpg",
                    thumb_path="derived/v1/thumb/x.webp", display_path="derived/v1/display/x.webp")
    write_local(tmp_path, "2024-05-01/old.jpg", jpeg_with_gps())
    resp = client.get(f"/photos/{pid}/download")
    assert resp.status_code == 200
    assert not media.has_private_metadata(resp.data)
    with app_module.app.app_context():
        assert app_module.db.session.get(app_module.Photo, pid).meta_clean is True


def test_original_is_not_served_while_cleaning_is_unfinished(app_module, client, tmp_path, monkeypatch):
    uid = make_user(app_module)
    pid = add_photo(app_module, uid, relative_path="2024-05-01/slow.jpg")
    write_local(tmp_path, "2024-05-01/slow.jpg", jpeg_with_gps())
    # 처리가 시간 안에 끝나지 못한 상황
    monkeypatch.setattr(app_module, "process_photos", lambda photos, **kw: 0)
    for url in (f"/photos/{pid}/download", f"/photos/{pid}/image"):
        resp = client.get(url)
        assert resp.status_code == 503
        assert resp.headers["Retry-After"]


def test_backfill_cleans_old_photos_and_can_retry_failed(app_module, client, tmp_path):
    import media

    admin = make_user(app_module, "admin@example.com", is_admin=True)
    pid = add_photo(app_module, admin, relative_path="2024-05-01/old.jpg",
                    thumb_path="derived/v1/thumb/x.webp", display_path="derived/v1/display/x.webp")
    write_local(tmp_path, "2024-05-01/old.jpg", jpeg_with_gps())
    failed = add_photo(app_module, admin, relative_path="2024-05-01/f.jpg", derive_failed=True)
    write_local(tmp_path, "2024-05-01/f.jpg", jpeg_with_gps())
    login(client, admin)

    html = client.get("/admin/pending").get_data(as_text=True)
    assert 'id="backfillBtn"' in html and 'id="retryFailedBtn"' in html

    data = client.post("/admin/derivatives", headers={"X-CSRF-Token": CSRF}).get_json()
    assert data["remaining"] == 0 and data["failed"] == 1
    assert not media.has_private_metadata((tmp_path / "uploads/2024-05-01/old.jpg").read_bytes())

    data = client.post("/admin/derivatives", json={"retry_failed": True}, headers={"X-CSRF-Token": CSRF}).get_json()
    assert data == {"success": True, "processed": 1, "remaining": 0, "failed": 0}
    with app_module.app.app_context():
        photo = app_module.db.session.get(app_module.Photo, failed)
        assert photo.meta_clean and photo.thumb_path
    assert pid
