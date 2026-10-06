"""gadgets_command waits (bounded) for the device's report and returns the outcome."""

import asyncio
import json
import time

from grok_gadgets_gateway.domain import Gateway
from grok_gadgets_gateway.mcp_server import make_server
from test_domain import hello

RGB = {"r": 1, "g": 2, "b": 3, "on": True}


def setup(wait):
    gateway = Gateway()
    session = gateway.register(hello("dev-1"))
    server = make_server(gateway, command_wait=wait)
    return gateway, session, server


async def command(server, **extra):
    result = await server.call_tool(
        "gadgets_command",
        {"device_id": "dev-1", "capability": "rgb.set", "arguments": RGB, **extra},
    )
    content = result[0] if isinstance(result, tuple) else result
    return json.loads(content[0].text)


async def device(gateway, session, status="executed"):
    """A device that polls every 10 ms and acknowledges the first command it gets."""
    while True:
        delivered = gateway.handle("dev-1", session, {"type": "poll"})["commands"]
        if delivered:
            ack = {"type": "ack", "command_id": delivered[0]["command_id"], "status": status}
            ack["state"] = {"rgb": RGB}
            if status == "failed":
                ack["error"] = {"code": "device_error", "message": "x"}
            gateway.handle("dev-1", session, ack)
            return
        await asyncio.sleep(0.01)


async def test_command_returns_the_device_outcome():
    gateway, session, server = setup(3)
    worker = asyncio.create_task(device(gateway, session))
    response = await command(server)
    await worker
    assert response["ok"] and response["command"]["status"] == "executed"
    assert response["command"]["reported_state"] == {"rgb": RGB}
    assert response["command"]["duplicate"] is False


async def test_command_returns_a_failed_outcome():
    gateway, session, server = setup(3)
    worker = asyncio.create_task(device(gateway, session, "failed"))
    response = await command(server)
    await worker
    assert response["command"]["status"] == "failed"
    assert response["command"]["error"]["code"] == "device_failed"


async def test_silent_device_returns_open_status_after_the_bound():
    gateway, _, server = setup(0.2)
    started = time.monotonic()
    response = await command(server)
    elapsed = time.monotonic() - started
    assert response["command"]["status"] == "accepted"
    assert 0.15 <= elapsed < 2
    status = gateway.command_status(response["command"]["command_id"])
    assert status["status"] == "accepted"


async def test_wait_never_exceeds_the_ack_deadline():
    from grok_gadgets_gateway import mcp_server
    from grok_gadgets_gateway.domain import ACK_TIMEOUT_SECONDS

    assert mcp_server.COMMAND_WAIT_SECONDS <= ACK_TIMEOUT_SECONDS
