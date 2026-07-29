"""``website`` source — crawl a list of company domains for lead details.

Given ``acme-plumbing.com.au`` it fetches the homepage, follows the most
promising internal links (contact / about / careers / team) up to
``max_pages_per_site``, and folds everything into one :class:`Lead`.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import urlparse

from .. import extract
from ..models import Lead
from ..net import Fetcher, Page
from .base import Source

log = logging.getLogger("scrapbot.website")


class WebsiteSource(Source):
    name = "website"
    help = "Crawl company websites from a seed list of domains or URLs."

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--seeds",
            type=Path,
            help="File with one domain or URL per line ('#' comments allowed). "
            "Use '-' to read from stdin.",
        )
        parser.add_argument(
            "--domains",
            nargs="+",
            default=[],
            metavar="DOMAIN",
            help="Domains or URLs to scrape, in addition to --seeds.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Stop after this many seeds (0 = no limit).",
        )

    # -- seeds ------------------------------------------------------------
    def _load_seeds(self) -> list[str]:
        raw: list[str] = list(self.args.domains or [])

        seeds_path: Path | None = self.args.seeds
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
        if not seeds:
            raise SystemExit("no seeds given — pass --seeds FILE or --domains example.com")
        limit = self.args.limit or 0
        return seeds[:limit] if limit > 0 else seeds

    # -- run --------------------------------------------------------------
    async def run(self, fetcher: Fetcher) -> AsyncIterator[Lead]:
        seeds = self._load_seeds()
        log.info("scraping %d site(s) with concurrency %d", len(seeds), self.settings.concurrency)

        semaphore = asyncio.Semaphore(self.settings.concurrency)

        async def worker(domain: str) -> Lead | None:
            async with semaphore:
                try:
                    return await self.scrape_site(fetcher, domain)
                except Exception:  # one broken site must not kill the run
                    log.exception("unhandled error scraping %s", domain)
                    return None

        tasks = [asyncio.create_task(worker(d)) for d in seeds]
        try:
            for finished in asyncio.as_completed(tasks):
                lead = await finished
                if lead is not None:
                    yield lead
        finally:
            for task in tasks:
                task.cancel()

    # -- per-site ---------------------------------------------------------
    async def scrape_site(self, fetcher: Fetcher, domain: str) -> Lead | None:
        home = await self._fetch_home(fetcher, domain)
        if home is None:
            log.info("no reachable homepage for %s", domain)
            return None

        lead = Lead(domain=domain, url=home.url, source=self.name)
        tree = extract.parse(home.html)
        text = extract.visible_text(tree)

        lead.company_name = extract.company_name(tree, domain)
        lead.description = extract.description(tree)
        lead.socials = extract.socials(tree, home.url)
        self._absorb(lead, home, tree, text, domain)

        candidates = extract.internal_links(tree, home.url, domain)
        budget = max(0, self.settings.max_pages_per_site - 1)
        visited = {home.url.rstrip("/")}

        for _score, url in candidates:
            if budget <= 0:
                break
            if url.rstrip("/") in visited:
                continue
            visited.add(url.rstrip("/"))
            budget -= 1

            page = await fetcher.get(url)
            if not page.ok:
                continue
            sub_tree = extract.parse(page.html)
            sub_text = extract.visible_text(sub_tree)
            self._absorb(lead, page, sub_tree, sub_text, domain)
            for platform, link in extract.socials(sub_tree, page.url).items():
                lead.socials.setdefault(platform, link)
            if not lead.description:
                lead.description = extract.description(sub_tree)

        if not (lead.emails or lead.phones or lead.socials):
            lead.notes.append("no contact details found")
        return lead

    async def _fetch_home(self, fetcher: Fetcher, domain: str) -> Page | None:
        for scheme in ("https", "http"):
            page = await fetcher.get(f"{scheme}://{domain}/")
            if page.ok:
                return page
            if page.status == 999:  # robots.txt disallow — don't try the other scheme
                return None
        return None

    def _absorb(self, lead: Lead, page: Page, tree, text: str, domain: str) -> None:
        """Fold one fetched page's findings into ``lead``."""
        lead.pages_crawled += 1

        for addr in extract.emails(page.html, tree, domain):
            if addr not in lead.emails:
                lead.emails.append(addr)
        for phone in extract.phones(tree, text):
            if phone not in lead.phones:
                lead.phones.append(phone)

        if not lead.location:
            lead.location = extract.location(tree, text)

        for hint in extract.industry_hints(text, lead.description):
            if hint not in lead.industry_hints:
                lead.industry_hints.append(hint)

        path = urlparse(page.url).path.lower()
        if lead.careers_url is None and re.search(r"career|jobs|vacanc|work-with-us", path):
            lead.careers_url = page.url
        if extract.has_open_roles(text):
            lead.has_open_roles = True
        elif lead.has_open_roles is None and lead.careers_url:
            lead.has_open_roles = False

        lead.emails = lead.emails[:10]
        lead.phones = lead.phones[:5]


def normalize_domain(value: str) -> str | None:
    """``https://WWW.Acme.com/about?x=1`` -> ``acme.com``.

    A non-default port is kept (``localhost:8765`` stays intact) so fixtures
    and staging hosts remain addressable.
    """
    value = value.strip().strip('"\'').lower()
    if not value:
        return None
    if "://" not in value:
        value = "https://" + value
    netloc = urlparse(value).netloc.split("@")[-1]
    host, _, port = netloc.partition(":")
    host = host.removeprefix("www.")
    if not host or " " in host:
        return None
    if "." not in host and host != "localhost":
        return None
    return f"{host}:{port}" if port and port not in ("80", "443") else host
