# AI Diff Review Service (Python / FastAPI)

Same contract, same behavior as the Node version — this is a straight port,
re-tested end to end. See `CANDIDATE-TASK.md` for the full contract.

## Run locally

```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt

export BEARER_TOKEN=<pick-any-secret-string>
# optional, only needed for the llm provider path:
export ANTHROPIC_API_KEY=<your-key>

uvicorn app.main:app --host 0.0.0.0 --port 3000
```

## Environment variables

| Var | Required | Purpose |
|---|---|---|
| `BEARER_TOKEN` | yes | Clients must send `Authorization: Bearer <token>`. App refuses to start without it. |
| `ANTHROPIC_API_KEY` | no | Enables the `llm` provider. Without it, `provider: "llm"` jobs fail gracefully with a `failed` status and a clear error — never a crash. |

## Endpoints

- `GET /health`, `GET /spec` — public.
- `POST /v1/reviews` — submit a diff, returns `202 {jobId, status}`.
- `GET /v1/reviews/:jobId` — poll status/findings.
- `GET /v1/reviews/:jobId/stream` — SSE, replays full history then streams live.

All `/v1/*` routes require `Authorization: Bearer <BEARER_TOKEN>`.

## Deploying for the 48-hour scoring window

### Option A — Render.com (free tier)
1. Push this repo to GitHub.
2. Render → New → Web Service → connect the repo.
3. Build command: `pip install -r requirements.txt`.
   Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.
4. Add environment variable `BEARER_TOKEN` (and `ANTHROPIC_API_KEY` if wiring up llm).
5. Deploy. Render gives you `https://<name>.onrender.com`.
   Free tier sleeps after inactivity; the first probe after idle may be slow.

### Option B — Fly.io
```bash
fly launch --no-deploy
fly secrets set BEARER_TOKEN=<your-token>
fly deploy
```
(Fly's Python builder auto-detects `requirements.txt`; make sure the
generated `fly.toml` start command matches the uvicorn command above.)

### Option C — Tunnel your own machine
```bash
uvicorn app.main:app --host 0.0.0.0 --port 3000 &
cloudflared tunnel --url http://localhost:3000
# or: ngrok http 3000
```
Keep both processes running for the full 48-hour window.

Verify from a different network before submitting:
```bash
curl https://<your-url>/health
```

## Testing it yourself

```bash
curl https://<your-url>/health
curl https://<your-url>/spec
curl -X POST https://<your-url>/v1/reviews \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"diff":"diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n+console.log(1)"}'
```
