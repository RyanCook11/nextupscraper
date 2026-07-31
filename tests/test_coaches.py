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
from tests.fixtures import (
    FACULTY_DIRECTORY, MALFORMED, SIDEARM_DIRECTORY, STAFF_CARDS, STAFF_DIRECTORY,
    STAFF_SECTIONED,
    FixtureSite,
)


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
    assert coach.phones == ["+1 231 555 0102"]
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
    assert dana.phones == ["+1 231 555 0101"]  # kept from the row that had one


def test_single_table_with_section_rows():
    """Sections marked by a one-cell row must set the sport, not become people."""
    people = {c.name: c for c in _parse(STAFF_SECTIONED, "wildcat.edu")}

    assert set(people) == {"Dale Nash", "Rob Vance", "Kim Doyle", "Ana Reyes"}
    # The section labels themselves are not people.
    assert "Football" not in people and "Administration" not in people

    assert people["Dale Nash"].department == "Administration"
    assert people["Rob Vance"].sport == "Football"
    assert people["Kim Doyle"].sport == "Football"
    assert people["Ana Reyes"].sport == "Volleyball"


def test_page_title_is_not_used_as_a_group():
    """'Staff Directory' is the page's own heading, not a sport."""
    for contact in _parse(STAFF_SECTIONED, "wildcat.edu"):
        assert contact.sport != "Staff Directory"
        assert contact.department != "Staff Directory"


def test_section_rows_still_flag_shared_program_inboxes():
    from scrapbot.sources.coaches import _flag_shared_emails

    people = _parse(STAFF_SECTIONED, "wildcat.edu")
    _flag_shared_emails(people)
    by_name = {c.name: c for c in people}
    # Only two people share football@, below the threshold of three.
    assert by_name["Rob Vance"].shared_email is False
    assert by_name["Ana Reyes"].shared_email is False


def test_card_layout_fallback():
    people = {c.name: c for c in _parse(STAFF_CARDS, "cardinal.edu")}
    assert set(people) == {"Robin Ellis", "Jamie Fox"}
    robin = people["Robin Ellis"]
    assert robin.emails == ["robin.ellis@cardinal.edu"]
    assert robin.phones == ["+1 231 555 0103"]
    assert robin.is_coach is True
    assert robin.sport == "Women's Soccer"


def test_a_jersey_number_and_class_year_are_not_part_of_the_name():
    """Division III directories decorate the name with both.

    Chestnut Hill lists "#42 Matthew Owens '18" — a jersey number in front, the
    alumni class year behind. Neither belongs in the name field: a search for
    "Matthew Owens" has to find him, and two records of one coach must not
    differ only by the year he graduated.
    """
    from scrapbot.sources.coaches import normalize_person_name as clean

    assert clean("#13 Robert Spratt") == "Robert Spratt"
    assert clean("#42 Matthew Owens '18") == "Matthew Owens"
    assert clean("Aiesha Smith '12") == "Aiesha Smith"
    assert clean("Camille Dunham '26") == "Camille Dunham"
    assert clean("Erick Camodeca ’06") == "Erick Camodeca"  # curly apostrophe

    # An apostrophe inside a surname is not a class year.
    assert clean("Ed O'Melia") == "Ed O'Melia"
    assert clean("#27 Ed O'Melia") == "Ed O'Melia"
    assert clean("Kim D'Angelo") == "Kim D'Angelo"
    assert clean("Anne-Marie O'Shea '99") == "Anne-Marie O'Shea"

    # The surname-comma flip still works, and sees the undecorated name.
    assert clean("O'Brien, Sean '14") == "Sean O'Brien"
    assert clean("Baker, Alycia") == "Alycia Baker"
    assert clean("Smith, Jr.") == "Smith, Jr."


