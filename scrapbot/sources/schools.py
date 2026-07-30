"""``schools`` source — build the institution records the origin database holds.

Division and conference come from the NCAA member directory; city, cost,
SAT/ACT and public/private come from the federal College Scorecard; region is
derived from the state. Nothing here is scraped off a marketing site, because
every field has an official machine-readable source.

Each record also carries the school's athletics host, which is the join to the
``coaches`` source — a :class:`Contact`'s ``school_domain`` is this school's
``athletics_domain``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from typing import AsyncIterator

from .. import apis, usregions
from ..models import School
from ..net import Fetcher
from .base import Source

log = logging.getLogger("scrapbot.schools")


class SchoolsSource(Source):
    name = "schools"
    help = "Build school records (division, conference, cost, academics) from official APIs."
    record_cls = School

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--association",
            nargs="+",
            default=["ncaa", "naia", "njcaa"],
            choices=["ncaa", "naia", "njcaa"],
            metavar="ASSOC",
            help="Which associations to build (default: all three).",
        )
        parser.add_argument(
            "--division",
            nargs="+",
            default=["I", "II", "III"],
            choices=["I", "II", "III"],
            metavar="DIV",
            help="NCAA divisions to include (default: all three). NAIA has none.",
        )
        parser.add_argument(
            "--naia-pdf",
            metavar="URL",
            help="Override the NAIA member-institutions PDF URL (it moves each season).",
        )
        parser.add_argument(
            "--state",
            nargs="+",
            default=[],
            metavar="ST",
            help="Only these states, as codes or names.",
        )
        parser.add_argument(
            "--school",
            nargs="+",
            default=[],
            metavar="NAME",
            help="Only schools whose name contains one of these (case-insensitive).",
        )
        parser.add_argument(
            "--no-academics",
            action="store_true",
            help="Skip the College Scorecard lookup — NCAA fields only, no API key needed.",
        )
        parser.add_argument(
            "--limit", type=int, default=0, help="Stop after this many schools (0 = no limit)."
        )

    async def run(self, fetcher: Fetcher) -> AsyncIterator[School]:
        # This source talks to JSON APIs, not web pages, so it uses its own
        # client rather than the HTML fetcher it is handed.
        async with apis.ApiClient(timeout=self.settings.timeout) as client:
            associations = [a.lower() for a in self.args.association]
            members: list[dict] = []
            if "ncaa" in associations:
                members += await apis.ncaa_members(client, list(self.args.division))
            if "naia" in associations:
                members += await apis.naia_members(client, self.args.naia_pdf)
            if "njcaa" in associations:
                members += await apis.njcaa_members(client)
            members = [m for m in members if self._wanted(m)]

            limit = self.args.limit or 0
            if limit > 0 and len(members) > limit:
                log.info("%d schools matched, capping at --limit %d", len(members), limit)
                members = members[:limit]
            log.info("building %d school record(s)", len(members))

            if self.args.no_academics:
                for record in members:
                    yield _base_school(record, self.name)
                return

            if apis.scorecard_key() == apis.DEMO_KEY:
                log.warning(
                    "using the shared College Scorecard DEMO_KEY (~30 requests/hour). "
                    "For a full run, get a free key at https://api.data.gov/signup/ "
                    "and set SCRAPBOT_SCORECARD_KEY."
                )

            semaphore = asyncio.Semaphore(self.settings.concurrency)

            async def worker(record: dict) -> School:
                async with semaphore:
                    return await self._enrich(client, record)

            tasks = [asyncio.create_task(worker(m)) for m in members]
            try:
                for finished in asyncio.as_completed(tasks):
                    yield await finished
            finally:
                for task in tasks:
                    task.cancel()

    def _wanted(self, record: dict) -> bool:
        states = {usregions.state_code(s) for s in (self.args.state or [])}
        states.discard(None)
        if states and apis.ncaa_state(record) not in states:
            return False
        names = [n.lower() for n in (self.args.school or [])]
        if names:
            official = (record.get("nameOfficial") or "").lower()
            if not any(n in official for n in names):
                return False
        return True

    async def _enrich(self, client: apis.ApiClient, record: dict) -> School:
        school = _base_school(record, self.name)
        match = await apis.scorecard_lookup(
            client, record.get("nameOfficial") or "", apis.ncaa_state(record)
        )
        if match is None:
            school.notes.append("no College Scorecard match — academics and cost unfilled")
            return school

        school.city = (match.get("school.city") or "").strip() or school.city
        # NAIA members arrive with no website at all — Scorecard supplies it,
        # and the coaches source hops from there to the athletics site.
        school.website = (match.get("school.school_url") or "").strip() or school.website
        # Scorecard's state is authoritative over the NCAA's mailing address.
        school.state = usregions.state_name(match.get("school.state")) or school.state
        school.region = usregions.region_for(match.get("school.state")) or school.region
        school.totalYearlyCost = apis.total_yearly_cost(match)
        school.academicData = apis.academic_data(match)
        school.privatePublic = apis.private_public(match, record)

        if not school.academicData:
            # Two-year colleges are open-admission and so publish no admissions
            # test scores at all; four-year schools that report nothing are
            # usually test-optional. Different causes, so say which.
            school.notes.append(
                "no admissions test scores — open-admission two-year college"
                if school.association == "NJCAA"
                else "no test scores published (test-optional reporting)"
            )
        return school


def _base_school(record: dict, source: str) -> School:
    """Everything the member list alone can tell us (NCAA or NAIA)."""
    state = apis.ncaa_state(record)
    return School(
        school=(record.get("nameOfficial") or "").strip(),
        # NJCAA records carry a city; the NCAA directory does not.
        city=(record.get("city") or "").strip() or None,
        state=usregions.state_name(state),
        region=usregions.region_for(state),
        division=apis.ncaa_division(record),
        conference=(record.get("conferenceName") or "").strip() or None,
        privatePublic=apis.private_public(None, record),
        association=record.get("association") or "NCAA",
        athletics_domain=apis.ncaa_domain(record),
        website=(record.get("webSiteUrl") or "").strip() or None,
        ncaa_org_id=record.get("orgId"),
        source=source,
    )
