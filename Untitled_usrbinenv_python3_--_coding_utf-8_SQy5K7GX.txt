#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════════╗
║       WhatsApp Group Manager - Telegram Bot                      ║
║       Python Backend + Node.js Baileys Bridge                    ║
║       Complete Production-Ready Script                           ║
╚══════════════════════════════════════════════════════════════════╝

SETUP INSTRUCTIONS:
=====================================

1. Install Python Dependencies:
   pip install python-telegram-bot==20.7 flask requests python-dotenv aiohttp

2. Create a .env file:
   BOT_TOKEN=your_telegram_bot_token
   BRIDGE_URL=http://localhost:3000
   BRIDGE_SECRET=your_bridge_secret
   ADMIN_IDS=123456789,987654321
   RENDER_URL=https://your-app.onrender.com   # leave blank for polling
   PORT=8080

3. Run:
   python bot.py
"""

import os
import re
import json
import sqlite3
import logging
import asyncio
import threading
from datetime import datetime, timedelta, timezone
from functools import wraps
from io import BytesIO

import requests
from dotenv import load_dotenv
from flask import Flask, request as flask_request

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    InputFile, BotCommand
)
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, ContextTypes,
    filters
)
from telegram.error import BadRequest, TelegramError

# ─────────────────────────────────────────────
#  ENV & CONFIG
# ─────────────────────────────────────────────
load_dotenv()

BOT_TOKEN    = os.getenv("BOT_TOKEN", "")
BRIDGE_URL   = os.getenv("BRIDGE_URL", "http://localhost:3000").rstrip("/")
BRIDGE_SECRET = os.getenv("BRIDGE_SECRET", "")
ADMIN_IDS    = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
RENDER_URL   = os.getenv("RENDER_URL", "").rstrip("/")
PORT         = int(os.getenv("PORT", 8080))
DB_PATH      = "whatsapp_bot.db"

IST = timezone(timedelta(hours=5, minutes=30))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  CONVERSATION STATES  (44 total)
# ─────────────────────────────────────────────
(
    # Connect
    ST_CONNECT_METHOD, ST_CONNECT_PHONE, ST_CONNECT_QR_POLL,
    # Create Group
    ST_CG_NAME, ST_CG_PHOTO, ST_CG_DISAPPEAR, ST_CG_PERMS,
    ST_CG_MEMBERS, ST_CG_NUM_START, ST_CG_COUNT, ST_CG_CONFIRM,
    # Join Groups
    ST_JOIN_LINKS, ST_JOIN_CONFIRM,
    # CTC Checker
    ST_CTC_MODE, ST_CTC_ACCOUNT, ST_CTC_UPLOAD, ST_CTC_RESULT,
    # Get Link
    ST_GETLINK_SELECT,
    # Leave Groups
    ST_LEAVE_SELECT, ST_LEAVE_CONFIRM,
    # Remove Members
    ST_RM_GROUP_SELECT, ST_RM_CONFIRM,
    # Make/Remove Admin
    ST_ADMIN_ACTION, ST_ADMIN_NUMBERS, ST_ADMIN_GROUP_SELECT, ST_ADMIN_CONFIRM,
    # Approval Setting
    ST_APPROVAL_CHOICE,
    # Pending List
    ST_PENDING_ACCOUNT,
    # Add Members
    ST_AM_LINKS, ST_AM_FILES, ST_AM_PAIRING, ST_AM_CONFIRM,
    # Disconnect
    ST_DISC_SELECT,
    # Admin Panel
    ST_ADM_ADD_ID, ST_ADM_ADD_DAYS, ST_ADM_REMOVE_ID,
    ST_ADM_TEMP_ID, ST_ADM_TEMP_HOURS,
    # Generic account selector
    ST_ACCT_SELECT,
    # Feature router
    ST_FEATURE_ACCT,
) = range(44)

# ─────────────────────────────────────────────
#  IN-MEMORY SESSIONS
# ─────────────────────────────────────────────
user_sessions: dict = {}   # user_id -> {"account": ..., ...}
temp_data:     dict = {}   # user_id -> arbitrary scratch data


def get_session(user_id: int) -> dict:
    if user_id not in user_sessions:
        user_sessions[user_id] = {}
    return user_sessions[user_id]


def set_temp(user_id: int, key: str, value):
    if user_id not in temp_data:
        temp_data[user_id] = {}
    temp_data[user_id][key] = value


def get_temp(user_id: int, key: str, default=None):
    return temp_data.get(user_id, {}).get(key, default)


def clear_temp(user_id: int):
    temp_data.pop(user_id, None)


# ─────────────────────────────────────────────
#  SQLITE DATABASE
# ─────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS premium_users (
            user_id   INTEGER PRIMARY KEY,
            username  TEXT,
            added_by  INTEGER,
            added_at  TEXT,
            expires_at TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS temp_access (
            user_id    INTEGER PRIMARY KEY,
            granted_by INTEGER,
            granted_at TEXT,
            expires_at TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS bot_settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    # default: FREE mode
    c.execute(
        "INSERT OR IGNORE INTO bot_settings (key, value) VALUES (?, ?)",
        ("access_mode", "FREE")
    )
    conn.commit()
    conn.close()


def db_conn():
    return sqlite3.connect(DB_PATH)


def get_access_mode() -> str:
    with db_conn() as conn:
        row = conn.execute(
            "SELECT value FROM bot_settings WHERE key = 'access_mode'"
        ).fetchone()
    return row[0] if row else "FREE"


def set_access_mode(mode: str):
    with db_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)",
            ("access_mode", mode)
        )


def add_premium_user(user_id: int, username: str, added_by: int, days: int = 0):
    now = datetime.now(IST).isoformat()
    expires = (datetime.now(IST) + timedelta(days=days)).isoformat() if days > 0 else None
    with db_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO premium_users (user_id, username, added_by, added_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, username, added_by, now, expires)
        )


def remove_premium_user(user_id: int):
    with db_conn() as conn:
        conn.execute("DELETE FROM premium_users WHERE user_id = ?", (user_id,))


def is_premium(user_id: int) -> bool:
    with db_conn() as conn:
        row = conn.execute(
            "SELECT expires_at FROM premium_users WHERE user_id = ?", (user_id,)
        ).fetchone()
    if not row:
        return False
    if row[0] is None:
        return True
    return datetime.fromisoformat(row[0]) > datetime.now(IST)


def add_temp_access(user_id: int, granted_by: int, hours: int):
    now = datetime.now(IST).isoformat()
    expires = (datetime.now(IST) + timedelta(hours=hours)).isoformat()
    with db_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO temp_access (user_id, granted_by, granted_at, expires_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, granted_by, now, expires)
        )


def has_temp_access(user_id: int) -> bool:
    with db_conn() as conn:
        row = conn.execute(
            "SELECT expires_at FROM temp_access WHERE user_id = ?", (user_id,)
        ).fetchone()
    if not row:
        return False
    return datetime.fromisoformat(row[0]) > datetime.now(IST)


def list_premium_users() -> list:
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT user_id, username, added_at, expires_at FROM premium_users"
        ).fetchall()
    return rows


def bot_stats() -> dict:
    with db_conn() as conn:
        premium_count = conn.execute("SELECT COUNT(*) FROM premium_users").fetchone()[0]
        temp_count    = conn.execute("SELECT COUNT(*) FROM temp_access").fetchone()[0]
        mode          = conn.execute(
            "SELECT value FROM bot_settings WHERE key='access_mode'"
        ).fetchone()[0]
    return {
        "premium": premium_count,
        "temp": temp_count,
        "mode": mode,
        "active_sessions": len(user_sessions),
    }


# ─────────────────────────────────────────────
#  ACCESS CONTROL DECORATOR
# ─────────────────────────────────────────────
def require_access(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        mode = get_access_mode()
        if mode == "FREE":
            return await func(update, context, *args, **kwargs)
        # PAID mode
        if user_id in ADMIN_IDS:
            return await func(update, context, *args, **kwargs)
        if is_premium(user_id) or has_temp_access(user_id):
            return await func(update, context, *args, **kwargs)
        msg = (
            "🔒 *Access Restricted*\n\n"
            "This bot is currently in PAID mode.\n"
            "Contact an admin to get premium access."
        )
        if update.callback_query:
            await update.callback_query.answer()
            await safe_edit(update.callback_query.message, msg)
        else:
            await update.message.reply_text(msg, parse_mode="Markdown")
        return ConversationHandler.END
    return wrapper


# ─────────────────────────────────────────────
#  BRIDGE API CLIENT
# ─────────────────────────────────────────────
class BridgeAPI:
    def __init__(self):
        self.base = BRIDGE_URL
        self.headers = {
            "Content-Type": "application/json",
            "X-Bridge-Secret": BRIDGE_SECRET,
        }

    def _post(self, endpoint: str, data: dict) -> dict:
        try:
            r = requests.post(
                f"{self.base}{endpoint}",
                json=data,
                headers=self.headers,
                timeout=30
            )
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Bridge POST {endpoint} error: {e}")
            return {"success": False, "error": str(e)}

    def _get(self, endpoint: str, params: dict = None) -> dict:
        try:
            r = requests.get(
                f"{self.base}{endpoint}",
                params=params,
                headers=self.headers,
                timeout=30
            )
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Bridge GET {endpoint} error: {e}")
            return {"success": False, "error": str(e)}

    def connect_qr(self, session_id: str) -> dict:
        return self._post("/connect/qr", {"sessionId": session_id})

    def connect_phone(self, session_id: str, phone: str) -> dict:
        return self._post("/connect/phone", {"sessionId": session_id, "phone": phone})

    def get_pairing_code(self, session_id: str) -> dict:
        return self._get("/connect/pairing-code", {"sessionId": session_id})

    def disconnect(self, session_id: str) -> dict:
        return self._post("/disconnect", {"sessionId": session_id})

    def get_status(self, session_id: str) -> dict:
        return self._get("/status", {"sessionId": session_id})

    def create_group(self, session_id: str, name: str, members: list) -> dict:
        return self._post("/group/create", {
            "sessionId": session_id, "name": name, "members": members
        })

    def set_group_photo(self, session_id: str, group_id: str, photo_url: str) -> dict:
        return self._post("/group/photo", {
            "sessionId": session_id, "groupId": group_id, "photoUrl": photo_url
        })

    def set_disappear(self, session_id: str, group_id: str, duration: int) -> dict:
        return self._post("/group/disappear", {
            "sessionId": session_id, "groupId": group_id, "duration": duration
        })

    def set_permissions(self, session_id: str, group_id: str,
                        send_messages: bool, edit_info: bool) -> dict:
        return self._post("/group/permissions", {
            "sessionId": session_id, "groupId": group_id,
            "sendMessages": send_messages, "editInfo": edit_info
        })

    def get_groups(self, session_id: str) -> dict:
        return self._get("/groups", {"sessionId": session_id})

    def get_group_info(self, session_id: str, group_id: str) -> dict:
        return self._get("/group/info", {"sessionId": session_id, "groupId": group_id})

    def join_group(self, session_id: str, invite_link: str) -> dict:
        return self._post("/group/join", {
            "sessionId": session_id, "inviteLink": invite_link
        })

    def get_invite_link(self, session_id: str, group_id: str) -> dict:
        return self._get("/group/invite-link", {
            "sessionId": session_id, "groupId": group_id
        })

    def leave_group(self, session_id: str, group_id: str) -> dict:
        return self._post("/group/leave", {
            "sessionId": session_id, "groupId": group_id
        })

    def get_members(self, session_id: str, group_id: str) -> dict:
        return self._get("/group/members", {
            "sessionId": session_id, "groupId": group_id
        })

    def remove_member(self, session_id: str, group_id: str, member_jid: str) -> dict:
        return self._post("/group/remove-member", {
            "sessionId": session_id, "groupId": group_id, "memberJid": member_jid
        })

    def make_admin(self, session_id: str, group_id: str, member_jid: str) -> dict:
        return self._post("/group/make-admin", {
            "sessionId": session_id, "groupId": group_id, "memberJid": member_jid
        })

    def remove_admin(self, session_id: str, group_id: str, member_jid: str) -> dict:
        return self._post("/group/remove-admin", {
            "sessionId": session_id, "groupId": group_id, "memberJid": member_jid
        })

    def set_approval(self, session_id: str, group_id: str, enabled: bool) -> dict:
        return self._post("/group/approval", {
            "sessionId": session_id, "groupId": group_id, "enabled": enabled
        })

    def get_pending(self, session_id: str, group_id: str) -> dict:
        return self._get("/group/pending", {
            "sessionId": session_id, "groupId": group_id
        })

    def reject_pending(self, session_id: str, group_id: str, member_jid: str) -> dict:
        return self._post("/group/reject-pending", {
            "sessionId": session_id, "groupId": group_id, "memberJid": member_jid
        })

    def add_member(self, session_id: str, group_id: str, member_jid: str) -> dict:
        return self._post("/group/add-member", {
            "sessionId": session_id, "groupId": group_id, "memberJid": member_jid
        })

    def is_on_whatsapp(self, session_id: str, phone: str) -> dict:
        return self._post("/check-number", {
            "sessionId": session_id, "phone": phone
        })

    def get_all_sessions(self) -> dict:
        return self._get("/sessions")


bridge = BridgeAPI()


# ─────────────────────────────────────────────
#  UTILITY FUNCTIONS
# ─────────────────────────────────────────────
def parse_vcf(text: str) -> list:
    """Parse VCF content and return list of phone numbers."""
    numbers = []
    for line in text.splitlines():
        line = line.strip()
        if line.upper().startswith("TEL"):
            parts = line.split(":")
            if len(parts) >= 2:
                number = re.sub(r"[^\d+]", "", parts[-1])
                if number:
                    numbers.append(number)
    return numbers


def parse_numbers_text(text: str) -> list:
    """Parse plain text for phone numbers (one per line or comma-separated)."""
    raw = re.split(r"[\n,;]+", text)
    numbers = []
    for item in raw:
        item = item.strip()
        number = re.sub(r"[^\d+]", "", item)
        if len(number) >= 7:
            numbers.append(number)
    return numbers


def format_jid(phone: str) -> str:
    """Convert a phone number to WhatsApp JID format."""
    phone = re.sub(r"[^\d]", "", phone)
    return f"{phone}@s.whatsapp.net"


def extract_group_id_from_link(link: str) -> str:
    """Extract the invite code from a WhatsApp group link."""
    match = re.search(r"chat\.whatsapp\.com/([A-Za-z0-9]+)", link)
    return match.group(1) if match else link


def accounts_keyboard(sessions: list, callback_prefix: str) -> InlineKeyboardMarkup:
    """Build an inline keyboard listing WhatsApp accounts."""
    buttons = []
    for s in sessions:
        sid   = s.get("sessionId", "?")
        phone = s.get("phone", sid)
        status = s.get("status", "unknown")
        icon = "🟢" if status == "connected" else "🔴"
        buttons.append(
            [InlineKeyboardButton(f"{icon} {phone}", callback_data=f"{callback_prefix}:{sid}")]
        )
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="goto_start")])
    return InlineKeyboardMarkup(buttons)


def make_main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔗 Connect WhatsApp",    callback_data="feat_connect"),
            InlineKeyboardButton("🔌 Disconnect WhatsApp", callback_data="feat_disconnect"),
        ],
        [
            InlineKeyboardButton("📋 Connected Accounts",  callback_data="feat_accounts"),
            InlineKeyboardButton("❓ Help",                callback_data="feat_help"),
        ],
        [
            InlineKeyboardButton("➕ Create Group",         callback_data="feat_create_group"),
            InlineKeyboardButton("🔗 Join Groups",         callback_data="feat_join_groups"),
        ],
        [
            InlineKeyboardButton("🔍 CTC Checker",         callback_data="feat_ctc"),
            InlineKeyboardButton("🔗 Get Link",            callback_data="feat_get_link"),
        ],
        [
            InlineKeyboardButton("🚪 Leave Groups",        callback_data="feat_leave"),
            InlineKeyboardButton("🗑 Remove Members",      callback_data="feat_remove_members"),
        ],
        [
            InlineKeyboardButton("👑 Make/Remove Admin",   callback_data="feat_admin_action"),
            InlineKeyboardButton("✅ Approval Setting",    callback_data="feat_approval"),
        ],
        [
            InlineKeyboardButton("📜 Pending List",        callback_data="feat_pending"),
            InlineKeyboardButton("➕ Add Members",         callback_data="feat_add_members"),
        ],
    ])


async def safe_edit(message, text: str, reply_markup=None, parse_mode: str = "Markdown"):
    """Edit message text, silently ignore 'message not modified' errors."""
    try:
        await message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except BadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise


async def safe_reply(update: Update, text: str, reply_markup=None, parse_mode: str = "Markdown"):
    """Reply to a message or answer a callback query."""
    if update.callback_query:
        await update.callback_query.answer()
        await safe_edit(update.callback_query.message, text, reply_markup, parse_mode)
    elif update.message:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)


def ist_now() -> datetime:
    return datetime.now(IST)


def fmt_date(dt: datetime) -> str:
    return dt.strftime("%d %B %Y")


def fmt_time(dt: datetime) -> str:
    return dt.strftime("%I:%M %p IST")


# ─────────────────────────────────────────────
#  FEATURE ROUTER (callback_data dispatcher)
# ─────────────────────────────────────────────
FEATURE_MAP = {}   # populated after handler definitions


# ─────────────────────────────────────────────
#  /start HANDLER
# ─────────────────────────────────────────────
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    now  = ist_now()
    text = (
        "🤖 *WhatsApp Group Manager*\n\n"
        f"👤 Name: {user.full_name}\n"
        f"🆔 User ID: `{user.id}`\n"
        f"📅 Date: {fmt_date(now)}\n"
        f"🕐 Time: {fmt_time(now)}\n\n"
        "Welcome! Choose an option below:"
    )
    keyboard = make_main_menu()
    if update.callback_query:
        await update.callback_query.answer()
        await safe_edit(update.callback_query.message, text, keyboard)
    elif update.message:
        await update.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")
    return ConversationHandler.END


# ─────────────────────────────────────────────
#  GOTO START (cancel / back)
# ─────────────────────────────────────────────
async def goto_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
    clear_temp(update.effective_user.id)
    return await start_handler(update, context)


# ─────────────────────────────────────────────
#  CANCEL HANDLER
# ─────────────────────────────────────────────
async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    clear_temp(uid)
    await safe_reply(update, "❌ Cancelled. Back to main menu.")
    await start_handler(update, context)
    return ConversationHandler.END


# ─────────────────────────────────────────────
#  ADMIN PANEL
# ─────────────────────────────────────────────
def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕ Add Premium",   callback_data="adm_add"),
            InlineKeyboardButton("➖ Remove Premium", callback_data="adm_remove"),
        ],
        [
            InlineKeyboardButton("⏱ Temp Access",   callback_data="adm_temp"),
            InlineKeyboardButton("📋 Premium List",  callback_data="adm_list"),
        ],
        [
            InlineKeyboardButton("🆓 FREE Mode",     callback_data="adm_free"),
            InlineKeyboardButton("💰 PAID Mode",     callback_data="adm_paid"),
        ],
        [
            InlineKeyboardButton("📊 Bot Stats",     callback_data="adm_stats"),
            InlineKeyboardButton("🔙 Back",          callback_data="goto_start"),
        ],
    ])


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await safe_reply(update, "⛔ Admin only command.")
        return ConversationHandler.END
    text = "🔧 *Admin Panel*\n\nChoose an action:"
    if update.callback_query:
        await update.callback_query.answer()
        await safe_edit(update.callback_query.message, text, admin_keyboard())
    else:
        await update.message.reply_text(text, reply_markup=admin_keyboard(), parse_mode="Markdown")
    return ST_ADM_ADD_ID   # will be overridden by callbacks


async def adm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    if user_id not in ADMIN_IDS:
        await query.message.reply_text("⛔ Admins only.")
        return ConversationHandler.END

    if data == "adm_add":
        await safe_edit(
            query.message,
            "➕ *Add Premium User*\n\nEnter the user's Telegram ID:",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="adm_back")]])
        )
        context.user_data["adm_action"] = "add"
        return ST_ADM_ADD_ID

    elif data == "adm_remove":
        await safe_edit(
            query.message,
            "➖ *Remove Premium User*\n\nEnter the user's Telegram ID:",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="adm_back")]])
        )
        context.user_data["adm_action"] = "remove"
        return ST_ADM_REMOVE_ID

    elif data == "adm_temp":
        await safe_edit(
            query.message,
            "⏱ *Temp Access*\n\nEnter the user's Telegram ID:",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="adm_back")]])
        )
        context.user_data["adm_action"] = "temp"
        return ST_ADM_TEMP_ID

    elif data == "adm_list":
        rows = list_premium_users()
        if not rows:
            text = "📋 *Premium Users*\n\nNo premium users found."
        else:
            lines = ["📋 *Premium Users*\n"]
            for uid, uname, added_at, expires in rows:
                exp_str = expires[:10] if expires else "Lifetime"
                lines.append(f"• `{uid}` (@{uname}) — Expires: {exp_str}")
            text = "\n".join(lines)
        await safe_edit(
            query.message, text,
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="adm_back")]])
        )
        return ST_ADM_ADD_ID

    elif data == "adm_free":
        set_access_mode("FREE")
        await safe_edit(
            query.message,
            "✅ Bot switched to *FREE* mode. Everyone can use the bot.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="adm_back")]])
        )
        return ST_ADM_ADD_ID

    elif data == "adm_paid":
        set_access_mode("PAID")
        await safe_edit(
            query.message,
            "💰 Bot switched to *PAID* mode. Only premium/temp users can use features.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="adm_back")]])
        )
        return ST_ADM_ADD_ID

    elif data == "adm_stats":
        stats = bot_stats()
        text = (
            "📊 *Bot Statistics*\n\n"
            f"👥 Premium Users: {stats['premium']}\n"
            f"⏱ Temp Access: {stats['temp']}\n"
            f"🔒 Mode: {stats['mode']}\n"
            f"💬 Active Sessions: {stats['active_sessions']}"
        )
        await safe_edit(
            query.message, text,
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="adm_back")]])
        )
        return ST_ADM_ADD_ID

    elif data == "adm_back":
        await safe_edit(query.message, "🔧 *Admin Panel*\n\nChoose an action:", admin_keyboard())
        return ST_ADM_ADD_ID

    return ST_ADM_ADD_ID


async def adm_got_add_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("❌ Invalid ID. Send a numeric Telegram user ID.")
        return ST_ADM_ADD_ID
    context.user_data["adm_target_id"] = int(text)
    await update.message.reply_text(
        "✅ ID received. How many days of access? (0 = lifetime)"
    )
    return ST_ADM_ADD_DAYS


async def adm_got_add_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("❌ Send a number of days (0 = lifetime).")
        return ST_ADM_ADD_DAYS
    days = int(text)
    uid = context.user_data.get("adm_target_id")
    add_premium_user(uid, "", update.effective_user.id, days)
    label = f"{days} days" if days > 0 else "Lifetime"
    await update.message.reply_text(
        f"✅ User `{uid}` added as premium ({label}).",
        parse_mode="Markdown",
        reply_markup=admin_keyboard()
    )
    return ST_ADM_ADD_ID


async def adm_got_remove_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("❌ Invalid ID.")
        return ST_ADM_REMOVE_ID
    uid = int(text)
    remove_premium_user(uid)
    await update.message.reply_text(
        f"✅ User `{uid}` removed from premium.",
        parse_mode="Markdown",
        reply_markup=admin_keyboard()
    )
    return ST_ADM_ADD_ID


async def adm_got_temp_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("❌ Invalid ID.")
        return ST_ADM_TEMP_ID
    context.user_data["adm_temp_id"] = int(text)
    await update.message.reply_text("⏱ How many hours of temp access?")
    return ST_ADM_TEMP_HOURS


async def adm_got_temp_hours(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("❌ Send a number of hours.")
        return ST_ADM_TEMP_HOURS
    hours = int(text)
    uid = context.user_data.get("adm_temp_id")
    add_temp_access(uid, update.effective_user.id, hours)
    await update.message.reply_text(
        f"✅ Temp access granted to `{uid}` for {hours} hour(s).",
        parse_mode="Markdown",
        reply_markup=admin_keyboard()
    )
    return ST_ADM_ADD_ID


admin_conv_handler = ConversationHandler(
    entry_points=[
        CommandHandler("admin", admin_panel),
        CallbackQueryHandler(adm_callback, pattern="^adm_")
    ],
    states={
        ST_ADM_ADD_ID: [
            CallbackQueryHandler(adm_callback, pattern="^adm_"),
            MessageHandler(filters.TEXT & ~filters.COMMAND, adm_got_add_id),
        ],
        ST_ADM_ADD_DAYS: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, adm_got_add_days),
        ],
        ST_ADM_REMOVE_ID: [
            CallbackQueryHandler(adm_callback, pattern="^adm_"),
            MessageHandler(filters.TEXT & ~filters.COMMAND, adm_got_remove_id),
        ],
        ST_ADM_TEMP_ID: [
            CallbackQueryHandler(adm_callback, pattern="^adm_"),
            MessageHandler(filters.TEXT & ~filters.COMMAND, adm_got_temp_id),
        ],
        ST_ADM_TEMP_HOURS: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, adm_got_temp_hours),
        ],
    },
    fallbacks=[
        CommandHandler("cancel", cancel_handler),
        CallbackQueryHandler(goto_start, pattern="^goto_start$"),
    ],
    allow_reentry=True,
)


# ─────────────────────────────────────────────
#  FEATURE 1: CONNECT WHATSAPP
# ─────────────────────────────────────────────
@require_access
async def feat_connect_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📷 QR Code",       callback_data="connect_qr"),
            InlineKeyboardButton("📱 Phone Number",  callback_data="connect_phone"),
        ],
        [InlineKeyboardButton("🔙 Back", callback_data="goto_start")],
    ])
    await safe_reply(update, "🔗 *Connect WhatsApp*\n\nChoose connection method:", keyboard)
    return ST_CONNECT_METHOD


async def connect_method_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    method = query.data

    uid = query.from_user.id
    session_id = str(uid)
    set_temp(uid, "session_id", session_id)

    if method == "connect_qr":
        await safe_edit(
            query.message,
            "⏳ Requesting QR code from bridge...",
            None
        )
        result = bridge.connect_qr(session_id)
        if result.get("success"):
            qr_url = result.get("qrUrl", "")
            if qr_url:
                try:
                    img_data = requests.get(qr_url, timeout=15).content
                    await query.message.reply_photo(
                        photo=BytesIO(img_data),
                        caption=(
                            "📷 *Scan this QR code with WhatsApp*\n\n"
                            "Open WhatsApp → Settings → Linked Devices → Link a Device\n\n"
                            "QR code expires in 60 seconds."
                        ),
                        parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("🔙 Back", callback_data="goto_start")]
                        ])
                    )
                except Exception:
                    await safe_edit(
                        query.message,
                        f"📷 Scan QR: {qr_url}\n\n_QR expires in 60 seconds._",
                        InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="goto_start")]])
                    )
            else:
                await safe_edit(
                    query.message,
                    "⚠️ QR generated but no URL returned. Check bridge.",
                    InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="goto_start")]])
                )
        else:
            err = result.get("error", "Unknown error")
            await safe_edit(
                query.message,
                f"❌ Failed to get QR code.\n\nError: `{err}`",
                InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="goto_start")]])
            )
        return ConversationHandler.END

    elif method == "connect_phone":
        await safe_edit(
            query.message,
            "📱 *Phone Number Pairing*\n\nEnter your WhatsApp number with country code:\nExample: `+919876543210`",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="goto_start")]])
        )
        return ST_CONNECT_PHONE

    return ConversationHandler.END


async def connect_phone_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    phone = update.message.text.strip()
    session_id = get_temp(uid, "session_id", str(uid))

    await update.message.reply_text("⏳ Requesting pairing code...")

    result = bridge.connect_phone(session_id, phone)
    if result.get("success"):
        code_result = bridge.get_pairing_code(session_id)
        code = code_result.get("pairingCode", "N/A")
        await update.message.reply_text(
            f"✅ *Pairing Code:*\n\n`{code}`\n\n"
            "Enter this code in WhatsApp → Settings → Linked Devices → Link with Phone Number",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="goto_start")]])
        )
    else:
        err = result.get("error", "Unknown")
        await update.message.reply_text(
            f"❌ Failed to connect.\n\nError: `{err}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="goto_start")]])
        )
    clear_temp(uid)
    return ConversationHandler.END


connect_conv = ConversationHandler(
    entry_points=[CallbackQueryHandler(feat_connect_entry, pattern="^feat_connect$")],
    states={
        ST_CONNECT_METHOD: [
            CallbackQueryHandler(connect_method_handler, pattern="^connect_(qr|phone)$"),
        ],
        ST_CONNECT_PHONE: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, connect_phone_handler),
        ],
    },
    fallbacks=[
        CommandHandler("cancel", cancel_handler),
        CallbackQueryHandler(goto_start, pattern="^goto_start$"),
    ],
    allow_reentry=True,
)


# ─────────────────────────────────────────────
#  FEATURE 2: DISCONNECT WHATSAPP
# ─────────────────────────────────────────────
@require_access
async def feat_disconnect_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = bridge.get_all_sessions()
    sessions = result.get("sessions", [])
    if not sessions:
        await safe_reply(
            update,
            "📭 No connected accounts found.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="goto_start")]])
        )
        return ConversationHandler.END
    keyboard = accounts_keyboard(sessions, "disc_select")
    await safe_reply(update, "🔌 *Disconnect WhatsApp*\n\nSelect account to disconnect:", keyboard)
    return ST_DISC_SELECT


async def disc_select_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session_id = query.data.split(":", 1)[1]

    await safe_edit(query.message, f"⏳ Disconnecting `{session_id}`...", None)
    result = bridge.disconnect(session_id)
    if result.get("success"):
        await safe_edit(
            query.message,
            f"✅ Account `{session_id}` disconnected successfully.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="goto_start")]])
        )
    else:
        err = result.get("error", "Unknown")
        await safe_edit(
            query.message,
            f"❌ Failed to disconnect.\n\nError: `{err}`",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="goto_start")]])
        )
    return ConversationHandler.END


disconnect_conv = ConversationHandler(
    entry_points=[CallbackQueryHandler(feat_disconnect_entry, pattern="^feat_disconnect$")],
    states={
        ST_DISC_SELECT: [
            CallbackQueryHandler(disc_select_handler, pattern="^disc_select:"),
        ],
    },
    fallbacks=[
        CommandHandler("cancel", cancel_handler),
        CallbackQueryHandler(goto_start, pattern="^goto_start$"),
    ],
    allow_reentry=True,
)


# ─────────────────────────────────────────────
#  FEATURE 3: CONNECTED ACCOUNTS
# ─────────────────────────────────────────────
@require_access
async def feat_accounts_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = bridge.get_all_sessions()
    sessions = result.get("sessions", [])
    if not sessions:
        await safe_reply(
            update,
            "📭 *No connected accounts.*\n\nUse 🔗 Connect WhatsApp to add an account.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="goto_start")]])
        )
        return

    lines = ["📋 *Connected Accounts*\n"]
    for i, s in enumerate(sessions, 1):
        sid    = s.get("sessionId", "?")
        phone  = s.get("phone", sid)
        status = s.get("status", "unknown")
        icon   = "🟢" if status == "connected" else "🔴"
        lines.append(f"{i}. {icon} `{phone}` — {status}")

    await safe_reply(
        update,
        "\n".join(lines),
        InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="goto_start")]])
    )


# ─────────────────────────────────────────────
#  FEATURE 4: HELP
# ─────────────────────────────────────────────
@require_access
async def feat_help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "❓ *WhatsApp Group Manager — Help*\n\n"
        "*Main Commands:*\n"
        "/start — Main menu\n"
        "/admin — Admin panel (admins only)\n"
        "/cancel — Cancel current operation\n\n"
        "*Features:*\n"
        "🔗 *Connect WhatsApp* — Link your WA account via QR or phone\n"
        "🔌 *Disconnect* — Unlink a WhatsApp account\n"
        "📋 *Connected Accounts* — View all linked accounts\n"
        "➕ *Create Group* — Create new WA group (7-step flow)\n"
        "🔗 *Join Groups* — Bulk join via invite links\n"
        "🔍 *CTC Checker* — Check if numbers are on WA, find pending members\n"
        "🔗 *Get Link* — Fetch invite links for your groups\n"
        "🚪 *Leave Groups* — Leave one or all groups\n"
        "🗑 *Remove Members* — Remove all non-admin members\n"
        "👑 *Make/Remove Admin* — Promote or demote group members\n"
        "✅ *Approval Setting* — Enable/disable approval for groups\n"
        "📜 *Pending List* — Get pending join requests table\n"
        "➕ *Add Members* — Add numbers to groups via VCF or text\n\n"
        "_Tip: Press 🔙 Back anytime to return to the main menu._"
    )
    await safe_reply(
        update, text,
        InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="goto_start")]])
    )


# ─────────────────────────────────────────────
#  FEATURE 5: CREATE GROUP (7-step)
# ─────────────────────────────────────────────
@require_access
async def feat_create_group_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    clear_temp(uid)
    # Step 1: Name
    await safe_reply(
        update,
        "➕ *Create New Group*\n\n*Step 1/7:* Enter the group name:",
        InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="goto_start")]])
    )
    return ST_CG_NAME


async def cg_got_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    set_temp(uid, "cg_name", update.message.text.strip())
    await update.message.reply_text(
        "📷 *Step 2/7:* Send a group photo or skip.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⏩ Skip", callback_data="cg_skip_photo"),
            InlineKeyboardButton("❌ Cancel", callback_data="goto_start"),
        ]])
    )
    return ST_CG_PHOTO


async def cg_skip_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    set_temp(uid, "cg_photo", None)
    await update.callback_query.answer()
    await _cg_ask_disappear(update.callback_query.message, uid)
    return ST_CG_DISAPPEAR


async def cg_got_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    photo = update.message.photo[-1]
    file = await photo.get_file()
    set_temp(uid, "cg_photo", file.file_path)
    await _cg_ask_disappear(update.message, uid)
    return ST_CG_DISAPPEAR


async def _cg_ask_disappear(message, uid: int):
    await message.reply_text(
        "⏳ *Step 3/7:* Set disappearing messages duration:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("24h",   callback_data="cg_disappear:86400"),
                InlineKeyboardButton("7 days",callback_data="cg_disappear:604800"),
                InlineKeyboardButton("90 days",callback_data="cg_disappear:7776000"),
            ],
            [
                InlineKeyboardButton("⏩ Skip", callback_data="cg_disappear:0"),
                InlineKeyboardButton("❌ Cancel", callback_data="goto_start"),
            ]
        ])
    )


async def cg_got_disappear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    duration = int(query.data.split(":")[1])
    set_temp(uid, "cg_disappear", duration)

    # Permissions toggles
    set_temp(uid, "cg_perm_send", True)   # only admins send
    set_temp(uid, "cg_perm_edit", True)   # only admins edit info

    await _cg_show_perms(query.message, uid)
    return ST_CG_PERMS


def _cg_perms_keyboard(send: bool, edit: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"✉️ Send: {'Admins Only' if send else 'Everyone'}",
                callback_data="cg_toggle_send"
            )
        ],
        [
            InlineKeyboardButton(
                f"✏️ Edit Info: {'Admins Only' if edit else 'Everyone'}",
                callback_data="cg_toggle_edit"
            )
        ],
        [
            InlineKeyboardButton("✅ Confirm Permissions", callback_data="cg_perms_done"),
            InlineKeyboardButton("❌ Cancel", callback_data="goto_start"),
        ]
    ])


async def _cg_show_perms(message, uid: int):
    send = get_temp(uid, "cg_perm_send", True)
    edit = get_temp(uid, "cg_perm_edit", True)
    await message.reply_text(
        "🔐 *Step 4/7:* Configure group permissions (toggle to change):",
        parse_mode="Markdown",
        reply_markup=_cg_perms_keyboard(send, edit)
    )


async def cg_perm_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id

    if query.data == "cg_toggle_send":
        set_temp(uid, "cg_perm_send", not get_temp(uid, "cg_perm_send", True))
    elif query.data == "cg_toggle_edit":
        set_temp(uid, "cg_perm_edit", not get_temp(uid, "cg_perm_edit", True))
    elif query.data == "cg_perms_done":
        await _cg_ask_members(query.message, uid)
        return ST_CG_MEMBERS

    send = get_temp(uid, "cg_perm_send", True)
    edit = get_temp(uid, "cg_perm_edit", True)
    await safe_edit(query.message,
        "🔐 *Step 4/7:* Configure group permissions (toggle to change):",
        _cg_perms_keyboard(send, edit)
    )
    return ST_CG_PERMS


async def _cg_ask_members(message, uid: int):
    await message.reply_text(
        "👥 *Step 5/7:* Send member numbers (one per line / comma-separated) or attach a VCF file. Skip to create empty group.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⏩ Skip", callback_data="cg_skip_members"),
            InlineKeyboardButton("❌ Cancel", callback_data="goto_start"),
        ]])
    )


async def cg_skip_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.callback_query.from_user.id
    await update.callback_query.answer()
    set_temp(uid, "cg_members", [])
    await _cg_ask_numbering(update.callback_query.message)
    return ST_CG_NUM_START


async def cg_got_members_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    numbers = parse_numbers_text(update.message.text)
    set_temp(uid, "cg_members", numbers)
    await _cg_ask_numbering(update.message)
    return ST_CG_NUM_START


async def cg_got_members_vcf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    doc = update.message.document
    file = await doc.get_file()
    data = await file.download_as_bytearray()
    numbers = parse_vcf(data.decode("utf-8", errors="ignore"))
    set_temp(uid, "cg_members", numbers)
    await update.message.reply_text(
        f"✅ Parsed {len(numbers)} numbers from VCF."
    )
    await _cg_ask_numbering(update.message)
    return ST_CG_NUM_START


async def _cg_ask_numbering(message):
    await message.reply_text(
        "🔢 *Step 6/7:* Group numbering — enter the starting number for the group name suffix (e.g. 1):",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⏩ Skip (no numbering)", callback_data="cg_skip_numstart"),
            InlineKeyboardButton("❌ Cancel", callback_data="goto_start"),
        ]])
    )


async def cg_skip_numstart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.callback_query.from_user.id
    await update.callback_query.answer()
    set_temp(uid, "cg_num_start", None)
    set_temp(uid, "cg_count", 1)
    await _cg_show_confirm(update.callback_query.message, uid)
    return ST_CG_CONFIRM


async def cg_got_num_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("❌ Enter a valid number.")
        return ST_CG_NUM_START
    set_temp(uid, "cg_num_start", int(text))
    await update.message.reply_text(
        "📊 *Step 7/7:* How many groups to create?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="goto_start")]])
    )
    return ST_CG_COUNT


async def cg_got_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text.strip()
    if not text.isdigit() or int(text) < 1:
        await update.message.reply_text("❌ Enter a valid number >= 1.")
        return ST_CG_COUNT
    set_temp(uid, "cg_count", int(text))
    await _cg_show_confirm(update.message, uid)
    return ST_CG_CONFIRM


async def _cg_show_confirm(message, uid: int):
    name      = get_temp(uid, "cg_name", "?")
    count     = get_temp(uid, "cg_count", 1)
    num_start = get_temp(uid, "cg_num_start")
    members   = get_temp(uid, "cg_members", [])
    disappear = get_temp(uid, "cg_disappear", 0)
    send_perm = "Admins Only" if get_temp(uid, "cg_perm_send", True) else "Everyone"
    edit_perm = "Admins Only" if get_temp(uid, "cg_perm_edit", True) else "Everyone"

    dis_str = {0: "Off", 86400: "24h", 604800: "7 days", 7776000: "90 days"}.get(disappear, str(disappear))
    name_preview = f"{name} {num_start}" if num_start else name

    text = (
        "📋 *Create Group — Confirm*\n\n"
        f"📝 Name: `{name_preview}` (×{count})\n"
        f"👥 Members: {len(members)}\n"
        f"⏳ Disappearing: {dis_str}\n"
        f"✉️ Send: {send_perm}\n"
        f"✏️ Edit Info: {edit_perm}\n\n"
        "Proceed?"
    )
    await message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Create", callback_data="cg_do_create"),
                InlineKeyboardButton("❌ Cancel", callback_data="goto_start"),
            ]
        ])
    )


async def cg_do_create(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid       = query.from_user.id
    name      = get_temp(uid, "cg_name", "Group")
    count     = get_temp(uid, "cg_count", 1)
    num_start = get_temp(uid, "cg_num_start")
    members   = get_temp(uid, "cg_members", [])
    disappear = get_temp(uid, "cg_disappear", 0)
    send_perm = get_temp(uid, "cg_perm_send", True)
    edit_perm = get_temp(uid, "cg_perm_edit", True)
    photo_url = get_temp(uid, "cg_photo")

    # Select account
    result = bridge.get_all_sessions()
    sessions = result.get("sessions", [])
    if not sessions:
        await safe_edit(
            query.message,
            "❌ No WhatsApp accounts connected. Please connect first.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="goto_start")]])
        )
        return ConversationHandler.END

    session_id = sessions[0]["sessionId"]
    member_jids = [format_jid(n) for n in members]

    await safe_edit(query.message, f"⏳ Creating {count} group(s)...")

    results_text = []
    for i in range(count):
        group_name = f"{name} {num_start + i}" if num_start is not None else (
            f"{name} {i+1}" if count > 1 else name
        )
        res = bridge.create_group(session_id, group_name, member_jids)
        if res.get("success"):
            gid = res.get("groupId", "")
            results_text.append(f"✅ `{group_name}` created")
            if photo_url:
                bridge.set_group_photo(session_id, gid, photo_url)
            if disappear:
                bridge.set_disappear(session_id, gid, disappear)
            bridge.set_permissions(session_id, gid, send_perm, edit_perm)
        else:
            results_text.append(f"❌ `{group_name}` failed: {res.get('error','?')}")

    clear_temp(uid)
    await safe_edit(
        query.message,
        "📋 *Create Group Results:*\n\n" + "\n".join(results_text),
        InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="goto_start")]])
    )
    return ConversationHandler.END


create_group_conv = ConversationHandler(
    entry_points=[CallbackQueryHandler(feat_create_group_entry, pattern="^feat_create_group$")],
    states={
        ST_CG_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, cg_got_name)],
        ST_CG_PHOTO: [
            MessageHandler(filters.PHOTO, cg_got_photo),
            CallbackQueryHandler(cg_skip_photo, pattern="^cg_skip_photo$"),
        ],
        ST_CG_DISAPPEAR: [
            CallbackQueryHandler(cg_got_disappear, pattern="^cg_disappear:"),
        ],
        ST_CG_PERMS: [
            CallbackQueryHandler(cg_perm_toggle, pattern="^cg_(toggle_send|toggle_edit|perms_done)$"),
        ],
        ST_CG_MEMBERS: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, cg_got_members_text),
            MessageHandler(filters.Document.ALL, cg_got_members_vcf),
            CallbackQueryHandler(cg_skip_members, pattern="^cg_skip_members$"),
        ],
        ST_CG_NUM_START: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, cg_got_num_start),
            CallbackQueryHandler(cg_skip_numstart, pattern="^cg_skip_numstart$"),
        ],
        ST_CG_COUNT: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, cg_got_count),
        ],
        ST_CG_CONFIRM: [
            CallbackQueryHandler(cg_do_create, pattern="^cg_do_create$"),
        ],
    },
    fallbacks=[
        CommandHandler("cancel", cancel_handler),
        CallbackQueryHandler(goto_start, pattern="^goto_start$"),
    ],
    allow_reentry=True,
)


# ─────────────────────────────────────────────
#  FEATURE 6: JOIN GROUPS
# ─────────────────────────────────────────────
@require_access
async def feat_join_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    clear_temp(uid)
    await safe_reply(
        update,
        "🔗 *Join Groups*\n\nPaste WhatsApp group invite links (one per line):",
        InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="goto_start")]])
    )
    return ST_JOIN_LINKS


async def join_got_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    raw = update.message.text.strip()
    links = [l.strip() for l in raw.splitlines() if "chat.whatsapp.com/" in l]
    if not links:
        await update.message.reply_text("❌ No valid WhatsApp invite links found. Try again.")
        return ST_JOIN_LINKS
    set_temp(uid, "join_links", links)
    await update.message.reply_text(
        f"📋 Found *{len(links)}* link(s). Proceed to join all?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Join All", callback_data="join_confirm"),
                InlineKeyboardButton("❌ Cancel",   callback_data="goto_start"),
            ]
        ])
    )
    return ST_JOIN_CONFIRM


async def join_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    links = get_temp(uid, "join_links", [])

    result = bridge.get_all_sessions()
    sessions = result.get("sessions", [])
    if not sessions:
        await safe_edit(
            query.message,
            "❌ No WhatsApp accounts connected.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="goto_start")]])
        )
        return ConversationHandler.END

    session_id = sessions[0]["sessionId"]
    results = []
    for link in links:
        res = bridge.join_group(session_id, link)
        if res.get("success"):
            results.append(f"✅ Joined: `{link[-20:]}`")
        else:
            results.append(f"❌ Failed: `{link[-20:]}` — {res.get('error','?')}")

    clear_temp(uid)
    await safe_edit(
        query.message,
        "📋 *Join Results:*\n\n" + "\n".join(results),
        InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="goto_start")]])
    )
    return ConversationHandler.END


join_conv = ConversationHandler(
    entry_points=[CallbackQueryHandler(feat_join_entry, pattern="^feat_join_groups$")],
    states={
        ST_JOIN_LINKS:   [MessageHandler(filters.TEXT & ~filters.COMMAND, join_got_links)],
        ST_JOIN_CONFIRM: [CallbackQueryHandler(join_confirm, pattern="^join_confirm$")],
    },
    fallbacks=[
        CommandHandler("cancel", cancel_handler),
        CallbackQueryHandler(goto_start, pattern="^goto_start$"),
    ],
    allow_reentry=True,
)


# ─────────────────────────────────────────────
#  FEATURE 7: CTC CHECKER
# ─────────────────────────────────────────────
@require_access
async def feat_ctc_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    clear_temp(uid)
    await safe_reply(
        update,
        "🔍 *CTC Checker*\n\nSelect mode:",
        InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📋 Check Pending Members", callback_data="ctc_mode:pending"),
                InlineKeyboardButton("👥 Check Members",         callback_data="ctc_mode:member"),
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="goto_start")],
        ])
    )
    return ST_CTC_MODE


async def ctc_mode_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid  = query.from_user.id
    mode = query.data.split(":")[1]
    set_temp(uid, "ctc_mode", mode)

    # Select account
    result = bridge.get_all_sessions()
    sessions = result.get("sessions", [])
    if not sessions:
        await safe_edit(
            query.message,
            "❌ No WhatsApp accounts connected.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="goto_start")]])
        )
        return ConversationHandler.END

    keyboard = accounts_keyboard(sessions, "ctc_acct")
    await safe_edit(query.message, "Select account:", keyboard)
    return ST_CTC_ACCOUNT


async def ctc_account_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    session_id = query.data.split(":", 1)[1]
    set_temp(uid, "ctc_session", session_id)

    await safe_edit(
        query.message,
        "📎 Upload a VCF file or send numbers (one per line) to check:",
        InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="goto_start")]])
    )
    return ST_CTC_UPLOAD


async def ctc_upload_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    numbers = parse_numbers_text(update.message.text)
    set_temp(uid, "ctc_numbers", numbers)
    await _ctc_process(update, uid)
    return ConversationHandler.END


async def ctc_upload_vcf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    doc = update.message.document
    file = await doc.get_file()
    data = await file.download_as_bytearray()
    numbers = parse_vcf(data.decode("utf-8", errors="ignore"))
    set_temp(uid, "ctc_numbers", numbers)
    await update.message.reply_text(f"✅ Parsed {len(numbers)} numbers from VCF. Checking...")
    await _ctc_process(update, uid)
    return ConversationHandler.END


async def _ctc_process(update: Update, uid: int):
    session_id = get_temp(uid, "ctc_session")
    mode       = get_temp(uid, "ctc_mode", "member")
    numbers    = get_temp(uid, "ctc_numbers", [])

    if not numbers:
        await update.message.reply_text("❌ No numbers found.")
        return

    on_wa = []
    not_on_wa = []
    for num in numbers:
        res = bridge.is_on_whatsapp(session_id, num)
        if res.get("onWhatsApp"):
            on_wa.append(num)
        else:
            not_on_wa.append(num)

    lines = [
        f"✅ On WhatsApp: {len(on_wa)}",
        f"❌ Not on WhatsApp: {len(not_on_wa)}",
        "",
    ]
    if not_on_wa:
        lines.append("*Numbers NOT on WhatsApp:*")
        lines.extend([f"• `{n}`" for n in not_on_wa[:50]])
        if len(not_on_wa) > 50:
            lines.append(f"... and {len(not_on_wa)-50} more")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="goto_start")]])
    )
    clear_temp(uid)


ctc_conv = ConversationHandler(
    entry_points=[CallbackQueryHandler(feat_ctc_entry, pattern="^feat_ctc$")],
    states={
        ST_CTC_MODE:    [CallbackQueryHandler(ctc_mode_handler, pattern="^ctc_mode:")],
        ST_CTC_ACCOUNT: [CallbackQueryHandler(ctc_account_handler, pattern="^ctc_acct:")],
        ST_CTC_UPLOAD: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, ctc_upload_text),
            MessageHandler(filters.Document.ALL, ctc_upload_vcf),
        ],
    },
    fallbacks=[
        CommandHandler("cancel", cancel_handler),
        CallbackQueryHandler(goto_start, pattern="^goto_start$"),
    ],
    allow_reentry=True,
)


# ─────────────────────────────────────────────
#  FEATURE 8: GET LINK
# ─────────────────────────────────────────────
@require_access
async def feat_get_link_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    clear_temp(uid)

    result = bridge.get_all_sessions()
    sessions = result.get("sessions", [])
    if not sessions:
        await safe_reply(
            update, "❌ No WhatsApp accounts connected.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="goto_start")]])
        )
        return ConversationHandler.END

    session_id = sessions[0]["sessionId"]
    set_temp(uid, "gl_session", session_id)

    groups_res = bridge.get_groups(session_id)
    groups = groups_res.get("groups", [])
    if not groups:
        await safe_reply(
            update, "📭 No groups found.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="goto_start")]])
        )
        return ConversationHandler.END

    set_temp(uid, "gl_groups", groups)
    buttons = [[InlineKeyboardButton("📋 Get All Links", callback_data="gl_all")]]
    for i, g in enumerate(groups[:20]):
        name = g.get("name", f"Group {i+1}")
        gid  = g.get("id", "")
        buttons.append([InlineKeyboardButton(f"🔗 {name}", callback_data=f"gl_one:{gid}")])
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="goto_start")])

    await safe_reply(update, "🔗 *Get Invite Links*\n\nSelect groups:", InlineKeyboardMarkup(buttons))
    return ST_GETLINK_SELECT


async def gl_select_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    session_id = get_temp(uid, "gl_session")
    groups = get_temp(uid, "gl_groups", [])
    data = query.data

    if data == "gl_all":
        await safe_edit(query.message, "⏳ Fetching all invite links...")
        lines = ["🔗 *Group Invite Links*\n"]
        for g in groups:
            name = g.get("name", "Unknown")
            gid  = g.get("id", "")
            res  = bridge.get_invite_link(session_id, gid)
            link = res.get("link", "N/A")
            lines.append(f"📌 *{name}*\n`{link}`\n")
        await safe_edit(
            query.message,
            "\n".join(lines),
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="goto_start")]])
        )
    else:
        gid = data.split(":", 1)[1]
        res = bridge.get_invite_link(session_id, gid)
        link = res.get("link", "N/A")
        name = next((g["name"] for g in groups if g["id"] == gid), gid)
        await safe_edit(
            query.message,
            f"🔗 *{name}*\n\n`{link}`",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="goto_start")]])
        )

    clear_temp(uid)
    return ConversationHandler.END


get_link_conv = ConversationHandler(
    entry_points=[CallbackQueryHandler(feat_get_link_entry, pattern="^feat_get_link$")],
    states={
        ST_GETLINK_SELECT: [
            CallbackQueryHandler(gl_select_handler, pattern="^gl_"),
        ],
    },
    fallbacks=[
        CommandHandler("cancel", cancel_handler),
        CallbackQueryHandler(goto_start, pattern="^goto_start$"),
    ],
    allow_reentry=True,
)


# ─────────────────────────────────────────────
#  FEATURE 9: LEAVE GROUPS
# ─────────────────────────────────────────────
@require_access
async def feat_leave_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    clear_temp(uid)

    result = bridge.get_all_sessions()
    sessions = result.get("sessions", [])
    if not sessions:
        await safe_reply(
            update, "❌ No WhatsApp accounts connected.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="goto_start")]])
        )
        return ConversationHandler.END

    session_id = sessions[0]["sessionId"]
    set_temp(uid, "leave_session", session_id)
    groups_res = bridge.get_groups(session_id)
    groups = groups_res.get("groups", [])
    set_temp(uid, "leave_groups", groups)
    set_temp(uid, "leave_selected", [])

    buttons = [[InlineKeyboardButton("🚪 Leave ALL Groups", callback_data="leave_all")]]
    for g in groups[:20]:
        name = g.get("name", "?")
        gid  = g.get("id", "")
        buttons.append([InlineKeyboardButton(f"🔘 {name}", callback_data=f"leave_toggle:{gid}")])
    buttons.append([
        InlineKeyboardButton("✅ Leave Selected", callback_data="leave_confirm_selected"),
        InlineKeyboardButton("❌ Cancel", callback_data="goto_start"),
    ])

    await safe_reply(update, "🚪 *Leave Groups*\n\nSelect groups to leave:", InlineKeyboardMarkup(buttons))
    return ST_LEAVE_SELECT


async def leave_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    data = query.data

    if data == "leave_all":
        set_temp(uid, "leave_confirm_all", True)
        await safe_edit(
            query.message,
            "⚠️ *Leave ALL groups?* This cannot be undone.",
            InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Yes, Leave All", callback_data="leave_do_all"),
                    InlineKeyboardButton("❌ Cancel", callback_data="goto_start"),
                ]
            ])
        )
        return ST_LEAVE_CONFIRM

    if data.startswith("leave_toggle:"):
        gid = data.split(":", 1)[1]
        selected = get_temp(uid, "leave_selected", [])
        if gid in selected:
            selected.remove(gid)
        else:
            selected.append(gid)
        set_temp(uid, "leave_selected", selected)
        await query.answer(f"{'Added' if gid in selected else 'Removed'} from selection")
        return ST_LEAVE_SELECT

    if data == "leave_confirm_selected":
        selected = get_temp(uid, "leave_selected", [])
        if not selected:
            await query.answer("No groups selected!")
            return ST_LEAVE_SELECT
        await safe_edit(
            query.message,
            f"⚠️ Leave *{len(selected)}* selected group(s)?",
            InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Confirm", callback_data="leave_do_selected"),
                    InlineKeyboardButton("❌ Cancel",  callback_data="goto_start"),
                ]
            ])
        )
        return ST_LEAVE_CONFIRM

    return ST_LEAVE_SELECT


async def leave_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    session_id = get_temp(uid, "leave_session")
    groups = get_temp(uid, "leave_groups", [])
    data = query.data

    if data == "leave_do_all":
        targets = [g["id"] for g in groups]
    else:
        targets = get_temp(uid, "leave_selected", [])

    await safe_edit(query.message, f"⏳ Leaving {len(targets)} group(s)...")
    results = []
    for gid in targets:
        name = next((g["name"] for g in groups if g["id"] == gid), gid)
        res = bridge.leave_group(session_id, gid)
        if res.get("success"):
            results.append(f"✅ Left: *{name}*")
        else:
            results.append(f"❌ Failed: *{name}* — {res.get('error','?')}")

    clear_temp(uid)
    await safe_edit(
        query.message,
        "📋 *Leave Results:*\n\n" + "\n".join(results),
        InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="goto_start")]])
    )
    return ConversationHandler.END


leave_conv = ConversationHandler(
    entry_points=[CallbackQueryHandler(feat_leave_entry, pattern="^feat_leave$")],
    states={
        ST_LEAVE_SELECT: [
            CallbackQueryHandler(leave_toggle, pattern="^leave_(all|toggle:|confirm_selected)"),
        ],
        ST_LEAVE_CONFIRM: [
            CallbackQueryHandler(leave_confirm, pattern="^leave_do_"),
        ],
    },
    fallbacks=[
        CommandHandler("cancel", cancel_handler),
        CallbackQueryHandler(goto_start, pattern="^goto_start$"),
    ],
    allow_reentry=True,
)


# ─────────────────────────────────────────────
#  FEATURE 10: REMOVE MEMBERS
# ─────────────────────────────────────────────
@require_access
async def feat_remove_members_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    clear_temp(uid)

    result = bridge.get_all_sessions()
    sessions = result.get("sessions", [])
    if not sessions:
        await safe_reply(
            update, "❌ No WhatsApp accounts connected.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="goto_start")]])
        )
        return ConversationHandler.END

    session_id = sessions[0]["sessionId"]
    set_temp(uid, "rm_session", session_id)
    groups_res = bridge.get_groups(session_id)
    groups = groups_res.get("groups", [])
    set_temp(uid, "rm_groups", groups)

    buttons = [[InlineKeyboardButton("🗑 Remove from ALL Groups", callback_data="rm_all")]]
    for g in groups[:20]:
        name = g.get("name", "?")
        gid  = g.get("id", "")
        buttons.append([InlineKeyboardButton(f"👥 {name}", callback_data=f"rm_one:{gid}")])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="goto_start")])

    await safe_reply(
        update,
        "🗑 *Remove Members*\n\nThis removes all non-admin members. Select group(s):",
        InlineKeyboardMarkup(buttons)
    )
    return ST_RM_GROUP_SELECT


async def rm_select_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    data = query.data
    groups = get_temp(uid, "rm_groups", [])

    if data == "rm_all":
        targets = [g["id"] for g in groups]
        label = "ALL groups"
    else:
        gid = data.split(":", 1)[1]
        targets = [gid]
        label = next((g["name"] for g in groups if g["id"] == gid), gid)

    set_temp(uid, "rm_targets", targets)
    await safe_edit(
        query.message,
        f"⚠️ Remove all non-admin members from *{label}*?",
        InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Confirm", callback_data="rm_confirm"),
                InlineKeyboardButton("❌ Cancel",  callback_data="goto_start"),
            ]
        ])
    )
    return ST_RM_CONFIRM


async def rm_confirm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    session_id = get_temp(uid, "rm_session")
    targets = get_temp(uid, "rm_targets", [])
    groups = get_temp(uid, "rm_groups", [])

    await safe_edit(query.message, "⏳ Removing members...")
    total_removed = 0
    results = []
    for gid in targets:
        name = next((g["name"] for g in groups if g["id"] == gid), gid)
        members_res = bridge.get_members(session_id, gid)
        members = members_res.get("members", [])
        removed = 0
        for m in members:
            if m.get("role") not in ("admin", "superadmin"):
                res = bridge.remove_member(session_id, gid, m["jid"])
                if res.get("success"):
                    removed += 1
        total_removed += removed
        results.append(f"✅ *{name}*: removed {removed}")

    clear_temp(uid)
    await safe_edit(
        query.message,
        f"📋 *Remove Members Results:*\n\nTotal removed: {total_removed}\n\n" + "\n".join(results),
        InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="goto_start")]])
    )
    return ConversationHandler.END


remove_members_conv = ConversationHandler(
    entry_points=[CallbackQueryHandler(feat_remove_members_entry, pattern="^feat_remove_members$")],
    states={
        ST_RM_GROUP_SELECT: [CallbackQueryHandler(rm_select_handler, pattern="^rm_")],
        ST_RM_CONFIRM:      [CallbackQueryHandler(rm_confirm_handler, pattern="^rm_confirm$")],
    },
    fallbacks=[
        CommandHandler("cancel", cancel_handler),
        CallbackQueryHandler(goto_start, pattern="^goto_start$"),
    ],
    allow_reentry=True,
)


# ─────────────────────────────────────────────
#  FEATURE 11: MAKE / REMOVE ADMIN
# ─────────────────────────────────────────────
@require_access
async def feat_admin_action_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    clear_temp(uid)
    await safe_reply(
        update,
        "👑 *Make / Remove Admin*\n\nSelect action:",
        InlineKeyboardMarkup([
            [
                InlineKeyboardButton("👑 Make Admin",   callback_data="adact_make"),
                InlineKeyboardButton("🚫 Remove Admin", callback_data="adact_remove"),
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="goto_start")],
        ])
    )
    return ST_ADMIN_ACTION


async def adact_type_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    action = query.data.split("_")[1]
    set_temp(uid, "adact_action", action)

    await safe_edit(
        query.message,
        "📱 Enter phone number(s) to promote/demote (one per line or comma-separated):",
        InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="goto_start")]])
    )
    return ST_ADMIN_NUMBERS


async def adact_got_numbers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    numbers = parse_numbers_text(update.message.text)
    set_temp(uid, "adact_numbers", numbers)

    result = bridge.get_all_sessions()
    sessions = result.get("sessions", [])
    if not sessions:
        await update.message.reply_text("❌ No WhatsApp accounts connected.")
        return ConversationHandler.END

    session_id = sessions[0]["sessionId"]
    set_temp(uid, "adact_session", session_id)
    groups_res = bridge.get_groups(session_id)
    groups = groups_res.get("groups", [])
    set_temp(uid, "adact_groups", groups)

    buttons = [[InlineKeyboardButton("🌐 All Groups", callback_data="adact_grp:ALL")]]
    for g in groups[:20]:
        name = g.get("name", "?")
        gid  = g.get("id", "")
        buttons.append([InlineKeyboardButton(f"👥 {name}", callback_data=f"adact_grp:{gid}")])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="goto_start")])

    await update.message.reply_text(
        "📂 Select group(s):",
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    return ST_ADMIN_GROUP_SELECT


async def adact_group_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    gid_sel = query.data.split(":", 1)[1]
    groups  = get_temp(uid, "adact_groups", [])

    if gid_sel == "ALL":
        targets = [g["id"] for g in groups]
        label = "ALL groups"
    else:
        targets = [gid_sel]
        label = next((g["name"] for g in groups if g["id"] == gid_sel), gid_sel)

    set_temp(uid, "adact_targets", targets)
    action  = get_temp(uid, "adact_action", "make")
    numbers = get_temp(uid, "adact_numbers", [])
    action_label = "make admin" if action == "make" else "remove admin"

    await safe_edit(
        query.message,
        f"⚠️ *{action_label.title()}* for {len(numbers)} number(s) in *{label}*?",
        InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Confirm", callback_data="adact_do"),
                InlineKeyboardButton("❌ Cancel",  callback_data="goto_start"),
            ]
        ])
    )
    return ST_ADMIN_CONFIRM


async def adact_do(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    session_id = get_temp(uid, "adact_session")
    targets  = get_temp(uid, "adact_targets", [])
    numbers  = get_temp(uid, "adact_numbers", [])
    action   = get_temp(uid, "adact_action", "make")
    groups   = get_temp(uid, "adact_groups", [])

    await safe_edit(query.message, "⏳ Processing...")
    results = []
    for gid in targets:
        name = next((g["name"] for g in groups if g["id"] == gid), gid)
        for num in numbers:
            jid = format_jid(num)
            if action == "make":
                res = bridge.make_admin(session_id, gid, jid)
            else:
                res = bridge.remove_admin(session_id, gid, jid)
            icon = "✅" if res.get("success") else "❌"
            results.append(f"{icon} {num} in *{name}*")

    clear_temp(uid)
    await safe_edit(
        query.message,
        "📋 *Admin Action Results:*\n\n" + "\n".join(results[:50]),
        InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="goto_start")]])
    )
    return ConversationHandler.END


admin_action_conv = ConversationHandler(
    entry_points=[CallbackQueryHandler(feat_admin_action_entry, pattern="^feat_admin_action$")],
    states={
        ST_ADMIN_ACTION:       [CallbackQueryHandler(adact_type_handler, pattern="^adact_(make|remove)$")],
        ST_ADMIN_NUMBERS:      [MessageHandler(filters.TEXT & ~filters.COMMAND, adact_got_numbers)],
        ST_ADMIN_GROUP_SELECT: [CallbackQueryHandler(adact_group_handler, pattern="^adact_grp:")],
        ST_ADMIN_CONFIRM:      [CallbackQueryHandler(adact_do, pattern="^adact_do$")],
    },
    fallbacks=[
        CommandHandler("cancel", cancel_handler),
        CallbackQueryHandler(goto_start, pattern="^goto_start$"),
    ],
    allow_reentry=True,
)


# ─────────────────────────────────────────────
#  FEATURE 12: APPROVAL SETTING
# ─────────────────────────────────────────────
@require_access
async def feat_approval_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    clear_temp(uid)

    result = bridge.get_all_sessions()
    sessions = result.get("sessions", [])
    if not sessions:
        await safe_reply(
            update, "❌ No WhatsApp accounts connected.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="goto_start")]])
        )
        return ConversationHandler.END

    session_id = sessions[0]["sessionId"]
    set_temp(uid, "appr_session", session_id)

    await safe_reply(
        update,
        "✅ *Approval Setting*\n\nApply to all groups:",
        InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Enable Approval (All)",  callback_data="appr_on"),
                InlineKeyboardButton("❌ Disable Approval (All)", callback_data="appr_off"),
            ],
            [InlineKeyboardButton("🔙 Back", callback_data="goto_start")],
        ])
    )
    return ST_APPROVAL_CHOICE


async def approval_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    session_id = get_temp(uid, "appr_session")
    enabled = query.data == "appr_on"

    groups_res = bridge.get_groups(session_id)
    groups = groups_res.get("groups", [])

    await safe_edit(query.message, f"⏳ {'Enabling' if enabled else 'Disabling'} approval for {len(groups)} groups...")
    ok = fail = 0
    for g in groups:
        res = bridge.set_approval(session_id, g["id"], enabled)
        if res.get("success"):
            ok += 1
        else:
            fail += 1

    clear_temp(uid)
    status = "enabled" if enabled else "disabled"
    await safe_edit(
        query.message,
        f"✅ Approval *{status}* for {ok} group(s). Failed: {fail}.",
        InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="goto_start")]])
    )
    return ConversationHandler.END


approval_conv = ConversationHandler(
    entry_points=[CallbackQueryHandler(feat_approval_entry, pattern="^feat_approval$")],
    states={
        ST_APPROVAL_CHOICE: [CallbackQueryHandler(approval_choice, pattern="^appr_(on|off)$")],
    },
    fallbacks=[
        CommandHandler("cancel", cancel_handler),
        CallbackQueryHandler(goto_start, pattern="^goto_start$"),
    ],
    allow_reentry=True,
)


# ─────────────────────────────────────────────
#  FEATURE 13: GET PENDING LIST
# ─────────────────────────────────────────────
@require_access
async def feat_pending_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    clear_temp(uid)

    result = bridge.get_all_sessions()
    sessions = result.get("sessions", [])
    if not sessions:
        await safe_reply(
            update, "❌ No WhatsApp accounts connected.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="goto_start")]])
        )
        return ConversationHandler.END

    keyboard = accounts_keyboard(sessions, "pending_acct")
    await safe_reply(update, "📜 *Pending Join Requests*\n\nSelect account:", keyboard)
    return ST_PENDING_ACCOUNT


async def pending_account_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    session_id = query.data.split(":", 1)[1]

    await safe_edit(query.message, "⏳ Fetching pending requests from all groups...")

    groups_res = bridge.get_groups(session_id)
    groups = groups_res.get("groups", [])

    lines = ["📜 *Pending Join Requests*\n"]
    total = 0
    for g in groups:
        pend_res = bridge.get_pending(session_id, g["id"])
        pending = pend_res.get("pending", [])
        if pending:
            total += len(pending)
            lines.append(f"📌 *{g.get('name','?')}* ({len(pending)} pending)")
            for p in pending[:10]:
                jid = p.get("jid", "?")
                phone = jid.split("@")[0]
                lines.append(f"  • `{phone}`")
            if len(pending) > 10:
                lines.append(f"  ... and {len(pending)-10} more")

    lines.append(f"\n📊 *Total pending: {total}*")

    await safe_edit(
        query.message,
        "\n".join(lines),
        InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="goto_start")]])
    )
    return ConversationHandler.END


pending_conv = ConversationHandler(
    entry_points=[CallbackQueryHandler(feat_pending_entry, pattern="^feat_pending$")],
    states={
        ST_PENDING_ACCOUNT: [CallbackQueryHandler(pending_account_handler, pattern="^pending_acct:")],
    },
    fallbacks=[
        CommandHandler("cancel", cancel_handler),
        CallbackQueryHandler(goto_start, pattern="^goto_start$"),
    ],
    allow_reentry=True,
)


# ─────────────────────────────────────────────
#  FEATURE 14: ADD MEMBERS
# ─────────────────────────────────────────────
@require_access
async def feat_add_members_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    clear_temp(uid)

    result = bridge.get_all_sessions()
    sessions = result.get("sessions", [])
    if not sessions:
        await safe_reply(
            update, "❌ No WhatsApp accounts connected.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="goto_start")]])
        )
        return ConversationHandler.END

    session_id = sessions[0]["sessionId"]
    set_temp(uid, "am_session", session_id)

    await safe_reply(
        update,
        "➕ *Add Members*\n\n*Step 1:* Paste group invite links (one per line):",
        InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="goto_start")]])
    )
    return ST_AM_LINKS


async def am_got_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    raw = update.message.text.strip()
    links = [l.strip() for l in raw.splitlines() if "chat.whatsapp.com/" in l]
    if not links:
        await update.message.reply_text("❌ No valid links found. Try again.")
        return ST_AM_LINKS
    set_temp(uid, "am_links", links)
    set_temp(uid, "am_numbers", [])
    await update.message.reply_text(
        f"✅ {len(links)} link(s) saved.\n\n*Step 2:* Send VCF file(s) or phone numbers (one per line).\nSend /done when finished.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="goto_start")]])
    )
    return ST_AM_FILES


async def am_got_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    doc = update.message.document
    file = await doc.get_file()
    data = await file.download_as_bytearray()
    new_nums = parse_vcf(data.decode("utf-8", errors="ignore"))
    existing = get_temp(uid, "am_numbers", [])
    existing.extend(new_nums)
    set_temp(uid, "am_numbers", existing)
    await update.message.reply_text(
        f"✅ Added {len(new_nums)} numbers. Total: {len(existing)}.\n"
        "Send more files/numbers or /done to proceed."
    )
    return ST_AM_FILES


async def am_got_text_numbers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if update.message.text.strip() == "/done":
        return await am_proceed(update, context)
    new_nums = parse_numbers_text(update.message.text)
    existing = get_temp(uid, "am_numbers", [])
    existing.extend(new_nums)
    set_temp(uid, "am_numbers", existing)
    await update.message.reply_text(
        f"✅ Added {len(new_nums)} numbers. Total: {len(existing)}.\n"
        "Send more or type /done to proceed."
    )
    return ST_AM_FILES


async def am_proceed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    links = get_temp(uid, "am_links", [])
    numbers = get_temp(uid, "am_numbers", [])

    if not numbers:
        await update.message.reply_text("❌ No phone numbers found. Send numbers or a VCF file.")
        return ST_AM_FILES

    await update.message.reply_text(
        f"📋 *Confirm Add Members*\n\n"
        f"📌 Groups: {len(links)}\n"
        f"👥 Numbers: {len(numbers)}\n\n"
        "Proceed?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Add Members", callback_data="am_confirm"),
                InlineKeyboardButton("❌ Cancel",       callback_data="goto_start"),
            ]
        ])
    )
    return ST_AM_CONFIRM


async def am_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    session_id = get_temp(uid, "am_session")
    links   = get_temp(uid, "am_links", [])
    numbers = get_temp(uid, "am_numbers", [])

    await safe_edit(query.message, "⏳ Joining groups and adding members...")

    results = []
    for link in links:
        join_res = bridge.join_group(session_id, link)
        gid = join_res.get("groupId", "")
        if not join_res.get("success") and not gid:
            code = extract_group_id_from_link(link)
            results.append(f"❌ Could not join group from `{code}`")
            continue

        added = failed = 0
        for num in numbers:
            jid = format_jid(num)
            res = bridge.add_member(session_id, gid, jid)
            if res.get("success"):
                added += 1
            else:
                failed += 1
        results.append(f"✅ Group `{link[-15:]}`: added {added}, failed {failed}")

    clear_temp(uid)
    await safe_edit(
        query.message,
        "📋 *Add Members Results:*\n\n" + "\n".join(results),
        InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="goto_start")]])
    )
    return ConversationHandler.END


add_members_conv = ConversationHandler(
    entry_points=[CallbackQueryHandler(feat_add_members_entry, pattern="^feat_add_members$")],
    states={
        ST_AM_LINKS: [MessageHandler(filters.TEXT & ~filters.COMMAND, am_got_links)],
        ST_AM_FILES: [
            MessageHandler(filters.Document.ALL, am_got_file),
            MessageHandler(filters.TEXT & ~filters.COMMAND, am_got_text_numbers),
            CommandHandler("done", am_proceed),
        ],
        ST_AM_CONFIRM: [CallbackQueryHandler(am_confirm, pattern="^am_confirm$")],
    },
    fallbacks=[
        CommandHandler("cancel", cancel_handler),
        CallbackQueryHandler(goto_start, pattern="^goto_start$"),
    ],
    allow_reentry=True,
)


# ─────────────────────────────────────────────
#  MISC CALLBACK HANDLER (accounts, help, etc.)
# ─────────────────────────────────────────────
async def misc_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "feat_accounts":
        await feat_accounts_handler(update, context)
    elif data == "feat_help":
        await feat_help_handler(update, context)
    elif data == "goto_start":
        await goto_start(update, context)


# ─────────────────────────────────────────────
#  FLASK APP
# ─────────────────────────────────────────────
flask_app = Flask(__name__)
telegram_app: Application = None   # set in main()


@flask_app.route("/")
def index():
    return "Bot is running!", 200


@flask_app.route(f"/webhook/{BOT_TOKEN}", methods=["POST"])
def webhook():
    if not telegram_app:
        return "Not ready", 503
    try:
        update_data = flask_request.get_json(force=True)
        update = Update.de_json(update_data, telegram_app.bot)
        asyncio.run_coroutine_threadsafe(
            telegram_app.process_update(update),
            telegram_app.loop
        )
        return "OK", 200
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return "Error", 500


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────
def main():
    global telegram_app

    # Initialize DB
    init_db()
    logger.info("Database initialized.")

    # Build application
    telegram_app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )

    # ── Register all handlers ──────────────────
    # Admin conv (high priority)
    telegram_app.add_handler(admin_conv_handler, group=0)

    # Feature conversations
    telegram_app.add_handler(connect_conv,        group=1)
    telegram_app.add_handler(disconnect_conv,     group=1)
    telegram_app.add_handler(create_group_conv,   group=1)
    telegram_app.add_handler(join_conv,           group=1)
    telegram_app.add_handler(ctc_conv,            group=1)
    telegram_app.add_handler(get_link_conv,       group=1)
    telegram_app.add_handler(leave_conv,          group=1)
    telegram_app.add_handler(remove_members_conv, group=1)
    telegram_app.add_handler(admin_action_conv,   group=1)
    telegram_app.add_handler(approval_conv,       group=1)
    telegram_app.add_handler(pending_conv,        group=1)
    telegram_app.add_handler(add_members_conv,    group=1)

    # Global /start and /cancel
    telegram_app.add_handler(CommandHandler("start",  start_handler),  group=2)
    telegram_app.add_handler(CommandHandler("cancel", cancel_handler), group=2)

    # Misc callbacks (accounts, help, goto_start)
    telegram_app.add_handler(
        CallbackQueryHandler(misc_callback, pattern="^(feat_accounts|feat_help|goto_start)$"),
        group=3
    )

    # Set bot commands
    async def post_init(app: Application):
        await app.bot.set_my_commands([
            BotCommand("start",  "Main menu"),
            BotCommand("admin",  "Admin panel"),
            BotCommand("cancel", "Cancel current operation"),
            BotCommand("done",   "Finish file/number input"),
        ])

    telegram_app.post_init = post_init

    # ── Deployment mode ────────────────────────
    if RENDER_URL:
        logger.info(f"Webhook mode — URL: {RENDER_URL}")
        webhook_url = f"{RENDER_URL}/webhook/{BOT_TOKEN}"

        async def set_webhook_and_start():
            await telegram_app.initialize()
            await telegram_app.bot.set_webhook(webhook_url)
            await telegram_app.start()
            logger.info(f"Webhook set: {webhook_url}")

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        telegram_app.loop = loop
        loop.run_until_complete(set_webhook_and_start())

        # Run Flask in main thread
        flask_app.run(host="0.0.0.0", port=PORT, debug=False)

    else:
        logger.info("Polling mode (local dev)")
        telegram_app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