def test_a_vacant_post_is_not_a_person():
    """Directories list unfilled jobs with a placeholder where the name goes.

    SUNY Broome, Cal, Charleston Southern, Denison and Cal Poly between them
    spell it "TBD", "TBA TBA", "T BA", "TBA ,", ". TBD" and "- Vacant -". Each
    one is an honest entry — the post really is open — but a contact named TBA
    is a job opening, not somebody to contact.
    """
    from scrapbot.sources.coaches import is_placeholder_name

    for placeholder in (
        "TBD", "TBA", "T BA", "TBA ,", ". TBD", "TBA ...", "TBD TBD", "TBA TBA",
        "- Vacant -", "To Be Announced", "to be determined", "Open Position",
        "POSITION OPEN", "N/A", "",
    ):
        assert is_placeholder_name(placeholder), placeholder

    # Real names must survive, including ones that merely start the same way.
    for name in (
        "Tim Hays", "Tom Carter", "Nate Bannister", "Tabitha Bannon",
        "Tyson McDowell", "Ana Reyes", "T.J. Otzelberger", "Naomi Tba-Adjacent",
    ):
        assert not is_placeholder_name(name), name


def test_a_placeholder_row_is_skipped_by_the_table_parser():
    people = _parse(
        """<html><body><table>
          <tr><th>Name</th><th>Title</th><th>Email</th></tr>
          <tr><td>Rob Germaine</td><td>Assistant Coach Baseball</td>
              <td><a href="mailto:germainerd@sunybroome.edu">e</a></td></tr>
          <tr><td>TBD</td><td>Head Coach Baseball</td>
              <td><a href="mailto:athleticsdept@sunybroome.edu">e</a></td></tr>
        </table></body></html>""",
        "broomehornets.com",
    )
    assert [c.name for c in people] == ["Rob Germaine"]


def test_a_nested_card_wrapper_is_not_a_second_person():
    """utrockets.com: a coach's name came out as their phone number.

    The modern Sidearm card nests wrappers three deep, and every one of them
    holds the single mailto: that marks a person block. The ancestor check that
    should have skipped the inner ones compared ``id(node)`` — but selectolax
    returns a fresh Python wrapper on every access, so ``id(node.parent)`` never
    matched the ``id()`` recorded for that same element and the check never
    fired. The innermost wrapper is the contact block, whose first ``<a>`` is
    the ``tel:`` link, so it parsed as a person named "419-530-4796" — and,
    sharing the outer card's profile URL, it merged over the real name.
    """
    from scrapbot.sources.coaches import _parse_cards

    html = """<html><body>
      <div class="s-person-card">
        <div class="s-person-card__content">
          <div class="s-person-details">
            <a href="/staff-directory/jordan-lauf/590" aria-hidden="true"></a>
            <h4>Jordan Lauf</h4>
            <p>Assistant Men's Basketball Coach</p>
          </div>
          <div class="s-person-card__content__person-contact-info">
            <a href="tel:+14195304796">419-530-4796</a>
            <a href="mailto:jordan.lauf@utoledo.edu">Email</a>
          </div>
        </div>
      </div>
    </body></html>"""
    people = _parse_cards(
        extract.parse(html), "https://utrockets.com/staff-directory",
        "utrockets.com", "Toledo", "coaches",
    )
    assert len(people) == 1, [p.name for p in people]
    person = people[0]
    assert person.name == "Jordan Lauf"
    assert person.title == "Assistant Men's Basketball Coach"
    assert person.emails == ["jordan.lauf@utoledo.edu"]
    assert person.phones == ["+1 419 530 4796"]


