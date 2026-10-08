import io
import json
import stat

import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from test_http import DEVICE, TOKEN, free_port, write_local_config

from grok_gadgets_gateway.mcp_server import REDACTED, redact
from grok_gadgets_gateway.operator import OperatorError
from grok_gadgets_gateway.rehearse import CLIENT_NAME, rehearse
from grok_gadgets_gateway.service import serve_gateway

SECRET = "s3cr3t-argument-value"


def read_lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


async def test_request_log_records_who_called_what_without_secrets(tmp_path):
    credentials, mcp_token = write_local_config(tmp_path)
    log_path = tmp_path / "requests.jsonl"
    stderr = io.StringIO()
    async with serve_gateway(
        credentials,
        mcp_token,
        simulator=True,
        port=free_port(),
        device_port=free_port(),
        log_stream=stderr,
        request_log=log_path,
    ) as running:
        await rehearse(running.mcp_port, TOKEN, out=lambda _line: None)
        headers = {"Authorization": f"Bearer {TOKEN}"}
        async with (
            httpx2.AsyncClient(headers=headers) as http,
            streamable_http_client(running.url, http_client=http) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            await session.call_tool(
                "gadgets_command",
                {
                    "device_id": "sim-c124",
                    "capability": "rgb.set",
                    "arguments": {"r": 0, "g": 0, "b": 9, "on": True, "api_token": SECRET},
                },
            )

    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600
    text = log_path.read_text()
    assert TOKEN not in text and DEVICE not in text and SECRET not in text
    lines = read_lines(log_path)
    assert lines[0]["event"] == "serve_started"
    assert lines[0]["url"] == running.url and lines[0]["simulator"] == "sim-c124"
    assert lines[-1]["event"] == "serve_stopped"

    calls = [line for line in lines if line["event"] == "mcp_tool_call"]
    rehearsal = [call for call in calls if call["client"].startswith(CLIENT_NAME + "/")]
    tools = [call["tool"] for call in rehearsal]
    assert tools == ["gadgets_list_devices", "gadgets_command", "gadgets_get_state"]
    listed, command, state = rehearsal
    assert listed["devices"] == ["sim-c124"]
    assert command["device_id"] == "sim-c124" and command["capability"] == "rgb.set"
    assert command["arguments"] == {"r": 0, "g": 120, "b": 255, "on": True}
    assert command["status"] == "executed" and command["simulated"] is True
    assert state["state"]["rgb"] == {"r": 0, "g": 120, "b": 255, "on": True}
    assert state["freshness"] == "fresh" and state["simulated"] is True
    assert all(call["ts"] and call["user_agent"] for call in rehearsal)

    rejected = calls[-1]
    assert not rejected["client"].startswith(CLIENT_NAME)
    assert rejected["outcome"] == "invalid_arguments"
    assert rejected["arguments"]["api_token"] == REDACTED and rejected["arguments"]["b"] == 9

    # The stderr log keeps its privacy contract: no arguments, state or file-only detail.
    assert '"arguments"' not in stderr.getvalue() and '"state"' not in stderr.getvalue()
    assert '"tool":"gadgets_command"' in stderr.getvalue()


async def test_request_log_appends_across_runs(tmp_path):
    credentials, mcp_token = write_local_config(tmp_path)
    log_path = tmp_path / "requests.jsonl"
    for _ in range(2):
        async with serve_gateway(
            credentials,
            mcp_token,
            port=free_port(),
            device_port=free_port(),
            log_stream=io.StringIO(),
            request_log=log_path,
        ):
            pass
    events = [line["event"] for line in read_lines(log_path)]
    assert events == ["serve_started", "serve_stopped"] * 2


async def test_request_log_is_off_by_default_and_rejects_an_unwritable_path(tmp_path):
    credentials, mcp_token = write_local_config(tmp_path)
    with pytest.raises(OperatorError, match="cannot open request log"):
        async with serve_gateway(
            credentials,
            mcp_token,
            port=free_port(),
            device_port=free_port(),
            request_log=tmp_path / "missing" / "requests.jsonl",
        ):
            pass
    async with serve_gateway(
        credentials, mcp_token, port=free_port(), device_port=free_port(), log_stream=io.StringIO()
    ):
        pass
    assert sorted(path.name for path in tmp_path.iterdir()) == ["credentials.json", "mcp-token"]


def test_redact_hides_write_only_properties_and_secret_looking_keys():
    contract = {
        "type": "object",
        "properties": {"pin": {"type": "string", "writeOnly": True}, "level": {"type": "integer"}},
    }
    value = {"pin": "1234", "level": 3, "nested": {"password": "x", "ok": [1, {"secret": 2}]}}
    assert redact(value, contract) == {
        "pin": REDACTED,
        "level": 3,
        "nested": {"password": REDACTED, "ok": [1, {"secret": REDACTED}]},
    }
