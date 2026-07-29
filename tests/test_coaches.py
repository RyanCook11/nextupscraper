"""The ``coaches`` source: one record per person, not one per site."""

from __future__ import annotations

import asyncio
import json

from scrapbot import extract
from scrapbot.cli import build_parser, filter_contacts, settings_from_args
from scrapbot.models import Contact
from scrapbot.runner import run_source
from scrapbot.sources.coaches import is_coaching_title, parse_directory
from scrapbot.storage import ContactStore
from tests.fixtures import MALFORMED, STAFF_CARDS, STAFF_DIRECTORY, FixtureSite


def _run(argv: list[str]):
    args = build_parser().parse_args(argv)
    settings = settings_from_args(args)
    dry_run = getattr(args, "dry_run", False)
    result = asyncio.run(run_source(args.source, args, settings, dry_run=dry_run))
    return result, settings


def _parse(html: str, domain: str = "state.edu"):
    return parse_directory(
        extract.parse(html), f"https://{domain}/staff-directory", domain, "State", "coaches"
    )


# --- parsing -------------------------------------------------------------

def test_table_layout_yields_one_record_per_person():
    people = {c.name: c for c in _parse(STAFF_DIRECTORY)}

    # Four rows, three people — Dana Reyes is listed under two groups.
    assert set(people) == {"Dana Reyes", "Chris Vance", "Pat Oduya", "Sam Webb"}

    coach = people["Chris Vance"]
    assert coach.title == "Head Coach"
    assert coach.sport == "Men's Basketball"
    assert coach.emails == ["chris.vance@state.edu"]
    assert coach.phones == ["+15551110002"]
    assert coach.profile_url.endswith("/staff-directory/chris-vance/34")
    assert coach.is_coach is True


def test_each_person_only_gets_their_own_row_details():
    """The whole point: contact details must not bleed between rows."""
    people = {c.name: c for c in _parse(STAFF_DIRECTORY)}
    assert people["Pat Oduya"].emails == ["pat.oduya@state.edu"]
    assert people["Pat Oduya"].phones == []  # blank cell, not the row above's
    assert people["Sam Webb"].emails == ["sam.webb@state.edu"]


def test_support_staff_is_not_flagged_as_a_coach():
    people = {c.name: c for c in _parse(STAFF_DIRECTORY)}
    assert people["Sam Webb"].is_coach is False
    assert people["Dana Reyes"].is_coach is False


def test_titles_that_only_mention_coaching_are_not_coaches():
    assert is_coaching_title("Head Coach") is True
    assert is_coaching_title("Assistant Cheer Coach") is True
    assert is_coaching_title("Associate Head Coach (Sprints)") is True
    assert is_coaching_title("Executive Assistant to the Head Coach") is False
    assert is_coaching_title("Director of Athletics") is False
    assert is_coaching_title(None) is False


def test_person_listed_twice_merges_and_keeps_both_groups():
    people = {c.name: c for c in _parse(STAFF_DIRECTORY)}
    dana = people["Dana Reyes"]
    assert dana.department == "Senior Administration"
    assert dana.sport == "Track & Field"
    assert dana.emails == ["dana.reyes@state.edu"]
    assert dana.phones == ["+15551110001"]  # kept from the row that had one


def test_card_layout_fallback():
    people = {c.name: c for c in _parse(STAFF_CARDS, "cardinal.edu")}
    assert set(people) == {"Robin Ellis", "Jamie Fox"}
    robin = people["Robin Ellis"]
    assert robin.emails == ["robin.ellis@cardinal.edu"]
    assert robin.phones == ["+15552220003"]
    assert robin.is_coach is True
    assert robin.sport == "Women's Soccer"


def test_sport_versus_department_classification():
    people = {c.name: c for c in _parse(STAFF_DIRECTORY)}
    assert people["Chris Vance"].sport == "Men's Basketball"
    assert people["Chris Vance"].department is None
    assert people["Sam Webb"].department is None  # sport table, so it's a sport
    assert people["Dana Reyes"].department == "Senior Administration"


# --- the malformed-page regression ---------------------------------------

def test_visible_text_survives_a_script_that_nests_the_document():
    """A <script> holding markup used to swallow the page on decompose."""
    text = extract.visible_text(extract.parse(MALFORMED))
    assert "Real visible copy that must survive" in text
    assert "var tpl" not in text  # the script's own source is still excluded
    assert "fib = 8 13 21 34 55" not in text  # code samples still excluded
    assert len(text) > 100


def test_phones_still_ignore_code_samples_on_a_malformed_page():
    tree = extract.parse(MALFORMED)
    found = extract.phones(tree, extract.visible_text(tree))
    assert any("9876 5432" in p for p in found)
    assert not any("13 21 34" in p for p in found)


# --- end to end ----------------------------------------------------------

