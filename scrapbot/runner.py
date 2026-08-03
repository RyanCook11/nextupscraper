"""Glue: drive a source, persist what it yields, report what happened."""

from __future__ import annotations

import argparse
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import sources, storage
from .config import Settings
from .models import Contact, Lead, School, SiteOutcome
from .net import Fetcher

log = logging.getLogger("scrapbot.runner")


@dataclass
class RunResult:
    run_id: str
    source: str
    leads: list[Lead | Contact | School] = field(default_factory=list)
    new: int = 0
    updated: int = 0
    departed: int = 0
    """People marked as gone because a successful re-scrape no longer listed them."""
    returned: int = 0
    """People previously marked departed who turned up again — the flag is cleared."""
    seconds: float = 0.0
    fetch_stats: dict = field(default_factory=dict)
    out_dir: Path | None = None
    outcomes: list[SiteOutcome] = field(default_factory=list)
    reconcile: storage.ReconcileReport | None = None

    @property
    def with_contact(self) -> int:
        return sum(1 for lead in self.leads if lead.emails or lead.phones)

    @property
    def succeeded(self) -> list[SiteOutcome]:
        return [o for o in self.outcomes if o.ok]

    @property
    def failed(self) -> list[SiteOutcome]:
        return [o for o in self.outcomes if not o.ok]

    @property
    def retryable(self) -> list[SiteOutcome]:
        """Failures worth another attempt — blocks, timeouts, crashes."""
        return [o for o in self.outcomes if o.retryable]

    def by_status(self) -> dict[str, list[SiteOutcome]]:
        grouped: dict[str, list[SiteOutcome]] = {}
        for outcome in self.outcomes:
            grouped.setdefault(outcome.status, []).append(outcome)
        return grouped


async def run_source(
    source_name: str,
    args: argparse.Namespace,
    settings: Settings,
    *,
    dry_run: bool = False,
) -> RunResult:
    settings.ensure_dirs()
    if dry_run:
        return await _run_source(source_name, args, settings, dry_run=True)
    # A dry run writes nothing, so it needs no lock. Everything else does:
    # two runs sharing a data dir overwrite each other's contacts wholesale.
    with storage.StoreLock(settings):
        return await _run_source(source_name, args, settings, dry_run=False)


async def _run_source(
    source_name: str,
    args: argparse.Namespace,
    settings: Settings,
    *,
    dry_run: bool,
) -> RunResult:
    source_cls = sources.get(source_name)
    source = source_cls(settings, args)
    store_cls = {
        Contact: storage.ContactStore,
        School: storage.SchoolStore,
    }.get(source_cls.record_cls, storage.LeadStore)
    store = store_cls(settings).load()

    rid = storage.run_id()
    result = RunResult(run_id=rid, source=source_name)
    started = time.monotonic()

    checkpoint_secs = getattr(settings, "checkpoint_secs", 0) or 0
    last_checkpoint = time.monotonic()

    async with Fetcher(settings) as fetcher:
        async for lead in source.run(fetcher):
            status = "-" if dry_run else store.upsert(lead)
            result.leads.append(lead)
            log.info(
                "[%s] %-32s %-28s %s",
                status,
                lead.label[:32],
                lead.sublabel[:28],
                _summarize(lead),
            )
            # Sweeps run for hours; without this the store is written once, at
            # the very end, and an interrupted run loses everything it found.
            if (
                not dry_run
                and checkpoint_secs > 0
                and time.monotonic() - last_checkpoint >= checkpoint_secs
            ):
                store.save(checkpoint=True)
                last_checkpoint = time.monotonic()
        result.fetch_stats = dict(fetcher.stats)
        result.fetch_stats["cache"] = fetcher.cache.stats()

    result.outcomes = list(source.outcomes)

    # Reconcile before the counters are read: a coach the school stopped
    # listing has left, and merging alone can never notice that, because it
    # only ever sees records that *were* scraped.
    #
    # Only successful sites are eligible, and that is enforced at the source:
    # note_roster() is called on the OK path and nowhere else, so a blocked,
    # empty or crashed site contributes no roster and its people are never
    # touched. Filtering again here against outcome domains would be worse
    # than redundant — a site reached through a host mapping files its outcome
    # under the seed domain but its people under the athletics host, so the
    # intersection would silently drop schools that scraped perfectly well.
    if not dry_run and getattr(source, "rosters", None) and not getattr(
        args, "no_reconcile", False
    ):
        result.reconcile = store.reconcile(
            source.rosters,
            max_loss=getattr(args, "reconcile_max_loss", 0.5),
        )
        result.departed = result.reconcile.total

    result.seconds = time.monotonic() - started
    result.new = store.new_count
    result.updated = store.updated_count
    result.returned = store.returned_count

    if dry_run:
        log.info("dry run — nothing written to %s", settings.data_dir)
        return result

    store.save()
    result.out_dir = storage.save_run(
        settings,
        rid,
        result.leads,
        {
            "run_id": rid,
            "source": source_name,
            "sites": {
                "attempted": len(result.outcomes),
                "succeeded": len(result.succeeded),
                "failed": len(result.failed),
                "by_status": {k: len(v) for k, v in result.by_status().items()},
            },
            "args": {
                k: (str(v) if isinstance(v, Path) else v)
                for k, v in vars(args).items()
                if k not in {"func", "settings"}
            },
            "leads": len(result.leads),
            "new": result.new,
            "updated": result.updated,
            "returned": result.returned,
            "reconcile": result.reconcile.to_dict() if result.reconcile else None,
            "seconds": round(result.seconds, 1),
            "fetch_stats": result.fetch_stats,
        },
        outcomes=result.outcomes,
    )
    return result


def _summarize(lead: Lead | Contact | School) -> str:
    if isinstance(lead, School):
        bits = [b for b in (lead.division, lead.state, lead.totalYearlyCost) if b]
        return " | ".join(bits) or "no details"

    bits = []
    if lead.emails:
        bits.append(lead.emails[0])
    if lead.phones:
        bits.append(lead.phones[0])
    extra = getattr(lead, "location", None) or getattr(lead, "sport", None)
    if extra:
        bits.append(extra[:30])
    if not bits:
        bits.append("no contact details")
    return " | ".join(bits)
