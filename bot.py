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
   pip install python-telegram-bot==20.7 pymongo flask requests python-dotenv

2. Environment Variables (.env):
   BOT_TOKEN=your_telegram_bot_token
   ADMIN_IDS=123456789,987654321
   BRIDGE_URL=http://localhost:3000
   BRIDGE_SECRET=your_bridge_secret
   MONGO_URI=mongodb+srv://user:pass@cluster.mongodb.net/whatsapp_bot
   RENDER_URL=https://your-app.onrender.com   (optional, for webhook)
   PORT=8080                                   (optional, default 8080)

3. Node.js Baileys Bridge (bridge.js) must be running separately.

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
from flask import Flask, request as flask_request, jsonify
from pymongo import MongoClient
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
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
BOT_TOKEN  = os.getenv("BOT_TOKEN", "")
BRIDGE_URL = os.getenv("BRIDGE_URL", "http://localhost:3000")
BRIDGE_KEY = os.getenv("BRIDGE_SECRET", "")           # X-Secret header
MONGO_URI  = os.getenv("MONGO_URI", "mongodb+srv://user:pass@cluster.mongodb.net/whatsapp_bot")
RENDER_URL = os.getenv("RENDER_URL", "")
PORT       = int(os.getenv("PORT", 8080))

_raw_admins = os.getenv("ADMIN_IDS", "")
ADMIN_IDS   = set(int(x.strip()) for x in _raw_admins.split(",") if x.strip().isdigit())

IST = timezone(timedelta(hours=5, minutes=30))

# ─────────────────────────────────────────────────────────────────
# MONGODB SETUP
# ─────────────────────────────────────────────────────────────────
mongo_client    = MongoClient(MONGO_URI)
db              = mongo_client["whatsapp_bot"]
premium_col     = db["premium_users"]     # {user_id, added_by, added_at}
temp_access_col = db["temp_access"]       # {user_id, granted_by, granted_at, expires_at}
settings_col    = db["bot_settings"]      # {key, value}


def init_db():
    """Initialize default settings if they do not exist."""
    if not settings_col.find_one({"key": "bot_mode"}):
        settings_col.insert_one({"key": "bot_mode", "value": "paid"})
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
            "user_id":  user_id,
            "added_by": added_by,
            "added_at": datetime.now(IST).isoformat()
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
# BRIDGE API CLASS — ALL METHODS MATCHING bridge.js ROUTES
# ─────────────────────────────────────────────────────────────────
class BridgeAPI:
    """Wrapper for all Node.js Baileys Bridge API calls."""

    def __init__(self, base_url: str, api_key: str):
        self.base = base_url.rstrip("/")
        self.key  = api_key
        self.hdrs = {"X-Secret": api_key, "Content-Type": "application/json"}

    def _post(self, path: str, payload: dict) -> dict:
        try:
            r = requests.post(
                f"{self.base}{path}", json=payload,
                headers=self.hdrs, timeout=60
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.error("Bridge POST %s → %s", path, e)
            return {"success": False, "error": str(e)}

    def _get(self, path: str, params: dict = None) -> dict:
        try:
            r = requests.get(
                f"{self.base}{path}", params=params,
                headers=self.hdrs, timeout=30
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.error("Bridge GET %s → %s", path, e)
            return {"success": False, "error": str(e)}

    # 1. Connect via QR code
    def connect_qr(self, session_id: str) -> dict:
        return self._post("/connect/qr", {"accountId": session_id})

    # 2. Connect via phone number pairing
    def connect_phone(self, session_id: str, phone: str) -> dict:
        return self._post("/connect/phone", {"accountId": session_id, "phone": phone})

    # 3. Get pairing code (poll after connect_phone)
    def get_pairing_code(self, session_id: str) -> dict:
        return self._get(f"/connect/pairing-code/{session_id}")

    # 4. Disconnect session
    def disconnect(self, session_id: str) -> dict:
        return self._post("/disconnect", {"accountId": session_id})

    # 5. Get session status
    def get_status(self, session_id: str) -> dict:
        return self._get(f"/status/{session_id}")

    # 6. Get all joined groups
    def get_groups(self, session_id: str) -> dict:
        return self._get(f"/groups/{session_id}")

    # 7. Create a WhatsApp group
    def create_group(self, session_id: str, name: str, participants: list) -> dict:
        return self._post("/group/create", {
            "accountId": session_id,
            "name":      name,
            "members":   participants
        })

    # 8. Join a group via invite link
    def join_group(self, session_id: str, link: str) -> dict:
        return self._post("/group/join", {"accountId": session_id, "link": link})

    # 9. Get group invite link
    def get_group_link(self, session_id: str, group_id: str) -> dict:
        return self._get(f"/group/invite/{session_id}/{group_id}")

    # 10. Leave a group
    def leave_group(self, session_id: str, group_id: str) -> dict:
        return self._post("/group/leave", {"accountId": session_id, "groupId": group_id})

    # 11. Get group members
    def get_members(self, session_id: str, group_id: str) -> dict:
        return self._get(f"/group/members/{session_id}/{group_id}")

    # 12. Add member to group
    def add_member(self, session_id: str, group_id: str, phone: str) -> dict:
        return self._post("/group/add-member", {
            "accountId": session_id,
            "groupId":   group_id,
            "phone":     phone
        })

    # 13. Remove members from group (one by one)
    def remove_members(self, session_id: str, group_id: str, numbers: list) -> dict:
        results = []
        for num in numbers:
            r = self._post("/group/remove-member", {
                "accountId": session_id,
                "groupId":   group_id,
                "memberJid": f"{num}@s.whatsapp.net"
            })
            results.append(r)
        return {"success": True, "results": results}

    # 14. Promote members to admin (one by one)
    def make_admin(self, session_id: str, group_id: str, numbers: list) -> dict:
        results = []
        for num in numbers:
            r = self._post("/group/make-admin", {
                "accountId": session_id,
                "groupId":   group_id,
                "memberJid": f"{num}@s.whatsapp.net"
            })
            results.append(r)
        return {"success": True, "results": results}

    # 15. Demote admins to regular member (one by one)
    def remove_admin(self, session_id: str, group_id: str, numbers: list) -> dict:
        results = []
        for num in numbers:
            r = self._post("/group/remove-admin", {
                "accountId": session_id,
                "groupId":   group_id,
                "memberJid": f"{num}@s.whatsapp.net"
            })
            results.append(r)
        return {"success": True, "results": results}

    # 16. Set group approval/join settings
    def set_approval(self, session_id: str, group_id: str, mode: str) -> dict:
        enabled = True if mode == "on" else False
        return self._post("/group/approval", {
            "accountId": session_id,
            "groupId":   group_id,
            "enabled":   enabled
        })

    # 17. Get pending join requests
    def get_pending(self, session_id: str, group_id: str) -> dict:
        return self._get(f"/group/pending/{session_id}/{group_id}")

    # 18. Approve a pending request
    def approve_pending(self, session_id: str, group_id: str, jid: str) -> dict:
        return self._post("/group/approve", {
            "accountId": session_id,
            "groupId":   group_id,
            "memberJid": jid
        })

    # 19. Reject a pending request
    def reject_pending(self, session_id: str, group_id: str, jid: str) -> dict:
        return self._post("/group/reject-pending", {
            "accountId": session_id,
            "groupId":   group_id,
            "memberJid": jid
        })

    # 20. Check if number is on WhatsApp
    def check_number(self, session_id: str, number: str) -> dict:
        return self._post("/check-number", {"accountId": session_id, "phone": number})

    # 21. Bulk CTC check
    def check_numbers_bulk(self, session_id: str, numbers: list) -> dict:
        return self._post("/api/check/bulk", {"sessionId": session_id, "numbers": numbers})

    # 22. Send a text message to a group or number
    def send_message_to_group(self, session_id: str, group_id: str, text: str) -> dict:
        return self._post("/message/send", {
            "accountId": session_id,
            "to":        group_id,
            "text":      text
        })

    # 23. Get group info
    def get_group_info(self, session_id: str, group_id: str) -> dict:
        return self._get(f"/group/{session_id}/{group_id}")

    # 24. List all active sessions
    def list_sessions(self) -> dict:
        return self._get("/api/sessions")

    # 25. Reset / logout session completely
    def reset_session(self, session_id: str) -> dict:
        return self._post("/api/session/reset", {"sessionId": session_id})


bridge = BridgeAPI(BRIDGE_URL, BRIDGE_KEY)


# ─────────────────────────────────────────────────────────────────
# CONVERSATION STATES
# ─────────────────────────────────────────────────────────────────
MAIN_MENU = 0

# Connect
CONNECT_METHOD, CONNECT_PHONE_INPUT, CONNECT_SESSION_ID = range(1, 4)

# Create Group (7-step)
CG_SESSION, CG_NAME, CG_DESCRIPTION, CG_PROFILE, CG_MEMBERS, CG_WELCOME, CG_APPROVAL, CG_CONFIRM = range(4, 12)

# Join Groups
JOIN_SESSION, JOIN_LINKS = range(12, 14)

# CTC Checker
CTC_SESSION, CTC_MODE, CTC_NUMBERS, CTC_GROUP = range(14, 18)

# Get Link
GETLINK_SESSION, GETLINK_SCOPE = range(18, 20)

# Leave Groups
LEAVE_SESSION, LEAVE_SCOPE, LEAVE_CONFIRM = range(20, 23)

# Remove Members
RM_SESSION, RM_NUMBERS = range(23, 25)

# Make/Remove Admin
ADMIN_OP_SESSION, ADMIN_OP_MEMBERS, ADMIN_OP_ACTION = range(25, 28)

# Approval
APPROVAL_SESSION, APPROVAL_SCOPE, APPROVAL_MODE = range(28, 31)

# Pending
PENDING_SESSION, PENDING_SCOPE, PENDING_ACTION = range(31, 34)

# Add Members
ADDM_SESSION, ADDM_NUMBERS = range(34, 36)

# Disconnect
DISC_SESSION, DISC_CONFIRM = range(36, 38)

# Admin Panel
ADMIN_MENU         = 38
ADMIN_ADD_PREMIUM  = 39
ADMIN_REMOVE_PREM  = 40
ADMIN_TEMP_UID     = 41
ADMIN_TEMP_DURATION= 42

# Inline Group Selector (shared state)
GROUP_SELECT_STATE = 43

# Send Message
SENDMSG_SESSION, SENDMSG_SCOPE, SENDMSG_COLLECT, SENDMSG_CONFIRM = range(44, 48)


# ─────────────────────────────────────────────────────────────────
# UTILITY: KEYBOARDS
# ─────────────────────────────────────────────────────────────────
def make_main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Connect WhatsApp",  callback_data="connect"),
         InlineKeyboardButton("❌ Disconnect",         callback_data="disconnect")],
        [InlineKeyboardButton("📋 Connected Accounts", callback_data="accounts"),
         InlineKeyboardButton("❓ Help",               callback_data="help")],
        [InlineKeyboardButton("➕ Create Group",       callback_data="create_group"),
         InlineKeyboardButton("🔗 Join Groups",        callback_data="join")],
        [InlineKeyboardButton("📞 CTC Checker",        callback_data="ctc"),
         InlineKeyboardButton("🔑 Get Link",           callback_data="getlink")],
        [InlineKeyboardButton("🚪 Leave Groups",       callback_data="leave"),
         InlineKeyboardButton("🗑 Remove Members",     callback_data="remove_members")],
        [InlineKeyboardButton("👑 Make/Remove Admin",  callback_data="admin_op"),
         InlineKeyboardButton("✅ Approval Setting",   callback_data="approval")],
        [InlineKeyboardButton("📋 Pending List",       callback_data="pending"),
         InlineKeyboardButton("➕ Add Members",        callback_data="add_members")],
        [InlineKeyboardButton("📨 Send Message",       callback_data="sendmsg")],
        [InlineKeyboardButton("🛠 Admin Panel",        callback_data="admin_panel")],
    ])


