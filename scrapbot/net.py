# scrapbot/net.py
"""Fetching layer: robots.txt, per-host rate limiting, optional JS rendering."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import httpx

from .config import Settings
from .profiles import SessionProfile, create_session_profile, find_chrome


log = logging.getLogger("scrapbot.net")


# Below this much visible text we assume the page is client-rendered and,
# in ``render=auto`` mode, retry it through a real browser.
JS_TEXT_THRESHOLD = 600


# Sentinel statuses for failures that aren't an HTTP response.
ROBOTS_BLOCKED = 999
NETWORK_FAILED = 0


@dataclass
class Page:
    url: str
    status: int
    html: str
    rendered: bool = False
    error: str = ""
    """Why a non-HTTP failure happened, for the run report."""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 and bool(self.html)

    @property
    def blocked(self) -> bool:
        """The server refused an automated client rather than failing."""
        return self.status in (401, 403, 405, 406, 429) or self.status == 451

    @property
    def robots_blocked(self) -> bool:
        return self.status == ROBOTS_BLOCKED

    @property
    def network_failed(self) -> bool:
        return self.status == NETWORK_FAILED


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
        self._robots_client: httpx.AsyncClient | None = None
        self._robots: RobotsCache | None = None
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._host_last_hit: dict[str, float] = {}
        self._playwright = None
        self._browser = None
        self._browser_lock = asyncio.Lock()
        self._stealth_unavailable = False
        # Hosts whose bot challenge the browser failed to clear, so we stop
        # paying for a render on every subsequent page of that site.
        self._unsolvable_hosts: set[str] = set()
        self.stats = {"requests": 0, "rendered": 0, "blocked": 0, "errors": 0}

        # Human-mimicry: small random jitter added to delays
        self._jitter_min = getattr(settings, "jitter_min", 0.2)
        self._jitter_max = getattr(settings, "jitter_max", 0.8)

    async def __aenter__(self) -> "Fetcher":
        # Create a default profile for the robots.txt client
        self._default_profile = create_session_profile()
        headers = self._default_profile.to_httpx_headers()

        self._robots_client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=self.settings.timeout,
            headers=headers,
        )
        self._robots = RobotsCache(self._robots_client, self._default_profile.user_agent)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._browser is not None:
            await self._browser.close()
        if self._playwright is not None:
            await self._playwright.stop()
        if self._robots_client is not None:
            await self._robots_client.aclose()

    # -- rate limiting ----------------------------------------------------
    async def _throttle(self, host: str, delay: float) -> None:
        lock = self._host_locks.setdefault(host, asyncio.Lock())
        async with lock:
            last = self._host_last_hit.get(host)
            now = time.monotonic()
            if last is not None:
                wait = delay - (now - last)
                if wait > 0:
                    # Add small random jitter to avoid perfectly periodic hits
                    jitter = random.uniform(self._jitter_min, self._jitter_max)
                    wait += jitter
                    await asyncio.sleep(wait)
            self._host_last_hit[host] = time.monotonic()

    # -- human-like delay helper -----------------------------------------
    async def _human_delay(self, min_sec: float = 0.5, max_sec: float = 2.0) -> None:
        # ``--delay 0`` is an explicit "go as fast as you can" from the
        # operator (the tests rely on it against a local fixture server).
        # Mimicking a human is pointless there, so honour the setting.
        if self.settings.delay <= 0:
            return
        await asyncio.sleep(random.uniform(min_sec, max_sec))

    # -- public API -------------------------------------------------------
    async def get(self, url: str) -> Page:
        assert self._robots_client is not None and self._robots is not None, "use as async context manager"
        host = urlparse(url).netloc

        if self.settings.respect_robots and not await self._robots.allowed(url):
            self.stats["blocked"] += 1
            log.info("robots.txt disallows %s — skipping", url)
            return Page(
                url=url, status=ROBOTS_BLOCKED, html="", error="robots.txt disallow"
            )

        # Fresh profile per request (UA + headers), sent over the shared,
        # pooled client. Building a client per request cost ~0.28s each and
        # threw away connection and TLS reuse; it also made us *less*
        # browser-like, since a real Chrome keeps connections alive rather
        # than opening a new one for every page.
        profile = create_session_profile()
        headers = profile.to_httpx_headers()
        client = self._robots_client

        delay = self.settings.delay
        robots_delay = await self._robots.crawl_delay(url)
        if robots_delay:
            delay = max(delay, robots_delay)

        # Optional extra human-like pause before each fetch
        await self._human_delay(0.3, 1.2)

        if self.settings.render == "always":
            await self._throttle(host, delay)
            return await self._render(url, profile=profile)

        page = await self._get_static(url, host, delay, client=client, headers=headers)

        if self.settings.render == "auto":
            looks_client_rendered = (
                page.ok and _visible_text_length(page.html) < JS_TEXT_THRESHOLD
            )
            # A JS challenge (AWS WAF, Cloudflare) answers 403/405 rather than
            # 200, so the `page.ok` test above never fires for exactly the case
            # a browser is most likely to solve. Retry those too — but at most
            # once per host: when the challenge beats the browser as well,
            # every further page on that host would pay a browser launch to
            # fail in the same way.
            challenged = page.blocked and host not in self._unsolvable_hosts

            if looks_client_rendered or challenged:
                reason = "looks client-rendered" if looks_client_rendered else "was challenged"
                log.debug("%s %s, retrying with a browser", url, reason)
                await self._throttle(host, delay)
                rendered = await self._render(url, profile=profile)
                if rendered.ok:
                    return rendered
                if challenged:
                    log.info(
                        "%s: browser could not clear the challenge either; "
                        "skipping browser retries for this host",
                        host,
                    )
                    self._unsolvable_hosts.add(host)
        return page

    async def get_rendered(self, url: str) -> Page:
        """Fetch a page through a real browser, whatever ``render`` is set to.

        For the caller that has already read the static HTML and found nothing
        on it: "reachable but parsed to nobody" is the signal that the content
        is script-built, and it's a far better trigger than guessing from how
        much visible text came back. ``render=never`` is still honoured — that
        one is the operator saying no browsers, full stop.
        """
        assert self._robots is not None, "use as async context manager"
        if self.settings.render == "never":
            return Page(url=url, status=NETWORK_FAILED, html="", error="render disabled")
        if self.settings.respect_robots and not await self._robots.allowed(url):
            self.stats["blocked"] += 1
            return Page(url=url, status=ROBOTS_BLOCKED, html="", error="robots.txt disallow")

        host = urlparse(url).netloc
        delay = max(self.settings.delay, await self._robots.crawl_delay(url) or 0)
        await self._throttle(host, delay)
        return await self._render(url)

    async def get_bytes(self, url: str, max_bytes: int = 2_000_000) -> bytes | None:
        """Fetch a binary asset (a headshot) under the same rules as a page.

        Same robots check and same per-host delay as :meth:`get` — an image is
        still a request to someone's server. No retries and no browser: a
        missing photo is not worth a second round trip.
        """
        assert self._robots_client is not None and self._robots is not None, "use as async context manager"
        if self.settings.respect_robots and not await self._robots.allowed(url):
            self.stats["blocked"] += 1
            return None

        host = urlparse(url).netloc
        delay = max(self.settings.delay, await self._robots.crawl_delay(url) or 0)
        await self._throttle(host, delay)

        # Fresh profile for this request
        profile = create_session_profile()
        try:
            self.stats["requests"] += 1
            resp = await self._robots_client.get(
                url,
                headers=profile.to_httpx_headers(),
            )
        except httpx.HTTPError as exc:
            log.debug("photo %s failed: %s", url, exc)
            self.stats["errors"] += 1
            return None

        if resp.status_code != 200:
            return None
        if not resp.headers.get("content-type", "").startswith("image/"):
            return None
        data = resp.content
        return data[:max_bytes] if data else None

    async def _get_static(
        self,
        url: str,
        host: str,
        delay: float,
        client: httpx.AsyncClient,
        headers: dict[str, str] | None = None,
    ) -> Page:
        last_error: Exception | None = None
        for attempt in range(self.settings.max_retries + 1):
            await self._throttle(host, delay)
            try:
                self.stats["requests"] += 1
                resp = await client.get(url, headers=headers)
            except httpx.HTTPError as exc:
                last_error = exc
                if _is_permanent(exc):
                    # A name that doesn't resolve won't resolve on retry, and a
                    # seed list of a few hundred schools usually holds a few
                    # dead domains. Backing off on those wastes minutes.
                    log.debug("%s failed permanently: %s", url, exc)
                    break
                await asyncio.sleep(min(2**attempt, 8))
                continue

            if resp.status_code in (429, 500, 502, 503, 504) and attempt < self.settings.max_retries:
                retry_after = resp.headers.get("retry-after")
                sleep_for = (
                    float(retry_after)
                    if (retry_after or "").isdigit()
                    else min(2**attempt, 8)
                )
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
        return Page(
            url=url,
            status=NETWORK_FAILED,
            html="",
            error=f"{type(last_error).__name__}: {last_error}" if last_error else "no response",
        )

    async def _ensure_browser(self):
        async with self._browser_lock:
            if self._browser is None:
                from playwright.async_api import async_playwright

                self._playwright = await async_playwright().start()
                args = [
                    "--disable-blink-features=AutomationControlled",
                    "--disable-features=IsolateOrigins,site-per-process",
                ]

                # Launch by explicit path rather than channel="chrome". The
                # channel lookup takes the first Chrome it finds, which on a
                # machine with both a machine-wide and a per-user install can
                # be a badly outdated one — and the UA we claim is derived from
                # whatever we launch, so picking the newest keeps the two
                # consistent.
                chrome = find_chrome()
                if chrome is not None:
                    path, version = chrome
                    log.debug("using Chrome %s at %s", ".".join(map(str, version)), path)
                    self._browser = await self._playwright.chromium.launch(
                        executable_path=path,
                        headless=self.settings.headless,
                        args=args,
                    )
                else:
                    log.debug("no system Chrome found; using bundled Chromium")
                    self._browser = await self._playwright.chromium.launch(
                        headless=self.settings.headless,
                        args=args,
                    )
            return self._browser

    async def _apply_stealth(self, context) -> None:
        """Patch the headless tells, if playwright-stealth is installed.

        The package renamed its entry point in 2.0 (``stealth_async`` became
        ``Stealth.apply_stealth_async``), so both spellings are tried. A plain
        ``except ImportError`` here would hide a version mismatch and leave the
        crawler silently unstealthed, which is how this went unnoticed before —
        so a missing package is logged once, at debug level.
        """
        if self._stealth_unavailable:
            return
        try:
            from playwright_stealth import Stealth
        except ImportError:
            try:
                from playwright_stealth import stealth_async  # type: ignore[attr-defined]
            except ImportError:
                self._stealth_unavailable = True
                log.debug(
                    "playwright-stealth not installed; rendering without stealth patches"
                )
                return
            await stealth_async(context)
            return

        await Stealth().apply_stealth_async(context)

    async def _render(self, url: str, profile: SessionProfile | None = None) -> Page:
        if profile is None:
            profile = create_session_profile()

        try:
            browser = await self._ensure_browser()
        except Exception as exc:  # playwright missing / browser not installed
            log.warning(
                "browser rendering unavailable (%s). Run: playwright install chromium", exc
            )
            self.settings.render = "never"
            return Page(
                url=url, status=NETWORK_FAILED, html="", error=f"browser unavailable: {exc}"
            )

        context_opts = profile.to_playwright_context_options()

        context = await browser.new_context(**context_opts)

        # Optional stealth patches (pip install playwright-stealth). Applied to
        # the *context*, so the init scripts land on every page it opens rather
        # than only the first one. Without this, headless Chrome has no
        # ``window.chrome`` and no ``navigator.plugins`` — both are the first
        # things a detector looks at after ``navigator.webdriver``.
        await self._apply_stealth(context)

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
            return Page(
                url=url, status=NETWORK_FAILED, html="",
                error=f"render failed: {type(exc).__name__}: {exc}",
            )
        finally:
            await context.close()


# DNS answers don't change between two attempts a second apart.
_PERMANENT_ERRORS = (
    "getaddrinfo failed",
    "name or service not known",
    "nodename nor servname",
    "temporary failure in name resolution",
    "no address associated with hostname",
)


def _is_permanent(exc: Exception) -> bool:
    """True when retrying this failure cannot plausibly help."""
    if not isinstance(exc, httpx.ConnectError):
        return False
    message = str(exc).lower()
    return any(marker in message for marker in _PERMANENT_ERRORS)


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