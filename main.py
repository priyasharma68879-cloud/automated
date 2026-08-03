#!/usr/bin/env python3
"""
Swiggy TG Bot — Single Number Checker (FIXED v2)
Fixes:
  1. Race condition in referral counting — atomic read-modify-write under lock
  2. Bulk access grant — single lock acquisition, single file write
  3. Stale OTP timer — cancel old task before starting new check
  4. Admin data pollution — skip admins in bulk grant
"""

import asyncio
import json
import logging
import os
import re
import signal
import sys
import time
import random
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Tuple

import aiohttp
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
from telegram.error import TelegramError, Conflict, NetworkError

# ─── Config ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
    level=logging.INFO,
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_IDS: set[int] = {
    int(x) for x in os.environ.get("ADMIN_IDS", "1446058092,6894923643").split(",") if x.strip()
}

REQUIRED_CHANNELS = [
    {"username": "blankkdealz",     "url": "https://t.me/blankkdealz",      "label": "📢 Blank Dealz"},
    {"username": "earnwithsakx",    "url": "https://t.me/earnwithsakx",     "label": "💰 Earn With Sakx"},
]

DEFAULT_CAMPAIGN_ID = "ougwl_MjU3MTUyNzI0I1JhaHVs"
DEFAULT_THRESHOLD   = 190
OTP_TIMEOUT         = 30
REFERRALS_FOR_1H    = 3
ACCESS_HOURS        = 1

# Storage paths
_BASE      = Path(os.environ.get("DATA_DIR", Path(__file__).parent))
USERS_FILE = _BASE / "users.json"

# ─── Async Lock for User Data (prevents race conditions) ──────────────────────
_user_lock = asyncio.Lock()

# ─── Persistent Storage ───────────────────────────────────────────────────────

def load_json(path: Path, default):
    if path.exists():
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.error("Error loading %s: %s", path, e)
    return default


def save_json(path: Path, data):
    try:
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        tmp.replace(path)
    except Exception as e:
        logger.error("Error saving %s: %s", path, e)


def load_users() -> dict:
    return load_json(USERS_FILE, {})


def save_users(u: dict):
    save_json(USERS_FILE, u)


def get_user(uid: int) -> Optional[dict]:
    return load_users().get(str(uid))


async def safe_upsert_user(uid: int, **kwargs) -> dict:
    """Update a user's fields under the lock. Safe for concurrent calls."""
    async with _user_lock:
        users = load_users()
        key = str(uid)
        if key not in users:
            users[key] = {
                "username": "",
                "access_expiry": 0,
                "referrals_given": 0,
                "referred_by": None,
                "joined_at": int(time.time()),
            }
        users[key].update(kwargs)
        save_users(users)
        return users[key]


def has_access(uid: int) -> bool:
    if uid in ADMIN_IDS:
        return True
    u = get_user(uid)
    return bool(u and u.get("access_expiry", 0) > time.time())


async def safe_grant_access_hours(uid: int, hours: int = ACCESS_HOURS) -> float:
    """Extend a single user's access under the lock."""
    async with _user_lock:
        users = load_users()
        key = str(uid)
        if key not in users:
            users[key] = {
                "username": "",
                "access_expiry": 0,
                "referrals_given": 0,
                "referred_by": None,
                "joined_at": int(time.time()),
            }
        now = time.time()
        current_expiry = users[key].get("access_expiry", now)
        new_expiry = max(current_expiry, now) + hours * 3600
        users[key]["access_expiry"] = new_expiry
        save_users(users)
        return new_expiry


# ─── FIX #1: Atomic Referral Increment ────────────────────────────────────────
#
# The old code did:
#   ref_data = get_user(referrer_id)        ← read outside lock
#   new_refs = ref_data["referrals_given"] + 1
#   await safe_upsert_user(referrer_id, ...) ← write inside lock
#
# Two concurrent referrals could both read the same count, both increment
# to the same value, and one referral would be silently lost.
#
# This function does the entire read-modify-write under ONE lock acquisition,
# so no referral can be dropped.

