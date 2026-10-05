"""Dynamic property schema (SRD §III).

The AI auto-detects the property type and surfaces only the core fields relevant to
that type's group. Every property also carries shared identity fields and, when it is
a rental, the shared rental terms. Data is accumulated gradually across conversations,
so all fields are optional and start empty.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class PropertyGroup(str, Enum):
    RESIDENTIAL = "residential"   # Nhóm 1: Nhà ở & Kinh doanh dân dụng
    LAND = "land"                 # Nhóm 2: Đất trống & Đất nền
    INDUSTRIAL = "industrial"     # Nhóm 3: Sản xuất & Thương mại
    UNKNOWN = "unknown"


# Loại hình BĐS -> nhóm. Used to map a detected property type onto its field group.
TYPE_TO_GROUP: dict[str, PropertyGroup] = {
    # Nhóm 1
    "nha_pho": PropertyGroup.RESIDENTIAL,
    "biet_thu": PropertyGroup.RESIDENTIAL,
    "chung_cu": PropertyGroup.RESIDENTIAL,
    "chdv": PropertyGroup.RESIDENTIAL,
    "nha_cap_4": PropertyGroup.RESIDENTIAL,
    "khach_san": PropertyGroup.RESIDENTIAL,
    # Nhóm 2
    "dat_tho_cu": PropertyGroup.LAND,
    "dat_du_an": PropertyGroup.LAND,
    "dat_nong_nghiep": PropertyGroup.LAND,
    # Nhóm 3
    "dat_skc": PropertyGroup.INDUSTRIAL,
    "dat_tmd": PropertyGroup.INDUSTRIAL,
    "kho_bai": PropertyGroup.INDUSTRIAL,
    "nha_xuong": PropertyGroup.INDUSTRIAL,
}

# Trường cốt lõi theo nhóm (SRD §III). These drive which question the AI asks next.
CORE_FIELDS: dict[PropertyGroup, list[str]] = {
    PropertyGroup.RESIDENTIAL: [
        "ket_cau",            # Kết cấu (số lầu, phòng ngủ)
        "lo_gioi_hem",        # Lộ giới / Hẻm
        "tinh_trang_ban_giao",  # Tình trạng bàn giao (trống / có khách thuê)
        "phap_ly_hoan_cong",  # Pháp lý, hoàn công
    ],
    PropertyGroup.LAND: [
        "kich_thuoc",         # Kích thước
        "chi_tieu_xay_dung",  # Chỉ tiêu xây dựng
        "quy_hoach",          # Quy hoạch
        "yeu_to_ha_tang",     # Hạ tầng (vướng cột điện / hố ga)
    ],
    PropertyGroup.INDUSTRIAL: [
        "thoi_han_su_dung",   # Thời hạn sử dụng đất
        "chieu_cao_tran",     # Chiều cao trần
        "tai_trong_san",      # Tải trọng sàn
        "tram_bien_ap",       # Trạm biến áp
        "tieu_chuan_container",  # Tiêu chuẩn Container
    ],
    PropertyGroup.UNKNOWN: [],
}

# Trường nhận diện chung — luôn cần cho mọi BĐS.
IDENTITY_FIELDS: list[str] = [
    "loai_hinh",   # property type slug
    "dia_chi",     # address
    "dien_tich",   # area (m2) — single area (chung cư/đất/công nghiệp: chỉ có 1 diện tích)
    "gia",         # price
]

# Nhà có ĐẤT RIÊNG (không phải căn hộ/chung cư) mang 2 diện tích khác nhau — diện tích đất
# và diện tích nhà (gộp chung "sử dụng"/"xây dựng" thành một trường, theo yêu cầu người dùng:
# không cần tách chi tiết hơn). Các loại còn lại (chung cư, đất, công nghiệp...) chỉ có 1.
HOUSE_TYPES: set[str] = {"nha_pho", "biet_thu", "nha_cap_4"}
HOUSE_AREA_FIELDS: list[str] = ["dien_tich_dat", "dien_tich_nha"]

# A listing is "khá đầy đủ" (ready to draft an ad) at this fraction of expected fields,
# on top of having every identity field. Tune to make the draft fire earlier/later.
SUMMARY_READY_COMPLETENESS: float = 0.75

# Thông số cho thuê (áp dụng chung khi là tin cho thuê) — SRD §III.
RENTAL_FIELDS: list[str] = [
    "thoi_han_hd",   # Thời hạn hợp đồng
    "muc_coc",       # Mức cọc
    "truot_gia",     # Trượt giá
    "thoi_gian_fitout",  # Thời gian Fit-out
]


@dataclass
class Property:
    """A single listing whose data is filled in gradually."""

    loai_hinh: str | None = None        # detected type slug (key of TYPE_TO_GROUP)
    is_rental: bool = False             # tin cho thuê?
    fields: dict[str, str] = field(default_factory=dict)  # all captured values

    @property
    def group(self) -> PropertyGroup:
        if self.loai_hinh is None:
            return PropertyGroup.UNKNOWN
        return TYPE_TO_GROUP.get(self.loai_hinh, PropertyGroup.UNKNOWN)

    def identity_fields(self) -> list[str]:
        """Identity fields for THIS property, adapting to type AND lifecycle phase:
        - area: a standalone house (nhà phố/biệt thự/nhà cấp 4) has its own land, so it uses
          `dien_tich_dat` + `dien_tich_nha` instead of a single `dien_tich`;
        - locator: normally the street `dia_chi`, but a PROJECT unit still in pre-sale (a
          `du_an` is known and NO address yet) is located by `du_an` + `ma_so_can` (mã số
          căn) — a unit "graduates" to being located by `dia_chi` once it has one. See the
          identity-handle-decay note in [[aioi-data-model]]."""
        areas = HOUSE_AREA_FIELDS if self.loai_hinh in HOUSE_TYPES else ["dien_tich"]
        if self.fields.get("du_an") and not self.fields.get("dia_chi"):
            locator = ["du_an", "ma_so_can"]   # pre-sale project unit: no address yet
        else:
            locator = ["dia_chi"]
        return ["loai_hinh", *locator, *areas, "gia"]

    def expected_fields(self) -> list[str]:
        """All fields this property should eventually have, given its type."""
        expected = self.identity_fields() + list(CORE_FIELDS[self.group])
        if self.is_rental:
            expected += RENTAL_FIELDS
        return expected

    def summary_ready(self) -> bool:
        """True once the listing is "khá đầy đủ" — detailed enough to draft a real ad.

        Requires ALL identity fields (type, address, area(s), price) AND a high overall
        completeness, so the draft includes core details, not just the basics.
        """
        if not all(self._has(f) for f in self.identity_fields()):
            return False
        return self.completeness() >= SUMMARY_READY_COMPLETENESS

    def missing_fields(self) -> list[str]:
        """Fields still unknown — the pool the AI draws its next question from."""
        have = {"loai_hinh"} if self.loai_hinh else set()
        have |= {k for k, v in self.fields.items() if v}
        return [f for f in self.expected_fields() if f not in have]

    def completeness(self) -> float:
        """Fraction of expected fields that are filled, in [0, 1]."""
        expected = self.expected_fields()
        if not expected:
            return 0.0
        filled = sum(1 for f in expected if self._has(f))
        return filled / len(expected)

    def _has(self, name: str) -> bool:
        if name == "loai_hinh":
            return self.loai_hinh is not None
        return bool(self.fields.get(name))

    def update(self, captured: dict[str, str]) -> list[str]:
        """Merge newly extracted fields; return the names that were newly set."""
        newly: list[str] = []
        for k, v in captured.items():
            if not v:
                continue
            if k == "loai_hinh":
                if self.loai_hinh != v:
                    self.loai_hinh = v
                    newly.append(k)
                continue
            if self.fields.get(k) != v:
                self.fields[k] = v
                newly.append(k)
        return newly
