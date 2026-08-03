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
    # The raw heading the directory used, tidied into one of ~83 canonical
    # labels. Derived on read (see scrapbot.sports), so it is never stored and
    # can never go stale against the merge rules.
    "sport_canonical",
    "department",
    "emails",
    "phones",
    "school",
    "school_domain",
    "profile_url",
    "source_url",
    "photo_url",
    "photo_file",
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
    "country",
    "division",
    "conference",
    "region",
    "totalYearlyCost",
    "academicData",
    "privatePublic",
]

ACADEMIC_KEYS = ["SATMath", "averageGPA", "ACTComposite", "SATReady"]


@dataclass
class SiteOutcome:
    """What happened to one site in a run, so failures are never invisible.

    A site that blocks bots and a site that simply has no staff directory both
    yield zero people; without a reason they are indistinguishable, and a run
    that quietly returned nothing looks like a run that found nothing.
    """

    OK: ClassVar[str] = "ok"
    EMPTY: ClassVar[str] = "empty"
    BLOCKED: ClassVar[str] = "blocked"
    ROBOTS: ClassVar[str] = "robots"
    NETWORK: ClassVar[str] = "network"
    NO_DIRECTORY: ClassVar[str] = "no_directory"
    ERROR: ClassVar[str] = "error"

    # Everything but OK/EMPTY is worth retrying once conditions change.
    RETRYABLE: ClassVar[frozenset[str]] = frozenset({"blocked", "network", "error"})

    domain: str
    status: str
    detail: str = ""
    people: int = 0
    url: str | None = None
    school: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == self.OK

    @property
    def retryable(self) -> bool:
        return self.status in self.RETRYABLE

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# How each outcome reads in the run summary.
OUTCOME_LABELS = {
    SiteOutcome.OK: "scraped",
    SiteOutcome.EMPTY: "directory found but no people parsed",
    SiteOutcome.BLOCKED: "blocked the bot (HTTP 403/429)",
    SiteOutcome.ROBOTS: "disallowed by robots.txt",
    SiteOutcome.NETWORK: "network failure / timeout",
    SiteOutcome.NO_DIRECTORY: "no staff directory found",
    SiteOutcome.ERROR: "unhandled error",
}


# Associations whose name *is* the division value, because they have no tiers
# the way NCAA I/II/III does. CCCAA (California) and NWAC (Pacific Northwest)
# are separate governing bodies from the NJCAA, not conferences within it.
SELF_NAMED_DIVISIONS = {"NAIA", "NJCAA", "CCCAA", "NWAC"}


def normalize_division(value: str) -> str:
    """Accept ``I``/``d1``/``DIII``/``naia`` and return the stored form.

    The NAIA is a division value in its own right, so it must not get the
    ``D`` prefix the NCAA tiers use.
    """
    token = " ".join((value or "").strip().upper().split())
    if not token:
        return token
    if token in SELF_NAMED_DIVISIONS:
        return token
    if token == "DNAIA":  # an older run put the NCAA "D" prefix on the NAIA
        return "NAIA"
    if token.startswith("NJCAA"):
        # The NJCAA tier is dropped on purpose: it is a per-sport designation,
        # not a property of the college. A junior college routinely plays DI
        # basketball and DII baseball, so 29 of them appeared in two of the
        # source lists and ended up stored as "NJCAA DI; NJCAA DII" — a value
        # no filter matched. One "NJCAA" per college is the honest shape.
        return "NJCAA"
    return token if token.startswith("D") else f"D{token}"


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
    source_url: str | None = None
    """The page this record was read from — the staff directory itself, not the
    person's own bio. ``profile_url`` answers "where is this coach's page";
    this answers "where did this row come from", which is what you need to
    check a value or re-read the source by hand. They are usually different
    pages and one is often absent: a table row may carry no bio link at all,
    and a record imported from a supplied list has no directory behind it."""
    photo_url: str | None = None
    """Headshot published on the staff directory. Stored as a URL; the file is
    only downloaded when the operator asks for it with ``--save-photos``."""
    photo_file: str | None = None
    """Path of the downloaded headshot, relative to the data directory."""
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
        # Computed into this local copy, not into to_dict(), so the CSV gains a
        # column while contacts.json keeps only what was actually scraped.
        # Imported here because scrapbot.sports reads the coaches vocabulary,
        # which imports this module.
        from .sports import canonical_field

        d["sport_canonical"] = canonical_field(self.sport)

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
        for name in (
            "name", "school", "title", "profile_url", "source_url",
            "photo_url", "photo_file",
        ):
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
    """US state or province."""
    country: str | None = None
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
    association: str | None = None
    """NCAA / NAIA / NJCAA. Part of the identity: Cottey College and Marian
    University each appear in both the NAIA and NJCAA lists as different
    institutions, and would otherwise overwrite one another."""
    source: str = "unknown"
    first_seen: str = field(default_factory=_utcnow)
    last_seen: str = field(default_factory=_utcnow)
    notes: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        if self.ncaa_org_id is not None:
            return f"ncaa:{self.ncaa_org_id}"
        # Otherwise the name alone is not unique — there are two Bethel
        # Universities, two Columbia Colleges and two Universities of Saint
        # Francis, each pair in a different state. Qualify the slug with
        # wherever the school is.
        slug = re.sub(r"[^a-z0-9]+", "-", self.school.lower()).strip("-")
        where = (self.state or self.country or "").lower().replace(" ", "-")
        assoc = (self.association or "").lower()
        return "|".join(part for part in (slug, where, assoc) if part)

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
        school = cls(**{k: v for k, v in raw.items() if k in known})
        if not school.association:
            # Records written before `association` existed: derive it so their
            # key stays stable instead of splitting into a duplicate row.
            school.association = _association_for(school)
        return school

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
            "school", "city", "state", "country", "conference", "region",
            "totalYearlyCost", "privatePublic", "athletics_domain", "website",
            "ncaa_org_id", "association",
        ):
            new = getattr(other, name)
            if new:
                setattr(merged, name, new)
        # An NJCAA college can play at different division levels in different
        # sports, so it appears in more than one division list. Keep both
        # rather than letting the last one win.
        merged.division = _join_unique(merged.division, other.division)
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


def _association_for(school: "School") -> str | None:
    """Infer the association from a record that predates the field."""
    if school.ncaa_org_id is not None:
        return "NCAA"
    division = (school.division or "").upper()
    if division.startswith("NJCAA"):
        return "NJCAA"
    if division in SELF_NAMED_DIVISIONS:
        return division
    if division:
        return "NCAA"
    return None  # non-athletic record, e.g. an academic-only institution


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
