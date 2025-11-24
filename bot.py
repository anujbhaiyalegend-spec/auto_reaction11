from keep_alive import keep_alive
import os
import logging
import datetime
import asyncio
import threading
import io
import random
from typing import Dict, Any

from flask import Flask
from pymongo import MongoClient
import certifi  # Fix for SSL errors

# python-telegram-bot v20+ imports
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
)
from telegram.constants import ChatType, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ChatMemberHandler,
    ContextTypes,
    filters,
)
from telegram.helpers import escape_markdown

# --- CONFIGURATION ---
# 1. Telegram Settings
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8133117251:AAH2pr-gQ2bjr4EYxKhdk_tcPlqQxAaXF9Y")
MAIN_CHANNEL_USERNAME = os.environ.get("MAIN_CHANNEL_USERNAME", "Unix_Bots")
raw_admins = os.environ.get("ADMIN_IDS", "7191595289,7258860451")
ADMIN_IDS = [int(x) for x in raw_admins.split(",") if x.strip()]

# 2. MongoDB Settings
DB_USER = "thehider09_db_user"
DB_PASS = "WHTUO1kQJj834fsV"
DB_CLUSTER = "cluster0.cwfxlzq.mongodb.net"
DB_NAME = "telegram_bot_db"
MONGO_URI = f"mongodb+srv://{DB_USER}:{DB_PASS}@{DB_CLUSTER}/?retryWrites=true&w=majority"

# 3. Reactions
POSITIVE_REACTIONS = ["👍", "❤️", "🔥", "🎉", "👏", "🤩", "💯", "🙏", "💘", "😘", "🤗", "🆒", "😇", "⚡", "🫡"]
FALLBACK_REACTIONS = ["👌", "😁", "❤️‍🔥", "🥰", "💋"]

# --- LOGGING ---
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# --- MONGODB CONNECTION ---
try:
    # UPDATED: Added tlsAllowInvalidCertificates=True to bypass Windows SSL issues
    mongo_client = MongoClient(
        MONGO_URI, 
        tlsCAFile=certifi.where(),
        tlsAllowInvalidCertificates=True 
    )
    
    # Send a ping to confirm a successful connection
    mongo_client.admin.command('ping')
    
    db = mongo_client[DB_NAME]
    users_col = db['users']
    chats_col = db['chats']
    pending_col = db['pending_notifications']
    logger.info("✅ Connected to MongoDB Successfully")
except Exception as e:
    logger.critical(f"❌ Failed to connect to MongoDB: {e}")
    # We exit because the bot cannot function without the DB
    exit(1)

# --- FLASK KEEP-ALIVE SERVER (FOR RENDER) ---
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running with MongoDB storage!", 200

def run_flask_app():
    port = int(os.environ.get("PORT", 5000))
    # Disable flask logging to keep console clean
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)
    app.run(host='0.0.0.0', port=port)

# --- DATABASE HELPER FUNCTIONS ---

def track_user(user, update_last_seen: bool = False) -> None:
    """Upsert user data into MongoDB."""
    if not user:
        return
    
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    
    update_fields = {
        "username": user.username,
        "first_name": user.first_name,
        "is_bot": user.is_bot
    }
    
    if update_last_seen:
        update_fields["last_seen"] = now_iso

    try:
        users_col.update_one(
            {"_id": user.id},
            {
                "$set": update_fields,
                "$setOnInsert": {"last_seen": now_iso, "joined_at": now_iso}
            },
            upsert=True
        )
    except Exception as e:
        logger.error(f"DB Error tracking user: {e}")

def track_chat(chat_id: int, title: str, chat_type: str, adder_user_id: int) -> None:
    """Upsert chat data into MongoDB."""
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    try:
        chats_col.update_one(
            {"_id": chat_id},
            {
                "$set": {
                    "title": title,
                    "type": chat_type,
                    "adder_id": adder_user_id,
                    "last_active": now_iso
                },
                "$setOnInsert": {"added_at": now_iso}
            },
            upsert=True
        )
    except Exception as e:
        logger.error(f"DB Error tracking chat: {e}")

