# scrapbot/cache.py
"""On-disk cache of fetched responses.

A sweep of a few thousand schools re-visits the same pages constantly: a
re-run after a crash, a ``retry-failed`` pass, a second source that happens to
walk the same athletics site. Every one of those was a fresh request to
someone's server, and request *volume* — not what our headers look like — is
what puts a host into challenging us.

The cache is keyed on the URL and expires on age alone. Nothing here tries to
implement HTTP caching semantics (``ETag``, ``Vary``, ``Cache-Control``); the
pages we scrape are directory listings that change on the scale of a season,
so a flat TTL is both simpler and closer to how the data actually behaves.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("scrapbot.cache")


@dataclass
class CachedResponse:
    url: str
    """The URL as requested."""
    final_url: str
    """Where it landed after redirects."""
    status: int
    body: str | bytes
    content_type: str = ""
    rendered: bool = False
    """Whether a browser produced this body, so a cache hit can say so."""
    fetched_at: float = 0.0

    @property
    def age(self) -> float:
        return max(0.0, time.time() - self.fetched_at)


# Statuses worth remembering. 2xx is the point; 404/410 are cached because a
# dead page stays dead and re-asking costs the same as asking.
CACHEABLE_STATUSES = frozenset({200, 201, 203, 204, 301, 308, 404, 410})


class ResponseCache:
    """Content-addressed response store under ``root``.

    Disabled — every call becomes a no-op — when ``ttl`` is zero or negative,
    which is what ``--no-cache`` sets.
    """

    def __init__(self, root: Path, ttl: float) -> None:
        self.root = Path(root)
        self.ttl = float(ttl)
        self.hits = 0
        self.misses = 0
        self.writes = 0

    @property
    def enabled(self) -> bool:
        return self.ttl > 0

    # -- layout -----------------------------------------------------------
    def _path(self, url: str) -> Path:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        # One level of fan-out: a flat directory of 100k entries is slow to
        # list on Windows and unpleasant to inspect by hand.
        return self.root / digest[:2] / f"{digest}.json"

    # -- read -------------------------------------------------------------
    def get(self, url: str) -> CachedResponse | None:
        """The stored response for ``url``, or ``None`` if absent or stale."""
        if not self.enabled:
            return None
        path = self._path(url)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.misses += 1
            return None
        except (OSError, ValueError) as exc:
            # A truncated entry (killed mid-write on a previous run) must not
            # take the crawl down with it — drop it and re-fetch.
            log.debug("discarding unreadable cache entry %s: %s", path, exc)
            _unlink(path)
            self.misses += 1
            return None

        fetched_at = float(raw.get("fetched_at", 0.0))
        if time.time() - fetched_at > self.ttl:
            self.misses += 1
            return None

        body: str | bytes
        if raw.get("binary"):
            body = base64.b64decode(raw.get("body", ""))
        else:
            body = raw.get("body", "")

        self.hits += 1
        return CachedResponse(
            url=raw.get("url", url),
            final_url=raw.get("final_url", url),
            status=int(raw.get("status", 0)),
            body=body,
            content_type=raw.get("content_type", ""),
            rendered=bool(raw.get("rendered", False)),
            fetched_at=fetched_at,
        )

    # -- write ------------------------------------------------------------
    def put(
        self,
        url: str,
        *,
        status: int,
        body: str | bytes,
        final_url: str | None = None,
        content_type: str = "",
        rendered: bool = False,
    ) -> None:
        if not self.enabled or status not in CACHEABLE_STATUSES:
            return

        binary = isinstance(body, (bytes, bytearray))
        payload = {
            "url": url,
            "final_url": final_url or url,
            "status": status,
            "content_type": content_type,
            "rendered": rendered,
            "fetched_at": time.time(),
            "binary": binary,
            "body": base64.b64encode(bytes(body)).decode("ascii") if binary else body,
        }

        path = self._path(url)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Write-then-rename so a crash can't leave a half-written entry
            # that the next run would have to parse. The pid suffix keeps two
            # concurrent runs from colliding on the same temp name.
            tmp = path.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            log.debug("could not cache %s: %s", url, exc)
            return
        self.writes += 1

    # -- maintenance ------------------------------------------------------
    def purge_expired(self) -> int:
        """Delete entries older than the TTL. Returns how many went."""
        if not self.root.exists():
            return 0
        cutoff = time.time() - self.ttl
        removed = 0
        for entry in self.root.glob("*/*.json"):
            try:
                if entry.stat().st_mtime < cutoff:
                    entry.unlink()
                    removed += 1
            except OSError:
                continue
        return removed

    def stats(self) -> dict[str, int]:
        return {"hits": self.hits, "misses": self.misses, "writes": self.writes}


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
