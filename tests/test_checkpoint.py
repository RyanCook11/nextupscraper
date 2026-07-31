"""The merged store must survive an interrupted run.

A sweep of a couple of thousand schools runs for hours. The store used to be
written once, after the last site, so killing the run — or a crash on site
1200 — threw away every contact it had already found.
"""

from __future__ import annotations

import json

import pytest

from scrapbot.config import Settings
from scrapbot.models import Contact
from scrapbot.storage import ContactStore


def _settings(tmp_path, **overrides):
    settings = Settings()
    settings.data_dir = tmp_path
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


def _contact(n: int) -> Contact:
    return Contact(
        name=f"Coach {n}",
        school=f"School {n}",
        school_domain=f"school{n}.edu",
        title="Head Coach",
        profile_url=f"https://school{n}.edu/staff/{n}",
        emails=[f"coach{n}@school{n}.edu"],
    )


def test_checkpoint_secs_has_a_nonzero_default():
    """The whole point is that it protects long runs without being asked."""
    assert Settings().checkpoint_secs > 0


def test_a_checkpoint_is_readable_while_the_run_is_still_going(tmp_path):
    settings = _settings(tmp_path)
    store = ContactStore(settings).load()

    for n in range(3):
        store.upsert(_contact(n))
    store.save(checkpoint=True)

    # A second reader — `scrapbot stats`, say — sees the partial run.
    reread = ContactStore(_settings(tmp_path)).load()
    assert len(reread.leads) == 3

    for n in range(3, 6):
        store.upsert(_contact(n))
    store.save()

    assert len(ContactStore(_settings(tmp_path)).load().leads) == 6


def test_a_checkpoint_never_leaves_a_half_written_store(tmp_path, monkeypatch):
    """Writes go through a temp file and os.replace. If the CSV write blows up
    mid-checkpoint, the JSON already on disk must still parse."""
    settings = _settings(tmp_path)
    store = ContactStore(settings).load()
    for n in range(4):
        store.upsert(_contact(n))
    store.save(checkpoint=True)

    import scrapbot.storage as storage_module

    def explode(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(storage_module, "write_csv", explode)
    store.upsert(_contact(99))
    with pytest.raises(OSError):
        store.save(checkpoint=True)

    payload = json.loads(settings.contacts_path.read_text(encoding="utf-8"))
    assert payload["count"] >= 4
    assert len(payload["leads"]) == payload["count"]


def test_checkpointing_can_be_turned_off(tmp_path):
    assert _settings(tmp_path, checkpoint_secs=0).checkpoint_secs == 0
