"""One-shot cleanup: delete contacts whose name is a vacancy marker.

Directories list an unfilled post with a placeholder where the name goes — SUNY
Broome uses "TBD", Cal "TBA", Charleston Southern "T BA", Denison "TBA ,", Cal
Poly "TBD TBD". Nothing was mis-parsed; the page really does say that. But a
contact named TBA is a job opening, not a person, and it cannot be written to or
de-duplicated sensibly.

The parser now skips these rows (see ``is_placeholder_name``), so this only has
to clear what earlier runs stored. Re-scraping would not: a stored record is
only ever merged into, never removed, so a placeholder survives every later run.

An address on such a row is a department inbox rather than anyone's own. Where
that address appears nowhere else at the school it is reported before deletion,
so a genuinely unique contact route is a decision rather than a silent loss.

    python scripts/drop_placeholder_contacts.py            # report
    python scripts/drop_placeholder_contacts.py --apply    # delete, keeping a .bak
"""

from __future__ import annotations

import argparse
import collections
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scrapbot.config import Settings  # noqa: E402
from scrapbot.sources.coaches import is_placeholder_name  # noqa: E402
from scrapbot.storage import ContactStore, StoreLock  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Delete them.")
    parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args()

    settings = Settings()
    if args.data_dir:
        settings.data_dir = args.data_dir

    store = ContactStore(settings).load()
    contacts = list(store.leads.values())

    doomed = [c for c in contacts if is_placeholder_name(c.name or "")]
    survivors = [c for c in contacts if not is_placeholder_name(c.name or "")]

    # Addresses that only a placeholder row carries, per school.
    kept_emails: dict[str, set[str]] = collections.defaultdict(set)
    for contact in survivors:
        for address in contact.emails:
            kept_emails[contact.school_domain].add(address.lower())
    sole = [
        (c, a)
        for c in doomed
        for a in c.emails
        if a.lower() not in kept_emails.get(c.school_domain, set())
    ]

    print(f"{len(contacts)} contact(s) in {settings.contacts_path}")
    print(f"  vacancy placeholders : {len(doomed)}")
    print(f"  schools affected     : {len({c.school_domain for c in doomed})}")
    print(f"  of those, marked is_coach: {sum(1 for c in doomed if c.is_coach)}")

    if doomed:
        print("\n  spellings found:")
        for value, count in collections.Counter(c.name for c in doomed).most_common(12):
            print(f"    {value!r:<22} {count}")
        print("\n  worst affected:")
        for domain, count in collections.Counter(c.school_domain for c in doomed).most_common(6):
            print(f"    {domain:<32} {count}")

    if sole:
        print(f"\n  {len(sole)} address(es) held by no one else at that school:")
        for contact, address in sole[:10]:
            print(f"    {contact.school_domain:<30} {address}")
        if len(sole) > 10:
            print(f"    ... and {len(sole) - 10} more")
        print("  (department inboxes — the school stays reachable via its own site)")

    if not args.apply:
        print("\nreport only — nothing deleted. Re-run with --apply.")
        return 0
    if not doomed:
        print("\nnothing to delete")
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
        for contact in doomed:
            store.leads.pop(contact.key, None)
        store.save()
        print(f"deleted {len(doomed)} placeholder(s) -> {len(store.leads)} contact(s) remain")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
