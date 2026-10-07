import asyncio

import pytest

from grok_gadgets_gateway.rehearse import RehearsalError, main, rehearse
from grok_gadgets_gateway.service import serve_gateway

from test_http import TOKEN, free_port, write_local_config


async def test_rehearse_calls_the_six_tools_and_sets_the_simulated_light_blue(tmp_path):
    credentials, mcp_token = write_local_config(tmp_path)
    lines = []
    async with serve_gateway(
        credentials, mcp_token, simulator=True, port=free_port(), device_port=free_port()
    ) as running:
        state = await rehearse(running.mcp_port, TOKEN, out=lines.append)
    assert state["state"]["rgb"] == {"r": 0, "g": 120, "b": 255, "on": True}
    assert state["simulated"] is True
    assert any("six tools Grok Bot will use" in line for line in lines)
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
    assert f"no authenticated gateway answers on 127.0.0.1:{port}" in capsys.readouterr().out


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
