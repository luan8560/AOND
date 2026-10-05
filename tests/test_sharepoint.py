from aioi.sharepoint import (
    GraphAuth,
    MediaRef,
    SharePointError,
    SharePointMediaStore,
    extension_for,
    listing_folder,
    sanitize_filename,
    sanitize_path,
)


def test_extension_for_known_media_types():
    assert extension_for("image/jpeg") == ".jpg"
    assert extension_for("image/png") == ".png"
    assert extension_for("application/pdf") == ".pdf"


def test_extension_for_unknown_media_type_is_empty():
    assert extension_for("application/octet-stream") == ""


def test_sanitize_filename_strips_forbidden_chars():
    assert sanitize_filename('so_hong "2024".jpg') == "so_hong _2024_.jpg"
    assert sanitize_filename("a#b%c*d:e<f>g?h/i\\j{k|l}m") == "a_b_c_d_e_f_g_h_i_j_k_l_m"


def test_sanitize_filename_empty_falls_back():
    assert sanitize_filename("") == "tep"
    assert sanitize_filename("   ") == "tep"


def test_sanitize_path_drops_empty_segments():
    assert sanitize_path("Botanic Towers//căn 9/") == "Botanic Towers/căn 9"


def test_listing_folder_uses_project_or_nha_le():
    f = listing_folder("Botanic Towers", "123 Nguyễn Văn A", "abcd1234efgh5678")
    assert f == "Botanic Towers/123 Nguyễn Văn A_abcd1234"
    f2 = listing_folder(None, None, "abcd1234efgh5678")
    assert f2 == "Nhà lẻ/abcd1234"


def test_listing_folder_uses_ma_so_can_when_no_address():
    # Pre-sale project unit: no address yet → folder labelled by the mã số căn.
    assert listing_folder("Nyah Phú Định", None, "abcd1234ef", "căn 42") == "Nyah Phú Định/căn 42_abcd1234"
    # Address wins over the code once both are present (the unit has graduated).
    assert listing_folder("Nyah Phú Định", "198A", "abcd1234ef", "căn 42") == "Nyah Phú Định/198A_abcd1234"


def test_listing_folder_keeps_hem_address_as_one_segment():
    # "198A/22" (hẻm notation) must NOT split into a nested folder — the label is one segment.
    f = listing_folder("Nyah Phú Định", "198A/22 Trương Đình Hội", "abcd1234ef")
    assert f == "Nyah Phú Định/198A-22 Trương Đình Hội_abcd1234"
    assert f.count("/") == 1  # exactly the top/label separator, nothing from the address


def test_graph_auth_caches_token_until_expiry():
    calls = []
    def fake_fetch(tenant, client, secret, timeout):
        calls.append(1)
        return {"access_token": f"tok{len(calls)}", "expires_in": 3600}
    auth = GraphAuth("t", "c", "s", token_fetcher=fake_fetch)
    assert auth.token() == "tok1"
    assert auth.token() == "tok1"  # cached, no second fetch
    assert len(calls) == 1


def test_graph_auth_refreshes_when_near_expiry():
    calls = []
    def fake_fetch(tenant, client, secret, timeout):
        calls.append(1)
        return {"access_token": f"tok{len(calls)}", "expires_in": 30}  # under the 60s margin
    auth = GraphAuth("t", "c", "s", token_fetcher=fake_fetch)
    assert auth.token() == "tok1"
    assert auth.token() == "tok2"  # immediately stale (30s < 60s safety margin) -> refetched
    assert len(calls) == 2


def test_graph_auth_wraps_failures():
    def boom(tenant, client, secret, timeout):
        raise RuntimeError("network down")
    auth = GraphAuth("t", "c", "s", token_fetcher=boom)
    try:
        auth.token()
        assert False, "expected SharePointError"
    except SharePointError:
        pass


class _FakeAuth:
    def token(self):
        return "TOKEN"


def test_media_store_upload_builds_correct_path_and_returns_ref():
    seen = {}
    def fake_put(url, token, data, content_type, timeout):
        seen["url"] = url
        seen["token"] = token
        seen["data"] = data
        seen["content_type"] = content_type
        return {"id": "ITEM1", "webUrl": "https://nhadatcompany.sharepoint.com/.../mat_tien.jpg"}

    store = SharePointMediaStore(_FakeAuth(), "SITE123", requester=fake_put)
    ref = store.upload("Botanic Towers/căn 9", "mặt tiền.jpg", b"\xff\xd8\xff", "image/jpeg")

    assert isinstance(ref, MediaRef)
    assert ref.item_id == "ITEM1"
    assert ref.path == "Botanic Towers/căn 9/mặt tiền.jpg"
    assert seen["token"] == "TOKEN"
    assert seen["data"] == b"\xff\xd8\xff"
    assert seen["content_type"] == "image/jpeg"
    assert "SITE123" in seen["url"] and "content" in seen["url"]


