# Phase 4.1 M.M.1.d — local end-to-end smoke

## Goal

Prove the Google OAuth authorization-code flow works end-to-end on
your Mac before we ship it to prod. When this runbook succeeds, you'll
have:

- Signed in with your real Google account
- Received a JWT
- Successfully called a placeholder authenticated route with it
- Verified that the M.M.1.a `oauth_google_sub` column got backfilled
  on the row for your allowlisted email

If any step fails, that's the bug to fix before merging M.M.1.d.

**Time budget:** ~15 minutes.

## Prereqs

1. You already ran M.M.1.b: Google Cloud OAuth client exists, has
   `http://localhost:8000/auth/google/callback` in its authorized
   redirect URIs, and the consent screen is set to "In production"
   with `openid` + `email` scopes.
2. `.env` has values for the six new variables added in M.M.1.c:
   ```
   JWT_SECRET=<openssl rand -hex 32>
   JWT_TTL_MINUTES=43200
   GOOGLE_OAUTH_CLIENT_ID=<from Google Cloud Console>
   GOOGLE_OAUTH_CLIENT_SECRET=<from Google Cloud Console>
   GOOGLE_OAUTH_REDIRECT_URI=http://localhost:8000/auth/google/callback
   JOIN_SLUG=<openssl rand -base64 24 | tr '+/' '-_' | tr -d '='>
   ```
