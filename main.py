# main.py
import os
import json
import logging
from fastapi import FastAPI, Request, HTTPException
import httpx

log = logging.getLogger("uvicorn.error")
app = FastAPI()

# ---- Required envs (use only BOT_TOKEN) ----
BOT_TOKEN = os.getenv("BOT_TOKEN")                    # Telegram BotFather token
LETTA_BASE_URL = (os.getenv("LETTA_BASE_URL") or "").rstrip("/")
LETTA_AGENT_ID = os.getenv("LETTA_AGENT_ID")
LETTA_TOKEN = os.getenv("LETTA_TOKEN")                # optional if Letta is unsecured

if not all([BOT_TOKEN, LETTA_BASE_URL, LETTA_AGENT_ID]):
    missing = [k for k, v in {
        "BOT_TOKEN": BOT_TOKEN,
        "LETTA_BASE_URL": LETTA_BASE_URL,
        "LETTA_AGENT_ID": LETTA_AGENT_ID,
    }.items() if not v]
    log.error("Missing required env vars: %s", ", ".join(missing))

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else None

def auth_headers():
    h = {"Content-Type": "application/json"}
    if LETTA_TOKEN:
        h["Authorization"] = f"Bearer {LETTA_TOKEN}"
    return h

@app.get("/healthz")
async def healthz():
    return {
        "ok": bool(BOT_TOKEN and LETTA_BASE_URL and LETTA_AGENT_ID),
        "has_bot_token": bool(BOT_TOKEN),
        "has_letta_base": bool(LETTA_BASE_URL),
        "has_letta_agent": bool(LETTA_AGENT_ID),
        "uses_auth": bool(LETTA_TOKEN),
    }

@app.post("/telegram/{token}")
async def telegram_webhook(token: str, request: Request):
    # Basic sanity checks
    if not (BOT_TOKEN and LETTA_BASE_URL and LETTA_AGENT_ID):
        raise HTTPException(status_code=500, detail="Missing required environment variables")

    # Shared-secret path check
    if token != BOT_TOKEN:
        raise HTTPException(status_code=403, detail="forbidden")

    try:
        update = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="bad json")

    message = update.get("message") or {}
    text = (message.get("text") or "").strip()
    chat_id = (message.get("chat") or {}).get("id")
    if not (text and chat_id):
        # Ignore non-text updates
        log.info("Ignoring update: %s", json.dumps(update)[:500])
        return {"ok": True}

    # ---- 1) Forward to Letta ----
    payload = {
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": text}]}
        ],
        "use_assistant_message": True
    }
    letta_url = f"{LETTA_BASE_URL}/v1/agents/{LETTA_AGENT_ID}/messages"

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(letta_url, headers=auth_headers(), json=payload)
            r.raise_for_status()
            data = r.json()
            # Try common shapes for reply text
            reply = "(no reply)"
            for m in data.get("messages", []):
                c = m.get("content")
                if isinstance(c, list):
                    for p in c:
                        if isinstance(p, dict) and p.get("type") == "text" and p.get("text"):
                            reply = p["text"]
                            break
                elif isinstance(c, str) and c.strip():
                    reply = c.strip()
                if reply != "(no reply)":
                    break
    except httpx.HTTPStatusError as e:
        log.error("Letta %s: %s", e.response.status_code, e.response.text[:500])
        reply = f"(Letta error {e.response.status_code})"
    except Exception as e:
        log.exception("Letta request failed: %s", e)
        reply = "(Letta request failed)"

    # ---- 2) Send back to Telegram ----
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            await client.post(
                f"{TELEGRAM_API}/sendMessage",
                json={"chat_id": chat_id, "text": reply, "disable_web_page_preview": True}
            )
    except Exception as e:
        log.exception("Telegram sendMessage failed: %s", e)
        raise HTTPException(status_code=502, detail="telegram send failed")

    return {"ok": True}