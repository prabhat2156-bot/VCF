#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════════╗
║       WhatsApp Group Manager - Telegram Bot                      ║
║       Python Backend + Node.js Baileys Bridge                    ║
║       Complete Production-Ready Script (MongoDB Edition)         ║
╚══════════════════════════════════════════════════════════════════╝

SETUP INSTRUCTIONS:
=====================================

1. Install Python Dependencies:
   pip install python-telegram-bot pymongo flask requests python-dotenv

2. Environment Variables (.env):
   BOT_TOKEN=your_telegram_bot_token
   ADMIN_IDS=123456789,987654321
   BRIDGE_URL=http://localhost:3000
   BRIDGE_API_KEY=your_bridge_api_key
   MONGO_URI=mongodb+srv://user:pass@cluster.mongodb.net/whatsapp_bot
   RENDER_URL=https://your-app.onrender.com   (optional, for webhook)
   PORT=8080                                   (optional, default 8080)

3. Node.js Baileys Bridge must be running separately.

4. Run: python bot.py
"""

import os
import sys
import json
import logging
import threading
import time
import asyncio
from datetime import datetime, timedelta, timezone
from functools import wraps

import requests
from flask import Flask, request, jsonify
from pymongo import MongoClient
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    BotCommand
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ConversationHandler, ContextTypes,
    filters
)
from dotenv import load_dotenv

# ─────────────────────────────────────────────────────────────────
# LOAD ENVIRONMENT
# ─────────────────────────────────────────────────────────────────
load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────
BOT_TOKEN   = os.getenv("BOT_TOKEN", "")
BRIDGE_URL  = os.getenv("BRIDGE_URL", "http://localhost:3000")
BRIDGE_KEY  = os.getenv("BRIDGE_API_KEY", "")
MONGO_URI   = os.getenv("MONGO_URI", "mongodb+srv://user:pass@cluster.mongodb.net/whatsapp_bot")
RENDER_URL  = os.getenv("RENDER_URL", "")
PORT        = int(os.getenv("PORT", 8080))

_raw_admins = os.getenv("ADMIN_IDS", "")
ADMIN_IDS   = set(int(x.strip()) for x in _raw_admins.split(",") if x.strip().isdigit())

IST = timezone(timedelta(hours=5, minutes=30))

# ─────────────────────────────────────────────────────────────────
# MONGODB SETUP
# ─────────────────────────────────────────────────────────────────
mongo_client   = MongoClient(MONGO_URI)
db             = mongo_client["whatsapp_bot"]
premium_col    = db["premium_users"]    # {user_id, added_by, added_at, expires_at}
temp_access_col= db["temp_access"]      # {user_id, granted_by, granted_at, expires_at}
settings_col   = db["bot_settings"]     # {key, value}


def init_db():
    """Initialize default settings if they do not exist."""
    if not settings_col.find_one({"key": "bot_mode"}):
        settings_col.insert_one({"key": "bot_mode", "value": "paid"})
    # Create indexes for fast lookups
    premium_col.create_index("user_id", unique=True)
    temp_access_col.create_index("user_id", unique=True)
    logger.info("MongoDB initialized.")


def get_bot_mode() -> str:
    doc = settings_col.find_one({"key": "bot_mode"})
    return doc["value"] if doc else "paid"


def set_bot_mode(mode: str):
    settings_col.update_one(
        {"key": "bot_mode"},
        {"$set": {"value": mode}},
        upsert=True
    )


def is_premium(user_id: int) -> bool:
    return premium_col.find_one({"user_id": user_id}) is not None


def add_premium(user_id: int, added_by: int):
    premium_col.update_one(
        {"user_id": user_id},
        {"$set": {
            "user_id":   user_id,
            "added_by":  added_by,
            "added_at":  datetime.now(IST).isoformat()
        }},
        upsert=True
    )


def remove_premium(user_id: int):
    premium_col.delete_one({"user_id": user_id})


def get_all_premium() -> list:
    return list(premium_col.find({}, {"_id": 0}))


def grant_temp_access(user_id: int, granted_by: int, hours: float):
    expires = datetime.now(IST) + timedelta(hours=hours)
    temp_access_col.update_one(
        {"user_id": user_id},
        {"$set": {
            "user_id":    user_id,
            "granted_by": granted_by,
            "granted_at": datetime.now(IST).isoformat(),
            "expires_at": expires.isoformat()
        }},
        upsert=True
    )


def has_temp_access(user_id: int) -> bool:
    doc = temp_access_col.find_one({"user_id": user_id})
    if not doc:
        return False
    expires = datetime.fromisoformat(doc["expires_at"])
    if datetime.now(IST) > expires:
        temp_access_col.delete_one({"user_id": user_id})
        return False
    return True


def get_active_temp_users() -> list:
    now_iso = datetime.now(IST).isoformat()
    return list(temp_access_col.find({"expires_at": {"$gt": now_iso}}, {"_id": 0}))


def get_temp_expiry_str(user_id: int) -> str:
    """Return human-readable expiry for a temp user."""
    doc = temp_access_col.find_one({"user_id": user_id})
    if not doc:
        return "Unknown"
    expires = datetime.fromisoformat(doc["expires_at"])
    return expires.strftime("%d %B %Y, %I:%M %p IST")


def user_has_access(user_id: int) -> bool:
    """Central access check: admin OR free mode OR premium OR temp."""
    if user_id in ADMIN_IDS:
        return True
    if get_bot_mode() == "free":
        return True
    if is_premium(user_id):
        return True
    if has_temp_access(user_id):
        return True
    return False


# ─────────────────────────────────────────────────────────────────
# ACCESS CONTROL DECORATOR
# ─────────────────────────────────────────────────────────────────
def require_access(func):
    """Decorator: block users without access in PAID mode."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_has_access(user_id):
            return await func(update, context)
        msg = update.message or (update.callback_query.message if update.callback_query else None)
        if msg:
            await msg.reply_text(
                "⛔ <b>Access Denied</b>\n\n"
                "This bot is for premium users only.\n"
                "Contact the admin to get access.",
                parse_mode=ParseMode.HTML
            )
        return ConversationHandler.END
    return wrapper


