"""Google Drive listing via the Drive API (needs an API key in GOOGLE_API_KEY).

For files/folders shared "anyone with the link", a plain API key (no OAuth) can resolve
an ID to its name + mimeType, list a folder's children, and yield per-file download URLs.
This lets a seller paste a Drive FOLDER link and we pull the photos/PDFs inside.

`resolve_drive(url, client)` returns a list of `(download_url, clean_name)` pairs — one
for a file, many for a folder (names come from the API as proper UTF-8, unlike the
mojibake Content-Disposition Drive sends on direct downloads). The engine then fetches
each download_url through the usual guarded `fetch_media`.
"""
from __future__ import annotations

import json
import re
import urllib.request
from urllib.parse import quote, urlparse

from .fetch import ALLOWED_TYPES, FetchError, _looks_broken

_API = "https://www.googleapis.com/drive/v3"
FOLDER_MIME = "application/vnd.google-apps.folder"
DEFAULT_MAX_FILES = 30  # cap how many files we pull from one folder


class DriveError(FetchError):
    """A Drive API call failed (bad key, folder not public, quota, ...)."""


def extract_drive_id(url: str) -> str | None:
    """Pull the file/folder ID from any Drive URL form, else None (not a Drive link)."""
    if (urlparse(url).hostname or "").lower() not in ("drive.google.com", "docs.google.com"):
        return None
    m = re.search(r"/(?:file|folders)/d?/?([\w-]{10,})", url) or re.search(r"[?&]id=([\w-]{10,})", url)
    return m.group(1) if m else None


def download_url(file_id: str) -> str:
    # NOT the API alt=media endpoint — that 403s for an anonymous API key. The public
    # usercontent endpoint serves file bytes for anything shared "anyone with the link".
    return f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t"


def _clean_name(name: str) -> str:
    """Drop a stored filename that's mojibake (some uploads corrupt non-Latin names)."""
    name = (name or "").strip()
    return "" if _looks_broken(name) else name


class DriveClient:
    def __init__(self, api_key: str, *, timeout: float = 20.0):
        self._key = api_key
        self._timeout = timeout

    def _get_json(self, url: str) -> dict:
        try:
            with urllib.request.urlopen(url, timeout=self._timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            raise DriveError(f"Gọi Drive API thất bại: {e}") from e

    def get_meta(self, file_id: str) -> dict:
        url = (f"{_API}/files/{file_id}"
               f"?fields=id,name,mimeType,size&supportsAllDrives=true&key={self._key}")
        return self._get_json(url)

    def list_children(self, folder_id: str) -> list[dict]:
        q = quote(f"'{folder_id}' in parents and trashed = false")
        files: list[dict] = []
        page = ""
        while True:
            url = (f"{_API}/files?q={q}&fields=nextPageToken,files(id,name,mimeType,size)"
                   f"&pageSize=100&supportsAllDrives=true&includeItemsFromAllDrives=true"
                   f"&key={self._key}")
            if page:
                url += f"&pageToken={page}"
            data = self._get_json(url)
            files.extend(data.get("files", []))
            page = data.get("nextPageToken", "")
            if not page:
                return files


def resolve_drive(
    url: str, client: DriveClient, *, max_files: int = DEFAULT_MAX_FILES
) -> list[tuple[str, str]]:
    """Map a Drive URL to a list of (download_url, name). Folder → its image/PDF children."""
    file_id = extract_drive_id(url)
    if file_id is None:
        raise DriveError("Không phải link Google Drive.")
    meta = client.get_meta(file_id)
    if meta.get("mimeType") == FOLDER_MIME:
        kids = client.list_children(file_id)
        media = [k for k in kids if k.get("mimeType") in ALLOWED_TYPES]
        return [(download_url(k["id"]), _clean_name(k.get("name", ""))) for k in media[:max_files]]
    return [(download_url(file_id), _clean_name(meta.get("name", "")))]


def folder_name(url: str, client: DriveClient) -> str | None:
    """The Drive FOLDER's own name (not its children) — used as a hint for grouping a
    multi-file batch ("Nhà phố Q5" tells the model these files are likely one căn)."""
    file_id = extract_drive_id(url)
    if file_id is None:
        return None
    try:
        meta = client.get_meta(file_id)
    except DriveError:
        return None
    return _clean_name(meta.get("name", "")) if meta.get("mimeType") == FOLDER_MIME else None


def make_resolver(api_key: str | None):
    """Return a url -> [(url, name|None)] resolver. With a Drive key it expands Drive
    links (incl. folders); without one, or for non-Drive links, it's a pass-through."""
    if not api_key:
        return lambda url: [(url, None)]
    client = DriveClient(api_key)

    def resolve(url: str) -> list[tuple[str, str | None]]:
        if extract_drive_id(url) is None:
            return [(url, None)]
        try:
            return list(resolve_drive(url, client))
        except DriveError:
            return [(url, None)]  # fall back to a direct fetch (which will error cleanly)

    return resolve


def make_folder_namer(api_key: str | None):
    """Return a url -> folder name|None lookup — a companion to `make_resolver` giving the
    ENCLOSING folder's own name (its children's names already come from make_resolver)."""
    if not api_key:
        return lambda url: None
    client = DriveClient(api_key)
    return lambda url: folder_name(url, client)
