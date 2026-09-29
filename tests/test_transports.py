import asyncio
import json
import os
import select
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from grok_gadgets_gateway.domain import Gateway
from grok_gadgets_gateway.transport import Credentials, DeviceServer
from grok_gadgets_gateway.usb_bridge import bridge
from test_domain import hello

TOKEN = "local-test-device-token-only"


def write_credentials(path, revoked=False):
    path.write_text(json.dumps({"devices": {"dev-1": {"token": TOKEN, "revoked": revoked}}}))
    path.chmod(0o600)


async def exchange(reader, writer, message):
    writer.write((json.dumps(message) + "\n").encode())
    await writer.drain()
    return json.loads(await asyncio.wait_for(reader.readline(), 2))


async def connect(server, token=TOKEN, device_id="dev-1"):
    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    msg = hello(device_id)
    msg["token"] = token
    reply = await exchange(reader, writer, msg)
    return reader, writer, reply


async def test_authenticated_network_command_and_revocation(tmp_path):
    path = tmp_path / "auth.json"
    write_credentials(path)
    g = Gateway()
    server = await DeviceServer(g, Credentials(path), port=0).start()
    try:
        r, w, reply = await connect(server)
        assert reply["ok"] and reply["session_id"]
        args = {"r": 2, "g": 3, "b": 4, "on": True}
        g.command("dev-1", "rgb.set", args, "cmd")
        command = (await exchange(r, w, {"type": "poll"}))["commands"][0]
        assert command["arguments"] == args
        ack = {"type": "ack", "command_id": "cmd", "status": "executed", "state": {"rgb": args}}
        assert (await exchange(r, w, ack))["ok"]
        assert (await exchange(r, w, ack))["duplicate"]
        assert g.command_status("cmd")["status"] == "executed"
        assert (
            await exchange(
                r,
                w,
                {"type": "event", "event_id": "e", "name": "button", "data": {"pressed": True}},
            )
        )["ok"]
        assert g.read_events()["events"][0]["data"]["pressed"]
        write_credentials(path, revoked=True)
        reply = await exchange(r, w, {"type": "ping"})
        assert reply["error"]["code"] == "revoked"
        assert TOKEN not in json.dumps(g.diagnostics())
        w.close()
        await w.wait_closed()
        await asyncio.sleep(0)
        assert not g.state("dev-1")["available"]
    finally:
        await server.close()


async def test_unauthorized_device_isolation_and_reconnect(tmp_path):
    path = tmp_path / "auth.json"
    write_credentials(path)
    g = Gateway()
    server = await DeviceServer(g, Credentials(path), port=0, idle_timeout=0.2).start()
    try:
        r, w, reply = await connect(server, device_id="not-authorized")
        assert reply["error"]["code"] == "unauthorized"
        assert "not-authorized" not in g.devices
        w.close()
        await w.wait_closed()
        r, w, reply = await connect(server, token="bad-token-at-least-16")
        assert reply["error"]["code"] == "unauthorized"
        w.close()
        await w.wait_closed()
        r1, w1, _ = await connect(server)
        r2, w2, _ = await connect(server)
        assert (await exchange(r1, w1, {"type": "ping"}))["error"]["code"] == "stale_session"
        assert (await exchange(r2, w2, {"type": "ping"}))["ok"]
        w1.close()
        await w1.wait_closed()
        assert g.state("dev-1")["available"]
        await asyncio.sleep(0.25)
        assert not g.state("dev-1")["available"]
        w2.close()
        await w2.wait_closed()
    finally:
        await server.close()


async def test_network_frame_bound_and_loopback_guard(tmp_path):
    path = tmp_path / "auth.json"
    write_credentials(path)
    with pytest.raises(ValueError):
        DeviceServer(Gateway(), Credentials(path), host="0.0.0.0")
    server = await DeviceServer(Gateway(), Credentials(path), port=0).start()
    try:
        r, w = await asyncio.open_connection("127.0.0.1", server.port)
        w.write(b"x" * 2049 + b"\n")
        await w.drain()
        reply = json.loads(await r.readline())
        assert reply["error"]["code"] == "invalid_request"
        w.close()
        await w.wait_closed()
    finally:
        await server.close()


async def test_usb_bridge_pty_canonical_transcript(tmp_path):
    path = tmp_path / "auth.json"
    write_credentials(path)
    g = Gateway()
    server = await DeviceServer(g, Credentials(path), port=0).start()
    master, slave = os.openpty()
    slave_path = os.ttyname(slave)
    stop = asyncio.Event()
    task = asyncio.create_task(bridge(slave_path, TOKEN, port=server.port, stop=stop))
    response_path = Path(__file__).parents[1] / "protocol/0.1.0/device-response.schema.json"
    validator = Draft202012Validator(json.loads(response_path.read_text()))

    async def usb(message):
        os.write(master, (json.dumps(message) + "\n").encode())
        value = bytearray()
        while not value.endswith(b"\n"):
            ready = await asyncio.to_thread(select.select, [master], [], [], 2)
            assert ready[0], "bridge response missing"
            value.extend(os.read(master, 1))
        result = json.loads(value)
        validator.validate(result)
        return result

    try:
        # Let pySerial configure the slave and open the TCP socket first.
        await asyncio.sleep(0.05)
        assert (await usb(hello()))["ok"]  # no firmware token; bridge injects credential
        args = {"r": 0, "g": 255, "b": 0, "on": True}
        g.command("dev-1", "rgb.set", args, "usb-cmd")
        assert (await usb({"type": "poll"}))["commands"][0]["arguments"] == args
        assert (
            await usb(
                {
                    "type": "ack",
                    "command_id": "usb-cmd",
                    "status": "executed",
                    "state": {"rgb": args},
                }
            )
        )["ok"]
        assert (
            await usb(
                {"type": "event", "event_id": "edge-1", "name": "button", "data": {"pressed": True}}
            )
        )["ok"]
        assert g.command_status("usb-cmd")["status"] == "executed"
        assert g.read_events()["events"][0]["data"]["pressed"]
    finally:
        stop.set()
        await asyncio.wait_for(task, 2)
        os.close(master)
        os.close(slave)
        await server.close()
