"""The record shape scrapbot produces."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, ClassVar

# Column order used for CSV export and for the dashboard table.
CSV_COLUMNS = [
    "domain",
    "company_name",
    "url",
    "description",
    "emails",
    "phones",
    "location",
    "socials",
    "industry_hints",
    "careers_url",
    "has_open_roles",
    "pages_crawled",
    "source",
    "first_seen",
    "last_seen",
]

# Column order for the person-level store written by the ``coaches`` source.
CONTACT_CSV_COLUMNS = [
    "name",
    "title",
    "sport",
    "department",
    "emails",
    "phones",
    "school",
    "school_domain",
    "profile_url",
    "is_coach",
    "shared_email",
    "source",
    "first_seen",
    "last_seen",
]


# The origin database's school shape, in its own field order. ``id`` and
# ``logo`` are deliberately absent — those are assigned on your side, and the
# scraper must never invent them.
SCHOOL_COLUMNS = [
    "school",
    "city",
    "state",
    "division",
    "conference",
    "region",
    "totalYearlyCost",
    "academicData",
    "privatePublic",
]

ACADEMIC_KEYS = ["SATMath", "averageGPA", "ACTComposite", "SATReady"]


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Lead:
    """One company. ``domain`` is the de-duplication key."""

    COLUMNS: ClassVar[list[str]] = CSV_COLUMNS

    domain: str
    company_name: str | None = None
    url: str | None = None
    description: str | None = None
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    location: str | None = None
    socials: dict[str, str] = field(default_factory=dict)
    industry_hints: list[str] = field(default_factory=list)
    careers_url: str | None = None
    has_open_roles: bool | None = None
    pages_crawled: int = 0
    source: str = "unknown"
    first_seen: str = field(default_factory=_utcnow)
    last_seen: str = field(default_factory=_utcnow)
    notes: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return self.domain.lower()

    @property
    def label(self) -> str:
        """What the run log shows in its first column."""
        return self.domain

    @property
    def sublabel(self) -> str:
        return self.company_name or ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Lead":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in raw.items() if k in known})

    def to_row(self) -> dict[str, str]:
        """Flatten to strings for CSV."""
        d = self.to_dict()
        row: dict[str, str] = {}
        for col in self.COLUMNS:
            val = d.get(col)
            if isinstance(val, list):
                row[col] = "; ".join(str(v) for v in val)
            elif isinstance(val, dict):
                row[col] = "; ".join(f"{k}={v}" for k, v in val.items())
            elif val is None:
                row[col] = ""
            else:
                row[col] = str(val)
        return row

    def merge(self, other: "Lead") -> "Lead":
        """Fold a freshly scraped ``other`` into ``self`` (the stored record).

        Scalars prefer the newer non-empty value; collections are unioned so
        contact details found on an earlier run are never lost.
        """
        merged = Lead.from_dict(self.to_dict())
        for name in ("company_name", "url", "description", "location", "careers_url"):
            new = getattr(other, name)
            if new:
                setattr(merged, name, new)
        if other.has_open_roles is not None:
            merged.has_open_roles = other.has_open_roles
        merged.emails = _union(merged.emails, other.emails)
        merged.phones = _union(merged.phones, other.phones)
        merged.industry_hints = _union(merged.industry_hints, other.industry_hints)
        merged.notes = _union(merged.notes, other.notes)
        merged.socials = {**merged.socials, **other.socials}
        merged.pages_crawled = max(merged.pages_crawled, other.pages_crawled)
        merged.source = other.source or merged.source
        merged.first_seen = min(merged.first_seen, other.first_seen)
        merged.last_seen = max(merged.last_seen, other.last_seen)
        return merged


@dataclass
class Contact:
    """One person at an institution — a coach, or other listed staff.

    Unlike :class:`Lead`, which is one record per organisation, this is one
    record per human. The de-duplication key prefers the staff profile URL
    (stable, unique) and falls back to school + name.

    Only professional, publicly published contact details belong here: the
    name, work title, work email and work phone an institution puts on its own
    staff directory. Nothing personal.
    """

    COLUMNS: ClassVar[list[str]] = CONTACT_CSV_COLUMNS

    name: str
    school_domain: str
    school: str | None = None
    title: str | None = None
    sport: str | None = None
    department: str | None = None
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    profile_url: str | None = None
    is_coach: bool = False
    shared_email: bool = False
    """The listed address belongs to a gatekeeper or a shared inbox — it is on
    several people's rows at this school, so it does not reach this person
    directly. Duke lists a head coach's executive assistant this way."""
    source: str = "unknown"
    first_seen: str = field(default_factory=_utcnow)
    last_seen: str = field(default_factory=_utcnow)
    notes: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        if self.profile_url:
            return self.profile_url.split("?")[0].rstrip("/").lower()
        slug = re.sub(r"[^a-z0-9]+", "-", self.name.lower()).strip("-")
        return f"{self.school_domain.lower()}|{slug}"

    @property
    def label(self) -> str:
        return self.name

    @property
    def sublabel(self) -> str:
        return self.title or ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Contact":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in raw.items() if k in known})

    def to_row(self) -> dict[str, str]:
        d = self.to_dict()
        row: dict[str, str] = {}
        for col in self.COLUMNS:
            val = d.get(col)
            if isinstance(val, list):
                row[col] = "; ".join(str(v) for v in val)
            elif isinstance(val, dict):
                row[col] = "; ".join(f"{k}={v}" for k, v in val.items())
            elif val is None:
                row[col] = ""
            else:
                row[col] = str(val)
        return row

    def merge(self, other: "Contact") -> "Contact":
        """Same policy as :meth:`Lead.merge` — newest scalar wins, lists union."""
        merged = Contact.from_dict(self.to_dict())
        for name in ("name", "school", "title", "profile_url"):
            new = getattr(other, name)
            if new:
                setattr(merged, name, new)
        # A person is often listed twice — once under Senior Administration and
        # again under the sport they actually work with, or under both Cross
        # Country and Track & Field. Overwriting would throw one away.
        merged.sport = _join_unique(merged.sport, other.sport)
        merged.department = _join_unique(merged.department, other.department)
        merged.emails = _union(merged.emails, other.emails)
        merged.phones = _union(merged.phones, other.phones)
        merged.notes = _union(merged.notes, other.notes)
        merged.is_coach = merged.is_coach or other.is_coach
        merged.shared_email = merged.shared_email or other.shared_email
        merged.source = other.source or merged.source
        merged.first_seen = min(merged.first_seen, other.first_seen)
        merged.last_seen = max(merged.last_seen, other.last_seen)
        return merged


