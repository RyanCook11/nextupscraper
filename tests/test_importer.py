"""Importing a supplied school list into the store."""

from __future__ import annotations

import csv

from scrapbot.importer import import_schools, juco_body, normalize_name
from scrapbot.models import School


def _csv(tmp_path, rows, header=("School", "Level", "Conference", "State", "Team page (link)")):
    path = tmp_path / "list.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)
    return path


def test_fills_in_a_missing_athletics_site(tmp_path):
    path = _csv(tmp_path, [
        ("Andrew College", "Junior College", "GCAA", "Georgia", "https://andrewfightingtigers.com/sports/mens-basketball"),
    ])
    stored = [School(school="Andrew College", state="Georgia", association="NJCAA")]

    updates, report = import_schools(path, stored)

    assert report.filled == 1
    assert [u.athletics_domain for u in updates] == ["andrewfightingtigers.com"]
    # Identity fields come along so the update merges rather than duplicating.
    assert updates[0].key == stored[0].key


def test_same_name_in_two_states_is_not_crossed_over(tmp_path):
    """The store holds two Bethel Universities. Matching on name alone would
    give one of them the other's athletics site."""
    path = _csv(tmp_path, [
        ("Bethel University", "NAIA", "Crossroads", "Indiana", "https://bethelpilots.com/x"),
    ])
    stored = [
        School(school="Bethel University", state="Tennessee", association="NAIA"),
        School(school="Bethel University", state="Indiana", association="NAIA"),
    ]

    updates, report = import_schools(path, stored)

    assert report.filled == 1
    assert len(updates) == 1
    assert updates[0].state == "Indiana"


def test_an_official_dedicated_host_is_not_overwritten(tmp_path):
    path = _csv(tmp_path, [
        ("Arizona State University", "NCAA Division I", "Big 12", "Arizona", "https://thesundevils.com/x"),
    ])
    stored = [School(school="Arizona State University", state="Arizona",
                     athletics_domain="sundevils.com", association="NCAA")]

    updates, report = import_schools(path, stored)

    assert updates == []
    assert report.conflicting == [("Arizona State University", "sundevils.com", "thesundevils.com")]


def test_a_university_host_masquerading_as_an_athletics_site_is_replaced(tmp_path):
    """We recorded beloit.edu as the "athletics domain" — it is just the college.
    A dedicated host cannot wander into the faculty directory."""
    path = _csv(tmp_path, [
        ("Beloit College", "NCAA Division III", "Midwest", "Wisconsin", "https://beloitcollegeathletics.com/x"),
    ])
    stored = [School(school="Beloit College", state="Wisconsin", website="www.beloit.edu",
                     athletics_domain="beloit.edu", association="NCAA")]

    updates, report = import_schools(path, stored)

    assert report.replaced_university_host == 1
    assert updates[0].athletics_domain == "beloitcollegeathletics.com"


def test_unmatched_rows_are_reported_and_only_added_on_request(tmp_path):
    path = _csv(tmp_path, [
        ("Allan Hancock College", "Junior College", "WSC", "California", "https://hancockathletics.com/x"),
    ])

    updates, report = import_schools(path, [])
    assert updates == [] and report.unmatched == ["Allan Hancock College"]

    updates, report = import_schools(path, [], add_new=True)
    assert len(updates) == 1
    added = updates[0]
    assert added.association == "NJCAA"      # "Junior College" maps to NJCAA
    assert added.athletics_domain == "hancockathletics.com"
    assert added.region == "West (Pacific)"  # derived from the state


def test_rows_without_a_usable_link_are_counted_not_crashed(tmp_path):
    path = _csv(tmp_path, [
        ("No Link College", "NAIA", "X", "Ohio", ""),
        ("", "NAIA", "X", "Ohio", "https://example.com/x"),
    ])
    updates, report = import_schools(path, [], add_new=True)
    assert updates == [] and report.unusable == 2


def test_level_maps_to_association_and_division(tmp_path):
    path = _csv(tmp_path, [
        ("A College", "NCAA Division II", "X", "Ohio", "https://a.com/x"),
        ("B College", "NAIA", "X", "Ohio", "https://b.com/x"),
    ])
    updates, _ = import_schools(path, [], add_new=True)
    by = {u.school: u for u in updates}
    assert (by["A College"].association, by["A College"].division) == ("NCAA", "DII")
    assert (by["B College"].association, by["B College"].division) == ("NAIA", "NAIA")


def test_name_normalisation_ignores_filler_words():
    assert normalize_name("The University of Texas at Austin") == normalize_name(
        "University of Texas Austin"
    )
    assert normalize_name("St. John's University") != normalize_name("St. Joseph's University")


def test_a_junior_college_gets_the_njcaa_division_not_a_blank(tmp_path):
    """Mapping "Junior College" to an empty division left 241 imported schools
    outside every division filter."""
    path = _csv(tmp_path, [
        ("Iowa Central", "Junior College", "ICCAC", "Iowa", "https://tritonathletics.com/x"),
    ])
    updates, _ = import_schools(path, [], add_new=True)

    assert updates[0].association == "NJCAA"
    assert updates[0].division == "NJCAA"


def test_california_and_northwest_juco_bodies_are_not_filed_as_njcaa(tmp_path):
    """A supplied list calls every two-year college "Junior College", but the
    CCCAA and NWAC are separate governing bodies, not NJCAA conferences."""
    path = _csv(tmp_path, [
        ("Allan Hancock College", "Junior College",
         "California Community College Athletic Association (CCCAA)", "California",
         "https://hancockathletics.com/x"),
        ("Bellevue College", "Junior College",
         "Northwest Athletic Conference (NWAC)", "Washington",
         "https://bellevuecollegeathletics.com/x"),
        ("Iowa Central", "Junior College",
         "Iowa Community College Athletic Conference (ICCAC)", "Iowa",
         "https://tritonathletics.com/x"),
    ])
    by = {u.school: u for u in import_schools(path, [], add_new=True)[0]}

    assert (by["Allan Hancock College"].association, by["Allan Hancock College"].division) == ("CCCAA", "CCCAA")
    assert (by["Bellevue College"].association, by["Bellevue College"].division) == ("NWAC", "NWAC")
    # An ordinary NJCAA conference stays NJCAA.
    assert (by["Iowa Central"].association, by["Iowa Central"].division) == ("NJCAA", "NJCAA")


def test_reclassifying_a_juco_gives_it_a_distinct_store_key(tmp_path):
    """The key is slug|state|association, so a CCCAA college and an NJCAA one of
    the same name in the same state stay separate records."""
    cccaa = School(school="A College", state="California", association="CCCAA")
    njcaa = School(school="A College", state="California", association="NJCAA")
    assert cccaa.key != njcaa.key


def test_juco_body_reads_the_conference_column():
    assert juco_body("California Community College Athletic Association (CCCAA)") == "CCCAA"
    assert juco_body("Northwest Athletic Conference (NWAC)") == "NWAC"
    assert juco_body("Kansas Jayhawk Community College Conference (KJCCC)") is None
    assert juco_body(None) is None
