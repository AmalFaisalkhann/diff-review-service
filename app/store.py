"""In-memory store: jobs, content-hash cache, idempotency keys, and a
per-job event log + async pub/sub for SSE streaming with replay."""

import asyncio
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field


def hash_payload(diff: str, options: dict) -> str:
    normalized = json.dumps({"diff": diff, "options": options or {}}, sort_keys=True)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def hash_body(body: dict) -> str:
    normalized = json.dumps(body, sort_keys=True)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass
class Job:
    job_id: str
    status: str = "queued"
    findings: list = None
    usage: dict = None
    error: dict = None
    provider: str = "mock"
    content_hash: str = None
    event_log: list = field(default_factory=list)
    subscribers: list = field(default_factory=list)  # list[asyncio.Queue]
    created_at: float = field(default_factory=time.time)


class Store:
    def __init__(self):
        self.jobs: dict[str, Job] = {}
        self.content_cache: dict[str, dict] = {}
        self.idempotency: dict[str, dict] = {}

    def create_job(self, content_hash: str, provider: str) -> Job:
        job_id = str(uuid.uuid4())
        job = Job(job_id=job_id, content_hash=content_hash, provider=provider)
        self.jobs[job_id] = job
        self._log_event(job, "status", {"status": "queued"})
        return job

    def get_job(self, job_id: str):
        return self.jobs.get(job_id)

    def set_running(self, job: Job):
        job.status = "running"
        self._log_event(job, "status", {"status": "running"})

    def add_finding(self, job: Job, finding: dict):
        self._log_event(job, "finding", finding)

    def complete(self, job: Job, findings: list, usage: dict):
        job.status = "done"
        job.findings = findings
        job.usage = usage
        self._log_event(job, "done", {"total": len(findings), "usage": usage})
        if job.content_hash:
            cached_usage = dict(usage)
            cached_usage["cacheHit"] = True
            self.content_cache[job.content_hash] = {"findings": findings, "usage": cached_usage}

    def fail(self, job: Job, message: str, code: str = "internal"):
        job.status = "failed"
        job.error = {"code": code, "message": message}
        self._log_event(job, "status", {"status": "failed", "error": job.error})
        self._log_event(
            job,
            "done",
            {"total": 0, "usage": job.usage or {"inputBytes": 0, "chunks": 0, "cacheHit": False}},
        )

    def get_cached(self, content_hash: str):
        return self.content_cache.get(content_hash)

    def check_idempotency(self, key: str, body_hash: str):
        existing = self.idempotency.get(key)
        if not existing:
            return {"conflict": False, "job_id": None}
        if existing["bodyHash"] == body_hash:
            return {"conflict": False, "job_id": existing["jobId"]}
        return {"conflict": True, "job_id": None}

    def record_idempotency(self, key: str, body_hash: str, job_id: str):
        self.idempotency[key] = {"bodyHash": body_hash, "jobId": job_id}

    def _log_event(self, job: Job, event: str, data: dict):
        job.event_log.append({"event": event, "data": data})
        for q in list(job.subscribers):
            q.put_nowait({"event": event, "data": data})

    def subscribe(self, job: Job) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        job.subscribers.append(q)
        return q

    def unsubscribe(self, job: Job, q: asyncio.Queue):
        if q in job.subscribers:
            job.subscribers.remove(q)
