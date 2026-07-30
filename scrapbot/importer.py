"""Import a hand-maintained school list (``.xlsx`` or ``.csv``) into the store.

The official directories are the source of truth for *who exists*: the NCAA
publishes both the university and the athletics host for its members, but the
NAIA PDF and the NJCAA articles carry neither. That left 726 schools known by
name with no athletics site to scrape — the bot had to start from the
university homepage and guess, which is where it went wrong most often.

A spreadsheet that already pairs each school with its athletics team page fills
exactly that gap. This module only *adds* what the official sources lack; it
never overwrites a domain the NCAA published.
"""

from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from . import usregions
from .models import School, normalize_division
from .sources.website import normalize_domain

log = logging.getLogger("scrapbot.importer")

# Header text -> the field it fills. Matched case-insensitively on a squashed
# version of the header, so "Team page (link)" and "team_page" both work.
COLUMN_ALIASES = {
    "school": "school",
    "schoolname": "school",
    "name": "school",
    "institution": "school",
    "level": "level",
    "division": "level",
    "association": "level",
    "conference": "conference",
    "state": "state",
    "city": "city",
    "teampagelink": "link",
    "teampage": "link",
    "link": "link",
    "url": "link",
    "athleticsurl": "link",
    "website": "link",
}

# "NCAA Division I" / "Junior College" / "NAIA" -> (association, division)
LEVELS = {
    "ncaa division i": ("NCAA", "DI"),
    "ncaa division ii": ("NCAA", "DII"),
    "ncaa division iii": ("NCAA", "DIII"),
    "naia": ("NAIA", "NAIA"),
    # A junior college has one division value, "NJCAA", for the same reason the
    # three NJCAA articles collapse to one: the tier is per-sport, not per
    # college. Leaving it blank (the first version of this) put 241 schools in
    # no division at all, so no division filter reached them.
    "junior college": ("NJCAA", "NJCAA"),
    "njcaa": ("NJCAA", "NJCAA"),
}


# Junior-college governing bodies that are *not* the NJCAA. A supplied list
# calls all of them "Junior College", but California's CCCAA and the Pacific
# Northwest's NWAC run their own championships and eligibility rules, so filing
# their colleges as NJCAA members states something untrue. The conference column
# is what gives them away.
JUCO_BODIES = {
    "CCCAA": re.compile(r"\bCCCAA\b|California Community College Athletic", re.I),
    "NWAC": re.compile(r"\bNWAC\b|Northwest Athletic Conference", re.I),
}


def juco_body(conference: str | None) -> str | None:
    """'…(CCCAA)' -> 'CCCAA'. None when it is an ordinary NJCAA conference."""
    text = conference or ""
    for name, pattern in JUCO_BODIES.items():
        if pattern.search(text):
            return name
    return None


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def normalize_name(name: str) -> str:
    """A school name reduced to something two lists can agree on.

    "The University of Texas at Austin" and "University of Texas Austin" have to
    land on the same key, but "Bethel University" in Indiana and in Tennessee
    must not — so this is only ever used together with the state.
    """
    n = (name or "").lower().replace("&", " and ").replace("-", " ")
    n = re.sub(r"\b(the|of|at)\b", " ", n)
    n = re.sub(r"[^a-z0-9 ]+", " ", n)
    return " ".join(n.split())


@dataclass
class ImportReport:
    rows: int = 0
    unusable: int = 0
    filled: int = 0
    replaced_university_host: int = 0
    already_known: int = 0
    conflicting: list[tuple[str, str, str]] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"{self.rows} row(s) read",
            f"  athletics site filled in : {self.filled}"
            + (
                f"  ({self.replaced_university_host} replaced a university host)"
                if self.replaced_university_host
                else ""
            ),
            f"  already on record        : {self.already_known}",
            f"  disagreed with the store : {len(self.conflicting)}",
            f"  matched no stored school : {len(self.unmatched)}",
        ]
        if self.added:
            lines.append(f"  new schools added        : {len(self.added)}")
        return "\n".join(lines)


def read_rows(path: Path) -> Iterator[dict[str, Any]]:
    """Yield ``{field: value}`` per data row, from .xlsx or .csv."""
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        yield from _read_xlsx(path)
    else:
        with path.open(newline="", encoding="utf-8-sig") as fh:
            reader = csv.reader(fh)
            header = next(reader, None) or []
            mapping = _map_header(header)
            for raw in reader:
                yield _row(mapping, raw)


