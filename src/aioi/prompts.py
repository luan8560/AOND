"""System prompts and the extraction JSON schema.

Encodes the SRD rules: 30-word cap, praise-first, max 1 question/turn, honesty about
images, friendly local tone, and the assistant's assigned persona.
"""
from __future__ import annotations

from typing import Any

from .projects import Project, UnitType
from .revenue import SellerRole
from .schema import (
    CORE_FIELDS,
    HOUSE_AREA_FIELDS,
    IDENTITY_FIELDS,
    RENTAL_FIELDS,
    Property,
)

# Human-readable Vietnamese labels for each field slug — used when asking questions.
FIELD_LABELS: dict[str, str] = {
    "loai_hinh": "loại hình BĐS",
    "dia_chi": "địa chỉ",
    "ma_so_can": "mã số căn (vd căn số 42, block/tầng)",
    "dien_tich": "diện tích",
    "dien_tich_dat": "diện tích đất",
    "dien_tich_nha": "diện tích nhà",
    "gia": "giá",
    "ket_cau": "kết cấu (số lầu, phòng ngủ)",
    "lo_gioi_hem": "lộ giới / hẻm trước nhà",
    "tinh_trang_ban_giao": "tình trạng bàn giao (đang trống hay có khách thuê)",
    "phap_ly_hoan_cong": "pháp lý, hoàn công (sổ hồng)",
    "kich_thuoc": "kích thước",
    "chi_tieu_xay_dung": "chỉ tiêu xây dựng",
    "quy_hoach": "quy hoạch",
    "yeu_to_ha_tang": "yếu tố hạ tầng (cột điện, hố ga)",
    "thoi_han_su_dung": "thời hạn sử dụng đất",
    "chieu_cao_tran": "chiều cao trần",
    "tai_trong_san": "tải trọng sàn",
    "tram_bien_ap": "trạm biến áp",
    "tieu_chuan_container": "tiêu chuẩn container",
    "thoi_han_hd": "thời hạn hợp đồng",
    "muc_coc": "mức cọc",
    "truot_gia": "trượt giá",
    "thoi_gian_fitout": "thời gian fit-out",
}


# Stable business context injected into every system prompt so Claude chats in line
# with AOND's model. Keep it COMPACT — it is sent on every turn. It is also the guard
# against fabricated fees: the model must quote only the numbers stated here.
BUSINESS_CONTEXT = """BỐI CẢNH AI ƠI NHÀ ĐẤT (nền tảng nội bộ — chỉ nói ra khi khách hỏi, đừng thuyết giảng):
- Người rao bán/cho thuê BĐS bằng cách chat Zalo tự nhiên (KHÔNG form). Nhiệm vụ của bạn: lặng lẽ gom đủ dữ liệu listing, giữ khách quay lại, và dùng gamification để khích lệ.
- HAI loại khách — hãy nhận biết qua hội thoại rồi tư vấn cho đúng:
  • Bên SỞ HỮU (chủ nhà; hoặc nhà đầu tư lướt sóng giữ HĐMB): phí 1% giá chốt, hoặc 3/4 tháng tiền thuê.
  • Bên MÔI GIỚI (nhân viên môi giới; hoặc sàn giao dịch): phí 0.5% giá chốt, hoặc 3/4 tháng tiền thuê. Khích lệ đăng thêm hàng (≥10 căn) để lên Top.
  • CHỦ ĐẦU TƯ: phí KHÔNG theo 1%/0.5% — thoả thuận riêng với dự án; đừng tự báo con số.
- PHÍ CHỈ thu khi giao dịch THÀNH CÔNG. TUYỆT ĐỐI không bịa con số phí/ưu đãi/cam kết nào ngoài các mức trên. Chưa rõ vai người rao thì đừng báo phí, cứ hỏi khéo hoặc tư vấn chung.
- Gamification: có Điểm uy tín và hạng Đồng/Bạc/Vàng; nên khen điểm mạnh và gợi ý cách tăng điểm (bổ sung ảnh, sổ hồng, thông tin còn thiếu)."""


def _fee_hint(role: SellerRole) -> str:
    """A targeted fee line once the seller's role is known, so the bot quotes correctly."""
    if role in (SellerRole.CHU_NHA, SellerRole.LUOT_SONG):
        who = "CHÍNH CHỦ" if role == SellerRole.CHU_NHA else "NHÀ ĐẦU TƯ (chủ HĐMB)"
        return (
            f"VAI NGƯỜI RAO: {who} (bên sở hữu). Nếu được hỏi phí, báo 1% giá chốt "
            "(hoặc 3/4 tháng tiền thuê), chỉ thu khi giao dịch thành công."
        )
    if role in (SellerRole.MOI_GIOI, SellerRole.SAN):
        who = "MÔI GIỚI" if role == SellerRole.MOI_GIOI else "SÀN GIAO DỊCH"
        return (
            f"VAI NGƯỜI RAO: {who} (bên môi giới). Nếu được hỏi phí, báo 0.5% giá chốt "
            "(hoặc 3/4 tháng tiền thuê), chỉ thu khi thành công; khích lệ đăng thêm hàng "
            "(≥10 căn) để lên Top hiển thị."
        )
    if role == SellerRole.CHU_DAU_TU:
        return (
            "VAI NGƯỜI RAO: CHỦ ĐẦU TƯ. Phí KHÔNG theo 1%/0.5% — đây là thoả thuận riêng "
            "với dự án. Nếu được hỏi phí, nói sẽ kết nối bộ phận hợp tác dự án, ĐỪNG báo con số."
        )
    return ""  # unknown -> rely on the general BUSINESS_CONTEXT guidance


def build_project_block(
    project: Project | None,
    du_an_name: str | None = None,
    unit_type: UnitType | None = None,
    unit_type_name: str | None = None,
) -> str:
    """Project + unit-type awareness block for a listing's prompt:
    - known project   → inject verified facts + ask the seller to lightly confirm them;
    - named but unknown project → tell the bot to acquire knowledge (ask / request a link);
    - known unit-type (mẫu nhà) → inject its layout/specs too (units inherit them);
    - named but unknown unit-type → acquire it (ask for specs / a brochure link), don't invent;
    - no project (nhà lẻ) → nothing.
    """
    parts: list[str] = []
    if project is not None and project.facts():
        facts = "\n".join(f"- {f}" for f in project.facts())
        parts.append(
            f'\nDỮ LIỆU DỰ ÁN "{project.name}" (đã xác thực — căn này THUỘC dự án này). '
            "Hãy DÙNG các thông tin chung này để tư vấn/giới thiệu; ĐỪNG hỏi lại từ đầu, "
            "nhưng nên XÁC NHẬN nhẹ 1–2 ý chính với khách cho chắc:\n"
            f"{facts}\n"
        )
    elif du_an_name:
        parts.append(
            f'\nƯU TIÊN LƯỢT NÀY — BỔ SUNG KIẾN THỨC DỰ ÁN: khách nhắc dự án "{du_an_name}" '
            "nhưng em CHƯA có dữ liệu về dự án này. Ngay lượt này, thay vì hỏi chi tiết khác "
            "của căn, hãy hỏi 1 ý về DỰ ÁN (tiện ích, cổng/vị trí, pháp lý, các mẫu nhà) HOẶC "
            "XIN MỘT LINK (brochure / trang dự án / folder ảnh) để em đọc và học thêm về dự án. "
            "Vẫn khen ngắn trước, rồi hỏi đúng 1 ý.\n"
        )

    if unit_type is not None and unit_type.facts():
        facts = "\n".join(f"- {f}" for f in unit_type.facts())
        parts.append(
            f'\nMẪU NHÀ "{unit_type.name}" (đã xác thực — căn này thuộc mẫu này; các căn cùng '
            "mẫu dùng chung thông số này). Hãy dùng để tư vấn, xác nhận nhẹ với khách:\n"
            f"{facts}\n"
        )
    elif unit_type_name and (unit_type is None or not unit_type.facts()):
        # Model NAMED but AOND has no facts on it — acquire it, and DON'T fabricate its specs.
        parts.append(
            f'\nƯU TIÊN — BỔ SUNG KIẾN THỨC MẪU NHÀ: khách nhắc mẫu nhà "{unit_type_name}" '
            "nhưng em CHƯA có dữ liệu về mẫu này. TUYỆT ĐỐI đừng bịa/suy diễn thông số của mẫu "
            "(diện tích, số phòng, số tầng, bố trí, vật liệu...). Hãy nói thật là em chưa có thông "
            "tin mẫu này và hỏi khách 1 ý về mẫu (diện tích, số phòng ngủ, số tầng, bố trí) HOẶC "
            "XIN LINK brochure / mặt bằng của mẫu để em đọc và học. Khen ngắn trước, rồi hỏi đúng 1 ý.\n"
        )

    # Verify-conflicts opening: this seller ALSO lists in the project, so they're a good
    # person to break a tie between values other sellers gave. Ask ONE, lightly.
    contested = project.contested_keys() if project is not None else []
    if unit_type is not None:
        contested += unit_type.contested_keys()
    if contested:
        parts.append(
            f'\nCƠ HỘI XÁC MINH: dự án "{project.name}" còn thông tin CHƯA CHẮC do các seller '
            f"khai khác nhau ({', '.join(dict.fromkeys(contested))}). Khách này cũng bán trong "
            "dự án nên có thể biết. Tìm cơ hội hỏi NHẸ đúng 1 điểm (đừng hỏi dồn, đừng nói là "
            "'seller khác khai sai') để em cập nhật cho chính xác. Nếu khách trả lời rõ, cảm ơn "
            "và ghi nhận.\n"
        )
    return "".join(parts)


