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
from pathlib import Path
from typing import AsyncIterator, Iterable
from urllib.parse import urljoin, urlparse

from selectolax.parser import HTMLParser

from .. import extract, storage
from ..models import Contact
from ..net import Fetcher, Page
from .base import Source
from .website import normalize_domain

log = logging.getLogger("scrapbot.coaches")

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
    "baseball", "basketball", "beach volleyball", "bowling", "cross country",
    "equestrian", "fencing", "field hockey", "football", "golf", "gymnastics",
    "ice hockey", "lacrosse", "rifle", "rowing", "rugby", "sailing", "skiing",
    "soccer", "softball", "squash", "swimming", "diving", "tennis",
    "track & field", "track and field", "triathlon", "volleyball",
    "water polo", "wrestling", "crew",
)

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

    # -- seeds ------------------------------------------------------------
    def _load_seeds(self) -> list[str]:
        raw: list[str] = list(self.args.sites or [])

        seeds_path: Path | None = getattr(self.args, "seeds", None)
        if seeds_path is not None:
            if str(seeds_path) == "-":
                raw.extend(sys.stdin.read().splitlines())
            elif seeds_path.exists():
                raw.extend(seeds_path.read_text(encoding="utf-8").splitlines())
            else:
                raise SystemExit(f"seed file not found: {seeds_path}")

        seen: dict[str, None] = {}
        for line in raw:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            domain = normalize_domain(line)
            if domain:
                seen.setdefault(domain, None)

        seeds = list(seen)
        if not seeds and not self.args.directory_url:
            raise SystemExit(
                "no seeds given — pass --seeds FILE, --sites goduke.com, or --directory-url URL"
            )
        limit = self.args.limit or 0
        return seeds[:limit] if limit > 0 else seeds

    # -- run --------------------------------------------------------------
    async def run(self, fetcher: Fetcher) -> AsyncIterator[Contact]:
        seeds = self._load_seeds()
        jobs: list[tuple[str, str | None]] = [(d, None) for d in seeds]
        for url in self.args.directory_url or []:
            domain = normalize_domain(url)
            if domain:
                jobs.append((domain, url))

        log.info("scraping %d athletics site(s) with concurrency %d",
                 len(jobs), self.settings.concurrency)
        semaphore = asyncio.Semaphore(self.settings.concurrency)

        async def worker(domain: str, direct: str | None) -> list[Contact]:
            async with semaphore:
                try:
                    return await self.scrape_site(fetcher, domain, direct)
                except Exception:  # one broken site must not kill the run
                    log.exception("unhandled error scraping %s", domain)
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

    # -- per-site ---------------------------------------------------------
    async def scrape_site(
        self, fetcher: Fetcher, domain: str, direct_url: str | None = None
    ) -> list[Contact]:
        if not direct_url:
            mapped = self._athletics_host_map().get(domain)
            if mapped:
                log.info("%s is a university host — using athletics site %s", domain, mapped)
                domain = mapped

        page = (
            await fetcher.get(direct_url)
            if direct_url
            else await self._find_directory(fetcher, domain)
        )
        if page is None or not page.ok:
            log.info("no staff directory found for %s", domain)
            return []

        tree = extract.parse(page.html)
        school = _school_name(tree, domain)
        contacts = parse_directory(tree, page.url, domain, school, self.name)
        _flag_shared_emails(contacts)

        if not contacts:
            log.info("staff directory at %s parsed to 0 people", page.url)
        else:
            with_email = sum(1 for c in contacts if c.emails)
            log.info(
                "%s: %d people (%d with email) from %s",
                domain, len(contacts), with_email, page.url,
            )
        return contacts

    async def _find_directory(
        self, fetcher: Fetcher, domain: str, hop: int = 0
    ) -> Page | None:
        """Try the well-known paths, then fall back to scoring homepage links."""
        scheme = await self._working_scheme(fetcher, domain)
        if scheme is None:
            return None

        for path in DIRECTORY_PATHS:
            page = await fetcher.get(f"{scheme}://{domain}{path}")
            if page.ok and _looks_like_directory(page.html):
                return page
            if page.status == 999:  # robots.txt disallow — stop asking
                log.info("robots.txt disallows the staff directory on %s", domain)
                return None

        home = await fetcher.get(f"{scheme}://{domain}/")
        if not home.ok:
            return None
        tree = extract.parse(home.html)
        for _score, url in _directory_links(tree, home.url, domain):
            page = await fetcher.get(url)
            if page.ok and _looks_like_directory(page.html):
                return page

        # Still nothing: this may be a university host whose athletics site
        # lives on another domain and isn't in the school store yet.
        # One hop only — an athletics site that links back to the university
        # must not bounce us around forever.
        athletics = _athletics_host(tree, home.url, domain) if hop == 0 else None
        if athletics:
            log.info("following the athletics site link from %s to %s", domain, athletics)
            return await self._find_directory(fetcher, athletics, hop=1)

        log.info("no staff-directory link found on %s", home.url)
        return None

    async def _working_scheme(self, fetcher: Fetcher, domain: str) -> str | None:
        """Settle https-vs-http once, so a plain-HTTP host isn't probed twice
        for every candidate path."""
        for scheme in ("https", "http"):
            page = await fetcher.get(f"{scheme}://{domain}/")
            if page.ok:
                return scheme
            if page.status == 999:
                log.info("robots.txt disallows %s", domain)
                return None
        return None


