"""Sidearm's person-card staff directory.

Every large athletics site — Texas, Georgia, Kansas State — serves this layout,
and all of them were reported as "site reachable but no staff directory found".
Two separate things had to be wrong at once for that to happen, so both are
pinned here:

* ``_looks_like_directory`` rejected the page before it was ever parsed, because
  the page has no ``mailto:`` and no ``<tr>``.
* ``_parse_cards`` requires exactly one ``mailto:`` per person block, and these
  cards carry none at all — the address lives on the person's profile page.
"""

from __future__ import annotations

from scrapbot import extract
from scrapbot.sources.coaches import (
    _looks_like_directory,
    _parse_person_cards,
    parse_directory,
)
from tests.fixtures import SIDEARM_PERSON_CARDS

BASE = "https://georgiadogs.com/staff-directory"
DOMAIN = "georgiadogs.com"


def _parse():
    tree = extract.parse(SIDEARM_PERSON_CARDS)
    return parse_directory(tree, BASE, DOMAIN, "Bulldog Athletics", "coaches")


def test_a_card_directory_is_recognised_as_a_directory():
    """The gate ran before parsing, so a page it rejected could never yield
    anyone however good the parser was."""
    assert _looks_like_directory(SIDEARM_PERSON_CARDS)


def test_a_page_with_no_people_is_still_rejected():
    assert not _looks_like_directory("<html><body><h1>Page not found</h1></body></html>")


def test_every_person_on_the_card_page_is_found():
    people = _parse()
    assert [p.name for p in people] == [
        "Josh Brooks",
        "Mike White",
        "Chad Dollar",
        "Tom Black",
        "Kim Doyle",
    ]


def test_titles_come_off_the_position_element():
    titles = {p.name: p.title for p in _parse()}
    assert titles["Mike White"] == "Head Coach"
    assert titles["Josh Brooks"] == "Director of Athletics"


def test_the_profile_url_is_absolute_and_per_person():
    urls = {p.name: p.profile_url for p in _parse()}
    assert urls["Mike White"] == "https://georgiadogs.com/staff-directory/mike-white/1201"
    assert len(set(urls.values())) == 5


def test_the_sport_heading_above_a_card_is_carried_onto_it():
    sports = {p.name: p.sport for p in _parse()}
    assert sports["Mike White"] == "Men's Basketball"
    assert sports["Tom Black"] == "Volleyball"


def test_a_person_is_not_emitted_twice_when_both_selectors_match():
    """_CARD_ROOT matches the card by data-test-id *and* by class. Without the
    self-check in the seen set, every person came back doubled."""
    tree = extract.parse(SIDEARM_PERSON_CARDS)
    raw = _parse_person_cards(tree, BASE, DOMAIN, "Bulldog Athletics", "coaches")
    assert len(raw) == 5, f"expected 5 cards, got {len(raw)} (duplicated?)"


def test_cards_without_an_email_are_still_kept():
    """The old card reader dropped anyone with no mailto, which is everyone
    on this layout."""
    people = _parse()
    assert people and all(not p.emails for p in people)


# --- per-sport coach pages ------------------------------------------------

from scrapbot.sources.coaches import (  # noqa: E402
    MIN_SPORT_PAGE_SIGNALS,
    _sport_slugs,
)

SPORT_NAV = """<!doctype html><html><body>
  <a href="/sports/baseball/">Baseball</a>
  <a href="/sports/mens-basketball/roster">Men's Basketball</a>
  <a href="/sports/womens-golf/coaches">Women's Golf</a>
  <a href="/sports/baseball/schedule">Baseball again</a>
  <a href="/sports/2024/recap">2024 season</a>
  <a href="/news/2026/7/29/some-story">A story</a>
</body></html>"""


def test_sport_slugs_are_unique_and_exclude_season_archives():
    """Arizona and Fullerton publish no combined directory; the only way to
    their coaches is one page per sport, so the slug list has to be right."""
    tree = extract.parse(SPORT_NAV)
    assert _sport_slugs(tree) == ["baseball", "mens-basketball", "womens-golf"]


def test_a_single_sport_page_clears_the_lowered_gate():
    """Men's golf lists a head coach and an assistant. The whole-school
    threshold reads two people as noise and drops the page."""
    two_coaches = (
        "<html><body><table>"
        '<tr><td>A Coach</td><td><a href="mailto:a@x.edu">a@x.edu</a></td></tr>'
        '<tr><td>B Coach</td><td><a href="mailto:b@x.edu">b@x.edu</a></td></tr>'
        "</table></body></html>"
    )
    assert not _looks_like_directory(two_coaches)
    assert _looks_like_directory(two_coaches, MIN_SPORT_PAGE_SIGNALS)


def test_the_lowered_gate_still_rejects_a_page_with_nobody_on_it():
    assert not _looks_like_directory(
        "<html><body><h1>Golf</h1><p>Season preview.</p></body></html>",
        MIN_SPORT_PAGE_SIGNALS,
    )
