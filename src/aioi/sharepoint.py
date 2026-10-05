"""SharePoint media store — the "kho gốc" (system of record) for property files
(photos, sổ hồng scans, brochures). See [[aioi-data-model]] for the two-tier design:
SharePoint holds BYTES; the DB (SqliteConversationStore) holds only a REFERENCE
(Graph item id + webUrl + path) on each `Listing`.

Auth: an Azure AD app, client-credentials (app-only) OAuth, `Sites.Selected` scope
granted to exactly one site (least-privilege — see the setup steps in chat history).
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable

_GRAPH = "https://graph.microsoft.com/v1.0"

# SharePoint forbids these characters in file/folder names (plus a few Windows-reserved
# ones); Drive-style mojibake is a separate, already-handled problem (see fetch.py).
_FORBIDDEN = re.compile(r'[~"#%&*:<>?/\\{|}]')


class SharePointError(Exception):
    """A Graph API call for the SharePoint store failed (auth, upload, or network)."""


def sanitize_filename(name: str) -> str:
    """Strip characters SharePoint forbids in a file/folder path segment."""
    cleaned = _FORBIDDEN.sub("_", (name or "").strip())
    return cleaned or "tep"


def sanitize_path(path: str) -> str:
    """Sanitize every segment of a `/`-joined folder path, dropping empty segments."""
    return "/".join(sanitize_filename(p) for p in path.split("/") if p.strip())


_EXTENSION_BY_MEDIA_TYPE = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
}


def extension_for(media_type: str) -> str:
    """A file extension for a MIME type — needed when the seller's file has no real
    filename (a generic display placeholder like "ảnh" has no extension, and without one
    Windows/SharePoint can't tell what app should open it, even though the bytes are a
    perfectly valid JPEG)."""
    return _EXTENSION_BY_MEDIA_TYPE.get(media_type, "")


def listing_folder(
    du_an: str | None, dia_chi: str | None, listing_id: str, ma_so_can: str | None = None
) -> str:
    """Folder for one listing's files: `<Dự án hoặc 'Nhà lẻ'>/<nhãn>_<id ngắn>`, where the
    label is the street address if known, else the mã số căn (a pre-sale project unit has a
    code before it has an address), else just the id.

    The stable `listing_id` (never reclassified) anchors the folder even if the
    address/project name is later corrected — a rename just moves the folder, the DB
    reference (item id) doesn't change. A full Quận/Đường/Số nhà taxonomy needs `dia_chi`
    decomposed into parts, which the schema doesn't capture yet (single combined
    string) — this is the pragmatic version until that lands."""
    # Keep each value as ONE path segment: a Vietnamese hẻm address like "198A/22" contains
    # a slash that would otherwise be split into an unintended nested folder downstream.
    def seg(s: str | None) -> str:
        return (s or "").replace("/", "-").replace("\\", "-").strip()

    top = seg(du_an) or "Nhà lẻ"
    short_id = listing_id[:8]
    label_part = seg(dia_chi) or seg(ma_so_can)
    label = f"{label_part}_{short_id}" if label_part else short_id
    return f"{top}/{label}"


@dataclass(frozen=True)
class MediaRef:
    """A reference to one file stored in SharePoint. The DB keeps ONLY this — never
    the bytes — so records stay small and SharePoint remains the single system of
    record for the actual files (permissions, versioning, human browsing)."""

    item_id: str
    web_url: str
    path: str
    filename: str

    def to_dict(self) -> dict[str, str]:
        return {"item_id": self.item_id, "web_url": self.web_url,
                "path": self.path, "filename": self.filename}

    @staticmethod
    def from_dict(d: dict[str, str]) -> "MediaRef":
        return MediaRef(d["item_id"], d["web_url"], d["path"], d["filename"])


class MediaStore(ABC):
    @abstractmethod
    def upload(self, folder: str, filename: str, data: bytes, content_type: str) -> MediaRef: ...

    def move_folder(self, old_path: str, new_path: str) -> None:
        """Rename/move a listing's folder when its address (or project) is captured/changed
        AFTER files were already stored — the stable listing_id anchors the folder, this
        just updates the human-readable label. Default: no-op (a store without a folder
        concept keeps files where they are)."""
        return None

    def delete(self, item_id: str) -> None:
        """Remove a stored file by its id (best-effort) — used when a listing is removed from
        a seller's basket. Default: no-op."""
        return None


