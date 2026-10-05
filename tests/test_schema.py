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


def test_house_not_summary_ready_until_both_areas_known():
    p = Property(loai_hinh="nha_pho")
    p.update({"dia_chi": "hẻm 123", "gia": "5 tỷ", "dien_tich_dat": "100m2"})
    assert not p.summary_ready()  # dien_tich_nha still missing
    p.update({"dien_tich_nha": "80m2", "ket_cau": "1 trệt 1 lầu",
              "lo_gioi_hem": "hẻm 6m", "tinh_trang_ban_giao": "trống",
              "phap_ly_hoan_cong": "sổ hồng"})
    assert p.summary_ready()
