"""Suite-wide defaults.

The response cache is real and on by default, which is right for a crawl and
wrong for a test: a test that fetches the same fixture URL twice would get the
second one off disk, silently stop exercising the fetcher, and — worse — leave
entries in the project's real ``data/cache``. Everything here starts cold, and
the cache's own tests turn it back on explicitly.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def cold_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    # Settings reads this at construction, so it reaches every Settings()
    # built inside a test without each one having to know about it.
    monkeypatch.setenv("SCRAPBOT_CACHE_TTL", "0")