def _fetch_token(tenant_id: str, client_id: str, client_secret: str, timeout: float) -> dict[str, Any]:
    """Real network call — client-credentials (app-only) token request. Factored out
    so `GraphAuth` can be tested with a fake token fetcher (no network in tests)."""
    url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    body = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": "https://graph.microsoft.com/.default",
        "grant_type": "client_credentials",
    }).encode("ascii")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise SharePointError(
            f"Đăng nhập Azure AD thất bại ({e.code}): {e.read().decode('utf-8', 'replace')}"
        ) from e
    except Exception as e:
        raise SharePointError(f"Đăng nhập Azure AD thất bại: {e}") from e


class GraphAuth:
    """Client-credentials OAuth against Azure AD — caches the access token until near
    expiry so we're not round-tripping to login.microsoftonline.com on every upload."""

    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        *,
        timeout: float = 20.0,
        token_fetcher: Callable[[str, str, str, float], dict[str, Any]] = _fetch_token,
    ):
        self._tenant_id = tenant_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._timeout = timeout
        self._token_fetcher = token_fetcher  # injectable so tests don't hit the network
        self._token: str | None = None
        self._expires_at: float = 0.0

    def token(self) -> str:
        if self._token is None or time.time() >= self._expires_at - 60:  # 60s safety margin
            try:
                data = self._token_fetcher(self._tenant_id, self._client_id, self._client_secret, self._timeout)
            except SharePointError:
                raise
            except Exception as e:  # a custom/injected fetcher may not wrap its own errors
                raise SharePointError(f"Đăng nhập Azure AD thất bại: {e}") from e
            self._token = data["access_token"]
            self._expires_at = time.time() + int(data.get("expires_in", 3600))
        return self._token


