"""School records: region derivation, API shaping, and the origin schema."""

from __future__ import annotations

import json

from scrapbot import apis, models, storage, usregions
from scrapbot.cli import build_parser, settings_from_args
from scrapbot.models import School
from scrapbot.sources.schools import _base_school


# --- region is derived, not scraped --------------------------------------

def test_region_matches_the_origin_databases_wording():
    assert usregions.region_for("NJ") == "Northeast (Middle Atlantic)"
    assert usregions.region_for("New Jersey") == "Northeast (Middle Atlantic)"
    assert usregions.region_for("AL") == "South (East South Central)"
    assert usregions.state_name("AL") == "Alabama"
    assert usregions.region_for("ZZ") is None
    assert usregions.region_for(None) is None


def test_every_state_has_a_region():
    for code in usregions.STATE_NAMES:
        assert usregions.region_for(code), code


# --- API shaping ---------------------------------------------------------

def test_private_public_mapping_matches_the_origin_strings():
    assert apis.private_public({"school.ownership": 1}) == "Public"
    assert apis.private_public({"school.ownership": 2}) == "Private (not-for-profit)"
    assert apis.private_public({"school.ownership": 3}) == "Private (for-profit)"
    # Falls back to the NCAA flag when Scorecard has no match.
    assert apis.private_public(None, {"privateFlag": "Y"}) == "Private (not-for-profit)"
    assert apis.private_public(None, {"privateFlag": "N"}) == "Public"
    assert apis.private_public(None, None) is None


def test_public_cost_is_a_pair_and_private_is_single():
    public = {
        "school.ownership": 1,
        "latest.cost.attendance.academic_year": 23165,
        "latest.cost.tuition.in_state": 10176,
        "latest.cost.tuition.out_of_state": 20352,
    }
    assert apis.total_yearly_cost(public) == "$23,165/$33,341"

    private = {
        "school.ownership": 2,
        "latest.cost.attendance.academic_year": 54204,
        "latest.cost.tuition.in_state": 38004,
        "latest.cost.tuition.out_of_state": 38004,
    }
    assert apis.total_yearly_cost(private) == "$54,204"
    assert apis.total_yearly_cost(None) is None
    assert apis.total_yearly_cost({"latest.cost.attendance.academic_year": None}) is None


def test_academic_data_omits_fields_with_no_published_scores():
    result = {
        "latest.admissions.sat_scores.25th_percentile.math": 450,
        "latest.admissions.sat_scores.75th_percentile.math": 590,
        "latest.admissions.act_scores.25th_percentile.cumulative": None,
        "latest.admissions.act_scores.75th_percentile.cumulative": None,
    }
    assert apis.academic_data(result) == {"SATMath": "450-590"}
    assert apis.academic_data(None) == {}


def test_ncaa_helpers():
    record = {
        "divisionRoman": "III",
        "division": 3,
        "athleticWebUrl": "www.fdudevils.com",
        "memberOrgAddress": {"state": "NJ"},
    }
    assert apis.ncaa_division(record) == "DIII"
    assert apis.ncaa_domain(record) == "fdudevils.com"
    assert apis.ncaa_state(record) == "NJ"
    assert apis.ncaa_domain({"athleticWebUrl": "https://goduke.com/"}) == "goduke.com"
    assert apis.ncaa_domain({}) is None


# --- the origin schema ---------------------------------------------------

def test_to_origin_dict_is_exactly_the_origin_shape():
    school = _base_school(
        {
            "nameOfficial": "Troy University",
            "divisionRoman": "I",
            "conferenceName": "Sun Belt",
            "memberOrgAddress": {"state": "AL"},
            "athleticWebUrl": "www.troytrojans.com",
            "privateFlag": "N",
            "orgId": 674,
        },
        "schools",
    )
    school.city = "Troy"
    school.totalYearlyCost = "$23,165/$33,341"
    school.academicData = {"SATMath": "450-590", "ACTComposite": "18-25"}

    record = school.to_origin_dict()
    assert list(record) == models.SCHOOL_COLUMNS
    # Bookkeeping and the fields you assign yourself never leak into the export.
    for absent in ("id", "logo", "athletics_domain", "ncaa_org_id", "source", "first_seen"):
        assert absent not in record
    assert record["region"] == "South (East South Central)"
    assert record["state"] == "Alabama"
    assert record["division"] == "DI"
    assert record["privatePublic"] == "Public"
    assert record["academicData"] == {"SATMath": "450-590", "ACTComposite": "18-25"}


