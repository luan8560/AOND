import io

from aioi.bulk import parse_csv, sheets_export_url, sniff_kind, xlsx_to_csv

CSV = (
    "Loại hình,Địa chỉ,Diện tích,Giá,Dự án,Phòng ngủ\n"
    "Nhà phố,146 Trần Bình Trọng Q5,50m2,10 tỷ,,3\n"
    "Căn hộ,Botanic Towers Q7,70m2,5 tỷ,Botanic Towers,2\n"
    "đất,Long An,1000m2,8 tỷ,,\n"
)


def test_parse_csv_maps_columns_and_types():
    rows = parse_csv(CSV)
    assert len(rows) == 3
    assert rows[0]["loai_hinh"] == "nha_pho"      # "Nhà phố" → slug
    assert rows[0]["dia_chi"] == "146 Trần Bình Trọng Q5"
    assert rows[0]["dien_tich"] == "50m2"
    assert rows[0]["gia"] == "10 tỷ"
    assert rows[0]["ket_cau"] == "3"              # "Phòng ngủ" → ket_cau
    assert rows[1]["loai_hinh"] == "chung_cu"     # "Căn hộ" → slug
    assert rows[1]["du_an"] == "Botanic Towers"
    assert rows[2]["loai_hinh"] == "dat_tho_cu"


def test_blank_cells_are_skipped():
    rows = parse_csv(CSV)
    assert "du_an" not in rows[0]   # empty Dự án cell omitted
    assert "ket_cau" not in rows[2]  # empty Phòng ngủ cell omitted


def test_project_rohang_columns_recognized():
    # A pre-sale project rổ hàng: mã số căn + mẫu nhà + giá are mapped deterministically.
    csv = (
        "Loại hình,Dự án,Mẫu nhà,Mã số căn,Diện tích,Giá\n"
        "Chung cư,Nyah Phú Định,Fusion Gen 5,42,70m2,3 tỷ\n"
        "Chung cư,Nyah Phú Định,Fusion Gen 5,41,70m2,2.9 tỷ\n"
    )
    rows = parse_csv(csv)
    assert rows[0]["ma_so_can"] == "42" and rows[1]["ma_so_can"] == "41"
    assert rows[0]["unit_type"] == "Fusion Gen 5"
    assert rows[0]["du_an"] == "Nyah Phú Định"
    assert rows[0]["gia"] == "3 tỷ"


def test_rohang_without_code_or_model_still_imports():
    # No mã số căn / mẫu nhà columns → those fields simply stay unset (standalone listings).
    rows = parse_csv("Loại hình,Địa chỉ,Giá\nNhà phố,123 Trần Bình Trọng,10 tỷ\n")
    assert rows[0]["dia_chi"] == "123 Trần Bình Trọng"
    assert "ma_so_can" not in rows[0]
    assert "unit_type" not in rows[0]


def test_ragged_row_does_not_crash():
    # A row with MORE cells than headers (an unquoted VN price "3,2 tỷ") must not crash the
    # import — the extra cells are absorbed, not fatal.
    rows = parse_csv("Loại hình,Địa chỉ,Giá\nNhà phố,123 Trần Bình Trọng,3,2 tỷ\n")
    assert len(rows) == 1
    assert rows[0]["dia_chi"] == "123 Trần Bình Trọng"


def test_unknown_columns_ignored():
    rows = parse_csv("Ghi chú,Giá\nnhà đẹp,10 tỷ\n")
    assert rows == [{"gia": "10 tỷ"}]  # "Ghi chú" not a recognized column


def test_sheets_export_url():
    u = sheets_export_url("https://docs.google.com/spreadsheets/d/ABC123/edit#gid=42")
    assert u == "https://docs.google.com/spreadsheets/d/ABC123/export?format=csv&gid=42"
    assert sheets_export_url("https://drive.google.com/file/d/X/view") is None


def test_sniff_kind_routes_by_content():
    import io
    import openpyxl
    assert sniff_kind(b"Loai,Gia\nNha pho,10\n", "text/csv", "x.csv") == "csv"
    assert sniff_kind(b"a,b\n1,2\n", "text/plain", "x") == "csv"        # comma heuristic
    assert sniff_kind(b"\xff\xd8\xff\xe0", "image/jpeg", "x") is None    # image → not a sheet
    assert sniff_kind(b"%PDF-1.4", "application/pdf", "x") is None       # pdf → not a sheet
    wb = openpyxl.Workbook(); wb.active.append(["a"]); buf = io.BytesIO(); wb.save(buf)
    assert sniff_kind(buf.getvalue(), "application/octet-stream", "x") == "xlsx"


def test_xlsx_to_csv_roundtrip():
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Loại hình", "Giá"])
    ws.append(["Nhà phố", "10 tỷ"])
    buf = io.BytesIO()
    wb.save(buf)
    text = xlsx_to_csv(buf.getvalue())
    rows = parse_csv(text)
    assert rows == [{"loai_hinh": "nha_pho", "gia": "10 tỷ"}]
