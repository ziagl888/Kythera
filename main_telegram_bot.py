import logging
import os

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

# Core imports
from core.bot_utils import check_permission, log_command_atomic

# Handler imports
from handlers.open_handler import open_command_callback

logging.basicConfig(level=logging.INFO, format='%(asctime)s - TELEGRAM_BOT - %(message)s')
logger = logging.getLogger(__name__)

# FIX: Token aus Environment laden statt hardcoded.
# The .env file (NOT in the repo!) must set TELEGRAM_BOT_TOKEN.
# Siehe .env.example für das Format.
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError(
        "TELEGRAM_BOT_TOKEN is not set in the environment. "
        "Please create a .env file with this value (see .env.example)."
    )


async def global_command_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Diese Funktion fängt ALLE Textafterrichten ab, egal ob in einer Gruppe oder einem Channel!
    """
    msg = update.message or update.channel_post
    if not msg or not msg.text:
        return

    text = msg.text.strip()

    # Ignore everything that does not start with !
    if not text.startswith("!"):
        return

    command_name = text.split()[0].lower()

    # In Channels gibt es oft keinen "User", der schreibt, sondern der Channel selbst. Fallback:
    user = update.effective_user
    username = user.username if user else (user.full_name if user else "Channel_Admin")

    # 1. Rechtesystem prüfen
    if not check_permission(username, command_name):
        await msg.reply_text("⛔ You do not have permission to use this command.")
        logger.warning(f"Unauthorized access denied for {username} ({command_name})")
        return

    # 2. Logging
    log_command_atomic(username, command_name, text)
    logger.info(f"Command executed: {username} -> {text}")

    # 3. An den richtigen Handler weiterleiten
    try:
        if command_name == "!open":
            await open_command_callback(update, context)
        elif command_name == "!ping":
            await msg.reply_text("🏓 Pong! Das neue System läuft reibungslos.")
        else:
            await msg.reply_text(f"❓ Command {command_name} is not yet implemented.")
    except Exception as e:
        logger.error(f"Error for Ausführung von {command_name}: {e}", exc_info=True)
        await msg.reply_text("❌ Es gab einen internen Error for der Ausführung.")


def main():
    logger.info("Starting Telegram Command Listener...")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, global_command_router))

    logger.info("Bot lauscht auf Befehle...")
    app.run_polling()


if __name__ == "__main__":
    main()
