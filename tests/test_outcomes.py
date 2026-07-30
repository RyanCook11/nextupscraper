"""Per-site success/failure reporting: a blocked site must never look empty."""

from __future__ import annotations

import json

from scrapbot.cli import build_parser, settings_from_args
from scrapbot.models import SiteOutcome
from scrapbot.net import NETWORK_FAILED, ROBOTS_BLOCKED, Page
from scrapbot.sources.coaches import CoachesSource, _failure_outcome
from tests.fixtures import FixtureSite
from tests.test_coaches import _run


def page(status: int, url: str = "https://x.edu/", error: str = "", html: str = "") -> Page:
    return Page(url=url, status=status, html=html, error=error)


# --- classification ------------------------------------------------------

def test_a_403_is_reported_as_blocked_not_as_empty():
    outcome = _failure_outcome("x.edu", [page(200, html="<html>hi</html>"), page(403)])
    assert outcome.status == SiteOutcome.BLOCKED
    assert "403" in outcome.detail
    assert outcome.retryable is True


def test_429_counts_as_blocked():
    assert _failure_outcome("x.edu", [page(429)]).status == SiteOutcome.BLOCKED


def test_robots_disallow_is_its_own_reason():
    outcome = _failure_outcome("x.edu", [page(ROBOTS_BLOCKED)])
    assert outcome.status == SiteOutcome.ROBOTS
    # Retrying a robots rule is pointless — it is a decision, not a glitch.
    assert outcome.retryable is False


def test_total_network_failure_is_reported_as_network():
    outcome = _failure_outcome(
        "x.edu", [page(NETWORK_FAILED, error="ConnectTimeout: timed out")]
    )
    assert outcome.status == SiteOutcome.NETWORK
    assert "ConnectTimeout" in outcome.detail
    assert outcome.retryable is True


def test_reachable_site_with_nothing_to_find_is_not_a_network_failure():
    """london.edu really has no staff directory — retrying will not help."""
    outcome = _failure_outcome("x.edu", [page(200, html="<html>hi</html>"), page(404)])
    assert outcome.status == SiteOutcome.NO_DIRECTORY
    assert outcome.retryable is False


def test_blocked_wins_over_not_found():
    """If any request was refused, that is the actionable reason."""
    outcome = _failure_outcome("x.edu", [page(200, html="<html>hi</html>"), page(403), page(404)])
    assert outcome.status == SiteOutcome.BLOCKED


def test_no_attempts_at_all_is_a_network_failure():
    assert _failure_outcome("x.edu", []).status == SiteOutcome.NETWORK


def test_only_error_states_are_retryable():
    retryable = {SiteOutcome.BLOCKED, SiteOutcome.NETWORK, SiteOutcome.ERROR}
    for status in [SiteOutcome.OK, SiteOutcome.EMPTY, SiteOutcome.ROBOTS,
                   SiteOutcome.NO_DIRECTORY, SiteOutcome.BLOCKED,
                   SiteOutcome.NETWORK, SiteOutcome.ERROR]:
        assert SiteOutcome("d", status).retryable is (status in retryable)


# --- retry policy --------------------------------------------------------

def test_dns_failures_are_not_retried():
    """A name that doesn't resolve won't resolve a second later, and a seed
    list of hundreds of schools usually holds a few dead domains."""
    import httpx

    from scrapbot.net import _is_permanent

    assert _is_permanent(httpx.ConnectError("[Errno 11001] getaddrinfo failed"))
    assert _is_permanent(httpx.ConnectError("Name or service not known"))
    # Transient things must still be retried.
    assert not _is_permanent(httpx.ConnectError("Connection refused"))
    assert not _is_permanent(httpx.ReadTimeout("timed out"))
    assert not _is_permanent(httpx.ConnectTimeout("timed out"))


# --- end to end ----------------------------------------------------------

def test_run_reports_success_and_failure_lists(tmp_path):
    with FixtureSite() as netloc:
        result, _ = _run([
            "run", "--data-dir", str(tmp_path), "--delay", "0", "--render", "never",
            "coaches", "--sites", netloc, "this-host-does-not-exist-xyz.invalid",
        ])

        assert len(result.outcomes) == 2
        by_domain = {o.domain: o for o in result.outcomes}

        good = by_domain[netloc]
        assert good.status == SiteOutcome.OK and good.people == 4

        bad = by_domain["this-host-does-not-exist-xyz.invalid"]
        assert bad.status == SiteOutcome.NETWORK
        assert bad.retryable is True

        assert len(result.succeeded) == 1
        assert len(result.failed) == 1
        assert len(result.retryable) == 1


def test_run_writes_a_retryable_seed_file(tmp_path):
    with FixtureSite() as netloc:
        result, _ = _run([
            "run", "--data-dir", str(tmp_path), "--delay", "0", "--render", "never",
            "coaches", "--sites", netloc, "this-host-does-not-exist-xyz.invalid",
        ])

        assert result.out_dir is not None
        sites = json.loads((result.out_dir / "sites.json").read_text(encoding="utf-8"))
        assert {s["status"] for s in sites} == {"ok", "network"}

        failed = (result.out_dir / "failed.txt").read_text(encoding="utf-8")
        assert "this-host-does-not-exist-xyz.invalid" in failed
        assert netloc not in failed
        assert "network" in failed

        succeeded = (result.out_dir / "succeeded.txt").read_text(encoding="utf-8")
        assert netloc in succeeded

        # The failed list is a seed file, so a retry is one command.
        args = build_parser().parse_args(
            ["run", "coaches", "--seeds", str(result.out_dir / "failed.txt")]
        )
        source = CoachesSource(settings_from_args(args), args)
        assert source._load_seeds() == ["this-host-does-not-exist-xyz.invalid"]


