"""The dashboard serves all three stores, each with its own columns."""

from __future__ import annotations

import csv
import io

import pytest
from fastapi.testclient import TestClient

from scrapbot import storage
from scrapbot.config import Settings
from scrapbot.models import Contact, Lead, School
from scrapbot.web.app import create_app


@pytest.fixture
def client(tmp_path):
    settings = Settings()
    settings.data_dir = tmp_path

    contacts = storage.ContactStore(settings)
    contacts.upsert(Contact(name="Chris Vance", school_domain="state.edu", school="State",
                            title="Head Coach", sport="Men's Basketball", is_coach=True,
                            emails=["chris.vance@state.edu"], phones=["+15551110002"]))
    contacts.upsert(Contact(name="Sam Webb", school_domain="state.edu", school="State",
                            title="Equipment Manager", sport="Men's Basketball"))
    contacts.save()

    schools = storage.SchoolStore(settings)
    schools.upsert(School(school="Troy University", ncaa_org_id=674, division="DI",
                          conference="Sun Belt", state="Alabama", city="Troy",
                          totalYearlyCost="$23,165/$33,341",
                          academicData={"SATMath": "450-590"},
                          athletics_domain="troytrojans.com"))
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
    assert stats["schools"]["total"] == 1
    assert stats["schools"]["divisions"] == {"DI": 1}
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
    assert body["total"] == 1
    assert body["schools"][0]["conference"] == "Sun Belt"

    assert client.get("/api/schools", params={"division": "DI"}).json()["total"] == 1
    assert client.get("/api/schools", params={"division": "III"}).json()["total"] == 0
    # Bare numerals work too, so "I,II" from the UI behaves.
    assert client.get("/api/schools", params={"division": "I"}).json()["total"] == 1


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


def test_empty_stores_do_not_error(tmp_path):
    settings = Settings()
    settings.data_dir = tmp_path
    empty = TestClient(create_app(settings))
    assert empty.get("/api/stats").json()["contacts"]["total"] == 0
    assert empty.get("/api/contacts").json()["contacts"] == []
    assert empty.get("/api/schools").json()["schools"] == []
