"""``coaches`` source — pull staff directories off college athletics sites.

Give it one or more athletics site URLs (``https://goduke.com/``). For each it
locates the staff directory, parses it **row by row**, and yields one
:class:`Contact` per person — name, title, sport, work email, work phone.

The row-wise parse is the whole point. The ``website`` source folds a page into
a single company record, which would turn 444 Duke staff into one lead holding
338 unrelated email addresses. Here, a person's email has to come from that
person's own row.

Most athletics sites run on a handful of platforms (Sidearm, PrestoSports,
WMT), so two layouts cover the bulk of them:

* **table view** — one ``<table>`` per sport/department, a header cell naming
  the group, then Name / Title / Email / Phone columns.
* **card view** — repeated per-person blocks, each with a heading and its own
  ``mailto:``/``tel:`` links.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Iterable
from urllib.parse import urljoin, urlparse

from selectolax.parser import HTMLParser

from .. import extract, storage
from ..models import Contact, SiteOutcome
from ..net import Fetcher, Page
from .base import Source
from .website import normalize_domain

log = logging.getLogger("scrapbot.coaches")


@dataclass
class Candidate:
    """A page that parsed into people, and how athletic it turned out to be.

    Discovery judges candidates by this, not by their URL. A university's
    ``/faculty-staff/`` page looks exactly like a staff directory from the
    outside — the difference only shows up once the rows are parsed and none of
    them coach anything.
    """

    page: Page
    contacts: list[Contact]
    school: str | None
    athletics: bool


# A real athletics directory attributes people to sports: Duke 46%, Kentucky
# 57%, Jacksonville State 60%. Campus directories score exactly 0% — they have
# no column for it.
#
# Coach *ratio* looks like it should work too and does not: Duke is 24.5%
# coaches and Kentucky 26.8%, but Andrew College's combined faculty list is
# 25.9%. The two are indistinguishable on that axis, so sport is the only test.
# Anything failing it still goes through _coaches_only(), which keeps the
# coaches and drops the professors.
SPORT_SHARE = 0.10


def is_athletics_directory(contacts: list[Contact]) -> bool:
    if not contacts:
        return False
    return sum(1 for c in contacts if c.sport) / len(contacts) >= SPORT_SHARE

# Tried in order against the site root before falling back to link scoring.
DIRECTORY_PATHS = [
    "/staff-directory",
    "/staff.aspx",
    "/coaches",
    "/staff",
    "/sports/staff-directory",
    "/information/directory",
    "/athletics/staff-directory",
]

# Anchor text / hrefs that lead to a staff directory, best first.
DIRECTORY_HINTS = [
    ("staff-directory", 100),
    ("staff directory", 100),
    ("staff.aspx", 95),
    ("coaching-staff", 90),
    ("coaches", 80),
    ("directory", 70),
    ("staff", 60),
]

# Column headers we understand, mapped to the Contact field they fill.
COLUMN_ALIASES = {
    "name": "name",
    "full name": "name",
    "staff member": "name",
    "title": "title",
    "position": "title",
    "job title": "title",
    "role": "title",
    "email": "email",
    "e-mail": "email",
    "email address": "email",
    "phone": "phone",
    "telephone": "phone",
    "phone number": "phone",
    "office phone": "phone",
    "contact": "phone",
}

# A group heading is a sport if it looks like one; otherwise it's a department.
SPORT_WORDS = (
    "baseball", "basketball", "beach volleyball", "bowling", "cheerleading",
    "cross country", "equestrian", "fencing", "field hockey", "flag football",
    "football", "golf", "gymnastics", "ice hockey", "lacrosse", "rifle",
    "rowing", "rugby", "sailing", "skiing", "soccer", "softball", "squash",
    "swimming", "diving", "tennis", "track & field", "track and field",
    "triathlon", "volleyball", "water polo", "wrestling", "crew",
)

# Every sport name, longest first so "flag football" wins over "football" and
# "beach volleyball" over "volleyball", with an optional gender qualifier in
# front. Used to read the sport back out of a job title.
_GENDER = r"men's|women's|mens|womens|men|women|boys|girls"
_GENDER_RE = re.compile(rf"\b({_GENDER})\b", re.I)
# Longest sport first, so "flag football" beats "football" and "beach
# volleyball" beats "volleyball".
_SPORTS = "|".join(
    re.escape(w).replace(r"\ ", r"\s+") for w in sorted(SPORT_WORDS, key=len, reverse=True)
)
# Three groups: a shared-program qualifier ("Men's *and* Women's Cross Country"
# is one coach over two teams, and matching only the adjacent one lost the men),
# the adjacent qualifier, then the sport.
_SPORT_IN_TITLE_RE = re.compile(
    rf"\b(?:({_GENDER})\s*(?:and|&|/)\s*)?(?:({_GENDER})\s+)?({_SPORTS})\b", re.I
)


def _gender(qualifier: str | None) -> str | None:
    """``Men's``/``mens``/``boys`` -> ``Men``; anything else -> None."""
    word = (qualifier or "").lower().rstrip("'s").rstrip("s")
    return {"men": "Men", "boy": "Men", "women": "Women", "girl": "Women"}.get(word)


def sport_from_title(title: str | None) -> str | None:
    """Read the sport out of a job title, for directories that have no column.

    A college with no athletics site of its own lists its coaches on the campus
    staff page, where the only thing said about the job is the title — see
    :func:`_coaches_only`. "Head Baseball Coach" is still unambiguous, and one
    person often covers two programs ("Head Men's Soccer Coach Head Women's
    Soccer Coach"), so every distinct sport named is kept, in the same
    semicolon-joined form a directory's own grouping uses.
    """
    if not title:
        return None
    # Directories overwhelmingly use a typographic apostrophe, so "Men’s
    # Wrestling" would otherwise lose its qualifier and come out as "Wrestling".
    text = title.replace("’", "'").replace("ʼ", "'").replace("‘", "'")

    # Keyed on sport *and* gender: one person often runs both programs, and
    # "Head Men's Soccer Coach Head Women's Soccer Coach" is two jobs, not one.
    found: list[tuple[str, str | None]] = []
    for shared, adjacent, sport in _SPORT_IN_TITLE_RE.findall(text):
        sport = re.sub(r"\s+", " ", sport).strip().title()
        # "Track & Field" and "track and field" are the same program.
        sport = sport.replace(" And ", " & ")
        for qualifier in (shared, adjacent) if shared else (adjacent,):
            pair = (sport, _gender(qualifier))
            if pair not in found:
                found.append(pair)

    # "Women's Asst. Basketball Coach" puts the qualifier a word or two away
    # from the sport. When the title names exactly one gender and nothing was
    # matched adjacently, that gender can only belong to the sport(s) named.
    if found and not any(gender for _, gender in found):
        genders = {_gender(q) for q in _GENDER_RE.findall(text)} - {None}
        if len(genders) == 1:
            only = genders.pop()
            found = [(sport, only) for sport, _ in found]

    return "; ".join(
        f"{gender}'s {sport}" if gender else sport for sport, gender in found
    ) or None

# Titles that make someone a coach rather than support staff.
COACH_TITLE_RE = re.compile(r"\bcoach(?:es|ing)?\b", re.I)

# ...except when the title is support *for* a coach. "Executive Assistant to
# the Head Coach" and "Director of Basketball Operations" both mention coaching
# without being coaching roles.
NOT_COACH_TITLE_RE = re.compile(
    r"\bassistant to\b|\bsecretary\b|\bcoordinator of coaching\b", re.I
)


def is_coaching_title(title: str | None) -> bool:
    text = title or ""
    return bool(COACH_TITLE_RE.search(text)) and not NOT_COACH_TITLE_RE.search(text)


