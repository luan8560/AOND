"""The project-inventory query layer — 4 plain, DETERMINISTIC functions over a project's
`units` (see `projects.Unit`). No LLM, no loop: this is the substrate the agent loop (a later
step) will expose as tools. Everything is ATTRIBUTE-AGNOSTIC — it operates on arbitrary
attribute KEYS, so the same functions serve a phân-lô development (dien_tich_dat / mat_tien /
vi_tri) and a condo (dien_tich / huong / view / tang) with no property-type branching. The
model, in the loop, maps a fuzzy human criterion ("căn góc lớn nhất", "2PN view hồ tầng cao")
to these calls; the functions just answer truthfully from what's known.

    lookup(project, code)                  → what we know about ONE unit (or None)
    find(project, where=..., largest=...)  → units matching filters / the extreme of an attribute
    remember(project, code, attrs, voter)  → LEARN a fact about a unit (shared, vote-tracked)
    coverage(project)                      → what we know vs DON'T (so AOND can ask for the gap)
"""
from __future__ import annotations

import re

from .projects import Project, Unit, normalize_project_name


def _num(value: str | None) -> float | None:
    """Best-effort number out of a messy value ("43.75m²"→43.75, "8,98 tỷ"→8.98, "12"→12,
    "16,566,000,000"→16566000000). A lone comma with ≤2 trailing digits is a decimal comma;
    otherwise commas are thousands separators. None if there's no number."""
    if not value:
        return None
    m = re.search(r"\d[\d.,]*", value)
    if not m:
        return None
    s = m.group(0).rstrip(".,")
    if s.count(",") == 1 and "." not in s and len(s.split(",")[1]) <= 2:
        s = s.replace(",", ".")   # decimal comma (Vietnamese "8,98")
    else:
        s = s.replace(",", "")    # thousands separators
    try:
        return float(s)
    except ValueError:
        return None


def _attr_matches(unit: Unit, key: str, query: str) -> bool:
    """Does the unit's attribute `key` match `query`? Normalized equality OR substring, so
    "office" matches "Office 1"/"Office 2" and "2pn" matches "2PN". Empty/unknown → no."""
    v = unit.attrs.get(key)
    if not v:
        return False
    vn, qn = normalize_project_name(v), normalize_project_name(str(query))
    return bool(qn) and (qn == vn or qn in vn)


def lookup(project: Project, ma_so_can: str) -> dict[str, str] | None:
    """Everything KNOWN about one unit (its identity + non-empty attributes), or None if the
    project has no such mã số căn."""
    u = project.find_unit(ma_so_can)
    return u.known() if u is not None else None


def find(
    project: Project,
    where: dict[str, str] | None = None,
    largest: str | None = None,
    smallest: str | None = None,
) -> list[Unit]:
    """Units matching ALL `where` equality filters (by attribute key), optionally narrowed to
    the extreme of a numeric attribute. `largest`/`smallest` name the attribute to rank by —
    units whose that attribute is UNKNOWN are excluded (we can't claim an unknown is the max),
    and ties return every unit at the extreme. No filters + no extreme → the whole inventory."""
    units = list(project.units)
    if where:
        units = [u for u in units if all(_attr_matches(u, k, v) for k, v in where.items())]
    attr = largest or smallest
    if attr:
        scored = [(u, _num(u.attrs.get(attr))) for u in units]
        scored = [(u, n) for u, n in scored if n is not None]
        if not scored:
            return []
        target = (max if largest else min)(n for _, n in scored)
        return [u for u, n in scored if n == target]
    return units


def remember(project: Project, ma_so_can: str, attrs: dict[str, str], voter: str = "?",
             loai_hinh: str = "") -> Unit:
    """LEARN facts about a unit (a seller told us) — create it if new, else merge via votes so
    knowledge is SHARED with the next seller and conflicts settle. Returns the unit. The caller
    persists the project."""
    return project.upsert_unit(ma_so_can, attrs, voter=voter, loai_hinh=loai_hinh)


def coverage(project: Project) -> dict[str, object]:
    """What AOND KNOWS vs DOESN'T about the project's inventory — so it can say "em có 9/50 lô,
    chưa có diện tích" and ask for exactly the gap instead of guessing. Reports the number of
    units on file, the seller-declared total if known (`tong_so_can` project attribute), and per
    attribute how many units have a value for it."""
    units = project.units
    attr_counts: dict[str, int] = {}
    for u in units:
        for k, v in u.attrs.items():
            if v:
                attr_counts[k] = attr_counts.get(k, 0) + 1
    return {
        "so_can_da_biet": len(units),
        "tong_so_can_khai_bao": project.attributes.get("tong_so_can"),
        "thuoc_tinh_da_biet": dict(sorted(attr_counts.items(), key=lambda kv: -kv[1])),
    }
