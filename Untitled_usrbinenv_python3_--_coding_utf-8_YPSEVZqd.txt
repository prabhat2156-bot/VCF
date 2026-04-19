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
   pip install python-telegram-bot==20.7 aiohttp python-dotenv aiofiles

2. Node.js Setup (Baileys Bridge):
   mkdir whatsapp-bridge && cd whatsapp-bridge
   npm init -y
   npm install @whiskeysockets/baileys @hapi/boom pino express qrcode

3. Environment Variables (create a .env file):
   BOT_TOKEN=your_telegram_bot_token_here
   BRIDGE_URL=http://localhost:3000
   BRIDGE_SECRET=your_secret_key_here
   ADMIN_IDS=123456789,987654321

4. Run:
   Terminal 1: node bridge.js
   Terminal 2: python bot.py

5. Companion Node.js file: bridge.js (provided in comments below)
"""

# ═══════════════════════════════════════════════════════════════
#                    IMPORTS / LIBRARIES
# ═══════════════════════════════════════════════════════════════
import asyncio
import json
import logging
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

import aiohttp
from dotenv import load_dotenv
from telegram import (
    Bot,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ═══════════════════════════════════════════════════════════════
#                 CONFIGURATION
# ═══════════════════════════════════════════════════════════════
load_dotenv()

BOT_TOKEN      = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
BRIDGE_URL     = os.getenv("BRIDGE_URL", "http://localhost:3000")
BRIDGE_SECRET  = os.getenv("BRIDGE_SECRET", "your_secret_key")
ADMIN_IDS_STR  = os.getenv("ADMIN_IDS", "")
ADMIN_IDS      = [int(x.strip()) for x in ADMIN_IDS_STR.split(",") if x.strip()]
SESSIONS_DIR   = Path("sessions")
SESSIONS_DIR.mkdir(exist_ok=True)

# Logging setup
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler("bot.log")],
)
logger = logging.getLogger("WA_Manager")

# ═══════════════════════════════════════════════════════════════
#              CONVERSATION STATES
# ═══════════════════════════════════════════════════════════════
(
    # Connect / Disconnect states
    CONNECT_CHOOSE_METHOD,
    CONNECT_PHONE_INPUT,
    CONNECT_WAITING_CODE,
    DISCONNECT_SELECT,

    # Create Group states
    CG_NAME,
    CG_PHOTO,
    CG_DISAPPEAR,
    CG_PERMISSIONS,
    CG_MEMBERS,
    CG_NUM_START,
    CG_NUM_COUNT,
    CG_ACCOUNT_SELECT,

    # Join Group states
    JOIN_LINKS,
    JOIN_CONFIRM,
    JOIN_ACCOUNT,

    # CTC Checker states
    CTC_MODE,
    CTC_LINKS,
    CTC_FILES,
    CTC_PROCESSING,
    CTC_ACTION,

    # Get Link states
    GL_SCOPE,
    GL_SELECT,
    GL_ACCOUNT,

    # Leave Group states
    LG_SCOPE,
    LG_SELECT,
    LG_CONFIRM,
    LG_ACCOUNT,

    # Remove Members states
    RM_SCOPE,
    RM_SELECT,
    RM_ACCOUNT,

    # Admin states
    ADMIN_NUMBERS,
    ADMIN_ACTION,
    ADMIN_SCOPE,
    ADMIN_SELECT,
    ADMIN_ACCOUNT,

    # Approval Setting states
    APPROVAL_ACTION,
    APPROVAL_ACCOUNT,

    # Pending List states
    PL_SCOPE,
    PL_SELECT,
    PL_ACCOUNT,

    # Add Members states
    AM_LINKS,
    AM_FILES,
    AM_ACCOUNT,
) = range(44)

# ═══════════════════════════════════════════════════════════════
#           USER DATA STORAGE
# ═══════════════════════════════════════════════════════════════
# In-memory store; use Redis/SQLite in production
user_sessions: dict[int, dict] = {}   # {user_id: {account_id: session_info}}
user_temp: dict[int, dict]     = {}   # {user_id: temp conversation data}


def get_temp(user_id: int) -> dict:
    """Get temporary data for the current conversation."""
    if user_id not in user_temp:
        user_temp[user_id] = {}
    return user_temp[user_id]


def clear_temp(user_id: int):
    """Clear temporary conversation data."""
    user_temp[user_id] = {}


def get_sessions(user_id: int) -> dict:
    """Get all WhatsApp sessions for a user."""
    return user_sessions.get(user_id, {})


def save_session(user_id: int, account_id: str, info: dict):
    """Save a WhatsApp session."""
    if user_id not in user_sessions:
        user_sessions[user_id] = {}
    user_sessions[user_id][account_id] = info


def remove_session(user_id: int, account_id: str):
    """Remove a WhatsApp session."""
    if user_id in user_sessions:
        user_sessions[user_id].pop(account_id, None)


# ═══════════════════════════════════════════════════════════════
#              BRIDGE API HELPER
# ═══════════════════════════════════════════════════════════════
class BridgeAPI:
    """Class for communicating with the Node.js Baileys Bridge."""

    def __init__(self):
        self.base = BRIDGE_URL
        self.headers = {
            "Content-Type": "application/json",
            "X-Secret": BRIDGE_SECRET,
        }

    async def _request(self, method: str, endpoint: str, data: dict = None) -> dict:
        """Send an HTTP request to the bridge."""
        url = f"{self.base}{endpoint}"
        try:
            async with aiohttp.ClientSession() as session:
                kwargs = {"headers": self.headers, "timeout": aiohttp.ClientTimeout(total=60)}
                if data:
                    kwargs["json"] = data
                async with getattr(session, method)(url, **kwargs) as resp:
                    result = await resp.json()
                    return result
        except aiohttp.ClientConnectorError:
            return {"success": False, "error": "Bridge server is not running. Please start bridge.js."}
        except Exception as e:
            logger.error(f"Bridge error: {e}")
            return {"success": False, "error": str(e)}

    async def connect_qr(self, account_id: str) -> dict:
        """Connect WhatsApp via QR code."""
        return await self._request("post", "/connect/qr", {"accountId": account_id})

    async def connect_phone(self, account_id: str, phone: str) -> dict:
        """Connect via phone number pairing code."""
        return await self._request("post", "/connect/phone", {"accountId": account_id, "phone": phone})

    async def get_pairing_code(self, account_id: str) -> dict:
        """Fetch the pairing code."""
        return await self._request("get", f"/connect/pairing-code/{account_id}")

    async def disconnect(self, account_id: str) -> dict:
        """Disconnect an account."""
        return await self._request("post", "/disconnect", {"accountId": account_id})

    async def get_status(self, account_id: str) -> dict:
        """Check connection status."""
        return await self._request("get", f"/status/{account_id}")

    async def create_group(self, account_id: str, name: str, members: list) -> dict:
        """Create a WhatsApp group."""
        return await self._request("post", "/group/create", {
            "accountId": account_id, "name": name, "members": members
        })

    async def set_group_photo(self, account_id: str, group_id: str, photo_base64: str) -> dict:
        """Set the group profile photo."""
        return await self._request("post", "/group/photo", {
            "accountId": account_id, "groupId": group_id, "photo": photo_base64
        })

    async def set_disappear(self, account_id: str, group_id: str, duration: int) -> dict:
        """Set disappearing messages."""
        return await self._request("post", "/group/disappear", {
            "accountId": account_id, "groupId": group_id, "duration": duration
        })

    async def set_permissions(self, account_id: str, group_id: str, perms: dict) -> dict:
        """Set group permissions."""
        return await self._request("post", "/group/permissions", {
            "accountId": account_id, "groupId": group_id, "permissions": perms
        })

    async def get_groups(self, account_id: str) -> dict:
        """Get the list of all groups."""
        return await self._request("get", f"/groups/{account_id}")

    async def get_group_info(self, account_id: str, group_id: str) -> dict:
        """Get group info."""
        return await self._request("get", f"/group/{account_id}/{group_id}")

    async def join_group(self, account_id: str, invite_link: str) -> dict:
        """Join a group via invite link."""
        return await self._request("post", "/group/join", {
            "accountId": account_id, "link": invite_link
        })

    async def get_invite_link(self, account_id: str, group_id: str) -> dict:
        """Get the group invite link."""
        return await self._request("get", f"/group/invite/{account_id}/{group_id}")

    async def leave_group(self, account_id: str, group_id: str) -> dict:
        """Leave a group."""
        return await self._request("post", "/group/leave", {
            "accountId": account_id, "groupId": group_id
        })

    async def get_members(self, account_id: str, group_id: str) -> dict:
        """Get group members."""
        return await self._request("get", f"/group/members/{account_id}/{group_id}")

    async def remove_member(self, account_id: str, group_id: str, member_jid: str) -> dict:
        """Remove a member from the group."""
        return await self._request("post", "/group/remove-member", {
            "accountId": account_id, "groupId": group_id, "memberJid": member_jid
        })

    async def make_admin(self, account_id: str, group_id: str, member_jid: str) -> dict:
        """Promote a member to admin."""
        return await self._request("post", "/group/make-admin", {
            "accountId": account_id, "groupId": group_id, "memberJid": member_jid
        })

    async def remove_admin(self, account_id: str, group_id: str, member_jid: str) -> dict:
        """Demote an admin to regular member."""
        return await self._request("post", "/group/remove-admin", {
            "accountId": account_id, "groupId": group_id, "memberJid": member_jid
        })

    async def set_approval(self, account_id: str, group_id: str, enabled: bool) -> dict:
        """Toggle membership approval."""
        return await self._request("post", "/group/approval", {
            "accountId": account_id, "groupId": group_id, "enabled": enabled
        })

    async def get_pending(self, account_id: str, group_id: str) -> dict:
        """Get pending membership requests."""
        return await self._request("get", f"/group/pending/{account_id}/{group_id}")

    async def reject_pending(self, account_id: str, group_id: str, member_jid: str) -> dict:
        """Reject a pending membership request."""
        return await self._request("post", "/group/reject-pending", {
            "accountId": account_id, "groupId": group_id, "memberJid": member_jid
        })

    async def add_member(self, account_id: str, group_id: str, phone: str) -> dict:
        """Add a member to the group."""
        return await self._request("post", "/group/add-member", {
            "accountId": account_id, "groupId": group_id, "phone": phone
        })

    async def is_on_whatsapp(self, account_id: str, phone: str) -> dict:
        """Check if a number is on WhatsApp."""
        return await self._request("post", "/check-number", {
            "accountId": account_id, "phone": phone
        })


bridge = BridgeAPI()

# ═══════════════════════════════════════════════════════════════
#                UTILITY FUNCTIONS
# ═══════════════════════════════════════════════════════════════
def parse_vcf(content: str) -> list[str]:
    """Extract phone numbers from a VCF file."""
    phones = []
    for line in content.splitlines():
        if line.startswith("TEL"):
            parts = line.split(":")
            if len(parts) >= 2:
                num = re.sub(r"[^\d+]", "", parts[-1].strip())
                if num:
                    phones.append(num)
    return phones


def parse_numbers_text(text: str) -> list[str]:
    """Extract phone numbers from plain text."""
    numbers = []
    for token in re.split(r"[\s,;\n]+", text):
        num = re.sub(r"[^\d+]", "", token.strip())
        if len(num) >= 8:
            numbers.append(num)
    return numbers


def format_jid(phone: str) -> str:
    """Convert a phone number to WhatsApp JID format."""
    phone = re.sub(r"[^\d]", "", phone)
    if not phone.startswith("91") and len(phone) == 10:
        phone = "91" + phone
    return f"{phone}@s.whatsapp.net"


def extract_group_id_from_link(link: str) -> str:
    """Extract the group code from an invite link."""
    match = re.search(r"chat\.whatsapp\.com/([A-Za-z0-9]+)", link)
    return match.group(1) if match else link


def accounts_keyboard(sessions: dict, action_prefix: str) -> InlineKeyboardMarkup:
    """Build an inline keyboard of connected accounts."""
    buttons = []
    for acc_id, info in sessions.items():
        label = f"📱 {info.get('name', acc_id)} ({info.get('phone', 'Unknown')})"
        buttons.append([InlineKeyboardButton(label, callback_data=f"{action_prefix}:{acc_id}")])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
    return InlineKeyboardMarkup(buttons)


def make_main_menu() -> InlineKeyboardMarkup:
    """Build the main menu keyboard."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Connect WhatsApp", callback_data="main:connect"),
         InlineKeyboardButton("🔌 Disconnect WhatsApp", callback_data="main:disconnect")],
        [InlineKeyboardButton("📋 Connected Accounts", callback_data="main:accounts"),
         InlineKeyboardButton("❓ Help", callback_data="main:help")],
    ])


