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


# --- NAIA ----------------------------------------------------------------

NAIA_PAGE = "\n".join([
    "NATIONAL ASSOCIATION OF INTERCOLLEGIATE ATHLETICS  2026-2027 MEMBER INSTITUTIONS",
    "Last Modified: 7/13/26  1",
    "Total Schools (5)",
    "Baker University – KS  HAAC",
    "Bethel University – IN  Crossroads",
    "Bethel University – TN  Mid-South",
    # Inverted for sorting, and missing the trailing "of" as the real PDF is.
    "Saint Francis, University – IL  CCAC",
    # One row in the real PDF uses a plain hyphen instead of an en dash.
    "Victoria, University of - BC CAC",
])


def _naia(monkeypatch, text=NAIA_PAGE):
    """Drive parse_naia_pdf without a real PDF."""
    class FakePage:
        def extract_text(self):
            return text

    class FakeReader:
        def __init__(self, *a, **k):
            self.pages = [FakePage()]

    import pypdf
    monkeypatch.setattr(pypdf, "PdfReader", FakeReader)
    return apis.parse_naia_pdf(b"%PDF-fake")


def test_naia_pdf_parses_every_row(monkeypatch):
    members = _naia(monkeypatch)
    assert len(members) == 5
    assert members[0]["nameOfficial"] == "Baker University"
    assert members[0]["conferenceName"] == "Heart of America Athletic Conference"
    assert members[0]["memberOrgAddress"]["state"] == "KS"
    assert apis.ncaa_division(members[0]) == "NAIA"


def test_naia_same_name_schools_are_kept_apart(monkeypatch):
    members = _naia(monkeypatch)
    bethels = [m for m in members if m["nameOfficial"] == "Bethel University"]
    assert {m["memberOrgAddress"]["state"] for m in bethels} == {"IN", "TN"}


def test_naia_inverted_names_are_restored(monkeypatch):
    names = {m["nameOfficial"] for m in _naia(monkeypatch)}
    assert "University of Saint Francis" in names
    assert "University of Victoria" in names  # plain-hyphen row still parsed


def test_naia_conference_codes_expand_to_full_names(monkeypatch):
    confs = {m["conferenceName"] for m in _naia(monkeypatch)}
    assert "Crossroads League" in confs
    assert "Chicagoland Collegiate Athletic Conference" in confs
    assert not any(len(c) <= 6 for c in confs)  # no bare codes survive


def test_naia_count_mismatch_is_reported(monkeypatch, caplog):
    text = NAIA_PAGE.replace("Total Schools (5)", "Total Schools (9)")
    with caplog.at_level("WARNING"):
        _naia(monkeypatch, text)
    assert "but the PDF states 9" in caplog.text


def test_division_normalization_does_not_mangle_naia():
    """NAIA is a division value itself — prefixing it with D hid all 233."""
    assert models.normalize_division("naia") == "NAIA"
    assert models.normalize_division("NAIA") == "NAIA"
    assert models.normalize_division("I") == "DI"
    assert models.normalize_division("iii") == "DIII"
    assert models.normalize_division("DII") == "DII"
    assert models.normalize_division("") == ""


def test_canadian_members_get_a_state_name_and_region():
    assert usregions.state_name("BC") == "British Columbia"
    assert usregions.region_for("BC") == "Canada"


def test_same_named_naia_schools_do_not_collide_in_the_store():
    a = School(school="Bethel University", state="Indiana", division="NAIA")
    b = School(school="Bethel University", state="Tennessee", division="NAIA")
    assert a.key != b.key

    store_keys = {a.key, b.key}
    assert len(store_keys) == 2


# --- NJCAA ---------------------------------------------------------------

