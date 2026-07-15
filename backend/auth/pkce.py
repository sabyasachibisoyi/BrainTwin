"""PKCE — Proof Key for Code Exchange (RFC 7636). Phase 4.1 M.M.1.c.

Standard defense for OAuth 2.0 authorization-code flows on clients
that can't hold a client secret safely (mobile / SPA). Google requires
it for public clients and recommends it for confidential clients too;
we opt in even though our backend IS confidential, because the marginal
cost is negligible and it defends against a stolen auth-code being
redeemed by an attacker who intercepted the redirect (e.g. malicious
Chrome extension).

Flow:
    1. `GET /auth/google/start` (backend):
         - verifier   = generate_verifier()          — 43-char random
         - challenge  = derive_challenge(verifier)   — SHA256(v) base64url
         - state      = generate_state()             — 43-char random (CSRF)
         - store_state(session, state=state, code_verifier=verifier)
         - Redirect user → Google, sending `state` + `code_challenge`
                            + `code_challenge_method=S256`
    2. Google → user → `GET /auth/google/callback?code=…&state=…`:
         - verifier = await consume_state(session, state)   — one-shot
         - if verifier is None → 400 (state expired / never issued /
                                       already consumed = replay attempt)
         - Exchange (code, verifier) with Google's token endpoint;
           Google recomputes SHA256(verifier) and rejects if it doesn't
           match the challenge from step 1.

Design decisions:
    - S256 only, no `plain` fallback. Google requires S256 for public
      clients and it's the only method worth using; `plain` sends the
      secret in the auth URL, which is the whole thing PKCE is trying
      to prevent. Hard-code `S256` in the query string in M.M.1.d.
    - state and verifier share the same generator (32 bytes → 43
      base64url chars). RFC 7636 accepts 43-128 chars for the verifier;
      shorter is fine as long as entropy is high.
    - State stored in SQLite (see `oauth_state` in schema.py) because
      cookies don't survive Google's cross-origin redirect back to us
      reliably under strict-SameSite browsers. Table stays tiny (~one
      row per active sign-in attempt, 10-minute TTL) and gets swept.
    - `consume_state` is one-shot: even if the row hasn't expired, it
      gets deleted on read. Prevents an attacker with a stolen `state`
      from replaying the callback.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.storage.schema import oauth_state


# 32 bytes → 43 base64url characters (after stripping padding). RFC 7636
# specifies a min of 43 chars, max 128; more bytes gives longer output
# for no security benefit given a good CSPRNG.
_ENTROPY_BYTES = 32


# --------------------------------------------------------------------
# Pure crypto helpers — no DB access, no async, trivially unit-testable
# --------------------------------------------------------------------

def _b64url(data: bytes) -> str:
    """URL-safe base64 without padding — the encoding PKCE + OAuth expects.

    RFC 4648 §5. `urlsafe_b64encode` swaps `+/` for `-_`; stripping `=`
    matches what Google's docs show. All comparisons downstream (Google's
    server-side SHA256(verifier) check) recompute the same encoding, so
    padding-strip is fine.
    """
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def generate_verifier() -> str:
    """RFC 7636 code_verifier: high-entropy URL-safe random string.

    `secrets.token_bytes` uses the OS CSPRNG (getrandom() on Linux/macOS,
    BCryptGenRandom on Windows). 32 bytes = 256 bits of entropy — far
    more than needed for a PKCE verifier (attacker gets one guess per
    auth flow) but consistent with what other library defaults use.
    """
    return _b64url(secrets.token_bytes(_ENTROPY_BYTES))


def derive_challenge(verifier: str) -> str:
    """S256 method: base64url(SHA256(verifier)).

    Deterministic given the same verifier — that's the whole point.
    Google's token endpoint recomputes this at exchange time and
    rejects if it doesn't match the `code_challenge` we sent in step 1.
    """
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def generate_state() -> str:
    """CSRF token for the auth URL. Independent of the verifier — same
    entropy source, different purpose (round-trip verification of the
    same-user assumption; PKCE protects the *code exchange* separately).
    """
    return _b64url(secrets.token_bytes(_ENTROPY_BYTES))


# --------------------------------------------------------------------
# oauth_state table access — the async, DB-touching layer
# --------------------------------------------------------------------

# Default TTL: 10 min. Google's own auth URL expires long before this,
# and a legit user needs at most a few seconds (or a couple of minutes
# if they got distracted and switched tabs). 10 min keeps the table
# tiny while giving generous slack for the "wait, where's my phone for
# 2FA?" case.
DEFAULT_STATE_TTL_MINUTES = 10


async def store_state(
    session: AsyncSession,
    *,
    state: str,
    code_verifier: str,
    ttl_minutes: int = DEFAULT_STATE_TTL_MINUTES,
) -> None:
    """Persist (state → code_verifier) for retrieval by the callback.

    Assumes state is unique per attempt (generate_state does the work).
    Does NOT commit — leaves that to the caller's session_scope so the
    whole /auth/google/start handler either commits atomically or rolls
    back.

    Piggybacks `sweep_expired` on every call: this function is the
    table's ONLY growth path, so reaping here bounds the table size by
    construction. /auth/google/start is unauthenticated, so without
    this an attacker hammering it would grow oauth_state without limit
    (abandoned sign-ins never reach consume_state). The sweep is an
    index range scan on idx_oauth_state_expires_at — cheap.
    """
    await sweep_expired(session)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(minutes=ttl_minutes)
    await session.execute(
        insert(oauth_state).values(
            state=state,
            code_verifier=code_verifier,
            created_at=now.isoformat(),
            expires_at=expires.isoformat(),
        )
    )


async def consume_state(session: AsyncSession, state: str) -> str | None:
    """Retrieve the verifier for a state and DELETE the row atomically.

    Returns None if:
      - state not found (never issued, or already consumed = replay
        attempt, or previously swept as expired)
      - state expired (row present but expires_at is past)

    Delete-on-consume is the standard security practice: prevents an
    attacker who intercepted the state value from replaying the callback
    with a code they somehow separately obtained. Even for the expired
    case, we delete the row so the table stays trim without a separate
    sweep having to catch it.

    Consumption is a single atomic `DELETE … RETURNING`: the DELETE
    takes the write lock, so exactly one caller can ever receive the
    verifier for a given state — two concurrent callbacks replaying
    the same state can't both win, even though each request runs in
    its own session and coroutines interleave across awaits. (A
    SELECT-then-DELETE would race under WAL, where readers don't
    block.) Works on SQLite ≥ 3.35 and Postgres unchanged.
    """
    now = datetime.now(timezone.utc).isoformat()
    result = await session.execute(
        delete(oauth_state)
        .where(oauth_state.c.state == state)
        .returning(oauth_state.c.code_verifier, oauth_state.c.expires_at)
    )
    row = result.first()
    if row is None:
        return None
    if row.expires_at < now:
        return None
    return row.code_verifier


async def sweep_expired(session: AsyncSession) -> int:
    """Delete all expired oauth_state rows. Returns count deleted.

    The idx_oauth_state_expires_at index makes the WHERE clause an
    index range scan — fast even if the table has accumulated
    thousands of abandoned attempts. Wired in via `store_state`, which
    calls this on every new sign-in attempt — the table's only growth
    path reaps as it grows, so no external nightly task is needed.
    """
    now = datetime.now(timezone.utc).isoformat()
    result = await session.execute(
        delete(oauth_state).where(oauth_state.c.expires_at < now)
    )
    return result.rowcount or 0


__all__ = [
    "DEFAULT_STATE_TTL_MINUTES",
    "generate_verifier",
    "derive_challenge",
    "generate_state",
    "store_state",
    "consume_state",
    "sweep_expired",
]