def test_end_to_end_writes_a_contacts_store(tmp_path):
    with FixtureSite() as netloc:
        argv = [
            "run", "--data-dir", str(tmp_path), "--delay", "0", "--render", "never",
            "coaches", "--sites", netloc,
        ]
        result, settings = _run(argv)

        assert len(result.leads) == 4
        assert all(isinstance(c, Contact) for c in result.leads)
        assert result.new == 4 and result.updated == 0

        # People go to contacts.json, and never touch the company store.
        assert settings.contacts_path.exists()
        assert not settings.store_path.exists()
        stored = json.loads(settings.contacts_path.read_text(encoding="utf-8"))
        assert stored["count"] == 4

        csv_text = settings.contacts_csv_path.read_text(encoding="utf-8-sig")
        assert csv_text.splitlines()[0].startswith("name,title,sport")
        assert "chris.vance@state.edu" in csv_text

        # The run snapshot is named for what it holds.
        assert result.out_dir is not None
        assert (result.out_dir / "contacts.csv").exists()

        # Re-running merges instead of duplicating.
        result2, settings2 = _run(argv)
        assert result2.new == 0 and result2.updated == 4
        reloaded = ContactStore(settings2).load().sorted_leads()
        assert len(reloaded) == 4


def test_coaches_only_and_sport_filters(tmp_path):
    with FixtureSite() as netloc:
        result, _ = _run([
            "run", "--data-dir", str(tmp_path), "--delay", "0", "--render", "never",
            "coaches", "--sites", netloc, "--coaches-only", "--dry-run",
        ])
        assert {c.name for c in result.leads} == {"Chris Vance", "Pat Oduya"}

        result2, _ = _run([
            "run", "--data-dir", str(tmp_path), "--delay", "0", "--render", "never",
            "coaches", "--sites", netloc, "--sport", "basketball", "--dry-run",
        ])
        assert {c.name for c in result2.leads} == {"Chris Vance", "Pat Oduya", "Sam Webb"}


def test_shared_inbox_is_flagged_not_dropped():
    """Directories list an assistant or a program inbox in the coach's row.
    That address is real and often the only way in — flag it, keep it."""
    from scrapbot.sources.coaches import _flag_shared_emails

    people = [
        Contact(name="Head Coach", school_domain="s.edu", emails=["assistant@s.edu"]),
        Contact(name="Assoc Coach", school_domain="s.edu", emails=["assistant@s.edu"]),
        Contact(name="Asst Coach", school_domain="s.edu", emails=["assistant@s.edu"]),
        Contact(name="Solo Coach", school_domain="s.edu", emails=["solo@s.edu"]),
        Contact(name="No Email", school_domain="s.edu"),
    ]
    _flag_shared_emails(people)

    assert [p.shared_email for p in people] == [True, True, True, False, False]
    assert people[0].emails == ["assistant@s.edu"]  # kept, not removed
    assert "shared by 3 people" in people[0].notes[0]


def test_direct_email_filter_excludes_shared_inboxes():
    people = [
        Contact(name="A", school_domain="s.edu", emails=["a@s.edu"]),
        Contact(name="B", school_domain="s.edu", emails=["desk@s.edu"], shared_email=True),
        Contact(name="C", school_domain="s.edu"),
    ]
    assert [c.name for c in filter_contacts(people, direct_email=True)] == ["A"]
    assert [c.name for c in filter_contacts(people, has_email=True)] == ["A", "B"]


def test_university_host_resolves_to_the_athletics_host(tmp_path):
    """A university URL must not be crawled for a staff directory — the
    athletics site is a different domain, and the school store knows it."""
    from scrapbot.models import School
    from scrapbot.sources.coaches import CoachesSource
    from scrapbot.storage import SchoolStore

    args = build_parser().parse_args(["stats", "--data-dir", str(tmp_path)])
    settings = settings_from_args(args)
    store = SchoolStore(settings)
    store.upsert(School(school="Jacksonville State University", ncaa_org_id=311,
                        website="www.jsu.edu", athletics_domain="jaxstatesports.com"))
    store.save()

    source = CoachesSource(settings, args)
    assert source._athletics_host_map() == {"jsu.edu": "jaxstatesports.com"}


def test_athletics_link_is_found_on_a_university_homepage():
    from scrapbot.sources.coaches import _athletics_host

    html = """<html><body>
      <a href="https://www.facebook.com/jsu">Athletics on Facebook</a>
      <a href="https://www.jaxstatesports.com/">Athletics</a>
      <a href="https://www.jsu.edu/admissions">Admissions</a>
    </body></html>"""
    host = _athletics_host(extract.parse(html), "https://www.jsu.edu/", "jsu.edu")
    assert host == "jaxstatesports.com"  # social host rejected, same-host ignored


def test_export_filters():
    people = [
        Contact(name="A", school_domain="s.edu", title="Head Coach", is_coach=True,
                sport="Rowing", emails=["a@s.edu"]),
        Contact(name="B", school_domain="s.edu", title="Ticket Manager", sport=None,
                department="Ticket Office"),
    ]
    assert [c.name for c in filter_contacts(people, coaches_only=True)] == ["A"]
    assert [c.name for c in filter_contacts(people, has_email=True)] == ["A"]
    assert [c.name for c in filter_contacts(people, sport="rowing")] == ["A"]
    assert [c.name for c in filter_contacts(people, sport="ticket")] == ["B"]
