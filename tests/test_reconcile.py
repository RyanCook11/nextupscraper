"""Departures: a coach the school stopped listing is marked, not left in post.

Merging alone only ever touches records that *were* scraped, so a fired coach
would otherwise survive forever. These cover the other side of that — and,
just as importantly, the cases where a disappearance must NOT be read as a
departure.
"""

from __future__ import annotations

import json

from scrapbot.cli import build_parser, filter_contacts, settings_from_args
from scrapbot.models import Contact
from scrapbot.storage import ContactStore
from tests import fixtures
from tests.fixtures import FixtureSite
from tests.test_coaches import _run


def _store(tmp_path, *contacts: Contact) -> ContactStore:
    args = build_parser().parse_args(["stats", "--data-dir", str(tmp_path)])
    store = ContactStore(settings_from_args(args))
    for contact in contacts:
        store.upsert(contact)
    return store


def _person(name: str, domain: str = "goduke.com", **kw) -> Contact:
    return Contact(
        name=name,
        school_domain=domain,
        profile_url=f"https://{domain}/staff/{name.lower().replace(' ', '-')}",
        **kw,
    )


# --- the core rule --------------------------------------------------------

def test_person_missing_from_a_successful_scrape_is_marked_departed(tmp_path):
    stay, gone = _person("Stay Put"), _person("Moved On")
    store = _store(tmp_path, stay, gone)

    report = store.reconcile({"goduke.com": {stay.key}})

    assert gone.departed is True
    assert gone.departed_at  # when we noticed, recorded
    assert stay.departed is False
    assert report.total == 1
    assert report.departed == {"goduke.com": 1}


def test_departed_people_are_kept_not_deleted(tmp_path):
    """The email and the history stay useful after they leave."""
    stay, gone = _person("Stay Put"), _person("Moved On", emails=["gone@goduke.com"])
    store = _store(tmp_path, stay, gone)
    store.reconcile({"goduke.com": {stay.key}})
    store.save()

    reloaded = ContactStore(store.settings).load()
    assert len(reloaded.leads) == 2
    assert reloaded.leads[gone.key].emails == ["gone@goduke.com"]
    assert reloaded.leads[gone.key].departed is True


def test_marking_is_idempotent_and_keeps_the_first_date(tmp_path):
    """Five scrapes without them is still one departure, on the first date."""
    stay, gone = _person("Stay Put"), _person("Moved On")
    store = _store(tmp_path, stay, gone)

    first = store.reconcile({"goduke.com": {stay.key}})
    noticed = gone.departed_at
    second = store.reconcile({"goduke.com": {stay.key}})

    assert first.total == 1 and second.total == 0
    assert gone.departed_at == noticed


def test_a_returning_coach_is_un_departed(tmp_path):
    """Rehired, or wrongly flagged. Either way the scrape is newer evidence."""
    gone = _person("Came Back")
    store = _store(tmp_path, gone)
    store.reconcile({"goduke.com": set()} | {"goduke.com": {"someone-else"}})
    assert gone.departed is True

    store.upsert(_person("Came Back", title="Head Coach"))

    assert store.leads[gone.key].departed is False
    assert store.leads[gone.key].departed_at is None
    assert store.returned_count == 1


# --- when a disappearance must NOT count ----------------------------------

def test_a_school_that_was_not_scraped_is_untouched(tmp_path):
    """The guarantee behind skipping failed sites: no roster, no reconcile."""
    duke = _person("Duke Person", "goduke.com")
    unc = _person("UNC Person", "goheels.com")
    store = _store(tmp_path, duke, unc)

    store.reconcile({"goduke.com": {duke.key}})

    assert unc.departed is False


def test_a_site_that_parsed_to_nobody_proves_nothing(tmp_path):
    """An empty page is a broken scrape, not an empty athletics department."""
    person = _store(tmp_path, _person("Still There"))
    store = person
    store.reconcile({"goduke.com": set()})
    assert all(not c.departed for c in store.leads.values())


def test_a_mass_disappearance_is_refused_and_reported(tmp_path):
    """A site redesign looks exactly like mass firing. Do not guess."""
    people = [_person(f"Coach {i}") for i in range(10)]
    store = _store(tmp_path, *people)

    report = store.reconcile({"goduke.com": {people[0].key}})

    assert report.total == 0
    assert all(not c.departed for c in people)
    assert report.skipped == {"goduke.com": (9, 10)}


def test_the_operator_can_override_the_safety_catch(tmp_path):
    people = [_person(f"Coach {i}") for i in range(10)]
    store = _store(tmp_path, *people)

    report = store.reconcile({"goduke.com": {people[0].key}}, max_loss=1.0)

    assert report.total == 9
    assert not report.skipped


