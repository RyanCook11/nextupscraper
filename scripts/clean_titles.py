"""One-shot cleanup: strip contact details out of stored contact titles.

Card layouts with no internal markup collapsed a whole block into the title
field, so ``data/contacts.json`` holds rows like::

    ATHLETICS Head Baseball Coach 229-732-5901 adambiss@andrewcollege.edu

The parser no longer produces those (see ``coaches.clean_title``), but rows
scraped before the fix keep the old value until they are re-scraped. This
rewrites them in place.

    python scripts/clean_titles.py            # report what would change
    python scripts/clean_titles.py --apply    # rewrite, keeping a .bak

Any address or number peeled out of a title is added to that person's emails or
phones first, so the cleanup only ever moves data into the right field.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scrapbot import extract  # noqa: E402
from scrapbot.config import Settings  # noqa: E402
from scrapbot.sources.coaches import clean_title  # noqa: E402
from scrapbot.storage import ContactStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Write the changes. Without this, only report them.")
    parser.add_argument("--data-dir", type=Path, help="Override the data directory.")
    parser.add_argument("--limit", type=int, default=15,
                        help="How many examples to print (default 15).")
    args = parser.parse_args()

    settings = Settings()
    if args.data_dir:
        settings.data_dir = args.data_dir

    store = ContactStore(settings).load()
    if not store.leads:
        print(f"no contacts in {store.json_path}")
        return 0

    changed = []
    for contact in store.leads.values():
        cleaned = clean_title(contact.title)
        if cleaned == contact.title:
            continue
        # Keep anything the title was hiding, in the field it belongs to.
        for addr in extract.EMAIL_RE.findall(contact.title or ""):
            if addr.lower() not in {e.lower() for e in contact.emails}:
                contact.emails.append(addr.lower())
        changed.append((contact.title, cleaned))
        contact.title = cleaned

    print(f"{len(changed)} of {len(store.leads)} title(s) would change\n")
    for before, after in changed[: args.limit]:
        print(f"  - {before}\n  + {after or '(empty)'}\n")
    if len(changed) > args.limit:
        print(f"  … {len(changed) - args.limit} more\n")

    if not changed:
        return 0
    if not args.apply:
        print("dry run — pass --apply to write")
        return 0

    backup = store.json_path.with_suffix(".json.bak")
    shutil.copy2(store.json_path, backup)
    store.save()
    print(f"wrote {store.json_path} (backup at {backup})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