# A department banner a card layout runs into the job title. The email and phone
# already have their own columns, so repeating them in the title is pure noise.
# Kept deliberately narrow. Widening it to "Academic" ate the first word of
# "Academic Support Center Professional Tutor", and matching the singular ate
# the "Athletic" of "Athletic Director" — plural is the banner, singular is an
# adjective inside a real job title.
_TITLE_SECTION_RE = re.compile(
    r"^athletics(?:\s+department)?\b[\s:|/·—–-]*", re.I
)
# "Contact:", "Email:", "Phone:" left behind once the value itself is gone. The
# colon is required, so "Office Manager" survives and "Office:" does not.
_TITLE_LABEL_RE = re.compile(
    r"\b(?:e-?mail(?:\s*address)?|phone(?:\s*number)?|telephone|tel|office|"
    r"cell|mobile|fax|contact)\s*[:.]\s*", re.I
)


def clean_title(text: str | None) -> str | None:
    """Strip the contact details a directory crams into the title cell.

    Card layouts with no internal markup collapse a whole block into one
    string, so the title arrived as ``ATHLETICS Head Baseball Coach
    229-732-5901 adambiss@andrewcollege.edu``. The address and number are
    already parsed into their own fields; what belongs here is the job.
    """
    if not text:
        return None
    cleaned = extract.EMAIL_RE.sub(" ", text)
    cleaned = extract.PHONE_RE.sub(" ", cleaned)
    cleaned = _TITLE_LABEL_RE.sub(" ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" \t-–—·|,;:/")

    # Drop a leading section banner only when a job title is left without it.
    # "Athletics Director" keeps its first word; "ATHLETICS Head Coach" does not
    # need one, because the department already has its own column.
    without = _TITLE_SECTION_RE.sub("", cleaned)
    if without != cleaned and len(without.split()) >= 2:
        cleaned = without.strip(" \t-–—·|,;:/")
    return cleaned or None

# Row-level noise: a directory row that is really a heading or a spacer.
SKIP_NAME_RE = re.compile(r"^(?:name|staff|full name|\s*)$", re.I)

# Anchor text on a university homepage that points at the athletics site.
ATHLETICS_LINK_RE = re.compile(r"\bathletics\b|\bvarsity sports\b|\bgo\s+\w+s\b", re.I)


class CoachesSource(Source):
    name = "coaches"
    help = "Scrape college/university athletics staff directories, one record per person."
    record_cls = Contact

    def __init__(self, settings, args) -> None:
        super().__init__(settings, args)
        self._host_map: dict[str, str] | None = None
        # Hosts already given one browser attempt after the static HTML parsed
        # to nobody, so a site that needs a browser costs one render, not one
        # per candidate path we try on it.
        self._render_tried: set[str] = set()

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--seeds",
            type=Path,
            help="File with one athletics site URL per line ('#' comments allowed). "
            "Use '-' to read from stdin.",
        )
        parser.add_argument(
            "--sites",
            nargs="+",
            default=[],
            metavar="URL",
            help="Athletics site URLs to scrape, in addition to --seeds.",
        )
        parser.add_argument(
            "--directory-url",
            nargs="+",
            default=[],
            metavar="URL",
            help="Skip discovery and parse these staff-directory URLs directly.",
        )
        parser.add_argument(
            "--coaches-only",
            action="store_true",
            help="Yield only people whose title reads like a coaching role.",
        )
        parser.add_argument(
            "--sport",
            help="Only people whose sport/department matches this (substring, case-insensitive).",
        )
        parser.add_argument(
            "--limit", type=int, default=0, help="Stop after this many sites (0 = no limit)."
        )
        parser.add_argument(
            "--manual-dir",
            type=Path,
            metavar="DIR",
            help="Parse staff-directory pages you saved from your own browser. "
            "Layout: DIR/<athletics-domain>/<anything>.html — the folder name "
            "says which school the page belongs to. Use this for sites that "
            "refuse automated requests.",
        )
        parser.add_argument(
            "--save-photos",
            action="store_true",
            help="Download coach headshots into <data-dir>/photos/. Off by default: "
            "it roughly doubles the requests per site. The photo URL is always "
            "recorded either way.",
        )

    # -- seeds ------------------------------------------------------------
    def _load_seeds(self) -> list[str]:
        raw: list[str] = list(self.args.sites or [])

        seeds_path: Path | None = getattr(self.args, "seeds", None)
        if seeds_path is not None:
            if str(seeds_path) == "-":
                raw.extend(sys.stdin.read().splitlines())
            elif seeds_path.exists():
                # utf-8-sig: seed files written by Notepad, Excel or PowerShell
                # start with a BOM, which would otherwise ride along into the
                # first hostname and make it an invalid IDNA name.
                raw.extend(seeds_path.read_text(encoding="utf-8-sig").splitlines())
            else:
                raise SystemExit(f"seed file not found: {seeds_path}")

        seen: dict[str, None] = {}
        for line in raw:
            line = line.split("#", 1)[0].strip().lstrip("﻿").strip()
            if not line:
                continue
            domain = normalize_domain(line)
            if domain:
                seen.setdefault(domain, None)

        seeds = list(seen)
        if not seeds and not self.args.directory_url and not getattr(
            self.args, "manual_dir", None
        ):
            raise SystemExit(
                "no seeds given — pass --seeds FILE, --sites goduke.com, "
                "--directory-url URL, or --manual-dir DIR"
            )
        limit = self.args.limit or 0
        return seeds[:limit] if limit > 0 else seeds

    # -- run --------------------------------------------------------------
    def _manual_pages(self) -> list[tuple[str, Path]]:
        """``(domain, file)`` for every page saved under ``--manual-dir``.

        The folder name is the school's host, so a saved page needs no flags
        and no naming convention beyond the directory it sits in.
        """
        root: Path | None = getattr(self.args, "manual_dir", None)
        if root is None:
            return []
        if not root.is_dir():
            raise SystemExit(f"--manual-dir is not a directory: {root}")

        pages: list[tuple[str, Path]] = []
        for folder in sorted(p for p in root.iterdir() if p.is_dir()):
            domain = normalize_domain(folder.name)
            if not domain:
                log.warning("skipping %s — folder name is not a hostname", folder)
                continue
            files = sorted(
                f for f in folder.iterdir()
                if f.is_file() and f.suffix.lower() in (".html", ".htm")
            )
            if not files:
                log.warning("no .html saved under %s", folder)
            pages.extend((domain, f) for f in files)

        loose = [f for f in root.iterdir() if f.is_file() and f.suffix.lower() in (".html", ".htm")]
        if loose:
            log.warning(
                "%d .html file(s) sit directly in %s and were skipped — put each "
                "page in a folder named after its athletics host",
                len(loose), root,
            )
        return pages

    def scrape_saved(self, domain: str, path: Path) -> list[Contact]:
        """Parse a page fetched by a person in their own browser.

        No relevance gate here: choosing to save this page *is* the judgement
        that it is the right one. Links are resolved against the site's own
        host so profile URLs and headshots still come out absolute.
        """
        html = path.read_text(encoding="utf-8", errors="replace")
        base_url = f"https://{domain}/"
        tree = extract.parse(html)
        school = _school_name(tree, domain)
        contacts = parse_directory(tree, base_url, domain, school, self.name)
        _flag_shared_emails(contacts)

        if not contacts:
            log.warning("%s parsed to 0 people — is it the staff directory page?", path)
            self.record(
                SiteOutcome(
                    domain=domain,
                    status=SiteOutcome.EMPTY,
                    detail=f"saved page {path.name} had no staff rows",
                    url=str(path),
                    school=school,
                )
            )
            return []

        with_email = sum(1 for c in contacts if c.emails)
        log.info(
            "%s: %d people (%d with email) from saved page %s",
            domain, len(contacts), with_email, path.name,
        )
        self.record(
            SiteOutcome(
                domain=domain,
                status=SiteOutcome.OK,
                detail=f"{with_email} with an email address (saved page)",
                people=len(contacts),
                url=str(path),
                school=school,
            )
        )
        return contacts

    async def run(self, fetcher: Fetcher) -> AsyncIterator[Contact]:
        for domain, path in self._manual_pages():
            for contact in self.scrape_saved(domain, path):
                if self._wanted(contact):
                    yield contact

        seeds = self._load_seeds()
        jobs: list[tuple[str, str | None]] = [(d, None) for d in seeds]
        for url in self.args.directory_url or []:
            domain = normalize_domain(url)
            if domain:
                jobs.append((domain, url))

        if not jobs:
            return  # a manual-only run: everything was parsed from disk above
        log.info("scraping %d athletics site(s) with concurrency %d",
                 len(jobs), self.settings.concurrency)
        semaphore = asyncio.Semaphore(self.settings.concurrency)

        async def worker(domain: str, direct: str | None) -> list[Contact]:
            async with semaphore:
                try:
                    return await self.scrape_site(fetcher, domain, direct)
                except Exception as exc:  # one broken site must not kill the run
                    log.exception("unhandled error scraping %s", domain)
                    self.record(
                        SiteOutcome(
                            domain=domain,
                            status=SiteOutcome.ERROR,
                            detail=f"{type(exc).__name__}: {exc}",
                        )
                    )
                    return []

        tasks = [asyncio.create_task(worker(d, u)) for d, u in jobs]
        try:
            for finished in asyncio.as_completed(tasks):
                for contact in await finished:
                    if self._wanted(contact):
                        yield contact
        finally:
            for task in tasks:
                task.cancel()

    def _wanted(self, contact: Contact) -> bool:
        if self.args.coaches_only and not contact.is_coach:
            return False
        needle = (self.args.sport or "").strip().lower()
        if needle:
            haystack = f"{contact.sport or ''} {contact.department or ''}".lower()
            if needle not in haystack:
                return False
        return True

    # -- university host -> athletics host ---------------------------------
    def _athletics_host_map(self) -> dict[str, str]:
        """``jsu.edu -> jaxstatesports.com``, from the school store.

        The NCAA directory records both hosts for every member, so once
        ``scrapbot run schools`` has been run, a university URL resolves to the
        athletics site without a single request.
        """
        if self._host_map is None:
            self._host_map = {}
            try:
                schools = storage.SchoolStore(self.settings).load().sorted_leads()
            except Exception:  # a missing/unreadable store is not fatal here
                return self._host_map
            for school in schools:
                site = normalize_domain(school.website or "")
                if site and school.athletics_domain and site != school.athletics_domain:
                    self._host_map[site] = school.athletics_domain
        return self._host_map

    def _university_host_map(self) -> dict[str, str]:
        """``wranglersports.net -> cisco.edu`` — the map read backwards.

        Some athletics platforms (PrestoSports behind CloudFront) answer 403 to
        anything that isn't a browser, even where robots.txt allows the page.
        We don't work around that. But the college's own campus directory is
        usually readable and lists the coaches among its staff, so a blocked
        athletics host is a reason to try the university, not to give up.
        """
        return {
            athletics: site for site, athletics in self._athletics_host_map().items()
        }

    # -- per-site ---------------------------------------------------------
    async def scrape_site(
        self, fetcher: Fetcher, domain: str, direct_url: str | None = None
    ) -> list[Contact]:
        seed = domain
        if not direct_url:
            mapped = self._athletics_host_map().get(domain)
            if mapped:
                log.info("%s is a university host — using athletics site %s", domain, mapped)
                domain = mapped

        attempts: list[Page] = []
        if direct_url:
            # An explicit --directory-url is the operator's decision; take the
            # page as given rather than second-guessing whether it's athletic.
            got = await fetcher.get(direct_url)
            attempts.append(got)
            found = await self._candidate(got, domain, fetcher) if got.ok else None
        else:
            found = await self._find_directory(fetcher, domain, attempts=attempts)

            if found is None and _failure_outcome(seed, attempts).status in (
                SiteOutcome.BLOCKED,
                SiteOutcome.ROBOTS,
            ):
                university = self._university_host_map().get(domain)
                if university and university != domain:
                    log.info(
                        "%s refused us — trying the college's own site %s",
                        domain, university,
                    )
                    found = await self._find_directory(
                        fetcher, university, attempts=attempts
                    )
                    if found is not None:
                        domain = university

        if found is None:
            self.record(_failure_outcome(seed, attempts))
            return []

        page = found.page
        school = found.school
        contacts = found.contacts
        _flag_shared_emails(contacts)
        if getattr(self.args, "save_photos", False):
            await self._save_photos(fetcher, contacts)

        if not contacts:
            log.info("staff directory at %s parsed to 0 people", page.url)
            self.record(
                SiteOutcome(
                    domain=seed,
                    status=SiteOutcome.EMPTY,
                    detail="page fetched but no staff rows recognised",
                    url=page.url,
                    school=school,
                )
            )
        else:
            with_email = sum(1 for c in contacts if c.emails)
            kind = "athletics directory" if found.athletics else "general directory, coaches only"
            log.info(
                "%s: %d people (%d with email) from %s [%s]",
                domain, len(contacts), with_email, page.url, kind,
            )
            self.record(
                SiteOutcome(
                    domain=seed,
                    status=SiteOutcome.OK,
                    detail=f"{with_email} with an email address ({kind})",
                    people=len(contacts),
                    url=page.url,
                    school=school,
                )
            )
        return contacts

    async def _save_photos(self, fetcher: Fetcher, contacts: list[Contact]) -> None:
        """Download the headshots for one site, sequentially.

        Sequential on purpose: these all come from the same host as the
        directory we just read, and firing 80 image requests at a college's
        server in parallel is exactly the behaviour that gets a scraper blocked.
        """
        saved = 0
        for contact in contacts:
            if not contact.photo_url or contact.photo_file:
                continue
            data = await fetcher.get_bytes(contact.photo_url)
            if not data:
                continue
            suffix = Path(urlparse(contact.photo_url).path).suffix.lower()
            if suffix not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
                suffix = ".jpg"
            slug = re.sub(r"[^a-z0-9]+", "-", contact.name.lower()).strip("-") or "unnamed"
            # A hostname can carry a port, and ':' is not legal in a Windows
            # path — the directory creation fails outright rather than degrading.
            folder = re.sub(r"[^a-z0-9.-]+", "-", contact.school_domain.lower()).strip("-.")
            rel = Path("photos") / (folder or "unknown") / f"{slug}{suffix}"
            target = self.settings.data_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            contact.photo_file = rel.as_posix()
            saved += 1
        if saved:
            log.info("saved %d headshot(s) to %s", saved, self.settings.data_dir / "photos")

    async def _candidate(
        self, page: Page, domain: str, fetcher: Fetcher | None = None
    ) -> Candidate | None:
        """Parse a fetched page and judge whether it is an athletics directory.

        A page that answers 200 and parses to nobody is the signature of a
        script-built directory — Arizona State serves 375 people that way and
        none of them are in the static HTML. That is a far sharper trigger than
        ``render=auto``'s visible-text heuristic, which measures the page
        *chrome* rather than the staff table: ASU's shell carries 1,639
        characters and so never tripped the 600-character threshold.
        """
        found = self._parse_page(page, domain)
        if found is not None or fetcher is None:
            return found

        # Nothing on the static HTML. One browser attempt, then give up: the
        # memo on the fetcher stops a hostile host from being retried per page.
        if page.rendered or domain in self._render_tried:
            return None
        self._render_tried.add(domain)
        rendered = await fetcher.get_rendered(page.url)
        if not rendered.ok:
            return None
        found = self._parse_page(rendered, domain)
        if found is not None:
            log.info("%s only lists its staff once rendered", page.url)
        return found

    def _parse_page(self, page: Page, domain: str) -> Candidate | None:
        if not _looks_like_directory(page.html):
            return None
        tree = extract.parse(page.html)
        school = _school_name(tree, domain)
        contacts = parse_directory(tree, page.url, domain, school, self.name)
        if not contacts:
            return None
        return Candidate(
            page=page,
            contacts=contacts,
            school=school,
            athletics=is_athletics_directory(contacts),
        )

    async def _find_directory(
        self, fetcher: Fetcher, domain: str, hop: int = 0,
        attempts: list[Page] | None = None,
        fallback: list[Candidate] | None = None,
    ) -> Candidate | None:
        """Try the well-known paths, then fall back to scoring homepage links.

        A candidate is accepted only once it *parses* into athletics staff. The
        first version returned any page that looked like a directory, so
        aquinas.edu matched its own ``/faculty-staff/`` page and never got as
        far as the athletics site at aqsaints.com — 151 professors, no coaches.

        A general campus directory is kept aside in ``fallback`` rather than
        discarded: small colleges list their coaches there and nowhere else.
        It's only used if the athletics hunt comes up empty, and then only for
        the people whose titles say they coach.

        Every response is appended to ``attempts`` so the caller can report
        *why* nothing was found rather than just reporting zero.
        """
        seen: list[Page] = attempts if attempts is not None else []
        general: list[Candidate] = fallback if fallback is not None else []
        base = await self._working_base(fetcher, domain, seen)
        if base is None:
            return None

        async def consider(page: Page) -> Candidate | None:
            found = await self._candidate(page, domain, fetcher)
            if found is None:
                return None
            if found.athletics:
                return found
            log.debug("%s is a general directory, not athletics", page.url)
            general.append(found)
            return None

        for path in DIRECTORY_PATHS:
            page = await fetcher.get(f"{base}{path}")
            seen.append(page)
            if page.ok and (hit := await consider(page)) is not None:
                return hit
            if page.robots_blocked:  # stop asking; the answer will not change
                log.info("robots.txt disallows the staff directory on %s", domain)
                return None

        home = await fetcher.get(f"{base}/")
        seen.append(home)
        if home.ok:
            tree = extract.parse(home.html)
            for _score, url in _directory_links(tree, home.url, domain):
                page = await fetcher.get(url)
                seen.append(page)
                if page.ok and (hit := await consider(page)) is not None:
                    return hit

            # This may be a university host whose athletics site lives on
            # another domain and isn't in the school store yet. One hop only —
            # an athletics site links back to the university, and without a cap
            # the two would bounce us around forever.
            athletics = _athletics_host(tree, home.url, domain) if hop == 0 else None
            if athletics:
                log.info("following the athletics site link from %s to %s", domain, athletics)
                hit = await self._find_directory(
                    fetcher, athletics, hop=1, attempts=seen, fallback=general
                )
                if hit is not None:
                    return hit

            # Some programs publish no combined directory at all — Arizona and
            # Cal State Fullerton put the staff on one page per sport instead.
            # Walking those is the only way to see their coaches, so it runs
            # last: it costs one request per sport.
            hit = await self._sport_coach_pages(fetcher, base, domain, tree, seen)
            if hit is not None:
                return hit

        return _coaches_only(general, domain)

    async def _sport_coach_pages(
        self, fetcher: Fetcher, base: str, domain: str, home_tree, seen: list[Page]
    ) -> Candidate | None:
        """Collect ``/sports/<sport>/coaches`` pages into one candidate."""
        slugs = _sport_slugs(home_tree)
        if not slugs:
            return None

        budget = max(0, self.settings.max_pages_per_site - len(seen))
        if budget <= 0:
            return None
        if len(slugs) > budget:
            log.info(
                "%s: %d sports but only %d request(s) of budget left; "
                "raise --max-pages to cover them all",
                domain, len(slugs), budget,
            )
            slugs = slugs[:budget]

        contacts: list[Contact] = []
        school: str | None = None
        first: Page | None = None
        for slug in slugs:
            page = await fetcher.get(f"{base}/sports/{slug}/coaches")
            seen.append(page)
            if not page.ok or not _looks_like_directory(
                page.html, MIN_SPORT_PAGE_SIGNALS
            ):
                continue
            tree = extract.parse(page.html)
            school = school or _school_name(tree, domain)
            found = parse_directory(tree, page.url, domain, school, self.name)
            if found:
                first = first or page
                contacts.extend(found)

        if not contacts or first is None:
            return None
        contacts = _dedupe(contacts)
        log.info("%s: %d coach(es) across %d sport page(s)", domain, len(contacts), len(slugs))
        return Candidate(
            page=first,
            contacts=contacts,
            school=school,
            athletics=is_athletics_directory(contacts),
        )

    async def _working_base(
        self, fetcher: Fetcher, domain: str, seen: list[Page]
    ) -> str | None:
        """Settle scheme *and* host once, e.g. ``https://www.baynorse.com``.

        Two things are being decided here. https-vs-http, so a plain-HTTP host
        isn't probed twice for every candidate path — and bare-vs-``www``,
        because they are not always the same server: baynorse.com resolves to
        the college's own box (which refuses connections) while
        www.baynorse.com is on CloudFront. Stripping the ``www`` and stopping
        there reported the site as a network failure when the truth was a 403,
        which is a different problem with a different fix.
        """
        hosts = [domain]
        if not domain.startswith("www."):
            hosts.append(f"www.{domain}")

        for host in hosts:
            for scheme in ("https", "http"):
                page = await fetcher.get(f"{scheme}://{host}/")
                seen.append(page)
                if page.ok:
                    if host != domain:
                        log.info("%s only answers as %s", domain, host)
                    return f"{scheme}://{host}"
                if page.robots_blocked:
                    log.info("robots.txt disallows %s", host)
                    return None
                if page.blocked:
                    # A 403 is an answer: this host exists and is refusing us.
                    # Trying the other spelling would only collect another 403.
                    return None
        return None


