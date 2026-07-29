"""Fetching layer: robots.txt, per-host rate limiting, optional JS rendering."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import httpx

from .config import Settings

log = logging.getLogger("scrapbot.net")

# Below this much visible text we assume the page is client-rendered and,
# in ``render=auto`` mode, retry it through a real browser.
JS_TEXT_THRESHOLD = 600


@dataclass
class Page:
    url: str
    status: int
    html: str
    rendered: bool = False

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 and bool(self.html)


class RobotsCache:
    """One RobotFileParser per host, fetched at most once."""

    def __init__(self, client: httpx.AsyncClient, user_agent: str) -> None:
        self._client = client
        self._ua = user_agent
        self._parsers: dict[str, RobotFileParser | None] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, host: str) -> asyncio.Lock:
        return self._locks.setdefault(host, asyncio.Lock())

    async def allowed(self, url: str) -> bool:
        parts = urlparse(url)
        host = parts.netloc
        if not host:
            return False
        async with self._lock(host):
            if host not in self._parsers:
                self._parsers[host] = await self._load(parts.scheme or "https", host)
        parser = self._parsers[host]
        if parser is None:
            # No reachable robots.txt — the convention is that everything is allowed.
            return True
        return parser.can_fetch(self._ua, url)

    async def crawl_delay(self, url: str) -> float | None:
        host = urlparse(url).netloc
        parser = self._parsers.get(host)
        if parser is None:
            return None
        try:
            value = parser.crawl_delay(self._ua)
        except Exception:  # pragma: no cover - stdlib is quirky on odd files
            return None
        return float(value) if value else None

    async def _load(self, scheme: str, host: str) -> RobotFileParser | None:
        robots_url = urlunparse((scheme, host, "/robots.txt", "", "", ""))
        try:
            resp = await self._client.get(robots_url)
        except httpx.HTTPError as exc:
            log.debug("robots.txt unreachable for %s: %s", host, exc)
            return None
        if resp.status_code >= 400:
            return None
        parser = RobotFileParser()
        parser.parse(resp.text.splitlines())
        return parser


class Fetcher:
    """Polite HTTP fetcher with a lazily started Playwright fallback.

    Use as an async context manager so the browser and HTTP client are
    always torn down::

        async with Fetcher(settings) as fetcher:
            page = await fetcher.get("https://example.com")
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: httpx.AsyncClient | None = None
        self._robots: RobotsCache | None = None
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._host_last_hit: dict[str, float] = {}
        self._playwright = None
        self._browser = None
        self._browser_lock = asyncio.Lock()
        self.stats = {"requests": 0, "rendered": 0, "blocked": 0, "errors": 0}

    async def __aenter__(self) -> "Fetcher":
        self._client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=self.settings.timeout,
            headers={
                "User-Agent": self.settings.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-AU,en;q=0.9",
            },
        )
        self._robots = RobotsCache(self._client, self.settings.user_agent)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._browser is not None:
            await self._browser.close()
        if self._playwright is not None:
            await self._playwright.stop()
        if self._client is not None:
            await self._client.aclose()

    # -- rate limiting ----------------------------------------------------
    async def _throttle(self, host: str, delay: float) -> None:
        lock = self._host_locks.setdefault(host, asyncio.Lock())
        async with lock:
            last = self._host_last_hit.get(host)
            now = time.monotonic()
            if last is not None:
                wait = delay - (now - last)
                if wait > 0:
                    await asyncio.sleep(wait)
            self._host_last_hit[host] = time.monotonic()

    # -- public API -------------------------------------------------------
    async def get(self, url: str) -> Page:
        assert self._client is not None and self._robots is not None, "use as async context manager"
        host = urlparse(url).netloc

        if self.settings.respect_robots and not await self._robots.allowed(url):
            self.stats["blocked"] += 1
            log.info("robots.txt disallows %s — skipping", url)
            return Page(url=url, status=999, html="")

        delay = self.settings.delay
        robots_delay = await self._robots.crawl_delay(url)
        if robots_delay:
            delay = max(delay, robots_delay)

        if self.settings.render == "always":
            await self._throttle(host, delay)
            return await self._render(url)

        page = await self._get_static(url, host, delay)

        if (
            self.settings.render == "auto"
            and page.ok
            and _visible_text_length(page.html) < JS_TEXT_THRESHOLD
        ):
            log.debug("%s looks client-rendered, retrying with a browser", url)
            await self._throttle(host, delay)
            rendered = await self._render(url)
            if rendered.ok:
                return rendered
        return page

    async def _get_static(self, url: str, host: str, delay: float) -> Page:
        assert self._client is not None
        last_error: Exception | None = None
        for attempt in range(self.settings.max_retries + 1):
            await self._throttle(host, delay)
            try:
                self.stats["requests"] += 1
                resp = await self._client.get(url)
            except httpx.HTTPError as exc:
                last_error = exc
                await asyncio.sleep(min(2**attempt, 8))
                continue

            if resp.status_code in (429, 500, 502, 503, 504) and attempt < self.settings.max_retries:
                retry_after = resp.headers.get("retry-after")
                sleep_for = float(retry_after) if (retry_after or "").isdigit() else min(2**attempt, 8)
                log.debug("%s returned %s, backing off %.1fs", url, resp.status_code, sleep_for)
                await asyncio.sleep(sleep_for)
                continue

            ctype = resp.headers.get("content-type", "")
            if "html" not in ctype and "xml" not in ctype:
                return Page(url=str(resp.url), status=resp.status_code, html="")
            html = resp.text[: self.settings.max_bytes_per_page]
            return Page(url=str(resp.url), status=resp.status_code, html=html)

        self.stats["errors"] += 1
        log.info("giving up on %s: %s", url, last_error)
        return Page(url=url, status=0, html="")

    async def _ensure_browser(self):
        async with self._browser_lock:
            if self._browser is None:
                from playwright.async_api import async_playwright

                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(
                    headless=self.settings.headless
                )
            return self._browser

    async def _render(self, url: str) -> Page:
        try:
            browser = await self._ensure_browser()
        except Exception as exc:  # playwright missing / browser not installed
            log.warning(
                "browser rendering unavailable (%s). Run: playwright install chromium", exc
            )
            self.settings.render = "never"
            return Page(url=url, status=0, html="")

        context = await browser.new_context(
            user_agent=self.settings.user_agent,
            locale="en-AU",
        )
        try:
            page = await context.new_page()
            resp = await page.goto(
                url, wait_until="domcontentloaded", timeout=self.settings.timeout * 1000
            )
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass  # networkidle is a nice-to-have, not a requirement
            html = await page.content()
            self.stats["requests"] += 1
            self.stats["rendered"] += 1
            return Page(
                url=page.url,
                status=resp.status if resp else 200,
                html=html[: self.settings.max_bytes_per_page],
                rendered=True,
            )
        except Exception as exc:
            self.stats["errors"] += 1
            log.info("render failed for %s: %s", url, exc)
            return Page(url=url, status=0, html="")
        finally:
            await context.close()


def _visible_text_length(html: str) -> int:
    """How much readable copy a page has, for the ``render=auto`` decision.

    Shares :func:`extract.visible_text` rather than reimplementing it — the
    private copy that used to live here deleted script/noscript subtrees, which
    on a malformed page takes the real content with it and reports an empty
    page that then gets pointlessly re-fetched through a browser.
    """
    from . import extract

    try:
        tree = extract.parse(html)
    except Exception:
        return len(html)
    return len(extract.visible_text(tree))
