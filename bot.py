import os
import json
import logging
from pathlib import Path

from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

from mcrcon import MCRcon

# --------------------------------------------------
# Load environment
# --------------------------------------------------

# Load .env from script directory (works with systemd)
env_path = Path(__file__).parent / ".env"
load_dotenv(env_path)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ALLOWED_GROUP_IDS = [int(gid.strip()) for gid in os.getenv("ALLOWED_GROUP_ID", "0").split(",")]
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

MC_PATH = Path(os.getenv("MC_PATH", "/opt/minecraft_spigot/server"))
USERCACHE_FILE = MC_PATH / "usercache.json"
WHITELIST_FILE = MC_PATH / "whitelist.json"
BANNED_FILE = MC_PATH / "banned-players.json"
SERVER_PROPERTIES_FILE = MC_PATH / "server.properties"

# --------------------------------------------------
# Read server.properties for RCON settings
# --------------------------------------------------

def read_server_properties():
    """Read server.properties file and return dict of properties."""
    properties = {}
    if not SERVER_PROPERTIES_FILE.exists():
        return properties
    
    try:
        with open(SERVER_PROPERTIES_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                # Skip comments and empty lines
                if line.startswith("#") or not line or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                properties[key.strip()] = value.strip()
        return properties
    except Exception as e:
        return properties

# Initial load of server properties (will be reloaded in main with logging)
server_props = read_server_properties()

RCON_HOST = os.getenv("RCON_HOST", "127.0.0.1")
RCON_PORT = server_props.get("rcon.port") or os.getenv("RCON_PORT")
RCON_PASSWORD = server_props.get("rcon.password") or os.getenv("RCON_PASSWORD")
RCON_ENABLED = server_props.get("enable-rcon", "").lower() == "true"

# Convert RCON_PORT to int if it exists
if RCON_PORT:
    try:
        RCON_PORT = int(RCON_PORT)
    except ValueError:
        RCON_PORT = None

BAN_REASON = os.getenv("BAN_REASON", "Banned via Telegram")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# --------------------------------------------------
# Logging
# --------------------------------------------------

log_file = Path(__file__).parent / "whitelist_bot.log"

logger = logging.getLogger("whitelistbot")
logger.setLevel(getattr(logging, LOG_LEVEL))
logger.handlers = []

formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s",
    "%Y-%m-%d %H:%M:%S",
)

console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

file_handler = logging.FileHandler(log_file, mode="w", encoding="utf-8")
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

# --------------------------------------------------
# JSON Helpers (READ ONLY)
# --------------------------------------------------

def load_json(path: Path):
    """Load JSON file, return empty list if file doesn't exist."""
    if not path.exists():
        logger.warning(f"File not found: {path}")
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON in {path}: {e}")
        return []
    except Exception as e:
        logger.error(f"Error reading {path}: {e}")
        return []

# --------------------------------------------------
# RCON
# --------------------------------------------------

def test_rcon_connection():
    """Test RCON connection and return True if successful."""
    try:
        with MCRcon(RCON_HOST, RCON_PASSWORD, port=RCON_PORT) as mcr:
            response = mcr.command("list")
            logger.info(f"RCON connection test successful")
            logger.debug(f"RCON test response: {response}")
            return True
    except Exception as e:
        logger.error(f"RCON connection test failed: {e}")
        return False


def rcon_command(command: str):
    """Execute RCON command, return response or None on error."""
    try:
        with MCRcon(RCON_HOST, RCON_PASSWORD, port=RCON_PORT) as mcr:
            response = mcr.command(command)
            logger.info(f"RCON executed: {command}")
            logger.debug(f"RCON response: {response}")
            return response
    except Exception as e:
        logger.error(f"RCON error for command '{command}': {e}")
        return None

# --------------------------------------------------
# Inline Menu Builder
# --------------------------------------------------

def build_user_menu(users, callback_prefix):
    """Build inline keyboard menu from user list."""
    keyboard = []
    for user in users:
        keyboard.append([
            InlineKeyboardButton(
                user["name"],
                callback_data=f"{callback_prefix}:{user['uuid']}"
            )
        ])
    return InlineKeyboardMarkup(keyboard)

# --------------------------------------------------
# Commands
# --------------------------------------------------