def make_back_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Menu", callback_data="back_menu")]])


def make_cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]])


def make_scope_kb() -> InlineKeyboardMarkup:
    """All Groups vs Select Groups vs Cancel."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 All Groups",    callback_data="scope_all"),
         InlineKeyboardButton("☑️ Select Groups", callback_data="scope_select")],
        [InlineKeyboardButton("❌ Cancel",         callback_data="cancel")]
    ])


def make_admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Premium",       callback_data="adm_add_premium"),
         InlineKeyboardButton("➖ Remove Premium",    callback_data="adm_rm_premium")],
        [InlineKeyboardButton("⏱ Grant Temp Access", callback_data="adm_temp"),
         InlineKeyboardButton("📋 Premium List",      callback_data="adm_list")],
        [InlineKeyboardButton("🆓 Set FREE Mode",     callback_data="adm_free"),
         InlineKeyboardButton("💰 Set PAID Mode",     callback_data="adm_paid")],
        [InlineKeyboardButton("📊 Bot Stats",         callback_data="adm_stats"),
         InlineKeyboardButton("🔙 Back to Menu",      callback_data="back_menu")],
    ])


def make_temp_duration_kb(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("1 Hour",   callback_data=f"td_1_{uid}"),
         InlineKeyboardButton("6 Hours",  callback_data=f"td_6_{uid}"),
         InlineKeyboardButton("12 Hours", callback_data=f"td_12_{uid}")],
        [InlineKeyboardButton("24 Hours", callback_data=f"td_24_{uid}"),
         InlineKeyboardButton("3 Days",   callback_data=f"td_72_{uid}"),
         InlineKeyboardButton("7 Days",   callback_data=f"td_168_{uid}")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
    ])


# ─────────────────────────────────────────────────────────────────
# INLINE GROUP SELECTOR — Reusable component
# ─────────────────────────────────────────────────────────────────
def build_group_selector_kb(groups: list, selected_indices: list) -> InlineKeyboardMarkup:
    """Build inline keyboard with toggleable group buttons."""
    rows = []
    for i, g in enumerate(groups):
        name = g.get("name", g.get("subject", f"Group {i+1}"))[:30]
        icon = "☑️" if i in selected_indices else "☐"
        rows.append([InlineKeyboardButton(f"{icon} {name}", callback_data=f"gs_toggle_{i}")])
    rows.append([
        InlineKeyboardButton("✅ Done",         callback_data="gs_done"),
        InlineKeyboardButton("📋 Select All",   callback_data="gs_all"),
        InlineKeyboardButton("❌ Cancel",        callback_data="cancel")
    ])
    return InlineKeyboardMarkup(rows)


async def show_group_selector(
    message,
    session_id: str,
    context: ContextTypes.DEFAULT_TYPE,
    next_state: int,
    action_label: str = "select"
):
    """
    Reusable group selector — fetches all groups from bridge and shows inline buttons.
    Stores next_state in user_data so group_selector_callback knows where to go after Done.
    Returns GROUP_SELECT_STATE or MAIN_MENU on error.
    """
    res = bridge.get_groups(session_id)
    groups = res.get("groups", [])

    if not groups:
        await message.reply_text("❌ No groups found.", reply_markup=make_back_menu())
        return MAIN_MENU

    context.user_data["available_groups"] = groups
    context.user_data["selected_groups"]  = []
    context.user_data["gs_next_state"]    = next_state

    kb = build_group_selector_kb(groups, [])
    await message.reply_text(
        f"📋 <b>Select Groups</b> ({len(groups)} found)\n\nTap to select/deselect:",
        parse_mode=ParseMode.HTML,
        reply_markup=kb
    )
    return GROUP_SELECT_STATE


async def group_selector_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles all gs_toggle_, gs_all, gs_done callbacks.
    When Done, sets context.user_data['final_selected_groups'] and returns gs_next_state.
    """
    q    = update.callback_query
    data = q.data
    await q.answer()

    groups   = context.user_data.get("available_groups", [])
    selected = context.user_data.get("selected_groups", [])

    if data == "cancel":
        return await cancel(update, context)

    if data.startswith("gs_toggle_"):
        idx = int(data.split("_")[2])
        if idx in selected:
            selected.remove(idx)
        else:
            selected.append(idx)
        context.user_data["selected_groups"] = selected
        kb = build_group_selector_kb(groups, selected)
        count = len(selected)
        await q.edit_message_text(
            f"📋 <b>Select Groups</b> ({count}/{len(groups)} selected)\n\nTap to select/deselect:",
            parse_mode=ParseMode.HTML,
            reply_markup=kb
        )
        return GROUP_SELECT_STATE

    if data == "gs_all":
        selected = list(range(len(groups)))
        context.user_data["selected_groups"] = selected
        kb = build_group_selector_kb(groups, selected)
        await q.edit_message_text(
            f"📋 <b>All {len(groups)} groups selected!</b>\n\nTap to deselect or press Done:",
            parse_mode=ParseMode.HTML,
            reply_markup=kb
        )
        return GROUP_SELECT_STATE

    if data == "gs_done":
        if not selected:
            await q.answer("⚠️ Select at least 1 group!", show_alert=True)
            return GROUP_SELECT_STATE

        # Save final selected group objects
        context.user_data["final_selected_groups"] = [groups[i] for i in selected]
        next_state = context.user_data.get("gs_next_state", MAIN_MENU)

        # Route to the correct post-selection handler based on the feature
        feature = context.user_data.get("gs_feature", "")

        if feature == "getlink":
            return await getlink_after_select(update, context)
        elif feature == "leave":
            return await leave_after_select(update, context)
        elif feature == "remove_members":
            return await rm_after_select(update, context)
        elif feature == "admin_op":
            return await adminop_after_select(update, context)
        elif feature == "approval":
            return await approval_after_select(update, context)
        elif feature == "pending":
            return await pending_after_select(update, context)
        elif feature == "add_members":
            return await addm_after_select(update, context)
        elif feature == "sendmsg":
            return await sendmsg_after_select(update, context)

        return next_state

    return GROUP_SELECT_STATE


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
    await update.message.reply_text(
        text, reply_markup=make_main_menu(), parse_mode=ParseMode.HTML
    )
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
        "/admin — Admin panel (admin only)\n"
        "/cancel — Cancel current operation\n\n"
        "<b>Features:</b>\n"
        "🔗 <b>Connect</b> — Link your WhatsApp via QR or phone number\n"
        "❌ <b>Disconnect</b> — Log out your WhatsApp session\n"
        "📋 <b>Connected Accounts</b> — View all active sessions\n"
        "➕ <b>Create Group</b> — Create a new WhatsApp group (7-step)\n"
        "🔗 <b>Join Groups</b> — Join groups via invite links\n"
        "📞 <b>CTC Checker</b> — Check if numbers are on WhatsApp\n"
        "🔑 <b>Get Link</b> — Get invite link for your group(s)\n"
        "🚪 <b>Leave Groups</b> — Leave one or more groups\n"
        "🗑 <b>Remove Members</b> — Remove members from group(s)\n"
        "👑 <b>Make/Remove Admin</b> — Promote or demote group admins\n"
        "✅ <b>Approval Setting</b> — Enable/disable join approval\n"
        "📋 <b>Get Pending List</b> — View and manage join requests\n"
        "➕ <b>Add Members</b> — Add members to group(s)\n"
        "📨 <b>Send Message</b> — Send messages to groups (instant or scheduled)\n\n"
        "<b>Support:</b> Contact the bot admin for help."
    )
    if update.message:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=make_back_menu())
    elif update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=make_back_menu())
    return MAIN_MENU


