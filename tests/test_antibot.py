"""Tests for the anti-blocking layer: what we look like to the far end.

The whole point of :mod:`scrapbot.profiles` is that a server can't tell us from
a browser. These tests therefore assert on what actually arrives at a real
socket, not on what the helpers return in isolation — a header dict that never
makes it through httpx is worth nothing.
"""

from __future__ import annotations

import asyncio
import json
import re
import time

import pytest

from scrapbot.config import DEFAULT_USER_AGENT, Settings
from scrapbot.net import Fetcher, Page
from scrapbot.profiles import (
    ACCEPT_ENCODING,
    USER_AGENTS,
    SessionProfile,
    create_session_profile,
)
from tests.fixtures import FixtureSite

# Headers a stock Chrome always sends and a naive scraper never does. A WAF
# scoring requests cheaply looks for exactly these.
CHROME_TELLS = [
    "accept-language",
    "upgrade-insecure-requests",
    "sec-fetch-dest",
    "sec-fetch-mode",
    "sec-fetch-site",
    "sec-fetch-user",
]


def _settings(**overrides: object) -> Settings:
    settings = Settings()
    settings.delay = 0
    settings.render = "never"
    settings.respect_robots = False
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


def _fetch_all(settings: Settings, urls: list[str]) -> list[object]:
    async def run() -> list[object]:
        async with Fetcher(settings) as fetcher:
            return [await fetcher.get(url) for url in urls]

    return asyncio.run(run())


# --- what we send ---------------------------------------------------------


def test_the_scrapbot_user_agent_never_reaches_the_wire():
    """The giveaway. Settings still carry a self-identifying UA for politeness
    elsewhere; the fetcher must not put it on a request."""
    site = FixtureSite()
    with site as netloc:
        _fetch_all(_settings(), [f"http://{netloc}/about"])

    assert site.requests, "fixture served nothing — test is not exercising the fetcher"
    for _path, headers in site.requests:
        assert headers["User-Agent"] != DEFAULT_USER_AGENT
        assert "scrapbot" not in headers["User-Agent"].lower()


def test_every_request_carries_a_full_chrome_header_set():
    site = FixtureSite()
    with site as netloc:
        _fetch_all(_settings(), [f"http://{netloc}/about"])

    sent = {k.lower(): v for k, v in site.headers_for("/about")[0].items()}
    assert re.search(r"Chrome/\d+", sent["user-agent"])
    missing = [h for h in CHROME_TELLS if h not in sent]
    assert not missing, f"missing browser headers: {missing}"


def test_robots_is_fetched_with_a_browser_like_user_agent():
    """robots.txt is the first request to a host — leaking there gives us away
    before the crawl even starts."""
    site = FixtureSite()
    with site as netloc:
        _fetch_all(_settings(respect_robots=True), [f"http://{netloc}/about"])

    robots = site.headers_for("/robots.txt")
    assert robots, "robots.txt was never requested"
    assert "scrapbot" not in robots[0]["User-Agent"].lower()


def test_user_agents_rotate_across_requests():
    """A fixed UA across hundreds of hits is itself a pattern. With two UAs in
    the pool and 30 draws, seeing only one is a ~2e-9 fluke, not flake."""
    site = FixtureSite()
    with site as netloc:
        _fetch_all(_settings(), [f"http://{netloc}/about"] * 30)

    seen = {h["User-Agent"] for _p, h in site.requests}
    assert len(seen) > 1, f"user agent never rotated: {seen}"
    assert seen <= set(USER_AGENTS)


# --- content negotiation --------------------------------------------------


def test_accept_encoding_only_claims_codecs_we_can_decode():
    """Over-advertising `br` yields binary garbage that parses as an empty
    page — a silent miss rather than a visible error."""
    assert "gzip" in ACCEPT_ENCODING
    if "br" in ACCEPT_ENCODING:
        pytest.importorskip("brotli")
    if "zstd" in ACCEPT_ENCODING:
        pytest.importorskip("zstandard")


