"""One-shot cleanup: repair sport/department on already-scraped contacts.

Two defects are undone here, both fixed at the parser for future runs:

* **A colleague's name as the department.** In card layouts the person's name is
  the card's own heading, so walking up for a section heading found the previous
  *card* — Adam Biss came out filed under "Fran Balkcom".
* **No sport at all** on coaches salvaged from a campus staff page, which has no
  sport column. The title still names it ("Head Baseball Coach").

    python scripts/fix_sports.py            # report what would change
    python scripts/fix_sports.py --apply    # rewrite, keeping a .bak

A department is only dropped on one of two signatures, so a real department
named after a person (a bequest, a named centre) is left alone:

* it matches another contact's name at the same school; or
* every single department value at that school is unique. A real department is
  shared — "Administration" appears 472 times across the store, "Sports
  Medicine" 240 — while a bled name belongs to exactly one row. Only the
  handful of card-layout schools trip this.
"""

from __future__ import annotations

import argparse
import collections
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scrapbot.config import Settings  # noqa: E402
from scrapbot.sources.coaches import sport_from_title  # noqa: E402
from scrapbot.storage import ContactStore  # noqa: E402

NOTE = "sport read from the job title (campus directory, no sport column)"


def backup(path: Path) -> Path:
    """Copy ``path`` aside without clobbering an earlier script's backup."""
    target = path.with_suffix(".json.bak")
    n = 2
    while target.exists():
        target = path.with_suffix(f".json.bak{n}")
        n += 1
    shutil.copy2(path, target)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Write the changes. Without this, only report them.")
    parser.add_argument("--data-dir", type=Path, help="Override the data directory.")
    parser.add_argument("--limit", type=int, default=15, help="Examples to print.")
    parser.add_argument(
        "--sports-only",
        action="store_true",
        help="Backfill sports and leave departments alone. The two repairs are "
        "independent, and the 'every value at this school is unique' rule below "
        "is a fair guess on a fresh parse but too blunt for a store-wide sweep: "
        "it clears real departments ('Administration', 'Facilities') at small "
        "schools. scripts/fix_departments.py does the name-bleed half exactly.",
    )
    args = parser.parse_args()

    settings = Settings()
    if args.data_dir:
        settings.data_dir = args.data_dir

    store = ContactStore(settings).load()
    contacts = list(store.leads.values())
    if not contacts:
        print(f"no contacts in {store.json_path}")
        return 0

    # Names per school, so a department is only judged against colleagues.
    names: dict[str, set[str]] = {}
    departments: dict[str, collections.Counter] = {}
    for contact in contacts:
        names.setdefault(contact.school_domain, set()).add(contact.name.strip().lower())
        if contact.department:
            departments.setdefault(
                contact.school_domain, collections.Counter()
            )[contact.department.strip()] += 1

    # Schools where no two people share a department — nobody really organises a
    # directory that way, so these are per-row values that bled in from the
    # neighbouring card.
    all_unique = {
        domain for domain, counts in departments.items()
        if len(counts) >= 3 and all(n == 1 for n in counts.values())
    }
    if args.sports_only:
        all_unique = set()
    if all_unique:
        print("schools where every department value is unique (treated as bled names):")
        for domain in sorted(all_unique):
            print(f"  {domain} ({len(departments[domain])} values)")
        print()

    unfiled, backfilled = [], []
    for contact in contacts:
        dept = (contact.department or "").strip()
        colleague = dept.lower() in names.get(contact.school_domain, set()) \
            and dept.lower() != contact.name.strip().lower()
        if dept and not args.sports_only and (colleague or contact.school_domain in all_unique):
            unfiled.append((contact.name, dept))
            contact.department = None

        if not contact.sport and contact.is_coach:
            sport = sport_from_title(contact.title)
            if sport:
                contact.sport = sport
                if NOTE not in contact.notes:
                    contact.notes.append(NOTE)
                backfilled.append((contact.name, contact.title, sport))

    print(f"{len(unfiled)} department(s) that were really a colleague's name")
    for name, dept in unfiled[: args.limit]:
        print(f"  {name}: department {dept!r} -> None")
    print(f"\n{len(backfilled)} coach(es) given a sport from their title")
    for name, title, sport in backfilled[: args.limit]:
        print(f"  {name}: {title!r} -> {sport}")
    if len(backfilled) > args.limit:
        print(f"  … {len(backfilled) - args.limit} more")

    if not unfiled and not backfilled:
        return 0
    if not args.apply:
        print("\ndry run — pass --apply to write")
        return 0

    saved = backup(store.json_path)
    store.save()
    print(f"\nwrote {store.json_path} (backup at {saved})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
