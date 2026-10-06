import asyncio
import io
import json
import os
import socket
import sys
from pathlib import Path

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
        # Static bearer mode is not OAuth: no resource metadata, no discovery documents.
        base = running.url.removesuffix("/mcp")
        async with httpx.AsyncClient(trust_env=False) as plain:
            denied = await plain.post(running.url, json={})
            assert denied.status_code == 401
            assert denied.headers["www-authenticate"] == 'Bearer error="invalid_token"'
            assert "resource_metadata" not in denied.text
            for path in (
                "/.well-known/oauth-protected-resource",
                "/.well-known/oauth-protected-resource/mcp",
                "/.well-known/oauth-authorization-server",
            ):
                found = await plain.get(base + path, headers={"Authorization": f"Bearer {TOKEN}"})
                assert found.status_code == 404, path

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


async def test_cli_serves_enrolled_device_and_revokes_live_access(tmp_path):
    """Run the documented commands in a real process with private throwaway config."""
    env = {**os.environ, "XDG_CONFIG_HOME": str(tmp_path)}

    async def cli(*args):
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "grok_gadgets_gateway.cli",
            *args,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
        assert process.returncode == 0, stderr.decode()
        return stdout.decode()

    await cli("init")
    enrolled = await cli("enroll", "local-test-device")
    device_token = enrolled.strip().split("=", 1)[1]
    token_file = tmp_path / "grok-gadgets" / "mcp-token"
    mcp_token = token_file.read_text().strip()
    assert await cli("devices") == "local-test-device\n"
    port, device_port = free_port(), free_port()
    while device_port == port:
        device_port = free_port()
    url = f"http://127.0.0.1:{port}/mcp"
    writer = None
    with (tmp_path / "serve.log").open("wb") as log:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "grok_gadgets_gateway.cli",
            "serve",
            "--simulator",
            "--port",
            str(port),
            "--device-port",
            str(device_port),
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=log,
        )
        try:
            async with asyncio.timeout(10):
                async with httpx.AsyncClient(timeout=1, trust_env=False) as probe:
                    while True:
                        assert process.returncode is None, "serve exited before readiness"
                        try:
                            if (await probe.get(url)).status_code == 401:
                                break
                        except httpx.TransportError:
                            pass
                        await asyncio.sleep(0.05)
            reader, writer = await asyncio.open_connection("127.0.0.1", device_port)

            async def exchange(value):
                writer.write((json.dumps(value) + "\n").encode())
                await writer.drain()
                return json.loads(await asyncio.wait_for(reader.readline(), 3))

            fixture = Path(__file__).parents[1] / "protocol/0.1.0/fixtures/device-transcript.json"
            hello = json.loads(fixture.read_text())[0]
            hello["device"].update(device_id="local-test-device", simulated=True)
            hello["token"] = device_token
            assert (await exchange(hello))["ok"]
            async with httpx.AsyncClient(
                headers={"Authorization": f"Bearer {mcp_token}"},
                trust_env=False,
            ) as http:
                async with streamable_http_client(url, http_client=http) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()

                        async def call(name, arguments):
                            result = await session.call_tool(name, arguments)
                            assert not result.isError
                            return result.structuredContent or json.loads(result.content[0].text)

                        devices = (await call("gadgets_list_devices", {}))["devices"]
                        assert {d["device_id"] for d in devices} == {
                            "sim-c124",
                            "local-test-device",
                        }
                        arguments = {"r": 7, "g": 11, "b": 13, "on": True}
                        request = {
                            "device_id": "local-test-device",
                            "capability": "rgb.set",
                            "arguments": arguments,
                        }
                        # gadgets_command waits for the device's ACK and returns the outcome.
                        pending = asyncio.create_task(call("gadgets_command", request))
                        delivered = []
                        while not delivered:
                            delivered = (await exchange({"type": "poll"}))["commands"]
                        command_id = delivered[0]["command_id"]
                        assert delivered == [
                            {
                                "command_id": command_id,
                                "capability": "rgb.set",
                                "arguments": arguments,
                            }
                        ]
                        assert (
                            await exchange(
                                {
                                    "type": "ack",
                                    "command_id": command_id,
                                    "status": "executed",
                                    "state": {"rgb": arguments},
                                }
                            )
                        )["ok"]
                        receipt = (await pending)["command"]
                        assert receipt["command_id"] == command_id
                        assert receipt["status"] == "executed"
                        assert receipt["reported_state"] == {"rgb": arguments}
                        retry = (
                            await call(
                                "gadgets_command", {**request, "command_id": receipt["command_id"]}
                            )
                        )["command"]
                        assert retry["duplicate"] and retry["status"] == "executed"
                        assert retry["simulated"] and not retry["physical_verified"]
                        assert (await exchange({"type": "poll"}))["commands"] == []

            # Device service survives the MCP client's departure.
            assert (await exchange({"type": "ping"}))["ok"]
            await cli("revoke", "local-test-device")
            assert (await exchange({"type": "ping"}))["error"]["code"] == "revoked"
            assert await asyncio.wait_for(reader.readline(), 3) == b""
            writer.close()
            await writer.wait_closed()
            writer = None
            await cli("rotate-mcp-token")
            rotated = token_file.read_text().strip()
            assert rotated != mcp_token
            assert await first_status(url, {"Authorization": f"Bearer {mcp_token}"}) == [401]
            assert (await first_status(url, {"Authorization": f"Bearer {rotated}"}))[0] == 200
            assert process.returncode is None
        finally:
            if writer is not None:
                writer.close()
                await writer.wait_closed()
            if process.returncode is None:
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 10)
            except TimeoutError:
                process.kill()
                await process.wait()
                raise
    logs = (tmp_path / "serve.log").read_text()
    assert device_token not in logs and mcp_token not in logs and rotated not in logs
    assert '"event":"mcp_tool_call"' in logs and '"duplicate":true' in logs
    # SIGTERM is a graceful stop: devices and HTTP close, and the process exits 0.
    assert process.returncode == 0
    assert "Gateway stopped; device sessions closed." in logs
