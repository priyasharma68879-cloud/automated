# SMS Panel Monitor Bot

## Railway Pe Deploy Kaise Karein

### Step 1: Bot Token Lo
1. Telegram pe [@BotFather](https://t.me/BotFather) jao
2. `/newbot` type karo
3. Bot ka naam aur username do
4. Token copy karo

### Step 2: Railway Pe Deploy
1. [railway.app](https://railway.app) pe jao
2. "New Project" → "Deploy from GitHub" ya "New Project" → "Deploy Repo"
3. Is folder ko GitHub pe push karo ya directly Railway pe upload karo
4. Environment Variables mein:
   - `BOT_TOKEN` = apna bot token paste karo
5. Deploy button dabao

### Step 3: Bot Use Karna
1. Telegram pe apne bot ko open karo
2. `/start` bhejo
3. Menu buttons dikhege — sab kuch yahi chat mein hoga

### Features
- **Sab kuch bot chat mein** — kisi aur jagah nahi
- **Reply Keyboard** — buttons se control karo
- **Auto OTP Detection** — OTP messages turant aayenge
- **Auto Reward Code Detection** — Reward codes turant aayenge
- **Add Panel** — naya panel add kar sakte ho
- **Remove Panel** — panel hata sakte ho
- **Multi-user** — jo bhi /start kare usse messages aayenge

### Pre-loaded Panels
- ZXKAI Panel 1 (473 devices)
- ZXKAI Panel 2 (473 devices)
- FireX Panel (112 devices)

### Environment Variables
| Variable | Required | Description |
|---|---|---|
| `BOT_TOKEN` | Yes | Telegram Bot Token from @BotFather |
