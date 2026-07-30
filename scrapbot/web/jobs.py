"""Background retry jobs for the dashboard.

A scrape takes seconds to minutes — crawl delays alone can be 30s per host — so
the retry endpoint starts a job and returns immediately. The browser polls for
progress.

Only one job runs at a time. Two concurrent retries of the same host would
fight each other's rate limiting, and the point of a retry is to be gentler,
not more aggressive.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..config import Settings
from ..runner import run_source

log = logging.getLogger("scrapbot.web.jobs")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Job:
    job_id: str
    domains: list[str]
    status: str = "running"  # running | done | failed
    started: str = field(default_factory=_utcnow)
    finished: str | None = None
    error: str | None = None
    run_id: str | None = None
    people: int = 0
    succeeded: int = 0
    failed: int = 0
    outcomes: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "domains": self.domains,
            "status": self.status,
            "started": self.started,
            "finished": self.finished,
            "error": self.error,
            "run_id": self.run_id,
            "people": self.people,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "outcomes": self.outcomes,
        }


class JobManager:
    """Runs at most one retry at a time and remembers what happened."""

    def __init__(self, settings: Settings, history: int = 20) -> None:
        self.settings = settings
        self.history = history
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._task: asyncio.Task | None = None

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def recent(self) -> list[Job]:
        return [self._jobs[j] for j in reversed(self._order) if j in self._jobs]

    def start(self, domains: list[str]) -> Job:
        if self.busy:
            raise RuntimeError("a retry is already running")

        job_id = datetime.now(timezone.utc).strftime("job-%Y%m%dT%H%M%SZ")
        # Same second twice — rare, but a collision would overwrite history.
        suffix = 1
        while job_id in self._jobs:
            suffix += 1
            job_id = f"{job_id.split('-r')[0]}-r{suffix}"

        job = Job(job_id=job_id, domains=domains)
        self._jobs[job_id] = job
        self._order.append(job_id)
        while len(self._order) > self.history:
            self._jobs.pop(self._order.pop(0), None)

        self._task = asyncio.create_task(self._run(job))
        return job

    async def _run(self, job: Job) -> None:
        try:
            args = argparse.Namespace(
                seeds=None,
                sites=list(job.domains),
                directory_url=[],
                coaches_only=False,
                sport=None,
                limit=0,
            )
            log.info("retry job %s: %s", job.job_id, ", ".join(job.domains))
            result = await run_source("coaches", args, self.settings)

            job.run_id = result.run_id
            job.people = len(result.leads)
            job.succeeded = len(result.succeeded)
            job.failed = len(result.failed)
            job.outcomes = [o.to_dict() for o in result.outcomes]
            job.status = "done"
        except Exception as exc:  # a failed retry must not take the server down
            log.exception("retry job %s failed", job.job_id)
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
        finally:
            job.finished = _utcnow()