def add_pending_notification(user_id: int, message: str):
    try:
        pending_col.update_one(
            {"_id": user_id},
            {"$push": {"messages": message}},
            upsert=True
        )
    except Exception as e:
        logger.error(f"DB Error adding notification: {e}")

def get_and_clear_pending_notifications(user_id: int):
    try:
        doc = pending_col.find_one_and_delete({"_id": user_id})
        return doc.get("messages", []) if doc else []
    except Exception:
        return []

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

# --- BOT COMMANDS & LOGIC ---

DEFAULT_COMMANDS = [BotCommand("start", "👋 Start the bot & check subscription")]
ADMIN_COMMANDS = [BotCommand("start", "👋 Start the bot & check subscription"),
                  BotCommand("admin", "👑 Access admin panel (Admin only)"),
                  BotCommand("cancel_broadcast", "🛑 Cancel ongoing broadcast"),
                  BotCommand("export_data", "📁 Export stored JSON")]

async def post_init_commands(application: Application) -> None:
    await application.bot.set_my_commands(DEFAULT_COMMANDS, scope=BotCommandScopeAllPrivateChats())
    for admin_id in ADMIN_IDS:
        try:
            await application.bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception:
            pass
    logger.info("Bot commands configured.")

async def is_user_member_of_channel(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id=f"@{MAIN_CHANNEL_USERNAME}", user_id=user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception as e:
        logger.warning("Membership check failed for %s: %s", user_id, e)
        return False

# --- HANDLERS ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or update.message.chat.type != ChatType.PRIVATE:
        return

    user = update.effective_user
    track_user(user, update_last_seen=True)

    msgs = get_and_clear_pending_notifications(user.id)
    for txt in msgs:
        try:
            await context.bot.send_message(chat_id=user.id, text=txt, disable_web_page_preview=True)
        except Exception:
            pass

    bot_username = (await context.bot.get_me()).username
    is_member = await is_user_member_of_channel(context, user.id)

    if is_member:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("➕ Add to Group ➕", url=f"https://t.me/{bot_username}?startgroup=true"),
            InlineKeyboardButton("📢 Add to Channel 📢", url=f"https://t.me/{bot_username}?startchannel=true"),
        ]])
        text = (
            "🌟 *Welcome!*\n\n"
            "You are a member of our main channel and can now use the bot.\n\n"
            "Add me to a group or channel using the buttons below:"
        )
    else:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"1. Join @{MAIN_CHANNEL_USERNAME}", url=f"https://t.me/{MAIN_CHANNEL_USERNAME}")],
            [InlineKeyboardButton("2. I Have Joined ✅", callback_data="check_join")],
        ])
        text = (
            "🔒 *Access Required*\n\n"
            "To use this bot, you must first join our main channel.\n\n"
            "Please join the channel and then click 'I Have Joined ✅'."
        )

    try:
        await update.message.reply_text(escape_markdown(text, version=2), parse_mode=ParseMode.MARKDOWN_V2, reply_markup=keyboard)
    except Exception:
        await update.message.reply_text(text.replace("*", ""), reply_markup=keyboard)

async def check_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user = query.from_user
    track_user(user, update_last_seen=True)

    if await is_user_member_of_channel(context, user.id):
        bot_username = (await context.bot.get_me()).username
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("➕ Add to Group ➕", url=f"https://t.me/{bot_username}?startgroup=true"),
            InlineKeyboardButton("📢 Add to Channel 📢", url=f"https://t.me/{bot_username}?startchannel=true"),
        ]])
        text = "✅ *Thank you for joining!*\nYou can now add me to a group or channel:"
        try:
            await query.edit_message_text(escape_markdown(text, version=2), parse_mode=ParseMode.MARKDOWN_V2, reply_markup=keyboard)
        except:
            await query.edit_message_text(text.replace("*", ""), reply_markup=keyboard)
    else:
        await query.answer("❌ You haven't joined the channel yet. Please join and try again.", show_alert=True)

