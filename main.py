import httpx, json, logging
log = logging.getLogger("uvicorn.error")

async def letta_create_task_or_reply(user_text: str):
    """
    1) Try async create (fast response expected). If we get a task_id, return (task_id, None).
    2) If async create times out, FALL BACK to a long-read sync create and return (None, reply_text).
    3) If server replies synchronously even to async (no task_id), return (None, reply_text).
    """
    url = f"{LETTA_BASE_URL}/v1/agents/{LETTA_AGENT_ID}/messages"

    async_payload = {
        "messages": [{"role": "user", "content": [{"type": "text", "text": user_text}]}],
        "use_assistant_message": True,
        "async": True,   # servers ignoring this will just reply synchronously
    }

    # Short timeout for the async enqueue step
    short = httpx.Timeout(
        connect=CONNECT_TIMEOUT,
        read=READ_TIMEOUT,     # e.g. 30s (from env)
        write=15.0,
        pool=READ_TIMEOUT,
    )

    try:
        async with httpx.AsyncClient(timeout=short, follow_redirects=True) as c:
            r = await c.post(url, headers=auth_headers(), json=async_payload)
            r.raise_for_status()
            data = r.json()

            # Async shape
            task_id = data.get("task_id") or data.get("id") or (data.get("task") or {}).get("id")
            if task_id:
                log.info("Created Letta task_id=%s", task_id)
                return task_id, None

            # Sync shape (server returned final messages immediately)
            reply = (extract_reply_from_messages(data)
                     or extract_reply_from_messages(data.get("result", {}))
                     or data.get("result_text") or "")
            if reply:
                log.info("Letta replied synchronously (fast)")
                return None, reply

            log.error("Unknown Letta response shape: %s", json.dumps(data)[:400])
            raise RuntimeError("Unrecognized Letta response (no task_id and no reply).")

    except httpx.ReadTimeout:
        # Fallback to a synchronous call with a long read timeout (e.g. 10 minutes)
        long_timeout = httpx.Timeout(
            connect=CONNECT_TIMEOUT,
            read=600.0,   # 10 minutes
            write=30.0,
            pool=600.0,
        )
        sync_payload = {
            "messages": [{"role": "user", "content": [{"type": "text", "text": user_text}]}],
            "use_assistant_message": True,
            # no "async": True -> ask server to complete and return messages
        }
        log.warning("Async create timed out; falling back to sync with long read timeout")
        async with httpx.AsyncClient(timeout=long_timeout, follow_redirects=True) as c:
            r = await c.post(url, headers=auth_headers(), json=sync_payload)
            r.raise_for_status()
            data = r.json()
            reply = (extract_reply_from_messages(data)
                     or extract_reply_from_messages(data.get("result", {}))
                     or data.get("result_text") or "(no reply)")
            log.info("Letta replied synchronously (fallback)")
            return None, reply

    except httpx.HTTPStatusError as e:
        log.error("Create HTTP %s: %s", e.response.status_code, e.response.text[:400])
        raise
