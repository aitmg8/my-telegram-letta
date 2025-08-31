# main.py — Telegram ↔ Letta bridge with voice transcription + structured location (Lat/Lon)
import os, json, asyncio, logging, contextlib, time
from collections import OrderedDict
from fastapi import FastAPI, Request, HTTPException
import httpx

log = logging.getLogger("uvicorn.error")
app = FastAPI()  # uvicorn main:app

# ====== ENV ======
BOT_TOKEN       = os.getenv("BOT_TOKEN")
LETTA_BASE_URL  = (os.getenv("LETTA_BASE_URL") or "").rstrip("/")
LETTA_AGENT_ID  = os.getenv("LETTA_AGENT_ID")
LETTA_TOKEN     = os.getenv("LETTA_TOKEN")  # optional

OPENAI_API_KEY        = os.getenv("OPENAI_API_KEY")              # for voice
OPENAI_WHISPER_MODEL  = os.getenv("OPENAI_WHISPER_MODEL", "whisper-1")

# Timeouts / polling
CONNECT_TIMEOUT = float(os.getenv("CONNECT_TIMEOUT", "20"))
READ_TIMEOUT    = float(os.getenv("READ_TIMEOUT",   "30"))   # async create short read
LONG_READ       = float(os.getenv("LONG_READ",     "600"))   # sync fallback long read
POLL_INTERVAL   = float(os.getenv("LETTA_POLL_INTERVAL", "2.5"))
POLL_TIMEOUT    = float(os.getenv("LETTA_POLL_TIMEOUT",  "600"))

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else None
TELEGRAM_FILE_API = f"https://api.telegram.org/file/bot{BOT_TOKEN}" if BOT_TOKEN else None

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
async def send_typing(chat_id: int, action: str = "typing"):
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            await c.post(f"{TELEGRAM_API}/sendChatAction",
                         json={"chat_id": chat_id, "action": action})
    except Exception as e:
        log.debug("sendChatAction failed: %s", e)

async def telegram_send(chat_id: int, text: str):
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            await c.post(f"{TELEGRAM_API}/sendMessage",
                         json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True})
    except Exception as e:
        log.exception("sendMessage failed: %s", e)

# ====== Voice/transcription ======
async def telegram_get_file_path(file_id: str) -> str:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{TELEGRAM_API}/getFile", params={"file_id": file_id})
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"getFile not ok: {data}")
        return data["result"]["file_path"]

async def telegram_download_file(file_path: str) -> bytes:
    url = f"{TELEGRAM_FILE_API}/{file_path}"
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.get(url)
        r.raise_for_status()
        return r.content

async def whisper_transcribe_ogg(opus_bytes: bytes, filename: str = "audio.ogg") -> str:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY missing; cannot transcribe voice.")
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    files = {
        "file": (filename, opus_bytes, "audio/ogg"),
        "model": (None, OPENAI_WHISPER_MODEL),
    }
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post("https://api.openai.com/v1/audio/transcriptions",
                         headers=headers, files=files)
        r.raise_for_status()
        data = r.json()
        return (data.get("text") or "").strip()

async def transcribe_from_telegram_message(msg: dict, chat_id: int) -> str | None:
    voice = msg.get("voice")
    video_note = msg.get("video_note")
    audio = msg.get("audio")
    file_id = None
    filename = "audio.ogg"
    if voice and voice.get("file_id"):
        file_id = voice["file_id"]; filename = "voice.ogg"
    elif video_note and video_note.get("file_id"):
        file_id = video_note["file_id"]; filename = "video_note.ogg"
    elif audio and audio.get("file_id"):
        file_id = audio["file_id"]; filename = audio.get("file_name") or "audio.m4a"
    if not file_id:
        return None
    await send_typing(chat_id, action="record_voice")
    try:
        file_path = await telegram_get_file_path(file_id)
        blob = await telegram_download_file(file_path)
        transcript = await whisper_transcribe_ogg(blob, filename=filename)
        return transcript or None
    except Exception as e:
        log.exception("Transcription failed: %s", e)
        await telegram_send(chat_id, "(Couldn’t transcribe the voice message.)")
        return None

