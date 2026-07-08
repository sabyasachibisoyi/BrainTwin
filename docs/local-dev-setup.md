# Local Dev Setup — New Machine (macOS)

End-to-end setup for running BrainTwin locally on a fresh Mac, written so a
new contributor can go from a clean clone to a running server + green tests.
Covers both **Apple Silicon** (M-series / Mac Studio) and **Intel** Macs — the
few differences are called out inline.

> **Local vs cloud.** Real Chrome-extension and Telegram capture traffic goes
> to the **cloud-hosted** instance (AWS). This local setup is for development
> and verifying changes before they ship. Running the app locally is safe;
> running the **Telegram bot** locally is *not* if the cloud bot uses the same
> token — Telegram allows only one `getUpdates` consumer per token, so two
> pollers collide. Leave `TELEGRAM_BOT_TOKEN` unset locally unless you have a
> dedicated dev bot.

---

## 0. Prerequisites — system tools (Homebrew)

These are **native binaries**, not Python packages — they must be installed at
the OS level (they cannot live in a venv, and the Dockerfile installs their
Linux equivalents the same way). Install [Homebrew](https://brew.sh) first,
then:

```bash
brew install python@3.12 whisper-cpp ffmpeg
```

- **`python@3.12`** — the interpreter we build the venv from. The repo needs
  3.11+, and 3.12 matches the Docker image, so torch/numpy pins resolve to the
  same wheels locally and in the cloud. (macOS system Python is 3.9 — too old.)
- **`whisper-cpp`** — provides `whisper-cli`, which the backend shells out to
  for local video transcription (Phase 2.5).
- **`ffmpeg`** — whisper.cpp can't decode m4a/webm/opus; ffmpeg pre-converts to
  wav.

Optional, only if you regenerate the HLD diagram (`docs/diagrams/`):
```bash
brew install graphviz
```

### ⚠️ Homebrew prefix differs by chip

| Chip | Homebrew prefix | `whisper-cli` lands at |
|------|-----------------|------------------------|
| Apple Silicon | `/opt/homebrew` | `/opt/homebrew/bin/whisper-cli` |
| Intel | `/usr/local` | `/usr/local/bin/whisper-cli` |

`backend/config.py` defaults `whisper_binary_path` to `/usr/local/bin/whisper-cli`
(the Intel path — it also matches where the Docker image bakes the binary).
**On Apple Silicon you must override it in `.env`** (step 3). Confirm your actual
path any time with `which whisper-cli`.

---

## 1. Clone and enter the repo

```bash
git clone <repo-url> BrainTwin
cd BrainTwin
```

---

## 2. Python venv + dependencies

Create the venv from Homebrew's 3.12 (not the system `python3`). The repo's
`.gitignore` ignores `venv/`, `.venv/`, and `env/`, so any of those names is
safe — this guide uses `venv/` (matches the README).

```bash
python3.12 -m venv venv          # explicitly 3.12; see caveat below
source venv/bin/activate
pip install --upgrade pip wheel setuptools
pip install -r requirements.txt
```

> All Python dependencies (torch, fastapi, chromadb, …) install **inside the
> venv**. Nothing touches system Python.

**Verify the native build** (especially on Apple Silicon — proves torch is
arm64, not x86 under Rosetta):

```bash
python -c "import platform, torch, numpy; \
print('arch', platform.machine(), '| torch', torch.__version__, \
'| numpy', numpy.__version__, '| MPS', torch.backends.mps.is_available())"
# Apple Silicon → arch arm64 | torch 2.2.2 | numpy 1.26.4 | MPS True
```

> **Why the old torch/numpy pins?** `torch==2.2.2` / `numpy<2` were a ceiling
> forced by *Intel* Macs (which shipped no newer torch wheels), **not** a
> floor. They install and run fine natively on Apple Silicon, and keeping them
> preserves parity with the cloud (Graviton/`linux/arm64`) image. Don't bump
> them casually — see the comment at the top of `requirements.txt`.

---

## 3. Configure `.env`

```bash
cp .env.example .env
```

Then edit `.env`:

1. **Bearer token (required).** Generate a strong one:
   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```
   Paste into `BACKEND_BEARER_TOKEN=`. Protects `/capture`, `/recall`,
   `/stats`, `/failures` (`/` and `/health` stay public).

2. **`ANTHROPIC_API_KEY`** (required for real capture/recall). From
   [console.anthropic.com](https://console.anthropic.com). *The app boots
   without it — only live Claude calls fail, at request time, not at startup.*

3. **`TELEGRAM_BOT_TOKEN` / `ALLOWED_TELEGRAM_USER_IDS`** — leave unset locally
   unless you have a dedicated dev bot (see the local-vs-cloud note at top).

4. **Apple Silicon only — whisper path override.** Add:
   ```
   WHISPER_BINARY_PATH=/opt/homebrew/bin/whisper-cli
   ```
   (Intel Macs can omit this — the config default already matches.)

### ⚠️ `.env` gotchas

- **`BRAINTWIN_DOCS_ENABLED` must stay empty in `.env`.** It's read via
  `os.environ` in `backend/main.py`, **not** a `Settings` field — a *non-empty*
  value in `.env` trips pydantic-settings' `extra_forbidden` and the app won't
  boot. To enable the `/docs` UI locally, export it in your shell instead:
  ```bash
  export BRAINTWIN_DOCS_ENABLED=1
  ```
- Don't quote values or leave trailing spaces on secret lines.
- `.env` is gitignored — never commit it.

---

## 4. Whisper model (for video transcription)

The ~244 MB `small.en` model is per-machine and gitignored — download it once:

```bash
bash scripts/setup_whisper.sh
```

This is idempotent: it skips the already-installed `whisper-cpp`/`ffmpeg` and
just fetches the model into `data/models/`. (If you'd rather skip video
transcription entirely, set `VIDEO_TRANSCRIBE_ENABLED=false` in `.env` and you
can skip this step.)

---

## 5. Run the app

```bash
source venv/bin/activate
uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload
```

Smoke-test in another terminal:

```bash
curl -s http://127.0.0.1:8000/health          # {"status":"ok",...}
TOK=$(grep '^BACKEND_BEARER_TOKEN=' .env | cut -d= -f2-)
curl -s http://127.0.0.1:8000/stats            # 401 without a token
curl -s -H "Authorization: Bearer $TOK" http://127.0.0.1:8000/stats   # 200
```

> Don't run `uvicorn` and `docker compose up app` at the same time — both write
> `data/braintwin.db`'s SQLite WAL and one will lose.

Load the Chrome extension: `chrome://extensions` → Developer Mode → Load
Unpacked → select `extension/`.

---

## 6. Run the tests

```bash
source venv/bin/activate
pytest -q                          # full suite (~16s, 361 tests)
BRAINTWIN_FAST_TESTS=1 pytest -q   # fast subset (~2s, skips torch/chromadb/whisper imports)
```

---

## 7. Enable the pre-commit hook (once per clone)

The hook script (`scripts/git-hooks/pre-commit`) is checked into git, but git
**never** version-controls hook *activation* — `.git/hooks/` and
`core.hooksPath` are machine-local, so every new clone must enable it once:

```bash
git config core.hooksPath scripts/git-hooks
```

It runs the fast test subset on every commit and blocks the commit if it fails.
Bypass a single commit with `git commit --no-verify`.

### ⚠️ Activate the venv before committing

When the venv isn't activated, the hook searches only `env/`, `.venv/`, and
`$VIRTUAL_ENV` for pytest. If your venv is named `venv/` (this guide's
default), the hook finds pytest **only when the venv is activated** — commit
without activating and it *silently skips* (exit 0, no tests run). So:
`source venv/bin/activate` before you commit. (Alternatively name your venv
`.venv`, which the hook finds even when not activated.)

---

## Apple Silicon vs Intel — summary of differences

| Thing | Intel | Apple Silicon | Action |
|-------|-------|---------------|--------|
| Homebrew prefix | `/usr/local` | `/opt/homebrew` | — |
| `whisper-cli` path | matches config default | differs from default | set `WHISPER_BINARY_PATH` in `.env` |
| torch/numpy pins | ceiling forced by Intel | run natively (MPS available) | keep pins for cloud parity |
| Docker build arch | `linux/amd64` | `linux/arm64` | Apple Silicon matches Graviton EC2 → *better* parity |
| Everything else | — | — | identical |

Bottom line: the **only** code-facing difference between the two is the
whisper binary path, handled by one `.env` line.

---

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| `ValidationError: braintwin_docs_enabled Extra inputs are not permitted` on boot | Non-empty `BRAINTWIN_DOCS_ENABLED` in `.env` — empty it; `export` it in the shell instead (step 3). |
| Video capture never transcribes | `whisper-cli` not found — check `which whisper-cli` matches `WHISPER_BINARY_PATH`; run `scripts/setup_whisper.sh`. |
| `pip install` fails on torch | Ensure the venv is Python 3.11–3.12 (`python --version`); torch 2.2.2 has no wheels for 3.13+. |
| Live capture/recall returns an auth error from Anthropic | Placeholder `ANTHROPIC_API_KEY` still in `.env` — paste a real key. |
| Commit succeeds but tests didn't run | venv wasn't activated → hook skipped (step 7). |
| Telegram bot errors with a `getUpdates` conflict | The cloud bot is already polling that token — don't run the bot locally with the prod token. |
</content>
