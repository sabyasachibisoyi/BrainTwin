# Phase 4.0.6 M.1 — Local container smoke test

**Purpose:** verify the M.1 deliverables (bearer-token auth + Dockerfile +
docker-compose) actually work on your Mac before any cloud step.

Total time: ~10 min once the image is built (first build is slower —
torch + chromadb + sentence-transformers add up to ~3 GB).

> **You are here:** local Docker only. No EC2, no Cloudflare, no
> domain, no Litestream. The next milestone (M.2) introduces the CDK
> repo and the cloud topology.

---

## 0. Prereqs

- Docker Desktop running (`docker info` returns no errors)
- The branch with M.1 checked out on `BrainTwin/`
- A 30+ char random string for `BACKEND_BEARER_TOKEN`
- Your `ANTHROPIC_API_KEY` ready (for the recall path to be useful;
  the auth gate itself works without it)
- Chrome with the unpacked extension already loaded from
  `chrome://extensions → Load unpacked → extension/`

---

## 1. Configure `.env`

```bash
cd /Users/sabyasachibisoyi/Desktop/LLM/BrainTwin
cp .env.example .env

# Generate a strong token and paste it into .env's BACKEND_BEARER_TOKEN line:
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Edit `.env`:

```
BACKEND_BEARER_TOKEN=<the string you just generated>
ANTHROPIC_API_KEY=sk-ant-…
```

Leave `TELEGRAM_BOT_TOKEN` and `ALLOWED_TELEGRAM_USER_IDS` empty if
you're not testing the bot today.

---

## 2. Build the image

```bash
docker compose build
```

First build: ~5–8 min on your Mac. Subsequent rebuilds with code-only
changes (no `requirements.txt` edit): ~30 sec thanks to layer caching.

Verify:

```bash
docker images | grep braintwin
# braintwin   local   <sha>   …   ~3GB
```

If the image is much larger than ~3 GB, something pulled in a CUDA
wheel — check `requirements.txt` is using the CPU torch pin (it is, as
of M.1).

---

## 3. Boot the app container

```bash
docker compose up app
```

First-boot startup logs to look for:

```
braintwin-app  |  INFO  uvicorn.error: Uvicorn running on http://0.0.0.0:8000
braintwin-app  |  INFO  backend.main: ...
```

Wait for the container to go `healthy` (`docker compose ps` shows
status):

```bash
docker compose ps
# NAME           STATUS                  PORTS
# braintwin-app  Up 30s (healthy)        127.0.0.1:8000->8000/tcp
```

If status is `unhealthy` after ~1 min:

```bash
docker compose logs app | tail -50
```

Common failures:

- `auth not configured` on /health — should NOT happen; /health is
  public. If you see this, the dep is over-broad — file a bug.
- ModuleNotFoundError — rebuild without cache: `docker compose build --no-cache app`
- `[Errno 13] Permission denied: '/data/...'` — volume ownership
  mismatch; nuke with `docker compose down -v` then re-up.

---

## 4. Verify the auth gate from curl

### /health is public

```bash
curl -i http://127.0.0.1:8000/health
```

Expected: `HTTP/1.1 200 OK`, JSON body with `status: ok`.

### / is public

```bash
curl -s http://127.0.0.1:8000/ | python -m json.tool
```

Expected: `name: DigitalTwin`, status: running.

### /recall without token → 401

```bash
curl -i -X POST http://127.0.0.1:8000/recall \
  -H 'Content-Type: application/json' \
  -d '{"query": "anything"}'
```

Expected: `HTTP/1.1 401 Unauthorized`, body `{"detail": "bearer token required"}`,
`WWW-Authenticate: Bearer` header.

### /recall with wrong token → 401

```bash
curl -i -X POST http://127.0.0.1:8000/recall \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer this-is-not-the-token' \
  -d '{"query": "anything"}'
```

Expected: `HTTP/1.1 401 Unauthorized`, body `{"detail": "bearer token invalid"}`.

### /recall with the right token → 200 (or 503)

```bash
TOKEN=$(grep '^BACKEND_BEARER_TOKEN=' .env | cut -d= -f2-)
curl -i -X POST http://127.0.0.1:8000/recall \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${TOKEN}" \
  -d '{"query": "anything"}'
```

Expected:

- `200 OK` with a RecallResponse JSON, OR
- `503` with `detail: "Recall agent not initialized — set ANTHROPIC_API_KEY"`
  if you skipped putting the key in `.env`. **This 503 path is fine
  for M.1 — the auth gate is what we're proving today, not the
  recall agent.**

### /capture without token → 401

```bash
curl -i -X POST http://127.0.0.1:8000/capture \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com","title":"x","text":"y","client":"chrome","timestamp":"2026-06-11T12:00:00Z","dwell_time_seconds":42}'
```

Expected: 401.

### /capture with the right token → 200

```bash
curl -i -X POST http://127.0.0.1:8000/capture \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${TOKEN}" \
  -d '{"url":"https://example.com","title":"smoke","text":"hello from M.1","client":"chrome","timestamp":"2026-06-11T12:00:00Z","dwell_time_seconds":42}'
