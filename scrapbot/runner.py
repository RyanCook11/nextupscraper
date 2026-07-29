"""Glue: drive a source, persist what it yields, report what happened."""

from __future__ import annotations

import argparse
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import sources, storage
from .config import Settings
from .models import Contact, Lead, School
from .net import Fetcher

log = logging.getLogger("scrapbot.runner")


@dataclass
class RunResult:
    run_id: str
    source: str
    leads: list[Lead | Contact | School] = field(default_factory=list)
    new: int = 0
    updated: int = 0
    seconds: float = 0.0
    fetch_stats: dict = field(default_factory=dict)
    out_dir: Path | None = None

    @property
    def with_contact(self) -> int:
        return sum(1 for lead in self.leads if lead.emails or lead.phones)


async def run_source(
    source_name: str,
    args: argparse.Namespace,
    settings: Settings,
    *,
    dry_run: bool = False,
) -> RunResult:
    settings.ensure_dirs()
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
        result.fetch_stats = dict(fetcher.stats)

    result.seconds = time.monotonic() - started
    result.new = store.new_count
    result.updated = store.updated_count

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
            "args": {
                k: (str(v) if isinstance(v, Path) else v)
                for k, v in vars(args).items()
                if k not in {"func", "settings"}
            },
            "leads": len(result.leads),
            "new": result.new,
            "updated": result.updated,
            "seconds": round(result.seconds, 1),
            "fetch_stats": result.fetch_stats,
        },
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
