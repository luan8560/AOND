"""Projects (condos / developments) and their shared attributes.

A project carries facts that belong to EVERY property in it — pool, gym, entrance
gate, security, access road, location. Register a project once; when a lister then
lists a property whose `du_an` (project) matches, the property INHERITS these facts,
so AOND can advise about the pool/gate without asking and without inventing (project
data is operator-verified ground truth).

In-memory for now, behind a `ProjectStore` interface so SharePoint / a DB can back it
later ("Cắm Rút"), same as the LLM provider. See [[aioi-data-model]] (Project→UnitType→Unit).
"""
from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass, field


# A contested value SETTLES (stops being flagged) once it wins decisively: AT LEAST
# SETTLE_N distinct-seller votes AND the opposing values together stay AT MOST SETTLE_N/3.
# n=6 → a fact is confirmed at ≥6 sellers with ≤2 dissenters (user-set 2026-07-01).
SETTLE_N = 6

# `votes[key][value]` is the LIST OF DISTINCT SELLER IDs that asserted this value — never a
# raw counter — so one seller repeating themselves can't inflate the tally ("no phantom votes").
Tally = dict[str, list[str]]  # value -> voter ids


def _count(tally: Tally, value: str) -> int:
    return len(tally.get(value, []))


def _leading(tally: Tally) -> str:
    """Value with the most distinct-seller votes; ties keep the earliest-seen value."""
    return max(tally, key=lambda v: len(tally[v]))


def _is_settled(tally: Tally) -> bool:
    """True once one value wins decisively (see SETTLE_N) — the bot may stop asking."""
    if not tally:
        return False
    lead = _leading(tally)
    opposing = sum(len(vs) for v, vs in tally.items() if v != lead)
    return len(tally[lead]) >= SETTLE_N and opposing <= SETTLE_N / 3


def merge_facts(
    current: dict[str, str],
    votes: dict[str, Tally],
    new: dict[str, str],
    voter: str = "?",
) -> None:
    """Record each seller's assertions as VOTES keyed by seller identity. A seller gets ONE
    vote per key: re-asserting the same value is a no-op (no phantom votes), asserting a
    different value MOVES their vote (they changed their mind). `current[key]` tracks the
    LEADING value for inheritance; disagreements surface via `contested_keys` until they
    settle. Pass `voter` = the seller's id (user_key)."""
    for key, value in new.items():
        value = (value or "").strip()
        if not value:
            continue
        tally = votes.setdefault(key, {})
        for v, voters in tally.items():  # one vote per seller: drop their prior pick
            if v != value and voter in voters:
                voters.remove(voter)
        voters = tally.setdefault(value, [])
        if voter not in voters:
            voters.append(voter)
        for v in [v for v, vs in tally.items() if not vs]:  # prune emptied values
            del tally[v]
        current[key] = _leading(tally)


def _contested(votes: dict[str, Tally]) -> list[str]:
    """Keys sellers DISAGREE on and that haven't SETTLED yet — worth verifying."""
    return [
        k for k, tally in votes.items()
        if len([v for v in tally if tally[v]]) > 1 and not _is_settled(tally)
    ]


def _fact_lines(values: dict[str, str], votes: dict[str, Tally]) -> list[str]:
    lines = []
    for key, value in values.items():
        if not value:
            continue
        tally = votes.get(key, {})
        distinct = [v for v in tally if tally[v]]
        if len(distinct) > 1 and not _is_settled(tally):  # unsettled → leader + tally + flag
            detail = " / ".join(
                f"{v}×{_count(tally, v)}" for v in sorted(distinct, key=lambda v: -_count(tally, v))
            )
            lines.append(f"{key}: {value} ⚠️ CHƯA CHẮC ({detail}) — hỏi khách để xác minh")
        else:
            lines.append(f"{key}: {value}")
    return lines