# --- parsing -------------------------------------------------------------

def parse_directory(
    tree: HTMLParser, base_url: str, domain: str, school: str | None, source: str
) -> list[Contact]:
    """Parse a staff-directory page into one :class:`Contact` per person."""
    contacts = _parse_tables(tree, base_url, domain, school, source)
    if not contacts:
        # Sidearm's current template, before the generic card fallback: its
        # cards carry no mailto at all, which _parse_cards requires, so they
        # would otherwise be skipped one by one and the page read as empty.
        contacts = _parse_person_cards(tree, base_url, domain, school, source)
    if not contacts:
        contacts = _parse_cards(tree, base_url, domain, school, source)
    return _dedupe(contacts)


def _parse_tables(
    tree: HTMLParser, base_url: str, domain: str, school: str | None, source: str
) -> list[Contact]:
    out: list[Contact] = []
    for table in tree.css("table"):
        rows = table.css("tbody tr") or [r for r in table.css("tr") if r.css("td")]
        headers = _header_cells(table)
        mapping, group = _read_headers(headers, _body_width(rows))
        if "name" not in mapping.values():
            continue
        group = group or _table_group(table) or _preceding_heading(table)

        current = group
        for row in rows:
            # Some directories are one long table whose sections are marked by a
            # single-cell row ("Administration", "Men's Basketball") rather than
            # by a separate table per sport. Kentucky is built this way; Duke is
            # not. Treat such a row as the running group, not as a person.
            section = _section_label(row)
            if section is not None:
                current = section
                continue
            contact = _row_to_contact(row, mapping, current, base_url, domain, school, source)
            if contact is not None:
                out.append(contact)
    return out