def test_small_schools_are_exempt_from_the_ratio(tmp_path):
    """With three on file, one leaving is 33% — a normal event, not a signal."""
    people = [_person(f"Coach {i}") for i in range(3)]
    store = _store(tmp_path, *people)

    report = store.reconcile({"goduke.com": {people[0].key}})

    assert report.total == 2
    assert not report.skipped


def test_the_ratio_counts_only_people_still_in_post(tmp_path):
    """Already-departed records must not dilute the denominator.

    Counting them as "stored" would shrink the apparent loss and let a real
    site breakage slip past the safety catch — the longer a school has been
    tracked, the more departures it has accrued, and the weaker the guard
    would get.
    """
    people = [_person(f"Coach {i}") for i in range(20)]
    store = _store(tmp_path, *people)
    for person in people[:10]:
        person.mark_departed()

    # 10 remain; 9 of them vanish. 90% of the live roster -> refused. Counting
    # the 10 already gone would read as 9/20 = 45%, and it would go through.
    report = store.reconcile({"goduke.com": {people[10].key}})

    assert report.total == 0
    assert report.skipped == {"goduke.com": (9, 10)}


# --- exports and filters --------------------------------------------------

def test_departed_are_left_out_of_lists_by_default():
    here, gone = _person("Here"), _person("Gone")
    gone.mark_departed()
    people = [here, gone]

    assert [c.name for c in filter_contacts(people)] == ["Here"]
    assert [c.name for c in filter_contacts(people, include_departed=True)] == ["Here", "Gone"]
    assert [c.name for c in filter_contacts(people, departed_only=True)] == ["Gone"]


def test_export_hides_departed_and_departed_only_lists_them(tmp_path):
    from scrapbot.cli import cmd_export

    here, gone = _person("Here"), _person("Gone")
    store = _store(tmp_path, here, gone)
    store.reconcile({"goduke.com": {here.key}})
    store.save()

    def _export(*extra) -> list[str]:
        out = tmp_path / f"out{len(extra)}{''.join(extra)}.json".replace("--", "")
        args = build_parser().parse_args(
            ["export", "--data-dir", str(tmp_path), "--contacts", "--out", str(out), *extra]
        )
        assert cmd_export(args) == 0
        return [c["name"] for c in json.loads(out.read_text(encoding="utf-8"))]

    assert _export() == ["Here"]
    assert _export("--departed-only") == ["Gone"]
    assert sorted(_export("--include-departed")) == ["Gone", "Here"]


# --- end to end -----------------------------------------------------------

def _swap_directory(html: str):
    """Serve a different staff directory, as a school would after a change."""
    original = fixtures.PAGES["/staff-directory"]
    fixtures.PAGES["/staff-directory"] = html
    return original


# The same directory a season later: Pat Oduya is gone, Jo Kim is the new
# assistant, everyone else is where they were. Kept the same size as the
# original on purpose — a page that shrinks below MIN_DIRECTORY_SIGNALS is
# rejected as "not a directory", which is a different scenario (and the one
# test_a_site_that_fails_marks_nobody covers).
HIRED_AND_FIRED = """<!doctype html>
<html><head><title>Staff Directory | State University Athletics</title>
  <meta property="og:site_name" content="State University Athletics" />
</head><body>
  <h1>Staff Directory</h1>
  <table>
    <thead><tr><th>Senior Administration</th><th>Name</th><th>Title</th>
      <th>Email</th><th>Phone</th></tr></thead>
    <tbody>
      <tr><td><a href="/staff-directory/dana-reyes/12">Dana Reyes</a></td>
          <td>Director of Athletics</td>
          <td><a href="mailto:dana.reyes@state.edu">dana.reyes@state.edu</a></td>
          <td><a href="tel:+12315550101">(231) 555-0101</a></td></tr>
    </tbody>
  </table>
  <table>
    <thead><tr><th>Men's Basketball</th><th>Name</th><th>Title</th>
      <th>Email</th><th>Phone</th></tr></thead>
    <tbody>
      <tr><td><a href="/staff-directory/chris-vance/34">Chris Vance</a></td>
          <td>Head Coach</td>
          <td><a href="mailto:chris.vance@state.edu">chris.vance@state.edu</a></td>
          <td><a href="tel:+12315550102">(231) 555-0102</a></td></tr>
      <tr><td><a href="/staff-directory/jo-kim/99">Jo Kim</a></td>
          <td>Assistant Coach</td>
          <td><a href="mailto:jo.kim@state.edu">jo.kim@state.edu</a></td>
          <td></td></tr>
      <tr><td><a href="/staff-directory/sam-webb/36">Sam Webb</a></td>
          <td>Equipment Manager</td>
          <td><a href="mailto:sam.webb@state.edu">sam.webb@state.edu</a></td>
          <td></td></tr>
    </tbody>
  </table>
</body></html>
"""