```

Expected: `200 OK` with `{"status":"captured","capture_id":"…","persisted":...}`.

---

## 5. Verify the Chrome extension talks to the container

1. Open the extension popup (toolbar icon)
2. Open the popup's DevTools: right-click the popup → "Inspect"
3. In the console, set the bearer:

   ```js
   await chrome.storage.local.set({ bearerToken: '<paste your token>' });
   ```

4. Confirm:

   ```js
   await chrome.storage.local.get('bearerToken');
   // → { bearerToken: '…' }
   ```

5. Switch to the **Remember** tab in the popup
6. Type any query, submit

Expected:

- If `ANTHROPIC_API_KEY` is configured: real results render
- If not: the popup shows
  *"The recall agent isn't running. Set ANTHROPIC_API_KEY in .env and restart the backend."*
- **Critically, NO 401 errors** — meaning the bearer made it through.

### Negative test — clear the token

```js
await chrome.storage.local.remove('bearerToken');
```

Submit a query again. Expected: popup shows
*"Bearer token missing or wrong. Open this popup's DevTools console and run: chrome.storage.local.set({bearerToken: 'your-token'})"*.

Re-set the token to undo.

---

## 6. (Optional) Test capture from a real page

1. With the token still set in chrome.storage.local, browse to any
   article and dwell for >30s
2. Open the page's DevTools console — look for `[BrainTwin] Content captured: …`
3. Open `docker compose logs app | tail` — look for `POST /capture HTTP/1.1 200`
4. Open the popup → Remember tab → search for words from the article →
   it should turn up

---

## 7. (Optional) Test the Telegram bot service

Only do this if you've got the bot tokens in `.env`.

```bash
docker compose --profile with-bot up
```

In Telegram, send `/start` to your bot. Expected greeting:

> 👋 Hi <name>. DigitalTwin is active for you.

Forward an article. Check `docker compose logs bot | tail`:

```
braintwin-bot  |  INFO  posting capture to http://app:8000/capture
braintwin-bot  |  INFO  POST /capture 200
```

If the bot logs `401`, the `BACKEND_BEARER_TOKEN` in `.env` doesn't
match what the app container is using — easiest fix: `docker compose
down && docker compose up` to make sure both containers re-read .env.

---

## 8. Tests pass inside the container

The pytest suite runs unchanged; auth fixtures override the dep so
the existing /recall tests stay focused on recall behavior, and the
new `tests/test_auth.py` covers the gate itself.

```bash
# Inside the container:
docker compose exec app pytest -q
```

Or, faster, just on your Mac venv (same thing since the code is
identical):

```bash
pytest -q
```

Expected: all green, including the new auth file.

---

## 9. Sign-off checklist

Mark these before considering M.1 done:

- [ ] `docker compose build` succeeds
- [ ] `docker compose up app` brings the container to `healthy`
- [ ] GET /health returns 200 with no Authorization header
- [ ] GET / returns 200 with `name: DigitalTwin`
- [ ] POST /recall without Authorization returns 401
- [ ] POST /recall with wrong Bearer returns 401
- [ ] POST /recall with the correct Bearer returns 200 (or a known 503)
- [ ] POST /capture is similarly gated
- [ ] Chrome extension can capture + recall after the bearer is set
- [ ] Chrome extension shows the helpful 401 message when bearer is missing
- [ ] (optional) Telegram bot service can post captures end-to-end
- [ ] `pytest -q` passes (`tests/test_auth.py` included)

Once all green → safe to move on to **M.2 (CDK skeleton in BrainTwinCDK)**.

---

## Tearing down

```bash
docker compose down              # stop containers; ./data on the host persists
```

> **Note (revised 2026-06-11):** the compose file was switched from a Docker
> named volume to a host bind-mount of `./data/`. Your captures now live
> on the Mac filesystem (same files your local `uvicorn` mode reads/writes),
> not in a Docker-managed volume. Don't run `uvicorn backend.main:app`
> AND `docker compose up app` simultaneously — both want write access to
> the SQLite WAL and one will lose. Pick one mode per session.
>
> If you previously brought the stack up under the old named-volume
> compose, the empty `braintwin-data` volume is still kicking around;
> remove it once with:
>
> ```bash
> docker compose down 2>/dev/null
> docker volume rm braintwin-data 2>/dev/null || true
> docker compose up -d app
> ```
>
> This same bind-mount shape goes to the cloud unchanged in M.3 — only
> the backing filesystem changes (Mac → EBS on EC2). See
> `docs/phase4.0.6-deployment-design.md` §3.1 for the cloud-side mount.
