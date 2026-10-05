"""Fetch a PDF / photo from an external URL, with guardrails (seller-provided links).

Used ONLY on the seller side (T•ai): the seller pastes a link, we fetch it so the
vision model can read it and we can confirm with the owner. NEVER used to answer
buyers — their side stays strictly DB-grounded.

The URL is user-supplied, so fetching it server-side is risky. Guardrails:
  - http/https only
  - SSRF: reject hosts that resolve to private/loopback/link-local/reserved IPs
  - redirects ARE followed, but every hop is re-validated for SSRF
  - hard size cap (read-with-limit) and a timeout
  - content-type allowlist (images + PDF)

Convenience: Google Drive share links (a viewer page) are normalized to the direct
download endpoint first. Google Photos has no such direct path — those stay attach-only.
"""
from __future__ import annotations

import ipaddress
import re
import socket
import urllib.request
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlparse

IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
PDF_TYPE = "application/pdf"
ALLOWED_TYPES = IMAGE_TYPES | {PDF_TYPE}


def sniff_media_type(data: bytes) -> str | None:
    """Guess an image/PDF MIME from the leading MAGIC bytes — needed because Google Drive
    (and some CDNs) serve a real PDF/JPEG as 'application/octet-stream', which the declared
    content-type allowlist would wrongly reject. Returns None if the bytes aren't a known
    image/PDF (e.g. an HTML login page)."""
    if data[:5] == b"%PDF-":
        return PDF_TYPE
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None

DEFAULT_MAX_BYTES = 20 * 1024 * 1024  # 20 MB — per-file cap (never read a file partially)
DEFAULT_BATCH_BUDGET = 20 * 1024 * 1024  # 20 MB — total across several files in one go
DEFAULT_TIMEOUT = 20.0


class FetchError(Exception):
    """Link could not be fetched safely (blocked, too big, unsupported, unreachable)."""


def is_drive_folder(url: str) -> bool:
    """A Google Drive FOLDER link (not a file) — can't be fetched without the Drive API."""
    p = urlparse(url)
    return (p.hostname or "").lower() == "drive.google.com" and "/folders/" in p.path


@dataclass
class FetchedMedia:
    data: bytes
    media_type: str  # one of ALLOWED_TYPES
    source_url: str
    filename: str = ""  # from Content-Disposition or the URL — shown back to the seller

    @property
    def kind(self) -> str:
        return "pdf" if self.media_type == PDF_TYPE else "image"

    @property
    def display_name(self) -> str:
        return self.filename or ("tài liệu" if self.kind == "pdf" else "ảnh")


def _url_basename(url: str) -> str:
    name = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    return name if "." in name else ""  # only use it if it looks like a real filename


def _looks_broken(name: str) -> bool:
    """A filename we couldn't decode cleanly. Real names don't contain control chars,
    the replacement char, or box-drawing/symbol glyphs — those signal mojibake (e.g.
    Google Drive double-encodes non-Latin names into valid-but-garbage UTF-8)."""
    for c in name:
        o = ord(c)
        if o < 0x20 or 0x7F <= o <= 0x9F:      # C0/C1 control chars, DEL
            return True
        if o == 0xFFFD or 0x2500 <= o <= 0x25FF:  # replacement char, box-drawing/blocks
            return True
    return False


def _filename_from_headers(headers) -> str:
    """Parse a Content-Disposition filename, handling UTF-8 (Vietnamese names) correctly.

    Prefer the RFC 5987 `filename*=UTF-8''...` form. For a plain `filename="..."`, urllib
    has decoded the header as latin-1, so undo that to recover real UTF-8 bytes. If the
    result is still garbage (some services, incl. Google Drive, send non-UTF-8 mojibake),
    return "" so the caller falls back to a clean generic name instead of showing junk.
    """
    cd = headers.get("Content-Disposition", "") or ""
    m = re.search(r"filename\*\s*=\s*([\w.\-]+)''([^;]+)", cd, re.I)
    if m:
        try:
            name = unquote(m.group(2), encoding=m.group(1) or "utf-8", errors="strict").strip()
            if name and not _looks_broken(name):
                return name
        except Exception:
            pass
    m = re.search(r'filename\s*=\s*"?([^";]+)"?', cd, re.I)
    if m:
        name = m.group(1).strip()
        try:  # undo the latin-1 header decode to recover the real UTF-8 bytes
            name = name.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return ""  # not UTF-8 — unrecoverable, let the caller use a generic name
        if name and not _looks_broken(name):
            return name
    return ""


def _is_blocked_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    return (
        addr.is_private or addr.is_loopback or addr.is_link_local
        or addr.is_reserved or addr.is_multicast or addr.is_unspecified
    )


