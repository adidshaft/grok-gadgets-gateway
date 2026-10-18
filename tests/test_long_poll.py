"""Long-poll (GW-PROTO-002 #182, hub ADR 014 §1): a poll may wait for a command.

A gadget that adds `wait_ms` to a poll gets the reply when a command is ready or when the wait
ends. A gadget that sends a bare poll gets the same replies as before.
"""

import asyncio
import json
import time

import pytest
from test_domain import hello
from test_transports import connect, write_credentials

from grok_gadgets_gateway.domain import Gateway
from grok_gadgets_gateway.protocol import GatewayError, validate_request
from grok_gadgets_gateway.transport import Credentials, DeviceServer


async def send(reader, writer, message, timeout=12):
    writer.write((json.dumps(message) + "\n").encode())
    await writer.drain()
    return json.loads(await asyncio.wait_for(reader.readline(), timeout))


@pytest.mark.parametrize("wait", [100, 2500, 10000])
def test_wait_ms_is_a_valid_poll_field(wait):
    validate_request({"type": "poll", "wait_ms": wait})


@pytest.mark.parametrize("wait", [99, 10001, 2500.0, True, "2500", None, -1])
def test_bad_wait_ms_is_refused(wait):
    with pytest.raises(GatewayError) as exc:
        validate_request({"type": "poll", "wait_ms": wait})
    assert exc.value.code == "invalid_request"


async def server_with_device(tmp_path):
    path = tmp_path / "auth.json"
    write_credentials(path)
    gateway = Gateway()
    server = await DeviceServer(gateway, Credentials(path), port=0).start()
    reader, writer, reply = await connect(server)
    assert reply["ok"]
    return gateway, server, reader, writer


async def test_a_waiting_poll_answers_at_once_when_a_command_arrives(tmp_path):
    gateway, server, reader, writer = await server_with_device(tmp_path)
    try:
        waiting = asyncio.create_task(send(reader, writer, {"type": "poll", "wait_ms": 8000}))
        await asyncio.sleep(0.3)
        assert not waiting.done()
        started = time.monotonic()
        queued = gateway.command("dev-1", "rgb.set", {"r": 1, "g": 2, "b": 3, "on": True})
        reply = await asyncio.wait_for(waiting, 2)
        assert time.monotonic() - started < 1
        assert [c["command_id"] for c in reply["commands"]] == [queued["command_id"]]
        assert gateway.command_status(queued["command_id"])["status"] == "dispatched"
    finally:
        writer.close()
        await server.close()


async def test_a_waiting_poll_ends_empty_after_its_wait(tmp_path):
    _, server, reader, writer = await server_with_device(tmp_path)
    try:
        started = time.monotonic()
        reply = await send(reader, writer, {"type": "poll", "wait_ms": 400})
        took = time.monotonic() - started
        assert reply == {"ok": True, "commands": []}
        assert 0.35 < took < 1.5
    finally:
        writer.close()
        await server.close()


async def test_a_queued_command_skips_the_wait(tmp_path):
    gateway, server, reader, writer = await server_with_device(tmp_path)
    try:
        gateway.command("dev-1", "rgb.set", {"r": 1, "g": 2, "b": 3, "on": True})
        started = time.monotonic()
        reply = await send(reader, writer, {"type": "poll", "wait_ms": 8000})
        assert reply["commands"] and time.monotonic() - started < 1
    finally:
        writer.close()
        await server.close()


async def test_a_bare_poll_never_waits(tmp_path):
    _, server, reader, writer = await server_with_device(tmp_path)
    try:
        started = time.monotonic()
        assert await send(reader, writer, {"type": "poll"}) == {"ok": True, "commands": []}
        assert time.monotonic() - started < 0.5
    finally:
        writer.close()
        await server.close()


async def test_a_replaced_session_ends_the_wait_as_stale(tmp_path):
    _, server, reader, writer = await server_with_device(tmp_path)
    try:
        waiting = asyncio.create_task(send(reader, writer, {"type": "poll", "wait_ms": 3000}))
        await asyncio.sleep(0.2)
        _, w2, reply = await connect(server)  # Same device, new session.
        assert reply["ok"]
        old = await asyncio.wait_for(waiting, 5)
        assert old["ok"] is False and old["error"]["code"] == "stale_session"
        w2.close()
    finally:
        writer.close()
        await server.close()


async def test_close_returns_while_a_poll_waits(tmp_path):
    _, server, _, writer = await server_with_device(tmp_path)
    writer.write(b'{"type":"poll","wait_ms":10000}\n')
    await writer.drain()
    await asyncio.sleep(0.2)
    async with asyncio.timeout(4):
        await server.close()
    writer.close()


async def test_the_gateway_tells_listeners_about_new_commands():
    gateway = Gateway()
    gateway.register(hello("dev-1"))
    seen = []
    gateway.listeners.append(seen.append)
    gateway.command("dev-1", "rgb.set", {"r": 0, "g": 0, "b": 0, "on": False})
    assert seen == ["dev-1"]
