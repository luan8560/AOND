from aioi.prompts import (
    build_chat_system,
    build_fee_followup_system,
    build_project_block,
    build_project_docs_block,
    extraction_schema,
)


def test_project_docs_block_injects_markdown_and_grounding_rule():
    docs = [{"title": "phân lô", "markdown": "# Phân lô\n| Lô | DT |\n|--|--|\n| 42 | 43.75m² |"}]
    block = build_project_docs_block("Nyah Phú Định", docs)
    assert "43.75m²" in block                    # the actual data is injected
    assert "không bịa" in block                  # grounding rule present
    assert build_project_docs_block("X", []) == ""  # no docs → nothing
from aioi.projects import Project, UnitType
from aioi.revenue import SellerRole
from aioi.schema import Property


def _prop() -> Property:
    p = Property(loai_hinh="nha_pho")
    p.fields.update({"dia_chi": "146 Trần Bình Trọng, Q5", "dien_tich_dat": "60m2",
                      "dien_tich_nha": "50m2", "gia": "10 tỷ"})
    return p


def test_extraction_schema_exposes_house_area_fields():
    # Regression: dien_tich_dat/dien_tich_nha must be real schema properties, or the
    # model has nowhere valid to put them (it'll improvise, e.g. JSON-stuffing "dien_tich").
    props = extraction_schema()["properties"]
    assert "dien_tich_dat" in props
    assert "dien_tich_nha" in props
    assert "dien_tich" in props  # still needed for non-house types (chung cư, đất...)


def test_extraction_schema_exposes_ma_so_can():
    assert "ma_so_can" in extraction_schema()["properties"]


def test_fee_followup_carries_persuasion_playbook_and_guardrails():
    s = build_fee_followup_system("T•ai", _prop(), SellerRole.CHU_NHA)
    # Has a persuasion branch + concrete value points to argue with.
    assert "THUYẾT PHỤC" in s
    assert "chỉ thu phí KHI giao dịch thành công".lower() in s.lower()
    # Guardrails: never discount, never be pushy.
    assert "KHÔNG tự ý giảm giá" in s
    assert "tối đa 1–2 lần" in s


def test_fee_followup_quotes_rate_by_seller_role():
    assert "1%" in build_fee_followup_system("T•ai", _prop(), SellerRole.CHU_NHA)
    assert "0.5%" in build_fee_followup_system("T•ai", _prop(), SellerRole.MOI_GIOI)
    assert "thoả thuận riêng" in build_fee_followup_system("T•ai", _prop(), SellerRole.CHU_DAU_TU)


def test_known_project_facts_injected_and_asks_light_confirm():
    proj = Project("Botanic Tower", amenities=["hồ bơi", "gym"],
                   attributes={"cổng": "Nguyễn Thượng Hiền"})
    block = build_project_block(proj, "Botanic Tower")
    s = build_chat_system("T•ai", _prop(), "gia", SellerRole.UNKNOWN, project_block=block)
    assert "Botanic Tower" in s and "hồ bơi" in s
    assert "XÁC NHẬN" in s  # known facts → confirm lightly, not silent assert


def test_unit_type_facts_injected_alongside_project():
    ut = UnitType("Type B", {"diện tích": "113m2", "số phòng ngủ": "3"})
    proj = Project("Botanic Towers", amenities=["hồ bơi"], unit_types=[ut])
    block = build_project_block(proj, "Botanic Towers", ut)
    assert "hồ bơi" in block                 # project facts
    assert 'MẪU NHÀ "Type B"' in block and "113m2" in block  # unit-type facts inherited


def test_unknown_project_prompts_to_acquire_knowledge():
    block = build_project_block(None, "Sunrise Riverside")
    assert "CHƯA có dữ liệu" in block
    assert "xin một LINK" in block or "LINK" in block  # ask seller for a link/info


def test_unknown_unit_type_prompts_to_acquire_and_not_invent():
    # Project known, but the model is named and unknown → ask for it, and don't fabricate.
    proj = Project("Nyah Phú Định", amenities=["hồ bơi"])
    block = build_project_block(proj, "Nyah Phú Định", None, "Cosmo Gen 2")
    assert "Cosmo Gen 2" in block
    assert "CHƯA có dữ liệu" in block
    assert "đừng bịa" in block.lower() or "TUYỆT ĐỐI" in block  # honesty guard
    assert "MẪU NHÀ" in block


def test_known_unit_type_does_not_trigger_acquisition():
    ut = UnitType("Type B", {"diện tích": "113m2"})
    proj = Project("Botanic Towers", amenities=["hồ bơi"], unit_types=[ut])
    block = build_project_block(proj, "Botanic Towers", ut, "Type B")
    assert "113m2" in block                       # injects known facts
    assert "BỔ SUNG KIẾN THỨC MẪU NHÀ" not in block  # not the acquisition prompt


def test_no_project_block_when_none():
    s = build_chat_system("T•ai", _prop(), "gia")
    assert "DỮ LIỆU DỰ ÁN" not in s
    assert build_project_block(None, None) == ""


def test_ambiguous_house_area_prompts_a_resolving_question():
    p = Property(loai_hinh="nha_pho")
    p.fields.update({"dia_chi": "146 Trần Bình Trọng, Q5", "dien_tich": "80m2", "gia": "10 tỷ"})
    s = build_chat_system("T•ai", p, "dien_tich_dat")
    assert "80m2" in s  # references the number already given, doesn't ask blind
    assert "CHƯA RÕ" in s


def test_normal_missing_field_question_unaffected_by_ambiguous_area_check():
    # _prop() has no ambiguous dien_tich sitting around -> the ordinary ask_line, unchanged.
    s = build_chat_system("T•ai", _prop(), "gia")
    assert "CHƯA RÕ" not in s
    assert '"giá"' in s  # asks about the actual next_field ("gia"), not diện tích at all
