"""BrainTwin Telegram bot — entry point.

Run from the project root in its own terminal:

    python -m backend.telegram_bot.bot

Setup before running:
    1. Create the bot via @BotFather on Telegram.
    2. Put the token in .env as TELEGRAM_BOT_TOKEN.
    3. /start the bot once from your phone — it'll DM you your user ID.
    4. Paste the user ID into ALLOWED_TELEGRAM_USER_IDS in .env.
    5. Restart this script.

The backend (uvicorn backend.main:app) should be running on the URL set
by BACKEND_CAPTURE_URL (default http://127.0.0.1:8000/capture). The bot
connects outbound to Telegram via long polling, so no inbound port or
public URL is needed.
"""

from __future__ import annotations

import logging
import sys

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from backend.config import reveal, settings
from backend.telegram_bot import handlers
from backend.telegram_bot.client import CaptureClient


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("braintwin.telegram")


async def _on_startup(app: Application) -> None:
    """Wire shared resources into context.bot_data so handlers can reach them."""
    app.bot_data["capture_client"] = CaptureClient()
    allowed = settings.allowed_telegram_ids()
    me = await app.bot.get_me()
    logger.info("Bot online: @%s (id=%s)", me.username, me.id)
    logger.info("Allowlist: %s", sorted(allowed) if allowed else "EMPTY (no one will be processed)")
    logger.info("POSTing captures to: %s", settings.backend_capture_url)


async def _on_shutdown(app: Application) -> None:
    client: CaptureClient | None = app.bot_data.get("capture_client")
    if client is not None:
        await client.aclose()


async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled error in handler", exc_info=context.error)


PLACEHOLDER_TOKEN = "123456789:ABCdefGHIjklMNOpqrSTUvwxYZ"


def build_application() -> Application:
    token = reveal(settings.telegram_bot_token).strip()
    if not token:
        print(
            "ERROR: TELEGRAM_BOT_TOKEN is empty. Set it in .env (see .env.example).",
            file=sys.stderr,
        )
        sys.exit(2)
    if token == PLACEHOLDER_TOKEN:
        print(
            "ERROR: TELEGRAM_BOT_TOKEN is still the placeholder from .env.example.\n"
            "       Create a real bot via @BotFather on Telegram, copy the token it gives you,\n"
            "       and replace the placeholder in .env. See docs/phase1-smoke-test.md (Part B, Pass 0).",
            file=sys.stderr,
        )
        sys.exit(2)

    app = (
        Application.builder()
        .token(token)
        .post_init(_on_startup)
        .post_shutdown(_on_shutdown)
        .build()
    )

    # Commands
    app.add_handler(CommandHandler("start", handlers.cmd_start))
    app.add_handler(CommandHandler("help", handlers.cmd_help))
    app.add_handler(CommandHandler("whoami", handlers.cmd_whoami))
    app.add_handler(CommandHandler("pause", handlers.cmd_pause))
    app.add_handler(CommandHandler("resume", handlers.cmd_resume))
    app.add_handler(CommandHandler("stats", handlers.cmd_stats))
    app.add_handler(CommandHandler("last", handlers.cmd_last))
    app.add_handler(CommandHandler("failures", handlers.cmd_failures))

    # Content (order matters — narrower filters first)
    app.add_handler(MessageHandler(filters.PHOTO, handlers.handle_photo))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handlers.handle_voice))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.ALL | filters.Sticker.ALL, handlers.handle_unsupported))
    # Plain text last — only catches messages that didn't match anything above.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.handle_text))

    app.add_error_handler(_error_handler)
    return app


def main() -> None:
    app = build_application()
    logger.info("Starting Telegram bot (polling). Ctrl-C to stop.")
    # drop_pending_updates=False — we WANT the backlog so a laptop-off
    # window doesn't lose messages (Telegram queues for ~24h).
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
