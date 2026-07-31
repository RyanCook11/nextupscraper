"""Canonical sport labels, derived at read time.

The store holds 2,895 distinct ``sport`` values for about thirty sports —
``FOOTBALL``, ``Tennis (M)``, ``Baseball - baseball@brandeis.edu``, and section
banners with an administrator's name attached. The filter listed every one of
them, which made it unusable.
"""

from __future__ import annotations

from scrapbot.models import Contact
from scrapbot.sports import base_of, canonical, canonical_field, matches, options


def _contact(sport=None, department=None, name="A Coach") -> Contact:
    return Contact(
        name=name,
        school="Test College",
        school_domain="test.edu",
        title="Head Coach",
        sport=sport,
        department=department,
    )


# --- canonicalising the raw values ----------------------------------------


def test_scraped_heading_noise_is_stripped():
    for raw, expected in [
        ("FOOTBALL", "Football"),
        ("Men's Basketball // Sport Administrator: Pat Garrity", "Men's Basketball"),
        ("Baseball - baseball@brandeis.edu", "Baseball"),
        ("Women's Basketball | Moody Coliseum | 845 Coliseum Way", "Women's Basketball"),
        ("2025-26 Yetis Baseball Coaches", "Baseball"),
        ("wrestling - women", "Women's Wrestling"),
    ]:
        assert canonical_field(raw) == expected, raw


def test_a_coach_over_two_programs_keeps_both():
    """Written by Contact.merge — one person, two teams. Dropping either would
    hide them from a filter they belong in."""
    assert canonical("Cross Country; Track & Field") == ["Cross Country", "Track & Field"]


def test_swimming_and_diving_become_one_program():
    """Schools run them with one staff, so two labels split every such coach."""
    assert canonical_field("Men's Swimming") == "Men's Swimming & Diving"
    assert canonical_field("Diving") == "Swimming & Diving"


def test_a_coach_listed_under_both_swimming_and_diving_appears_once():
    assert canonical("Men's Swimming; Men's Diving") == ["Men's Swimming & Diving"]


def test_crew_folds_into_rowing():
    assert canonical_field("Crew") == "Rowing"


def test_a_value_with_no_sport_in_it_resolves_to_nothing():
    assert canonical("Strength & Conditioning") == []
    assert canonical(None) == []


# --- filter semantics -----------------------------------------------------


def test_choosing_a_sport_includes_its_gendered_variants():
    """Picking Basketball must return all 11,762, not the 307 rows nobody
    assigned a gender to."""
    assert matches("Men's Basketball", "Basketball")
    assert matches("Women's Basketball", "Basketball")
    assert matches("Basketball", "Basketball")


def test_choosing_a_gendered_variant_narrows_to_it():
    assert matches("Men's Basketball", "Men's Basketball")
    assert not matches("Women's Basketball", "Men's Basketball")


def test_a_gendered_needle_is_not_matched_as_a_substring():
    """"men's basketball" is a literal substring of "women's basketball", so a
    substring test on a resolved value returns the wrong rows."""
    assert not matches("Women's Soccer", "Men's Soccer")
    assert not matches("Women's Wrestling", "Men's Wrestling")


def test_flag_football_is_not_football():
    assert not matches("Women's Flag Football", "Football")
    assert matches("Women's Flag Football", "Flag Football")


def test_department_still_matches_as_free_text():
    """A department is not a sport and never resolves, so it keeps the plain
    substring behaviour the filter always had."""
    assert matches(None, "strength", department="Strength & Conditioning")
    assert matches("Football", "strength", department="Strength & Conditioning")


def test_an_unresolvable_sport_value_still_matches_as_text():
    assert matches("Esports", "esports")


# --- the dropdown ---------------------------------------------------------


def test_variants_are_grouped_under_their_sport():
    """Alphabetical or by-count ordering scatters Men's/Women's Basketball away
    from Basketball, which is what made the list hard to scan."""
    raw = (
        ["Men's Basketball"] * 5
        + ["Women's Basketball"] * 4
        + ["Basketball"]
        + ["Men's Soccer"] * 2
        + ["Football"] * 20
    )
    assert list(options(raw)) == [
        "Football",
        "Basketball",
        "Men's Basketball",
        "Women's Basketball",
        "Men's Soccer",
    ]


def test_groups_are_ordered_by_their_combined_size():
    """Basketball's 24 across two variants outranks Football's 20, even though
    Football's single entry is larger than either Basketball entry. Ordering on
    the individual counts would put Football first and split the group up."""
    raw = ["Football"] * 20 + ["Men's Basketball"] * 12 + ["Women's Basketball"] * 12
    assert list(options(raw)) == ["Men's Basketball", "Women's Basketball", "Football"]


def test_counts_survive_the_grouping():
    raw = ["Men's Basketball"] * 3 + ["FOOTBALL"] * 2
    assert options(raw) == {"Football": 2, "Men's Basketball": 3}


def test_base_of_strips_the_qualifier():
    assert base_of("Women's Ice Hockey") == "Ice Hockey"
    assert base_of("Football") == "Football"


# --- the CSV column -------------------------------------------------------


def test_the_csv_gains_a_canonical_column():
    row = _contact(sport="MEN'S BASKETBALL // Sport Administrator: Pat Garrity").to_row()
    assert row["sport"] == "MEN'S BASKETBALL // Sport Administrator: Pat Garrity"
    assert row["sport_canonical"] == "Men's Basketball"


def test_the_canonical_value_is_never_stored():
    """Option A: derived on read. If it reached to_dict() it would be written
    into contacts.json and go stale the moment a merge rule changed."""
    contact = _contact(sport="Crew")
    assert "sport_canonical" not in contact.to_dict()
    assert contact.to_row()["sport_canonical"] == "Rowing"


def test_a_contact_with_no_sport_gets_an_empty_cell():
    assert _contact(sport=None).to_row()["sport_canonical"] == ""


# --- one coach over both programs -----------------------------------------


def test_a_coach_of_both_programs_reads_as_the_plain_sport():
    """"Men's & Women's Swimming & Diving" is one person over both squads.
    Two entries overstate it; the plain sport is what the heading means."""
    assert canonical_field("Men's & Women's Swimming & Diving") == "Swimming & Diving"
    assert canonical_field("Men's and Women's Cross Country") == "Cross Country"
    assert canonical_field("Men's/Women's Golf") == "Golf"


def test_the_merge_leaves_no_stray_ungendered_label():
    """"Diving" matches on its own with no qualifier, so merging it into
    Swimming & Diving used to emit a third, bare label alongside the two
    gendered ones."""
    assert canonical("Men's & Women's Swimming & Diving") == ["Swimming & Diving"]


def test_one_gender_is_not_collapsed():
    assert canonical_field("Men's Swimming & Diving") == "Men's Swimming & Diving"
    assert canonical_field("Women's Cross Country") == "Women's Cross Country"


def test_two_sports_for_both_genders_keep_both_sports():
    assert canonical("Men's and Women's Cross Country and Track & Field") == [
        "Cross Country",
        "Track & Field",
    ]
