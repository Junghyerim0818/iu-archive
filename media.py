"""원본 이미지에서 썸네일/화면용 WebP 파생 이미지를 만드는 순수 함수 모음 (스토리지/DB와 무관)."""

import struct
from dataclasses import dataclass
from io import BytesIO
from typing import Optional

from PIL import Image, ImageOps

try:
    # HEIC(아이폰 고효율 사진) 디코딩 지원
    from pillow_heif import register_heif_opener

    register_heif_opener()
    HEIF_SUPPORTED = True
except ImportError:  # pragma: no cover - 의존성이 없으면 HEIC만 거부
    HEIF_SUPPORTED = False

# 압축 폭탄 방지: 이 픽셀 수를 넘는 이미지는 처리하지 않음 (약 8000x10000)
Image.MAX_IMAGE_PIXELS = 80_000_000

# 파생 이미지 규칙이 바뀌면 숫자를 올린다 → 저장 경로/URL이 바뀌어 캐시가 자연스럽게 갱신됨
DERIVATIVE_VERSION = 1

# kind: (짧은 변 목표 px, 긴 변 최대 px, WebP 품질)
VARIANTS = {
    # 갤러리 정사각형 셀(최대 ~300px, 레티나 2x)을 object-fit: cover로 채워도 선명하도록 짧은 변 기준
    "thumb": (480, 1440, 72),
    # 모달 확대 보기용
    "display": (None, 1920, 82),
}


# 업로드 허용 형식 = 썸네일을 확실히 만들 수 있는 형식 (HEIC는 서버에서 JPG로 변환해 보관)
SUPPORTED_FORMATS = ("jpeg", "png", "webp", "gif") + (("heic",) if HEIF_SUPPORTED else ())
SUPPORTED_MIME_TYPES = {"image/jpeg", "image/jpg", "image/pjpeg", "image/png", "image/webp", "image/gif"} | (
    {"image/heic", "image/heif"} if HEIF_SUPPORTED else set()
)
SUPPORTED_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif") + ((".heic", ".heif") if HEIF_SUPPORTED else ())
SNIFF_BYTES = 32  # 형식 판별에 필요한 파일 앞부분 길이
CONVERTED_JPEG_QUALITY = 90

UNSUPPORTED_MESSAGE = (
    "JPG, PNG, WebP, GIF" + (", HEIC" if HEIF_SUPPORTED else "") + " 파일만 업로드할 수 있어요."
)
HEIC_MESSAGE = (
    "HEIC(아이폰 고효율 사진) 파일은 지원하지 않아요. JPG로 변환해서 올려주세요. "
    "(아이폰 설정 > 카메라 > 포맷 > '높은 호환성'으로 바꾸면 처음부터 JPG로 저장돼요)"
)

_HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1"}
_AVIF_BRANDS = {b"avif", b"avis"}


def sniff_image_format(head: bytes) -> Optional[str]:
    """파일 앞부분(매직 넘버)으로 실제 형식을 판별. 확장자/Content-Type은 믿지 않는다."""
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in _HEIF_BRANDS:
            return "heic"
        if brand in _AVIF_BRANDS:
            return "avif"
    return None


def unsupported_reason(fmt: Optional[str] = None, name: str = "", content_type: str = "") -> str:
    hint = f"{fmt or ''} {name} {content_type}".lower()
    if not HEIF_SUPPORTED and ("heic" in hint or "heif" in hint):
        return HEIC_MESSAGE
    return UNSUPPORTED_MESSAGE


class ImageProcessingError(Exception):
    pass


@dataclass
class Derivatives:
    width: int  # 원본(회전 보정 후) 크기
    height: int
    files: dict  # kind -> WebP bytes
    converted_jpeg: Optional[bytes] = None  # HEIC 원본을 대신할 JPG (그 외 형식은 None)


def _target_size(width: int, height: int, short_side, long_max) -> tuple[int, int]:
    scale = 1.0
    if short_side:
        scale = min(scale, short_side / min(width, height))
    scale = min(scale, long_max / max(width, height))
    return max(1, round(width * scale)), max(1, round(height * scale))