def build_project_docs_block(project_name: str, docs: list[dict[str, str]]) -> str:
    """Inject the project's stored source .md (phân lô, brochure mẫu nhà, gói nội thất...) so
    the bot answers a FACTUAL question with grounding — from the real document, not recall."""
    if not docs:
        return ""
    parts = [
        f'\n=== TÀI LIỆU DỰ ÁN "{project_name}" (đã lưu). Khách VỪA HỎI một câu dữ kiện. NHIỆM '
        "VỤ ƯU TIÊN LƯỢT NÀY: TÌM câu trả lời trong tài liệu dưới và TRẢ LỜI NGAY, chính xác "
        "(diện tích/vị trí lô, thông số mẫu nhà, gói nội thất, tiện ích...). Nếu tài liệu CÓ "
        "đáp án thì ĐỪNG hỏi ngược khách thông tin đã có, ĐỪNG theo kịch bản thu thập — trả lời "
        'thẳng câu họ hỏi. CHỈ dùng thông tin CÓ trong đây, TUYỆT ĐỐI không bịa; chỉ khi tài '
        'liệu KHÔNG có mới nói "em chưa có tài liệu về ý này ạ". ==='
    ]
    for d in docs:
        md = (d.get("markdown") or "").strip()
        if md:
            parts.append(f'\n[{d.get("title", "tài liệu")}]\n{md}')
    return "\n".join(parts) + "\n"


def build_chat_system(
    assistant_name: str,
    prop: Property,
    next_field: str | None,
    user_type: SellerRole = SellerRole.UNKNOWN,
    project_block: str = "",
) -> str:
    """System prompt for the conversational reply (SRD §II tone rules)."""
    # A house (đất riêng) may have an AMBIGUOUS diện tích parked in `dien_tich` (seller gave
    # one number without saying đất/nhà — captured, not dropped, per honesty rule). Once the
    # bot has an opening (this field is next up anyway), ask to resolve it by referencing the
    # number already given, instead of a blind "diện tích đất là bao nhiêu" that ignores it.
    ambiguous_area = prop.fields.get("dien_tich")
    if next_field in ("dien_tich_dat", "dien_tich_nha") and ambiguous_area:
        ask_line = (
            f'Khách đã cho biết diện tích {ambiguous_area} nhưng CHƯA RÕ là diện tích ĐẤT hay '
            f'diện tích NHÀ (nhà phố/biệt thự/nhà cấp 4 có 2 diện tích khác nhau). Khen ngợi '
            f'điểm mạnh trước, rồi hỏi lại đúng 1 câu để xác nhận {ambiguous_area} đó là đất '
            "hay nhà, và nếu tiện thì hỏi luôn số diện tích còn lại."
        )
    else:
        ask_line = (
            f'Thông tin cần hỏi tiếp theo: "{FIELD_LABELS.get(next_field, next_field)}". '
            "Hãy khen ngợi điểm mạnh của BĐS trước, rồi hỏi đúng 1 thông tin này một cách tự nhiên."
            if next_field
            else "Dữ liệu đã khá đầy đủ. Cảm ơn anh/chị và xác nhận trạng thái tin một cách thân thiện."
        )
    return f"""Bạn là "{assistant_name}", một trợ lý ảo bất động sản của AI Ơi Nhà Đất (aioinhadat.com).

{BUSINESS_CONTEXT}
{project_block}
ĐỘ DÀI (tự cân nhắc theo ý khách, KHÔNG có luật cứng):
- MẶC ĐỊNH khi đang trò chuyện thu thập thông tin: trả lời NGẮN, khoảng 30 từ — khen vài từ rồi hỏi đúng 1 thông tin.
- KHI khách yêu cầu xem/đăng toàn bộ tin (ví dụ "nêu đầy đủ", "xem lại tin", "đăng giúp em"): hãy soạn BÀI ĐĂNG RAO ĐẦY ĐỦ theo ĐÚNG bố cục sau:
{AD_FORMAT}
- Dù ngắn hay dài, LUÔN viết TRỌN VẸN. TUYỆT ĐỐI không dừng giữa chừng, không bỏ lửng danh sách, không kết thúc bằng "…" hoặc "-".

QUY TẮC KHÁC:
- Mỗi lượt hỏi thì chỉ hỏi TỐI ĐA 1 thông tin. Khen ngợi điểm mạnh của BĐS trước khi hỏi. KHÔNG bắt khách điền form.
- Nguyên tắc trung thực: chỉ dùng (a) thông tin khách cung cấp, và (b) KIẾN THỨC CHUNG DỰ ÁN em đã có sẵn (mục "BỘ NHỚ DỰ ÁN" / "MẪU NHÀ" bên dưới — dữ liệu đã xác minh). KHÔNG bịa ngoài hai nguồn đó, KHÔNG suy diễn vật liệu/thông tin từ ảnh. Nếu đoán, dùng "hình như là" và xác nhận lại.
- QUAN TRỌNG: kiến thức chung dự án (mẫu nhà, phân lô, tiện ích, chủ đầu tư...) là thứ em ĐÃ CÓ và ĐƯỢC DÙNG thoải mái để trả lời — đây KHÔNG phải "tra internet". Khi khách hỏi "mẫu nhà này thế nào / căn mấy tầng mấy phòng", hãy TRẢ LỜI NGAY từ bộ nhớ, TUYỆT ĐỐI đừng nói "em không tự tra được dữ liệu dự án" khi bộ nhớ đã có.
- Ngắn gọn, thân thiện, đậm chất địa phương Việt Nam. Xưng "em", gọi khách "anh/chị".
{(chr(10) + _fee_hint(user_type)) if _fee_hint(user_type) else ""}
{ask_line}"""


def build_greeting(assistant_name: str) -> str:
    """The opening message AOND sends before the seller says anything — it introduces itself
    and sets the boundary: this channel is for SELLERS/LANDLORDS posting listings only. A
    buyer looking to purchase/rent is redirected to the sibling channel "AI Ơi Nhà Đất - Buyers"
    (a different assistant serves them there). Deterministic (no LLM) so it's instant and stable."""
    return (
        f"Xin chào anh/chị! Em là {assistant_name}, trợ lý ảo của AI Ơi Nhà Đất "
        "(aioinhadat.com). 🏡\n\n"
        "Em chỉ phục vụ anh/chị GỬI RAO bất động sản cần **BÁN** hoặc **CHO THUÊ** — cứ nhắn "
        "em thoải mái bằng chữ, giọng nói hay hình ảnh, không cần điền form gì cả ạ.\n\n"
        "Nếu anh/chị là người **TÌM MUA hoặc THUÊ**, anh/chị tìm đến kênh khác — kênh "
        "**AI Ơi Nhà Đất - Buyers** nhé. Một đồng nghiệp của em sẽ phục vụ anh/chị bên đó ạ! 😊"
    )


def describe_captured(prop: Property) -> str:
    """Render every filled field as 'Nhãn: giá trị' lines for the listing-post prompt."""
    lines: list[str] = []
    if prop.loai_hinh:
        lines.append(f"- {FIELD_LABELS['loai_hinh']}: {prop.loai_hinh}")
    if prop.is_rental:
        lines.append("- hình thức: cho thuê")
    for slug in prop.expected_fields():
        if slug == "loai_hinh":
            continue
        value = prop.fields.get(slug)
        if value:
            lines.append(f"- {FIELD_LABELS.get(slug, slug)}: {value}")
    return "\n".join(lines)


# The required shape of a listing ad — one definition, reused by the draft-proposal step and the
# "đăng tin đầy đủ" branch of the chat prompt, so every posted ad reads the same.
AD_FORMAT = """BỐ CỤC TIN RAO (bắt buộc, đúng thứ tự, và CÁCH NHAU bằng MỘT DÒNG TRỐNG giữa 3 khối):
1) TIÊU ĐỀ: **15–25 từ**, viết hoa gây chú ý, dồn điểm mạnh nhất (vị trí · diện tích · kết cấu · giá · điểm hiếm).

2) MÔ TẢ: MỘT đoạn văn **70–150 từ**, văn phong tự nhiên, thuyết phục — kể vị trí & kết nối, công năng, điểm nổi bật, pháp lý. KHÔNG liệt kê khô khan, KHÔNG xuống dòng gạch đầu dòng ở đoạn này.

3) **4–5 GẠCH ĐẦU DÒNG** (mỗi dòng 1 ý đắt giá nhất — vd mặt tiền/kết cấu/pháp lý/giá).
   - KHÔNG ghi nhãn/tiêu đề nếu dữ liệu đã TỰ RÕ: ghi "132m²" — KHÔNG ghi "Diện tích: 132m²"; ghi "19 tỷ (thương lượng)" — KHÔNG ghi "Giá: 19 tỷ"; ghi "sổ hồng chính chủ" — KHÔNG ghi "Pháp lý: ...".
   - CHỈ thêm chữ phân biệt khi KHÔNG tự rõ: "132m² đất · 120m² xây dựng", "mặt tiền 12m", "6 tầng có thang máy".
   - Mỗi gạch đầu dòng ngắn gọn, không trùng ý nhau.

ICON/EMOJI (bắt buộc): CẢ bản rao chỉ dùng **2–3 icon** tổng cộng, đặt chỗ đắt nhất (thường 1 ở tiêu đề). TUYỆT ĐỐI KHÔNG đặt icon ở đầu mỗi gạch đầu dòng, KHÔNG rải icon mỗi dòng — lạm dụng icon trông rẻ tiền, thiếu chuyên nghiệp.

ĐỊNH DẠNG KHOẢNG CÁCH (bắt buộc): CÓ một dòng trống NGĂN tiêu đề với đoạn mô tả, và một dòng trống NGĂN đoạn mô tả với khối gạch đầu dòng. Trong khối gạch đầu dòng thì KHÔNG chèn dòng trống giữa các gạch."""