def _section_label(row) -> str | None:
    """The group name if this row is a section divider, else None.

    Kentucky marks its sections with a single ``<td>``; Sidearm (aqsaints.com)
    uses a single ``<th>``. Missing the ``<th>`` form cost every person their
    sport, which in turn made the page read as a non-athletics directory.
    """
    cells = row.css("td") or row.css("th")
    if len(cells) != 1 or row.css("a[href]"):
        return None
    text = _text(cells[0])
    return text if text and len(text) <= 80 else None


# Sidearm splits addresses across two JS variables so they don't sit in the
# HTML as text. The halves are right there in the row's own <script>, so this
# needs no browser — and the address is the point of the whole exercise.
_SPLIT_EMAIL_RE = re.compile(
    r"""firstHalf\s*=\s*["']([^"']+)["'].*?secondHalf\s*=\s*["']([^"']+)["']""",
    re.S | re.I,
)


# Directories fill empty headshot slots with a stock silhouette or a logo.
# Storing those as "the coach's photo" would be worse than storing nothing.
_PLACEHOLDER_IMAGE_RE = re.compile(
    r"placeholder|no[-_]?(?:photo|image|headshot)|default|silhouette|blank|"
    r"generic|spacer|logo|avatar",
    re.I,
)


def _row_photo(row, base_url: str) -> str | None:
    """The headshot in this person's row, if it has a real one."""
    for img in row.css("img"):
        attrs = img.attributes
        src = attrs.get("data-src") or attrs.get("src") or ""
        if not src or src.startswith("data:"):
            continue
        if _PLACEHOLDER_IMAGE_RE.search(src) or _PLACEHOLDER_IMAGE_RE.search(
            attrs.get("alt") or ""
        ):
            continue
        return urljoin(base_url, src).split("?")[0]
    return None


