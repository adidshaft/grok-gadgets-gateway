from grok_gadgets_gateway.demo import acceptance


async def test_real_official_mcp_client_simulator_acceptance():
    await acceptance()


async def test_simulation_controls_absent_by_default():
    await acceptance(False)


async def test_real_mcp_event_windows_with_two_authenticated_devices(tmp_path):
    import asyncio
    import json
    import socket
    import sys

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    from test_domain import event, hello
    from test_transports import TOKEN, exchange

    path = tmp_path / "credentials.json"
    path.write_text(json.dumps({"devices": {did: {"token": TOKEN} for did in ("a", "b")}}))
    path.chmod(0o600)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "grok_gadgets_gateway.cli",
            "--credentials",
            str(path),
            "--device-port",
            str(port),
        ],
    )
    writers = []
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()

            async def call(name, arguments=None):
                result = await client.call_tool(name, arguments or {})
                assert not result.isError, result.content
                return result.structuredContent or json.loads(result.content[0].text)

            async def connect(did, boot="boot-1"):
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writers.append(writer)
                registration = hello(did)
                registration["token"] = TOKEN
                registration["device"]["boot_id"] = boot
                assert (await exchange(reader, writer, registration))["ok"]
                return reader, writer

            try:
                a, aw = await connect("a")
                b, bw = await connect("b")
                assert (await exchange(a, aw, event())) == {"ok": True}
                assert (await exchange(b, bw, event())) == {"ok": True}
                for index in range(256):
                    assert (await exchange(b, bw, event(f"b-{index}"))) == {"ok": True}
                before = await call("gadgets_read_events", {"limit": 128})
                assert before["history_lost"] and len(before["events"]) == 128
                assert before["events"][-1]["sequence"] == 258
                assert (await exchange(a, aw, event())) == {"ok": True, "duplicate": True}
                assert (await call("gadgets_read_events", {"cursor": before["next_cursor"]}))[
                    "events"
                ] == []
                conflict = await exchange(a, aw, event(pressed=False))
                assert conflict["error"]["code"] == "duplicate_conflict"
                a2, aw2 = await connect("a")
                assert (await exchange(a, aw, event("stale")))["error"]["code"] == "stale_session"
                assert (await exchange(a2, aw2, event()))["duplicate"]
                a3, aw3 = await connect("a", "reboot")
                assert (await exchange(a3, aw3, event())) == {"ok": True}
                after = await call("gadgets_read_events", {"cursor": before["next_cursor"]})
                assert len(after["events"]) == 1
                assert after["events"][0]["device_id"] == "a"
                assert after["events"][0]["boot_id"] == "reboot"
                assert after["events"][0]["sequence"] == 259
            finally:
                for writer in writers:
                    writer.close()
                await asyncio.gather(*(writer.wait_closed() for writer in writers))