async def handle_chat_addition(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.my_chat_member:
        return
    chat_member = update.my_chat_member
    chat = chat_member.chat
    adder = chat_member.from_user
    new_status = chat_member.new_chat_member.status
    old_status = chat_member.old_chat_member.status
    
    was_added = new_status in ("member", "administrator") and old_status not in ("member", "administrator")
    
    if was_added and adder:
        chat_title = chat.title or (f"Channel ID: {chat.id}" if chat.type == ChatType.CHANNEL else "Private Group")
        chat_type_str = "Group" if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP) else "Channel"

        track_chat(chat.id, chat_title, chat_type_str, adder.id)

        private_msg = None
        if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
            private_msg = f"✅ Thanks for adding me to the group '{chat_title}'! I will react to new messages automatically."
        elif chat.type == ChatType.CHANNEL and new_status == "administrator":
            private_msg = f"📢 Thanks for adding me to the channel '{chat_title}'! Please ensure I have 'Add Reactions' permission."

        if private_msg:
            try:
                await context.bot.send_message(chat_id=adder.id, text=private_msg)
            except Exception:
                add_pending_notification(adder.id, private_msg)

async def react_to_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.channel_post or update.message
    if not message:
        return
    if (message.text and message.text.startswith("/")) or message.via_bot or message.new_chat_members or message.left_chat_member:
        return

    if message.from_user:
        track_user(message.from_user, update_last_seen=True)

    all_reactions = POSITIVE_REACTIONS + FALLBACK_REACTIONS
    chosen_emojis = random.sample(all_reactions, min(len(all_reactions), 3))
    
    for emoji in chosen_emojis:
        try:
            await context.bot.set_message_reaction(
                chat_id=message.chat.id, 
                message_id=message.message_id, 
                reaction=[emoji], 
                is_big=False
            )
            return
        except Exception:
            continue

# --- ADMIN PANEL ---

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    source = update.message or (update.callback_query and update.callback_query.message)
    
    if not user or not is_admin(user.id):
        if source: await source.reply_text("❌ Access denied.")
        return

    try:
        total_users = users_col.count_documents({})
        total_chats = chats_col.count_documents({})
    except Exception:
        total_users = "Err"
        total_chats = "Err"

    text = (
        f"👑 Admin Panel\n\n"
        f"👥 Total Users: `{total_users}`\n"
        f"📢 Total Chats/Channels: `{total_chats}`\n\n"
        "Select an action below:"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📨 Broadcast Message", callback_data="admin_broadcast_start")],
        [InlineKeyboardButton("📊 View User List", callback_data="admin_view_users")],
        [InlineKeyboardButton("🏢 View Chat List", callback_data="admin_view_chats")],
        [InlineKeyboardButton("📁 Export Data", callback_data="admin_export_data")],
    ])
    await source.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)

async def admin_panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return await query.edit_message_text("❌ Access expired.")

    data = query.data
    if data == "admin_broadcast_start":
        context.user_data["broadcast_mode"] = True
        await query.edit_message_text("📡 Broadcast mode activated.\nSend the message you want to broadcast now (or /cancel_broadcast).", 
                                      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="admin_back")]]))
    elif data == "admin_view_users":
        await show_list(query.message.chat_id, context, "users")
    elif data == "admin_view_chats":
        await show_list(query.message.chat_id, context, "chats")
    elif data == "admin_export_data":
        await export_data_to_admin(query.message.chat_id, context)
    elif data == "admin_back":
        await admin_command(update, context)

