from io import BytesIO

from PIL import Image

from aioi.imaging import MAX_DIM, downscale_image


def _png(w: int, h: int) -> bytes:
    out = BytesIO()
    Image.new("RGB", (w, h), (180, 180, 180)).save(out, format="PNG")
    return out.getvalue()


def test_downscales_oversized_image_to_jpeg():
    big = _png(4000, 3000)
    data, media_type = downscale_image(big, "image/png")
    assert media_type == "image/jpeg"
    w, h = Image.open(BytesIO(data)).size
    assert max(w, h) == MAX_DIM  # longest edge clamped


def test_small_image_left_untouched():
    small = _png(800, 600)
    data, media_type = downscale_image(small, "image/png")
    assert (data, media_type) == (small, "image/png")


def test_pdf_never_touched():
    blob = b"%PDF-1.4 fake"
    assert downscale_image(blob, "application/pdf") == (blob, "application/pdf")