def test_card_title_drops_the_contact_details_it_ran_together():
    """andrewcollege.edu's cards have no inner markup, so the whole block
    collapsed into the title: "ATHLETICS Head Baseball Coach 229-732-5901
    adambiss@andrewcollege.edu"."""
    from scrapbot.sources.coaches import _parse_cards

    html = """<html><body><ul>
      <li><h4>Adam Biss</h4> ATHLETICS Head Baseball Coach 229-732-5901
          <a href="mailto:adambiss@andrewcollege.edu">adambiss@andrewcollege.edu</a></li>
    </ul></body></html>"""
    people = _parse_cards(extract.parse(html), "https://andrewcollege.edu/staff",
                          "andrewcollege.edu", "Andrew College", "coaches")
    assert [c.title for c in people] == ["Head Baseball Coach"]
    assert people[0].emails == ["adambiss@andrewcollege.edu"]
    # Cleaning must not cost the row its coach flag (set later, in _dedupe).
    assert is_coaching_title(people[0].title) is True


def test_clean_title_keeps_the_job_and_nothing_else():
    from scrapbot.sources.coaches import clean_title

    assert clean_title("ATHLETICS Head Baseball Coach 229-732-5901 "
                       "adambiss@andrewcollege.edu") == "Head Baseball Coach"
    assert clean_title("Athletics Athletic Director Head Men's Basketball Coach "
                       "229-732-5918 brianskortz@andrewcollege.edu") == \
        "Athletic Director Head Men's Basketball Coach"
    assert clean_title("Academic Support Center Professional Tutor / Academic Coach "
                       "912-704-4760 angelaroberts@andrewcollege.edu") == \
        "Academic Support Center Professional Tutor / Academic Coach"
    assert clean_title("Phone: 229-732-5942 Email: billygordy@andrewcollege.edu "
                       "Head Men's Wrestling Coach") == "Head Men's Wrestling Coach"
    # A clean title is left exactly as it is.
    assert clean_title("Head Coach, Women's Soccer") == "Head Coach, Women's Soccer"
    # "Office"/"Athletics" are only noise as a label or a banner, not as words.
    assert clean_title("Office Manager") == "Office Manager"
    assert clean_title("Athletics Director") == "Athletics Director"
    # Singular "Athletic" is part of the job, not a banner — and cleaning an
    # already-clean title has to be a no-op, or a second pass keeps eating it.
    for title in ("Athletic Director Head Men's Basketball Coach",
                  "Athletic Trainer", "Faculty Athletics Representative"):
        assert clean_title(title) == title
        assert clean_title(clean_title(title)) == title
    assert clean_title("") is None
    assert clean_title("229-732-5901") is None


def test_a_title_column_holding_a_phone_number_is_cleaned_too():
    html = """<html><body><table>
      <tr><th>Name</th><th>Title</th><th>Email</th></tr>
      <tr><td>Casey Lane</td><td>Head Track Coach 555-123-4567</td>
          <td><a href="mailto:casey@school.edu">casey@school.edu</a></td></tr>
    </table></body></html>"""
    people = _parse(html, "school.edu")
    assert [c.title for c in people] == ["Head Track Coach"]
    assert people[0].phones == []   # a title cell is not a phone column


def test_a_card_heading_is_never_the_previous_persons_name():
    """Andrew College's coaches were all filed under a colleague's name.

    The name lives in the card's own heading, so the block above a card is the
    previous *card* — walking up for a section heading found their name.
    """
    from scrapbot.sources.coaches import _parse_cards

    html = """<html><body><div>
      <h2>Coaching Staff</h2>
      <ul>
        <li><h4>Fran Balkcom</h4> Professor of Biology
            <a href="mailto:franbalkcom@andrewcollege.edu">franbalkcom@andrewcollege.edu</a></li>
        <li><h4>Adam Biss</h4> Head Baseball Coach
            <a href="mailto:adambiss@andrewcollege.edu">adambiss@andrewcollege.edu</a></li>
      </ul></div></body></html>"""
    people = {c.name: c for c in _parse_cards(
        extract.parse(html), "https://andrewcollege.edu/staff",
        "andrewcollege.edu", "Andrew College", "coaches")}

    for field in ("department", "sport"):
        assert getattr(people["Adam Biss"], field) != "Fran Balkcom"
    # The real heading above both cards still comes through, once the cards
    # themselves stop being mined for one.
    assert people["Adam Biss"].department == "Coaching Staff"