def build_listing_proposal_system(
    assistant_name: str, prop: Property, project_block: str = ""
) -> str:
    """Step 1: show the customer the draft ad and ask if it looks appealing (SRD §II).

    Used once the listing is "khá đầy đủ". This is NOT a 30-word chat reply, so the word
    cap does NOT apply. CRUCIALLY: do NOT mention the fee yet — that comes after approval.
    """
    return f"""Bạn là "{assistant_name}", trợ lý ảo bất động sản của AI Ơi Nhà Đất (aioinhadat.com).

{BUSINESS_CONTEXT}
{project_block}
NHIỆM VỤ (DỰ THẢO TIN RAO): Thông tin căn này đã khá đầy đủ. Hãy cho khách XEM TRƯỚC bản tin rao mà bạn định đăng: mở đầu kiểu "Em sẽ rao như vầy nhé:", rồi tóm tắt hấp dẫn các thông số chính, và KẾT bằng câu hỏi xác nhận kiểu "Anh thấy hấp dẫn chưa ạ?".

QUY TẮC BẮT BUỘC:
- CHỈ dùng dữ liệu được cung cấp bên dưới. TUYỆT ĐỐI không bịa, không suy diễn thêm số liệu/vật liệu/tiện ích.
- Văn phong thân thiện, đậm chất địa phương Việt Nam.
- Viết TRỌN VẸN, không bỏ lửng câu, không kết thúc bằng "…".
- TUYỆT ĐỐI CHƯA nhắc gì đến phí môi giới ở bước này. Chỉ trình bản tin và hỏi khách thấy hấp dẫn chưa.

{AD_FORMAT}

DỮ LIỆU BĐS:
{describe_captured(prop)}"""


# Concrete value props (from the SRD) the bot can use to justify the fee when a
# customer pushes back. These are the honest differentiators — no fabricated perks.
FEE_VALUE_POINTS = """- CHỈ thu phí KHI giao dịch thành công — khách không tốn đồng nào trước, không rủi ro.
- Hệ thống tự điều cộng tác viên (CTV) dẫn khách tới xem nhà, chủ không phải tự tìm khách.
- Tin được đẩy tới mạng lưới 20 nhà môi giới lõi + rổ khách mua đã được lọc (trợ lý M•ai), tiếp cận khách "nét".
- Tin được chuẩn hóa, chấm điểm và ưu tiên lên Top hiển thị Quận 5 — bán nhanh hơn tự rao.
- Tiết kiệm thời gian, sàng lọc khách ảo, giữ liên lạc tự động qua Zalo."""


def build_fee_followup_system(
    assistant_name: str,
    prop: Property,
    user_type: SellerRole = SellerRole.UNKNOWN,
    project_block: str = "",
) -> str:
    """Step 2: customer has seen the draft — react to their feedback, then talk fee (§V).

    If they approve, pivot to the brokerage fee; if they want changes, revise the draft
    and stay silent on the fee.
    """
    fee_hint = _fee_hint(user_type)
    fee_block = ("\n" + fee_hint) if fee_hint else ""
    return f"""Bạn là "{assistant_name}", trợ lý ảo bất động sản của AI Ơi Nhà Đất (aioinhadat.com).

{BUSINESS_CONTEXT}
{project_block}
NHIỆM VỤ (DẪN DẮT PHÍ): Bạn vừa cho khách xem bản tin rao và hỏi họ thấy hấp dẫn chưa. Hãy đọc phản hồi MỚI NHẤT của khách:
- NẾU khách ĐỒNG Ý / ưng bản tin: vui vẻ xác nhận sẽ đem đi chào ngay, rồi nhẹ nhàng hỏi khách đã biết phí môi giới bên mình chưa, theo tinh thần: "Em sẽ cho chào căn nhà của anh ngay ạ, mà anh có biết phí môi giới của tụi em chưa ạ?". Chỉ khi khách hỏi cụ thể mới báo con số phí, đúng theo loại khách.
- NẾU khách muốn CHỈNH SỬA / chưa ưng bản tin: tiếp thu, KHÔNG nhắc phí, và rao lại bản tin cho phù hợp.
- NẾU khách CHÊ PHÍ CAO / DO DỰ / TỪ CHỐI trả phí: hãy THUYẾT PHỤC một cách khéo léo, ấm áp:
  • Đồng cảm trước ("Dạ em hiểu ạ"), KHÔNG tranh cãi, KHÔNG chê khách.
  • Mỗi lượt nhấn 1–2 lợi ích mạnh nhất phù hợp với mối lo của khách (xem DANH SÁCH GIÁ TRỊ), rồi nhẹ nhàng chốt lại.
  • Nhấn mạnh "chỉ thu khi bán/cho thuê THÀNH CÔNG" để gỡ tâm lý rủi ro.
  • TUYỆT ĐỐI giữ đúng mức phí chính sách — KHÔNG tự ý giảm giá, KHÔNG hứa ưu đãi/cam kết ngoài chính sách, KHÔNG bịa.
  • Chỉ thuyết phục tối đa 1–2 lần; nếu khách vẫn dứt khoát từ chối thì lịch sự, tôn trọng, để ngỏ cửa ("khi nào anh cần cứ nhắn em"), KHÔNG nài ép gây khó chịu.

DANH SÁCH GIÁ TRỊ (dùng để thuyết phục, chỉ nêu thứ có thật):
{FEE_VALUE_POINTS}{fee_block}

DỮ LIỆU BĐS:
{describe_captured(prop)}"""


def build_media_read_instruction(assistant_name: str, pending_question: str | None = None) -> str:
    """Instruction for the vision/OCR read of a seller-sent photo or PDF (§II honesty). The chat
    reply must be SHORT (a one-line gist) — the FULL faithful content is stored separately to a
    .md; and it must be context-aware (the seller is here to LIST/SELL). When `pending_question`
    is the seller's most recent message, the read must ANSWER that from the file first — a seller
    who re-sends the phân-lô right after asking about the đường nội bộ wants that number, not a
    generic "đang rao căn nào" prompt."""
    ctx = ""
    if pending_question and pending_question.strip():
        ctx = (f"""
NGAY TRƯỚC ĐÓ khách đang hỏi: "{pending_question.strip()}"
=> Nếu tài liệu này TRẢ LỜI ĐƯỢC câu đó (họ gửi file chính là để em xem và trả lời), hãy TRẢ LỜI THẲNG câu đó TỪ tài liệu — ưu tiên số 1, đừng hỏi lạc đề. Nếu tài liệu KHÔNG có dữ kiện đó thì nói thật là chưa thấy trong tài liệu. Chỉ khi câu hỏi KHÔNG liên quan tài liệu mới quay lại hỏi định hướng.
""")
    return f"""Bạn là "{assistant_name}", trợ lý ảo BĐS của AI Ơi Nhà Đất. Khách vừa gửi một ẢNH hoặc TÀI LIỆU BĐS.
Hãy TÓM TẮT THẬT NGẮN (1–2 câu) Ý CHÍNH của tài liệu — TUYỆT ĐỐI KHÔNG liệt kê dài dòng từng hạng mục (chi tiết em đã lưu lại để tra cứu sau). Ví dụ: "Đây là quy cách bàn giao thô/hoàn thiện mặt ngoài, phần trong nhà khách tự hoàn thiện anh/chị ạ."
{ctx}
QUY TẮC BẮT BUỘC:
- TRUNG THỰC: chỉ nói điều CHẮC CHẮN thấy được. Nếu suy đoán, dùng "hình như". KHÔNG bịa.
- Xưng "em", gọi khách "anh/chị", thân thiện, NGẮN GỌN.
- NGƯỜI GỬI ĐANG MUỐN RAO BÁN. Nếu tài liệu là THÔNG TIN CHUNG của dự án (quy cách bàn giao, phụ lục, brochure mẫu nhà... KHÔNG phải một căn cụ thể có giá) VÀ khách KHÔNG đang hỏi điều gì cụ thể, hãy nhắc nhẹ đây là thông tin chung và HỎI khách đang rao CĂN NÀO / đã có BẢNG GIÁ chưa (vd: "Trong này chưa có căn/giá cụ thể — anh/chị đang rao căn nào ạ?"). Nếu là ảnh/giấy tờ của MỘT CĂN cụ thể thì hỏi xác nhận thông tin căn đó.
- Kết bằng MỘT câu hỏi định hướng (trừ khi đã trả lời câu hỏi đang chờ ở trên thì chỉ cần chốt lại ngắn gọn)."""