def require_admin(func):
    """Decorator: allow only ADMIN_IDS."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id in ADMIN_IDS:
            return await func(update, context)
        msg = update.message or (update.callback_query.message if update.callback_query else None)
        if msg:
            await msg.reply_text("⛔ Admin only command.")
        return ConversationHandler.END
    return wrapper


# ─────────────────────────────────────────────────────────────────
# BRIDGE API CLASS — ALL 23 METHODS
# ─────────────────────────────────────────────────────────────────
class BridgeAPI:
    """Wrapper for all Node.js Baileys Bridge API calls."""

    def __init__(self, base_url: str, api_key: str):
        self.base  = base_url.rstrip("/")
        self.key   = api_key
        self.hdrs  = {"x-api-key": api_key, "Content-Type": "application/json"}

    def _post(self, path: str, payload: dict) -> dict:
        try:
            r = requests.post(f"{self.base}{path}", json=payload, headers=self.hdrs, timeout=60)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.error("Bridge POST %s → %s", path, e)
            return {"success": False, "error": str(e)}

    def _get(self, path: str, params: dict = None) -> dict:
        try:
            r = requests.get(f"{self.base}{path}", params=params, headers=self.hdrs, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.error("Bridge GET %s → %s", path, e)
            return {"success": False, "error": str(e)}

    # 1. Connect via QR code
    def connect_qr(self, session_id: str) -> dict:
        return self._post("/api/connect/qr", {"sessionId": session_id})

    # 2. Connect via phone number pairing
    def connect_phone(self, session_id: str, phone: str) -> dict:
        return self._post("/api/connect/phone", {"sessionId": session_id, "phone": phone})

    # 3. Disconnect session
    def disconnect(self, session_id: str) -> dict:
        return self._post("/api/disconnect", {"sessionId": session_id})

    # 4. List all active sessions
    def list_sessions(self) -> dict:
        return self._get("/api/sessions")

    # 5. Create a WhatsApp group
    def create_group(self, session_id: str, name: str, participants: list) -> dict:
        return self._post("/api/group/create", {
            "sessionId":    session_id,
            "name":         name,
            "participants": participants
        })

    # 6. Join a group via invite link
    def join_group(self, session_id: str, link: str) -> dict:
        return self._post("/api/group/join", {"sessionId": session_id, "link": link})

    # 7. Get group invite link
    def get_group_link(self, session_id: str, group_id: str) -> dict:
        return self._post("/api/group/link", {"sessionId": session_id, "groupId": group_id})

    # 8. Leave a group
    def leave_group(self, session_id: str, group_id: str) -> dict:
        return self._post("/api/group/leave", {"sessionId": session_id, "groupId": group_id})

    # 9. Get all joined groups
    def get_groups(self, session_id: str) -> dict:
        return self._post("/api/group/list", {"sessionId": session_id})

    # 10. Get group members
    def get_members(self, session_id: str, group_id: str) -> dict:
        return self._post("/api/group/members", {"sessionId": session_id, "groupId": group_id})

    # 11. Add members to group
    def add_members(self, session_id: str, group_id: str, numbers: list) -> dict:
        return self._post("/api/group/add", {
            "sessionId": session_id,
            "groupId":   group_id,
            "numbers":   numbers
        })

    # 12. Remove members from group
    def remove_members(self, session_id: str, group_id: str, numbers: list) -> dict:
        return self._post("/api/group/remove", {
            "sessionId": session_id,
            "groupId":   group_id,
            "numbers":   numbers
        })

    # 13. Promote members to admin
    def make_admin(self, session_id: str, group_id: str, numbers: list) -> dict:
        return self._post("/api/group/promote", {
            "sessionId": session_id,
            "groupId":   group_id,
            "numbers":   numbers
        })

    # 14. Demote admins to regular member
    def remove_admin(self, session_id: str, group_id: str, numbers: list) -> dict:
        return self._post("/api/group/demote", {
            "sessionId": session_id,
            "groupId":   group_id,
            "numbers":   numbers
        })

    # 15. Set group approval/join settings
    def set_approval(self, session_id: str, group_id: str, mode: str) -> dict:
        # mode: "on" | "off"
        return self._post("/api/group/approval", {
            "sessionId": session_id,
            "groupId":   group_id,
            "mode":      mode
        })

    # 16. Get pending join requests
    def get_pending(self, session_id: str, group_id: str) -> dict:
        return self._post("/api/group/pending", {"sessionId": session_id, "groupId": group_id})

    # 17. Approve a pending request
    def approve_pending(self, session_id: str, group_id: str, number: str) -> dict:
        return self._post("/api/group/approve", {
            "sessionId": session_id,
            "groupId":   group_id,
            "number":    number
        })

    # 18. Reject a pending request
    def reject_pending(self, session_id: str, group_id: str, number: str) -> dict:
        return self._post("/api/group/reject", {
            "sessionId": session_id,
            "groupId":   group_id,
            "number":    number
        })

    # 19. Check if number is on WhatsApp (CTC)
    def check_number(self, session_id: str, number: str) -> dict:
        return self._post("/api/check/number", {"sessionId": session_id, "number": number})

    # 20. Bulk CTC check
    def check_numbers_bulk(self, session_id: str, numbers: list) -> dict:
        return self._post("/api/check/bulk", {"sessionId": session_id, "numbers": numbers})

    # 21. Send message to number
    def send_message(self, session_id: str, number: str, text: str) -> dict:
        return self._post("/api/message/send", {
            "sessionId": session_id,
            "number":    number,
            "text":      text
        })

    # 22. Get session status
    def get_status(self, session_id: str) -> dict:
        return self._get("/api/status", {"sessionId": session_id})

    # 23. Reset / logout session completely
    def reset_session(self, session_id: str) -> dict:
        return self._post("/api/session/reset", {"sessionId": session_id})


bridge = BridgeAPI(BRIDGE_URL, BRIDGE_KEY)


# ─────────────────────────────────────────────────────────────────
# CONVERSATION STATES
# ─────────────────────────────────────────────────────────────────
# Main menu
MAIN_MENU = 0

# Connect
CONNECT_METHOD, CONNECT_PHONE_INPUT, CONNECT_SESSION_ID = range(1, 4)

# Create Group
CG_NAME, CG_DESCRIPTION, CG_PROFILE, CG_MEMBERS, CG_WELCOME, CG_APPROVAL, CG_CONFIRM = range(4, 11)

# Join Groups
JOIN_SESSION, JOIN_LINKS = range(11, 13)

# CTC Checker
CTC_SESSION, CTC_MODE, CTC_NUMBERS, CTC_GROUP = range(13, 17)

# Get Link
GETLINK_SESSION, GETLINK_GROUP = range(17, 19)

# Leave Groups
LEAVE_SESSION, LEAVE_GROUP, LEAVE_CONFIRM = range(19, 22)

# Remove Members
RM_SESSION, RM_GROUP, RM_NUMBERS = range(22, 25)

# Make/Remove Admin
ADMIN_OP_SESSION, ADMIN_OP_GROUP, ADMIN_OP_MEMBERS, ADMIN_OP_ACTION = range(25, 29)

# Approval
APPROVAL_SESSION, APPROVAL_GROUP, APPROVAL_MODE = range(29, 32)

# Pending
PENDING_SESSION, PENDING_GROUP, PENDING_ACTION = range(32, 35)

# Add Members
ADDM_SESSION, ADDM_GROUP, ADDM_NUMBERS = range(35, 38)

# Disconnect
DISC_SESSION, DISC_CONFIRM = range(38, 40)

# Connected accounts (info only, no states needed)

# Admin Panel States
ADMIN_MENU         = 40
ADMIN_ADD_PREMIUM  = 41
ADMIN_REMOVE_PREM  = 42
ADMIN_TEMP_UID     = 43
ADMIN_TEMP_DURATION= 44


# ─────────────────────────────────────────────────────────────────
# UTILITY: KEYBOARDS
# ─────────────────────────────────────────────────────────────────
def make_main_menu() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🔗 Connect WhatsApp",   callback_data="connect"),
         InlineKeyboardButton("❌ Disconnect",          callback_data="disconnect")],
        [InlineKeyboardButton("📋 Connected Accounts",  callback_data="accounts"),
         InlineKeyboardButton("❓ Help",                callback_data="help")],
        [InlineKeyboardButton("➕ Create Group",        callback_data="create_group"),
         InlineKeyboardButton("🔗 Join Groups",         callback_data="join")],
        [InlineKeyboardButton("📞 CTC Checker",         callback_data="ctc"),
         InlineKeyboardButton("🔑 Get Link",            callback_data="getlink")],
        [InlineKeyboardButton("🚪 Leave Groups",        callback_data="leave"),
         InlineKeyboardButton("🗑 Remove Members",      callback_data="remove_members")],
        [InlineKeyboardButton("👑 Make/Remove Admin",   callback_data="admin_op"),
         InlineKeyboardButton("✅ Approval Setting",    callback_data="approval")],
        [InlineKeyboardButton("📋 Get Pending List",    callback_data="pending"),
         InlineKeyboardButton("➕ Add Members",         callback_data="add_members")],
        [InlineKeyboardButton("🛠 Admin Panel",         callback_data="admin_panel")],
    ]
    return InlineKeyboardMarkup(rows)


def make_back_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Menu", callback_data="back_menu")]])


def make_cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]])


def make_admin_menu() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("➕ Add Premium",          callback_data="adm_add_premium"),
         InlineKeyboardButton("➖ Remove Premium",       callback_data="adm_rm_premium")],
        [InlineKeyboardButton("⏱ Grant Temp Access",    callback_data="adm_temp"),
         InlineKeyboardButton("📋 Premium List",         callback_data="adm_list")],
        [InlineKeyboardButton("🆓 Set FREE Mode",        callback_data="adm_free"),
         InlineKeyboardButton("💰 Set PAID Mode",        callback_data="adm_paid")],
        [InlineKeyboardButton("📊 Bot Stats",            callback_data="adm_stats"),
         InlineKeyboardButton("🔙 Back to Menu",         callback_data="back_menu")],
    ]
    return InlineKeyboardMarkup(rows)


def make_temp_duration_kb(uid: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("1 Hour",   callback_data=f"td_1_{uid}"),
         InlineKeyboardButton("6 Hours",  callback_data=f"td_6_{uid}"),
         InlineKeyboardButton("12 Hours", callback_data=f"td_12_{uid}")],
        [InlineKeyboardButton("24 Hours", callback_data=f"td_24_{uid}"),
         InlineKeyboardButton("3 Days",   callback_data=f"td_72_{uid}"),
         InlineKeyboardButton("7 Days",   callback_data=f"td_168_{uid}")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
    ]
    return InlineKeyboardMarkup(rows)


# ─────────────────────────────────────────────────────────────────
# /start COMMAND
# ─────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    now  = datetime.now(IST)
    date_str = now.strftime("%d %B %Y")
    time_str = now.strftime("%I:%M %p IST")

    uid = user.id
    if uid in ADMIN_IDS:
        status = "👑 Admin"
    elif is_premium(uid):
        status = "⭐ Premium User"
    elif has_temp_access(uid):
        status = "⏱ Temporary Access"
    elif get_bot_mode() == "free":
        status = "🆓 Free Access"
    else:
        status = "🔒 No Access"

    text = (
        f"🤖 <b>WhatsApp Group Manager</b>\n\n"
        f"👤 Name: {user.first_name} {user.last_name or ''}\n"
        f"🆔 User ID: <code>{uid}</code>\n"
        f"📅 Date: {date_str}\n"
        f"🕐 Time: {time_str}\n"
        f"📌 Status: {status}\n\n"
        f"Welcome! Choose an option below:"
    )
    await update.message.reply_text(text, reply_markup=make_main_menu(), parse_mode=ParseMode.HTML)
    return MAIN_MENU


# ─────────────────────────────────────────────────────────────────
# HELP
# ─────────────────────────────────────────────────────────────────
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "❓ <b>Help — WhatsApp Group Manager</b>\n\n"
        "<b>Commands:</b>\n"
        "/start — Show main menu\n"
        "/help — Show this help\n"
        "/admin — Admin panel (admin only)\n\n"
        "<b>Features:</b>\n"
        "🔗 <b>Connect</b> — Link your WhatsApp via QR or phone number\n"
        "❌ <b>Disconnect</b> — Log out your WhatsApp session\n"
        "📋 <b>Connected Accounts</b> — View all active sessions\n"
        "➕ <b>Create Group</b> — Create a new WhatsApp group\n"
        "🔗 <b>Join Groups</b> — Join groups via invite links\n"
        "📞 <b>CTC Checker</b> — Check if numbers are on WhatsApp\n"
        "🔑 <b>Get Link</b> — Get invite link for your group\n"
        "🚪 <b>Leave Groups</b> — Leave one or more groups\n"
        "🗑 <b>Remove Members</b> — Remove members from a group\n"
        "👑 <b>Make/Remove Admin</b> — Promote or demote group admins\n"
        "✅ <b>Approval Setting</b> — Enable or disable join approval\n"
        "📋 <b>Get Pending List</b> — View and manage join requests\n"
        "➕ <b>Add Members</b> — Add members to a group\n\n"
        "<b>Support:</b> Contact the bot admin for help."
    )
    if update.message:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=make_back_menu())
    elif update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=make_back_menu())


# ─────────────────────────────────────────────────────────────────
# CONNECTED ACCOUNTS
# ─────────────────────────────────────────────────────────────────
async def show_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show all active WhatsApp sessions from the bridge."""
    res = bridge.list_sessions()
    sessions = res.get("sessions", [])
    if not sessions:
        text = "📋 <b>Connected Accounts</b>\n\nNo active WhatsApp sessions found."
    else:
        lines = ["📋 <b>Connected Accounts</b>\n"]
        for i, s in enumerate(sessions, 1):
            sid    = s.get("sessionId", "unknown")
            status = s.get("status", "unknown")
            phone  = s.get("phone", "–")
            lines.append(f"{i}. 📱 <code>{sid}</code>\n   Phone: {phone}\n   Status: {status}\n")
        text = "\n".join(lines)

    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=make_back_menu())
    else:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=make_back_menu())


