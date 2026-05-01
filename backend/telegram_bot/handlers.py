"""Telegram message handlers — turns Telegram updates into CapturePayloads.

Decision 3 (locked): capture what *arrived* in your stream of consumption,
ignore your interpretive layer on top.

   Bare URL typed                      → capture URL
   Image (single or media-group)       → capture image + original sender's caption
   Image with your own added caption   → capture image, drop your caption
   Forwarded message                   → preserve forward_origin in metadata
   Plain text without URL or image     → ignore (it's your thought)
   Voice / audio / video               → reply "not supported yet" (Phase 1 scope)

Everything POSTs to the same /capture endpoint as the Chrome extension,
so the backend doesn't need a special code path.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from datetime import datetime, timedelta, timezone
from io import BytesIO
from typing import Any

from telegram import Message, Update, User
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from backend.config import settings
from backend.telegram_bot import state
from backend.telegram_bot.client import CaptureClient


logger = logging.getLogger(__name__)

# How long to wait after the first photo of a media-group arrives before
# we consider the album complete. Telegram delivers media-group items as
# separate updates within ~1s of each other.
MEDIA_GROUP_WINDOW_S = 2.0

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


# --- Auth ----------------------------------------------------------------

def _is_allowed(user: User | None) -> bool:
    if user is None:
        return False
    return user.id in settings.allowed_telegram_ids()


# --- Helpers -------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _platform_from_url(url: str) -> str:
    """Same hostname → platform mapping the Chrome extension uses."""
    host = url.lower()
    if "youtube.com" in host or "youtu.be" in host:
        return "youtube"
    if "twitter.com" in host or "x.com" in host:
        return "twitter"
    if "instagram.com" in host:
        return "instagram"
    if "reddit.com" in host:
        return "reddit"
    if "linkedin.com" in host:
        return "linkedin"
    if "facebook.com" in host:
        return "facebook"
    return "general"


def _forward_metadata(msg: Message) -> dict[str, Any]:
    """Extract Telegram forward attribution if present."""
    fo = getattr(msg, "forward_origin", None)
    if fo is None:
        return {}
    out: dict[str, Any] = {"forwarded": True}
    # Different forward_origin subclasses expose different fields; grab what we can.
    for attr in ("sender_user", "sender_chat", "sender_user_name", "chat", "author_signature"):
        val = getattr(fo, attr, None)
        if val is not None:
            try:
                out[f"forward_{attr}"] = getattr(val, "full_name", None) or getattr(val, "title", None) or str(val)
            except Exception:
                out[f"forward_{attr}"] = str(val)
    fdate = getattr(fo, "date", None)
    if fdate:
        out["forward_date"] = fdate.isoformat() if hasattr(fdate, "isoformat") else str(fdate)
    return out


async def _download_photo_b64(msg: Message) -> str | None:
    """Download the largest size of the photo and return data URL b64."""
    if not msg.photo:
        return None
    largest = msg.photo[-1]  # PhotoSize list is sorted small→large
    try:
        f = await largest.get_file()
        buf = BytesIO()
        await f.download_to_memory(out=buf)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return f"data:image/jpeg;base64,{b64}"
    except Exception:  # noqa: BLE001
        logger.exception("Failed to download photo file_id=%s", largest.file_id)
        return None


def _strip_user_caption_if_forward(msg: Message) -> str:
    """Decision 3 rule: if you added a caption to a forwarded image, drop it.

    Telegram doesn't tell us "this caption was added by the forwarder vs.
    inherited from the original" — but in practice, when you forward and
    add your own text, Telegram delivers it as the message's `caption`.
    Original sender captions on individual photos are lost during forward
    (for privacy). For media-group forwards, captions can travel.

    Pragmatic policy: if the message is a forward AND has a caption, treat
    the caption as your own layer and drop it. If the message is NOT a
    forward (e.g. you screenshotted a meme into the chat with no edit),
    keep the caption — it's part of what arrived.
    """
    if msg.caption is None:
        return ""
    if getattr(msg, "forward_origin", None) is not None:
        return ""  # your interpretive layer — drop
    return msg.caption


# --- /command handlers ---------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if user is None or chat is None:
        return
    name = user.first_name or "there"
    if _is_allowed(user):
        msg = (
            f"👋 Hi {name}. BrainTwin is active for you.\n\n"
            "Forward articles, images, or memes here — I'll capture them silently.\n"
            "Type /help for commands."
        )
    else:
        msg = (
            f"👋 Hi {name}.\n\n"
            f"Your Telegram user ID is {user.id}.\n\n"
            "To activate BrainTwin, paste this ID into ALLOWED_TELEGRAM_USER_IDS "
            "in your .env, then restart the bot."
        )
    # plain text — Markdown gets fragile with names containing _ or *
    await chat.send_message(msg)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    await chat.send_message(
        "*BrainTwin Telegram bot*\n\n"
        "Send / forward me anything you'd like remembered:\n"
        "• A URL (or text containing one) → I fetch the page\n"
        "• An image (single or album) → I capture it + any sender caption\n\n"
        "*Commands*\n"
        "/stats — capture totals\n"
        "/last — most recent capture\n"
        "/failures — last 10 failures\n"
        "/pause — stop processing\n"
        "/resume — start again\n"
        "/whoami — your Telegram user ID\n\n"
        "_Voice notes and your own typed thoughts are intentionally not captured._",
        parse_mode="Markdown",
    )


async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if user is None or chat is None:
        return
    await chat.send_message(f"Your Telegram user ID is {user.id}.")


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update.effective_user):
        return
    state.set_enabled(False)
    if update.effective_chat:
        await update.effective_chat.send_message("⏸ Paused. Send /resume to start again.")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update.effective_user):
        return
    state.set_enabled(True)
    if update.effective_chat:
        await update.effective_chat.send_message("▶️ Resumed.")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update.effective_user) or update.effective_chat is None:
        return
    client: CaptureClient = context.bot_data["capture_client"]
    try:
        resp = await client._client.get(settings.backend_capture_url.replace("/capture", "/stats"))
        body = resp.json()
        msg = (
            f"📊 Stats\n"
            f"Total captures: {body.get('total_captures', 0)}\n"
            f"Last capture: {body.get('last_capture') or 'never'}\n"
            f"Platforms: {body.get('platforms', {})}"
        )
    except Exception as e:  # noqa: BLE001
        msg = f"⚠️ Couldn't reach backend: {e}"
    await update.effective_chat.send_message(msg)


async def cmd_last(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update.effective_user) or update.effective_chat is None:
        return
    # Reuses /stats for now; a richer /last is a Phase 2 nice-to-have.
    await cmd_stats(update, context)


async def cmd_failures(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/failures` — last 10 failures with a per-phase breakdown.

    Phase 2 (Decision C) added a `phase` field on every failure row:
      - "capture"    → fetch / parse / process error (Phase 1 surfaces these
                       inline already; this command shows the history)
      - "enrichment" → Haiku call failed after retries (silent at capture
                       time per Decision C, surfaces here)
    """
    if not _is_allowed(update.effective_user) or update.effective_chat is None:
        return
    client: CaptureClient = context.bot_data["capture_client"]
    try:
        resp = await client._client.get(settings.backend_capture_url.replace("/capture", "/failures"))
        body = resp.json()
        recent = body.get("recent", [])
        total = body.get("total", 0)
        by_phase: dict[str, int] = body.get("by_phase") or {}

        if not recent:
            await update.effective_chat.send_message("✅ No recent failures.")
            return

        # Header: "⚠️ 8 failures (3 capture, 5 enrichment) — last 10:"
        if by_phase:
            breakdown = ", ".join(
                f"{n} {phase}" for phase, n in sorted(by_phase.items())
            )
            header = f"⚠️ {total} failures ({breakdown}) — last {len(recent)}:"
        else:
            header = f"⚠️ Last {len(recent)} failures (of {total} total):"

        lines = [header]
        for r in recent[-10:]:
            phase = r.get("phase", "capture")
            # Phase 2.5 Fix 1 — distinguish skipped (not-applicable) from
            # real enrichment failures and capture-side failures.
            if phase == "enrichment_skipped":
                tag = "[skipped]"
            elif phase == "enrichment":
                tag = "[enrich]"
            else:
                tag = f"[{r.get('source','?')}]"
            lines.append(
                f"• {r.get('timestamp','')} {tag} {r.get('reason','?')[:80]}"
            )
        await update.effective_chat.send_message("\n".join(lines))
    except Exception as e:  # noqa: BLE001
        await update.effective_chat.send_message(f"⚠️ Couldn't reach backend: {e}")