def test_sport_is_read_from_the_title_when_there_is_no_sport_column():
    from scrapbot.sources.coaches import sport_from_title

    assert sport_from_title("Head Baseball Coach") == "Baseball"
    assert sport_from_title("Head Men's Soccer Coach Head Women's Soccer Coach") == \
        "Men's Soccer; Women's Soccer"
    assert sport_from_title("Head Golf Coach Assistant Men's Basketball Coach") == \
        "Golf; Men's Basketball"
    assert sport_from_title("Head Women's Flag Football Coach") == "Women's Flag Football"
    assert sport_from_title("Head Cheerleading Coach") == "Cheerleading"
    assert sport_from_title("Head Track and Field Coach") == "Track & Field"
    # Longest name wins, or "flag football" would read as plain football.
    assert sport_from_title("Head Beach Volleyball Coach") == "Beach Volleyball"
    # One coach over two teams, written as a shared program.
    assert sport_from_title("Head Men's and Women's Cross Country Coach") == \
        "Men's Cross Country; Women's Cross Country"
    assert sport_from_title("Head Men's & Women's Golf Coach") == "Men's Golf; Women's Golf"
    # Directories use a typographic apostrophe far more often than a straight
    # one; missing it dropped the qualifier and merged the two programs.
    assert sport_from_title("Head Men’s Wrestling Coach") == "Men's Wrestling"
    # The qualifier is not always adjacent to the sport.
    assert sport_from_title("Women's Asst. Basketball Coach") == "Women's Basketball"
    # No sport named, so nothing is invented.
    assert sport_from_title("Athletic Director") is None
    assert sport_from_title("Academic Support Center Professional Tutor") is None
    assert sport_from_title("Director of Attendance") is None   # not "dance"
    assert sport_from_title(None) is None


def test_campus_directory_coaches_get_their_sport_from_their_title():
    """The salvage path had no sport at all — the whole point of the fix."""
    from scrapbot.sources.coaches import Candidate, _coaches_only
    from scrapbot.net import Page

    people = [
        Contact(name="Adam Biss", school_domain="andrewcollege.edu",
                title="Head Baseball Coach", is_coach=True, department="A-D"),
        Contact(name="Jane Doe", school_domain="andrewcollege.edu",
                title="Professor of Biology", department="A-D"),
    ]
    page = Page(url="https://andrewcollege.edu/staff-faculty-directory/", status=200, html="")
    found = _coaches_only(
        [Candidate(page=page, contacts=people, school="Andrew College", athletics=False)],
        "andrewcollege.edu",
    )
    assert [c.name for c in found.contacts] == ["Adam Biss"]   # professor dropped
    coach = found.contacts[0]
    assert coach.sport == "Baseball"
    assert coach.department is None          # "A-D" is not a department
    assert any("read from the job title" in n for n in coach.notes)


def test_a_campus_directory_is_still_not_mistaken_for_an_athletics_one():
    """Inferring sport must not let a faculty page pass the athletics gate.

    If it did, discovery would keep the professors too, so the inference has to
    stay downstream of is_athletics_directory().
    """
    from scrapbot.sources.coaches import is_athletics_directory

    people = _parse(FACULTY_DIRECTORY, "andrewcollege.edu")
    assert is_athletics_directory(people) is False