# ─────────────────────────────────────────────────────────────────
# MAIN MENU CALLBACK (entry point for button presses)
# ─────────────────────────────────────────────────────────────────
async def main_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    data = q.data
    uid  = q.from_user.id
    await q.answer()

    if data == "back_menu":
        await q.edit_message_text(
            "🤖 <b>WhatsApp Group Manager</b>\n\nChoose an option below:",
            reply_markup=make_main_menu(), parse_mode=ParseMode.HTML
        )
        return MAIN_MENU

    if data == "help":
        await help_cmd(update, context)
        return MAIN_MENU

    if data == "accounts":
        await show_accounts(update, context)
        return MAIN_MENU

    if data == "admin_panel":
        if uid not in ADMIN_IDS:
            await q.edit_message_text("⛔ Admin only.", reply_markup=make_back_menu())
            return MAIN_MENU
        await q.edit_message_text("👑 <b>Admin Panel</b>\n\nChoose an action:", parse_mode=ParseMode.HTML, reply_markup=make_admin_menu())
        return ADMIN_MENU

    # All other buttons require access
    if not user_has_access(uid):
        await q.edit_message_text(
            "⛔ <b>Access Denied</b>\n\n"
            "This bot is for premium users only.\n"
            "Contact the admin to get access.",
            parse_mode=ParseMode.HTML,
            reply_markup=make_back_menu()
        )
        return MAIN_MENU

    if data == "connect":
        await q.edit_message_text(
            "🔗 <b>Connect WhatsApp</b>\n\nHow would you like to connect?",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📷 QR Code",      callback_data="connect_qr"),
                 InlineKeyboardButton("📱 Phone Number", callback_data="connect_phone")],
                [InlineKeyboardButton("🔙 Back",          callback_data="back_menu")]
            ])
        )
        return CONNECT_METHOD

    if data == "disconnect":
        await q.edit_message_text(
            "❌ <b>Disconnect WhatsApp</b>\n\nEnter the session ID to disconnect:",
            parse_mode=ParseMode.HTML,
            reply_markup=make_cancel_kb()
        )
        return DISC_SESSION

    if data == "create_group":
        await q.edit_message_text(
            "➕ <b>Create WhatsApp Group</b>\n\n"
            "Step 1/7: Enter the <b>session ID</b> (your connected account ID):",
            parse_mode=ParseMode.HTML, reply_markup=make_cancel_kb()
        )
        return CG_NAME  # First we get session, reusing CG_NAME slot mapped below

    if data == "join":
        await q.edit_message_text(
            "🔗 <b>Join Groups</b>\n\nEnter the session ID of the account to use:",
            parse_mode=ParseMode.HTML, reply_markup=make_cancel_kb()
        )
        return JOIN_SESSION

    if data == "ctc":
        await q.edit_message_text(
            "📞 <b>CTC Checker</b>\n\nEnter the session ID of the account to use:",
            parse_mode=ParseMode.HTML, reply_markup=make_cancel_kb()
        )
        return CTC_SESSION

    if data == "getlink":
        await q.edit_message_text(
            "🔑 <b>Get Group Link</b>\n\nEnter the session ID of the account to use:",
            parse_mode=ParseMode.HTML, reply_markup=make_cancel_kb()
        )
        return GETLINK_SESSION

    if data == "leave":
        await q.edit_message_text(
            "🚪 <b>Leave Groups</b>\n\nEnter the session ID of the account to use:",
            parse_mode=ParseMode.HTML, reply_markup=make_cancel_kb()
        )
        return LEAVE_SESSION

    if data == "remove_members":
        await q.edit_message_text(
            "🗑 <b>Remove Members</b>\n\nEnter the session ID of the account to use:",
            parse_mode=ParseMode.HTML, reply_markup=make_cancel_kb()
        )
        return RM_SESSION

    if data == "admin_op":
        await q.edit_message_text(
            "👑 <b>Make / Remove Admin</b>\n\nEnter the session ID of the account to use:",
            parse_mode=ParseMode.HTML, reply_markup=make_cancel_kb()
        )
        return ADMIN_OP_SESSION

    if data == "approval":
        await q.edit_message_text(
            "✅ <b>Approval Setting</b>\n\nEnter the session ID of the account to use:",
            parse_mode=ParseMode.HTML, reply_markup=make_cancel_kb()
        )
        return APPROVAL_SESSION

    if data == "pending":
        await q.edit_message_text(
            "📋 <b>Get Pending List</b>\n\nEnter the session ID of the account to use:",
            parse_mode=ParseMode.HTML, reply_markup=make_cancel_kb()
        )
        return PENDING_SESSION

    if data == "add_members":
        await q.edit_message_text(
            "➕ <b>Add Members</b>\n\nEnter the session ID of the account to use:",
            parse_mode=ParseMode.HTML, reply_markup=make_cancel_kb()
        )
        return ADDM_SESSION

    return MAIN_MENU


# ─────────────────────────────────────────────────────────────────
# CANCEL HANDLER
# ─────────────────────────────────────────────────────────────────
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            "❌ Cancelled. Returning to main menu.",
            reply_markup=make_main_menu()
        )
    else:
        await update.message.reply_text("❌ Cancelled. Returning to main menu.", reply_markup=make_main_menu())
    return MAIN_MENU