async def show_list(chat_id: int, context: ContextTypes.DEFAULT_TYPE, type_str: str) -> None:
    try:
        if type_str == "users":
            cursor = users_col.find().sort("last_seen", -1).limit(20)
            lines = []
            for u in cursor:
                uname = f"@{u.get('username')}" if u.get('username') else "No User"
                lines.append(f"👤 {u.get('first_name', '?')} ({uname})\nID: `{u['_id']}` | Seen: `{u.get('last_seen', '?')[:16]}`")
            title = "User Details (Top 20 Active)"
        else:
            cursor = chats_col.find().sort("last_active", -1).limit(20)
            lines = []
            for c in cursor:
                lines.append(f"{c.get('title', '?')} ({c.get('type', '?')})\nID: `{c['_id']}`")
            title = "Chats (Top 20 Active)"
    except Exception as e:
        logger.error(f"DB Error fetching list: {e}")
        await context.bot.send_message(chat_id=chat_id, text="❌ Database error.")
        return
    
    text = f"📊 {title}\n\n" + "\n---\n".join(lines)
    await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN,
                                   reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="admin_back")]]))

async def admin_broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_admin(user.id) or not context.user_data.get("broadcast_mode"):
        return
    context.user_data.pop("broadcast_mode", None)
    
    try:
        cursor = users_col.find({}, {"_id": 1})
        user_ids = [doc["_id"] for doc in cursor]
    except Exception:
        await update.message.reply_text("❌ Database error.")
        return
    
    if not user_ids:
        await update.message.reply_text("⚠️ No users found.")
        return

    await update.message.reply_text(f"🚀 Starting broadcast to {len(user_ids)} users...")
    sent = 0
    failed = 0
    for target in user_ids:
        try:
            await context.bot.copy_message(chat_id=target, from_chat_id=update.message.chat_id, message_id=update.message.message_id)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1
    await update.message.reply_text(f"✅ Broadcast finished.\nSent: {sent}\nFailed: {failed}")

async def cancel_broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id): return
    context.user_data.pop("broadcast_mode", None)
    await update.message.reply_text("🛑 Broadcast cancelled.")

async def export_data_to_admin(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        users = list(users_col.find())
        chats = list(chats_col.find())
        data = {"users": users, "chats": chats}
        
        import json
        def default(o):
            if isinstance(o, (datetime.date, datetime.datetime)):
                return o.isoformat()
            return str(o)

        bio = io.BytesIO()
        bio.write(json.dumps(data, default=default, ensure_ascii=False, indent=2).encode("utf-8"))
        bio.seek(0)
        await context.bot.send_document(chat_id=chat_id, document=bio, filename="mongodb_dump.json")
    except Exception:
        await context.bot.send_message(chat_id=chat_id, text="❌ Export failed.")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update: %s", context.error)

def main() -> None:
    if not BOT_TOKEN:
        logger.critical("BOT_TOKEN is not set.")
        return

    logger.info("🚀 Starting Flask Server for Render...")
    t = threading.Thread(target=run_flask_app)
    t.daemon = True
    t.start()

    application = Application.builder().token(BOT_TOKEN).post_init(post_init_commands).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("cancel_broadcast", cancel_broadcast_command))
    application.add_handler(CommandHandler("export_data", lambda u, c: export_data_to_admin(u.effective_user.id, c)))

    application.add_handler(CallbackQueryHandler(admin_panel_callback, pattern="^admin_"))
    application.add_handler(CallbackQueryHandler(check_join_callback, pattern="^check_join$"))
    application.add_handler(ChatMemberHandler(handle_chat_addition, ChatMemberHandler.MY_CHAT_MEMBER))
    
    application.add_handler(MessageHandler(filters.User(ADMIN_IDS) & ~filters.COMMAND, admin_broadcast_message))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND & ~filters.User(ADMIN_IDS), react_to_post))
    
    application.add_error_handler(error_handler)

    logger.info("🚀 Starting Bot Polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    keep_alive()
    main()