async def safe_edit(query: CallbackQuery, text: str, reply_markup=None, parse_mode=ParseMode.HTML):
    """Safely edit a message."""
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception as e:
        logger.warning(f"Edit failed: {e}")
        try:
            await query.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        except Exception:
            pass


async def safe_reply(message: Message, text: str, reply_markup=None, parse_mode=ParseMode.HTML):
    """Safely reply to a message."""
    try:
        return await message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception as e:
        logger.error(f"Reply failed: {e}")


# ═══════════════════════════════════════════════════════════════
#              /start COMMAND HANDLER
# ═══════════════════════════════════════════════════════════════
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /start command - Display the main menu.
    Shows 4 main options as inline buttons.
    """
    user = update.effective_user
    text = (
        f"🤖 <b>WhatsApp Group Manager</b>\n\n"
        f"Hello {user.first_name}! 👋\n\n"
        f"This bot helps you manage your WhatsApp groups.\n"
        f"Choose an option below:"
    )
    await update.message.reply_text(text, reply_markup=make_main_menu(), parse_mode=ParseMode.HTML)


async def main_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle main menu button callbacks."""
    query = update.callback_query
    await query.answer()
    action = query.data.split(":")[1]

    if action == "connect":
        return await connect_start(update, context)
    elif action == "disconnect":
        return await disconnect_start(update, context)
    elif action == "accounts":
        return await show_accounts(update, context)
    elif action == "help":
        return await show_help(update, context)
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════
#         FEATURE 4: HELP
# ═══════════════════════════════════════════════════════════════
async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show the help message."""
    query = update.callback_query
    text = (
        "❓ <b>WhatsApp Group Manager - Help Guide</b>\n\n"
        "<b>🔗 Connect WhatsApp:</b>\n"
        "  • Connect via QR Code or Phone Number\n"
        "  • Connect multiple accounts simultaneously\n\n"
        "<b>📋 Available Commands:</b>\n"
        "  /start - Main menu\n"
        "  /create - Create a group\n"
        "  /join - Join groups\n"
        "  /ctc - Contact checker\n"
        "  /getlink - Get group links\n"
        "  /leave - Leave groups\n"
        "  /remove - Remove members\n"
        "  /admin - Manage admins\n"
        "  /approval - Approval setting\n"
        "  /pending - Pending list\n"
        "  /addmembers - Add members\n\n"
        "<b>📌 Tips:</b>\n"
        "  • Send multiple VCF files one by one\n"
        "  • Separate group links with commas\n"
        "  • Provide phone numbers in +91 format\n\n"
        "<b>⚡ Powered by Baileys + python-telegram-bot</b>"
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Back to Menu", callback_data="goto:start")]])
    await safe_edit(query, text, kb)
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════
#      FEATURE 3: CONNECTED ACCOUNTS
# ═══════════════════════════════════════════════════════════════
async def show_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show all connected accounts."""
    query = update.callback_query
    user_id = query.from_user.id
    sessions = get_sessions(user_id)

    if not sessions:
        text = "📋 <b>Connected Accounts</b>\n\n❌ No accounts connected.\nPlease connect WhatsApp first."
    else:
        lines = ["📋 <b>Connected WhatsApp Accounts</b>\n"]
        for i, (acc_id, info) in enumerate(sessions.items(), 1):
            status_icon = "🟢" if info.get("connected") else "🔴"
            lines.append(
                f"{status_icon} <b>{i}. {info.get('name', 'Unknown')}</b>\n"
                f"   📞 {info.get('phone', 'N/A')}\n"
                f"   🆔 {acc_id}\n"
            )
        text = "\n".join(lines)

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Back to Menu", callback_data="goto:start")]])
    await safe_edit(query, text, kb)
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════
#       FEATURE 1: CONNECT WHATSAPP
# ═══════════════════════════════════════════════════════════════
async def connect_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Choose connection method."""
    query = update.callback_query
    text = (
        "🔗 <b>Connect WhatsApp</b>\n\n"
        "Choose a connection method:"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📱 Phone Number (Pairing Code)", callback_data="connect:phone")],
        [InlineKeyboardButton("📷 QR Code Scan", callback_data="connect:qr")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
    ])
    await safe_edit(query, text, kb)
    return CONNECT_CHOOSE_METHOD


async def connect_method_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle connection method selection."""
    query = update.callback_query
    await query.answer()
    method = query.data.split(":")[1]
    user_id = query.from_user.id
    temp = get_temp(user_id)
    temp["connect_method"] = method

    if method == "phone":
        await safe_edit(
            query,
            "📱 <b>Enter Phone Number</b>\n\n"
            "Provide the number with country code:\n"
            "Example: <code>+919876543210</code>\n\n"
            "To connect multiple accounts, connect them one at a time."
        )
        return CONNECT_PHONE_INPUT
    else:
        # QR code flow
        import uuid
        account_id = f"acc_{uuid.uuid4().hex[:8]}"
        temp["account_id"] = account_id
        loading_msg = await query.message.reply_text("⏳ Generating QR Code...")

        result = await bridge.connect_qr(account_id)
        if result.get("success"):
            qr_data = result.get("qrData", "")
            qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&data={qr_data}"
            await loading_msg.delete()
            await query.message.reply_photo(
                photo=qr_url,
                caption=(
                    "📷 <b>Scan the QR Code</b>\n\n"
                    "1. Open WhatsApp\n"
                    "2. Settings → Linked Devices → Link a Device\n"
                    "3. Scan this QR code\n\n"
                    f"⏱ Account ID: <code>{account_id}</code>\n"
                    "Scan within 2 minutes!"
                ),
                parse_mode=ParseMode.HTML,
            )
            # Poll for connection
            await asyncio.sleep(5)
            for _ in range(24):  # 2 minutes
                status = await bridge.get_status(account_id)
                if status.get("connected"):
                    name = status.get("name", account_id)
                    phone = status.get("phone", "Unknown")
                    save_session(user_id, account_id, {
                        "name": name, "phone": phone, "connected": True, "method": "qr"
                    })
                    await query.message.reply_text(
                        f"✅ <b>Connected!</b>\n\n"
                        f"📱 Name: {name}\n"
                        f"📞 Phone: {phone}\n"
                        f"🆔 Account ID: <code>{account_id}</code>",
                        reply_markup=make_main_menu(),
                        parse_mode=ParseMode.HTML,
                    )
                    clear_temp(user_id)
                    return ConversationHandler.END
                await asyncio.sleep(5)

            await query.message.reply_text(
                "⏰ QR code expired. Please try again.",
                reply_markup=make_main_menu()
            )
        else:
            await loading_msg.edit_text(f"❌ Error: {result.get('error', 'Unknown error')}")
        return ConversationHandler.END