# ====== Location handling (Lat/Lon format) ======
def extract_location_text(msg: dict) -> str | None:
    """
    Build concise text from Telegram location/venue/live-location.
    Format:
      - First line: 'Lat: <>, Lon: <>'
      - Then a Google Maps link
      - Optional venue title/address
    """
    def fmt(v: float) -> str:
        return f"{v:.6f}"

    # Venue
    venue = msg.get("venue")
    if venue and isinstance(venue, dict):
        loc = venue.get("location") or {}
        lat, lon = loc.get("latitude"), loc.get("longitude")
        if lat is not None and lon is not None:
            title = venue.get("title") or None
            address = venue.get("address") or None
            lines = [f"Lat: {fmt(lat)}, Lon: {fmt(lon)}",
                     f"Map: https://maps.google.com/?q={fmt(lat)},{fmt(lon)}"]
            if title:   lines.append(f"Venue: {title}")
            if address: lines.append(f"Address: {address}")
            return "\n".join(lines)

    # Plain (or live) location
    loc = msg.get("location")
    if loc and isinstance(loc, dict):
        lat, lon = loc.get("latitude"), loc.get("longitude")
        if lat is None or lon is None:
            return None
        return "\n".join([
            f"Lat: {fmt(lat)}, Lon: {fmt(lon)}",
            f"Map: https://maps.google.com/?q={fmt(lat)},{fmt(lon)}",
        ])

    return None

# ====== Letta helpers ======
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

def idempotency_headers(chat_id: int, message_id: int) -> dict:
    h = auth_headers()
    h["Idempotency-Key"] = f"tg-{chat_id}-{message_id}"
    return h

async def letta_create_task_or_reply(user_text: str, chat_id: int, message_id: int):
    """Try async create first; if timeout, fallback to sync; if direct reply, return it."""
    url = f"{LETTA_BASE_URL}/v1/agents/{LETTA_AGENT_ID}/messages"
    headers = idempotency_headers(chat_id, message_id)

    async_payload = {
        "messages": [{"role": "user", "content": [{"type": "text", "text": user_text}]}],
        "use_assistant_message": True,
        "async": True,  # servers ignoring this just reply synchronously
    }
    short = httpx.Timeout(connect=CONNECT_TIMEOUT, read=READ_TIMEOUT, write=15.0, pool=READ_TIMEOUT)

    try:
        async with httpx.AsyncClient(timeout=short, follow_redirects=True) as c:
            r = await c.post(url, headers=headers, json=async_payload)
            r.raise_for_status()
            data = r.json()
            task_id = data.get("task_id") or data.get("id") or (data.get("task") or {}).get("id")
            if task_id:
                return task_id, None
            reply = (extract_reply_from_messages(data)
                     or extract_reply_from_messages(data.get("result", {}))
                     or data.get("result_text") or "")
            if reply:
                return None, reply
            log.error("Unrecognized Letta response: %s", json.dumps(data)[:400])
            return None, "(no reply)"

    except httpx.ReadTimeout:
        # Fallback: synchronous create with long read (same idempotency key)
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

# Main job
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
        "whisper_ready": bool(OPENAI_API_KEY),
    }

@app.post("/telegram/{token}")
async def telegram_webhook(token: str, request: Request):
    if not ok_env():
        raise HTTPException(status_code=500, detail="Missing required environment variables")
    if token != BOT_TOKEN:
        raise HTTPException(status_code=403, detail="forbidden")

    update = await request.json()
    msg = (update.get("message") or {})
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")
    text = (msg.get("text") or "").strip()

    if not (chat_id and message_id):
        return {"ok": True}

    # Deduplicate exact Telegram message
    dedup_key = f"{chat_id}:{message_id}"
    if DEDUP_CACHE.seen(dedup_key):
        log.info("Duplicate Telegram message ignored: %s", dedup_key)
        return {"ok": True}

    # If they sent a location/venue/live location, convert to "Lat/Lon" text
    loc_text = extract_location_text(msg)
    if loc_text:
        text = loc_text

    # If still no text, try voice/audio/video_note transcription
    if not text:
        if OPENAI_API_KEY:
            await telegram_send(chat_id, "🎤 Got your voice note — transcribing…")
        transcript = await transcribe_from_telegram_message(msg, chat_id)
        if transcript:
            text = transcript
        else:
            return {"ok": True}

    # Proceed with normal Letta flow
    asyncio.create_task(process_message(chat_id, message_id, text))
    return {"ok": True}
