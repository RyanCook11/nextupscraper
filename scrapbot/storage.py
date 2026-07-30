"""Persistence: a merged de-duplicated store plus per-run snapshots."""

from __future__ import annotations

import csv
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .config import Settings
from .models import CSV_COLUMNS, Contact, Lead, School, SiteOutcome

log = logging.getLogger("scrapbot.storage")

# Anything with .key/.to_dict()/.from_dict()/.to_row()/.merge().
Record = Lead | Contact | School


def run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def write_json(path: Path, payload: object) -> None:
    _write_atomic(path, json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False))


def write_csv(path: Path, records: Iterable[Record], columns: list[str] | None = None) -> None:
    records = list(records)
    if columns is None:
        # Header comes from the record type, so a contacts CSV isn't written
        # with company columns. Empty file falls back to the company shape.
        columns = records[0].COLUMNS if records else CSV_COLUMNS
    path.parent.mkdir(parents=True, exist_ok=True)
    # newline="" is required so csv doesn't emit \r\r\n on Windows.
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for record in records:
            writer.writerow(record.to_row())


class RecordStore:
    """A merged, de-duplicated JSON+CSV store keyed by ``record.key``.

    Subclasses pick the record class and the pair of files it lives in.
    """

    record_cls: type[Record] = Lead
    noun = "lead"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.leads: dict[str, Record] = {}
        self.new_count = 0
        self.updated_count = 0

    # -- where this store lives -------------------------------------------
    @property
    def json_path(self) -> Path:
        return self.settings.store_path

    @property
    def csv_path(self) -> Path:
        return self.settings.store_csv_path

    def load(self) -> "RecordStore":
        path = self.json_path
        if not path.exists():
            return self
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            backup = path.with_suffix(".corrupt.json")
            log.warning("could not read %s (%s); moving it to %s", path, exc, backup.name)
            os.replace(path, backup)
            return self
        records = raw.get("leads", raw) if isinstance(raw, dict) else raw
        for item in records or []:
            try:
                lead = self.record_cls.from_dict(item)
            except TypeError:
                continue
            self.leads[lead.key] = lead
        log.info("loaded %d existing %s(s)", len(self.leads), self.noun)
        return self

    def upsert(self, lead: Record) -> str:
        existing = self.leads.get(lead.key)
        if existing is None:
            self.leads[lead.key] = lead
            self.new_count += 1
            return "new"
        self.leads[lead.key] = existing.merge(lead)
        self.updated_count += 1
        return "updated"

    def sorted_leads(self) -> list[Record]:
        return sorted(self.leads.values(), key=lambda lead: lead.key)

    def save(self) -> None:
        self.settings.ensure_dirs()
        leads = self.sorted_leads()
        write_json(
            self.json_path,
            {
                "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "count": len(leads),
                "leads": [lead.to_dict() for lead in leads],
            },
        )
        write_csv(self.csv_path, leads, self.record_cls.COLUMNS)
        log.info("store now holds %d %s(s) -> %s", len(leads), self.noun, self.json_path)


class LeadStore(RecordStore):
    """The merged company store at ``data/leads.json``, keyed by domain."""

    record_cls = Lead
    noun = "lead"


class ContactStore(RecordStore):
    """The merged person store at ``data/contacts.json``, keyed by profile URL."""

    record_cls = Contact
    noun = "contact"

    @property
    def json_path(self) -> Path:
        return self.settings.contacts_path

    @property
    def csv_path(self) -> Path:
        return self.settings.contacts_csv_path


class SchoolStore(RecordStore):
    """The merged institution store at ``data/schools.json``."""

    record_cls = School
    noun = "school"

    @property
    def json_path(self) -> Path:
        return self.settings.schools_path

    @property
    def csv_path(self) -> Path:
        return self.settings.schools_csv_path


_STEMS = {Contact: "contacts", School: "schools"}


def save_run(
    settings: Settings,
    rid: str,
    leads: list[Record],
    meta: dict,
    outcomes: list[SiteOutcome] | None = None,
) -> Path:
    """Write an immutable snapshot of just this run."""
    out_dir = settings.runs_dir / rid
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = _STEMS.get(type(leads[0]), "leads") if leads else "leads"
    write_json(out_dir / f"{stem}.json", [lead.to_dict() for lead in leads])
    write_csv(out_dir / f"{stem}.csv", leads)
    write_json(out_dir / "meta.json", meta)
    if outcomes:
        write_outcomes(out_dir, outcomes)
    return out_dir


def write_outcomes(out_dir: Path, outcomes: list[SiteOutcome]) -> None:
    """Per-site results: the full report, plus a ready-to-use retry seed file.

    ``failed.txt`` is written in seed-file format on purpose, so a failed run
    can be retried with ``scrapbot run coaches --seeds <run>/failed.txt``.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    write_json(out_dir / "sites.json", [o.to_dict() for o in outcomes])

    succeeded = [o for o in outcomes if o.ok]
    failed = [o for o in outcomes if not o.ok]

    _write_atomic(
        out_dir / "succeeded.txt",
        "\n".join(
            ["# Sites scraped successfully in this run.", ""]
            + [f"{o.domain}  # {o.people} people" for o in succeeded]
        )
        + "\n",
    )
    _write_atomic(
        out_dir / "failed.txt",
        "\n".join(
            [
                "# Sites that produced nothing, and why.",
                "# Retry with: scrapbot run coaches --seeds this-file",
                "",
            ]
            + [f"{o.domain}  # {o.status}: {o.detail}" for o in failed]
        )
        + "\n",
    )