def _emails_from_script(row) -> list[str]:
    out: list[str] = []
    for script in row.css("script"):
        for first, second in _SPLIT_EMAIL_RE.findall(script.text() or ""):
            out.append(f"{first}@{second}")
    return out


def _header_cells(table) -> list[str]:
    """The column headers, from the header row only.

    ``table.css("th")`` sweeps up every ``<th>`` in the table, and Sidearm marks
    each section ("Adminstration", "Campus Ministry", …) with a one-cell ``<th>``
    row. On aqsaints.com that turned a 5-column header into a 12-entry list and
    threw the column mapping right off — names came out as email fragments.
    """
    head = table.css("thead tr")
    for row in head or table.css("tr"):
        cells = row.css("th")
        if len(cells) >= 2:  # a section divider is a single cell; a header isn't
            return [_text(c) for c in cells]
    return []


def _table_group(table) -> str | None:
    """The sport/department this table is for, from its caption row.

    Duke gives each table a one-cell ``<th>`` row ("Men's Basketball") above the
    real header row. Excluding those from the column headers is right, but the
    label still has to be read — without it every Duke coach loses their sport,
    and the page then scores as a non-athletics directory.
    """
    caption = table.css_first("caption")
    if caption is not None and (text := _text(caption)):
        return text if _usable_group(text) else None
    for row in table.css("tr"):
        cells = row.css("th")
        if len(cells) == 1 and not row.css("td"):
            text = _text(cells[0])
            if _usable_group(text):
                return text
    return None


def _body_width(rows) -> int:
    """The most common cell count across body rows."""
    counts: dict[int, int] = {}
    for row in rows:
        n = len(row.css("td"))
        if n > 1:
            counts[n] = counts.get(n, 0) + 1
    return max(counts, key=counts.get) if counts else 0


def _read_headers(headers: list[str], body_width: int = 0) -> tuple[dict[int, str], str | None]:
    """Map column index -> field name, and pull out the group label if present.

    Sidearm puts the sport/department in the first header cell, so a header row
    of ``["Men's Basketball", "Name", "Title", "Email", "Phone"]`` yields the
    group *and* the column layout.
    """
    mapping: dict[int, str] = {}
    group: str | None = None
    # Header columns only shift when the header row is *wider* than the body —
    # that's the Sidearm group cell, which body rows don't repeat. Deriving the
    # shift from the width difference keeps honest layouts honest: aqsaints has
    # a headerless image column, so its 5 headers line up 1:1 with 5 cells and
    # must not shift, while "Sport | Name | Title | Email" (5 headers, 4 cells)
    # must shift by one.
    #
    # The old rule counted every unknown header instead, which on a table like
    # "Name | Title | Department | Email" produced a negative index — cells[-3]
    # on a short row raises IndexError, and the `i < len(cells)` guard below
    # cannot catch it.
    offset = max(0, len(headers) - body_width) if body_width else 0
    for index, header in enumerate(headers):
        field = COLUMN_ALIASES.get(header.strip().lower())
        if field:
            mapping[index - offset] = field
        elif index < offset and group is None and header.strip():
            # Only a header the body rows do NOT repeat can be the group — that
            # is the Sidearm sport cell. Treating any unrecognised header as the
            # group made a campus directory's "Location" or "Department" column
            # the sport of every person under it (cisco.edu, bigbend.edu).
            group = header.strip()
    return mapping, group


def _row_to_contact(
    row, mapping: dict[int, str], group: str | None, base_url: str,
    domain: str, school: str | None, source: str,
) -> Contact | None:
    cells = row.css("td")
    if not cells:
        return None
    # Read the scripts before _text() strips them out of the cells.
    scripted = _emails_from_script(row)
    photo = _row_photo(row, base_url)
    values = {
        field: _text(cells[i]) for i, field in mapping.items() if 0 <= i < len(cells)
    }

    name = values.get("name", "")
    if not name or SKIP_NAME_RE.match(name):
        return None

    contact = _make(name, group, domain, school, source)
    contact.title = clean_title(values.get("title"))
    contact.photo_url = photo
    for addr in scripted:
        _add_email(contact, addr)

    # Prefer the row's own mailto:/tel: links over its rendered text — the text
    # is sometimes an icon or an "Email" placeholder.
    for node in row.css("a[href]"):
        href = node.attributes.get("href") or ""
        if href.startswith("mailto:"):
            _add_email(contact, href[len("mailto:"):].split("?")[0])
        elif href.startswith("tel:"):
            _add_phone(contact, href[len("tel:"):].split("?")[0])
        elif not contact.profile_url and _is_profile_link(href, domain):
            contact.profile_url = urljoin(base_url, href).split("?")[0]

    for addr in extract.EMAIL_RE.findall(values.get("email", "")):
        _add_email(contact, addr)
    _add_phone(contact, values.get("phone", ""))

    return contact


# Sidearm's web-component staff directory. Every large athletics site now
# renders this way: one card per person, no table, and — the reason the older
# card reader misses it — no mailto anywhere on the page. Names, titles and
# profile links are all present in the static HTML, so the page is readable
# without a browser once the right nodes are picked out.
# Tried in order, first one that matches wins. They are *not* combined into one
# comma selector: both match the same element, and selectolax hands back a
# fresh wrapper object each time, so identity checks can't spot the duplicate
# and every person comes out twice.
_CARD_ROOTS = (
    '[data-test-id="s-person-card-list__root"]',
    '[class*="s-person-card--list"]',
    '[class*="s-person-card"]',
)
_CARD_NAME = '[data-test-id="s-person-details__personal-single-line"]'
_CARD_TITLE = '[class*="s-person-details__position"]'