def test_rerunning_a_school_adds_the_hire_and_marks_the_fired(tmp_path):
    with FixtureSite() as netloc:
        base = [
            "run", "--data-dir", str(tmp_path), "--delay", "0", "--render", "never",
            "--no-cache", "coaches", "--sites", netloc,
        ]
        first, settings = _run(base)
        assert first.departed == 0
        before = {c.name for c in ContactStore(settings).load().sorted_leads()}
        assert "Pat Oduya" in before

        original = _swap_directory(HIRED_AND_FIRED)
        try:
            second, _ = _run(base)
        finally:
            fixtures.PAGES["/staff-directory"] = original

        people = {c.name: c for c in ContactStore(settings).load().sorted_leads()}

        # The hire is added...
        assert people["Jo Kim"].departed is False
        assert second.new == 1
        # ...the coach still listed is left alone...
        assert people["Chris Vance"].departed is False
        # ...and the one the page dropped is flagged, not deleted.
        assert people["Pat Oduya"].departed is True
        assert people["Pat Oduya"].departed_at
        assert people["Pat Oduya"].emails == ["pat.oduya@state.edu"]
        assert second.departed == 1
        # Nobody else was disturbed: 4 before + 1 hire, none removed.
        assert len(people) == len(before) + 1
        assert [c.name for c in people.values() if c.departed] == ["Pat Oduya"]


def test_a_filtered_run_never_reads_as_a_departure(tmp_path):
    """--coaches-only narrows what is yielded, not what the school employs.

    The roster is recorded before the filter for exactly this reason; reading
    departures off the yielded stream would retire every non-coach on file.
    """
    with FixtureSite() as netloc:
        base = [
            "run", "--data-dir", str(tmp_path), "--delay", "0", "--render", "never",
            "--no-cache", "coaches", "--sites", netloc,
        ]
        _, settings = _run(base)
        stored = ContactStore(settings).load().sorted_leads()
        assert any(not c.is_coach for c in stored)  # there is something to lose

        result, _ = _run(base + ["--coaches-only"])

        assert result.departed == 0
        people = ContactStore(settings).load().sorted_leads()
        assert [c.name for c in people if c.departed] == []


def test_a_site_that_fails_marks_nobody(tmp_path):
    """No roster is recorded unless the scrape succeeded, so this cannot fire."""
    with FixtureSite() as netloc:
        base = [
            "run", "--data-dir", str(tmp_path), "--delay", "0", "--render", "never",
            "--no-cache", "coaches", "--sites", netloc,
        ]
        _, settings = _run(base)
        stored = len(ContactStore(settings).load().leads)
        assert stored

        original = _swap_directory("<html><body><h1>Under construction</h1></body></html>")
        try:
            result, _ = _run(base)
        finally:
            fixtures.PAGES["/staff-directory"] = original

        assert result.departed == 0
        people = ContactStore(settings).load().sorted_leads()
        assert len(people) == stored
        assert [c.name for c in people if c.departed] == []


def test_no_reconcile_turns_the_whole_thing_off(tmp_path):
    with FixtureSite() as netloc:
        base = [
            "run", "--data-dir", str(tmp_path), "--delay", "0", "--render", "never",
            "--no-cache", "coaches", "--sites", netloc,
        ]
        _, settings = _run(base)

        original = _swap_directory(HIRED_AND_FIRED)
        try:
            result, _ = _run(base + ["--no-reconcile"])
        finally:
            fixtures.PAGES["/staff-directory"] = original

        assert result.departed == 0
        assert result.reconcile is None
        people = ContactStore(settings).load().sorted_leads()
        assert [c.name for c in people if c.departed] == []


def test_a_dry_run_marks_nobody(tmp_path):
    with FixtureSite() as netloc:
        base = [
            "run", "--data-dir", str(tmp_path), "--delay", "0", "--render", "never",
            "--no-cache", "coaches", "--sites", netloc,
        ]
        _, settings = _run(base)

        original = _swap_directory(HIRED_AND_FIRED)
        try:
            result, _ = _run(base + ["--dry-run"])
        finally:
            fixtures.PAGES["/staff-directory"] = original

        assert result.departed == 0
        people = ContactStore(settings).load().sorted_leads()
        assert [c.name for c in people if c.departed] == []