def test_merge_never_blanks_academics_we_already_hold():
    """A test-optional year returns nulls; that must not erase last year's."""
    stored = School(school="Troy University", ncaa_org_id=674,
                    academicData={"SATMath": "450-590", "averageGPA": "3.19"},
                    totalYearlyCost="$23,165")
    fresh = School(school="Troy University", ncaa_org_id=674, academicData={},
                   conference="Sun Belt")

    merged = stored.merge(fresh)
    assert merged.academicData == {"SATMath": "450-590", "averageGPA": "3.19"}
    assert merged.totalYearlyCost == "$23,165"
    assert merged.conference == "Sun Belt"


def test_average_gpa_survives_a_round_trip_even_though_we_never_fill_it():
    """You may already hold GPA; the store must carry it, not drop it."""
    school = School(school="X", academicData={"averageGPA": "3.16"})
    assert School.from_dict(school.to_dict()).academicData["averageGPA"] == "3.16"
    assert school.to_origin_dict()["academicData"] == {"averageGPA": "3.16"}


# --- store + export ------------------------------------------------------

def test_school_store_and_export_round_trip(tmp_path):
    args = build_parser().parse_args(["stats", "--schools", "--data-dir", str(tmp_path)])
    settings = settings_from_args(args)

    store = storage.SchoolStore(settings)
    store.upsert(School(school="Troy University", ncaa_org_id=674, division="DI",
                        state="Alabama", region="South (East South Central)",
                        athletics_domain="troytrojans.com", source="schools"))
    store.save()

    assert settings.schools_path.exists()
    assert not settings.store_path.exists()  # never touches the company store
    reloaded = storage.SchoolStore(settings).load().sorted_leads()
    assert len(reloaded) == 1
    assert reloaded[0].athletics_domain == "troytrojans.com"

    out = tmp_path / "origin.json"
    export_args = build_parser().parse_args(
        ["export", "--schools", "--out", str(out), "--data-dir", str(tmp_path)]
    )
    export_args.func(export_args)
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert list(payload[0]) == models.SCHOOL_COLUMNS


def test_seeds_command_writes_athletics_hosts(tmp_path):
    args = build_parser().parse_args(["stats", "--schools", "--data-dir", str(tmp_path)])
    settings = settings_from_args(args)
    store = storage.SchoolStore(settings)
    store.upsert(School(school="Troy University", ncaa_org_id=674, division="DI",
                        athletics_domain="troytrojans.com"))
    store.upsert(School(school="No Site College", ncaa_org_id=1, division="DIII"))
    store.save()

    out = tmp_path / "schools.txt"
    seed_args = build_parser().parse_args(
        ["seeds", "--out", str(out), "--data-dir", str(tmp_path)]
    )
    assert seed_args.func(seed_args) == 0
    text = out.read_text(encoding="utf-8")
    assert "troytrojans.com" in text
    assert "No Site College" not in text  # no athletics URL, so no seed line


def test_seeds_division_filter(tmp_path):
    args = build_parser().parse_args(["stats", "--schools", "--data-dir", str(tmp_path)])
    settings = settings_from_args(args)
    store = storage.SchoolStore(settings)
    store.upsert(School(school="A", ncaa_org_id=1, division="DI", athletics_domain="a.com"))
    store.upsert(School(school="B", ncaa_org_id=2, division="DIII", athletics_domain="b.com"))
    store.save()

    out = tmp_path / "d1.txt"
    seed_args = build_parser().parse_args(
        ["seeds", "--out", str(out), "--division", "I", "--data-dir", str(tmp_path)]
    )
    seed_args.func(seed_args)
    text = out.read_text(encoding="utf-8")
    assert "a.com" in text and "b.com" not in text
