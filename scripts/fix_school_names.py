"""One-shot cleanup: give every contact their institution's real name.

Two defects, one cause — the school name came from the athletics site's page
title, which is marketing rather than a name:

* **Missing entirely.** On a campus directory the title is "Campus Directory",
  which ``_NOT_A_SCHOOL_RE`` rightly rejects as not-a-school — leaving the field
  empty. Ten hosts stored people with no school at all: baycollege.edu, bc3.edu,
  bigbend.edu, ecc.edu among them.
* **Buried in boilerplate.** "The Official Home of Allan Hancock College
  Athletics" is an institution wrapped in advertising.

The school store already holds the official name from the NCAA, NAIA or NJCAA
member directory, keyed by the very host being scraped, and covers all but a
handful of them. So this is a join, not a guess. The parser now prefers the
same source (:meth:`CoachesSource._school_name_map`); this repairs what earlier
runs stored, which re-scraping would not — ``Contact.merge`` only replaces a
scalar when the incoming value is non-empty, and a boilerplate title is not
empty.

Hosts the store does not know keep their scraped name, with the boilerplate
stripped by :func:`clean_school_name`.

    python scripts/fix_school_names.py            # report
    python scripts/fix_school_names.py --apply    # rewrite, keeping a .bak
"""

from __future__ import annotations

import argparse
import collections
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scrapbot.config import Settings  # noqa: E402
from scrapbot.sources.coaches import clean_school_name  # noqa: E402
from scrapbot.sources.website import normalize_domain  # noqa: E402
from scrapbot.storage import ContactStore, SchoolStore, StoreLock  # noqa: E402


def official_names(settings: Settings) -> dict[str, str]:
    """``host -> official school name``, from the school store."""
    out: dict[str, str] = {}
    for school in SchoolStore(settings).load().sorted_leads():
        if not school.school:
            continue
        for host in (school.athletics_domain, normalize_domain(school.website or "")):
            if host:
                out.setdefault(host, school.school)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Rewrite the store.")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--limit", type=int, default=12, help="Examples to print.")
    args = parser.parse_args()

    settings = Settings()
    if args.data_dir:
        settings.data_dir = args.data_dir

    names = official_names(settings)
    store = ContactStore(settings).load()
    contacts = list(store.leads.values())
    print(f"{len(names)} host(s) with an official name in the school store")

    filled, corrected, cleaned = [], [], []
    unknown: collections.Counter = collections.Counter()

    for contact in contacts:
        current = (contact.school or "").strip()
        official = names.get(contact.school_domain)
        if official:
            if not current:
                filled.append((contact, official))
            elif current != official:
                corrected.append((contact, current, official))
            if args.apply:
                contact.school = official
            continue

        unknown[contact.school_domain] += 1
        tidied = clean_school_name(current) if current else None
        if tidied and tidied != current:
            cleaned.append((contact, current, tidied))
            if args.apply:
                contact.school = tidied

    print(f"{len(contacts)} contact(s) in {settings.contacts_path}")
    print(f"  school filled in (was empty)      : {len(filled)}")
    print(f"  school replaced with official name: {len(corrected)}")
    print(f"  boilerplate stripped (not in store): {len(cleaned)}")
    print(f"  hosts the store does not know     : {len(unknown)}")

    if filled:
        print("\n  filled in:")
        for domain, count in collections.Counter(
            c.school_domain for c, _n in filled
        ).most_common(args.limit):
            print(f"    {domain:<28} {count:>4}  -> {names[domain]!r}")
    if corrected:
        print("\n  replaced:")
        seen: set = set()
        for _c, old, new in corrected:
            if (old, new) in seen:
                continue
            seen.add((old, new))
            print(f"    {old[:46]!r:<48} -> {new!r}")
            if len(seen) >= args.limit:
                break
    if unknown:
        print("\n  not in the school store (name kept as scraped):")
        for domain, count in unknown.most_common(args.limit):
            print(f"    {domain:<28} {count}")

    if not args.apply:
        print("\nreport only — nothing written. Re-run with --apply.")
        return 0
    if not (filled or corrected or cleaned):
        print("\nnothing to change")
        return 0

    backup = settings.contacts_path.with_suffix(".json.bak")
    n = 2
    while backup.exists():
        backup = settings.contacts_path.with_suffix(f".json.bak{n}")
        n += 1
    shutil.copy2(settings.contacts_path, backup)
    print(f"\nbacked up to {backup}")
    with StoreLock(settings):
        store.save()
    print(f"rewrote {len(filled) + len(corrected) + len(cleaned)} school name(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
