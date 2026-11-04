"""원본 이미지에서 썸네일/화면용 WebP 파생 이미지를 만드는 순수 함수 모음 (스토리지/DB와 무관)."""

from dataclasses import dataclass
from io import BytesIO

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
