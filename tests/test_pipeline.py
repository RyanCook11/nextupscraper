"""End-to-end: crawl the fixture site, persist, re-run, export."""

from __future__ import annotations

import json

from scrapbot.cli import build_parser, filter_leads, settings_from_args
from scrapbot.models import Lead
from scrapbot.runner import run_source
from scrapbot.storage import LeadStore
from tests.fixtures import FixtureSite

import asyncio


def _run(argv: list[str]):
    args = build_parser().parse_args(argv)
    settings = settings_from_args(args)
    dry_run = getattr(args, "dry_run", False)
    result = asyncio.run(run_source(args.source, args, settings, dry_run=dry_run))
    return result, settings


def test_full_crawl_persists_and_merges(tmp_path):
    with FixtureSite() as netloc:
        argv = [
            "run", "--data-dir", str(tmp_path), "--delay", "0", "--render", "never",
            "website", "--domains", netloc,
        ]
        result, settings = _run(argv)

        assert len(result.leads) == 1
        lead = result.leads[0]
        assert lead.company_name == "Acme Scaffolding"
        assert lead.pages_crawled >= 3  # home + contact + about/careers
        assert "info@acme-scaffolding.com.au" in lead.emails
        assert lead.phones
        assert lead.location == "12 Vale Rd, Parramatta, NSW, 2150, AU"
        assert "construction" in lead.industry_hints
        assert lead.socials["linkedin"].endswith("/company/acme-scaffolding")
        assert lead.careers_url and lead.careers_url.endswith("/careers")
        assert lead.has_open_roles is True
        assert result.new == 1 and result.updated == 0

        # store + csv + run snapshot all landed
        assert settings.store_path.exists()
        assert settings.store_csv_path.exists()
        assert result.out_dir is not None and (result.out_dir / "meta.json").exists()
        stored = json.loads(settings.store_path.read_text(encoding="utf-8"))
        assert stored["count"] == 1

        csv_text = settings.store_csv_path.read_text(encoding="utf-8-sig")
        assert "info@acme-scaffolding.com.au" in csv_text
        assert csv_text.splitlines()[0].startswith("domain,company_name")

        # second run updates rather than duplicating
        result2, settings2 = _run(argv)
        assert result2.new == 0 and result2.updated == 1
        assert json.loads(settings2.store_path.read_text(encoding="utf-8"))["count"] == 1


def test_directory_source_discovers_and_filters_noise(tmp_path):
    with FixtureSite() as netloc:
        result, _ = _run([
            "run", "--data-dir", str(tmp_path), "--delay", "0", "--render", "never",
            "directory", "--listing", f"http://{netloc}/members", "--discover-only",
        ])
    domains = sorted(lead.domain for lead in result.leads)
    assert domains == ["acme-scaffolding.com.au", "globex-freight.com.au"]


def test_dry_run_writes_nothing(tmp_path):
    with FixtureSite() as netloc:
        result, settings = _run([
            "run", "--data-dir", str(tmp_path), "--delay", "0", "--render", "never", "--dry-run",
            "website", "--domains", netloc,
        ])
    assert result.leads and result.out_dir is None
    assert not settings.store_path.exists()


def test_robots_disallow_is_respected(tmp_path):
    """The fixture disallows /admin; a seed pointing there yields nothing."""
    with FixtureSite() as netloc:
        parser = build_parser()
        args = parser.parse_args([
            "run", "--data-dir", str(tmp_path), "--delay", "0", "--render", "never",
            "website", "--domains", netloc,
        ])
        settings = settings_from_args(args)
        from scrapbot.net import Fetcher

        async def check():
            async with Fetcher(settings) as fetcher:
                blocked = await fetcher.get(f"http://{netloc}/admin")
                allowed = await fetcher.get(f"http://{netloc}/about")
                return blocked, allowed

        blocked, allowed = asyncio.run(check())
        assert blocked.status == 999 and not blocked.ok
        assert allowed.ok


def test_common_flags_survive_the_source_subparser():
    args = build_parser().parse_args(
        ["run", "--delay", "3", "--concurrency", "9", "website", "--domains", "acme.com"]
    )
    settings = settings_from_args(args)
    assert settings.delay == 3 and settings.concurrency == 9
    # ...and still work when given after the source name
    args2 = build_parser().parse_args(
        ["run", "website", "--domains", "acme.com", "--delay", "7", "--max-pages", "2"]
    )
    settings2 = settings_from_args(args2)
    assert settings2.delay == 7 and settings2.max_pages_per_site == 2


def test_merge_unions_collections_and_keeps_history():
    old = Lead(domain="a.com", emails=["a@a.com"], phones=["1"], first_seen="2020-01-01T00:00:00+00:00",
               last_seen="2020-01-01T00:00:00+00:00", company_name="Old", pages_crawled=3)
    new = Lead(domain="a.com", emails=["b@a.com"], company_name="New", pages_crawled=1,
               first_seen="2026-01-01T00:00:00+00:00", last_seen="2026-01-01T00:00:00+00:00")
    merged = old.merge(new)
    assert merged.emails == ["a@a.com", "b@a.com"]
    assert merged.phones == ["1"]           # not lost when the new scrape misses it
    assert merged.company_name == "New"     # newer non-empty scalar wins
    assert merged.pages_crawled == 3
    assert merged.first_seen.startswith("2020") and merged.last_seen.startswith("2026")


def test_filters():
    leads = [
        Lead(domain="a.com", emails=["a@a.com"], industry_hints=["construction"]),
        Lead(domain="b.com", phones=["123"], industry_hints=["healthcare"]),
    ]
    assert [l.domain for l in filter_leads(leads, has_email=True)] == ["a.com"]
    assert [l.domain for l in filter_leads(leads, has_phone=True)] == ["b.com"]
    assert [l.domain for l in filter_leads(leads, industry="health")] == ["b.com"]
    assert [l.domain for l in filter_leads(leads, search="A@A.COM")] == ["a.com"]


def test_store_recovers_from_corrupt_json(tmp_path):
    args = build_parser().parse_args(["stats", "--data-dir", str(tmp_path)])
    settings = settings_from_args(args)
    settings.ensure_dirs()
    settings.store_path.write_text("{not json", encoding="utf-8")
    store = LeadStore(settings).load()
    assert store.leads == {}
    assert settings.store_path.with_suffix(".corrupt.json").exists()
