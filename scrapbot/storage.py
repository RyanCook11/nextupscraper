"""Persistence: a merged de-duplicated store plus per-run snapshots."""

from __future__ import annotations

import csv
import json
import logging
import os
from dataclasses import dataclass, field
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


class StoreBusy(RuntimeError):
    """Another run already holds the store."""


class StoreLock:
    """Exclusive advisory lock over one data directory.

    Every run loads the whole store, merges in memory and writes it back, so
    two runs against the same ``--data-dir`` do not interleave — the second to
    finish overwrites the first's work wholesale, and with mid-run checkpoints
    they clobber each other repeatedly. That is silent: both runs report
    success and the contacts simply are not there afterwards.

    The lock is taken by the operating system on an open descriptor, so a run
    that is killed or crashes releases it immediately. There is no stale lock
    file to detect or clean up, which is the part hand-rolled PID files get
    wrong.
    """

    def __init__(self, settings: Settings) -> None:
        self.path = settings.data_dir / ".scrapbot.lock"
        self._fd: int | None = None

    def __enter__(self) -> "StoreLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            _lock_exclusive(fd)
        except OSError as exc:
            os.close(fd)
            raise StoreBusy(
                f"another scrapbot run is using {self.path.parent} — "
                f"wait for it to finish, or pass a different --data-dir"
            ) from exc
        os.truncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._fd = fd
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._fd is None:
            return
        try:
            _unlock(self._fd)
        finally:
            os.close(self._fd)
            self._fd = None


def _lock_exclusive(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fd: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass  # closing the descriptor releases it anyway


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
        self.returned_count = 0

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
        if getattr(existing, "departed", False):
            # merge() clears the flag; count it here, while we can still see it.
            self.returned_count += 1
        self.leads[lead.key] = existing.merge(lead)
        self.updated_count += 1
        return "updated"

    def sorted_leads(self) -> list[Record]:
        return sorted(self.leads.values(), key=lambda lead: lead.key)

    def save(self, *, checkpoint: bool = False) -> None:
        """Write the merged store.

        Both files go out through :func:`_write_atomic`, so a mid-run
        checkpoint can never leave a half-written store behind: readers see
        either the previous version or the new one.
        """
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
        if checkpoint:
            log.info("checkpoint: %d %s(s) saved so far", len(leads), self.noun)
        else:
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

    def reconcile(
        self,
        rosters: dict[str, set[str]],
        *,
        max_loss: float = 0.5,
        floor: int = 5,
    ) -> "ReconcileReport":
        """Mark people the school no longer lists as departed.

        Merging alone only ever touches records that *were* scraped, so a
        coach who is fired just stops being updated and lingers forever. This
        closes that gap from the other side: for a school we scraped
        successfully, anyone in the store who was absent from the page has
        left the post.

        ``rosters`` must hold the complete membership of each site — only
        schools the run actually reached, and every person the directory
        listed, before any ``--coaches-only``-style filter. A school missing
        from it is left alone, which is what makes a blocked or failed site
        harmless.

        Records are flagged, never deleted: the email stays useful, and a
        scrape is evidence rather than proof.

        ``max_loss`` is the safety catch. A site redesign that halves what the
        parser recognises looks exactly like mass firing, and the difference
        matters far too much to guess — so if a school would lose more than
        this fraction of its people at once, the whole school is skipped and
        reported instead. ``floor`` exempts schools too small for a ratio to
        mean anything: with three people on file, one departure is 33% and
        two is 67%, and neither is suspicious.
        """
        report = ReconcileReport()
        by_domain: dict[str, list[Contact]] = {}
        for contact in self.leads.values():
            if not contact.departed:
                by_domain.setdefault(contact.school_domain.lower(), []).append(contact)

        for domain, seen in rosters.items():
            if not seen:
                continue  # nothing parsed: says nothing about who is still there
            stored = by_domain.get(domain, [])
            missing = [c for c in stored if c.key not in seen]
            if not missing:
                continue
            if len(stored) >= floor and len(missing) / len(stored) > max_loss:
                log.warning(
                    "%s: %d of %d stored people missing from this scrape (>%.0f%%) — "
                    "leaving them alone. A parser or site change is likelier than "
                    "that many departures; re-run with --reconcile-max-loss 1.0 if "
                    "the scrape is right.",
                    domain, len(missing), len(stored), max_loss * 100,
                )
                report.skipped[domain] = (len(missing), len(stored))
                continue
            marked = sum(1 for c in missing if c.mark_departed())
            if marked:
                report.departed[domain] = marked
                log.info("%s: %d person(s) no longer listed — marked departed", domain, marked)
        return report


@dataclass
class ReconcileReport:
    """What one reconcile pass changed, for the run summary."""

    departed: dict[str, int] = field(default_factory=dict)
    """School domain -> how many people were newly marked as gone."""
    skipped: dict[str, tuple[int, int]] = field(default_factory=dict)
    """School domain -> (missing, stored) for schools the safety catch spared."""

    @property
    def total(self) -> int:
        return sum(self.departed.values())

    def to_dict(self) -> dict:
        return {
            "departed": self.total,
            "schools_reconciled": len(self.departed),
            "by_school": self.departed,
            "skipped_suspicious": {
                domain: {"missing": missing, "stored": stored}
                for domain, (missing, stored) in self.skipped.items()
            },
        }


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
