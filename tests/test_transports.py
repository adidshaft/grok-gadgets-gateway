import asyncio
import json
import os
import select
import socket
import struct
from pathlib import Path
from unittest.mock import Mock

import pytest
from jsonschema import Draft202012Validator

from grok_gadgets_gateway.domain import Gateway
from grok_gadgets_gateway.simulator import Simulator
from grok_gadgets_gateway.transport import CredentialError, Credentials, DeviceServer
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


async def test_close_returns_while_device_polls(tmp_path):
    path = tmp_path / "auth.json"
    write_credentials(path)
    g = Gateway()
    server = await DeviceServer(g, Credentials(path), port=0).start()
    r, w, reply = await connect(server)
    assert reply["ok"]

    async def poll_forever():
        while True:
            w.write(b'{"type":"poll"}\n')
            await w.drain()
            if not await r.readline():
                return "eof"
            await asyncio.sleep(0.05)

    poller = asyncio.create_task(poll_forever())
    await asyncio.sleep(0.2)
    try:
        async with asyncio.timeout(3):
            await server.close()
        assert await asyncio.wait_for(poller, 2) == "eof"
        assert not server.connections and not server.writers
        assert not g.state("dev-1")["available"]
    finally:
        poller.cancel()
        w.close()


async def test_simulator_device_id_is_reserved(tmp_path):
    path = tmp_path / "auth.json"
    path.write_text(json.dumps({"devices": {"sim-c124": {"token": TOKEN}}}))
    path.chmod(0o600)
    g = Gateway()
    simulator = Simulator(g)
    server = await DeviceServer(
        g, Credentials(path), port=0, reserved_ids=[simulator.device_id]
    ).start()
    try:
        _, w, reply = await connect(server, device_id="sim-c124")
        assert reply["error"]["code"] == "unauthorized"
        assert g.state("sim-c124")["simulated"] and g.state("sim-c124")["available"]
        w.close()
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


async def test_peer_reset_does_not_escape_server_callback(tmp_path):
    path = tmp_path / "auth.json"
    write_credentials(path)
    gateway = Gateway()
    server = await DeviceServer(gateway, Credentials(path), port=0).start()
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    unhandled = []
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    writer = None
    try:
        _, writer, reply = await connect(server)
        assert reply["ok"]
        gateway.command("dev-1", "rgb.set", {"r": 1, "g": 2, "b": 3, "on": True}, "pending")
        # An abortive TCP close reproduces a child agent disappearing with a reset.
        writer.get_extra_info("socket").setsockopt(
            socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
        )
        writer.close()
        await writer.wait_closed()
        async with asyncio.timeout(2):
            while any(not task.done() for task in server.connections):
                await asyncio.sleep(0)
        await asyncio.sleep(0)  # Deliver the stream protocol's task-done callback.
        assert not unhandled, unhandled
        assert not server.connections
        assert not server.writers
        assert not gateway.state("dev-1")["available"]
        assert gateway.command_status("pending")["status"] == "not_delivered"
    finally:
        if writer is not None:
            writer.close()
        await server.close()
        loop.set_exception_handler(previous_handler)


class CleanupWriter:
    def __init__(self, error):
        self.error = error
        self.closed = False
        self.data = bytearray()

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        raise self.error


async def test_cancelled_cleanup_propagates_and_removes_connection(tmp_path):
    reader = asyncio.StreamReader()
    reader.feed_eof()
    writer = CleanupWriter(asyncio.CancelledError())
    server = DeviceServer(Gateway(), Credentials(tmp_path / "unused.json"), port=0)
    with pytest.raises(asyncio.CancelledError):
        await server.client(reader, writer)
    assert writer.closed
    assert not server.connections
    assert not server.writers