def test_the_athletics_page_layout_assigns_each_coach_their_own_sport():
    """andrewfightingtigers.com: one <h2> section plus one table per sport.

    This is the layout behind --manual-dir for sites that refuse us, so the
    sport has to come from the section heading, not from a title guess.
    """
    html = """<html><body><h1>Staff Directory</h1>
      <h2>Administration</h2>
      <table><tr><th>Name</th><th>Title</th><th>Phone</th><th>E-Mail</th></tr>
        <tr><td>Brian Skortz</td><td>Athletic Director / Head Men's Basketball Coach</td>
            <td>(229) 732-5918</td>
            <td><a href="mailto:brianskortz@andrewcollege.edu">e</a></td></tr></table>
      <h2>Baseball</h2>
      <table><tr><th>Name</th><th>Title</th><th>Phone</th><th>E-Mail</th></tr>
        <tr><td>Sean Stevens</td><td>Head Baseball Coach</td><td></td>
            <td><a href="mailto:seanstevens@andrewcollege.edu">e</a></td></tr></table>
      <h2>Women's Basketball</h2>
      <table><tr><th>Name</th><th>Title</th><th>Phone</th><th>E-Mail</th></tr>
        <tr><td>Kelly Britsky</td><td>Head Women's Basketball Coach</td>
            <td>(229) 209-5270</td>
            <td><a href="mailto:kellybritsky@andrewcollege.edu">e</a></td></tr></table>
    </body></html>"""
    people = {c.name: c for c in _parse(html, "andrewfightingtigers.com")}

    assert people["Sean Stevens"].sport == "Baseball"
    assert people["Kelly Britsky"].sport == "Women's Basketball"
    assert people["Brian Skortz"].department == "Administration"
    # Nobody inherits the section above their own.
    assert people["Sean Stevens"].department is None
    assert people["Kelly Britsky"].sport != "Baseball"


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


# --- telling an athletics directory from a campus one ---------------------
#
# aquinas.edu shipped 151 professors into the contact store because discovery
# accepted the first page that *looked* like a directory. The athletics site
# (aqsaints.com) was never reached.

def test_campus_directory_is_not_taken_for_an_athletics_one():
    from scrapbot.sources.coaches import is_athletics_directory

    faculty = _parse(FACULTY_DIRECTORY, domain="aq.edu")
    assert len(faculty) == 8
    assert not is_athletics_directory(faculty)

    # The real thing, by contrast, attributes people to sports.
    assert is_athletics_directory(_parse(STAFF_DIRECTORY))


def test_a_trailing_unknown_column_does_not_break_the_row_parse():
    """`Name | Title | Department | Email` shifted the named columns left by
    one, indexing cells[-1] — and cells[-3] on a short row, which raised."""
    people = {c.name: c for c in _parse(FACULTY_DIRECTORY, domain="aq.edu")}

    assert people["Alan Reed"].title == "Professor of Biology"
    assert people["Alan Reed"].emails == ["alan.reed@aq.edu"]
    # Finn Doyle's row has no email cell at all — the short row that crashed.
    assert people["Finn Doyle"].title == "Assistant Coach"
    assert people["Finn Doyle"].emails == []


def test_general_directory_is_salvaged_for_its_coaches_only():
    """A small college with no athletics site of its own still has coaches in
    the campus list. Keep those, drop the professors."""
    from scrapbot.sources.coaches import Candidate, _coaches_only

    parsed = _parse(FACULTY_DIRECTORY, domain="aq.edu")
    page = type("P", (), {"url": "https://aq.edu/faculty-staff"})()
    salvaged = _coaches_only(
        [Candidate(page=page, contacts=parsed, school="Aquinas", athletics=False)],
        "aq.edu",
    )

    assert salvaged is not None
    assert {c.name for c in salvaged.contacts} == {"Eve Marsh", "Finn Doyle"}
    assert salvaged.athletics is False


def test_a_campus_directory_with_no_coaches_is_not_salvaged():
    from scrapbot.sources.coaches import Candidate, _coaches_only

    professors = [c for c in _parse(FACULTY_DIRECTORY, domain="aq.edu") if not c.is_coach]
    page = type("P", (), {"url": "https://aq.edu/faculty-staff"})()
    assert _coaches_only(
        [Candidate(page=page, contacts=professors, school="Aquinas", athletics=False)],
        "aq.edu",
    ) is None


