"""Bulk listing import from a spreadsheet (CSV) — the fastest way to grow the rổ hàng.

A broker/sàn keeps their basket in Excel/Sheets; export to CSV and import → one listing
per row. Deterministic column mapping (no LLM, offline-testable): recognized Vietnamese/
English headers map to our field slugs; type names map to type slugs. Unknown columns are
ignored. (An LLM-assisted header mapping for arbitrary sheets is a future enhancement.)
"""
from __future__ import annotations

import csv
import io
import re
import unicodedata
import zipfile

# Normalized header → field slug.
COLUMN_ALIASES: dict[str, str] = {
    "loai hinh": "loai_hinh", "loai": "loai_hinh", "type": "loai_hinh",
    "dia chi": "dia_chi", "address": "dia_chi", "vi tri": "dia_chi",
    "dien tich": "dien_tich", "dt": "dien_tich", "area": "dien_tich",
    "dien tich dat": "dien_tich_dat", "dt dat": "dien_tich_dat", "land area": "dien_tich_dat",
    "dien tich nha": "dien_tich_nha", "dt nha": "dien_tich_nha", "dt su dung": "dien_tich_nha",
    "dt xay dung": "dien_tich_nha", "floor area": "dien_tich_nha",
    "gia": "gia", "price": "gia", "gia ban": "gia",
    "ket cau": "ket_cau", "so phong ngu": "ket_cau", "phong ngu": "ket_cau", "so tang": "ket_cau",
    "du an": "du_an", "project": "du_an",
    "ma so can": "ma_so_can", "ma can": "ma_so_can", "so can": "ma_so_can",
    "can so": "ma_so_can", "unit code": "ma_so_can", "ma can ho": "ma_so_can",
    "mau nha": "unit_type", "loai can": "unit_type", "unit type": "unit_type",
    "lo gioi": "lo_gioi_hem", "hem": "lo_gioi_hem",
    "phap ly": "phap_ly_hoan_cong", "so": "phap_ly_hoan_cong", "so hong": "phap_ly_hoan_cong",
    "huong": "huong",
}

# Type-name (value) → loai_hinh slug.
TYPE_NAME_TO_SLUG: dict[str, str] = {
    "nha pho": "nha_pho", "nhà phố": "nha_pho",
    "biet thu": "biet_thu", "villa": "biet_thu", "biệt thự": "biet_thu",
    "chung cu": "chung_cu", "can ho": "chung_cu", "căn hộ": "chung_cu", "chung cư": "chung_cu",
    "nha cap 4": "nha_cap_4",
    "dat": "dat_tho_cu", "dat tho cu": "dat_tho_cu", "đất": "dat_tho_cu", "dat nen": "dat_du_an",
    "kho": "kho_bai", "kho bai": "kho_bai",
    "nha xuong": "nha_xuong", "nhà xưởng": "nha_xuong",
}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s or "").casefold().replace("đ", "d")  # đ isn't a decomposable accent
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")  # strip accents
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s)).strip()


def csv_headers(text: str) -> list[str]:
    return next(csv.reader(io.StringIO(text)), [])


def auto_header_map(headers: list[str]) -> dict[str, str]:
    """Deterministic {header → field slug} for recognized columns (exact header keys)."""
    return {h: COLUMN_ALIASES[_norm(h)] for h in headers if _norm(h) in COLUMN_ALIASES}


def rows_from_mapping(text: str, header_to_slug: dict[str, str]) -> list[dict[str, str]]:
    """Parse CSV rows into {field_slug: value} using an explicit header→slug mapping."""
    rows: list[dict[str, str]] = []
    for raw in csv.DictReader(io.StringIO(text)):
        row: dict[str, str] = {}
        for header, value in raw.items():
            slug = header_to_slug.get(header)
            if not slug:  # unmapped column, or the restkey (None) holding a ragged row's extras
                continue
            if isinstance(value, list):  # more cells than headers (e.g. an unquoted "3,2 tỷ")
                value = " ".join(v for v in value if v)
            value = (value or "").strip()
            if not value:
                continue
            if slug == "loai_hinh":
                value = TYPE_NAME_TO_SLUG.get(_norm(value), value)
            row[slug] = value
        if row:
            rows.append(row)
    return rows


def parse_csv(text: str) -> list[dict[str, str]]:
    """Parse CSV into rows of {field_slug: value} using deterministic column mapping."""
    return rows_from_mapping(text, auto_header_map(csv_headers(text)))


def sniff_kind(data: bytes, media_type: str, url: str) -> str | None:
    """Guess a fetched file's kind: "xlsx", "csv", or None (not a spreadsheet — e.g. image/PDF).
    So a pasted link can auto-route without the seller typing a command."""
    mt = (media_type or "").lower()
    u = (url or "").lower()
    if data[:4] == b"PK\x03\x04":  # a zip — xlsx (or docx/pptx/…)
        try:
            names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        except Exception:
            return None
        return "xlsx" if any(n.startswith("xl/") for n in names) else None
    if "csv" in mt or u.endswith(".csv") or "spreadsheet" in mt or "excel" in mt:
        return "csv"
    if "html" in mt:
        return None
    try:  # heuristic: decodable text whose header row has a comma → CSV
        first = data.decode("utf-8-sig", "strict").splitlines()[0] if data else ""
    except UnicodeDecodeError:
        return None
    return "csv" if ("," in first and len(first) < 2000) else None


def sheets_export_url(url: str) -> str | None:
    """A Google Sheets link → its CSV export URL (needs the sheet shared 'anyone with link')."""
    m = re.search(r"docs\.google\.com/spreadsheets/d/([\w-]+)", url)
    if not m:
        return None
    g = re.search(r"[#&?]gid=(\d+)", url)
    return (f"https://docs.google.com/spreadsheets/d/{m.group(1)}"
            f"/export?format=csv&gid={g.group(1) if g else '0'}")


def xlsx_to_csv(data: bytes) -> str:
    """Convert an Excel (.xlsx) file's active sheet to CSV text (first sheet)."""
    import openpyxl  # lazy: only needed for Excel imports

    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    out = io.StringIO()
    writer = csv.writer(out)
    for row in wb.active.iter_rows(values_only=True):
        writer.writerow(["" if c is None else str(c) for c in row])
    return out.getvalue()
