import asyncio
import json

import pytest
from test_http import TOKEN, free_port, write_local_config

from grok_gadgets_gateway.rehearse import RehearsalError, main, rehearse
from grok_gadgets_gateway.service import serve_gateway


@pytest.mark.parametrize(
    ("protocol", "negotiated"), [("legacy", "2025-11-25"), ("modern", "2026-07-28")]
)
async def test_rehearse_calls_the_six_tools_and_sets_the_simulated_light_blue(
    tmp_path, protocol, negotiated
):
    credentials, mcp_token = write_local_config(tmp_path)
    lines = []
    log = tmp_path / "requests.jsonl"
    async with serve_gateway(
        credentials,
        mcp_token,
        simulator=True,
        port=free_port(),
        device_port=free_port(),
        request_log=log,
    ) as running:
        state = await rehearse(running.mcp_port, TOKEN, out=lines.append, protocol=protocol)
    assert state["state"]["rgb"] == {"r": 0, "g": 120, "b": 255, "on": True}
    assert state["simulated"] is True
    assert any(f"Grok Bot will use ({protocol} MCP, {negotiated})" in line for line in lines)
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    calls = [call for call in calls if call["event"] == "mcp_tool_call"]
    assert calls and {call["protocol"] for call in calls} == {negotiated}
    assert any("-> executed" in line for line in lines)


async def test_rehearse_needs_a_gadget_with_rgb(tmp_path):
    credentials, mcp_token = write_local_config(tmp_path)
    async with serve_gateway(
        credentials, mcp_token, port=free_port(), device_port=free_port()
    ) as running:
        with pytest.raises(RehearsalError, match="--simulator"):
            await rehearse(running.mcp_port, TOKEN, out=lambda _line: None)


def test_main_reports_a_missing_gateway_and_fails(capsys):
    port = free_port()
    assert main(port, "unused", None, lambda _path: TOKEN) == 1
    assert (
        f"no authenticated gateway answers legacy MCP on 127.0.0.1:{port}"
        in capsys.readouterr().out
    )


async def test_main_rejects_a_wrong_token(tmp_path, capsys):
    credentials, mcp_token = write_local_config(tmp_path)
    async with serve_gateway(
        credentials, mcp_token, simulator=True, port=free_port(), device_port=free_port()
    ) as running:
        code = await asyncio.to_thread(
            main, running.mcp_port, "unused", None, lambda _path: "wrong-token-value-123"
        )
    assert code == 1
    assert "Rehearsal failed" in capsys.readouterr().out


async def test_rehearse_calls_a_named_command_with_arguments(tmp_path):
    credentials, mcp_token = write_local_config(tmp_path)
    lines = []
    async with serve_gateway(
        credentials, mcp_token, simulator=True, port=free_port(), device_port=free_port()
    ) as running:
        args = {"r": 1, "g": 2, "b": 3, "on": True}
        state = await rehearse(running.mcp_port, TOKEN, "sim-c124", lines.append, "rgb.set", args)
        with pytest.raises(RehearsalError, match="no gadget offers led.set"):
            await rehearse(running.mcp_port, TOKEN, None, lambda _l: None, "led.set", {})
        with pytest.raises(RehearsalError, match="no gadget nope"):
            await rehearse(running.mcp_port, TOKEN, "nope", lambda _l: None)
    assert state["state"]["rgb"] == args


async def test_main_rehearses_both_eras_by_default(tmp_path, capsys):
    credentials, mcp_token = write_local_config(tmp_path)
    async with serve_gateway(
        credentials, mcp_token, simulator=True, port=free_port(), device_port=free_port()
    ) as running:
        code = await asyncio.to_thread(main, running.mcp_port, "unused", None, lambda _p: TOKEN)
    out = capsys.readouterr().out
    assert code == 0
    assert "(legacy MCP, 2025-11-25)" in out and "(modern MCP, 2026-07-28)" in out
    assert "Rehearsal passed (legacy, modern MCP)" in out


async def test_rehearse_refuses_an_unknown_era():
    with pytest.raises(ValueError, match="protocol"):
        await rehearse(1, TOKEN, protocol="2024")
