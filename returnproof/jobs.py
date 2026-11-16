"""Small in-process background job runner.

The interface is deliberately queue-shaped: a job has an id, org, status and a
JSON-serialisable result. Swapping this for Redis/Celery later should not change
the HTTP contract in :mod:`returnproof.api.job_routes`.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("returnproof.jobs")


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class Job:
    id: str
    name: str
    org_id: int
    status: str = "queued"
    submitted_at: datetime = field(default_factory=_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: dict[str, Any] | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "submitted_at": self.submitted_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "result": self.result,
            "error": self.error,
        }


class InProcessJobRunner:
    def __init__(self, max_workers: int = 2) -> None:
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="returnproof-job")
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def submit(
        self,
        name: str,
        fn: Callable[..., dict[str, Any]],
        *args: Any,
        owner_org_id: int,
        **kwargs: Any,
    ) -> Job:
        job = Job(id=uuid.uuid4().hex, name=name, org_id=owner_org_id)
        with self._lock:
            self._jobs[job.id] = job
        self._executor.submit(self._run, job, fn, args, kwargs)
        return job

    def _run(
        self,
        job: Job,
        fn: Callable[..., dict[str, Any]],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> None:
        with self._lock:
            job.status = "running"
            job.started_at = _now()
        try:
            result = fn(*args, **kwargs)
            with self._lock:
                job.status = "succeeded"
                job.result = result
                job.finished_at = _now()
            logger.info("job_succeeded", extra={"job_id": job.id, "job_name": job.name})
        except Exception as exc:  # the failure is reported through the job record
            logger.exception("job_failed", extra={"job_id": job.id, "job_name": job.name})
            with self._lock:
                job.status = "failed"
                job.error = str(exc)
                job.finished_at = _now()

    def get(self, job_id: str, *, org_id: int | None = None) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None or (org_id is not None and job.org_id != org_id):
            return None
        return job

    def list_for_org(self, org_id: int) -> list[Job]:
        with self._lock:
            return [job for job in self._jobs.values() if job.org_id == org_id]

    def wait(self, job_id: str, timeout: float = 10.0) -> Job | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.get(job_id)
            if job is not None and job.status in {"succeeded", "failed"}:
                return job
            time.sleep(0.05)
        return self.get(job_id)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