def _parse_person_cards(
    tree: HTMLParser, base_url: str, domain: str, school: str | None, source: str
) -> list[Contact]:
    """Parse the Sidearm person-card layout (Texas, Georgia, Kansas State…)."""
    cards = []
    for selector in _CARD_ROOTS:
        cards = tree.css(selector)
        if cards:
            break

    out: list[Contact] = []
    seen_nodes: set[int] = set()

    for node in cards:
        # A card can still nest inside an outer wrapper the same selector
        # matches; keep the outermost so a person isn't emitted twice.
        if any(id(ancestor) in seen_nodes for ancestor in _ancestors(node)):
            continue
        seen_nodes.add(id(node))

        name_node = node.css_first(_CARD_NAME)
        name = name_node.text(strip=True) if name_node else None
        if not name:
            continue

        contact = _make(name, _preceding_heading(node), domain, school, source)

        title_node = node.css_first(_CARD_TITLE)
        if title_node:
            contact.title = clean_title(title_node.text(separator=" ", strip=True))

        for link in node.css("a[href]"):
            href = link.attributes.get("href") or ""
            if not contact.profile_url and _is_profile_link(href, domain):
                contact.profile_url = urljoin(base_url, href).split("?")[0]

        # Email and phone are usually absent here, but a few builds still
        # include them; take them when they are there.
        for mail in node.css('a[href^="mailto:"]'):
            addr = (mail.attributes.get("href") or "")[len("mailto:"):].split("?")[0].lower()
            if _email_ok(addr):
                _add_email(contact, addr)
        for tel in node.css('a[href^="tel:"]'):
            _add_phone(contact, (tel.attributes.get("href") or "")[len("tel:"):])

        out.append(contact)
    return _drop_colleague_departments(out)


def _parse_cards(
    tree: HTMLParser, base_url: str, domain: str, school: str | None, source: str
) -> list[Contact]:
    """Fallback for card/list layouts: one block per person."""
    out: list[Contact] = []
    seen_nodes: set[int] = set()

    for node in tree.css('[class*="staff"], [class*="person"], [class*="coach"], li, article'):
        mailto = node.css('a[href^="mailto:"]')
        # A person block holds exactly one email. More than that and we're
        # looking at a container of several people, not one person.
        if len(mailto) != 1:
            continue
        if any(id(ancestor) in seen_nodes for ancestor in _ancestors(node)):
            continue
        seen_nodes.add(id(node))

        addr = (mailto[0].attributes.get("href") or "")[len("mailto:"):].split("?")[0].lower()
        if not _email_ok(addr):
            continue

        name = _card_name(node)
        if not name:
            continue
        contact = _make(name, _preceding_heading(node), domain, school, source)
        _add_email(contact, addr)
        contact.title = _card_title(node, name)
        for tel in node.css('a[href^="tel:"]'):
            _add_phone(contact, (tel.attributes.get("href") or "")[len("tel:"):])
        for link in node.css("a[href]"):
            href = link.attributes.get("href") or ""
            if not contact.profile_url and _is_profile_link(href, domain):
                contact.profile_url = urljoin(base_url, href).split("?")[0]
        out.append(contact)
    return _drop_colleague_departments(out)


# --- helpers -------------------------------------------------------------

def _coaches_only(general: list[Candidate], domain: str) -> Candidate | None:
    """Last resort: salvage the coaches out of a general campus directory.

    Andrew College publishes one combined faculty/staff list — 54 people, 14 of
    them coaches, no athletics site of its own. Returning all 54 would file
    professors as athletics staff; returning nothing would lose 14 real coaches.
    """
    for found in general:
        coaches = [c for c in found.contacts if c.is_coach]
        if coaches:
            # Whatever grouped this page ("A-D", "Jump to a Section", a campus
            # department) is not a sport and not an athletics department. The
            # title is the only reliable thing a campus row says about the job —
            # so read the sport back out of it, which is the one place these rows
            # do state it ("Head Baseball Coach").
            #
            # Deliberately here and not in _dedupe: filling sport in earlier
            # would make a campus faculty page pass is_athletics_directory() and
            # be scraped whole, professors and all.
            for coach in coaches:
                coach.department = None
                coach.sport = sport_from_title(coach.title)
                if coach.sport:
                    note = "sport read from the job title (campus directory, no sport column)"
                    if note not in coach.notes:
                        coach.notes.append(note)
            log.info(
                "%s: no athletics directory — keeping %d coach(es) of %d from %s",
                domain, len(coaches), len(found.contacts), found.page.url,
            )
            return Candidate(
                page=found.page, contacts=coaches, school=found.school, athletics=False
            )
    return None


def _directory_links(tree: HTMLParser, base_url: str, domain: str) -> list[tuple[int, str]]:
    """Same-site links that look like a staff directory, best first."""
    root = domain.split(":")[0].removeprefix("www.").lower()
    scored: dict[str, int] = {}

    for node in tree.css("a[href]"):
        href = node.attributes.get("href") or ""
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        absolute = urljoin(base_url, href).split("#")[0].rstrip("/")
        parts = urlparse(absolute)
        if parts.scheme not in ("http", "https"):
            continue
        host = parts.netloc.split(":")[0].removeprefix("www.").lower()
        if host != root and not host.endswith("." + root):
            continue

        haystack = f"{parts.path.lower()} {(node.text() or '').strip().lower()}"
        score = 0
        for hint, weight in DIRECTORY_HINTS:
            if hint in haystack:
                score = max(score, weight)
        if score:
            scored[absolute] = max(scored.get(absolute, 0), score)

    return sorted(((s, u) for u, s in scored.items()), key=lambda pair: -pair[0])[:5]


def _failure_outcome(domain: str, attempts: list[Page]) -> SiteOutcome:
    """Say why a site produced nothing, from what its responses looked like.

    Ordered by how actionable it is: a block or a robots rule is a decision
    someone made, a network failure is worth retrying, and "no directory" only
    applies once we know the site was actually reachable.
    """
    if not attempts:
        return SiteOutcome(
            domain=domain, status=SiteOutcome.NETWORK, detail="no requests completed"
        )

    blocked = [p for p in attempts if p.blocked]
    if blocked:
        codes = sorted({p.status for p in blocked})
        return SiteOutcome(
            domain=domain,
            status=SiteOutcome.BLOCKED,
            detail=f"server refused automated requests (HTTP {', '.join(map(str, codes))})",
            url=blocked[0].url,
        )

    if any(p.robots_blocked for p in attempts):
        return SiteOutcome(
            domain=domain,
            status=SiteOutcome.ROBOTS,
            detail="robots.txt disallows the pages we need",
            url=attempts[0].url,
        )

    # Reachable at all? If nothing ever came back, it's the network, not the site.
    if all(p.network_failed for p in attempts):
        detail = next((p.error for p in attempts if p.error), "no response")
        return SiteOutcome(
            domain=domain, status=SiteOutcome.NETWORK, detail=detail, url=attempts[0].url
        )

    if not any(p.ok for p in attempts):
        codes = sorted({p.status for p in attempts if p.status not in (0,)})
        return SiteOutcome(
            domain=domain,
            status=SiteOutcome.NETWORK,
            detail=f"no page fetched successfully (HTTP {', '.join(map(str, codes))})"
            if codes
            else "no page fetched successfully",
            url=attempts[0].url,
        )

    return SiteOutcome(
        domain=domain,
        status=SiteOutcome.NO_DIRECTORY,
        detail=f"site reachable but no staff directory found in {len(attempts)} request(s)",
        url=attempts[0].url,
    )


