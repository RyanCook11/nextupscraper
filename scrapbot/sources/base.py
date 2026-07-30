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

    def record(self, outcome: SiteOutcome) -> SiteOutcome:
        self.outcomes.append(outcome)
        return outcome

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser) -> None:
        """Register source-specific CLI flags. Optional."""

    @abstractmethod
    def run(self, fetcher: Fetcher) -> AsyncIterator[Lead]:
        """Yield leads. Implement as ``async def`` with ``yield``."""
        raise NotImplementedError
