"""Offline mock provider — deterministic, no API key, no network.

Lets the CLI demo and the test-suite run end-to-end. It does *naive* keyword-based
extraction and templated replies, enough to exercise the engine, gamification, and
schema logic. Swap in ClaudeProvider for real language understanding.
"""
from __future__ import annotations

import re
from typing import Any

from ..config import Settings
from .base import LLMProvider, Message

# Very small keyword map mirroring the SRD transcripts. Real extraction is the LLM's job.
_TYPE_KEYWORDS = {
    "nha_pho": ["nhà phố", "nha pho"],
    "chung_cu": ["chung cư", "chung cu", "căn hộ", "can ho"],
    "biet_thu": ["biệt thự", "villa"],
    "nha_xuong": ["nhà xưởng", "nha xuong"],
    "kho_bai": ["kho bãi", "kho bai"],
    "dat_tho_cu": ["đất thổ cư", "dat tho cu", "đất nền", "dat nen"],
}


class MockProvider(LLMProvider):
    def __init__(self, settings: Settings):
        self._settings = settings

    def chat(self, system: str, messages: list[Message], max_tokens: int = 256) -> str:
        if "DỰ THẢO TIN RAO" in system:
            # Step 1: show the draft ad and ask for approval — NO fee yet.
            return (
                "Em sẽ rao như vầy nhé:\n"
                "🏠 BĐS vị trí đẹp, pháp lý rõ ràng, sẵn sàng giao dịch. (offline)\n\n"
                "Anh thấy hấp dẫn chưa ạ?"
            )
        if "DẪN DẮT PHÍ" in system:
            # Step 2: customer approved -> pivot to the brokerage fee.
            return (
                "Dạ tuyệt vời! Em sẽ cho chào căn nhà của anh ngay ạ, mà anh có biết "
                "phí môi giới của tụi em chưa ạ? (offline)"
            )
        # A friendly, complete, <30-word templated reply that praises then asks one thing.
        return (
            "Dạ em ghi nhận rồi ạ! Anh/chị cho em xin thêm 1 thông tin "
            "để hoàn thiện hồ sơ nhé? (offline)"
        )

    def describe_media(self, items: list[tuple[bytes, str]], instruction: str,
                       max_tokens: int = 1024) -> str:
        # Offline stand-in for vision/OCR: a complete, honest-sounding, confirm-ending reply.
        n = len(items)
        what = "tài liệu/ảnh" if n == 1 else f"{n} ảnh/tài liệu"
        return (
            f"Dạ em xem {what} của mình rồi ạ. Hình như là giấy tờ/hình ảnh BĐS. "
            f"Anh/chị xác nhận giúp em thông tin có đúng không ạ? (offline)"
        )

    def group_media(
        self,
        items: list[tuple[bytes, str]],
        labels: list[str],
        instruction: str,
        json_schema: dict[str, Any],
    ) -> dict[str, Any]:
        # Offline stand-in: no pixels to look at, so group purely by FILENAME hints —
        # "can1_..."/"nha2_..." → separate căn; a chung-ish name → shared project info;
        # anything else falls into one leftover "chưa rõ" group.
        chung_kw = ("chung", "tienich", "amenit", "hoboi", "duan", "cong", "sanh")
        by_index: dict[str, list[str]] = {}
        chung: list[str] = []
        other: list[str] = []
        for label in labels:
            norm = re.sub(r"[^a-z0-9]", "", label.lower())
            m = re.search(r"(?:can|nha|unit)(\d+)", norm)
            if m:
                by_index.setdefault(m.group(1), []).append(label)
            elif any(k in norm for k in chung_kw):
                chung.append(label)
            else:
                other.append(label)
        nhom: list[dict[str, Any]] = [
            {"pham_vi": "can_moi", "tep": files,
             "mo_ta": f"Căn #{idx} (offline — chưa đọc chi tiết ảnh, theo tên tệp {files[0]})."}
            for idx, files in by_index.items()
        ]
        if chung:
            nhom.append({"pham_vi": "chung", "tep": chung, "mo_ta": "Ảnh chung của dự án (offline)."})
        if other:
            nhom.append({"pham_vi": "can_moi", "tep": other, "mo_ta": "Ảnh chưa rõ nhóm (offline)."})
        return {"nhom": nhom}

    def extract_media(
        self, items: list[tuple[bytes, str]], instruction: str, json_schema: dict[str, Any]
    ) -> dict[str, Any]:
        # Offline: no pixels to read → not a price list. Tests use a fixed provider double.
        return {}

    def extract(
        self, system: str, messages: list[Message], json_schema: dict[str, Any]
    ) -> dict[str, Any]:
        raw = " ".join(m.content for m in messages if m.role == "user")
        text = raw.lower()
        props = json_schema.get("properties", {})

        # Operation-plan schema: run the same keyword extraction, then reshape the flat flags
        # into an ordered list of operations (the engine's new contract). Keeps all the offline
        # keyword logic; only the OUTPUT shape changes.
        if "operations" in props:
            flat = self.extract(system, messages, {"properties": {}})  # reuse the flat brain
            return _flat_to_operations(flat)

        # Column mapping (LLM-assisted spreadsheet import): map odd headers → field slugs.
        if "ánh xạ cột" in system:
            headers = [h.strip() for h in raw.split(",") if h.strip()]
            keymap = {
                "dia_chi": ["địa", "address", "khu vực", "vị trí"], "gia": ["giá", "price"],
                "dien_tich": ["diện", "dt", "area"], "loai_hinh": ["loại", "type"],
                "du_an": ["dự án", "project"], "ket_cau": ["phòng", "kết cấu", "tầng"],
                "unit_type": ["mẫu", "loại căn"], "phap_ly_hoan_cong": ["pháp lý", "sổ"],
                "huong": ["hướng"], "lo_gioi_hem": ["lộ giới", "hẻm"],
            }
            mapping: dict[str, Any] = {}
            for slug, kws in keymap.items():
                for h in headers:
                    if any(k in h.lower() for k in kws):
                        mapping[slug] = h
                        break
            return mapping

        # Unit-type (house model) extraction from a brochure read-out.
        if "ten_mau_nha" in props:
            out: dict[str, Any] = {}
            m = re.search(r"mẫu nhà\s+([^\n,.:;]+)", raw, re.IGNORECASE)
            if m:
                out["ten_mau_nha"] = m.group(1).strip()
            m = re.search(r"(?:dự án|chung cư)\s+([^\n,.:;]+)", raw, re.IGNORECASE)
            if m:
                out["ten_du_an"] = m.group(1).strip()
            m = re.search(r"(\d+(?:[.,]\d+)?)\s*m2", text)
            if m:
                out["dien_tich"] = f"{m.group(1)}m2"
            m = re.search(r"(\d+)\s*phòng ngủ", text)
            if m:
                out["so_phong_ngu"] = m.group(1)
            return out

        # Project extraction (shared attributes of a condo/development).
        if "tien_ich" in props:
            out: dict[str, Any] = {}
            m = re.search(r"(?:dự án|chung cư|tòa|toà)\s+([^\n,.:;]+)", raw, re.IGNORECASE)
            if m:
                out["ten_du_an"] = m.group(1).strip()
            amenities = [a for a in ("hồ bơi", "gym", "công viên", "sauna", "bảo vệ", "thang máy")
                         if a in text]
            if amenities:
                out["tien_ich"] = amenities
            return out

        out = {}

        for slug, kws in _TYPE_KEYWORDS.items():
            if any(kw in text for kw in kws):
                out["loai_hinh"] = slug
                break

        # Address: capture "đường <name>" or "hẻm <...>".
        m = re.search(r"(hẻm[^.,\n]{0,40}|đường\s+[^.,\n]{0,30})", text)
        if m:
            out["dia_chi"] = m.group(1).strip()

        # Area: a standalone house (đất riêng) has 2 areas — fill dien_tich_dat/dien_tich_nha
        # only when the text explicitly ties a number to "đất"/"nhà"; a lone ambiguous m2
        # figure is parked in dien_tich (captured, not dropped) so the bot can ask to resolve
        # it once it has an opening. Other types: always the single dien_tich.
        is_house = out.get("loai_hinh") in ("nha_pho", "biet_thu", "nha_cap_4")
        m_dat = re.search(r"đất\s*[:\-]?\s*(\d+(?:[.,]\d+)?)\s*m2", text) if is_house else None
        m_nha = re.search(r"nhà\s*[:\-]?\s*(\d+(?:[.,]\d+)?)\s*m2", text) if is_house else None
        if m_dat:
            out["dien_tich_dat"] = f"{m_dat.group(1)}m2"
        if m_nha:
            out["dien_tich_nha"] = f"{m_nha.group(1)}m2"
        if not m_dat and not m_nha:
            m = re.search(r"(\d+(?:[.,]\d+)?)\s*m2", text)
            if m:
                out["dien_tich"] = f"{m.group(1)}m2"

        # Nhiều căn trong 1 tin: "căn 42 và 41", "42 với 41", "các căn 40, 41 và 42". Require an
        # explicit "và"/"với"/"&" conjunction so a trailing area like "căn 42, 70m2" isn't
        # misread as a 2-code list (the real LLM disambiguates by context; the offline mock can't).
        mm = re.search(r"căn(?:\s*số)?[:\s]+(\d+(?:\s*(?:,|và|với|&)\s*\d+)+)", raw, re.IGNORECASE)
        if mm and any(c in mm.group(1).lower() for c in ("và", "với", "&")):
            out["cac_can"] = re.findall(r"\d+", mm.group(1))
        # Mã số căn (pre-sale project unit code): explicit "căn số 42" / "mã (số) căn A-15".
        # (Only the explicit forms — the bare word "căn" is too common to key off safely.)
        m = re.search(r"(?:mã\s*(?:số\s*)?căn|căn\s*số)\s*[:\-]?\s*([A-Za-z0-9][\w\-/.]*)",
                      raw, re.IGNORECASE)
        if m:
            out["ma_so_can"] = m.group(1).strip()

        # Price: "<num> tỷ".
        m = re.search(r"(\d+(?:[.,]\d+)?)\s*tỷ", text)
        if m:
            out["gia"] = f"{m.group(1)} tỷ"

        # Structure: "<n> lầu" / "<n> phòng ngủ".
        if re.search(r"\d+\s*lầu|trệt|lửng", text) or re.search(r"\d+\s*phòng ngủ", text):
            out["ket_cau"] = "đã ghi nhận kết cấu"

        # Trạng thái giao dịch của một căn đã có ("căn 42 bán rồi").
        status_kw = {
            "da_ban": ("bán rồi", "bán được rồi", "đã bán", "bán xong", "bán hết", "chốt rồi"),
            "dung_ban": ("dừng bán", "ngừng bán", "không bán nữa", "tạm dừng"),
            "da_cho_thue": ("cho thuê rồi", "đã cho thuê", "thuê xong"),
        }
        for slug, kws in status_kw.items():
            if any(k in text for k in kws):
                out["trang_thai"] = slug
                break
        # Reference to an existing căn (a "căn <số>" mentioned alongside a status/price change).
        mref = re.search(r"căn\s*(?:số\s*)?(\d+)", raw, re.IGNORECASE)
        if mref and (out.get("trang_thai") or "gia" in out):
            out["can_dang_noi"] = mref.group(1)

        # BỎ BỚT căn khỏi rổ: "chỉ có/giữ căn 23, 24" (keep-only) or "bỏ căn 50, 3" (remove).
        mkeep = re.search(r"chỉ\s+(?:có|giữ|lấy|bán).*?căn\s+([\d\s,vàớiv&]+)", raw, re.IGNORECASE)
        if mkeep:
            out["giu_lai_cac_can"] = re.findall(r"\d+", mkeep.group(1))
        mdrop = re.search(r"(?:bỏ|xóa|loại|hủy)\b.*?căn\s+([\d\s,vàớiv&]+)", raw, re.IGNORECASE)
        if mdrop:
            out["bo_cac_can"] = re.findall(r"\d+", mdrop.group(1))
        # XÓA TOÀN BỘ rổ: "xóa hết", "bỏ hết", "xóa tất cả/toàn bộ/sạch", "làm lại từ đầu".
        if re.search(r"(?:xóa|bỏ|dọn)\s+(?:hết|sạch|tất cả|toàn bộ)|làm lại (?:từ đầu|rổ)", text):
            out["xoa_toan_bo"] = True

        # "A different property" hint → start a new listing.
        if any(k in text for k in ("căn khác", "căn nữa", "còn căn", "thêm căn", "qua căn", "căn tiếp")):
            out["is_new_listing"] = True

        # Rental hint.
        if "cho thuê" in text or "thuê" in text:
            out["is_rental"] = True

        # Seller-role hint (chủ nhà / lướt sóng / môi giới / sàn / chủ đầu tư).
        if "chủ đầu tư" in text or "chu dau tu" in text:
            out["seller_role"] = "chu_dau_tu"
        elif "sàn" in text:
            out["seller_role"] = "san"
        elif "môi giới" in text or "rổ hàng" in text or "giỏ hàng" in text:
            out["seller_role"] = "moi_gioi"
        elif "lướt sóng" in text or "sang nhượng" in text or "bán chênh" in text:
            out["seller_role"] = "luot_song"
        elif "chính chủ" in text or "chủ nhà" in text:
            out["seller_role"] = "chu_nha"

        # Unit-type / mẫu nhà mentioned on a listing.
        m = re.search(r"(?:mẫu nhà|loại căn|mẫu)\s+([^\n,.:;]+)", raw, re.IGNORECASE)
        if m:
            out["unit_type"] = m.group(1).strip()

        return out


