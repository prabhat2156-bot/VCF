import os, asyncio, logging, time, sys, shutil, zipfile, re, secrets, base64
from datetime import datetime, timezone
from asyncio import create_subprocess_exec
from asyncio.subprocess import PIPE

import psutil
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ConversationHandler, ContextTypes,
    filters
)

# ─────────────────────────────────────────────────────────────
# Bootstrap
# ─────────────────────────────────────────────────────────────
load_dotenv()
logging.basicConfig(
    format="%(asctime)s — %(name)s — %(levelname)s — %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN       = os.getenv("BOT_TOKEN", "")
OWNER_ID        = int(os.getenv("OWNER_ID", "0"))
OWNER_USERNAME  = os.getenv("OWNER_USERNAME", "owner")
MONGODB_URI     = os.getenv("MONGODB_URI", "")
DATABASE_NAME   = os.getenv("DATABASE_NAME", "god_madara_hosting")
BASE_URL        = os.getenv("BASE_URL", "http://localhost:8080")
PORT            = int(os.getenv("PORT", "8080"))

# Secondary MongoDB (optional)
MONGODB_URI_2   = os.getenv("MONGODB_URI_2", "")
DATABASE_NAME_2 = os.getenv("DATABASE_NAME_2", "")

# Primary DB
mongo_client = AsyncIOMotorClient(MONGODB_URI)
db           = mongo_client[DATABASE_NAME]
users_col    = db["users"]
projects_col = db["projects"]
tokens_col   = db["file_tokens"]
backups_col  = db["backups"]

# Secondary DB (optional — for user's other bot/project)
mongo_client_2 = None
db_2 = None
if MONGODB_URI_2 and DATABASE_NAME_2:
    mongo_client_2 = AsyncIOMotorClient(MONGODB_URI_2)
    db_2 = mongo_client_2[DATABASE_NAME_2]
    logging.getLogger(__name__).info(f"Secondary MongoDB connected: {DATABASE_NAME_2}")

BOT_START_TIME = time.time()

# Global bot reference for notifications (set in post_init)
notification_bot = None

# ─────────────────────────────────────────────────────────────
# Conversation states
# ─────────────────────────────────────────────────────────────
(
    NEW_PROJECT_NAME,
    NEW_PROJECT_FILES,
    EDIT_RUN_CMD,
    ADMIN_GIVE_PREMIUM_ID,
    ADMIN_REMOVE_PREMIUM_ID,
    ADMIN_TEMP_PREMIUM_ID,
    ADMIN_TEMP_PREMIUM_DUR,
    ADMIN_BAN_ID,
    ADMIN_UNBAN_ID,
    ADMIN_BROADCAST_MSG,
    ADMIN_SEND_USER_ID,
    ADMIN_SEND_USER_MSG,
    ENV_ADD_KEY,
    ENV_ADD_VALUE,
    ENV_EDIT_VALUE,
) = range(15)

FREE_LIMIT    = 1
PREMIUM_LIMIT = 10

PROJECTS_ROOT = os.path.join(os.path.dirname(__file__), "projects")
os.makedirs(PROJECTS_ROOT, exist_ok=True)

# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def project_dir(user_id: int, project_name: str) -> str:
    return os.path.join(PROJECTS_ROOT, str(user_id), project_name)

def fmt_bytes(b: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"

def fmt_uptime(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}h {m}m {sec}s" if h else (f"{m}m {sec}s" if m else f"{sec}s")

def fmt_duration(total_seconds: float) -> str:
    return fmt_uptime(total_seconds)

async def safe_edit(query, text: str, reply_markup=None, parse_mode=ParseMode.MARKDOWN):
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except BadRequest as e:
        logger.warning(f"safe_edit BadRequest: {e}")
        # Retry without parse_mode as fallback
        try:
            await query.edit_message_text(text, reply_markup=reply_markup)
        except Exception:
            pass
    except Exception as e:
        logger.error(f"safe_edit error: {e}")

async def ensure_user(user):
    """Upsert user document."""
    await users_col.update_one(
        {"user_id": user.id},
        {"$setOnInsert": {
            "user_id":       user.id,
            "username":      user.username or "",
            "first_name":    user.first_name or "",
            "is_premium":    False,
            "premium_expiry": None,
            "is_banned":     False,
            "joined_date":   datetime.now(timezone.utc),
        }},
        upsert=True,
    )
    await users_col.update_one(
        {"user_id": user.id},
        {"$set": {
            "username":   user.username or "",
            "first_name": user.first_name or "",
        }},
    )

async def check_premium_expiry(user_id: int):
    """Strip premium if expired."""
    doc = await users_col.find_one({"user_id": user_id})
    if doc and doc.get("premium_expiry"):
        if doc["premium_expiry"] < datetime.now(timezone.utc):
            await users_col.update_one(
                {"user_id": user_id},
                {"$set": {"is_premium": False, "premium_expiry": None}},
            )

async def get_user(user_id: int):
    return await users_col.find_one({"user_id": user_id})

async def is_banned(user_id: int) -> bool:
    doc = await get_user(user_id)
    return bool(doc and doc.get("is_banned"))

async def is_premium(user_id: int) -> bool:
    await check_premium_expiry(user_id)
    doc = await get_user(user_id)
    return bool(doc and doc.get("is_premium"))

async def project_count(user_id: int) -> int:
    return await projects_col.count_documents({"user_id": user_id})

async def get_project(user_id: int, name: str):
    return await projects_col.find_one({"user_id": user_id, "name": name})

async def running_project_count() -> int:
    return await projects_col.count_documents({"status": "running"})

async def check_ban_and_premium(update: Update):
    """Return (is_banned, doc). Updates premium expiry."""
    user = update.effective_user
    await ensure_user(user)
    await check_premium_expiry(user.id)
    banned = await is_banned(user.id)
    return banned

# ─────────────────────────────────────────────────────────────
# /start
# ─────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await ensure_user(user)
    await check_premium_expiry(user.id)

    if await is_banned(user.id):
        await update.message.reply_text("🚫 You are banned. Contact owner.", parse_mode=ParseMode.MARKDOWN)
        return

    doc      = await get_user(user.id)
    premium  = doc.get("is_premium", False)
    count    = await project_count(user.id)
    plan_lbl = "Premium ✨" if premium else "Free"
    limit_lbl = "∞" if premium else str(FREE_LIMIT)

    text = (
        f"🌟 *Welcome to God Madara Hosting Bot!*\n\n"
        f"👋 Hello {user.first_name}!\n\n"
        f"🚀 *What I can do:*\n"
        f"• Host Python projects 24/7\n"
        f"• Web File Manager — Edit files in browser\n"
        f"• Auto-install requirements.txt\n"
        f"• Real-time logs & monitoring\n"
        f"• Free: 1 project | Premium: Unlimited\n\n"
        f"📊 *Your Status:*\n"
        f"👤 ID: `{user.id}`\n"
        f"💎 Plan: {plan_lbl}\n"
        f"📁 Projects: {count}/{limit_lbl}\n\n"
        f"Choose an option below:"
    )

    kb = [
        [
            InlineKeyboardButton("🆕 New Project",  callback_data="new_project"),
            InlineKeyboardButton("📂 My Projects",  callback_data="my_projects"),
        ],
        [
            InlineKeyboardButton("💎 Premium",       callback_data="premium"),
            InlineKeyboardButton("📊 Bot Status",    callback_data="bot_status"),
        ],
    ]
    if user.id == OWNER_ID:
        kb.append([InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin_panel")])

    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

# ─────────────────────────────────────────────────────────────
# Back-to-start helper via callback
# ─────────────────────────────────────────────────────────────

async def cb_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    await ensure_user(user)
    await check_premium_expiry(user.id)

    if await is_banned(user.id):
        await safe_edit(query, "🚫 You are banned. Contact owner.", parse_mode=ParseMode.MARKDOWN)
        return

    doc      = await get_user(user.id)
    premium  = doc.get("is_premium", False)
    count    = await project_count(user.id)
    plan_lbl = "Premium ✨" if premium else "Free"
    limit_lbl = "∞" if premium else str(FREE_LIMIT)

    text = (
        f"🌟 *Welcome to God Madara Hosting Bot!*\n\n"
        f"👋 Hello {user.first_name}!\n\n"
        f"🚀 *What I can do:*\n"
        f"• Host Python projects 24/7\n"
        f"• Web File Manager — Edit files in browser\n"
        f"• Auto-install requirements.txt\n"
        f"• Real-time logs & monitoring\n"
        f"• Free: 1 project | Premium: Unlimited\n\n"
        f"📊 *Your Status:*\n"
        f"👤 ID: `{user.id}`\n"
        f"💎 Plan: {plan_lbl}\n"
        f"📁 Projects: {count}/{limit_lbl}\n\n"
        f"Choose an option below:"
    )

    kb = [
        [
            InlineKeyboardButton("🆕 New Project",  callback_data="new_project"),
            InlineKeyboardButton("📂 My Projects",  callback_data="my_projects"),
        ],
        [
            InlineKeyboardButton("💎 Premium",       callback_data="premium"),
            InlineKeyboardButton("📊 Bot Status",    callback_data="bot_status"),
        ],
    ]
    if user.id == OWNER_ID:
        kb.append([InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin_panel")])

    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

# ─────────────────────────────────────────────────────────────
# 📊 Bot Status
# ─────────────────────────────────────────────────────────────

async def cb_bot_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if await is_banned(query.from_user.id):
        await safe_edit(query, "🚫 You are banned. Contact owner.")
        return

    try:
        # Measure DB ping safely
        db_ping = 0
        try:
            t0 = time.time()
            await db.command("ping")
            db_ping = int((time.time() - t0) * 1000)
        except Exception:
            db_ping = -1

        # Measure bot API ping safely
        api_ping = 0
        try:
            t1 = time.time()
            await context.bot.get_me()
            api_ping = int((time.time() - t1) * 1000)
        except Exception:
            api_ping = -1

        total_users = await users_col.count_documents({})
        premium_users = await users_col.count_documents({"is_premium": True})
        total_proj = await projects_col.count_documents({})
        running_proj = await running_project_count()

        cpu = psutil.cpu_percent(interval=None)
        ram = psutil.virtual_memory()
        disk = psutil.disk_usage("/")

        uptime = fmt_uptime(time.time() - BOT_START_TIME)
        py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"

        # Backup info
        backup_line = "💾 Last Backup: `Never`\n"
        try:
            meta = await backups_col.find_one({"type": "backup_meta"})
            if meta:
                backup_time = meta["backed_up_at"].strftime("%Y-%m-%d %H:%M UTC")
                backup_size = fmt_bytes(meta.get("total_size", 0))
                backup_files = meta.get("total_files", 0)
                backup_line = (
                    f"💾 Last Backup: `{backup_time}`\n"
                    f"📦 Backup: `{backup_files}` files, `{backup_size}`\n"
                )
        except Exception:
            pass

        # DB2 status
        db2_line = ""
        if MONGODB_URI_2 and DATABASE_NAME_2:
            db2_line = f"💾 DB2: `{DATABASE_NAME_2}` ✅\n"

        # Format ping display
        db_ping_str = f"{db_ping}ms" if db_ping >= 0 else "Error"
        api_ping_str = f"{api_ping}ms" if api_ping >= 0 else "Error"

        text = (
            f"📊 *Bot Dashboard*\n\n"
            f"👥 Total Users: `{total_users}`\n"
            f"💎 Premium Users: `{premium_users}`\n"
            f"📁 Total Projects: `{total_proj}`\n"
            f"🟢 Running Projects: `{running_proj}`\n"
            f"💾 Database: MongoDB ✅\n"
            f"{db2_line}"
            f"🐍 Python: `{py_ver}`\n\n"
            f"💻 *System:*\n"
            f"├ CPU: `{cpu}%`\n"
            f"├ RAM: `{fmt_bytes(ram.used)}/{fmt_bytes(ram.total)}` (`{ram.percent}%`)\n"
            f"└ Disk: `{fmt_bytes(disk.used)}/{fmt_bytes(disk.total)}` (`{disk.percent}%`)\n\n"
            f"🏓 Bot Ping: `{api_ping_str}`\n"
            f"💾 DB Ping: `{db_ping_str}`\n"
            f"⏰ Uptime: `{uptime}`\n\n"
            f"*Backup Status:*\n"
            f"{backup_line}"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔃 Refresh", callback_data="bot_status"),
             InlineKeyboardButton("🔙 Back", callback_data="back_start")],
        ])
        await safe_edit(query, text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"bot_status error: {e}")
        await safe_edit(
            query,
            f"📊 *Bot Dashboard*\n\n⚠️ Error loading stats: {str(e)[:200]}\n\nBot is online and working!",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔃 Retry", callback_data="bot_status"),
                 InlineKeyboardButton("🔙 Back", callback_data="back_start")],
            ]),
            parse_mode=ParseMode.MARKDOWN,
        )

# ─────────────────────────────────────────────────────────────
# 💎 Premium page
# ─────────────────────────────────────────────────────────────

async def cb_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    if await is_banned(uid):
        await safe_edit(query, "🚫 You are banned. Contact owner.")
        return

    premium = await is_premium(uid)

    features = (
        f"*Free Plan:*\n"
        f"• 1 Project only\n"
        f"• File Manager (10 min)\n\n"
        f"*Premium Plan:*\n"
        f"• ✅ Unlimited projects\n"
        f"• ✅ Priority support\n"
        f"• ✅ Extended file manager\n"
        f"• ✅ Advanced monitoring\n\n"
    )

    if premium:
        text = (
            f"💎 *Premium Membership*\n\n"
            f"✨ *You are Premium!* ✨\n\n"
            + features +
            f"🌟 Premium is active!"
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_start")]])
    else:
        text = (
            f"💎 *Premium Membership*\n\n"
            + features +
            f"To get Premium, contact the owner!"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📩 Contact Owner", url=f"https://t.me/{OWNER_USERNAME}")],
            [InlineKeyboardButton("🔙 Back",          callback_data="back_start")],
        ])

    await safe_edit(query, text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

# ─────────────────────────────────────────────────────────────
# 📂 My Projects
# ─────────────────────────────────────────────────────────────

async def cb_my_projects(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    if await is_banned(uid):
        await safe_edit(query, "🚫 You are banned. Contact owner.")
        return

    projects = await projects_col.find({"user_id": uid}).to_list(length=100)
    if not projects:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_start")]])
        await safe_edit(query, "📂 *My Projects*\n\nYou have no projects yet.", reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        return

    kb_rows = []
    for p in projects:
        icon = "🟢" if p.get("status") == "running" else "🔴"
        kb_rows.append([InlineKeyboardButton(f"{icon} {p['name']}", callback_data=f"proj:{p['name']}")])
    kb_rows.append([InlineKeyboardButton("🔙 Back", callback_data="back_start")])

    await safe_edit(query, "📂 *My Projects*\n\nSelect a project:", reply_markup=InlineKeyboardMarkup(kb_rows), parse_mode=ParseMode.MARKDOWN)

# ─────────────────────────────────────────────────────────────
# Project Dashboard
# ─────────────────────────────────────────────────────────────

def escape_md(text: str) -> str:
    """Escape Markdown v1 special characters."""
    for ch in ('_', '*', '`', '['):
        text = str(text).replace(ch, f'\\{ch}')
    return text

def project_dashboard_text(p: dict) -> str:
    status  = p.get("status", "stopped")
    icon    = "🟢 Running" if status == "running" else "🔴 Stopped"
    pid     = str(p.get("pid")) if p.get("pid") else "N/A"
    uptime  = "N/A"
    if status == "running" and p.get("started_at"):
        try:
            started = p["started_at"]
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            elapsed = (datetime.now(timezone.utc) - started).total_seconds()
            uptime  = fmt_uptime(max(0, elapsed))
        except Exception:
            uptime = "N/A"
    last_run = "Never"
    if p.get("last_run"):
        try:
            last_run = p["last_run"].strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            last_run = str(p["last_run"])
    exit_code = str(p.get("exit_code")) if p.get("exit_code") is not None else "None"
    run_cmd   = p.get("run_command") or "Not set"
    created   = "N/A"
    if p.get("created_date"):
        try:
            created = p["created_date"].strftime("%Y-%m-%d")
        except Exception:
            created = str(p["created_date"])

    ar_status = "✅ ON" if p.get("auto_restart", True) else "❌ OFF"

    return (
        f"📊 Project: *{p['name']}*\n\n"
        f"🔹 Status: {icon}\n"
        f"🔹 PID: `{pid}`\n"
        f"🔹 Uptime: `{uptime}`\n"
        f"🔹 Last Run: `{last_run}`\n"
        f"🔹 Exit Code: `{exit_code}`\n"
        f"🔹 Run Command: `{run_cmd}`\n"
        f"🔹 Auto-Restart: {ar_status}\n"
        f"📅 Created: `{created}`"
    )

def project_dashboard_kb(user_id: int, project_name: str, auto_restart: bool = True, is_running: bool = False) -> InlineKeyboardMarkup:
    pn = project_name
    ar_label = "⏰ Auto-Restart: ✅" if auto_restart else "⏰ Auto-Restart: ❌"

    if is_running:
        row1 = [
            InlineKeyboardButton("⏹ Stop",      callback_data=f"stop:{pn}"),
            InlineKeyboardButton("🔄 Restart",   callback_data=f"restart:{pn}"),
            InlineKeyboardButton("📋 Logs",      callback_data=f"logs:{pn}"),
        ]
    else:
        row1 = [
            InlineKeyboardButton("▶️ Run",       callback_data=f"run:{pn}"),
            InlineKeyboardButton("🔄 Restart",   callback_data=f"restart:{pn}"),
            InlineKeyboardButton("📋 Logs",      callback_data=f"logs:{pn}"),
        ]

    return InlineKeyboardMarkup([
        row1,
        [
            InlineKeyboardButton("🔃 Refresh",   callback_data=f"proj:{pn}"),
            InlineKeyboardButton("✏️ Edit CMD",  callback_data=f"editcmd:{pn}"),
            InlineKeyboardButton("📁 Files",     callback_data=f"filemgr:{pn}"),
        ],
        [
            InlineKeyboardButton(ar_label,        callback_data=f"toggle_ar:{pn}"),
            InlineKeyboardButton("🔐 Env Vars",  callback_data=f"envvars:{pn}"),
        ],
        [
            InlineKeyboardButton("🗑 Delete",    callback_data=f"delete:{pn}"),
            InlineKeyboardButton("🔙 Back",      callback_data="my_projects"),
        ],
    ])

async def cb_project_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid  = query.from_user.id
    name = query.data.split(":", 1)[1]

    if await is_banned(uid):
        await safe_edit(query, "🚫 You are banned. Contact owner.")
        return

    p = await get_project(uid, name)
    if not p:
        await safe_edit(query, "❌ Project not found.", parse_mode=ParseMode.MARKDOWN)
        return

    await safe_edit(query, project_dashboard_text(p), reply_markup=project_dashboard_kb(uid, name, p.get("auto_restart", True), p.get("status") == "running"), parse_mode=ParseMode.MARKDOWN)

# ─────────────────────────────────────────────────────────────
# ▶️ Run project
# ─────────────────────────────────────────────────────────────

async def start_project_process(uid: int, name: str) -> dict:
    """Start project subprocess. Returns updated project dict."""
    p   = await get_project(uid, name)
    pdir = project_dir(uid, name)
    cmd  = p.get("run_command") or "python main.py"

    log_path = os.path.join(pdir, "output.log")

    venv_python = os.path.join(pdir, "venv", "bin", "python")
    if not os.path.exists(venv_python):
        venv_python = sys.executable

    import shlex
    parts = shlex.split(cmd)
    if parts and parts[0] in ("python", "python3"):
        parts[0] = venv_python

    logger.info(f"Starting process: {' '.join(parts)} in {pdir}")

    # Use file descriptor for log output
    log_fd = open(log_path, "a")

    proc = await create_subprocess_exec(
        *parts,
        stdout=log_fd,
        stderr=log_fd,
        cwd=pdir,
    )

    logger.info(f"Process started with PID {proc.pid}")

    now = datetime.now(timezone.utc)
    await projects_col.update_one(
        {"user_id": uid, "name": name},
        {"$set": {
            "status":       "running",
            "pid":          proc.pid,
            "started_at":   now,
            "last_run":     now,
            "exit_code":    None,
            "admin_stopped": False,
        }},
    )
    # Store proc object in memory for monitoring
    context_store[f"{uid}:{name}"] = proc

    # Verify it was saved
    updated = await get_project(uid, name)
    logger.info(f"DB updated - status: {updated.get('status')}, pid: {updated.get('pid')}")
    return updated

context_store: dict = {}

async def cb_run(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid  = query.from_user.id
    name = query.data.split(":", 1)[1]

    if await is_banned(uid):
        await safe_edit(query, "🚫 You are banned. Contact owner.")
        return

    p = await get_project(uid, name)
    if not p:
        await safe_edit(query, "❌ Project not found.", parse_mode=ParseMode.MARKDOWN)
        return

    if p.get("admin_stopped"):
        await safe_edit(
            query,
            "⚠️ Your project was stopped by admin. Contact owner.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=f"proj:{name}")]]),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if p.get("status") == "running" and p.get("pid"):
        if psutil.pid_exists(p["pid"]):
            await safe_edit(query, "▶️ Project is already running.", parse_mode=ParseMode.MARKDOWN)
            return

    if not p.get("run_command"):
        await safe_edit(
            query,
            "❌ No run command set. Use ✏️ Edit CMD first.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=f"proj:{name}")]]),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await safe_edit(query, f"▶️ Starting {name}...")

    try:
        updated = await start_project_process(uid, name)
        logger.info(f"Started project {name} for user {uid}, PID: {updated.get('pid')}")
        await safe_edit(
            query,
            project_dashboard_text(updated),
            reply_markup=project_dashboard_kb(uid, name, updated.get("auto_restart", True), updated.get("status") == "running"),
        )
    except Exception as e:
        logger.error(f"Failed to start project {name}: {e}")
        await safe_edit(query, f"❌ Failed to start: {str(e)[:300]}")

# ─────────────────────────────────────────────────────────────
# ⏹ Stop project
# ─────────────────────────────────────────────────────────────

async def cb_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    name = query.data.split(":", 1)[1]

    if await is_banned(uid):
        await safe_edit(query, "🚫 You are banned. Contact owner.")
        return

    p = await get_project(uid, name)
    if not p:
        await safe_edit(query, "❌ Project not found.")
        return

    if p.get("status") != "running":
        await safe_edit(query, "⏹ Project is not running.", parse_mode=ParseMode.MARKDOWN)
        return

    await safe_edit(query, f"⏹ Stopping {name}...")
    await kill_project(uid, name)

    p = await get_project(uid, name)
    await safe_edit(
        query,
        project_dashboard_text(p),
        reply_markup=project_dashboard_kb(uid, name, p.get("auto_restart", True), p.get("status") == "running"),
    )

# ─────────────────────────────────────────────────────────────
# 🔄 Restart
# ─────────────────────────────────────────────────────────────

async def kill_project(uid: int, name: str):
    p = await get_project(uid, name)
    if p and p.get("pid"):
        try:
            proc = psutil.Process(p["pid"])
            for child in proc.children(recursive=True):
                child.kill()
            proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    await projects_col.update_one(
        {"user_id": uid, "name": name},
        {"$set": {"status": "stopped", "pid": None}},
    )

async def cb_restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid  = query.from_user.id
    name = query.data.split(":", 1)[1]

    if await is_banned(uid):
        await safe_edit(query, "🚫 You are banned. Contact owner.")
        return

    p = await get_project(uid, name)
    if not p:
        await safe_edit(query, "❌ Project not found.", parse_mode=ParseMode.MARKDOWN)
        return

    if p.get("admin_stopped"):
        await safe_edit(query, "⚠️ Your project was stopped by admin. Contact owner.", parse_mode=ParseMode.MARKDOWN)
        return

    await safe_edit(query, f"🔄 Restarting *{escape_md(name)}*...", parse_mode=ParseMode.MARKDOWN)
    await kill_project(uid, name)
    await asyncio.sleep(1)

    try:
        updated = await start_project_process(uid, name)
        await safe_edit(
            query,
            project_dashboard_text(updated),
            reply_markup=project_dashboard_kb(uid, name, updated.get("auto_restart", True), updated.get("status") == "running"),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        await safe_edit(query, f"❌ Restart failed: {escape_md(str(e))}", parse_mode=ParseMode.MARKDOWN)

# ─────────────────────────────────────────────────────────────
# 📋 Logs
# ─────────────────────────────────────────────────────────────

async def cb_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid  = query.from_user.id
    name = query.data.split(":", 1)[1]

    if await is_banned(uid):
        await safe_edit(query, "🚫 You are banned. Contact owner.")
        return

    log_path = os.path.join(project_dir(uid, name), "output.log")
    if not os.path.exists(log_path):
        lines = "No logs yet."
    else:
        with open(log_path, "r", errors="replace") as f:
            all_lines = f.readlines()
        lines = "".join(all_lines[-50:]) or "Log file is empty."

    # Truncate to Telegram's 4096 char limit
    if len(lines) > 3500:
        lines = "...(truncated)...\n" + lines[-3500:]

    text = f"📋 *Logs — {escape_md(name)}*\n\n```\n{escape_md(lines)}\n```"
    kb   = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=f"proj:{name}")]])
    await safe_edit(query, text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

# ─────────────────────────────────────────────────────────────
# ✏️ Edit Run CMD — ConversationHandler
# ─────────────────────────────────────────────────────────────

async def cb_editcmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    name = query.data.split(":", 1)[1]
    context.user_data["editcmd_project"] = name
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"proj:{name}")]])
    await safe_edit(
        query,
        f"✏️ *Edit Run Command for {escape_md(name)}*\n\nSend the new run command.\nExample: `python main.py`",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN,
    )
    return EDIT_RUN_CMD

async def editcmd_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    cmd  = update.message.text.strip()
    name = context.user_data.get("editcmd_project")

    await projects_col.update_one(
        {"user_id": uid, "name": name},
        {"$set": {"run_command": cmd}},
    )
    p   = await get_project(uid, name)
    kb  = project_dashboard_kb(uid, name, p.get("auto_restart", True), p.get("status") == "running")
    await update.message.reply_text(
        f"✅ Run command updated!\n\n" + project_dashboard_text(p),
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN,
    )
    return ConversationHandler.END

# ─────────────────────────────────────────────────────────────
# 📁 File Manager
# ─────────────────────────────────────────────────────────────

async def cb_filemgr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid  = query.from_user.id
    name = query.data.split(":", 1)[1]

    if await is_banned(uid):
        await safe_edit(query, "🚫 You are banned. Contact owner.")
        return

    token    = secrets.token_urlsafe(24)
    now      = datetime.now(timezone.utc)
    expires  = now.timestamp() + 600  # 10 minutes

    # Store in memory for Flask
    from file_manager import token_store
    token_store[token] = {
        "user_id":      uid,
        "project_name": name,
        "project_dir":  project_dir(uid, name),
        "expires_at":   expires,
    }
    # Store in MongoDB
    await tokens_col.insert_one({
        "token":        token,
        "user_id":      uid,
        "project_name": name,
        "created_at":   now,
        "expires_at":   datetime.fromtimestamp(expires, tz=timezone.utc),
    })

    url = f"{BASE_URL}/fm/{token}/"
    kb  = InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 Open File Manager", url=url)],
        [InlineKeyboardButton("🔙 Back",              callback_data=f"proj:{name}")],
    ])
    await safe_edit(
        query,
        f"📁 *File Manager*\n\nYour session link (valid 10 min):\n`{escape_md(url)}`",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN,
    )

