import asyncio
import io
import json
import os
import stat
import sys

import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from test_http import TOKEN, first_status, free_port, write_local_config

from grok_gadgets_gateway.operator import (
    OperatorError,
    issue_mcp_token,
    list_mcp_tokens,
    read_named_tokens,
    revoke_mcp_token,
    rotate_mcp_token,
)
from grok_gadgets_gateway.service import serve_gateway
from grok_gadgets_gateway.transport import CredentialError


def test_named_tokens_store_only_hashes_and_can_be_reissued_after_revoke(tmp_path):
    path = tmp_path / "mcp-tokens.json"
    token = issue_mcp_token("grok-bot", path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert token not in path.read_text()
    assert list_mcp_tokens(path)[0][0::2] == ("grok-bot", False)

    with pytest.raises(OperatorError, match="already active"):
        issue_mcp_token("grok-bot", path)
    revoke_mcp_token("grok-bot", path)
    assert list_mcp_tokens(path)[0][2] is True
    replacement = issue_mcp_token("grok-bot", path)
    assert replacement != token and list_mcp_tokens(path)[0][2] is False

    for bad in ("default", "", "-x", "a b", "x" * 65):
        with pytest.raises(OperatorError):
            issue_mcp_token(bad, path)
    with pytest.raises(OperatorError, match="not a named"):
        revoke_mcp_token("unknown", path)


def test_named_token_list_must_be_private_and_well_formed(tmp_path):
    path = tmp_path / "mcp-tokens.json"
    assert read_named_tokens(path) == {}
    issue_mcp_token("one", path)
    path.chmod(0o644)
    with pytest.raises(CredentialError):
        read_named_tokens(path)
    path.write_text('{"tokens": {"one": {"sha256": "short", "revoked": false}}}')
    path.chmod(0o600)
    with pytest.raises(CredentialError):
        read_named_tokens(path)


async def call_tools(url, token, headers=None):
    async with (
        httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}", **(headers or {})}) as h,
        streamable_http_client(url, http_client=h) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        await session.call_tool("gadgets_list_devices", {})


async def test_missing_invalid_revoked_and_rotated_tokens_are_rejected_without_restart(tmp_path):
    credentials, mcp_token = write_local_config(tmp_path)
    tokens = tmp_path / "mcp-tokens.json"
    log_path = tmp_path / "requests.jsonl"
    async with serve_gateway(
        credentials,
        mcp_token,
        simulator=True,
        port=free_port(),
        device_port=free_port(),
        mcp_tokens=tokens,
        log_stream=io.StringIO(),
        request_log=log_path,
    ) as running:
        url = running.url
        # Missing, malformed and unknown credentials.
        assert await first_status(url) == [401]
        assert await first_status(url, {"Authorization": "Basic abc"}) == [401]
        assert await first_status(url, {"Authorization": "Bearer"}) == [401]
        assert await first_status(url, {"Authorization": "Bearer not-a-real-token-1"}) == [401]

        # A token issued while serve runs works at once; the default token keeps working.
        named = issue_mcp_token("grok-bot", tokens)
        await call_tools(url, named)
        await call_tools(url, TOKEN, {"X-Grok-Gadgets-Token": "grok-bot"})

        # Revocation applies to the next request, including on an open session.
        async with (
            httpx2.AsyncClient(headers={"Authorization": f"Bearer {named}"}) as h,
            streamable_http_client(url, http_client=h) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            await session.call_tool("gadgets_list_devices", {})
            revoke_mcp_token("grok-bot", tokens)
            with pytest.raises(Exception):  # noqa: B017 - the client surfaces the 401 its own way
                await asyncio.wait_for(session.call_tool("gadgets_list_devices", {}), 5)
        assert await first_status(url, {"Authorization": f"Bearer {named}"}) == [401]

        # A damaged list grants nothing but does not lock out the default token.
        tokens.write_text("{")
        assert await first_status(url, {"Authorization": f"Bearer {named}"}) == [401]
        await call_tools(url, TOKEN)

        rotated = rotate_mcp_token(mcp_token)
        assert await first_status(url, {"Authorization": f"Bearer {TOKEN}"}) == [401]
        await call_tools(url, rotated)

    text = log_path.read_text()
    assert named not in text and TOKEN not in text and rotated not in text
    calls = [json.loads(line) for line in text.splitlines() if '"mcp_tool_call"' in line]
    # The token name comes from the gateway, never from a client-sent header.
    assert [call["token"] for call in calls] == [
        "grok-bot",
        "default",
        "grok-bot",
        "default",
        "default",
    ]


async def test_serve_refuses_an_insecure_token_list(tmp_path):
    credentials, mcp_token = write_local_config(tmp_path)
    tokens = tmp_path / "mcp-tokens.json"
    issue_mcp_token("grok-bot", tokens)
    tokens.chmod(0o644)
    with pytest.raises(OperatorError, match="0600"):
        async with serve_gateway(
            credentials, mcp_token, port=free_port(), device_port=free_port(), mcp_tokens=tokens
        ):
            pass


async def test_cli_issues_lists_and_revokes_named_tokens(tmp_path):
    env = {**os.environ, "XDG_CONFIG_HOME": str(tmp_path)}

    async def cli(*args, ok=True):
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "grok_gadgets_gateway.cli",
            "mcp-token",
            *args,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
        assert (process.returncode == 0) is ok, stderr.decode()
        return stdout.decode()

    printed = await cli("issue", "grok-bot")
    assert printed.startswith("GROK_GADGETS_MCP_TOKEN=")
    token_file = tmp_path / "laptop.token"
    assert await cli("issue", "laptop", "--token-file", str(token_file)) == ""
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    listing = await cli("list")
    assert "grok-bot" in listing and "laptop" in listing and "active" in listing
    assert printed.split("=", 1)[1].strip() not in listing
    await cli("revoke", "grok-bot")
    assert "grok-bot\t" in await cli("list") and "revoked" in await cli("list")
    await cli("issue", "default", ok=False)