# ─────────────────────────────────────────────────────────────────
# CONNECTED ACCOUNTS
# ─────────────────────────────────────────────────────────────────
async def show_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    res      = bridge.list_sessions()
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
        await update.callback_query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=make_back_menu()
        )
    else:
        await update.message.reply_text(
            text, parse_mode=ParseMode.HTML, reply_markup=make_back_menu()
        )
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
        await update.message.reply_text(
            "❌ Cancelled. Returning to main menu.",
            reply_markup=make_main_menu()
        )
    return MAIN_MENU


# ─────────────────────────────────────────────────────────────────
# MAIN MENU CALLBACK
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
        await q.edit_message_text(
            "👑 <b>Admin Panel</b>\n\nChoose an action:",
            parse_mode=ParseMode.HTML, reply_markup=make_admin_menu()
        )
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
            parse_mode=ParseMode.HTML, reply_markup=make_cancel_kb()
        )
        return DISC_SESSION

    if data == "create_group":
        await q.edit_message_text(
            "➕ <b>Create WhatsApp Group</b>\n\n"
            "Step 1/7: Enter the <b>session ID</b> (your connected account ID):",
            parse_mode=ParseMode.HTML, reply_markup=make_cancel_kb()
        )
        return CG_SESSION

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

    if data == "sendmsg":
        await q.edit_message_text(
            "📨 <b>Send Message</b>\n\nEnter the session ID of the account to use:",
            parse_mode=ParseMode.HTML, reply_markup=make_cancel_kb()
        )
        return SENDMSG_SESSION

    return MAIN_MENU


# ─────────────────────────────────────────────────────────────────
# FEATURE: CONNECT WHATSAPP (Fix 1 & 2: Pairing + QR Polling)
# ─────────────────────────────────────────────────────────────────

def _check_single_session_limit(exclude_session: str = None) -> str | None:
    """
    Fix 3: Enforce single account limit.
    Returns the existing session ID if one is already connected, else None.
    """
    res = bridge.list_sessions()
    sessions = res.get("sessions", [])
    for s in sessions:
        sid = s.get("sessionId", s.get("accountId", ""))
        if sid and sid != exclude_session:
            status = bridge.get_status(sid)
            if status.get("connected"):
                return sid
    return None


async def connect_method_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    data = q.data
    await q.answer()

    if data == "back_menu":
        await q.edit_message_text(
            "🤖 <b>WhatsApp Group Manager</b>\n\nChoose an option below:",
            reply_markup=make_main_menu(), parse_mode=ParseMode.HTML
        )
        return MAIN_MENU

    if data in ("connect_qr", "connect_phone"):
        context.user_data["connect_method"] = data
        label = "QR Code" if data == "connect_qr" else "Phone Number"
        await q.edit_message_text(
            f"📱 <b>{label} Connection</b>\n\n"
            "Enter a unique session ID for this connection\n"
            "(e.g. <code>myphone1</code>):",
            parse_mode=ParseMode.HTML, reply_markup=make_cancel_kb()
        )
        return CONNECT_SESSION_ID

    if data == "cancel":
        return await cancel(update, context)

    return CONNECT_METHOD


async def connect_session_id_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sid    = update.message.text.strip()
    method = context.user_data.get("connect_method", "connect_qr")
    context.user_data["session_id"] = sid

    # Fix 3: Single account limit check
    existing = _check_single_session_limit()
    if existing:
        await update.message.reply_text(
            f"⛔ <b>Single Account Limit Reached</b>\n\n"
            f"Session <code>{existing}</code> is already connected.\n"
            f"Please disconnect it first before connecting a new account.",
            parse_mode=ParseMode.HTML,
            reply_markup=make_back_menu()
        )
        context.user_data.clear()
        return MAIN_MENU

    if method == "connect_qr":
        # Fix 2: QR Polling
        status_msg = await update.message.reply_text(
            "⏳ Requesting QR code… (attempt 1/15)"
        )
        res = bridge.connect_qr(sid)

        if not res.get("success"):
            await status_msg.edit_text(
                f"❌ Failed to initiate QR connection: {res.get('error', 'Unknown error')}",
            )
            context.user_data.clear()
            return MAIN_MENU

        # Poll for QR data
        qr_data = res.get("qrData", "")
        attempt  = 1
        while not qr_data and attempt <= 15:
            attempt += 1
            await asyncio.sleep(3)
            poll_res = bridge.get_status(sid)
            qr_check = bridge.connect_qr(sid) if not qr_data else {}
            # Try to get qr from status (bridge may embed it) or retry connect
            qr_data = qr_check.get("qrData", "") or poll_res.get("qrData", "")
            try:
                await status_msg.edit_text(
                    f"⏳ Generating QR code… (attempt {attempt}/15)"
                )
            except Exception:
                pass

        if qr_data:
            # Build a qr.png URL via a public QR API
            import urllib.parse
            qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&data={urllib.parse.quote(qr_data)}"
            await status_msg.delete()
            try:
                await update.message.reply_photo(
                    photo=qr_url,
                    caption=(
                        f"📷 <b>Scan this QR Code in WhatsApp</b>\n\n"
                        f"Open WhatsApp → ⋮ Menu → Linked Devices → Link a Device\n\n"
                        f"Session ID: <code>{sid}</code>\n\n"
                        f"⚠️ QR code expires in ~60 seconds."
                    ),
                    parse_mode=ParseMode.HTML,
                    reply_markup=make_back_menu()
                )
            except Exception:
                await update.message.reply_text(
                    f"📷 <b>Scan this QR Code in WhatsApp</b>\n\n"
                    f"<pre>{qr_data[:400]}</pre>\n\n"
                    f"Session ID: <code>{sid}</code>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=make_back_menu()
                )
        else:
            await status_msg.edit_text(
                "⚠️ QR code generation is taking longer than expected.\n"
                "Please try again in a moment.",
                reply_markup=make_back_menu()
            )

        context.user_data.clear()
        return MAIN_MENU

    else:
        # Phone pairing
        await update.message.reply_text(
            "📱 Enter the phone number to pair (with country code, no + or spaces):\n"
            "Example: <code>919876543210</code>",
            parse_mode=ParseMode.HTML, reply_markup=make_cancel_kb()
        )
        return CONNECT_PHONE_INPUT