NJCAA_PAGE = "\n".join([
    "There are 4 Division I teams in the [[National Junior College Athletic Association]].",
    "==Members==",
    "===Alabama===",
    "*[[Bevill State Community College]] Bears in [[Sumiton, Alabama|Sumiton]]",
    "*[[Coastal Alabama Community College]] Sun Chiefs in [[Bay Minette, Alabama|Bay Minette]]",
    "===Arkansas===",
    # Division II/III articles write the city as plain text, not a link.
    "*[[North Arkansas College]] Pioneers in Harrison",
    # Piped link with an anchor: the display half is the real name.
    "*[[University of Connecticut#Avery Point campus|UConn Avery Point]] Pointers in Groton",
    "===External links===",
    "* [https://www.njcaa.org/member_colleges/directory/members NJCAA members]",
    "*Pacific Northwest Christian College Gladiators in Kennewick",
])


def test_njcaa_parses_both_city_formats():
    members = apis.parse_njcaa_wikitext(NJCAA_PAGE, "NJCAA DI")
    by_name = {m["nameOfficial"]: m for m in members}
    assert by_name["Bevill State Community College"]["city"] == "Sumiton"
    assert by_name["North Arkansas College"]["city"] == "Harrison"
    assert by_name["Bevill State Community College"]["memberOrgAddress"]["state"] == "AL"
    assert by_name["North Arkansas College"]["memberOrgAddress"]["state"] == "AR"


def test_njcaa_piped_link_uses_the_display_name():
    names = {m["nameOfficial"] for m in apis.parse_njcaa_wikitext(NJCAA_PAGE, "NJCAA DI")}
    assert "UConn Avery Point" in names
    assert not any("#" in n for n in names)


def test_njcaa_reference_bullets_are_not_schools():
    names = {m["nameOfficial"] for m in apis.parse_njcaa_wikitext(NJCAA_PAGE, "NJCAA DI")}
    assert not any("njcaa.org" in n or n.startswith("http") for n in names)
    # The unlinked entry is skipped rather than guessed at, and logged.
    assert not any("Gladiators" in n for n in names)


def test_njcaa_tiers_collapse_to_one_division():
    """The NJCAA tier is per-sport, not per-college, so all three articles
    produce a single "NJCAA" — and it can never be mistaken for an NCAA tier."""
    members = apis.parse_njcaa_wikitext(NJCAA_PAGE, "NJCAA DI")
    assert apis.ncaa_division(members[0]) == "NJCAA"
    for value in ("njcaa 1", "NJCAA DI", "njcaa dii", "NJCAA DIII", "njcaa"):
        assert models.normalize_division(value) == "NJCAA"
    assert models.normalize_division("I") == "DI"  # still NCAA


def test_same_name_in_naia_and_njcaa_are_different_schools():
    """Cottey College and Marian University are in both lists — as different
    institutions. Without the association they overwrote one another."""
    naia = School(school="Marian University", state="Indiana", division="NAIA",
                  association="NAIA")
    njcaa = School(school="Marian University", state="Indiana", division="NJCAA DII",
                   association="NJCAA")
    assert naia.key != njcaa.key


def test_a_college_in_two_njcaa_lists_stays_one_division():
    """29 colleges appear in two of the three NJCAA articles because they play
    different levels in different sports. That used to merge into
    "NJCAA DI; NJCAA DII", which no division filter matched."""
    d1 = School(school="Iowa Central", state="Iowa",
                division=models.normalize_division("NJCAA DI"), association="NJCAA")
    d2 = School(school="Iowa Central", state="Iowa",
                division=models.normalize_division("NJCAA DII"), association="NJCAA")
    assert d1.key == d2.key
    assert d1.merge(d2).division == "NJCAA"


def test_association_is_backfilled_for_records_written_before_the_field():
    """Old rows must keep their key, not split into duplicates."""
    assert School.from_dict({"school": "X", "division": "NAIA"}).association == "NAIA"
    assert School.from_dict({"school": "X", "division": "NJCAA DI"}).association == "NJCAA"
    assert School.from_dict({"school": "X", "division": "DI"}).association == "NCAA"
    assert School.from_dict({"school": "X", "ncaa_org_id": 5}).association == "NCAA"
    assert School.from_dict({"school": "X"}).association is None


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
