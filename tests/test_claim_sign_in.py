"""GW-CLAIM-001 (hub ADR 011): sign in to a relay in accounts mode with the code a gadget shows."""

import asyncio
import io
import json
import re
from urllib.parse import parse_qs, urlparse

import httpx2
import pytest
from mcp.client.auth import OAuthClientProvider
from mcp.shared.auth import AuthorizationCodeResult, OAuthClientMetadata
from test_domain import hello
from test_http import DEVICE, TOKEN, free_port, write_local_config
from test_oauth import CODE, Memory, list_devices, signed_in_client

from grok_gadgets_gateway.accounts import Accounts
from grok_gadgets_gateway.oauth import sign_ins
from grok_gadgets_gateway.operator import OperatorError
from grok_gadgets_gateway.service import serve_gateway

RECOVERY = re.compile(r"(?:[A-HJ-NP-Z2-9]{4}-){3}[A-HJ-NP-Z2-9]{4}")
REQUEST = re.compile(r'name="request" value="([^"]+)"')


def accounts_server(tmp_path, port, device_port, *, migrated=True, **extra):
    """A relay in accounts mode. `migrated`: dev-1 was enrolled after the migration, so it
    is unclaimed; without it the first start gives dev-1 to the operator account."""
    credentials, mcp_token = write_local_config(tmp_path)
    store = tmp_path / "oauth" / "accounts.json"
    if migrated and not store.exists():
        Accounts(store).open([])
    return serve_gateway(
        credentials,
        mcp_token,
        port=port,
        device_port=device_port,
        log_stream=io.StringIO(),
        request_log=tmp_path / "requests.jsonl",
        oauth=True,
        oauth_issuer=f"http://localhost:{port}",
        oauth_directory=tmp_path / "oauth",
        accounts=True,
        **extra,
    )


async def device(device_port, claim="ready"):
    reader, writer = await asyncio.open_connection("127.0.0.1", device_port)
    message = hello("dev-1")
    message["token"] = DEVICE
    if claim is not None:
        message["claim"] = claim

    async def exchange(value):
        writer.write((json.dumps(value) + "\n").encode())
        await writer.drain()
        return json.loads(await asyncio.wait_for(reader.readline(), 3))

    return writer, await exchange(message), exchange


def code_client(url, codes, pages=None):
    """The SDK OAuth client; its browser types each code into the relay's sign-in page."""
    result = {}

    async def browser(authorization_url):
        async with httpx2.AsyncClient(follow_redirects=False) as web:
            first = await web.get(authorization_url)
            assert first.status_code == 302
            page_url = first.headers["location"]
            page = await web.get(page_url)
            assert page.status_code == 200 and "code on your gadget" in page.text
            assert 'http-equiv="refresh"' not in page.text
            request_id = REQUEST.search(page.text).group(1)
            for code in codes:
                page = await web.post(
                    page_url.split("?")[0], data={"request": request_id, "code": code}
                )
                if pages is not None:
                    pages.append(page)
            # A right code hands straight back to Grok Bot, as the operator approval does:
            # a phone's sign-in sheet closes only on that redirect (17 October 2026).
            assert page.status_code == 302, page.text
            query = parse_qs(urlparse(page.headers["location"]).query)
            result.update({k: v[0] for k, v in query.items()})

    async def callback():
        return AuthorizationCodeResult(code=result["code"], state=result.get("state"))

    storage = Memory()
    metadata = OAuthClientMetadata(
        client_name="test-bot", redirect_uris=["http://localhost:9/callback"]
    )
    return OAuthClientProvider(url, metadata, storage, browser, callback), storage


def state(tmp_path):
    return json.loads((tmp_path / "oauth" / "state.json").read_text())


def accounts_file(tmp_path):
    return json.loads((tmp_path / "oauth" / "accounts.json").read_text())