# ─────────────────────────────────────────────────────────────
# 🗑 Delete project
# ─────────────────────────────────────────────────────────────

async def cb_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    name = query.data.split(":", 1)[1]
    kb   = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Yes, Delete", callback_data=f"delete_yes:{name}"),
            InlineKeyboardButton("❌ Cancel",       callback_data=f"proj:{name}"),
        ],
    ])
    await safe_edit(
        query,
        f"🗑 *Delete {escape_md(name)}?*\n\nThis cannot be undone.",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN,
    )

async def cb_delete_yes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid  = query.from_user.id
    name = query.data.split(":", 1)[1]

    await kill_project(uid, name)
    pdir = project_dir(uid, name)
    if os.path.exists(pdir):
        shutil.rmtree(pdir, ignore_errors=True)
    await projects_col.delete_one({"user_id": uid, "name": name})
    # Also remove any backups for this project
    await backups_col.delete_many({"type": "file_backup", "user_id": uid, "project_name": name})

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 My Projects", callback_data="my_projects")]])
    await safe_edit(query, f"✅ Project *{escape_md(name)}* deleted.", reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

# ─────────────────────────────────────────────────────────────
# 🆕 New Project — ConversationHandler
# ─────────────────────────────────────────────────────────────

async def cb_new_project(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id

    if await is_banned(uid):
        await safe_edit(query, "🚫 You are banned. Contact owner.")
        return ConversationHandler.END

    premium = await is_premium(uid)
    count   = await project_count(uid)
    limit   = PREMIUM_LIMIT if premium else FREE_LIMIT

    if count >= limit:
        lbl = "∞" if premium else str(FREE_LIMIT)
        await safe_edit(
            query,
            f"❌ Project limit reached ({count}/{lbl}).\nUpgrade to Premium for more!",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_start")]]),
            parse_mode=ParseMode.MARKDOWN,
        )
        return ConversationHandler.END

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="back_start")]])
    await safe_edit(
        query,
        "📝 *New Project*\n\nEnter a project name:\n(alphanumeric + underscore, max 20 chars)",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN,
    )
    return NEW_PROJECT_NAME

