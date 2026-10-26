from aioi.schema import Property, PropertyGroup


def test_group_detection_from_type():
    assert Property(loai_hinh="nha_pho").group is PropertyGroup.RESIDENTIAL
    assert Property(loai_hinh="dat_tho_cu").group is PropertyGroup.LAND
    assert Property(loai_hinh="nha_xuong").group is PropertyGroup.INDUSTRIAL
    assert Property().group is PropertyGroup.UNKNOWN


def test_expected_fields_change_by_group():
    res = Property(loai_hinh="nha_pho")
    ind = Property(loai_hinh="nha_xuong")
    assert "phap_ly_hoan_cong" in res.expected_fields()
    assert "phap_ly_hoan_cong" not in ind.expected_fields()
    assert "tai_trong_san" in ind.expected_fields()


def test_rental_adds_rental_fields():
    p = Property(loai_hinh="nha_pho", is_rental=True)
    assert "muc_coc" in p.expected_fields()
    assert "thoi_gian_fitout" in p.expected_fields()


def test_completeness_and_update():
    p = Property()
    assert p.completeness() == 0.0
    newly = p.update({"loai_hinh": "nha_pho", "dia_chi": "hẻm 123", "dien_tich": "50m2"})
    assert set(newly) == {"loai_hinh", "dia_chi", "dien_tich"}
    assert 0.0 < p.completeness() < 1.0
    # Re-applying the same data captures nothing new.
    assert p.update({"dia_chi": "hẻm 123"}) == []


def test_adding_house_number_keeps_the_rest_of_the_address():
    # Regression: adding a số nhà must NOT erase the street/ward already captured.
    p = Property(fields={"dia_chi": "Trần Hưng Đạo, Cầu Ông Lãnh, Quận 1, TP.HCM"})
    p.update({"dia_chi": "391/34"})
    assert p.fields["dia_chi"] == "391/34 Trần Hưng Đạo, Cầu Ông Lãnh, Quận 1, TP.HCM"
    # A fragment already contained is a no-op; a fuller address replaces; a different full address corrects.
    assert p.update({"dia_chi": "Trần Hưng Đạo"}) == []        # contained → unchanged
    p.update({"dia_chi": "456 Lê Lợi, Quận 1"})
    assert p.fields["dia_chi"] == "456 Lê Lợi, Quận 1"         # genuine correction replaces


def test_missing_fields_excludes_captured():
    p = Property(loai_hinh="nha_pho")
    p.update({"dia_chi": "hẻm 123"})
    missing = p.missing_fields()
    assert "dia_chi" not in missing
    assert "gia" in missing
    assert "loai_hinh" not in missing


def test_house_types_split_area_into_dat_and_nha():
    for slug in ("nha_pho", "biet_thu", "nha_cap_4"):
        p = Property(loai_hinh=slug)
        assert "dien_tich_dat" in p.identity_fields()
        assert "dien_tich_nha" in p.identity_fields()
        assert "dien_tich" not in p.identity_fields()


def test_non_house_types_keep_single_area():
    for slug in ("chung_cu", "dat_tho_cu", "nha_xuong", None):
        p = Property(loai_hinh=slug)
        assert "dien_tich" in p.identity_fields()
        assert "dien_tich_dat" not in p.identity_fields()
        assert "dien_tich_nha" not in p.identity_fields()


def test_presale_project_unit_located_by_code_not_address():
    # A project unit still in pre-sale (du_an known, no address) is located by mã số căn.
    p = Property(loai_hinh="chung_cu")
    p.fields["du_an"] = "Nyah Phú Định"
    idf = p.identity_fields()
    assert "du_an" in idf and "ma_so_can" in idf
    assert "dia_chi" not in idf


def test_unit_graduates_to_address_once_it_has_one():
    # Once the finished unit gets a street address, it's located by that — the code is no
    # longer a REQUIRED identity field (still stored, just not demanded).
    p = Property(loai_hinh="chung_cu")
    p.fields.update({"du_an": "Nyah Phú Định", "ma_so_can": "42",
                     "dia_chi": "198A/22 Trương Đình Hội"})
    idf = p.identity_fields()
    assert "dia_chi" in idf
    assert "ma_so_can" not in idf


def test_standalone_property_still_requires_address():
    p = Property(loai_hinh="nha_pho")  # no du_an → not a project unit
    idf = p.identity_fields()
    assert "dia_chi" in idf
    assert "ma_so_can" not in idf


def test_house_can_publish_with_minimum_but_waits_to_draft_until_facets_fill():
    # Model redesign: a house can rao (go live) with loại hình + vị trí + giá, but AOND should NOT
    # draft the ad on that alone — summary_ready is a HIGHER bar (≥3 of 4 key facets filled) so the
    # ad doesn't read thin ("hơi vội rao"). The đất/nhà area split is enrichment, not a publish-blocker.
    p = Property(loai_hinh="nha_pho")
    assert not p.can_publish()                                 # no địa chỉ / giá yet
    p.update({"dia_chi": "hẻm 123", "gia": "5 tỷ"})
    assert p.can_publish()                                     # minimum met → eligible to go live
    assert not p.summary_ready()                               # but too thin to DRAFT the ad yet
    # The remaining questions are per empty FACET (not atomic slots), in facet order.
    missing = p.missing_fields()
    assert set(missing) <= {"vi_the", "dat", "cong_trinh", "phap_ly", "gia_tm"}
    assert "dat" in missing and "cong_trinh" in missing         # no size / building info yet
    # Fill 3 key facets: vị thế + đất + công trình. Pháp lý stays empty (seller hasn't said it) —
    # that's ≤1 empty facet, enough to draft WITHOUT inventing a legal status.
    p.update({"lo_gioi_hem": "6m", "dien_tich_dat": "100m2", "ket_cau": "1 trệt 1 lầu"})
    assert "dat" not in p.missing_fields() and "cong_trinh" not in p.missing_fields()
    assert p.summary_ready()                                   # ≤1 empty facet → draft the ad


def test_free_form_highlights_accumulate_and_are_never_dropped():
    # "Bóc tách không thiếu": an advantage the seller mentions that fits NO structured slot is
    # still captured (free-form uu_the), accumulates across turns, and dedupes — never dropped.
    p = Property(loai_hinh="nha_pho")
    p.update({"uu_the": "gần trường học · khu an ninh"})
    assert p.fields["uu_the"] == "gần trường học · khu an ninh"
    newly = p.update({"uu_the": "khu an ninh · nhà mới sơn"})   # one repeat + one new
    assert p.fields["uu_the"] == "gần trường học · khu an ninh · nhà mới sơn"
    assert "uu_the" in newly
    assert p.update({"uu_the": "nhà mới sơn"}) == []            # nothing new → no change
    # Highlights live under the free-form "Khác" facet, not as a structured atom, and don't gate
    # drafting (họ là ưu thế phụ, không phải 1 trong 4 facet chính).
    facets = {fid: kv for fid, _lbl, kv in p.by_facet()}
    assert facets["khac"]["uu_the"] == p.fields["uu_the"]
    assert "khac" not in p.empty_facets()
