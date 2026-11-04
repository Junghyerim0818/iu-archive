import sys
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from media import ImageProcessingError, make_derivatives  # noqa: E402


def jpeg_bytes(size=(4000, 3000), orientation=None, color=(200, 30, 30)):
    img = Image.new("RGB", size, color)
    buf = BytesIO()
    exif = Image.Exif()
    if orientation:
        exif[0x0112] = orientation
    img.save(buf, "JPEG", exif=exif.tobytes())
    return buf.getvalue()


def open_webp(data):
    img = Image.open(BytesIO(data))
    assert img.format == "WEBP"
    return img


def test_thumb_short_side_and_display_long_side():
    result = make_derivatives(jpeg_bytes((4000, 3000)))
    assert (result.width, result.height) == (4000, 3000)
    thumb = open_webp(result.files["thumb"])
    display = open_webp(result.files["display"])
    assert min(thumb.size) == 480
    assert max(display.size) == 1920
    assert len(result.files["thumb"]) < 50_000


def test_exif_orientation_is_applied():
    # orientation 6 = 90도 회전 → 가로 사진이 세로로
    result = make_derivatives(jpeg_bytes((4000, 3000), orientation=6))
    assert (result.width, result.height) == (3000, 4000)
    thumb = open_webp(result.files["thumb"])
    assert thumb.size[0] < thumb.size[1]


def test_small_image_is_not_upscaled():
    result = make_derivatives(jpeg_bytes((300, 200)))
    assert open_webp(result.files["thumb"]).size == (300, 200)
    assert open_webp(result.files["display"]).size == (300, 200)


def test_png_with_alpha_keeps_transparency():
    img = Image.new("RGBA", (800, 800), (0, 0, 0, 0))
    buf = BytesIO()
    img.save(buf, "PNG")
    thumb = open_webp(make_derivatives(buf.getvalue()).files["thumb"])
    assert thumb.mode == "RGBA"


def test_garbage_raises_processing_error():
    with pytest.raises(ImageProcessingError):
        make_derivatives(b"not an image at all")
