"""FastAPI app backing the local dashboard.

Read-only: it reflects whatever ``data/leads.json`` currently holds, so a
scrape running in another terminal shows up on refresh.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from .. import models, storage
from ..cli import filter_contacts, filter_leads
from ..config import Settings
from ..models import (
    CONTACT_CSV_COLUMNS,
    CSV_COLUMNS,
    OUTCOME_LABELS,
    SCHOOL_COLUMNS,
    SiteOutcome,
)
from ..sources.website import normalize_domain
from .jobs import JobManager

STATIC_DIR = Path(__file__).resolve().parent / "static"

RETRYABLE_STATUSES = set(SiteOutcome.RETRYABLE)


class RetryRequest(BaseModel):
    run: str | None = None
    domains: list[str] | None = None


def _sort_key(value) -> str:
    """One comparable string per cell, so mixed column types never blow up.

    The dashboard sorts by whatever column the user clicked, and a column can
    hold a list (emails), a dict (academicData), a number or nothing at all.
    Numbers are zero-padded so they still order numerically as text.
    """
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    elif isinstance(value, dict):
        value = next(iter(value.values()), "")
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return str(int(value))
    if isinstance(value, (int, float)):
        return f"{value:030.6f}"
    return str(value).lower()


def _sorted(records: list, key, order: str) -> list:
    """:func:`_page`'s ordering, applied to records instead of dicts.

    Same contract — blanks sink to the bottom whichever way the sort points —
    but it never materialises a dict per record, so a store of 80,000 contacts
    can be sorted to return a page of 50 without serialising all of them.
    """
    keyed = [(_sort_key(key(record)), index, record) for index, record in enumerate(records)]
    filled = [k for k in keyed if k[0]]
    blank = [k for k in keyed if not k[0]]
    filled.sort(key=lambda k: k[0], reverse=order == "desc")
    return [k[2] for k in filled + blank]


def _page(rows: list[dict], sort: str | None, order: str, offset: int, limit: int) -> list[dict]:
    """Sort the whole result set, then hand back one window of it.

    Sorting has to happen server side: with pagination the browser only ever
    holds one page, so sorting there would only shuffle the visible rows.
    Blank cells sink to the bottom in both directions rather than clumping at
    whichever end the sort happens to point.
    """
    if sort:
        keyed = [(_sort_key(row.get(sort)), index, row) for index, row in enumerate(rows)]
        filled = [k for k in keyed if k[0]]
        blank = [k for k in keyed if not k[0]]
        filled.sort(key=lambda k: k[0], reverse=order == "desc")
        rows = [k[2] for k in filled + blank]
    return rows[offset : offset + limit]


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="scrapbot", docs_url="/api/docs", redoc_url=None)
    jobs = JobManager(settings)

    # Every endpoint re-read its whole store from disk on every request. At
    # 80,000 contacts that is a 51 MB JSON parse per call — about two seconds —
    # so changing a filter left the table showing the old rows for long enough
    # to read as "the filter does nothing". The dashboard is a reader; the files
    # only change when a run writes them, so the parse belongs behind a cache.
    #
    # Keyed on the file's modification time rather than a timer, so a scrape
    # that finishes while the page is open is picked up on the next request and
    # a file that has not changed is never parsed twice. st_mtime_ns, not
    # st_mtime: a run writing twice inside one filesystem tick would otherwise
    # look unchanged.
    cache: dict[str, tuple[int, int, list]] = {}

    def _load_cached(name: str, store_cls):
        store = store_cls(settings)
        path = store.json_path
        try:
            stat = path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            stamp = (0, 0)
        hit = cache.get(name)
        if hit is not None and hit[:2] == stamp:
            return hit[2]
        records = store.load().sorted_leads()
        cache[name] = (*stamp, records)
        return records

    def load():
        return _load_cached("leads", storage.LeadStore)

    def load_contacts():
        return _load_cached("contacts", storage.ContactStore)

    def load_schools():
        return _load_cached("schools", storage.SchoolStore)

    def _division_by_host() -> dict[str, str]:
        """``goduke.com -> "DI"``, from the school store.

        A contact has no division of its own — it belongs to the institution,
        and ``School.athletics_domain`` *is* ``Contact.school_domain``. Joining
        here rather than copying the value onto every contact keeps one source
        of truth: re-run ``scrapbot run schools`` and the coaches tab follows,
        with no backfill and no rows left holding last season's tier.
        """
        out: dict[str, str] = {}
        for school in load_schools():
            if not school.division:
                continue
            for host in (school.athletics_domain, normalize_domain(school.website or "")):
                if host:
                    out.setdefault(host, school.division)
        return out

    def _divisions(division: str | None) -> set[str] | None:
        if not division:
            return None
        return {
            models.normalize_division(d) for d in division.split(",") if d.strip()
        }

    @app.get("/", response_class=HTMLResponse)
    def index():
        page = STATIC_DIR / "index.html"
        if not page.exists():  # pragma: no cover
            return HTMLResponse("<h1>dashboard assets missing</h1>", status_code=500)
        # The whole dashboard — markup, styles and script — is this one file, so
        # a cached copy is a cached *application*. FileResponse sends
        # Last-Modified and an ETag but no Cache-Control, and a browser given
        # that combination is free to reuse the page without asking. The result
        # is that a change ships, the operator reloads, and the old UI comes
        # back: a filter added here simply does not appear, with nothing on
        # screen to explain why.
        #
        # "no-cache" is revalidate-every-time, not don't-store. FileResponse
        # does not answer If-None-Match, so revalidation costs a full 27 KB
        # rather than a 304 — on a dashboard bound to localhost that is a fair
        # price for never serving a stale application.
        return FileResponse(page, headers={"Cache-Control": "no-cache"})

    @app.get("/api/leads")
    def api_leads(
        search: str | None = None,
        industry: str | None = None,
        has_email: bool = False,
        has_phone: bool = False,
        hiring: bool = False,
        sort: str | None = None,
        order: str = Query("asc", pattern="^(asc|desc)$"),
        limit: int = Query(500, ge=1, le=10_000),
        offset: int = Query(0, ge=0),
    ):
        leads = filter_leads(
            load(),
            has_email=has_email,
            has_phone=has_phone,
            industry=industry,
            search=search,
        )
        if hiring:
            leads = [lead for lead in leads if lead.has_open_roles]
        rows = [lead.to_dict() for lead in leads]
        return {
            "total": len(rows),
            "offset": offset,
            "limit": limit,
            "columns": CSV_COLUMNS,
            "leads": _page(rows, sort, order, offset, limit),
        }

    @app.get("/api/contacts")
    def api_contacts(
        search: str | None = None,
        sport: str | None = None,
        division: str | None = None,
        coaches_only: bool = False,
        direct_email: bool = False,
        has_email: bool = False,
        has_phone: bool = False,
        sort: str | None = None,
        order: str = Query("asc", pattern="^(asc|desc)$"),
        limit: int = Query(500, ge=1, le=10_000),
        offset: int = Query(0, ge=0),
    ):
        contacts = filter_contacts(
            load_contacts(),
            has_email=has_email,
            has_phone=has_phone,
            sport=sport,
            coaches_only=coaches_only,
            direct_email=direct_email,
            search=search,
        )
        by_host = _division_by_host()
        wanted = _divisions(division)
        if wanted:
            contacts = [c for c in contacts if by_host.get(c.school_domain) in wanted]

        # Sort and slice the records themselves, and only build dicts for the
        # page being returned. Serialising all 80,000 to hand back 50 cost most
        # of a second per request, which is the difference between a filter that
        # responds and one that looks broken.
        total = len(contacts)
        if sort == "division":
            contacts = _sorted(contacts, lambda c: by_host.get(c.school_domain), order)
        elif sort:
            contacts = _sorted(contacts, lambda c: getattr(c, sort, None), order)
        window = contacts[offset : offset + limit]
        return {
            "total": total,
            "offset": offset,
            "limit": limit,
            # Division is joined from the school store, not stored on a contact,
            # so it is appended here rather than added to the CSV schema.
            "columns": CONTACT_CSV_COLUMNS + ["division"],
            "contacts": [
                {**c.to_dict(), "division": by_host.get(c.school_domain)} for c in window
            ],
        }

    @app.get("/api/schools")
    def api_schools(
        search: str | None = None,
        division: str | None = None,
        sort: str | None = None,
        order: str = Query("asc", pattern="^(asc|desc)$"),
        limit: int = Query(500, ge=1, le=10_000),
        offset: int = Query(0, ge=0),
    ):
        schools = load_schools()
        wanted = _divisions(division)
        if wanted:
            schools = [s for s in schools if s.division in wanted]
        if search:
            needle = search.lower()
            schools = [s for s in schools if needle in json.dumps(s.to_dict()).lower()]
        rows = [s.to_dict() for s in schools]
        return {
            "total": len(rows),
            "offset": offset,
            "limit": limit,
            "columns": SCHOOL_COLUMNS,
            "schools": _page(rows, sort, order, offset, limit),
        }

    @app.get("/api/stats")
    def api_stats():
        leads = load()
        contacts = load_contacts()
        schools = load_schools()

        industries: dict[str, int] = {}
        for lead in leads:
            for hint in lead.industry_hints:
                industries[hint] = industries.get(hint, 0) + 1

        # Canonical labels, grouped so each sport's gender variants sit
        # together. The raw values run to 2,895 entries, most of them scraped
        # section headings with a phone number or an administrator's name
        # attached, which made the filter unusable.
        from ..sports import options as sport_options

        sports = sport_options(c.sport for c in contacts)

        divisions: dict[str, int] = {}
        for school in schools:
            if school.division:
                divisions[school.division] = divisions.get(school.division, 0) + 1

        return {
            "total": len(leads),
            "with_email": sum(1 for lead in leads if lead.emails),
            "with_phone": sum(1 for lead in leads if lead.phones),
            "with_linkedin": sum(1 for lead in leads if "linkedin" in lead.socials),
            "hiring": sum(1 for lead in leads if lead.has_open_roles),
            "industries": dict(sorted(industries.items(), key=lambda kv: -kv[1])),
            "contacts": {
                "total": len(contacts),
                "coaches": sum(1 for c in contacts if c.is_coach),
                "with_email": sum(1 for c in contacts if c.emails),
                "with_phone": sum(1 for c in contacts if c.phones),
                "schools": len({c.school_domain for c in contacts}),
                # Already ordered by sport_options: groups by size, variants
                # adjacent. Re-sorting by count here would scatter them again.
                "sports": sports,
            },
            "schools": {
                "total": len(schools),
                "with_academics": sum(1 for s in schools if s.academicData),
                "with_cost": sum(1 for s in schools if s.totalYearlyCost),
                "with_site": sum(1 for s in schools if s.athletics_domain),
                "divisions": dict(sorted(divisions.items())),
            },
            "data_dir": str(settings.data_dir),
            "runs": sorted(
                (p.name for p in settings.runs_dir.glob("*") if p.is_dir()), reverse=True
            )[:20],
        }

    def _run_dirs() -> list[Path]:
        if not settings.runs_dir.exists():
            return []
        return sorted(
            (p for p in settings.runs_dir.iterdir() if p.is_dir()),
            key=lambda p: p.name,
            reverse=True,
        )

    def _read_json(path: Path):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    @app.get("/api/runs")
    def api_runs(limit: int = Query(50, ge=1, le=500)):
        """Every run, newest first, with its per-site tally."""
        runs = []
        for directory in _run_dirs()[:limit]:
            meta = _read_json(directory / "meta.json") or {}
            runs.append(
                {
                    "run_id": directory.name,
                    "source": meta.get("source"),
                    "records": meta.get("leads"),
                    "new": meta.get("new"),
                    "updated": meta.get("updated"),
                    "seconds": meta.get("seconds"),
                    "sites": meta.get("sites"),
                    "has_report": (directory / "sites.json").exists(),
                }
            )
        return {"total": len(runs), "runs": runs}

    @app.get("/api/sites")
    def api_sites(
        run: str | None = None,
        status: str | None = None,
        search: str | None = None,
        sort: str | None = None,
        order: str = Query("asc", pattern="^(asc|desc)$"),
        limit: int = Query(1000, ge=1, le=10_000),
        offset: int = Query(0, ge=0),
    ):
        """Per-site outcomes. Defaults to the most recent run that has a report."""
        directories = _run_dirs()
        if run and run != "all":
            if "/" in run or "\\" in run or run.startswith("."):
                return {"error": "bad run id", "sites": [], "total": 0}
            directories = [d for d in directories if d.name == run]
        elif not run:
            directories = [d for d in directories if (d / "sites.json").exists()][:1]

        sites: list[dict] = []
        for directory in directories:
            for entry in _read_json(directory / "sites.json") or []:
                sites.append({**entry, "run_id": directory.name})

        if status and status != "all":
            sites = [s for s in sites if s.get("status") == status]
        if search:
            needle = search.lower()
            sites = [s for s in sites if needle in json.dumps(s).lower()]

        counts: dict[str, int] = {}
        for entry in sites:
            key = entry.get("status") or "unknown"
            counts[key] = counts.get(key, 0) + 1

        return {
            "total": len(sites),
            "offset": offset,
            "limit": limit,
            "run": directories[0].name if len(directories) == 1 else "all",
            "counts": counts,
            "labels": OUTCOME_LABELS,
            "sites": _page(sites, sort, order, offset, limit),
        }

    # Async on purpose: a sync endpoint runs in a worker thread, where there is
    # no running event loop for the job to attach to.
    @app.post("/api/retry")
    async def api_retry(payload: RetryRequest):
        """Re-scrape sites that failed, from a run's own report.

        Domains must already appear in a stored report with a retryable
        status. The dashboard therefore cannot be used to point the scraper at
        an arbitrary host, which matters if it is ever bound to more than
        localhost.
        """
        if jobs.busy:
            return JSONResponse(
                {"error": "a retry is already running"}, status_code=409
            )

        directories = _run_dirs()
        if payload.run and payload.run != "all":
            if "/" in payload.run or "\\" in payload.run or payload.run.startswith("."):
                return JSONResponse({"error": "bad run id"}, status_code=400)
            directories = [d for d in directories if d.name == payload.run]

        allowed: dict[str, str] = {}
        for directory in directories:
            for entry in _read_json(directory / "sites.json") or []:
                if entry.get("status") in RETRYABLE_STATUSES:
                    allowed[entry.get("domain", "")] = entry.get("status", "")
        allowed.pop("", None)

        wanted = [d.strip() for d in (payload.domains or list(allowed)) if d and d.strip()]
        domains = [d for d in dict.fromkeys(wanted) if d in allowed]
        rejected = [d for d in dict.fromkeys(wanted) if d not in allowed]

        if not domains:
            return JSONResponse(
                {
                    "error": "nothing to retry",
                    "detail": "domains must appear in a run report with a "
                    "retryable outcome (blocked, network or error)",
                    "rejected": rejected,
                },
                status_code=400,
            )

        job = jobs.start(domains)
        return {"job": job.to_dict(), "rejected": rejected}

    @app.get("/api/jobs")
    def api_jobs():
        return {"busy": jobs.busy, "jobs": [j.to_dict() for j in jobs.recent()]}

    @app.get("/api/jobs/{job_id}")
    def api_job(job_id: str):
        job = jobs.get(job_id)
        if job is None:
            return JSONResponse({"error": "unknown job"}, status_code=404)
        return job.to_dict()

    @app.get("/api/runs/{run_id}")
    def api_run(run_id: str):
        # Guard against path traversal — run ids are flat directory names.
        if "/" in run_id or "\\" in run_id or run_id.startswith("."):
            return {"error": "bad run id"}
        meta = settings.runs_dir / run_id / "meta.json"
        if not meta.exists():
            return {"error": "unknown run"}
        return json.loads(meta.read_text(encoding="utf-8"))

    @app.get("/api/export.csv")
    def api_export(
        dataset: str = "leads",
        search: str | None = None,
        industry: str | None = None,
        sport: str | None = None,
        division: str | None = None,
        coaches_only: bool = False,
        direct_email: bool = False,
        has_email: bool = False,
        has_phone: bool = False,
        hiring: bool = False,
    ):
        extra: dict = {}
        if dataset == "contacts":
            records = filter_contacts(
                load_contacts(),
                has_email=has_email,
                has_phone=has_phone,
                sport=sport,
                coaches_only=coaches_only,
                direct_email=direct_email,
                search=search,
            )
            # The division filter has to apply to the download too, or the
            # button quietly hands back more than the table is showing.
            by_host = _division_by_host()
            wanted = _divisions(division)
            if wanted:
                records = [c for c in records if by_host.get(c.school_domain) in wanted]
            # ...and the column has to come with it. A CSV that omits what the
            # table shows is the export not matching the screen it came from.
            columns = CONTACT_CSV_COLUMNS + ["division"]
            extra = {"division": lambda c: by_host.get(c.school_domain) or ""}
        elif dataset == "schools":
            records = load_schools()
            wanted = _divisions(division)
            if wanted:
                records = [s for s in records if s.division in wanted]
            if search:
                needle = search.lower()
                records = [s for s in records if needle in json.dumps(s.to_dict()).lower()]
            columns = SCHOOL_COLUMNS
        else:
            dataset = "leads"
            records = filter_leads(
                load(),
                has_email=has_email,
                has_phone=has_phone,
                industry=industry,
                search=search,
            )
            if hiring:
                records = [lead for lead in records if lead.has_open_roles]
            columns = CSV_COLUMNS

        buffer = io.StringIO()
        import csv as _csv

        writer = _csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for record in records:
            row = record.to_row()
            # Columns joined from another store rather than held on the record.
            row.update({name: get(record) for name, get in extra.items()})
            writer.writerow(row)
        buffer.seek(0)
        return StreamingResponse(
            iter([buffer.getvalue()]),
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="scrapbot-{dataset}.csv"'
            },
        )

    return app
