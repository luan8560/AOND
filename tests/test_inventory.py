"""Step 1: the project-inventory query layer. Deterministic, no LLM, no loop.

The load-bearing test is `test_same_functions_serve_house_and_condo` — the 4 functions must
answer correctly for BOTH a phân-lô development (dien_tich_dat / vi_tri) and a condo
(dien_tich / view / tang), proving they're attribute-agnostic, not hard-coded to houses.
"""
from aioi.inventory import coverage, find, lookup, remember
from aioi.projects import Project, SqliteProjectStore, Unit


def _nyah() -> Project:
    # A phân-lô development: units carry dien_tich_dat / mat_tien / vi_tri.
    return Project(name="Nyah Phú Định", units=[
        Unit("42", {"mau_nha": "Cosmo Gen 2", "dien_tich_dat": "43.75m²", "gia": "8,981 tỷ"}, loai_hinh="nha_pho"),
        Unit("50", {"mau_nha": "Cosmo Gen 2", "dien_tich_dat": "57.31m²", "gia": "11,47 tỷ"}, loai_hinh="nha_pho"),
        Unit("26", {"mau_nha": "Office 2", "dien_tich_dat": "92m²", "gia": "16.566 tỷ"}, loai_hinh="nha_pho"),
        Unit("3", {"mau_nha": "Cosmo", "dien_tich_dat": "40m²", "gia": "9,71 tỷ"}, loai_hinh="nha_pho"),
    ])


def _valeo() -> Project:
    # A condo: units carry dien_tich / loai_can / huong / view / tang — DIFFERENT keys.
    return Project(name="Valéo Đầm Sen", units=[
        Unit("B-12-05", {"loai_can": "2PN", "dien_tich": "68m²", "view": "hồ", "tang": "12"}, loai_hinh="chung_cu"),
        Unit("B-08-03", {"loai_can": "2PN", "dien_tich": "65m²", "tang": "8"}, loai_hinh="chung_cu"),
        Unit("A-20-01", {"loai_can": "3PN", "dien_tich": "95m²", "view": "nội khu", "tang": "20"}, loai_hinh="chung_cu"),
    ])


def _codes(units) -> list[str]:
    return sorted(u.ma_so_can for u in units)


def test_lookup_returns_known_attributes_or_none():
    p = _nyah()
    assert lookup(p, "42") == {"ma_so_can": "42", "mau_nha": "Cosmo Gen 2",
                               "dien_tich_dat": "43.75m²", "gia": "8,981 tỷ"}
    assert lookup(p, "999") is None


def test_same_functions_serve_house_and_condo():
    # HOUSE (Nyah): "căn góc lớn nhất" → largest dien_tich_dat = lô 26; by model = Cosmo Gen 2.
    nyah = _nyah()
    assert _codes(find(nyah, largest="dien_tich_dat")) == ["26"]
    assert _codes(find(nyah, where={"mau_nha": "Cosmo Gen 2"})) == ["42", "50"]  # not "3" (Cosmo)
    assert _codes(find(nyah, where={"mau_nha": "Office"})) == ["26"]             # substring match

    # CONDO (Valéo): the SAME functions, different attribute keys — nothing house-specific.
    valeo = _valeo()
    assert _codes(find(valeo, largest="dien_tich")) == ["A-20-01"]              # largest condo area
    assert _codes(find(valeo, where={"loai_can": "2PN"})) == ["B-08-03", "B-12-05"]
    assert _codes(find(valeo, where={"view": "hồ"})) == ["B-12-05"]
    # combined filter + extreme: highest-floor 2PN
    assert _codes(find(valeo, where={"loai_can": "2PN"}, largest="tang")) == ["B-12-05"]


def test_find_extreme_excludes_units_with_unknown_attribute():
    # B-08-03 has no "view" — a query ranking by an attribute only considers units that have it.
    p = _valeo()
    assert _codes(find(p, largest="tang")) == ["A-20-01"]        # all have tang → the 20th floor
    # nobody has "gia" here → an extreme over an unknown attribute returns nothing (never guesses)
    assert find(p, largest="gia") == []


def test_remember_learns_a_new_attribute_then_it_is_queryable():
    p = _nyah()
    assert find(p, where={"vi_tri": "góc"}) == []                # unknown at first
    remember(p, "26", {"vi_tri": "góc"}, voter="seller1")        # a seller tells us lô 26 is a corner
    assert _codes(find(p, where={"vi_tri": "góc"})) == ["26"]    # now findable
    assert lookup(p, "26")["vi_tri"] == "góc"
    # learning a brand-new mã số căn creates it
    remember(p, "7", {"mau_nha": "Cosmo"}, voter="seller1")
    assert lookup(p, "7") == {"ma_so_can": "7", "mau_nha": "Cosmo"}


def test_coverage_reports_what_is_known_and_the_gap():
    p = _nyah()
    p.attributes["tong_so_can"] = "50"       # a seller told us the project has 50 lô
    cov = coverage(p)
    assert cov["so_can_da_biet"] == 4        # we only have 4 on file
    assert cov["tong_so_can_khai_bao"] == "50"   # ...of a declared 50 → AOND knows the gap
    assert cov["thuoc_tinh_da_biet"]["dien_tich_dat"] == 4  # area known for all 4
    assert "vi_tri" not in cov["thuoc_tinh_da_biet"]        # corner-lot: not known for any yet


def test_inventory_persists_across_restart(tmp_path):
    db = str(tmp_path / "inv.db")
    s1 = SqliteProjectStore(db)
    s1.save(_valeo())
    # a fresh store on the same DB sees the units + their free-form attrs
    s2 = SqliteProjectStore(db)
    p = s2.get("Valéo Đầm Sen")
    assert p is not None and len(p.units) == 3
    assert _codes(find(p, where={"loai_can": "2PN"})) == ["B-08-03", "B-12-05"]
    assert lookup(p, "A-20-01")["view"] == "nội khu"