def _graph_json(
    method: str, url: str, token: str, body: dict[str, Any] | None, timeout: float, allow_404: bool
) -> dict[str, Any] | None:
    """Real network call for a JSON Graph request (GET/POST/PATCH) — factored out so the
    folder-move operations can be tested with an injected transport (no network in tests).
    Returns None on 404 when `allow_404` (used for a probe-if-exists lookup)."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        if e.code == 404 and allow_404:
            return None
        raise SharePointError(
            f"Graph {method} thất bại ({e.code}): {e.read().decode('utf-8', 'replace')}"
        ) from e
    except Exception as e:
        raise SharePointError(f"Graph {method} thất bại: {e}") from e


def _graph_put(url: str, token: str, data: bytes, content_type: str, timeout: float) -> dict[str, Any]:
    """Real network call for a small-file upload — factored out so `SharePointMediaStore`
    can be tested with an injected requester (no network in tests)."""
    req = urllib.request.Request(url, data=data, method="PUT")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", content_type or "application/octet-stream")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise SharePointError(
            f"Tải lên SharePoint thất bại ({e.code}): {e.read().decode('utf-8', 'replace')}"
        ) from e
    except Exception as e:
        raise SharePointError(f"Tải lên SharePoint thất bại: {e}") from e


class SharePointMediaStore(MediaStore):
    """Upload property files to one SharePoint site's default document library
    (`/sites/{site_id}/drive`), via a simple PUT (Graph's small-file upload — under
    4 MB). Good enough for downscaled photos/PDFs under the batch budget; a file over
    the limit needs a chunked upload session (not needed yet — raises instead)."""

    SMALL_FILE_LIMIT = 4 * 1024 * 1024

    def __init__(
        self,
        auth: GraphAuth,
        site_id: str,
        *,
        timeout: float = 30.0,
        requester: Callable[[str, str, bytes, str, float], dict[str, Any]] = _graph_put,
        json_requester: Callable[..., dict[str, Any] | None] = _graph_json,
    ):
        self._auth = auth
        self._site_id = site_id
        self._timeout = timeout
        self._requester = requester            # PUT file bytes (upload)
        self._json_requester = json_requester  # GET/POST/PATCH JSON (folder move); both injectable

    def _drive(self) -> str:
        return f"{_GRAPH}/sites/{self._site_id}/drive"

    def _json(self, method: str, url: str, body: dict[str, Any] | None = None,
              *, allow_404: bool = False) -> dict[str, Any] | None:
        return self._json_requester(method, url, self._auth.token(), body, self._timeout, allow_404)

    def _item_by_path(self, path: str) -> dict[str, Any] | None:
        """The drive item at `path` (folder or file), or None if it doesn't exist."""
        loc = f"{self._drive()}/root:/{urllib.parse.quote(path)}" if path else f"{self._drive()}/root"
        return self._json("GET", loc, allow_404=True)

    def ensure_folder(self, path: str) -> str:
        """Create `path` (nested folders) if missing; return the leaf folder's item id.
        Idempotent — existing segments are reused, not duplicated."""
        parent = ""
        item = self._item_by_path("")  # drive root
        folder_id = item["id"]
        for seg in [s for s in sanitize_path(path).split("/") if s]:
            current = f"{parent}/{seg}".lstrip("/")
            existing = self._item_by_path(current)
            if existing is None:
                loc = (f"{self._drive()}/root:/{urllib.parse.quote(parent)}:/children"
                       if parent else f"{self._drive()}/root/children")
                existing = self._json("POST", loc,
                                      {"name": seg, "folder": {},
                                       "@microsoft.graph.conflictBehavior": "fail"})
            folder_id = existing["id"]
            parent = current
        return folder_id

    def move_folder(self, old_path: str, new_path: str) -> None:
        old_path, new_path = sanitize_path(old_path), sanitize_path(new_path)
        if old_path == new_path:
            return
        old = self._item_by_path(old_path)
        if old is None:  # nothing stored there (never uploaded, or already moved) — nothing to do
            return
        new_parent, _, new_leaf = new_path.rpartition("/")
        parent_id = self.ensure_folder(new_parent) if new_parent else self._item_by_path("")["id"]
        self._json("PATCH", f"{self._drive()}/items/{old['id']}",
                   {"name": new_leaf, "parentReference": {"id": parent_id}})

    def delete(self, item_id: str) -> None:
        self._json("DELETE", f"{self._drive()}/items/{item_id}")

    def upload(self, folder: str, filename: str, data: bytes, content_type: str) -> MediaRef:
        if len(data) > self.SMALL_FILE_LIMIT:
            raise SharePointError(
                f"Tệp {len(data)} bytes vượt giới hạn upload đơn giản "
                f"({self.SMALL_FILE_LIMIT} bytes) — cần upload session (chưa hỗ trợ)."
            )
        safe_name = sanitize_filename(filename)
        safe_folder = sanitize_path(folder)
        item_path = f"{safe_folder}/{safe_name}" if safe_folder else safe_name
        url = f"{_GRAPH}/sites/{self._site_id}/drive/root:/{urllib.parse.quote(item_path)}:/content"
        try:
            item = self._requester(url, self._auth.token(), data, content_type, self._timeout)
        except SharePointError:
            raise
        except Exception as e:  # a custom/injected requester may not wrap its own errors
            raise SharePointError(f"Tải lên SharePoint thất bại: {e}") from e
        return MediaRef(item_id=item["id"], web_url=item.get("webUrl", ""),
                         path=item_path, filename=safe_name)