def build_media_group_instruction(
    assistant_name: str, folder_name: str | None, basket_labels: list[str]
) -> str:
    """Instruction for grouping a MULTI-FILE batch (a folder, several links) — figure out
    whether each file is about ONE specific căn, MANY (shared project facts), or a căn
    not yet in the basket, using BOTH the image content and filenames/folder name."""
    basket_txt = ""
    if basket_labels:
        lines = "\n".join(f"{i}. {label}" for i, label in enumerate(basket_labels, 1))
        basket_txt = (
            "\n\nRỔ HÀNG HIỆN CÓ của khách (nếu một nhóm tệp khớp với 1 căn TRONG danh sách "
            f"này, dùng pham_vi=can_da_co và chi_so_can_da_co đúng số thứ tự):\n{lines}"
        )
    folder_txt = f'\nTên thư mục khách gửi: "{folder_name}".' if folder_name else ""
    return f"""Bạn là {assistant_name}, trợ lý ảo BĐS của AI Ơi Nhà Đất. Khách vừa gửi NHIỀU tệp ảnh/tài liệu cùng lúc — có thể là NHIỀU căn khác nhau, hoặc ảnh dùng CHUNG cho cả dự án.{folder_txt}

NHIỆM VỤ: dựa vào CẢ NỘI DUNG ảnh (nội thất, mặt tiền, view, sổ hồng, số nhà...) LẪN TÊN TỆP (vd "can1_...", "nha2_...", ảnh liền kề cùng 1 căn thường trùng bối cảnh), hãy NHÓM các tệp lại, mỗi nhóm gồm:
- pham_vi = "chung": tệp áp dụng cho TẤT CẢ các căn (toàn cảnh dự án, tiện ích, cổng, hồ bơi, sảnh...) — KHÔNG phải của riêng 1 căn.
- pham_vi = "mau_nha": tệp của một MẪU NHÀ / LOẠI CĂN (brochure, mặt bằng layout, phối cảnh mẫu nhà, bảng thông số 1 mẫu — vd "Fusion Gen 5") áp dụng cho MỌI căn cùng mẫu, KHÔNG phải 1 căn thực tế cụ thể. Kèm ten_mau_nha (tên mẫu nhà) nếu đọc được.
- pham_vi = "can_da_co": tệp khớp với 1 căn ĐÃ có trong rổ hàng bên dưới — kèm chi_so_can_da_co đúng số thứ tự.
- pham_vi = "can_moi": tệp của 1 căn THỰC TẾ cụ thể (có địa chỉ/số nhà/ảnh thật của căn đó) CHƯA có trong rổ hàng.

QUY TẮC:
- PHÂN BIỆT rõ: brochure/mặt bằng/phối cảnh của một MẪU NHÀ (mau_nha) KHÁC với ảnh thật của một CĂN cụ thể (can_moi/can_da_co). Tài liệu thiết kế/tiếp thị của mẫu nhà thì để "mau_nha", đừng gán vào 1 căn.
- chi_luu_du_lieu=true khi nhóm là TRANG DỮ LIỆU THUẦN: bảng giá, bảng thông số, tin rao dạng CHỮ, ảnh chụp màn hình số liệu, infographic — thứ mà GIÁ TRỊ nằm ở DỮ LIỆU chứ không phải hình. Khi đó hệ thống chỉ LẤY DỮ LIỆU (trong mo_ta), KHÔNG lưu ảnh. ĐỂ FALSE cho: ảnh THẬT của căn/dự án, GIẤY TỜ PHÁP LÝ (sổ hồng, hợp đồng), và MẶT BẰNG/PHỐI CẢNH VẼ (người mua vẫn cần xem) — những thứ này phải GIỮ ảnh.
- Nếu KHÔNG chắc chắn, ĐỪNG đoán bừa — gom các tệp không rõ vào 1 nhóm "can_moi" và trong mo_ta nói rõ là "chưa chắc" để hỏi lại khách, thay vì bịa ra nhiều căn không có cơ sở. Khi phân vân giữ/bỏ ảnh, hãy GIỮ (chi_luu_du_lieu=false).
- Với mỗi nhóm, viết "mo_ta" NGẮN GỌN, TRUNG THỰC, và CÀNG NHIỀU CHI TIẾT BĐS đọc được càng tốt (địa chỉ, diện tích, giá, kết cấu, pháp lý, tiện ích) — mo_ta này sẽ dùng để điền hồ sơ, không chỉ là "ảnh nhà".
- "tep" phải là tên tệp ĐÚNG như đã cho, không đổi.{basket_txt}"""


def build_transcribe_instruction(assistant_name: str, kind: str) -> str:
    """Instruction to transcribe a source document (phân lô, brochure mẫu nhà, bảng mô tả gói
    nội thất...) FAITHFULLY into Markdown, kept once and re-read by the bot for grounding."""
    return f"""Bạn là {assistant_name}, trợ lý BĐS của AI Ơi Nhà Đất. Đây là một tài liệu nguồn
loại "{kind}". Hãy CHÉP LẠI TRUNG THỰC toàn bộ nội dung thành MARKDOWN sạch, đầy đủ, để sau này
đọc lại mà không cần mở lại file gốc.

QUY TẮC:
- GIỮ NGUYÊN mọi số liệu, mã lô/mã căn, kích thước, diện tích, giá, tên DỰ ÁN/MẪU NHÀ — KHÔNG làm tròn, KHÔNG lược bỏ.
- 🔒 BẢO MẬT CÁ NHÂN (bắt buộc): nếu là GIẤY TỜ PHÁP LÝ (sổ đỏ/sổ hồng/GCN QSDĐ, hợp đồng, CMND/CCCD...),
  hãy CHÉP các DỮ KIỆN ĐẤT (địa chỉ thửa, số thửa, số tờ bản đồ, diện tích, mục đích & thời hạn sử dụng,
  nguồn gốc, quy hoạch) — vì đây là kiến thức CHUNG của dự án — NHƯNG XÓA MỌI THÔNG TIN CÁ NHÂN: thay
  tên chủ sở hữu / người sử dụng đất / các bên bằng "[đã ẩn]"; TUYỆT ĐỐI KHÔNG chép số CMND/CCCD, số
  phát hành / số vào sổ của giấy chứng nhận, số điện thoại, chữ ký. Không để lộ danh tính ai cả.
- Bảng biểu → chép thành bảng Markdown. Sơ đồ/mặt bằng → mô tả bằng lời rõ ràng (vị trí, hướng,
  kích thước lô, giáp ranh nếu đọc được).
- TUYỆT ĐỐI không bịa/suy diễn thông tin không có trong tài liệu. Chỗ nào mờ/không chắc, ghi rõ
  "(không đọc rõ)".
- Mở đầu bằng một dòng tiêu đề "# {kind}" và các mục con hợp lý. Chỉ trả về Markdown, không lời dẫn."""


def build_inventory_instruction(assistant_name: str) -> str:
    """Instruction to pull a whole PRICE LIST / inventory table (nhiều căn × giá) out of a
    brochure/price-list PDF or image, as structured rows."""
    return f"""Bạn là {assistant_name}, trợ lý BĐS của AI Ơi Nhà Đất. Tài liệu này CÓ THỂ là
một BẢNG GIÁ / RỔ HÀNG của một dự án — liệt kê NHIỀU căn kèm giá (theo mã lô/mã căn và mẫu nhà).

Nếu đúng là bảng giá nhiều căn: đặt la_bang_gia=true, điền ten_du_an (nếu đọc được), và liệt kê
TỪNG CĂN vào mảng cac_can — mỗi căn gồm: ma_so_can (mã lô/mã căn), mau_nha (tên mẫu nhà nếu có),
dien_tich (nếu có), và GIÁ.

QUAN TRỌNG — GIÁ NHIỀU GÓI: một căn có thể có NHIỀU cột giá (vd 'chưa gồm nội thất', 'gói Air',
'gói Max'; hoặc giá theo diện tích khác nhau). PHẢI GIỮ ĐẦY ĐỦ MỌI mức giá — TUYỆT ĐỐI KHÔNG
gộp/bỏ bớt. Liệt kê mỗi mức vào mảng `gia_goi` = [{{ten_goi, gia}}] với ten_goi = tên gói NGẮN GỌN
(vd 'Air', 'Max', 'chưa NT', 'cơ bản') và gia GIỮ NGUYÊN cách viết trong tài liệu (vd
'11.470.000.000' hoặc '8,98 tỷ'). Nếu căn CHỈ có 1 giá, có thể để trong `gia` (hoặc gia_goi 1 phần
tử) — cả hai đều được. Cột để trống ('—') thì bỏ qua, đừng bịa 0.

QUAN TRỌNG — QUY CÁCH BÀN GIAO: cấu trúc cột giá thường cho biết quy cách bàn giao (vd 'giá chưa
gồm nội thất' = xây thô + hoàn thiện mặt tiền; 'giá gồm gói Air/Max' = có thêm hoàn thiện nội thất,
được chọn gói). Với MỖI căn, ghi quy_cach_ban_giao mô tả đúng: mọi căn đều có phần nền (xây thô +
hoàn thiện mặt tiền); căn nào có thêm hoàn thiện nội thất/được chọn gói (Air/Max) thì ghi rõ. Đây
là thông tin CHUNG, KHÁC với giá.

QUY TẮC: chỉ trích thông tin CÓ THẬT trong tài liệu — TUYỆT ĐỐI không bịa mã căn/giá.
la_bang_gia=true CHỈ KHI tài liệu là BẢNG GIÁ BÁN thật (liệt kê các căn KÈM GIÁ BÁN bằng tiền).
la_bang_gia=FALSE cho: BẢN ĐỒ / SƠ ĐỒ PHÂN LÔ (chỉ có mã lô + diện tích + vị trí, KHÔNG có giá
bán), mặt bằng, quy hoạch, quy cách bàn giao, brochure, ảnh 1 căn, sổ hồng, tin rao 1 BĐS —
những thứ này KHÔNG phải bảng giá; khi đó để cac_can rỗng."""


