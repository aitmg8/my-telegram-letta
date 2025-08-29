# main.py — Telegram ↔ Letta bridge (no "Letta request failed" on direct replies)
import os, json, asyncio, logging, contextlib, time
from collections import OrderedDict
from fastapi import FastAPI, Request, HTTPException
import httpx

log = logging.getLogger("uvicorn.error")
app = FastAPI()  # <- uvicorn main:app needs this

# ====== ENV ======
BOT_TOKEN       = os.getenv("BOT_TOKEN")
LETTA_BASE_URL  = (os.getenv("LETTA_BASE_URL") or "").rstrip("/")
LETTA_AGENT_ID  = os.getenv("LETTA_AGENT_ID")
LETTA_TOKEN     = os.getenv("LETTA_TOKEN")  # optional

# Timeouts / polling
CONNECT_TIMEOUT = float(os.getenv("CONNECT_TIMEOUT", "20"))
READ_TIMEOUT    = float(os.getenv("READ_TIMEOUT",   "30"))  # short read for async create
LONG_READ       = float(os.getenv("LONG_READ",     "600"))  # fallback sync read
POLL_INTERVAL   = float(os.getenv("LETTA_POLL_INTERVAL", "2.5"))
POLL_TIMEOUT    = float(os.getenv("LETTA_POLL_TIMEOUT",  "600"))

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else None


def ok_env() -> bool:
    return bool(BOT_TOKEN and LETTA_BASE_URL and LETTA_AGENT_ID)


def auth_headers() -> dict:
    h = {"Content-Type": "application/json"}
    if LETTA_TOKEN:
        h["Authorization"] = f"Bearer {LETTA_TOKEN}"
    return h


# ====== Simple LRU+TTL to dedupe Telegram updates ======
class LruTtl:
    def __init__(self, maxsize=5000, ttl=1800):
        self.maxsize = maxsize
        self.ttl = ttl
        self.store = OrderedDict()

    def seen(self, key) -> bool:
        now = time.time()
        # purge expired
        for k, (t, _) in list(self.store.items()):
            if now - t > self.ttl:
                self.store.pop(k, None)
        if key in self.store:
            self.store.move_to_end(key)
            return True
        self.store[key] = (now, True)
        if len(self.store) > self.maxsize:
            self.store.popitem(last=False)
        return False

DEDUP_CACHE = LruTtl()


# ====== Telegram helpers ======
async def send_typing(chat_id: int):
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            await c.post(f"{TELEGRAM_API}/sendChatAction",
                         json={"chat_id": chat_id, "action": "typing"})
    except Exception as e:
        log.debug("typing failed: %s", e)


async def telegram_send(chat_id: int, text: str):
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            await c.post(f"{TELEGRAM_API}/sendMessage",
                         json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True})
    except Exception as e:
        log.exception("sendMessage failed: %s", e)


# ====== Extract first text chunk from Letta messages ======
def extract_reply_from_messages(payload: dict) -> str:
    for m in payload.get("messages", []):
        c = m.get("content")
        if isinstance(c, list):
            for p in c:
                if isinstance(p, dict) and p.get("type") == "text" and p.get("text"):
                    return p["text"]
        elif isinstance(c, str) and c.strip():
            return c.strip()
    return ""


# Idempotency header so async+fallback doesn’t create duplicates
def idempotency_headers(chat_id: int, message_id: int) -> dict:
    h = auth_headers()
    h["Idempotency-Key"] = f"tg-{chat_id}-{message_id}"
    return h


# ====== Letta create: async first; if timeout, sync fallback; if direct messages, return them ======
async def letta_create_task_or_reply(user_text: str, chat_id: int, message_id: int):
    url = f"{LETTA_BASE_URL}/v1/agents/{LETTA_AGENT_ID}/messages"
    headers = idempotency_headers(chat_id, message_id)

    async_payload = {
        "messages": [{"role": "user", "content": [{"type": "text", "text": user_text}]}],
        "use_assistant_message": True,
        "async": True,  # servers ignoring this will reply synchronously
    }

    short = httpx.Timeout(connect=CONNECT_TIMEOUT, read=READ_TIMEOUT, write=15.0, pool=READ_TIMEOUT)

    try:
        async with httpx.AsyncClient(timeout=short, follow_redirects=True) as c:
            r = await c.post(url, headers=headers, json=async_payload)
            r.raise_for_status()
            data = r.json()

            # (A) Async task response
            task_id = data.get("task_id") or data.get("id") or (data.get("task") or {}).get("id")
            if task_id:
                return task_id, None

            # (B) Direct reply response (no task_id): return messages right away
            reply = (extract_reply_from_messages(data)
                     or extract_reply_from_messages(data.get("result", {}))
                     or data.get("result_text") or "")
            if reply:
                return None, reply

            # (C) Unknown shape
            log.error("Unrecognized Letta response: %s", json.dumps(data)[:400])
            return None, "(no reply)"

    except httpx.ReadTimeout:
        # Fallback to a synchronous call with long read timeout (same idempotency)
        long_timeout = httpx.Timeout(connect=CONNECT_TIMEOUT, read=LONG_READ, write=30.0, pool=LONG_READ)
        sync_payload = {
            "messages": [{"role": "user", "content": [{"type": "text", "text": user_text}]}],
            "use_assistant_message": True,
        }
        async with httpx.AsyncClient(timeout=long_timeout, follow_redirects=True) as c:
            r = await c.post(url, headers=headers, json=sync_payload)
            r.raise_for_status()
            data = r.json()
            reply = (extract_reply_from_messages(data)
                     or extract_reply_from_messages(data.get("result", {}))
                     or data.get("result_text") or "(no reply)")
            return None, reply

    except httpx.HTTPStatusError as e:
        log.error("Create HTTP %s: %s", e.response.status_code, e.response.text[:400])
        raise


