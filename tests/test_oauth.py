"""serve --oauth: owner-approved sign-in through the MCP SDK's own OAuth client (gateway #71)."""

import io
import json
import re
from urllib.parse import parse_qs, urlparse

import httpx2
import pytest
from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import AuthorizationCodeResult, OAuthClientMetadata
from test_http import TOKEN, ForcedHost, free_port, write_local_config

from grok_gadgets_gateway import cli
from grok_gadgets_gateway.oauth import approve, pending_requests, revoke_name, sign_ins
from grok_gadgets_gateway.operator import OperatorError, set_remote
from grok_gadgets_gateway.service import serve_gateway

CODE = re.compile(r"[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}")


class Memory:
    def __init__(self):
        self.tokens = self.client = None

    async def get_tokens(self):
        return self.tokens

    async def set_tokens(self, tokens):
        self.tokens = tokens

    async def get_client_info(self):
        return self.client

    async def set_client_info(self, client_info):
        self.client = client_info


def oauth_server(tmp_path, port, **extra):
    credentials, mcp_token = write_local_config(tmp_path)
    return serve_gateway(
        credentials,
        mcp_token,
        simulator=True,
        port=port,
        device_port=free_port(),
        log_stream=io.StringIO(),
        request_log=tmp_path / "requests.jsonl",
        oauth=True,
        oauth_issuer=f"http://localhost:{port}",
        oauth_directory=tmp_path / "oauth",
        remote_switch=tmp_path / "remote-off",
        **extra,
    )


def signed_in_client(url, folder, decision="grok-bot", seen=None):
    """The SDK OAuth client; its browser step approves (or denies) through the CLI functions."""
    result = {}

    async def browser(authorization_url):
        async with httpx2.AsyncClient(follow_redirects=False) as browser:
            first = await browser.get(authorization_url)
            assert first.status_code == 302
            approve_page = first.headers["location"]
            waiting = await browser.get(approve_page)
            assert waiting.status_code == 200
            assert "oauth approve" in waiting.text and 'http-equiv="refresh"' in waiting.text
            code = CODE.search(waiting.text).group()
            assert [r["code"] for r in pending_requests(folder)] == [code]
            if seen is not None:
                seen.append(waiting)
            approve(code, decision, folder, deny=decision is None)
            done = await browser.get(approve_page)
            assert done.status_code == 302
            query = parse_qs(urlparse(done.headers["location"]).query)
            result.update({k: v[0] for k, v in query.items()})
            # The page works once: a replay finds nothing.
            assert (await browser.get(approve_page)).status_code == 400

    async def callback():
        if "error" in result:
            raise RuntimeError(result["error"])
        return AuthorizationCodeResult(code=result["code"], state=result.get("state"))

    storage = Memory()
    metadata = OAuthClientMetadata(
        client_name="test-bot", redirect_uris=["http://localhost:9/callback"]
    )
    return OAuthClientProvider(url, metadata, storage, browser, callback), storage


