import os
from fastapi import FastAPI, Request, HTTPException
import httpx

app = FastAPI()

BOT_TOKEN = os.environ["8380690989:AAE0AZEbZdjQZVX901DzKKkQPoymXvENrYk"]
LETTA_BASE_URL = os.environ["https://lettalettalatest-production-e0a4.up.railway.app"].rstrip("/")
LETTA_AGENT_ID = os.environ["agent-63b9ba8e-7d6a-420d-a63b-aa3f5497c54a"]
LETTA_TOKEN = os.environ.get("5793")

TELEGRAM_API = f"https://api.telegram.org/bot8380690989:AAE0AZEbZdjQZVX901DzKKkQPoymXvENrYk"

def auth_headers():
    h = {"Content-Type": "application/json"}
    if LETTA_TOKEN:
        h["Authorization"] = f"Bearer 5793"
    return h

@app.post("/telegram/{token}")
async def telegram_webhook(token: str, request: Request):
    if token != BOT_TOKEN:
        raise HTTPException(status_code=403, detail="forbidden")

    update = await request.json()
    message = update.get("message") or {}
    text = message.get("text")
    chat_id = message.get("chat", {}).get("id")
    if not (text and chat_id):
        return {"ok": True}

    # Forward to Letta
    payload = {
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": text}]}
        ],
        "use_assistant_message": True
    }
    letta_url = f"{LETTA_BASE_URL}/v1/agents/{LETTA_AGENT_ID}/messages"

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(letta_url, headers=auth_headers(), json=payload)
        r.raise_for_status()
        letta_reply = r.json()["messages"][-1]["content"][0]["text"]

    # Reply back on Telegram
    async with httpx.AsyncClient(timeout=30) as client:
        await client.post(
            f"{TELEGRAM_API}/sendMessage",
            json={"chat_id": chat_id, "text": letta_reply}
        )

    return {"ok": True}