@pytest.mark.parametrize("cleanup_error", [ConnectionResetError, BrokenPipeError])
async def test_unexpected_error_replies_once_and_cleanup_reset_is_quiet(
    tmp_path, monkeypatch, cleanup_error
):
    path = tmp_path / "auth.json"
    write_credentials(path)
    gateway = Gateway()
    monkeypatch.setattr(gateway, "handle", Mock(side_effect=RuntimeError("command failure")))
    server = DeviceServer(gateway, Credentials(path), port=0)
    greeting = {**hello(), "token": TOKEN}
    reader = asyncio.StreamReader()
    reader.feed_data((json.dumps(greeting) + '\n{"type":"ping"}\n').encode())
    reader.feed_eof()
    writer = CleanupWriter(cleanup_error("peer closed"))
    await server.client(reader, writer)
    replies = [json.loads(line) for line in writer.data.splitlines()]
    assert replies[0]["ok"] and replies[-1]["error"]["code"] == "internal_error"
    assert "command failure" not in writer.data.decode()
    assert writer.closed
    assert not server.connections
    assert not server.writers
    assert not gateway.state("dev-1")["available"]


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
        old_session = g.state("dev-1")["session_id"]
        assert (await usb(hello()))["ok"]
        assert g.state("dev-1")["session_id"] != old_session
        old_port = server.port
        await server.close()
        assert (await usb({"type": "ping"}))["error"]["code"] == "gateway_unavailable"
        assert (await usb({"type": "poll"}))["error"]["code"] == "stale_session"
        server = await DeviceServer(g, Credentials(path), port=old_port).start()
        assert (await usb(hello()))["ok"]
        assert (await usb({"type": "ping"}))["ok"]
    finally:
        stop.set()
        await asyncio.wait_for(task, 2)
        os.close(master)
        os.close(slave)
        await server.close()


async def _pty_bridge(tmp_path):
    path = tmp_path / "auth.json"
    write_credentials(path)
    gateway = Gateway()
    server = await DeviceServer(gateway, Credentials(path), port=0).start()
    master, slave = os.openpty()
    stop = asyncio.Event()
    task = asyncio.create_task(bridge(os.ttyname(slave), TOKEN, port=server.port, stop=stop))
    await asyncio.sleep(0.05)

    async def read_frame():
        value = bytearray()
        while not value.endswith(b"\n"):
            ready = await asyncio.to_thread(select.select, [master], [], [], 2)
            assert ready[0], "bridge response missing"
            value.extend(os.read(master, 4096))
        line, _, rest = bytes(value).partition(b"\n")
        assert rest == b""
        return json.loads(line)

    async def finish():
        stop.set()
        await asyncio.wait_for(task, 2)
        os.close(master)
        os.close(slave)
        await server.close()

    return gateway, server, master, read_frame, finish


async def test_usb_bridge_discards_oversize_frame_then_accepts_hello(tmp_path):
    _gateway, server, master, read_frame, finish = await _pty_bridge(tmp_path)
    try:
        await asyncio.to_thread(os.write, master, b"{" + (b"x" * 3000) + b"\n")
        assert (await read_frame())["error"]["code"] == "invalid_request"
        os.write(master, (json.dumps(hello()) + "\n").encode())
        assert (await read_frame())["ok"]
        assert server.port
    finally:
        await finish()


async def test_usb_bridge_stays_silent_on_log_lines_and_rejects_non_objects(tmp_path):
    _gateway, _server, master, read_frame, finish = await _pty_bridge(tmp_path)
    try:
        os.write(master, b"firmware boot log\n\n")
        os.write(master, (json.dumps(hello()) + "\n").encode())
        assert (await read_frame())["ok"]
        os.write(master, b"[]\n")
        assert (await read_frame())["error"]["code"] == "invalid_request"
        os.write(master, (json.dumps({"type": "ping"}) + "\n").encode())
        assert (await read_frame())["ok"]
    finally:
        await finish()


async def test_usb_bridge_replies_once_when_json_recursion_fails(tmp_path):
    _gateway, _server, master, read_frame, finish = await _pty_bridge(tmp_path)
    nested = b"[" * 1000 + b"1" + b"]" * 1000 + b"\n"
    assert len(nested) <= 2048
    try:
        await asyncio.to_thread(os.write, master, nested)
        assert (await read_frame())["error"] == {
            "code": "invalid_request",
            "message": "Malformed USB JSON",
        }
        os.write(master, (json.dumps(hello()) + "\n").encode())
        assert (await read_frame())["ok"]
    finally:
        await finish()


