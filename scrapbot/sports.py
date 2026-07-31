"""Canonical sport names, derived at read time.

A staff directory writes its sport heading however it likes. The store holds
2,895 distinct ``sport`` values for what are really thirty sports: case variants
(``FOOTBALL``), headings with contact details baked in (``Baseball -
baseball@brandeis.edu``), alternate gender notation (``Tennis (M)``), and
section banners (``Men's Basketball // Sport Administrator: Pat Garrity``).

Nothing here re-parses those. :func:`~scrapbot.sources.coaches.sport_from_title`
already resolves every one of them — this module only groups the result and
applies the handful of merges that vocabulary deliberately keeps apart.

Derived on read rather than stored: the merges below are judgement calls that
will be revised, and a stored column would need an 80,000-record backfill after
each revision, with every export taken in between silently stale.
"""

from __future__ import annotations

import re

from .sources.coaches import sport_from_title

# Programs the vocabulary separates but schools run as one, with one staff.
MERGES = {
    "Swimming": "Swimming & Diving",
    "Diving": "Swimming & Diving",
    "Crew": "Rowing",
}

_GENDERED = re.compile(r"^(Men's|Women's)\s+(.*)$")


def split_gender(label: str) -> tuple[str, str]:
    """``"Men's Basketball"`` -> ``("Men's", "Basketball")``."""
    found = _GENDERED.match(label)
    return (found.group(1), found.group(2)) if found else ("", label)


def base_of(label: str) -> str:
    """The sport without its gender qualifier."""
    return split_gender(label)[1]


def canonical(raw: str | None) -> list[str]:
    """Canonical sport labels for one raw ``sport`` value.

    Returns several when the person genuinely holds several — a cross country
    and track coach is one record over two programs, joined by
    :meth:`Contact.merge`. Order follows the source string.
    """
    resolved = sport_from_title(raw or "")
    if not resolved:
        return []

    labels: list[str] = []
    for part in resolved.split(";"):
        part = part.strip()
        if not part:
            continue
        gender, base = split_gender(part)
        base = MERGES.get(base, base)
        label = f"{gender} {base}".strip()
        # Swimming and Diving collapse onto one label, so a coach listed under
        # both must not appear twice.
        if label not in labels:
            labels.append(label)

    # Reconcile the genders one heading gave for a single sport:
    #
    #   both      -> the plain sport. "Men's & Women's Swimming & Diving" is one
    #                coach over both squads; two entries overstate it.
    #   one       -> that one. "Men's Swimming & Diving" resolves to Men's
    #                Swimming plus a bare Diving, because "& Diving" carries no
    #                qualifier of its own — but the heading did say men's.
    #   none      -> the plain sport, meaning nobody stated a gender.
    genders_seen: dict[str, set[str]] = {}
    for label in labels:
        gender, base = split_gender(label)
        genders_seen.setdefault(base, set()).add(gender)

    out: list[str] = []
    for label in labels:
        gender, base = split_gender(label)
        stated = genders_seen[base] - {""}
        if len(stated) == 1:
            label = f"{next(iter(stated))} {base}"
        else:
            # Both genders, or neither: the sport alone carries it.
            label = base
        if label not in out:
            out.append(label)
    return out


def canonical_field(raw: str | None) -> str:
    """The canonical labels as one CSV cell, joined like the raw field."""
    return "; ".join(canonical(raw))


def matches(raw: str | None, needle: str, *, department: str | None = None) -> bool:
    """Does this contact belong under ``needle``?

    Selecting a bare sport includes its gendered variants — picking
    ``Basketball`` returns all 11,762, not only the rows nobody assigned a
    gender to. Selecting ``Men's Basketball`` narrows to that variant.
    """
    if not needle:
        return True
    wanted = needle.strip().lower()
    wanted_is_gendered = bool(_GENDERED.match(needle.strip()))

    labels = canonical(raw)
    for label in labels:
        if label.lower() == wanted:
            return True
        if not wanted_is_gendered and base_of(label).lower() == wanted:
            return True

    # Department is not a sport and never resolves, so it keeps the plain
    # substring test — as do free-text values like "strength".
    if department and wanted in department.lower():
        return True

    # Only when the raw value resolved to nothing does it get a substring test.
    # Applying that to a resolved value would make "Men's Basketball" match a
    # women's row, because it is a literal substring of "Women's Basketball".
    return not labels and wanted in (raw or "").lower()


def options(raw_values) -> dict[str, int]:
    """Ordered ``{label: count}`` for the sport filter.

    Grouped so each sport's variants sit together — ``Basketball``, then
    ``Men's Basketball``, then ``Women's Basketball`` — with the groups
    themselves ordered by size. The ungendered label carries the count of rows
    where no directory stated a gender, which is most of Football and Baseball;
    it is "unknown", not a third category.
    """
    counts: dict[str, int] = {}
    for raw in raw_values:
        for label in canonical(raw):
            counts[label] = counts.get(label, 0) + 1

    groups: dict[str, int] = {}
    for label, n in counts.items():
        base = base_of(label)
        groups[base] = groups.get(base, 0) + n

    ordered: dict[str, int] = {}
    for base, _total in sorted(groups.items(), key=lambda kv: -kv[1]):
        for label in (base, f"Men's {base}", f"Women's {base}"):
            if label in counts:
                ordered[label] = counts[label]
    return ordered