def _flat_to_operations(flat: dict[str, Any]) -> dict[str, Any]:
    """Reshape the mock's flat flag dict into the operation-plan the engine now expects.
    Precedence mirrors the old engine: keep_only/remove > clear > act-on-existing (status /
    reference) > fan-out add > new-listing add > update. Basket-state-dependent decisions
    (does a referenced code already exist?) are left to the engine, which resolves targets."""
    out = dict(flat)
    result: dict[str, Any] = {"operations": []}
    role = out.pop("seller_role", None)
    if role:
        result["seller_role"] = role
    if out.pop("is_rental", None):
        result["is_rental"] = True
    ops = result["operations"]

    keep = list(out.pop("giu_lai_cac_can", None) or [])
    drop = list(out.pop("bo_cac_can", None) or [])
    clear = bool(out.pop("xoa_toan_bo", False))
    status = out.pop("trang_thai", None)
    ref = out.pop("can_dang_noi", None)
    codes = list(out.pop("cac_can", None) or [])
    is_new = out.pop("is_new_listing", False)
    fields = {k: v for k, v in out.items() if isinstance(v, str)}
    data = {k: v for k, v in fields.items() if k != "ma_so_can"}

    if keep:  # an explicit keep/drop wins over "bỏ hết" wording
        ops.append({"op": "keep_only", "targets": keep})
    elif drop:
        ops.append({"op": "remove", "targets": drop})
    elif clear:
        ops.append({"op": "clear"})
    elif status and (codes or ref):  # trạng thái báo cho (những) căn đã nhắc
        ops.append({"op": "set_status", "status": status, "targets": codes or [ref], "fields": data})
    elif len(codes) >= 2:  # several units of the same model in one message
        ops.append({"op": "add", "targets": codes, "fields": data})
    elif is_new:  # "thêm căn"/"còn căn nữa" → a NEW căn (beats a spurious ref sub-match)
        ops.append({"op": "add", "fields": fields})
    elif ref:  # a price/field edit that names one existing căn
        ops.append({"op": "update", "target": ref, "fields": data})
    elif status:  # bare status report on the active căn
        ops.append({"op": "set_status", "status": status, "fields": fields})
    elif fields:  # ordinary data on the active căn
        ops.append({"op": "update", "fields": fields})
    return result