@dataclass
class UnitType:
    """A house model / unit type within a project (e.g. "Fusion Gen 5", "Type B 3PN").
    Units of this type share ~99% of these facts (layout, areas, số phòng...)."""

    name: str
    specs: dict[str, str] = field(default_factory=dict)  # diện tích, số phòng, số tầng, layout...
    votes: dict[str, Tally] = field(default_factory=dict)  # per-spec voters {value: [seller ids]}
    media: list[dict[str, str]] = field(default_factory=list)  # model brochure/floor-plan/renders
    # (SharePoint MediaRef dicts) — shared by EVERY unit of this model; see [[aioi-data-model]].

    def facts(self) -> list[str]:
        return _fact_lines(self.specs, self.votes)

    def contested_keys(self) -> list[str]:
        return _contested(self.votes)


@dataclass
class Unit:
    """A PHYSICAL unit in a project's inventory — a lô in a phân-lô development, or a căn in a
    condo. Identity = `ma_so_can` within the project. `attrs` is a FREE-FORM attribute bag
    (key→value) so ONE structure fits ANY property type: a phân-lô unit carries
    dien_tich_dat/mat_tien/vi_tri, a condo unit carries dien_tich/huong/view/tang — same shape,
    different keys, nothing hard-coded to houses. Each attribute tracks votes so a fact learned
    from one seller is shared with the next and conflicts settle (see [[aioi-data-model]]). This
    is the PROJECT's inventory (all units), distinct from a seller's basket (the units they rao)."""

    ma_so_can: str
    attrs: dict[str, str] = field(default_factory=dict)
    votes: dict[str, Tally] = field(default_factory=dict)  # per-attribute voters {value: [seller ids]}
    loai_hinh: str = ""  # nha_pho / chung_cu / ... — which attribute set is meaningful

    def learn(self, new: dict[str, str], voter: str = "?") -> None:
        """Record what a seller told us about this unit as VOTES (shared knowledge that grows)."""
        merge_facts(self.attrs, self.votes, {k: v for k, v in new.items() if k != "ma_so_can"}, voter)

    def known(self) -> dict[str, str]:
        return {"ma_so_can": self.ma_so_can, **{k: v for k, v in self.attrs.items() if v}}


@dataclass
class Project:
    name: str
    amenities: list[str] = field(default_factory=list)       # hồ bơi, gym, công viên...
    attributes: dict[str, str] = field(default_factory=dict)  # cổng, an ninh, đường vào, vị trí...
    unit_types: list[UnitType] = field(default_factory=list)  # mẫu nhà / loại căn của dự án
    votes: dict[str, Tally] = field(default_factory=dict)  # per-attribute voters {value: [seller ids]}
    docs: list[dict[str, str]] = field(default_factory=list)  # source docs read ONCE, kept as
    # faithful MARKDOWN for the model to re-read cheaply (grounding): {kind, title, markdown,
    # web_url, item_id, fetched_at}. See [[aioi-data-model]] — phân lô, brochure mẫu nhà,
    # bảng mô tả gói nội thất... loaded once, reused forever.
    units: list[Unit] = field(default_factory=list)  # the project's INVENTORY (each mã số căn +
    # its free-form attribute bag). Seeded from a price list / phân lô; grows as sellers tell us
    # more. Queried by the 4 plain functions in aioi.inventory (the substrate for the agent loop).

    def find_unit(self, ma_so_can: str | None) -> Unit | None:
        key = normalize_project_name(ma_so_can or "")
        if not key:
            return None
        return next((u for u in self.units if normalize_project_name(u.ma_so_can) == key), None)

    def upsert_unit(self, ma_so_can: str, attrs: dict[str, str], voter: str = "?",
                    loai_hinh: str = "") -> Unit:
        """Add the unit if new (mã số căn is the key), else MERGE the attrs into the existing
        one via votes — a project can't hold the same physical căn twice."""
        u = self.find_unit(ma_so_can)
        if u is None:
            u = Unit(ma_so_can=ma_so_can, loai_hinh=loai_hinh)
            self.units.append(u)
        if loai_hinh and not u.loai_hinh:
            u.loai_hinh = loai_hinh
        u.learn(attrs, voter)
        return u

    def doc(self, kind: str | None = None, title: str | None = None) -> dict[str, str] | None:
        """The most recent stored source doc matching `kind`/`title` (for grounded re-reads)."""
        for d in reversed(self.docs):
            if (kind is None or d.get("kind") == kind) and (title is None or d.get("title") == title):
                return d
        return None

    def facts(self) -> list[str]:
        """Human-readable lines of the project's shared facts, for prompts/inheritance."""
        lines: list[str] = []
        if self.amenities:
            lines.append("Tiện ích: " + ", ".join(self.amenities))
        return lines + _fact_lines(self.attributes, self.votes)

    def contested_keys(self) -> list[str]:
        """Attributes sellers disagree on — the bot should find a chance to verify these."""
        return _contested(self.votes)

    def find_unit_type(self, name: str | None) -> UnitType | None:
        if not name:
            return None
        key = find_matching_key(
            normalize_project_name(name), [normalize_project_name(u.name) for u in self.unit_types]
        )
        if key is None:
            return None
        return next(u for u in self.unit_types if normalize_project_name(u.name) == key)

    def upsert_unit_type(self, unit_type: UnitType) -> None:
        for i, existing in enumerate(self.unit_types):
            if normalize_project_name(existing.name) == normalize_project_name(unit_type.name):
                self.unit_types[i] = unit_type  # update in place
                return
        self.unit_types.append(unit_type)


