import sys
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from media import ImageProcessingError, make_derivatives, sniff_image_format, unsupported_reason  # noqa: E402


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


@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "WEBP", "GIF"])
def test_sniff_supported_formats(fmt):
    buf = BytesIO()
    Image.new("RGB", (10, 10)).save(buf, fmt)
    assert sniff_image_format(buf.getvalue()[:32]) == fmt.lower()


def test_sniff_heic_avif_and_unknown():
    assert sniff_image_format(b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00") == "heic"
    assert sniff_image_format(b"\x00\x00\x00\x18ftypmif1\x00\x00\x00\x00") == "heic"
    assert sniff_image_format(b"\x00\x00\x00\x1cftypavif\x00\x00\x00\x00") == "avif"
    assert sniff_image_format(b"<svg xmlns=...>") is None
    assert sniff_image_format(b"") is None


def test_heic_is_converted_to_jpeg_without_exif():
    import pillow_heif  # noqa: F401

    img = Image.new("RGB", (1600, 1200), (30, 160, 60))
    exif = Image.Exif()
    exif[0x8825] = {1: "N", 2: (37.0, 33.0, 0.0)}
    buf = BytesIO()
    img.save(buf, "HEIF", quality=80, exif=exif.tobytes())

    result = make_derivatives(buf.getvalue())
    assert (result.width, result.height) == (1600, 1200)
    jpg = Image.open(BytesIO(result.converted_jpeg))
    assert jpg.format == "JPEG" and jpg.size == (1600, 1200)
    assert len(jpg.getexif()) == 0
    assert min(open_webp(result.files["thumb"]).size) == 480


def test_non_heic_has_no_converted_copy():
    assert make_derivatives(jpeg_bytes((800, 600))).converted_jpeg is None


def test_large_jpeg_reports_original_size_even_with_draft_decoding():
    # draft 디코딩으로 축소해서 읽어도 원본 크기를 기록해야 함
    result = make_derivatives(jpeg_bytes((8000, 6000)))
    assert (result.width, result.height) == (8000, 6000)


def test_unsupported_reason():
    assert "JPG" in unsupported_reason(None, "a.bmp", "image/bmp")