# --- the Sidearm layout (aqsaints.com) ------------------------------------

def _sidearm():
    return {c.name: c for c in _parse(SIDEARM_DIRECTORY, domain="aqsaints.com")}


def test_sidearm_section_headers_do_not_shift_the_columns():
    """Sections are one-cell <th> rows. Sweeping them into the header list made
    a 5-column table look 7 wide, and names came out as email fragments."""
    people = _sidearm()

    assert set(people) == {"Damon Bouwkamp", "Ryan Bertoia", "Dana Poe"}
    assert people["Damon Bouwkamp"].title == "Director of Intercollegiate Athletics (AD)"
    assert people["Ryan Bertoia"].title == "Head Coach"


def test_sidearm_th_sections_assign_the_sport():
    """Only <td> section rows were recognised, so everyone lost their sport —
    which then made the whole page look like a non-athletics directory."""
    from scrapbot.sources.coaches import is_athletics_directory

    people = _sidearm()
    assert people["Ryan Bertoia"].sport == "Men's Basketball"
    assert people["Damon Bouwkamp"].sport is None  # Adminstration is a department
    assert people["Damon Bouwkamp"].department == "Adminstration"
    assert is_athletics_directory(list(people.values()))


def test_script_assembled_emails_are_recovered():
    """The address never appears in the HTML as text — only as two JS halves."""
    people = _sidearm()
    assert people["Damon Bouwkamp"].emails == ["bouwkdam@aquinas.edu"]
    assert people["Ryan Bertoia"].emails == ["rmb004@aquinas.edu"]
    # and the script source must never leak into a visible field
    assert "placeholder" not in (people["Ryan Bertoia"].title or "")
    assert all("var " not in c.name for c in people.values())


def test_headshots_are_captured_but_placeholders_are_not():
    people = _sidearm()
    assert people["Ryan Bertoia"].photo_url == (
        "https://aqsaints.com/images/2024/9/12/Ryan_Bertoia.jpg"
    )
    assert people["Dana Poe"].photo_url is None  # stock silhouette, not a person


def test_save_photos_downloads_headshots(tmp_path):
    with FixtureSite() as netloc:
        result, settings = _run([
            "run", "--data-dir", str(tmp_path), "--delay", "0", "--render", "never",
            "coaches", "--directory-url", f"http://{netloc}/sidearm", "--save-photos",
        ])

        saved = {c.name: c.photo_file for c in result.leads if c.photo_file}
        assert set(saved) == {"Damon Bouwkamp", "Ryan Bertoia"}  # Dana Poe had a placeholder

        on_disk = settings.data_dir / saved["Ryan Bertoia"]
        assert on_disk.exists() and on_disk.read_bytes().startswith(b"\xff\xd8")
        assert saved["Ryan Bertoia"].startswith(f"photos/{netloc.split(':')[0]}")

        # The CSV carries the photo columns so the data is usable outside the app.
        header = settings.contacts_csv_path.read_text(encoding="utf-8-sig").splitlines()[0]
        assert "photo_url" in header and "photo_file" in header


def test_photos_are_not_downloaded_by_default(tmp_path):
    """The URL is always recorded; fetching 80 images per school is opt-in."""
    with FixtureSite() as netloc:
        result, _ = _run([
            "run", "--data-dir", str(tmp_path), "--delay", "0", "--render", "never",
            "coaches", "--directory-url", f"http://{netloc}/sidearm", "--dry-run",
        ])

        assert any(c.photo_url for c in result.leads)
        assert not any(c.photo_file for c in result.leads)
        assert not (tmp_path / "photos").exists()