async def list_devices(url, auth):
    async with (
        httpx2.AsyncClient(auth=auth, timeout=10) as http,
        streamable_http_client(url, http_client=http) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        result = await session.call_tool("gadgets_list_devices", {})
        assert not result.is_error


async def test_owner_approved_sign_in_refresh_and_revoke(tmp_path):
    port = free_port()
    folder = tmp_path / "oauth"
    async with oauth_server(tmp_path, port) as running:
        url = running.url.replace("127.0.0.1", "localhost")
        async with httpx2.AsyncClient() as plain:
            denied = await plain.post(url, json={})
            assert denied.status_code == 401
            metadata_url = f"http://localhost:{port}/.well-known/oauth-protected-resource/mcp"
            assert f'resource_metadata="{metadata_url}"' in denied.headers["www-authenticate"]
            resource = (await plain.get(metadata_url)).json()
            assert resource["resource"] == url and resource["scopes_supported"] == ["gadgets"]
            server = (
                await plain.get(f"http://localhost:{port}/.well-known/oauth-authorization-server")
            ).json()
            assert server["registration_endpoint"].endswith("/register")
            assert "S256" in server["code_challenge_methods_supported"]
            # Honest about the 2026-07-28 registration change: no Client ID Metadata Documents.
            assert server["client_id_metadata_document_supported"] is False

        pages = []
        auth, storage = signed_in_client(url, folder, seen=pages)
        await list_devices(url, auth)
        page = pages[0]
        assert page.headers["x-frame-options"] == "DENY"
        assert page.headers["cache-control"] == "no-store"

        access = storage.tokens.access_token
        assert access.startswith("gga_") and storage.tokens.refresh_token.startswith("ggr_")
        assert [row[:2] for row in sign_ins(folder)] == [("grok-bot", "test-bot")]
        bearer = {"Authorization": f"Bearer {access}"}
        async with httpx2.AsyncClient() as plain:
            # The access token is an ordinary bearer token for this gateway.
            ok = await plain.post(
                url,
                headers={**bearer, "Accept": "application/json, text/event-stream"},
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            )
            assert ok.status_code != 401

            # Refresh rotates both tokens; the old refresh token stops working.
            form = {
                "grant_type": "refresh_token",
                "refresh_token": storage.tokens.refresh_token,
                "client_id": storage.client.client_id,
                "client_secret": storage.client.client_secret,
            }
            token_url = f"http://localhost:{port}/token"
            fresh = (await plain.post(token_url, data=form)).json()
            assert fresh["access_token"] != access
            assert (await plain.post(token_url, data=form)).status_code == 400
            old = await plain.post(url, headers=bearer, json={})
            assert old.status_code == 401

            # Revoking the name ends every sign-in at once.
            new_bearer = {"Authorization": f"Bearer {fresh['access_token']}"}
            revoke_name("grok-bot", folder)
            assert (await plain.post(url, headers=new_bearer, json={})).status_code == 401
            assert sign_ins(folder) == []

    state = (folder / "state.json").read_text()
    text = (tmp_path / "requests.jsonl").read_text()
    for secret in (access, fresh["access_token"], fresh["refresh_token"]):
        assert secret not in state and secret not in text
    events = [json.loads(line) for line in text.splitlines()]
    calls = [e for e in events if e["event"] == "mcp_tool_call"]
    assert [(c["token"], c["client"].split("/")[0]) for c in calls] == [("grok-bot", "mcp")]
    names = {e["event"] for e in events}
    assert {"oauth_client_registered", "oauth_sign_in_requested", "oauth_sign_in_approved"} <= names
    assert "oauth_token_issued" in names


async def test_a_denied_or_unapproved_sign_in_gets_no_token(tmp_path):
    port = free_port()
    folder = tmp_path / "oauth"
    async with oauth_server(tmp_path, port) as running:
        url = running.url.replace("127.0.0.1", "localhost")
        auth, storage = signed_in_client(url, folder, decision=None)
        with pytest.raises(Exception):  # noqa: B017 - the SDK surfaces the refusal its own way
            await list_devices(url, auth)
        assert storage.tokens is None and sign_ins(folder) == []
        with pytest.raises(OperatorError, match="no pending sign-in"):
            approve("ABCD-EFGH", "grok-bot", folder)
        with pytest.raises(OperatorError, match="not a sign-in code"):
            approve("ABCD-EFG0", "grok-bot", folder)
        with pytest.raises(OperatorError, match="not 'default'"):
            approve("ABCD-EFGH", "default", folder)
        async with httpx2.AsyncClient() as plain:
            unknown = await plain.get(f"http://localhost:{port}/oauth/approve?request=nope")
            assert unknown.status_code == 400


async def test_sign_in_paths_keep_the_host_check_and_kill_switch(tmp_path):
    port = free_port()
    async with oauth_server(tmp_path, port, allowed_hosts=["gadgets.example.net"]):
        well_known = f"http://127.0.0.1:{port}/.well-known/oauth-authorization-server"
        async with httpx2.AsyncClient(transport=ForcedHost("wrong.example.org")) as wrong:
            assert (await wrong.get(well_known)).status_code == 421
        async with httpx2.AsyncClient(transport=ForcedHost("gadgets.example.net")) as tunnel:
            assert (await tunnel.get(well_known)).status_code == 200
            set_remote(False, tmp_path / "remote-off")
            assert (await tunnel.get(well_known)).status_code == 503
            # The default local token still never works remotely.
            denied = await tunnel.post(
                f"http://127.0.0.1:{port}/mcp", headers={"Authorization": f"Bearer {TOKEN}"}
            )
            assert denied.status_code == 503


async def test_oauth_needs_one_tunnel_name_and_the_request_log(tmp_path):
    credentials, mcp_token = write_local_config(tmp_path)
    common = {"simulator": True, "port": free_port(), "device_port": free_port()}
    with pytest.raises(OperatorError, match="exactly one --allowed-host"):
        async with serve_gateway(credentials, mcp_token, oauth=True, **common):
            pass
    with pytest.raises(OperatorError, match="--request-log"):
        async with serve_gateway(
            credentials, mcp_token, oauth=True, allowed_hosts=["gadgets.example.net"], **common
        ):
            pass


def test_oauth_cli(tmp_path, capsys, private_home):
    assert cli.main(["oauth", "pending"]) == 0
    assert "No sign-in is waiting" in capsys.readouterr().err
    assert cli.main(["oauth", "list"]) == 0
    with pytest.raises(SystemExit):
        cli.main(["oauth", "approve", "ABCD-EFGH"])
    assert "no pending sign-in" in capsys.readouterr().err
    assert cli.main(["oauth", "revoke", "grok-bot"]) == 0
    assert "Ended every grok-bot sign-in" in capsys.readouterr().err