async def test_usb_bridge_reopens_serial_after_serial_exception(tmp_path, monkeypatch):
    import serial

    from grok_gadgets_gateway import usb_bridge

    real = serial.Serial

    class Flaky(real):
        opens = 0

        def __init__(self, *args, **kwargs):
            Flaky.opens += 1
            super().__init__(*args, **kwargs)
            self.fail_read = Flaky.opens == 1

        def read(self, size=1):
            if self.fail_read:
                self.fail_read = False
                raise serial.SerialException("unplugged")
            return super().read(size)

    monkeypatch.setattr(usb_bridge.serial, "Serial", Flaky)
    _gateway, _server, master, read_frame, finish = await _pty_bridge(tmp_path)
    try:
        for _ in range(50):
            if Flaky.opens >= 2:
                break
            await asyncio.sleep(0.02)
        assert Flaky.opens >= 2
        os.write(master, (json.dumps(hello()) + "\n").encode())
        assert (await read_frame())["ok"]
    finally:
        await finish()


def write_registry(path, text):
    path.write_text(text)
    path.chmod(0o600)


@pytest.mark.parametrize(
    "text",
    [
        '{"devices": []}',
        '{"devices": null}',
        "[]",
        '{"devices": {"dev-1": "token"}}',
        '{"devices": {"dev-1": {"token": 5}}}',
        '{"devices": {"dev-1": {"token": "short"}}}',
        f'{{"devices": {{"dev-1": {{"token": "{TOKEN}", "revoked": "false"}}}}}}',
        f'{{"devices": {{"dev-1": {{"token": "{TOKEN}", "revoked": true}}, '
        f'"dev-1": {{"token": "{TOKEN}"}}}}}}',
        '{"devices": {"bad\\n": {"token": "' + TOKEN + '"}}}',
        "[" * 5000 + "]" * 5000,
    ],
    ids=lambda text: text[:40],
)
async def test_malformed_registry_is_rejected_with_reply(tmp_path, text):
    path = tmp_path / "auth.json"
    write_registry(path, text)
    with pytest.raises(CredentialError):
        Credentials(path).validate()
    server = await DeviceServer(Gateway(), Credentials(path), port=0).start()
    try:
        _, w, reply = await connect(server)
        assert reply["error"] == {
            "code": "unavailable",
            "message": "Credential configuration unavailable",
        }
        assert TOKEN not in json.dumps(reply)
        w.close()
    finally:
        await server.close()


async def test_registry_glitch_keeps_last_good_then_reports_unavailable(tmp_path):
    path = tmp_path / "auth.json"
    write_credentials(path)
    clock = [0]
    credentials = Credentials(path, grace=30, clock=lambda: clock[0])
    g = Gateway()
    server = await DeviceServer(g, credentials, port=0).start()
    try:
        r, w, reply = await connect(server)
        assert reply["ok"]
        path.write_text("")  # A truncating, non-atomic editor save.
        assert (await exchange(r, w, {"type": "ping"}))["ok"]
        clock[0] = 31
        assert (await exchange(r, w, {"type": "ping"}))["error"]["code"] == "unavailable"
        assert credentials.status == "invalid"
        write_credentials(path)
        assert (await exchange(r, w, {"type": "ping"}))["ok"]
        assert g.state("dev-1")["available"]
        path.chmod(0o644)
        assert (await exchange(r, w, {"type": "ping"}))["error"]["code"] == "unavailable"
        w.close()
    finally:
        await server.close()


async def test_pre_auth_failures_are_indistinguishable(tmp_path):
    path = tmp_path / "auth.json"
    write_credentials(path)
    server = await DeviceServer(Gateway(), Credentials(path), port=0).start()
    try:
        replies = []
        for device_id in ("dev-1", "not-enrolled"):
            _, w, reply = await connect(server, token="é" * 16, device_id=device_id)
            replies.append(reply)
            w.close()
        assert replies[0] == replies[1]
        assert replies[0]["error"]["code"] == "unauthorized"
    finally:
        await server.close()