def make_derivatives(data: bytes) -> Derivatives:
    """이미지 바이트 → 썸네일/화면용 WebP (+ HEIC면 원본 대체용 JPG).

    열 수 없는 파일이면 ImageProcessingError.
    """
    fmt = sniff_image_format(data[:SNIFF_BYTES])
    try:
        with Image.open(BytesIO(data)) as img:
            # 원본 크기 (EXIF 회전이 있으면 가로/세로가 바뀜)
            width, height = img.size
            if img.getexif().get(0x0112) in (5, 6, 7, 8):
                width, height = height, width
            icc_profile = img.info.get("icc_profile")  # 아이폰 Display P3 등 색 정보 유지

            if fmt == "jpeg":
                # 필요한 크기만큼만 디코딩 (큰 사진에서 수 배 빠르고 메모리도 적게 씀)
                longest_needed = max(v[1] for v in VARIANTS.values())
                img.draft("RGB", (longest_needed, longest_needed))
            img = ImageOps.exif_transpose(img)  # 휴대폰 사진 회전 정보 반영

            has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
            img = img.convert("RGBA" if has_alpha else "RGB")

            converted_jpeg = None
            if fmt == "heic":
                # EXIF(GPS 위치 등)는 넣지 않음. 회전은 이미 픽셀에 반영됨
                buf = BytesIO()
                img.convert("RGB").save(
                    buf, "JPEG", quality=CONVERTED_JPEG_QUALITY, progressive=True, icc_profile=icc_profile
                )
                converted_jpeg = buf.getvalue()

            files = {}
            for kind, (short_side, long_max, quality) in VARIANTS.items():
                size = _target_size(img.width, img.height, short_side, long_max)
                resized = img if size == img.size else img.resize(size, Image.LANCZOS)
                buf = BytesIO()
                resized.save(buf, "WEBP", quality=quality, method=4, icc_profile=icc_profile)
                files[kind] = buf.getvalue()
            return Derivatives(width=width, height=height, files=files, converted_jpeg=converted_jpeg)
    except Image.DecompressionBombError as exc:
        raise ImageProcessingError(f"이미지가 너무 큽니다: {exc}") from exc
    except (OSError, ValueError, SyntaxError) as exc:
        # UnidentifiedImageError는 OSError의 하위 클래스
        raise ImageProcessingError(f"이미지를 처리할 수 없습니다: {exc}") from exc


# ---------------------------------------------------------------------------
# 메타데이터(EXIF/XMP 등) 제거 — 촬영 위치(GPS), 기기 정보, 촬영 시각 노출 방지
# ---------------------------------------------------------------------------
#
# 픽셀 데이터는 그대로 두고 메타데이터 블록만 잘라낸다 (재압축 없음 → 화질 손실 0).
# 단, JPEG의 회전 정보(Orientation)는 남겨야 사진이 옆으로 눕지 않으므로 그 값 하나만 다시 넣는다.
# 파일 구조가 이상해서 잘라낼 수 없으면 Pillow로 다시 저장한다(메타데이터 없이).

_JPEG_DROP_APP = {0xE1, 0xED}  # APP1: EXIF·XMP, APP13: Photoshop/IPTC
_PNG_DROP_CHUNKS = {b"eXIf", b"tEXt", b"zTXt", b"iTXt", b"tIME"}
_WEBP_DROP_CHUNKS = {b"EXIF", b"XMP "}


class MetadataError(ValueError):
    pass


def strip_metadata(data: bytes) -> bytes:
    """이미지에서 개인정보성 메타데이터를 제거한 바이트를 반환. 이미 깨끗하면 같은 내용을 반환.

    HEIC는 JPG 변환 과정에서 메타데이터를 넣지 않으므로 여기서는 건드리지 않는다.
    GIF는 위치 정보를 담는 표준 블록이 없어 그대로 둔다.
    """
    fmt = sniff_image_format(data[:SNIFF_BYTES])
    stripper = {"jpeg": _strip_jpeg, "png": _strip_png, "webp": _strip_webp}.get(fmt)
    if stripper is None:
        return data
    try:
        return stripper(data)
    except (MetadataError, struct.error, IndexError):
        return _reencode_without_metadata(data, fmt)


def has_private_metadata(data: bytes) -> bool:
    """테스트/점검용: GPS 등 EXIF 태그가 남아 있는지 (Orientation만 있으면 False)."""
    with Image.open(BytesIO(data)) as img:
        exif = img.getexif()
        tags = set(exif.keys()) - {0x0112}
        return bool(tags) or bool(exif.get_ifd(0x8825)) or "xmp" in img.info or "XML:com.adobe.xmp" in img.info


def _jpeg_orientation(data: bytes) -> int:
    try:
        with Image.open(BytesIO(data)) as img:
            return int(img.getexif().get(0x0112) or 1)
    except Exception:
        return 1


def _orientation_app1(orientation: int) -> bytes:
    exif = Image.Exif()
    exif[0x0112] = orientation
    payload = exif.tobytes()  # b"Exif\0\0" + TIFF
    return b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload


def _find_next_marker(data: bytes, pos: int) -> int:
    """SOS 뒤 압축 데이터에서 다음 마커 위치 (FF00 바이트 스터핑과 RSTn은 건너뜀)."""
    while True:
        pos = data.find(b"\xff", pos)
        if pos < 0 or pos + 1 >= len(data):
            return len(data)
        nxt = data[pos + 1]
        if nxt == 0x00 or 0xD0 <= nxt <= 0xD7 or nxt == 0xFF:
            pos += 1 if nxt == 0xFF else 2
            continue
        return pos