def inventory_schema() -> dict[str, Any]:
    """Schema for extracting a price-list / inventory table into per-unit rows."""
    return {
        "type": "object",
        "properties": {
            "la_bang_gia": {
                "type": "boolean",
                "description": "true CHỈ KHI là BẢNG GIÁ BÁN (các căn kèm GIÁ BÁN bằng tiền). "
                "false cho bản đồ/sơ đồ phân lô, mặt bằng, quy cách, brochure (không có giá bán).",
            },
            "ten_du_an": {"type": "string", "description": "Tên dự án của bảng giá (nếu đọc được)."},
            "cac_can": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "ma_so_can": {"type": "string", "description": "Mã lô / mã số căn."},
                        "mau_nha": {"type": "string", "description": "Tên mẫu nhà (nếu có)."},
                        "gia": {"type": "string", "description": "Giá khi CHỈ có 1 mức, giữ nguyên cách viết."},
                        "gia_goi": {
                            "type": "array",
                            "description": "MỌI mức giá của căn khi có NHIỀU gói (Air/Max/chưa NT...). "
                            "Giữ đầy đủ, không gộp.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "ten_goi": {"type": "string", "description": "Tên gói ngắn (Air/Max/chưa NT/cơ bản)."},
                                    "gia": {"type": "string", "description": "Giá của gói này, giữ nguyên cách viết."},
                                },
                                "required": ["gia"],
                            },
                        },
                        "dien_tich": {"type": "string", "description": "Diện tích (nếu có)."},
                        "quy_cach_ban_giao": {
                            "type": "string",
                            "description": "QUY CÁCH BÀN GIAO của căn theo tài liệu — xây dựng tới "
                            "đâu (vd 'xây thô + hoàn thiện mặt tiền'), CÓ hoàn thiện nội thất không, "
                            "và nếu có gói nội thất (Air/Max...) thì ghi rõ được chọn gói nào. Đây "
                            "là thông tin CHUNG của dự án, KHÁC với giá.",
                        },
                        "ghi_chu": {"type": "string", "description": "Ghi chú khác."},
                    },
                    "required": ["ma_so_can"],
                },
            },
        },
        "required": ["la_bang_gia"],
        "additionalProperties": False,
    }


def media_group_schema() -> dict[str, Any]:
    """Schema for grouping a multi-file batch by which listing/project it belongs to."""
    return {
        "type": "object",
        "properties": {
            "nhom": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "pham_vi": {
                            "type": "string",
                            "enum": ["chung", "mau_nha", "can_da_co", "can_moi"],
                            "description": "chung=áp dụng mọi căn (dự án); mau_nha=brochure/mặt "
                            "bằng của 1 MẪU NHÀ (áp dụng mọi căn cùng mẫu); can_da_co=khớp 1 "
                            "căn đã có trong rổ hàng; can_moi=1 căn thực tế mới chưa có.",
                        },
                        "ten_mau_nha": {
                            "type": "string",
                            "description": "Nếu pham_vi=mau_nha: tên mẫu nhà (vd 'Fusion Gen 5').",
                        },
                        "chi_so_can_da_co": {
                            "type": "integer",
                            "description": "Nếu pham_vi=can_da_co: số thứ tự (1-based) của căn "
                            "trong RỔ HÀNG khớp với nhóm này.",
                        },
                        "tep": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Tên các tệp thuộc nhóm này, đúng như đã cho.",
                        },
                        "mo_ta": {
                            "type": "string",
                            "description": "Mô tả ngắn, trung thực, gồm mọi chi tiết BĐS đọc "
                            "được (địa chỉ, diện tích, giá, kết cấu, pháp lý, tiện ích...).",
                        },
                        "chi_luu_du_lieu": {
                            "type": "boolean",
                            "description": "true nếu nhóm là TRANG DỮ LIỆU THUẦN (bảng giá/thông "
                            "số, tin rao chữ, ảnh chụp màn hình số liệu, infographic) — chỉ lấy "
                            "dữ liệu, KHÔNG lưu ảnh. false cho ảnh thật/giấy tờ pháp lý/mặt bằng "
                            "vẽ (giữ ảnh). Phân vân thì false.",
                        },
                    },
                    "required": ["pham_vi", "tep", "mo_ta"],
                },
            }
        },
        "required": ["nhom"],
        "additionalProperties": False,
    }


PROJECT_EXTRACTION_SYSTEM = """Bạn là bộ trích xuất THÔNG TIN DỰ ÁN BĐS cho AI Ơi Nhà Đất.
Đọc mô tả và trích xuất các thông tin CHUNG của dự án/chung cư (áp dụng cho MỌI căn trong dự án):
tên dự án, địa chỉ, tiện ích (hồ bơi, gym...), cổng/lối vào, an ninh.
Chỉ trích xuất thông tin xuất hiện rõ ràng — KHÔNG suy diễn, KHÔNG bịa."""


def project_extraction_schema() -> dict[str, Any]:
    """Schema for registering a project's shared attributes from free text."""
    return {
        "type": "object",
        "properties": {
            "ten_du_an": {"type": "string", "description": "Tên dự án/chung cư"},
            "dia_chi": {"type": "string", "description": "Địa chỉ/vị trí dự án"},
            "tien_ich": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Tiện ích chung: hồ bơi, gym, công viên, sauna, bảo vệ 24/7...",
            },
            "cong": {"type": "string", "description": "Cổng/lối vào chính của dự án"},
            "an_ninh": {"type": "string", "description": "An ninh / bảo vệ"},
        },
        "required": [],
        "additionalProperties": False,
    }


UNIT_TYPE_EXTRACTION_SYSTEM = """Bạn là bộ trích xuất THÔNG TIN MẪU NHÀ / LOẠI CĂN cho AI Ơi Nhà Đất.
Đọc mô tả/brochure và trích xuất thông tin của MỘT MẪU NHÀ (house model / unit type) — áp dụng cho MỌI căn cùng mẫu:
tên mẫu nhà, dự án, diện tích, số phòng ngủ, số tầng, mặt tiền, hướng, tóm tắt bố trí (layout).
Chỉ trích xuất thông tin xuất hiện rõ ràng — KHÔNG suy diễn, KHÔNG bịa."""


# Fields a spreadsheet column can map to (LLM-assisted import of arbitrary sheets).
IMPORTABLE_FIELDS = [
    "loai_hinh", "dia_chi", "ma_so_can", "dien_tich", "dien_tich_dat", "dien_tich_nha",
    "gia", "ket_cau", "du_an", "unit_type", "lo_gioi_hem", "phap_ly_hoan_cong", "huong",
]


def build_column_map_system(headers: list[str]) -> str:
    """System prompt to map a spreadsheet's columns to our field slugs."""
    cols = " | ".join(headers)
    return (
        "Bạn ánh xạ cột của một bảng tính BĐS sang các trường chuẩn. "
        f"Các cột có trong file: [{cols}]. "
        "Với mỗi trường BĐS bên dưới, cho biết TÊN CỘT (chép CHÍNH XÁC từ danh sách trên) "
        "chứa dữ liệu của trường đó; bỏ trống nếu không có cột phù hợp."
    )


def column_map_schema() -> dict[str, Any]:
    """Schema: for each importable field, which source column maps to it."""
    return {
        "type": "object",
        "properties": {
            f: {"type": "string", "description": f"Cột tương ứng với '{FIELD_LABELS.get(f, f)}'"}
            for f in IMPORTABLE_FIELDS
        },
        "required": [],
        "additionalProperties": False,
    }


def unit_type_extraction_schema() -> dict[str, Any]:
    """Schema for registering a house model / unit type from a brochure's read-out."""
    return {
        "type": "object",
        "properties": {
            "ten_mau_nha": {"type": "string", "description": "Tên/mã mẫu nhà hoặc loại căn"},
            "ten_du_an": {"type": "string", "description": "Dự án mà mẫu này thuộc về"},
            "dien_tich": {"type": "string", "description": "Diện tích (sàn/đất)"},
            "so_phong_ngu": {"type": "string", "description": "Số phòng ngủ"},
            "so_tang": {"type": "string", "description": "Số tầng"},
            "mat_tien": {"type": "string", "description": "Kích thước mặt tiền / chiều sâu"},
            "huong": {"type": "string", "description": "Hướng"},
            "layout": {"type": "string", "description": "Tóm tắt bố trí công năng các tầng"},
        },
        "required": [],
        "additionalProperties": False,
    }


