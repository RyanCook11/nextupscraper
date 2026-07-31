"""One-shot cleanup: delete the phantom contacts the card parser invented.

``_parse_cards`` de-duplicated nested card wrappers with ``id(node)``. selectolax
returns a fresh Python wrapper on every access, so that check never fired and
each wrapper inside a card parsed as its own person. The innermost wrapper is
the contact block, whose first ``<a>`` is the ``tel:`` link -- so it became a
person *named after a phone number*.

The parser no longer does this (see ``_parse_cards``), and re-scraping repairs
most of the damage on its own: a phantom that carries the card's profile URL
shares the real person's store key, so the correct record merges straight over
it. A phantom with no profile URL does not -- it is keyed on the phone number
itself and survives re-scraping forever. Those are what this removes.

A phantom is only deleted when the person's details are demonstrably safe
somewhere else: another contact at the same school, with a real name, holding
the same email address. Anything else is reported and kept, because a phantom
that is the only record of an address is still the address.

    python scripts/drop_phantom_contacts.py            # report what would go
    python scripts/drop_phantom_contacts.py --apply    # delete, keeping a .bak
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


def is_phantom(name: str) -> bool:
    """A name with no letters in it is a phone number, not a person."""
    return bool(name) and not any(ch.isalpha() for ch in name)


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

    # Every email held by a properly-named person, per school.
    real_emails: dict[str, set[str]] = collections.defaultdict(set)
    for contact in contacts:
        if not is_phantom(contact.name):
            for address in contact.emails:
                real_emails[contact.school_domain].add(address.lower())

    covered: list = []
    orphaned: list = []
    for contact in contacts:
        if not is_phantom(contact.name):
            continue
        known = real_emails.get(contact.school_domain, set())
        if contact.emails and all(a.lower() in known for a in contact.emails):
            covered.append(contact)
        else:
            orphaned.append(contact)

    print(f"{len(contacts)} contact(s) in {settings.contacts_path}")
    print(f"  phantoms (phone-number names) : {len(covered) + len(orphaned)}")
    print(f"    safe to delete              : {len(covered)}")
    print(f"    kept — sole record of a detail: {len(orphaned)}")

    if covered:
        print("\n  schools losing the most phantoms:")
        worst = collections.Counter(c.school_domain for c in covered).most_common(8)
        for domain, count in worst:
            print(f"    {domain:<34} {count}")

    if orphaned:
        print("\n  kept, because no properly-named contact holds their address:")
        for contact in orphaned[:10]:
            print(f"    {contact.school_domain:<30} {contact.name:<18} {'; '.join(contact.emails) or 'no email'}")
        if len(orphaned) > 10:
            print(f"    ... and {len(orphaned) - 10} more")

    if not args.apply:
        print("\nreport only — nothing deleted. Re-run with --apply.")
        return 0
    if not covered:
        print("\nnothing to delete")
        return 0

    backup = settings.contacts_path.with_suffix(".json.bak")
    shutil.copy2(settings.contacts_path, backup)
    print(f"\nbacked up to {backup}")

    with StoreLock(settings):
        store = ContactStore(settings).load()
        for contact in covered:
            store.leads.pop(contact.key, None)
        store.save()
        print(f"deleted {len(covered)} phantom(s) -> {len(store.leads)} contact(s) remain")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
