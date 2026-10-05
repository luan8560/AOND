"""Conversation engine — orchestrates the chat brain.

Per turn it: (1) silently extracts property data from the user's message into the
dynamic schema, (2) recomputes gamification scores, (3) picks the next single field to
ask about, and (4) generates a <=30-word persona reply. State is kept in-memory per
conversation (a real deployment would persist to SharePoint per SRD §VII).
"""
from __future__ import annotations

import hashlib
import html as _html
import json
import logging
import re
import sqlite3
import time
import unicodedata
from urllib.parse import urlparse
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from uuid import uuid4

from . import gamification as g
from .assistants import Assistant, assign_assistant
from .fetch import (
    DEFAULT_BATCH_BUDGET,
    FetchedMedia,
    FetchError,
    fetch_media,
    fetch_raw,
    is_drive_folder,
    sniff_media_type,
)
from .llm import LLMProvider, Message
from . import bulk
from . import inventory as inv
from .projects import (
    InMemoryProjectStore,
    Project,
    ProjectStore,
    UnitType,
    merge_facts,
    normalize_project_name,
)
from .prompts import (
    EXTRACTION_SYSTEM,
    OPERATIONS_SYSTEM,
    PROJECT_EXTRACTION_SYSTEM,
    UNIT_TYPE_EXTRACTION_SYSTEM,
    build_chat_system,
    build_column_map_system,
    build_project_docs_block,
    build_transcribe_instruction,
    build_fee_followup_system,
    build_greeting,
    build_listing_proposal_system,
    build_inventory_instruction,
    build_media_group_instruction,
    build_media_read_instruction,
    build_project_block,
    column_map_schema,
    extraction_schema,
    inventory_schema,
    inventory_scope_schema,
    media_group_schema,
    operations_schema,
    INVENTORY_SCOPE_SYSTEM,
    project_extraction_schema,
    unit_type_extraction_schema,
)
from .imaging import downscale_image
from .revenue import SellerRole
from .schema import Property
from .sharepoint import MediaStore, SharePointError, extension_for, listing_folder, sanitize_filename

# Headroom so the token cap never truncates a reply. The model controls actual length
# via the prompt (brief while chatting, full ad when asked / complete).
REPLY_MAX_TOKENS = 4096

# Extracted trạng-thái intent → the stored Listing.status value.
STATUS_MAP = {"da_ban": "đã bán", "dung_ban": "dừng bán", "da_cho_thue": "đã cho thuê",
              "khong_rao": "không rao", "thoi_dang_ky": "thôi đăng ký", "rao": "đang bán"}

# AOND NEVER deletes a seller's data. A seller can only RAO (advertise), stop advertising
# (KHÔNG RAO — hidden but kept), or withdraw (THÔI ĐĂNG KÝ — kept). The old "remove/clear" ops
# flip to this status instead of dropping the row, so nothing is ever lost and everything is
# reversible by rao-ing again.
KHONG_RAO = "không rao"
THOI_DANG_KY = "thôi đăng ký"

# Max PHOTOS stored per listing (documents/PDFs aren't capped). The bot still SEES every
# photo the seller sends (vision reads them all); this only bounds what's KEPT in the kho
# gốc, so a seller dumping 100 photos of one căn can't balloon storage.
MAX_PHOTOS_PER_LISTING = 20

# Hard cap on the TOTAL bytes of one send (all files/links/folders together). Over this we
# don't try to process it (a big vision/transcription job on Zalo would time out / cost a lot)
# — we escalate to a human admin who can open the chat and handle it. Keep in step with the
# fetcher's per-file guard in fetch.py.
MAX_SEND_BYTES = 20 * 1024 * 1024  # 20 MB

log = logging.getLogger(__name__)


def _image_count(media: list[dict[str, str]]) -> int:
    """How many of a listing's stored media are photos (not PDFs/docs)."""
    return sum(1 for r in media if not r.get("filename", "").lower().endswith(".pdf"))

# Cheap pre-filter: does a seller's message plausibly DESCRIBE a project/model (worth an
# extra extract to LEARN from), vs. small talk ("ok em", "phí bao nhiêu")? Avoids spending
# an LLM call every turn just because the listing belongs to a project.
_LEARN_HINTS = (
    "phòng", "tầng", "lầu", "m2", "diện tích", "layout", "bố trí", "hướng", "mặt tiền",
    "hồ bơi", "gym", "cổng", "tiện ích", "an ninh", "công viên", "sổ", "pháp lý", "thang máy",
)


def _looks_descriptive(text: str) -> bool:
    t = (text or "").lower()
    return any(h in t for h in _LEARN_HINTS)


# Does the seller/buyer ask a FACTUAL QUESTION we should answer from the project's stored
# source docs (phân lô/brochure/gói nội thất) rather than let the model recall/fabricate?
# Requires BOTH a doc TOPIC and a QUESTION marker — so a seller STATING info ("căn này diện
# tích 50m2") doesn't trigger the answer path; only a real question does.
_DOC_TOPIC_HINTS = (
    "diện tích", "lô ", "lô số", "mẫu nhà", "mẫu ", "gói ", "air", "max", "tiện ích", "hướng",
    "vị trí", "kích thước", "mặt tiền", "pháp lý", "nội thất", "phân lô", "sơ đồ", "cosmo",
    "fusion", "opus", "office", "cashmere", "signature",
)
_QUESTION_MARKERS = (
    "?", "bao nhiêu", "là gì", "thế nào", "gồm gì", "gồm những", "khác nhau", "có gì",
    "cho hỏi", "cho em hỏi", "cho anh hỏi", "cho chị hỏi", "ra sao", "mấy ", "như thế nào",
)


def _wants_project_facts(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in _DOC_TOPIC_HINTS) and any(q in t for q in _QUESTION_MARKERS)


# Max tool-calling steps in one agent turn (a safety cap so the loop can't run away).
MAX_AGENT_STEPS = 6

# The TOOL-usage rules appended to the seller-chat system prompt when the seller is in a project
# with an inventory: the model reasons over the sales-mode prompt AND has these tools — it decides
# per turn whether to query the catalog / add a căn / learn a fact / ask for a link, WITHOUT a
# keyword gate (that's the point of "Mảnh B": no code catching each case).
_AGENT_TOOL_GUIDE = (
    "\n\n=== CÔNG CỤ TRA RỔ HÀNG DỰ ÁN (kiến thức CHUNG) ===\n"
    "Em có công cụ tra dự án. Tự quyết khi nào cần dùng:\n"
    "- Khách nhắc một căn theo ĐẶC ĐIỂM ('căn góc', 'lớn nhất', 'view hồ', 'rẻ nhất') → dùng "
    "tim_can/tra_can để xác định ĐÓ LÀ MÃ SỐ CĂN NÀO. ĐỪNG đoán, ĐỪNG tạo căn rỗng/tin rác.\n"
    "- Khách hỏi về MẪU NHÀ ('mẫu Cosmo Gen 2', 'căn này mấy tầng/mấy phòng', 'bố trí sao') → "
    "tra_mau_nha để lấy thông số CHUNG của mẫu (đã lưu sẵn). Đây KHÔNG phải 'tra internet' — là "
    "kiến thức chung ĐÃ CÓ. TUYỆT ĐỐI đừng nói 'em không tra cứu được' khi tra_mau_nha có dữ liệu.\n"
    "- Thiếu thông tin CÔNG CỘNG (số căn/quy hoạch/chủ đầu tư...) → du_lieu_du_an để biết thiếu "
    "gì; nói thật là chưa có và XIN khách LINK trang tin cậy (em đọc & lưu lại). ĐỪNG bịa.\n"
    "- Khách cho dữ kiện CÔNG CỘNG mới về căn (diện tích, góc, quy cách bàn giao...) → ghi_nho_can/"
    "ghi_nho_du_an (kèm `nguon`) để lưu vào kiến thức chung, seller sau cũng hưởng.\n"
    "- Thêm căn vào rổ: them_can_vao_ro khi đã RÕ mã căn hoặc khách đã xác nhận (tự lấy mẫu/diện "
    "tích/quy cách từ kho chung).\n"
    "- NGƯỜI ĐANG CHAT CHÍNH LÀ NGƯỜI RAO (chủ nhà / môi giới) đang gửi bán căn của họ. GIÁ họ "
    "chào là thông tin CỦA HỌ: khi họ báo/hỏi giá, hãy TIẾP NHẬN và ghi vào RỔ của họ, hoặc nói "
    "lại giá em đang có. TUYỆT ĐỐI KHÔNG coi họ như bên thứ ba, KHÔNG nói 'để em kết nối với bên "
    "rao', KHÔNG xin số điện thoại 'để chuyển cho bên bán' — HỌ chính là bên bán.\n"
    "- 'Giá là của riêng người rao' CHỈ có nghĩa: đừng đưa giá này thành kiến thức CHUNG của dự "
    "án, và đừng lấy giá của người rao KHÁC áp lên. KHÔNG có nghĩa là né người rao đang chat.\n"
    "- Căn/mẫu/diện tích/quy cách bàn giao/quy hoạch là kiến thức CHUNG (dùng lại). Chỉ khẳng "
    "định thông tin công cộng khi CÓ NGUỒN.\n"
)


# A request to SEE a drawing / floor plan / render ("bản vẽ căn 24", "cho xem mặt bằng") — this
# is NOT a basket edit; it must NEVER run listing operations (that once pruned the basket).
_MEDIA_REQUEST_HINTS = (
    "bản vẽ", "bản vẻ", "bản thiết kế", "mặt bằng", "mặt cắt", "sơ đồ", "phối cảnh",
    "layout", "brochure", "mẫu thiết kế", "bản đồ",
)


def _wants_media(text: str) -> bool:
    return any(h in (text or "").lower() for h in _MEDIA_REQUEST_HINTS)


# A read-out description that indicates the file is SHARED REFERENCE knowledge (a phân-lô map,
# floor plan, zoning, handover spec, brochure...) rather than a photo of one specific căn — so it
# should be ingested to a grounded .md, even when it's an IMAGE (not just PDF).
_REFERENCE_DOC_HINTS = (
    "phân lô", "phan lo", "sơ đồ", "so do", "bản đồ", "ban do", "mặt bằng", "mat bang",
    "quy hoạch", "quy cách", "phụ lục", "tiện ích", "layout", "tổng thể", "phối cảnh",
    "bàn giao", "quy cach", "phu luc",
)


def _looks_like_reference_doc(description: str) -> bool:
    return any(h in (description or "").lower() for h in _REFERENCE_DOC_HINTS)


# A sổ đỏ / legal cert in a PROJECT is treated as SHARED knowledge for its LAND facts, but the
# owner's IDENTITY must be stripped before it is stored/relayed (user decision 2026-07). These
# power a deterministic backstop under the LLM redaction in build_transcribe_instruction.
_PII_PHONE_RE = re.compile(r"(?<!\d)(?:\+?84|0)\d{9}(?!\d)")          # VN phone number
_PII_ID_RE = re.compile(r"(?<!\d)\d{9}(?:\d{3})?(?!\d)")              # CMND (9) / CCCD (12) digits
_PII_KEYWORD_RE = re.compile(r"(?i)\b(cccd|cmnd|căn cước công dân|chứng minh nhân dân)\b")
_PII_OWNER_RE = re.compile(                                          # a "Chủ sở hữu: <tên>" field
    # Personal-identity FIELD labels — the ":" requirement keeps "Ông:/Bà:" from catching a place
    # name like "Bà Rịa" (no colon). The property's own address ("Địa chỉ:" in the thửa section) is
    # a LAND fact and is left alone; only the owner's residence ("... thường trú") is stripped.
    r"(?im)^(\s*[-*>#]*\s*(?:họ và tên|họ tên|chủ sở hữu|chủ hộ|người sử dụng đất|"
    r"người mua|người bán|bên mua|bên bán|đại diện|ông|bà|năm sinh|ngày sinh|"
    r"địa chỉ thường trú|nơi thường trú)\s*[:：]).*$")
# TITLE markers of a legal cert (with diacritics — DON'T use ambiguous "so do", which collides
# with "sơ đồ"/site-map). A phân-lô/quy-hoạch title has none of these, so it's never retitled.
_LEGAL_TITLE_HINTS = ("sổ đỏ", "sổ hồng", "gcn", "gcnqsdđ", "giấy chứng nhận")


def _looks_personal_doc(markdown: str) -> bool:
    """A doc carries PERSONAL data only when there's an actual identity SIGNAL — an owner/party
    field, or a CCCD/CMND number/keyword. A mere mention of 'GCN'/'quyền sử dụng đất' (which shows
    up in phân-lô and quy-hoạch legends) is NOT enough, else a site map gets mis-flagged."""
    m = markdown or ""
    return bool(_PII_OWNER_RE.search(m) or _PII_ID_RE.search(m) or _PII_KEYWORD_RE.search(m))


def _redact_personal_data(text: str) -> str:
    """Strip personal IDENTITY from a doc kept as SHARED project knowledge: blank the value of an
    owner/party field, and any CMND/CCCD or phone number. LAND facts (diện tích, thửa, tờ, quy
    hoạch) are untouched. Deterministic backstop under the LLM redaction — belt and braces so a
    sổ đỏ AOND relays never carries someone's name/ID even if the model slips."""
    if not text:
        return text
    text = _PII_OWNER_RE.sub(r"\1 [đã ẩn]", text)
    text = _PII_PHONE_RE.sub("[đã ẩn]", text)
    text = _PII_ID_RE.sub("[đã ẩn]", text)
    return text


def _redact_doc_title(title: str) -> str:
    """A legal-cert filename often embeds the owner's name + certificate serial (e.g. 'NPD_50 …
    GCNQSDĐ 933551 … Vũ Ngọc Nga'). Replace it with a neutral, personal-data-free label. Gated on
    a LEGAL-CERT title marker so a phân-lô/sơ-đồ title is never renamed."""
    low = (title or "").lower()
    if not any(h in low for h in _LEGAL_TITLE_HINTS):
        return title
    m = re.search(r"(?:căn|can|npd|lô|lo|c)[\s_#]*(\d+)", low)
    return f"Sổ đỏ / GCN QSDĐ căn {m.group(1)}" if m else "Sổ đỏ / GCN QSDĐ (đã ẩn thông tin cá nhân)"


# DESTRUCTIVE basket ops never apply on the first read — they wait for the seller to CONFIRM, so a
# misread/garbled message can't wipe the rổ. (See Conversation.pending_destructive.)
_DESTRUCTIVE_OPS = {"clear", "keep_only", "remove"}


def _strip_accents(s: str) -> str:
    s = (s or "").replace("đ", "d").replace("Đ", "D")  # NFD won't decompose đ — map it first
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


# Short-message confirm / cancel words (accent-insensitive) for a pending destructive action.
_AFFIRM_WORDS = {"dung", "ung", "u", "uh", "um", "ok", "oke", "okay", "okie", "co", "phai", "chuan",
                 "dong y", "xac nhan", "yes", "y", "duoc", "xoa", "xoa het", "xoa di", "bo", "dong ri"}
_DENY_WORDS = {"khong", "ko", "k", "thoi", "dung lai", "khoan", "dung co", "huy", "no", "giu", "giu lai",
               "dung xoa", "khong xoa", "sai"}


def _affirms(text: str) -> bool:
    t = _strip_accents((text or "").strip().lower())
    t = re.sub(r"[^\w\s]", " ", t).strip()
    if not t or len(t) > 40:          # a long message isn't a plain "yes" — don't risk a delete
        return False
    if any(w in _DENY_WORDS for w in t.split()) or t in _DENY_WORDS:
        return False
    return t in _AFFIRM_WORDS or any(w in _AFFIRM_WORDS for w in t.split())


def _denies(text: str) -> bool:
    t = _strip_accents((text or "").strip().lower())
    t = re.sub(r"[^\w\s]", " ", t).strip()
    return bool(t) and (t in _DENY_WORDS or any(w in _DENY_WORDS for w in t.split()))


@dataclass
class Turn:
    role: str
    content: str
    at: float


@dataclass
class Listing:
    """One property in a seller's basket (rổ hàng). `listing_id` is the stable surrogate
    identity — never changes even if type/address is reclassified (see [[aioi-data-model]])."""

    listing_id: str = field(default_factory=lambda: uuid4().hex)
    prop: Property = field(default_factory=Property)
    listing_proposed: bool = False  # shown the draft ad & asked "hấp dẫn chưa"?
    history_start: int = 0  # index into the seller's history where THIS listing's turns begin
    status: str = "đang bán"  # đang bán / đã bán / dừng bán / đã cho thuê
    media: list[dict[str, str]] = field(default_factory=list)  # SharePoint MediaRef.to_dict()s

    @property
    def display_status(self) -> str:
        """The lifecycle status as shown to the user. `status` defaults to the sale-oriented
        'đang bán' for every listing; for a RENTAL that reads wrong, so the default active state
        is displayed as 'đang cho thuê'. Terminal states (đã bán / đã cho thuê / dừng bán) are
        explicit and shown as-is."""
        if self.prop.is_rental and self.status == "đang bán":
            return "đang cho thuê"
        return self.status

    def label(self) -> str:
        """Short human label for disambiguation ("căn nào?"). Prefers the street address;
        falls back to a pre-sale project position (dự án + mã số căn) when there's no
        address yet, then the type, then a placeholder."""
        f = self.prop.fields
        loc = f.get("dia_chi")
        if not loc and f.get("ma_so_can"):
            loc = f"{f['du_an']} - {f['ma_so_can']}" if f.get("du_an") else f["ma_so_can"]
        loc = loc or self.prop.loai_hinh or "căn chưa rõ địa chỉ"
        model = f" · mẫu {f['unit_type']}" if f.get("unit_type") else ""
        gia = f" · {f['gia']}" if f.get("gia") else ""
        return f"{loc}{model}{gia} [{self.display_status}]"