def normalize_project_name(name: str) -> str:
    """Case/space/punctuation-insensitive key for matching project & unit-type names.
    Drops punctuation so "Fusion (Gen 5)" matches "Fusion Gen 5"."""
    s = unicodedata.normalize("NFKC", name or "").casefold()
    s = re.sub(r"[^\w\s]", " ", s)  # punctuation (parens, dashes, ...) → space
    return re.sub(r"\s+", " ", s).strip()


# Generic development-type words that sellers add/drop in front of the REAL project name —
# "Khu Nhà Biệt Lập Ny'ah Phú Định" and "Ny'ah Phú Định" are the same place. Stripped only as a
# LEADING run so a name that is *only* these words isn't erased, and interior words are untouched.
_GENERIC_PREFIX = (
    "khu nhà biệt lập", "khu biệt lập", "khu nhà ở", "khu dân cư", "khu đô thị",
    "khu căn hộ", "khu chung cư", "khu nhà", "dự án", "khu",
)


def _strip_generic_prefix(name: str) -> str:
    changed = True
    while changed:
        changed = False
        for pre in _GENERIC_PREFIX:
            if name == pre:
                return name  # the name is ONLY generic words — don't erase it
            if name.startswith(pre + " "):
                name = name[len(pre) + 1:]
                changed = True
                break
    return name


def find_matching_key(query: str, stored_keys: Iterable[str]) -> str | None:
    """Match a normalized query name to a stored normalized key: exact first, then a
    lenient prefix match with a small length diff ("Botanic Tower" ↔ "Botanic Towers"),
    while a generic partial ("Masteri") won't grab "Masteri Thảo Điền".

    Also collapses the two ways sellers unintentionally spawn a DUPLICATE project for the same
    place: (1) apostrophe/space noise — "Ny'ah Phú Định" normalizes to "ny ah phú định" while
    "Nyah Phú Định" → "nyah phú định"; comparing SPACELESS makes them equal. (2) a leading
    development-type phrase — "Khu Nhà Biệt Lập Ny'ah Phú Định" vs "Ny'ah Phú Định". Both checks
    are exact-after-cleaning (not fuzzy), so genuinely different projects don't get merged."""
    keys = list(stored_keys)
    if query in keys:
        return query
    for k in keys:
        if (k.startswith(query) or query.startswith(k)) and abs(len(k) - len(query)) <= 3:
            return k
    # spaceless + generic-prefix-stripped exact match (catches the duplicate-project variants)
    def canon(s: str) -> str:
        return _strip_generic_prefix(s).replace(" ", "")
    q = canon(query)
    if q:
        for k in keys:
            if canon(k) == q:
                return k
    return None


class ProjectStore(ABC):
    @abstractmethod
    def save(self, project: Project) -> None: ...

    @abstractmethod
    def get(self, name: str | None) -> Project | None: ...

    @abstractmethod
    def all(self) -> list[Project]: ...