# ─────────────────────────────────────────────────────────────────
# FEATURE 1 — CONNECT WHATSAPP
# ─────────────────────────────────────────────────────────────────
async def connect_method_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    data = q.data
    await q.answer()

    if data == "back_menu":
        await q.edit_message_text("🤖 <b>WhatsApp Group Manager</b>\n\nChoose an option below:",
                                   reply_markup=make_main_menu(), parse_mode=ParseMode.HTML)
        return MAIN_MENU

    if data == "connect_qr":
        context.user_data["connect_method"] = "qr"
        await q.edit_message_text(
            "📷 <b>QR Code Connection</b>\n\nEnter a unique session ID for this connection\n"
            "(e.g. <code>myphone1</code>):",
            parse_mode=ParseMode.HTML, reply_markup=make_cancel_kb()
        )
        return CONNECT_SESSION_ID

    if data == "connect_phone":
        context.user_data["connect_method"] = "phone"
        await q.edit_message_text(
            "📱 <b>Phone Number Connection</b>\n\nEnter a unique session ID for this connection:",
            parse_mode=ParseMode.HTML, reply_markup=make_cancel_kb()
        )
        return CONNECT_SESSION_ID

    if data == "cancel":
        return await cancel(update, context)

    return CONNECT_METHOD


async def connect_session_id_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sid = update.message.text.strip()
    context.user_data["session_id"] = sid
    method = context.user_data.get("connect_method", "qr")

    if method == "qr":
        await update.message.reply_text("⏳ Requesting QR code from bridge…")
        res = bridge.connect_qr(sid)
        if res.get("success"):
            qr_data = res.get("qr", "")
            await update.message.reply_text(
                f"📷 <b>Scan this QR Code</b> in WhatsApp:\n\n"
                f"<pre>{qr_data[:400]}</pre>\n\n"
                f"Session ID: <code>{sid}</code>",
                parse_mode=ParseMode.HTML, reply_markup=make_back_menu()
            )
        else:
            await update.message.reply_text(
                f"❌ Failed to get QR: {res.get('error', 'Unknown error')}",
                reply_markup=make_back_menu()
            )
        context.user_data.clear()
        return MAIN_MENU

    else:  # phone
        await update.message.reply_text(
            "📱 Enter the phone number to pair (with country code, no + or spaces):\n"
            "Example: <code>919876543210</code>",
            parse_mode=ParseMode.HTML, reply_markup=make_cancel_kb()
        )
        return CONNECT_PHONE_INPUT


async def connect_phone_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip().replace("+", "").replace(" ", "")
    sid   = context.user_data.get("session_id", "default")
    await update.message.reply_text("⏳ Requesting pairing code…")
    res = bridge.connect_phone(sid, phone)
    if res.get("success"):
        code = res.get("code", "N/A")
        await update.message.reply_text(
            f"🔢 <b>Pairing Code:</b> <code>{code}</code>\n\n"
            f"Enter this code in WhatsApp → Linked Devices → Link a Device.\n"
            f"Session ID: <code>{sid}</code>",
            parse_mode=ParseMode.HTML, reply_markup=make_back_menu()
        )
    else:
        await update.message.reply_text(
            f"❌ Failed: {res.get('error', 'Unknown error')}",
            reply_markup=make_back_menu()
        )
    context.user_data.clear()
    return MAIN_MENU


# ─────────────────────────────────────────────────────────────────
# FEATURE 2 — DISCONNECT WHATSAPP
# ─────────────────────────────────────────────────────────────────
async def disc_session_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sid = update.message.text.strip()
    context.user_data["disc_session"] = sid
    await update.message.reply_text(
        f"❌ Are you sure you want to disconnect session <code>{sid}</code>?",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Yes, Disconnect", callback_data="disc_yes"),
             InlineKeyboardButton("❌ Cancel",           callback_data="cancel")]
        ])
    )
    return DISC_CONFIRM


async def disc_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "cancel":
        return await cancel(update, context)

    sid = context.user_data.get("disc_session", "")
    res = bridge.disconnect(sid)
    if res.get("success"):
        await q.edit_message_text(f"✅ Session <code>{sid}</code> disconnected successfully.", parse_mode=ParseMode.HTML, reply_markup=make_back_menu())
    else:
        await q.edit_message_text(f"❌ Failed: {res.get('error', 'Unknown error')}", reply_markup=make_back_menu())
    context.user_data.clear()
    return MAIN_MENU


# ─────────────────────────────────────────────────────────────────
# FEATURE 5 — CREATE GROUP (7-step flow)
# ─────────────────────────────────────────────────────────────────
# State mapping for create group (reusing CG_* constants):
# CG_NAME       → actually captures SESSION ID first, then NAME
# We store phase in user_data["cg_phase"]

async def cg_step1_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 1: receive session ID, then ask for group name."""
    sid = update.message.text.strip()
    context.user_data["cg_session"] = sid
    await update.message.reply_text(
        "✏️ <b>Step 2/7:</b> Enter the <b>group name</b>:",
        parse_mode=ParseMode.HTML, reply_markup=make_cancel_kb()
    )
    context.user_data["cg_phase"] = "name"
    return CG_NAME


async def cg_step2_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 2: receive group name, ask description."""
    context.user_data["cg_name"] = update.message.text.strip()
    await update.message.reply_text(
        "📝 <b>Step 3/7:</b> Enter a <b>group description</b> (or type 'skip' to skip):",
        parse_mode=ParseMode.HTML, reply_markup=make_cancel_kb()
    )
    return CG_DESCRIPTION


async def cg_step3_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    context.user_data["cg_description"] = "" if txt.lower() == "skip" else txt
    await update.message.reply_text(
        "🖼 <b>Step 4/7:</b> Send a <b>profile photo</b> for the group, or type 'skip':",
        parse_mode=ParseMode.HTML, reply_markup=make_cancel_kb()
    )
    return CG_PROFILE


async def cg_step4_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.photo:
        context.user_data["cg_profile"] = update.message.photo[-1].file_id
    else:
        context.user_data["cg_profile"] = None
    await update.message.reply_text(
        "👥 <b>Step 5/7:</b> Enter <b>member phone numbers</b> (one per line, with country code):\n\n"
        "Example:\n<code>919876543210\n447911123456</code>",
        parse_mode=ParseMode.HTML, reply_markup=make_cancel_kb()
    )
    return CG_MEMBERS


async def cg_step5_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines   = [l.strip().replace("+","").replace(" ","") for l in update.message.text.strip().splitlines() if l.strip()]
    context.user_data["cg_members"] = lines
    await update.message.reply_text(
        "👋 <b>Step 6/7:</b> Enter a <b>welcome message</b> to send to the group after creation\n"
        "(or type 'skip' to skip):",
        parse_mode=ParseMode.HTML, reply_markup=make_cancel_kb()
    )
    return CG_WELCOME


async def cg_step6_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    context.user_data["cg_welcome"] = "" if txt.lower() == "skip" else txt
    await update.message.reply_text(
        "🔒 <b>Step 7/7:</b> Enable <b>join approval</b> for the group?",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Yes (Approval ON)",  callback_data="cg_approval_on"),
             InlineKeyboardButton("❌ No (Anyone can join)", callback_data="cg_approval_off")]
        ])
    )
    return CG_APPROVAL


async def cg_step7_approval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    data = q.data
    await q.answer()

    context.user_data["cg_approval"] = "on" if data == "cg_approval_on" else "off"

    name     = context.user_data.get("cg_name", "")
    members  = context.user_data.get("cg_members", [])
    approval = context.user_data.get("cg_approval", "off")
    desc     = context.user_data.get("cg_description", "")

    summary = (
        f"📋 <b>Confirm Group Creation</b>\n\n"
        f"📛 Name: <b>{name}</b>\n"
        f"📝 Description: {desc or '(none)'}\n"
        f"👥 Members: {len(members)}\n"
        f"🔒 Approval: {approval.upper()}\n\n"
        f"Proceed?"
    )
    await q.edit_message_text(
        summary, parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Create Group", callback_data="cg_confirm"),
             InlineKeyboardButton("❌ Cancel",        callback_data="cancel")]
        ])
    )
    return CG_CONFIRM


