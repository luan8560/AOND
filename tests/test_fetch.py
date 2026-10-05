import pytest

from aioi.fetch import (
    FetchedMedia,
    FetchError,
    _looks_broken,
    assert_public_url,
    is_drive_folder,
    normalize_media_url,
    sniff_media_type,
)


def test_rejects_non_http_scheme():
    with pytest.raises(FetchError):
        assert_public_url("ftp://example.com/x.pdf")


def test_sniff_media_type_from_magic_bytes():
    # Drive serves these as application/octet-stream; we must recognize them by content.
    assert sniff_media_type(b"%PDF-1.7\r\n%abc") == "application/pdf"
    assert sniff_media_type(b"\xff\xd8\xff\xe0junk") == "image/jpeg"
    assert sniff_media_type(b"\x89PNG\r\n\x1a\n....") == "image/png"
    assert sniff_media_type(b"GIF89a....") == "image/gif"
    assert sniff_media_type(b"RIFF\x00\x00\x00\x00WEBPvp8") == "image/webp"
    assert sniff_media_type(b"<!DOCTYPE html><html>login</html>") is None  # not media
    with pytest.raises(FetchError):
        assert_public_url("file:///etc/passwd")


def test_rejects_loopback_and_internal_ips():
    for url in (
        "http://127.0.0.1/x.jpg",
        "http://169.254.169.254/latest/meta-data",  # cloud metadata
        "http://10.0.0.5/x.png",
        "http://[::1]/x.pdf",
    ):
        with pytest.raises(FetchError):
            assert_public_url(url)


def test_allows_public_ip_literal():
    # Public IP literal passes validation (no network fetch happens here).
    assert_public_url("https://8.8.8.8/photo.jpg")


def test_fetchedmedia_kind():
    assert FetchedMedia(b"x", "application/pdf", "u").kind == "pdf"
    assert FetchedMedia(b"x", "image/jpeg", "u").kind == "image"


def test_normalizes_google_drive_share_links():
    direct = "https://drive.usercontent.google.com/download?id=ABC123&export=download&confirm=t"
    assert normalize_media_url("https://drive.google.com/file/d/ABC123/view?usp=sharing") == direct
    assert normalize_media_url("https://drive.google.com/open?id=ABC123") == direct
    assert normalize_media_url("https://drive.google.com/uc?export=download&id=ABC123") == direct


def test_normalize_leaves_direct_links_untouched():
    url = "https://upload.wikimedia.org/wikipedia/commons/3/3a/Cat03.jpg"
    assert normalize_media_url(url) == url


def test_display_name_falls_back_by_kind():
    assert FetchedMedia(b"x", "image/jpeg", "u", filename="mat_tien.jpg").display_name == "mat_tien.jpg"
    assert FetchedMedia(b"x", "image/jpeg", "u").display_name == "ảnh"
    assert FetchedMedia(b"x", "application/pdf", "u").display_name == "tài liệu"


def test_flags_mojibake_filenames():
    assert _looks_broken("m║u nhà.jpg")   # box-drawing junk (Drive double-encode)
    assert _looks_broken("file\x01.jpg")             # control char
    assert not _looks_broken("Sổ_Hồng_Q5.pdf")       # clean Vietnamese is fine
    assert not _looks_broken("Cat03.jpg")


def test_detects_drive_folder_links():
    assert is_drive_folder("https://drive.google.com/drive/folders/ABC123?usp=sharing")
    assert not is_drive_folder("https://drive.google.com/file/d/ABC123/view")
    assert not is_drive_folder("https://example.com/photo.jpg")
