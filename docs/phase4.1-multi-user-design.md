# Phase 4.1 — Multi-user (private beta, allowlist-gated)

> **Status:** design, milestones not yet started. Created 2026-06-29
> after a friend asked to try BrainTwin. Scope is deliberately
> minimal: turn a working single-user product into a private beta
> that supports 1-10 friends, without opening the cost blast radius
> of "anyone with a Google account can rack up my Anthropic bill."
> Runs in parallel with Phase 4.0.5 (eval); the two can be worked on
> concurrently, with two coordination seams — the `/recall` auth
> contract and the shared `response.usage` metering — that the
> *second* phase to merge must reconcile (§7.4).

---

## 0. The one-line problem

The current product is authenticated by a single shared bearer
token. To let a friend use BrainTwin, either they get my token
(gives them read/write access to my entire corpus) or I let them
in through a proper per-user auth boundary. This phase builds the
per-user auth boundary — with a cost-safe onboarding gate — so
friends can use the product against their own corpus.

---

## 1. Why now, and why this shape

**Real product signal.** A friend has asked to use BrainTwin. When
someone who isn't building the product wants to use it, that's the
first honest quality signal a personal product gets — better than
any dashboard metric.

**Why not "just enable Google sign-in."** Google OAuth accepts
every Google user on the internet. If I enable it naively, one
Hacker News post could deposit hundreds of curious strangers into
my Anthropic bill overnight. Enrichment alone at ~10 captures per
user per day at Haiku's rates is ~$0.05/user/day; recall at Sonnet
is more. 100 curious strangers = ~$5-15/day of stranger-driven
Anthropic spend that I can't monetize. The whole point of Phase
4.1 is to make cost blast radius equal exactly the users I chose
to admit — no more, no less.

**Why not build a proper self-serve signup with paid tiers.** Two
answers. First, at 1-10 friends the ceremony (billing integration,
tiers, invoicing, tax) costs more than the friction it removes.
Second, and more honestly: I don't want to run a business right
now. I want friends to be able to use my thing. That's a
different product goal.

**Why now over Phase 4.0.7 (Postgres migration).** Postgres would
close the §14.1 EBS-deadlock class of downtime, which becomes
worse when real users depend on the service. For a **private beta
with one friend from India**, the EBS-dance downtime (~30 min
during rare deploys) is acceptable — he signed up for beta. If the
friend count grows past 3-4, that calculus flips and 4.0.7 becomes
urgent. See §7.1 for the deferred-until-then trigger.

---

## 2. The design in one paragraph

Add Google OAuth as the identity provider. Add an `email` column
to the existing `users` table with a unique index. Add
`get_current_user` as a FastAPI dependency that reads a JWT from
the `Authorization` header, looks the email up in `users`, and
returns the row (or 403). Every route that currently hardcodes
`DEFAULT_USER_ID = 1` gets a `user: User = Depends(get_current_user)`
parameter and uses `user.id` instead. The Chrome extension gains
a "Sign in with Google" button; the Telegram bot gains a `/link
<code>` flow that binds a Telegram user ID to an app user. Admin
CRUD is a pair of shell scripts (`add-user.sh`, `remove-user.sh`)
that operate directly on the `users` table. Rate limiting is a
`usage_counters` table keyed by `(user_id, date_utc)` with hard
daily caps on captures, recalls, and Anthropic input tokens. A
minimal landing page at `braintwin.net` shows only a "Sign in
with Google" button (no request-access CTA visible); the actual
onboarding walkthrough lives at a hidden URL (`/join/<slug>`)
whose slug is a runtime SSM secret. Requests for access go to
sabya.bisoyi@gmail.com out-of-band. A `DELETE /account` endpoint
cascade-deletes all of a user's captures, chunks, and Chroma
vectors. That's the whole phase.

---

## 3. Scope

### 3.1 In scope

| Item | What it does |
|------|--------------|
| Google OAuth | Identity: proves who someone is |
| Allowlist gate (via `users` table) | Authorization: proves they're allowed to use BrainTwin |
| Per-user data isolation | Every SELECT/INSERT scoped by `user_id`; Chroma already filters on `user_id` (retrieval.py) |
| JWT sessions | Stateless auth token the extension and web can carry |
| Chrome extension sign-in | "Sign in with Google" button + JWT storage |
| Telegram bot `/link <code>` | Binds Telegram user to app user |
| Landing page at `braintwin.net` | What it is + how to request access + sign in |
| `add-user.sh` / `remove-user.sh` | Admin CRUD via CLI (adds/removes email from `users`) |
| Per-user daily quotas | Hard cap on captures, recalls, Anthropic tokens |
| `DELETE /account` | Cascade-delete a user's captures, chunks, Chroma vectors |
| Short privacy note | Plain-English "what is stored, who sees it, how to delete" |

### 3.2 Out of scope (explicit)

| Item | Why not |
|------|---------|
| Self-serve signup | Anyone-can-sign-up is precisely what we're avoiding. Request-access is out-of-band by design. |
| Request-queue database or admin panel | At 1-10 friends the manual out-of-band flow (email/WhatsApp → I run a script) is less work than any UI |
| Multiple OAuth providers (GitHub, Apple, etc.) | Google covers every friend I have. Add another if a specific friend can't use Google. |
| Password auth | Nobody wants to manage another password. OAuth-only. |
| Team accounts, sharing captures | Multi-user ≠ collaboration. Deferred. |
| Billing / paid tiers | See §1 — I'm not running a business right now |
| Formal ToS with legal review | Plain-English privacy note is enough for a friend-only beta |
| Formal user admin panel | CLI scripts. Add a panel when there are enough users that CLI is annoying. |
| Rate limiting for abuse | Quotas are for *cost cap*, not abuse — the allowlist means no adversaries |

---

## 4. Auth architecture

### 4.1 Data model

Add to the existing `users` table:

```sql
ALTER TABLE users ADD COLUMN email TEXT NOT NULL DEFAULT '';
-- NOTE: SQLite forbids adding a UNIQUE column via ALTER TABLE ADD
-- COLUMN ("Cannot add a UNIQUE column"). Add the column plain, then
-- enforce uniqueness with a separate CREATE UNIQUE INDEX below.
ALTER TABLE users ADD COLUMN oauth_google_sub TEXT;  -- Google's stable subject id
ALTER TABLE users ADD COLUMN added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE users ADD COLUMN is_admin BOOLEAN DEFAULT FALSE;
ALTER TABLE users ADD COLUMN token_version INTEGER NOT NULL DEFAULT 0;  -- bumped to revoke live JWTs (§4.3)
ALTER TABLE users ADD COLUMN is_eval BOOLEAN DEFAULT FALSE;  -- quota-exempt eval user (§6, eval doc §4.3)
CREATE UNIQUE INDEX users_email_idx ON users(email);
CREATE UNIQUE INDEX users_oauth_sub_idx ON users(oauth_google_sub);
```

The `oauth_google_sub` starts NULL — populated on first successful
Google sign-in. That way `add-user.sh someone@gmail.com` doesn't
need to know their Google `sub` upfront; it's discovered on their
first sign-in. SQLite treats NULLs as distinct in a unique index, so
many not-yet-signed-in users can coexist with `oauth_google_sub =
NULL` under `users_oauth_sub_idx`.

**Migration ordering.** Backfill Sabya's real email (§7.3) BEFORE
creating `users_email_idx` — the `DEFAULT ''` would otherwise leave
the single existing row at `email = ''`, which is fine for one row
but collides the instant a second `''` appears. Create the index
after the backfill so the invariant is real from the start.

> **⚠️ Foreign keys must be enabled or every cascade below is a
> silent no-op.** SQLite defaults `PRAGMA foreign_keys = OFF` *per
> connection*, and `backend/storage/db.py::_configure_sqlite_pragmas`
> currently sets only `journal_mode` / `synchronous` / `busy_timeout`
> — NOT `foreign_keys`. Until we add `PRAGMA foreign_keys = ON` to
> that connect-event listener, every `ON DELETE CASCADE` in the tables
> below does nothing and account deletion leaves orphaned rows —
> directly breaking the privacy promise in §5.3. **M.M.1 must add the
> pragma**, and deletion (§5.2, `DELETE /account`) must *also* issue
> explicit per-table `DELETE`s rather than trusting cascades alone.

New table for Telegram binding (small, orthogonal):

```sql
CREATE TABLE telegram_bindings (
  telegram_user_id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  linked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  link_code TEXT UNIQUE  -- temporary one-time code, NULL after linked
);
```

New table for rate limiting:

```sql
CREATE TABLE usage_counters (
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  date_utc TEXT NOT NULL,  -- YYYY-MM-DD
  captures INTEGER DEFAULT 0,
  recalls INTEGER DEFAULT 0,
  anthropic_input_tokens INTEGER DEFAULT 0,
  anthropic_output_tokens INTEGER DEFAULT 0,
  PRIMARY KEY (user_id, date_utc)
);
```

### 4.2 OAuth flow

Standard Google OAuth 2.0 authorization code + PKCE:

```
Friend clicks "Sign in with Google" (extension or landing page)
  ↓
Browser redirects to https://accounts.google.com/o/oauth2/v2/auth
with our client_id + scope=openid email + PKCE challenge
  ↓
Friend authenticates on Google
  ↓
Google redirects to https://braintwin.net/auth/google/callback?code=…
  ↓
Backend exchanges code for id_token via Google's token endpoint
  ↓
Backend verifies `state` matches the cookie (CSRF), then exchanges
the code using the stored PKCE `code_verifier`
  ↓
Backend verifies id_token signature + issuer + audience
  ↓
Backend reads `email`, `email_verified`, and `sub` from id_token.
REJECT if `email_verified != true` — we allowlist on email, so an
unverified email claim must not grant access.
  ↓
Backend looks up `SELECT * FROM users WHERE email = ?`
  ├─ found:    UPDATE users SET oauth_google_sub = ? WHERE id = ?
  │            → mint JWT, return to client
  └─ NOT found: return 403 "Access by invitation only"
                page tells the visitor to email
                sabya.bisoyi@gmail.com (or WhatsApp) with their
                Google email address to request access. This is
                the ONLY place the request channel is disclosed.
```

**Backend routes:**
- `GET /auth/google/start?next=<where-to-return>` — generates the PKCE
  `code_verifier` + `state`, stores BOTH server-side-bound (a signed,
  `HttpOnly`, `SameSite=Lax` cookie carrying `state` + `code_verifier`,
  or a short-lived server row keyed by `state`), redirects to Google.
  The `code_verifier` MUST survive to the callback — it's required to
  complete the token exchange.
- `GET /auth/google/callback?code=…&state=…` — verifies `state` equals
  the cookie value (reject otherwise), exchanges `code` with the stored
  `code_verifier`, verifies id_token (sig + iss + aud + `email_verified`),
  mints JWT or 403s.

> **id_token verification: use `google-auth`, don't hand-roll (added
> 2026-07-02).** Signature verification means fetching + caching
> Google's JWKS, selecting the right key by `kid`, and checking
> sig/iss/aud/exp — `google-auth`'s
> `id_token.verify_oauth2_token(token, request, CLIENT_ID)` does all
> of it in one audited call. The learning value in this phase is the
> *flow* (state, PKCE, allowlist, revocation), which we do own;
> reimplementing signature verification on an auth boundary is where
> hand-rolling turns from learning into risk.

> **Consent-screen publishing status (added 2026-07-02).** Leave the
> Google OAuth app in "Testing" mode and every friend must ALSO be
> hand-added as a test user in Google Console — a shadow allowlist
> (capped at 100) duplicating the `users` table, plus a scary
> warning screen. With only non-sensitive scopes (`openid email`),
> the consent screen can be set to **"In production" without
> Google's verification review** — do that during M.M.1 setup. The
> `users` table remains the only allowlist.

**JWT contents:**
- `sub`: `str(user.id)` — JWT `sub` is a string per RFC 7519; cast on
  mint and `int()` on read so PyJWT doesn't surprise you.
- `email`: for debugging
- `tv`: `user.token_version` — checked on every request (§4.3) so a
  single-user revoke is possible without rotating the global secret.
- `iat`, `exp`: 30 days
- signed with `HS256` using a secret from SSM at `/braintwin/jwt_secret`

30-day sessions are generous, but rotating JWTs mid-session for
a private beta of friends adds ceremony without value. If the
beta grows or a session leaks, we can rotate the JWT signing
secret and log everyone out at once.

### 4.3 `get_current_user` dependency

```python
async def get_current_user(
    # Header(None), not Header(...): a required header makes a missing
    # token a 422 validation error. We want a clean 401.
    authorization: str | None = Header(None),
    session: AsyncSession = Depends(session_scope),
) -> User:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    token = authorization.split(" ", 1)[1]
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError as e:
        raise HTTPException(401, f"invalid token: {e}")
    user = await UserRepository(session).get(int(payload["sub"]))
    if user is None:
        raise HTTPException(403, "user not found")
    # Live-revocation check: a JWT is only valid while its `tv` claim
    # matches the user's current token_version. Bumping token_version
    # (revoke) invalidates every outstanding JWT for THAT user without
    # rotating the global signing secret (which logs everyone out).
    if payload.get("tv") != user.token_version:
        raise HTTPException(401, "token revoked — sign in again")
    return user
```

> **Revocation, corrected.** An earlier draft claimed nulling
> `oauth_google_sub` forces re-auth. It does **not** — this dependency
> never reads that column, so a compromised 30-day JWT keeps working.
> The `token_version` claim above is the actual per-user revoke lever:
> `UPDATE users SET token_version = token_version + 1 WHERE id = ?`
> invalidates that user's live tokens immediately. Nulling
> `oauth_google_sub` only forces a fresh Google `sub` binding on next
> sign-in; it is not a session-revocation mechanism.

Every route that currently uses `DEFAULT_USER_ID` gets:

```python
@app.post("/capture")
async def capture(
    payload: CapturePayload,
    user: User = Depends(get_current_user),
):
    # ... user.id instead of DEFAULT_USER_ID ...
```

### 4.4 Chrome extension changes

Extension gains a "Sign in" state:

- **Unsigned-in state:** popup shows "Sign in with Google" button →
  opens a new tab at `https://braintwin.net/auth/google/start?next=extension`
  → after successful OAuth, callback page shows the JWT and a
  "Copy to extension" button → user pastes into extension → extension
  stores it in `chrome.storage.local`
- **Signed-in state:** popup shows "Signed in as friend@gmail.com" +
  sign-out button. Captures + recalls send the stored JWT.

The paste-JWT UX is deliberately clunky-but-honest for MVP.
Chrome's extension OAuth story is fiddly (chrome.identity API,
manifest v3 constraints, etc.); a proper in-extension flow is a
week of its own. Paste-the-JWT works today, no manifest changes.

> **Known risk — don't display the 30-day token itself.** A 30-day
> bearer JWT rendered as copyable text lands in the clipboard (and
> possibly screenshots / browser history), and any script on the
> callback page can read it. Prefer showing a **short-lived one-time
> code** (minutes-TTL, single-use) that the extension POSTs back to
> exchange for the real JWT — the long-lived token then never touches
> the clipboard or the page DOM. Same paste UX, far smaller blast
> radius if the code leaks. This is a small addition to M.M.3, not a
> separate milestone.

### 4.5 Telegram bot linking

Binding a Telegram account to an app user:

1. Friend signs in on the web
2. Web UI shows: "Your Telegram link code: **ABC12345** (valid 10 min).
   Send `/link ABC12345` to @BrainTwinBot on Telegram."
3. Backend stores `(user_id=X, link_code=ABC12345, expires_at=+10min)`
4. Bot receives `/link ABC12345`, looks up the code, records
   `telegram_bindings(telegram_user_id, user_id)`, clears link_code
5. All future messages from that Telegram user route to that app user

Unbinding is `/unlink` — sets the row's user_id to NULL or deletes
the row. Idempotent.

The old `ALLOWED_TELEGRAM_USER_IDS` env var goes away entirely —
it's a broken model (shared allowlist across all app users).

### 4.5.1 Bot→backend auth — per-user JWT minting (decided 2026-07-02)

Linking answers "which app user is this Telegram user"; it does NOT
answer how the bot *authenticates to the backend* afterwards. The bot
is a separate container that POSTs to the app over HTTP using the
shared bearer (`backend/telegram_bot/client.py`) — a credential
M.M.2 removes from every route. Post-migration, each bot request must
carry *whose corpus* it touches, both for data isolation and so
quotas attribute Anthropic spend to the right friend.

**Decision: the bot mints a short-lived per-user JWT per request**,
rather than keeping a service token + trusted `X-User-Id` header:

1. On each message, resolve `telegram_user_id → user_id` via
   `telegram_bindings` (the bot already bind-mounts the same SQLite).
2. Read the user's current `token_version` at mint time — so a
   revoked friend gets at most the token TTL of grace.
