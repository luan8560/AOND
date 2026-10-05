from aioi.drive import (
    FOLDER_MIME,
    DriveError,
    download_url,
    extract_drive_id,
    folder_name,
    make_folder_namer,
    make_resolver,
    resolve_drive,
)


def test_extract_drive_id_from_all_forms():
    assert extract_drive_id("https://drive.google.com/file/d/ABC1234567/view") == "ABC1234567"
    assert extract_drive_id("https://drive.google.com/open?id=ABC1234567&usp=drive_fs") == "ABC1234567"
    assert extract_drive_id("https://drive.google.com/drive/folders/FLD1234567?usp=sharing") == "FLD1234567"
    assert extract_drive_id("https://example.com/x.jpg") is None


def test_download_url_uses_public_usercontent_endpoint():
    assert download_url("FID1234567") == (
        "https://drive.usercontent.google.com/download?id=FID1234567&export=download&confirm=t"
    )


class _FakeClient:
    """Stands in for DriveClient with no network."""
    _key = "KEY"

    def __init__(self, meta, children=None):
        self._meta = meta
        self._children = children or []

    def get_meta(self, file_id):
        return self._meta

    def list_children(self, folder_id):
        return self._children


def test_resolve_single_file():
    client = _FakeClient({"id": "F1", "name": "so_hong.jpg", "mimeType": "image/jpeg"})
    out = resolve_drive("https://drive.google.com/open?id=F1xxxxxxxx", client)
    assert out == [(download_url("F1xxxxxxxx"), "so_hong.jpg")]


def test_resolve_folder_keeps_only_images_and_pdfs():
    client = _FakeClient(
        {"id": "FLD", "name": "Nhà phố Q5", "mimeType": FOLDER_MIME},
        children=[
            {"id": "a", "name": "mat_tien.jpg", "mimeType": "image/jpeg"},
            {"id": "b", "name": "so_hong.pdf", "mimeType": "application/pdf"},
            {"id": "c", "name": "notes.txt", "mimeType": "text/plain"},  # filtered out
        ],
    )
    out = resolve_drive("https://drive.google.com/drive/folders/FLD1234567", client)
    names = [n for _, n in out]
    assert names == ["mat_tien.jpg", "so_hong.pdf"]  # the .txt is dropped


def test_make_resolver_passthrough_without_key():
    resolve = make_resolver(None)
    assert resolve("https://example.com/x.jpg") == [("https://example.com/x.jpg", None)]


def test_folder_name_returns_the_folders_own_name():
    client = _FakeClient({"id": "FLD", "name": "Nhà phố Q5", "mimeType": FOLDER_MIME})
    assert folder_name("https://drive.google.com/drive/folders/FLD1234567", client) == "Nhà phố Q5"


def test_folder_name_is_none_for_a_single_file():
    client = _FakeClient({"id": "F1", "name": "so_hong.jpg", "mimeType": "image/jpeg"})
    assert folder_name("https://drive.google.com/open?id=F1xxxxxxxx", client) is None


def test_make_folder_namer_passthrough_without_key():
    namer = make_folder_namer(None)
    assert namer("https://drive.google.com/drive/folders/FLD1234567") is None