async def cg_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "cancel":
        return await cancel(update, context)

    sid      = context.user_data.get("cg_session", "")
    name     = context.user_data.get("cg_name", "")
    members  = context.user_data.get("cg_members", [])
    approval = context.user_data.get("cg_approval", "off")
    welcome  = context.user_data.get("cg_welcome", "")

    await q.edit_message_text("⏳ Creating group…")
    res = bridge.create_group(sid, name, members)

    if res.get("success"):
        group_id = res.get("groupId", "")
        # Set approval
        if approval == "on":
            bridge.set_approval(sid, group_id, "on")
        # Send welcome
        if welcome and group_id:
            bridge.send_message(sid, group_id, welcome)

        await q.edit_message_text(
            f"✅ <b>Group Created!</b>\n\n"
            f"📛 Name: {name}\n"
            f"🆔 Group ID: <code>{group_id}</code>\n"
            f"👥 Members: {len(members)}\n"
            f"🔒 Approval: {approval.upper()}",
            parse_mode=ParseMode.HTML, reply_markup=make_back_menu()
        )
    else:
        await q.edit_message_text(
            f"❌ Failed to create group: {res.get('error', 'Unknown error')}",
            reply_markup=make_back_menu()
        )
    context.user_data.clear()
    return MAIN_MENU


# ─────────────────────────────────────────────────────────────────
# FEATURE 6 — JOIN GROUPS
# ─────────────────────────────────────────────────────────────────
async def join_session_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["join_session"] = update.message.text.strip()
    await update.message.reply_text(
        "🔗 <b>Join Groups</b>\n\n"
        "Paste the WhatsApp group invite links, one per line:\n\n"
        "Example:\n<code>https://chat.whatsapp.com/abc123\nhttps://chat.whatsapp.com/xyz789</code>",
        parse_mode=ParseMode.HTML, reply_markup=make_cancel_kb()
    )
    return JOIN_LINKS


async def join_links_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sid   = context.user_data.get("join_session", "")
    links = [l.strip() for l in update.message.text.strip().splitlines() if l.strip()]

    results = []
    for link in links:
        res = bridge.join_group(sid, link)
        if res.get("success"):
            results.append(f"✅ Joined: {link}")
        else:
            results.append(f"❌ Failed ({res.get('error','err')}): {link}")

    text = "🔗 <b>Join Results</b>\n\n" + "\n".join(results)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=make_back_menu())
    context.user_data.clear()
    return MAIN_MENU


# ─────────────────────────────────────────────────────────────────
# FEATURE 7 — CTC CHECKER
# ─────────────────────────────────────────────────────────────────
async def ctc_session_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["ctc_session"] = update.message.text.strip()
    await update.message.reply_text(
        "📞 <b>CTC Checker</b>\n\nSelect mode:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 Pending Members (group)",  callback_data="ctc_pending"),
             InlineKeyboardButton("✅ All Members (group)",       callback_data="ctc_members")],
            [InlineKeyboardButton("📝 Enter numbers manually",   callback_data="ctc_manual")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]
        ])
    )
    return CTC_MODE


async def ctc_mode_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    data = q.data
    await q.answer()

    if data == "cancel":
        return await cancel(update, context)

    context.user_data["ctc_mode"] = data

    if data == "ctc_manual":
        await q.edit_message_text(
            "📝 Enter phone numbers to check, one per line (with country code):",
            reply_markup=make_cancel_kb()
        )
        return CTC_NUMBERS

    # Group modes — need group ID
    await q.edit_message_text(
        "Enter the <b>Group ID</b> to check:",
        parse_mode=ParseMode.HTML, reply_markup=make_cancel_kb()
    )
    return CTC_GROUP


async def ctc_group_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    group_id = update.message.text.strip()
    sid      = context.user_data.get("ctc_session", "")
    mode     = context.user_data.get("ctc_mode", "ctc_members")

    await update.message.reply_text("⏳ Fetching members…")

    if mode == "ctc_pending":
        res     = bridge.get_pending(sid, group_id)
        numbers = res.get("pending", [])
    else:
        res     = bridge.get_members(sid, group_id)
        numbers = [m.get("id", "").replace("@s.whatsapp.net","") for m in res.get("members", [])]

    if not numbers:
        await update.message.reply_text("ℹ️ No numbers found.", reply_markup=make_back_menu())
        context.user_data.clear()
        return MAIN_MENU

    await update.message.reply_text(f"⏳ Checking {len(numbers)} numbers on WhatsApp…")
    res2 = bridge.check_numbers_bulk(sid, numbers)

    on_wa  = res2.get("onWhatsApp", [])
    not_wa = res2.get("notOnWhatsApp", [])

    text = (
        f"📞 <b>CTC Results</b>\n\n"
        f"✅ On WhatsApp: {len(on_wa)}\n"
        f"❌ Not on WhatsApp: {len(not_wa)}\n\n"
        f"<b>On WhatsApp:</b>\n" + "\n".join(on_wa[:30]) +
        ("\n..." if len(on_wa) > 30 else "")
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=make_back_menu())
    context.user_data.clear()
    return MAIN_MENU


async def ctc_numbers_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sid     = context.user_data.get("ctc_session", "")
    numbers = [l.strip().replace("+","").replace(" ","")
               for l in update.message.text.strip().splitlines() if l.strip()]

    await update.message.reply_text(f"⏳ Checking {len(numbers)} numbers…")
    res = bridge.check_numbers_bulk(sid, numbers)

    on_wa  = res.get("onWhatsApp", [])
    not_wa = res.get("notOnWhatsApp", [])

    text = (
        f"📞 <b>CTC Results</b>\n\n"
        f"✅ On WhatsApp ({len(on_wa)}):\n" + "\n".join(on_wa[:30]) + "\n\n"
        f"❌ Not on WhatsApp ({len(not_wa)}):\n" + "\n".join(not_wa[:20])
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=make_back_menu())
    context.user_data.clear()
    return MAIN_MENU


# ─────────────────────────────────────────────────────────────────
# FEATURE 8 — GET LINK
# ─────────────────────────────────────────────────────────────────
async def getlink_session_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["gl_session"] = update.message.text.strip()
    await update.message.reply_text(
        "🔑 Enter the <b>Group ID</b> to get the invite link:",
        parse_mode=ParseMode.HTML, reply_markup=make_cancel_kb()
    )
    return GETLINK_GROUP


async def getlink_group_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sid      = context.user_data.get("gl_session", "")
    group_id = update.message.text.strip()

    res  = bridge.get_group_link(sid, group_id)
    link = res.get("link", "")

    if res.get("success") and link:
        await update.message.reply_text(
            f"🔑 <b>Invite Link</b>\n\n{link}",
            parse_mode=ParseMode.HTML, reply_markup=make_back_menu()
        )
    else:
        await update.message.reply_text(
            f"❌ Failed: {res.get('error', 'Unknown error')}",
            reply_markup=make_back_menu()
        )
    context.user_data.clear()
    return MAIN_MENU


# ─────────────────────────────────────────────────────────────────
# FEATURE 9 — LEAVE GROUPS
# ─────────────────────────────────────────────────────────────────
async def leave_session_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["leave_session"] = update.message.text.strip()
    await update.message.reply_text(
        "🚪 Enter the <b>Group IDs</b> to leave, one per line:",
        parse_mode=ParseMode.HTML, reply_markup=make_cancel_kb()
    )
    return LEAVE_GROUP


async def leave_group_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    gids = [l.strip() for l in update.message.text.strip().splitlines() if l.strip()]
    context.user_data["leave_groups"] = gids

    await update.message.reply_text(
        f"🚪 You are about to leave <b>{len(gids)}</b> group(s). Confirm?",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Leave All", callback_data="leave_yes"),
             InlineKeyboardButton("❌ Cancel",     callback_data="cancel")]
        ])
    )
    return LEAVE_CONFIRM


async def leave_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "cancel":
        return await cancel(update, context)

    sid  = context.user_data.get("leave_session", "")
    gids = context.user_data.get("leave_groups", [])

    results = []
    for gid in gids:
        res = bridge.leave_group(sid, gid)
        if res.get("success"):
            results.append(f"✅ Left: <code>{gid}</code>")
        else:
            results.append(f"❌ Failed ({res.get('error','')}): <code>{gid}</code>")

    text = "🚪 <b>Leave Results</b>\n\n" + "\n".join(results)
    await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=make_back_menu())
    context.user_data.clear()
    return MAIN_MENU


# ─────────────────────────────────────────────────────────────────
# FEATURE 10 — REMOVE MEMBERS
# ─────────────────────────────────────────────────────────────────
async def rm_session_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["rm_session"] = update.message.text.strip()
    await update.message.reply_text(
        "🗑 Enter the <b>Group ID</b> to remove members from:",
        parse_mode=ParseMode.HTML, reply_markup=make_cancel_kb()
    )
    return RM_GROUP