3. Sign `{sub: str(user_id), tv, iat, exp: +5 min}` with the same
   `JWT_SECRET` the backend validates — already available to the bot
   via the shared `secrets.env`. Cache per user until expiry.
4. Send as `Authorization: Bearer <jwt>` on `/capture` (and `/recall`
   when the bot grows it).

**Why this over service-token + user-id header:** the backend keeps
exactly ONE auth path — `get_current_user` cannot even tell a bot
request from an extension request — so quotas, `token_version`
revocation, and any future middleware apply to bot traffic with zero
special-casing. And what travels the wire is a 5-minute single-user
token, not an eternal all-users skeleton key. Compromise of the bot
box is equivalent under both options (it holds `JWT_SECRET` either
way); the win is backend uniformity + wire blast radius, not
bot-compromise resistance. Note this makes the bot the second
*minter* alongside the OAuth callback (and the eval provisioning
script the third) — many minters, one validator is the intended
shape.

**Degradation:** unknown/unlinked `telegram_user_id` → no binding row
→ the bot replies "send `/link <code>` to connect your account" and
never calls the backend. A removed friend degrades the same way once
their binding is deleted.

---

## 5. Web pages + admin

### 5.0 Tech stack + repo layout (decided 2026-06-29)

**Stack:** vanilla HTML + Tailwind CSS via CDN + a few lines of
vanilla JavaScript for copy-to-clipboard interactions. No build
step, no npm, no framework. Total surface is 5-6 nearly-static
pages; anything more is over-engineering.

**Not React, not TypeScript for this surface.** Zero interactive
state to manage; ~30 lines of JS across all pages. TypeScript on
30 lines is signalling, not engineering. If the surface ever grows
into a real interactive app, a Chrome-extension rewrite is a much
better home for TS + Preact/React than the landing page.

**Repo:** `BrainTwin/web/` directory in the app repo — not a
separate repo. Web content is product code; it changes in the
same PR as backend routes it depends on. `BrainTwinCDK/` is
separate for real reasons (deploy story, different reviewers,
ops cadence); the landing page has none of those needs.

**Serving:** Caddy bind-mounts `BrainTwin/web/` and serves static
files for the paths listed below. FastAPI handles the ONE dynamic
route (`GET /join/{slug}`, §5.4).

### 5.1 Landing page (`braintwin.net`)

Single page, deliberately minimal. **No "request access" CTA
visible.** A visitor who arrives without an invite doesn't see any
onboarding instructions — the site presents itself as a gated
product they either have access to or don't. Content:

- **Hero:** "BrainTwin — a personal knowledge twin that remembers
  what you read."
- **Sign in with Google button** — the only interactive element.
  Redirects to `/auth/google/start`.
- **Footer link to** `/privacy` (§5.3).

That's it. No screenshot, no marketing copy, no request-access
form or contact info. Someone who lands here without a link sees
a sign-in gate. If they sign in and aren't on the allowlist, they
get a friendly 403 that says:

> **Access is by invitation only.**
> If you know Sabya, message him at **sabya.bisoyi@gmail.com** (or
> WhatsApp) with your Google email address to request access.

That's the ONLY place the request channel is disclosed to
someone-who-signed-in-and-was-rejected — no hint of it on the
landing page itself.

**Why minimal:** the beta shouldn't feel like a marketing page
that just happens to be gated. It should feel like a product a
friend told you about, where you sign in and use it. When we
ever open it up, the landing page gets rewritten. Until then, we
don't invest UX effort in a page 3 people will see.

### 5.2 Admin CLI

Two scripts in `BrainTwin/scripts/`:

```bash
# scripts/add-user.sh
# Usage: ./scripts/add-user.sh friend@gmail.com "Ramesh"
#
# Adds a row to the users table. Idempotent - re-running with the
# same email is a no-op. Prints the row afterwards for verification.
```

```bash
# scripts/remove-user.sh
# Usage: ./scripts/remove-user.sh friend@gmail.com
#
# Issues EXPLICIT DELETEs per table (captures, enrichments, chunks,
# telegram_bindings, usage_counters) then the users row — plus a
# Chroma `.delete(where={"user_id": X})` for the vector store. Do NOT
# rely on ON DELETE CASCADE alone: it silently no-ops unless
# `PRAGMA foreign_keys = ON` is set on the connection (§4.1 warning).
# Explicit deletes are correct whether or not the pragma landed, and
# they let us print an affected-row count per table for verification.
# Wrap all deletes + the Chroma call in one transaction where possible
# so a partial failure doesn't leave a half-deleted user.
```

`DELETE /account` (the self-serve path, §3.1) uses the **same**
explicit-delete routine as `remove-user.sh` — factor it into one
`delete_user(user_id)` function called by both, so the privacy
promise (§5.3) has a single, tested implementation rather than two
that can drift.

Both scripts run against the production SQLite via SSM Session
Manager (SSH into EC2, run script). No admin HTTP endpoint —
scripts run inside the box, no auth-on-auth complexity.

### 5.3 Privacy note (plain English)

Draft to live at `braintwin.net/privacy`:

> **What BrainTwin stores about you**
>
> When you capture an article, page, video, or Telegram message,
> BrainTwin stores its URL, title, the text of the content, and
> an LLM-generated summary and entity list. It also stores the
> vector embeddings of the text so it can find the capture again
> when you ask.
>
> **Where it lives**
>
> Everything is stored on Sabya's AWS account, in an EC2 instance
> and S3 bucket in Oregon (us-west-2). Captured content is
> encrypted at rest and only accessible to you (via your signed-in
> account) and to Sabya (as the system operator).
>
> **What we send to Anthropic**
>
> When your capture is enriched, or when you run a recall, the
> content of your captures is sent to Anthropic's API to be
> processed by Claude (Haiku for enrichment, Sonnet for recall).
> Anthropic's data-use policy applies to that content.
>
> **How to delete**
>
> Sign in and visit `/account/delete`, or ask Sabya. Deletion is
> permanent and cascades to every table (captures, enrichments,
> vectors, backups within 7 days).
>
> **Contact**
>
> Email: **sabya.bisoyi@gmail.com** (WhatsApp on request).

Not a lawyer document. Honest and short.

### 5.4 The onboarding page — hidden URL, walkthrough content

The landing page (§5.1) doesn't tell anyone *how* to request access
or *how* the pieces fit together after they're approved. That
information lives on a separate onboarding page at an unguessable
path — the URL is a runtime secret, never in the repo.

**Threat model.** The BrainTwin repo is public. Any URL hardcoded
in code, Caddyfile, or HTML lives in git and is discoverable by
anyone browsing the source. So the slug can't live in code. It has
to be a runtime secret rotated via the same M.10 discovery pattern
we use for API keys.

**The pattern.**

1. **Slug lives in SSM** at `/braintwin/join_slug`. Value is a
   random string chosen by Sabya, e.g., `friends-x7k9zq4n`.
2. **M.10 discovery refresh** picks it up automatically as
   `$JOIN_SLUG` in `/etc/braintwin/secrets.env` — no CDK change, no
   deploy. Same mechanism as every other secret.
3. **FastAPI route** validates the slug:

   ```python
   import hmac

   @app.get("/join/{slug}")
   async def onboarding_page(slug: str):
       expected = os.environ.get("JOIN_SLUG")
       # constant-time compare — a plain `!=` leaks the slug
       # byte-by-byte via response timing
       if not expected or not hmac.compare_digest(slug, expected):
           raise HTTPException(404)
       return FileResponse("web/join.html")
   ```

4. **Rotation** is one command; no code change, no deploy:

   ```bash
   ./scripts/put-secrets.sh join_slug "friends-new-slug-value"
   ./scripts/refresh.sh   # SSM RunCommand → refresh
   # Old URL 404s within ~10 seconds. New URL is live.
   ```

5. **Distribution** is out-of-band: Sabya sends the URL over email,
   WhatsApp, iMessage, or a signed post-it note. Never posts it
   publicly. If it leaks, rotate.

**Threat model, honestly:**

- Defends against casual repo-browsers, drive-by URL scanners, and
  ordinary search-engine indexing (the page 404s without the slug).
- Doesn't defend against someone with SSM read access on
  `494567491756` — but that's only Sabya, and even if the value
  leaked they'd still hit the allowlist gate at sign-in time (no
  product access, only knowledge of the invite URL).
- Doesn't defend against a friend who shares the URL. That's a
  social contract, not a technical control. The onboarding page
  itself says "please don't share this URL."

**Onboarding page content** (`web/join.html`):