def _strip_jpeg(data: bytes) -> bytes:
    if not data.startswith(b"\xff\xd8"):
        raise MetadataError("JPEG SOI 없음")
    orientation = _jpeg_orientation(data)
    out = bytearray(b"\xff\xd8")
    orientation_written = orientation == 1
    pos = 2
    while pos < len(data):
        if data[pos] != 0xFF:
            raise MetadataError(f"잘못된 마커 위치 {pos}")
        marker = data[pos + 1]
        if marker == 0xFF:  # 채움 바이트
            pos += 1
            continue
        if marker == 0xD9:  # EOI → 여기서 끝. 뒤에 붙은 데이터(MPF 보조 이미지 등, 자체 EXIF 포함)는 버림
            if not orientation_written:
                out += _orientation_app1(orientation)
            out += b"\xff\xd9"
            return bytes(out)
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:
            out += data[pos:pos + 2]
            pos += 2
            continue

        (length,) = struct.unpack(">H", data[pos + 2:pos + 4])
        end = pos + 2 + length
        if length < 2 or end > len(data):
            raise MetadataError("세그먼트 길이 오류")
        segment = data[pos:end]

        if not orientation_written and marker != 0xE0:
            # JFIF(APP0) 바로 뒤, 다른 세그먼트보다 앞에 회전 정보만 담은 EXIF를 넣음
            out += _orientation_app1(orientation)
            orientation_written = True

        drop = (
            marker in _JPEG_DROP_APP
            or marker == 0xFE  # 주석
            or (marker == 0xE2 and not segment[4:16] == b"ICC_PROFILE\x00")  # MPF 등 (ICC 색 정보는 유지)
            or (0xE3 <= marker <= 0xEF and marker != 0xEE)  # 기타 제조사 APP (Adobe APP14는 색 변환에 필요)
        )
        if not drop:
            out += segment
        pos = end

        if marker == 0xDA:  # SOS: 압축 데이터는 그대로 복사
            scan_end = _find_next_marker(data, pos)
            out += data[pos:scan_end]
            pos = scan_end
    # EOI 없이 끝난 파일: 있는 데까지 + EOI
    out += b"\xff\xd9"
    return bytes(out)


def _strip_png(data: bytes) -> bytes:
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise MetadataError("PNG 시그니처 없음")
    out = bytearray(data[:8])
    pos = 8
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        ctype = data[pos + 4:pos + 8]
        end = pos + 12 + length
        if end > len(data):
            raise MetadataError("PNG 청크 길이 오류")
        if ctype not in _PNG_DROP_CHUNKS:
            out += data[pos:end]
        pos = end
        if ctype == b"IEND":
            return bytes(out)
    raise MetadataError("PNG IEND 없음")


def _strip_webp(data: bytes) -> bytes:
    if data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise MetadataError("WebP 헤더 없음")
    out = bytearray(b"RIFF\x00\x00\x00\x00WEBP")
    pos = 12
    while pos + 8 <= len(data):
        fourcc = data[pos:pos + 4]
        (size,) = struct.unpack("<I", data[pos + 4:pos + 8])
        end = pos + 8 + size + (size & 1)
        if pos + 8 + size > len(data):
            raise MetadataError("WebP 청크 길이 오류")
        chunk = bytearray(data[pos:min(end, len(data))])
        if fourcc == b"VP8X":
            chunk[8] &= ~(0x08 | 0x04) & 0xFF  # EXIF / XMP 플래그 해제
        if fourcc not in _WEBP_DROP_CHUNKS:
            out += chunk
        pos = end
    struct.pack_into("<I", out, 4, len(out) - 8)
    return bytes(out)


def _reencode_without_metadata(data: bytes, fmt: str) -> bytes:
    """구조를 해석할 수 없는 파일의 최후 수단: 픽셀만 다시 저장 (회전은 픽셀에 반영)."""
    try:
        with Image.open(BytesIO(data)) as img:
            icc_profile = img.info.get("icc_profile")
            img = ImageOps.exif_transpose(img)
            buf = BytesIO()
            if fmt == "jpeg":
                img.convert("RGB").save(buf, "JPEG", quality=95, icc_profile=icc_profile)
            elif fmt == "png":
                img.save(buf, "PNG", icc_profile=icc_profile)
            else:
                img.save(buf, "WEBP", quality=95, icc_profile=icc_profile)
            return buf.getvalue()
    except Exception as exc:
        raise ImageProcessingError(f"메타데이터를 제거할 수 없습니다: {exc}") from exc
