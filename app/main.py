import asyncio
import hashlib
import json
import os
import time

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .chunker import CHUNK_BYTES, chunk_files
from .diff_parser import parse_diff
from .llm_provider import LlmUnavailableError, run_llm_provider
from .mock_provider import run_mock_provider
from .rate_limiter import TokenBucket
from .store import Store, hash_body, hash_payload

MAX_PAYLOAD_BYTES = 1048576
MAX_CONCURRENT_JOBS = 4
RATE_LIMIT_PER_MINUTE = 30
RATE_BURST = 30
VERSION = "1.0.0"

# Check environment variable without exposing secret value
print("BEARER_TOKEN exists:", bool(os.environ.get("BEARER_TOKEN")))

BEARER_TOKEN = os.environ.get("BEARER_TOKEN")

if not BEARER_TOKEN:
    raise RuntimeError("FATAL: BEARER_TOKEN env var must be set.")

START_TIME = time.time()

store = Store()
bucket = TokenBucket(RATE_LIMIT_PER_MINUTE, RATE_BURST)

running_count = 0
queue: asyncio.Queue = asyncio.Queue()
pump_lock = asyncio.Lock()

app = FastAPI()

def error_response(status_code: int, code: str, message: str, extra_headers: dict = None):
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
        headers=extra_headers or {},
    )


def check_auth(request: Request):
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer ") or auth[len("Bearer "):] != BEARER_TOKEN:
        return False
    return True


# ---------------- Public routes ----------------


@app.get("/health")
async def health():
    return {"status": "ok", "version": VERSION, "uptimeSeconds": int(time.time() - START_TIME)}


@app.get("/spec")
async def spec():
    return {
        "specVersion": "1.0",
        "providers": ["mock", "llm"],
        "limits": {
            "maxPayloadBytes": MAX_PAYLOAD_BYTES,
            "chunkBytes": CHUNK_BYTES,
            "maxConcurrentJobs": MAX_CONCURRENT_JOBS,
            "rateLimitPerMinute": RATE_LIMIT_PER_MINUTE,
        },
    }


# ---------------- POST /v1/reviews ----------------


@app.post("/v1/reviews")
async def create_review(request: Request):
    if not check_auth(request):
        return error_response(401, "unauthorized", "Missing or invalid bearer token")

    raw_body = await request.body()
    if len(raw_body) > MAX_PAYLOAD_BYTES:
        return error_response(413, "payload_too_large", "Request body exceeds 1 MiB limit")

    rl = bucket.try_consume()
    if not rl["allowed"]:
        return error_response(
            429,
            "rate_limited",
            "Too many submissions; slow down",
            extra_headers={"Retry-After": str(rl["retry_after_seconds"])},
        )

    try:
        body = json.loads(raw_body.decode("utf-8")) if raw_body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return error_response(400, "invalid_json", "Request body is not valid JSON")

    if not isinstance(body, dict):
        return error_response(400, "invalid_json", "Request body is not a JSON object")

    diff_text = body.get("diff")
    options = body.get("options") or {}
    provider = "llm" if options.get("provider") == "llm" else "mock"
    max_findings = options.get("maxFindings")
    if not isinstance(max_findings, int) or isinstance(max_findings, bool):
        max_findings = 100

    if not isinstance(diff_text, str) or diff_text.strip() == "":
        return error_response(422, "invalid_diff", "diff is missing or empty")

    parsed = parse_diff(diff_text)
    if not parsed.valid:
        return error_response(422, "invalid_diff", "diff could not be parsed as a unified diff")

    normalized_options = {"provider": provider, "maxFindings": max_findings}
    content_hash = hash_payload(diff_text, normalized_options)
    idempotency_key = request.headers.get("idempotency-key")

    pending_idem_key = None
    pending_body_hash = None

    if idempotency_key:
        body_hash = hash_body(body)
        check = store.check_idempotency(idempotency_key, body_hash)
        if check["conflict"]:
            return error_response(409, "idempotency_conflict", "Idempotency-Key reused with a different body")
        if check["job_id"]:
            existing = store.get_job(check["job_id"])
            return JSONResponse(status_code=202, content={"jobId": existing.job_id, "status": existing.status})
        pending_idem_key = idempotency_key
        pending_body_hash = body_hash

    cached = store.get_cached(content_hash)
    job = store.create_job(content_hash, provider)

    if pending_idem_key:
        store.record_idempotency(pending_idem_key, pending_body_hash, job.job_id)

    if cached:
        asyncio.create_task(resolve_from_cache(job, cached, max_findings))
    else:
        await queue.put(
            {
                "job_id": job.job_id,
                "diff": diff_text,
                "parsed": parsed,
                "provider": provider,
                "max_findings": max_findings,
            }
        )
        asyncio.create_task(pump())

    return JSONResponse(status_code=202, content={"jobId": job.job_id, "status": "queued"})