@dataclass
class Conversation:
    """One seller (Zalo user). Holds the chat thread + a BASKET of listings (rổ hàng),
    with one active listing the chat is currently about. Seller-level state (role, score,
    assistant) is shared across all their listings (SRD §IV: user score = avg of listings)."""

    user_key: str
    assistant: Assistant
    history: list[Turn] = field(default_factory=list)
    last_ai_at: float | None = None  # for timeliness scoring
    user_type: SellerRole = SellerRole.UNKNOWN  # seller role, once the bot can tell (§V)
    listings: list[Listing] = field(default_factory=list)
    active_listing_id: str = ""
    # Transient (not persisted): the structural basket ops applied on the latest turn, so the
    # reply layer can confirm exactly what happened. Reset each turn by _plan_and_apply.
    last_receipts: list[tuple] = field(default_factory=list)
    # Things handed off to a human admin (e.g. an oversize media send we won't process) — a
    # Live-Chat-Monitor / Takeover (SRD §VI) reads these to know a chat needs attention.
    admin_escalations: list[dict] = field(default_factory=list)
    # The SELLER'S OWN price reference (from a price list they sent) — keyed by normalized mã lô.
    # PRIVATE to this seller; NOT shared, NOT auto-listed. Used to attach their price when they
    # explicitly rao a căn. (Sending a price list ≠ rao bán — the rổ is populated only on intent.)
    price_book: dict[str, dict] = field(default_factory=dict)
    # Transient (not persisted): a clarifying question AOND should append to its next reply when a
    # project name read from a doc looked AMBIGUOUS (a model name, or a near-miss to an existing
    # project) — the "nghi ngờ thì hỏi" rule. Set by import_price_list, consumed by the reply.
    pending_name_note: str = ""
    # Transient (not persisted): a DESTRUCTIVE basket op (clear / bỏ căn / chỉ giữ) the planner
    # proposed but has NOT applied — held until the seller CONFIRMS. Prevents a misread message
    # (e.g. garbled voice-to-text) from silently wiping the basket. Applied on confirm, dropped
    # on deny or when the seller moves on.
    pending_destructive: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.listings:
            self.listings.append(Listing())
        if not self.active_listing_id:
            self.active_listing_id = self.listings[0].listing_id

    @property
    def active(self) -> Listing:
        return next(li for li in self.listings if li.listing_id == self.active_listing_id)

    def new_listing(self) -> Listing:
        """Start a fresh listing and make it active (seller is listing another property)."""
        li = Listing(history_start=len(self.history))
        self.listings.append(li)
        self.active_listing_id = li.listing_id
        return li

    # --- backward-compat proxies to the active listing (engine/tests use these) ---
    @property
    def prop(self) -> Property:
        return self.active.prop

    @property
    def listing_id(self) -> str:
        return self.active.listing_id

    @property
    def listing_proposed(self) -> bool:
        return self.active.listing_proposed

    @listing_proposed.setter
    def listing_proposed(self, value: bool) -> None:
        self.active.listing_proposed = value

    def messages(self) -> list[Message]:
        # Only the ACTIVE listing's turns — so a new listing doesn't inherit the prior
        # one's address/details (the Zalo thread is shared, but each căn reads clean).
        return [Message(t.role, t.content) for t in self.history[self.active.history_start:]]


@dataclass
class TurnResult:
    reply: str
    assistant_name: str
    captured: list[str]        # field slugs newly captured this turn
    next_field: str | None     # what the AI chose to ask about
    user_type: str             # seller role: chu_nha/luot_song/moi_gioi/san/chu_dau_tu/unknown
    is_listing_post: bool      # True when the reply is a full listing ad, not a chat turn
    completeness: float        # 0..1
    property_score: float      # 0..100
    user_score: float          # 0..100
    tier: str
    escalated: bool = False    # True when this turn was handed to a human admin (oversize media)


class ConversationStore(ABC):
    """Persist a seller's listing/conversation so it survives restarts and is reused
    later (same role as the project store; the listing tier of the Store layer)."""

    @abstractmethod
    def load(self, user_key: str) -> Conversation | None: ...

    @abstractmethod
    def save(self, conv: Conversation) -> None: ...