def _athletics_host(tree: HTMLParser, base_url: str, domain: str) -> str | None:
    """The off-site athletics host a university homepage links to.

    Athletics departments almost always sit on their own domain
    (``jsu.edu`` -> ``jaxstatesports.com``), so a link that leaves the
    university host under an "Athletics" label is the one worth following.
    """
    root = domain.split(":")[0].removeprefix("www.").lower()
    counts: dict[str, int] = {}

    for node in tree.css("a[href]"):
        href = node.attributes.get("href") or ""
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        label = (node.text() or "").strip()
        if not ATHLETICS_LINK_RE.search(f"{label} {href}"):
            continue
        parts = urlparse(urljoin(base_url, href))
        if parts.scheme not in ("http", "https"):
            continue
        host = parts.netloc.split(":")[0].removeprefix("www.").lower()
        if not host or host == root or host.endswith("." + root):
            continue
        if _NOT_ATHLETICS_HOST_RE.search(host):
            continue
        counts[host] = counts.get(host, 0) + 1

    if not counts:
        return None
    return max(counts, key=lambda host: counts[host])


# Social and ticketing hosts get "Athletics"-ish link text too.
_NOT_ATHLETICS_HOST_RE = re.compile(
    r"(?:^|\.)(?:facebook\.com|twitter\.com|x\.com|instagram\.com|youtube\.com|"
    r"youtu\.be|tiktok\.com|linkedin\.com|ticketmaster\.\w+|seatgeek\.com|"
    r"ncaa\.com|ncaa\.org|espn\.com)$",
    re.I,
)


# "Jr", "III", "PhD" — a comma before one of these is not a surname-first name.
_NAME_SUFFIX_RE = re.compile(
    r"^(?:[JS]r|I{1,3}|IV|V|VI{0,3}|Ph\.?D|Ed\.?D|M\.?[DSA]|D\.?M\.?D|CPA|Esq)\.?$",
    re.I,
)


def normalize_person_name(raw: str) -> str:
    """``"Baker, Alycia"`` -> ``"Alycia Baker"``.

    Campus directories sort by surname and store the name that way. Left alone
    it reaches the dashboard as "Baker, Alycia", sorts under B, and never
    matches a search for the person's actual name.

    Only a single comma with real words either side is flipped, so "Smith, Jr."
    and "Lee, PhD" are left exactly as they are.
    """
    name = " ".join((raw or "").split())
    if name.count(",") != 1:
        return name
    surname, rest = (part.strip() for part in name.split(","))
    if not surname or not rest or _NAME_SUFFIX_RE.match(rest):
        return name
    return f"{rest} {surname}"


