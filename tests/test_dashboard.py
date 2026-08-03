"""The dashboard serves all three stores, each with its own columns."""

from __future__ import annotations

import csv
import io

import pytest
from fastapi.testclient import TestClient

from scrapbot import storage
from scrapbot.config import Settings
from scrapbot.models import CSV_COLUMNS, SCHOOL_COLUMNS, Contact, Lead, School
from scrapbot.web.app import create_app


@pytest.fixture
def client(tmp_path):
    settings = Settings()
    settings.data_dir = tmp_path

    contacts = storage.ContactStore(settings)
    contacts.upsert(Contact(name="Chris Vance", school_domain="troytrojans.com", school="Troy",
                            title="Head Coach", sport="Men's Basketball", is_coach=True,
                            emails=["chris.vance@state.edu"], phones=["+12315550102"]))
    contacts.upsert(Contact(name="Sam Webb", school_domain="state.edu", school="State",
                            title="Equipment Manager", sport="Men's Basketball"))
    contacts.save()

    schools = storage.SchoolStore(settings)
    schools.upsert(School(school="Troy University", ncaa_org_id=674, division="DI",
                          conference="Sun Belt", state="Alabama", city="Troy",
                          totalYearlyCost="$23,165/$33,341",
                          academicData={"SATMath": "450-590"},
                          athletics_domain="troytrojans.com"))
    schools.upsert(School(school="Baker University", division="NAIA", state="Kansas",
                          conference="Heart of America Athletic Conference"))
    schools.save()

    leads = storage.LeadStore(settings)
    leads.upsert(Lead(domain="acme.com", company_name="Acme", emails=["info@acme.com"]))
    leads.save()

    return TestClient(create_app(settings))


def test_stats_covers_all_three_stores(client):
    stats = client.get("/api/stats").json()
    assert stats["contacts"]["total"] == 2
    assert stats["contacts"]["coaches"] == 1
    assert stats["contacts"]["sports"] == {"Men's Basketball": 2}
    assert stats["schools"]["total"] == 2
    assert stats["schools"]["divisions"] == {"DI": 1, "NAIA": 1}
    assert stats["total"] == 1  # leads


def test_contacts_endpoint_filters(client):
    body = client.get("/api/contacts").json()
    assert body["total"] == 2

    only_coaches = client.get("/api/contacts", params={"coaches_only": True}).json()
    assert [c["name"] for c in only_coaches["contacts"]] == ["Chris Vance"]

    by_email = client.get("/api/contacts", params={"has_email": True}).json()
    assert by_email["total"] == 1

    by_search = client.get("/api/contacts", params={"search": "equipment"}).json()
    assert [c["name"] for c in by_search["contacts"]] == ["Sam Webb"]


def test_schools_endpoint_filters(client):
    body = client.get("/api/schools").json()
    assert body["total"] == 2
    assert {s["conference"] for s in body["schools"]} == {
        "Sun Belt", "Heart of America Athletic Conference"
    }

    assert client.get("/api/schools", params={"division": "DI"}).json()["total"] == 1
    assert client.get("/api/schools", params={"division": "III"}).json()["total"] == 0
    # Bare numerals work too, so "I,II" from the UI behaves.
    assert client.get("/api/schools", params={"division": "I"}).json()["total"] == 1


def test_naia_division_filter(client):
    """NAIA must not be turned into 'DNAIA', which matched nothing."""
    assert client.get("/api/schools", params={"division": "NAIA"}).json()["total"] == 1
    assert client.get("/api/schools", params={"division": "naia"}).json()["total"] == 1
    assert client.get("/api/schools", params={"division": "DI"}).json()["total"] == 1


def test_contacts_endpoint_sorts_and_pages(tmp_path):
    """Sorting is server side, so it orders the whole set — not one page."""
    settings = Settings()
    settings.data_dir = tmp_path
    store = storage.ContactStore(settings)
    for i in range(5):
        store.upsert(Contact(name=f"Coach {i}", school_domain=f"s{i}.edu", title="Head Coach"))
    store.save()
    client = TestClient(create_app(settings))

    def names(**params):
        return [c["name"] for c in client.get("/api/contacts", params=params).json()["contacts"]]

    # Page 2 of a descending sort holds rows 3-4 of the reversed order, which
    # only works if the sort ran before the window was cut.
    assert names(sort="name", order="desc", limit=2) == ["Coach 4", "Coach 3"]
    assert names(sort="name", order="desc", limit=2, offset=2) == ["Coach 2", "Coach 1"]
    assert names(sort="name", order="asc", limit=2) == ["Coach 0", "Coach 1"]

    body = client.get("/api/contacts", params={"limit": 2, "offset": 2}).json()
    assert body["total"] == 5      # the full match count, for the page count
    assert body["limit"] == 2 and body["offset"] == 2
    assert len(body["contacts"]) == 2