class InMemoryProjectStore(ProjectStore):
    def __init__(self) -> None:
        self._by_name: dict[str, Project] = {}

    def save(self, project: Project) -> None:
        self._by_name[normalize_project_name(project.name)] = project

    def get(self, name: str | None) -> Project | None:
        if not name:
            return None
        key = find_matching_key(normalize_project_name(name), self._by_name)
        return self._by_name.get(key) if key else None

    def all(self) -> list[Project]:
        return list(self._by_name.values())


class SqliteProjectStore(ProjectStore):
    """Persistent ProjectStore — projects survive restarts and are reused for later
    listings (same interface as InMemoryProjectStore; the first piece of the Store layer).
    SQLite locally; a SharePoint/Postgres backend can replace it behind ProjectStore."""

    def __init__(self, path: str = "aioi.db"):
        self._conn = sqlite3.connect(path)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS projects ("
            "key TEXT PRIMARY KEY, name TEXT NOT NULL, amenities TEXT NOT NULL, "
            "attributes TEXT NOT NULL, unit_types TEXT NOT NULL DEFAULT '[]', "
            "votes TEXT NOT NULL DEFAULT '{}', docs TEXT NOT NULL DEFAULT '[]', "
            "units TEXT NOT NULL DEFAULT '[]')"
        )
        for col, default in (("unit_types", "'[]'"), ("votes", "'{}'"), ("docs", "'[]'"),
                             ("units", "'[]'")):
            try:  # migrate older tables that predate a column
                self._conn.execute(f"ALTER TABLE projects ADD COLUMN {col} TEXT NOT NULL DEFAULT {default}")
            except sqlite3.OperationalError:
                pass  # column already exists
        self._conn.commit()

    @staticmethod
    def _row_to_project(row) -> Project:
        units_types = [
            UnitType(u["name"], u.get("specs", {}), u.get("votes", {}), u.get("media", []))
            for u in json.loads(row[3])
        ]
        docs = json.loads(row[5]) if len(row) > 5 and row[5] else []
        units = [
            Unit(u["ma_so_can"], u.get("attrs", {}), u.get("votes", {}), u.get("loai_hinh", ""))
            for u in (json.loads(row[6]) if len(row) > 6 and row[6] else [])
        ]
        return Project(row[0], json.loads(row[1]), json.loads(row[2]), units_types,
                       json.loads(row[4]), docs, units)

    def save(self, project: Project) -> None:
        self._conn.execute(
            "INSERT INTO projects(key, name, amenities, attributes, unit_types, votes, docs, units) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET name=excluded.name, "
            "amenities=excluded.amenities, attributes=excluded.attributes, "
            "unit_types=excluded.unit_types, votes=excluded.votes, docs=excluded.docs, "
            "units=excluded.units",
            (
                normalize_project_name(project.name),
                project.name,
                json.dumps(project.amenities, ensure_ascii=False),
                json.dumps(project.attributes, ensure_ascii=False),
                json.dumps(
                    [{"name": u.name, "specs": u.specs, "votes": u.votes, "media": u.media}
                     for u in project.unit_types],
                    ensure_ascii=False,
                ),
                json.dumps(project.votes, ensure_ascii=False),
                json.dumps(project.docs, ensure_ascii=False),
                json.dumps(
                    [{"ma_so_can": u.ma_so_can, "attrs": u.attrs, "votes": u.votes,
                      "loai_hinh": u.loai_hinh} for u in project.units],
                    ensure_ascii=False,
                ),
            ),
        )
        self._conn.commit()

    _COLS = "name, amenities, attributes, unit_types, votes, docs, units"

    def get(self, name: str | None) -> Project | None:
        if not name:
            return None
        keys = [r[0] for r in self._conn.execute("SELECT key FROM projects")]
        key = find_matching_key(normalize_project_name(name), keys)
        if key is None:
            return None
        row = self._conn.execute(
            f"SELECT {self._COLS} FROM projects WHERE key=?", (key,)
        ).fetchone()
        return self._row_to_project(row)

    def all(self) -> list[Project]:
        rows = self._conn.execute(f"SELECT {self._COLS} FROM projects")
        return [self._row_to_project(r) for r in rows]
