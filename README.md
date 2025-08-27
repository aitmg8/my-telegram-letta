# Telegram ↔ Letta Bot Bridge

A FastAPI webhook that connects your Telegram bot to your Letta agent.

[![Deploy on Railway](https://railway.app/button.svg)](https://railway.app/template?referralCode=telegram-letta&envs=BOT_TOKEN,LETTA_BASE_URL,LETTA_AGENT_ID,LETTA_TOKEN&optionalEnvs=LETTA_TOKEN)

---

## ⚙️ Required Environment Variables

- `BOT_TOKEN` → from [BotFather](https://t.me/botfather)
- `LETTA_BASE_URL` → your Letta server (e.g. `https://lettalettalatest-production-xxxx.up.railway.app`)
- `LETTA_AGENT_ID` → the ID of your Letta agent
- `LETTA_TOKEN` → *(optional)* only if your Letta server requires auth

---

## 🚀 Deploying

1. Click **Deploy on Railway** button above.  
2. Enter your environment variables.  
3. Railway will build and deploy the FastAPI app.  
4. Get your public Railway URL (e.g. `https://my-telegram-letta.up.railway.app`).  

---

## 🔗 Set Telegram Webhook

Replace `BOT_TOKEN` and `YOUR_RAILWAY_URL`:

```bash
https://api.telegram.org/bot<BOT_TOKEN>/setWebhook?url=https://YOUR_RAILWAY_URL/telegram/<BOT_TOKEN>
