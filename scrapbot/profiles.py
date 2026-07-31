# scrapbot/profiles.py
from __future__ import annotations

import os
import random
import re
import sys
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any


# Used only if no real Chrome can be found to read a version off.
FALLBACK_CHROME_MAJOR = 131

UA_TEMPLATES = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36",
]

# Where Chrome installs itself on each platform. Windows is listed with the
# machine-wide path first: a stale per-user copy under LOCALAPPDATA is common
# (it is what Playwright's channel="chrome" picked here, an engine 47 versions
# behind the machine-wide one), so every candidate is version-checked rather
# than taking the first hit.
_CHROME_PATHS = {
    "win32": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ],
    "darwin": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"],
    "linux": ["/usr/bin/google-chrome", "/usr/bin/google-chrome-stable"],
}


def _version_of(path: str) -> tuple[int, ...] | None:
    """Read a Chrome build's version without launching it."""
    if not os.path.exists(path):
        return None
    if sys.platform == "win32":
        # Reading the PE version resource avoids spawning the browser, which
        # on Windows would flash a window and cost ~a second.
        try:
            import subprocess

            out = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    f"(Get-Item '{path}').VersionInfo.ProductVersion",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            ).stdout.strip()
        except Exception:
            return None
    else:
        try:
            import subprocess

            out = subprocess.run(
                [path, "--version"], capture_output=True, text=True, timeout=15
            ).stdout.strip()
        except Exception:
            return None
    found = re.search(r"(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?", out)
    if not found:
        return None
    return tuple(int(g) for g in found.groups() if g is not None)


@lru_cache(maxsize=1)
def find_chrome() -> tuple[str, tuple[int, ...]] | None:
    """The newest real Chrome on this machine, as ``(path, version)``.

    Returns ``None`` when Chrome isn't installed, in which case the caller
    should fall back to Playwright's bundled Chromium.
    """
    candidates = []
    for path in _CHROME_PATHS.get(sys.platform, []):
        version = _version_of(path)
        if version:
            candidates.append((path, version))
    if not candidates:
        return None
    return max(candidates, key=lambda pair: pair[1])


@lru_cache(maxsize=1)
def chrome_major() -> int:
    """Major version to claim in the User-Agent.

    Derived from the browser we will actually launch, so the UA string can't
    contradict the engine reported by Client Hints and feature detection.
    """
    found = find_chrome()
    return found[1][0] if found else FALLBACK_CHROME_MAJOR


def user_agents() -> list[str]:
    return [template.format(major=chrome_major()) for template in UA_TEMPLATES]


# Convenience for callers (and tests) that just want the current pool.
USER_AGENTS = user_agents()

ACCEPT_LANGUAGES = [
    "en-US,en;q=0.9",
    "en-GB,en;q=0.9",
    "de-DE,de;q=0.9,en;q=0.8",
    "es-ES,es;q=0.9,en;q=0.8",
]


def _supported_encodings() -> str:
    """Only advertise what httpx can actually decode.

    Chrome sends ``gzip, deflate, br, zstd``, but brotli/zstd decoding in httpx
    needs optional packages. Claiming ``br`` without them means a server may
    reply with brotli that we hand back as binary garbage.
    """
    encodings = ["gzip", "deflate"]
    try:
        import brotli  # noqa: F401

        encodings.append("br")
    except ImportError:
        try:
            import brotlicffi  # noqa: F401

            encodings.append("br")
        except ImportError:
            pass
    try:
        import zstandard  # noqa: F401

        encodings.append("zstd")
    except ImportError:
        pass
    return ", ".join(encodings)


ACCEPT_ENCODING = _supported_encodings()


@dataclass
class SessionProfile:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    user_agent: str = field(default_factory=lambda: random.choice(user_agents()))
    accept_language: str = field(default_factory=lambda: random.choice(ACCEPT_LANGUAGES))
    # No proxy field needed if you never use proxies

    def to_httpx_headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": self.accept_language,
            "Accept-Encoding": ACCEPT_ENCODING,
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Cache-Control": "max-age=0",
        }

    def to_playwright_context_options(self) -> dict[str, Any]:
        locale = self.accept_language.split(",", 1)[0]

        opts: dict[str, Any] = {
            "user_agent": self.user_agent,
            "locale": locale,
            "viewport": {"width": 1366, "height": 768},
            # No proxy key here
        }
        return opts


def create_session_profile() -> SessionProfile:
    """Create a fresh random fingerprint profile."""
    return SessionProfile()