#!/usr/bin/env python3
import os
import json
import re
import logging
from pathlib import Path
from datetime import datetime

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes
from mcrcon import MCRcon

# ----------------- Load .env -----------------
load_dotenv()

import logging
from pathlib import Path
import os

# ---------------- Logging ----------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
log_file = Path(__file__).parent / "whitelist_bot.log"

# Create a logger for the bot
logger = logging.getLogger("whitelistbot")
logger.setLevel(getattr(logging, LOG_LEVEL))

# Remove old handlers
logger.handlers = []

# Formatter
formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")

# Console handler
console_handler = logging.StreamHandler()
console_handler.setLevel(getattr(logging, LOG_LEVEL))
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

# File handler (overwrite each start)
file_handler = logging.FileHandler(log_file, mode="w", encoding="utf-8")
file_handler.setLevel(getattr(logging, LOG_LEVEL))
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

# ---------------- Fix for python-telegram-bot async logging ----------------
# Make sure library logs also go to our file
tg_logger = logging.getLogger("telegram")
tg_logger.setLevel(getattr(logging, LOG_LEVEL))
tg_logger.handlers = [console_handler, file_handler]

tg_logger_async = logging.getLogger("telegram.ext")
tg_logger_async.setLevel(getattr(logging, LOG_LEVEL))
tg_logger_async.handlers = [console_handler, file_handler]

# Propagate root logs to our handlers
logging.getLogger().handlers = [console_handler, file_handler]
logging.getLogger().setLevel(getattr(logging, LOG_LEVEL))

# ----------------- Environment Variables -----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
ALLOWED_GROUP_ID = int(os.getenv("ALLOWED_GROUP_ID", 0))
MC_PATH = Path(os.getenv("MC_PATH", "/opt/minecraft_spigot/server"))
RCON_HOST = os.getenv("RCON_HOST", "127.0.0.1")

USERCACHE_FILE = MC_PATH / "usercache.json"
WHITELIST_FILE = MC_PATH / "whitelist.json"
PROPS_FILE = MC_PATH / "server.properties"

# Debug output of loaded environment
logger.debug("=== Loaded Environment Variables ===")
logger.debug(f"BOT_TOKEN = {BOT_TOKEN}")
logger.debug(f"ALLOWED_GROUP_ID = {ALLOWED_GROUP_ID}")
logger.debug(f"MC_PATH = {MC_PATH}")
logger.debug(f"USERCACHE_FILE = {USERCACHE_FILE}")
logger.debug(f"WHITELIST_FILE = {WHITELIST_FILE}")
logger.debug(f"PROPS_FILE = {PROPS_FILE}")
logger.debug(f"RCON_HOST = {RCON_HOST}")
logger.debug("===================================")

# ----------------- Helper Functions -----------------
USERNAME_REGEX = re.compile(r"^[A-Za-z0-9_]{3,16}$")

def load_json_file(path: Path):
    with path.open("r") as f:
        return json.load(f)

def load_server_properties(path: Path) -> dict:
    props = {}
    if not path.exists():
        return props
    with path.open("r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, value = line.split("=", 1)
                props[key.strip()] = value.strip()
    return props

def get_whitelist_names():
    if not WHITELIST_FILE.exists():
        return set()
    data = load_json_file(WHITELIST_FILE)
    return {entry["name"] for entry in data}

def get_last_3_non_whitelisted():
    if not USERCACHE_FILE.exists():
        return []
    usercache = load_json_file(USERCACHE_FILE)
    whitelist = get_whitelist_names()

    # Sort newest first by expiresOn timestamp
    usercache.sort(
        key=lambda x: datetime.strptime(x["expiresOn"], "%Y-%m-%d %H:%M:%S %z"),
        reverse=True,
    )

    candidates = []
    for entry in usercache:
        name = entry["name"]
        if name not in whitelist and name not in candidates:
            candidates.append(name)
        if len(candidates) == 3:
            break
    return candidates

# ----------------- Telegram Handlers -----------------
async def whitelist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ALLOWED_GROUP_ID:
        logger.debug(f"Ignored command from chat {update.effective_chat.id}")
        return

    try:
        candidates = get_last_3_non_whitelisted()
    except Exception as e:
        logger.error(f"Error reading files: {e}")
        await update.message.reply_text(f"File error: {e}")
        return

    if not candidates:
        await update.message.reply_text("No non-whitelisted players found.")
        return

    keyboard = [[InlineKeyboardButton(name, callback_data=f"wl:{name}")] for name in candidates]
    keyboard.append([InlineKeyboardButton("Close", callback_data="wl:close")])

    await update.message.reply_text(
        "Select player to whitelist:", reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if update.effective_chat.id != ALLOWED_GROUP_ID:
        logger.debug(f"Ignored callback from chat {update.effective_chat.id}")
        return

    data = query.data
    if data == "wl:close":
        await query.edit_message_text("Closed.")
        return
    if not data.startswith("wl:"):
        return

    username = data.split("wl:")[1]

    # Validate username
    if not USERNAME_REGEX.match(username):
        await query.edit_message_text("Invalid username format.")
        return

    # Re-check files
    try:
        whitelist = get_whitelist_names()
        usercache = load_json_file(USERCACHE_FILE)
        known_users = {entry["name"] for entry in usercache}
    except Exception as e:
        logger.error(f"File error: {e}")
        await query.edit_message_text(f"File error: {e}")
        return

    if username not in known_users:
        await query.edit_message_text("User not found in usercache.")
        return
    if username in whitelist:
        await query.edit_message_text(f"{username} is already whitelisted.")
        return

    # Load RCON password & port from server.properties
    props = load_server_properties(PROPS_FILE)
    RCON_PASSWORD = props.get("rcon.password")
    RCON_PORT = int(props.get("rcon.port", 25575))

    # Execute RCON
    try:
        with MCRcon(RCON_HOST, RCON_PASSWORD, port=RCON_PORT) as mcr:
            response = mcr.command(f"whitelist add {username}")
    except Exception as e:
        logger.error(f"RCON error: {e}")
        await query.edit_message_text(f"RCON error: {e}")
        return

    approver = (
        f"@{update.effective_user.username}"
        if update.effective_user.username
        else update.effective_user.full_name
    )

    summary = (
        f"Whitelist updated\n\n"
        f"Player: {username}\n"
        f"Approved by: {approver}\n"
        f"Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC\n\n"
        f"Server response:\n{response}"
    )

    logger.info(f"{approver} whitelisted {username} (RCON response: {response})")
    await query.edit_message_text(summary)

# ----------------- Main -----------------
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("whitelist", whitelist_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    logger.info("Whitelist bot started...")
    app.run_polling()

if __name__ == "__main__":
    main()
