import asyncio
import io
import json

import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from test_http import TOKEN, free_port, write_local_config

from grok_gadgets_gateway.mcp_server import RateLimiter, RequestLog
from grok_gadgets_gateway.operator import issue_mcp_token
from grok_gadgets_gateway.protocol import GatewayError
from grok_gadgets_gateway.service import BearerAuth, Limits, serve_gateway

BLUE = {"r": 0, "g": 120, "b": 255, "on": True}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_rate_limiter_windows_are_per_token_and_recover():
    clock = Clock()
    limiter = RateLimiter(calls_per_minute=3, commands_per_minute=1, clock=clock)
    limiter.check("a", "gadgets_command")
    with pytest.raises(GatewayError, match="commands") as caught:
        limiter.check("a", "gadgets_command")
    assert caught.value.code == "rate_limited" and "60 s" in caught.value.message
    limiter.check("a", "gadgets_list_devices")
    limiter.check("a", "gadgets_get_state")  # rejected command was not counted
    with pytest.raises(GatewayError, match="tool calls"):
        limiter.check("a", "gadgets_diagnostics")
    limiter.check("b", "gadgets_command")  # another token has its own windows
    limiter.check(None, "gadgets_command")  # stdio has no token and no limit
    clock.now += 60
    limiter.check("a", "gadgets_command")


async def test_serve_returns_rate_limited_per_token(tmp_path):
    credentials, mcp_token = write_local_config(tmp_path)
    tokens = tmp_path / "mcp-tokens.json"
    other = issue_mcp_token("other", tokens)
    log_path = tmp_path / "requests.jsonl"
    async with serve_gateway(
        credentials,
        mcp_token,
        simulator=True,
        port=free_port(),
        device_port=free_port(),
        mcp_tokens=tokens,
        rate_limiter=RateLimiter(calls_per_minute=10, commands_per_minute=2),
        log_stream=io.StringIO(),
        request_log=log_path,
    ) as running:

        async def commands(token, count):
            outcomes = []
            async with (
                httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http,
                streamable_http_client(running.url, http_client=http) as (read, write),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                for _ in range(count):
                    result = await session.call_tool(
                        "gadgets_command",
                        {"device_id": "sim-c124", "capability": "rgb.set", "arguments": BLUE},
                    )
                    body = result.structured_content or json.loads(result.content[0].text)
                    outcomes.append("ok" if body["ok"] else body["error"]["code"])
            return outcomes

        assert await commands(TOKEN, 3) == ["ok", "ok", "rate_limited"]
        assert await commands(other, 1) == ["ok"]
    calls = [json.loads(line) for line in log_path.read_text().splitlines()]
    limited = [c for c in calls if c.get("outcome") == "rate_limited"]
    assert len(limited) == 1 and limited[0]["token"] == "default"


class Verifier:
    async def verify_token(self, token):
        return {"t1": "one", "t2": "two"}.get(token)


def request(token="t1", method="POST", body=b"{}", length=True):
    headers = [(b"authorization", f"Bearer {token}".encode())]
    if length:
        headers.append((b"content-length", str(len(body)).encode()))
    return {"type": "http", "method": method, "path": "/mcp", "headers": headers}, body


async def call(auth, token="t1", method="POST", body=b"{}", length=True, chunks=1):
    scope, payload = request(token, method, body, length)
    size = max(1, len(payload) // chunks)
    parts = [payload[i : i + size] for i in range(0, len(payload), size)] or [b""]
    messages = [
        {"type": "http.request", "body": part, "more_body": i < len(parts) - 1}
        for i, part in enumerate(parts)
    ]
    sent = []

    async def receive():
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    await auth(scope, receive, send)
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


async def echo_app(scope, receive, send):
    while True:
        message = await receive()
        if not message.get("more_body"):
            break
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


async def test_body_cap_rejects_declared_and_streamed_bodies():
    stream = io.StringIO()
    auth = BearerAuth(
        echo_app, Verifier(), Limits(max_body_bytes=100), RequestLog("test", stream=stream)
    )
    assert await call(auth, body=b"x" * 100) == 200
    assert await call(auth, body=b"x" * 101) == 413
    assert await call(auth, body=b"x" * 400, length=False, chunks=4) == 413
    assert await call(auth, token="bad") == 401
    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [(line["status"], line["reason"], line["token"]) for line in lines] == [
        (413, "body_too_large", "one"),
        (413, "body_too_large", "one"),
        (401, "invalid_token", None),
    ]
    assert "t1" not in stream.getvalue()


async def test_concurrency_cap_is_per_token_and_released():
    gate = asyncio.Event()
    entered = 0

    async def slow_app(scope, receive, send):
        nonlocal entered
        entered += 1
        await gate.wait()
        await echo_app(scope, receive, send)

    auth = BearerAuth(slow_app, Verifier(), Limits(max_concurrent=2))
    held = [asyncio.create_task(call(auth, method="GET")) for _ in range(2)]
    while entered < 2:
        await asyncio.sleep(0.01)
    assert await call(auth) == 429
    other = asyncio.create_task(call(auth, token="t2"))
    while entered < 3:
        await asyncio.sleep(0.01)
    gate.set()
    assert await asyncio.gather(*held, other) == [200, 200, 200]
    assert await call(auth) == 200 and auth.active == {}


async def test_request_timeout_applies_to_post_only():
    async def stuck_app(scope, receive, send):
        await asyncio.sleep(0.3)
        await echo_app(scope, receive, send)

    auth = BearerAuth(stuck_app, Verifier(), Limits(request_timeout=0.05))
    assert await call(auth) == 504
    assert await call(auth, method="GET") == 200
    assert auth.active == {}