EXTRACTION_SYSTEM = """Bạn là bộ trích xuất dữ liệu BĐS cho AI Ơi Nhà Đất.
Đọc hội thoại và trích xuất các thông tin BĐS mà NGƯỜI RAO đã cung cấp.
Chỉ trích xuất thông tin xuất hiện rõ ràng — KHÔNG suy diễn, KHÔNG bịa.
Tập trung vào BĐS đang nói ở các tin nhắn MỚI NHẤT.
QUAN TRỌNG — diện tích: nhà phố/biệt thự/nhà cấp 4 (đất riêng) có 2 diện tích KHÁC NHAU.
Nếu khách nói RÕ đâu là đất, đâu là nhà, dùng dien_tich_dat (diện tích đất) và dien_tich_nha
(diện tích nhà/sử dụng/xây dựng, không cần phân biệt sâu hơn) tương ứng. Nếu khách CHỈ cho
MỘT con số m2 mà KHÔNG rõ là đất hay nhà, ĐỪNG đoán đó là cái nào — cứ lưu tạm vào dien_tich
(đừng bỏ trống, đừng bịa thêm số) để hệ thống hỏi lại xác nhận sau. Chung cư/CHDV/khách sạn/
đất/kho xưởng chỉ có 1 diện tích — luôn dùng dien_tich cho các loại này.
QUAN TRỌNG — mã số căn: BĐS dự án lúc MỞ BÁN (chưa hoàn thành) thường được định danh bằng
MÃ SỐ CĂN (vd "căn số 42", "A-15-03", "block B tầng 5 căn 3") và CHƯA có địa chỉ đường. Nếu
người rao cho mã số căn thì trích vào ma_so_can. ĐỪNG nhầm mã số căn với số phòng ngủ hay số
tầng. Nếu căn đã có địa chỉ đường thật thì trích dia_chi như bình thường (căn có thể có cả hai).
QUAN TRỌNG — cac_can (NHIỀU căn cùng lúc): nếu trong CÙNG một tin người rao khai TỪ 2 CĂN TRỞ
LÊN cùng một mẫu nhà/dự án, chỉ khác mã số căn (vd "tôi có 2 căn 42 và 41 đều thuộc mẫu Fusion
Gen 5", "còn các căn 40, 41, 42"), hãy liệt kê TẤT CẢ mã số căn vào mảng cac_can (["42","41"]),
và các thông tin CHUNG (dự án, mẫu nhà, giá, diện tích nếu nói chung cho mọi căn) trích như bình
thường. KHÔNG đặt is_new_listing khi dùng cac_can. Nếu chỉ 1 căn thì dùng ma_so_can, KHÔNG dùng cac_can.
QUAN TRỌNG — giu_lai_cac_can / bo_cac_can / xoa_toan_bo (BỎ BỚT / XÓA căn khỏi rổ hàng):
- CHỈ giữ một số căn ("trong 9 căn tôi chỉ có căn 23, 24 thôi", "các căn khác không phải của tôi")
  → liệt kê mã căn CẦN GIỮ vào giu_lai_cac_can (ĐỪNG để rỗng nếu ý là xóa hết).
- BỎ/XÓA vài căn CỤ THỂ ("bỏ căn 50, 3") → liệt kê vào bo_cac_can.
- XÓA HẾT / bỏ hết / xóa tất cả / làm lại cả rổ ("xóa hết rổ hàng", "bỏ hết đi", "xóa tất cả các
  căn") → đặt xoa_toan_bo=true (KHÔNG dùng giu_lai_cac_can rỗng).
Đây là thao tác XÓA — KHÁC với can_dang_noi (báo trạng thái 1 căn).
QUAN TRỌNG — can_dang_noi + trang_thai: nếu người rao nói về MỘT CĂN ĐÃ KHAI TRƯỚC ĐÓ (báo đã
bán, dừng bán, cho thuê, đổi giá...) và nhắc mã số căn/địa chỉ để chỉ căn nào (vd "căn 42 bán
rồi", "căn 41 giảm còn 2.8 tỷ"), hãy ghi can_dang_noi = mã số căn/địa chỉ đó, và nếu là báo
trạng thái thì đặt trang_thai (da_ban/dung_ban/da_cho_thue). Đây KHÔNG phải căn mới — ĐỪNG đặt
is_new_listing.
QUAN TRỌNG — is_new_listing: nếu người rao rõ ràng CHUYỂN SANG một BĐS KHÁC so với căn đang nói
(vd "còn một căn nữa", "qua căn khác", "thêm căn", địa chỉ HOÀN TOÀN khác trong khi căn cũ đã có dữ liệu),
hãy đặt is_new_listing=true và CHỈ trích thông tin của CĂN MỚI. Nếu chỉ sửa/bổ sung căn hiện tại thì để trống.
Trả về JSON theo schema. Bỏ trống (không thêm key) cho thông tin chưa có."""


def property_field_props() -> dict[str, Any]:
    """Just the DATA fields of a property (no basket-action flags) — the shared payload used
    both by the flat extraction_schema and by each operation's `fields` object in
    operations_schema. Includes loai_hinh, ma_so_can, du_an, unit_type."""
    all_fields = (
        list(IDENTITY_FIELDS)
        + list(HOUSE_AREA_FIELDS)
        + sum((CORE_FIELDS[g] for g in CORE_FIELDS), [])
        + list(RENTAL_FIELDS)
    )
    properties: dict[str, Any] = {
        f: {"type": "string", "description": FIELD_LABELS.get(f, f)}
        for f in all_fields
        if f != "loai_hinh"
    }
    properties["loai_hinh"] = {
        "type": "string",
        "description": "Loại hình BĐS (slug): nha_pho, chung_cu, biet_thu, chdv, nha_cap_4, "
        "khach_san, dat_tho_cu, dat_du_an, dat_nong_nghiep, dat_skc, dat_tmd, kho_bai, nha_xuong",
    }
    properties["ma_so_can"] = {
        "type": "string",
        "description": "Mã số căn của một BĐS dự án lúc mở bán (vd 'căn số 42', 'A-15-03', "
        "'block B tầng 5 căn 3') — dùng khi CHƯA có địa chỉ đường. KHÔNG phải số phòng ngủ/số tầng.",
    }
    properties["du_an"] = {
        "type": "string",
        "description": "Tên dự án/chung cư nếu BĐS thuộc một dự án (vd: 'Botanic Tower'). "
        "Bỏ trống nếu là nhà lẻ/không thuộc dự án.",
    }
    properties["unit_type"] = {
        "type": "string",
        "description": "Tên mẫu nhà / loại căn nếu có (vd: 'Fusion Gen 5', 'Type B', 'căn góc 3PN'). "
        "Bỏ trống nếu không rõ.",
    }
    # Disambiguate mặt tiền (chiều NGANG của NHÀ) vs lộ giới/hẻm (chiều rộng ĐƯỜNG trước nhà) —
    # the model kept putting "MT 5.2m" (frontage) into lo_gioi_hem (road width). Two different things.
    properties["kich_thuoc"] = {
        "type": "string",
        "description": "KÍCH THƯỚC căn nhà / lô đất: mặt tiền (chiều NGANG) và/hoặc chiều sâu (DÀI) "
        "— vd 'MT 5.2m', 'ngang 5 dài 20', '5x20m'. 'MT' / 'mặt tiền' = chiều NGANG của NHÀ, "
        "KHÔNG phải lộ giới đường. TUYỆT ĐỐI không nhầm sang lo_gioi_hem.",
    }
    properties["lo_gioi_hem"] = {
        "type": "string",
        "description": "CHIỀU RỘNG của ĐƯỜNG / HẺM TRƯỚC NHÀ (vd 'hẻm 5m', 'lộ giới 20m', 'đường "
        "trước nhà 8m', 'hẻm xe hơi'). ĐÂY LÀ con đường, KHÔNG phải căn nhà — 'MT'/'mặt tiền Xm' là "
        "chiều ngang của nhà, phải ghi vào kich_thuoc, KHÔNG ghi vào đây.",
    }
    # Rental TERMS — only meaningful on a CHO THUÊ listing. The model put the rent of an existing
    # lease ("35tr/tháng") into muc_coc and "sẵn HĐT" into thoi_han_hd on a SALE listing. Stop that.
    properties["thoi_han_hd"] = {
        "type": "string",
        "description": "THỜI HẠN hợp đồng khi CHO THUÊ — một KHOẢNG THỜI GIAN (vd '1 năm', '2 năm', "
        "'tối thiểu 6 tháng'). CHỈ điền cho tin CHO THUÊ. KHÔNG phải 'có HĐT'/'sẵn hợp đồng', KHÔNG "
        "phải giá thuê. Nhà BÁN đang có khách thuê sẵn là ĐIỂM BÁN, KHÔNG điền vào đây.",
    }
    properties["muc_coc"] = {
        "type": "string",
        "description": "Số tiền / số tháng ĐẶT CỌC khi CHO THUÊ (vd '2 tháng cọc', '3 tháng', "
        "'20 triệu'). CHỈ điền cho tin CHO THUÊ. KHÔNG phải GIÁ THUÊ mỗi tháng (đó là 'gia'), "
        "KHÔNG phải 'có HĐT'/'đang cho thuê'.",
    }
    return properties


