import os
import json
import re
from pathlib import Path
from datetime import datetime
import logging

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from mcrcon import MCRcon

# ================= ENV CONFIG =================
load_dotenv()


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


BOT_TOKEN = require_env("BOT_TOKEN")
ALLOWED_GROUP_ID = int(require_env("ALLOWED_GROUP_ID"))

MC_PATH = Path(require_env("MC_PATH"))
USERCACHE_FILE = MC_PATH / "usercache.json"
WHITELIST_FILE = MC_PATH / "whitelist.json"
PROPS_FILE = MC_PATH / "server.properties"

# ================= Logging Setup =================
LOG_FILE = Path("whitelist_bot.log")  # current working directory
log_level_str = os.getenv("LOG_LEVEL", "INFO").upper()
log_level = getattr(logging, log_level_str, logging.INFO)

logging.basicConfig(
    level=log_level,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)
# ==================================================

USERNAME_REGEX = re.compile(r"^[A-Za-z0-9_]{3,16}$")


# ================= Helper Functions =================
def load_json_file(path: Path):
    try:
        with path.open("r") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load {path}: {e}")
        raise


def load_server_properties(path: Path) -> dict:
    props = {}
    try:
        with path.open("r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, value = line.split("=", 1)
                    props[key.strip()] = value.strip()
        return props
    except Exception as e:
        logger.error(f"Failed to load server.properties: {e}")
        raise


def get_whitelist_names():
    data = load_json_file(WHITELIST_FILE)
    return {entry["name"] for entry in data}


def get_last_3_non_whitelisted():
    usercache = load_json_file(USERCACHE_FILE)
    whitelist = get_whitelist_names()

    # Sort newest first using expiresOn timestamp
    usercache.sort(
        key=lambda x: datetime.strptime(
            x["expiresOn"], "%Y-%m-%d %H:%M:%S %z"
        ),
        reverse=True,
    )

    candidates = []
    for entry in usercache:
        name = entry["name"]
        if name not in whitelist:
            candidates.append(name)
        if len(candidates) == 3:
            break

    logger.debug(f"Last 3 non-whitelisted candidates: {candidates}")
    return candidates
# ====================================================

# ================= Load server.properties for RCON =================
props = load_server_properties(PROPS_FILE)
RCON_PASSWORD = props.get("rcon.password")
RCON_PORT = int(props.get("rcon.port"))
RCON_HOST = "127.0.0.1"  # always local, dont change ideally
logger.info(f"Loaded RCON config: host={RCON_HOST}, port={RCON_PORT}")
# ====================================================================

# ================= Telegram Handlers =================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    logger.info(f"/start received in chat ID {chat.id} (type: {chat.type})")
    # Only logging, no message sent


async def whitelist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ALLOWED_GROUP_ID:
        logger.debug(f"Ignored /whitelist in chat {update.effective_chat.id}")
        return

    try:
        candidates = get_last_3_non_whitelisted()
    except Exception as e:
        logger.error(f"Error fetching candidates: {e}")
        await update.message.reply_text(f"File error: {e}")
        return

    if not candidates:
        logger.info("No non-whitelisted players found in usercache")
        await update.message.reply_text(
            "No known non-whitelisted players found in usercache."
        )
        return

    keyboard = [
        [InlineKeyboardButton(name, callback_data=f"wl:{name}")]
        for name in candidates
    ]
    keyboard.append([InlineKeyboardButton("Close", callback_data="wl:close")])

    logger.info(f"Showing whitelist options to user {update.effective_user.id}")
    await update.message.reply_text(
        "Select player to whitelist:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if update.effective_chat.id != ALLOWED_GROUP_ID:
        logger.debug(f"Ignored callback in chat {update.effective_chat.id}")
        return

    data = query.data
    if data == "wl:close":
        logger.info(f"User {update.effective_user.id} closed the inline keyboard")
        await query.edit_message_text("Closed.")
        return

    if not data.startswith("wl:"):
        logger.warning(f"Unknown callback data: {data}")
        return

    username = data.split("wl:")[1]

    # Validate username
    if not USERNAME_REGEX.match(username):
        logger.warning(f"Invalid username format selected: {username}")
        await query.edit_message_text("Invalid username format.")
        return

    # Re-validate against current files
    try:
        whitelist = get_whitelist_names()
        usercache = load_json_file(USERCACHE_FILE)
        known_users = {entry["name"] for entry in usercache}
    except Exception as e:
        logger.error(f"File error during validation: {e}")
        await query.edit_message_text(f"File error: {e}")
        return

    if username not in known_users:
        logger.warning(f"Selected username not in usercache: {username}")
        await query.edit_message_text("User not found in usercache.")
        return

    if username in whitelist:
        logger.info(f"{username} already whitelisted")
        await query.edit_message_text(f"{username} is already whitelisted.")
        return

    # Execute RCON command
    try:
        with MCRcon(RCON_HOST, RCON_PASSWORD, port=RCON_PORT) as mcr:
            response = mcr.command(f"whitelist add {username}")
        logger.info(f"Whitelisted {username} via RCON by user {update.effective_user.id}")
    except Exception as e:
        logger.error(f"RCON command failed for {username}: {e}")
        await query.edit_message_text(f"RCON error: {e}")
        return

    approver = (
        f"@{update.effective_user.username}"
        if update.effective_user.username
        else update.effective_user.full_name
    )

    summary = (
        f"✅ Whitelist updated\n\n"
        f"Player: {username}\n"
        f"Approved by: {approver}\n"
        f"Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC\n\n"
        f"Server response:\n{response}"
    )

    await query.edit_message_text(summary)
# ===================================================

def main():
    logger.info("Starting whitelist bot...")
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start", start_command))  # optional logging only
    app.add_handler(CommandHandler("whitelist", whitelist_command))
    app.add_handler(CallbackQueryHandler(button_handler))

    app.run_polling()


if __name__ == "__main__":
    main()