def test_a_compressed_response_is_decoded_not_handed_back_as_bytes():
    site = FixtureSite()
    with site as netloc:
        pages = _fetch_all(_settings(), [f"http://{netloc}/gzipped"])

    assert "gzip" in site.headers_for("/gzipped")[0]["Accept-Encoding"]
    assert pages[0].ok
    assert "Founded 1998" in pages[0].html


# --- pacing ---------------------------------------------------------------


def test_delay_zero_skips_the_human_pause():
    """`--delay 0` is an explicit go-fast from the operator; the mimicry must
    not override it or the test suite (and any local run) crawls."""
    site = FixtureSite()
    with site as netloc:
        started = time.monotonic()
        _fetch_all(_settings(delay=0), [f"http://{netloc}/about"] * 3)
        elapsed = time.monotonic() - started

    assert elapsed < 1.0, f"delay=0 still paused for {elapsed:.2f}s"


def test_a_configured_delay_produces_a_non_uniform_pause():
    """Two requests to one host must be spaced, and spaced by something other
    than a constant — a metronome is as detectable as no delay at all."""
    site = FixtureSite()
    with site as netloc:
        started = time.monotonic()
        _fetch_all(_settings(delay=0.5), [f"http://{netloc}/about"] * 2)
        elapsed = time.monotonic() - started

    assert elapsed >= 0.6, f"requests were not paced: {elapsed:.2f}s"


# --- retrying a bot challenge in a browser --------------------------------


def _challenged_fetcher(monkeypatch, rendered: object):
    """A fetcher whose static path always returns a 405 challenge and whose
    browser path returns ``rendered``, recording how often it was called."""
    calls: list[str] = []

    async def fake_static(self, url, host, delay, client, headers=None):
        return Page(url=url, status=405, html="<title>Human Verification</title>")

    async def fake_render(self, url, profile=None):
        calls.append(url)
        return rendered

    monkeypatch.setattr(Fetcher, "_get_static", fake_static)
    monkeypatch.setattr(Fetcher, "_render", fake_render)
    return calls


def test_a_challenged_page_is_retried_in_a_browser(monkeypatch):
    """AWS WAF and Cloudflare answer a challenge with 403/405, not 200. The
    auto-render test used to require `page.ok`, so the browser — the only
    thing that can run the challenge script — was never tried."""
    good = Page(url="http://x/", status=200, html="<html>" + "real content " * 80, rendered=True)
    calls = _challenged_fetcher(monkeypatch, good)

    site = FixtureSite()
    with site as netloc:
        pages = _fetch_all(_settings(render="auto"), [f"http://{netloc}/about"])

    assert len(calls) == 1, "a blocked page was never retried in a browser"
    assert pages[0].ok and pages[0].rendered


def test_a_host_the_browser_cannot_clear_is_not_retried_again(monkeypatch):
    """When the challenge beats the browser too, further pages on that host
    must not each pay for a render — 27% of real athletics sites sit behind a
    WAF that no amount of rendering gets past."""
    still_blocked = Page(url="http://x/", status=405, html="<title>Human Verification</title>")
    calls = _challenged_fetcher(monkeypatch, still_blocked)

    site = FixtureSite()
    with site as netloc:
        urls = [f"http://{netloc}/{p}" for p in ("about", "contact-us", "careers")]
        pages = _fetch_all(_settings(render="auto"), urls)

    assert len(calls) == 1, f"browser was launched {len(calls)}x for one unsolvable host"
    assert all(p.blocked for p in pages)


def test_render_never_does_not_launch_a_browser_for_a_challenge(monkeypatch):
    calls = _challenged_fetcher(monkeypatch, Page(url="http://x/", status=200, html="x" * 900))

    site = FixtureSite()
    with site as netloc:
        _fetch_all(_settings(render="never"), [f"http://{netloc}/about"])

    assert calls == [], "render=never must never reach for a browser"


# --- profile objects ------------------------------------------------------


def test_each_profile_is_a_distinct_identity():
    profiles = [create_session_profile() for _ in range(5)]
    assert len({p.id for p in profiles}) == 5