async def connect_phone_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive phone number input."""
    user_id = update.effective_user.id
    phone = update.message.text.strip()
    temp = get_temp(user_id)

    if not re.match(r"^\+?\d{7,15}$", phone.replace(" ", "")):
        await safe_reply(update.message, "❌ Invalid phone number. Example: <code>+919876543210</code>")
        return CONNECT_PHONE_INPUT

    import uuid
    account_id = f"acc_{uuid.uuid4().hex[:8]}"
    temp["account_id"] = account_id
    temp["phone"] = phone

    loading_msg = await safe_reply(update.message, "⏳ Requesting pairing code...")
    result = await bridge.connect_phone(account_id, phone)

    if result.get("success"):
        # Wait for pairing code
        await asyncio.sleep(3)
        code_result = await bridge.get_pairing_code(account_id)
        code = code_result.get("code", "XXXX-XXXX")
        await loading_msg.edit_text(
            f"✅ <b>Pairing Code Received!</b>\n\n"
            f"📲 WhatsApp → Settings → Linked Devices\n"
            f"→ Link a Device → Link with Phone Number\n\n"
            f"🔑 <b>Code: <code>{code}</code></b>\n\n"
            f"⏱ Enter the code within 2 minutes!\n"
            f"The bot will automatically detect confirmation.",
            parse_mode=ParseMode.HTML,
        )
        # Poll for connection confirmation
        for _ in range(24):
            await asyncio.sleep(5)
            status = await bridge.get_status(account_id)
            if status.get("connected"):
                name = status.get("name", account_id)
                save_session(user_id, account_id, {
                    "name": name, "phone": phone, "connected": True, "method": "phone"
                })
                await safe_reply(
                    update.message,
                    f"✅ <b>WhatsApp Connected!</b>\n\n"
                    f"📱 {name}\n📞 {phone}\n🆔 <code>{account_id}</code>",
                    reply_markup=make_main_menu(),
                )
                clear_temp(user_id)
                return ConversationHandler.END
        await safe_reply(update.message, "⏰ Connection timed out. Please try again.", reply_markup=make_main_menu())
    else:
        await loading_msg.edit_text(f"❌ Error: {result.get('error', 'Unknown')}")

    clear_temp(user_id)
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════
#      FEATURE 2: DISCONNECT WHATSAPP
# ═══════════════════════════════════════════════════════════════
async def disconnect_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the disconnect flow."""
    query = update.callback_query
    user_id = query.from_user.id
    sessions = get_sessions(user_id)

    if not sessions:
        await safe_edit(
            query,
            "❌ No connected accounts found.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="goto:start")]])
        )
        return ConversationHandler.END

    text = "🔌 <b>Disconnect WhatsApp</b>\n\nWhich account do you want to disconnect?"
    kb = accounts_keyboard(sessions, "disconnect_acc")
    await safe_edit(query, text, kb)
    return DISCONNECT_SELECT


async def disconnect_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Disconnect the selected account."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    account_id = query.data.split(":")[1]

    loading_msg = await query.message.reply_text("⏳ Disconnecting...")
    result = await bridge.disconnect(account_id)

    if result.get("success") or "error" not in result:
        remove_session(user_id, account_id)
        await loading_msg.edit_text(
            f"✅ Account <code>{account_id}</code> has been disconnected.",
            parse_mode=ParseMode.HTML,
            reply_markup=make_main_menu(),
        )
    else:
        await loading_msg.edit_text(f"❌ Error: {result.get('error')}", reply_markup=make_main_menu())

    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════
#         FEATURE 5: CREATE GROUP
# ═══════════════════════════════════════════════════════════════

# Default permissions state
DEFAULT_PERMISSIONS = {
    "send_messages": True,
    "send_media": True,
    "send_stickers": True,
    "send_polls": True,
    "add_members": False,
    "edit_group_info": False,
}

PERM_LABELS = {
    "send_messages":   "💬 Send Messages",
    "send_media":      "🖼 Send Media",
    "send_stickers":   "😄 Send Stickers/GIFs",
    "send_polls":      "📊 Send Polls",
    "add_members":     "➕ Add Members",
    "edit_group_info": "✏️ Edit Group Info",
}

DISAPPEAR_OPTIONS = {
    "0":       "❌ Off",
    "86400":   "📅 24 Hours",
    "604800":  "📅 7 Days",
    "7776000": "📅 90 Days",
}


def permissions_keyboard(perms: dict) -> InlineKeyboardMarkup:
    """Build the permissions toggle keyboard."""
    buttons = []
    for key, label in PERM_LABELS.items():
        status = "✅" if perms.get(key, False) else "❌"
        buttons.append([InlineKeyboardButton(
            f"{status} {label}", callback_data=f"perm_toggle:{key}"
        )])
    buttons.append([
        InlineKeyboardButton("✅ Confirm Permissions", callback_data="perm_confirm"),
        InlineKeyboardButton("❌ Cancel", callback_data="cancel"),
    ])
    return InlineKeyboardMarkup(buttons)


async def create_group_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the create group flow."""
    user_id = update.effective_user.id
    sessions = get_sessions(user_id)

    if not sessions:
        await safe_reply(
            update.message,
            "❌ Please connect WhatsApp first. /start → Connect WhatsApp"
        )
        return ConversationHandler.END

    clear_temp(user_id)
    temp = get_temp(user_id)
    temp["permissions"] = DEFAULT_PERMISSIONS.copy()

    await safe_reply(
        update.message,
        "📝 <b>Create Group</b>\n\n"
        "<b>Step 1/7:</b> Enter the group name:\n\n"
        "Example: <code>My Business Group</code>\n"
        "Note: A number will be added automatically — e.g., My Business Group 1, 2, 3..."
    )
    return CG_NAME


async def cg_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive the group name."""
    user_id = update.effective_user.id
    temp = get_temp(user_id)
    temp["group_name"] = update.message.text.strip()

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏭ Skip (no photo)", callback_data="cg_photo:skip")]
    ])
    await safe_reply(
        update.message,
        f"✅ Group name: <b>{temp['group_name']}</b>\n\n"
        "<b>Step 2/7:</b> Send the group profile photo:\n"
        "(or press Skip)",
        kb
    )
    return CG_PHOTO


async def cg_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive the group photo."""
    user_id = update.effective_user.id
    temp = get_temp(user_id)

    if update.message.photo:
        # Download the photo
        photo = update.message.photo[-1]
        photo_file = await photo.get_file()
        photo_bytes = await photo_file.download_as_bytearray()
        import base64
        temp["group_photo"] = base64.b64encode(bytes(photo_bytes)).decode()
        photo_status = "✅ Photo will be set"
    else:
        temp["group_photo"] = None
        photo_status = "⏭ Photo skipped"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(opt, callback_data=f"cg_disappear:{secs}")]
        for secs, opt in DISAPPEAR_OPTIONS.items()
    ] + [[InlineKeyboardButton("⏭ Skip", callback_data="cg_disappear:skip")]])

    await safe_reply(
        update.message,
        f"✅ {photo_status}\n\n"
        "<b>Step 3/7:</b> Disappearing Messages setting:\n"
        "Choose how long before messages auto-delete:"
        , kb
    )
    return CG_DISAPPEAR


async def cg_photo_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Photo skip callback."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    get_temp(user_id)["group_photo"] = None

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(opt, callback_data=f"cg_disappear:{secs}")]
        for secs, opt in DISAPPEAR_OPTIONS.items()
    ] + [[InlineKeyboardButton("⏭ Skip", callback_data="cg_disappear:skip")]])

    await safe_edit(
        query,
        "⏭ Photo skipped\n\n"
        "<b>Step 3/7:</b> Choose Disappearing Messages setting:",
        kb
    )
    return CG_DISAPPEAR


async def cg_disappear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive the disappearing messages setting."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    temp = get_temp(user_id)
    value = query.data.split(":")[1]
    temp["disappear"] = 0 if value == "skip" else int(value)

    label = DISAPPEAR_OPTIONS.get(value, "Off") if value != "skip" else "Skipped"
    await safe_edit(
        query,
        f"✅ Disappearing: {label}\n\n"
        "<b>Step 4/7:</b> Set Group Permissions:\n"
        "Toggle each permission ON/OFF:",
        permissions_keyboard(temp["permissions"])
    )
    return CG_PERMISSIONS


async def cg_permissions_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Toggle a permission."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    temp = get_temp(user_id)
    perm_key = query.data.split(":")[1]

    if perm_key in temp["permissions"]:
        temp["permissions"][perm_key] = not temp["permissions"][perm_key]

    await query.edit_message_reply_markup(permissions_keyboard(temp["permissions"]))
    return CG_PERMISSIONS


async def cg_permissions_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Confirm the permissions selection."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏭ Skip (add later)", callback_data="cg_members:skip")]
    ])
    await safe_edit(
        query,
        "✅ Permissions set!\n\n"
        "<b>Step 5/7:</b> Add Members (Optional):\n\n"
        "Enter phone numbers (comma or newline separated):\n"
        "Example: <code>+919876543210, +918765432109</code>\n\n"
        "Or skip:",
        kb
    )
    return CG_MEMBERS


async def cg_members(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive member input."""
    user_id = update.effective_user.id
    temp = get_temp(user_id)
    numbers = parse_numbers_text(update.message.text)
    temp["initial_members"] = numbers

    await safe_reply(
        update.message,
        f"✅ {len(numbers)} members will be added.\n\n"
        "<b>Step 6/7:</b> Where should numbering start?\n\n"
        "Example: <code>1</code> (Group 1, 2, 3...)\n"
        "Or <code>50</code> (Group 50, 51, 52...)"
    )
    return CG_NUM_START


async def cg_members_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Skip adding members."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    get_temp(user_id)["initial_members"] = []

    await safe_edit(
        query,
        "⏭ Members skipped\n\n"
        "<b>Step 6/7:</b> Where should numbering start?\n"
        "Example: <code>1</code> or <code>50</code>"
    )
    return CG_NUM_START