async def resolve_from_cache(job, cached, max_findings):
    store.set_running(job)
    truncated = cached["findings"][:max_findings]
    for f in truncated:
        store.add_finding(job, f)
    usage = dict(cached["usage"])
    usage["cacheHit"] = True
    store.complete(job, truncated, usage)


# ---------------- GET /v1/reviews/{job_id} ----------------


@app.get("/v1/reviews/{job_id}")
async def get_review(job_id: str, request: Request):
    if not check_auth(request):
        return error_response(401, "unauthorized", "Missing or invalid bearer token")

    job = store.get_job(job_id)
    if not job:
        return error_response(404, "not_found", "Unknown jobId")

    payload = {"jobId": job.job_id, "status": job.status}
    if job.status == "done":
        payload["findings"] = job.findings
        payload["usage"] = job.usage
    elif job.status == "failed":
        payload["error"] = job.error
        payload["usage"] = job.usage or {"inputBytes": 0, "chunks": 0, "cacheHit": False}

    return JSONResponse(status_code=200, content=payload)


# ---------------- GET /v1/reviews/{job_id}/stream (SSE) ----------------


@app.get("/v1/reviews/{job_id}/stream")
async def stream_review(job_id: str, request: Request):
    if not check_auth(request):
        return error_response(401, "unauthorized", "Missing or invalid bearer token")

    job = store.get_job(job_id)
    if not job:
        return error_response(404, "not_found", "Unknown jobId")

    async def event_gen():
        for entry in list(job.event_log):
            yield f"event: {entry['event']}\ndata: {json.dumps(entry['data'])}\n\n"

        if job.status in ("done", "failed"):
            return

        q = store.subscribe(job)
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    item = await asyncio.wait_for(q.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                yield f"event: {item['event']}\ndata: {json.dumps(item['data'])}\n\n"
                if item["event"] == "done":
                    break
        finally:
            store.unsubscribe(job, q)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


# ---------------- Job processing (concurrency-limited queue) ----------------


async def pump():
    global running_count
    async with pump_lock:
        while running_count < MAX_CONCURRENT_JOBS and not queue.empty():
            task = queue.get_nowait()
            running_count += 1
            asyncio.create_task(run_and_release(task))


async def run_and_release(task):
    global running_count
    try:
        await process_job(task)
    finally:
        running_count -= 1
        asyncio.create_task(pump())


async def process_job(task):
    job = store.get_job(task["job_id"])
    if not job:
        return
    store.set_running(job)

    diff_text = task["diff"]
    parsed = task["parsed"]
    provider = task["provider"]
    max_findings = task["max_findings"]

    chunks = chunk_files(parsed.files)
    input_bytes = len(diff_text.encode("utf-8"))
    usage = {"inputBytes": input_bytes, "chunks": len(chunks), "cacheHit": False}

    try:
        if provider == "mock":
            findings = run_mock_provider(parsed.files)
        else:
            findings = await run_llm_provider(parsed.files, diff_text)

        truncated = findings[:max_findings]
        for f in truncated:
            store.add_finding(job, f)
        store.complete(job, truncated, usage)
    except LlmUnavailableError as err:
        job.usage = usage
        store.fail(job, str(err), "internal")
    except Exception as err:  # noqa: BLE001 - never crash the process
        job.usage = usage
        store.fail(job, f"Unexpected error: {err}", "internal")