async def atomic_increment_referral(
    referrer_id: int,
    app_bot=None,
) -> Tuple[int, bool, Optional[float]]:
    """
    Atomically increment a referrer's referral count.

    Returns:
        (new_refs, access_granted, new_expiry_or_None)
    """
    async with _user_lock:
        users = load_users()
        key = str(referrer_id)

        if key not in users:
            return (0, False, None)

        old_refs = users[key].get("referrals_given", 0)
        new_refs = old_refs + 1
        users[key]["referrals_given"] = new_refs

        access_granted = False
        new_expiry = None

        if new_refs % REFERRALS_FOR_1H == 0:
            now = time.time()
            current_expiry = users[key].get("access_expiry", now)
            new_expiry = max(current_expiry, now) + ACCESS_HOURS * 3600
            users[key]["access_expiry"] = new_expiry
            access_granted = True

        save_users(users)

    # Notify the referrer OUTSIDE the lock to avoid holding it during I/O
    if app_bot is not None:
        try:
            if access_granted:
                exp_str = datetime.fromtimestamp(new_expiry).strftime("%H:%M %d/%m")
                await app_bot.send_message(
                    referrer_id,
                    "🎉 *Congratulations!*\n"
                    f"You now have *{new_refs} referrals*!\n"
                    "✅ *1 hour access granted!*\n"
                    f"⏰ Expires: {exp_str}",
                    parse_mode="Markdown",
                )
            else:
                remaining = REFERRALS_FOR_1H - (new_refs % REFERRALS_FOR_1H)
                await app_bot.send_message(
                    referrer_id,
                    "👥 *New referral!*\n"
                    f"Total: {new_refs} | {remaining} more needed for 1h access",
                    parse_mode="Markdown",
                )
        except Exception:
            pass

    return (new_refs, access_granted, new_expiry)


# ─── Channel Gate ─────────────────────────────────────────────────────────────

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
    buttons.append([InlineKeyboardButton("✅ I've Joined — Check Now", callback_data="check_join")])
    return InlineKeyboardMarkup(buttons)


# ─── Utility ──────────────────────────────────────────────────────────────────

def generate_csrf() -> str:
    chars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return "".join(random.choices(chars, k=40))


def generate_uuid() -> str:
    return str(uuid.uuid4())


def random_user_agent() -> str:
    return (
        "Mozilla/5.0 (Linux; Android 14; SM-A065F Build/UP1A.231005.007; wv) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
        "Chrome/150.0.7871.181 Mobile Safari/537.36"
    )


# ─── Swiggy API Client ────────────────────────────────────────────────────────

