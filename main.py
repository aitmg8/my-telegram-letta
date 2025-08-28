import os, json, asyncio, logging
from fastapi import FastAPI, Request, HTTPException
import httpx

log = logging.getLogger("uvicorn.error")
app = FastAPI()

# --- env vars ---
BOT_TOKEN       = os.getenv("BOT_TOKEN")
LETTA_BASE_URL  = (os.getenv("LETTA_BASE_URL") or "").rstrip("/")
LETTA_AGENT_ID  = os.getenv("LETTA_AGENT_ID")
LETTA_TOKEN     = os.getenv("LETTA_TOKEN")  # optional

if not all([BOT_TOKEN, LETTA_BASE_URL, LETTA_AGENT_ID]):
    missing = [k for k,v in {
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

# ---------- utilities ----------
async def send_typing(chat_id: int):
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            await c.post(f"{TELEGRAM_API}/sendChatAction",
                         json={"chat_id": chat_id, "action": "typing"})
    except Exception as e:
        log.debug("send_typing failed: %s", e)

def extract_reply(data: dict) -> str:
    # pull first textual assistant content we can find
    for m in data.get("messages", []):
        c = m.get("content")
        if isinstance(c, list):
            for p in c:
                if isinstance(p, dict):
                    if p.get("type") == "text" and p.get("text"):
                        return p["text"]
                    if p.get("type") == "assistant_message" and p.get("message"):
                        return p["message"]
        elif isinstance(c, str) and c.strip():
            return c.strip()
    return "(no reply)"

async def query_letta_and_reply(chat_id: int, user_text: str):
    # periodic "typing…" while we work
    typing_task = asyncio.create_task(_typing_loop(chat_id))

    reply = "(Letta request failed)"
    try:
        payload = {
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": user_text}]}
            ],
            "use_assistant_message": True
        }
        timeout = httpx.Timeout(connect=20.0, read=120.0, write=30.0, pool=120.0)
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(f"{LETTA_BASE_URL}/v1/agents/{LETTA_AGENT_ID}/messages",
                             headers=auth_headers(), json=payload)
            r.raise_for_status()
            reply = extract_reply(r.json())
    except httpx.HTTPStatusError as e:
        log.error("Letta %s: %s", e.response.status_code, e.response.text[:400])
        reply = f"(Letta error {e.response.status_code})"
    except Exception as e:
        log.exception("Letta request failed: %s", e)
        reply = "(Letta request failed)"
    finally:
        typing_task.cancel()
        with contextlib.suppress(Exception):
            await typing_task

    # send answer back to user
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            await c.post(f"{TELEGRAM_API}/sendMessage",
                         json={"chat_id": chat_id, "text": reply,
                               "disable_web_page_preview": True})
    except Exception as e:
        log.exception("Telegram sendMessage failed: %s", e)

async def _typing_loop(chat_id: int):
    # send typing every ~4s until cancelled
    try:
        while True:
            await send_typing(chat_id)
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        pass

# ---------- endpoints ----------
@app.get("/healthz")
async def healthz():
    return {
        "ok": bool(BOT_TOKEN and LETTA_BASE_URL and LETTA_AGENT_ID),
        "has_bot_token": bool(BOT_TOKEN),
        "has_letta_base": bool(LETTA_BASE_URL),
        "has_letta_agent": bool(LETTA_AGENT_ID),
        "uses_auth": bool(LETTA_TOKEN),
    }

@app.get("/debug/letta")
async def debug_letta():
    # quick reachability test to Letta
    try:
        headers = {}
        if LETTA_TOKEN:
            headers["Authorization"] = f"Bearer {LETTA_TOKEN}"
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{LETTA_BASE_URL}/v1/health", headers=headers)
        return {"ok": r.is_success, "status": r.status_code, "text": r.text[:200]}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.post("/telegram/{token}")
async def telegram_webhook(token: str, request: Request):
    if not (BOT_TOKEN and LETTA_BASE_URL and LETTA_AGENT_ID):
        raise HTTPException(status_code=500, detail="Missing required environment variables")
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
        # ignore non-text updates
        log.info("Ignoring update: %s", json.dumps(update)[:500])
        return {"ok": True}

    # fire-and-forget; immediately ACK Telegram
    asyncio.create_task(query_letta_and_reply(chat_id, text))
    return {"ok": True}

# needed for contextlib in finally
import contextlib