async def cg_num_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive the starting number input."""
    user_id = update.effective_user.id
    temp = get_temp(user_id)
    text = update.message.text.strip()

    if not text.isdigit():
        await safe_reply(update.message, "❌ Please enter only a number, e.g. <code>1</code>")
        return CG_NUM_START

    temp["num_start"] = int(text)
    await safe_reply(
        update.message,
        f"✅ Numbering will start from {text}.\n\n"
        "<b>Step 7/7:</b> How many groups do you want to create?\n"
        "Example: <code>5</code>"
    )
    return CG_NUM_COUNT


async def cg_num_count(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive group count and execute creation."""
    user_id = update.effective_user.id
    text = update.message.text.strip()

    if not text.isdigit() or int(text) < 1:
        await safe_reply(update.message, "❌ Please enter a valid number, e.g. <code>3</code>")
        return CG_NUM_COUNT

    sessions = get_sessions(user_id)
    temp = get_temp(user_id)
    count = int(text)
    temp["group_count"] = count

    if len(sessions) == 1:
        temp["selected_account"] = list(sessions.keys())[0]
        return await cg_execute(update, context)
    else:
        kb = accounts_keyboard(sessions, "cg_account")
        await safe_reply(
            update.message,
            f"✅ {count} groups will be created.\n\n"
            "Which WhatsApp account should be used?",
            kb
        )
        return CG_ACCOUNT_SELECT


async def cg_account_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Account select callback."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    account_id = query.data.split(":")[1]
    get_temp(user_id)["selected_account"] = account_id
    return await cg_execute(update, context)


async def cg_execute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Create groups one by one."""
    user_id = update.effective_user.id
    temp = get_temp(user_id)
    account_id = temp["selected_account"]
    base_name = temp["group_name"]
    count = temp["group_count"]
    start_num = temp["num_start"]
    members = temp.get("initial_members", [])
    photo_b64 = temp.get("group_photo")
    disappear = temp.get("disappear", 0)
    permissions = temp.get("permissions", {})

    msg = update.message or update.callback_query.message
    status_msg = await msg.reply_text(
        f"⚙️ <b>Creating groups...</b>\n\n"
        f"Total: {count} groups\n"
        f"Account: <code>{account_id}</code>",
        parse_mode=ParseMode.HTML,
    )

    created = []
    failed = []

    for i in range(count):
        num = start_num + i
        group_name = f"{base_name} {num}"

        try:
            # Step 1: Create the group
            result = await bridge.create_group(account_id, group_name, members)

            if not result.get("success"):
                failed.append(f"{group_name}: {result.get('error', 'Unknown')}")
                await status_msg.edit_text(
                    f"⚙️ Progress: {i + 1}/{count}\n"
                    f"❌ Failed: {group_name}\n"
                    f"✅ Created: {len(created)}",
                    parse_mode=ParseMode.HTML,
                )
                await asyncio.sleep(2)
                continue

            group_id = result.get("groupId")

            # Step 2: Set the photo (if provided)
            if photo_b64 and group_id:
                await bridge.set_group_photo(account_id, group_id, photo_b64)
                await asyncio.sleep(1)

            # Step 3: Set disappearing messages
            if disappear and disappear > 0 and group_id:
                await bridge.set_disappear(account_id, group_id, disappear)
                await asyncio.sleep(1)

            # Step 4: Set permissions
            if group_id:
                await bridge.set_permissions(account_id, group_id, permissions)
                await asyncio.sleep(1)

            created.append(group_name)

            # Update progress
            await status_msg.edit_text(
                f"⚙️ <b>Progress: {i + 1}/{count}</b>\n"
                f"✅ Created: {group_name}\n"
                f"📊 Done: {len(created)} | Failed: {len(failed)}",
                parse_mode=ParseMode.HTML,
            )

        except Exception as e:
            failed.append(f"{group_name}: {str(e)}")
            logger.error(f"Group create error: {e}")

        await asyncio.sleep(2)  # Rate limiting

    # Final summary
    summary = (
        f"🎉 <b>Group creation complete!</b>\n\n"
        f"✅ Successfully Created: <b>{len(created)}</b>\n"
        f"❌ Failed: <b>{len(failed)}</b>\n\n"
    )
    if created:
        summary += "<b>Created Groups:</b>\n" + "\n".join(f"• {g}" for g in created[:10])
        if len(created) > 10:
            summary += f"\n... and {len(created) - 10} more groups"
    if failed:
        summary += "\n\n<b>Failed:</b>\n" + "\n".join(f"• {f}" for f in failed[:5])

    await status_msg.edit_text(summary, parse_mode=ParseMode.HTML, reply_markup=make_main_menu())
    clear_temp(user_id)
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════
#         FEATURE 6: JOIN GROUPS
# ═══════════════════════════════════════════════════════════════
async def join_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the join groups flow."""
    user_id = update.effective_user.id
    sessions = get_sessions(user_id)

    if not sessions:
        await safe_reply(update.message, "❌ Please connect WhatsApp first.")
        return ConversationHandler.END

    clear_temp(user_id)
    await safe_reply(
        update.message,
        "🔗 <b>Join Groups</b>\n\n"
        "Paste group invite links:\n"
        "(one link per line, or comma-separated)\n\n"
        "Example:\n"
        "<code>https://chat.whatsapp.com/ABC123\n"
        "https://chat.whatsapp.com/XYZ789</code>"
    )
    return JOIN_LINKS


async def join_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive links and ask for confirmation."""
    user_id = update.effective_user.id
    temp = get_temp(user_id)
    text = update.message.text.strip()

    links = [l.strip() for l in re.split(r"[\n,]+", text) if "chat.whatsapp.com" in l]
    if not links:
        await safe_reply(update.message, "❌ No valid WhatsApp group links found. Please try again.")
        return JOIN_LINKS

    temp["join_links"] = links
    sessions = get_sessions(user_id)

    if len(sessions) == 1:
        temp["selected_account"] = list(sessions.keys())[0]
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirm & Join", callback_data="join_confirm:yes"),
             InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
        ])
        await safe_reply(
            update.message,
            f"📋 <b>{len(links)} links to join:</b>\n\n"
            + "\n".join(f"{i + 1}. <code>{l}</code>" for i, l in enumerate(links[:10]))
            + ("\\n..." if len(links) > 10 else "")
            + "\n\nConfirm?",
            kb
        )
        return JOIN_CONFIRM
    else:
        kb = accounts_keyboard(sessions, "join_account")
        await safe_reply(update.message, f"✅ {len(links)} links found.\n\nWhich account should join?", kb)
        return JOIN_ACCOUNT


async def join_account_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Select account for joining."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    account_id = query.data.split(":")[1]
    temp = get_temp(user_id)
    temp["selected_account"] = account_id
    links = temp["join_links"]

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirm & Join", callback_data="join_confirm:yes"),
         InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
    ])
    await safe_edit(
        query,
        f"📋 <b>{len(links)} links will be joined:</b>\n\n"
        + "\n".join(f"{i + 1}. <code>{l}</code>" for i, l in enumerate(links[:10]))
        + "\n\nConfirm?",
        kb
    )
    return JOIN_CONFIRM


async def join_execute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Join links one by one."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    temp = get_temp(user_id)
    account_id = temp["selected_account"]
    links = temp["join_links"]

    status_msg = await query.message.reply_text(f"⏳ Joining {len(links)} groups...")

    joined, failed = [], []
    for i, link in enumerate(links):
        result = await bridge.join_group(account_id, link)
        if result.get("success"):
            joined.append(link)
        else:
            failed.append(f"{link}: {result.get('error', 'Failed')}")

        await status_msg.edit_text(
            f"⚙️ Progress: {i + 1}/{len(links)}\n✅ Joined: {len(joined)} | ❌ Failed: {len(failed)}"
        )
        await asyncio.sleep(3)

    await status_msg.edit_text(
        f"✅ <b>Join complete!</b>\n\n"
        f"✅ Joined: {len(joined)}\n❌ Failed: {len(failed)}\n"
        + ("\n\n<b>Failed Links:</b>\n" + "\n".join(failed[:5]) if failed else ""),
        parse_mode=ParseMode.HTML,
        reply_markup=make_main_menu(),
    )
    clear_temp(user_id)
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════
#     FEATURE 7: CTC CHECKER (CONTACT CHECKER)
# ═══════════════════════════════════════════════════════════════
async def ctc_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the CTC Checker."""
    user_id = update.effective_user.id
    sessions = get_sessions(user_id)

    if not sessions:
        await safe_reply(update.message, "❌ Please connect WhatsApp first.")
        return ConversationHandler.END

    clear_temp(user_id)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏳ Pending Request Check", callback_data="ctc_mode:pending")],
        [InlineKeyboardButton("👥 Member Check", callback_data="ctc_mode:member")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
    ])
    await safe_reply(
        update.message,
        "🔍 <b>CTC Checker (Contact Check)</b>\n\nSelect mode:",
        kb
    )
    return CTC_MODE


async def ctc_mode_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Select CTC mode."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    mode = query.data.split(":")[1]
    get_temp(user_id)["ctc_mode"] = mode

    sessions = get_sessions(user_id)
    if len(sessions) == 1:
        get_temp(user_id)["selected_account"] = list(sessions.keys())[0]

    await safe_edit(
        query,
        f"✅ Mode: {'Pending Request' if mode == 'pending' else 'Member'} Check\n\n"
        "Paste group links:\n"
        "(multiple links, separated by newline)"
    )
    return CTC_LINKS


async def ctc_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive CTC links."""
    user_id = update.effective_user.id
    temp = get_temp(user_id)
    text = update.message.text.strip()

    links = [l.strip() for l in re.split(r"[\n,]+", text) if l.strip()]
    temp["ctc_links"] = links

    await safe_reply(
        update.message,
        f"✅ {len(links)} links received.\n\n"
        "Now send contact .vcf file(s):\n"
        "(Send multiple files one by one, then type /done)"
    )
    temp["ctc_vcf_numbers"] = []
    return CTC_FILES


async def ctc_files(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive VCF files."""
    user_id = update.effective_user.id
    temp = get_temp(user_id)

    if update.message.document:
        doc = update.message.document
        if doc.file_name and doc.file_name.endswith(".vcf"):
            file = await doc.get_file()
            content = (await file.download_as_bytearray()).decode("utf-8", errors="ignore")
            numbers = parse_vcf(content)
            temp["ctc_vcf_numbers"].extend(numbers)
            await safe_reply(
                update.message,
                f"✅ File: {doc.file_name}\n📞 {len(numbers)} numbers found.\n"
                f"Total: {len(temp['ctc_vcf_numbers'])}\n\n"
                "Send more files or type /done."
            )
        else:
            await safe_reply(update.message, "❌ Only .vcf files are supported.")
    return CTC_FILES


