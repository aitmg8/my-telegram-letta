import os, json, asyncio, logging, contextlib
from fastapi import FastAPI, Request, HTTPException
import httpx

log = logging.getLogger("uvicorn.error")
app = FastAPI()

# ====== ENV ======
BOT_TOKEN       = os.getenv("BOT_TOKEN")
LETTA_BASE_URL  = (os.getenv("LETTA_BASE_URL") or "").rstrip("/")
LETTA_AGENT_ID  = os.getenv("LETTA_AGENT_ID")
LETTA_TOKEN     = os.getenv("LETTA_TOKEN")  # optional

# Polling knobs (seconds)
POLL_INTERVAL   = float(os.getenv("LETTA_POLL_INTERVAL", "2.5"))
POLL_TIMEOUT    = float(os.getenv("LETTA_POLL_TIMEOUT",  "600"))

# Network knobs
CONNECT_TIMEOUT = float(os.getenv("CONNECT_TIMEOUT", "20"))
READ_TIMEOUT    = float(os.getenv("READ_TIMEOUT",   "30"))

TELEGRAM_API    = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else None


def ok_env():
    return bool(BOT_TOKEN and LETTA_BASE_URL and LETTA_AGENT_ID)


def auth_headers():
    h = {"Content-Type": "application/json"}
    if LETTA_TOKEN:
        h["Authorization"] = f"Bearer {LETTA_TOKEN}"
    return h


# ====== Telegram helpers ======
async def send_typing(chat_id: int):
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            await c.post(f"{TELEGRAM_API}/sendChatAction",
                         json={"chat_id": chat_id, "action": "typing"})
    except Exception as e:
        log.debug("send_typing failed: %s", e)


async def telegram_send(chat_id: int, text: str):
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            await c.post(f"{TELEGRAM_API}/sendMessage",
                         json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True})
    except Exception as e:
        log.exception("Telegram sendMessage failed: %s", e)


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


# ====== Letta: dual-mode (async or sync) ======
async def letta_create_task_or_reply(user_text: str):
    """
    Try async first. If server returns task_id -> (task_id, None).
    If it returns messages immediately -> (None, reply_text).
    """
    url = f"{LETTA_BASE_URL}/v1/agents/{LETTA_AGENT_ID}/messages"
    payload = {
        "messages": [{"role": "user", "content": [{"type": "text", "text": user_text}]}],
        "use_assistant_message": True,
        "async": True,  # safe: servers that ignore it just reply synchronously
    }
    timeout = httpx.Timeout(connect=CONNECT_TIMEOUT, read=READ_TIMEOUT, write=15.0, pool=READ_TIMEOUT)

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
        r = await c.post(url, headers=auth_headers(), json=payload)
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            log.error("Create HTTP %s: %s", e.response.status_code, e.response.text[:400])
            raise

        data = r.json()
        # 1) Async shape
        task_id = data.get("task_id") or data.get("id") or (data.get("task") or {}).get("id")
        if task_id:
            log.info("Created Letta task_id=%s", task_id)
            return task_id, None

        # 2) Sync shape
        reply = extract_reply_from_messages(data) or extract_reply_from_messages(data.get("result", {})) \
                or data.get("result_text") or ""
        if reply:
            log.info("Letta replied synchronously")
            return None, reply

        log.error("Unknown Letta response shape: %s", json.dumps(data)[:400])
        raise RuntimeError("Unrecognized Letta response (no task_id and no reply).")


async def letta_poll_task(task_id: str) -> dict:
    url = f"{LETTA_BASE_URL}/v1/tasks/{task_id}"
    deadline = asyncio.get_event_loop().time() + POLL_TIMEOUT
    timeout = httpx.Timeout(connect=CONNECT_TIMEOUT, read=READ_TIMEOUT, write=15.0, pool=READ_TIMEOUT)

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
        while True:
            r = await c.get(url, headers=auth_headers())
            try:
                r.raise_for_status()
            except httpx.HTTPStatusError as e:
                log.error("Poll %s HTTP %s: %s", task_id, e.response.status_code, e.response.text[:400])
                raise

            data = r.json()
            status = (data.get("status") or "").lower()
            log.debug("Task %s status=%s", task_id, status)

            if status in {"succeeded", "completed", "done"}:
                return data
            if status in {"failed", "error", "canceled", "cancelled"}:
                raise RuntimeError(f"Task ended with status={status}")

            if asyncio.get_event_loop().time() >= deadline:
                raise TimeoutError("Task polling timed out")

            await asyncio.sleep(POLL_INTERVAL)


async def query_letta_dual(chat_id: int, user_text: str):
    typing_task = asyncio.create_task(_typing_loop(chat_id))
    log.info("Telegram update: chat_id=%s text=%r", chat_id, user_text)

    try:
        task_id, immediate_reply = await letta_create_task_or_reply(user_text)
    except Exception as e:
        log.exception("Create failed: %s", e)
        await telegram_send(chat_id, "(Letta request failed)")
        typing_task.cancel()
        with contextlib.suppress(Exception):
            await typing_task
        return

    # If sync reply, send it now.
    if immediate_reply is not None:
        typing_task.cancel()
        with contextlib.suppress(Exception):
            await typing_task
        await telegram_send(chat_id, immediate_reply or "(no reply)")
        return

    # Otherwise, poll the task.
    try:
        final = await letta_poll_task(task_id)
        reply = extract_reply_from_messages(final) \
                or extract_reply_from_messages(final.get("result", {})) \
                or final.get("result_text") \
                or "(no reply)"
        log.info("Task %s done; sending reply", task_id)
    except TimeoutError:
        reply = "(Still working—I’ll keep checking and reply when ready.)"
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
        reply = extract_reply_from_messages(final) \
                or extract_reply_from_messages(final.get("result", {})) \
                or final.get("result_text") \
                or "(no reply)"
        await telegram_send(chat_id, reply)
    except Exception as e:
        log.debug("Late-finish polling aborted for %s: %s", task_id, e)


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
        "uses_auth": bool(LETTA_TOKEN),
        "poll_interval": POLL_INTERVAL,
        "poll_timeout": POLL_TIMEOUT,
    }


@app.get("/debug/letta")
async def debug_letta():
    try:
        headers = auth_headers()
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
            r = await c.get(f"{LETTA_BASE_URL}/v1/health/", headers=headers)
        return {"ok": r.is_success, "status": r.status_code, "text": r.text[:200]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/telegram/{token}")
async def telegram_webhook(token: str, request: Request):
    if not ok_env():
        raise HTTPException(status_code=500, detail="Missing required environment variables")
    if token != BOT_TOKEN:
        raise HTTPException(status_code=403, detail="forbidden")

    try:
        update = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="bad json")

    msg = update.get("message") or {}
    text = (msg.get("text") or "").strip()
    chat_id = (msg.get("chat") or {}).get("id")
    if not (text and chat_id):
        log.info("Ignoring update: %s", json.dumps(update)[:500])
        return {"ok": True}

    asyncio.create_task(query_letta_dual(chat_id, text))
    return {"ok": True}
