"""One-shot cleanup: strip jersey numbers and class years out of stored names.

Division III directories decorate the name field — Chestnut Hill publishes
"#42 Matthew Owens '18", with a jersey number in front and the alumni class
year behind. Neither is part of the name: a search for "Matthew Owens" misses
him, he sorts under "#", and if he is listed again next season as "'18" beside
a differently-decorated spelling the two records never merge.

The parser now strips both (see ``strip_name_decorations``), so this only has
to repair what earlier runs stored. Re-scraping would not fix it on its own:
the decorated name is part of the store key wherever a record has no profile
URL, so a clean re-scrape lands beside the old row rather than merging into it.

Where stripping makes two stored records identical, they are merged with the
store's own :meth:`Contact.merge`, so no email or phone is lost.

    python scripts/fix_name_decorations.py            # report
    python scripts/fix_name_decorations.py --apply    # rewrite, keeping a .bak
"""

from __future__ import annotations

import argparse
import collections
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scrapbot.config import Settings  # noqa: E402
from scrapbot.sources.coaches import strip_name_decorations  # noqa: E402
from scrapbot.storage import ContactStore, StoreLock  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Rewrite the store.")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--limit", type=int, default=12, help="Examples to print.")
    args = parser.parse_args()

    settings = Settings()
    if args.data_dir:
        settings.data_dir = args.data_dir

    store = ContactStore(settings).load()
    contacts = list(store.leads.values())

    changed = [
        (c, c.name, strip_name_decorations(c.name))
        for c in contacts
        if c.name and strip_name_decorations(c.name) != c.name
    ]

    print(f"{len(contacts)} contact(s) in {settings.contacts_path}")
    print(f"  names carrying a jersey number or class year : {len(changed)}")
    print(f"  schools affected                             : "
          f"{len({c.school_domain for c, _o, _n in changed})}")

    if changed:
        print("\n  worst affected:")
        worst = collections.Counter(c.school_domain for c, _o, _n in changed).most_common(8)
        for domain, count in worst:
            print(f"    {domain:<30} {count}")
        print("\n  examples:")
        for _c, old, new in changed[: args.limit]:
            print(f"    {old!r:<32} -> {new!r}")

    if not args.apply:
        print("\nreport only — nothing written. Re-run with --apply.")
        return 0
    if not changed:
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
        store = ContactStore(settings).load()
        merged = 0
        # Rebuild rather than mutate in place: the name is part of the key for
        # any record without a profile URL, so a renamed record belongs under a
        # different key and may collide with one already there.
        rebuilt: dict = {}
        for contact in store.sorted_leads():
            contact.name = strip_name_decorations(contact.name) if contact.name else contact.name
            existing = rebuilt.get(contact.key)
            if existing is None:
                rebuilt[contact.key] = contact
            else:
                rebuilt[contact.key] = existing.merge(contact)
                merged += 1
        store.leads = rebuilt
        store.save()
        print(f"rewrote {len(changed)} name(s); {merged} record(s) merged into an existing one")
        print(f"store now holds {len(store.leads)} contact(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