def test_a_caption_row_names_the_sport_for_the_whole_table():
    """Duke puts the group in its own one-cell <th> row above the header row.
    Excluding it from the column headers is right; dropping it is not — without
    it every coach loses their sport and the page reads as non-athletics."""
    html = """<html><body><table>
      <thead>
        <tr><th>Men's Basketball</th></tr>
        <tr><th>Name</th><th>Title</th><th>Email</th><th>Phone</th></tr>
      </thead>
      <tbody>
        <tr><td>Kara Voss</td><td>Head Coach</td><td>kv@duke.edu</td><td></td></tr>
      </tbody>
    </table></body></html>"""
    people = _parse(html)

    assert len(people) == 1
    assert people[0].name == "Kara Voss"          # not shifted by the caption row
    assert people[0].sport == "Men's Basketball"
    assert people[0].emails == ["kv@duke.edu"]


# --- campus-directory hygiene ---------------------------------------------
#
# cisco.edu and bigbend.edu reached the dashboard as "Baker, Alycia" whose sport
# was "Location" and whose school was "Campus Directory".

def test_surname_first_names_are_flipped():
    from scrapbot.sources.coaches import normalize_person_name

    assert normalize_person_name("Baker, Alycia") == "Alycia Baker"
    assert normalize_person_name("Batie-Smoose, Melissa") == "Melissa Batie-Smoose"
    assert normalize_person_name("O'Brien, Sean") == "Sean O'Brien"
    # Already in reading order, or a suffix rather than a forename: leave alone.
    assert normalize_person_name("Bailey Johnson") == "Bailey Johnson"
    assert normalize_person_name("Smith, Jr.") == "Smith, Jr."
    assert normalize_person_name("Lee, PhD") == "Lee, PhD"


def test_an_unrecognised_column_is_not_the_sport():
    """"Location" and "Department" are columns, not groups. Treating any
    unknown header as the group made them every person's sport."""
    html = """<html><body><table>
      <tr><th>Name</th><th>Title</th><th>Location</th><th>Email</th></tr>
      <tr><td>Alycia Baker</td><td>Assistant Basketball Coach</td>
          <td>Main Campus</td><td>alycia.baker@cisco.edu</td></tr>
    </table></body></html>"""
    person = _parse(html, domain="cisco.edu")[0]

    assert person.sport is None and person.department is None
    assert person.title == "Assistant Basketball Coach"
    assert person.emails == ["alycia.baker@cisco.edu"]


def test_the_pages_own_title_is_not_the_school():
    from scrapbot.sources.coaches import _school_name

    for title in ("Campus Directory", "Faculty & Staff Directory", "Employee Directory"):
        html = f"<html><head><title>{title}</title></head><body><h1>{title}</h1></body></html>"
        assert _school_name(extract.parse(html), "bigbend.edu") is None

    html = "<html><head><title>Cisco College</title></head><body><h1>Cisco College</h1></body></html>"
    assert _school_name(extract.parse(html), "cisco.edu") == "Cisco College"


def test_alphabet_buckets_and_nav_headings_are_not_groups():
    from scrapbot.sources.coaches import _usable_group

    for junk in ("A-D", "A", "0-9", "Campus Directory", "Jump to a Section", "Filter by name"):
        assert not _usable_group(junk), junk
    for real in ("Men's Basketball", "Sports Medicine", "Athletic Administration"):
        assert _usable_group(real), real


def test_salvaged_coaches_carry_no_campus_grouping():
    """A campus page's grouping is not athletic, so it must not survive into
    the sport or department of a coach salvaged from it."""
    from scrapbot.sources.coaches import Candidate, _coaches_only

    people = _parse(FACULTY_DIRECTORY, domain="aq.edu")
    for person in people:
        person.department = "A-D"
    page = type("P", (), {"url": "https://aq.edu/faculty-staff"})()

    salvaged = _coaches_only(
        [Candidate(page=page, contacts=people, school="Aquinas", athletics=False)], "aq.edu"
    )
    assert {c.name for c in salvaged.contacts} == {"Eve Marsh", "Finn Doyle"}
    assert all(c.department is None and c.sport is None for c in salvaged.contacts)