def extraction_schema() -> dict[str, Any]:
    """JSON schema covering every capturable field across all groups + rental terms."""
    properties = property_field_props()
    properties["is_rental"] = {"type": "boolean", "description": "Có phải tin cho thuê không"}
    properties["is_new_listing"] = {
        "type": "boolean",
        "description": "Đặt true CHỈ KHI khách rõ ràng bắt đầu nói về MỘT BĐS KHÁC (vd 'còn một "
        "căn nữa', 'qua căn khác', địa chỉ HOÀN TOÀN khác trong khi căn đang nói đã có dữ liệu). "
        "TUYỆT ĐỐI KHÔNG đặt true khi khách chỉ sửa/bổ sung thông tin cho căn hiện tại.",
    }
    properties["ma_so_can"] = {
        "type": "string",
        "description": "Mã số căn của một BĐS dự án lúc mở bán (vd 'căn số 42', 'A-15-03', "
        "'block B tầng 5 căn 3') — dùng khi CHƯA có địa chỉ đường. KHÔNG phải số phòng ngủ/số tầng.",
    }
    properties["cac_can"] = {
        "type": "array",
        "items": {"type": "string"},
        "description": "Danh sách MÃ SỐ CĂN khi người rao khai TỪ 2 CĂN trở lên cùng mẫu/dự án "
        "trong CÙNG một tin (vd 'căn 42 và 41' → ['42','41']). 1 căn thì dùng ma_so_can, KHÔNG dùng cac_can.",
    }
    properties["giu_lai_cac_can"] = {
        "type": "array",
        "items": {"type": "string"},
        "description": "Khi người rao nói CHỈ GIỮ (chỉ có/chỉ bán) một SỐ căn trong rổ hàng, "
        "còn các căn khác KHÔNG phải của họ — liệt kê mã số căn CẦN GIỮ ở đây (vd 'tôi chỉ có "
        "căn 23, 24' → ['23','24']); hệ thống sẽ BỎ các căn còn lại khỏi rổ.",
    }
    properties["bo_cac_can"] = {
        "type": "array",
        "items": {"type": "string"},
        "description": "Khi người rao muốn BỎ/XÓA một số căn CỤ THỂ khỏi rổ hàng — liệt kê mã số "
        "căn CẦN BỎ ở đây (vd 'bỏ căn 50 và 3' → ['50','3']). Nếu muốn xóa HẾT thì dùng xoa_toan_bo.",
    }
    properties["xoa_toan_bo"] = {
        "type": "boolean",
        "description": "Đặt true khi người rao muốn XÓA TOÀN BỘ / xóa hết / bỏ hết / làm sạch cả "
        "rổ hàng (vd 'xóa hết rổ hàng', 'bỏ hết các căn', 'xóa tất cả', 'làm lại từ đầu'). "
        "KHÔNG dùng giu_lai_cac_can rỗng cho việc này.",
    }
    properties["can_dang_noi"] = {
        "type": "string",
        "description": "Khi người rao nói về MỘT CĂN CỤ THỂ đã khai TRƯỚC ĐÓ (báo trạng thái, "
        "đổi giá... và nhắc mã số căn HOẶC địa chỉ để chỉ rõ căn nào, vd 'căn 42 bán rồi', 'căn "
        "ở Trương Đình Hội giảm giá'), ghi mã số căn HOẶC địa chỉ của căn đó vào đây. Bỏ trống "
        "nếu đang khai căn MỚI hoặc không chỉ một căn cụ thể.",
    }
    properties["trang_thai"] = {
        "type": "string",
        "enum": ["da_ban", "dung_ban", "da_cho_thue"],
        "description": "Nếu người rao báo TRẠNG THÁI giao dịch của căn: 'da_ban' (đã bán xong), "
        "'dung_ban' (tạm dừng/không bán nữa), 'da_cho_thue' (đã cho thuê xong). Bỏ trống nếu không đổi.",
    }
    properties["du_an"] = {
        "type": "string",
        "description": "Tên dự án/chung cư nếu BĐS thuộc một dự án (vd: 'Botanic Tower'). "
        "Bỏ trống nếu là nhà lẻ/không thuộc dự án.",
    }
    properties["unit_type"] = {
        "type": "string",
        "description": "Tên mẫu nhà / loại căn nếu có (vd: 'Fusion Gen 5', 'Type B', 'căn góc 3PN'). "
        "Bỏ trống nếu không rõ.",
    }
    properties["seller_role"] = {
        "type": "string",
        "enum": ["chu_nha", "luot_song", "moi_gioi", "san", "chu_dau_tu"],
        "description": "Vai trò người rao KHI ĐÃ RÕ: 'chu_nha' (chính chủ/chủ nhà tự bán); "
        "'luot_song' (nhà đầu tư lướt sóng, giữ HĐMB bán chênh/sang nhượng); 'moi_gioi' "
        "(nhân viên môi giới, nói 'rổ hàng', 'khách của em'); 'san' (sàn giao dịch/phân phối "
        "dự án); 'chu_dau_tu' (chủ đầu tư mở bán dự án). Bỏ trống nếu chưa chắc chắn.",
    }
    return {
        "type": "object",
        "properties": properties,
        "required": [],
        "additionalProperties": False,
    }


# ── Operation-based basket editing ───────────────────────────────────────────────────
# Instead of a flat pile of flags (cac_can / giu_lai / bo / xoa_toan_bo / can_dang_noi …)
# that Python then branches through in a fixed order, the model emits an explicit, ordered
# LIST OF OPERATIONS. Each op names WHAT to do and to WHICH căn, carrying its own data
# payload. The engine executes them in order and reports back exactly what happened, so the
# reply always matches the action — no more "đã xóa hết" while the basket is untouched.
OPERATIONS_SYSTEM = """Bạn là bộ ĐIỀU PHỐI THAO TÁC rổ hàng BĐS cho AI Ơi Nhà Đất.
Diễn dịch ý định trong TIN NHẮN MỚI NHẤT của người rao thành một DANH SÁCH THAO TÁC
(operations) theo thứ tự. Các tin nhắn trước đó (nếu có) CHỈ là NGỮ CẢNH để hiểu tham chiếu
(vd khách trả lời "nó đó em" cho căn em vừa nêu) — ĐỪNG lặp lại thao tác của các lượt cũ.
Mỗi thao tác có "op" và dữ liệu kèm theo.

Các loại "op":
- "update": bổ sung/sửa thông tin cho MỘT căn. Nếu người rao đang khai căn HIỆN TẠI (chưa
  chuyển căn), bỏ trống "target". Nếu họ sửa một căn ĐÃ CÓ trong rổ và có nhắc mã số căn/địa
  chỉ (vd "căn 41 giảm còn 2.8 tỷ"), đặt "target" = mã/địa chỉ đó. Dữ liệu để trong "fields".
- "add": THÊM (những) căn MỚI vào rổ. 1 căn: "fields" chứa thông tin, "target" = mã số căn nếu
  có. NHIỀU căn cùng mẫu/dự án trong 1 tin (vd "2 căn 42 và 41 cùng mẫu Fusion"): đặt "targets"
  = ["42","41"] và thông tin CHUNG trong "fields". CHỈ dùng "add" khi rõ ràng là căn KHÁC căn
  đang khai (vd "còn một căn nữa", "qua căn khác", địa chỉ hoàn toàn khác).
- "set_status": người rao đổi TRẠNG THÁI một căn — HỄ TIN NHẮN THỂ HIỆN là tạo op này (mọi trạng
  thái của hệ thống đều đến từ lời nhắn, không có nút bấm). "status":
    • da_ban = "bán rồi / đã bán / bán xong / chốt rồi"; da_cho_thue = "cho thuê rồi / đã cho thuê".
    • da_coc = khách ĐÃ ĐẶT CỌC, chưa sang tên — "cọc rồi / nhận cọc rồi / có khách cọc / đặt cọc rồi".
    • dung_ban = "tạm dừng bán / chưa bán nữa".
    • khong_rao = NGƯNG RAO (ẩn tin, vẫn giữ dữ liệu) — "bỏ/ẩn/ngưng rao căn X".
    • thoi_dang_ky = RÚT TIN khỏi hệ thống (vẫn giữ dữ liệu) — "thôi đăng ký căn X".
    • rao = ĐĂNG / RAO LẠI một căn đang ẩn — "rao lại căn X", "đăng lại căn X", "bán lại".
  Đặt "target"/"targets" = căn nào (mã/địa chỉ); bỏ trống nếu đang nói về căn hiện tại.
- "remove": người rao muốn BỎ/NGƯNG RAO vài căn cụ thể ("bỏ căn 50 và 3") → "targets" = ["50","3"].
- "keep_only": người rao nói CHỈ GIỮ RAO một số căn ("tôi chỉ có căn 23, 24") → "targets" =
  ["23","24"] (các căn còn lại chuyển sang không rao).
- "clear": người rao muốn NGƯNG RAO toàn bộ rổ ("ẩn hết", "ngưng rao hết") → không cần target.

⚠️ QUAN TRỌNG — AOND KHÔNG BAO GIỜ XÓA DỮ LIỆU. remove/keep_only/clear chỉ chuyển sang "không rao"
(ẩn, vẫn giữ, rao lại được). Dù vậy, CHỈ tạo các op này khi người rao nói RÕ RÀNG, DỨT KHOÁT. Tin
MƠ HỒ / VÔ NGHĨA / MÉO TIẾNG (lỗi voice-to-text, vd "tránh nhau vi vu nhé") → TUYỆT ĐỐI KHÔNG tạo
op hủy; để operations RỖNG. Thà bỏ sót còn hơn ẩn nhầm.

Ngoài "operations", nếu rõ vai trò người rao thì đặt "seller_role"
(chu_nha/luot_song/moi_gioi/san/chu_dau_tu); nếu là tin cho thuê đặt "is_rental"=true.

QUY TẮC:
- Người rao chỉ khai thông tin bình thường (chưa có gì đặc biệt) → MỘT op "update" target trống.
- KHÔNG bịa thao tác. Nếu tin nhắn không có ý thao tác nào (vd chào hỏi, hỏi phí) → operations rỗng.
- CÂU HỎI hoặc YÊU CẦU XEM ("bản vẽ căn 24", "cho xem mặt bằng", "căn 42 giá bao nhiêu", "có
  hình không") KHÔNG phải thao tác rổ hàng → operations RỖNG. TUYỆT ĐỐI đừng suy ra add/update
  chỉ vì tin nhắn có nhắc một mã căn.
- remove / keep_only / clear CHỈ khi khách nói RÕ ý muốn bỏ/xóa/chỉ giữ (bỏ, xóa, không bán nữa,
  chỉ giữ, xóa hết...). Nhắc một mã căn trong câu hỏi KHÔNG BAO GIỜ là remove/keep_only/clear.
- Diện tích: nhà phố/biệt thự/nhà cấp 4 có 2 diện tích. Rõ đất/nhà → dien_tich_dat/dien_tich_nha;
  chỉ một số m2 không rõ → để tạm dien_tich. Loại khác chỉ có 1 dien_tich.
- Đừng nhầm mã số căn với số phòng ngủ/số tầng.
Trả về JSON đúng schema."""