# --- Content handlers ----------------------------------------------------

async def _ack(msg: Message) -> Message | None:
    """Send a one-line ack back to the chat that the message arrived in.

    Logs any failure loudly — silently-swallowed reply errors hide a class
    of "the user thinks the bot is dead but the backend is happy" bugs.
    """
    try:
        return await msg.reply_text("📥 Captured")
    except Exception:  # noqa: BLE001
        logger.exception("Failed to send ack reply to chat=%s message=%s", msg.chat_id, msg.message_id)
        return None


async def _maybe_catchup_notice(msg: Message) -> None:
    """If a long gap has passed since the last processed message, tell the user."""
    last = state.get_last_processed_at()
    if last is None:
        return
    now = datetime.now(timezone.utc)
    gap = now - last
    if gap > timedelta(minutes=settings.telegram_catchup_gap_minutes):
        try:
            mins = int(gap.total_seconds() // 60)
            await msg.chat.send_message(
                f"📥 Caught up — {mins // 60}h{mins % 60:02d}m since the last capture. Draining now."
            )
        except Exception:  # noqa: BLE001
            pass


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Plain text — capture only if it contains a URL. Otherwise ignore (Decision 3)."""
    if not _is_allowed(update.effective_user):
        return
    if not state.is_enabled():
        return
    msg = update.message
    if msg is None or msg.text is None:
        return

    urls = URL_RE.findall(msg.text)
    if not urls:
        # Pure typed thought — silently ignored per Decision 3.
        return

    await _maybe_catchup_notice(msg)
    await msg.chat.send_action(ChatAction.TYPING)
    await _ack(msg)

    client: CaptureClient = context.bot_data["capture_client"]

    for url in urls:
        payload = {
            "url": url,
            "title": "Telegram link",          # backend's extractor will overwrite from <title>
            "platform": _platform_from_url(url),
            "content_type": "article",
            "text": "",                         # backend fetches the page itself
            "images": [],
            "timestamp": _now_iso(),
            "dwell_time_seconds": 0,
            "metadata": {
                "source": "telegram",
                "telegram_message_id": msg.message_id,
                "chat_id": msg.chat_id,
                **_forward_metadata(msg),
            },
        }
        ok, body = await client.post_capture(payload)
        if not ok:
            try:
                await msg.reply_text(f"⚠️ Couldn't process: {body}")
            except Exception:  # noqa: BLE001
                pass
        else:
            state.mark_processed()


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A single photo OR one item of a media-group."""
    if not _is_allowed(update.effective_user):
        return
    if not state.is_enabled():
        return
    msg = update.message
    if msg is None or not msg.photo:
        return

    # ----- Media group? Open a 2s window and batch -----
    media_group_id = getattr(msg, "media_group_id", None)
    if media_group_id:
        if state.seen_media_group(media_group_id):
            # Another item of an already-batched album. Append it.
            buf: list[Message] = context.application.bot_data.setdefault(
                f"_mg_{media_group_id}", []
            )
            buf.append(msg)
            return
        # First photo we see for this album — ack now, then wait for siblings.
        context.application.bot_data[f"_mg_{media_group_id}"] = [msg]
        await _maybe_catchup_notice(msg)
        await _ack(msg)
        await asyncio.sleep(MEDIA_GROUP_WINDOW_S)
        msgs: list[Message] = context.application.bot_data.pop(f"_mg_{media_group_id}", [msg])
        await _post_photo_batch(msgs, context, msg)
        return

    # ----- Single photo -----
    await _maybe_catchup_notice(msg)
    await _ack(msg)
    await _post_photo_batch([msg], context, msg)


async def _post_photo_batch(
    msgs: list[Message], context: ContextTypes.DEFAULT_TYPE, reply_to: Message
) -> None:
    """Download all photos in the batch, build one CapturePayload, POST it."""
    images: list[str] = []
    for m in msgs:
        b64 = await _download_photo_b64(m)
        if b64:
            images.append(b64)

    # Caption: prefer the original-sender caption (kept on non-forwarded messages),
    # drop the user's own added text if this was a forward.
    caption_parts: list[str] = []
    for m in msgs:
        c = _strip_user_caption_if_forward(m)
        if c:
            caption_parts.append(c)
    caption = "\n".join(caption_parts)

    head = msgs[0]
    payload = {
        "url": f"tg://message/{head.chat_id}/{head.message_id}",
        "title": (caption[:80] if caption else "Telegram image"),
        "platform": "telegram_image",
        "content_type": "image",
        "text": caption,
        "images": images,
        "timestamp": _now_iso(),
        "dwell_time_seconds": 0,
        "metadata": {
            "source": "telegram",
            "telegram_message_id": head.message_id,
            "chat_id": head.chat_id,
            "image_count": len(images),
            **_forward_metadata(head),
        },
    }

    client: CaptureClient = context.bot_data["capture_client"]
    ok, body = await client.post_capture(payload)
    if not ok:
        try:
            await reply_to.reply_text(f"⚠️ Couldn't process: {body}")
        except Exception:  # noqa: BLE001
            pass
    else:
        state.mark_processed()


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Phase 1: voice notes are out of scope. Reply once and move on."""
    if not _is_allowed(update.effective_user):
        return
    if update.message is None:
        return
    try:
        await update.message.reply_text(
            "🎙 Voice notes aren't supported yet — see Phase 1 design (Part 5)."
        )
    except Exception:  # noqa: BLE001
        pass


async def handle_unsupported(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Documents, video, audio, stickers — politely decline."""
    if not _is_allowed(update.effective_user):
        return
    if update.message is None:
        return
    try:
        await update.message.reply_text(
            "📎 That media type isn't supported yet. Phase 1 handles URLs and images."
        )
    except Exception:  # noqa: BLE001
        pass