async def new_project_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    name = update.message.text.strip()

    if not re.match(r"^[a-zA-Z0-9_]{1,20}$", name):
        await update.message.reply_text(
            "❌ Invalid name. Use only letters, numbers, underscore (max 20). Try again:",
            parse_mode=ParseMode.MARKDOWN,
        )
        return NEW_PROJECT_NAME

    existing = await get_project(uid, name)
    if existing:
        await update.message.reply_text(
            f"❌ You already have a project named *{escape_md(name)}*. Choose another:",
            parse_mode=ParseMode.MARKDOWN,
        )
        return NEW_PROJECT_NAME

    context.user_data["new_project_name"]  = name
    context.user_data["new_project_files"] = []

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Done Uploading", callback_data="upload_done")]])
    await update.message.reply_text(
        f"📁 *Project: {escape_md(name)}*\n\n"
        f"Now send your files one by one, or a single `.zip` file.\n"
        f"When done, click *Done Uploading* or send /done.",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN,
    )
    return NEW_PROJECT_FILES

async def new_project_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    name = context.user_data.get("new_project_name")
    pdir = project_dir(uid, name)
    os.makedirs(pdir, exist_ok=True)

    doc = update.message.document
    if not doc:
        await update.message.reply_text("Please send a file document.", parse_mode=ParseMode.MARKDOWN)
        return NEW_PROJECT_FILES

    file_obj  = await doc.get_file()
    file_name = doc.file_name or "file"
    dest      = os.path.join(pdir, file_name)
    await file_obj.download_to_drive(dest)

    context.user_data["new_project_files"].append(file_name)

    # Auto-extract zip
    if file_name.endswith(".zip"):
        with zipfile.ZipFile(dest, "r") as zf:
            zf.extractall(pdir)
        os.remove(dest)
        await update.message.reply_text(
            f"📦 `{escape_md(file_name)}` extracted. Send more files or click Done.",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await update.message.reply_text(
            f"✅ `{escape_md(file_name)}` uploaded. Send more or click Done.",
            parse_mode=ParseMode.MARKDOWN,
        )

    return NEW_PROJECT_FILES

async def new_project_done_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _finalize_new_project(update, context, via_message=True)

async def new_project_done_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    return await _finalize_new_project(update, context, via_message=False)

# ─────────────────────────────────────────────────────────────
# ISSUE 3 FIX: Animated setup progress in _finalize_new_project
# ─────────────────────────────────────────────────────────────

async def _finalize_new_project(update: Update, context: ContextTypes.DEFAULT_TYPE, via_message: bool):
    uid  = update.effective_user.id
    name = context.user_data.get("new_project_name")
    pdir = project_dir(uid, name)

    status_msg = await (update.message or update.callback_query.message).reply_text(
        f"⚙️ *Setting up {escape_md(name)}*\n\n"
        f"⏳ Initializing project...",
        parse_mode=ParseMode.MARKDOWN,
    )

    results = []

    # Animated step function
    async def update_status(step_text, completed_results):
        progress_bar = "█" * len(completed_results) + "░" * (3 - len(completed_results))
        steps_text = "\n".join(completed_results) if completed_results else ""
        try:
            await status_msg.edit_text(
                f"⚙️ *Setting up {escape_md(name)}*\n\n"
                f"{steps_text}\n\n"
                f"⏳ {step_text}\n"
                f"[{progress_bar}]",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass

    # Step 1: Create venv
    await update_status("📁 Creating virtual environment...", results)
    await asyncio.sleep(0.5)  # Small delay for visual effect

    try:
        proc = await asyncio.wait_for(
            create_subprocess_exec(sys.executable, "-m", "venv", os.path.join(pdir, "venv"),
                                   stdout=PIPE, stderr=PIPE),
            timeout=60,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode == 0:
            results.append("✅ Virtual environment created")
        else:
            results.append(f"❌ venv failed: {stderr.decode()[:200]}")
    except asyncio.TimeoutError:
        results.append("❌ venv timed out")
    except Exception as e:
        results.append(f"❌ venv error: {e}")

    # Step 2: Install requirements if present
    req_path = os.path.join(pdir, "requirements.txt")
    pip_path = os.path.join(pdir, "venv", "bin", "pip")
    if os.path.exists(req_path) and os.path.exists(pip_path):
        await update_status("📦 Installing requirements...", results)

        try:
            proc = await asyncio.wait_for(
                create_subprocess_exec(
                    pip_path, "install", "-r", req_path,
                    stdout=PIPE, stderr=PIPE, cwd=pdir,
                ),
                timeout=300,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
            if proc.returncode == 0:
                results.append("✅ Requirements installed")
            else:
                results.append(f"❌ pip install failed: {stderr.decode()[:300]}")
        except asyncio.TimeoutError:
            results.append("❌ pip install timed out")
        except Exception as e:
            results.append(f"❌ pip error: {e}")

        # Step 3: Verify installation
        await update_status("🔍 Verifying packages...", results)

        if os.path.exists(pip_path):
            try:
                proc2 = await asyncio.wait_for(
                    create_subprocess_exec(pip_path, "list", stdout=PIPE, stderr=PIPE),
                    timeout=30,
                )
                out2, _ = await asyncio.wait_for(proc2.communicate(), timeout=30)
                pkg_count = len(out2.decode().strip().splitlines()) - 2
                results.append(f"✅ {pkg_count} packages verified")
            except Exception:
                results.append("⚠️ Could not verify packages")
    else:
        results.append("ℹ️ No requirements.txt found")

    # Determine default run command
    main_candidates = ["main.py", "bot.py", "app.py", "index.py", "run.py"]
    default_cmd = None
    for c in main_candidates:
        if os.path.exists(os.path.join(pdir, c)):
            default_cmd = f"python {c}"
            break

    # Save to DB
    await projects_col.insert_one({
        "user_id":      uid,
        "name":         name,
        "run_command":  default_cmd,
        "created_date": datetime.now(timezone.utc),
        "last_run":     None,
        "exit_code":    None,
        "status":       "stopped",
        "pid":          None,
        "admin_stopped": False,
        "auto_restart":  True,
        "restart_count": 0,
        "last_restart_at": None,
    })

    result_text = "\n".join(results)
    if default_cmd:
        result_text += f"\n\n🚀 Default run cmd: `{escape_md(default_cmd)}`"
    else:
        result_text += "\n\n⚠️ No main file detected. Set run command manually."

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Open Dashboard", callback_data=f"proj:{name}")],
        [InlineKeyboardButton("🔙 My Projects",    callback_data="my_projects")],
    ])
    await status_msg.edit_text(
        f"🎉 *Project {escape_md(name)} ready!*\n\n{result_text}\n\n[████████████] ✅ Complete!",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN,
    )
    context.user_data.clear()
    return ConversationHandler.END

async def new_project_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    if update.callback_query:
        await update.callback_query.answer()
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_start")]])
    msg = update.effective_message
    await msg.reply_text("❌ Cancelled.", reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END

# ─────────────────────────────────────────────────────────────
# ⚙️ Admin Panel
# ─────────────────────────────────────────────────────────────

def owner_only(func):
    import functools
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        if uid != OWNER_ID:
            if update.callback_query:
                await update.callback_query.answer("⛔ Owner only", show_alert=True)
            return
        return await func(update, context)
    return wrapper

@owner_only
async def cb_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    total_users   = await users_col.count_documents({})
    premium_count = await users_col.count_documents({"is_premium": True})
    banned_count  = await users_col.count_documents({"is_banned": True})
    total_proj    = await projects_col.count_documents({})
    running_proj  = await running_project_count()

    # Backup status
    meta = await backups_col.find_one({"type": "backup_meta"})
    if meta:
        backup_time = escape_md(meta["backed_up_at"].strftime("%Y-%m-%d %H:%M UTC"))
        backup_info = f"\n💾 Last Backup: `{backup_time}`"
    else:
        backup_info = "\n💾 Last Backup: `Never`"

    text = (
        f"⚙️ *Admin Panel*\n\n"
        f"👥 Total Users: `{total_users}`\n"
        f"💎 Premium: `{premium_count}`\n"
        f"🚫 Banned: `{banned_count}`\n"
        f"📁 Projects: `{total_proj}`\n"
        f"🟢 Running: `{running_proj}`"
        f"{backup_info}"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 User List",        callback_data="admin:user_list:0"),
         InlineKeyboardButton("🟢 Running Scripts",  callback_data="admin:running")],
        [InlineKeyboardButton("💎 Give Premium",     callback_data="admin:give_premium"),
         InlineKeyboardButton("❌ Remove Premium",   callback_data="admin:remove_premium")],
        [InlineKeyboardButton("⏰ Temp Premium",     callback_data="admin:temp_premium"),
         InlineKeyboardButton("🚫 Ban User",         callback_data="admin:ban")],
        [InlineKeyboardButton("✅ Unban User",       callback_data="admin:unban"),
         InlineKeyboardButton("📢 Broadcast",        callback_data="admin:broadcast_menu")],
        [InlineKeyboardButton("💾 Backup Now",       callback_data="admin:backup_now")],
        [InlineKeyboardButton("🔙 Back",             callback_data="back_start")],
    ])
    await safe_edit(query, text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

@owner_only
async def cb_admin_backup_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Trigger an immediate backup from admin panel."""
    query = update.callback_query
    await query.answer("⏳ Running backup...", show_alert=False)

    await safe_edit(
        query,
        "💾 *Backup in progress...*\n\nThis may take a moment.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]),
        parse_mode=ParseMode.MARKDOWN,
    )

    try:
        all_projects = await projects_col.find({}).to_list(length=10000)
        total_files = 0
        total_size = 0

        for proj in all_projects:
            uid  = proj["user_id"]
            name = proj["name"]
            pdir = project_dir(uid, name)

            if not os.path.exists(pdir):
                continue

            files_data = []
            for root, dirs, files in os.walk(pdir):
                dirs[:] = [d for d in dirs if d not in ("venv", "__pycache__", ".git", "node_modules")]
                for fname in files:
                    if fname in ("output.log",) or fname.endswith(".pyc"):
                        continue
                    fpath   = os.path.join(root, fname)
                    rel_path = os.path.relpath(fpath, pdir)
                    try:
                        file_size = os.path.getsize(fpath)
                        if file_size > 15 * 1024 * 1024:
                            continue
                        try:
                            with open(fpath, "r", encoding="utf-8") as f:
                                content = f.read()
                            content_b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
                            is_binary = False
                        except (UnicodeDecodeError, ValueError):
                            with open(fpath, "rb") as f:
                                content_bytes = f.read()
                            content_b64 = base64.b64encode(content_bytes).decode("ascii")
                            is_binary = True
                        files_data.append({
                            "path":        rel_path,
                            "content_b64": content_b64,
                            "size":        file_size,
                            "is_binary":   is_binary,
                        })
                        total_files += 1
                        total_size  += file_size
                    except Exception:
                        continue

            if files_data:
                await backups_col.delete_many({
                    "type":         "file_backup",
                    "user_id":      uid,
                    "project_name": name,
                })
                await backups_col.insert_one({
                    "type":         "file_backup",
                    "user_id":      uid,
                    "project_name": name,
                    "files":        files_data,
                    "backed_up_at": datetime.now(timezone.utc),
                })

        await backups_col.delete_many({"type": "backup_meta"})
        now = datetime.now(timezone.utc)
        await backups_col.insert_one({
            "type":           "backup_meta",
            "total_projects": len(all_projects),
            "total_files":    total_files,
            "total_size":     total_size,
            "backed_up_at":   now,
        })

        logger.info(f"Manual backup complete: {len(all_projects)} projects, {total_files} files, {total_size} bytes")

        backup_time = escape_md(now.strftime("%Y-%m-%d %H:%M UTC"))
        result_text = (
            f"✅ *Backup Complete!*\n\n"
            f"📁 Projects: `{len(all_projects)}`\n"
            f"📄 Files: `{total_files}`\n"
            f"📦 Size: `{escape_md(fmt_bytes(total_size))}`\n"
            f"🕐 Time: `{backup_time}`"
        )
    except Exception as e:
        logger.error(f"Manual backup failed: {e}")
        result_text = f"❌ *Backup Failed!*\n\n`{escape_md(str(e))}`"

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]])
    await safe_edit(query, result_text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

@owner_only
async def cb_admin_user_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    page = int(query.data.split(":")[-1])
    per_page = 10

    total = await users_col.count_documents({})
    users = await users_col.find({}).skip(page * per_page).limit(per_page).to_list(length=per_page)

    lines = [f"👥 *User List* (page {page+1})\n"]
    for u in users:
        badges = ""
        if u.get("is_premium"):
            badges += " 💎"
        if u.get("is_banned"):
            badges += " 🚫"
        uname = f"@{u['username']}" if u.get("username") else "no-username"
        lines.append(f"`{u['user_id']}` {escape_md(uname)}{badges}")

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"admin:user_list:{page-1}"))
    if (page + 1) * per_page < total:
        nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"admin:user_list:{page+1}"))

    kb_rows = []
    if nav:
        kb_rows.append(nav)
    kb_rows.append([InlineKeyboardButton("🔙 Back", callback_data="admin_panel")])

    await safe_edit(query, "\n".join(lines), reply_markup=InlineKeyboardMarkup(kb_rows), parse_mode=ParseMode.MARKDOWN)

# ─────────────────────────────────────────────────────────────
# ISSUE 1 + 2 FIX: Enhanced cb_admin_running with timezone fix
# ─────────────────────────────────────────────────────────────

@owner_only
async def cb_admin_running(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        running = await projects_col.find({"status": "running"}).to_list(length=100)
        if not running:
            await safe_edit(
                query,
                "🟢 *Running Scripts*\n\nNo projects running.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]),
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        lines = ["🟢 *Running Scripts*\n"]
        kb_rows = []

        for p in running:
            user_doc = await get_user(p["user_id"])
            fname = user_doc.get("first_name", "Unknown") if user_doc else "Unknown"
            uname = f"@{user_doc['username']}" if user_doc and user_doc.get("username") else "no-username"
            pid = p.get("pid", "N/A")
            # ISSUE 1 FIX: timezone-safe uptime calculation
            uptime = "N/A"
            if p.get("started_at"):
                try:
                    started = p["started_at"]
                    if started.tzinfo is None:
                        started = started.replace(tzinfo=timezone.utc)
                    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
                    uptime = fmt_uptime(max(0, elapsed))
                except Exception:
                    uptime = "N/A"

            lines.append(
                f"- - - - - - - - - - -\n"
                f"👤 {fname} ({uname})\n"
                f"📁 Project: {p['name']}\n"
                f"🔹 PID: {pid} | Uptime: {uptime}"
            )
            kb_rows.append([
                InlineKeyboardButton(f"⏹ Stop {p['name']}", callback_data=f"admin_stop:{p['user_id']}:{p['name']}"),
                InlineKeyboardButton(f"📥 Download", callback_data=f"admin_dl:{p['user_id']}:{p['name']}"),
            ])

        kb_rows.append([InlineKeyboardButton("👥 All Users & Projects", callback_data="admin:all_projects:0")])
        kb_rows.append([InlineKeyboardButton("🔙 Back", callback_data="admin_panel")])

        full_text = "\n".join(lines)
        if len(full_text) > 4000:
            full_text = full_text[:3900] + "\n...(truncated)"

        await safe_edit(query, full_text, reply_markup=InlineKeyboardMarkup(kb_rows), parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"cb_admin_running error: {e}")
        await safe_edit(
            query,
            f"❌ Error loading running scripts: {str(e)[:200]}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]),
        )

# ─────────────────────────────────────────────────────────────
# ISSUE 2 NEW HANDLERS: All Users & Projects, Admin Run, Admin Download
# ─────────────────────────────────────────────────────────────

@owner_only
async def cb_admin_all_projects(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show all users with their projects for admin management."""
    query = update.callback_query
    await query.answer()
    page = int(query.data.split(":")[-1])
    per_page = 5

    # Get all users who have projects
    all_projects = await projects_col.find({}).to_list(length=10000)

    # Group by user
    user_projects = {}
    for p in all_projects:
        uid = p["user_id"]
        if uid not in user_projects:
            user_projects[uid] = []
        user_projects[uid].append(p)

    user_ids = list(user_projects.keys())
    total = len(user_ids)
    start = page * per_page
    end = min(start + per_page, total)
    page_user_ids = user_ids[start:end]

    if not page_user_ids:
        await safe_edit(
            query,
            "👥 *All Users & Projects*\n\nNo projects found.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin:running")]]),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    lines = [f"👥 *All Users & Projects* (page {page+1})\n"]
    kb_rows = []

    for uid in page_user_ids:
        user_doc = await get_user(uid)
        fname = user_doc.get("first_name", "Unknown") if user_doc else "Unknown"
        uname = f"@{user_doc['username']}" if user_doc and user_doc.get("username") else ""

        projects = user_projects[uid]
        proj_lines = []
        for p in projects:
            status_icon = "🟢" if p.get("status") == "running" else "🔴"
            proj_lines.append(f"  {status_icon} {p['name']}")

            # Add control buttons for each project
            if p.get("status") == "running":
                kb_rows.append([
                    InlineKeyboardButton(f"⏹ Stop {p['name']}", callback_data=f"admin_stop:{uid}:{p['name']}"),
                    InlineKeyboardButton(f"📥 DL {p['name']}", callback_data=f"admin_dl:{uid}:{p['name']}"),
                ])
            else:
                kb_rows.append([
                    InlineKeyboardButton(f"▶️ Run {p['name']}", callback_data=f"admin_run:{uid}:{p['name']}"),
                    InlineKeyboardButton(f"📥 DL {p['name']}", callback_data=f"admin_dl:{uid}:{p['name']}"),
                ])

        lines.append(
            f"- - - - - - - - - - -\n"
            f"👤 {fname} {uname} (`{uid}`)\n"
            f"📁 {len(projects)} project(s):\n" + "\n".join(proj_lines)
        )

    # Pagination
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"admin:all_projects:{page-1}"))
    if end < total:
        nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"admin:all_projects:{page+1}"))
    if nav:
        kb_rows.append(nav)

    kb_rows.append([InlineKeyboardButton("🔙 Back", callback_data="admin:running")])

    full_text = "\n".join(lines)
    if len(full_text) > 4000:
        full_text = full_text[:3900] + "\n...(truncated)"

    await safe_edit(query, full_text, reply_markup=InlineKeyboardMarkup(kb_rows), parse_mode=ParseMode.MARKDOWN)