async def connect_phone_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip().replace("+", "").replace(" ", "")
    sid   = context.user_data.get("session_id", "default")

    status_msg = await update.message.reply_text("⏳ Initiating phone pairing…")

    res = bridge.connect_phone(sid, phone)
    if not res.get("success"):
        await status_msg.edit_text(
            f"❌ Failed: {res.get('error', 'Unknown error')}",
        )
        context.user_data.clear()
        return MAIN_MENU

    # Fix 1: Poll for pairing code
    await status_msg.edit_text("⏳ Waiting for pairing code… (attempt 1/15)")
    code      = ""
    attempt   = 1

    while attempt <= 15:
        await asyncio.sleep(3)
        code_res = bridge.get_pairing_code(sid)
        raw_code = code_res.get("code", "") or code_res.get("pairingCode", "")

        if raw_code and raw_code != "Generating...":
            code = raw_code
            break

        attempt += 1
        try:
            await status_msg.edit_text(
                f"⏳ Waiting for pairing code… (attempt {attempt}/15)"
            )
        except Exception:
            pass

    if code:
        await status_msg.delete()
        await update.message.reply_text(
            f"🔢 <b>Your Pairing Code:</b>\n\n"
            f"<code>{code}</code>\n\n"
            f"Enter this code in WhatsApp:\n"
            f"⋮ Menu → Linked Devices → Link a Device → Link with phone number\n\n"
            f"Session ID: <code>{sid}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=make_back_menu()
        )
    else:
        await status_msg.edit_text(
            "⚠️ Pairing code is taking longer than expected.\n"
            "The code may appear shortly on the bridge side.\n"
            f"Session ID: <code>{sid}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=make_back_menu()
        )

    context.user_data.clear()
    return MAIN_MENU


# ─────────────────────────────────────────────────────────────────
# FEATURE: DISCONNECT WHATSAPP
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
        await q.edit_message_text(
            f"✅ Session <code>{sid}</code> disconnected successfully.",
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
# FEATURE: CREATE GROUP (7-step flow)
# ─────────────────────────────────────────────────────────────────
async def cg_step1_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 1: Receive session ID."""
    context.user_data["cg_session"] = update.message.text.strip()
    await update.message.reply_text(
        "✏️ <b>Step 2/7:</b> Enter the <b>group name</b>:",
        parse_mode=ParseMode.HTML, reply_markup=make_cancel_kb()
    )
    return CG_NAME


async def cg_step2_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 2: Receive group name."""
    context.user_data["cg_name"] = update.message.text.strip()
    await update.message.reply_text(
        "📝 <b>Step 3/7:</b> Enter a <b>group description</b> (or type 'skip'):",
        parse_mode=ParseMode.HTML, reply_markup=make_cancel_kb()
    )
    return CG_DESCRIPTION


async def cg_step3_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 3: Receive description."""
    txt = update.message.text.strip()
    context.user_data["cg_description"] = "" if txt.lower() == "skip" else txt
    await update.message.reply_text(
        "🖼 <b>Step 4/7:</b> Send a <b>profile photo</b> for the group, or type 'skip':",
        parse_mode=ParseMode.HTML, reply_markup=make_cancel_kb()
    )
    return CG_PROFILE


async def cg_step4_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 4: Receive profile photo or skip."""
    if update.message.photo:
        context.user_data["cg_profile"] = update.message.photo[-1].file_id
    else:
        context.user_data["cg_profile"] = None
    await update.message.reply_text(
        "👥 <b>Step 5/7:</b> Enter <b>member phone numbers</b>, one per line (with country code):\n\n"
        "Example:\n<code>919876543210\n447911123456</code>",
        parse_mode=ParseMode.HTML, reply_markup=make_cancel_kb()
    )
    return CG_MEMBERS


async def cg_step5_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 5: Receive member numbers."""
    lines = [
        l.strip().replace("+", "").replace(" ", "")
        for l in update.message.text.strip().splitlines() if l.strip()
    ]
    context.user_data["cg_members"] = lines
    await update.message.reply_text(
        "👋 <b>Step 6/7:</b> Enter a <b>welcome message</b> to send after creation\n"
        "(or type 'skip' to skip):",
        parse_mode=ParseMode.HTML, reply_markup=make_cancel_kb()
    )
    return CG_WELCOME


async def cg_step6_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 6: Receive welcome message."""
    txt = update.message.text.strip()
    context.user_data["cg_welcome"] = "" if txt.lower() == "skip" else txt
    await update.message.reply_text(
        "🔒 <b>Step 7/7:</b> Enable <b>join approval</b> for the group?",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Yes (Approval ON)",    callback_data="cg_approval_on"),
             InlineKeyboardButton("❌ No (Open to anyone)", callback_data="cg_approval_off")]
        ])
    )
    return CG_APPROVAL