def test_media_store_sanitizes_folder_and_filename():
    def fake_put(url, token, data, content_type, timeout):
        return {"id": "X", "webUrl": ""}
    store = SharePointMediaStore(_FakeAuth(), "SITE", requester=fake_put)
    ref = store.upload("Dự án: A/B", 'so#hong.jpg', b"x", "image/jpeg")
    assert "#" not in ref.path and ":" not in ref.path


def test_media_store_rejects_file_over_small_upload_limit():
    def fake_put(url, token, data, content_type, timeout):
        raise AssertionError("should not be called")
    store = SharePointMediaStore(_FakeAuth(), "SITE", requester=fake_put)
    big = b"\x00" * (5 * 1024 * 1024)
    try:
        store.upload("f", "big.jpg", big, "image/jpeg")
        assert False, "expected SharePointError"
    except SharePointError:
        pass


def test_media_ref_round_trips_through_dict():
    ref = MediaRef("id1", "https://x", "a/b.jpg", "b.jpg")
    assert MediaRef.from_dict(ref.to_dict()) == ref


class _FakeGraph:
    """A tiny in-memory Graph drive for testing folder move without network. Tracks a set
    of existing folder paths and records PATCH (move) calls."""

    def __init__(self, existing_paths):
        self.paths = set(existing_paths)  # folder paths that "exist"
        self.created = []
        self.patched = []

    def __call__(self, method, url, token, body, timeout, allow_404):
        # crude URL parse: everything after "root:/" up to ":" is the path; "/root" alone = ""
        if method == "GET":
            path = self._path_from(url)
            if path == "" or path in self.paths:
                return {"id": f"id:{path}"}
            if allow_404:
                return None
            raise SharePointError("404")
        if method == "POST":  # create child folder
            parent = self._path_from(url).rsplit("/children", 1)[0].rstrip(":")
            name = body["name"]
            new = f"{parent}/{name}".lstrip("/")
            self.paths.add(new)
            self.created.append(new)
            return {"id": f"id:{new}"}
        if method == "PATCH":  # move
            self.patched.append(body)
            return {"id": "moved"}
        raise AssertionError(method)

    @staticmethod
    def _path_from(url):
        import urllib.parse
        if "root:/" in url:
            seg = url.split("root:/", 1)[1]
            seg = seg.split(":/children", 1)[0].split(":", 1)[0]
            return urllib.parse.unquote(seg)
        return ""  # ".../root" or ".../root/children"


def test_move_folder_patches_to_new_leaf_when_parent_unchanged():
    graph = _FakeGraph({"Nhà lẻ", "Nhà lẻ/9ba11cb1"})
    store = SharePointMediaStore(_FakeAuth(), "SITE", json_requester=graph)
    store.move_folder("Nhà lẻ/9ba11cb1", "Nhà lẻ/123 Trần Bình Trọng_9ba11cb1")
    assert len(graph.patched) == 1
    assert graph.patched[0]["name"] == "123 Trần Bình Trọng_9ba11cb1"
    assert "parentReference" in graph.patched[0]


def test_move_folder_noop_when_paths_equal():
    graph = _FakeGraph({"a/b"})
    store = SharePointMediaStore(_FakeAuth(), "SITE", json_requester=graph)
    store.move_folder("a/b", "a/b")
    assert graph.patched == []


def test_move_folder_noop_when_source_missing():
    graph = _FakeGraph(set())  # old folder doesn't exist
    store = SharePointMediaStore(_FakeAuth(), "SITE", json_requester=graph)
    store.move_folder("Nhà lẻ/gone", "Botanic/here")
    assert graph.patched == []


def test_ensure_folder_creates_missing_segments():
    graph = _FakeGraph(set())  # nothing exists yet
    store = SharePointMediaStore(_FakeAuth(), "SITE", json_requester=graph)
    store.ensure_folder("Botanic Towers/căn 9")
    assert "Botanic Towers" in graph.created
    assert "Botanic Towers/căn 9" in graph.created