3. Your email is in the `users` table (should already be there as
   Sabya's row, `user_id=1`).
4. `pip install -r requirements.txt` — picks up the new `google-auth`
   pin.

## Step 1 — run tests

Before any browser gymnastics, confirm the unit tests pass:

```bash
pytest tests/test_auth_google.py tests/test_auth_routes.py -v
```

Expected: 18 passing. If not, that's the bug — fix before proceeding.

Also confirm the M.M.1.c tests still pass (they should — nothing
touched their scope):

```bash
pytest tests/test_auth_jwt.py tests/test_auth_pkce.py tests/test_auth_deps.py -v
```

Expected: 36 passing.

## Step 2 — boot the backend locally

Fresh terminal:

```bash
cd BrainTwin
source env/bin/activate         # or however you activate your venv
uvicorn backend.main:app --reload --port 8000
```

Expected log lines:

```
INFO     Uvicorn running on http://127.0.0.1:8000
INFO     Application startup complete.
```

If uvicorn fails with `ImportError` on `google.oauth2` or `google.auth`,
your venv's `google-auth` is missing — re-run `pip install`.

If uvicorn fails with `RuntimeError: JWT_SECRET is too short`, your
`.env` doesn't have a 32-char JWT_SECRET yet — go generate one.

## Step 3 — kick off the sign-in flow

Open a browser (a real browser — not curl; the flow needs cookies
and a JavaScript context for Google's consent screen):

```
http://localhost:8000/auth/google/start
```

Expected: instant redirect to `https://accounts.google.com/...` — the
familiar Google account picker.

Pick your `sabya.bisoyi@gmail.com` account.

If prompted with the consent screen ("This app is requesting access
to: your email address"), click "Continue" / "Allow".

**Failure modes at this step:**

- **"Error 400: redirect_uri_mismatch"** — the `GOOGLE_OAUTH_REDIRECT_URI`
  in your `.env` doesn't match any URI registered on the Google OAuth
  client. Go to Google Cloud Console → APIs & Services → Credentials
  → click your OAuth 2.0 Client → check "Authorized redirect URIs"
  → add `http://localhost:8000/auth/google/callback` if missing → save.
- **"Access blocked: This app's request is invalid"** — usually means
  the OAuth consent screen isn't published. Go to Console → OAuth
  consent screen → click "Publish App".
- **"Error 401: invalid_client"** — `GOOGLE_OAUTH_CLIENT_ID` in `.env`
  doesn't match the client shown in Console.

## Step 4 — land back at the app

After Google approves, browser gets redirected to:

```
http://localhost:8000/#token=<a very long JWT string>
```

You should see the browser's URL bar showing the fragment. The root
page (`/`) just renders `{"name": "BrainTwin", "status": "running", ...}` —
that's fine. The important thing is the JWT in the fragment.

Copy the JWT from the URL bar (everything after `#token=`,
URL-decoded — if the string contains `%2E` etc., that's URL encoding
which you'll need to decode).

**Failure modes at this step:**

- **400 "invalid or expired state"** — the /start's state row was
  consumed already (browser back-button? refresh loop?) or expired.
  Retry from Step 3 with a fresh /start.
- **401 "google code exchange failed"** — check the backend logs.
  Usually indicates a `client_secret` mismatch or the auth code was
  already used (retry from Step 3).
- **403 "not authorised: ... is not on the allowlist"** — the email
  you signed in with isn't in the `users` table. Sanity-check with
  `sqlite3 data/braintwin.db "SELECT id, email FROM users;"`.

## Step 5 — call an authenticated route with the JWT

The catch: as of M.M.1.d, NO existing route requires the JWT — M.M.2
is the substep that flips `/capture`, `/recall`, etc. from
`Depends(require_bearer_token)` to `Depends(get_current_user)`.

To smoke the JWT independently, hit any route with a Bearer header —
`/health` accepts everything, `/capture` still enforces the M.1 shared
bearer, so the cleanest smoke uses a temporary throwaway route.

Add this scratch route to `backend/main.py` for testing (delete after):

```python
from backend.auth import get_current_user
from backend.storage.models import User

@app.get("/whoami")
async def whoami(user: User = Depends(get_current_user)):
    return {
        "id": user.id,
        "email": user.email,
        "oauth_google_sub": user.oauth_google_sub,
        "is_admin": user.is_admin,
        "token_version": user.token_version,
    }
```

Restart uvicorn (auto-reloads on save). Then:

```bash
curl -H "Authorization: Bearer <the-jwt-from-step-4>" \
     http://localhost:8000/whoami
```

Expected response:

```json
{
  "id": 1,
  "email": "sabya.bisoyi@gmail.com",
  "oauth_google_sub": "117...",
  "is_admin": true,
  "token_version": 0
}
```

Key check: `oauth_google_sub` is NOT null. This proves M.M.1.d's
backfill logic ran on first sign-in (the row previously had null there
because you were pre-seeded by email).

**Failure modes:**

- **401 "bearer token required"** — you forgot the `-H` flag or the
  header is malformed. Double-check the exact string.
- **401 "invalid token"** — JWT_SECRET at mint time (Step 3) differs
  from JWT_SECRET now. If you edited `.env` between the sign-in and
  this step, the JWT is now invalid. Sign in again to get a fresh one.
- **403 "user not found"** — someone deleted your user row between
  sign-in and now. `SELECT * FROM users;` should show `user_id=1`.

## Step 6 — verify revocation

Prove that `bump_token_version` actually invalidates a live JWT:

```bash
sqlite3 data/braintwin.db "UPDATE users SET token_version = token_version + 1 WHERE id = 1;"
```

Then re-run the curl from Step 5. Expected: **401 "token revoked —
sign in again"**.

This is the primitive that lets you kick a friend out mid-session
after M.M.2 ships. Regression here means the promise fails.

## Step 7 — clean up

1. Remove the throwaway `/whoami` route from `backend/main.py` (do NOT
   commit it — that's a leak of pre-M.M.2 auth for a route that
   nobody planned for).
2. Reset `token_version` back to 0 if you want:
   ```bash
   sqlite3 data/braintwin.db "UPDATE users SET token_version = 0 WHERE id = 1;"
   ```
   (Or leave it — a positive `token_version` doesn't harm anything.)

## Success criteria

If every step passed, M.M.1.d is proven. Merge the PR when ready.

## What this smoke does NOT prove

- **Prod redirect_uri works.** Local smoke uses localhost; prod uses
  `https://braintwin.net/auth/google/callback`. Both must be registered
  in the Google OAuth client. First cloud deploy after M.M.1.d ships
  will surface any mismatch.
- **Route enforcement.** Nothing in this smoke exercises what happens
  when a real friend tries to `/capture` without a JWT. That's M.M.2's
  smoke — deferred until after the route flip.
- **Chrome extension sign-in UX.** No extension in this flow. That
  arrives in M.M.3 (if we pick extension-first) or later.

## Follow-ups after merge

1. Update `docs/phase4.1-multi-user-design.md` §7 "What shipped in
   M.M.1.d" table — matches the pattern used in phase 4.0.6.1 §2.4.1.
2. Kick off M.M.2 on a new branch.