@owner_only
async def cb_admin_run_project(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, uid_str, name = query.data.split(":", 2)
    uid = int(uid_str)

    p = await get_project(uid, name)
    if not p:
        await safe_edit(query, "❌ Project not found.")
        return

    if not p.get("run_command"):
        await safe_edit(query, f"❌ No run command set for {name}.")
        return

    try:
        await start_project_process(uid, name)
        await query.answer(f"▶️ {name} started!", show_alert=True)
        # Refresh the page
        query.data = f"admin:all_projects:0"
        await cb_admin_all_projects(update, context)
    except Exception as e:
        await safe_edit(query, f"❌ Failed to start: {str(e)[:200]}")


@owner_only
async def cb_admin_download_project(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("📥 Creating zip...", show_alert=False)
    _, uid_str, name = query.data.split(":", 2)
    uid = int(uid_str)

    pdir = project_dir(uid, name)
    if not os.path.exists(pdir):
        await query.answer("❌ Project directory not found!", show_alert=True)
        return

    # Create zip file
    zip_path = os.path.join(PROJECTS_ROOT, f"{uid}_{name}.zip")
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(pdir):
                # Skip venv and __pycache__
                dirs[:] = [d for d in dirs if d not in ("venv", "__pycache__", ".git", "node_modules")]
                for fname_file in files:
                    if fname_file in ("output.log",) or fname_file.endswith(".pyc"):
                        continue
                    fpath = os.path.join(root, fname_file)
                    arcname = os.path.relpath(fpath, pdir)
                    zf.write(fpath, arcname)

        # Send the zip file
        with open(zip_path, "rb") as f:
            await query.message.reply_document(
                document=f,
                filename=f"{name}.zip",
                caption=f"📥 Project: {name}\nUser ID: {uid}",
            )
    except Exception as e:
        logger.error(f"Admin download failed: {e}")
        await query.answer(f"❌ Download failed: {str(e)[:100]}", show_alert=True)
    finally:
        # Clean up zip
        if os.path.exists(zip_path):
            os.remove(zip_path)


@owner_only
async def cb_admin_stop_project(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, uid_str, name = query.data.split(":", 2)
    uid = int(uid_str)
    await kill_project(uid, name)
    await projects_col.update_one(
        {"user_id": uid, "name": name},
        {"$set": {"admin_stopped": True}},
    )
    await safe_edit(
        query,
        f"✅ Project *{escape_md(name)}* stopped (admin).",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin:running")]]),
        parse_mode=ParseMode.MARKDOWN,
    )

# Admin give/remove/temp premium, ban/unban, broadcast — ConversationHandler

@owner_only
async def cb_admin_give_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["admin_action"] = "give_premium"
    await safe_edit(query, "💎 *Give Premium*\n\nSend the user ID:", parse_mode=ParseMode.MARKDOWN)
    return ADMIN_GIVE_PREMIUM_ID

async def admin_give_premium_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Invalid ID. Send a numeric user ID:", parse_mode=ParseMode.MARKDOWN)
        return ADMIN_GIVE_PREMIUM_ID

    await users_col.update_one(
        {"user_id": uid},
        {"$set": {"is_premium": True, "premium_expiry": None}},
    )
    try:
        await update.get_bot().send_message(uid, "🎉 You have been granted *Premium*! Enjoy unlimited projects!", parse_mode=ParseMode.MARKDOWN)
    except Exception:
        pass
    await update.message.reply_text(f"✅ Premium granted to `{uid}`.", parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END

@owner_only
async def cb_admin_remove_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await safe_edit(query, "❌ *Remove Premium*\n\nSend the user ID:", parse_mode=ParseMode.MARKDOWN)
    return ADMIN_REMOVE_PREMIUM_ID

async def admin_remove_premium_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Invalid ID:", parse_mode=ParseMode.MARKDOWN)
        return ADMIN_REMOVE_PREMIUM_ID

    await users_col.update_one({"user_id": uid}, {"$set": {"is_premium": False, "premium_expiry": None}})
    await update.message.reply_text(f"✅ Premium removed from `{uid}`.", parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END

@owner_only
async def cb_admin_temp_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await safe_edit(query, "⏰ *Temp Premium*\n\nSend the user ID:", parse_mode=ParseMode.MARKDOWN)
    return ADMIN_TEMP_PREMIUM_ID

async def admin_temp_premium_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Invalid ID:", parse_mode=ParseMode.MARKDOWN)
        return ADMIN_TEMP_PREMIUM_ID
    context.user_data["temp_premium_uid"] = uid
    await update.message.reply_text(
        "⏰ Send duration (e.g. `24h` or `7d`):",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ADMIN_TEMP_PREMIUM_DUR

async def admin_temp_premium_dur(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    uid  = context.user_data.get("temp_premium_uid")
    m = re.match(r"^(\d+)([hd])$", text)
    if not m:
        await update.message.reply_text("❌ Invalid format. Use `24h` or `7d`:", parse_mode=ParseMode.MARKDOWN)
        return ADMIN_TEMP_PREMIUM_DUR

    amount, unit = int(m.group(1)), m.group(2)
    seconds = amount * 3600 if unit == "h" else amount * 86400
    expiry  = datetime.fromtimestamp(time.time() + seconds, tz=timezone.utc)

    await users_col.update_one(
        {"user_id": uid},
        {"$set": {"is_premium": True, "premium_expiry": expiry}},
    )
    try:
        await update.get_bot().send_message(uid, f"🎉 You received *Temp Premium* for {escape_md(text)}!", parse_mode=ParseMode.MARKDOWN)
    except Exception:
        pass
    await update.message.reply_text(f"✅ Temp premium set for `{uid}` — expires {escape_md(expiry.strftime('%Y-%m-%d %H:%M UTC'))}.", parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END

@owner_only
async def cb_admin_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await safe_edit(query, "🚫 *Ban User*\n\nSend the user ID:", parse_mode=ParseMode.MARKDOWN)
    return ADMIN_BAN_ID

async def admin_ban_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Invalid ID:", parse_mode=ParseMode.MARKDOWN)
        return ADMIN_BAN_ID

    await users_col.update_one({"user_id": uid}, {"$set": {"is_banned": True}})
    # Stop all their projects
    user_projects = await projects_col.find({"user_id": uid, "status": "running"}).to_list(length=100)
    for p in user_projects:
        await kill_project(uid, p["name"])
    await update.message.reply_text(f"✅ User `{uid}` banned and all projects stopped.", parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END

@owner_only
async def cb_admin_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await safe_edit(query, "✅ *Unban User*\n\nSend the user ID:", parse_mode=ParseMode.MARKDOWN)
    return ADMIN_UNBAN_ID

async def admin_unban_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Invalid ID:", parse_mode=ParseMode.MARKDOWN)
        return ADMIN_UNBAN_ID

    await users_col.update_one({"user_id": uid}, {"$set": {"is_banned": False}})
    try:
        await update.get_bot().send_message(uid, "✅ You have been unbanned! You can use the bot again.", parse_mode=ParseMode.MARKDOWN)
    except Exception:
        pass
    await update.message.reply_text(f"✅ User `{uid}` unbanned.", parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END

@owner_only
async def cb_admin_broadcast_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Broadcast All",  callback_data="admin:broadcast_all")],
        [InlineKeyboardButton("📩 Send to User",   callback_data="admin:send_to_user")],
        [InlineKeyboardButton("🔙 Back",           callback_data="admin_panel")],
    ])
    await safe_edit(query, "📢 *Broadcast Menu*", reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

@owner_only
async def cb_admin_broadcast_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["broadcast_type"] = "all"
    await safe_edit(query, "📢 *Broadcast All*\n\nSend the message:", parse_mode=ParseMode.MARKDOWN)
    return ADMIN_BROADCAST_MSG

@owner_only
async def cb_admin_send_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["broadcast_type"] = "user"
    await safe_edit(query, "📩 *Send to User*\n\nSend the target user ID:", parse_mode=ParseMode.MARKDOWN)
    return ADMIN_SEND_USER_ID

async def admin_send_user_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Invalid ID:", parse_mode=ParseMode.MARKDOWN)
        return ADMIN_SEND_USER_ID
    context.user_data["broadcast_target"] = uid
    await update.message.reply_text("Send the message:", parse_mode=ParseMode.MARKDOWN)
    return ADMIN_SEND_USER_MSG

async def admin_send_user_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = context.user_data.get("broadcast_target")
    msg = update.message.text
    try:
        await update.get_bot().send_message(uid, msg)
        await update.message.reply_text(f"✅ Sent to `{uid}`.", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text(f"❌ Failed: {escape_md(str(e))}", parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END

async def admin_broadcast_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg  = update.message.text
    bot  = update.get_bot()
    all_users = await users_col.find({}).to_list(length=10000)
    sent = failed = 0
    for u in all_users:
        try:
            await bot.send_message(u["user_id"], msg)
            sent += 1
        except Exception:
            failed += 1
    await update.message.reply_text(
        f"📢 Broadcast complete!\n✅ Sent: `{sent}`\n❌ Failed: `{failed}`",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ConversationHandler.END

async def admin_conv_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    if update.callback_query:
        await update.callback_query.answer()
    await (update.effective_message).reply_text("❌ Cancelled.", parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END

# ─────────────────────────────────────────────────────────────
# ⏰ Feature 1: Auto-Restart Toggle Handler
# ─────────────────────────────────────────────────────────────

async def cb_toggle_auto_restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    name = query.data.split(":", 1)[1]

    p = await get_project(uid, name)
    if not p:
        await safe_edit(query, "❌ Project not found.")
        return

    current = p.get("auto_restart", True)
    new_val = not current

    await projects_col.update_one(
        {"user_id": uid, "name": name},
        {"$set": {"auto_restart": new_val}},
    )

    status = "✅ ON" if new_val else "❌ OFF"
    await query.answer(f"Auto-Restart: {status}", show_alert=True)

    # Refresh dashboard
    p = await get_project(uid, name)
    await safe_edit(
        query,
        project_dashboard_text(p),
        reply_markup=project_dashboard_kb(uid, name, p.get("auto_restart", True), p.get("status") == "running"),
    )

# ─────────────────────────────────────────────────────────────
# 🔐 Feature 3: Environment Variables Manager
# ─────────────────────────────────────────────────────────────

async def cb_envvars(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show environment variables for a project."""
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    name = query.data.split(":", 1)[1]

    if await is_banned(uid):
        await safe_edit(query, "🚫 You are banned. Contact owner.")
        return

    pdir = project_dir(uid, name)
    env_path = os.path.join(pdir, ".env")

    env_vars = {}
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    env_vars[key.strip()] = value.strip()

    if not env_vars:
        text = f"🔐 *Environment Variables — {escape_md(name)}*\n\nNo variables set yet.\n\n_Tip: Click Add Variable and send like:_\n`BOT_TOKEN=your_value`"
    else:
        lines = [f"🔐 *Environment Variables — {escape_md(name)}*\n"]
        for key, value in env_vars.items():
            # Mask value: show first 3 chars + ***
            masked = value[:3] + "***" if len(value) > 3 else "***"
            lines.append(f"• `{key}` = `{masked}`")
        text = "\n".join(lines)

    kb_rows = []
    # Show edit/delete button for each var
    for key in env_vars:
        kb_rows.append([
            InlineKeyboardButton(f"✏️ {key}", callback_data=f"env_edit:{name}:{key}"),
            InlineKeyboardButton(f"🗑 {key}", callback_data=f"env_del:{name}:{key}"),
        ])
    kb_rows.append([InlineKeyboardButton("➕ Add Variable", callback_data=f"env_add:{name}")])
    kb_rows.append([InlineKeyboardButton("🔙 Back", callback_data=f"proj:{name}")])

    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(kb_rows), parse_mode=ParseMode.MARKDOWN)


async def cb_env_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    name = query.data.split(":", 1)[1]
    context.user_data["env_project"] = name

    await safe_edit(
        query,
        "➕ *Add Environment Variables*\n\n"
        "Send your variables in any format:\n\n"
        "1️⃣ *Single variable:*\n"
        "`API_KEY=your_value`\n\n"
        "2️⃣ *Multiple at once (one per line):*\n"
        "`TOKEN=abc123`\n"
        "`DB_URI=mongodb://...`\n"
        "`OWNER_ID=12345`\n\n"
        "3️⃣ *Just key name:*\n"
        "`API_KEY`\n"
        "_(bot will ask for value next)_\n\n"
        "💡 Spaces around `=` are fine!",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"envvars:{name}")]]),
        parse_mode=ParseMode.MARKDOWN,
    )
    return ENV_ADD_KEY


async def env_add_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    name = context.user_data.get("env_project")
    uid = update.effective_user.id
    pdir = project_dir(uid, name)
    env_path = os.path.join(pdir, ".env")

    # Check if multi-line or contains = (KEY=VALUE format)
    lines = text.strip().split("\n")
    pairs_to_save = []

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if key:
                pairs_to_save.append((key, value))

    if pairs_to_save:
        # Read existing env file
        existing = {}
        existing_order = []
        if os.path.exists(env_path):
            with open(env_path, "r") as f:
                for eline in f:
                    eline_stripped = eline.strip()
                    if eline_stripped and not eline_stripped.startswith("#") and "=" in eline_stripped:
                        ekey, _, evalue = eline_stripped.partition("=")
                        ekey = ekey.strip()
                        existing[ekey] = evalue.strip()
                        existing_order.append(ekey)
                    elif eline_stripped:
                        # Keep comments and other lines
                        pass

        # Update/add new pairs
        for key, value in pairs_to_save:
            existing[key] = value
            if key not in existing_order:
                existing_order.append(key)

        # Write all vars back
        with open(env_path, "w") as f:
            for key in existing_order:
                f.write(f"{key}={existing[key]}\n")
            # Write any new keys not in order
            for key, value in pairs_to_save:
                if key not in existing_order:
                    f.write(f"{key}={value}\n")

        saved_keys = [k for k, v in pairs_to_save]
        saved_list = "\n".join([f"• `{k}` ✅" for k in saved_keys])

        await update.message.reply_text(
            f"✅ *{len(pairs_to_save)} variable(s) saved!*\n\n"
            f"{saved_list}\n\n"
            f"_Restart your project for changes to take effect._",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Add More", callback_data=f"env_add:{name}")],
                [InlineKeyboardButton("🔙 Back to Env Vars", callback_data=f"envvars:{name}")],
            ]),
        )
        context.user_data.pop("env_key", None)
        context.user_data.pop("env_project", None)
        return ConversationHandler.END

    # No = found — treat as single KEY name
    key = text.strip().split()[0] if text.strip() else ""
    if not key or len(key) > 100:
        await update.message.reply_text(
            "❌ Could not parse variables.\n\n"
            "Send in one of these formats:\n\n"
            "1️⃣ Single: `API_KEY=your_value`\n\n"
            "2️⃣ Multiple (one per line):\n"
            "`TOKEN=abc123`\n"
            "`DB_URI=mongodb://...`\n"
            "`PORT=8080`\n\n"
            "3️⃣ Just key name: `API_KEY`\n"
            "_(bot will ask for value next)_",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ENV_ADD_KEY

    context.user_data["env_key"] = key
    await update.message.reply_text(
        f"Now send the value for `{key}`:",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ENV_ADD_VALUE


async def env_add_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = update.message.text.strip()
    name = context.user_data.get("env_project")
    key = context.user_data.get("env_key")
    uid = update.effective_user.id

    pdir = project_dir(uid, name)
    env_path = os.path.join(pdir, ".env")

    # Read existing env vars
    env_lines = []
    key_found = False
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            for line in f:
                if line.strip().startswith(f"{key}="):
                    env_lines.append(f"{key}={value}\n")
                    key_found = True
                else:
                    env_lines.append(line)

    if not key_found:
        env_lines.append(f"{key}={value}\n")

    with open(env_path, "w") as f:
        f.writelines(env_lines)

    await update.message.reply_text(
        f"✅ Variable `{key}` saved!\n\n_Restart your project for changes to take effect._",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Env Vars", callback_data=f"envvars:{name}")]]),
    )
    context.user_data.pop("env_key", None)
    context.user_data.pop("env_project", None)
    return ConversationHandler.END


async def cb_env_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":", 2)
    name = parts[1]
    key = parts[2]
    context.user_data["env_project"] = name
    context.user_data["env_key"] = key

    await safe_edit(
        query,
        f"✏️ *Edit `{key}`*\n\nSend the new value:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"envvars:{name}")]]),
        parse_mode=ParseMode.MARKDOWN,
    )
    return ENV_EDIT_VALUE


async def env_edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Same as env_add_value — it overwrites the key
    return await env_add_value(update, context)


async def cb_env_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":", 2)
    name = parts[1]
    key = parts[2]
    uid = query.from_user.id

    pdir = project_dir(uid, name)
    env_path = os.path.join(pdir, ".env")

    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            lines = f.readlines()
        with open(env_path, "w") as f:
            for line in lines:
                if not line.strip().startswith(f"{key}="):
                    f.write(line)

    await query.answer(f"🗑 {key} deleted!", show_alert=True)

    # Refresh the env vars screen
    query.data = f"envvars:{name}"
    await cb_envvars(update, context)

# ─────────────────────────────────────────────────────────────
# Background: process monitor (Feature 1 & 2 integrated)
# ─────────────────────────────────────────────────────────────

async def process_monitor():
    while True:
        await asyncio.sleep(30)
        try:
            running = await projects_col.find({"status": "running"}).to_list(length=1000)
            for p in running:
                pid = p.get("pid")
                if pid and not psutil.pid_exists(pid):
                    # Try to get exit code
                    key  = f"{p['user_id']}:{p['name']}"
                    proc = context_store.get(key)
                    code = None
                    if proc:
                        code = proc.returncode

                    await projects_col.update_one(
                        {"user_id": p["user_id"], "name": p["name"]},
                        {"$set": {"status": "stopped", "pid": None, "exit_code": code}},
                    )

                    logger.info(f"Process {key} exited with code {code}")

                    # Feature 1: Auto-restart logic
                    if p.get("auto_restart", True) and code != 0 and not p.get("admin_stopped"):
                        # Check restart limit: max 3 restarts in 5 minutes
                        now = datetime.now(timezone.utc)
                        last_restart = p.get("last_restart_at")
                        restart_count = p.get("restart_count", 0)

                        # Reset counter if last restart was more than 5 minutes ago
                        if last_restart:
                            if last_restart.tzinfo is None:
                                last_restart = last_restart.replace(tzinfo=timezone.utc)
                            if (now - last_restart).total_seconds() > 300:
                                restart_count = 0

                        if restart_count < 3:
                            try:
                                logger.info(f"Auto-restarting {key} (attempt {restart_count + 1}/3)")
                                await asyncio.sleep(3)  # Brief delay before restart
                                await start_project_process(p["user_id"], p["name"])
                                await projects_col.update_one(
                                    {"user_id": p["user_id"], "name": p["name"]},
                                    {"$set": {"restart_count": restart_count + 1, "last_restart_at": now}},
                                )

                                # Feature 2: Send auto-restart notification
                                if notification_bot:
                                    msg_text = (
                                        f"🔄 *Auto-Restart*\n\n"
                                        f"Project `{p['name']}` crashed (exit code: {code}).\n"
                                        f"Auto-restarted successfully! ({restart_count + 1}/3)"
                                    )
                                    try:
                                        await notification_bot.send_message(
                                            chat_id=p["user_id"],
                                            text=msg_text,
                                            parse_mode=ParseMode.MARKDOWN,
                                        )
                                    except Exception:
                                        pass

                            except Exception as e:
                                logger.error(f"Auto-restart failed for {key}: {e}")
                        else:
                            # Too many restarts — notify user
                            logger.warning(f"Auto-restart limit reached for {key}")
                            if notification_bot:
                                msg_text = (
                                    f"⚠️ *Auto-Restart Limit Reached*\n\n"
                                    f"Project `{p['name']}` crashed {restart_count} times in 5 minutes.\n"
                                    f"Auto-restart disabled temporarily.\n\n"
                                    f"Check your logs and fix the issue, then restart manually."
                                )
                                try:
                                    await notification_bot.send_message(
                                        chat_id=p["user_id"],
                                        text=msg_text,
                                        parse_mode=ParseMode.MARKDOWN,
                                    )
                                except Exception:
                                    pass

                    # Feature 2: Crash notification (auto_restart is OFF)
                    elif code != 0 and not p.get("admin_stopped"):
                        if notification_bot:
                            try:
                                log_path = os.path.join(project_dir(p["user_id"], p["name"]), "output.log")
                                error_lines = ""
                                if os.path.exists(log_path):
                                    with open(log_path, "r", errors="replace") as f:
                                        lines_list = f.readlines()
                                    error_lines = "".join(lines_list[-10:]).strip()
                                    if len(error_lines) > 500:
                                        error_lines = "..." + error_lines[-500:]

                                msg_text = (
                                    f"❌ *Project Crashed*\n\n"
                                    f"Project: `{p['name']}`\n"
                                    f"Exit Code: `{code}`\n"
                                    f"Auto-Restart: OFF\n\n"
                                    f"📋 *Last Log Lines:*\n```\n{error_lines}\n```"
                                )
                                if len(msg_text) > 4000:
                                    msg_text = msg_text[:4000] + "..."

                                await notification_bot.send_message(
                                    chat_id=p["user_id"],
                                    text=msg_text,
                                    parse_mode=ParseMode.MARKDOWN,
                                )
                            except Exception:
                                pass

        except Exception as e:
            logger.warning(f"Monitor error: {e}")

# ─────────────────────────────────────────────────────────────
# 💾 Auto Backup Task (runs every 5 minutes)
# ─────────────────────────────────────────────────────────────

async def backup_task():
    """Runs every 5 minutes. Backs up all project files to MongoDB."""
    while True:
        await asyncio.sleep(300)  # 5 minutes
        try:
            all_projects = await projects_col.find({}).to_list(length=10000)

            total_files = 0
            total_size  = 0

            for proj in all_projects:
                uid  = proj["user_id"]
                name = proj["name"]
                pdir = project_dir(uid, name)

                if not os.path.exists(pdir):
                    continue

                files_data = []
                # Walk through project directory, SKIP venv/, __pycache__/, output.log
                for root, dirs, files in os.walk(pdir):
                    # Skip venv and __pycache__ directories
                    dirs[:] = [d for d in dirs if d not in ("venv", "__pycache__", ".git", "node_modules")]

                    for fname in files:
                        if fname in ("output.log",) or fname.endswith(".pyc"):
                            continue

                        fpath    = os.path.join(root, fname)
                        rel_path = os.path.relpath(fpath, pdir)

                        try:
                            # Read file (try text first, fall back to binary)
                            try:
                                with open(fpath, "r", encoding="utf-8") as f:
                                    content = f.read()
                                content_b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
                                is_binary = False
                            except (UnicodeDecodeError, ValueError):
                                with open(fpath, "rb") as f:
                                    content_bytes = f.read()
                                content_b64 = base64.b64encode(content_bytes).decode("ascii")
                                is_binary = True

                            file_size = os.path.getsize(fpath)
                            # Skip files > 15MB (MongoDB document limit is 16MB)
                            if file_size > 15 * 1024 * 1024:
                                continue

                            files_data.append({
                                "path":        rel_path,
                                "content_b64": content_b64,
                                "size":        file_size,
                                "is_binary":   is_binary,
                            })
                            total_files += 1
                            total_size  += file_size
                        except Exception:
                            continue

                if files_data:
                    # Delete old backup for this project, then insert new
                    await backups_col.delete_many({
                        "type":         "file_backup",
                        "user_id":      uid,
                        "project_name": name,
                    })
                    await backups_col.insert_one({
                        "type":         "file_backup",
                        "user_id":      uid,
                        "project_name": name,
                        "files":        files_data,
                        "backed_up_at": datetime.now(timezone.utc),
                    })

            # Update backup metadata
            await backups_col.delete_many({"type": "backup_meta"})
            await backups_col.insert_one({
                "type":           "backup_meta",
                "total_projects": len(all_projects),
                "total_files":    total_files,
                "total_size":     total_size,
                "backed_up_at":   datetime.now(timezone.utc),
            })

            logger.info(f"Backup complete: {len(all_projects)} projects, {total_files} files, {total_size} bytes")

        except Exception as e:
            logger.error(f"Backup failed: {e}")

# ─────────────────────────────────────────────────────────────
# 🔄 Keep-Alive Task (prevents Render free plan from sleeping)
# ─────────────────────────────────────────────────────────────

async def keep_alive_task():
    """Ping own health endpoint every 10 minutes to prevent Render free plan from sleeping."""
    import urllib.request
    health_url = f"{BASE_URL}/health"
    logger.info(f"Keep-alive task started. Pinging {health_url} every 10 minutes.")

    while True:
        await asyncio.sleep(600)  # 10 minutes
        try:
            # Run in executor to not block event loop
            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(
                None,
                lambda: urllib.request.urlopen(health_url, timeout=30).status
            )
            logger.info(f"Keep-alive ping OK ({resp})")
        except Exception as e:
            logger.warning(f"Keep-alive ping failed: {e}")

# ─────────────────────────────────────────────────────────────
# 🔄 Auto Restore (runs ONCE at startup in post_init)
# ─────────────────────────────────────────────────────────────

async def restore_from_backup():
    """Restore all project files from MongoDB backup. Called once at startup."""
    try:
        logger.info("Checking for backups to restore...")

        meta = await backups_col.find_one({"type": "backup_meta"})
        if not meta:
            logger.info("No backup found. Fresh start.")
            return

        logger.info(
            f"Found backup from {meta['backed_up_at']} — "
            f"{meta['total_projects']} projects, {meta['total_files']} files"
        )

        backups = backups_col.find({"type": "file_backup"})
        restored_projects = 0
        restored_files    = 0

        async for backup in backups:
            uid  = backup["user_id"]
            name = backup["project_name"]
            pdir = project_dir(uid, name)

            os.makedirs(pdir, exist_ok=True)

            for file_data in backup.get("files", []):
                rel_path    = file_data["path"]
                content_b64 = file_data["content_b64"]
                is_binary   = file_data.get("is_binary", False)

                file_path = os.path.join(pdir, rel_path)
                parent_dir = os.path.dirname(file_path)
                if parent_dir:
                    os.makedirs(parent_dir, exist_ok=True)

                try:
                    decoded = base64.b64decode(content_b64)
                    if is_binary:
                        with open(file_path, "wb") as f:
                            f.write(decoded)
                    else:
                        with open(file_path, "w", encoding="utf-8") as f:
                            f.write(decoded.decode("utf-8"))
                    restored_files += 1
                except Exception as e:
                    logger.warning(f"Failed to restore {rel_path}: {e}")

            restored_projects += 1

        logger.info(f"Files restored: {restored_projects} projects, {restored_files} files")

        # Setup venvs in background (don't block startup)
        asyncio.create_task(setup_venvs_background())

    except Exception as e:
        logger.error(f"Restore failed (non-fatal): {e}")


async def setup_venvs_background():
    """Setup virtualenvs for all restored projects in background."""
    try:
        all_projects = await projects_col.find({}).to_list(length=10000)
        for proj in all_projects:
            uid  = proj["user_id"]
            name = proj["name"]
            pdir = project_dir(uid, name)
            venv_dir = os.path.join(pdir, "venv")

            if os.path.exists(pdir) and not os.path.exists(venv_dir):
                try:
                    proc = await create_subprocess_exec(
                        sys.executable, "-m", "venv", venv_dir,
                        stdout=PIPE, stderr=PIPE
                    )
                    await asyncio.wait_for(proc.communicate(), timeout=120)

                    req_file = os.path.join(pdir, "requirements.txt")
                    pip_path = os.path.join(pdir, "venv", "bin", "pip")
                    if os.path.exists(req_file) and os.path.exists(pip_path):
                        proc2 = await create_subprocess_exec(
                            pip_path, "install", "-r", req_file, "--quiet",
                            stdout=PIPE, stderr=PIPE, cwd=pdir
                        )
                        await asyncio.wait_for(proc2.communicate(), timeout=300)
                    logger.info(f"Venv setup complete for {name}")
                except Exception as e:
                    logger.warning(f"Failed to setup venv for {name}: {e}")
    except Exception as e:
        logger.error(f"Background venv setup failed: {e}")

# ─────────────────────────────────────────────────────────────
# App setup
# ─────────────────────────────────────────────────────────────

def build_application() -> Application:
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .post_init(post_init)
        .build()
    )

    # New project conversation
    new_proj_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cb_new_project, pattern="^new_project$")],
        states={
            NEW_PROJECT_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, new_project_name),
                CallbackQueryHandler(new_project_cancel, pattern="^back_start$"),
            ],
            NEW_PROJECT_FILES: [
                MessageHandler(filters.Document.ALL, new_project_file),
                CommandHandler("done", new_project_done_cmd),
                CallbackQueryHandler(new_project_done_cb, pattern="^upload_done$"),
                CallbackQueryHandler(new_project_cancel, pattern="^back_start$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", new_project_cancel),
            CommandHandler("start", new_project_cancel),
        ],
        per_chat=True,
    )

    # Edit run command conversation
    editcmd_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cb_editcmd_start, pattern=r"^editcmd:")],
        states={
            EDIT_RUN_CMD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, editcmd_receive),
                CallbackQueryHandler(admin_conv_cancel, pattern=r"^proj:"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", admin_conv_cancel),
            CommandHandler("start", admin_conv_cancel),
        ],
        per_chat=True,
    )

    # Env vars conversation (Feature 3)
    env_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(cb_env_add_start,  pattern=r"^env_add:"),
            CallbackQueryHandler(cb_env_edit_start, pattern=r"^env_edit:"),
        ],
        states={
            ENV_ADD_KEY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, env_add_key),
            ],
            ENV_ADD_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, env_add_value),
            ],
            ENV_EDIT_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, env_edit_value),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", admin_conv_cancel),
            CommandHandler("start", admin_conv_cancel),
        ],
        per_chat=True,
    )

    # Admin conversations (combined into one handler with different states)
    admin_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(cb_admin_give_premium,   pattern="^admin:give_premium$"),
            CallbackQueryHandler(cb_admin_remove_premium, pattern="^admin:remove_premium$"),
            CallbackQueryHandler(cb_admin_temp_premium,   pattern="^admin:temp_premium$"),
            CallbackQueryHandler(cb_admin_ban,            pattern="^admin:ban$"),
            CallbackQueryHandler(cb_admin_unban,          pattern="^admin:unban$"),
            CallbackQueryHandler(cb_admin_broadcast_all,  pattern="^admin:broadcast_all$"),
            CallbackQueryHandler(cb_admin_send_to_user,   pattern="^admin:send_to_user$"),
        ],
        states={
            ADMIN_GIVE_PREMIUM_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_give_premium_id),
                CallbackQueryHandler(admin_conv_cancel, pattern="^admin_panel$"),
            ],
            ADMIN_REMOVE_PREMIUM_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_remove_premium_id),
                CallbackQueryHandler(admin_conv_cancel, pattern="^admin_panel$"),
            ],
            ADMIN_TEMP_PREMIUM_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_temp_premium_id),
                CallbackQueryHandler(admin_conv_cancel, pattern="^admin_panel$"),
            ],
            ADMIN_TEMP_PREMIUM_DUR: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_temp_premium_dur),
                CallbackQueryHandler(admin_conv_cancel, pattern="^admin_panel$"),
            ],
            ADMIN_BAN_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_ban_id),
                CallbackQueryHandler(admin_conv_cancel, pattern="^admin_panel$"),
            ],
            ADMIN_UNBAN_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_unban_id),
                CallbackQueryHandler(admin_conv_cancel, pattern="^admin_panel$"),
            ],
            ADMIN_BROADCAST_MSG: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_broadcast_msg),
                CallbackQueryHandler(admin_conv_cancel, pattern="^admin_panel$"),
            ],
            ADMIN_SEND_USER_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_send_user_id),
                CallbackQueryHandler(admin_conv_cancel, pattern="^admin_panel$"),
            ],
            ADMIN_SEND_USER_MSG: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_send_user_msg),
                CallbackQueryHandler(admin_conv_cancel, pattern="^admin_panel$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", admin_conv_cancel),
            CommandHandler("start", admin_conv_cancel),
        ],
        per_chat=True,
    )

    # Register handlers (conversations first!)
    app.add_handler(new_proj_conv)
    app.add_handler(editcmd_conv)
    app.add_handler(env_conv)
    app.add_handler(admin_conv)

    app.add_handler(CommandHandler("start", start))

    # Callback handlers
    app.add_handler(CallbackQueryHandler(cb_start,             pattern="^back_start$"))
    app.add_handler(CallbackQueryHandler(cb_my_projects,       pattern="^my_projects$"))
    app.add_handler(CallbackQueryHandler(cb_bot_status,        pattern="^bot_status$"))
    app.add_handler(CallbackQueryHandler(cb_premium,           pattern="^premium$"))
    app.add_handler(CallbackQueryHandler(cb_admin_panel,       pattern="^admin_panel$"))
    app.add_handler(CallbackQueryHandler(cb_admin_user_list,   pattern=r"^admin:user_list:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_admin_running,     pattern="^admin:running$"))
    app.add_handler(CallbackQueryHandler(cb_admin_stop_project,pattern=r"^admin_stop:"))
    app.add_handler(CallbackQueryHandler(cb_admin_broadcast_menu, pattern="^admin:broadcast_menu$"))
    app.add_handler(CallbackQueryHandler(cb_admin_backup_now,  pattern="^admin:backup_now$"))

    # ISSUE 2 NEW HANDLERS: All Projects, Run Project, Download Project
    app.add_handler(CallbackQueryHandler(cb_admin_all_projects,     pattern=r"^admin:all_projects:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_admin_run_project,      pattern=r"^admin_run:"))
    app.add_handler(CallbackQueryHandler(cb_admin_download_project, pattern=r"^admin_dl:"))

    app.add_handler(CallbackQueryHandler(cb_project_dashboard, pattern=r"^proj:"))
    app.add_handler(CallbackQueryHandler(cb_run,               pattern=r"^run:"))
    app.add_handler(CallbackQueryHandler(cb_stop,              pattern=r"^stop:"))
    app.add_handler(CallbackQueryHandler(cb_restart,           pattern=r"^restart:"))
    app.add_handler(CallbackQueryHandler(cb_logs,              pattern=r"^logs:"))
    app.add_handler(CallbackQueryHandler(cb_filemgr,           pattern=r"^filemgr:"))
    app.add_handler(CallbackQueryHandler(cb_delete_confirm,    pattern=r"^delete:[a-zA-Z0-9_]+$"))
    app.add_handler(CallbackQueryHandler(cb_delete_yes,        pattern=r"^delete_yes:"))

    # Feature 1 & 3 standalone handlers
    app.add_handler(CallbackQueryHandler(cb_toggle_auto_restart, pattern=r"^toggle_ar:"))
    app.add_handler(CallbackQueryHandler(cb_envvars,             pattern=r"^envvars:"))
    app.add_handler(CallbackQueryHandler(cb_env_delete,          pattern=r"^env_del:"))

    return app


async def post_init(app: Application):
    global notification_bot
    notification_bot = app.bot

    await app.bot.set_my_commands([
        BotCommand("start",  "Start the bot"),
        BotCommand("done",   "Finish file upload"),
        BotCommand("cancel", "Cancel current action"),
    ])
    # Restore from backup first (before bot starts accepting updates)
    await restore_from_backup()
    # Start background tasks
    asyncio.create_task(process_monitor())
    asyncio.create_task(backup_task())
    asyncio.create_task(keep_alive_task())


def main():
    # Start Flask file manager in daemon thread
    from file_manager import start_flask
    import threading
    t = threading.Thread(target=start_flask, args=(PORT,), daemon=True)
    t.start()
    logger.info(f"Flask file manager started on port {PORT}")

    application = build_application()
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
