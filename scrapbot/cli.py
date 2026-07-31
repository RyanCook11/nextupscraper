"""scrapbot command line interface."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from . import models, sources, storage
from .config import Settings
from .models import Contact, Lead, School
from .runner import run_source

log = logging.getLogger("scrapbot")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scrapbot",
        description="Company / lead data scraper for NextUp Recruitment.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # Shared flags live on reusable parent parsers whose defaults are
    # SUPPRESSed, so an unset flag is simply *absent* from the namespace and a
    # source subparser re-declaring --delay cannot clobber `run --delay 3
    # website`. Every reader below therefore uses getattr() with a fallback.
    # Do NOT call set_defaults() for these: it mutates the shared parent
    # actions in place and would undo the SUPPRESS.
    common = _common_parser()
    run_only = _run_only_parser()

    # --- run -------------------------------------------------------------
    run = sub.add_parser("run", help="Scrape leads from a source.", parents=[common, run_only])
    source_sub = run.add_subparsers(dest="source", required=True, metavar="SOURCE")
    for name, cls in sorted(sources.SOURCES.items()):
        # Flags are accepted on either side of the source name.
        sp = source_sub.add_parser(
            name, help=cls.help, description=cls.help, parents=[common, run_only]
        )
        cls.add_arguments(sp)
    run.set_defaults(func=cmd_run)

    # --- sources ---------------------------------------------------------
    listing = sub.add_parser("sources", help="List available sources.")
    listing.set_defaults(func=cmd_sources)

    # --- stats -----------------------------------------------------------
    stats = sub.add_parser("stats", help="Summarize the stored leads.", parents=[common])
    stats.add_argument(
        "--contacts", action="store_true", help="Summarize the people store instead."
    )
    stats.add_argument(
        "--schools", action="store_true", help="Summarize the school store instead."
    )
    stats.set_defaults(func=cmd_stats)

    # --- seeds -----------------------------------------------------------
    seeds = sub.add_parser(
        "seeds",
        help="Write a coaches seed list of athletics hosts from the school store.",
        parents=[common],
    )
    seeds.add_argument("--out", type=Path, required=True, help="Seed file to write.")
    seeds.add_argument("--division", nargs="+", metavar="DIV", help="Only these divisions.")
    seeds.add_argument("--state", nargs="+", metavar="ST", help="Only these states.")
    seeds.add_argument(
        "--from-failures",
        nargs="*",
        metavar="STATUS",
        help="Instead of the school store, collect hosts that failed across all "
        "stored runs (default statuses: blocked error network). Hosts that "
        "succeeded in any run are left out.",
    )
    seeds.set_defaults(func=cmd_seeds)

    # --- import ----------------------------------------------------------
    imp = sub.add_parser(
        "import-schools",
        help="Fill in athletics sites from a spreadsheet (.xlsx or .csv).",
        parents=[common],
    )
    imp.add_argument("file", type=Path, help="Spreadsheet with School / State / link columns.")
    imp.add_argument(
        "--add-new",
        action="store_true",
        help="Also create schools the list has that the store does not. Off by "
        "default: the official directories decide who exists.",
    )
    imp.add_argument("--dry-run", action="store_true", help="Report but write nothing.")
    imp.set_defaults(func=cmd_import_schools)

    # --- export ----------------------------------------------------------
    export = sub.add_parser(
        "export", help="Filter the store into a CSV or JSON file.", parents=[common]
    )
    export.add_argument("--out", type=Path, required=True, help="Output file (.csv or .json).")
    export.add_argument("--has-email", action="store_true", help="Only leads with an email.")
    export.add_argument("--has-phone", action="store_true", help="Only leads with a phone number.")
    export.add_argument("--industry", help="Only leads matching this industry hint.")
    export.add_argument("--search", help="Case-insensitive substring match across all fields.")
    export.add_argument(
        "--contacts", action="store_true", help="Export the people store instead of the leads."
    )
    export.add_argument(
        "--schools",
        action="store_true",
        help="Export the school store, in the origin database's shape.",
    )
    export.add_argument(
        "--division", nargs="+", metavar="DIV", help="Schools/contacts in these divisions."
    )
    export.add_argument(
        "--sport", help="Contacts only: sport or department substring, e.g. 'basketball'."
    )
    export.add_argument(
        "--coaches-only", action="store_true", help="Contacts only: skip non-coaching staff."
    )
    export.add_argument(
        "--direct-email",
        action="store_true",
        help="Contacts only: skip shared inboxes and assistants' addresses.",
    )
    export.set_defaults(func=cmd_export)

    # --- dashboard -------------------------------------------------------
    dash = sub.add_parser(
        "dashboard", help="Serve the local review dashboard.", parents=[common]
    )
    dash.add_argument("--host", default="127.0.0.1")
    dash.add_argument("--port", type=int, default=8000)
    dash.set_defaults(func=cmd_dashboard)

    return parser


S = argparse.SUPPRESS


def _run_only_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--dry-run", action="store_true", default=S, help="Scrape but write nothing."
    )
    return parser


def _common_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    group = parser.add_argument_group("common options")
    group.add_argument(
        "--data-dir", type=Path, default=S, help="Where to read/write scraped data."
    )
    group.add_argument(
        "--delay", type=float, default=S, help="Minimum seconds between hits on one host."
    )
    group.add_argument(
        "--concurrency", type=int, default=S, help="How many sites to work on at once."
    )
    group.add_argument("--max-pages", type=int, default=S, help="Max pages to fetch per site.")
    group.add_argument(
        "--max-sport-pages", type=int, default=S,
        help="Max /sports/<sport>/coaches pages per site, for sites that publish "
             "coaches one page per team instead of one staff directory.",
    )
    group.add_argument(
        "--cache-ttl",
        type=float,
        default=S,
        metavar="SECONDS",
        help="How long a fetched page stays reusable (default: one week).",
    )
    group.add_argument(
        "--no-cache",
        action="store_true",
        default=S,
        help="Re-fetch everything instead of reusing cached pages.",
    )
    group.add_argument(
        "--render",
        choices=["never", "auto", "always"],
        default=S,
        help="Use a real browser: never, auto (when static HTML looks empty), or always.",
    )
    group.add_argument(
        "--headful", action="store_true", default=S, help="Show the browser window."
    )
    group.add_argument(
        "--ignore-robots",
        action="store_true",
        default=S,
        help="Ignore robots.txt. Only for sites you own or have written permission to crawl.",
    )
    group.add_argument("-v", "--verbose", action="store_true", default=S, help="Debug logging.")
    group.add_argument(
        "-q", "--quiet", action="store_true", default=S, help="Warnings and errors only."
    )
    return parser


def settings_from_args(args: argparse.Namespace) -> Settings:
    settings = Settings()
    if getattr(args, "data_dir", None):
        settings.data_dir = args.data_dir
    if getattr(args, "delay", None) is not None:
        settings.delay = args.delay
    if getattr(args, "concurrency", None):
        settings.concurrency = max(1, args.concurrency)
    if getattr(args, "max_pages", None):
        settings.max_pages_per_site = max(1, args.max_pages)
    if getattr(args, "max_sport_pages", None):
        settings.max_sport_pages = max(1, args.max_sport_pages)
    if getattr(args, "cache_ttl", None) is not None:
        settings.cache_ttl = max(0.0, args.cache_ttl)
    if getattr(args, "no_cache", False):
        settings.cache_ttl = 0.0
    if getattr(args, "render", None):
        settings.render = args.render
    if getattr(args, "headful", False):
        settings.headless = False
    if getattr(args, "ignore_robots", False):
        settings.respect_robots = False
        log.warning(
            "robots.txt is being ignored — make sure you have permission to crawl these sites."
        )
    return settings


# --- commands ------------------------------------------------------------

def cmd_sources(args: argparse.Namespace) -> int:
    print("Available sources:\n")
    for name, cls in sorted(sources.SOURCES.items()):
        print(f"  {name:<12} {cls.help}")
    print("\nSee `scrapbot run SOURCE --help` for source-specific options.")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    settings = settings_from_args(args)
    dry_run = getattr(args, "dry_run", False)
    result = asyncio.run(run_source(args.source, args, settings, dry_run=dry_run))

    record_cls = sources.get(args.source).record_cls
    people = record_cls is Contact
    noun = {Contact: "people", School: "schools"}.get(record_cls, "leads")

    print()
    print(f"run {result.run_id} via '{result.source}' finished in {result.seconds:.1f}s")
    print(f"  {noun + ' yielded':<16}: {len(result.leads)}")
    print(f"  with contact    : {result.with_contact}")
    print(f"  new / updated   : {result.new} / {result.updated}")
    stats = result.fetch_stats
    print(
        "  requests        : {requests} ({rendered} rendered, {blocked} robots-blocked, "
        "{errors} failed)".format(
            requests=stats.get("requests", 0),
            rendered=stats.get("rendered", 0),
            blocked=stats.get("blocked", 0),
            errors=stats.get("errors", 0),
        )
    )
    store_path = {
        Contact: settings.contacts_path,
        School: settings.schools_path,
    }.get(record_cls, settings.store_path)
    review = {
        Contact: "scrapbot stats --contacts",
        School: "scrapbot stats --schools",
    }.get(record_cls, "scrapbot dashboard")

    _print_site_report(result)

    if result.out_dir:
        print(f"  snapshot        : {result.out_dir}")
        print(f"  merged store    : {store_path}")
        print(f"\nReview it with: {review}")
    return 0


def _print_site_report(result) -> None:
    """Per-site success and failure, so a blocked site is never invisible."""
    if not result.outcomes:
        return

    grouped = result.by_status()
    succeeded, failed = result.succeeded, result.failed

    print()
    print(f"  sites attempted : {len(result.outcomes)}")
    print(f"    succeeded     : {len(succeeded)} ({_pct(len(succeeded), len(result.outcomes))})")
    print(f"    failed        : {len(failed)}")

    for status, items in sorted(grouped.items(), key=lambda kv: -len(kv[1])):
        if status == models.SiteOutcome.OK:
            continue
        label = models.OUTCOME_LABELS.get(status, status)
        print(f"      {label:<38} {len(items)}")
        for outcome in items[:3]:
            print(f"        - {outcome.domain}: {outcome.detail[:70]}")
        if len(items) > 3:
            print(f"        ... and {len(items) - 3} more")

    if result.retryable:
        print(
            f"\n  {len(result.retryable)} failure(s) are worth retrying "
            "(blocked, timed out, or errored)."
        )
    if result.out_dir:
        print(f"  per-site detail : {result.out_dir / 'sites.json'}")
        print(f"  retry list      : {result.out_dir / 'failed.txt'}")


def cmd_stats(args: argparse.Namespace) -> int:
    settings = settings_from_args(args)
    if getattr(args, "schools", False):
        return _school_stats(settings)
    if getattr(args, "contacts", False):
        return _contact_stats(settings)
    store = storage.LeadStore(settings).load()
    leads = store.sorted_leads()
    if not leads:
        print(f"No leads stored yet in {settings.data_dir}. Try: scrapbot run website --domains ...")
        return 0

    with_email = sum(1 for lead in leads if lead.emails)
    with_phone = sum(1 for lead in leads if lead.phones)
    with_linkedin = sum(1 for lead in leads if "linkedin" in lead.socials)
    hiring = sum(1 for lead in leads if lead.has_open_roles)

    print(f"{len(leads)} lead(s) in {settings.store_path}")
    print(f"  with email      : {with_email} ({_pct(with_email, len(leads))})")
    print(f"  with phone      : {with_phone} ({_pct(with_phone, len(leads))})")
    print(f"  with linkedin   : {with_linkedin} ({_pct(with_linkedin, len(leads))})")
    print(f"  signs of hiring : {hiring}")

    counts: dict[str, int] = {}
    for lead in leads:
        for hint in lead.industry_hints:
            counts[hint] = counts.get(hint, 0) + 1
    if counts:
        print("\n  top industries:")
        for industry, count in sorted(counts.items(), key=lambda kv: -kv[1])[:10]:
            print(f"    {industry:<16} {count}")
    return 0


def cmd_import_schools(args: argparse.Namespace) -> int:
    """Merge athletics sites from a supplied list into the school store."""
    from . import importer

    if not args.file.exists():
        print(f"no such file: {args.file}", file=sys.stderr)
        return 1

    settings = settings_from_args(args)
    store = storage.SchoolStore(settings).load()
    schools = store.sorted_leads()
    if not schools and not args.add_new:
        print(
            f"No schools stored yet in {settings.data_dir}. Run `scrapbot run schools` "
            "first, or pass --add-new to build the store from this file.",
            file=sys.stderr,
        )
        return 1

    updates, report = importer.import_schools(args.file, schools, add_new=args.add_new)
    print(report.summary())

    if report.conflicting:
        print("\n  the list disagrees with the official directory (kept the official one):")
        for name, current, offered in report.conflicting[:10]:
            print(f"    {name}: {current}  (list said {offered})")
        if len(report.conflicting) > 10:
            print(f"    ... and {len(report.conflicting) - 10} more")

    if report.unmatched and not args.add_new:
        print(f"\n  {len(report.unmatched)} row(s) matched no stored school, e.g.:")
        for name in report.unmatched[:5]:
            print(f"    {name}")
        print("  re-run with --add-new to add them.")

    if args.dry_run:
        print("\ndry run — nothing written")
        return 0
    if not updates:
        print("\nnothing to write")
        return 0

    for school in updates:
        store.upsert(school)
    store.save()
    print(f"\nstore now holds {len(store.sorted_leads())} school(s) -> {settings.schools_path}")
    print("Regenerate the seed list with: scrapbot seeds --out data/seeds/all-schools.txt")
    return 0


def _failed_hosts(settings: Settings, statuses: list[str]) -> tuple[list[tuple[str, str]], int]:
    """Hosts that failed in some run and never succeeded in any.

    A site can fail in one batch and succeed in a later retry, so the whole
    run history is read before deciding — otherwise a second pass would keep
    chasing sites that are already done.
    """
    wanted = {s.lower() for s in statuses}
    failures: dict[str, str] = {}
    succeeded: set[str] = set()

    for report in sorted((settings.data_dir / "runs").glob("*/sites.json")):
        try:
            rows = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.warning("skipping unreadable run report %s", report)
            continue
        for row in rows if isinstance(rows, list) else rows.get("sites", []):
            domain, status = row.get("domain"), (row.get("status") or "").lower()
            if not domain:
                continue
            if status == "ok":
                succeeded.add(domain)
            elif status in wanted:
                failures[domain] = status

    still_failing = [(d, s) for d, s in sorted(failures.items()) if d not in succeeded]
    return still_failing, len(failures) - len(still_failing)


def cmd_seeds(args: argparse.Namespace) -> int:
    """Turn the school store into a seed file for `scrapbot run coaches`."""
    settings = settings_from_args(args)

    if args.from_failures is not None:
        statuses = args.from_failures or ["blocked", "error", "network"]
        hosts, recovered = _failed_hosts(settings, statuses)
        if not hosts:
            print(f"No unresolved {'/'.join(statuses)} failures in {settings.data_dir}/runs.")
            return 0
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            "\n".join(
                [f"# Hosts still failing ({', '.join(statuses)}) across all stored runs.", ""]
                + [f"{host}  # {status}" for host, status in hosts]
            )
            + "\n",
            encoding="utf-8",
        )
        counts: dict[str, int] = {}
        for _host, status in hosts:
            counts[status] = counts.get(status, 0) + 1
        print(f"wrote {len(hosts)} host(s) to {args.out}")
        for status, count in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"  {status:<10} {count}")
        if recovered:
            print(f"  ({recovered} earlier failure(s) succeeded later and were skipped)")
        return 0

    schools = storage.SchoolStore(settings).load().sorted_leads()
    if not schools:
        print(
            f"No schools stored yet in {settings.data_dir}. Try: scrapbot run schools",
            file=sys.stderr,
        )
        return 1

    if args.division:
        wanted = {models.normalize_division(d) for d in args.division}
        schools = [s for s in schools if s.division in wanted]
    if args.state:
        from . import usregions

        codes = {usregions.state_code(s) for s in args.state}
        schools = [s for s in schools if usregions.state_code(s.state) in codes]

    from .sources.website import normalize_domain

    lines = ["# Generated by `scrapbot seeds` from the school store.", ""]
    missing = 0
    fallbacks = 0
    for school in schools:
        host = school.athletics_domain
        if not host:
            # NAIA members carry no athletics URL, so seed the university host —
            # the coaches source follows the "Athletics" link from there.
            host = normalize_domain(school.website or "")
            if host:
                fallbacks += 1
        if host:
            lines.append(f"{host}  # {school.school}")
        else:
            missing += 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {len(lines) - 2} host(s) to {args.out}")
    if fallbacks:
        print(f"  ({fallbacks} used the university host — no athletics URL on record)")
    if missing:
        print(f"  ({missing} school(s) had no usable URL at all)")
    return 0


def _school_stats(settings: Settings) -> int:
    schools = storage.SchoolStore(settings).load().sorted_leads()
    if not schools:
        print(f"No schools stored yet in {settings.data_dir}. Try: scrapbot run schools")
        return 0

    complete = sum(1 for s in schools if s.totalYearlyCost and s.academicData)
    with_domain = sum(1 for s in schools if s.athletics_domain)

    print(f"{len(schools)} school(s) in {settings.schools_path}")
    print(f"    with cost+scores  : {complete} ({_pct(complete, len(schools))})")
    print(f"    with athletics site: {with_domain} ({_pct(with_domain, len(schools))})")

    by_division: dict[str, int] = {}
    for school in schools:
        if school.division:
            by_division[school.division] = by_division.get(school.division, 0) + 1
    for division, count in sorted(by_division.items()):
        print(f"    {division:<10} {count}")
    return 0


def _contact_stats(settings: Settings) -> int:
    contacts = storage.ContactStore(settings).load().sorted_leads()
    if not contacts:
        print(
            f"No contacts stored yet in {settings.data_dir}. "
            "Try: scrapbot run coaches --sites goduke.com"
        )
        return 0

    coaches = [c for c in contacts if c.is_coach]
    with_email = sum(1 for c in contacts if c.emails)
    with_phone = sum(1 for c in contacts if c.phones)
    schools = {c.school_domain for c in contacts}

    print(f"{len(contacts)} contact(s) from {len(schools)} school(s) in {settings.contacts_path}")
    print(f"  coaching roles  : {len(coaches)} ({_pct(len(coaches), len(contacts))})")
    print(f"  with email      : {with_email} ({_pct(with_email, len(contacts))})")
    print(f"  with phone      : {with_phone} ({_pct(with_phone, len(contacts))})")

    counts: dict[str, int] = {}
    for contact in contacts:
        if contact.sport:
            counts[contact.sport] = counts.get(contact.sport, 0) + 1
    if counts:
        print("\n  top sports:")
        for sport, count in sorted(counts.items(), key=lambda kv: -kv[1])[:10]:
            print(f"    {sport:<24} {count}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    settings = settings_from_args(args)
    if getattr(args, "schools", False):
        return _export_schools(args, settings)
    if getattr(args, "contacts", False):
        return _export_contacts(args, settings)
    leads = filter_leads(
        storage.LeadStore(settings).load().sorted_leads(),
        has_email=args.has_email,
        has_phone=args.has_phone,
        industry=args.industry,
        search=args.search,
    )
    out: Path = args.out
    if out.suffix.lower() == ".json":
        storage.write_json(out, [lead.to_dict() for lead in leads])
    else:
        storage.write_csv(out, leads)
    print(f"wrote {len(leads)} lead(s) to {out}")
    return 0


def _export_schools(args: argparse.Namespace, settings: Settings) -> int:
    schools = storage.SchoolStore(settings).load().sorted_leads()
    if args.division:
        wanted = {models.normalize_division(d) for d in args.division}
        schools = [s for s in schools if s.division in wanted]
    if args.search:
        needle = args.search.lower()
        schools = [s for s in schools if needle in json.dumps(s.to_dict()).lower()]

    out: Path = args.out
    if out.suffix.lower() == ".json":
        # The origin shape exactly: no bookkeeping fields, no id, no logo.
        storage.write_json(out, [s.to_origin_dict() for s in schools])
    else:
        storage.write_csv(out, schools, models.SCHOOL_COLUMNS)
    print(f"wrote {len(schools)} school(s) to {out}")
    return 0


def _export_contacts(args: argparse.Namespace, settings: Settings) -> int:
    contacts = filter_contacts(
        storage.ContactStore(settings).load().sorted_leads(),
        has_email=args.has_email,
        has_phone=args.has_phone,
        sport=args.sport,
        coaches_only=args.coaches_only,
        direct_email=args.direct_email,
        search=args.search,
    )
    out: Path = args.out
    if out.suffix.lower() == ".json":
        storage.write_json(out, [c.to_dict() for c in contacts])
    else:
        storage.write_csv(out, contacts, models.CONTACT_CSV_COLUMNS)
    print(f"wrote {len(contacts)} contact(s) to {out}")
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    settings = settings_from_args(args)
    try:
        import uvicorn
    except ImportError:
        print("uvicorn is not installed. Run: pip install -e .", file=sys.stderr)
        return 1
    from .web.app import create_app

    print(f"dashboard on http://{args.host}:{args.port}  (reading {settings.data_dir})")
    uvicorn.run(create_app(settings), host=args.host, port=args.port, log_level="warning")
    return 0


# --- helpers -------------------------------------------------------------

def filter_leads(
    leads: list[Lead],
    *,
    has_email: bool = False,
    has_phone: bool = False,
    industry: str | None = None,
    search: str | None = None,
) -> list[Lead]:
    out = leads
    if has_email:
        out = [lead for lead in out if lead.emails]
    if has_phone:
        out = [lead for lead in out if lead.phones]
    if industry:
        needle = industry.lower()
        out = [lead for lead in out if any(needle in hint.lower() for hint in lead.industry_hints)]
    if search:
        needle = search.lower()
        out = [lead for lead in out if needle in json.dumps(lead.to_dict()).lower()]
    return out


def filter_contacts(
    contacts: list[Contact],
    *,
    has_email: bool = False,
    has_phone: bool = False,
    sport: str | None = None,
    coaches_only: bool = False,
    direct_email: bool = False,
    search: str | None = None,
) -> list[Contact]:
    out = contacts
    if has_email:
        out = [c for c in out if c.emails]
    if direct_email:
        out = [c for c in out if c.emails and not c.shared_email]
    if has_phone:
        out = [c for c in out if c.phones]
    if coaches_only:
        out = [c for c in out if c.is_coach]
    if sport:
        # Canonical match, so picking "Basketball" returns its gendered
        # variants too and "Men's Basketball" narrows to one of them. Falls
        # back to a substring test, which is what department and free-text
        # values like "strength" still need.
        from .sports import matches as sport_matches

        out = [c for c in out if sport_matches(c.sport, sport, department=c.department)]
    if search:
        needle = search.lower()
        out = [c for c in out if needle in json.dumps(c.to_dict()).lower()]
    return out


def _pct(part: int, whole: int) -> str:
    return f"{(100 * part / whole):.0f}%" if whole else "0%"


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    level = logging.INFO
    if getattr(args, "verbose", False):
        level = logging.DEBUG
    elif getattr(args, "quiet", False):
        level = logging.WARNING
    logging.basicConfig(level=level, format="%(message)s", stream=sys.stderr)
    # httpx logs every request at INFO, which drowns out the run log.
    logging.getLogger("httpx").setLevel(logging.DEBUG if level == logging.DEBUG else logging.WARNING)

    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except storage.StoreBusy as exc:
        # Not a crash — the operator started a second run by mistake. Say so
        # plainly rather than showing them a traceback.
        print(f"\n{exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