def normalize_media_url(url: str) -> str:
    """Rewrite a Google Drive share link into its direct-download endpoint.

    `drive.google.com/file/d/<ID>/view`, `/open?id=<ID>`, `/uc?...id=<ID>` all become
    `drive.usercontent.google.com/download?id=<ID>&export=download&confirm=t` (the
    `confirm=t` skips Drive's large-file scan interstitial). Other URLs pass through.
    """
    p = urlparse(url)
    if (p.hostname or "").lower() in ("drive.google.com", "docs.google.com"):
        m = re.search(r"/file/d/([\w-]+)", p.path)
        file_id = m.group(1) if m else parse_qs(p.query).get("id", [None])[0]
        if file_id:
            return (
                f"https://drive.usercontent.google.com/download"
                f"?id={file_id}&export=download&confirm=t"
            )
    return url


def assert_public_url(url: str) -> None:
    """Validate scheme + host (SSRF) WITHOUT fetching. Raises FetchError if unsafe."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise FetchError(f"Chỉ chấp nhận http/https (không phải {parsed.scheme or 'rỗng'!r}).")
    host = parsed.hostname
    if not host:
        raise FetchError("URL không có hostname.")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port)
    except socket.gaierror as e:
        raise FetchError(f"Không phân giải được tên miền: {host}") from e
    for info in infos:
        ip = info[4][0]
        if _is_blocked_ip(ip):
            raise FetchError(f"Địa chỉ nội bộ bị chặn ({ip}).")


def fetch_raw(url: str, *, max_bytes: int = 10 * 1024 * 1024, timeout: float = DEFAULT_TIMEOUT
              ) -> tuple[bytes, str]:
    """Fetch ANY file's bytes + content-type with the same SSRF/redirect/size guards as
    fetch_media but WITHOUT the image/PDF allowlist — for data files (CSV/Excel/Sheets)."""
    url = normalize_media_url(url)
    assert_public_url(url)
    opener = urllib.request.build_opener(_ValidatingRedirect)
    req = urllib.request.Request(url, headers={"User-Agent": "AOND/1.0"})
    try:
        with opener.open(req, timeout=timeout) as resp:
            media_type = (resp.headers.get_content_type() or "").lower()
            data = resp.read(max_bytes + 1)
    except FetchError:
        raise
    except Exception as e:
        raise FetchError(f"Không tải được link: {e}") from e
    if len(data) > max_bytes:
        raise FetchError(f"Tệp quá lớn (>{max_bytes // (1024 * 1024)}MB).")
    return data, media_type


class _ValidatingRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but re-check each hop's host against the SSRF rules."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        assert_public_url(newurl)  # re-validate every hop (urllib caps the count)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_media(
    url: str, *, max_bytes: int = DEFAULT_MAX_BYTES, timeout: float = DEFAULT_TIMEOUT
) -> FetchedMedia:
    """Fetch an image/PDF URL with guardrails. Raises FetchError on any problem."""
    if is_drive_folder(url):
        raise FetchError(
            "Đây là link THƯ MỤC Drive (nhiều file), không phải một file ảnh/PDF. "
            "Anh/chị gửi từng ảnh/file hoặc link từng tấm giúp em nhé."
        )
    url = normalize_media_url(url)
    assert_public_url(url)
    opener = urllib.request.build_opener(_ValidatingRedirect)
    req = urllib.request.Request(url, headers={"User-Agent": "AOND/1.0"})
    try:
        with opener.open(req, timeout=timeout) as resp:
            declared = (resp.headers.get_content_type() or "").lower()
            filename = _filename_from_headers(resp.headers)
            data = resp.read(max_bytes + 1)  # one over the cap to detect oversize
    except FetchError:
        raise
    except Exception as e:  # urllib raises many error types
        raise FetchError(f"Không tải được link: {e}") from e
    # Trust the declared type only if it's allowed; otherwise sniff the magic bytes — Drive
    # serves real PDFs/images as 'application/octet-stream', so the header alone would reject.
    media_type = declared if declared in ALLOWED_TYPES else (sniff_media_type(data) or "")
    if media_type not in ALLOWED_TYPES:
        if declared in ("text/html", "application/xhtml+xml"):
            raise FetchError("Link dẫn tới trang xem/đăng nhập, không phải file ảnh/PDF trực tiếp.")
        raise FetchError(f"Loại tệp không hỗ trợ: {declared or 'không rõ'}.")
    if len(data) > max_bytes:
        raise FetchError(f"Tệp quá lớn (>{max_bytes // (1024 * 1024)}MB).")
    return FetchedMedia(
        data=data,
        media_type=media_type,
        source_url=url,
        filename=filename or _url_basename(url),
    )
