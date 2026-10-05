"""Downscale a large image before sending it to the vision model.

Claude's vision API has its own per-image size limits, and a 20 MB photo wastes
tokens and request budget. We shrink the longest edge and re-encode so the read is
fast and reliable. Best-effort: if Pillow is missing or the bytes aren't a decodable
image, the original is returned unchanged. PDFs are never touched (Claude reads them
natively as document blocks).
"""
from __future__ import annotations

from io import BytesIO

MAX_DIM = 1600          # longest edge, px — comfortably within the vision API limits
SOFT_MAX_BYTES = 4 * 1024 * 1024  # re-encode even small-dim images above this
JPEG_QUALITY = 88


def downscale_image(data: bytes, media_type: str) -> tuple[bytes, str]:
    """Return (data, media_type), shrunk to <=MAX_DIM and re-encoded if oversized."""
    if media_type == "application/pdf":
        return data, media_type
    if len(data) <= SOFT_MAX_BYTES:
        # Still might be huge-dimensioned; cheap to check below, but skip tiny files.
        pass
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow is a declared dependency
        return data, media_type
    try:
        im = Image.open(BytesIO(data))
        im.load()
    except Exception:
        return data, media_type  # not a decodable image — leave it to the API

    w, h = im.size
    if max(w, h) <= MAX_DIM and len(data) <= SOFT_MAX_BYTES:
        return data, media_type  # already small enough

    if max(w, h) > MAX_DIM:
        scale = MAX_DIM / max(w, h)
        im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))))
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    out = BytesIO()
    im.save(out, format="JPEG", quality=JPEG_QUALITY)
    return out.getvalue(), "image/jpeg"