class SqliteConversationStore(ConversationStore):
    """Persist a seller and their BASKET of listings. Two tables: `conversations`
    (seller-level: role, history, active listing) and `listings` (one row per property,
    keyed by `listing_id`, with `user_key` linking back). A seller has 1..n listings."""

    def __init__(self, path: str = "aioi.db"):
        self._conn = sqlite3.connect(path)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS conversations ("
            "user_key TEXT PRIMARY KEY, user_type TEXT, last_ai_at REAL, "
            "active_listing_id TEXT, history_json TEXT, "
            "price_book_json TEXT NOT NULL DEFAULT '{}')"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS listings ("
            "listing_id TEXT PRIMARY KEY, user_key TEXT NOT NULL, loai_hinh TEXT, "
            "is_rental INTEGER, du_an TEXT, listing_proposed INTEGER, "
            "history_start INTEGER, status TEXT, fields_json TEXT, "
            "media_json TEXT NOT NULL DEFAULT '[]')"
        )
        for table, col, default in (("listings", "media_json", "'[]'"),
                                    ("conversations", "price_book_json", "'{}'")):
            try:  # migrate older tables that predate this column
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {col} TEXT NOT NULL DEFAULT {default}")
            except sqlite3.OperationalError:
                pass  # column already exists
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_listings_user ON listings(user_key)")
        self._conn.commit()

    def save(self, conv: Conversation) -> None:
        self._conn.execute(
            "INSERT INTO conversations(user_key, user_type, last_ai_at, active_listing_id, "
            "history_json, price_book_json) VALUES (?,?,?,?,?,?) ON CONFLICT(user_key) DO UPDATE SET "
            "user_type=excluded.user_type, last_ai_at=excluded.last_ai_at, "
            "active_listing_id=excluded.active_listing_id, history_json=excluded.history_json, "
            "price_book_json=excluded.price_book_json",
            (
                conv.user_key,
                conv.user_type.value,
                conv.last_ai_at,
                conv.active_listing_id,
                json.dumps([t.__dict__ for t in conv.history], ensure_ascii=False),
                json.dumps(conv.price_book, ensure_ascii=False),  # the seller's OWN uploaded prices
            ),
        )
        for li in conv.listings:
            self._conn.execute(
                "INSERT INTO listings(listing_id, user_key, loai_hinh, is_rental, du_an, "
                "listing_proposed, history_start, status, fields_json, media_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(listing_id) DO UPDATE SET loai_hinh=excluded.loai_hinh, "
                "is_rental=excluded.is_rental, du_an=excluded.du_an, "
                "listing_proposed=excluded.listing_proposed, history_start=excluded.history_start, "
                "status=excluded.status, fields_json=excluded.fields_json, "
                "media_json=excluded.media_json",
                (
                    li.listing_id,
                    conv.user_key,
                    li.prop.loai_hinh,
                    int(li.prop.is_rental),
                    li.prop.fields.get("du_an"),  # denormalized for query/inheritance
                    int(li.listing_proposed),
                    li.history_start,
                    li.status,
                    json.dumps(li.prop.fields, ensure_ascii=False),
                    json.dumps(li.media, ensure_ascii=False),
                ),
            )
        # Reconcile: drop rows for listings the seller REMOVED from the basket (e.g. "chỉ
        # giữ căn 23, 24") — otherwise a pruned căn would linger in the DB after restart.
        keep_ids = [li.listing_id for li in conv.listings]
        placeholders = ",".join("?" * len(keep_ids)) or "''"
        self._conn.execute(
            f"DELETE FROM listings WHERE user_key=? AND listing_id NOT IN ({placeholders})",
            (conv.user_key, *keep_ids),
        )
        self._conn.commit()

    def load(self, user_key: str) -> Conversation | None:
        row = self._conn.execute(
            "SELECT user_type, last_ai_at, active_listing_id, history_json, price_book_json "
            "FROM conversations WHERE user_key=?",
            (user_key,),
        ).fetchone()
        if row is None:
            return None
        lrows = self._conn.execute(
            "SELECT listing_id, loai_hinh, is_rental, listing_proposed, history_start, "
            "status, fields_json, media_json FROM listings WHERE user_key=?",
            (user_key,),
        ).fetchall()
        listings = [
            Listing(
                listing_id=r[0],
                prop=Property(loai_hinh=r[1], is_rental=bool(r[2]), fields=json.loads(r[6])),
                listing_proposed=bool(r[3]),
                history_start=r[4] or 0,
                status=r[5] or "đang bán",
                media=json.loads(r[7]) if r[7] else [],
            )
            for r in lrows
        ]
        history = [Turn(**d) for d in json.loads(row[3])]
        return Conversation(
            user_key=user_key,
            assistant=assign_assistant(user_key),  # deterministic — recompute, don't store
            history=history,
            last_ai_at=row[1],
            user_type=SellerRole(row[0]),
            listings=listings,
            active_listing_id=row[2],
            price_book=json.loads(row[4]) if row[4] else {},  # the seller's uploaded prices survive restart
        )


class Engine:
    def __init__(
        self,
        provider: LLMProvider,
        fetcher: Callable[[str], FetchedMedia] = fetch_media,
        resolver: Callable[[str], list[tuple[str, str | None]]] | None = None,
        folder_namer: Callable[[str], str | None] | None = None,
        project_store: ProjectStore | None = None,
        conversation_store: ConversationStore | None = None,
        media_store: MediaStore | None = None,
        max_photos_per_listing: int = MAX_PHOTOS_PER_LISTING,
    ):
        self._provider = provider
        self._fetcher = fetcher  # injectable so tests/offline don't hit the network
        self._max_photos = max_photos_per_listing  # cap photos KEPT per listing (storage cost)
        # url -> [(download_url, name|None)]; expands Google Drive folders/files when a
        # GOOGLE_API_KEY is configured. Default is a pass-through (one url, no name).
        self._resolve = resolver or (lambda url: [(url, None)])
        # url -> the ENCLOSING folder's own name (a hint for grouping a multi-file batch).
        self._folder_name = folder_namer or (lambda url: None)
        # SharePoint (kho gốc) — stores the seller's raw files; None = files aren't kept
        # (only their vision read-out is), the behavior before this store existed.
        self._media_store = media_store
        self._projects = project_store or InMemoryProjectStore()
        # Optional durable store for listings; the in-memory dict is always the cache.
        self._convstore = conversation_store
        self._conversations: dict[str, Conversation] = {}
        # Transient: when a NEW project is created from a name that looks like a near-miss
        # (mishearing) of an existing one, register_project stashes the clarifying question here;
        # the ingest reply drains it into conv.pending_name_note so AOND ASKS ("nghi ngờ thì hỏi")
        # instead of silently forking a duplicate project/folder.
        self._pending_name_concern = ""

    # --- projects (condos/developments) ---------------------------------

    def register_project(
        self,
        name: str,
        amenities: list[str] | None = None,
        attributes: dict[str, str] | None = None,
        voter: str = "?",
    ) -> Project:
        """Register/ENRICH a project's shared facts. The project registry is GLOBAL —
        shared across ALL sellers — so info one seller gives about a project is reused when
        another seller lists a different unit in it. MERGES into any existing project
        (unions amenities, fills/updates attributes, keeps unit_types) so nothing a prior
        seller provided is lost."""
        name = self._canonical_du_an(name)  # a variant/model name enriches the real project
        project = self._projects.get(name)
        if project is None:  # about to CREATE a new project — is it a near-miss of an existing one?
            concern = self.project_name_concern(name)
            if concern is not None and concern[0] == "near":
                self._pending_name_concern = concern[2]  # surfaced by the ingest reply → AOND asks
            project = Project(name=name)
        for a in amenities or []:
            if a not in project.amenities:
                project.amenities.append(a)
        merge_facts(project.attributes, project.votes, attributes or {}, voter)
        self._projects.save(project)
        return project

    def register_project_from_text(
        self, text: str, project_name: str | None = None, voter: str = "?"
    ) -> Project | None:
        """Extract a project's shared facts from free text (e.g. a lister's description).
        Pass `project_name` to attach the facts to a KNOWN project even when the text itself
        doesn't re-name it — used to record a seller's answer to a verify question as a vote.
        `voter` = the seller's id, so each seller counts once."""
        data = self._provider.extract(
            PROJECT_EXTRACTION_SYSTEM, [Message("user", text)], project_extraction_schema()
        )
        name = project_name or (data.get("ten_du_an") or "").strip()
        if not name:
            return None
        attrs: dict[str, str] = {}
        for key, label in (("dia_chi", "vị trí"), ("cong", "cổng"), ("an_ninh", "an ninh")):
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                attrs[label] = val.strip()
        amenities = [a for a in (data.get("tien_ich") or []) if isinstance(a, str)]
        return self.register_project(name, amenities=amenities, attributes=attrs, voter=voter)

    def project_for(self, prop: Property) -> Project | None:
        """The registered project a listing belongs to (by its captured `du_an`), if any."""
        return self._projects.get(prop.fields.get("du_an"))

    def project_name_concern(self, name: str) -> tuple[str, str, str] | None:
        """The "nghi ngờ thì hỏi" guard. When a project name read from a doc/seller is AMBIGUOUS,
        return (kind, canonical_project_name, question) so the caller can ASK instead of silently
        spawning a duplicate/mis-modelled project. Returns None when `name` confidently matches an
        existing project (the improved matcher already collapsed the variant) or is clearly new.

          kind="model" → `name` looks like a MẪU NHÀ (unit_type) of an existing project; a model
                         name is never a project. canonical = that project's name.
          kind="near"  → `name` shares its distinctive tail with an existing project (maybe the
                         same place under a different lead — a developer name, a mishearing…).
                         canonical = the suspected existing project's name.
        Sub-names can clash across projects, so this only SUSPECTS — the seller confirms."""
        name = (name or "").strip()
        if not name or self._projects.get(name) is not None:
            return None  # empty, or a confident match → not ambiguous
        # (a) does it look like a MODEL of an existing project? (Cosmo, Fusion Gen 5…)
        for p in self._projects.all():
            if p.find_unit_type(name) is not None:
                return ("model", p.name,
                        f"‘{name}’ nghe giống MẪU NHÀ trong dự án “{p.name}” — đây là mẫu nhà "
                        f"của “{p.name}”, hay là một dự án riêng ạ? Em hỏi cho chắc kẻo nhầm.")
        # (b) near-miss to an existing project — a likely mishearing/typo. Two shapes, both worth
        #     a confirm: the difference is at the FRONT (same distinctive tail tokens, e.g. "Nhã
        #     Đạt Phú Định" vs "…Phú Định") OR at the TAIL (long shared prefix, e.g. "Nyah Phú
        #     BÌNH" vs "Nyah Phú ĐỊNH"). Character-exact after cleaning — not blind fuzzy.
        q_tokens = normalize_project_name(name).split()
        qs = "".join(q_tokens)
        for p in self._projects.all():
            p_tokens = normalize_project_name(p.name).split()
            ps = "".join(p_tokens)
            if not qs or not ps or qs == ps or q_tokens == p_tokens:
                continue
            shared_tail = q_tokens[-2:] == p_tokens[-2:]
            prefix = 0
            for a, b in zip(qs, ps):
                if a != b:
                    break
                prefix += 1
            shared_prefix = prefix >= 0.6 * min(len(qs), len(ps))
            if shared_tail or shared_prefix:
                return ("near", p.name,
                        f"Dự án “{name}” có phải cũng là “{p.name}” mình đang có không, hay là "
                        "một dự án khác ạ? Em hỏi cho chắc để khỏi tách nhầm thành 2 dự án.")
        return None

    def _canonical_du_an(self, name: str | None) -> str:
        """Map a raw project name (as heard/read) to the CANONICAL registered project name BEFORE
        it is used to build any SharePoint path — so every spelling/apostrophe/prefix variant, and
        a MẪU NHÀ name used as a project ("Cosmo"), all land in ONE folder + one project record
        instead of spawning duplicates. This is the SOURCE fix that stops the folder mess. Falls
        back to the raw name for a genuinely new project (nothing registered matches)."""
        if not name:
            return name or ""
        p = self._projects.get(name)          # collapses apostrophe/spacing/prefix variants
        if p is not None:
            return p.name
        concern = self.project_name_concern(name)
        if concern is not None and concern[0] == "model":  # a model name → its project's folder
            return concern[1]
        return name

    def _drain_name_concern(self) -> str:
        """Return and clear the pending near-miss clarifying question (set by register_project when
        a new project name looked like a mishearing of an existing one). Empty when there's none."""
        q, self._pending_name_concern = self._pending_name_concern, ""
        return q

    def register_unit_type(
        self, project_name: str, name: str, specs: dict[str, str], voter: str = "?"
    ) -> UnitType | None:
        """Attach a house model / unit type to its project (creating the project if new)."""
        if not project_name or not name:
            return None
        project_name = self._canonical_du_an(project_name)  # attach to the real project, not a variant
        project = self._projects.get(project_name) or Project(name=project_name)
        unit_type = project.find_unit_type(name)
        if unit_type is None:
            unit_type = UnitType(name=name)
            project.unit_types.append(unit_type)
        merge_facts(unit_type.specs, unit_type.votes, {k: v for k, v in specs.items() if v}, voter)
        self._projects.save(project)
        return unit_type

    def register_unit_type_from_text(
        self, text: str, project_name: str | None = None, name: str | None = None, voter: str = "?"
    ) -> UnitType | None:
        """Extract a house model's specs from free text (a brochure read-out OR a seller's
        chat answer). Pass `project_name`/`name` to attach the specs to a KNOWN model even when
        the text doesn't re-name it — how the bot LEARNS a model from a chat answer, not just
        a file. (An empty model is harmless — acquisition still asks and learning still retries
        while it has no facts.)"""
        data = self._provider.extract(
            UNIT_TYPE_EXTRACTION_SYSTEM, [Message("user", text)], unit_type_extraction_schema()
        )
        name = name or (data.get("ten_mau_nha") or "").strip()
        project_name = project_name or (data.get("ten_du_an") or "").strip()
        if not name or not project_name:
            return None
        labels = {"dien_tich": "diện tích", "so_phong_ngu": "số phòng ngủ", "so_tang": "số tầng",
                  "mat_tien": "mặt tiền", "huong": "hướng", "layout": "bố trí"}
        specs = {label: data[key].strip() for key, label in labels.items()
                 if isinstance(data.get(key), str) and data[key].strip()}
        return self.register_unit_type(project_name, name, specs, voter=voter)

    def ingest_source_doc(
        self, url: str, project_name: str, kind: str = "tài liệu", title: str | None = None,
        assistant_name: str = "Đ•ai",
    ) -> dict[str, str]:
        """Read a source (phân lô, brochure mẫu nhà, bảng mô tả gói nội thất...) ONCE, transcribe
        it FAITHFULLY to Markdown, store the .md on the PROJECT (persisted + uploaded to
        SharePoint `<Dự án>/_Chung/`), and return the doc record. The bot re-reads this cheap
        text later to advise with grounding — no re-OCR, no fuzzy recall. See [[aioi-data-model]]."""
        # A brochure/phân lô can be hi-res and large; allow a bigger fetch (an image is
        # downscaled in describe_media before it hits the vision API anyway).
        data, mt = fetch_raw(url, max_bytes=30 * 1024 * 1024)
        media_type = sniff_media_type(data) or (mt if mt in ("application/pdf",) else "application/pdf")
        return self._ingest_doc_bytes(data, media_type, project_name, kind, title, assistant_name)

    def _ingest_doc_bytes(
        self, data: bytes, media_type: str, project_name: str, kind: str = "tài liệu",
        title: str | None = None, assistant_name: str = "Đ•ai",
    ) -> dict[str, str]:
        """Core of ingest_source_doc, but from ALREADY-FETCHED bytes — so a brochure the seller
        sends inline (not just an admin-pasted link) is ALSO transcribed once to a faithful .md
        and stored on the project, ready for cheap grounded Q&A. See [[aioi-data-model]]."""
        markdown = self._provider.describe_media(
            [(data, media_type)], build_transcribe_instruction(assistant_name, kind),
            max_tokens=8192,  # full multi-page transcription needs headroom (don't truncate)
        ).strip()
        return self._save_project_doc(project_name, kind, title or kind, markdown)

    def _save_project_doc(self, project_name: str, kind: str, title: str, markdown: str) -> dict[str, str]:
        """Store a grounded Markdown doc on a project: upload the .md to SharePoint `_Chung`
        (best-effort) and append it to `Project.docs` (persisted), so the bot can re-read it
        later for grounded answers. Re-saving the same title REPLACES the old copy."""
        project_name = self._canonical_du_an(project_name)  # one folder per project, not per spelling
        # SHARED knowledge must never carry personal identity — strip it (and neutralise a legal-cert
        # title that embeds the owner's name) before storing anywhere AOND can relay it.
        title = _redact_doc_title(title)
        markdown = _redact_personal_data(markdown)
        web_url = item_id = ""
        if self._media_store is not None:
            try:
                ref = self._media_store.upload(
                    f"{project_name}/_Chung", f"{sanitize_filename(title)}.md",
                    markdown.encode("utf-8"), "text/markdown")
                web_url, item_id = ref.web_url, ref.item_id
            except SharePointError:
                pass  # DB copy still kept; SharePoint is best-effort
        doc = {"kind": kind, "title": title, "markdown": markdown,
               "web_url": web_url, "item_id": item_id, "fetched_at": str(int(time.time()))}
        project = self._projects.get(project_name) or Project(name=project_name)
        project.docs = [d for d in project.docs if d.get("title") != title]  # re-ingest replaces
        project.docs.append(doc)
        self._projects.save(project)
        return doc

    def unit_type_for(self, prop: Property) -> UnitType | None:
        """The registered house model a listing is (by `unit_type` within its project)."""
        project = self.project_for(prop)
        if project is None:
            return None
        return project.find_unit_type(prop.fields.get("unit_type"))

    def conversation(self, user_key: str) -> Conversation:
        conv = self._conversations.get(user_key)
        if conv is None and self._convstore is not None:
            conv = self._convstore.load(user_key)  # restore a prior listing after restart
            if conv is not None:
                self._conversations[user_key] = conv
                self._dedup_basket(conv)  # SELF-HEAL: collapse duplicate mã căn from an old DB
        if conv is None:
            conv = Conversation(user_key=user_key, assistant=assign_assistant(user_key))
            self._conversations[user_key] = conv
        return conv

    def greeting(self, user_key: str) -> str:
        """Opening message for a brand-new seller: introduces the assistant and states this
        channel serves SELLERS only (buyers → 'AI Ơi Nhà Đất - Buyers'). Uses the same stable
        persona the conversation will use. The channel (CLI / Zalo webhook) decides WHEN to send
        it — typically on first contact, when the seller has no history yet."""
        return build_greeting(self.conversation(user_key).assistant.name)

    def is_new_conversation(self, user_key: str) -> bool:
        """True if this user has never exchanged a turn — the channel uses it to know whether to
        lead with `greeting()` (so a returning seller isn't greeted from scratch every restart)."""
        return not self.conversation(user_key).history

    def _dedup_basket(self, conv: Conversation) -> None:
        """Collapse listings that share a (dự án, mã số căn) — a seller can't list the same
        căn twice. Keeps the FIRST of each and merges the others' non-empty fields + media into
        it. Heals baskets persisted BEFORE the unique-code invariant existed. Re-saves if it
        changed anything."""
        seen: dict[str, Listing] = {}
        survivors: list[Listing] = []
        changed = False
        for li in conv.listings:
            key = normalize_project_name(li.prop.fields.get("ma_so_can") or "") or None
            if key is not None and key in seen:  # same mã số căn → duplicate căn → merge
                keep = seen[key]
                for k, v in li.prop.fields.items():
                    if not v:
                        continue
                    if not keep.prop.fields.get(k):
                        keep.prop.fields[k] = v
                    elif k == "du_an" and keep.prop.fields[k] != v:
                        # conflicting du_an (a mẫu-nhà name may have leaked in) → keep the one
                        # that is a REGISTERED project; a model name isn't a project.
                        if self._projects.get(v) is not None and self._projects.get(keep.prop.fields[k]) is None:
                            keep.prop.fields[k] = v
                have = {m.get("item_id") for m in keep.media}
                keep.media += [m for m in li.media if m.get("item_id") not in have]
                changed = True
                continue
            if key is not None:
                seen[key] = li
            survivors.append(li)
        if not changed:
            return
        conv.listings = survivors or [Listing()]
        if all(li.listing_id != conv.active_listing_id for li in conv.listings):
            conv.active_listing_id = conv.listings[-1].listing_id
        if self._convstore is not None:
            self._convstore.save(conv)  # reconciles the DB (deletes the dropped duplicate rows)

    def start_new_listing(self, user_key: str) -> Listing:
        """Seller starts another property — add a fresh active listing to their basket."""
        conv = self.conversation(user_key)
        listing = conv.new_listing()
        if self._convstore is not None:
            self._convstore.save(conv)
        return listing

    @staticmethod
    def _listing_by_code(conv: Conversation, code: str | None, du_an: str | None = None) -> Listing | None:
        """An existing basket listing with this mã số căn (normalized-equal). Within ONE
        seller's basket the mã số căn is the unique key — a seller can't list the same căn
        twice — so we match by CODE ALONE (a drifting/mistyped du_an, e.g. a mẫu nhà name
        landing in the du_an field, must not spawn a duplicate)."""
        key = normalize_project_name(code or "")
        if not key:
            return None
        for li in conv.listings:
            lc = li.prop.fields.get("ma_so_can")
            if lc and normalize_project_name(lc) == key:
                return li
        return None

    def import_listings(self, user_key: str, rows: list[dict[str, str]]) -> int:
        """Bulk-create listings from parsed spreadsheet rows (each row = one property). A row
        whose mã số căn already exists in the basket UPDATES that căn instead of duplicating.
        Returns how many NEW listings were added. The fastest way to grow the rổ hàng."""
        conv = self.conversation(user_key)
        added = 0
        for row in rows:
            if not row:
                continue
            existing = self._listing_by_code(conv, row.get("ma_so_can"), row.get("du_an"))
            if existing is not None:
                existing.prop.update(row)  # idempotent — re-sending the same list won't dupe
                continue
            # Fill the empty starter listing first; a fresh one for each subsequent row.
            target = conv.active if self._listing_empty(conv.active) else conv.new_listing()
            target.prop.update(row)
            added += 1
        if self._convstore is not None and rows:
            self._convstore.save(conv)
        return added

    @staticmethod
    def _listing_empty(li: Listing) -> bool:
        return li.prop.loai_hinh is None and not li.prop.fields

    def import_price_list(
        self, user_key: str, media: list[FetchedMedia], caption: str | None = None,
    ) -> tuple[int, int, str, int, bool] | None:
        """Read a PRICE-LIST / inventory PDF (or image). The WHOLE list is kept as a grounded
        reference .md on the project; but only the căn the SELLER actually rao go into their
        basket — `caption` (their message sent with the file, e.g. "tôi bán 2 căn Cosmo Gen 2
        trong này") scopes the import. Registers the project + models. Returns
        (n_new, n_imported, du_an, n_in_list) if it WAS a price list, else None. Re-sending the
        same list UPDATES existing căn (n_new < n_imported), never duplicates."""
        data = self._provider.extract_media(
            [(m.data, m.media_type) for m in media],
            build_inventory_instruction(self.conversation(user_key).assistant.name),
            inventory_schema(),
        )
        if not data.get("la_bang_gia"):
            return None
        du_an = (data.get("ten_du_an") or "").strip()
        rows: list[dict[str, str]] = []
        for r in data.get("cac_can") or []:
            code = (r.get("ma_so_can") or "").strip() if isinstance(r, dict) else ""
            if not code:
                continue
            row = {"ma_so_can": code}
            if du_an:
                row["du_an"] = du_an
            if (mn := (r.get("mau_nha") or "").strip()):
                row["unit_type"] = mn
            if (dt := (r.get("dien_tich") or "").strip()):
                row["dien_tich"] = dt
            if (qc := (r.get("quy_cach_ban_giao") or "").strip()):  # SHARED handover spec (public)
                row["quy_cach_ban_giao"] = qc
            # Keep EVERY price the căn has — a căn may carry several package prices (Air / Max /
            # chưa NT). `gia` = primary for labels, `gia_goi` = full breakdown, plus one key per
            # package (gia_air, gia_max...) so nothing is dropped and each is queryable.
            primary, breakdown, per_package = self._unit_prices(r)
            if primary:
                row["gia"] = primary
            if breakdown and breakdown != primary:
                row["gia_goi"] = breakdown
            for k, v in per_package.items():
                if k != "gia":
                    row[k] = v
            rows.append(row)
        if not rows:
            return None
        n_in_list = len(rows)
        # "Nghi ngờ thì hỏi": canonicalize the project name FIRST so a variant/MẪU-NHÀ name lands
        # on the real project (one folder), then register — register_project stashes a clarifying
        # question in self._pending_name_concern when the name is a near-miss (mishearing) of an
        # existing project, which we drain into the reply below so AOND asks instead of forking.
        conv = self.conversation(user_key)
        conv.pending_name_note = ""
        if du_an:
            du_an = self._canonical_du_an(du_an)
            for r in rows:
                if r.get("du_an"):
                    r["du_an"] = du_an
        # Share the project + models (from the FULL list) so other sellers inherit them, and keep
        # a FAITHFUL grounded .md of the WHOLE price list (mã lô ↔ mẫu nhà ↔ giá) so the bot can
        # re-read it later ("2 căn Cosmo Gen 2 là căn nào?"). Built from the rows — no extra call.
        if du_an:
            self.register_project(du_an, voter=user_key)
            for ut_name in {r["unit_type"] for r in rows if r.get("unit_type")}:
                self.register_unit_type(du_an, ut_name, {}, voter=user_key)
            self._save_project_doc(du_an, kind="danh mục", title=f"danh mục {du_an}",
                                   markdown=self._catalog_markdown(du_an, rows))
            self._seed_inventory(du_an, rows, voter=user_key)
        conv.pending_name_note = self._drain_name_concern()  # near-miss → ask (empty if none)
        # The prices are the SELLER'S OWN reference — keep them (keyed by mã lô) so we can attach
        # a price when they later rao a căn. Sending a price list ≠ rao bán.
        for r in rows:
            conv.price_book[normalize_project_name(r["ma_so_can"])] = r
        # Attach the seller's price to any căn ALREADY in their rổ (they rao'd it before sending
        # the price list) — so those listings now carry the seller's own price.
        for li in conv.listings:
            pb = conv.price_book.get(normalize_project_name(li.prop.fields.get("ma_so_can") or ""))
            if pb:
                li.prop.update({k: v for k, v in pb.items() if k.startswith("gia") and v})
        # Populate the BASKET only with căn the seller EXPLICITLY says to rao (never auto-rao the
        # whole list just because a file was sent). No rao intent → nothing enters the rổ.
        selling, uncertain = self._inventory_scope(user_key, caption, rows)
        return self.import_listings(user_key, selling), len(selling), du_an, n_in_list, uncertain

    def _inventory_scope(
        self, user_key: str, caption: str | None, rows: list[dict[str, str]]
    ) -> tuple[list[dict[str, str]], bool]:
        """Which căn from the price list is the seller actually rao? A bảng giá is the WHOLE
        project's inventory. Returns (rows_to_rao, uncertain). GỬI FILE ≠ RAO: the basket is
        populated ONLY when the caption expresses an explicit RAO/BÁN intent for specific căn —
        no caption, or a lookup/question ("xem diện tích các căn trong đó"), imports NOTHING (the
        price list is still kept as reference). `uncertain` True when a rao intent is ambiguous."""
        if not caption or not caption.strip():
            return [], False   # a file with no words → not a rao intent → nothing into the rổ
        summary = "\n".join(
            f"- lô {r['ma_so_can']}: mẫu {r.get('unit_type', '?')}, giá {r.get('gia', '?')}"
            for r in rows)
        data = self._provider.extract(
            INVENTORY_SCOPE_SYSTEM,
            [Message("user", f'Người bán nhắn: "{caption.strip()}"\n\nBảng giá gồm:\n{summary}')],
            inventory_scope_schema(),
        )
        uncertain = bool(data.get("khong_chac"))
        if not data.get("co_y_rao"):   # sent as reference / a question, not to rao
            return [], uncertain
        if data.get("ban_tat_ca"):
            return rows, uncertain
        wanted = {normalize_project_name(str(c)) for c in (data.get("cac_ma_lo") or []) if c}
        keep = [r for r in rows if normalize_project_name(r["ma_so_can"]) in wanted]
        return keep, uncertain   # rao intent but no căn resolved → empty (ask, don't guess-all)

    @staticmethod
    def _unit_prices(can: dict) -> tuple[str, str, dict[str, str]]:
        """From an extracted price-list căn that may carry MULTIPLE package prices
        (`gia_goi`=[{ten_goi, gia}]) plus/or a single `gia`, return
        (primary, breakdown_display, per_package_keys) — nothing dropped. per_package keys are
        `gia_<slug>` (gia_air, gia_max, gia_chua_nt...) so each price is stored and queryable."""
        goi = [((str(g.get("ten_goi") or "").strip()), (str(g.get("gia") or "").strip()))
               for g in (can.get("gia_goi") or [])
               if isinstance(g, dict) and str(g.get("gia") or "").strip()]
        single = str(can.get("gia") or "").strip()
        per: dict[str, str] = {}
        for name, price in goi:
            slug = re.sub(r"\s+", "_", normalize_project_name(name)) if name else ""
            per["gia_" + slug if slug else "gia"] = price
        if single and "gia" not in per:
            per["gia"] = single
        breakdown = " · ".join(f"{name or 'giá'}: {price}" for name, price in goi) if goi else single
        primary = single or (goi[0][1] if goi else "")
        return primary, breakdown, per

    def _seed_inventory(self, du_an: str, rows: list[dict[str, str]], voter: str) -> None:
        """Populate the PROJECT's inventory (`Project.units`) from a price list — one unit per
        mã lô with its known attributes (mẫu nhà, giá, diện tích). This is the queryable
        substrate `aioi.inventory` reads; it GROWS later as sellers tell us more (vi_tri, huong,
        view...). Distinct from the seller's basket (what THEY rao)."""
        project = self._projects.get(du_an)
        if project is None:
            return
        for r in rows:
            code = r.get("ma_so_can")
            if not code:
                continue
            # SHARED knowledge is the PHYSICAL/public reality — the catalog: which căn exist,
            # their mẫu nhà, diện tích. GIÁ is the SELLER'S OWN offer (each seller quotes their
            # own) → it does NOT belong in the shared inventory; it lives on the seller's basket.
            attrs: dict[str, str] = {}
            if r.get("unit_type"):
                attrs["mau_nha"] = r["unit_type"]
            if r.get("dien_tich"):
                attrs["dien_tich"] = r["dien_tich"]
            if r.get("quy_cach_ban_giao"):   # handover spec (thô/mặt tiền/nội thất Air-Max) — SHARED
                attrs["quy_cach_ban_giao"] = r["quy_cach_ban_giao"]
            project.upsert_unit(code, attrs, voter=voter)
        self._projects.save(project)

    @staticmethod
    def _catalog_markdown(du_an: str, rows: list[dict[str, str]]) -> str:
        """A faithful Markdown CATALOG (shared knowledge) — which mã lô exists, its mẫu nhà,
        diện tích, and its QUY CÁCH BÀN GIAO (xây thô + hoàn thiện mặt tiền; và có hoàn thiện nội
        thất / chọn gói Air-Max hay không). NO price: giá (kể cả giá từng gói) is each seller's
        OWN offer (kept on their basket), not shared."""
        lines = [f"# Danh mục căn — {du_an}", "",
                 "*(Kiến thức chung: mã lô ↔ mẫu nhà ↔ diện tích ↔ quy cách bàn giao. GIÁ (kể cả "
                 "giá từng gói) là của riêng từng người rao, không lưu ở đây.)*", "",
                 "| Mã lô/căn | Mẫu nhà | Diện tích | Quy cách bàn giao |", "|---|---|---|---|"]
        for r in rows:
            lines.append(f"| {r.get('ma_so_can','')} | {r.get('unit_type','')} | "
                         f"{r.get('dien_tich','')} | {r.get('quy_cach_ban_giao','')} |")
        return "\n".join(lines)

    def _map_columns(self, headers: list[str]) -> dict[str, str]:
        """{header → field slug}: deterministic aliases first, LLM for the rest (arbitrary sheets)."""
        mapping = bulk.auto_header_map(headers)
        unmapped = [h for h in headers if h not in mapping]
        if unmapped:
            data = self._provider.extract(
                build_column_map_system(unmapped),
                [Message("user", ", ".join(unmapped))],
                column_map_schema(),
            )
            used = set(mapping.values())
            for slug, header in data.items():
                if isinstance(header, str) and header.strip() in unmapped and slug not in used:
                    mapping[header.strip()] = slug
                    used.add(slug)
        return mapping

    def import_csv(self, user_key: str, text: str) -> int:
        """Bulk-import a CSV (deterministic + LLM-assisted column mapping for odd headers)."""
        rows = bulk.rows_from_mapping(text, self._map_columns(bulk.csv_headers(text)))
        return self.import_listings(user_key, rows)

    def import_from_url(self, user_key: str, url: str) -> int:
        """Import listings from a Google Sheets link, or a Drive/other link to a CSV/Excel file."""
        return self.import_csv(user_key, self._spreadsheet_text(url))

    def _spreadsheet_text(self, url: str) -> str:
        """Resolve a link to CSV text: Google Sheets → CSV export; Drive/other → fetch the
        file and decode (Excel via openpyxl, detected by the .xlsx zip magic 'PK')."""
        sheet = bulk.sheets_export_url(url)
        if sheet is not None:
            data, _ = fetch_raw(sheet)
            return data.decode("utf-8", "replace")
        data, _ = fetch_raw(url)
        if data[:2] == b"PK":  # .xlsx is a zip
            return bulk.xlsx_to_csv(data)
        return data.decode("utf-8-sig", "replace")  # CSV / text

    def switch_listing(self, user_key: str, index: int) -> Listing | None:
        """Make listing #index (1-based) active — the seller picked which căn they mean."""
        conv = self.conversation(user_key)
        if not (1 <= index <= len(conv.listings)):
            return None
        conv.active_listing_id = conv.listings[index - 1].listing_id
        if self._convstore is not None:
            self._convstore.save(conv)
        return conv.active

    def set_status(self, user_key: str, status: str) -> Listing:
        """Update the ACTIVE listing's status (đã bán / dừng bán / đã cho thuê...)."""
        conv = self.conversation(user_key)
        conv.active.status = status
        if self._convstore is not None:
            self._convstore.save(conv)
        return conv.active

    def _basket_block(self, conv: Conversation) -> str:
        """Make the bot aware of the WHOLE basket so it can disambiguate ("căn nào?")
        when the seller acts on 'a' listing. Empty for a single-listing seller."""
        if len(conv.listings) <= 1:
            return ""
        lines = []
        for i, li in enumerate(conv.listings, 1):
            mark = " (đang nói tới)" if li.listing_id == conv.active_listing_id else ""
            lines.append(f"{i}. {li.label()}{mark}")
        return (
            f"\nRỔ HÀNG CỦA KHÁCH — khách đang có {len(conv.listings)} căn:\n"
            + "\n".join(lines)
            + "\nKhi khách báo một hành động lên 'một căn' (đã bán, dừng bán, đổi giá, có khách...) "
            "mà CHƯA RÕ là căn nào trong danh sách trên, hãy HỎI LẠI 'căn nào ạ?' (nhắc địa chỉ "
            "để khách chọn) TRƯỚC khi xử lý. Nếu khách báo đã bán/cho thuê xong thì chúc mừng "
            "và xác nhận cập nhật.\n"
        )

    @staticmethod
    def _structured_shared_block(project: Project | None) -> str:
        """The STRUCTURED shared knowledge that lives on the Project object (NOT in the .md docs):
        project-wide facts (chủ đầu tư, vị trí, tiện ích...) + every MẪU NHÀ's specs (số tầng, số
        phòng, bố trí). Without this, both the reply memory AND the grounded-Q&A path miss the model
        details and AOND wrongly says 'em không tra cứu được mẫu Cosmo Gen 2'. Shared → reused by
        the next seller. Returns markdown sections (no wrapper), or '' if there's nothing."""
        if project is None:
            return ""
        sections: list[str] = []
        shared = project.facts()  # amenities + project-level attributes (chủ đầu tư, vị trí, cổng...)
        if shared:
            sections.append("### Thông tin CHUNG dự án\n" + "\n".join(f"- {l}" for l in shared))
        models = [ut for ut in project.unit_types if ut.specs]
        if models:
            lines = []
            for ut in models:
                facts = "; ".join(ut.facts())
                lines.append(f"- **{ut.name}**: {facts}" if facts else f"- **{ut.name}**")
            sections.append(
                "### MẪU NHÀ (áp dụng cho MỌI căn cùng mẫu — kiến thức chung, dùng lại được)\n"
                + "\n".join(lines))
        return "\n\n".join(sections)

    def _project_memory_block(self, project: Project | None) -> str:
        """The project knowledge the bot ALREADY HAS (price list the seller just shared + the
        pre-loaded phân lô / mẫu nhà docs) — injected every seller turn so AOND never 'forgets'
        the căn it just read and can RESOLVE a reference ('căn Cosmo', 'căn góc', 'căn lớn nhất')
        against real data instead of asking dumb questions or creating a junk listing."""
        if project is None:
            return ""
        sections: list[str] = []
        if (structured := self._structured_shared_block(project)):
            sections.append(structured)
        for d in project.docs:
            sections.append(f'### {d.get("title", "tài liệu")}\n{d.get("markdown", "")}')
        if not sections:
            return ""
        docs = "\n\n".join(sections)
        return (
            f"\n\n=== BỘ NHỚ DỰ ÁN '{project.name}' (em ĐÃ CÓ SẴN — KHÔNG hỏi lại thứ có ở đây) ===\n"
            + docs
            + "\n=== HẾT BỘ NHỚ ===\n"
            "- Khi khách hỏi về MỘT MẪU NHÀ ('mẫu Cosmo Gen 2', 'căn này mẫu gì') — hãy TRẢ LỜI "
            "NGAY từ mục MẪU NHÀ ở trên (số tầng, số phòng, bố trí...). Đây là kiến thức CHUNG em "
            "ĐÃ CÓ, KHÔNG phải 'tra cứu internet' — TUYỆT ĐỐI đừng nói 'em không tra cứu được'.\n"
            "CÁCH DÙNG BỘ NHỚ (bắt buộc):\n"
            "- Khi khách nhắc tới MỘT CĂN — kể cả mơ hồ ('căn Cosmo', 'căn góc', 'căn diện tích "
            "lớn nhất', 'căn view sông') — hãy TRA bảng giá + phân lô ở trên để biết ĐÓ LÀ MÃ LÔ "
            "NÀO, diện tích, địa chỉ, giá bao nhiêu. TUYỆT ĐỐI đừng hỏi lại thông tin đã có ở "
            "trên, đừng tạo căn rỗng/tin rác.\n"
            "- Đặc điểm dự án ('lớn nhất', 'góc', 'mặt tiền đường lớn', 'view'...) phải xét TRÊN "
            "TOÀN BỘ dự án trong phân lô — KHÔNG chỉ vài căn đang có trong rổ. Căn khớp CÓ THỂ là "
            "một căn CHƯA có trong rổ; đó là bình thường (khách đang rao thêm căn mới).\n"
            "- Nếu suy ra được nhưng CHƯA chắc 100% (vd không rõ có phải lô góc), hãy NÊU căn em "
            "nghĩ tới kèm dữ kiện (mã lô, diện tích, mặt tiền) rồi HỎI XÁC NHẬN ('...phải nó không "
            "anh?'). Khi khách XÁC NHẬN, hãy THÊM đúng căn đó vào rổ (nếu chưa có) với mã lô + "
            "diện tích + mẫu + giá từ bộ nhớ, rồi đề xuất rao với mức giá đó.\n"
            "- Nếu bộ nhớ KHÔNG có đủ dữ kiện để xác định (vd phân lô không ghi diện tích/giá của "
            "căn đó), hãy nói THẲNG em chưa có dữ liệu đó và hỏi khách, TUYỆT ĐỐI không bịa số.\n"
        )

    @staticmethod
    def _seller_price_block(conv: Conversation) -> str:
        """The prices the SELLER already gave — injected into the reply so AOND KNOWS it has them
        and answers "giá căn X?" instead of lying "em chưa có giá / không đọc được file". Two
        sources, both the seller's OWN private prices: the uploaded price list (`price_book`) AND
        the căn already in their basket (a listing carries gia/gia_air/gia_max). The basket matters
        because `price_book` for a căn already rao'd may be gone after a restart while the listing's
        prices persist — without this AOND wrongly says it can't read the price file."""
        rows: dict[str, dict] = {}
        for row in conv.price_book.values():
            code = normalize_project_name(row.get("ma_so_can", ""))
            if code:
                rows[code] = dict(row)
        for li in conv.listings:  # basket listings carry the seller's own price too (persisted)
            f = li.prop.fields
            code = normalize_project_name(f.get("ma_so_can", ""))
            if not code or not any(f.get(k) for k in ("gia", "gia_goi", "gia_air", "gia_max")):
                continue
            rows.setdefault(code, {}).update({k: v for k, v in f.items()
                                              if (k.startswith("gia") or k in ("ma_so_can", "unit_type")) and v})
        if not rows:
            return ""
        lines = []
        for row in rows.values():
            code = row.get("ma_so_can", "")
            mau = f" ({row['unit_type']})" if row.get("unit_type") else ""
            gia = row.get("gia_goi") or row.get("gia") or "?"
            lines.append(f"- căn {code}{mau}: {gia}")
        return (
            "\n\n=== GIÁ ANH/CHỊ (người rao) ĐÃ GỬI (từ bảng giá) — em ĐÃ CÓ sẵn ===\n"
            + "\n".join(lines)
            + "\nĐây là GIÁ của CHÍNH người rao đang chat. Khi họ hỏi giá / hỏi 'giá 3 căn', hãy "
            "TRẢ LỜI NGAY bằng các giá trên. TUYỆT ĐỐI KHÔNG nói 'em chưa có giá', 'anh chưa báo "
            "giá', hay 'em không đọc được file' — em CÓ các giá này rồi.\n"
        )

    def handle(self, user_key: str, text: str, now: float | None = None) -> TurnResult:
        now = now if now is not None else time.time()
        conv = self.conversation(user_key)
        conv.history.append(Turn("user", text, now))

        # 0−. PENDING DESTRUCTIVE CONFIRMATION: a clear/bỏ/chỉ-giữ proposed last turn waits here.
        #     Apply it ONLY on an explicit yes; a "thôi/không" cancels; anything else (the seller
        #     moved on) also cancels safely and falls through to normal handling — never delete
        #     without a clear confirm.
        if conv.pending_destructive:
            if _affirms(text):
                _changed, receipts = self._apply_pending_destructive(conv)
                reply = self._receipt_reply(conv, receipts) or "Dạ em đã cập nhật rổ hàng rồi ạ."
                conv.history.append(Turn("assistant", reply, now))
                conv.last_ai_at = now
                return self._build_result(conv, reply, captured=[], now=now)
            conv.pending_destructive = []  # not a clear yes → keep the basket intact
            if _denies(text):
                reply = "Dạ em giữ nguyên rổ hàng, không xóa gì đâu ạ 👍"
                conv.history.append(Turn("assistant", reply, now))
                conv.last_ai_at = now
                return self._build_result(conv, reply, captured=[], now=now)
            # else: fall through and process this message normally (basket left untouched)

        # 0. FACTUAL QUESTION about the project ("diện tích lô 42?", "gói Max gồm gì?") → answer
        #    GROUNDED from the stored source .md, BEFORE any extraction — a question is NOT an
        #    edit to the basket, so it must not run listing extraction (else "lô 42 và 50" would
        #    be misread as adding 2 căn).
        q_project = self.project_for(conv.prop)
        factual_q = _wants_project_facts(text)
        # A seller whose project has an INVENTORY is handled by the agent loop (section 2). For a
        # factual question we do NOT divert to `_answer_from_docs` — that's a historyless, focused
        # call, so a pronoun question ("mẫu nhà NÀY thế nào?", "bạn không tra ra sao?") loses its
        # referent and AOND wrongly says "em không tra cứu được". The agent loop answers it with
        # the conversation history + the structured memory (model specs) + the catalog tools.
        agent_project = q_project is not None and bool(q_project.units)
        if (q_project is not None and not agent_project
                and (q_project.docs or conv.price_book) and factual_q):
            reply = self._answer_from_docs(conv, q_project, text)
            conv.history.append(Turn("assistant", reply, now))
            conv.last_ai_at = now
            return self._build_result(conv, reply, captured=[], now=now)

        # 0b. A request to SEE a drawing / floor plan / render ("bản vẽ căn 24") is a MEDIA
        #     request, not a basket edit — answer with the model's stored drawing if we have it,
        #     else offer to get it. Handled BEFORE extraction so it can never mutate the basket
        #     (a mis-parse once pruned the rổ hàng from "bản vẽ căn 24").
        if _wants_media(text):
            reply = self._media_request_reply(conv, q_project, text)
            conv.history.append(Turn("assistant", reply, now))
            conv.last_ai_at = now
            return self._build_result(conv, reply, captured=[], now=now)

        # 1. Silent planning: turn the seller's message into an ordered list of basket
        #    operations and execute them (returns the changed field names; structural receipts
        #    land on conv.last_receipts). SKIPPED when an agent-loop seller is asking a factual
        #    question — a question is NOT a basket edit, and running operations could misread it
        #    (e.g. "lô 42 và 50?") as adding căn. The agent loop answers it below.
        if agent_project and factual_q:
            captured = []
        else:
            captured = self._extract(conv)

            # 1a-destructive. A DESTRUCTIVE basket op (xóa hết / bỏ căn / chỉ giữ) is confirmed
            #    with a reply built from the REAL post-action state, so the words match the deed —
            #    never "đã xóa hết" while căn remain. Return immediately (no persona reply needed).
            receipt_reply = self._receipt_reply(conv, conv.last_receipts)
            if receipt_reply is not None:
                conv.history.append(Turn("assistant", receipt_reply, now))
                conv.last_ai_at = now
                return self._build_result(conv, receipt_reply, captured, now)

            # 1a. A newly-captured address/project makes a listing's folder name readable — if it
            #     already has files stored under the old (id-only) folder, rename it to match.
            if captured:
                self._sync_media_folder(conv.active)

        # 1b. If this seller's listing is in a project we have UNSETTLED facts about, treat
        #     their message as a possible answer to the open question and cast a vote — this
        #     is how the bot's "let me verify" ask actually resolves the conflict (majority
        #     wins). Gated on contested keys so we don't run an extra extract every turn.
        project = self.project_for(conv.prop)
        if project is not None and project.contested_keys():
            self.register_project_from_text(text, project_name=project.name, voter=user_key)
            project = self.project_for(conv.prop)  # reload: the vote may have shifted/settled

        # 1c. LEARN project / model knowledge from a seller's CHAT ANSWER (not just from a
        #     file) — so info one seller types is reused for the next seller of the same
        #     project/model (the shared registry). Gated: only for a project unit, only on a
        #     descriptive message, and only while the project/model is still missing facts —
        #     so it's not an extra LLM call every turn.
        du_an = conv.prop.fields.get("du_an")
        ut_name = conv.prop.fields.get("unit_type")
        if du_an and _looks_descriptive(text):
            if project is None or not project.facts():
                self.register_project_from_text(text, project_name=du_an, voter=user_key)
            ut = self.unit_type_for(conv.prop)
            if ut_name and (ut is None or not ut.specs):
                self.register_unit_type_from_text(text, project_name=du_an, name=ut_name, voter=user_key)
            project = self.project_for(conv.prop)  # reload for the block below

        # 2. Choose the reply mode (no word cap on the listing stages):
        #    - essentials still missing       -> <=30-word praise + 1 question (collecting)
        #    - "khá đầy đủ", not yet proposed -> draft the ad, ask "hấp dẫn chưa" (no fee)
        #    - draft already shown            -> react to approval & pivot to the fee (§V)
        missing = conv.prop.missing_fields()
        next_field = missing[0] if missing else None
        # Project awareness: if the listing's project is registered, inherit its verified
        # facts (and lightly confirm); if it's named but unknown, prompt the bot to acquire
        # knowledge (ask the seller / request a link); if no project, nothing.
        project_block = build_project_block(
            project,
            conv.prop.fields.get("du_an"),
            self.unit_type_for(conv.prop),
            conv.prop.fields.get("unit_type"),
        )
        is_listing_post = conv.prop.summary_ready()
        if not is_listing_post:
            system = build_chat_system(
                conv.assistant.name, conv.prop, next_field, conv.user_type, project_block
            )
        elif not conv.listing_proposed:
            conv.listing_proposed = True
            system = build_listing_proposal_system(conv.assistant.name, conv.prop, project_block)
        else:
            system = build_fee_followup_system(
                conv.assistant.name, conv.prop, conv.user_type, project_block
            )
        system += self._basket_block(conv)  # basket awareness → disambiguate "căn nào?"
        system += self._project_memory_block(project)  # remember the price list + phân lô; reason over it
        system += self._seller_price_block(conv)  # the seller's OWN prices → answer, don't lie "chưa có"
        # A seller in a project WITH an inventory → the reply runs through the agent LOOP (same
        # sales-mode prompt + tools). The model decides per turn whether to query the catalog,
        # add a resolved căn, learn a fact, or ask for a link — NO keyword gate (Mảnh B). A plain
        # listing (no project inventory) uses the single chat call, unchanged.
        # Length is model-decided from the prompt (short while chatting, full ad when complete) —
        # NOT clamped by a token cap (Vietnamese is token-dense; a tight cap hard-cuts mid-word).
        if project is not None and project.units:
            reply = self._agent_reply(conv, project, system, now)
        else:
            reply = self._provider.chat(system, conv.messages(), max_tokens=REPLY_MAX_TOKENS).strip()

        conv.history.append(Turn("assistant", reply, now))
        conv.last_ai_at = now
        return self._build_result(conv, reply, captured, now)

    @staticmethod
    def _inventory_tools() -> list[dict]:
        """The tools the agent loop can call — read/query/learn the project inventory, and add
        a resolved căn to the seller's basket. Attribute keys are FREE-FORM (works for houses
        and condos alike). The model reasons over these; the engine executes + verifies each."""
        return [
            {"name": "tra_can",
             "description": "Tra những gì ĐÃ BIẾT về MỘT căn theo mã số căn (mẫu nhà, giá, diện "
                            "tích, vị trí...). Trả 'chưa có' nếu dự án không có mã căn đó.",
             "input_schema": {"type": "object",
                              "properties": {"ma_so_can": {"type": "string"}},
                              "required": ["ma_so_can"]}},
            {"name": "tim_can",
             "description": "Tìm (các) căn theo tiêu chí. `where` = lọc theo thuộc tính bất kỳ "
                            "(vd {\"mau_nha\":\"Cosmo Gen 2\"} hoặc {\"loai_can\":\"2PN\",\"view\":\"hồ\"}). "
                            "`lon_nhat`/`nho_nhat` = tên thuộc tính SỐ để lấy căn lớn/nhỏ nhất "
                            "(vd dien_tich_dat, dien_tich, tang, gia). Trả rỗng nếu chưa đủ dữ liệu "
                            "— KHÔNG đoán.",
             "input_schema": {"type": "object", "properties": {
                 "where": {"type": "object", "additionalProperties": {"type": "string"}},
                 "lon_nhat": {"type": "string"}, "nho_nhat": {"type": "string"}}, "required": []}},
            {"name": "tra_mau_nha",
             "description": "Tra thông số CHUNG của một MẪU NHÀ / loại căn (vd 'Cosmo Gen 2', "
                            "'Office 1'): số tầng, số phòng ngủ, mặt tiền, bố trí... — áp dụng cho "
                            "MỌI căn cùng mẫu. Đây là KIẾN THỨC CHUNG đã lưu (KHÔNG phải tra "
                            "internet). Trả 'chưa có' nếu dự án chưa có mẫu đó.",
             "input_schema": {"type": "object",
                              "properties": {"ten_mau": {"type": "string"}},
                              "required": ["ten_mau"]}},
            {"name": "du_lieu_du_an",
             "description": "Xem em ĐÃ BIẾT gì / CHƯA BIẾT gì về rổ hàng dự án (số căn đã có, "
                            "tổng số căn khách khai, thuộc tính nào đã có). Dùng để biết cần hỏi "
                            "khách bổ sung gì.",
             "input_schema": {"type": "object", "properties": {}, "required": []}},
            {"name": "ghi_nho_can",
             "description": "GHI NHỚ dữ kiện CÔNG CỘNG về một căn (vd căn 26 là lô góc, diện tích "
                            "92m2 — KHÔNG phải giá). Lưu vào KIẾN THỨC CHUNG của dự án, dùng lại cho "
                            "seller sau. `nguon` = nguồn ('khách', 'tài liệu', hoặc link web).",
             "input_schema": {"type": "object", "properties": {
                 "ma_so_can": {"type": "string"},
                 "thuoc_tinh": {"type": "object", "additionalProperties": {"type": "string"}},
                 "nguon": {"type": "string"}},
                 "required": ["ma_so_can", "thuoc_tinh"]}},
            {"name": "ghi_nho_du_an",
             "description": "GHI NHỚ dữ kiện CÔNG CỘNG cấp DỰ ÁN (vd 'dự án có 50 lô', quy hoạch, "
                            "tiện ích) vào kiến thức chung. Dùng cho thông tin công cộng, có NGUỒN "
                            "(khách khai / tài liệu / web). `tong_so_can` nếu biết tổng số căn.",
             "input_schema": {"type": "object", "properties": {
                 "thuoc_tinh": {"type": "object", "additionalProperties": {"type": "string"}},
                 "nguon": {"type": "string"}},
                 "required": ["thuoc_tinh"]}},
            {"name": "them_can_vao_ro",
             "description": "Thêm một căn (đã xác định RÕ mã số căn) vào RỔ HÀNG của khách đang "
                            "chat — tự lấy mẫu nhà/giá/diện tích từ bộ nhớ dự án. Chỉ gọi khi đã "
                            "chắc chắn hoặc khách đã xác nhận đúng căn.",
             "input_schema": {"type": "object",
                              "properties": {"ma_so_can": {"type": "string"}},
                              "required": ["ma_so_can"]}},
        ]

    def _exec_tool(self, conv: Conversation, project: Project, call) -> str:
        """Run ONE tool the model asked for, against the REAL store/basket, and return a short
        result string. The engine is the source of truth — tools return only true data (never
        a guess), so the model's reply can't fabricate."""
        name, inp = call.name, (call.input or {})
        if name == "tra_can":
            r = inv.lookup(project, str(inp.get("ma_so_can", "")))
            if not r:
                return "Dự án chưa có mã căn này."
            # Enrich with the MẪU NHÀ's shared specs (số tầng, số phòng...) so a căn lookup already
            # carries its model's details — the model can describe the căn without a second call.
            ut = project.find_unit_type(r.get("mau_nha") or r.get("unit_type"))
            if ut is not None and ut.specs:
                r = {**r, "mau_nha_chi_tiet": ut.specs}
            return json.dumps(r, ensure_ascii=False)
        if name == "tra_mau_nha":
            ut = project.find_unit_type(str(inp.get("ten_mau", "")))
            if ut is None or not ut.specs:
                return "Dự án chưa có thông số cho mẫu nhà này."
            return json.dumps({"mau_nha": ut.name, **ut.specs}, ensure_ascii=False)
        if name == "tim_can":
            where = inp.get("where") if isinstance(inp.get("where"), dict) else None
            units = inv.find(project, where=where or None,
                             largest=inp.get("lon_nhat") or None, smallest=inp.get("nho_nhat") or None)
            if not units:
                return "Không có căn nào khớp / chưa đủ dữ liệu cho tiêu chí này."
            return "; ".join(json.dumps(u.known(), ensure_ascii=False) for u in units[:12])
        if name == "du_lieu_du_an":
            return json.dumps(inv.coverage(project), ensure_ascii=False)
        if name == "ghi_nho_can":
            attrs = inp.get("thuoc_tinh") if isinstance(inp.get("thuoc_tinh"), dict) else {}
            nguon = str(inp.get("nguon") or "").strip() or conv.user_key   # provenance = voter id
            inv.remember(project, str(inp.get("ma_so_can", "")),
                         {k: str(v) for k, v in attrs.items()}, voter=nguon)
            self._projects.save(project)
            return f"Đã ghi nhớ vào kiến thức chung dự án (nguồn: {nguon})."
        if name == "ghi_nho_du_an":
            attrs = inp.get("thuoc_tinh") if isinstance(inp.get("thuoc_tinh"), dict) else {}
            nguon = str(inp.get("nguon") or "").strip() or conv.user_key
            merge_facts(project.attributes, project.votes,
                        {k: str(v) for k, v in attrs.items() if str(v).strip()}, voter=nguon)
            self._projects.save(project)
            return f"Đã lưu kiến thức chung dự án (nguồn: {nguon})."
        if name == "them_can_vao_ro":
            code = str(inp.get("ma_so_can", "")).strip()
            if not code:
                return "Thiếu mã số căn."
            u = project.find_unit(code)
            row = {"ma_so_can": code, "du_an": project.name}
            if u is not None:  # pull PUBLIC facts from the shared catalog
                for src, dst in (("mau_nha", "unit_type"), ("dien_tich", "dien_tich"),
                                 ("quy_cach_ban_giao", "quy_cach_ban_giao")):
                    if u.attrs.get(src):
                        row[dst] = u.attrs[src]
            pb = conv.price_book.get(normalize_project_name(code))  # the SELLER's own price, if any
            if pb:
                for k, v in pb.items():
                    if k.startswith("gia") and v:
                        row[k] = v
            self.import_listings(conv.user_key, [row])
            n = sum(1 for li in conv.listings if li.prop.fields.get("ma_so_can"))
            return f"Đã thêm căn {code} vào rổ. Rổ giờ có {n} căn."
        return "Không rõ công cụ."

    def _agent_reply(self, conv: Conversation, project: Project, base_system: str, now: float) -> str:
        """The DEFAULT reply for a project-inventory seller (Mảnh B): run the SAME sales-mode
        prompt (`base_system` = collect / draft-ad / fee, + basket + memory) but as a tool-using
        AGENT LOOP. The model decides per turn — reply, query the catalog, resolve 'căn góc lớn
        nhất' → a real mã số căn, ask for a link when data is missing, learn a fact, add to the
        basket — with NO keyword gate. A manual loop so the engine executes/verifies every tool."""
        system = base_system + _AGENT_TOOL_GUIDE
        messages: list[dict] = [
            {"role": m.role, "content": m.content} for m in conv.messages()
        ] or [{"role": "user", "content": conv.history[-1].content}]
        tools = self._inventory_tools()
        final = ""
        for _ in range(MAX_AGENT_STEPS):
            turn = self._provider.converse(system, messages, tools, max_tokens=REPLY_MAX_TOKENS)
            if turn.text:
                final = turn.text
            if turn.tool_calls:  # our custom tools → execute + feed results back
                messages.append({"role": "assistant", "content": turn.assistant_content})
                messages.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": c.id, "content": self._exec_tool(conv, project, c)}
                    for c in turn.tool_calls
                ]})
                continue
            if turn.stop_reason == "pause_turn":  # a server tool (web_search) needs another round-trip
                messages.append({"role": "assistant", "content": turn.assistant_content})
                continue
            break
        return final or "Dạ anh/chị nói rõ thêm giúp em căn nào ạ?"

    # PROJECT-LEVEL map words (phân lô / bản đồ / quy hoạch / tổng thể). Distinct from a per-căn
    # "bản vẽ/mặt bằng căn 24" — these refer to the whole-development map we often already hold as a
    # grounded doc. Bare "sơ đồ" is left out on purpose ("sơ đồ căn" usually means a floor plan).
    _MAP_HINTS = ("phân lô", "phan lo", "bản đồ", "ban do", "quy hoạch", "quy hoach",
                  "tổng thể", "tong the")

    @staticmethod
    def _project_map_doc(project: Project | None) -> dict[str, str] | None:
        """The project's stored phân-lô / sơ-đồ / quy-hoạch map doc, if any (most recent)."""
        if project is None:
            return None
        needles = ("phan lo", "so do", "ban do", "quy hoach")
        for d in reversed(project.docs):
            hay = normalize_project_name((d.get("title") or "") + " " + (d.get("kind") or ""))
            if any(n in hay for n in needles):
                return d
        return None

    def _media_request_reply(self, conv: Conversation, project: Project | None, text: str) -> str:
        """Answer a 'cho xem bản vẽ/mặt bằng căn X' request WITHOUT touching the basket. Resolve
        the căn → its mẫu nhà, and point to the model's stored brochure/floor-plan if we have it
        (units of a model share it); else be honest and offer to get it."""
        # The seller is asking about the PROJECT'S phân-lô/bản-đồ and we DO hold it as a grounded
        # doc → READ it to answer (they want us to consult the map), and offer the stored file. This
        # fixes AOND wrongly insisting "em CHƯA có bản vẽ" when the phân lô .md is right there.
        if any(h in text.lower() for h in self._MAP_HINTS):
            doc = self._project_map_doc(project)
            if doc is not None:
                answer = self._answer_from_docs(conv, project, text)
                link = doc.get("web_url")
                if link:
                    answer += f"\n\n📐 Em có sẵn file phân lô/bản đồ đây ạ: {link}"
                return answer
        codes = re.findall(r"\d+[\w\-/]*", text)
        mau = None
        if project is not None:
            for c in codes:
                u = project.find_unit(c)
                if u is not None and u.attrs.get("mau_nha"):
                    mau = u.attrs["mau_nha"]
                    break
            if mau is None:  # maybe they named the model directly ("bản vẽ mẫu Cosmo")
                for ut in project.unit_types:
                    if normalize_project_name(ut.name) in normalize_project_name(text):
                        mau = ut.name
                        break
        ut = project.find_unit_type(mau) if (project is not None and mau) else None
        who = (f"căn {codes[0]}" if codes else "căn mình")
        if mau:
            who += f" (mẫu {mau})"
        if ut is not None and ut.media:
            link = next((m.get("web_url") for m in ut.media if m.get("web_url")), "")
            tail = f" đây ạ: {link}" if link else " em có lưu, để em gửi lại nha."
            return f"Dạ bản vẽ/mặt bằng mẫu {ut.name}{tail} 📐"
        return (f"Dạ em CHƯA có bản vẽ/mặt bằng {who} ạ 😅. Anh/chị gửi giúp em file bản vẽ, "
                "hoặc để em xin bên dự án rồi gửi lại nhé! (Em không đụng gì tới rổ hàng đâu ạ.)")

    def _answer_from_docs(self, conv: Conversation, project: Project, text: str) -> str:
        """Answer a factual question STRICTLY from the project's stored source docs — a focused
        call (docs + the question only) so the model reads the document instead of following the
        collection script or recalling/fabricating. The seller's OWN price book (private, from any
        price list they sent) is injected too, so a price question is answered from it instead of
        the price-free shared catalog — otherwise AOND wrongly says 'em chưa có giá'."""
        structured = self._structured_shared_block(project)  # mẫu nhà specs + project facts
        system = (
            f'Bạn là "{conv.assistant.name}", trợ lý BĐS của AI Ơi Nhà Đất. '
            + build_project_docs_block(project.name, project.docs)
            + (f"\n\n=== KIẾN THỨC CHUNG DỰ ÁN (em ĐÃ CÓ SẴN) ===\n{structured}\n" if structured else "")
            + self._seller_price_block(conv)
            + "\nTrả lời câu hỏi của khách NGẮN GỌN, ĐÚNG theo tài liệu / mẫu nhà / bảng giá trên. Khi "
            "khách hỏi chi tiết MẪU NHÀ (số tầng, số phòng, bố trí), TRẢ LỜI NGAY từ mục MẪU NHÀ — đó "
            "là kiến thức chung em ĐÃ CÓ, KHÔNG nói 'em không tra cứu được'. Xưng \"em\", "
            "gọi khách \"anh/chị\", thân thiện. TUYỆT ĐỐI không bịa; nếu KHÔNG có trong tài liệu lẫn "
            "bảng giá thì mới nói \"em chưa có thông tin về ý này ạ\" rồi mời khách bổ sung."
        )
        return self._provider.chat(system, [Message("user", text)], max_tokens=REPLY_MAX_TOKENS).strip()

    def handle_link(self, user_key: str, url: str, now: float | None = None,
                    caption: str | None = None) -> TurnResult:
        """Seller pasted ONE link — auto-detect what it is (no command needed): a Google
        Sheets / CSV / Excel → import listings; a photo/PDF → read it; a folder → the media
        path. `caption` = any words the seller sent WITH the link (e.g. "tôi bán 2 căn Cosmo
        Gen 2 trong này") — used to scope a price-list import. Entry point for a pasted link."""
        now = now if now is not None else time.time()
        text: str | None = None
        data: bytes | None = None
        media_type: str | None = None
        if bulk.sheets_export_url(url) is not None:  # Google Sheets
            try:
                text = self._spreadsheet_text(url)
            except FetchError:
                text = None
        elif not is_drive_folder(url):
            try:
                data, media_type = fetch_raw(url)
            except FetchError:
                data = None
            if data is not None:
                kind = bulk.sniff_kind(data, media_type, url)
                if kind == "xlsx":
                    text = bulk.xlsx_to_csv(data)
                elif kind == "csv":
                    text = data.decode("utf-8-sig", "replace")

        if text is not None:  # it's a spreadsheet → bulk import
            conv = self.conversation(user_key)
            n = self.import_csv(user_key, text)
            reply = (
                f"Dạ em nhận được bảng tính rồi, đã nhập {n} căn vào rổ hàng của mình ạ! 🎉 "
                "Anh/chị xem lại giúp em nhé."
                if n else
                "Dạ em có nhận bảng tính nhưng chưa đọc ra căn nào — anh/chị kiểm tra lại "
                "các cột (loại hình, địa chỉ, diện tích, giá...) giúp em nhé."
            )
            conv.history.append(Turn("user", f"[Khách gửi bảng tính qua link: {url}]", now))
            conv.history.append(Turn("assistant", reply, now))
            conv.last_ai_at = now
            return self._build_result(conv, reply, captured=[], now=now)

        # A WEB PAGE (project website / info page) → read + summarize to a grounded .md in the
        # project's SHARED knowledge (source = the link), so it's re-readable fast later.
        if data is not None and self._looks_like_html(data, media_type):
            return self._ingest_web_link(user_key, url, data, now)

        # Not a spreadsheet / web page → photo/PDF/folder: the vision-read path.
        return self.handle_media_batch(user_key, [url], now, caption)

    def handle_media(self, user_key: str, url: str, now: float | None = None,
                     caption: str | None = None) -> TurnResult:
        """Seller sent a link to a photo or PDF — read it and confirm. See handle_media_batch."""
        return self.handle_media_batch(user_key, [url], now, caption)

    @staticmethod
    def _looks_like_html(data: bytes, media_type: str | None) -> bool:
        if (media_type or "").lower().startswith(("text/html", "application/xhtml")):
            return True
        head = data[:512].lstrip().lower()
        return (head.startswith((b"<!doctype html", b"<html"))
                or b"<head" in head or b"<body" in head)

    @staticmethod
    def _html_to_text(data: bytes) -> str:
        """Crude HTML → visible text (drop scripts/styles/tags, unescape entities). Good enough
        for the model to summarize — no parser dependency."""
        s = data.decode("utf-8", "replace")
        s = re.sub(r"(?is)<(script|style|noscript|template)[^>]*>.*?</\1>", " ", s)
        s = re.sub(r"(?s)<!--.*?-->", " ", s)
        s = re.sub(r"(?s)<[^>]+>", " ", s)
        return re.sub(r"\s+", " ", _html.unescape(s)).strip()

    def _ingest_web_link(self, user_key: str, url: str, data: bytes, now: float) -> TurnResult:
        """Seller gave a LINK to a project web page → read it, summarize FAITHFULLY to a grounded
        .md, and (if we know the project) store it in the SHARED project knowledge tagged with the
        source URL — so this seller and the next can ask about it. Public info, sourced — no
        fabrication. This completes the "ask for a trusted link" loop."""
        conv = self.conversation(user_key)
        du_an = conv.prop.fields.get("du_an")
        host = urlparse(url).netloc or "trang web"
        page = self._html_to_text(data)[:20000]
        conv.history.append(Turn("user", f"[Khách gửi link trang web: {url}]", now))
        if not page:
            reply = ("Dạ em mở link nhưng chưa đọc được nội dung ạ 😅. Anh/chị gửi link khác, "
                     "hoặc dán nội dung giúp em nhé.")
        else:
            system = (
                "Bạn là trợ lý BĐS của AI Ơi Nhà Đất. Đọc NỘI DUNG TRANG WEB dưới đây và tóm tắt "
                "TRUNG THỰC thành Markdown các thông tin CÔNG CỘNG của dự án BĐS có trong trang: "
                "chủ đầu tư, quy hoạch/tổng số căn-lô, vị trí, tiện ích, pháp lý, tiến độ, mẫu nhà... "
                "CHỈ ghi thông tin CÓ THẬT trong trang — TUYỆT ĐỐI không bịa, không thêm giá bán của "
                "người rao. Ngắn gọn, gạch đầu dòng. Nếu trang KHÔNG phải về dự án BĐS thì nói rõ."
            )
            summary = self._provider.chat(system, [Message("user", page)],
                                          max_tokens=REPLY_MAX_TOKENS).strip()
            if du_an:
                markdown = f"# Tham khảo web — {du_an}\n\n*(Nguồn: {url})*\n\n{summary}"
                self._save_project_doc(du_an, kind="tham khảo web",
                                       title=f"tham khảo web — {host}", markdown=markdown)
                reply = (f"Dạ em đọc trang **{host}** rồi ạ:\n\n{summary}\n\n"
                         f"Em đã lưu vào kiến thức dự án (nguồn: {url}) — lần sau anh/chị hỏi là "
                         "có ngay ạ 😊")
            else:
                reply = (f"Dạ em đọc trang **{host}** rồi ạ:\n\n{summary}\n\n"
                         "Anh/chị đang rao dự án nào để em gắn thông tin này vào cho đúng ạ?")
        conv.history.append(Turn("assistant", reply, now))
        conv.last_ai_at = now
        return self._build_result(conv, reply, captured=[], now=now)

    def _escalate_oversize(self, conv: Conversation, urls: list[str], now: float) -> TurnResult:
        """The send exceeded MAX_SEND_BYTES — don't process it, hand it to a human admin. We
        record the escalation (a Live-Chat-Monitor picks it up) and tell the seller not to
        worry: an admin will open this chat and handle their file/link."""
        conv.admin_escalations.append(
            {"reason": "media_over_size", "limit_bytes": MAX_SEND_BYTES,
             "urls": list(urls), "at": now})
        log.warning("Oversize media send (>%d bytes) escalated to admin for %s: %s",
                    MAX_SEND_BYTES, conv.user_key, urls)
        reply = (
            "Dạ file to quá ạ (>20MB), xin lỗi em chưa xử lý được 😔. Em đã báo admin rồi ạ — "
            "anh/chị không cần làm gì thêm đâu. Admin của em sẽ vào chat này đọc file/link của "
            "mình rồi xử lý sau nhé!"
        )
        conv.history.append(Turn("user", f"[Khách gửi file/link >20MB: {', '.join(urls)}]", now))
        conv.history.append(Turn("assistant", reply, now))
        conv.last_ai_at = now
        return replace(self._build_result(conv, reply, captured=[], now=now), escalated=True)

    def handle_media_batch(
        self, user_key: str, urls: list[str], now: float | None = None,
        caption: str | None = None,
    ) -> TurnResult:
        """Seller sent one or more photo/PDF links: fetch (whole send capped at MAX_SEND_BYTES),
        have the vision model read them in one call, capture any data, and reply by stating what
        we saw and asking to confirm. `caption` = words sent WITH the files (scopes a price
        list — e.g. only the 2 căn the seller rao, not the whole project inventory)."""
        now = now if now is not None else time.time()
        conv = self.conversation(user_key)

        # Expand each link first: a Drive folder becomes its many child files (with the
        # API's clean names); a plain file or non-Drive link stays as one source.
        sources: list[tuple[str, str | None]] = []
        for url in urls:
            try:
                sources.extend(self._resolve(url))
            except Exception:
                sources.append((url, None))

        fetched: list[FetchedMedia] = []
        total = 0
        for src_url, name in sources:
            try:
                media = self._fetcher(src_url)
            except FetchError:
                continue
            if name:  # prefer the resolver's clean name over the download header's
                media = replace(media, filename=name)
            total += len(media.data)
            # The WHOLE send (all files/links/folders together) is capped at MAX_SEND_BYTES.
            # Over it, we don't process anything — we hand the chat to a human admin.
            if total > MAX_SEND_BYTES:
                return self._escalate_oversize(conv, urls, now)
            fetched.append(media)

        if not fetched:
            # Folder links get a specific nudge; everything else gets the friendly fallback.
            if any(is_drive_folder(u) for u in urls):
                reply = ("Dạ đây là link thư mục Drive ạ. Anh/chị gửi từng ảnh/file "
                         "(hoặc link từng tấm) giúp em nhé!")
            else:
                reply = ("Dạ em chưa mở được link này ạ. Anh/chị chụp và gửi thẳng "
                         "giúp em vài tấm nhé!")
            conv.history.append(Turn("user", f"[Khách gửi link không đọc được: {', '.join(urls)}]", now))
            conv.history.append(Turn("assistant", reply, now))
            conv.last_ai_at = now
            return self._build_result(conv, reply, captured=[], now=now)

        # Show real filenames individually; collapse unnamed files into "N ảnh / N tài liệu"
        # (Drive often stores mojibake names we drop) so the list stays readable.
        named = [m.filename for m in fetched if m.filename]
        n_img = sum(1 for m in fetched if not m.filename and m.kind == "image")
        n_pdf = sum(1 for m in fetched if not m.filename and m.kind == "pdf")
        parts = list(named)
        if n_img:
            parts.append(f"{n_img} ảnh")
        if n_pdf:
            parts.append(f"{n_pdf} tài liệu")
        names = ", ".join(parts)

        # A PRICE LIST / rổ hàng (many căn × giá) usually comes as a PDF, or a SINGLE image
        # screenshot of a table — try importing it as structured inventory first; if it isn't
        # one, fall through to the normal read. (A single house photo costs one extra cheap
        # extract_media call that returns la_bang_gia=false; a multi-photo batch is skipped so
        # normal photo sends aren't taxed.)
        try_inventory = any(m.kind == "pdf" for m in fetched) or (
            len(fetched) == 1 and fetched[0].kind == "image"
        )
        if try_inventory:
            result = self.import_price_list(user_key, fetched, caption=caption)
            if result is not None:
                n_new, n_imported, du_an, n_in_list, uncertain = result
                where = f" — dự án {du_an}" if du_an else ""
                # GỬI FILE ≠ RAO: nothing entered the rổ (no explicit rao intent) → keep the price
                # list as reference and ASK which căn they want to rao. Don't auto-list.
                if n_imported == 0:
                    cap_line = f"“{caption.strip()}” — " if caption and caption.strip() else ""
                    reply = (f"📊 {cap_line}Em đọc được bảng giá ({names}) rồi ạ — **{n_in_list} căn**"
                             f"{where}, em đã LƯU LẠI GIÁ của từng căn. Em chưa cho căn nào vào rổ "
                             "rao vì mình chưa nói rao căn nào — anh/chị muốn RAO căn nào ạ (hoặc "
                             "hỏi em giá căn nào cũng được)? 😊")
                    if conv.pending_name_note:
                        reply += "\n\n🤔 " + conv.pending_name_note
                        conv.pending_name_note = ""
                    conv.history.append(Turn("user",
                        f"[Khách gửi bảng giá tham chiếu: {names}]" if not caption else
                        f"[Khách nhắn: “{caption.strip()}” kèm bảng giá: {names}]", now))
                    conv.history.append(Turn("assistant", reply, now))
                    conv.last_ai_at = now
                    return self._build_result(conv, reply, captured=[], now=now)
                # If the seller scoped it ("chỉ 2 căn Cosmo Gen 2"), say we took only those.
                scope = (f" (bảng có {n_in_list} căn, em chỉ lấy đúng {n_imported} căn anh/chị rao)"
                         if n_imported < n_in_list else "")
                if n_new == n_imported:
                    body = f"đã nhập **{n_imported} căn** vào rổ hàng{where}{scope} ạ! Mỗi căn em ghi theo mã lô + mẫu nhà + giá."
                elif n_new == 0:
                    body = f"rổ hàng đã có đủ **{n_imported} căn** này rồi{where}{scope} — em CẬP NHẬT lại giá/thông tin, KHÔNG thêm trùng nha."
                else:
                    body = f"em thêm **{n_new} căn mới** và cập nhật {n_imported - n_new} căn đã có{where}{scope} (không trùng lặp)."
                cap_line = f"“{caption.strip()}” — " if caption and caption.strip() else ""
                # Ambiguous scope → import the best guess but ASK to confirm (don't guess silently).
                if uncertain:
                    picked = ", ".join(f"lô {li.prop.fields['ma_so_can']}"
                                       for li in conv.listings if li.prop.fields.get("ma_so_can"))
                    tail = (f"\n⚠️ Em CHƯA chắc ý anh/chị nên tạm lấy: **{picked}**. Đúng những căn "
                            "này không ạ, hay anh/chị muốn căn nào khác? Em chỉnh lại ngay.")
                else:
                    tail = "\nAnh/chị xem giúp em có đúng không — cần sửa căn nào báo em ạ."
                reply = f"📊 {cap_line}Em đọc bảng giá ({names}) và {body}{tail}"
                if conv.pending_name_note:
                    reply += "\n\n🤔 " + conv.pending_name_note
                    conv.pending_name_note = ""
                seller_said = f"[Khách nhắn: “{caption.strip()}” kèm bảng giá: {names}]" if caption and caption.strip() else f"[Khách gửi bảng giá: {names}]"
                conv.history.append(Turn("user", seller_said, now))
                conv.history.append(Turn("assistant", reply, now))
                conv.last_ai_at = now
                return self._build_result(conv, reply, captured=["ma_so_can", "gia"], now=now)

        if len(fetched) > 1:
            # A batch (folder / several links) may hold MULTIPLE different căn, or ảnh
            # shared by the whole project — figure out which before extracting anything.
            reply, captured = self._group_media_batch(conv, fetched, urls, user_key, now)
        else:
            instruction = build_media_read_instruction(
                conv.assistant.name, self._pending_seller_question(conv))
            description = self._provider.describe_media(
                [(m.data, m.media_type) for m in fetched], instruction
            ).strip()
            conv.history.append(Turn(
                "user",
                f"[Khách gửi {len(fetched)} tệp ({names}). Nội dung em đọc được từ tài liệu của khách:]\n{description}",
                now,
            ))
            # Register the project + model, then store the files where they belong: a MẪU NHÀ
            # brochure → the UnitType's shared media (units inherit), else the active căn.
            captured, capped = self._route_readout_media(conv, description, fetched, user_key)
            reply = f"📎 Em đã nhận: {names}\n\n{description}" + self._cap_note(capped)
            if (q := self._drain_name_concern()):  # read doc named a near-miss project → ASK
                reply += "\n\n🤔 " + q

        if len(fetched) < len(sources):
            reply += (
                f"\n\n(Có {len(sources) - len(fetched)} tệp em chưa mở được — anh/chị "
                f"gửi lại giúp em nhé ạ.)"
            )
        conv.history.append(Turn("assistant", reply, now))
        conv.last_ai_at = now
        return self._build_result(conv, reply, captured, now)

    @staticmethod
    def _pending_seller_question(conv: Conversation) -> str:
        """The seller's most recent REAL message (not a bracketed media/system marker) — passed
        into a media read so that a file re-sent right after a question gets ANSWERED from the file
        instead of a generic prompt (the seller who re-sends the phân lô wants the đường-nội-bộ
        number they just asked for). Empty if the last seller turn was itself a file/marker."""
        for t in reversed(conv.history):
            if t.role != "user":
                continue
            c = (t.content or "").strip()
            return "" if c.startswith("[") else c
        return ""

    def _folder_for(self, listing: Listing) -> str:
        return listing_folder(self._canonical_du_an(listing.prop.fields.get("du_an")),
                               listing.prop.fields.get("dia_chi"),
                               listing.listing_id, listing.prop.fields.get("ma_so_can"))

    @staticmethod
    def _unittype_folder(project_name: str, ut_name: str) -> str:
        """Shared media folder for a house model: `<Dự án>/<Mẫu nhà>/_Chung` — every unit
        of the model inherits these (brochure/floor-plan/renders), per [[aioi-data-model]]."""
        return f"{project_name}/{ut_name}/_Chung"

    def _store_unittype_media(self, project_name: str, ut_name: str, items: list[FetchedMedia]) -> list[dict[str, str]]:
        """Upload model brochure/render files to the UnitType's shared folder and attach the
        refs to the UnitType (NOT to any one căn) so every unit of the model shares them."""
        project = self._projects.get(project_name)
        if project is None:
            return []
        ut = project.find_unit_type(ut_name)
        if ut is None:
            return []
        refs, _ = self._upload_media(self._unittype_folder(project.name, ut.name), items)
        ut.media += refs
        self._projects.save(project)
        return refs

    def _ingest_group_docs(
        self, project_name: str | None, items: list[FetchedMedia], kind: str,
        default_title: str | None = None,
    ) -> list[str]:
        """A seller-sent DOCUMENT (PDF brochure / phân lô / gói nội thất) → transcribe ONCE to
        a faithful grounded .md on the project, so the bot can answer factual questions about it
        later (Phase 2 grounding). Only PDFs become docs (photos are stored as media); returns
        the titles ingested (for the receipt). Best-effort — a failed transcription is skipped."""
        if not project_name:
            return []
        titles: list[str] = []
        for m in items:
            if m.kind != "pdf":
                continue
            title = default_title or (m.filename.rsplit(".", 1)[0] if m.filename else kind)
            try:
                self._ingest_doc_bytes(m.data, m.media_type, project_name, kind=kind, title=title)
                titles.append(title)
            except (FetchError, SharePointError, ValueError):
                continue
        return titles

    def _route_readout_media(
        self, conv: Conversation, description: str, fetched: list[FetchedMedia],
        user_key: str, store: bool = True,
    ) -> tuple[list[str], int]:
        """After a vision read-out, register the project + model, then store the files where
        they BELONG: a MẪU NHÀ brochure (house model) → the UnitType's shared media (every
        unit of the model inherits them), otherwise the active căn's own folder. `store=False`
        for a pure DATA page (infometric — bảng giá/thông số): the DATA is still extracted, the
        image is abandoned. Returns `(captured, n_capped)`."""
        proj = self.register_project_from_text(description, voter=user_key)
        ut = self.register_unit_type_from_text(description, voter=user_key)
        if ut is not None and proj is not None:
            if store:
                self._store_unittype_media(proj.name, ut.name, fetched)
                # A lone brochure PDF for a model → also transcribe to a grounded .md.
                self._ingest_group_docs(proj.name, fetched, kind="mẫu nhà", default_title=ut.name)
            return [], 0
        # A project DOCUMENT that isn't a model brochure — phân lô, sơ đồ, quy cách bàn giao,
        # phụ lục, tiện ích... — is SHARED project knowledge: transcribe it to a grounded .md so
        # it's re-readable, not read-once-and-forgotten. PDFs always; IMAGES only when they read
        # as a REFERENCE doc (a phân-lô map, not a photo of one căn — that stays on the căn).
        du_an = (proj.name if proj is not None else None) or conv.prop.fields.get("du_an")
        is_ref = _looks_like_reference_doc(description)
        to_ingest = [m for m in fetched if m.kind == "pdf" or (is_ref and m.kind == "image")]
        if store and du_an and to_ingest:
            for m in to_ingest:
                title = self._doc_title(m.filename, description)
                try:
                    self._ingest_doc_bytes(m.data, m.media_type, du_an,
                                           kind="tài liệu dự án", title=title)
                except (FetchError, SharePointError, ValueError):
                    pass
            return [], 0
        captured = self._extract(conv)
        if store:
            refs, capped = self._upload_media(
                self._folder_for(conv.active), fetched,
                image_slots=self._max_photos - _image_count(conv.active.media),
            )
            conv.active.media += refs
            return captured, capped
        return captured, 0

    @staticmethod
    def _doc_title(filename: str | None, description: str) -> str:
        """A short doc title — from a 'Phụ lục ...' heading the model read, else the filename."""
        m = re.search(r"(Ph[ụu] l[ụu]c[^\n.:*]{0,60})", description or "")
        if m:
            return re.sub(r"\s+", " ", m.group(1)).strip()
        base = (filename or "tài liệu dự án").rsplit(".", 1)[0]
        return re.sub(r"\s+", " ", base).strip() or "tài liệu dự án"

    def _sync_media_folder(self, listing: Listing) -> None:
        """If a listing already has files stored and its computed folder changed (address or
        project captured/corrected AFTER the upload), rename/move the SharePoint folder to
        match — the stable listing_id keeps the folder identity, only the human label moves.
        Best-effort; the stored item_ids stay valid regardless (a move doesn't change them)."""
        if self._media_store is None or not listing.media:
            return
        new_folder = self._folder_for(listing)
        old_folder = listing.media[0]["path"].rsplit("/", 1)[0]
        if old_folder == new_folder:
            return
        try:
            self._media_store.move_folder(old_folder, new_folder)
        except SharePointError:
            return  # leave refs on the old path (item_ids still resolve); retry a later turn
        for ref in listing.media:  # keep refs' folder in sync so the next move computes correctly
            ref["path"] = f"{new_folder}/{ref['filename']}"

    def _upload_media(
        self, folder: str, items: list[FetchedMedia], image_slots: int | None = None
    ) -> tuple[list[dict[str, str]], int]:
        """Upload each fetched file to SharePoint (kho gốc) under `folder`. Returns
        `(refs, n_capped)` — the stored references (the DB keeps ONLY these, never the bytes)
        and how many PHOTOS were skipped because the per-listing cap was reached (so the bot
        can tell the seller). To keep storage cheap (see [[aioi-data-model]]): PHOTOS are
        DOWNSCALED (~10× smaller, 1600px — plenty for a listing display) before upload, and
        `image_slots` caps how many MORE photos this folder may take (None = unlimited);
        PDFs/docs (sổ hồng, brochure) are kept ORIGINAL. Best-effort: a failed upload skipped."""
        if self._media_store is None or not items:
            return [], 0
        refs: list[dict[str, str]] = []
        n_capped = 0
        for m in items:
            if m.kind == "image":
                if image_slots is not None:
                    if image_slots <= 0:
                        n_capped += 1  # per-listing photo cap reached — skip + count the extras
                        continue
                    image_slots -= 1
                data, media_type = downscale_image(m.data, m.media_type)  # shrink to save storage
            else:
                data, media_type = m.data, m.media_type  # keep documents original
            # Name the file, with an extension matching the (possibly RE-ENCODED) bytes: a
            # downscaled PNG becomes JPEG, so a "mat_tien.png" must be stored as "mat_tien.jpg"
            # or Windows mis-opens it. An unnamed file (Drive mojibake / bare link) gets a
            # content-hash name so many unnamed files don't collide to one and re-sends are
            # idempotent (hash of the STORED bytes).
            ext = extension_for(media_type)
            if m.filename:
                base = m.filename.rsplit(".", 1)[0] if "." in m.filename else m.filename
                filename = base + ext if ext else m.filename
            else:
                filename = f"{m.display_name}_{hashlib.sha1(data).hexdigest()[:8]}{ext}"
            try:
                ref = self._media_store.upload(folder, filename, data, media_type)
            except SharePointError:
                continue
            refs.append(ref.to_dict())
        return refs, n_capped

    def _cap_note(self, n_capped: int) -> str:
        """A friendly line telling the seller the photo limit and how many weren't kept.
        Honest about the behavior: we keep the FIRST `max_photos` and skip the rest (no
        swap mechanism yet), so it suggests sending the best shots first."""
        if n_capped <= 0:
            return ""
        return (
            f"\n\n(Mỗi căn em chỉ lưu tối đa {self._max_photos} ảnh cho tin gọn, tải nhanh nha — "
            f"nên {n_capped} tấm sau em CHƯA lưu ạ. Lần sau anh/chị gửi trước {self._max_photos} "
            "tấm ĐẸP NHẤT là đủ đó!)"
        )

    def _apply_text_to_listing(self, listing: Listing, text: str) -> list[str]:
        """Extract structured fields straight into ONE listing's Property from arbitrary
        text (a media-group's description) — used when the target isn't necessarily the
        seller's currently-active listing (see `_group_media_batch`)."""
        if not text.strip():
            return []
        data = self._provider.extract(EXTRACTION_SYSTEM, [Message("user", text)], extraction_schema())
        data.pop("is_new_listing", None)  # listing boundaries are already decided by the grouping
        if data.pop("is_rental", False):
            listing.prop.is_rental = True
        data.pop("seller_role", None)  # a media group doesn't speak for who the seller is
        return listing.prop.update({k: v for k, v in data.items() if isinstance(v, str)})

    def _group_media_batch(
        self,
        conv: Conversation,
        fetched: list[FetchedMedia],
        urls: list[str],
        user_key: str,
        now: float,
    ) -> tuple[str, list[str]]:
        """A batch of >1 files may be several different căn, ảnh chung of the project, or
        ảnh for a căn already in the basket — read filenames+folder name+image content
        TOGETHER and route each group to the right place (never guess silently; ask to
        verify when unsure) instead of blending everything into one listing."""
        labels = [m.filename or f"{'ảnh' if m.kind == 'image' else 'tài liệu'} {i}"
                  for i, m in enumerate(fetched, 1)]
        folder_name = next((n for u in urls if (n := self._folder_name(u))), None)
        basket_labels = [li.label() for li in conv.listings]
        instruction = build_media_group_instruction(conv.assistant.name, folder_name, basket_labels)
        result = self._provider.group_media(
            [(m.data, m.media_type) for m in fetched], labels, instruction, media_group_schema()
        )
        groups = [g for g in (result.get("nhom") or []) if g.get("tep")]

        if len(groups) <= 1:  # not actually multiple căn — treat as the old single-blob read
            instruction = build_media_read_instruction(
                conv.assistant.name, self._pending_seller_question(conv))
            description = self._provider.describe_media(
                [(m.data, m.media_type) for m in fetched], instruction
            ).strip()
            names = ", ".join(labels)
            conv.history.append(Turn(
                "user",
                f"[Khách gửi {len(fetched)} tệp ({names}). Nội dung em đọc được từ tài liệu của khách:]\n{description}",
                now,
            ))
            # A pure DATA page (bảng giá/thông số): extract the data, abandon the image.
            data_only = bool(groups and groups[0].get("chi_luu_du_lieu"))
            captured, capped = self._route_readout_media(
                conv, description, fetched, user_key, store=not data_only)
            tail = "\n\n(Em đã lấy thông tin từ tài liệu, không cần lưu ảnh bảng dữ liệu nha.)" if data_only else ""
            if (q := self._drain_name_concern()):  # read doc named a near-miss project → ASK
                tail += "\n\n🤔 " + q
            return (f"📎 Em đã nhận: {names}\n\n{description}{tail}" + self._cap_note(capped), captured)

        # Multiple groups: route each — "chung" enriches the shared project, "can_da_co"
        # updates an EXISTING basket listing, "can_moi" fills the empty starter listing
        # first then creates a fresh one per additional group (same pattern as CSV import).
        by_label = dict(zip(labels, fetched))  # group's "tep" filenames -> the fetched bytes
        captured: list[str] = []
        used_active = False
        summary: list[str] = []
        total_capped = 0
        known_project = conv.active.prop.fields.get("du_an")
        for g in groups:
            scope = g.get("pham_vi")
            files = ", ".join(g.get("tep") or [])
            mo_ta = (g.get("mo_ta") or "").strip()
            items = [by_label[f] for f in (g.get("tep") or []) if f in by_label]
            # A pure DATA page (bảng giá/thông số): keep the extracted data, abandon the image.
            data_only = bool(g.get("chi_luu_du_lieu"))
            store_items = [] if data_only else items
            tag = " (chỉ lấy dữ liệu, không lưu ảnh)" if data_only else ""
            if scope == "chung":
                project = self.register_project_from_text(mo_ta, project_name=known_project, voter=user_key)
                self.register_unit_type_from_text(mo_ta, voter=user_key)
                folder = f"{project.name}/_Chung" if project else "_Chung/Chua_ro_du_an"
                self._upload_media(folder, store_items)  # no listing — project-level
                # A project-level DOCUMENT (phân lô, quy hoạch...) → grounded .md for Q&A later.
                docs = [] if data_only else self._ingest_group_docs(
                    project.name if project else None, items, kind="tài liệu chung")
                doc_note = f" — đã lưu tài liệu để tra cứu: {', '.join(docs)}" if docs else ""
                summary.append(f"📁 Thông tin CHUNG dự án ({files}){tag}{doc_note}:\n{mo_ta}")
            elif scope == "mau_nha":
                # A house-model brochure (mẫu nhà) — belongs to the UnitType, shared by every
                # unit of the model, NOT to any one căn.
                project = self.register_project_from_text(mo_ta, project_name=known_project, voter=user_key)
                ut = self.register_unit_type_from_text(mo_ta, voter=user_key)
                docs: list[str] = []
                if ut is not None and project is not None:
                    self._store_unittype_media(project.name, ut.name, store_items)
                    # The brochure PDF → a grounded .md titled by the model, so "mẫu này mấy
                    # tầng, mặt tiền bao nhiêu?" can be answered from the document later.
                    if not data_only:
                        docs = self._ingest_group_docs(
                            project.name, items, kind="mẫu nhà", default_title=ut.name)
                doc_note = f" — đã lưu tài liệu để tra cứu: {', '.join(docs)}" if docs else ""
                summary.append(f"🏗️ Mẫu nhà{' ' + ut.name if ut else ''} ({files}){tag}{doc_note}:\n{mo_ta}")
            elif scope == "can_da_co":
                idx = g.get("chi_so_can_da_co")
                target = (conv.listings[idx - 1] if isinstance(idx, int) and 1 <= idx <= len(conv.listings)
                          else conv.active)
                captured += self._apply_text_to_listing(target, mo_ta)
                refs, capped = self._upload_media(
                    self._folder_for(target), store_items,
                    image_slots=self._max_photos - _image_count(target.media))
                target.media += refs
                total_capped += capped
                summary.append(f"🏠 Cập nhật căn: {target.label()} ({files}){tag}:\n{mo_ta}")
            else:  # "can_moi"
                target = conv.active if not used_active and self._listing_empty(conv.active) else conv.new_listing()
                used_active = True
                captured += self._apply_text_to_listing(target, mo_ta)
                refs, capped = self._upload_media(
                    self._folder_for(target), store_items,
                    image_slots=self._max_photos - _image_count(target.media))
                target.media += refs
                total_capped += capped
                summary.append(f"🆕 Căn mới: {target.label()} ({files}){tag}:\n{mo_ta}")
        if self._convstore is not None:
            self._convstore.save(conv)

        reply = (
            f"📎 Em đã nhận {len(fetched)} tệp, phân tích thấy có {len(groups)} nhóm:\n\n"
            + "\n\n".join(summary)
            + "\n\nAnh/chị xem giúp em tách vậy có đúng không, có căn nào bị gộp/lẫn không ạ?"
            + self._cap_note(total_capped)
        )
        if (q := self._drain_name_concern()):  # a group named a near-miss project → ASK
            reply += "\n\n🤔 " + q
        return reply, captured

    # --- internals -------------------------------------------------------

    @staticmethod
    def _is_stub_listing(li: Listing) -> bool:
        """A listing with nothing that identifies a real property — no mã số căn, no address, no
        price. (Loại hình / dự án alone don't make it a real căn.)"""
        f = li.prop.fields
        return not (f.get("ma_so_can") or f.get("dia_chi") or f.get("gia"))

    def _drop_stub_listings(self, conv: Conversation) -> None:
        """Remove the leftover empty STARTER listing (a blank Listing() every conversation opens
        with, that once lingered as junk 'căn 1 · nha_pho' in the rổ) once REAL căn exist — but
        never drop the one the seller is actively building."""
        if not any(not self._is_stub_listing(li) for li in conv.listings):
            return  # nothing real yet — the seller is still starting their first căn
        kept = [li for li in conv.listings
                if not self._is_stub_listing(li) or li.listing_id == conv.active_listing_id]
        if len(kept) != len(conv.listings):
            conv.listings = kept or [Listing()]
            if all(li.listing_id != conv.active_listing_id for li in conv.listings):
                conv.active_listing_id = conv.listings[-1].listing_id

    def _build_result(
        self, conv: Conversation, reply: str, captured: list[str], now: float
    ) -> TurnResult:
        self._drop_stub_listings(conv)   # clean up the blank starter once real căn exist
        if self._convstore is not None:  # persist the listing after every turn
            self._convstore.save(conv)
        timeliness = self._timeliness(conv, now)
        completeness = conv.prop.completeness()  # the active listing
        prop_score = g.property_score(completeness, timeliness)
        # User score aggregates across the WHOLE basket (SRD §IV: avg of property scores ×
        # scale bonus) — more listings → scale bonus, the point of growing the rổ hàng.
        all_scores = [g.property_score(li.prop.completeness(), timeliness) for li in conv.listings]
        u_score = g.user_score(all_scores, num_listings=len(conv.listings))
        missing = conv.prop.missing_fields()
        return TurnResult(
            reply=reply,
            assistant_name=conv.assistant.name,
            captured=captured,
            next_field=missing[0] if missing else None,
            user_type=conv.user_type.value,
            is_listing_post=conv.prop.summary_ready(),
            completeness=completeness,
            property_score=prop_score,
            user_score=u_score,
            tier=g.tier_for(u_score).value,
        )

    def _match_listing(self, conv: Conversation, ref: str) -> Listing | None:
        """Resolve a seller's textual reference to an EXISTING basket listing — by unit code
        number (e.g. "42" ↔ ma_so_can "căn số 42") first, then by address token overlap (a
        partial "căn ở Trương Đình Hội" matches "198A/22 Trương Đình Hội"). None if no match."""
        ref_nums = set(re.findall(r"\d+", ref))
        if ref_nums:
            for li in conv.listings:
                if set(re.findall(r"\d+", li.prop.fields.get("ma_so_can") or "")) & ref_nums:
                    return li
        ref_tokens = set(normalize_project_name(ref).split())
        for li in conv.listings:
            addr = li.prop.fields.get("dia_chi")
            if addr and len(ref_tokens & set(normalize_project_name(addr).split())) >= 2:
                return li
        return None

    def _unlist_listings(self, conv: Conversation, keep: list[str], drop: list[str],
                         clear: bool = False, status: str = KHONG_RAO) -> int:
        """AOND NEVER deletes data. 'bỏ/xóa/chỉ giữ' only flips a listing's STATUS (default
        'không rao' — hidden but kept, rao-able again anytime); the row and its media stay.
        `clear` = unlist the WHOLE basket, `keep` = unlist every OTHER coded listing, `drop` =
        unlist the named căn. Returns how many listings were affected."""
        keep_ids = {li.listing_id for c in keep if (li := self._match_listing(conv, c))}
        drop_ids = {li.listing_id for c in drop if (li := self._match_listing(conv, c))}

        def targeted(li: Listing) -> bool:
            if self._listing_empty(li):
                return False                       # a blank starter has nothing to unlist
            if clear:
                return True
            if li.listing_id in drop_ids:
                return True
            if keep:  # keep-only: unlist other CODED listings; leave code-less ones alone
                return li.listing_id not in keep_ids and bool(li.prop.fields.get("ma_so_can"))
            return False

        n = 0
        for li in conv.listings:
            if targeted(li):
                li.status = status                 # data + media kept; just hidden from active rao
                n += 1
        if self._convstore is not None:
            self._convstore.save(conv)             # persists the status change — deletes nothing
        return n

    def _extract(self, conv: Conversation) -> list[str]:
        """Plan-then-execute: ask the model for an ordered LIST OF OPERATIONS on the basket,
        then run each deterministically. Returns the changed field names (for media-sync). The
        structural receipts (what was cleared/removed/added) are stashed on the conv so the
        reply layer can confirm the true result."""
        captured, conv.last_receipts = self._plan_and_apply(conv)
        return captured

    def _plan_and_apply(self, conv: Conversation) -> tuple[list[str], list[tuple]]:
        # With a BASKET (>1 căn) the seller acts on ONE căn per message, but a reference may span
        # turns ("...phải nó không?" → "nó đó em") — so read from just AFTER the PREVIOUS user
        # turn: the latest user line plus the bot's question(s) right before it (confirm context),
        # WITHOUT re-including earlier user ACTIONS (which would re-trigger old ops). A single
        # listing still reads its full slice so data accumulates across turns.
        multi = len(conv.listings) > 1
        src = self._plan_window(conv) if multi and conv.history else conv.messages()
        # Give the planner the SAME memory the reply gets: the project's price list + phân lô +
        # the basket — so it resolves "căn Cosmo"/"căn góc lớn nhất" to a real mã lô (with its
        # data) instead of fabricating a blank căn. When the reference needs a guess it isn't
        # sure of, it leaves operations empty so the reply can confirm first.
        project = self.project_for(conv.prop)
        system = OPERATIONS_SYSTEM + self._project_memory_block(project)
        if project is not None and project.docs:
            system += (
                "KHI LẬP THAO TÁC từ bộ nhớ: khi khách rõ ràng THÊM/nhắc một căn CÓ trong bảng "
                "giá, tạo op 'add' và điền LUÔN mã lô + mẫu nhà + giá + diện tích lấy từ bộ nhớ. "
                "NHƯNG nếu phải SUY LUẬN mà chưa chắc (căn 'góc', 'lớn nhất', 'view'...), để "
                "operations RỖNG — phần trả lời sẽ hỏi xác nhận; chỉ 'add' sau khi khách xác nhận.\n"
            )
        system += self._basket_block(conv)
        plan = self._provider.extract(system, src, operations_schema())

        # Sticky conversation attributes (never downgrade role to unknown).
        raw_role = plan.get("seller_role")
        if raw_role in {r.value for r in SellerRole if r is not SellerRole.UNKNOWN}:
            conv.user_type = SellerRole(raw_role)
        if plan.get("is_rental"):
            conv.prop.is_rental = True

        changed: set[str] = set()
        receipts: list[tuple] = []
        ops = [op for op in (plan.get("operations") or []) if isinstance(op, dict)]
        safe = [op for op in ops if op.get("op") not in _DESTRUCTIVE_OPS]
        destructive = [op for op in ops if op.get("op") in _DESTRUCTIVE_OPS]
        for op in safe:  # updates/adds/status apply immediately — they don't lose data
            self._apply_operation(conv, op, changed, receipts)
        # DESTRUCTIVE ops (clear / bỏ / chỉ giữ) do NOT apply on first read — a misread or garbled
        # message must never silently wipe the rổ. Hold them for an explicit CONFIRM (handled at
        # the top of handle()). Skip the gate only when there's nothing real to lose.
        if destructive and self._basket_size(conv) > 0:
            conv.pending_destructive = destructive
            receipts.append(("confirm_destructive", self._describe_destructive(conv, destructive)))
        else:
            for op in destructive:
                self._apply_operation(conv, op, changed, receipts)
        return sorted(changed), receipts

    def _describe_destructive(self, conv: Conversation, ops: list[dict]) -> str:
        """A short description of what the pending unlist op(s) WOULD do, for the confirmation
        question — so a misread ('ẩn hết 5 căn?') is obvious. NB: this NEVER deletes data; it
        moves listings to 'không rao' (hidden but kept), reversible by rao-ing again."""
        n = self._basket_size(conv)
        for op in ops:
            if op.get("op") == "clear":
                return f"NGƯNG RAO (ẩn) toàn bộ {n} căn — dữ liệu vẫn giữ, rao lại được bất cứ lúc nào"
            if op.get("op") == "keep_only":
                keep = ", ".join(self._op_targets(op)) or "(không rõ)"
                return f"chỉ GIỮ RAO căn {keep}, NGƯNG RAO (ẩn) các căn còn lại (dữ liệu vẫn giữ)"
            if op.get("op") == "remove":
                drop = ", ".join(self._op_targets(op)) or "(không rõ)"
                return f"NGƯNG RAO (ẩn) căn {drop} — dữ liệu vẫn giữ, rao lại được"
        return "đổi trạng thái rao của rổ hàng"

    def _apply_pending_destructive(self, conv: Conversation) -> tuple[list[str], list[tuple]]:
        """Apply the destructive op(s) the seller just CONFIRMED, then clear the pending state."""
        changed: set[str] = set()
        receipts: list[tuple] = []
        for op in conv.pending_destructive:
            self._apply_operation(conv, op, changed, receipts)
        conv.pending_destructive = []
        return sorted(changed), receipts

    @staticmethod
    def _plan_window(conv: Conversation) -> list[Message]:
        """The turns to plan from: everything AFTER the previous user turn — i.e. the latest
        user message plus the bot's turn(s) right before it (so a confirmation like 'nó đó em'
        resolves against the căn the bot just proposed), but NOT earlier user actions."""
        user_idxs = [i for i, t in enumerate(conv.history) if t.role == "user"]
        start = (user_idxs[-2] + 1) if len(user_idxs) >= 2 else 0
        return [Message(t.role, t.content) for t in conv.history[start:]]

    def _extract_fields(self, conv: Conversation) -> dict[str, str]:
        """Fields-only extraction of the ACTIVE listing's clean history slice (used when a new
        căn is split off, so its data is read fresh from just its own messages)."""
        data = self._provider.extract(EXTRACTION_SYSTEM, conv.messages(), extraction_schema())
        return {k: v for k, v in data.items() if isinstance(v, str)}

    @staticmethod
    def _op_targets(op: dict) -> list[str]:
        raw = list(op.get("targets") or [])
        single = op.get("target")
        if single:
            raw = [single] + [t for t in raw if t != single]
        return list(dict.fromkeys(
            str(t).strip() for t in raw if isinstance(t, (str, int)) and str(t).strip()
        ))

    def _resolve_targets(self, conv: Conversation, targets: list[str]) -> list[Listing]:
        out: list[Listing] = []
        seen: set[str] = set()
        for t in targets:
            li = self._match_listing(conv, t)
            if li is not None and li.listing_id not in seen:
                seen.add(li.listing_id)
                out.append(li)
        return out

    def _apply_operation(self, conv: Conversation, op: dict, changed: set[str],
                         receipts: list[tuple]) -> None:
        kind = op.get("op")
        targets = self._op_targets(op)
        fields = op.get("fields") if isinstance(op.get("fields"), dict) else {}

        def _data(exclude_code: bool = False) -> dict[str, str]:
            return {k: v for k, v in fields.items()
                    if isinstance(v, str) and v.strip() and not (exclude_code and k == "ma_so_can")}

        # "xóa/bỏ/chỉ giữ" NEVER delete — they move listings to 'không rao' (hidden, kept).
        if kind == "clear":
            n = self._unlist_listings(conv, keep=[], drop=[], clear=True)
            receipts.append(("clear", n))
            return
        if kind == "keep_only":
            self._unlist_listings(conv, keep=targets, drop=[])
            receipts.append(("keep_only", targets, self._rao_count(conv)))
            return
        if kind == "remove":
            self._unlist_listings(conv, keep=[], drop=targets)
            receipts.append(("remove", targets, self._rao_count(conv)))
            return
        if kind == "add":
            # Multiple units of the SAME model ("2 căn 42 và 41") → one listing per mã số căn
            # (an existing code UPDATES, never duplicates). A single add starts a fresh listing
            # (filling the empty starter first) so its fields don't bleed onto the previous căn.
            if len(targets) >= 2:
                changed.update(self._fan_out_units(conv, _data(exclude_code=True), targets))
            elif self._listing_empty(conv.active):
                # First căn / empty starter — no previous căn to bleed from; use the plan fields.
                data = _data()
                if targets:
                    data["ma_so_can"] = targets[0]
                changed.update(conv.active.prop.update(data))
            else:
                # A genuinely NEW căn alongside existing ones: open a fresh listing pointed at
                # the describing message and RE-READ its fields from that clean slice, so nothing
                # bleeds from the previous căn.
                li = conv.new_listing()
                li.history_start = max(0, len(conv.history) - 1)
                data = self._extract_fields(conv)
                if targets:
                    data["ma_so_can"] = targets[0]
                changed.update(li.prop.update(data))
            receipts.append(("add", targets))
            return
        if kind == "set_status":
            status = STATUS_MAP.get(op.get("status") or "")
            listings = self._resolve_targets(conv, targets) if targets else []
            if listings:  # the referenced căn(s) exist → mark each and switch context to them
                for li in listings:
                    if status:
                        li.status = status
                    changed.update(li.prop.update(_data(exclude_code=True)))
                conv.active_listing_id = listings[-1].listing_id
            else:  # no/unknown target → the active (only) căn; adopt the code if one was named
                if status:
                    conv.active.status = status
                data = _data()
                if targets:
                    data.setdefault("ma_so_can", targets[0])
                changed.update(conv.active.prop.update(data))
            receipts.append(("set_status", status, listings or [conv.active]))
            return
        # kind == "update" (or unknown → treat as a plain data update). A resolvable target
        # edits that existing căn; an UNresolved target names THIS căn (adopt the code); no
        # target = the seller is building the active căn.
        if targets:
            listings = self._resolve_targets(conv, targets)
            if listings:
                for li in listings:
                    changed.update(li.prop.update(_data(exclude_code=True)))
                conv.active_listing_id = listings[-1].listing_id
            else:
                data = _data()
                data.setdefault("ma_so_can", targets[0])
                changed.update(conv.active.prop.update(data))
        else:
            changed.update(conv.active.prop.update(_data()))

    @staticmethod
    def _basket_size(conv: Conversation) -> int:
        """How many REAL (non-empty) căn are in the basket right now (any status)."""
        return sum(1 for li in conv.listings if (li.prop.fields.get("ma_so_can")
                                                 or li.prop.fields.get("dia_chi")
                                                 or li.prop.completeness() > 0))

    @staticmethod
    def _rao_count(conv: Conversation) -> int:
        """How many căn are CURRENTLY being advertised (status not 'không rao'/'thôi đăng ký'/
        'đã bán'/'đã cho thuê'). The unlisted ones are kept, just hidden from the active rao."""
        hidden = {KHONG_RAO, THOI_DANG_KY, "đã bán", "đã cho thuê"}
        return sum(1 for li in conv.listings
                   if li.status not in hidden and not Engine._listing_empty(li))

    def _receipt_reply(self, conv: Conversation, receipts: list[tuple]) -> str | None:
        """Build the reply for an unlist op DIRECTLY from the real post-action state — so the
        words can never disagree with the deed. These ops NEVER delete: they move listings to
        'không rao' (hidden, kept). Non-destructive ops return None → normal persona reply."""
        kinds = {r[0] for r in receipts}
        if "confirm_destructive" in kinds:  # ask BEFORE changing anything
            what = next((r[1] for r in receipts if r[0] == "confirm_destructive"), "đổi trạng thái rao")
            return (f"Anh/chị muốn em **{what}** — đúng không ạ? "
                    "Nhắn **\"đúng\"** để em làm, hoặc **\"thôi\"** để giữ nguyên nhé. "
                    "(Em KHÔNG xóa dữ liệu đâu — chỉ ẩn tin, rao lại lúc nào cũng được.)")
        if "clear" in kinds:
            return ("Dạ em đã NGƯNG RAO toàn bộ rổ hàng ạ — các tin giờ ở trạng thái **không rao** "
                    "(em vẫn giữ nguyên dữ liệu, anh/chị muốn rao lại căn nào cứ nhắn em). 🙈")
        if kinds & {"remove", "keep_only"}:
            n = self._rao_count(conv)
            return (f"Dạ em đã cập nhật — giờ còn **{n} căn đang rao** ạ. Các căn kia em chuyển sang "
                    "**không rao** (vẫn giữ dữ liệu, rao lại được bất cứ lúc nào nha). ✅")
        return None

    def _fan_out_units(self, conv: Conversation, shared: dict[str, str], codes: list[str]) -> list[str]:
        """One listing per unit code, all sharing `shared` (du_an, unit_type, giá...). A code
        that ALREADY exists in the basket UPDATES that căn (one seller can't list the same căn
        twice); a new code fills the empty starter first, then a fresh listing. New units point
        their history slice at the describing message so the reply has context."""
        names: set[str] = set()
        start = max(0, len(conv.history) - 1)  # the message that named all the codes
        du_an = shared.get("du_an")
        for code in codes:
            existing = self._listing_by_code(conv, code, du_an)
            if existing is not None:
                names.update(existing.prop.update({**shared, "ma_so_can": code}))
                continue
            target = conv.active if self._listing_empty(conv.active) else conv.new_listing()
            target.history_start = start
            names.update(target.prop.update({**shared, "ma_so_can": code}))
        return sorted(names)

    def _timeliness(self, conv: Conversation, now: float) -> float:
        """Map seller response latency to [0, 1]. Fresh conversations start at 1.0."""
        if conv.last_ai_at is None:
            return 1.0
        elapsed_hours = (now - conv.last_ai_at) / 3600.0
        if elapsed_hours <= 1:
            return 1.0
        if elapsed_hours >= 120:  # 5 days = Keep-Alive threshold (SRD §VI)
            return 0.0
        return max(0.0, 1.0 - elapsed_hours / 120.0)