async def cg_step7_approval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 7: Receive approval setting, then show confirm."""
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
        if approval == "on" and group_id:
            bridge.set_approval(sid, group_id, "on")
        if welcome and group_id:
            bridge.send_message_to_group(sid, group_id, welcome)

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
# FEATURE: JOIN GROUPS
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
            results.append(f"❌ Failed ({res.get('error', 'err')}): {link}")

    text = "🔗 <b>Join Results</b>\n\n" + "\n".join(results)
    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=make_back_menu()
    )
    context.user_data.clear()
    return MAIN_MENU


# ─────────────────────────────────────────────────────────────────
# FEATURE: CTC CHECKER
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
            [InlineKeyboardButton("❌ Cancel",                    callback_data="cancel")]
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
        pending = res.get("pending", [])
        numbers = [p.get("phone", p.get("jid", "").replace("@s.whatsapp.net", "")) for p in pending]
    else:
        res     = bridge.get_members(sid, group_id)
        numbers = [
            m.get("jid", m.get("id", "")).replace("@s.whatsapp.net", "")
            for m in res.get("members", [])
        ]

    if not numbers:
        await update.message.reply_text("ℹ️ No numbers found.", reply_markup=make_back_menu())
        context.user_data.clear()
        return MAIN_MENU

    await update.message.reply_text(f"⏳ Checking {len(numbers)} numbers on WhatsApp…")
    res2   = bridge.check_numbers_bulk(sid, numbers)
    on_wa  = res2.get("onWhatsApp", [])
    not_wa = res2.get("notOnWhatsApp", [])

    text = (
        f"📞 <b>CTC Results</b>\n\n"
        f"✅ On WhatsApp: {len(on_wa)}\n"
        f"❌ Not on WhatsApp: {len(not_wa)}\n\n"
        f"<b>On WhatsApp:</b>\n" + "\n".join(on_wa[:30]) +
        ("\n..." if len(on_wa) > 30 else "")
    )
    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=make_back_menu()
    )
    context.user_data.clear()
    return MAIN_MENU


async def ctc_numbers_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sid     = context.user_data.get("ctc_session", "")
    numbers = [
        l.strip().replace("+", "").replace(" ", "")
        for l in update.message.text.strip().splitlines() if l.strip()
    ]

    await update.message.reply_text(f"⏳ Checking {len(numbers)} numbers…")
    res    = bridge.check_numbers_bulk(sid, numbers)
    on_wa  = res.get("onWhatsApp", [])
    not_wa = res.get("notOnWhatsApp", [])

    text = (
        f"📞 <b>CTC Results</b>\n\n"
        f"✅ On WhatsApp ({len(on_wa)}):\n" + "\n".join(on_wa[:30]) + "\n\n"
        f"❌ Not on WhatsApp ({len(not_wa)}):\n" + "\n".join(not_wa[:20])
    )
    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=make_back_menu()
    )
    context.user_data.clear()
    return MAIN_MENU


# ─────────────────────────────────────────────────────────────────
# FEATURE: GET LINK (Fix 4: Inline Group Selector)
# ─────────────────────────────────────────────────────────────────
async def getlink_session_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["gl_session"] = update.message.text.strip()
    await update.message.reply_text(
        "🔑 <b>Get Group Link</b>\n\nChoose scope:",
        parse_mode=ParseMode.HTML,
        reply_markup=make_scope_kb()
    )
    return GETLINK_SCOPE


async def getlink_scope_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    data = q.data
    await q.answer()

    if data == "cancel":
        return await cancel(update, context)

    sid = context.user_data.get("gl_session", "")

    if data == "scope_all":
        res    = bridge.get_groups(sid)
        groups = res.get("groups", [])
        if not groups:
            await q.edit_message_text("❌ No groups found.", reply_markup=make_back_menu())
            return MAIN_MENU
        context.user_data["final_selected_groups"] = groups
        await q.edit_message_text(f"⏳ Fetching links for {len(groups)} groups…")
        return await _do_getlink(q.message, context, edit=False, edit_msg=q)

    # scope_select
    context.user_data["gs_feature"] = "getlink"
    await q.edit_message_text("⏳ Loading groups…")
    return await show_group_selector(q.message, sid, context, MAIN_MENU, "select")


async def getlink_after_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.edit_message_text("⏳ Fetching invite links…")
    return await _do_getlink(q.message, context, edit=False, edit_msg=q)


async def _do_getlink(message, context: ContextTypes.DEFAULT_TYPE, edit: bool = True, edit_msg=None):
    sid    = context.user_data.get("gl_session", "")
    groups = context.user_data.get("final_selected_groups", [])

    lines = []
    for g in groups:
        gid  = g.get("id", "")
        name = g.get("name", g.get("subject", gid))[:30]
        res  = bridge.get_group_link(sid, gid)
        link = res.get("link", "")
        if res.get("success") and link:
            lines.append(f"✅ <b>{name}</b>\n{link}")
        else:
            lines.append(f"❌ <b>{name}</b> — {res.get('error', 'Failed')}")

    text = "🔑 <b>Group Invite Links</b>\n\n" + "\n\n".join(lines)
    try:
        if edit_msg:
            await edit_msg.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=make_back_menu())
        else:
            await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=make_back_menu())
    except Exception:
        await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=make_back_menu())

    context.user_data.clear()
    return MAIN_MENU


# ─────────────────────────────────────────────────────────────────
# FEATURE: LEAVE GROUPS (Fix 4: Inline Group Selector)
# ─────────────────────────────────────────────────────────────────
async def leave_session_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["leave_session"] = update.message.text.strip()
    await update.message.reply_text(
        "🚪 <b>Leave Groups</b>\n\nChoose scope:",
        parse_mode=ParseMode.HTML,
        reply_markup=make_scope_kb()
    )
    return LEAVE_SCOPE


async def leave_scope_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    data = q.data
    await q.answer()

    if data == "cancel":
        return await cancel(update, context)

    sid = context.user_data.get("leave_session", "")

    if data == "scope_all":
        res    = bridge.get_groups(sid)
        groups = res.get("groups", [])
        if not groups:
            await q.edit_message_text("❌ No groups found.", reply_markup=make_back_menu())
            return MAIN_MENU
        context.user_data["final_selected_groups"] = groups
        await q.edit_message_text(
            f"🚪 About to leave <b>ALL {len(groups)} groups</b>. Confirm?",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Leave All", callback_data="leave_confirm_yes"),
                 InlineKeyboardButton("❌ Cancel",     callback_data="cancel")]
            ])
        )
        return LEAVE_CONFIRM

    # scope_select
    context.user_data["gs_feature"] = "leave"
    await q.edit_message_text("⏳ Loading groups…")
    return await show_group_selector(q.message, sid, context, MAIN_MENU, "select")


async def leave_after_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q      = update.callback_query
    groups = context.user_data.get("final_selected_groups", [])
    await q.edit_message_text(
        f"🚪 About to leave <b>{len(groups)} selected group(s)</b>. Confirm?",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Leave Selected", callback_data="leave_confirm_yes"),
             InlineKeyboardButton("❌ Cancel",          callback_data="cancel")]
        ])
    )
    return LEAVE_CONFIRM


async def leave_confirm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "cancel":
        return await cancel(update, context)

    sid    = context.user_data.get("leave_session", "")
    groups = context.user_data.get("final_selected_groups", [])

    await q.edit_message_text(f"⏳ Leaving {len(groups)} group(s)…")

    ok = fail = 0
    for g in groups:
        gid = g.get("id", "")
        res = bridge.leave_group(sid, gid)
        if res.get("success"):
            ok += 1
        else:
            fail += 1

    await q.edit_message_text(
        f"🚪 <b>Leave Results</b>\n\n✅ Left: {ok}\n❌ Failed: {fail}",
        parse_mode=ParseMode.HTML, reply_markup=make_back_menu()
    )
    context.user_data.clear()
    return MAIN_MENU


# ─────────────────────────────────────────────────────────────────
# FEATURE: REMOVE MEMBERS (Fix 4: Inline Group Selector)
# ─────────────────────────────────────────────────────────────────
async def rm_session_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["rm_session"] = update.message.text.strip()
    context.user_data["gs_feature"] = "remove_members"
    sid = context.user_data["rm_session"]
    await update.message.reply_text("⏳ Loading groups…")
    return await show_group_selector(update.message, sid, context, MAIN_MENU, "select")


async def rm_after_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.edit_message_text(
        "📞 Enter phone numbers to <b>remove</b>, one per line (with country code):",
        parse_mode=ParseMode.HTML, reply_markup=make_cancel_kb()
    )
    return RM_NUMBERS


async def rm_numbers_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sid     = context.user_data.get("rm_session", "")
    groups  = context.user_data.get("final_selected_groups", [])
    numbers = [
        l.strip().replace("+", "").replace(" ", "")
        for l in update.message.text.strip().splitlines() if l.strip()
    ]

    await update.message.reply_text(
        f"⏳ Removing {len(numbers)} member(s) from {len(groups)} group(s)…"
    )

    total_ok = total_fail = 0
    for g in groups:
        gid = g.get("id", "")
        res = bridge.remove_members(sid, gid, numbers)
        for r in res.get("results", []):
            if r.get("success"):
                total_ok += 1
            else:
                total_fail += 1

    await update.message.reply_text(
        f"🗑 <b>Remove Results</b>\n\n"
        f"✅ Removed: {total_ok}\n"
        f"❌ Failed: {total_fail}",
        parse_mode=ParseMode.HTML, reply_markup=make_back_menu()
    )
    context.user_data.clear()
    return MAIN_MENU


# ─────────────────────────────────────────────────────────────────
# FEATURE: MAKE / REMOVE ADMIN (Fix 4: Inline Group Selector)
# ─────────────────────────────────────────────────────────────────
async def adminop_session_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["aop_session"] = update.message.text.strip()
    context.user_data["gs_feature"]  = "admin_op"
    sid = context.user_data["aop_session"]
    await update.message.reply_text("⏳ Loading groups…")
    return await show_group_selector(update.message, sid, context, MAIN_MENU, "select")


async def adminop_after_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.edit_message_text(
        "📞 Enter phone numbers to promote/demote, one per line:",
        reply_markup=make_cancel_kb()
    )
    return ADMIN_OP_MEMBERS


async def adminop_members_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    numbers = [
        l.strip().replace("+", "").replace(" ", "")
        for l in update.message.text.strip().splitlines() if l.strip()
    ]
    context.user_data["aop_members"] = numbers
    await update.message.reply_text(
        "👑 Choose action:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬆️ Make Admin",   callback_data="aop_promote"),
             InlineKeyboardButton("⬇️ Remove Admin", callback_data="aop_demote")],
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
    groups  = context.user_data.get("final_selected_groups", [])
    members = context.user_data.get("aop_members", [])

    action_label = "promoted to admin" if data == "aop_promote" else "demoted from admin"
    await q.edit_message_text(
        f"⏳ {action_label.capitalize()} {len(members)} member(s) in {len(groups)} group(s)…"
    )

    total_ok = total_fail = 0
    for g in groups:
        gid = g.get("id", "")
        if data == "aop_promote":
            res = bridge.make_admin(sid, gid, members)
        else:
            res = bridge.remove_admin(sid, gid, members)
        for r in res.get("results", []):
            if r.get("success"):
                total_ok += 1
            else:
                total_fail += 1

    await q.edit_message_text(
        f"👑 <b>Admin Operation Results</b>\n\n"
        f"Action: {action_label}\n"
        f"✅ Done: {total_ok}\n"
        f"❌ Failed: {total_fail}",
        parse_mode=ParseMode.HTML, reply_markup=make_back_menu()
    )
    context.user_data.clear()
    return MAIN_MENU


# ─────────────────────────────────────────────────────────────────
# FEATURE: APPROVAL SETTING (Fix 4: Inline Group Selector)
# ─────────────────────────────────────────────────────────────────
async def approval_session_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["appr_session"] = update.message.text.strip()
    await update.message.reply_text(
        "✅ <b>Approval Setting</b>\n\nChoose scope:",
        parse_mode=ParseMode.HTML,
        reply_markup=make_scope_kb()
    )
    return APPROVAL_SCOPE


async def approval_scope_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    data = q.data
    await q.answer()

    if data == "cancel":
        return await cancel(update, context)

    sid = context.user_data.get("appr_session", "")

    if data == "scope_all":
        res    = bridge.get_groups(sid)
        groups = res.get("groups", [])
        if not groups:
            await q.edit_message_text("❌ No groups found.", reply_markup=make_back_menu())
            return MAIN_MENU
        context.user_data["final_selected_groups"] = groups
    else:
        context.user_data["gs_feature"] = "approval"
        await q.edit_message_text("⏳ Loading groups…")
        return await show_group_selector(q.message, sid, context, MAIN_MENU)

    await q.edit_message_text(
        "✅ <b>Approval Setting</b>\n\nChoose mode:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Enable Approval (ON)",   callback_data="appr_on"),
             InlineKeyboardButton("❌ Disable Approval (OFF)", callback_data="appr_off")],
            [InlineKeyboardButton("❌ Cancel",                  callback_data="cancel")]
        ])
    )
    return APPROVAL_MODE


async def approval_after_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.edit_message_text(
        "✅ <b>Approval Setting</b>\n\nChoose mode:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Enable Approval (ON)",   callback_data="appr_on"),
             InlineKeyboardButton("❌ Disable Approval (OFF)", callback_data="appr_off")],
            [InlineKeyboardButton("❌ Cancel",                  callback_data="cancel")]
        ])
    )
    return APPROVAL_MODE


async def approval_mode_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    data = q.data
    await q.answer()

    if data == "cancel":
        return await cancel(update, context)

    sid    = context.user_data.get("appr_session", "")
    groups = context.user_data.get("final_selected_groups", [])
    mode   = "on" if data == "appr_on" else "off"

    await q.edit_message_text(f"⏳ Setting approval to {mode.upper()} for {len(groups)} group(s)…")

    ok = fail = 0
    for g in groups:
        gid = g.get("id", "")
        res = bridge.set_approval(sid, gid, mode)
        if res.get("success"):
            ok += 1
        else:
            fail += 1

    await q.edit_message_text(
        f"✅ <b>Approval Setting Results</b>\n\n"
        f"Mode: <b>{mode.upper()}</b>\n"
        f"✅ Updated: {ok}\n"
        f"❌ Failed: {fail}",
        parse_mode=ParseMode.HTML, reply_markup=make_back_menu()
    )
    context.user_data.clear()
    return MAIN_MENU


# ─────────────────────────────────────────────────────────────────
# FEATURE: GET PENDING LIST (Fix 4: Inline Group Selector)
# ─────────────────────────────────────────────────────────────────
async def pending_session_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["pend_session"] = update.message.text.strip()
    await update.message.reply_text(
        "📋 <b>Get Pending List</b>\n\nChoose scope:",
        parse_mode=ParseMode.HTML,
        reply_markup=make_scope_kb()
    )
    return PENDING_SCOPE


async def pending_scope_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    data = q.data
    await q.answer()

    if data == "cancel":
        return await cancel(update, context)

    sid = context.user_data.get("pend_session", "")

    if data == "scope_all":
        res    = bridge.get_groups(sid)
        groups = res.get("groups", [])
        if not groups:
            await q.edit_message_text("❌ No groups found.", reply_markup=make_back_menu())
            return MAIN_MENU
        context.user_data["final_selected_groups"] = groups
        await q.edit_message_text("⏳ Fetching pending requests from all groups…")
        return await _do_pending(q.message, context, edit_msg=q)

    context.user_data["gs_feature"] = "pending"
    await q.edit_message_text("⏳ Loading groups…")
    return await show_group_selector(q.message, sid, context, MAIN_MENU)


async def pending_after_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.edit_message_text("⏳ Fetching pending requests…")
    return await _do_pending(q.message, context, edit_msg=q)


async def _do_pending(message, context: ContextTypes.DEFAULT_TYPE, edit_msg=None):
    sid    = context.user_data.get("pend_session", "")
    groups = context.user_data.get("final_selected_groups", [])

    all_pending = []
    for g in groups:
        gid  = g.get("id", "")
        name = g.get("name", g.get("subject", gid))[:25]
        res  = bridge.get_pending(sid, gid)
        pend = res.get("pending", [])
        for p in pend:
            p["_group_id"]   = gid
            p["_group_name"] = name
        all_pending.extend(pend)

    if not all_pending:
        text = "📋 No pending join requests found across selected groups."
        if edit_msg:
            await edit_msg.edit_message_text(text, reply_markup=make_back_menu())
        else:
            await message.reply_text(text, reply_markup=make_back_menu())
        context.user_data.clear()
        return MAIN_MENU

    context.user_data["pend_all"] = all_pending

    lines = []
    for i, p in enumerate(all_pending[:30], 1):
        phone      = p.get("phone", p.get("jid", "?").replace("@s.whatsapp.net", ""))
        group_name = p.get("_group_name", "?")
        lines.append(f"{i}. <code>{phone}</code> — {group_name}")

    text = (
        f"📋 <b>Pending Requests</b> ({len(all_pending)} total)\n\n" +
        "\n".join(lines) +
        (f"\n<i>...and {len(all_pending)-30} more</i>" if len(all_pending) > 30 else "")
    )

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Approve All", callback_data="pend_approve_all"),
         InlineKeyboardButton("❌ Reject All",  callback_data="pend_reject_all")],
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_menu")]
    ])

    if edit_msg:
        await edit_msg.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    else:
        await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)

    return PENDING_ACTION


async def pending_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    data = q.data
    await q.answer()

    if data == "back_menu":
        return await cancel(update, context)

    sid        = context.user_data.get("pend_session", "")
    all_pending= context.user_data.get("pend_all", [])

    await q.edit_message_text(f"⏳ Processing {len(all_pending)} requests…")

    ok = fail = 0
    for p in all_pending:
        jid = p.get("jid", "")
        gid = p.get("_group_id", "")
        if data == "pend_approve_all":
            r = bridge.approve_pending(sid, gid, jid)
        else:
            r = bridge.reject_pending(sid, gid, jid)
        if r.get("success"):
            ok += 1
        else:
            fail += 1

    action_label = "Approved" if data == "pend_approve_all" else "Rejected"
    await q.edit_message_text(
        f"✅ {action_label}: {ok}   ❌ Failed: {fail}",
        reply_markup=make_back_menu()
    )
    context.user_data.clear()
    return MAIN_MENU


# ─────────────────────────────────────────────────────────────────
# FEATURE: ADD MEMBERS (Fix 4: Inline Group Selector)
# ─────────────────────────────────────────────────────────────────
async def addm_session_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["addm_session"] = update.message.text.strip()
    context.user_data["gs_feature"]   = "add_members"
    sid = context.user_data["addm_session"]
    await update.message.reply_text("⏳ Loading groups…")
    return await show_group_selector(update.message, sid, context, MAIN_MENU, "select")


async def addm_after_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.edit_message_text(
        "📞 Enter phone numbers to <b>add</b>, one per line (with country code):",
        parse_mode=ParseMode.HTML, reply_markup=make_cancel_kb()
    )
    return ADDM_NUMBERS


async def addm_numbers_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sid     = context.user_data.get("addm_session", "")
    groups  = context.user_data.get("final_selected_groups", [])
    numbers = [
        l.strip().replace("+", "").replace(" ", "")
        for l in update.message.text.strip().splitlines() if l.strip()
    ]

    await update.message.reply_text(
        f"⏳ Adding {len(numbers)} member(s) to {len(groups)} group(s)…"
    )

    total_ok = total_fail = 0
    for g in groups:
        gid = g.get("id", "")
        for num in numbers:
            res = bridge.add_member(sid, gid, num)
            if res.get("success"):
                total_ok += 1
            else:
                total_fail += 1

    await update.message.reply_text(
        f"➕ <b>Add Members Results</b>\n\n"
        f"✅ Added: {total_ok}\n"
        f"❌ Failed: {total_fail}",
        parse_mode=ParseMode.HTML, reply_markup=make_back_menu()
    )
    context.user_data.clear()
    return MAIN_MENU


# ─────────────────────────────────────────────────────────────────
# FEATURE: SEND MESSAGE (NEW — Fix 4 + scheduling)
# ─────────────────────────────────────────────────────────────────
async def sendmsg_session_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["sm_session"] = update.message.text.strip()
    await update.message.reply_text(
        "📨 <b>Send Message</b>\n\nChoose which groups to send to:",
        parse_mode=ParseMode.HTML,
        reply_markup=make_scope_kb()
    )
    return SENDMSG_SCOPE


async def sendmsg_scope_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    data = q.data
    await q.answer()

    if data == "cancel":
        return await cancel(update, context)

    sid = context.user_data.get("sm_session", "")

    if data == "scope_all":
        res    = bridge.get_groups(sid)
        groups = res.get("groups", [])
        if not groups:
            await q.edit_message_text("❌ No groups found.", reply_markup=make_back_menu())
            return MAIN_MENU
        context.user_data["final_selected_groups"] = groups
        context.user_data["sm_scope_label"]        = f"ALL ({len(groups)} groups)"
        context.user_data["sm_messages"]           = []
        await q.edit_message_text(
            f"📨 <b>Collecting Messages</b>\n\n"
            f"Scope: ALL ({len(groups)} groups)\n\n"
            f"Send your messages now — one per chat message.\n"
            f"Type /done when you're finished.",
            parse_mode=ParseMode.HTML,
            reply_markup=make_cancel_kb()
        )
        return SENDMSG_COLLECT

    # scope_select
    context.user_data["gs_feature"] = "sendmsg"
    await q.edit_message_text("⏳ Loading groups…")
    return await show_group_selector(q.message, sid, context, MAIN_MENU)


async def sendmsg_after_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q      = update.callback_query
    groups = context.user_data.get("final_selected_groups", [])
    context.user_data["sm_scope_label"] = f"{len(groups)} selected group(s)"
    context.user_data["sm_messages"]    = []
    await q.edit_message_text(
        f"📨 <b>Collecting Messages</b>\n\n"
        f"Scope: {len(groups)} selected group(s)\n\n"
        f"Send your messages now — one per chat message.\n"
        f"Type /done when you're finished.",
        parse_mode=ParseMode.HTML,
        reply_markup=make_cancel_kb()
    )
    return SENDMSG_COLLECT


async def sendmsg_collect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Collect messages from the user one by one until /done."""
    text = update.message.text or ""

    if text.strip().lower() in ("/done", "done"):
        msgs   = context.user_data.get("sm_messages", [])
        groups = context.user_data.get("final_selected_groups", [])
        scope  = context.user_data.get("sm_scope_label", "?")

        if not msgs:
            await update.message.reply_text(
                "⚠️ No messages collected! Send at least one message first.",
                reply_markup=make_cancel_kb()
            )
            return SENDMSG_COLLECT

        confirm_text = (
            f"📨 <b>Confirm Send</b>\n\n"
            f"Messages: <b>{len(msgs)}</b>\n"
            f"Groups: <b>{scope}</b>\n\n"
            f"Choose when to send:"
        )
        await update.message.reply_text(
            confirm_text,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🚀 Send Now",   callback_data="sm_now")],
                [InlineKeyboardButton("⏰ 5 min",  callback_data="sm_sched_5"),
                 InlineKeyboardButton("⏰ 15 min", callback_data="sm_sched_15"),
                 InlineKeyboardButton("⏰ 30 min", callback_data="sm_sched_30"),
                 InlineKeyboardButton("⏰ 60 min", callback_data="sm_sched_60")],
                [InlineKeyboardButton("❌ Cancel",     callback_data="cancel")]
            ])
        )
        return SENDMSG_CONFIRM

    # Store message
    msgs = context.user_data.get("sm_messages", [])
    msgs.append(text)
    context.user_data["sm_messages"] = msgs
    scope = context.user_data.get("sm_scope_label", "?")

    await update.message.reply_text(
        f"✅ Message {len(msgs)} saved.\n"
        f"Send another, or type /done when finished.\n"
        f"(Total: {len(msgs)} message(s))"
    )
    return SENDMSG_COLLECT