def test_meta_json_records_the_site_tally(tmp_path):
    with FixtureSite() as netloc:
        result, _ = _run([
            "run", "--data-dir", str(tmp_path), "--delay", "0", "--render", "never",
            "coaches", "--sites", netloc, "this-host-does-not-exist-xyz.invalid",
        ])
        meta = json.loads((result.out_dir / "meta.json").read_text(encoding="utf-8"))
        assert meta["sites"]["attempted"] == 2
        assert meta["sites"]["succeeded"] == 1
        assert meta["sites"]["failed"] == 1
        assert meta["sites"]["by_status"]["network"] == 1


# --- collecting failures across runs for a second pass --------------------

def _run_report(tmp_path, run_id, rows):
    import json
    d = tmp_path / "runs" / run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "sites.json").write_text(json.dumps(rows), encoding="utf-8")


def test_failures_are_collected_across_every_run(tmp_path):
    from scrapbot.cli import _failed_hosts, build_parser, settings_from_args

    _run_report(tmp_path, "20260101T000000Z", [
        {"domain": "a.com", "status": "blocked"},
        {"domain": "b.com", "status": "network"},
        {"domain": "c.com", "status": "ok"},
    ])
    _run_report(tmp_path, "20260102T000000Z", [
        {"domain": "d.com", "status": "error"},
        {"domain": "e.com", "status": "no_directory"},
    ])
    settings = settings_from_args(
        build_parser().parse_args(["stats", "--data-dir", str(tmp_path)])
    )

    hosts, recovered = _failed_hosts(settings, ["blocked", "error", "network"])
    assert [h for h, _ in hosts] == ["a.com", "b.com", "d.com"]
    assert recovered == 0
    # no_directory is not a transient failure, so it is only picked up on request
    assert [h for h, _ in _failed_hosts(settings, ["no_directory"])[0]] == ["e.com"]


def test_a_host_that_later_succeeded_is_not_chased_again(tmp_path):
    """A site can fail in one batch and succeed on retry. A second pass that
    re-read only the failure files would keep hammering sites already done."""
    from scrapbot.cli import _failed_hosts, build_parser, settings_from_args

    _run_report(tmp_path, "20260101T000000Z", [{"domain": "a.com", "status": "blocked"}])
    _run_report(tmp_path, "20260102T000000Z", [{"domain": "a.com", "status": "ok"}])
    settings = settings_from_args(
        build_parser().parse_args(["stats", "--data-dir", str(tmp_path)])
    )

    hosts, recovered = _failed_hosts(settings, ["blocked"])
    assert hosts == [] and recovered == 1


def test_an_unreadable_run_report_does_not_stop_the_sweep(tmp_path):
    from scrapbot.cli import _failed_hosts, build_parser, settings_from_args

    _run_report(tmp_path, "20260101T000000Z", [{"domain": "a.com", "status": "blocked"}])
    broken = tmp_path / "runs" / "20260102T000000Z"
    broken.mkdir(parents=True, exist_ok=True)
    (broken / "sites.json").write_text("{not json", encoding="utf-8")
    settings = settings_from_args(
        build_parser().parse_args(["stats", "--data-dir", str(tmp_path)])
    )

    assert [h for h, _ in _failed_hosts(settings, ["blocked"])[0]] == ["a.com"]


def test_a_host_that_only_answers_as_www_is_still_found(monkeypatch):
    """baynorse.com resolves to the college's own box, which refuses
    connections; www.baynorse.com is a different server entirely. Stripping the
    www and stopping there reported a network failure when the real answer was
    a 403 — a different problem needing a different fix."""
    import asyncio
    from scrapbot.cli import build_parser, settings_from_args
    from scrapbot.net import NETWORK_FAILED, Page
    from scrapbot.sources.coaches import CoachesSource

    tried: list[str] = []

    class FakeFetcher:
        async def get(self, url):
            tried.append(url)
            if "www." in url:
                return Page(url=url, status=200, html="<html><body>ok</body></html>")
            return Page(url=url, status=NETWORK_FAILED, html="", error="no route")

    args = build_parser().parse_args(["stats"])
    source = CoachesSource(settings_from_args(args), args)
    seen: list[Page] = []
    base = asyncio.run(source._working_base(FakeFetcher(), "baynorse.com", seen))

    assert base == "https://www.baynorse.com"
    assert tried[0] == "https://baynorse.com/"        # bare form tried first
    assert any(u.startswith("https://www.") for u in tried)


def test_a_403_is_not_retried_under_www(monkeypatch):
    """A 403 means the host exists and is refusing us. Trying the other
    spelling would only collect a second 403 and slow every blocked site down."""
    import asyncio
    from scrapbot.cli import build_parser, settings_from_args
    from scrapbot.net import Page
    from scrapbot.sources.coaches import CoachesSource

    tried: list[str] = []

    class FakeFetcher:
        async def get(self, url):
            tried.append(url)
            return Page(url=url, status=403, html="")

    args = build_parser().parse_args(["stats"])
    source = CoachesSource(settings_from_args(args), args)
    assert asyncio.run(source._working_base(FakeFetcher(), "blocked.example", [])) is None
    assert tried == ["https://blocked.example/"]