async def letta_poll_task(task_id: str) -> dict:
    url = f"{LETTA_BASE_URL}/v1/tasks/{task_id}"
    deadline = asyncio.get_event_loop().time() + POLL_TIMEOUT
    timeout = httpx.Timeout(connect=CONNECT_TIMEOUT, read=READ_TIMEOUT, write=15.0, pool=READ_TIMEOUT)

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
        while True:
            r = await c.get(url, headers=auth_headers())
            r.raise_for_status()
            data = r.json()
            status = (data.get("status") or "").lower()

            if status in {"succeeded", "completed", "done"}:
                return data
            if status in {"failed", "error", "canceled", "cancelled"}:
                raise RuntimeError(f"Task ended with status={status}")

            if asyncio.get_event_loop().time() >= deadline:
                raise TimeoutError("Task polling timed out")

            await asyncio.sleep(POLL_INTERVAL)


# Main job: create→maybe poll→send to Telegram
async def process_message(chat_id: int, message_id: int, user_text: str):
    typing_task = asyncio.create_task(_typing_loop(chat_id))
    try:
        task_id, immediate_reply = await letta_create_task_or_reply(user_text, chat_id, message_id)
    except Exception as e:
        log.exception("Create failed: %s", e)
        await telegram_send(chat_id, "(Letta request failed)")
        typing_task.cancel()
        with contextlib.suppress(Exception):
            await typing_task
        return

    if immediate_reply is not None:
        typing_task.cancel()
        with contextlib.suppress(Exception):
            await typing_task
        await telegram_send(chat_id, immediate_reply or "(no reply)")
        return

    # Poll task
    try:
        final = await letta_poll_task(task_id)
        reply = (extract_reply_from_messages(final)
                 or extract_reply_from_messages(final.get("result", {}))
                 or final.get("result_text") or "(no reply)")
    except TimeoutError:
        reply = "(Still working—I'll post results when they're ready.)"
        asyncio.create_task(_finish_and_send_when_ready(chat_id, task_id))
    except Exception as e:
        log.exception("Polling failed: %s", e)
        reply = "(Letta request failed)"
    finally:
        typing_task.cancel()
        with contextlib.suppress(Exception):
            await typing_task

    await telegram_send(chat_id, reply)


async def _finish_and_send_when_ready(chat_id: int, task_id: str):
    try:
        final = await letta_poll_task(task_id)
        reply = (extract_reply_from_messages(final)
                 or extract_reply_from_messages(final.get("result", {}))
                 or final.get("result_text") or "(no reply)")
        await telegram_send(chat_id, reply)
    except Exception:
        pass


async def _typing_loop(chat_id: int):
    try:
        while True:
            await send_typing(chat_id)
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        pass


# ====== Routes ======
@app.get("/healthz")
async def healthz():
    return {
        "ok": ok_env(),
        "has_bot_token": bool(BOT_TOKEN),
        "has_letta_base": bool(LETTA_BASE_URL),
        "has_letta_agent": bool(LETTA_AGENT_ID),
    }


@app.post("/telegram/{token}")
async def telegram_webhook(token: str, request: Request):
    if not ok_env():
        raise HTTPException(status_code=500, detail="Missing required environment variables")
    if token != BOT_TOKEN:
        raise HTTPException(status_code=403, detail="forbidden")

    update = await request.json()
    msg = (update.get("message") or {})
    text = (msg.get("text") or "").strip()
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")

    if not (text and chat_id and message_id):
        return {"ok": True}

    # Deduplicate exact Telegram message to avoid double-creates on retries
    dedup_key = f"{chat_id}:{message_id}"
    if DEDUP_CACHE.seen(dedup_key):
        log.info("Duplicate Telegram message ignored: %s", dedup_key)
        return {"ok": True}

    asyncio.create_task(process_message(chat_id, message_id, text))
    return {"ok": True}