async def sendmsg_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    data = q.data
    await q.answer()

    if data == "cancel":
        return await cancel(update, context)

    msgs   = context.user_data.get("sm_messages", [])
    groups = context.user_data.get("final_selected_groups", [])
    sid    = context.user_data.get("sm_session", "")
    scope  = context.user_data.get("sm_scope_label", "?")

    if data == "sm_now":
        await q.edit_message_text(
            f"🚀 <b>Sending {len(msgs)} message(s) to {len(groups)} group(s)…</b>",
            parse_mode=ParseMode.HTML
        )
        ok, fail = await _do_send_messages(sid, groups, msgs, q.message)
        await q.message.reply_text(
            f"📨 <b>Send Complete</b>\n\n"
            f"✅ Delivered: {ok}\n"
            f"❌ Failed: {fail}",
            parse_mode=ParseMode.HTML, reply_markup=make_back_menu()
        )
        context.user_data.clear()
        return MAIN_MENU

    # Scheduled send
    delay_map = {"sm_sched_5": 5, "sm_sched_15": 15, "sm_sched_30": 30, "sm_sched_60": 60}
    delay_min = delay_map.get(data, 5)

    await q.edit_message_text(
        f"⏰ <b>Scheduled!</b>\n\n"
        f"Will send {len(msgs)} message(s) to {scope} in <b>{delay_min} minute(s)</b>.\n"
        f"You can keep using the bot in the meantime.",
        parse_mode=ParseMode.HTML, reply_markup=make_back_menu()
    )

    # Snapshot data for scheduled task
    _msgs   = list(msgs)
    _groups = list(groups)
    _sid    = str(sid)

    async def scheduled_send():
        await asyncio.sleep(delay_min * 60)
        ok, fail = await _do_send_messages(_sid, _groups, _msgs, None)
        try:
            # Notify the user
            await q.message.reply_text(
                f"⏰ <b>Scheduled Send Complete</b>\n\n"
                f"✅ Delivered: {ok}\n"
                f"❌ Failed: {fail}",
                parse_mode=ParseMode.HTML, reply_markup=make_back_menu()
            )
        except Exception:
            pass

    asyncio.create_task(scheduled_send())

    context.user_data.clear()
    return MAIN_MENU


