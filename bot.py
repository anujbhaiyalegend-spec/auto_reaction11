from keep_alive import keep_alive
import os
import logging
import datetime
import threading
import random
import certifi
from flask import Flask
from pymongo import MongoClient

# Import the new v21+ compatible library
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
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8133117251:AAH2pr-gQ2bjr4EYxKhdk_tcPlqQxAaXF9Y")
MAIN_CHANNEL_USERNAME = os.environ.get("MAIN_CHANNEL_USERNAME", "Unix_Bots")
raw_admins = os.environ.get("ADMIN_IDS", "7191595289,7258860451")
ADMIN_IDS = [int(x) for x in raw_admins.split(",") if x.strip()]

# MongoDB Credentials
DB_USER = "thehider09_db_user"
DB_PASS = "WHTUO1kQJj834fsV"
DB_CLUSTER = "cluster0.cwfxlzq.mongodb.net"
DB_NAME = "telegram_bot_db"
MONGO_URI = f"mongodb+srv://{DB_USER}:{DB_PASS}@{DB_CLUSTER}/?retryWrites=true&w=majority"

POSITIVE_REACTIONS = ["👍", "❤️", "🔥", "🎉", "👏", "🤩", "💯", "🙏", "💘", "😘", "🤗", "🆒", "😇", "⚡", "🫡"]
FALLBACK_REACTIONS = ["👌", "😁", "❤️‍🔥", "🥰", "💋"]

# --- LOGGING ---
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# --- MONGODB CONNECTION ---
try:
    # Use certifi to ensure SSL works on all platforms (Render & Windows)
    mongo_client = MongoClient(
        MONGO_URI, 
        tlsCAFile=certifi.where(),
        tlsAllowInvalidCertificates=True 
    )
    mongo_client.admin.command('ping')
    db = mongo_client[DB_NAME]
    users_col = db['users']
    chats_col = db['chats']
    pending_col = db['pending_notifications']
    logger.info("✅ Connected to MongoDB Successfully")
except Exception as e:
    logger.critical(f"❌ Failed to connect to MongoDB: {e}")
    exit(1)

# --- FLASK KEEP-ALIVE (Low Storage / No Log Files) ---
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running on Render with MongoDB!", 200

def run_flask_app():
    port = int(os.environ.get("PORT", 5000))
    # Suppress Flask logs to save console storage/noise
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)
    app.run(host='0.0.0.0', port=port)

# --- DATABASE LOGIC (Fixing Conflict Error) ---

def track_user(user, update_last_seen: bool = False) -> None:
    """Upsert user without creating a MongoDB conflict error."""
    if not user: return
    
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    
    # 1. Fields to set ALWAYS
    set_fields = {
        "username": user.username,
        "first_name": user.first_name,
        "is_bot": user.is_bot
    }
    
    # 2. Fields to set ONLY IF NEW
    setOnInsert_fields = {"joined_at": now_iso}

    if update_last_seen:
        # If updating last_seen, put it in $set
        set_fields["last_seen"] = now_iso
    else:
        # If not updating, put it in $setOnInsert (so it is set only if the user is new)
        setOnInsert_fields["last_seen"] = now_iso

    try:
        users_col.update_one(
            {"_id": user.id},
            {
                "$set": set_fields,
                "$setOnInsert": setOnInsert_fields
            },
            upsert=True
        )
    except Exception as e:
        logger.error(f"DB Error: {e}")

def track_chat(chat_id: int, title: str, chat_type: str, adder_user_id: int) -> None:
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
        logger.error(f"DB Error: {e}")

def add_pending_notification(user_id: int, message: str):
    try:
        pending_col.update_one(
            {"_id": user_id},
            {"$push": {"messages": message}},
            upsert=True
        )
    except Exception:
        pass

def get_and_clear_pending_notifications(user_id: int):
    try:
        doc = pending_col.find_one_and_delete({"_id": user_id})
        return doc.get("messages", []) if doc else []
    except Exception:
        return []

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

# --- BOT HANDLERS ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or update.message.chat.type != ChatType.PRIVATE:
        return

    user = update.effective_user
    track_user(user, update_last_seen=True)

    msgs = get_and_clear_pending_notifications(user.id)
    for txt in msgs:
        try:
            await context.bot.send_message(chat_id=user.id, text=txt)
        except Exception:
            pass

    bot_username = (await context.bot.get_me()).username
    is_member = await is_user_member_of_channel(context, user.id)

    if is_member:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("➕ Add to Group ➕", url=f"https://t.me/{bot_username}?startgroup=true"),
            InlineKeyboardButton("📢 Add to Channel 📢", url=f"https://t.me/{bot_username}?startchannel=true"),
        ]])
        text = "🌟 *Welcome!*\nYou can now add me to your groups."
    else:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"1. Join @{MAIN_CHANNEL_USERNAME}", url=f"https://t.me/{MAIN_CHANNEL_USERNAME}")],
            [InlineKeyboardButton("2. I Have Joined ✅", callback_data="check_join")],
        ])
        text = "🔒 *Access Required*\nPlease join our channel first."

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
        text = "✅ *Thank you!*\nYou can now add me to groups."
        try:
            await query.edit_message_text(escape_markdown(text, version=2), parse_mode=ParseMode.MARKDOWN_V2, reply_markup=keyboard)
        except:
            await query.edit_message_text(text.replace("*", ""), reply_markup=keyboard)
    else:
        await query.answer("❌ You haven't joined yet.", show_alert=True)

async def handle_chat_addition(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.my_chat_member: return
    chat = update.my_chat_member.chat
    adder = update.my_chat_member.from_user
    new = update.my_chat_member.new_chat_member.status
    old = update.my_chat_member.old_chat_member.status
    
    if new in ("member", "administrator") and old not in ("member", "administrator"):
        track_chat(chat.id, chat.title or str(chat.id), "Group" if chat.type in ("group", "supergroup") else "Channel", adder.id)
        msg = f"✅ Added to {chat.title}!"
        try:
            await context.bot.send_message(adder.id, msg)
        except:
            add_pending_notification(adder.id, msg)

async def react_to_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.channel_post or update.message
    if not msg: return
    # Ignore commands or service messages
    if (msg.text and msg.text.startswith("/")) or msg.via_bot or msg.new_chat_members: return

    if msg.from_user: track_user(msg.from_user, True)

    emojis = random.sample(POSITIVE_REACTIONS + FALLBACK_REACTIONS, 3)
    for e in emojis:
        try:
            await context.bot.set_message_reaction(msg.chat.id, msg.message_id, reaction=[e], is_big=False)
            return
        except: continue

# --- ADMIN (Simplified) ---
async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id): return
    users = users_col.count_documents({})
    chats = chats_col.count_documents({})
    await update.message.reply_text(f"📊 **Stats**\nUsers: {users}\nChats: {chats}", parse_mode="Markdown")

async def is_user_member_of_channel(context, user_id):
    try:
        m = await context.bot.get_chat_member(f"@{MAIN_CHANNEL_USERNAME}", user_id)
        return m.status in ("member", "administrator", "creator")
    except: return False

def main():
    if not BOT_TOKEN: return
    
    # Start Flask (No Storage Used)
    t = threading.Thread(target=run_flask_app)
    t.daemon = True
    t.start()

    # Start Bot
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("stats", admin_stats))
    app.add_handler(CallbackQueryHandler(check_join_callback, pattern="^check_join$"))
    app.add_handler(ChatMemberHandler(handle_chat_addition, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, react_to_post))
    
    logger.info("🚀 Starting Bot Polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    keep_alive()
    main()