def test_sorting_sinks_blank_cells_to_the_bottom(client):
    """An empty column must not crowd out real values at either end."""
    for order in ("asc", "desc"):
        body = client.get("/api/contacts", params={"sort": "emails", "order": order}).json()
        assert [bool(c["emails"]) for c in body["contacts"]] == [True, False], order


def test_sites_endpoint_pages(tmp_path):
    from scrapbot.models import SiteOutcome
    settings = Settings()
    settings.data_dir = tmp_path
    _write_report(settings, "20260202T000000Z", [
        SiteOutcome(f"s{i}.edu", SiteOutcome.OK, "fine") for i in range(4)
    ])
    client = TestClient(create_app(settings))

    body = client.get("/api/sites", params={"limit": 2, "offset": 2, "sort": "domain"}).json()
    assert body["total"] == 4                       # counts every match
    assert body["counts"] == {"ok": 4}              # tallies are set-wide too
    assert [s["domain"] for s in body["sites"]] == ["s2.edu", "s3.edu"]


def test_csv_export_uses_the_right_columns_per_dataset(client):
    def header(dataset):
        resp = client.get("/api/export.csv", params={"dataset": dataset})
        assert resp.status_code == 200
        assert dataset in resp.headers["content-disposition"]
        return next(csv.reader(io.StringIO(resp.text)))

    assert header("contacts")[:3] == ["name", "title", "sport"]
    assert header("schools")[:3] == ["school", "city", "state"]
    assert header("leads")[:2] == ["domain", "company_name"]


def test_csv_export_honours_filters(client):
    resp = client.get("/api/export.csv", params={"dataset": "contacts", "coaches_only": True})
    body = list(csv.DictReader(io.StringIO(resp.text)))
    assert [r["name"] for r in body] == ["Chris Vance"]


def test_unknown_dataset_falls_back_to_leads(client):
    resp = client.get("/api/export.csv", params={"dataset": "nonsense"})
    assert next(csv.reader(io.StringIO(resp.text)))[0] == "domain"


def _write_report(settings, run_id, outcomes, meta=None):
    from scrapbot import storage
    directory = settings.runs_dir / run_id
    directory.mkdir(parents=True, exist_ok=True)
    storage.write_json(directory / "meta.json", meta or {"source": "coaches"})
    storage.write_outcomes(directory, outcomes)
    return directory


def test_sites_endpoint_defaults_to_the_latest_report(tmp_path):
    from scrapbot.models import SiteOutcome
    settings = Settings()
    settings.data_dir = tmp_path

    _write_report(settings, "20260101T000000Z", [
        SiteOutcome("old.edu", SiteOutcome.OK, "fine", people=5),
    ])
    _write_report(settings, "20260202T000000Z", [
        SiteOutcome("good.edu", SiteOutcome.OK, "fine", people=9),
        SiteOutcome("blocked.edu", SiteOutcome.BLOCKED, "HTTP 403"),
        SiteOutcome("down.edu", SiteOutcome.NETWORK, "timed out"),
    ])
    client = TestClient(create_app(settings))

    body = client.get("/api/sites").json()
    assert body["run"] == "20260202T000000Z"   # newest, not the first on disk
    assert body["total"] == 3
    assert body["counts"] == {"ok": 1, "blocked": 1, "network": 1}
    assert "blocked" in body["labels"]