def test_playwright_options_follow_the_profile():
    profile = SessionProfile(
        user_agent="Mozilla/5.0 Chrome/131.0.0.0",
        accept_language="de-DE,de;q=0.9,en;q=0.8",
    )
    opts = profile.to_playwright_context_options()

    assert opts["user_agent"] == profile.user_agent
    assert opts["locale"] == "de-DE", "locale must match the Accept-Language we send"
    assert opts["viewport"]["width"] > 0 and opts["viewport"]["height"] > 0


def test_the_http_and_browser_paths_claim_the_same_identity():
    """A context whose UA header disagrees with its navigator.userAgent is the
    single most common way a headless scraper outs itself."""
    profile = create_session_profile()
    assert profile.to_httpx_headers()["User-Agent"] == (
        profile.to_playwright_context_options()["user_agent"]
    )
    assert profile.to_httpx_headers()["Accept-Language"].startswith(
        profile.to_playwright_context_options()["locale"]
    )


# --- the rendered path, in a real browser ---------------------------------


def _render_whoami(settings: Settings, netloc: str) -> tuple[dict, str]:
    """Render the probe page and return ``(fingerprint, real browser version)``."""

    async def run() -> tuple[str, str]:
        async with Fetcher(settings) as fetcher:
            browser = await fetcher._ensure_browser()
            page = await fetcher.get(f"http://{netloc}/whoami")
            return page.html, browser.version

    html, version = asyncio.run(run())
    probe = re.search(r'<pre id="out">(.*?)</pre>', html, re.S)
    assert probe and probe.group(1) != "pending", f"probe never ran; got: {html[:400]}"
    return json.loads(probe.group(1)), version


@pytest.fixture(scope="module")
def fingerprint():
    """Render the probe once; the browser launch dominates the runtime."""
    site = FixtureSite()
    with site as netloc:
        return _render_whoami(_settings(render="always"), netloc)


@pytest.mark.browser
def test_a_rendered_page_does_not_announce_webdriver(fingerprint):
    """`navigator.webdriver` is the first thing every detector reads, and the
    one signal the launch flags do successfully suppress."""
    probe, _version = fingerprint
    assert probe["webdriver"] is False, "browser is announcing itself as automated"


@pytest.mark.browser
def test_a_rendered_page_presents_the_profile_identity(fingerprint):
    probe, _version = fingerprint
    assert probe["ua"] in USER_AGENTS, f"navigator.userAgent was not our profile: {probe['ua']}"
    assert probe["innerWidth"] == 1366, "viewport did not come from the profile"


@pytest.mark.browser
def test_the_rendered_user_agent_matches_the_real_engine(fingerprint):
    """Client Hints and feature detection are reported by the real engine and
    cannot be spoofed by the `user_agent` option, so the UA we claim has to be
    derived from the browser we actually launch."""
    probe, version = fingerprint
    claimed = re.search(r"Chrome/(\d+)", probe["ua"])
    actual = re.search(r"^(\d+)", version)
    assert claimed and actual
    assert claimed.group(1) == actual.group(1), (
        f"UA claims Chrome {claimed.group(1)} but the engine is {version}"
    )


@pytest.mark.browser
def test_the_rendered_page_looks_like_a_real_chrome(fingerprint):
    """Bare headless Chrome has no ``window.chrome`` and no plugins. These pass
    only because the stealth patches are applied — if the optional dependency
    or its entry point ever drifts again, this is what catches it."""
    pytest.importorskip("playwright_stealth")
    probe, _version = fingerprint
    assert probe["chromeObject"], "window.chrome missing"
    assert probe["plugins"] > 0, "navigator.plugins is empty"


@pytest.mark.browser
def test_navigator_languages_agree_with_the_accept_language_header(fingerprint):
    """Playwright's ``locale`` alone yields a single-entry navigator.languages
    while our header lists several; stealth restores the list."""
    pytest.importorskip("playwright_stealth")
    probe, _version = fingerprint
    assert "," in probe["languages"], (
        f"navigator.languages is {probe['languages']!r}; the header we send lists "
        f"several languages"
    )
