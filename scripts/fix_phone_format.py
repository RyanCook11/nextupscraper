"""One-shot cleanup: put stored phone numbers into one shape.

The same number arrives written several ways — "443 454 5206" from a table
cell, "+14324663753" from the ``tel:`` link beside it, "(805) 922-6966 ext.
3227" from running text. :func:`extract.format_phone` now normalises them on
the way in, so this only has to rewrite what earlier runs stored. Re-scraping
would not: ``Contact.merge`` unions the phone list, so an old spelling survives
alongside the new one rather than being replaced.

Reformatting also lets duplicates collapse. Once "443 454 5206" and
"+14434545206" are both "+1 443 454 5206" they are visibly one number, and the
duplicate is dropped from the record.

Numbers the +1 plan's rules do not fit — international, fragments, digit soup —
are left exactly as they are and reported at the end.

    python scripts/fix_phone_format.py            # report what would change
    python scripts/fix_phone_format.py --apply    # rewrite, keeping a .bak
"""

from __future__ import annotations

import argparse
import collections
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scrapbot.config import Settings  # noqa: E402
from scrapbot.extract import format_phone  # noqa: E402
from scrapbot.storage import ContactStore, StoreLock  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Rewrite the store.")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--limit", type=int, default=10, help="Examples to print.")
    args = parser.parse_args()

    settings = Settings()
    if args.data_dir:
        settings.data_dir = args.data_dir

    store = ContactStore(settings).load()
    contacts = list(store.leads.values())

    reformatted = 0
    deduped = 0
    untouched: collections.Counter = collections.Counter()
    examples: list[tuple[str, str]] = []

    for contact in contacts:
        if not contact.phones:
            continue
        seen: dict[str, None] = {}
        for phone in contact.phones:
            formatted = format_phone(phone)
            if formatted != phone:
                reformatted += 1
                if len(examples) < args.limit:
                    examples.append((phone, formatted))
            else:
                untouched[phone] += 1
            if formatted in seen:
                deduped += 1
            seen.setdefault(formatted, None)
        new = list(seen)
        if args.apply:
            contact.phones = new

    total = sum(len(c.phones) for c in contacts)
    print(f"{len(contacts)} contact(s) in {settings.contacts_path}")
    print(f"  phone values           : {total}")
    print(f"  reformatted            : {reformatted}")
    print(f"  duplicates collapsed   : {deduped}")
    print(f"  left as written        : {sum(untouched.values()) - _canonical(untouched)}")

    if examples:
        print("\n  examples:")
        for old, new in examples:
            print(f"    {old:<24} -> {new}")

    odd = [(p, n) for p, n in untouched.items() if not p.startswith("+1 ")]
    if odd:
        print(f"\n  {sum(n for _p, n in odd)} value(s) the +1 rules do not fit, kept verbatim:")
        for phone, count in sorted(odd, key=lambda kv: -kv[1])[: args.limit]:
            print(f"    {phone!r:<24} x{count}")

    if not args.apply:
        print("\nreport only — nothing written. Re-run with --apply.")
        return 0
    if not reformatted and not deduped:
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
    print(f"rewrote {reformatted} number(s), dropped {deduped} duplicate(s)")
    return 0


def _canonical(counter: collections.Counter) -> int:
    return sum(n for p, n in counter.items() if p.startswith("+1 "))


if __name__ == "__main__":
    raise SystemExit(main())
