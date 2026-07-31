"""Reach for a browser when a page is reachable but parses to nobody.

``render=auto`` used to decide by measuring visible text: under 600 characters
meant "probably script-built, try a browser". That measures the page *chrome*,
not the staff table — a directory shell with a couple of paragraphs of blurb
sails past the threshold and is then read as a site with no directory on it.

Parsing to zero people is the sharper signal, and it is exactly what the
"site reachable but no staff directory found" outcome already means.
"""

from __future__ import annotations

import asyncio

import pytest

from scrapbot.cli import build_parser, settings_from_args
from scrapbot.net import _visible_text_length
from scrapbot.runner import run_source
from tests.fixtures import JS_BUILT_DIRECTORY, FixtureSite

JS_TEXT_THRESHOLD = 600


def _run(netloc: str, tmp_path, render: str):
    args = build_parser().parse_args([
        "run", "--data-dir", str(tmp_path), "--delay", "0", "--render", render,
        "coaches", "--directory-url", f"http://{netloc}/js-directory",
    ])
    settings = settings_from_args(args)
    return asyncio.run(run_source("coaches", args, settings, dry_run=True))


def test_the_shell_is_too_wordy_for_the_visible_text_heuristic():
    """If this ever drops below the threshold the fixture has stopped
    reproducing the bug, and the test below would pass for the wrong reason."""
    assert _visible_text_length(JS_BUILT_DIRECTORY) > JS_TEXT_THRESHOLD


def test_a_script_built_directory_yields_nobody_without_a_browser(tmp_path):
    with FixtureSite() as netloc:
        result = _run(netloc, tmp_path, "never")
    assert result.leads == []


@pytest.mark.browser
def test_a_script_built_directory_is_retried_in_a_browser(tmp_path):
    with FixtureSite() as netloc:
        result = _run(netloc, tmp_path, "auto")

    names = sorted(c.name for c in result.leads)
    assert names == [
        "Chris Vance",
        "Dana Reyes",
        "Jamie Fox",
        "Pat Oduya",
        "Robin Ellis",
        "Sam Webb",
    ]
    assert result.fetch_stats["rendered"] >= 1


@pytest.mark.browser
def test_render_never_still_refuses_to_open_a_browser(tmp_path):
    """`never` is the operator saying no browsers at all; the new trigger must
    not quietly override it."""
    with FixtureSite() as netloc:
        result = _run(netloc, tmp_path, "never")
    assert result.fetch_stats["rendered"] == 0
