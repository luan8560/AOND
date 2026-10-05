from aioi.projects import (
    InMemoryProjectStore,
    Project,
    SqliteProjectStore,
    UnitType,
    normalize_project_name,
)


def test_unit_type_upsert_and_find():
    p = Project("Nyah Phú Định")
    p.upsert_unit_type(UnitType("Fusion Gen 5", {"diện tích": "250m2", "số tầng": "6"}))
    p.upsert_unit_type(UnitType("Fusion Gen 5", {"số tầng": "6", "số phòng ngủ": "4"}))  # update
    assert len(p.unit_types) == 1  # same model name → updated, not duplicated
    ut = p.find_unit_type("fusion gen 5")  # case-insensitive
    assert ut is not None and ut.specs.get("số phòng ngủ") == "4"


def test_sqlite_persists_unit_types(tmp_path):
    db = str(tmp_path / "u.db")
    pr = Project("Nyah Phú Định")
    pr.upsert_unit_type(UnitType("Fusion Gen 5", {"số tầng": "6"}))
    SqliteProjectStore(db).save(pr)
    p2 = SqliteProjectStore(db).get("Nyah Phú Định")  # fresh store = restart
    assert p2.find_unit_type("Fusion Gen 5").specs["số tầng"] == "6"


def test_sqlite_persists_project_docs(tmp_path):
    db = str(tmp_path / "docs.db")
    pr = Project("Nyah Phú Định")
    pr.docs.append({"kind": "phân lô", "title": "Phân lô", "markdown": "# Phân lô\n- Lô 42: 100m2",
                    "web_url": "https://sp/x", "item_id": "i1", "fetched_at": "1"})
    SqliteProjectStore(db).save(pr)
    reloaded = SqliteProjectStore(db).get("Nyah Phú Định")
    assert reloaded.doc(kind="phân lô")["markdown"].startswith("# Phân lô")


def test_project_doc_returns_latest_matching():
    p = Project("X")
    p.docs.append({"kind": "brochure", "title": "A", "markdown": "old"})
    p.docs.append({"kind": "brochure", "title": "A", "markdown": "new"})
    assert p.doc(kind="brochure")["markdown"] == "new"   # most recent
    assert p.doc(kind="phân lô") is None


def test_sqlite_persists_unit_type_media(tmp_path):
    db = str(tmp_path / "um.db")
    pr = Project("Nyah Phú Định")
    ut = UnitType("Fusion Gen 5", {"số tầng": "6"})
    ut.media.append({"item_id": "i1", "web_url": "https://sp/x", "path": "a/b.jpg", "filename": "b.jpg"})
    pr.upsert_unit_type(ut)
    SqliteProjectStore(db).save(pr)
    reloaded = SqliteProjectStore(db).get("Nyah Phú Định").find_unit_type("Fusion Gen 5")
    assert reloaded.media == [{"item_id": "i1", "web_url": "https://sp/x",
                              "path": "a/b.jpg", "filename": "b.jpg"}]


def test_project_facts_lists_amenities_and_attributes():
    p = Project("Botanic Tower", amenities=["hồ bơi", "gym"], attributes={"cổng": "Nguyễn Thượng Hiền"})
    facts = p.facts()
    assert "Tiện ích: hồ bơi, gym" in facts
    assert "cổng: Nguyễn Thượng Hiền" in facts


def test_store_get_is_case_and_space_insensitive():
    store = InMemoryProjectStore()
    store.save(Project("Botanic Tower", amenities=["hồ bơi"]))
    assert store.get("botanic   tower") is not None      # case + extra spaces
    assert store.get("BOTANIC TOWER").amenities == ["hồ bơi"]
    assert store.get("Sunrise City") is None
    assert store.get(None) is None


def test_store_lenient_match_handles_singular_plural():
    store = InMemoryProjectStore()
    store.save(Project("Botanic Towers", amenities=["hồ bơi"]))
    assert store.get("Botanic Tower") is not None  # model dropped the 's'
    store.save(Project("Masteri Thảo Điền"))
    assert store.get("Masteri") is None  # too short / partial — must not over-match


def test_apostrophe_and_generic_prefix_variants_map_to_one_project():
    # The real mess: one place got registered under 4 spellings. A seller who later names any
    # variant must land on the SAME project, not spawn a duplicate.
    store = InMemoryProjectStore()
    store.save(Project("Ny'ah Phú Định", amenities=["cây xanh"]))
    for variant in ["Nyah Phú Định", "Ny'ah Phú Định", "Ny ah Phú Định",
                    "Khu Nhà Biệt Lập Ny'ah Phú Định", "Dự án Ny'ah Phú Định"]:
        assert store.get(variant) is not None, variant
    # ...but a genuinely different project must stay separate.
    store.save(Project("Masteri Thảo Điền"))
    assert store.get("Vinhomes Thảo Điền") is None
    assert store.get("Masteri An Phú") is None


def test_normalize_collapses_case_and_space():
    assert normalize_project_name("  Botanic   Tower ") == normalize_project_name("botanic tower")


def test_normalize_ignores_punctuation():
    # The vision model may read "Fusion (Gen 5)"; a listing says "Fusion Gen 5".
    assert normalize_project_name("Fusion (Gen 5)") == normalize_project_name("Fusion Gen 5")
    p = Project("Nyah Phú Định")
    p.upsert_unit_type(UnitType("Fusion (Gen 5)", {"số tầng": "6"}))
    assert p.find_unit_type("Fusion Gen 5") is not None  # matches across the parens


def test_sqlite_store_persists_across_reopen(tmp_path):
    db = str(tmp_path / "t.db")
    SqliteProjectStore(db).save(
        Project("Botanic Towers", amenities=["hồ bơi", "gym"], attributes={"cổng": "NTH"})
    )
    # A fresh store on the same file (simulating a restart) still has the project.
    reopened = SqliteProjectStore(db)
    p = reopened.get("Botanic Tower")  # lenient match survives too
    assert p is not None
    assert p.amenities == ["hồ bơi", "gym"]
    assert p.attributes == {"cổng": "NTH"}


def test_sqlite_store_upserts_not_duplicates(tmp_path):
    store = SqliteProjectStore(str(tmp_path / "t.db"))
    store.save(Project("Sunrise City", amenities=["hồ bơi"]))
    store.save(Project("Sunrise City", amenities=["hồ bơi", "gym"]))  # update
    assert len(store.all()) == 1
    assert store.get("Sunrise City").amenities == ["hồ bơi", "gym"]
