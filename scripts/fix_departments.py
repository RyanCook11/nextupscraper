"""One-shot cleanup: clear departments that are really a person's name.

Card layouts put the person's name in the card's own heading, so walking up for
a section heading found the *previous card* and mined their name. The parser no
longer does this (:func:`_is_person_block` now recognises a card by its name
node or profile link, and :func:`_drop_colleague_departments` is a backstop),
but stored rows keep the old value: ``Contact.merge`` **unions** department via
``_join_unique``, so a bad value survives every re-scrape rather than being
overwritten. It has to be cleared in place.

This applies the parser's own rule to the store — drop a department part that
exactly matches a contact name at the same school — and nothing else. It does
not guess. ``scripts/fix_sports.py`` additionally treats "every department value
at this school is unique" as evidence of a bled name, which is a reasonable
heuristic for a fresh parse but wrong here: it clears real departments like
"Strength and Conditioning" and "Administration" at small schools.

    python scripts/fix_departments.py            # report what would change
    python scripts/fix_departments.py --apply    # rewrite, keeping a .bak
"""

from __future__ import annotations

import argparse
import collections
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scrapbot.config import Settings  # noqa: E402
from scrapbot.storage import ContactStore, StoreLock  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Rewrite the store.")
    parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args()

    settings = Settings()
    if args.data_dir:
        settings.data_dir = args.data_dir

    store = ContactStore(settings).load()
    contacts = list(store.leads.values())

    # Names actually found at each school — the parser's own test, applied with
    # the whole store in hand instead of one page.
    names: dict[str, set[str]] = collections.defaultdict(set)
    for contact in contacts:
        name = (contact.name or "").strip().casefold()
        if name:
            names[contact.school_domain].add(name)

    changed: list[tuple] = []
    for contact in contacts:
        if not contact.department:
            continue
        known = names.get(contact.school_domain, set())
        kept = [
            part
            for part in (p.strip() for p in contact.department.split(";"))
            if part and part.casefold() not in known
        ]
        new = "; ".join(kept) or None
        if new != contact.department:
            changed.append((contact, contact.department, new))
            if args.apply:
                contact.department = new

    print(f"{len(contacts)} contact(s) in {settings.contacts_path}")
    print(f"  departments holding a person's name : {len(changed)}")
    emptied = sum(1 for _c, _old, new in changed if new is None)
    print(f"    cleared outright                  : {emptied}")
    print(f"    partly kept (a real department too): {len(changed) - emptied}")

    if changed:
        worst = collections.Counter(c.school_domain for c, _o, _n in changed).most_common(8)
        print("\n  worst affected:")
        for domain, count in worst:
            print(f"    {domain:<30} {count}")
        print("\n  examples:")
        for contact, old, new in changed[:10]:
            print(f"    {contact.name[:22]:<22} {old[:44]!r} -> {new!r}")

    if not args.apply:
        print("\nreport only — nothing written. Re-run with --apply.")
        return 0
    if not changed:
        print("\nnothing to change")
        return 0

    backup = settings.contacts_path.with_suffix(".json.bak")
    shutil.copy2(settings.contacts_path, backup)
    print(f"\nbacked up to {backup}")
    with StoreLock(settings):
        store.save()
    print(f"rewrote {len(changed)} department(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