def test_sites_endpoint_filters_by_status_and_run(tmp_path):
    from scrapbot.models import SiteOutcome
    settings = Settings()
    settings.data_dir = tmp_path
    _write_report(settings, "20260202T000000Z", [
        SiteOutcome("good.edu", SiteOutcome.OK, "fine", people=9),
        SiteOutcome("blocked.edu", SiteOutcome.BLOCKED, "HTTP 403"),
    ])
    _write_report(settings, "20260101T000000Z", [
        SiteOutcome("old.edu", SiteOutcome.OK, "fine", people=5),
    ])
    client = TestClient(create_app(settings))

    only_blocked = client.get("/api/sites", params={"status": "blocked"}).json()
    assert [s["domain"] for s in only_blocked["sites"]] == ["blocked.edu"]

    every = client.get("/api/sites", params={"run": "all"}).json()
    assert every["total"] == 3
    assert {s["run_id"] for s in every["sites"]} == {
        "20260101T000000Z", "20260202T000000Z"
    }

    one = client.get("/api/sites", params={"run": "20260101T000000Z"}).json()
    assert [s["domain"] for s in one["sites"]] == ["old.edu"]


def test_sites_endpoint_rejects_path_traversal(tmp_path):
    settings = Settings()
    settings.data_dir = tmp_path
    client = TestClient(create_app(settings))
    body = client.get("/api/sites", params={"run": "../../etc"}).json()
    assert body["sites"] == [] and "error" in body


def test_runs_endpoint_lists_newest_first_with_tallies(tmp_path):
    from scrapbot.models import SiteOutcome
    settings = Settings()
    settings.data_dir = tmp_path
    _write_report(settings, "20260101T000000Z", [SiteOutcome("a.edu", SiteOutcome.OK, "")],
                  meta={"source": "coaches", "sites": {"attempted": 1, "succeeded": 1}})
    _write_report(settings, "20260202T000000Z", [SiteOutcome("b.edu", SiteOutcome.BLOCKED, "")],
                  meta={"source": "coaches", "sites": {"attempted": 1, "succeeded": 0}})
    client = TestClient(create_app(settings))

    runs = client.get("/api/runs").json()["runs"]
    assert [r["run_id"] for r in runs] == ["20260202T000000Z", "20260101T000000Z"]
    assert all(r["has_report"] for r in runs)
    assert runs[0]["sites"]["succeeded"] == 0


def test_runs_without_a_report_are_marked(tmp_path):
    settings = Settings()
    settings.data_dir = tmp_path
    (settings.runs_dir / "20250101T000000Z").mkdir(parents=True)
    client = TestClient(create_app(settings))
    runs = client.get("/api/runs").json()["runs"]
    assert runs[0]["has_report"] is False
    # And asking for sites yields nothing rather than erroring.
    assert client.get("/api/sites").json()["sites"] == []


def _retry_client(tmp_path):
    from scrapbot.models import SiteOutcome
    settings = Settings()
    settings.data_dir = tmp_path
    _write_report(settings, "20260202T000000Z", [
        SiteOutcome("good.edu", SiteOutcome.OK, "fine", people=9),
        SiteOutcome("blocked.edu", SiteOutcome.BLOCKED, "HTTP 403"),
        SiteOutcome("down.edu", SiteOutcome.NETWORK, "timed out"),
        SiteOutcome("nothing.edu", SiteOutcome.NO_DIRECTORY, "no directory"),
    ])
    return TestClient(create_app(settings))


def test_retry_refuses_a_domain_that_is_not_in_any_report(tmp_path):
    """The dashboard must not be usable to point the scraper anywhere."""
    client = _retry_client(tmp_path)
    resp = client.post("/api/retry", json={"domains": ["evil.example.com"]})
    assert resp.status_code == 400
    assert resp.json()["rejected"] == ["evil.example.com"]


def test_retry_refuses_outcomes_that_are_not_retryable(tmp_path):
    client = _retry_client(tmp_path)
    # Succeeded and no_directory are both pointless to retry.
    for domain in ("good.edu", "nothing.edu"):
        resp = client.post("/api/retry", json={"domains": [domain]})
        assert resp.status_code == 400, domain


def test_retry_rejects_a_bad_run_id(tmp_path):
    client = _retry_client(tmp_path)
    resp = client.post("/api/retry", json={"run": "../../etc", "domains": ["blocked.edu"]})
    assert resp.status_code == 400
    assert resp.json()["error"] == "bad run id"


