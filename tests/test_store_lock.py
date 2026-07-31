"""Only one run may write a data directory at a time.

Every run loads the whole store, merges in memory and writes it back. Two runs
sharing a ``--data-dir`` therefore do not interleave: the second to finish
overwrites the first's work completely, and with mid-run checkpointing they
clobber each other over and over. Nothing errors — both runs report success and
the contacts are simply gone. That happened here, and cost a full harvest.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from scrapbot.config import Settings
from scrapbot.cli import build_parser, settings_from_args
from scrapbot.runner import run_source
from scrapbot.storage import ContactStore, StoreBusy, StoreLock
from tests.fixtures import FixtureSite


def _settings(tmp_path) -> Settings:
    settings = Settings()
    settings.data_dir = tmp_path
    return settings


def test_a_second_lock_on_the_same_dir_is_refused(tmp_path):
    with StoreLock(_settings(tmp_path)):
        with pytest.raises(StoreBusy):
            with StoreLock(_settings(tmp_path)):
                pass


def test_the_lock_is_released_when_the_run_finishes(tmp_path):
    with StoreLock(_settings(tmp_path)):
        pass
    with StoreLock(_settings(tmp_path)):
        pass  # must not raise


def test_two_different_data_dirs_do_not_block_each_other(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    with StoreLock(_settings(a)), StoreLock(_settings(b)):
        pass


def test_a_killed_run_does_not_leave_the_store_locked(tmp_path):
    """The OS drops the lock when the descriptor closes, so there is no stale
    lock file to detect — which is the part a PID file gets wrong."""
    project_root = Path(__file__).resolve().parent.parent
    script = textwrap.dedent(f"""
        import sys, time
        from pathlib import Path
        sys.path.insert(0, {str(project_root)!r})
        from scrapbot.config import Settings
        from scrapbot.storage import StoreLock
        s = Settings(); s.data_dir = Path({str(tmp_path)!r})
        with StoreLock(s):
            print("locked", flush=True)
            time.sleep(30)
    """)
    proc = subprocess.Popen(
        [sys.executable, "-c", script], stdout=subprocess.PIPE, text=True
    )
    try:
        assert proc.stdout.readline().strip() == "locked"
        with pytest.raises(StoreBusy):
            with StoreLock(_settings(tmp_path)):
                pass
    finally:
        proc.kill()
        proc.wait(timeout=10)

    with StoreLock(_settings(tmp_path)):
        pass  # the kill released it


def _run(netloc: str, tmp_path, dry_run: bool = False):
    args = build_parser().parse_args([
        "run", "--data-dir", str(tmp_path), "--delay", "0", "--render", "never",
        "coaches", "--directory-url", f"http://{netloc}/staff-directory",
    ])
    return asyncio.run(
        run_source("coaches", args, settings_from_args(args), dry_run=dry_run)
    )


def test_a_run_refuses_to_start_while_another_holds_the_store(tmp_path):
    with FixtureSite() as netloc:
        with StoreLock(_settings(tmp_path)):
            with pytest.raises(StoreBusy):
                _run(netloc, tmp_path)


def test_a_dry_run_needs_no_lock(tmp_path):
    """It writes nothing, so it cannot clobber anything."""
    with FixtureSite() as netloc:
        with StoreLock(_settings(tmp_path)):
            result = _run(netloc, tmp_path, dry_run=True)
    assert result.leads


def test_a_normal_run_still_writes_and_releases(tmp_path):
    with FixtureSite() as netloc:
        result = _run(netloc, tmp_path)
    assert result.leads
    assert ContactStore(_settings(tmp_path)).load().leads
    with StoreLock(_settings(tmp_path)):
        pass  # released at the end of the run