async def rm_group_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["rm_group"] = update.message.text.strip()
    await update.message.reply_text(
        "📞 Enter the <b>phone numbers</b> to remove, one per line (with country code):",
        parse_mode=ParseMode.HTML, reply_markup=make_cancel_kb()
    )
    return RM_NUMBERS


async def rm_numbers_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sid      = context.user_data.get("rm_session", "")
    group_id = context.user_data.get("rm_group", "")
    numbers  = [l.strip().replace("+","").replace(" ","")
                for l in update.message.text.strip().splitlines() if l.strip()]

    await update.message.reply_text(f"⏳ Removing {len(numbers)} members…")
    res = bridge.remove_members(sid, group_id, numbers)

    if res.get("success"):
        removed = res.get("removed", numbers)
        await update.message.reply_text(
            f"✅ Removed {len(removed)} member(s) from the group.",
            reply_markup=make_back_menu()
        )
    else:
        await update.message.reply_text(
            f"❌ Failed: {res.get('error', 'Unknown error')}",
            reply_markup=make_back_menu()
        )
    context.user_data.clear()
    return MAIN_MENU


# ─────────────────────────────────────────────────────────────────
# FEATURE 11 — MAKE / REMOVE ADMIN
# ─────────────────────────────────────────────────────────────────
async def adminop_session_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["aop_session"] = update.message.text.strip()
    await update.message.reply_text(
        "👑 Enter the <b>Group ID</b>:",
        parse_mode=ParseMode.HTML, reply_markup=make_cancel_kb()
    )
    return ADMIN_OP_GROUP


async def adminop_group_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["aop_group"] = update.message.text.strip()
    await update.message.reply_text(
        "📞 Enter the <b>phone numbers</b> to promote/demote, one per line:",
        parse_mode=ParseMode.HTML, reply_markup=make_cancel_kb()
    )
    return ADMIN_OP_MEMBERS


async def adminop_members_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    numbers = [l.strip().replace("+","").replace(" ","")
               for l in update.message.text.strip().splitlines() if l.strip()]
    context.user_data["aop_members"] = numbers
    await update.message.reply_text(
        "👑 Choose action:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬆️ Make Admin",    callback_data="aop_promote"),
             InlineKeyboardButton("⬇️ Remove Admin",  callback_data="aop_demote")],
            [InlineKeyboardButton("❌ Cancel",         callback_data="cancel")]
        ])
    )
    return ADMIN_OP_ACTION


async def adminop_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    data = q.data
    await q.answer()

    if data == "cancel":
        return await cancel(update, context)

    sid     = context.user_data.get("aop_session", "")
    gid     = context.user_data.get("aop_group", "")
    members = context.user_data.get("aop_members", [])

    if data == "aop_promote":
        res    = bridge.make_admin(sid, gid, members)
        action = "promoted to admin"
    else:
        res    = bridge.remove_admin(sid, gid, members)
        action = "demoted from admin"

    if res.get("success"):
        await q.edit_message_text(
            f"✅ {len(members)} member(s) {action} successfully.",
            reply_markup=make_back_menu()
        )
    else:
        await q.edit_message_text(
            f"❌ Failed: {res.get('error', 'Unknown error')}",
            reply_markup=make_back_menu()
        )
    context.user_data.clear()
    return MAIN_MENU


# ─────────────────────────────────────────────────────────────────
# FEATURE 12 — APPROVAL SETTING
# ─────────────────────────────────────────────────────────────────
async def approval_session_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["appr_session"] = update.message.text.strip()
    await update.message.reply_text(
        "✅ Enter the <b>Group ID</b>:",
        parse_mode=ParseMode.HTML, reply_markup=make_cancel_kb()
    )
    return APPROVAL_GROUP


async def approval_group_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["appr_group"] = update.message.text.strip()
    await update.message.reply_text(
        "✅ <b>Approval Setting</b>\n\nChoose the approval mode for this group:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Enable Approval (ON)",  callback_data="appr_on"),
             InlineKeyboardButton("❌ Disable Approval (OFF)", callback_data="appr_off")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]
        ])
    )
    return APPROVAL_MODE


async def approval_mode_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    data = q.data
    await q.answer()

    if data == "cancel":
        return await cancel(update, context)

    sid  = context.user_data.get("appr_session", "")
    gid  = context.user_data.get("appr_group", "")
    mode = "on" if data == "appr_on" else "off"

    res = bridge.set_approval(sid, gid, mode)
    if res.get("success"):
        await q.edit_message_text(
            f"✅ Approval set to <b>{mode.upper()}</b> for group <code>{gid}</code>.",
            parse_mode=ParseMode.HTML, reply_markup=make_back_menu()
        )
    else:
        await q.edit_message_text(
            f"❌ Failed: {res.get('error', 'Unknown error')}",
            reply_markup=make_back_menu()
        )
    context.user_data.clear()
    return MAIN_MENU


# ─────────────────────────────────────────────────────────────────
# FEATURE 13 — GET PENDING LIST
# ─────────────────────────────────────────────────────────────────
async def pending_session_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["pend_session"] = update.message.text.strip()
    await update.message.reply_text(
        "📋 Enter the <b>Group ID</b> to get pending join requests:",
        parse_mode=ParseMode.HTML, reply_markup=make_cancel_kb()
    )
    return PENDING_GROUP


async def pending_group_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sid = context.user_data.get("pend_session", "")
    gid = update.message.text.strip()
    context.user_data["pend_group"] = gid

    await update.message.reply_text("⏳ Fetching pending list…")
    res     = bridge.get_pending(sid, gid)
    pending = res.get("pending", [])

    if not pending:
        await update.message.reply_text(
            "📋 No pending join requests found.",
            reply_markup=make_back_menu()
        )
        context.user_data.clear()
        return MAIN_MENU

    lines = [f"{i+1}. <code>{p}</code>" for i, p in enumerate(pending[:30])]
    text  = f"📋 <b>Pending Requests</b> ({len(pending)} total)\n\n" + "\n".join(lines)
    context.user_data["pend_numbers"] = pending

    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Approve All", callback_data="pend_approve_all"),
             InlineKeyboardButton("❌ Reject All",  callback_data="pend_reject_all")],
            [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_menu")]
        ])
    )
    return PENDING_ACTION


async def pending_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    data = q.data
    await q.answer()

    if data == "back_menu":
        return await cancel(update, context)

    sid     = context.user_data.get("pend_session", "")
    gid     = context.user_data.get("pend_group", "")
    numbers = context.user_data.get("pend_numbers", [])

    await q.edit_message_text(f"⏳ Processing {len(numbers)} requests…")

    if data == "pend_approve_all":
        ok = fail = 0
        for num in numbers:
            r = bridge.approve_pending(sid, gid, num)
            if r.get("success"):
                ok += 1
            else:
                fail += 1
        await q.edit_message_text(
            f"✅ Approved: {ok}   ❌ Failed: {fail}",
            reply_markup=make_back_menu()
        )
    elif data == "pend_reject_all":
        ok = fail = 0
        for num in numbers:
            r = bridge.reject_pending(sid, gid, num)
            if r.get("success"):
                ok += 1
            else:
                fail += 1
        await q.edit_message_text(
            f"✅ Rejected: {ok}   ❌ Failed: {fail}",
            reply_markup=make_back_menu()
        )
    context.user_data.clear()
    return MAIN_MENU


# ─────────────────────────────────────────────────────────────────
# FEATURE 14 — ADD MEMBERS
# ─────────────────────────────────────────────────────────────────
async def addm_session_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["addm_session"] = update.message.text.strip()
    await update.message.reply_text(
        "➕ Enter the <b>Group ID</b> to add members to:",
        parse_mode=ParseMode.HTML, reply_markup=make_cancel_kb()
    )
    return ADDM_GROUP


async def addm_group_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["addm_group"] = update.message.text.strip()
    await update.message.reply_text(
        "📞 Enter the <b>phone numbers</b> to add, one per line (with country code):",
        parse_mode=ParseMode.HTML, reply_markup=make_cancel_kb()
    )
    return ADDM_NUMBERS


async def addm_numbers_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sid      = context.user_data.get("addm_session", "")
    group_id = context.user_data.get("addm_group", "")
    numbers  = [l.strip().replace("+","").replace(" ","")
                for l in update.message.text.strip().splitlines() if l.strip()]

    await update.message.reply_text(f"⏳ Adding {len(numbers)} members…")
    res = bridge.add_members(sid, group_id, numbers)

    if res.get("success"):
        added = res.get("added", numbers)
        failed = res.get("failed", [])
        await update.message.reply_text(
            f"✅ Added: {len(added)} member(s).\n"
            f"❌ Failed: {len(failed)}",
            reply_markup=make_back_menu()
        )
    else:
        await update.message.reply_text(
            f"❌ Failed: {res.get('error', 'Unknown error')}",
            reply_markup=make_back_menu()
        )
    context.user_data.clear()
    return MAIN_MENU


