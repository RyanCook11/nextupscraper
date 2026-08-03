"""Source plugin contract.

A source knows how to turn CLI arguments into a stream of :class:`Lead`
objects. It is handed an already-configured :class:`Fetcher`, so it never
worries about robots.txt, throttling or rendering.
"""

from __future__ import annotations

import argparse
from abc import ABC, abstractmethod
from typing import AsyncIterator

from ..config import Settings
from ..models import Contact, Lead, SiteOutcome
from ..net import Fetcher


class Source(ABC):
    name: str = "unnamed"
    help: str = ""
    record_cls: type[Lead] | type[Contact] = Lead
    """What this source yields. Decides which store the runner writes to."""

    def __init__(self, settings: Settings, args: argparse.Namespace) -> None:
        self.settings = settings
        self.args = args
        self.outcomes: list[SiteOutcome] = []
        """One entry per site attempted. The runner turns these into the run
        report, so a site that blocked us is distinguishable from one that
        simply had nothing to find."""

        self.rosters: dict[str, set[str]] = {}
        """Everyone a successfully-scraped site listed, keyed by school domain.

        This is the *complete* set the page showed, recorded before any
        ``--coaches-only`` or ``--sport`` filter narrows what gets yielded.
        The runner needs the whole roster to tell "not on the staff page any
        more" from "filtered out of this run", and only what the source
        actually saw can answer that. A source that cannot enumerate a full
        roster simply leaves this empty and no reconciliation happens."""

    def record(self, outcome: SiteOutcome) -> SiteOutcome:
        self.outcomes.append(outcome)
        return outcome

    def note_roster(self, contacts: list[Contact]) -> None:
        """Record the full membership of a site we scraped successfully.

        Unioned rather than replaced: a site whose staff are spread over one
        page per sport is scraped in several passes, and each pass sees only
        its own slice.
        """
        for contact in contacts:
            self.rosters.setdefault(contact.school_domain.lower(), set()).add(contact.key)

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser) -> None:
        """Register source-specific CLI flags. Optional."""

    @abstractmethod
    def run(self, fetcher: Fetcher) -> AsyncIterator[Lead]:
        """Yield leads. Implement as ``async def`` with ``yield``."""
        raise NotImplementedError
