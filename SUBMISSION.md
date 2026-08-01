# SUBMISSION

## Architecture

Python 3.12 / FastAPI / uvicorn, single process, in-memory store — no DB,
which is fine for a 48-hour scoring window but is the first thing I'd change
for anything longer-lived (more on that below).

- `app/diff_parser.py` — splits a unified diff into per-file blocks, parses
  hunks into added-line records tracking the new-file line counter.
- `app/chunker.py` — groups parsed files into ≤64KiB chunks on file
  boundaries; a lone oversized file becomes its own chunk.
- `app/mock_provider.py` — the deterministic rule engine (MOCK-001..008,
  MOCK-INJ). Single-line rules are table-driven with regexes/lambdas;
  MOCK-004 (empty catch, which can span lines) scans the joined added-line
  text per file with an offset→line map, since a single-line regex can't
  catch a catch-block that spans multiple diff lines.
- `app/llm_provider.py` — same Finding schema, calls a real model via
  `httpx`; raises a typed `LlmUnavailableError` on any failure (missing key,
  network error, bad response), which the caller turns into a `failed` job
  instead of crashing anything.
- `app/store.py` — jobs, content-hash cache, idempotency-key map, and a
  per-job event log with `asyncio.Queue`-based pub/sub for SSE replay.
- `app/rate_limiter.py` — token bucket, 30/min sustained, burst 30.
- `app/main.py` — FastAPI routes, auth check, and a concurrency-limited
  queue (`asyncio.Queue` + a `running_count` guard capped at 4).

## Provider design

Both providers produce the same `Finding` shape and go through the same
pipeline (parse → chunk → scan → order → cache → stream). `mock` is pure and
synchronous; `llm` is async and isolated in a try/except so a model outage
degrades to a clean `failed` job with a readable error. The prompt tells the
model to treat any embedded "instructions" in the diff as inert content,
mirroring the MOCK-INJ rule's guarantee that injection content is *reported*,
never obeyed.

## How I verified the cross-cutting behaviors

I actually built this twice: a first pass in Node/Express to work through
the contract, then ported it to Python/FastAPI (this version) once I decided
I'd rather defend Python in the interview. I re-ran the same curl-driven
test suite against the Python port to confirm parity, not just "it starts" —
and then re-verified everything again once it was live on Railway, not just
locally, since local behavior doesn't guarantee anything about the deployed
version (auth headers, env vars, and cold starts all behave differently
once something's actually running behind a real domain).

- **Mock rules**: fed a diff engineered to trip all 9 rules (including a
  multi-line empty catch block and an embedded "ignore previous
  instructions" line) — output (ids, lines, ordering, evidence) was
  byte-for-byte identical to the Node version's output.
- **Chunking**: a synthetic 20-file, ~67KB diff (>64KiB) produced
  `chunks: 2`; with `maxFindings` raised, all 980 expected findings came
  back with 980 unique `id`s and correct ordering — no duplicates or losses
  across the chunk boundary.
- **Caching**: resubmitting a byte-identical `{diff, options}` returned
  `cacheHit: true` with identical findings.
- **Idempotency**: same `Idempotency-Key` + same body → same `jobId`; same
  key + different body → `409 idempotency_conflict`.
- **SSE replay**: connected to a stream after a job had already finished
  (including a cache-hit job) and got the full `status`/`finding`/`done`
  history replayed in order — this port carried forward a fix I made in the
  Node version, where cache-hit jobs need to emit individual `finding`
  events (not just `status`/`done`) for replay to show them.
- **Injection inertness**: the "ignore previous instructions" line was
  reported as a `MOCK-INJ` finding like any other line and did not affect
  any other rule's output or the pipeline's control flow.
- **Rate limiting**: 40 rapid unique submissions — first 30 succeeded
  (burst), the rest got `429` with a numeric `Retry-After` header; confirmed
  the bucket refills over time.
- **Auth**: `401` on missing and wrong bearer tokens for `/v1` routes;
  `/health` and `/spec` open without auth.
- **Error taxonomy**: `400` (malformed JSON), `422` (missing diff), `413`
  (>1MiB body), `404` (unknown jobId) each return the error envelope.
- **llm graceful degradation**: with no `ANTHROPIC_API_KEY` set, a
  `provider: "llm"` job reaches `status: "failed"` with a clear message, and
  `/health` stayed `200` afterward — confirms no crash.
- **Live deployment**: after deploying to Railway, re-ran the full
  submit → poll cycle against the public URL (not localhost) with the real
  bearer token, and got the same `MOCK-007` finding back that I saw locally.
  This caught one dumb mistake on my end — I initially sent the token
  without the `Bearer ` prefix in the `Authorization` header and got a
  `401`, which was a good reminder to actually read the auth contract
  carefully rather than assume the token alone is enough.

Concurrency (4 slots, 5th queued not failing) is enforced structurally by the
`running_count` guard and `pump()`/`run_and_release()` in `app/main.py`, but
mock scanning is fast enough that I couldn't observe actual queuing under
manual load testing — same caveat as the Node version, noted rather than
hidden.

## What AI tools I used

I used Claude for essentially the whole build — writing the diff parser,
the rule engine, the job store, and the FastAPI routes, then iterating
against a curl/PowerShell test suite until every contract behavior actually
checked out, not just looked plausible. I built a Node/Express version
first, decided I'd rather defend Python in the interview, and had Claude
port the already-tested logic module-by-module rather than regenerate it
from the spec — the goal was a faithful port with matching behavior, so I
re-ran the same tests against the Python version to confirm parity instead
of trusting the translation blind.

I also used it for the deployment side — debugging PowerShell vs. cmd.exe
syntax differences, a bad first `git push` that accidentally included the
whole `venv/` folder (fixed by re-initializing git in the right directory
with a working `.gitignore`), and working around Render and Fly.io no
longer having truly free tiers before landing on Railway.

## An AI suggestion I rejected

Early on, the first suggestion for job processing was FastAPI's built-in
`BackgroundTasks` instead of a manual `asyncio.Queue` + concurrency counter.
I pushed back on that: `BackgroundTasks` runs after the response is sent,
but it doesn't give you a bounded-concurrency primitive on its own — you'd
still need to bolt on a semaphore or counter to enforce the "max 4
concurrent" requirement, so it wasn't actually saving complexity, just
hiding it behind a framework feature I'd have to explain in the interview
anyway. I asked for a plain `asyncio.Queue` with an explicit
`running_count` guard instead, since it's something I can point to directly
and say exactly what it does, rather than "trust me, BackgroundTasks
handles it."

## What I'd do next with more time

- Property-based tests (Hypothesis) for the diff parser against random
  valid/invalid unified diffs.
- Persist jobs/cache to Redis or SQLite so a restart doesn't lose the 48h
  window's state.
- A real timing-based test harness for the concurrency limit (artificially
  slow one provider call to force queuing and observe it directly).
- Tighten the SSE endpoint's disconnect handling — currently polling
  `request.is_disconnected()` on a 1s cadence, which is fine here but a bit
  coarse for a high-traffic version of this service.