async def _do_send_messages(
    session_id: str,
    groups: list,
    messages: list,
    progress_msg
) -> tuple[int, int]:
    """
    Send all messages to all groups with delays.
    Returns (ok_count, fail_count).
    """
    ok = fail = 0
    total     = len(groups) * len(messages)
    done      = 0

    for g_idx, g in enumerate(groups):
        gid  = g.get("id", "")
        name = g.get("name", g.get("subject", gid))[:25]

        for m_idx, msg in enumerate(messages):
            res = bridge.send_message_to_group(session_id, gid, msg)
            if res.get("success"):
                ok += 1
            else:
                fail += 1
            done += 1

            # Progress update every 10 messages
            if progress_msg and done % 10 == 0:
                try:
                    await progress_msg.reply_text(
                        f"📨 Sending… {done}/{total} done"
                    )
                except Exception:
                    pass

            # 2-sec delay between messages
            await asyncio.sleep(2)

        # 3-sec delay between groups
        if g_idx < len(groups) - 1:
            await asyncio.sleep(3)

    return ok, fail


# ─────────────────────────────────────────────────────────────────
# FEATURE: ADMIN PANEL
# ─────────────────────────────────────────────────────────────────
async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    if data == "adm_add_premium":
        await q.edit_message_text(
            "➕ <b>Add Premium User</b>\n\n"
            "Send the user's Telegram ID, or forward a message from them:",
            parse_mode=ParseMode.HTML, reply_markup=make_cancel_kb()
        )
        return ADMIN_ADD_PREMIUM

    if data == "adm_rm_premium":
        all_p = get_all_premium()
        if not all_p:
            await q.edit_message_text("📋 No premium users yet.", reply_markup=make_admin_menu())
            return ADMIN_MENU
        rows = []
        for p in all_p:
            rows.append([InlineKeyboardButton(
                f"❌ Remove {p['user_id']}",
                callback_data=f"rm_p_{p['user_id']}"
            )])
        rows.append([InlineKeyboardButton("🔙 Back", callback_data="adm_back")])
        await q.edit_message_text(
            "➖ <b>Remove Premium User</b>\n\nTap a user to remove:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows)
        )
        return ADMIN_REMOVE_PREM

    if data == "adm_temp":
        await q.edit_message_text(
            "⏱ <b>Grant Temporary Access</b>\n\nSend the user's Telegram ID:",
            parse_mode=ParseMode.HTML, reply_markup=make_cancel_kb()
        )
        return ADMIN_TEMP_UID

    if data == "adm_list":
        all_p = get_all_premium()
        temp  = get_active_temp_users()
        if all_p:
            lines = []
            for i, p in enumerate(all_p, 1):
                added = p.get("added_at", "")[:10]
                lines.append(f"{i}. <code>{p['user_id']}</code> — Added: {added}")
            prem_text = "\n".join(lines)
        else:
            prem_text = "(none)"
        text = (
            f"📋 <b>Premium Users</b>\n\n{prem_text}\n\n"
            f"Total premium: <b>{len(all_p)}</b>\n"
            f"Active temp access: <b>{len(temp)}</b>"
        )
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=make_admin_menu())
        return ADMIN_MENU

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

    if data == "adm_stats":
        now     = datetime.now(IST)
        mode    = get_bot_mode()
        all_p   = get_all_premium()
        temp    = get_active_temp_users()
        text = (
            f"📊 <b>Bot Statistics</b>\n\n"
            f"👥 Premium Users: <b>{len(all_p)}</b>\n"
            f"⏱ Active Temp Access: <b>{len(temp)}</b>\n"
            f"📌 Bot Mode: <b>{mode.upper()}</b>\n"
            f"🕐 Current Time: {now.strftime('%I:%M %p IST')}\n"
            f"📅 Date: {now.strftime('%d %B %Y')}\n"
            f"🤖 Bot Status: ✅ Running"
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


async def admin_add_premium_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid_admin = update.effective_user.id
    if update.message.forward_from:
        target_id = update.message.forward_from.id
    else:
        txt = update.message.text.strip()
        if not txt.isdigit():
            await update.message.reply_text(
                "❌ Invalid ID. Send a numeric Telegram ID:", reply_markup=make_cancel_kb()
            )
            return ADMIN_ADD_PREMIUM
        target_id = int(txt)

    add_premium(target_id, uid_admin)
    await update.message.reply_text(
        f"✅ User <code>{target_id}</code> added as premium user.",
        parse_mode=ParseMode.HTML,
        reply_markup=make_admin_menu()
    )
    return ADMIN_MENU


async def admin_remove_premium_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    data = q.data
    await q.answer()

    if data in ("adm_back", "cancel"):
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


async def admin_temp_uid_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if not txt.isdigit():
        await update.message.reply_text(
            "❌ Invalid ID. Send a numeric Telegram ID:", reply_markup=make_cancel_kb()
        )
        return ADMIN_TEMP_UID

    target_id = int(txt)
    context.user_data["temp_target"] = target_id
    await update.message.reply_text(
        f"⏱ Choose access duration for user <code>{target_id}</code>:",
        parse_mode=ParseMode.HTML,
        reply_markup=make_temp_duration_kb(target_id)
    )
    return ADMIN_TEMP_DURATION


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
    parts     = data.split("_")
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
# MASTER CONVERSATION HANDLER
# ─────────────────────────────────────────────────────────────────
def build_master_conv() -> ConversationHandler:
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
            # ── MAIN MENU ──────────────────────────────────────
            MAIN_MENU: [
                CallbackQueryHandler(main_menu_callback),
            ],

            # ── CONNECT ────────────────────────────────────────
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

            # ── DISCONNECT ─────────────────────────────────────
            DISC_SESSION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, disc_session_received),
                *cancel_handler,
            ],
            DISC_CONFIRM: [
                CallbackQueryHandler(disc_confirm),
            ],

            # ── CREATE GROUP (7-step) ───────────────────────────
            CG_SESSION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, cg_step1_session),
                *cancel_handler,
            ],
            CG_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, cg_step2_name),
                *cancel_handler,
            ],
            CG_DESCRIPTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, cg_step3_description),
                *cancel_handler,
            ],
            CG_PROFILE: [
                MessageHandler(filters.PHOTO | (filters.TEXT & ~filters.COMMAND), cg_step4_profile),
                *cancel_handler,
            ],
            CG_MEMBERS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, cg_step5_members),
                *cancel_handler,
            ],
            CG_WELCOME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, cg_step6_welcome),
                *cancel_handler,
            ],
            CG_APPROVAL: [
                CallbackQueryHandler(cg_step7_approval, pattern="^cg_approval_"),
                *cancel_handler,
            ],
            CG_CONFIRM: [
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
            GETLINK_SCOPE: [
                CallbackQueryHandler(getlink_scope_chosen),
            ],

            # ── LEAVE ───────────────────────────────────────────
            LEAVE_SESSION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, leave_session_received),
                *cancel_handler,
            ],
            LEAVE_SCOPE: [
                CallbackQueryHandler(leave_scope_chosen),
            ],
            LEAVE_CONFIRM: [
                CallbackQueryHandler(leave_confirm_handler),
            ],

            # ── REMOVE MEMBERS ──────────────────────────────────
            RM_SESSION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, rm_session_received),
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
            APPROVAL_SCOPE: [
                CallbackQueryHandler(approval_scope_chosen),
            ],
            APPROVAL_MODE: [
                CallbackQueryHandler(approval_mode_chosen),
            ],

            # ── PENDING ─────────────────────────────────────────
            PENDING_SESSION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, pending_session_received),
                *cancel_handler,
            ],
            PENDING_SCOPE: [
                CallbackQueryHandler(pending_scope_chosen),
            ],
            PENDING_ACTION: [
                CallbackQueryHandler(pending_action),
            ],

            # ── ADD MEMBERS ─────────────────────────────────────
            ADDM_SESSION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, addm_session_received),
                *cancel_handler,
            ],
            ADDM_NUMBERS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, addm_numbers_received),
                *cancel_handler,
            ],

            # ── SEND MESSAGE ────────────────────────────────────
            SENDMSG_SESSION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, sendmsg_session_received),
                *cancel_handler,
            ],
            SENDMSG_SCOPE: [
                CallbackQueryHandler(sendmsg_scope_chosen),
            ],
            SENDMSG_COLLECT: [
                CommandHandler("done", sendmsg_collect),
                MessageHandler(filters.TEXT & ~filters.COMMAND, sendmsg_collect),
                *cancel_handler,
            ],
            SENDMSG_CONFIRM: [
                CallbackQueryHandler(sendmsg_confirm),
            ],

            # ── INLINE GROUP SELECTOR (shared) ──────────────────
            GROUP_SELECT_STATE: [
                CallbackQueryHandler(group_selector_callback),
            ],

            # ── ADMIN PANEL ─────────────────────────────────────
            ADMIN_MENU: [
                CallbackQueryHandler(admin_remove_premium_callback, pattern=r"^(rm_p_|adm_back)"),
                CallbackQueryHandler(admin_temp_duration_callback,  pattern=r"^td_"),
                CallbackQueryHandler(admin_menu_callback),
            ],
            ADMIN_ADD_PREMIUM: [
                MessageHandler(filters.ALL & ~filters.COMMAND, admin_add_premium_received),
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
_ptb_app: Application = None   # set in main()
_event_loop = None              # shared event loop for webhook mode


@flask_app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "time": datetime.now(IST).strftime("%I:%M %p IST")}), 200