async def ctc_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start CTC processing."""
    user_id = update.effective_user.id
    temp = get_temp(user_id)
    contact_numbers = set(re.sub(r"[^\d]", "", n) for n in temp.get("ctc_vcf_numbers", []))

    if not contact_numbers:
        await safe_reply(update.message, "❌ No contact numbers found. Please send the file again.")
        return CTC_FILES

    sessions = get_sessions(user_id)
    account_id = temp.get("selected_account", list(sessions.keys())[0])
    links = temp["ctc_links"]
    mode = temp["ctc_mode"]

    status_msg = await safe_reply(
        update.message,
        f"🔍 <b>Starting check...</b>\n\n"
        f"Groups: {len(links)}\n"
        f"Contacts: {len(contact_numbers)}\n"
        f"Mode: {'Pending' if mode == 'pending' else 'Member'}"
    )

    unknown_members = []  # {group_id, group_name, member_jid, phone}

    for link in links:
        group_id = extract_group_id_from_link(link)
        group_info = await bridge.get_group_info(account_id, group_id)
        group_name = group_info.get("name", group_id) if group_info.get("success") else group_id

        if mode == "pending":
            result = await bridge.get_pending(account_id, group_id)
            members_list = result.get("pending", []) if result.get("success") else []
        else:
            result = await bridge.get_members(account_id, group_id)
            members_list = result.get("members", []) if result.get("success") else []

        for member in members_list:
            jid = member.get("jid", "")
            phone = re.sub(r"[^\d]", "", jid.replace("@s.whatsapp.net", ""))
            if phone not in contact_numbers:
                unknown_members.append({
                    "group_id": group_id,
                    "group_name": group_name,
                    "jid": jid,
                    "phone": phone,
                })

        await asyncio.sleep(1)

    temp["unknown_members"] = unknown_members

    if not unknown_members:
        await status_msg.edit_text(
            "✅ <b>Check complete!</b>\n\nAll members are in your contacts. No unknowns found.",
            parse_mode=ParseMode.HTML,
            reply_markup=make_main_menu(),
        )
        return ConversationHandler.END

    # Show unknown members
    summary = f"⚠️ <b>{len(unknown_members)} Unknown Members Found:</b>\n\n"
    for i, m in enumerate(unknown_members[:15], 1):
        summary += f"{i}. +{m['phone']} ({m['group_name']})\n"
    if len(unknown_members) > 15:
        summary += f"... and {len(unknown_members) - 15} more\n"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 Remove ALL from ALL Groups", callback_data="ctc_action:remove_all")],
        [InlineKeyboardButton("❌ Reject ALL Pending (Pending mode)", callback_data="ctc_action:reject_all")],
        [InlineKeyboardButton("✅ Keep All (Skip)", callback_data="ctc_action:skip")],
    ])

    await status_msg.edit_text(summary, parse_mode=ParseMode.HTML, reply_markup=kb)
    return CTC_ACTION


async def ctc_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Perform the selected CTC action."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    temp = get_temp(user_id)
    action = query.data.split(":")[1]
    sessions = get_sessions(user_id)
    account_id = temp.get("selected_account", list(sessions.keys())[0])
    unknown = temp.get("unknown_members", [])
    mode = temp.get("ctc_mode", "member")

    if action == "skip":
        await safe_edit(query, "✅ Skipped. No action taken.", make_main_menu())
        return ConversationHandler.END

    status_msg = await query.message.reply_text(f"⏳ Processing {len(unknown)} members...")
    done, errors = 0, 0

    for m in unknown:
        try:
            if action == "remove_all":
                result = await bridge.remove_member(account_id, m["group_id"], m["jid"])
            elif action == "reject_all" and mode == "pending":
                result = await bridge.reject_pending(account_id, m["group_id"], m["jid"])
            else:
                result = {"success": True}

            if result.get("success"):
                done += 1
            else:
                errors += 1
        except Exception as e:
            errors += 1
            logger.error(f"CTC action error: {e}")

        await asyncio.sleep(1)

    await status_msg.edit_text(
        f"✅ <b>Action complete!</b>\n\n"
        f"✅ Done: {done}\n❌ Errors: {errors}",
        parse_mode=ParseMode.HTML,
        reply_markup=make_main_menu(),
    )
    clear_temp(user_id)
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════
#        FEATURE 8: GET LINK
# ═══════════════════════════════════════════════════════════════
async def getlink_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the get invite links flow."""
    user_id = update.effective_user.id
    sessions = get_sessions(user_id)

    if not sessions:
        await safe_reply(update.message, "❌ Please connect WhatsApp first.")
        return ConversationHandler.END

    clear_temp(user_id)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 All Groups", callback_data="gl_scope:all")],
        [InlineKeyboardButton("☑️ Select Groups", callback_data="gl_scope:select")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
    ])
    await safe_reply(update.message, "🔗 <b>Get Invite Links</b>\n\nChoose scope:", kb)
    return GL_SCOPE


async def gl_scope(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Scope select callback."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    scope = query.data.split(":")[1]
    temp = get_temp(user_id)
    temp["gl_scope"] = scope
    sessions = get_sessions(user_id)

    if len(sessions) == 1:
        temp["selected_account"] = list(sessions.keys())[0]
        return await gl_fetch(update, context)
    else:
        kb = accounts_keyboard(sessions, "gl_account")
        await safe_edit(query, "Which account should the links be fetched from?", kb)
        return GL_ACCOUNT


async def gl_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Account select for get link."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    get_temp(user_id)["selected_account"] = query.data.split(":")[1]
    return await gl_fetch(update, context)


async def gl_fetch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Fetch groups and display their links."""
    user_id = update.effective_user.id if update.effective_user else update.callback_query.from_user.id
    temp = get_temp(user_id)
    account_id = temp["selected_account"]
    msg = update.callback_query.message if update.callback_query else update.message

    status = await msg.reply_text("⏳ Fetching groups...")
    result = await bridge.get_groups(account_id)

    if not result.get("success"):
        await status.edit_text(f"❌ Error: {result.get('error')}")
        return ConversationHandler.END

    groups = result.get("groups", [])
    output_lines = [f"🔗 <b>Group Invite Links ({len(groups)} groups)</b>\n"]

    for i, group in enumerate(groups, 1):
        group_id = group.get("id", "")
        name = group.get("name", "Unknown")
        link_result = await bridge.get_invite_link(account_id, group_id)
        link = link_result.get("link", "N/A") if link_result.get("success") else "N/A"
        output_lines.append(f"{i:02d}. <b>{name}</b>\n    🔗 {link}\n")
        await asyncio.sleep(0.5)

    full_text = "\n".join(output_lines)

    # Split if too long
    if len(full_text) > 4000:
        chunks = [full_text[i:i + 4000] for i in range(0, len(full_text), 4000)]
        await status.edit_text(chunks[0], parse_mode=ParseMode.HTML)
        for chunk in chunks[1:]:
            await msg.reply_text(chunk, parse_mode=ParseMode.HTML)
    else:
        await status.edit_text(full_text, parse_mode=ParseMode.HTML, reply_markup=make_main_menu())

    clear_temp(user_id)
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════
#       FEATURE 9: LEAVE GROUPS
# ═══════════════════════════════════════════════════════════════
async def leave_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the leave groups flow."""
    user_id = update.effective_user.id
    sessions = get_sessions(user_id)

    if not sessions:
        await safe_reply(update.message, "❌ Please connect WhatsApp first.")
        return ConversationHandler.END

    clear_temp(user_id)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Leave ALL Groups", callback_data="lg_scope:all")],
        [InlineKeyboardButton("☑️ Select Groups", callback_data="lg_scope:select")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
    ])
    await safe_reply(update.message, "🚪 <b>Leave Groups</b>\n\nChoose scope:", kb)
    return LG_SCOPE


async def lg_scope(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Select leave scope."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    scope = query.data.split(":")[1]
    temp = get_temp(user_id)
    temp["lg_scope"] = scope
    sessions = get_sessions(user_id)

    if len(sessions) == 1:
        temp["selected_account"] = list(sessions.keys())[0]
        return await lg_fetch_and_confirm(update, context)
    else:
        kb = accounts_keyboard(sessions, "lg_account")
        await safe_edit(query, "Which account's groups do you want to leave?", kb)
        return LG_ACCOUNT


async def lg_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Account select for leaving."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    get_temp(user_id)["selected_account"] = query.data.split(":")[1]
    return await lg_fetch_and_confirm(update, context)


async def lg_fetch_and_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Fetch groups and ask for confirmation."""
    query = update.callback_query
    user_id = query.from_user.id
    temp = get_temp(user_id)
    account_id = temp["selected_account"]

    result = await bridge.get_groups(account_id)
    if not result.get("success"):
        await safe_edit(query, f"❌ Error: {result.get('error')}")
        return ConversationHandler.END

    groups = result.get("groups", [])
    temp["lg_groups"] = groups

    group_list = "\n".join(f"{i + 1}. {g.get('name', 'Unknown')}" for i, g in enumerate(groups[:15]))
    if len(groups) > 15:
        group_list += f"\n... and {len(groups) - 15} more groups"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✅ Leave All {len(groups)} Groups", callback_data="lg_confirm:yes")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
    ])
    await safe_edit(
        query,
        f"⚠️ <b>Please Confirm!</b>\n\nYou will leave all the following groups:\n\n{group_list}\n\n"
        f"Total: <b>{len(groups)}</b> groups",
        kb
    )
    return LG_CONFIRM


async def lg_execute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Leave the selected groups."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    temp = get_temp(user_id)
    account_id = temp["selected_account"]
    groups = temp["lg_groups"]

    status_msg = await query.message.reply_text(f"⏳ Leaving {len(groups)} groups...")
    left, failed = 0, 0

    for i, group in enumerate(groups):
        group_id = group.get("id", "")
        result = await bridge.leave_group(account_id, group_id)
        if result.get("success"):
            left += 1
        else:
            failed += 1

        await status_msg.edit_text(
            f"⚙️ Progress: {i + 1}/{len(groups)}\n"
            f"✅ Left: {left} | ❌ Failed: {failed}"
        )
        await asyncio.sleep(2)

    await status_msg.edit_text(
        f"✅ <b>Leave complete!</b>\n\n✅ Left: {left}\n❌ Failed: {failed}",
        parse_mode=ParseMode.HTML,
        reply_markup=make_main_menu(),
    )
    clear_temp(user_id)
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════
#      FEATURE 10: REMOVE MEMBERS
# ═══════════════════════════════════════════════════════════════
async def remove_members_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the remove all members flow."""
    user_id = update.effective_user.id
    sessions = get_sessions(user_id)

    if not sessions:
        await safe_reply(update.message, "❌ Please connect WhatsApp first.")
        return ConversationHandler.END

    clear_temp(user_id)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 All Groups", callback_data="rm_scope:all")],
        [InlineKeyboardButton("☑️ Select Groups", callback_data="rm_scope:select")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
    ])
    await safe_reply(
        update.message,
        "🗑 <b>Remove All Members</b>\n\n"
        "⚠️ This will remove all members from the selected groups!\n\n"
        "Choose scope:",
        kb
    )
    return RM_SCOPE