def _read_xlsx(path: Path) -> Iterator[dict[str, Any]]:
    try:
        import openpyxl
    except ModuleNotFoundError as exc:  # pragma: no cover - dependency guard
        raise SystemExit(
            "reading .xlsx needs openpyxl: pip install openpyxl "
            "(or save the sheet as .csv)"
        ) from exc

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        for ws in wb.worksheets:
            rows = ws.iter_rows(values_only=True)
            header = next(rows, None)
            if not header:
                continue
            mapping = _map_header([str(h or "") for h in header])
            if "school" not in mapping.values():
                continue  # not a school sheet; skip it rather than guess
            for raw in rows:
                yield _row(mapping, list(raw))
    finally:
        wb.close()


def _map_header(header: list[str]) -> dict[int, str]:
    return {
        i: COLUMN_ALIASES[_squash(h)]
        for i, h in enumerate(header)
        if _squash(h) in COLUMN_ALIASES
    }


def _row(mapping: dict[int, str], raw: list[Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for i, field_name in mapping.items():
        if i < len(raw) and raw[i] is not None:
            value = str(raw[i]).strip()
            if value:
                out[field_name] = value
    return out


def _level(value: str) -> tuple[str, str]:
    """('NCAA Division II') -> ('NCAA', 'DII'). Unknown levels stay empty."""
    key = " ".join((value or "").lower().split())
    if key in LEVELS:
        return LEVELS[key]
    for name, pair in LEVELS.items():
        if key.startswith(name):
            return pair
    return ("", "")


def _index(schools: list[School]) -> dict[tuple[str, str], list[School]]:
    index: dict[tuple[str, str], list[School]] = {}
    for school in schools:
        key = (normalize_name(school.school), usregions.state_code(school.state or ""))
        index.setdefault(key, []).append(school)
    return index


def import_schools(
    path: Path, schools: list[School], *, add_new: bool = False
) -> tuple[list[School], ImportReport]:
    """Fill in athletics domains from ``path``. Returns records to upsert.

    Matching is on normalised name **plus** state. Name alone is not safe: the
    store holds two Bethel Universities and two Trinity Colleges in different
    states, and pairing one with the other's athletics site would be worse than
    leaving both blank.
    """
    report = ImportReport()
    index = _index(schools)
    updates: list[School] = []

    for row in read_rows(path):
        report.rows += 1
        name = row.get("school")
        link = row.get("link")
        if not name or not link:
            report.unusable += 1
            continue

        host = normalize_domain(link)
        if not host:
            report.unusable += 1
            continue

        state = usregions.state_code(row.get("state") or "")
        candidates = index.get((normalize_name(name), state), [])
        if not candidates:
            report.unmatched.append(name)
            if add_new:
                updates.append(_new_school(row, host))
                report.added.append(name)
            continue

        for school in candidates:
            current = school.athletics_domain
            if current == host:
                report.already_known += 1
            elif current and current != normalize_domain(school.website or ""):
                # The NCAA published a different dedicated host (sundevils.com
                # vs thesundevils.com). Both may work; trust the official feed
                # and record the disagreement rather than silently picking.
                report.conflicting.append((school.school, current, host))
            else:
                # Either nothing on record, or the recorded "athletics domain"
                # is really just the university's own site (beloit.edu). A
                # dedicated host (beloitcollegeathletics.com) is strictly better
                # to scrape: it is the athletics department, not the whole
                # college, so discovery cannot wander into the faculty list.
                if current:
                    report.replaced_university_host += 1
                # Carry the identity fields School.key is built from, so this
                # merges onto the stored record instead of creating a twin.
                updates.append(
                    School(
                        school=school.school,
                        state=school.state,
                        country=school.country,
                        ncaa_org_id=school.ncaa_org_id,
                        association=school.association,
                        athletics_domain=host,
                        source="import",
                    )
                )
                report.filled += 1

    return updates, report


def _new_school(row: dict[str, Any], host: str) -> School:
    association, division = _level(row.get("level", ""))
    # "Junior College" covers three governing bodies; the conference says which.
    if association == "NJCAA" and (body := juco_body(row.get("conference"))):
        association = division = body
    return School(
        school=row["school"],
        state=row.get("state") or None,
        city=row.get("city") or None,
        conference=row.get("conference") or None,
        division=normalize_division(division) if division else None,
        association=association or None,
        athletics_domain=host,
        region=usregions.region_for(row.get("state") or "") or None,
        source="import",
    )