# --- parsing -------------------------------------------------------------

def parse_directory(
    tree: HTMLParser, base_url: str, domain: str, school: str | None, source: str
) -> list[Contact]:
    """Parse a staff-directory page into one :class:`Contact` per person."""
    contacts = _parse_tables(tree, base_url, domain, school, source)
    if not contacts:
        contacts = _parse_cards(tree, base_url, domain, school, source)
    return _dedupe(contacts)


def _parse_tables(
    tree: HTMLParser, base_url: str, domain: str, school: str | None, source: str
) -> list[Contact]:
    out: list[Contact] = []
    for table in tree.css("table"):
        headers = [_text(th) for th in table.css("th")]
        mapping, group = _read_headers(headers)
        if "name" not in mapping.values():
            continue
        group = group or _preceding_heading(table)

        rows = table.css("tbody tr") or [r for r in table.css("tr") if r.css("td")]
        for row in rows:
            contact = _row_to_contact(row, mapping, group, base_url, domain, school, source)
            if contact is not None:
                out.append(contact)
    return out


def _read_headers(headers: list[str]) -> tuple[dict[int, str], str | None]:
    """Map column index -> field name, and pull out the group label if present.

    Sidearm puts the sport/department in the first header cell, so a header row
    of ``["Men's Basketball", "Name", "Title", "Email", "Phone"]`` yields the
    group *and* the column layout.
    """
    mapping: dict[int, str] = {}
    group: str | None = None
    known = [h for h in headers if h.strip().lower() in COLUMN_ALIASES]
    offset = len(headers) - len(known) if known else 0
    for index, header in enumerate(headers):
        field = COLUMN_ALIASES.get(header.strip().lower())
        if field:
            mapping[index - offset] = field
        elif group is None and header.strip():
            group = header.strip()
    return mapping, group


def _row_to_contact(
    row, mapping: dict[int, str], group: str | None, base_url: str,
    domain: str, school: str | None, source: str,
) -> Contact | None:
    cells = row.css("td")
    if not cells:
        return None
    values = {field: _text(cells[i]) for i, field in mapping.items() if i < len(cells)}

    name = values.get("name", "")
    if not name or SKIP_NAME_RE.match(name):
        return None

    contact = _make(name, group, domain, school, source)
    contact.title = values.get("title") or None

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
    return out


# --- helpers -------------------------------------------------------------

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


def _make(name: str, group: str | None, domain: str, school: str | None, source: str) -> Contact:
    contact = Contact(name=name, school_domain=domain, school=school, source=source)
    if group:
        if _is_sport(group):
            contact.sport = group
        else:
            contact.department = group
    return contact


def _is_sport(group: str) -> bool:
    lowered = group.lower()
    return any(word in lowered for word in SPORT_WORDS)


def _text(node) -> str:
    return re.sub(r"\s+", " ", (node.text(separator=" ") if node else "") or "").strip()


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


def _preceding_heading(node) -> str | None:
    """Nearest heading text above this node — the sport, on most layouts."""
    current = node
    for _ in range(60):
        prev = current.prev
        while prev is not None:
            if prev.tag in ("h1", "h2", "h3", "h4", "h5", "caption"):
                text = _text(prev)
                if text:
                    return text[:120]
            inner = prev.css("h1, h2, h3, h4, caption") if prev.tag != "-text" else []
            if inner:
                text = _text(inner[-1])
                if text:
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
        line = line.strip(" -–—·|")
        if line and COACH_TITLE_RE.search(line) and len(line) <= 120:
            return line
    return None


def _school_name(tree: HTMLParser, domain: str) -> str | None:
    return extract.company_name(tree, domain)


def _looks_like_directory(html: str) -> bool:
    """Cheap check that a candidate page is a staff list, not a 200-page 404."""
    lowered = html.lower()
    if lowered.count("mailto:") >= 5:
        return True
    return "staff directory" in lowered and lowered.count("<tr") >= 5


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
