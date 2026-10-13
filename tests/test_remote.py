"""A tunnel in front of serve: Host, tokens, kill switch and device ports (hub #4)."""

import asyncio
import io
import json

import pytest
from test_http import TOKEN, ForcedHost, first_status, free_port, write_local_config
from test_mcp_tokens import call_tools

from grok_gadgets_gateway import cli
from grok_gadgets_gateway.operator import (
    OperatorError,
    issue_mcp_token,
    remote_enabled,
    revoke_mcp_token,
    set_remote,
)
from grok_gadgets_gateway.service import is_remote, serve_gateway

TUNNEL = "gadgets.example.net"


def test_remote_means_a_tunnel_host_or_a_forwarding_header():
    for host in (b"127.0.0.1:8766", b"localhost", b"[::1]:8766", b"LOCALHOST:1"):
        assert not is_remote({b"host": host})
    for host in (b"gadgets.example.net", b"gadgets.example.net:443", b"", b"10.0.0.2:8766"):
        assert is_remote({b"host": host})
    assert is_remote({b"host": b"127.0.0.1:8766", b"x-forwarded-for": b"203.0.113.9"})
    assert is_remote({b"host": b"localhost", b"forwarded": b"for=203.0.113.9"})


def test_kill_switch_file(tmp_path, capsys, private_home):
    path = tmp_path / "remote-off"
    assert remote_enabled(path)
    set_remote(False, path)
    assert not remote_enabled(path) and (path.stat().st_mode & 0o777) == 0o600
    set_remote(False, path)  # Turning it off twice is fine.
    set_remote(True, path)
    set_remote(True, path)
    assert remote_enabled(path)

    assert cli.main(["remote", "off"]) == 0
    assert "remote: off" in capsys.readouterr().out
    assert cli.main(["remote", "status"]) == 0
    assert "remote: off" in capsys.readouterr().out
    assert cli.main(["remote", "on"]) == 0
    assert "remote: on" in capsys.readouterr().out


async def test_allowed_host_requires_the_request_log(tmp_path):
    credentials, mcp_token = write_local_config(tmp_path)
    with pytest.raises(OperatorError, match="--request-log"):
        async with serve_gateway(
            credentials,
            mcp_token,
            simulator=True,
            port=free_port(),
            device_port=free_port(),
            allowed_hosts=[TUNNEL],
        ):
            pass


async def test_tunnel_requests_need_a_live_named_token_and_the_switch_on(tmp_path):
    credentials, mcp_token = write_local_config(tmp_path)
    tokens = tmp_path / "mcp-tokens.json"
    switch = tmp_path / "remote-off"
    log_path = tmp_path / "requests.jsonl"
    named = issue_mcp_token("grok-bot", tokens)
    tunnel = {"transport": ForcedHost(TUNNEL)}
    async with serve_gateway(
        credentials,
        mcp_token,
        simulator=True,
        port=free_port(),
        device_port=free_port(),
        allowed_hosts=[TUNNEL],
        mcp_tokens=tokens,
        log_stream=io.StringIO(),
        request_log=log_path,
        remote_switch=switch,
    ) as running:
        url = running.url

        async def status(headers=None, transport=None):
            return await first_status(url, headers, transport)

        bearer = {"Authorization": f"Bearer {named}"}
        # Missing and invalid tokens through the tunnel.
        assert await status(None, ForcedHost(TUNNEL)) == [401]
        assert await status({"Authorization": "Bearer wrong-token-value"}, **tunnel) == [401]
        # The local default token never works remotely, by Host or by forwarding header.
        assert await status({"Authorization": f"Bearer {TOKEN}"}, ForcedHost(TUNNEL)) == [403]
        forwarded = {"Authorization": f"Bearer {TOKEN}", "X-Forwarded-For": "203.0.113.9"}
        assert await status(forwarded) == [403]
        # A wrong Host is refused even with a valid named token.
        assert 421 in await status(bearer, ForcedHost("wrong.example.org"))

        # The named token works through the tunnel; the default token still works locally.
        await call_tools(url, named, {"Host": TUNNEL})
        await call_tools(url, TOKEN)

        # Kill switch: remote requests stop at once; loopback keeps working.
        set_remote(False, switch)
        assert await status(bearer, ForcedHost(TUNNEL)) == [503]
        await call_tools(url, TOKEN)
        set_remote(True, switch)
        await call_tools(url, named, {"Host": TUNNEL})

        # A revoked token is refused on its next request.
        revoke_mcp_token("grok-bot", tokens)
        assert await status(bearer, ForcedHost(TUNNEL)) == [401]

    text = log_path.read_text()
    assert named not in text and TOKEN not in text
    lines = [json.loads(line) for line in text.splitlines()]
    calls = [(c["token"], c["remote"]) for c in lines if c["event"] == "mcp_tool_call"]
    assert calls == [("grok-bot", True), ("default", False), ("default", False), ("grok-bot", True)]
    rejected = [(r["status"], r["reason"]) for r in lines if r["event"] == "http_rejected"]
    assert (403, "named_token_required") in rejected
    assert (503, "remote_disabled") in rejected
    assert rejected.count((401, "invalid_token")) == 3


async def test_device_port_refuses_http(tmp_path):
    credentials, mcp_token = write_local_config(tmp_path)
    log_path = tmp_path / "requests.jsonl"
    async with serve_gateway(
        credentials,
        mcp_token,
        port=free_port(),
        device_port=free_port(),
        log_stream=io.StringIO(),
        request_log=log_path,
    ) as running:
        reader, writer = await asyncio.open_connection("127.0.0.1", running.device_port)
        writer.write(b"POST /mcp HTTP/1.1\r\nHost: gadgets.example.net\r\n\r\n")
        await writer.drain()
        assert await asyncio.wait_for(reader.read(), 5) == b""
        writer.close()
    events = [json.loads(line)["event"] for line in log_path.read_text().splitlines()]
    assert "device_http_rejected" in events
