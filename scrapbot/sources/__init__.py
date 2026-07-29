"""Source registry.

Add a new source by dropping a module in this package and registering the
class below — the CLI picks it up automatically.
"""

from __future__ import annotations

from .base import Source
from .coaches import CoachesSource
from .directory import DirectorySource
from .schools import SchoolsSource
from .website import WebsiteSource

SOURCES: dict[str, type[Source]] = {
    WebsiteSource.name: WebsiteSource,
    DirectorySource.name: DirectorySource,
    CoachesSource.name: CoachesSource,
    SchoolsSource.name: SchoolsSource,
}


def get(name: str) -> type[Source]:
    try:
        return SOURCES[name]
    except KeyError:
        known = ", ".join(sorted(SOURCES))
        raise SystemExit(f"unknown source {name!r}. Available: {known}") from None


__all__ = ["SOURCES", "Source", "get"]
