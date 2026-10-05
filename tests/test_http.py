import asyncio
import io
import json
import socket

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from grok_gadgets_gateway.domain import Gateway
from grok_gadgets_gateway.mcp_server import make_server
from grok_gadgets_gateway.operator import atomic_write
from grok_gadgets_gateway.service import serve_gateway
from grok_gadgets_gateway.simulator import Simulator


TOKEN = "mcp-test-token-value"
DEVICE = "0123456789abcdef"


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def write_local_config(tmp_path):
    credentials = tmp_path / "credentials.json"
    mcp_token = tmp_path / "mcp-token"
    atomic_write(
        credentials,
        json.dumps({"devices": {"dev-1": {"token": DEVICE, "revoked": False}}}).encode() + b"\n",
    )
    atomic_write(mcp_token, (TOKEN + "\n").encode())
    return credentials, mcp_token


class ForcedHost(httpx.AsyncBaseTransport):
    def __init__(self, host):
        self._inner = httpx.AsyncHTTPTransport()
        self._host = host

    async def handle_async_request(self, request):
        request.headers["host"] = self._host
        return await self._inner.handle_async_request(request)

    async def aclose(self):
        await self._inner.aclose()


async def first_status(url, headers=None, transport=None):
    seen = []

    async def hook(response):
        seen.append(response.status_code)

    client = httpx.AsyncClient(
        headers=headers,
        transport=transport,
        timeout=httpx.Timeout(3, read=3),
        follow_redirects=True,
        event_hooks={"response": [hook]},
    )
    try:
        async with client:
            async with streamable_http_client(url, http_client=client) as (read, write, _session):
                async with ClientSession(read, write) as session:
                    await asyncio.wait_for(session.initialize(), 3)
    except (Exception, ExceptionGroup):
        pass
    return seen


def test_http_mode_rejects_test_controls_and_non_loopback():
    gateway = Gateway()
    simulator = Simulator(gateway)
    with pytest.raises(ValueError):
        make_server(gateway, simulator, test_controls=True, http=True)
    with pytest.raises(ValueError):
        make_server(gateway, simulator, device_server=object(), http=True)


async def test_streamable_http_requires_token_and_runs_simulator(tmp_path):
    credentials, mcp_token = write_local_config(tmp_path)
    logs = io.StringIO()
    port = free_port()
    device_port = free_port()
    async with serve_gateway(
        credentials,
        mcp_token,
        simulator=True,
        device_port=device_port,
        port=port,
        allowed_hosts=["tunnel.example"],
        log_stream=logs,
    ) as running:
        assert running.url == f"http://127.0.0.1:{port}/mcp"
        assert running.device_port == device_port
        assert await first_status(running.url) == [401]
        assert await first_status(running.url, {"Authorization": "Bearer wrong-token"}) == [401]

        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {TOKEN}"},
            timeout=httpx.Timeout(5, read=5),
            follow_redirects=True,
        ) as http:
            async with streamable_http_client(running.url, http_client=http) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = (await session.list_tools()).tools
                    names = {tool.name for tool in tools}
                    assert "test_simulator_control" not in names
                    assert {
                        "gadgets_list_devices",
                        "gadgets_get_state",
                        "gadgets_command",
                        "gadgets_command_status",
                        "gadgets_read_events",
                        "gadgets_diagnostics",
                    } <= names
                    for tool in tools:
                        assert tool.annotations is not None
                        if tool.name == "gadgets_command":
                            assert tool.annotations.readOnlyHint is False
                        else:
                            assert tool.annotations.readOnlyHint is True
                    result = await session.call_tool(
                        "gadgets_command",
                        {
                            "device_id": "sim-c124",
                            "capability": "rgb.set",
                            "arguments": {"r": 0, "g": 255, "b": 0, "on": True},
                        },
                    )
                    assert not result.isError
                    body = result.structuredContent or json.loads(result.content[0].text)
                    assert body["command"]["status"] == "executed"
                    assert body["command"]["simulated"] is True
                    report = await session.call_tool("gadgets_diagnostics", {})
                    diag = report.structuredContent or json.loads(report.content[0].text)
                    listener = diag["report"]["device_listener"]
                    assert listener["host"] == "127.0.0.1" and listener["port"] == device_port

        rejected = await first_status(
            running.url,
            {"Authorization": f"Bearer {TOKEN}"},
            ForcedHost("evil.example"),
        )
        assert 421 in rejected

    text = logs.getvalue()
    assert "args_sha256" in text and TOKEN not in text and DEVICE not in text
    assert '"g":255' not in text and '"g": 255' not in text