async def whitelist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show menu to whitelist a user."""
    if update.effective_chat.id not in ALLOWED_GROUP_IDS:
        logger.warning(f"Whitelist command from unauthorized group: {update.effective_chat.id}")
        return

    usercache = load_json(USERCACHE_FILE)
    whitelist = load_json(WHITELIST_FILE)
    banned = load_json(BANNED_FILE)

    whitelisted_uuids = {u["uuid"] for u in whitelist}
    banned_uuids = {u["uuid"] for u in banned}

    candidates = [
        u for u in usercache
        if u["uuid"] not in whitelisted_uuids
        and u["uuid"] not in banned_uuids
    ]

    if not candidates:
        await update.message.reply_text("No users available to whitelist.\n(All cached users are already whitelisted or banned)")
        return

    logger.info(f"Whitelist menu shown to {update.effective_user.username} with {len(candidates)} candidates")
    reply_markup = build_user_menu(candidates, "wl_add")
    
    # Add cancel button
    keyboard = list(reply_markup.inline_keyboard)
    keyboard.append([InlineKeyboardButton("Cancel", callback_data="cancel")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text("Select user to whitelist:", reply_markup=reply_markup)


async def revoke_whitelist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show menu to remove a user from whitelist."""
    if update.effective_chat.id not in ALLOWED_GROUP_IDS:
        logger.warning(f"Revoke whitelist command from unauthorized group: {update.effective_chat.id}")
        return

    whitelist = load_json(WHITELIST_FILE)

    if not whitelist:
        await update.message.reply_text("Whitelist is empty.")
        return

    logger.info(f"Revoke whitelist menu shown to {update.effective_user.username}")
    reply_markup = build_user_menu(whitelist, "wl_remove")
    
    # Add cancel button
    keyboard = list(reply_markup.inline_keyboard)
    keyboard.append([InlineKeyboardButton("Cancel", callback_data="cancel")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text("Select user to remove from whitelist:", reply_markup=reply_markup)


async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show menu to ban a user (admin only)."""
    if update.effective_chat.id not in ALLOWED_GROUP_IDS:
        logger.warning(f"Ban command from unauthorized group: {update.effective_chat.id}")
        return

    issuer_id = update.effective_user.id

    if issuer_id != ADMIN_ID:
        logger.warning(f"Unauthorized ban attempt by {issuer_id} ({update.effective_user.username})")
        await update.message.reply_text("Only admins can ban users.")
        return

    usercache = load_json(USERCACHE_FILE)
    banned = load_json(BANNED_FILE)

    banned_uuids = {u["uuid"] for u in banned}

    candidates = [
        u for u in usercache
        if u["uuid"] not in banned_uuids
    ]

    if not candidates:
        await update.message.reply_text("No users available to ban.\n(All cached users are already banned)")
        return

    logger.info(f"Ban menu shown to admin {update.effective_user.username}")
    reply_markup = build_user_menu(candidates, "ban_add")
    
    # Add cancel button
    keyboard = list(reply_markup.inline_keyboard)
    keyboard.append([InlineKeyboardButton("Cancel", callback_data="cancel")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text("Select user to ban:", reply_markup=reply_markup)


async def revoke_ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show menu to unban a user (admin only)."""
    if update.effective_chat.id not in ALLOWED_GROUP_IDS:
        logger.warning(f"Revoke ban command from unauthorized group: {update.effective_chat.id}")
        return

    issuer_id = update.effective_user.id

    if issuer_id != ADMIN_ID:
        logger.warning(f"Unauthorized unban attempt by {issuer_id} ({update.effective_user.username})")
        await update.message.reply_text("Only admins can unban users.")
        return

    banned = load_json(BANNED_FILE)

    if not banned:
        await update.message.reply_text("No banned players.")
        return

    logger.info(f"Unban menu shown to admin {update.effective_user.username}")
    reply_markup = build_user_menu(banned, "ban_remove")
    
    # Add cancel button
    keyboard = list(reply_markup.inline_keyboard)
    keyboard.append([InlineKeyboardButton("Cancel", callback_data="cancel")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text("Select user to unban:", reply_markup=reply_markup)

# --------------------------------------------------
# Callback Handler
# --------------------------------------------------

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline button clicks."""
    query = update.callback_query
    
    # Verify group authorization
    if query.message.chat.id not in ALLOWED_GROUP_IDS:
        await query.answer("Unauthorized group", show_alert=True)
        logger.warning(f"Callback from unauthorized group: {query.message.chat.id}")
        return
    
    # Parse callback data
    if query.data == "cancel":
        await query.edit_message_text("Cancelled.")
        return
    
    try:
        action, uuid = query.data.split(":", 1)
    except ValueError:
        await query.answer("Invalid callback data", show_alert=True)
        logger.error(f"Invalid callback data: {query.data}")
        return
    
    # Verify admin permission for ban actions
    if action.startswith("ban_") and update.effective_user.id != ADMIN_ID:
        await query.answer("Admin only", show_alert=True)
        logger.warning(f"Non-admin {update.effective_user.id} attempted ban action")
        return
    
    await query.answer()

    # Find username from UUID
    username = None
    usercache = load_json(USERCACHE_FILE)
    for u in usercache:
        if u["uuid"] == uuid:
            username = u["name"]
            break

    if not username:
        await query.edit_message_text("User not found in cache.")
        logger.error(f"UUID {uuid} not found in usercache")
        return

    # Re-validate state before executing to avoid race conditions
    success = False
    error_msg = None
    
    if action == "wl_add":
        # Check if already whitelisted
        current_whitelist = load_json(WHITELIST_FILE)
        if any(u["uuid"] == uuid for u in current_whitelist):
            await query.edit_message_text(f"{username} is already whitelisted.")
            return
        
        result = rcon_command(f"whitelist add {username}")
        if result is not None:
            # Reload whitelist on server
            rcon_command("whitelist reload")
            success = True
        else:
            error_msg = f"Failed to whitelist {username}. Check RCON connection."

    elif action == "wl_remove":
        # Check if still whitelisted
        current_whitelist = load_json(WHITELIST_FILE)
        if not any(u["uuid"] == uuid for u in current_whitelist):
            await query.edit_message_text(f"{username} is not whitelisted.")
            return
        
        result = rcon_command(f"whitelist remove {username}")
        if result is not None:
            # Reload whitelist on server
            rcon_command("whitelist reload")
            success = True
        else:
            error_msg = f"Failed to remove {username} from whitelist. Check RCON connection."

    elif action == "ban_add":
        # Check if already banned
        current_banned = load_json(BANNED_FILE)
        if any(u["uuid"] == uuid for u in current_banned):
            await query.edit_message_text(f"{username} is already banned.")
            return
        
        result = rcon_command(f"ban {username}")
        if result is not None:
            success = True
        else:
            error_msg = f"Failed to ban {username}. Check RCON connection."

    elif action == "ban_remove":
        # Check if still banned
        current_banned = load_json(BANNED_FILE)
        if not any(u["uuid"] == uuid for u in current_banned):
            await query.edit_message_text(f"{username} is not banned.")
            return
        
        result = rcon_command(f"pardon {username}")
        if result is not None:
            success = True
        else:
            error_msg = f"Failed to unban {username}. Check RCON connection."

    else:
        await query.edit_message_text(f"Unknown action: {action}")
        logger.error(f"Unknown callback action: {action}")
        return

    # Send result to user
    if success:
        action_names = {
            "wl_add": "whitelisted",
            "wl_remove": "removed from whitelist",
            "ban_add": "banned",
            "ban_remove": "unbanned"
        }
        await query.edit_message_text(f"{username} has been {action_names[action]}.")
        logger.info(f"{action} executed for {username} by {update.effective_user.username}")
    else:
        await query.edit_message_text(error_msg)

# --------------------------------------------------
# Main
# --------------------------------------------------

def main():
    """Start the bot."""
    logger.info("=== Whitelist Bot Starting ===")
    
    # Log server.properties loading status
    logger.info("=== Server Properties ===")
    if SERVER_PROPERTIES_FILE.exists():
        props_count = len(server_props)
        logger.info(f"server.properties loaded: {props_count} properties found")
        logger.debug(f"RCON settings from server.properties: enable-rcon={server_props.get('enable-rcon')}, rcon.port={server_props.get('rcon.port')}, rcon.password={'SET' if server_props.get('rcon.password') else 'NOT SET'}")
    else:
        logger.warning(f"server.properties not found at {SERVER_PROPERTIES_FILE}")
    logger.info("=========================")
    
    # Log all environment variables for debugging
    logger.info("=== Environment Variables ===")
    logger.info(f"BOT_TOKEN: {'SET' if BOT_TOKEN else 'NOT SET'}")
    logger.info(f"ALLOWED_GROUP_IDS: {ALLOWED_GROUP_IDS}")
    logger.info(f"ADMIN_ID: {ADMIN_ID}")
    logger.info(f"MC_PATH: {MC_PATH}")
    logger.info(f"USERCACHE_FILE: {USERCACHE_FILE}")
    logger.info(f"WHITELIST_FILE: {WHITELIST_FILE}")
    logger.info(f"BANNED_FILE: {BANNED_FILE}")
    logger.info(f"SERVER_PROPERTIES_FILE: {SERVER_PROPERTIES_FILE}")
    logger.info(f"RCON_HOST: {RCON_HOST}")
    logger.info(f"RCON_PORT: {RCON_PORT}")
    logger.info(f"RCON_PASSWORD: {'SET' if RCON_PASSWORD else 'NOT SET'}")
    logger.info(f"RCON_ENABLED (from server.properties): {RCON_ENABLED}")
    logger.info(f"LOG_LEVEL: {LOG_LEVEL}")
    logger.info("================================")
    
    # Validate environment variables
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN must be set in environment")
        raise ValueError("BOT_TOKEN must be set in environment")
    if not ALLOWED_GROUP_IDS or ALLOWED_GROUP_IDS == [0]:
        logger.error("ALLOWED_GROUP_ID must be set to valid Telegram group ID(s)")
        raise ValueError("ALLOWED_GROUP_ID must be set to valid Telegram group ID(s)")
    if ADMIN_ID == 0:
        logger.warning("ADMIN_ID not set - ban commands will be unavailable")
    
    # Check if files exist
    logger.info("=== File System Check ===")
    logger.info(f"MC_PATH exists: {MC_PATH.exists()}")
    logger.info(f"USERCACHE_FILE exists: {USERCACHE_FILE.exists()}")
    logger.info(f"WHITELIST_FILE exists: {WHITELIST_FILE.exists()}")
    logger.info(f"BANNED_FILE exists: {BANNED_FILE.exists()}")
    logger.info("=========================")
    
    # Test RCON connection
    logger.info("=== Testing RCON Connection ===")
    
    # Validate RCON settings
    if not RCON_PORT:
        logger.error("RCON_PORT not found in server.properties or .env")
        raise ValueError("RCON_PORT must be set in server.properties (rcon.port) or .env")
    
    if not RCON_PASSWORD:
        logger.error("RCON_PASSWORD not found in server.properties or .env")
        raise ValueError("RCON_PASSWORD must be set in server.properties (rcon.password) or .env")
    
    if not RCON_ENABLED:
        logger.error("enable-rcon=false in server.properties - RCON is DISABLED")
        logger.error("Set enable-rcon=true in server.properties and restart Minecraft server")
        raise ValueError("RCON must be enabled in server.properties (enable-rcon=true)")
    
    # Test connection
    if test_rcon_connection():
        logger.info("RCON connection test PASSED")
    else:
        logger.error("RCON connection test FAILED")
        logger.error("Check server.properties: enable-rcon=true, rcon.port, rcon.password")
        raise ConnectionError("Failed to connect to RCON - check server.properties settings")
    
    logger.info("================================")

    logger.info("=== Whitelist Bot Configuration ===")
    logger.info(f"Allowed Group IDs: {ALLOWED_GROUP_IDS}")
    logger.info(f"Admin ID: {ADMIN_ID}")
    logger.info(f"MC Path: {MC_PATH}")
    logger.info(f"RCON Host: {RCON_HOST}:{RCON_PORT}")
    logger.info(f"Log Level: {LOG_LEVEL}")
    logger.info("===================================")

    # Build application
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Add command handlers
    app.add_handler(CommandHandler("whitelist", whitelist_command))
    app.add_handler(CommandHandler("revokewhitelist", revoke_whitelist_command))
    app.add_handler(CommandHandler("ban", ban_command))
    app.add_handler(CommandHandler("revokeban", revoke_ban_command))
    
    # Add callback handler for inline buttons
    app.add_handler(CallbackQueryHandler(button_handler))

    logger.info("Whitelist bot started and polling for updates...")
    
    # Start polling
    app.run_polling()


if __name__ == "__main__":
    main()