> ### Welcome to BrainTwin (private beta)
>
> You've been invited to try a personal knowledge twin — capture
> what you read across the web and Telegram, ask it questions
> weeks later in natural language when you only half-remember the
> topic.
>
> **To join:**
>
> **Step 1 — Request access.** Email Sabya at
> **sabya.bisoyi@gmail.com** (or WhatsApp if you have his number)
> with your Google email address. Sabya will confirm when you're
> approved — usually within a day.
>
> **Step 2 — Sign in.** Once approved, go to
> [braintwin.net](https://braintwin.net) and click "Sign in with
> Google." Sign in with the same Google account whose email you
> shared in Step 1.
>
> **Step 3 — Link your Telegram (optional).** After signing in
> you'll see a short link code on the screen. Send
> `/link <code>` to
> [@BrainTwinBot](https://t.me/BrainTwinBot) on Telegram. From
> then on, forwarding a URL to the bot will capture it into your
> BrainTwin.
>
> **Step 4 — Install the Chrome extension (optional).**
> [Download link when available.] Sign in with your Google account
> inside the extension popup. You'll then be able to capture the
> page you're reading with one click.
>
> **Step 5 — Try it.** Capture 5-10 things you've read recently.
> Wait an hour (background enrichment). Then ask the extension
> popup or the Telegram bot a vague question like "what did I read
> about X" — should surface the right capture.
>
> ---
>
> **Private beta — please don't share this URL.** If a friend of
> yours wants to try, ask Sabya first.
>
> **Privacy:** [link to /privacy] — plain-English summary of what's
> stored and how to delete.
>
> **Something broken?** Email sabya.bisoyi@gmail.com. This is a
> hobby project; response times reflect that.

**Rotation policy (default):** no scheduled rotation. Rotate only
if a leak is suspected — someone posts the URL online, the SSM
value shows up in a screenshot, a friend inadvertently shares.
Manual, reactive. If we ever ship this to more than 10 friends,
scheduled quarterly rotation becomes appropriate hygiene.

---

## 6. Cost cap: per-user daily quotas

Simple counters in the `usage_counters` table. Hard limits (rejects
the request if exceeded, not warnings):

| Action | Daily cap | Rough Anthropic cost at cap |
|--------|-----------|-----------------------------|
| Captures | 100 | ~$0.05 at Haiku rates |
| Recalls | 50 | ~$0.25 at Sonnet rates |
| Anthropic input tokens | 500,000 | Backstop; the two above should be tighter |

Implementation is a small middleware or a check in each route:

```python
async def check_quota(user: User, action: str, session):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    counters = await UsageCountersRepository(session).get_or_create(
        user_id=user.id, date_utc=today
    )
    if user.is_eval:
        return  # eval user is quota-exempt; see note below
    if action == "capture" and counters.captures >= CAPTURE_CAP:
        raise HTTPException(429, "daily capture cap reached")
    # ... similar for recall + tokens ...
    # Atomic increment — do NOT read-modify-write in Python
    # (`counters.captures += 1` undercounts under concurrency);
    # let the DB do the arithmetic:
    await session.execute(
        update(UsageCounters)
        .where(UsageCounters.user_id == user.id,
               UsageCounters.date_utc == today)
        .values(captures=UsageCounters.captures + 1)
    )
    await session.commit()
```

> **Eval-user exemption (added 2026-07-02).** The Phase 4.0.5 weekly
> end-to-end eval runs ~90-110 recalls in a single pass (positive +
> negative + conversational sets) — roughly double the 50/day recall
> cap, so without an exemption the Sunday eval 429s mid-run, every
> week. Eval doc §4.3 promises eval traffic "doesn't share rate-limit
> budgets"; the `is_eval` check above is where that promise is
> implemented. Counters are still *recorded* for the eval user (cost
> visibility), just never enforced. Only the provisioned eval user
> (eval doc §3.5) ever has `is_eval = TRUE`.

Tokens are counted from the Anthropic response's `usage` field
(which the M.11 EMF work already reads at the wire level — see
`llm_client.py`; deferred there for time, gets picked up here).

> **The token cap is reactive, by construction.** You only know a
> call's token count *after* it returns, so the 500k cap can't reject
> a request pre-emptively — it trips on the *next* request once the
> running total is already over. That's fine as a backstop (a single
> request can't overshoot by much), but say so: captures/recalls are
> the *preventive* caps; tokens are the *catch-all after the fact*.

> **Capture over quota: reject (429), don't silently accept.** This
> endpoint's existing contract returns 200 even on a persist failure
> (the non-blocking-capture invariant). Quota is different — an
> over-cap capture must **429 and not run**, because the whole point
> is to not spend. Resolve the tension explicitly: the quota check is
> a *synchronous gate at the top of the route* that 429s **before**
> the row is persisted and **before** the background enrichment task
> is scheduled. Non-blocking persistence applies only *after* the
> request is admitted; the quota gate sits in front of it. (Enrichment
> is a background task — if the gate ran after scheduling, the cap
> wouldn't actually cap the Anthropic spend.)

**Reset behaviour:** hard reset at 00:00 UTC. No rolling window.
For a friend beta this is fine; if we ever go paid, a rolling
30-day-window quota is the right upgrade.

**Adjusting limits per user:** the caps are constants for MVP. If
a friend needs more, I bump the constant and redeploy. When there
are 5+ friends and they diverge, add a `daily_capture_cap` column
to `users`. Not now.

### 6.1 The missing backstop — a global Anthropic spend cap

Per-user quotas bound *one* user. They do **not** bound the sum, and
the failure modes that motivate this whole phase are aggregate:
N friends all maxing caps, or one bug (a recall loop, a retry storm)
multiplied across users. Worst case at the current caps is
~$0.30–1.05/user/day; the phase should not depend on every per-user
counter being correct to avoid a runaway bill.

Two aggregate controls, both cheap, neither in the design yet:

1. **A hard monthly spend limit on the Anthropic org / API key**
   (Anthropic Console → usage limits). This is the real ceiling — it
   caps *total* spend regardless of how many users or how buggy the
   code. Set it before onboarding the first friend. **Note the AWS
   budget alarms (M.2.h) do NOT see Anthropic API spend** — it's a
   separate vendor, so nothing today watches this axis.
2. **A global daily counter** (a single `usage_counters` row with a
   sentinel `user_id`, or a process-level tally) that trips a FATAL /
   read-only mode if org-wide daily tokens exceed a ceiling — defense
   in depth behind the Console limit.

(1) is a 2-minute config and is the single highest-leverage cost
safeguard in this phase — do it in M.M.1. (2) is optional hardening.

> Dollar *totals* (per-user, eval, AWS) are deferred until a real
> one-month bill exists — this subsection is about the **control**,
> not the estimate.

---

## 7. Deployment considerations

### 7.1 Phase 4.0.7 (Postgres) — deferred, with a trigger

The §14.1 EBS-deadlock class of downtime (~30 min per deploy that
touches user-data) is acceptable for a beta of 1-3 friends. It
becomes user-hostile once:

- **5+ active friends** with regular daily usage, OR
- **A friend uses BrainTwin professionally** (during a workday, needs it up), OR
- **A user complains about a specific outage window**

At any of those triggers, Phase 4.0.7 (SQLite → Postgres on RDS)
moves ahead of any other Phase 4.1 work. Until then, the private
beta accepts the trade-off.

### 7.2 First deploy of Phase 4.1

Estimated as **one user-data change** (adding OAuth env vars, JWT
secret path in the refresh script) → one EBS-deadlock dance.

> **Bundle the pending CDK diff into the same dance (added
> 2026-07-02).** The M.10 + M.11 CDK infra changes are built but not
> yet deployed (app code went live via snapshot; `cdk deploy`
> replaces the instance). The Phase 4.1 user-data change forces that
> instance replacement anyway — fold the outstanding M.10/M.11 diff
> into this deploy so we pay for ONE EBS dance, not two. Verify with
> `cdk diff` before the dance that the combined change set is what's
> expected.

The Google OAuth `client_id` + `client_secret` land in SSM via
`put-secrets.sh` (M.10 discovery picks them up automatically —
zero CDK changes). The JWT signing secret does too.

### 7.3 Data model migration

The `users` table schema changes (`ALTER TABLE ADD COLUMN`) are
idempotent-friendly. Migration runs at app startup (see the
existing `init_db` in `backend/storage/db.py`). Backfill:
- Existing `user_id=1` (Sabya) gets `email=sabya@gmail.com`,
  `is_admin=TRUE`
- Existing captures/enrichments/chunks already carry `user_id=1`
  — no data migration needed
- The old `ALLOWED_TELEGRAM_USER_IDS` env var (single row that was
  Sabya's Telegram id) becomes a row in `telegram_bindings` for
  Sabya

The migration script (`scripts/migrate-to-multi-user.py` or
similar) runs once, idempotent, then can be deleted.

### 7.4 Coupling with Phase 4.0.5 (eval) — not fully independent

The status note calls 4.0.5 and 4.1 "independent, either can land
first." That's *mostly* true but has two real seams — worth naming
so whoever lands second isn't surprised:

1. **Auth contract.** Today every route uses `require_bearer_token`
   (`main.py`), and the eval harness authenticates to `/recall` with
   the single shared `/braintwin/bearer_token`. Phase 4.1 replaces
   that with per-user Google JWTs. **If 4.1 lands first, the nightly
   eval breaks** — the shared bearer no longer authenticates.
   **Decided 2026-07-02:** a dedicated SSM param
   `/braintwin/eval_bearer_token` is created on **Day 0** with the
   current shared bearer as its value, and the eval IAM role +
   workflow are scoped to that name from the start. When this phase
   lands, only the *value* is swapped — to a long-lived JWT minted
   for the **dedicated eval user** (eval doc §3.5 step 7), NOT for
   `user_id=1` — so eval traffic stays out of Sabya's corpus, gets
   the `is_eval` quota exemption (§6), and can be excluded from prod
   dashboards. No IAM or workflow rework at swap time. ⚠️ Bumping
   the eval user's `token_version` silently kills this token and the
   nightly run starts 401ing — treat eval 401s as an alarm, re-mint
   and re-put the secret after any deliberate bump.
2. **Shared `response.usage` metering.** Eval's token-spend metric and
   this phase's token quota both read the Anthropic `usage` field and
   both touch `llm_client.py`. Whoever lands second inherits a small
   merge — factor the usage read into one helper so it isn't written
   twice.

Neither blocks parallel work; both need a five-minute coordination
before the *second* phase merges.

---

## 8. Milestones (M.M.1 through M.M.5)

Sequential; the phase is short enough that parallel work isn't
worth the coordination.

### M.M.1 — Data model + Google OAuth backend (~3 days)

- Add `PRAGMA foreign_keys = ON` to
  `db.py::_configure_sqlite_pragmas` (§4.1) — *first*, so cascades and
  deletion actually work.
- Migrations for `users.email` (+ index after backfill),
  `users.oauth_google_sub` (plain column + separate unique index — no
  inline `UNIQUE`), `users.token_version`, `users.is_eval`,
  `usage_counters`, `telegram_bindings`
- Google Cloud setup: OAuth client + consent screen set to "In
  production" (non-sensitive scopes — no review needed, no shadow
  test-user allowlist; §4.2 note). id_token verification via
  `google-auth`, not hand-rolled (§4.2 note)
- `GET /auth/google/start` + `GET /auth/google/callback` — with
  `state` cookie, PKCE `code_verifier` persistence, and
  `email_verified` enforcement (§4.2)
- `get_current_user` dependency — `Header(None)` → 401, `token_version`
  revocation check (§4.3)
- JWT mint + verify (`sub` as string)
- `add-user.sh` + `remove-user.sh` (explicit per-table deletes, §5.2);
  shared `delete_user()` used by both the script and `DELETE /account`
- Seed Sabya as user_id=1 with email + admin flag
- **Set the Anthropic Console monthly spend limit (§6.1) before any
  friend is onboarded** — the aggregate backstop, 2-minute config
- Unit tests for JWT (incl. revocation via `token_version`), OAuth
  callback (mocked id_token, incl. `email_verified=false` → 403 and
  bad-`state` → reject), quota enforcement, and `delete_user()`
  leaving zero orphaned rows across all tables + Chroma

#### M.M.1 ✅ What shipped (2026-07-14 → 2026-07-21)

Five substeps landed as four merged PRs (`M.M.1.a`, `.c`, `.d` in
BrainTwin + `.b` in BrainTwinCDK for the SSM param names) plus this
doc-only pass (`.e`). All on `main`; **NOT yet deployed to prod** —
M.M.2 hasn't flipped routes to require the new JWT, so deploying
M.M.1 today is a safe drop-in with zero user-facing behaviour change.

Estimated ~3 days of focused work; elapsed ~1 week (2026-07-14 →
2026-07-21) because sessions were interleaved with review passes and
external Google Cloud setup. Actual coding time within the estimate.

##### M.M.1.a — Data model + repositories (2026-07-14, PR #33)

| Choice | What we did |
|--------|-------------|
| Schema evolution mechanism | **Extended the existing `_PENDING_COLUMN_ADDS` sweep in `backend/storage/db.py`** rather than pulling in Alembic. The Phase 3 codebase never adopted Alembic; adding it for 5 columns felt out of proportion. The sweep is idempotent (CREATE-IF-NOT-EXISTS pattern) and runs on every startup. |
| `users` table extension | 5 new columns: `oauth_google_sub`, `added_at`, `is_admin`, `token_version`, `is_eval`. `email` already existed from Phase 3 (TEXT UNIQUE NOT NULL) so the design doc's ALTER for email was skipped — spec error caught during implementation. |
| `users_oauth_sub_idx` | Named UNIQUE INDEX created via a new `_PENDING_INDEX_ADDS` sweep, called from `init_db` AFTER `_PENDING_COLUMN_ADDS` so the index-target column is guaranteed to exist. Codex Fix 1 — SQLite `ALTER TABLE` forbids inline `UNIQUE`. |
| `foreign_keys=ON` pragma | Added to `_configure_sqlite_pragmas`. Codex Fix 2. Without it, every `ForeignKey(...)` declaration in `schema.py` was silently unenforced; ON DELETE CASCADE was a no-op. Defense-in-depth guard against a future `delete_user()` implementation missing a child table. |
| `usage_counters` table | New (`user_id`, `date_utc`) composite-PK table for per-user daily rate-limit accounting. Backs M.M.2's `check_quota()`. |
| `telegram_bindings` table | New `telegram_user_id` → `user_id` mapping, replacing the M.7.5 shared allowlist. Review fix: added UNIQUE index on `user_id` too, making the mapping 1:1 in both directions (without it, one user could hold two bindings and `get_by_user()` was nondeterministic). |
| `delete_user()` cascade | Walks every user-owned table in FK-safe order (chunks_junctions → chunks → hydrations/enrichments → captures → usage_counters → telegram_bindings → users). Existing FKs don't carry ON DELETE CASCADE and SQLite can't ALTER them to add it; explicit walk + `foreign_keys=ON` guard is the correct pattern. |
| `bump_token_version()` | Atomic `UPDATE ... SET token_version = token_version + 1 RETURNING`. Codex Fix 3 — the *only* way to invalidate stateless JWTs before their `exp`. `get_current_user` (built in M.M.1.c) compares JWT's `tv` claim against this. |
| Atomic quota increments | `usage_counters.bump()` uses `INSERT ... ON CONFLICT DO UPDATE SET x = x + n` (Fable §6 + review fix — plain UPDATE matched 0 rows and silently dropped spend across UTC-midnight rollover). Never read-modify-write in Python. |
| Admin seed | `_ensure_default_user` in `backend/main.py` now creates `user_id=1` with `is_admin=True` on fresh installs and backfills `is_admin` on the existing pre-4.1 row (review fix — the ADD COLUMN sweep initialises to 0, so without this Sabya would ship locked out of future admin-gated routes). |
| Test surface | 13 tests in `tests/test_storage_mm1a.py` — pragma-is-on, migration idempotency, unique-oauth-sub-index behavior (including multi-NULL), user repo methods (get_by_oauth_sub, bump_token_version, delete_user idempotency), the full cascade-delete graph, usage counters (get_or_create + bump-creates-on-missing regression), telegram bindings (1:1 both directions). |

##### M.M.1.b — Google Cloud OAuth + SSM secrets (2026-07-14)

| Choice | What we did |
|--------|-------------|
| Google Cloud OAuth client | Created in `console.cloud.google.com` under project `braintwin`. Redirect URIs: `http://localhost:8000/auth/google/callback` (dev) + `https://braintwin.net/auth/google/callback` (prod). Scopes: `openid` + `email` only. |
| Consent screen publishing status | **"In production"** immediately — Fable §4.2. With only non-sensitive scopes, Google does NOT require app verification. This avoids the 100-user test-user allowlist and the "This app hasn't been verified" warning screen that would otherwise ruin the friend-onboarding UX. |
| SSM params (6 total) | `google_oauth_client_id`, `google_oauth_client_secret`, `jwt_secret`, `eval_bearer_token`, `join_slug` (5 from original design) + `google_oauth_redirect_uri` (Fable review fix — code default is localhost, prod OAuth would have hit `redirect_uri_mismatch` without this). All added to `BrainTwinCDK/scripts/put-secrets.sh` via commit `3bfbc2f`. |
| `eval_bearer_token` Day-0 value | Set to the current shared bearer's value (Fable §7.4). Swaps to the eval user's long-lived JWT after M.M.1.d — same SSM param name, so no IAM or workflow rework at swap time. |
| Retired param | `/braintwin/allowed_telegram_user_ids` removed from `put-secrets.sh` in the same commit. The shared allowlist model is replaced by `telegram_bindings` from M.M.4 onward. |
| No CDK changes | The M.10 discovery mechanism from Phase 4.0.6.1 automatically picks up any `/braintwin/*` param at boot and injects it as an env var — zero CDK code needed. |
| `.env` (local dev) parity | Local dev reads `.env`, not SSM. The M.M.1.d local smoke doc walks through fetching all 6 values via `aws ssm get-parameter --with-decryption` and pasting into `.env`. |

##### M.M.1.c — JWT + PKCE + `get_current_user` (2026-07-14, PR #34)

| Choice | What we did |
|--------|-------------|
| Package refactor | Promoted single-module `backend/auth.py` → package `backend/auth/` with `bearer.py` (the M.1 shared-bearer dep, retained until M.M.2), `jwt.py`, `pkce.py`, `deps.py`. `__init__.py` re-exports `require_bearer_token` + `get_current_user` + `settings`, so every pre-4.1 importer keeps working — nothing outside the package needed to change. |
| JWT library | `pyjwt==2.10.1`, HS256 symmetric-key only. No RS256/asymmetric extras — the backend both mints and verifies with the same secret; no third-party signature path in scope. |
| JWT_SECRET floor | ≥ 32 chars enforced at `_get_secret()` — every mint AND decode path passes through, so no code path can accidentally sign with a weak key. Fable review fix — HS256 is only as strong as the secret's entropy; `openssl rand -hex 32` (64 chars) is the recommended generation. |
| Algorithm pinning | `pyjwt.decode(token, secret, algorithms=["HS256"])`. Rejects the classic `alg: "none"` unsigned-token CVE class where naive decoders trust the token's own `alg` header. |
| JWT claims | `sub=str(user.id)` (RFC 7519 says string; cast on mint, `int()` on read), `email` (debug convenience; NOT trusted at read — `get_current_user` re-reads from DB), `tv=user.token_version` (Codex Fix 3 revocation), `iat`, `exp` (30-day TTL = `jwt_ttl_minutes` default). |
| PKCE (RFC 7636) | `generate_verifier` (43-char base64url from 32 bytes CSPRNG), `derive_challenge` (S256 = base64url(SHA256(verifier))), `generate_state` (same generator, CSRF token). `oauth_state` table persists (state → code_verifier) across the Google redirect. |
| `consume_state` atomic | Single `DELETE ... RETURNING` (Fable review fix — original SELECT-then-DELETE let two concurrent callbacks replaying the same state BOTH receive the verifier under WAL, defeating anti-replay). Works on SQLite ≥ 3.35 and Postgres unchanged. |
| `store_state` self-cleaning | Piggybacks `sweep_expired` on every call (Fable review fix — `store_state` is the table's only growth path and `/auth/google/start` is unauthenticated, so reaping on insert bounds the table by construction with no external nightly task). |
| `get_current_user` failure paths | 6 numbered paths, each mapping to the correct status: missing/wrong-scheme/empty bearer → 401 (Codex Fix 5: `Header(None)` NOT `Header(...)`, otherwise FastAPI 422s); expired → 401; invalid → 401; sub OR tv not int → 401 (review fix — tv was unguarded, would 500); user_id not in DB → 403; tv mismatch → 401. Plus a config path: unset/short JWT_SECRET → 503 "auth not configured" (Fable review fix — mirrors bearer.py's fail-closed contract). |
| Test surface | 36 tests total. `test_auth_jwt.py` (12: roundtrip, TTL, unset+short secret RuntimeError, expiry, wrong-secret, alg=none rejection, malformed, missing sub/tv guards). `test_auth_pkce.py` (12: verifier/state shape+randomness+independence, deterministic S256, store→consume roundtrip via TWO fresh sessions, one-shot anti-replay, expired-state returns None + deletes row, `store_state` reaps regression lock, `sweep_expired` selectivity). `test_auth_deps.py` (12: happy path, 401 on missing/wrong-scheme/no-token/expired/invalid-sig/malformed/sub-not-int/tv-not-int/tv-mismatch, 503 on unset-secret, 403 on deleted user). |

##### M.M.1.d — Google OAuth routes + local E2E smoke (2026-07-21, PR #35)

| Choice | What we did |
|--------|-------------|
| Google id_token verification library | `google-auth==2.44.0` — audited library for JWKS fetch + RS256 sig verify + iss/aud/exp checks. Deliberately not hand-rolled per Fable §4.2 ("learning becomes risk" on an auth boundary). |
| `email_verified` enforcement | WE enforce this — google-auth doesn't. Codex Fix 4 — impersonation defense: without it, an attacker registers a Google account claiming friend@x.com but never clicks the verification link, and we'd sign them in as the real friend. |
| Clock-skew tolerance | 10s (Fable review fix). google-auth's default is 0 — zero tolerance — which 401s legitimate sign-ins when our server clock is even 1s behind Google's. The classic "OAuth mysteriously stopped working overnight" bug. |
| Route packaging | FastAPI APIRouter in `backend/auth/routes.py`, `app.include_router(oauth_router)` in main.py. Keeps the OAuth surface in one file; future `/auth/*` routes (logout, whoami, DELETE /account) drop in here. |
| CSRF state-cookie binding | `bt_oauth_state` cookie set in `/start` = state value; `/callback` requires the state query param to match the cookie via `secrets.compare_digest`. Fable review fix — critical login-CSRF hole: without it, ANY browser with any unconsumed state passes the callback check, letting an attacker log the victim into the attacker's account via a phishing link. Cookie is HttpOnly + SameSite=Lax + path=/auth/google + TTL = state TTL + Secure in prod (auto-derived from redirect_uri scheme). |
| Transaction commit for anti-replay | `await session.commit()` right after `consume_state` returns success (Fable review fix). Without it, a downstream HTTPException (bad code, network flake) rolls back the whole session and RESTORES the deleted `oauth_state` row — anti-replay silently gone. Committing the delete on its own detaches replay-protection from the outcome. |
| `IdTokenUnavailable` vs `InvalidIdToken` | New exception for `google-auth.TransportError` (JWKS unreachable) — maps to 503 retryable, not 401 "your credential is bad" (Fable review fix). Correct HTTP semantics so client libraries retry appropriately. |
| Account-rebind detection | If `users.oauth_google_sub` is already set to a DIFFERENT sub than the incoming id_token's, reject with 403 (Fable review fix). Real edge case — Google's docs say sub is stable but email isn't; without this, a re-issued email could silently hijack another friend's account and all their captures. |
| Malformed token-response body | `token_response.json()` wrapped in try/except ValueError → 401 "google response malformed" (Fable review fix). Most likely cause: egress proxy / captive portal returning HTML. Was 500 before. |
| Backfill logic | On first successful sign-in for an email-allowlisted user (no `sub` yet), `UserRepository.set_oauth_sub()` binds the sub so future sign-ins short-circuit to `get_by_oauth_sub`. Uses `dataclasses.replace()` on the in-memory frozen User for consistency. |
| Success UX | Redirect to `LANDING_PATH#token=<jwt>` — fragment (not query) so the token doesn't hit access logs. Client-side JS grabs from `window.location.hash`. State cookie deleted on the way out. |
| Local E2E smoke doc | `docs/phase4.1-m1d-local-smoke.md` — 7-step runbook: prereqs → tests → uvicorn boot → hit /start in real browser → sign in with real Google → land back with JWT → curl temporary /whoami route → verify oauth_google_sub backfilled → verify revocation via bump_token_version. Includes failure-mode troubleshooting for each step. |
| Test surface | 20 tests total. `test_auth_google.py` (8: happy path, unset client_id RuntimeError, `email_verified=false` regression lock, missing email_verified defaults to reject, missing sub / missing email guards, google-auth ValueError translation, TransportError → IdTokenUnavailable regression lock). `test_auth_routes.py` (12 via FastAPI TestClient with mocked httpx + google-auth: happy path returns 302 with JWT in fragment + JWT decodes, /start has all required params + persists state row, unset config → 503, callback google-error → 400, missing code/state → 400, unknown/expired state → 400, google token exchange non-200 → 401, InvalidIdToken → 401, email not in allowlist → 403, first-signin sub backfill, unset client_secret → 503). |

##### M.M.1.e — Milestone wrap-up (this section, 2026-07-21)

| Choice | What we did |
|--------|-------------|
| Design doc "what shipped" | This §8 subsection. Per-substep tables covering every choice made and every fix from Codex + Fable review cycles. |
| Baseline `cdk synth` | Confirmed no infrastructure changes leaked in — M.M.1.a-d are all backend-only. `BrainTwinCDK` only touched `scripts/put-secrets.sh` (parameter names, not CDK constructs). No stack changes, no deploy required at M.M.1's close. |
| No prod deploy | Deliberate. The routes still `Depends(require_bearer_token)` from M.1; the new JWT + `get_current_user` are wired but unused by any production route. M.M.2 does the mechanical Depends() flip — THAT's the deploy that changes behavior. |

##### Codex + Fable review fixes applied across M.M.1

All 6 of Codex's original fixes are in the shipped code:

| # | Fix | Where it landed |
|---|-----|-----------------|
| 1 | `oauth_google_sub` as plain column + separate `CREATE UNIQUE INDEX` (SQLite ALTER TABLE forbids inline UNIQUE) | M.M.1.a schema.py + `_PENDING_INDEX_ADDS` in db.py |
| 2 | `PRAGMA foreign_keys = ON` per-connection | M.M.1.a `_configure_sqlite_pragmas` |
| 3 | `token_version` JWT revocation via atomic UPDATE | M.M.1.a `bump_token_version` + M.M.1.c `get_current_user` `tv` check |
| 4 | PKCE `code_verifier` persistence + `email_verified` enforcement | M.M.1.c `oauth_state` table + M.M.1.d `verify_google_id_token` |
| 5 | `Header(None)` NOT `Header(...)` — 401 not 422 on missing bearer | M.M.1.c `get_current_user` |
| 6 | Quota gate BEFORE persistence + reactive token cap | Data model shipped (M.M.1.a `usage_counters`); enforcement is M.M.2 |

Plus Fable's cross-phase review passes added: `is_eval` quota exemption, `/braintwin/eval_bearer_token` SSM param + Day-0 value, `google-auth` for id_token verification, consent screen "In production", `hmac.compare_digest` for join_slug (M.M.5), atomic `INSERT ... ON CONFLICT` for quota increments, bundle M.10/M.11 CDK diff into 4.1 EBS dance (deferred to M.M.2 deploy), M.M.4 estimate bump 0.5→1 day.

Plus Fable's M.M.1.c/d review passes added: JWT_SECRET 32-char floor, atomic `DELETE ... RETURNING` for `consume_state` (anti-replay under WAL), `store_state` self-reaping, transaction commit for anti-replay, CSRF state-cookie double-submit binding, clock-skew 10s tolerance, `IdTokenUnavailable` distinct from `InvalidIdToken` (503 vs 401), account-rebind 403, malformed-body 401 vs 500, `_extract_bearer` dedup, `.env.example` documentation, telegram_bindings 1:1 UNIQUE index.

##### Aggregate M.M.1 test count

**~76 tests** across the 4 M.M.1.* substeps in the BrainTwin repo:
- 13 in `test_storage_mm1a.py` (data model + repos)
- 12 in `test_auth_jwt.py` (M.M.1.c)
- 12 in `test_auth_pkce.py` (M.M.1.c)
- 12 in `test_auth_deps.py` (M.M.1.c)
- 8 in `test_auth_google.py` (M.M.1.d)
- 12 in `test_auth_routes.py` (M.M.1.d)
- ~7 additional in pre-existing test files (test_auth.py, test_recall_endpoint.py) that continued passing through the package refactor

All green on Sabya's Mac; the M.M.1.d local smoke was also driven manually end-to-end against real Google.

##### Deferred to M.M.2

- **Route flip.** Every `Depends(require_bearer_token)` on `/capture`, `/recall`, `/stats`, `/failures` becomes `Depends(get_current_user)`. Every hardcoded `user_id = DEFAULT_USER_ID` becomes `user_id = user.id`. Small mechanical diff; touches every route file.
- **`check_quota()` enforcement.** The `usage_counters` machinery is built; the "call it before every /capture and /recall" wiring is M.M.2.
- **`DELETE /account`.** The `UserRepository.delete_user` cascade is built; the route that exposes it (plus the `bump_token_version` to kill the caller's own JWT immediately) is M.M.2.
- **Anthropic Console monthly spend cap.** External step (2-minute config on `console.anthropic.com`). Not code; scheduled for right before the first friend is onboarded (M.M.5).

### M.M.2 — Route migration + quota enforcement (~1 day)

- Every route that uses `DEFAULT_USER_ID` grows a
  `user: User = Depends(get_current_user)` parameter
- `check_quota()` inserted before each Anthropic call — with the
  `is_eval` exemption and atomic DB-side increments (§6)
- `DELETE /account` endpoint
- Integration test: sign-in → capture → recall → delete cycle

### M.M.3 — Chrome extension sign-in flow (~1 day)

- "Sign in" button, opens new tab at `/auth/google/start?next=extension`
- Callback page shows JWT + "Copy" button (paste UX for MVP)
- Extension stores JWT in `chrome.storage.local`
- All requests carry `Authorization: Bearer <jwt>`
- Signed-out state clearly indicated; sign-out button

### M.M.4 — Telegram bot `/link` flow + per-user JWT minting (~1 day)

- Web UI shows generated link code after sign-in
- Bot handles `/link <code>` — looks up code in `users`, writes to
  `telegram_bindings`
- Bot handles `/unlink` — deletes the binding
- All bot messages now route to `telegram_bindings.user_id` instead
  of the shared allowlist
- **`mint_user_jwt(telegram_user_id)` helper in the bot** (§4.5.1):
  binding lookup + `token_version` read at mint time, 5-min TTL,
  cached until expiry; `CaptureClient` sends it per request instead
  of the shared bearer. Unlinked sender → "send /link" reply, no
  backend call. Unit test: minted token passes `get_current_user`;
  bumped `token_version` fails within TTL grace
- Delete the old `ALLOWED_TELEGRAM_USER_IDS` env var + SSM param

### M.M.5 — Web pages + first-friend onboarding (~1-1.5 days)

Web content stack: **vanilla HTML + Tailwind via CDN + a few
lines of vanilla JS** (§5.0). Location: `BrainTwin/web/` in the
same repo, served by Caddy (static) + one FastAPI route (dynamic
slug validation).

- **Static pages** in `BrainTwin/web/`:
  - `index.html` — landing (§5.1). Sign-in button only. No
    request-access CTA visible. No screenshot / marketing.
  - `privacy.html` — the privacy note (§5.3). Plain-English, short.
  - `join.html` — the onboarding walkthrough (§5.4). Steps 1-5,
    contact info (sabya.bisoyi@gmail.com), "please don't share
    this URL" note.
  - `403-not-approved.html` — the friendly 403 shown to Google
    users who signed in but aren't on the allowlist. Discloses
    sabya.bisoyi@gmail.com as the request channel.
- **Caddy config** — bind-mount `BrainTwin/web/` into the Caddy
  container; site block serves static files for `/`, `/privacy`,
  and `/403-not-approved.html`. Everything else proxies to app:8000.
- **FastAPI dynamic route** — `GET /join/{slug}` reads `$JOIN_SLUG`
  from env (SSM-discovered via M.10), returns `web/join.html` if
  match else 404. See §5.4.
- **Provision the slug**:
  `./scripts/put-secrets.sh join_slug "friends-<random-token>"`.
  Refresh script picks it up automatically.
- **Real friend onboarding**:
  `./scripts/add-user.sh friend@gmail.com "Ramesh"` against prod.
  Send friend the `braintwin.net/join/<slug>` URL via
  sabya.bisoyi@gmail.com. Walk them through in real time.
- Friend signs in, does one capture, does one recall, links
  Telegram — end-to-end smoke.
- Take a screenshot of the friend using it for portfolio purposes.

Total estimate: **~7-7.5 days of focused work** (M.M.1 bumped from 2
to 3 days once OAuth is hardened — state/PKCE, `email_verified`, the
FK pragma, `token_version` revocation, and the spend-cap config are
real work, not happy-path; M.M.4 bumped from 0.5 to 1 day for the
per-user JWT minting, §4.5.1). Treat this as optimistic and carry a
**~9 day** contingency: the security-sensitive OAuth path and the
MV3 extension sign-in are the two most likely to overrun, and this is
an auth boundary — getting it wrong is worse than shipping it a day
late.

**Actual as of M.M.1 close (2026-07-21):** M.M.1's ~3-day estimate held
in coded-time terms; wall-clock was ~1 week because of session
scheduling + external Google Cloud setup + two full Fable review
rounds (M.M.1.c and M.M.1.d). Coding effort per substep tracked to
plan; the review cycles caught 3 would-be production bugs (login-CSRF
state binding, `consume_state` race under WAL, clock-skew default of 0)
and ~10 hardening improvements, none of which were in the original
estimate — so total-elapsed being a week is closer to "estimate + review
cost" than "underestimated coding." Rolling that lesson into M.M.2:
assume a same-shape review round after the route flip lands.

---

## 9. What we learn from this phase

Beyond just shipping multi-user:

- **Real-user quality signal.** The friend's recall complaints
  become rows in the Phase 4.0.5 golden set. Two phases feed each
  other.
- **Anthropic cost at 2 users.** Per-user quotas give us actual
  numbers on "what does one active user cost per month." Feeds the
  decision on whether to open the beta wider.
- **What's genuinely single-user in the UX.** The
  `ConversationStore` is in-memory per process today. Two users
  running concurrent recall don't conflict, but a restart drops
  both. Whether that matters is a real-user-driven question.

---

## 10. Open decisions deferred

- **How does the friend actually authenticate the Chrome extension?**
  Paste-the-JWT is the MVP shape. Chrome's `chrome.identity` API is
  the proper answer; ~1 week of its own. Do the proper flow only
  if the paste UX is a real friction point after the first friend.
- **Do we host our own JWT verification, or use Cloudflare Access?**
  Cloudflare Access can offload the entire OAuth dance for a small
  monthly fee (or free at low seat counts). Cleaner for
  observability + audit, more infra to reason about. Deferred:
  MVP does its own JWT.
- **When does the friend beta become an "opt-in from anywhere"
  beta?** Cost story has to change first (either paid, or a
  hard-cap sponsored tier). Not this phase.
- **How do we revoke one user's live session?** Bump
  `users.token_version` (§4.3) — every outstanding JWT for that user
  fails its `tv` check on the next request. `oauth_google_sub` is NOT
  a revocation lever (nulling it doesn't touch a live JWT); it only
  re-binds the Google `sub` on next sign-in. Global "log everyone out"
  is still available via rotating `/braintwin/jwt_secret`.

---

## 11. Success criteria

Phase 4.1 is done when:

- A friend I've never given credentials to can sign in with their
  own Google account, capture an article from Chrome, and recall
  it from the extension popup — end-to-end, no Sabya-side
  intervention beyond running `add-user.sh` once
- The friend can link their Telegram to their app account and
  forward a URL to the bot; it lands as their capture, not mine
- A Google user NOT on the allowlist gets a clear 403 with
  instructions on how to request access, and their attempt does
  not touch Anthropic or Chroma
- Per-user daily quotas actually block a hypothetical caffeinated
  friend at the 101st capture of the day
- I can delete a friend's account with one CLI command and verify
  their captures + chunks + Chroma vectors are gone
- The Phase 4.1 deploy uses one EBS-deadlock dance (adding OAuth
  env vars is a user-data change; everything else after that ships
  via refresh)

---

## 12. References

- Main cloud deployment design: `phase4.0.6-deployment-design.md`
  (§14.1 in particular — the EBS-deadlock that becomes worse with
  real users)
- Polish phase: `phase4.0.6.1-polish-design.md` (M.7.5 established
  the pattern of adding SSM parameters via the discovery loop,
  which M.M.1 leans on for the Google OAuth secrets)
- Eval phase: `phase4.0.5-eval-design.md` (the friend's recall
  complaints become golden-set rows)
- Local-first ideation (out-of-band): `local-first-design.md` —
  §7 there notes that multi-user is **fundamentally incompatible**
  with local-first. Choosing 4.1 explicitly closes that direction
  for the primary product. Local-first, if it ever ships, is a
  separate product with a separate roadmap.

---

*Author: Sabya (with Claude as design partner). Created 2026-06-29
after a friend from India asked to try BrainTwin, and I realized
the shared-bearer-token architecture had zero honest answer.
Scoped deliberately as a private beta with allowlist-gated OAuth
— open enough to actually onboard the friend, closed enough that
strangers can't drive my Anthropic bill. Runs in parallel with
Phase 4.0.5 (eval); the two phases are independent and either can
land first.*

*Revised 2026-06-29 (later same day) after a read-through: (1) main
landing page (§5.1) simplified to a sign-in gate with no
request-access CTA visible — the beta shouldn't feel like a gated
marketing page; (2) added §5.0 tech-stack decision (vanilla HTML +
Tailwind CDN + BrainTwin/web/); (3) added §5.4 for a hidden
onboarding URL at `/join/<slug>` whose slug is a runtime SSM
secret (BrainTwin repo is public → any hardcoded slug is
discoverable, so the slug can't live in code); (4) request-access
channel updated from Telegram to email (sabya.bisoyi@gmail.com)
throughout — email is universal, Telegram is not; (5) M.M.5
milestone rescoped to reflect the four static pages + one dynamic
route.*

*Revised 2026-07-01 after an engineer+manager review pass grounded in
the current storage layer. Fixes: (1) `oauth_google_sub` can't be a
`UNIQUE` ADD COLUMN on SQLite — split into a plain column + separate
unique index; (2) `PRAGMA foreign_keys` is OFF in
`_configure_sqlite_pragmas`, so every `ON DELETE CASCADE` was a silent
no-op — M.M.1 now enables it and deletion uses explicit per-table
DELETEs behind a shared `delete_user()`; (3) the revocation story was
wrong (nulling `oauth_google_sub` doesn't kill a live JWT) — replaced
with a `token_version` claim checked in `get_current_user`; (4) OAuth
flow made complete — `state`/PKCE-`code_verifier` persistence and
`email_verified` enforcement; (5) `Header(...)`→`Header(None)` for a
clean 401; (6) §6 reconciled the over-quota-capture 429 against the
non-blocking-capture contract and flagged the token cap as reactive;
(7) added §6.1 — a global Anthropic Console spend cap as the aggregate
backstop the per-user quotas lack (AWS budget alarms don't see
Anthropic spend); (8) added §7.4 — the auth-contract + usage-metering
coupling with Phase 4.0.5 that the "independent" claim understated;
(9) M.M.1 → ~3 days and total → ~6.5-7 (contingency ~8-9), since
hardened OAuth is real work on an auth boundary. Dollar totals
deferred until a real one-month bill exists.*

*Revised 2026-07-02 after a cross-phase integration review (both this
doc and the eval doc, implemented concurrently): (1) added §4.5.1 —
the bot→backend auth contract was unspecified; post-M.M.2 the bot's
shared bearer stops working, so the bot now mints short-lived
per-user JWTs with the shared `JWT_SECRET` (one validator, many
minters; M.M.4 0.5→1 day, total ~7-7.5); (2) added the `is_eval`
quota exemption (§4.1, §6, M.M.2) — the weekly eval run of ~90-110
recalls would otherwise trip the 50/day cap every Sunday; (3) §7.4
eval-token contract made concrete — `/braintwin/eval_bearer_token`
created Day 0 (value = shared bearer, swapped to the dedicated eval
user's JWT post-landing), with the token_version-kills-eval warning;
(4) §4.2 notes — id_token verification via `google-auth`, consent
screen to "In production" (avoids the shadow test-user allowlist);
(5) §5.4 slug compare → `hmac.compare_digest`; §6 counter increments
made atomic DB-side; (6) §7.2 — bundle the undeployed M.10/M.11 CDK
diff into the Phase 4.1 EBS dance so one instance replacement covers
both.*

*Revised 2026-07-21 at M.M.1 close (M.M.1.e): added §8 "What shipped in
M.M.1" — per-substep tables for `a`-`d` documenting every choice made
and every fix landed from Codex (all 6) + Fable (2 review passes on
`c` and `d`), plus test surface totals, cross-substep Codex/Fable fix
tracker, and the deferred-to-M.M.2 list. Also revised the total-
estimate footer with actual elapsed vs planned: ~3-day M.M.1 coding
estimate held; ~1-week wall-clock reflects review round overhead, not
underestimated coding. Rolling that lesson into M.M.2's planning:
assume a same-shape review round after the route flip lands.*