def clean_group(text: str | None) -> tuple[str | None, list[str]]:
    """Split a section heading into the group name and any address it carries.

    Plenty of directories head each section with the team's own inbox —
    Brandeis writes "Men's Soccer - menssoccer@brandeis.edu" — and the whole
    string was being stored as the sport, so the dashboard showed the sport and
    an email mashed together and the address itself was never usable.

    Returns the group without its contact details, plus the addresses found.
    They are a *team* inbox, never a person's, which is why :func:`_make` files
    them as shared.
    """
    if not text:
        return None, []
    addresses = [a.lower() for a in extract.EMAIL_RE.findall(text) if _email_ok(a.lower())]
    cleaned = extract.EMAIL_RE.sub(" ", text)
    cleaned = extract.PHONE_RE.sub(" ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" \t-–—·|,;:/")
    return (cleaned or None), addresses


def _make(name: str, group: str | None, domain: str, school: str | None, source: str) -> Contact:
    contact = Contact(
        name=normalize_person_name(name), school_domain=domain, school=school, source=source
    )
    group, group_emails = clean_group(group)
    if group:
        if _is_sport(group):
            contact.sport = group
        else:
            contact.department = group
    for addr in group_emails:
        # Flagged here rather than left to _flag_shared_emails, which needs the
        # same address on three people before it calls one shared. A team inbox
        # off a section heading is shared by construction, even where the
        # section holds a single coach — and passing it off as their own
        # address is exactly the mistake that flag exists to prevent.
        _add_email(contact, addr)
        contact.shared_email = True
    return contact


def _is_sport(group: str) -> bool:
    lowered = group.lower()
    return any(word in lowered for word in SPORT_WORDS)


def _drop_colleague_departments(contacts: list[Contact]) -> list[Contact]:
    """Clear a department that is really another person on the same page.

    A backstop for :func:`_is_person_block`, which has to judge one node at a
    time. Here the whole page is in hand, so "is this heading just a colleague's
    name?" stops being a guess: it is answered against the names actually found.
    That keeps a real department named after a donor — "Frank Erwin Center" —
    while dropping "Michael Norman", who is a coach two cards up.
    """
    names = {(c.name or "").strip().casefold() for c in contacts}
    names.discard("")
    for contact in contacts:
        if not contact.department:
            continue
        kept = [
            part
            for part in (p.strip() for p in contact.department.split(";"))
            if part and part.casefold() not in names
        ]
        contact.department = "; ".join(kept) or None
    return contacts


def _text(node) -> str:
    """Visible text of one cell.

    Sidearm hides addresses behind a per-row ``<script>`` that assembles them at
    render time. ``node.text()`` happily returns that JavaScript, so a coach's
    email came out as ``var placeholder = document.getElementById(...)``. Drop
    script/style subtrees before reading the text; the row's ``mailto:`` link is
    where the real address comes from.
    """
    if node is None:
        return ""
    for junk in node.css("script, style, noscript, template"):
        junk.decompose()
    return re.sub(r"\s+", " ", (node.text(separator=" ") or "")).strip()


def _email_ok(addr: str) -> bool:
    return bool(addr) and bool(extract.EMAIL_RE.fullmatch(addr)) and not extract.EMAIL_NOISE.search(addr)


def _add_email(contact: Contact, raw: str) -> None:
    addr = raw.strip().lower()
    if _email_ok(addr) and addr not in contact.emails:
        contact.emails.append(addr)


def _add_phone(contact: Contact, raw: str) -> None:
    """Add a number unless we already hold the same one in another format.

    A directory row usually carries the number twice — once as ``tel:+15551110002``
    and once as the cell's own ``(555) 111-0002``. They differ as strings but
    are one phone, so compare on digits and keep whichever arrived first (the
    ``tel:`` link, which is the site's own canonical form).
    """
    phone = extract.clean_phone(raw)
    if not phone:
        return
    digits = re.sub(r"\D", "", phone)
    for existing in contact.phones:
        other = re.sub(r"\D", "", existing)
        if digits == other:
            return
        # One may carry a country code the other omits.
        if len(min(digits, other, key=len)) >= 7 and (
            digits.endswith(other) or other.endswith(digits)
        ):
            return
    contact.phones.append(phone)


def _is_profile_link(href: str, domain: str) -> bool:
    if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
        return False
    return bool(re.search(r"/staff-directory/|/staff/|/coaches/|/roster/|/bios?/", href, re.I))


def _ancestors(node) -> Iterable:
    parent = node.parent
    while parent is not None:
        yield parent
        parent = parent.parent


# The page's own title is not a group name, and neither is the A–D bucket a
# campus directory sorts itself into. Both were reaching the dashboard as a
# coach's department: "Campus Directory" on bigbend.edu, "A-D" on cisco.edu.
_GENERIC_HEADING = re.compile(
    r"""^(?:
        (?:campus\s*|employee\s*|faculty(?:\s*(?:and|&)\s*staff)?\s*|staff\s*)?
            (?:directory|listing|search(?:\s*results)?|index)
      | staff | coaches | contact\s*us | athletics
      | jump\s*to.* | skip\s*to.* | filter\s*by.* | sort\s*by.* | back\s*to\s*top
      | [A-Z0-9]\s*[-–—]\s*[A-Z0-9]      # "A-D", "0-9"
      | [A-Z]                            # a single letter bucket
    )$""",
    re.I | re.X,
)


def _usable_group(text: str | None) -> bool:
    """Is this label a real sport/department, or the page describing itself?"""
    label = (text or "").strip()
    return bool(label) and len(label) <= 80 and not _GENERIC_HEADING.match(label)


def _is_person_block(node) -> bool:
    """Does this node hold exactly one person's contact details?

    Card layouts put the person's name in a heading, so the block above a card
    is the *previous card* and its ``<h4>`` is a name, not a sport. Andrew
    College's whole coaching staff came out with a colleague's name as their
    department that way — Adam Biss filed under "Fran Balkcom".

    A mailto is not enough to recognise one. The Sidearm card layout keeps the
    address behind the profile page — see :func:`_parse_person_cards`, where
    "email and phone are usually absent" — so every card looked like a plain
    container and the previous card's name was mined as the heading anyway.
    Texas, Ole Miss, Georgia and Michigan State all filed their coaches under a
    colleague's name. Recognise the card by its own name node or its single
    profile link too, and keep the "exactly one" test throughout: a block
    holding several of any of these is a list of people, not a person.
    """
    if node is None or node.tag == "-text":
        return False
    if len(node.css('a[href^="mailto:"]')) == 1:
        return True
    if len(node.css(_CARD_NAME)) == 1:
        return True
    profile_links = {
        (link.attributes.get("href") or "").split("?")[0]
        for link in node.css("a[href]")
        if _is_profile_link(link.attributes.get("href") or "", "")
    }
    return len(profile_links) == 1


def _preceding_heading(node) -> str | None:
    """Nearest heading text above this node — the sport, on most layouts."""
    current = node
    for _ in range(60):
        prev = current.prev
        while prev is not None:
            if _is_person_block(prev):
                # Another person's card. Whatever heading it holds is their
                # name, so don't mine it — but keep walking past it, because the
                # real section heading sits further up.
                prev = prev.prev
                continue
            if prev.tag in ("h1", "h2", "h3", "h4", "h5", "caption"):
                text = _text(prev)
                if text and not _GENERIC_HEADING.match(text):
                    return text[:120]
            inner = prev.css("h1, h2, h3, h4, caption") if prev.tag != "-text" else []
            if inner:
                text = _text(inner[-1])
                if text and not _GENERIC_HEADING.match(text):
                    return text[:120]
            prev = prev.prev
        current = current.parent
        if current is None:
            return None
    return None


def _card_name(node) -> str | None:
    for selector in ("h1", "h2", "h3", "h4", "h5", '[class*="name"]', "a"):
        found = node.css_first(selector)
        text = _text(found)
        if text and 3 <= len(text) <= 60 and "@" not in text:
            return text
    return None


def _card_title(node, name: str) -> str | None:
    text = _text(node)
    text = text.replace(name, " ", 1)
    for line in re.split(r"\s{2,}|\|", text):
        line = clean_title(line.strip(" -–—·|"))
        if line and COACH_TITLE_RE.search(line) and len(line) <= 120:
            return line
    return None


# A page title that describes the page, not the institution. bigbend.edu's
# directory is titled "Campus Directory", and that reached the dashboard as the
# school name for all 389 of its people.
_NOT_A_SCHOOL_RE = re.compile(
    r"^(?:campus|staff|faculty|employee|college|university|athletics?)?[\s&/-]*"
    r"(?:staff|faculty|employee|campus|phone|contact|people|personnel)?[\s&/-]*"
    r"(?:directory|listing|list|search|index|home ?page)\s*$",
    re.I,
)


def _school_name(tree: HTMLParser, domain: str) -> str | None:
    name = extract.company_name(tree, domain)
    if name and _NOT_A_SCHOOL_RE.match(name.strip()):
        return None
    return name


# A Sidearm card directory links each person at /staff-directory/<slug>/<id>.
# Several of those is as strong a signal as a column of mailto links.
_PROFILE_LINK_RE = re.compile(r"/staff-directory/[a-z0-9-]+/\d+", re.I)
MIN_DIRECTORY_SIGNALS = 5


# /sports/<slug>/ also covers archive links like /sports/2024/, which are
# seasons rather than sports.
_SPORT_SLUG_RE = re.compile(r"/sports/([a-z][a-z0-9-]{2,40})/", re.I)
MAX_SPORT_PAGES = 40
# One sport's page is a short list: men's golf has a head coach and an
# assistant. The whole-school threshold would throw those away.
MIN_SPORT_PAGE_SIGNALS = 2


def _sport_slugs(tree) -> list[str]:
    """Sport slugs linked from a homepage, in a stable order."""
    slugs: list[str] = []
    for link in tree.css("a[href]"):
        found = _SPORT_SLUG_RE.search(link.attributes.get("href") or "")
        if not found:
            continue
        slug = found.group(1).lower()
        if slug.isdigit() or slug in slugs:
            continue
        slugs.append(slug)
    return sorted(slugs)[:MAX_SPORT_PAGES]


def _looks_like_directory(html: str, min_signals: int = MIN_DIRECTORY_SIGNALS) -> bool:
    """Cheap check that a candidate page is a staff list, not a 200-page 404.

    ``min_signals`` is lowered for a single-sport page: men's golf legitimately
    lists two coaches, and the whole-school threshold reads that as noise.
    """
    lowered = html.lower()
    if lowered.count("mailto:") >= min_signals:
        return True
    if "staff directory" in lowered and lowered.count("<tr") >= min_signals:
        return True
    # Sidearm's card template carries neither a mailto nor a table row, so the
    # two tests above reject it outright — which is how every large athletics
    # site came back "no staff directory found" while serving one.
    if lowered.count("s-person-card") >= min_signals:
        return True
    return len(set(_PROFILE_LINK_RE.findall(lowered))) >= min_signals


# How many people must list an address before it reads as a shared inbox
# rather than a personal one.
SHARED_EMAIL_THRESHOLD = 3


def _flag_shared_emails(contacts: list[Contact]) -> None:
    """Mark addresses that appear on several people's rows at one school.

    A directory routinely lists an executive assistant, a program inbox
    (``volleyball@jsu.edu``) or a front desk in the coach's own row. The row is
    parsed correctly — the address simply isn't that person's. Flagging beats
    dropping, because for many programs it is the only route in.
    """
    counts: dict[str, int] = {}
    for contact in contacts:
        for addr in set(contact.emails):
            counts[addr] = counts.get(addr, 0) + 1

    for contact in contacts:
        shared = [a for a in contact.emails if counts.get(a, 0) >= SHARED_EMAIL_THRESHOLD]
        if shared and len(shared) == len(contact.emails):
            contact.shared_email = True
            note = f"listed address is shared by {counts[shared[0]]} people here"
            if note not in contact.notes:
                contact.notes.append(note)


def _dedupe(contacts: list[Contact]) -> list[Contact]:
    merged: dict[str, Contact] = {}
    for contact in contacts:
        contact.is_coach = is_coaching_title(contact.title)
        existing = merged.get(contact.key)
        merged[contact.key] = existing.merge(contact) if existing else contact
    return list(merged.values())