async def rm_scope(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Select remove members scope."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    get_temp(user_id)["rm_scope"] = query.data.split(":")[1]
    sessions = get_sessions(user_id)

    if len(sessions) == 1:
        get_temp(user_id)["selected_account"] = list(sessions.keys())[0]
        return await rm_execute(update, context)
    else:
        kb = accounts_keyboard(sessions, "rm_account")
        await safe_edit(query, "Which account's groups should members be removed from?", kb)
        return RM_ACCOUNT


async def rm_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Account select for remove members."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    get_temp(user_id)["selected_account"] = query.data.split(":")[1]
    return await rm_execute(update, context)


async def rm_execute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Remove members from groups."""
    query = update.callback_query
    user_id = query.from_user.id
    temp = get_temp(user_id)
    account_id = temp["selected_account"]

    msg = query.message
    result = await bridge.get_groups(account_id)
    if not result.get("success"):
        await msg.reply_text(f"❌ Error fetching groups: {result.get('error')}")
        return ConversationHandler.END

    groups = result.get("groups", [])
    status_msg = await msg.reply_text(f"⏳ Processing {len(groups)} groups...")
    total_removed = 0

    for i, group in enumerate(groups):
        group_id = group.get("id", "")
        group_name = group.get("name", "Unknown")

        members_result = await bridge.get_members(account_id, group_id)
        members = members_result.get("members", []) if members_result.get("success") else []

        # Remove all non-admin, non-self members
        removable = [m for m in members if not m.get("isAdmin", False) and not m.get("isSelf", False)]

        for member in removable:
            jid = member.get("jid", "")
            await bridge.remove_member(account_id, group_id, jid)
            total_removed += 1
            await asyncio.sleep(0.5)

        await status_msg.edit_text(
            f"⚙️ Group {i + 1}/{len(groups)}: {group_name}\n"
            f"Removed from this group: {len(removable)}\n"
            f"Total removed: {total_removed}"
        )
        await asyncio.sleep(2)

    await status_msg.edit_text(
        f"✅ <b>Removal complete!</b>\n\nTotal Removed: {total_removed}",
        parse_mode=ParseMode.HTML,
        reply_markup=make_main_menu(),
    )
    clear_temp(user_id)
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════
#    FEATURE 11: MAKE / REMOVE ADMIN
# ═══════════════════════════════════════════════════════════════
async def admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the admin management flow."""
    user_id = update.effective_user.id
    sessions = get_sessions(user_id)

    if not sessions:
        await safe_reply(update.message, "❌ Please connect WhatsApp first.")
        return ConversationHandler.END

    clear_temp(user_id)
    await safe_reply(
        update.message,
        "👑 <b>Admin Management</b>\n\n"
        "Enter phone number(s):\n"
        "(Multiple: comma or newline separated)\n\n"
        "Example:\n<code>+919876543210\n+918765432109</code>"
    )
    return ADMIN_NUMBERS


async def admin_numbers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive admin phone numbers."""
    user_id = update.effective_user.id
    temp = get_temp(user_id)
    numbers = parse_numbers_text(update.message.text)

    if not numbers:
        await safe_reply(update.message, "❌ No valid phone numbers found. Please try again.")
        return ADMIN_NUMBERS

    temp["admin_numbers"] = numbers
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👑 Make Admin", callback_data="admin_action:make")],
        [InlineKeyboardButton("❌ Remove Admin", callback_data="admin_action:remove")],
        [InlineKeyboardButton("🚫 Cancel", callback_data="cancel")],
    ])
    await safe_reply(
        update.message,
        f"✅ {len(numbers)} numbers:\n"
        + "\n".join(f"• +{n}" for n in numbers[:10])
        + "\n\nChoose action:",
        kb
    )
    return ADMIN_ACTION


async def admin_action_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Select admin action."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    get_temp(user_id)["admin_action"] = query.data.split(":")[1]

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 All Groups", callback_data="admin_scope:all")],
        [InlineKeyboardButton("☑️ Select Groups", callback_data="admin_scope:select")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
    ])
    await safe_edit(query, "Choose scope:", kb)
    return ADMIN_SCOPE


async def admin_scope_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Select admin scope."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    get_temp(user_id)["admin_scope"] = query.data.split(":")[1]
    sessions = get_sessions(user_id)

    if len(sessions) == 1:
        get_temp(user_id)["selected_account"] = list(sessions.keys())[0]
        return await admin_execute(update, context)
    else:
        kb = accounts_keyboard(sessions, "admin_account")
        await safe_edit(query, "Which account's groups should the action be performed on?", kb)
        return ADMIN_ACCOUNT


async def admin_account_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Account select for admin action."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    get_temp(user_id)["selected_account"] = query.data.split(":")[1]
    return await admin_execute(update, context)


async def admin_execute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Perform the admin action."""
    query = update.callback_query
    user_id = query.from_user.id
    temp = get_temp(user_id)
    account_id = temp["selected_account"]
    numbers = temp["admin_numbers"]
    action = temp["admin_action"]

    msg = query.message
    result = await bridge.get_groups(account_id)
    if not result.get("success"):
        await msg.reply_text(f"❌ Error: {result.get('error')}")
        return ConversationHandler.END

    groups = result.get("groups", [])
    status_msg = await msg.reply_text(f"⏳ Starting admin action... {len(groups)} groups")

    success_count, skip_count, error_count = 0, 0, 0

    for group in groups:
        group_id = group.get("id", "")
        members_result = await bridge.get_members(account_id, group_id)
        members = members_result.get("members", []) if members_result.get("success") else []
        member_jids = {re.sub(r"[^\d]", "", m.get("jid", "").replace("@s.whatsapp.net", "")): m for m in members}

        for num in numbers:
            clean_num = re.sub(r"[^\d]", "", num)
            member = member_jids.get(clean_num)

            if not member:
                skip_count += 1  # Member is not in this group
                continue

            jid = member.get("jid", "")
            is_admin = member.get("isAdmin", False)

            if action == "make" and is_admin:
                skip_count += 1  # Already admin
                continue
            elif action == "remove" and not is_admin:
                skip_count += 1  # Already not admin
                continue

            if action == "make":
                res = await bridge.make_admin(account_id, group_id, jid)
            else:
                res = await bridge.remove_admin(account_id, group_id, jid)

            if res.get("success"):
                success_count += 1
            else:
                error_count += 1

            await asyncio.sleep(0.5)

        await asyncio.sleep(1)

    action_label = "Promoted to Admin" if action == "make" else "Demoted from Admin"
    await status_msg.edit_text(
        f"✅ <b>Admin action complete!</b>\n\n"
        f"✅ {action_label}: {success_count}\n"
        f"⏭ Skipped (already set): {skip_count}\n"
        f"❌ Errors: {error_count}",
        parse_mode=ParseMode.HTML,
        reply_markup=make_main_menu(),
    )
    clear_temp(user_id)
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════
#      FEATURE 12: APPROVAL SETTING
# ═══════════════════════════════════════════════════════════════
async def approval_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the approval setting flow."""
    user_id = update.effective_user.id
    sessions = get_sessions(user_id)

    if not sessions:
        await safe_reply(update.message, "❌ Please connect WhatsApp first.")
        return ConversationHandler.END

    clear_temp(user_id)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Turn ON Approval (All Groups)", callback_data="approval:on")],
        [InlineKeyboardButton("❌ Turn OFF Approval (All Groups)", callback_data="approval:off")],
        [InlineKeyboardButton("🚫 Cancel", callback_data="cancel")],
    ])
    await safe_reply(
        update.message,
        "🔐 <b>Approval Setting</b>\n\n"
        "Should new members require approval to join?\n\n"
        "Choose action:",
        kb
    )
    return APPROVAL_ACTION


async def approval_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Select approval action."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    enabled = query.data.split(":")[1] == "on"
    get_temp(user_id)["approval_enabled"] = enabled
    sessions = get_sessions(user_id)

    if len(sessions) == 1:
        get_temp(user_id)["selected_account"] = list(sessions.keys())[0]
        return await approval_execute(update, context)
    else:
        kb = accounts_keyboard(sessions, "approval_account")
        await safe_edit(query, "Which account's groups should the setting be changed on?", kb)
        return APPROVAL_ACCOUNT


async def approval_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Account select for approval."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    get_temp(user_id)["selected_account"] = query.data.split(":")[1]
    return await approval_execute(update, context)


async def approval_execute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Apply approval setting to all groups."""
    query = update.callback_query
    user_id = query.from_user.id
    temp = get_temp(user_id)
    account_id = temp["selected_account"]
    enabled = temp["approval_enabled"]

    msg = query.message
    result = await bridge.get_groups(account_id)
    if not result.get("success"):
        await msg.reply_text(f"❌ Error: {result.get('error')}")
        return ConversationHandler.END

    groups = result.get("groups", [])
    status_msg = await msg.reply_text(f"⏳ Changing approval setting for {len(groups)} groups...")

    success, failed = 0, 0
    for i, group in enumerate(groups):
        group_id = group.get("id", "")
        res = await bridge.set_approval(account_id, group_id, enabled)
        if res.get("success"):
            success += 1
        else:
            failed += 1

        await status_msg.edit_text(
            f"⚙️ Progress: {i + 1}/{len(groups)}\n✅ Done: {success} | ❌ Failed: {failed}"
        )
        await asyncio.sleep(1)

    status = "ON ✅" if enabled else "OFF ❌"
    await status_msg.edit_text(
        f"✅ <b>Approval setting complete!</b>\n\n"
        f"Setting: <b>{status}</b>\n"
        f"✅ Success: {success}\n❌ Failed: {failed}",
        parse_mode=ParseMode.HTML,
        reply_markup=make_main_menu(),
    )
    clear_temp(user_id)
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════
#      FEATURE 13: GET PENDING LIST
# ═══════════════════════════════════════════════════════════════
async def pending_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the pending list flow."""
    user_id = update.effective_user.id
    sessions = get_sessions(user_id)

    if not sessions:
        await safe_reply(update.message, "❌ Please connect WhatsApp first.")
        return ConversationHandler.END

    clear_temp(user_id)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 All Groups", callback_data="pl_scope:all")],
        [InlineKeyboardButton("☑️ Select Groups", callback_data="pl_scope:select")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
    ])
    await safe_reply(update.message, "⏳ <b>Pending Members List</b>\n\nChoose scope:", kb)
    return PL_SCOPE


async def pl_scope(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Select pending list scope."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    get_temp(user_id)["pl_scope"] = query.data.split(":")[1]
    sessions = get_sessions(user_id)

    if len(sessions) == 1:
        get_temp(user_id)["selected_account"] = list(sessions.keys())[0]
        return await pl_fetch(update, context)
    else:
        kb = accounts_keyboard(sessions, "pl_account")
        await safe_edit(query, "Which account should the pending list be fetched from?", kb)
        return PL_ACCOUNT


async def pl_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Account select for pending list."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    get_temp(user_id)["selected_account"] = query.data.split(":")[1]
    return await pl_fetch(update, context)


async def pl_fetch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Fetch the pending list."""
    query = update.callback_query
    user_id = query.from_user.id
    temp = get_temp(user_id)
    account_id = temp["selected_account"]

    msg = query.message
    result = await bridge.get_groups(account_id)
    if not result.get("success"):
        await msg.reply_text(f"❌ Error: {result.get('error')}")
        return ConversationHandler.END

    groups = result.get("groups", [])
    status_msg = await msg.reply_text(f"⏳ Fetching pending list for {len(groups)} groups...")

    total_pending = 0
    output_lines = ["⏳ <b>Pending Members Report</b>\n"]
    output_lines.append(f"{'Group Name':<30} | {'Pending':>7}")
    output_lines.append("─" * 42)

    for group in groups:
        group_id = group.get("id", "")
        name = group.get("name", "Unknown")[:28]
        pending_result = await bridge.get_pending(account_id, group_id)
        pending_list = pending_result.get("pending", []) if pending_result.get("success") else []
        count = len(pending_list)
        total_pending += count
        output_lines.append(f"{name:<30} | {count:>7}")
        await asyncio.sleep(0.5)

    output_lines.append("─" * 42)
    output_lines.append(f"{'TOTAL PENDING':<30} | {total_pending:>7}")

    full_text = "\n".join(output_lines)
    final_text = f"<pre>{full_text}</pre>\n\n📊 <b>Total Pending: {total_pending}</b>"

    await status_msg.edit_text(final_text, parse_mode=ParseMode.HTML, reply_markup=make_main_menu())
    clear_temp(user_id)
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════
#       FEATURE 14: ADD MEMBERS
# ═══════════════════════════════════════════════════════════════
async def addmembers_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the add members flow."""
    user_id = update.effective_user.id
    sessions = get_sessions(user_id)

    if not sessions:
        await safe_reply(update.message, "❌ Please connect WhatsApp first.")
        return ConversationHandler.END

    clear_temp(user_id)
    temp = get_temp(user_id)
    temp["am_links"] = []
    temp["am_numbers"] = []

    await safe_reply(
        update.message,
        "➕ <b>Add Members</b>\n\n"
        "Paste group invite link(s):\n"
        "(Multiple links separated by newline)\n\n"
        "Example:\n<code>https://chat.whatsapp.com/ABC123\nhttps://chat.whatsapp.com/XYZ789</code>"
    )
    return AM_LINKS


async def am_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive group links."""
    user_id = update.effective_user.id
    temp = get_temp(user_id)
    text = update.message.text.strip()
    links = [l.strip() for l in re.split(r"[\n,]+", text) if l.strip()]
    temp["am_links"] = links

    await safe_reply(
        update.message,
        f"✅ {len(links)} group links received.\n\n"
        "Now send numbers/files:\n"
        "• Send .vcf contact files\n"
        "• Or type numbers directly\n"
        "• Multiple files are supported\n\n"
        "After sending all files/numbers, type /addnow."
    )
    return AM_FILES


async def am_files(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive numbers or files."""
    user_id = update.effective_user.id
    temp = get_temp(user_id)

    if update.message.document:
        doc = update.message.document
        if doc.file_name and doc.file_name.endswith(".vcf"):
            file = await doc.get_file()
            content = (await file.download_as_bytearray()).decode("utf-8", errors="ignore")
            numbers = parse_vcf(content)
            temp["am_numbers"].extend(numbers)
            await safe_reply(
                update.message,
                f"✅ {doc.file_name}: {len(numbers)} numbers\n"
                f"Total: {len(temp['am_numbers'])}\n\n"
                "Send more files or type /addnow."
            )
    elif update.message.text and not update.message.text.startswith("/"):
        numbers = parse_numbers_text(update.message.text)
        temp["am_numbers"].extend(numbers)
        await safe_reply(
            update.message,
            f"✅ {len(numbers)} numbers added.\n"
            f"Total: {len(temp['am_numbers'])}\n\n"
            "Type /addnow when ready."
        )
    return AM_FILES


async def am_execute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/addnow command - Start adding members."""
    user_id = update.effective_user.id
    temp = get_temp(user_id)
    links = temp.get("am_links", [])
    numbers = temp.get("am_numbers", [])
    sessions = get_sessions(user_id)

    if not links:
        await safe_reply(update.message, "❌ No group links found. Please start with /addmembers.")
        return ConversationHandler.END

    if not numbers:
        await safe_reply(update.message, "❌ No numbers found. Please send files or enter numbers.")
        return AM_FILES

    if len(sessions) == 1:
        temp["selected_account"] = list(sessions.keys())[0]
        return await am_execute(update, context)
    else:
        kb = accounts_keyboard(sessions, "am_account")
        await safe_reply(update.message, "Which account should add the members?", kb)
        return AM_ACCOUNT


async def am_account_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Account select for add members."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    get_temp(user_id)["selected_account"] = query.data.split(":")[1]
    return await am_execute(update, context)


async def am_execute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Actually add members to the groups."""
    user_id = update.effective_user.id
    temp = get_temp(user_id)
    account_id = temp["selected_account"]
    links = temp["am_links"]
    all_numbers = temp["am_numbers"]

    msg = update.message or update.callback_query.message

    # If link count == number of files/groups, pair them; otherwise use all numbers for all links.
    # 10 links + 10 files mode: Link1+File1, Link2+File2, etc.
    # (Simple pairing: use numbers at the same index)

    status_msg = await msg.reply_text(
        f"➕ <b>Adding members...</b>\n\n"
        f"Groups: {len(links)}\nTotal Numbers: {len(all_numbers)}"
    )

    total_added, total_failed = 0, 0

    for i, link in enumerate(links):
        # Join/get group info
        join_result = await bridge.join_group(account_id, link)
        group_id = join_result.get("groupId", extract_group_id_from_link(link))

        # Determine numbers to use:
        # If links and numbers counts match, pair by index; otherwise use all numbers
        if len(links) == len(all_numbers):
            nums_to_add = [all_numbers[i]]
        else:
            nums_to_add = all_numbers

        added, failed = 0, 0
        for num in nums_to_add:
            res = await bridge.add_member(account_id, group_id, num)
            if res.get("success"):
                added += 1
                total_added += 1
            else:
                failed += 1
                total_failed += 1
            await asyncio.sleep(1)

        await status_msg.edit_text(
            f"⚙️ Group {i + 1}/{len(links)}\n"
            f"✅ Added: {added} | ❌ Failed: {failed}\n"
            f"📊 Total: Added={total_added}, Failed={total_failed}"
        )
        await asyncio.sleep(2)

    await status_msg.edit_text(
        f"✅ <b>Add Members complete!</b>\n\n"
        f"✅ Total Added: {total_added}\n❌ Total Failed: {total_failed}",
        parse_mode=ParseMode.HTML,
        reply_markup=make_main_menu(),
    )
    clear_temp(user_id)
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════
#        CANCEL HANDLER
# ═══════════════════════════════════════════════════════════════
async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel any ongoing conversation."""
    user_id = update.effective_user.id
    clear_temp(user_id)

    if update.callback_query:
        await update.callback_query.answer()
        await safe_edit(
            update.callback_query,
            "❌ Operation cancelled.",
            make_main_menu()
        )
    else:
        await safe_reply(update.message, "❌ Cancelled.", make_main_menu())

    return ConversationHandler.END


async def goto_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Back to main menu callback."""
    query = update.callback_query
    await query.answer()
    text = "🤖 <b>WhatsApp Group Manager</b>\n\nMain menu:"
    await safe_edit(query, text, make_main_menu())
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════
#         CONVERSATION HANDLERS
# ═══════════════════════════════════════════════════════════════
def build_connect_conv() -> ConversationHandler:
    """Build the Connect WhatsApp ConversationHandler."""
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(connect_method_chosen, pattern="^connect:(phone|qr)$")],
        states={
            CONNECT_PHONE_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, connect_phone_input)],
        },
        fallbacks=[
            CallbackQueryHandler(cancel_handler, pattern="^cancel$"),
            CommandHandler("cancel", cancel_handler),
        ],
        per_message=False,
    )


def build_create_group_conv() -> ConversationHandler:
    """Build the Create Group ConversationHandler - complete flow."""
    return ConversationHandler(
        entry_points=[CommandHandler("create", create_group_start)],
        states={
            CG_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, cg_name)],
            CG_PHOTO: [
                MessageHandler(filters.PHOTO, cg_photo),
                CallbackQueryHandler(cg_photo_skip, pattern="^cg_photo:skip$"),
            ],
            CG_DISAPPEAR: [CallbackQueryHandler(cg_disappear, pattern="^cg_disappear:")],
            CG_PERMISSIONS: [
                CallbackQueryHandler(cg_permissions_toggle, pattern="^perm_toggle:"),
                CallbackQueryHandler(cg_permissions_confirm, pattern="^perm_confirm$"),
            ],
            CG_MEMBERS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, cg_members),
                CallbackQueryHandler(cg_members_skip, pattern="^cg_members:skip$"),
            ],
            CG_NUM_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, cg_num_start)],
            CG_NUM_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, cg_num_count)],
            CG_ACCOUNT_SELECT: [CallbackQueryHandler(cg_account_select, pattern="^cg_account:")],
        },
        fallbacks=[
            CallbackQueryHandler(cancel_handler, pattern="^cancel$"),
            CommandHandler("cancel", cancel_handler),
        ],
        per_message=False,
    )


def build_join_conv() -> ConversationHandler:
    """Build the Join Groups ConversationHandler."""
    return ConversationHandler(
        entry_points=[CommandHandler("join", join_start)],
        states={
            JOIN_LINKS: [MessageHandler(filters.TEXT & ~filters.COMMAND, join_links)],
            JOIN_ACCOUNT: [CallbackQueryHandler(join_account_select, pattern="^join_account:")],
            JOIN_CONFIRM: [CallbackQueryHandler(join_execute, pattern="^join_confirm:yes$")],
        },
        fallbacks=[
            CallbackQueryHandler(cancel_handler, pattern="^cancel$"),
            CommandHandler("cancel", cancel_handler),
        ],
        per_message=False,
    )


def build_ctc_conv() -> ConversationHandler:
    """Build the CTC Checker ConversationHandler."""
    return ConversationHandler(
        entry_points=[CommandHandler("ctc", ctc_start)],
        states={
            CTC_MODE: [CallbackQueryHandler(ctc_mode_select, pattern="^ctc_mode:")],
            CTC_LINKS: [MessageHandler(filters.TEXT & ~filters.COMMAND, ctc_links)],
            CTC_FILES: [
                MessageHandler(filters.Document.ALL, ctc_files),
                CommandHandler("done", ctc_done),
            ],
            CTC_ACTION: [CallbackQueryHandler(ctc_action, pattern="^ctc_action:")],
        },
        fallbacks=[
            CallbackQueryHandler(cancel_handler, pattern="^cancel$"),
            CommandHandler("cancel", cancel_handler),
        ],
        per_message=False,
    )


def build_getlink_conv() -> ConversationHandler:
    """Build the Get Link ConversationHandler."""
    return ConversationHandler(
        entry_points=[CommandHandler("getlink", getlink_start)],
        states={
            GL_SCOPE: [CallbackQueryHandler(gl_scope, pattern="^gl_scope:")],
            GL_ACCOUNT: [CallbackQueryHandler(gl_account, pattern="^gl_account:")],
        },
        fallbacks=[
            CallbackQueryHandler(cancel_handler, pattern="^cancel$"),
            CommandHandler("cancel", cancel_handler),
        ],
        per_message=False,
    )


def build_leave_conv() -> ConversationHandler:
    """Build the Leave Groups ConversationHandler."""
    return ConversationHandler(
        entry_points=[CommandHandler("leave", leave_start)],
        states={
            LG_SCOPE: [CallbackQueryHandler(lg_scope, pattern="^lg_scope:")],
            LG_ACCOUNT: [CallbackQueryHandler(lg_account, pattern="^lg_account:")],
            LG_CONFIRM: [CallbackQueryHandler(lg_execute, pattern="^lg_confirm:yes$")],
        },
        fallbacks=[
            CallbackQueryHandler(cancel_handler, pattern="^cancel$"),
            CommandHandler("cancel", cancel_handler),
        ],
        per_message=False,
    )


def build_remove_conv() -> ConversationHandler:
    """Build the Remove Members ConversationHandler."""
    return ConversationHandler(
        entry_points=[CommandHandler("remove", remove_members_start)],
        states={
            RM_SCOPE: [CallbackQueryHandler(rm_scope, pattern="^rm_scope:")],
            RM_ACCOUNT: [CallbackQueryHandler(rm_account, pattern="^rm_account:")],
        },
        fallbacks=[
            CallbackQueryHandler(cancel_handler, pattern="^cancel$"),
            CommandHandler("cancel", cancel_handler),
        ],
        per_message=False,
    )


def build_admin_conv() -> ConversationHandler:
    """Build the Admin Management ConversationHandler."""
    return ConversationHandler(
        entry_points=[CommandHandler("admin", admin_start)],
        states={
            ADMIN_NUMBERS: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_numbers)],
            ADMIN_ACTION: [CallbackQueryHandler(admin_action_select, pattern="^admin_action:")],
            ADMIN_SCOPE: [CallbackQueryHandler(admin_scope_select, pattern="^admin_scope:")],
            ADMIN_ACCOUNT: [CallbackQueryHandler(admin_account_select, pattern="^admin_account:")],
        },
        fallbacks=[
            CallbackQueryHandler(cancel_handler, pattern="^cancel$"),
            CommandHandler("cancel", cancel_handler),
        ],
        per_message=False,
    )


def build_approval_conv() -> ConversationHandler:
    """Build the Approval Setting ConversationHandler."""
    return ConversationHandler(
        entry_points=[CommandHandler("approval", approval_start)],
        states={
            APPROVAL_ACTION: [CallbackQueryHandler(approval_action, pattern="^approval:")],
            APPROVAL_ACCOUNT: [CallbackQueryHandler(approval_account, pattern="^approval_account:")],
        },
        fallbacks=[
            CallbackQueryHandler(cancel_handler, pattern="^cancel$"),
            CommandHandler("cancel", cancel_handler),
        ],
        per_message=False,
    )


def build_pending_conv() -> ConversationHandler:
    """Build the Pending List ConversationHandler."""
    return ConversationHandler(
        entry_points=[CommandHandler("pending", pending_start)],
        states={
            PL_SCOPE: [CallbackQueryHandler(pl_scope, pattern="^pl_scope:")],
            PL_ACCOUNT: [CallbackQueryHandler(pl_account, pattern="^pl_account:")],
        },
        fallbacks=[
            CallbackQueryHandler(cancel_handler, pattern="^cancel$"),
            CommandHandler("cancel", cancel_handler),
        ],
        per_message=False,
    )


def build_addmembers_conv() -> ConversationHandler:
    """Build the Add Members ConversationHandler."""
    return ConversationHandler(
        entry_points=[CommandHandler("addmembers", addmembers_start)],
        states={
            AM_LINKS: [MessageHandler(filters.TEXT & ~filters.COMMAND, am_links)],
            AM_FILES: [
                MessageHandler(filters.Document.ALL, am_files),
                MessageHandler(filters.TEXT & ~filters.COMMAND, am_files),
                CommandHandler("addnow", am_execute_cmd),
            ],
            AM_ACCOUNT: [CallbackQueryHandler(am_account_select, pattern="^am_account:")],
        },
        fallbacks=[
            CallbackQueryHandler(cancel_handler, pattern="^cancel$"),
            CommandHandler("cancel", cancel_handler),
        ],
        per_message=False,
    )


# ═══════════════════════════════════════════════════════════════
#              MAIN APPLICATION SETUP
# ═══════════════════════════════════════════════════════════════
def main():
    """Start the bot."""
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("❌ BOT_TOKEN is not set! Please add BOT_TOKEN=your_token to your .env file.")
        sys.exit(1)

    logger.info("🤖 WhatsApp Group Manager Bot starting...")

    app = Application.builder().token(BOT_TOKEN).build()

    # ── Main menu handler (start command + inline buttons)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(main_menu_callback, pattern="^main:"))
    app.add_handler(CallbackQueryHandler(goto_start, pattern="^goto:start$"))
    app.add_handler(CallbackQueryHandler(cancel_handler, pattern="^cancel$"))

    # ── Connect/Disconnect in main menu conv (subset)
    app.add_handler(CallbackQueryHandler(disconnect_select, pattern="^disconnect_acc:"))
    app.add_handler(CallbackQueryHandler(connect_method_chosen, pattern="^connect:(phone|qr)$"))

    # ── Feature ConversationHandlers
    app.add_handler(build_create_group_conv())
    app.add_handler(build_join_conv())
    app.add_handler(build_ctc_conv())
    app.add_handler(build_getlink_conv())
    app.add_handler(build_leave_conv())
    app.add_handler(build_remove_conv())
    app.add_handler(build_admin_conv())
    app.add_handler(build_approval_conv())
    app.add_handler(build_pending_conv())
    app.add_handler(build_addmembers_conv())

    # ── Global cancel
    app.add_handler(CommandHandler("cancel", cancel_handler))

    logger.info("✅ All handlers registered. Bot is now polling...")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
