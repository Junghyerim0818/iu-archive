"""원본 이미지에서 썸네일/화면용 WebP 파생 이미지를 만드는 순수 함수 모음 (스토리지/DB와 무관)."""

from dataclasses import dataclass
from io import BytesIO
from typing import Optional

from PIL import Image, ImageOps

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


# 업로드 허용 형식 = 썸네일을 확실히 만들 수 있는 형식
SUPPORTED_FORMATS = ("jpeg", "png", "webp", "gif")
SUPPORTED_MIME_TYPES = {"image/jpeg", "image/jpg", "image/pjpeg", "image/png", "image/webp", "image/gif"}
SUPPORTED_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif")
SNIFF_BYTES = 32  # 형식 판별에 필요한 파일 앞부분 길이

UNSUPPORTED_MESSAGE = "JPG, PNG, WebP, GIF 파일만 업로드할 수 있어요."
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
    return HEIC_MESSAGE if ("heic" in hint or "heif" in hint) else UNSUPPORTED_MESSAGE


class ImageProcessingError(Exception):
    pass


@dataclass
class Derivatives:
    width: int  # 원본(회전 보정 후) 크기
    height: int
    files: dict  # kind -> WebP bytes


def _target_size(width: int, height: int, short_side, long_max) -> tuple[int, int]:
    scale = 1.0
    if short_side:
        scale = min(scale, short_side / min(width, height))
    scale = min(scale, long_max / max(width, height))
    return max(1, round(width * scale)), max(1, round(height * scale))


def make_derivatives(data: bytes) -> Derivatives:
    """이미지 바이트 → 썸네일/화면용 WebP. 열 수 없는 형식(HEIC 등)이면 ImageProcessingError."""
    try:
        with Image.open(BytesIO(data)) as img:
            # JPEG는 필요한 크기만큼만 디코딩 (큰 사진에서 수 배 빠르고 메모리도 적게 씀)
            longest_needed = max(v[1] for v in VARIANTS.values())
            img.draft("RGB", (longest_needed, longest_needed))
            img = ImageOps.exif_transpose(img)  # 휴대폰 사진 회전 정보 반영
            width, height = img.size

            has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
            img = img.convert("RGBA" if has_alpha else "RGB")

            files = {}
            for kind, (short_side, long_max, quality) in VARIANTS.items():
                size = _target_size(img.width, img.height, short_side, long_max)
                resized = img if size == img.size else img.resize(size, Image.LANCZOS)
                buf = BytesIO()
                resized.save(buf, "WEBP", quality=quality, method=4)
                files[kind] = buf.getvalue()
            return Derivatives(width=width, height=height, files=files)
    except Image.DecompressionBombError as exc:
        raise ImageProcessingError(f"이미지가 너무 큽니다: {exc}") from exc
    except (OSError, ValueError, SyntaxError) as exc:
        # UnidentifiedImageError는 OSError의 하위 클래스
        raise ImageProcessingError(f"이미지를 처리할 수 없습니다: {exc}") from exc
