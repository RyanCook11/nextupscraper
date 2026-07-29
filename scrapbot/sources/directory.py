"""``directory`` source — discover companies from a listing page, then scrape each.

Point it at an industry directory, chamber-of-commerce member list, awards
page or "our clients" page. It harvests the outbound company links, filters
the obvious noise, then reuses the ``website`` crawl on each discovered domain.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
from typing import AsyncIterator
from urllib.parse import urljoin, urlparse

from .. import extract
from ..models import Lead
from ..net import Fetcher
from .base import Source
from .website import WebsiteSource, normalize_domain

log = logging.getLogger("scrapbot.directory")

# Hosts that are never the lead itself.
SKIP_HOST_RE = re.compile(
    r"(?:^|\.)(?:"
    r"google\.\w+|gstatic\.com|googleapis\.com|doubleclick\.net|"
    r"facebook\.com|instagram\.com|twitter\.com|x\.com|linkedin\.com|youtube\.com|youtu\.be|"
    r"tiktok\.com|pinterest\.\w+|whatsapp\.com|t\.me|"
    r"wordpress\.(?:com|org)|wix\.com|squarespace\.com|shopify\.com|godaddy\.com|"
    r"cloudflare\.com|jsdelivr\.net|unpkg\.com|bootstrapcdn\.com|fontawesome\.com|"
    r"gov\.au|gov\.uk|\.gov|w3\.org|schema\.org|creativecommons\.org|"
    r"apple\.com|microsoft\.com|adobe\.com|amazon\.com|paypal\.com|stripe\.com|"
    r"mailchimp\.com|eventbrite\.\w+|trustpilot\.com|yelp\.\w+"
    r")$",
    re.I,
)


class DirectorySource(Source):
    name = "directory"
    help = "Harvest company domains from listing pages, then crawl each company site."

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--listing",
            nargs="+",
            required=True,
            metavar="URL",
            help="One or more listing/directory page URLs to harvest company links from.",
        )
        parser.add_argument(
            "--paginate",
            type=int,
            default=1,
            metavar="N",
            help="Also follow up to N 'next page' links from each listing (default 1 = "
            "listing page only).",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=50,
            help="Maximum companies to scrape (default 50, 0 = no limit).",
        )
        parser.add_argument(
            "--discover-only",
            action="store_true",
            help="Only record the discovered domains; skip crawling each company site.",
        )

    async def run(self, fetcher: Fetcher) -> AsyncIterator[Lead]:
        domains = await self._discover(fetcher)
        limit = self.args.limit or 0
        if limit > 0:
            if len(domains) > limit:
                log.info("discovered %d domains, capping at --limit %d", len(domains), limit)
            domains = domains[:limit]
        log.info("scraping %d discovered company site(s)", len(domains))

        if self.args.discover_only:
            for domain in domains:
                yield Lead(domain=domain, source=self.name, notes=["discovered, not crawled"])
            return

        crawler = WebsiteSource(self.settings, self.args)
        semaphore = asyncio.Semaphore(self.settings.concurrency)

        async def worker(domain: str) -> Lead | None:
            async with semaphore:
                try:
                    lead = await crawler.scrape_site(fetcher, domain)
                except Exception:
                    log.exception("unhandled error scraping %s", domain)
                    return None
                if lead is not None:
                    lead.source = self.name
                return lead

        tasks = [asyncio.create_task(worker(d)) for d in domains]
        try:
            for finished in asyncio.as_completed(tasks):
                lead = await finished
                if lead is not None:
                    yield lead
        finally:
            for task in tasks:
                task.cancel()

    async def _discover(self, fetcher: Fetcher) -> list[str]:
        found: dict[str, None] = {}
        for listing in self.args.listing:
            pages = [listing]
            for page_url in pages:
                page = await fetcher.get(page_url)
                if not page.ok:
                    log.info("listing page unavailable: %s (status %s)", page_url, page.status)
                    continue
                tree = extract.parse(page.html)
                listing_host = urlparse(page.url).netloc.removeprefix("www.").lower()

                for node in tree.css("a[href]"):
                    href = node.attributes.get("href") or ""
                    if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
                        continue
                    absolute = urljoin(page.url, href)
                    host = urlparse(absolute).netloc.removeprefix("www.").lower()
                    if not host or host == listing_host:
                        continue
                    if SKIP_HOST_RE.search(host):
                        continue
                    domain = normalize_domain(absolute)
                    if domain:
                        found.setdefault(domain, None)

                if len(pages) < max(1, self.args.paginate):
                    nxt = _next_page(tree, page.url)
                    if nxt and nxt not in pages:
                        pages.append(nxt)

        log.info("discovered %d candidate domain(s)", len(found))
        return list(found)


def _next_page(tree, base_url: str) -> str | None:
    node = tree.css_first('a[rel="next"]')
    if node and node.attributes.get("href"):
        return urljoin(base_url, node.attributes["href"])
    for candidate in tree.css("a[href]"):
        label = (candidate.text() or "").strip().lower()
        if label in {"next", "next page", "next ›", "›", "»"}:
            return urljoin(base_url, candidate.attributes.get("href") or "")
    return None
