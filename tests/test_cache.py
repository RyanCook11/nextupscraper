"""Tests for the two things that lower how often we get challenged: not
re-fetching what we already have, and slowing down when a host pushes back.

Both are asserted against a real socket — the fixture server records every
request, so "the cache worked" means the far end genuinely saw fewer hits, not
that a counter went up.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from scrapbot.cache import ResponseCache
from scrapbot.config import Settings
from scrapbot.net import Fetcher
from tests.fixtures import FixtureSite


def _settings(tmp_path, **overrides: object) -> Settings:
    settings = Settings()
    settings.data_dir = tmp_path
    settings.delay = 0
    settings.render = "never"
    settings.respect_robots = False
    settings.cache_ttl = 3600.0
    for key, value in overrides.items():
        setattr(settings, key, value)
    settings.ensure_dirs()
    return settings


def _fetch_all(settings: Settings, urls: list[str]) -> list[object]:
    async def run() -> list[object]:
        async with Fetcher(settings) as fetcher:
            return [await fetcher.get(url) for url in urls]

    return asyncio.run(run())


# --- the cache ------------------------------------------------------------


def test_a_repeated_url_is_served_from_disk_not_from_the_server(tmp_path):
    """The whole point: request volume is what gets a crawler challenged."""
    settings = _settings(tmp_path)
    site = FixtureSite()
    with site as netloc:
        url = f"http://{netloc}/about"
        pages = _fetch_all(settings, [url] * 5)

    hits = [p for p, _h in site.requests if p.rstrip("/") == "/about"]
    assert len(hits) == 1, f"server was asked {len(hits)}x for a cacheable page"
    assert all(p.ok for p in pages)
    assert [p.from_cache for p in pages] == [False, True, True, True, True]


def test_the_cache_survives_across_runs(tmp_path):
    """A re-run after a crash, or a retry-failed pass, must not re-hit."""
    settings = _settings(tmp_path)
    site = FixtureSite()
    with site as netloc:
        url = f"http://{netloc}/about"
        _fetch_all(settings, [url])
        # A second Fetcher entirely — same on-disk cache directory.
        pages = _fetch_all(_settings(tmp_path), [url])

    assert len(site.headers_for("/about")) == 1
    assert pages[0].ok and pages[0].from_cache
    assert "Founded 1998" in pages[0].html


def test_a_stale_entry_is_refetched(tmp_path):
    settings = _settings(tmp_path, cache_ttl=0.3)
    site = FixtureSite()
    with site as netloc:
        url = f"http://{netloc}/about"
        _fetch_all(settings, [url])
        time.sleep(0.4)
        pages = _fetch_all(settings, [url])

    assert len(site.headers_for("/about")) == 2, "an expired entry was reused"
    assert not pages[0].from_cache


def test_cache_ttl_zero_disables_the_cache_entirely(tmp_path):
    """`--no-cache` sets this, and it has to mean *nothing* is reused."""
    settings = _settings(tmp_path, cache_ttl=0)
    site = FixtureSite()
    with site as netloc:
        _fetch_all(settings, [f"http://{netloc}/about"] * 3)

    assert len(site.headers_for("/about")) == 3
    assert not (tmp_path / "cache").exists() or not list((tmp_path / "cache").glob("*/*.json"))


def test_robots_is_read_from_cache_on_a_later_run(tmp_path):
    site = FixtureSite()
    with site as netloc:
        url = f"http://{netloc}/about"
        _fetch_all(_settings(tmp_path, respect_robots=True), [url])
        _fetch_all(_settings(tmp_path, respect_robots=True), [f"http://{netloc}/careers"])

    assert len(site.headers_for("/robots.txt")) == 1, "robots.txt was re-fetched"


def test_an_image_is_cached_as_bytes(tmp_path):
    settings = _settings(tmp_path)

    async def run(image: str) -> list[bytes | None]:
        async with Fetcher(settings) as fetcher:
            return [await fetcher.get_bytes(image) for _ in range(2)]

    site = FixtureSite()
    with site as netloc:
        first, second = asyncio.run(run(f"http://{netloc}/images/headshot.jpg"))

    assert first == second and first is not None
    assert first.startswith(b"\xff\xd8"), "not the JPEG the fixture serves"
    assert len(site.headers_for("/images/headshot.jpg")) == 1


def test_a_server_error_is_not_cached(tmp_path):
    """Caching a 500 would freeze a transient failure in for a week."""
    cache = ResponseCache(tmp_path / "cache", ttl=3600)
    cache.put("http://x/boom", status=500, body="<h1>oops</h1>")
    assert cache.get("http://x/boom") is None


def test_a_404_is_cached(tmp_path):
    """A dead page stays dead, and the seed lists carry plenty of them."""
    cache = ResponseCache(tmp_path / "cache", ttl=3600)
    cache.put("http://x/gone", status=404, body="<h1>404</h1>")
    entry = cache.get("http://x/gone")
    assert entry is not None and entry.status == 404


def test_a_corrupt_entry_is_dropped_rather_than_raised(tmp_path):
    """A run killed mid-write must not poison every later run."""
    cache = ResponseCache(tmp_path / "cache", ttl=3600)
    cache.put("http://x/page", status=200, body="hello")
    path = cache._path("http://x/page")
    path.write_text('{"status": 200, "bo', encoding="utf-8")

    assert cache.get("http://x/page") is None
    assert not path.exists(), "the unreadable entry was left behind"


def test_purge_expired_removes_only_stale_entries(tmp_path):
    cache = ResponseCache(tmp_path / "cache", ttl=0.2)
    cache.put("http://x/old", status=200, body="old")
    time.sleep(0.3)
    cache.put("http://x/new", status=200, body="new")

    assert cache.purge_expired() == 1
    assert cache.get("http://x/new") is not None


# --- adaptive backoff -----------------------------------------------------


def test_a_rate_limited_host_gets_a_longer_delay(tmp_path):
    """429 is the host saying we are asking too often. The answer is to keep
    asking less often for the rest of the run, not to sleep once and resume."""
    settings = _settings(tmp_path, delay=0.1, max_retries=0)
    site = FixtureSite()
    with site as netloc:
        host = netloc

        async def run() -> float:
            async with Fetcher(settings) as fetcher:
                await fetcher.get(f"http://{netloc}/rate-limited")
                return fetcher._host_penalty.get(host, 1.0)

        penalty = asyncio.run(run())

    assert penalty > 1.0, "a 429 did not slow the host down"


def test_the_penalty_compounds_and_is_capped(tmp_path):
    settings = _settings(tmp_path, delay=0.1, max_retries=0, max_backoff=4.0)
    site = FixtureSite()
    with site as netloc:

        async def run() -> float:
            async with Fetcher(settings) as fetcher:
                for _ in range(6):
                    await fetcher.get(f"http://{netloc}/rate-limited")
                return fetcher._host_penalty[netloc]

        penalty = asyncio.run(run())

    assert penalty == pytest.approx(4.0), f"penalty ran away to {penalty}"


def test_retry_after_is_honoured_as_a_host_wide_cooldown(tmp_path):
    """Retry-After applies to the host, not to the one request that saw it."""
    settings = _settings(tmp_path, delay=0.1, max_retries=0)
    site = FixtureSite()
    with site as netloc:

        async def run() -> float:
            async with Fetcher(settings) as fetcher:
                await fetcher.get(f"http://{netloc}/rate-limited")
                started = time.monotonic()
                await fetcher.get(f"http://{netloc}/about")
                return time.monotonic() - started

        waited = asyncio.run(run())

    assert waited >= 0.9, f"the Retry-After cooldown was ignored ({waited:.2f}s)"


def test_a_clean_response_eases_the_penalty_back_down(tmp_path):
    settings = _settings(tmp_path, delay=0, max_retries=0, backoff_factor=4.0)
    site = FixtureSite()
    with site as netloc:

        async def run() -> tuple[float, float]:
            async with Fetcher(settings) as fetcher:
                await fetcher.get(f"http://{netloc}/rate-limited")
                after_block = fetcher._host_penalty[netloc]
                # Distinct URLs so the cache doesn't answer these for us.
                for path in ("about", "careers", "contact-us"):
                    await fetcher.get(f"http://{netloc}/{path}")
                return after_block, fetcher._host_penalty.get(netloc, 1.0)

        blocked, recovered = asyncio.run(run())

    assert recovered < blocked, "the penalty never eased off"
    assert recovered >= 1.0


def test_recovery_is_slower_than_escalation(tmp_path):
    """One 200 mid-streak is not evidence a rate limit has lifted."""
    settings = _settings(tmp_path, delay=0, max_retries=0, backoff_factor=4.0)
    site = FixtureSite()
    with site as netloc:

        async def run() -> tuple[float, float]:
            async with Fetcher(settings) as fetcher:
                await fetcher.get(f"http://{netloc}/rate-limited")
                blocked = fetcher._host_penalty[netloc]
                await fetcher.get(f"http://{netloc}/about")
                return blocked, fetcher._host_penalty[netloc]

        blocked, after_one_ok = asyncio.run(run())

    assert after_one_ok > 1.0, "a single success wiped the whole penalty"
    assert after_one_ok < blocked


def test_delay_zero_is_not_resurrected_by_a_penalty(tmp_path):
    """`--delay 0` is an explicit go-fast; backoff must not override it. A
    Retry-After cooldown still applies — that one came from the server."""
    settings = _settings(tmp_path, delay=0, max_retries=0)
    site = FixtureSite()
    with site as netloc:

        async def run() -> float:
            async with Fetcher(settings) as fetcher:
                fetcher._host_penalty[netloc] = 16.0
                started = time.monotonic()
                for path in ("about", "careers", "contact-us"):
                    await fetcher.get(f"http://{netloc}/{path}")
                return time.monotonic() - started

        elapsed = asyncio.run(run())

    assert elapsed < 1.0, f"delay=0 still paced at {elapsed:.2f}s"