@flask_app.route("/webhook", methods=["POST"])
def webhook():
    """Receive Telegram webhook updates."""
    data = flask_request.get_json(force=True)
    if data and _ptb_app and _event_loop:
        update = Update.de_json(data, _ptb_app.bot)
        asyncio.run_coroutine_threadsafe(
            _ptb_app.process_update(update),
            _event_loop
        )
    return jsonify({"ok": True}), 200


# ─────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────
def main():
    global _ptb_app, _event_loop

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

    # Register help command (works outside ConversationHandler too)
    _ptb_app.add_handler(CommandHandler("help", help_cmd))

    # Register master conversation handler
    _ptb_app.add_handler(build_master_conv())

    async def set_commands(app: Application):
        await app.bot.set_my_commands([
            BotCommand("start",  "Start the bot / Show main menu"),
            BotCommand("help",   "Show help information"),
            BotCommand("admin",  "Admin panel (admin only)"),
            BotCommand("cancel", "Cancel current operation"),
            BotCommand("done",   "Finish collecting messages (Send Message feature)"),
        ])

    _ptb_app.post_init = set_commands

    if RENDER_URL:
        # ── WEBHOOK MODE ──────────────────────────────────────
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

        # Run PTB in background thread (dedicated event loop)
        loop = asyncio.new_event_loop()
        _event_loop = loop

        def run_ptb():
            asyncio.set_event_loop(loop)
            loop.run_until_complete(_ptb_app.initialize())
            loop.run_forever()

        t = threading.Thread(target=run_ptb, daemon=True)
        t.start()
        time.sleep(2)  # Give PTB time to initialize

        # Run Flask (blocking)
        flask_app.run(host="0.0.0.0", port=PORT)

    else:
        # ── POLLING MODE ──────────────────────────────────────
        logger.info("Starting in polling mode (no RENDER_URL set).")
        _ptb_app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