@dataclass
class School:
    """One institution, in the origin database's shape.

    ``athletics_domain`` is the join to :class:`Contact` — it is the host the
    ``coaches`` source scrapes, so a coach's ``school_domain`` points back here.
    It and the timestamps are bookkeeping: :meth:`to_origin_dict` emits only the
    origin schema.
    """

    COLUMNS: ClassVar[list[str]] = SCHOOL_COLUMNS

    school: str
    city: str | None = None
    state: str | None = None
    division: str | None = None
    conference: str | None = None
    region: str | None = None
    totalYearlyCost: str | None = None
    academicData: dict[str, str] = field(default_factory=dict)
    privatePublic: str | None = None
    # --- bookkeeping, not part of the origin schema ---
    athletics_domain: str | None = None
    website: str | None = None
    ncaa_org_id: int | None = None
    source: str = "unknown"
    first_seen: str = field(default_factory=_utcnow)
    last_seen: str = field(default_factory=_utcnow)
    notes: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        if self.ncaa_org_id is not None:
            return f"ncaa:{self.ncaa_org_id}"
        return re.sub(r"[^a-z0-9]+", "-", self.school.lower()).strip("-")

    @property
    def label(self) -> str:
        return self.school

    @property
    def sublabel(self) -> str:
        return self.conference or ""

    # The runner logs emails/phones for every record type; a school has none.
    @property
    def emails(self) -> list[str]:
        return []

    @property
    def phones(self) -> list[str]:
        return []

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_origin_dict(self) -> dict[str, Any]:
        """Exactly the origin schema — no bookkeeping, no ``id``, no ``logo``."""
        record: dict[str, Any] = {}
        for col in SCHOOL_COLUMNS:
            value = getattr(self, col)
            if col == "academicData":
                record[col] = {k: value[k] for k in ACADEMIC_KEYS if value.get(k)}
            else:
                record[col] = value
        return record

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "School":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in raw.items() if k in known})

    def to_row(self) -> dict[str, str]:
        row: dict[str, str] = {}
        for col in self.COLUMNS:
            value = getattr(self, col)
            if col == "academicData":
                row[col] = "; ".join(
                    f"{k}={value[k]}" for k in ACADEMIC_KEYS if value.get(k)
                )
            else:
                row[col] = "" if value is None else str(value)
        return row

    def merge(self, other: "School") -> "School":
        merged = School.from_dict(self.to_dict())
        for name in (
            "school", "city", "state", "division", "conference", "region",
            "totalYearlyCost", "privatePublic", "athletics_domain", "website",
            "ncaa_org_id",
        ):
            new = getattr(other, name)
            if new:
                setattr(merged, name, new)
        # Never let a newer empty lookup blank out an academic value we already
        # hold — test-optional years return nulls for schools that once reported.
        merged.academicData = {
            **merged.academicData,
            **{k: v for k, v in other.academicData.items() if v},
        }
        merged.notes = _union(merged.notes, other.notes)
        merged.source = other.source or merged.source
        merged.first_seen = min(merged.first_seen, other.first_seen)
        merged.last_seen = max(merged.last_seen, other.last_seen)
        return merged


def _join_unique(a: str | None, b: str | None) -> str | None:
    """Combine two scalar labels into ``"one; two"``, dropping repeats."""
    parts: dict[str, None] = {}
    for value in (a, b):
        for piece in (value or "").split(";"):
            piece = piece.strip()
            if piece:
                parts.setdefault(piece, None)
    return "; ".join(parts) or None


def _union(a: list[str], b: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for item in list(a) + list(b):
        if item:
            seen.setdefault(item, None)
    return list(seen)