async def test_pre_auth_deep_json_gets_reply_and_hello_retry_works(tmp_path):
    path = tmp_path / "auth.json"
    write_credentials(path)
    g = Gateway()
    server = await DeviceServer(g, Credentials(path), port=0).start()
    try:
        r, w = await asyncio.open_connection("127.0.0.1", server.port)
        w.write(b"[" * 1000 + b"]" * 1000 + b"\n")
        await w.drain()
        assert json.loads(await r.readline())["error"]["code"] == "invalid_request"
        bad = {**hello(), "token": TOKEN}
        bad["device"] = {**bad["device"], "capabilities": ["rgb.set", "relay.set"]}
        bad["device"]["capability_schemas"] = {"relay.set": {"$ref": "https://invalid.test"}}
        assert (await exchange(r, w, bad))["error"]["code"] == "invalid_request"
        assert (await exchange(r, w, {**hello(), "token": TOKEN}))["ok"]
        assert (await exchange(r, w, {"type": "ping"}))["ok"]
        w.close()
    finally:
        await server.close()


async def test_idle_unauthenticated_sockets_cannot_lock_out_devices(tmp_path):
    path = tmp_path / "auth.json"
    write_credentials(path)
    server = await DeviceServer(Gateway(), Credentials(path), port=0, hello_timeout=0.5).start()
    hogs = []
    try:
        for _ in range(40):
            hogs.append((await asyncio.open_connection("127.0.0.1", server.port))[1])
        await asyncio.sleep(0.05)
        assert len(server.pending) <= server.max_pending
        _, w, reply = await connect(server)
        assert reply["ok"]
        await asyncio.sleep(0.7)
        assert not server.pending  # Silent sockets hit the short hello deadline.
        w.close()
    finally:
        for hog in hogs:
            hog.close()
        await server.close()


class StuckWriter(CleanupWriter):
    """A peer that stopped reading: drain never completes."""

    async def drain(self):
        await asyncio.Event().wait()

    async def wait_closed(self):
        pass


async def test_peer_that_stops_reading_is_disconnected(tmp_path):
    path = tmp_path / "auth.json"
    write_credentials(path)
    g = Gateway()
    server = DeviceServer(g, Credentials(path), port=0, idle_timeout=0.3)
    reader = asyncio.StreamReader()
    reader.feed_data((json.dumps({**hello(), "token": TOKEN}) + "\n").encode())
    writer = StuckWriter(None)
    async with asyncio.timeout(2):
        await server.client(reader, writer)
    assert writer.closed and not server.connections
    assert not g.state("dev-1")["available"]


async def test_rotation_rejects_the_old_token_and_keeps_receipts(tmp_path):
    from grok_gadgets_gateway.operator import enroll, revoke

    path = tmp_path / "auth.json"
    write_credentials(path)
    gateway = Gateway()
    server = await DeviceServer(gateway, Credentials(path), port=0).start()
    try:
        reader, writer, reply = await connect(server)
        assert reply["ok"]
        gateway.command("dev-1", "rgb.set", {"r": 1, "g": 2, "b": 3, "on": True}, "kept")
        new = enroll("dev-1", path, rotate=True)
        # The open session's next request carries the old token and is refused.
        refused = await exchange(reader, writer, {"type": "poll"})
        assert refused["error"]["code"] == "unauthorized"
        writer.close()
        await writer.wait_closed()
        old = await connect(server)
        assert old[2]["error"]["code"] == "unauthorized"
        old[1].close()
        fresh = await connect(server, token=new)
        assert fresh[2]["ok"]
        # The receipt survives rotation: retrying the same ID never runs a new action.
        retry = gateway.command("dev-1", "rgb.set", {"r": 1, "g": 2, "b": 3, "on": True}, "kept")
        assert retry["duplicate"] is True
        fresh[1].close()
        # Rotating a revoked device is an explicit operator action that reactivates it.
        revoke("dev-1", path)
        again = enroll("dev-1", path, rotate=True)
        reactivated = await connect(server, token=again)
        assert reactivated[2]["ok"]
        reactivated[1].close()
    finally:
        await server.close()