async def test_a_person_signs_in_with_the_code_their_gadget_shows(tmp_path):
    port, device_port = free_port(), free_port()
    async with accounts_server(tmp_path, port, device_port) as running:
        url = running.url.replace("127.0.0.1", "localhost")
        writer, reply, exchange = await device(device_port)
        assert reply["ok"] and reply["claimed"] is False and CODE.fullmatch(reply["claim_code"])

        pages = []
        auth, storage = code_client(url, ["AAAA-AAAA", reply["claim_code"]], pages)
        await list_devices(url, auth)
        wrong, done = pages
        assert "That code did not work." in wrong.text
        assert "AAAA-AAAA" not in wrong.text
        assert done.status_code == 302 and "code=" in done.headers["location"]

        # The gadget learns it is claimed in its next poll reply.
        assert (await exchange({"type": "poll"})) == {"ok": True, "commands": [], "claimed": True}
        writer.close()

    owner = accounts_file(tmp_path)["devices"]["dev-1"]["owner"]
    assert owner != accounts_file(tmp_path)["operator"]
    records = state(tmp_path)
    assert {r["account"] for r in records["refresh"].values()} == {owner}
    assert {r["name"] for r in records["access"].values()} == {owner}
    log = (tmp_path / "requests.jsonl").read_text()
    text = log + json.dumps(records) + json.dumps(accounts_file(tmp_path))
    for secret in (reply["claim_code"], storage.tokens.access_token):
        assert secret not in text
    events = {json.loads(line)["event"] for line in log.splitlines()}
    assert {"account_created", "device_claimed", "claim_rejected"} <= events


async def test_refresh_keeps_the_account_and_never_uses_the_client_id(tmp_path):
    port, device_port = free_port(), free_port()
    async with accounts_server(tmp_path, port, device_port) as running:
        url = running.url.replace("127.0.0.1", "localhost")
        writer, reply, _ = await device(device_port)
        auth, storage = code_client(url, [reply["claim_code"]])
        await list_devices(url, auth)
        writer.close()
        account = accounts_file(tmp_path)["devices"]["dev-1"]["owner"]
        form = {
            "grant_type": "refresh_token",
            "refresh_token": storage.tokens.refresh_token,
            "client_id": storage.client.client_id,
            "client_secret": storage.client.client_secret,
        }
        async with httpx2.AsyncClient() as plain:
            fresh = (await plain.post(f"http://localhost:{port}/token", data=form)).json()
            assert fresh["access_token"].startswith("gga_")
    records = state(tmp_path)
    assert [r["account"] for r in records["refresh"].values()] == [account]
    assert all(account != r["client_id"] for r in records["refresh"].values())


async def test_the_shared_bearer_token_is_refused_in_accounts_mode(tmp_path):
    port, device_port = free_port(), free_port()
    async with (
        accounts_server(tmp_path, port, device_port) as running,
        httpx2.AsyncClient() as plain,
    ):
        refused = await plain.post(
            running.url, headers={"Authorization": f"Bearer {TOKEN}"}, json={}
        )
    assert refused.status_code == 401
    assert refused.json()["error"] == "shared_credential_refused"


async def test_the_operator_cli_approval_still_signs_in_to_the_operator_account(tmp_path):
    port, device_port = free_port(), free_port()
    folder = tmp_path / "oauth"
    async with accounts_server(tmp_path, port, device_port) as running:
        url = running.url.replace("127.0.0.1", "localhost")
        auth, _ = signed_in_client(url, folder, accounts_mode=True)
        await list_devices(url, auth)
    operator = accounts_file(tmp_path)["operator"]
    assert [r["account"] for r in state(tmp_path)["refresh"].values()] == [operator]
    assert [row[0] for row in sign_ins(folder)] == ["grok-bot"]


async def test_a_live_single_operator_relay_migrates_without_a_new_sign_in(tmp_path):
    """Enrolled devices and live sign-ins (for example grok-bot) join the operator account."""
    port, device_port = free_port(), free_port()
    folder = tmp_path / "oauth"
    credentials, mcp_token = write_local_config(tmp_path)
    async with serve_gateway(
        credentials,
        mcp_token,
        simulator=True,
        port=port,
        device_port=device_port,
        log_stream=io.StringIO(),
        request_log=tmp_path / "requests.jsonl",
        oauth=True,
        oauth_issuer=f"http://localhost:{port}",
        oauth_directory=folder,
    ) as running:
        url = running.url.replace("127.0.0.1", "localhost")
        auth, storage = signed_in_client(url, folder)
        await list_devices(url, auth)
    assert not (folder / "accounts.json").exists()
    async with accounts_server(tmp_path, port, device_port, migrated=False) as running:
        # The same access token keeps working, now in the operator account.
        async with httpx2.AsyncClient() as plain:
            ok = await plain.post(
                running.url.replace("127.0.0.1", "localhost"),
                headers={
                    "Authorization": f"Bearer {storage.tokens.access_token}",
                    "Accept": "application/json, text/event-stream",
                },
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            )
            assert ok.status_code not in (401, 403)
        writer, reply, _ = await device(device_port)
        assert reply == {
            "ok": True,
            "session_id": reply["session_id"],
            "protocol_version": "0.1.0",
            "claimed": True,
        }
        writer.close()
    saved = accounts_file(tmp_path)
    assert saved["devices"]["dev-1"]["owner"] == saved["operator"]
    assert {r["account"] for r in state(tmp_path)["refresh"].values()} == {saved["operator"]}


