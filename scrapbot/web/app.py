"""FastAPI app backing the local dashboard.

Read-only: it reflects whatever ``data/leads.json`` currently holds, so a
scrape running in another terminal shows up on refresh.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

from .. import storage
from ..cli import filter_contacts, filter_leads
from ..config import Settings
from ..models import CONTACT_CSV_COLUMNS, CSV_COLUMNS, SCHOOL_COLUMNS

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="scrapbot", docs_url="/api/docs", redoc_url=None)

    def load():
        return storage.LeadStore(settings).load().sorted_leads()

    def load_contacts():
        return storage.ContactStore(settings).load().sorted_leads()

    def load_schools():
        return storage.SchoolStore(settings).load().sorted_leads()

    def _divisions(division: str | None) -> set[str] | None:
        if not division:
            return None
        return {
            d.upper() if d.upper().startswith("D") else f"D{d.upper()}"
            for d in division.split(",")
            if d.strip()
        }

    @app.get("/", response_class=HTMLResponse)
    def index():
        page = STATIC_DIR / "index.html"
        if not page.exists():  # pragma: no cover
            return HTMLResponse("<h1>dashboard assets missing</h1>", status_code=500)
        return FileResponse(page)

    @app.get("/api/leads")
    def api_leads(
        search: str | None = None,
        industry: str | None = None,
        has_email: bool = False,
        has_phone: bool = False,
        hiring: bool = False,
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
        window = leads[offset : offset + limit]
        return {
            "total": len(leads),
            "offset": offset,
            "limit": limit,
            "columns": CSV_COLUMNS,
            "leads": [lead.to_dict() for lead in window],
        }

    @app.get("/api/contacts")
    def api_contacts(
        search: str | None = None,
        sport: str | None = None,
        coaches_only: bool = False,
        direct_email: bool = False,
        has_email: bool = False,
        has_phone: bool = False,
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
        window = contacts[offset : offset + limit]
        return {
            "total": len(contacts),
            "offset": offset,
            "limit": limit,
            "columns": CONTACT_CSV_COLUMNS,
            "contacts": [c.to_dict() for c in window],
        }

    @app.get("/api/schools")
    def api_schools(
        search: str | None = None,
        division: str | None = None,
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
        window = schools[offset : offset + limit]
        return {
            "total": len(schools),
            "offset": offset,
            "limit": limit,
            "columns": SCHOOL_COLUMNS,
            "schools": [s.to_dict() for s in window],
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

        sports: dict[str, int] = {}
        for contact in contacts:
            for sport in (contact.sport or "").split(";"):
                sport = sport.strip()
                if sport:
                    sports[sport] = sports.get(sport, 0) + 1

        divisions: dict[str, int] = {}
        for school in schools:
            divisions[school.division or "unknown"] = (
                divisions.get(school.division or "unknown", 0) + 1
            )

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
                "sports": dict(sorted(sports.items(), key=lambda kv: -kv[1])),
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
            columns = CONTACT_CSV_COLUMNS
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
            writer.writerow(record.to_row())
        buffer.seek(0)
        return StreamingResponse(
            iter([buffer.getvalue()]),
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="scrapbot-{dataset}.csv"'
            },
        )

    return app