def test_retry_starts_a_job_for_retryable_domains(tmp_path, monkeypatch):
    import scrapbot.web.jobs as jobs_module

    seen = {}

    async def fake_run_source(source, args, settings, **kwargs):
        seen["source"] = source
        seen["sites"] = list(args.sites)
        from scrapbot.models import SiteOutcome
        from scrapbot.runner import RunResult
        return RunResult(
            run_id="20260303T000000Z", source=source, leads=[],
            outcomes=[SiteOutcome("blocked.edu", SiteOutcome.OK, "recovered", people=3)],
        )

    monkeypatch.setattr(jobs_module, "run_source", fake_run_source)
    client = _retry_client(tmp_path)

    resp = client.post("/api/retry", json={"domains": ["blocked.edu"]})
    assert resp.status_code == 200
    job_id = resp.json()["job"]["job_id"]

    # TestClient runs the loop per request, so the task completes by the next call.
    for _ in range(20):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] != "running":
            break
    assert job["status"] == "done"
    assert job["succeeded"] == 1
    assert seen["source"] == "coaches"
    assert seen["sites"] == ["blocked.edu"]


def test_retry_with_no_domains_takes_every_retryable_site(tmp_path, monkeypatch):
    import scrapbot.web.jobs as jobs_module

    captured = {}

    async def fake_run_source(source, args, settings, **kwargs):
        captured["sites"] = sorted(args.sites)
        from scrapbot.runner import RunResult
        return RunResult(run_id="r", source=source)

    monkeypatch.setattr(jobs_module, "run_source", fake_run_source)
    client = _retry_client(tmp_path)

    resp = client.post("/api/retry", json={})
    assert resp.status_code == 200
    assert sorted(resp.json()["job"]["domains"]) == ["blocked.edu", "down.edu"]


def test_unknown_job_is_a_404(tmp_path):
    client = _retry_client(tmp_path)
    assert client.get("/api/jobs/nope").status_code == 404


def test_empty_stores_do_not_error(tmp_path):
    settings = Settings()
    settings.data_dir = tmp_path
    empty = TestClient(create_app(settings))
    assert empty.get("/api/stats").json()["contacts"]["total"] == 0
    assert empty.get("/api/contacts").json()["contacts"] == []
    assert empty.get("/api/schools").json()["schools"] == []


def test_a_contact_carries_the_division_of_their_school(client):
    """A contact has no division of its own — it belongs to the institution.

    School.athletics_domain *is* Contact.school_domain, so the coaches tab
    joins on it rather than copying the tier onto every person. Re-running
    `scrapbot run schools` then moves the whole tab at once, and nobody is left
    holding last season's division.
    """
    body = client.get("/api/contacts").json()
    assert "division" in body["columns"]
    by_name = {c["name"]: c for c in body["contacts"]}
    assert by_name["Chris Vance"]["division"] == "DI"   # troytrojans.com is on record
    assert by_name["Sam Webb"]["division"] is None      # state.edu is not


def test_contacts_can_be_filtered_by_division(client):
    assert client.get("/api/contacts?division=DI").json()["total"] == 1
    assert client.get("/api/contacts?division=DIII").json()["total"] == 0
    # Accepts the same spellings the schools tab does.
    assert client.get("/api/contacts?division=I").json()["total"] == 1
    # ...and combines with the other filters rather than replacing them.
    assert client.get("/api/contacts?division=DI&coaches_only=true").json()["total"] == 1
    assert client.get(
        "/api/contacts?division=DI&sport=volleyball"
    ).json()["total"] == 0


def test_the_csv_download_honours_the_division_filter(client):
    """Otherwise the button quietly hands back more than the table shows."""
    body = client.get("/api/export.csv?dataset=contacts&division=DI").text
    assert "Chris Vance" in body
    assert "Sam Webb" not in body


def test_the_contacts_csv_carries_the_division_column(client):
    """The export has to match the screen it came from.

    Division is joined from the school store rather than held on a contact, so
    it has to be added to the CSV explicitly — record.to_row() cannot know it.
    """
    lines = client.get("/api/export.csv?dataset=contacts").text.splitlines()
    assert lines[0].strip().split(",")[-1] == "division"
    rows = {line.split(",")[0]: line.rstrip().split(",")[-1] for line in lines[1:]}
    assert rows["Chris Vance"] == "DI"   # troytrojans.com is on record
    assert rows["Sam Webb"] == ""        # state.edu is not — blank, not missing


def test_the_other_exports_are_unaffected_by_the_joined_column(client):
    """Only contacts gain it; leads and schools keep their own schema."""
    for dataset, expected in (("leads", CSV_COLUMNS), ("schools", SCHOOL_COLUMNS)):
        header = client.get(f"/api/export.csv?dataset={dataset}").text.splitlines()[0]
        assert header.strip().split(",") == list(expected)
