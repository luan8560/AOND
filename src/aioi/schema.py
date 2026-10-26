"""Dynamic property schema (SRD §III).

The AI auto-detects the property type and surfaces only the core fields relevant to
that type's group. Every property also carries shared identity fields and, when it is
a rental, the shared rental terms. Data is accumulated gradually across conversations,
so all fields are optional and start empty.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

# A bare house number / alley code: "391/34", "123", "12A", "198A/22", "15-17".
_HOUSE_NO_RE = re.compile(r"^\d+[\d/\-.]*[A-Za-z]?$")


def _merge_dia_chi(old: str, new: str) -> str:
    """Address fields ACCUMULATE — adding a số nhà must never erase the street/ward already
    captured. Rules: keep the richer value when one contains the other; if the new value is just
    a house number, PREPEND it to the known street ('391/34' + 'Trần Hưng Đạo…' → '391/34 Trần
    Hưng Đạo…'); otherwise treat it as a correction and take the new (fuller) value."""
    old, new = (old or "").strip(), (new or "").strip()
    if not old:
        return new
    if not new or new.lower() in old.lower():
        return old                              # nothing new, or already contained → keep richer old
    if old.lower() in new.lower():
        return new                              # new already includes the old → take fuller new
    if _HOUSE_NO_RE.match(new):
        return f"{new} {old}"                   # a house number → prepend, keep everything else
    return new                                  # a genuinely different/fuller address → correction


def _merge_uu_the(old: str, new: str) -> str:
    """Free-form highlights ACCUMULATE, never overwrite — "bóc tách không thiếu". Each is a short
    phrase; store them joined by ' · '. Appends only phrases not already present (case-insensitive),
    so re-extraction of the same ưu thế won't duplicate it."""
    have = [p.strip() for p in (old or "").split("·") if p.strip()]
    seen = {p.lower() for p in have}
    for p in (s.strip() for s in (new or "").split("·")):
        if p and p.lower() not in seen:
            have.append(p)
            seen.add(p.lower())
    return " · ".join(have)


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


# ── SEMANTIC FACETS (model redesign) ────────────────────────────────────────────────────────
# A property is understood as a few human FACETS, not many atomic form slots. Capture inside a
# facet is FREE-FORM (any attribute the seller mentions, never dropped); these key lists are only
# the KNOWN atoms — used to display by facet and to decide which facet still has nothing. The thin
# set of canonical atoms (loai_hinh, dia_chi/du_an, gia, diện tích...) is what search/folders use.
FACETS: list[tuple[str, str, list[str]]] = [
    ("vi_tri",     "Vị trí",           ["dia_chi", "du_an", "ma_so_can"]),
    ("vi_the",     "Vị thế",           ["lo_gioi_hem", "huong", "view", "vi_the", "goc", "mat_tien_duong"]),
    ("dat",        "Kích thước đất",   ["dien_tich", "dien_tich_dat", "kich_thuoc", "mat_tien", "no_hau"]),
    ("cong_trinh", "Quy mô xây dựng",  ["ket_cau", "dien_tich_nha", "so_tang", "so_phong_ngu", "so_phong_thue", "noi_that"]),
    ("phap_ly",    "Pháp lý",          ["phap_ly_hoan_cong", "nam_so", "quy_hoach", "thoi_han_su_dung"]),
    ("gia_tm",     "Giá & thương mại", ["gia", "tinh_trang_ban_giao", "muc_coc", "thoi_han_hd", "co_hdt", "thu_nhap_thue"]),
]
FACET_LABEL: dict[str, str] = {fid: label for fid, label, _ in FACETS}
_FACET_OF: dict[str, str] = {k: fid for fid, _, keys in FACETS for k in keys}


def facet_of(key: str) -> str:
    """Which facet an attribute key belongs to ('gia_tm' as a catch-all for unknown price-ish /
    commercial-ish keys, else 'khac')."""
    return _FACET_OF.get(key, "khac")


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

    def minimal_missing(self) -> list[str]:
        """What is still needed to be ABLE to rao (publish): LOẠI HÌNH + VỊ TRÍ (địa chỉ hoặc dự
        án) + GIÁ. Everything else is enriched over time — a listing can go live with just these."""
        need: list[str] = []
        if self.loai_hinh is None:
            need.append("loai_hinh")
        if not (self.fields.get("dia_chi") or self.fields.get("du_an")):
            need.append("dia_chi")
        if not self.fields.get("gia"):
            need.append("gia")
        return need

    def can_publish(self) -> bool:
        """True once the minimum to rao is present (loại hình + vị trí + giá)."""
        return not self.minimal_missing()

    def empty_facets(self) -> list[str]:
        """Facets with NO known attribute yet — the pool for ENRICH questions, in facet order.
        Vị trí is covered by minimal_missing (locator), so it's excluded here."""
        out: list[str] = []
        for fid, _, keys in FACETS:
            if fid == "vi_tri":
                continue
            if not any(self.fields.get(k) for k in keys):
                out.append(fid)
        return out

    def by_facet(self) -> list[tuple[str, str, dict[str, str]]]:
        """Captured data grouped by facet: (facet_id, label, {key: value}) for every facet that has
        at least one value. Free-form keys not in any facet fall under 'Khác'. For display."""
        buckets: dict[str, dict[str, str]] = {fid: {} for fid, _, _ in FACETS}
        buckets["khac"] = {}
        for k, v in self.fields.items():
            if v and k not in ("du_an", "ma_so_can") or (k in ("du_an", "ma_so_can") and v):
                buckets.setdefault(facet_of(k), {})[k] = v
        labels = {**FACET_LABEL, "khac": "Khác"}
        return [(fid, labels.get(fid, fid), b) for fid, b in buckets.items() if b]

    def summary_ready(self) -> bool:
        """Ready to DRAFT a real ad — a higher bar than can_publish. The minimum (loại hình + vị
        trí + giá) makes a listing eligible to go live, but AOND should NOT rush to write the ad on
        just that (it reads thin). It keeps asking until the KEY facets are mostly filled: can rao
        AND at most ONE enrichable facet still empty (≥3 of 4: vị thế / đất / công trình / pháp lý).
        After that it drafts, then still welcomes more detail."""
        return self.can_publish() and len(self.empty_facets()) <= 1

    def missing_fields(self) -> list[str]:
        """The pool the AI draws its NEXT question from: first the publish-blockers (minimum to
        rao), then ONE question per still-empty FACET to enrich — never the old 23-slot form."""
        m = self.minimal_missing()
        return m + [f for f in self.empty_facets() if f not in m]

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
            if k == "dia_chi":                  # MERGE, don't overwrite — keep street/ward intact
                v = _merge_dia_chi(self.fields.get("dia_chi", ""), v)
            if k == "uu_the":                   # free-form highlights ACCUMULATE (never drop earlier ones)
                v = _merge_uu_the(self.fields.get("uu_the", ""), v)
            if self.fields.get(k) != v:
                self.fields[k] = v
                newly.append(k)
        return newly