async def test_a_gadget_that_does_not_opt_in_gets_todays_hello_reply(tmp_path):
    port, device_port = free_port(), free_port()
    async with accounts_server(tmp_path, port, device_port):
        writer, reply, exchange = await device(device_port, claim=None)
        assert set(reply) == {"ok", "session_id", "protocol_version"}
        assert set(await exchange({"type": "poll"})) == {"ok", "commands"}
        writer.close()


async def test_accounts_mode_needs_oauth(tmp_path):
    credentials, mcp_token = write_local_config(tmp_path)
    with pytest.raises(OperatorError, match="--accounts needs --oauth"):
        async with serve_gateway(
            credentials, mcp_token, port=free_port(), device_port=free_port(), accounts=True
        ):
            pytest.fail("started")


async def test_five_wrong_codes_end_a_sign_in_and_floods_are_logged(tmp_path):
    port, device_port = free_port(), free_port()
    async with accounts_server(tmp_path, port, device_port) as running:
        url = running.url.replace("127.0.0.1", "localhost")
        writer, reply, _ = await device(device_port)
        async with httpx2.AsyncClient(follow_redirects=False) as web:
            register = await web.post(
                f"http://localhost:{port}/register",
                json={"client_name": "flood", "redirect_uris": ["http://localhost:9/cb"]},
            )
            client_id = register.json()["client_id"]
            first = await web.get(
                f"http://localhost:{port}/authorize",
                params={
                    "response_type": "code",
                    "client_id": client_id,
                    "redirect_uri": "http://localhost:9/cb",
                    "code_challenge": "x" * 43,
                    "code_challenge_method": "S256",
                    "resource": url,
                },
            )
            page_url = first.headers["location"]
            request_id = REQUEST.search((await web.get(page_url)).text).group(1)
            approve = page_url.split("?")[0]
            for attempt in range(5):
                page = await web.post(approve, data={"request": request_id, "code": "DDDD-DDDD"})
                assert page.status_code == (400 if attempt == 4 else 200)
            assert "Start the sign-in again" in page.text
            # The request is gone: even the right code no longer works on it.
            gone = await web.post(
                approve, data={"request": request_id, "code": reply["claim_code"]}
            )
            assert gone.status_code == 400
        writer.close()
    lines = [json.loads(x) for x in (tmp_path / "requests.jsonl").read_text().splitlines()]
    rejected = [line for line in lines if line["event"] == "claim_rejected"]
    assert len(rejected) == 5 and "DDDD-DDDD" not in json.dumps(lines)


async def test_the_code_page_lets_the_browser_follow_the_redirect_to_grok_bot(tmp_path):
    """Browsers apply the form page's `form-action` CSP to the redirect after the post, so
    `form-action 'self'` stopped a phone's hand-back to Grok Bot's callback (17 Oct 2026)."""
    port, device_port = free_port(), free_port()
    async with accounts_server(tmp_path, port, device_port) as running:
        url = running.url.replace("127.0.0.1", "localhost")
        writer, reply, _ = await device(device_port)
        pages = []
        auth, _ = code_client(url, ["AAAA-AAAA", reply["claim_code"]], pages)
        await list_devices(url, auth)
        writer.close()
    form_page, done = pages
    assert form_page.status_code == 200 and 'method="post"' in form_page.text
    policy = form_page.headers["content-security-policy"]
    assert "form-action" not in policy and "default-src 'none'" in policy
    assert form_page.headers["x-frame-options"] == "DENY"
    assert done.status_code == 302