# --- pages saved by hand from a browser -----------------------------------
#
# Some athletics hosts answer 403 to anything that identifies itself as a
# crawler. The page is public; a person can open it. This is how that page
# gets into the store without pretending to be someone we are not.

def _manual_run(tmp_path, data_dir, extra=()):
    return _run([
        "run", "--data-dir", str(data_dir), "--delay", "0", "--render", "never",
        "coaches", "--manual-dir", str(tmp_path), *extra,
    ])


def test_a_saved_page_is_parsed_without_a_single_request(tmp_path):
    saved = tmp_path / "saved"
    (saved / "aqsaints.com").mkdir(parents=True)
    (saved / "aqsaints.com" / "staff.html").write_text(SIDEARM_DIRECTORY, encoding="utf-8")

    result, settings = _manual_run(saved, tmp_path / "data")

    assert result.fetch_stats.get("requests", 0) == 0
    assert {c.name for c in result.leads} == {"Damon Bouwkamp", "Ryan Bertoia", "Dana Poe"}
    # The folder name is the school, and links resolve against that host.
    assert {c.school_domain for c in result.leads} == {"aqsaints.com"}
    assert next(c for c in result.leads if c.name == "Ryan Bertoia").photo_url == (
        "https://aqsaints.com/images/2024/9/12/Ryan_Bertoia.jpg"
    )
    assert (settings.contacts_path).exists()


def test_the_run_report_shows_where_a_saved_page_came_from(tmp_path):
    saved = tmp_path / "saved"
    (saved / "aqsaints.com").mkdir(parents=True)
    (saved / "aqsaints.com" / "staff.html").write_text(SIDEARM_DIRECTORY, encoding="utf-8")

    result, _ = _manual_run(saved, tmp_path / "data")
    outcome = result.outcomes[0]

    assert outcome.domain == "aqsaints.com" and outcome.status == "ok"
    assert "saved page" in outcome.detail
    assert outcome.people == 3


def test_a_saved_page_with_no_staff_rows_is_reported_not_silent(tmp_path):
    saved = tmp_path / "saved"
    (saved / "example.com").mkdir(parents=True)
    (saved / "example.com" / "wrong.html").write_text(
        "<html><body><h1>Ticket office</h1></body></html>", encoding="utf-8"
    )

    result, _ = _manual_run(saved, tmp_path / "data")

    assert result.leads == []
    assert result.outcomes[0].status == "empty"
    assert "wrong.html" in result.outcomes[0].detail


def test_saved_pages_and_live_sites_run_together(tmp_path):
    saved = tmp_path / "saved"
    (saved / "aqsaints.com").mkdir(parents=True)
    (saved / "aqsaints.com" / "staff.html").write_text(SIDEARM_DIRECTORY, encoding="utf-8")

    with FixtureSite() as netloc:
        result, _ = _manual_run(saved, tmp_path / "data", extra=["--sites", netloc])

    domains = {c.school_domain for c in result.leads}
    assert "aqsaints.com" in domains and any(d.startswith("127.0.0.1") for d in domains)


def test_a_heading_above_each_table_names_the_sport():
    """Bay College uses <h2>Baseball</h2> then a table, rather than a caption
    row. Its coaches showed no sport at all because the athletics host was
    never reached — but the layout itself must parse."""
    from tests.fixtures import HEADING_PER_SPORT
    from scrapbot.sources.coaches import is_athletics_directory

    people = {c.name: c for c in _parse(HEADING_PER_SPORT, domain="baynorse.com")}

    assert people["Travis Derrick"].sport == "Baseball"
    assert people["James Fassett"].sport == "Women's Basketball"
    # Administration is a department, not a sport.
    assert people["Matt Johnson"].sport is None
    assert people["Matt Johnson"].department == "Administration"
    assert is_athletics_directory(list(people.values()))