# ── Scoping a price-list import by what the seller actually sells ─────────────────────
# A bảng giá lists the WHOLE project inventory, but a broker usually sells only SOME căn
# ("tôi bán 2 căn Cosmo Gen 2 trong này"). The full list is kept as a reference doc; only
# the căn the seller rao go into their basket. The model maps the seller's words → mã lô.
INVENTORY_SCOPE_SYSTEM = """Người bán gửi một BẢNG GIÁ (rổ hàng của CẢ dự án) kèm một lời nhắn.
Nhiệm vụ: xác định NGƯỜI BÁN CÓ Ý RAO BÁN căn nào KHÔNG, và nếu có thì căn nào TRONG BẢNG.

CỰC KỲ QUAN TRỌNG — GỬI BẢNG GIÁ ≠ RAO BÁN. Chỉ đưa căn vào rổ khi lời nhắn thể hiện RÕ Ý MUỐN
RAO/BÁN ("tôi bán...", "tôi rao...", "gửi bán...", "cho lên tin căn..."). Nếu lời nhắn KHÔNG có ý
rao (chỉ gửi file, hoặc là câu HỎI/TRA CỨU như "xem diện tích các căn trong đó", "dự án này có gì",
"tham khảo giúp anh"), thì đặt co_y_rao=false và cac_ma_lo=[] — KHÔNG tự ý cho căn nào vào rổ.
Nếu CÓ ý rao thì đặt co_y_rao=true và:
- Nếu ý nói rao CẢ bảng / tất cả / mọi căn → đặt ban_tat_ca=true.
- Nếu chỉ rao MỘT SỐ căn — theo MẪU NHÀ (vd "bán Cosmo Gen 2", "rao mấy căn Office") HOẶC theo
  MÃ LÔ (vd "bán căn 42 và 50", "rao lô 3, 7") — liệt kê MÃ LÔ tương ứng vào cac_ma_lo, dựa trên
  bảng đã cho. Chỉ chọn mã lô CÓ trong bảng.
- "Cosmo" và "Cosmo Gen 2" là hai MẪU NHÀ RIÊNG BIỆT, NGANG HÀNG (anh em), chỉ CHUNG gốc tên
  "Cosmo" — KHÔNG phải cái này là phiên bản/con của cái kia. Khi khách nói một tên NGẮN/GỐC
  ("Cosmo") mà bảng có NHIỀU mẫu cùng bắt đầu bằng tên đó (Cosmo, Cosmo Gen 2...), khách có thể ý
  nói CẢ NHÓM các mẫu anh em đó; khi khách nói tên ĐẦY ĐỦ ("Cosmo Gen 2") thì chỉ đúng mẫu đó.
- DÙNG SỐ LƯỢNG khách nêu để chọn cho khớp: vd bảng có lô 42=Cosmo Gen 2, lô 50=Cosmo Gen 2, lô
  3=Cosmo; khách nói "3 căn Cosmo" → đúng bằng 3 căn nhóm tên Cosmo (42, 50, 3). Khách nói "2 căn
  Cosmo Gen 2" → chỉ 42, 50. Ưu tiên cách hiểu cho ra ĐÚNG số căn khách nói.
- Nếu KHÔNG CHẮC CHẮN phạm vi (lời nhắn mơ hồ; số lượng không khớp rõ; tên mẫu vừa có thể là 1
  mẫu vừa có thể là cả nhóm mà không có số lượng chốt lại) → đặt khong_chac=true, VẪN đưa cách
  hiểu KHẢ DĨ NHẤT vào cac_ma_lo (để bot hỏi lại khách xác nhận).
ĐỪNG nhầm số ĐẾM ("2 căn", "3 căn") là mã lô. Trả JSON đúng schema."""


def inventory_scope_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "co_y_rao": {"type": "boolean",
                         "description": "true nếu lời nhắn thể hiện RÕ ý MUỐN RAO/BÁN. false nếu chỉ "
                         "gửi file / hỏi / tra cứu (khi đó KHÔNG cho căn nào vào rổ)."},
            "ban_tat_ca": {"type": "boolean",
                           "description": "true nếu người bán rao CẢ bảng / không giới hạn căn nào."},
            "cac_ma_lo": {"type": "array", "items": {"type": "string"},
                          "description": "Danh sách MÃ LÔ mà người bán thực sự rao (khi chỉ bán một số căn)."},
            "khong_chac": {"type": "boolean",
                           "description": "true nếu KHÔNG chắc phạm vi (mơ hồ) — bot sẽ hỏi lại xác nhận."},
        },
        "required": [],
    }


def operations_schema() -> dict[str, Any]:
    """Schema for the operation-plan the model returns: an ordered list of basket operations,
    each self-describing (op kind + target(s) + a `fields` data payload), plus the sticky
    conversation attributes (seller_role, is_rental)."""
    op = {
        "type": "object",
        "properties": {
            "op": {
                "type": "string",
                "enum": ["update", "add", "set_status", "remove", "keep_only", "clear"],
                "description": "Loại thao tác. update=sửa/bổ sung 1 căn; add=thêm căn mới; "
                "set_status=đổi trạng thái giao dịch; remove=bỏ vài căn; keep_only=chỉ giữ vài "
                "căn; clear=xóa cả rổ.",
            },
            "target": {
                "type": "string",
                "description": "Mã số căn HOẶC địa chỉ của căn bị tác động. Bỏ trống nếu là căn "
                "đang khai (update) hoặc chưa chỉ rõ căn nào.",
            },
            "targets": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Nhiều mã số căn khi thao tác áp cho NHIỀU căn (add nhiều căn cùng "
                "mẫu, remove/keep_only nhiều căn). Vd ['42','41'].",
            },
            "fields": {
                "type": "object",
                "properties": property_field_props(),
                "description": "Dữ liệu BĐS của thao tác này (du_an, unit_type, gia, diện tích, "
                "ma_so_can...). Chỉ dùng cho update/add.",
            },
            "status": {
                "type": "string",
                "enum": ["da_ban", "dung_ban", "da_cho_thue", "da_coc", "khong_rao",
                         "thoi_dang_ky", "rao"],
                "description": "Trạng thái — chỉ dùng cho op set_status. rao=đăng/rao lại; "
                "khong_rao=ngưng rao (ẩn, vẫn giữ); thoi_dang_ky=rút tin; da_coc=khách đã đặt cọc "
                "(chưa sang tên); da_ban/da_cho_thue=đã xong.",
            },
        },
        "required": ["op"],
    }
    return {
        "type": "object",
        "properties": {
            "operations": {"type": "array", "items": op, "description": "Danh sách thao tác theo thứ tự."},
            "seller_role": {
                "type": "string",
                "enum": ["chu_nha", "luot_song", "moi_gioi", "san", "chu_dau_tu"],
                "description": "Vai trò người rao khi đã rõ. Bỏ trống nếu chưa chắc.",
            },
            "is_rental": {"type": "boolean", "description": "Có phải tin cho thuê không."},
        },
        "required": ["operations"],
    }