class SwiggyClient:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.csrf = generate_csrf()
        self.device_id = generate_uuid()
        self.tid: Optional[str] = None
        self.token: Optional[str] = None
        self.user_id: Optional[str] = None

    def _base_headers(self, extra: Optional[dict] = None) -> dict:
        headers = {
            "accept": "*/*",
            "content-type": "application/json",
            "origin": "https://www.swiggy.com",
            "referer": "https://www.swiggy.com/auth",
            "sec-ch-ua": '"Android WebView";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
            "sec-ch-ua-mobile": "?1",
            "sec-ch-ua-platform": '"Android"',
            "user-agent": random_user_agent(),
        }
        if extra:
            headers.update(extra)
        return headers

    async def send_otp(self, phone: str) -> bool:
        try:
            async with self.session.post(
                "https://www.swiggy.com/mapi/auth/signin-check",
                json={"mobile": phone, "countryCode": "91", "countryKey": "IN", "_csrf": self.csrf},
                headers=self._base_headers(),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 429:
                    retry_after = int(resp.headers.get("Retry-After", 15))
                    logger.warning("Swiggy rate-limited (signin-check) — sleeping %ds", retry_after)
                    await asyncio.sleep(retry_after)
                    return False
                if resp.status != 200:
                    return False
                data = await resp.json()
                if not data.get("data", {}).get("registered", False):
                    return False
        except Exception as e:
            logger.debug("send_otp signin-check error: %s", e)
            return False

        try:
            async with self.session.post(
                "https://www.swiggy.com/mapi/auth/sms-otp",
                json={"mobile": phone, "_csrf": self.csrf},
                headers=self._base_headers(),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 429:
                    retry_after = int(resp.headers.get("Retry-After", 15))
                    logger.warning("Swiggy rate-limited (sms-otp) — sleeping %ds", retry_after)
                    await asyncio.sleep(retry_after)
                    return False
                return resp.status == 200
        except Exception as e:
            logger.debug("send_otp sms-otp error: %s", e)
            return False

    async def verify_otp(self, phone: str, otp: str) -> bool:
        try:
            async with self.session.post(
                "https://www.swiggy.com/mapi/auth/otp-verify",
                json={"otp": otp, "_csrf": self.csrf},
                headers=self._base_headers(),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return False
                data = await resp.json()
                if data.get("statusCode") != 0:
                    return False
                self.token = data.get("data", {}).get("token")
                self.tid = data.get("tid")
                self.user_id = str(data.get("data", {}).get("customer_id", ""))
                return bool(self.token and self.tid)
        except Exception as e:
            logger.debug("verify_otp error: %s", e)
            return False

    async def get_free_cash(self, campaign_id: str) -> Optional[int]:
        if not self.token or not self.tid:
            return None
        headers = {
            "accept": "*/*",
            "accept-encoding": "gzip, deflate, br, zstd",
            "accept-language": "en-IN,en-US;q=0.9,en;q=0.8",
            "client-id": "portal",
            "content-type": "application/json",
            "origin": "https://webviews.swiggy.com",
            "priority": "u=1, i",
            "referer": "https://webviews.swiggy.com/",
            "sec-ch-ua": '"Not;A=Brand";v="8", "Chromium";v="150", "Android WebView";v="150"',
            "sec-ch-ua-mobile": "?1",
            "sec-ch-ua-platform": '"Android"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            "tid": self.tid,
            "token": self.token,
            "user-agent": random_user_agent(),
            "x-requested-with": "in.swiggy.android",
        }
        payload = {
            "generalContext": {"requestContext": {"clientId": "portal_invite"}},
            "campaignRewardRequests": [
                {
                    "campaignType": "CAMPAIGN_TYPE_BUZZ_MONEY_STREAKS",
                    "campaignId": campaign_id,
                    "rollingFreecashParams": {
                        "forceRefresh": True,
                        "requestParams": {
                            "dataRequested": "wallet,connections,transactions",
                            "consumerName": "User",
                            "source": "invite",
                        },
                    },
                }
            ],
        }
        try:
            async with self.session.post(
                "https://spns.swiggy.com/api/v1/campaign/rewards",
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 429:
                    retry_after = int(resp.headers.get("Retry-After", 15))
                    logger.warning("Swiggy rate-limited (rewards) — sleeping %ds", retry_after)
                    await asyncio.sleep(retry_after)
                    return None
                if resp.status != 200:
                    return None
                data = await resp.json()
                if data.get("statusCode") != 0:
                    return None
                reward_responses = data.get("data", {}).get("campaignRewardResponses", [])
                if not reward_responses:
                    return None
                for reward in reward_responses[0].get("rewards", []):
                    if reward.get("rewardType") == "REWARD_TYPE_ROLLING_FREECASH":
                        total_str = (
                            reward.get("rollingFreecash", {})
                            .get("totalEarned", {})
                            .get("units", "0")
                        )
                        try:
                            return int(total_str)
                        except (ValueError, TypeError):
                            return 0
                return 0
        except Exception as e:
            logger.debug("get_free_cash error: %s", e)
            return None


# ─── Single Number Check ──────────────────────────────────────────────────────

async def _close_check_session(context: ContextTypes.DEFAULT_TYPE):
    """Safely clean up a stored aiohttp session and cancel any pending expiry task."""
    stored_session = context.user_data.pop("check_session", None)
    context.user_data.pop("check_client", None)
    context.user_data.pop("check_phone", None)
    context.user_data.pop("awaiting_check_otp", None)
    context.user_data.pop("awaiting_check_phone", None)
    # Cancel existing expiry task
    old_task = context.user_data.pop("check_expire_task", None)
    if old_task and not old_task.done():
        old_task.cancel()
    if stored_session and not stored_session.closed:
        try:
            await stored_session.close()
        except Exception:
            pass


async def check_single_number_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ADMIN_IDS and not has_access(uid):
        await update.message.reply_text("⛔ Access expired. Share your referral link to unlock!")
        return

    # Clear any existing timer/session before starting new
    await _close_check_session(context)

    await update.message.reply_text(
        "🔢 *Check Single Number*\n\n"
        "Send the 10-digit Indian mobile number you want to check.\n"
        "Example: `9876543210`",
        parse_mode="Markdown",
    )
    context.user_data["awaiting_check_phone"] = True


async def check_single_number_otp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    phone_raw = update.message.text.strip()
    phone = re.sub(r"\D", "", phone_raw)

    if len(phone) != 10 or phone[0] not in "6789":
        await update.message.reply_text(
            "❌ Invalid number. Please send a valid 10-digit Indian mobile number (starts with 6–9)."
        )
        return

    await update.message.reply_text(f"📤 Sending OTP to `{phone}`...", parse_mode="Markdown")

    connector = aiohttp.TCPConnector(limit=0)
    session = aiohttp.ClientSession(connector=connector)
    client = SwiggyClient(session)

    try:
        sent = await client.send_otp(phone)
    except Exception as e:
        await session.close()
        await update.message.reply_text("⚠️ Error sending OTP. Please try again.")
        context.user_data.pop("awaiting_check_phone", None)
        logger.debug("check_single_number_otp error: %s", e)
        return

    if not sent:
        await session.close()
        await update.message.reply_text(
            "❌ Could not send OTP to `{phone}`.\n"
            "Number may not be registered on Swiggy, or Swiggy is rate-limiting. Try another number.",
            parse_mode="Markdown",
        )
        context.user_data.pop("awaiting_check_phone", None)
        return

    context.user_data["check_phone"] = phone
    context.user_data["check_session"] = session
    context.user_data["check_client"] = client
    context.user_data.pop("awaiting_check_phone", None)
    context.user_data["awaiting_check_otp"] = True

    await update.message.reply_text(
        f"✅ OTP sent to `{phone}`!\n\n"
        "📩 Check your phone and send the OTP here.\n"
        "⏰ You have 2 minutes before it expires.",
        parse_mode="Markdown",
    )

    async def expire_otp():
        try:
            await asyncio.sleep(120)
        except asyncio.CancelledError:
            return  # Task was cancelled — new session started
        # Verify this task is still the active one
        if context.user_data.get("check_expire_task") is not asyncio.current_task():
            return
        if context.user_data.get("awaiting_check_otp"):
            await _close_check_session(context)
            try:
                await context.bot.send_message(
                    uid, "⏰ OTP expired. Use *🔢 Check Number* to try again.", parse_mode="Markdown"
                )
            except Exception:
                pass

    task = asyncio.create_task(expire_otp())
    context.user_data["check_expire_task"] = task


async def check_single_number_verify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    otp_raw = update.message.text.strip()
    otp = re.sub(r"\D", "", otp_raw)

    if not (4 <= len(otp) <= 6):
        await update.message.reply_text("❌ Invalid OTP. Please send the 4–6 digit OTP you received.")
        return

    phone = context.user_data.get("check_phone", "")
    client: Optional[SwiggyClient] = context.user_data.get("check_client")
    session: Optional[aiohttp.ClientSession] = context.user_data.get("check_session")

    if not phone or not client or not session:
        await update.message.reply_text(
            "⚠️ Session expired. Use *🔢 Check Number* to start again.", parse_mode="Markdown"
        )
        context.user_data.pop("awaiting_check_otp", None)
        return

    await update.message.reply_text("🔐 Verifying OTP...")

    try:
        verified = await client.verify_otp(phone, otp)
    except Exception as e:
        await _close_check_session(context)
        await update.message.reply_text("⚠️ Error verifying OTP. Please try again.")
        logger.debug("verify_otp error: %s", e)
        return

    if not verified:
        await update.message.reply_text(
            "❌ OTP verification failed.\nWrong OTP or it expired. Use *🔢 Check Number* to try again.",
            parse_mode="Markdown",
        )
        await _close_check_session(context)
        return

    await update.message.reply_text("💳 OTP verified! Checking free cash balance...")

    campaign_id = context.user_data.get("check_campaign", DEFAULT_CAMPAIGN_ID)
    free_cash = await client.get_free_cash(campaign_id)

    await _close_check_session(context)

    if free_cash is None:
        await update.message.reply_text(
            f"📱 Number: `{phone}`\n⚠️ Could not fetch free cash. Account may have restrictions.",
            parse_mode="Markdown",
        )
    else:
        status = "🟢 HIGH" if free_cash >= DEFAULT_THRESHOLD else "🔴 Low"
        await update.message.reply_text(
            f"📱 Number: `{phone}`\n"
            f"💰 Free Cash: *₹{free_cash}* {status}\n"
            f"👤 User ID: `{client.user_id or 'N/A'}`",
            parse_mode="Markdown",
        )


# ─── Keyboards ────────────────────────────────────────────────────────────────

def get_main_keyboard(is_admin: bool) -> ReplyKeyboardMarkup:
    rows = [
        ["🔢 Check Number"],
        ["📊 My Status", "🔗 My Referral"],
        ["⏰ My Access"],
    ]
    if is_admin:
        rows.append(["👑 Give All Access", "👤 Give User Access"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


# ─── Menus & Commands ─────────────────────────────────────────────────────────

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    is_admin = uid in ADMIN_IDS

    # Clear any pending state
    await _close_check_session(context)
    for key in ["awaiting_give_all_access", "awaiting_give_user_access"]:
        context.user_data.pop(key, None)

    bot_info = await context.bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=r{uid}"

    u = get_user(uid)
    refs = u.get("referrals_given", 0) if u else 0
    expiry = u.get("access_expiry", 0) if u else 0

    if is_admin:
        access_str = "♾️ Unlimited (Admin)"
    elif expiry > time.time():
        exp_str = datetime.fromtimestamp(expiry).strftime("%H:%M %d/%m/%Y")
        access_str = f"✅ Active till {exp_str}"
    else:
        needed = REFERRALS_FOR_1H - (refs % REFERRALS_FOR_1H)
        access_str = f"❌ Expired | Refer {needed} more to unlock"

    role = "👑 Admin" if is_admin else "👤 Member"
    total_users = len(load_users())

    admin_section = ""
    if is_admin:
        admin_section = (
            "\n━━━━━━━━━━━━━━━━━━\n"
            "🛠 *Admin Panel*\n"
            f"👤 Total Users: {total_users}"
        )

    await update.effective_message.reply_text(
        "🍊 *Swiggy Checker Bot*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🎭 Role: {role}\n"
        f"🔑 Access: {access_str}\n"
        f"👥 Referrals: {refs}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🔗 Referral: `{ref_link}`"
        f"{admin_section}",
        reply_markup=get_main_keyboard(is_admin),
        parse_mode="Markdown",
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    username = update.effective_user.username or ""
    args = context.args or []

    await safe_upsert_user(uid, username=username)

    # Handle referral parameter
    if args and args[0].startswith("r"):
        try:
            referrer_id = int(args[0][1:])
            if referrer_id != uid:
                # Check if this user was already referred (idempotency guard)
                u = get_user(uid)
                if u and u.get("referred_by") is None:
                    await safe_upsert_user(uid, referred_by=referrer_id)
                    # FIX #1: Atomic increment — no race condition possible
                    await atomic_increment_referral(
                        referrer_id,
                        app_bot=context.bot,
                    )
        except (ValueError, IndexError):
            pass

    # Gate: channel membership check
    if uid not in ADMIN_IDS:
        not_joined = await check_channels(context.bot, uid)
        if not_joined:
            kb = channel_join_keyboard(not_joined)
            await update.message.reply_text(
                "👋 *Welcome to Swiggy Checker Bot!*\n\n"
                "📢 *Step 1:* Join these channels first, then you can use the bot:",
                reply_markup=kb,
                parse_mode="Markdown",
            )
            return

    # Gate: access check
    if uid not in ADMIN_IDS and not has_access(uid):
        bot_info = await context.bot.get_me()
        ref_link = f"https://t.me/{bot_info.username}?start=r{uid}"
        u = get_user(uid)
        refs = u.get("referrals_given", 0) if u else 0
        needed = REFERRALS_FOR_1H - (refs % REFERRALS_FOR_1H)

        await update.message.reply_text(
            "✅ *Channels joined!*\n\n"
            "🔗 *Step 2:* Share your referral link to unlock the bot.\n\n"
            f"Your Referral Link:\n`{ref_link}`\n\n"
            f"👥 Current referrals: {refs}\n"
            f"📌 {needed} more needed = 1 hour access\n\n"
            "💡 Every 3 referrals = 1 hour access!",
            parse_mode="Markdown",
        )
        return

    await show_main_menu(update, context)


async def check_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id

    not_joined = await check_channels(context.bot, uid)
    if not_joined:
        kb = channel_join_keyboard(not_joined)
        await query.edit_message_text(
            "❌ You haven't joined all channels yet:",
            reply_markup=kb,
        )
        return

    if uid not in ADMIN_IDS and not has_access(uid):
        bot_info = await context.bot.get_me()
        ref_link = f"https://t.me/{bot_info.username}?start=r{uid}"
        u = get_user(uid)
        refs = u.get("referrals_given", 0) if u else 0
        needed = REFERRALS_FOR_1H - (refs % REFERRALS_FOR_1H)
        await query.edit_message_text(
            "✅ *Channels joined!*\n\n"
            "🔗 *Step 2:* Share your referral link to unlock the bot.\n\n"
            f"Your Referral Link:\n`{ref_link}`\n\n"
            f"👥 Referrals: {refs}\n"
            f"📌 {needed} more needed = 1 hour access",
            parse_mode="Markdown",
        )
        return

    await query.edit_message_text("✅ All channels joined! Welcome!")
    await show_main_menu(update, context)


# ─── Admin Access Grant ───────────────────────────────────────────────────────

async def handle_give_all_access(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ADMIN_IDS:
        return
    await update.message.reply_text(
        "👑 *Give Access to ALL Users*\n\n"
        "How many hours should each user get?\n\n"
        "Send a number (e.g. `24` for 24 hours, `1` for 1 hour).",
        parse_mode="Markdown",
    )
    context.user_data["awaiting_give_all_access"] = True


async def handle_give_user_access(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ADMIN_IDS:
        return
    await update.message.reply_text(
        "👤 *Give Access to ONE User*\n\n"
        "Send: `user_id hours`\n\n"
        "Example: `123456789 24`\n\n"
        "_(You can find a user's ID from their referral link)_",
        parse_mode="Markdown",
    )
    context.user_data["awaiting_give_user_access"] = True


# ─── FIX #2: Bulk Access Grant — single lock, single write ────────────────────

async def _do_give_all_access(hours: int, app, admin_uid: int):
    """
    Grant access to every non-admin user in a single lock acquisition.
    Old code: N lock acquisitions, N file reads, N file writes.
    New code: 1 lock acquisition, 1 file read, 1 file write.
    """
    async with _user_lock:
        users = load_users()
        count = 0
        now = time.time()
        for uid_str in list(users.keys()):
            # Skip admins — their access is unlimited via ADMIN_IDS
            if int(uid_str) in ADMIN_IDS:
                continue
            current_expiry = users[uid_str].get("access_expiry", now)
            users[uid_str]["access_expiry"] = max(current_expiry, now) + hours * 3600
            count += 1
        save_users(users)

    expiry_str = datetime.fromtimestamp(now + hours * 3600).strftime("%H:%M %d/%m/%Y")
    try:
        await app.bot.send_message(
            admin_uid,
            "✅ *Done!* Access granted to *{count} users*.\n"
            f"⏰ Each user's access extended by *{hours}h*\n"
            f"📅 New expiry (from now): {expiry_str}",
            parse_mode="Markdown",
        )
    except Exception:
        pass


# ─── User Info Commands ───────────────────────────────────────────────────────

async def my_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = get_user(uid)
    refs = u.get("referrals_given", 0) if u else 0
    expiry = u.get("access_expiry", 0) if u else 0

    if uid in ADMIN_IDS:
        access_str = "♾️ Unlimited (Admin)"
    elif expiry > time.time():
        exp_str = datetime.fromtimestamp(expiry).strftime("%H:%M %d/%m/%Y")
        remaining = int(expiry - time.time())
        hours, rem = divmod(remaining, 3600)
        mins = rem // 60
        access_str = f"✅ Active till {exp_str} ({hours}h {mins}m left)"
    else:
        needed = REFERRALS_FOR_1H - (refs % REFERRALS_FOR_1H)
        access_str = f"❌ Expired — {needed} more referral(s) needed"

    await update.message.reply_text(
        "📊 *Your Status*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🔑 Access: {access_str}\n"
        f"👥 Referrals: {refs}",
        parse_mode="Markdown",
    )


async def my_referral_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = get_user(uid)
    refs = u.get("referrals_given", 0) if u else 0
    bot_info = await context.bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=r{uid}"
    next_milestone = REFERRALS_FOR_1H - (refs % REFERRALS_FOR_1H)

    await update.message.reply_text(
        "🔗 *Your Referral*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👥 Total Referrals: {refs}\n"
        f"⏱ Next 1h access in: {next_milestone} more referral(s)\n\n"
        f"Your Link:\n`{ref_link}`\n\n"
        "💡 *Every 3 referrals = 1 hour access!*\n"
        "Share and unlock the bot!",
        parse_mode="Markdown",
    )


async def my_access_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid in ADMIN_IDS:
        await update.message.reply_text("♾️ *Admin — Unlimited access!*", parse_mode="Markdown")
        return

    u = get_user(uid)
    expiry = u.get("access_expiry", 0) if u else 0
    refs = u.get("referrals_given", 0) if u else 0

    if expiry > time.time():
        remaining = int(expiry - time.time())
        hours, rem = divmod(remaining, 3600)
        mins = rem // 60
        exp_str = datetime.fromtimestamp(expiry).strftime("%H:%M %d/%m/%Y")
        await update.message.reply_text(
            "✅ *Access Active*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"⏰ Expires: {exp_str}\n"
            f"⌛ Remaining: {hours}h {mins}m\n"
            f"👥 Referrals: {refs}",
            parse_mode="Markdown",
        )
    else:
        needed = REFERRALS_FOR_1H - (refs % REFERRALS_FOR_1H)
        await update.message.reply_text(
            "❌ *No Active Access*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"👥 Referrals: {refs}\n"
            f"📌 {needed} more = 1 hour access\n\n"
            "Use /start to get your referral link.",
            parse_mode="Markdown",
        )


# ─── Text Message Router ──────────────────────────────────────────────────────

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    uid = update.effective_user.id

    # --- Admin: give all users access ---
    if context.user_data.get("awaiting_give_all_access"):
        context.user_data.pop("awaiting_give_all_access")
        if uid not in ADMIN_IDS:
            return
        try:
            hours = int(text.strip())
            if hours <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "❌ Invalid. Send a positive number like `24`.", parse_mode="Markdown"
            )
            return
        users = load_users()
        await update.message.reply_text(
            f"⏳ Granting *{hours}h* access to *{len(users)} users*...",
            parse_mode="Markdown",
        )
        asyncio.create_task(_do_give_all_access(hours, context.application, uid))
        return

    # --- Admin: give one user access ---
    if context.user_data.get("awaiting_give_user_access"):
        context.user_data.pop("awaiting_give_user_access")
        if uid not in ADMIN_IDS:
            return
        parts = text.strip().split()
        try:
            target_uid = int(parts[0])
            hours = int(parts[1]) if len(parts) >= 2 else ACCESS_HOURS
            if hours <= 0:
                raise ValueError
        except (ValueError, IndexError):
            await update.message.reply_text(
                "❌ Invalid format. Send: `user_id hours`\nExample: `123456789 24`",
                parse_mode="Markdown",
            )
            return
        expiry = await safe_grant_access_hours(target_uid, hours)
        exp_str = datetime.fromtimestamp(expiry).strftime("%H:%M %d/%m/%Y")
        await update.message.reply_text(
            "✅ *Access granted!*\n"
            f"👤 User: `{target_uid}`\n"
            f"⏰ Duration: *{hours} hour(s)*\n"
            f"📅 Expires: {exp_str}",
            parse_mode="Markdown",
        )
        try:
            await context.bot.send_message(
                target_uid,
                "🎉 *You have been granted access!*\n"
                f"⏰ Duration: *{hours} hour(s)*\n"
                f"📅 Expires: {exp_str}\n\n"
                "Use /start to open the menu.",
                parse_mode="Markdown",
            )
        except Exception:
            pass
        return

    # --- State machine: awaiting OTP or phone ---
    if context.user_data.get("awaiting_check_otp"):
        await check_single_number_verify(update, context)
        return

    if context.user_data.get("awaiting_check_phone"):
        await check_single_number_otp(update, context)
        return

    # --- Menu button routing ---
    menu_map = {
        "🔢 Check Number":     check_single_number_start,
        "📊 My Status":        my_status_command,
        "🔗 My Referral":      my_referral_command,
        "⏰ My Access":        my_access_command,
        "👑 Give All Access":  handle_give_all_access,
        "👤 Give User Access": handle_give_user_access,
        "🏠 Back to Menu":     show_main_menu,
    }
    handler = menu_map.get(text)
    if handler:
        await handler(update, context)
    else:
        await show_main_menu(update, context)


# ─── Error Handler ────────────────────────────────────────────────────────────

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    if isinstance(err, Conflict):
        logger.warning(
            "Conflict: another bot instance is already polling. "
            "Ensure only one Railway deployment is active at a time."
        )
        return
    if isinstance(err, NetworkError):
        logger.warning("Network error (will retry): %s", err)
        return
    logger.error("Unhandled exception:", exc_info=err)


# ─── Entry Point ──────────────────────────────────────────────────────────────

def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN environment variable is not set. Exiting.")
        sys.exit(1)

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .read_timeout(30)
        .write_timeout(30)
        .connect_timeout(30)
        .pool_timeout(30)
        .build()
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("menu", show_main_menu))
    application.add_handler(CallbackQueryHandler(check_join_callback, pattern="^check_join$"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    application.add_error_handler(error_handler)

    logger.info("🍊 Swiggy Checker Bot starting...")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        stop_signals=(signal.SIGINT, signal.SIGTERM),
    )


if __name__ == "__main__":
    main()