# ─────────────────────────────────────────────────────────────────
# FEATURE 15 — FULL ADMIN PANEL
# ─────────────────────────────────────────────────────────────────
async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point via /admin command."""
    uid = update.effective_user.id
    if uid not in ADMIN_IDS:
        await update.message.reply_text("⛔ Admin only command.")
        return ConversationHandler.END
    await update.message.reply_text(
        "👑 <b>Admin Panel</b>\n\nChoose an action:",
        parse_mode=ParseMode.HTML,
        reply_markup=make_admin_menu()
    )
    return ADMIN_MENU


async def admin_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    data = q.data
    uid  = q.from_user.id
    await q.answer()

    if uid not in ADMIN_IDS:
        await q.edit_message_text("⛔ Admin only.")
        return ConversationHandler.END

    if data == "back_menu":
        await q.edit_message_text(
            "🤖 <b>WhatsApp Group Manager</b>\n\nChoose an option below:",
            reply_markup=make_main_menu(), parse_mode=ParseMode.HTML
        )
        return MAIN_MENU

    # ── ADD PREMIUM ──────────────────────────────────────────────
    if data == "adm_add_premium":
        await q.edit_message_text(
            "➕ <b>Add Premium User</b>\n\n"
            "Send the user's Telegram ID, or forward a message from them:",
            parse_mode=ParseMode.HTML, reply_markup=make_cancel_kb()
        )
        return ADMIN_ADD_PREMIUM

    # ── REMOVE PREMIUM ───────────────────────────────────────────
    if data == "adm_rm_premium":
        all_p = get_all_premium()
        if not all_p:
            await q.edit_message_text(
                "📋 No premium users yet.",
                reply_markup=make_admin_menu()
            )
            return ADMIN_MENU

        rows = []
        for p in all_p:
            rows.append([InlineKeyboardButton(
                f"❌ {p['user_id']}",
                callback_data=f"rm_p_{p['user_id']}"
            )])
        rows.append([InlineKeyboardButton("🔙 Back", callback_data="adm_back")])
        await q.edit_message_text(
            "➖ <b>Remove Premium User</b>\n\nTap a user to remove:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows)
        )
        return ADMIN_REMOVE_PREM

    # ── GRANT TEMP ───────────────────────────────────────────────
    if data == "adm_temp":
        await q.edit_message_text(
            "⏱ <b>Grant Temporary Access</b>\n\nSend the user's Telegram ID:",
            parse_mode=ParseMode.HTML, reply_markup=make_cancel_kb()
        )
        return ADMIN_TEMP_UID

    # ── PREMIUM LIST ─────────────────────────────────────────────
    if data == "adm_list":
        all_p = get_all_premium()
        temp  = get_active_temp_users()

        if all_p:
            lines = []
            for i, p in enumerate(all_p, 1):
                added = p.get("added_at", "")[:10]
                lines.append(f"{i}. User ID: <code>{p['user_id']}</code> — Added: {added}")
            prem_text = "\n".join(lines)
        else:
            prem_text = "(none)"

        text = (
            f"📋 <b>Premium Users List</b>\n\n"
            f"{prem_text}\n\n"
            f"Total: <b>{len(all_p)}</b> premium users\n"
            f"Active Temp Access: <b>{len(temp)}</b> users"
        )
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=make_admin_menu())
        return ADMIN_MENU

    # ── SET FREE / PAID MODE ─────────────────────────────────────
    if data == "adm_free":
        set_bot_mode("free")
        await q.edit_message_text(
            "✅ Bot mode set to <b>FREE</b> — all users can access the bot.",
            parse_mode=ParseMode.HTML, reply_markup=make_admin_menu()
        )
        return ADMIN_MENU

    if data == "adm_paid":
        set_bot_mode("paid")
        await q.edit_message_text(
            "✅ Bot mode set to <b>PAID</b> — only premium users can access the bot.",
            parse_mode=ParseMode.HTML, reply_markup=make_admin_menu()
        )
        return ADMIN_MENU

    # ── BOT STATS ────────────────────────────────────────────────
    if data == "adm_stats":
        now      = datetime.now(IST)
        mode     = get_bot_mode()
        all_p    = get_all_premium()
        temp     = get_active_temp_users()
        time_str = now.strftime("%I:%M %p IST")

        text = (
            f"📊 <b>Bot Statistics</b>\n\n"
            f"👥 Premium Users: <b>{len(all_p)}</b>\n"
            f"⏱ Active Temp Access: <b>{len(temp)}</b>\n"
            f"📌 Bot Mode: <b>{mode.upper()}</b>\n"
            f"🕐 Current Time: {time_str}\n"
            f"🤖 Bot Status: Running"
        )
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=make_admin_menu())
        return ADMIN_MENU

    if data == "adm_back":
        await q.edit_message_text(
            "👑 <b>Admin Panel</b>\n\nChoose an action:",
            parse_mode=ParseMode.HTML, reply_markup=make_admin_menu()
        )
        return ADMIN_MENU

    return ADMIN_MENU


# ── ADD PREMIUM: receive user ID ─────────────────────────────────
async def admin_add_premium_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid_admin = update.effective_user.id

    # Support forwarded messages
    if update.message.forward_from:
        target_id = update.message.forward_from.id
    else:
        txt = update.message.text.strip()
        if not txt.isdigit():
            await update.message.reply_text("❌ Invalid ID. Send a numeric Telegram ID:", reply_markup=make_cancel_kb())
            return ADMIN_ADD_PREMIUM
        target_id = int(txt)

    add_premium(target_id, uid_admin)
    await update.message.reply_text(
        f"✅ User <code>{target_id}</code> added as premium user.",
        parse_mode=ParseMode.HTML,
        reply_markup=make_admin_menu()
    )
    return ADMIN_MENU


# ── REMOVE PREMIUM: handle inline button ─────────────────────────
async def admin_remove_premium_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    data = q.data
    await q.answer()

    if data == "adm_back" or data == "cancel":
        await q.edit_message_text(
            "👑 <b>Admin Panel</b>\n\nChoose an action:",
            parse_mode=ParseMode.HTML, reply_markup=make_admin_menu()
        )
        return ADMIN_MENU

    if data.startswith("rm_p_"):
        target_id = int(data.split("_")[2])
        remove_premium(target_id)
        await q.edit_message_text(
            f"✅ User <code>{target_id}</code> removed from premium.",
            parse_mode=ParseMode.HTML,
            reply_markup=make_admin_menu()
        )
        return ADMIN_MENU

    return ADMIN_REMOVE_PREM


# ── TEMP ACCESS: receive user ID ─────────────────────────────────
async def admin_temp_uid_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if not txt.isdigit():
        await update.message.reply_text("❌ Invalid ID. Send a numeric Telegram ID:", reply_markup=make_cancel_kb())
        return ADMIN_TEMP_UID

    target_id = int(txt)
    context.user_data["temp_target"] = target_id

    await update.message.reply_text(
        f"⏱ Choose access duration for user <code>{target_id}</code>:",
        parse_mode=ParseMode.HTML,
        reply_markup=make_temp_duration_kb(target_id)
    )
    return ADMIN_TEMP_DURATION


# ── TEMP ACCESS: handle duration button ──────────────────────────
async def admin_temp_duration_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    data = q.data
    uid  = q.from_user.id
    await q.answer()

    if data == "cancel":
        await q.edit_message_text(
            "👑 <b>Admin Panel</b>\n\nChoose an action:",
            parse_mode=ParseMode.HTML, reply_markup=make_admin_menu()
        )
        return ADMIN_MENU

    # Format: td_<hours>_<user_id>
    parts = data.split("_")
    hours     = float(parts[1])
    target_id = int(parts[2])

    grant_temp_access(target_id, uid, hours)
    expiry_str = get_temp_expiry_str(target_id)

    await q.edit_message_text(
        f"✅ User <code>{target_id}</code> granted temporary access until\n"
        f"<b>{expiry_str}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=make_admin_menu()
    )
    return ADMIN_MENU


# ─────────────────────────────────────────────────────────────────
# CONVERSATION HANDLER BUILDERS
# ─────────────────────────────────────────────────────────────────
def build_master_conv() -> ConversationHandler:
    """
    One master ConversationHandler covering all features.
    Entry: /start or callback from main menu buttons.
    """
    cancel_handler = [
        CallbackQueryHandler(cancel, pattern="^cancel$"),
        CommandHandler("cancel", cancel),
    ]

    return ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("admin", admin_cmd),
        ],
        states={
            # ── MAIN MENU ───────────────────────────────────────
            MAIN_MENU: [
                CallbackQueryHandler(main_menu_callback),
            ],

            # ── CONNECT ─────────────────────────────────────────
            CONNECT_METHOD: [
                CallbackQueryHandler(connect_method_chosen),
            ],
            CONNECT_SESSION_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, connect_session_id_received),
                *cancel_handler,
            ],
            CONNECT_PHONE_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, connect_phone_received),
                *cancel_handler,
            ],

            # ── DISCONNECT ──────────────────────────────────────
            DISC_SESSION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, disc_session_received),
                *cancel_handler,
            ],
            DISC_CONFIRM: [
                CallbackQueryHandler(disc_confirm),
            ],

            # ── CREATE GROUP ────────────────────────────────────
            CG_NAME: [
                # First message after clicking "Create Group" = session ID
                MessageHandler(filters.TEXT & ~filters.COMMAND, cg_step1_session),
                *cancel_handler,
            ],
            CG_DESCRIPTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, cg_step2_name),
                *cancel_handler,
            ],
            CG_PROFILE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, cg_step3_description),
                *cancel_handler,
            ],
            CG_MEMBERS: [
                MessageHandler(filters.PHOTO | (filters.TEXT & ~filters.COMMAND), cg_step4_profile),
                *cancel_handler,
            ],
            CG_WELCOME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, cg_step5_members),
                *cancel_handler,
            ],
            CG_APPROVAL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, cg_step6_welcome),
                *cancel_handler,
            ],
            CG_CONFIRM: [
                CallbackQueryHandler(cg_step7_approval, pattern="^cg_approval_"),
                CallbackQueryHandler(cg_confirm, pattern="^(cg_confirm|cancel)$"),
            ],

            # ── JOIN ────────────────────────────────────────────
            JOIN_SESSION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, join_session_received),
                *cancel_handler,
            ],
            JOIN_LINKS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, join_links_received),
                *cancel_handler,
            ],

            # ── CTC ─────────────────────────────────────────────
            CTC_SESSION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ctc_session_received),
                *cancel_handler,
            ],
            CTC_MODE: [
                CallbackQueryHandler(ctc_mode_chosen),
            ],
            CTC_NUMBERS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ctc_numbers_received),
                *cancel_handler,
            ],
            CTC_GROUP: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ctc_group_received),
                *cancel_handler,
            ],

            # ── GET LINK ────────────────────────────────────────
            GETLINK_SESSION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, getlink_session_received),
                *cancel_handler,
            ],
            GETLINK_GROUP: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, getlink_group_received),
                *cancel_handler,
            ],

            # ── LEAVE ───────────────────────────────────────────
            LEAVE_SESSION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, leave_session_received),
                *cancel_handler,
            ],
            LEAVE_GROUP: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, leave_group_received),
                *cancel_handler,
            ],
            LEAVE_CONFIRM: [
                CallbackQueryHandler(leave_confirm),
            ],

            # ── REMOVE MEMBERS ──────────────────────────────────
            RM_SESSION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, rm_session_received),
                *cancel_handler,
            ],
            RM_GROUP: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, rm_group_received),
                *cancel_handler,
            ],
            RM_NUMBERS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, rm_numbers_received),
                *cancel_handler,
            ],

            # ── ADMIN OP ────────────────────────────────────────
            ADMIN_OP_SESSION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, adminop_session_received),
                *cancel_handler,
            ],
            ADMIN_OP_GROUP: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, adminop_group_received),
                *cancel_handler,
            ],
            ADMIN_OP_MEMBERS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, adminop_members_received),
                *cancel_handler,
            ],
            ADMIN_OP_ACTION: [
                CallbackQueryHandler(adminop_action),
            ],

            # ── APPROVAL ────────────────────────────────────────
            APPROVAL_SESSION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, approval_session_received),
                *cancel_handler,
            ],
            APPROVAL_GROUP: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, approval_group_received),
                *cancel_handler,
            ],
            APPROVAL_MODE: [
                CallbackQueryHandler(approval_mode_chosen),
            ],

            # ── PENDING ─────────────────────────────────────────
            PENDING_SESSION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, pending_session_received),
                *cancel_handler,
            ],
            PENDING_GROUP: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, pending_group_received),
                *cancel_handler,
            ],
            PENDING_ACTION: [
                CallbackQueryHandler(pending_action),
            ],

            # ── ADD MEMBERS ─────────────────────────────────────
            ADDM_SESSION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, addm_session_received),
                *cancel_handler,
            ],
            ADDM_GROUP: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, addm_group_received),
                *cancel_handler,
            ],
            ADDM_NUMBERS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, addm_numbers_received),
                *cancel_handler,
            ],

            # ── ADMIN PANEL ─────────────────────────────────────
            ADMIN_MENU: [
                CallbackQueryHandler(admin_remove_premium_callback, pattern="^(rm_p_|adm_back)"),
                CallbackQueryHandler(admin_temp_duration_callback,  pattern="^td_"),
                CallbackQueryHandler(admin_menu_callback),
            ],
            ADMIN_ADD_PREMIUM: [
                MessageHandler(filters.ALL, admin_add_premium_received),
                *cancel_handler,
            ],
            ADMIN_REMOVE_PREM: [
                CallbackQueryHandler(admin_remove_premium_callback),
            ],
            ADMIN_TEMP_UID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_temp_uid_received),
                *cancel_handler,
            ],
            ADMIN_TEMP_DURATION: [
                CallbackQueryHandler(admin_temp_duration_callback),
            ],
        },
        fallbacks=[
            CommandHandler("start", start),
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(cancel, pattern="^cancel$"),
        ],
        allow_reentry=True,
        per_message=False,
    )


# ─────────────────────────────────────────────────────────────────
# FLASK APP (Webhook + Health)
# ─────────────────────────────────────────────────────────────────
flask_app = Flask(__name__)
_ptb_app: Application = None  # set in main()


@flask_app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "time": datetime.now(IST).strftime("%I:%M %p IST")}), 200


@flask_app.route("/webhook", methods=["POST"])
def webhook():
    """Receive Telegram webhook updates."""
    data = request.get_json(force=True)
    if data and _ptb_app:
        update = Update.de_json(data, _ptb_app.bot)
        asyncio.run_coroutine_threadsafe(
            _ptb_app.process_update(update),
            _ptb_app.loop
        )
    return jsonify({"ok": True}), 200


# ─────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────
def main():
    global _ptb_app

    if not BOT_TOKEN:
        logger.error("BOT_TOKEN is not set! Add it to your .env file.")
        sys.exit(1)

    # Initialize MongoDB
    init_db()

    # Build PTB application
    _ptb_app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # Register help command separately (works outside ConversationHandler too)
    _ptb_app.add_handler(CommandHandler("help", help_cmd))

    # Register master conversation handler
    _ptb_app.add_handler(build_master_conv())

    # Set bot commands menu
    async def set_commands(app):
        await app.bot.set_my_commands([
            BotCommand("start", "Start the bot / Show main menu"),
            BotCommand("help",  "Show help information"),
            BotCommand("admin", "Admin panel (admin only)"),
            BotCommand("cancel","Cancel current operation"),
        ])

    _ptb_app.post_init = set_commands

    if RENDER_URL:
        # ── WEBHOOK MODE ────────────────────────────────────────
        webhook_url = f"{RENDER_URL.rstrip('/')}/webhook"
        logger.info("Starting in webhook mode: %s", webhook_url)

        # Set webhook via Telegram API
        import urllib.request
        try:
            req_url = f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook?url={webhook_url}"
            urllib.request.urlopen(req_url)
            logger.info("Webhook set to %s", webhook_url)
        except Exception as e:
            logger.warning("Could not set webhook automatically: %s", e)

        # Run PTB in background thread (event loop)
        loop = asyncio.new_event_loop()
        _ptb_app.loop = loop

        def run_ptb():
            asyncio.set_event_loop(loop)
            loop.run_until_complete(_ptb_app.initialize())
            loop.run_forever()

        t = threading.Thread(target=run_ptb, daemon=True)
        t.start()
        time.sleep(2)

        # Run Flask (blocking)
        flask_app.run(host="0.0.0.0", port=PORT)

    else:
        # ── POLLING MODE ────────────────────────────────────────
        logger.info("Starting in polling mode (no RENDER_URL set).")
        _ptb_app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
