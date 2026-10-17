"""GW-CLAIM-002 (hub ADR 011 §8): every tool is scoped to the caller's account.

Two people sign in with the codes their own gadgets show. One cross-account test per tool,
then claim, share, revoke and remove through the tools themselves.
"""

import asyncio
import io
import json
from contextlib import AsyncExitStack

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from test_claim_sign_in import code_client
from test_domain import hello
from test_http import free_port

from grok_gadgets_gateway.accounts import Accounts
from grok_gadgets_gateway.operator import atomic_write
from grok_gadgets_gateway.service import serve_gateway

TOKENS = {"dev-1": "token-for-device-one", "dev-2": "token-for-device-two"}
EVENT = {"type": "event", "event_id": "boot-1-1", "name": "button", "data": {"pressed": True}}


def relay(tmp_path):
    credentials = tmp_path / "credentials.json"
    devices = {d: {"token": t, "revoked": False} for d, t in TOKENS.items()}
    atomic_write(credentials, json.dumps({"devices": devices}).encode())
    atomic_write(tmp_path / "mcp-token", b"mcp-test-token-value\n")
    Accounts(tmp_path / "oauth" / "accounts.json").open([])  # both gadgets unclaimed
    port = free_port()
    device_port = free_port()
    server = serve_gateway(
        credentials,
        tmp_path / "mcp-token",
        port=port,
        device_port=device_port,
        log_stream=io.StringIO(),
        request_log=tmp_path / "requests.jsonl",
        oauth=True,
        oauth_issuer=f"http://localhost:{port}",
        oauth_directory=tmp_path / "oauth",
        accounts=True,
    )
    return server, device_port


async def gadget(device_port, device_id):
    reader, writer = await asyncio.open_connection("127.0.0.1", device_port)
    message = hello(device_id)
    message["token"] = TOKENS[device_id]
    message["claim"] = "ready"

    async def exchange(value):
        writer.write((json.dumps(value) + "\n").encode())
        await writer.drain()
        return json.loads(await asyncio.wait_for(reader.readline(), 3))

    return writer, await exchange(message), exchange


class Person:
    """One Grok Bot sign-in, made with a code, and its MCP session."""

    def __init__(self, session):
        self.session = session

    async def call(self, tool, **arguments):
        result = await self.session.call_tool(tool, arguments)
        return result.structured_content or json.loads(result.content[0].text)


async def sign_in(stack, url, code):
    auth, _ = code_client(url, [code])
    http = await stack.enter_async_context(httpx2.AsyncClient(auth=auth, timeout=10))
    read, write = await stack.enter_async_context(streamable_http_client(url, http_client=http))
    session = await stack.enter_async_context(ClientSession(read, write))
    await session.initialize()
    return Person(session)


def error(response):
    assert response["ok"] is False, response
    return response["error"]["code"]


async def two_people(stack, tmp_path):
    server, device_port = relay(tmp_path)
    running = await stack.enter_async_context(server)
    url = running.url.replace("127.0.0.1", "localhost")
    one, reply_one, talk_one = await gadget(device_port, "dev-1")
    two, reply_two, talk_two = await gadget(device_port, "dev-2")
    stack.callback(one.close)
    stack.callback(two.close)
    alice = await sign_in(stack, url, reply_one["claim_code"])
    bob = await sign_in(stack, url, reply_two["claim_code"])
    return url, alice, bob, talk_one, talk_two, device_port


async def test_each_tool_answers_only_for_the_callers_own_gadgets(tmp_path):
    async with AsyncExitStack() as stack:
        _, alice, bob, talk_one, talk_two, _ = await two_people(stack, tmp_path)

        # gadgets_list_devices
        listed = await alice.call("gadgets_list_devices")
        assert [(d["device_id"], d["role"]) for d in listed["devices"]] == [("dev-1", "owner")]
        assert [d["device_id"] for d in (await bob.call("gadgets_list_devices"))["devices"]] == [
            "dev-2"
        ]

        # gadgets_get_state: another account's gadget looks exactly like a missing one.
        assert (await alice.call("gadgets_get_state", device_id="dev-1"))["ok"]
        foreign = await alice.call("gadgets_get_state", device_id="dev-2")
        missing = await alice.call("gadgets_get_state", device_id="dev-9")
        assert error(foreign) == error(missing) == "unknown_device"
        assert foreign["error"]["message"] == missing["error"]["message"]

        # gadgets_command
        arguments = {"r": 1, "g": 2, "b": 3, "on": True}
        assert (
            error(
                await alice.call(
                    "gadgets_command", device_id="dev-2", capability="rgb.set", arguments=arguments
                )
            )
            == "unknown_device"
        )

        # gadgets_command_status: Bob's receipt is invisible to Alice, also by its ID.
        sent = await bob.call(
            "gadgets_command",
            device_id="dev-2",
            capability="rgb.set",
            arguments=arguments,
            command_id="bob-cmd-1",
        )
        assert sent["ok"] and sent["command"]["command_id"] == "bob-cmd-1"
        assert (await bob.call("gadgets_command_status", command_id="bob-cmd-1"))["ok"]
        assert error(await alice.call("gadgets_command_status", command_id="bob-cmd-1")) == (
            "unknown_command"
        )
        # Reusing Bob's command ID never returns Bob's receipt to Alice.
        reused = await alice.call(
            "gadgets_command",
            device_id="dev-1",
            capability="rgb.set",
            arguments=arguments,
            command_id="bob-cmd-1",
        )
        assert error(reused) == "duplicate_conflict"
        assert "dev-2" not in json.dumps(reused)

        # gadgets_read_events
        assert (await talk_two(EVENT))["ok"]
        assert (await talk_one({**EVENT, "event_id": "boot-1-2"}))["ok"]
        seen = await alice.call("gadgets_read_events")
        assert [e["device_id"] for e in seen["events"]] == ["dev-1"]
        assert error(await alice.call("gadgets_read_events", device_id="dev-2")) == (
            "unknown_device"
        )
        assert [e["device_id"] for e in (await bob.call("gadgets_read_events"))["events"]] == [
            "dev-2"
        ]

        # gadgets_diagnostics
        report = (await alice.call("gadgets_diagnostics"))["report"]
        assert [d["device_id"] for d in report["devices"]] == ["dev-1"]
        assert report["event_count"] == 1 and report["command_count"] == 0
        assert "dev-2" not in json.dumps(report)


async def test_share_use_revoke_and_remove_through_the_tools(tmp_path):
    async with AsyncExitStack() as stack:
        url, alice, bob, _, talk_two, _ = await two_people(stack, tmp_path)
        arguments = {"r": 9, "g": 9, "b": 9, "on": True}

        # Bob shares dev-2 with Alice for use; Alice redeems the code with gadgets_claim.
        share = await bob.call("gadgets_share", device_id="dev-2", action="create", scope="use")
        assert share["ok"] and share["expires_in"] == 600
        assert error(await alice.call("gadgets_share", device_id="dev-2", action="create")) == (
            "unknown_device"
        )
        got = await alice.call("gadgets_claim", code=share["share_code"])
        assert got == {**got, "ok": True, "device_id": "dev-2", "role": "use"}
        roles = {
            d["device_id"]: d["role"] for d in (await alice.call("gadgets_list_devices"))["devices"]
        }
        assert roles == {"dev-1": "owner", "dev-2": "use"}
        used = await alice.call(
            "gadgets_command", device_id="dev-2", capability="rgb.set", arguments=arguments
        )
        assert used["ok"]
        # A grantee cannot share on.
        assert error(await alice.call("gadgets_share", device_id="dev-2", action="create")) == (
            "not_owner"
        )

        # Bob lists and revokes the grant; Alice loses the gadget at her next call.
        grants = (await bob.call("gadgets_share", device_id="dev-2", action="list"))["shares"]
        assert [g["scope"] for g in grants] == ["use"]
        revoked = await bob.call(
            "gadgets_share", device_id="dev-2", action="revoke", share_id=grants[0]["share_id"]
        )
        assert revoked["ok"]
        assert error(await alice.call("gadgets_get_state", device_id="dev-2")) == ("unknown_device")

        # A view grant reads but never commands.
        view = await bob.call("gadgets_share", device_id="dev-2", action="create", scope="view")
        await alice.call("gadgets_claim", code=view["share_code"])
        assert (await alice.call("gadgets_get_state", device_id="dev-2"))["ok"]
        assert (
            error(
                await alice.call(
                    "gadgets_command", device_id="dev-2", capability="rgb.set", arguments=arguments
                )
            )
            == "not_allowed"
        )
        assert (await alice.call("gadgets_remove", device_id="dev-2"))["result"] == "dropped"

        # Bob removes dev-2: unclaimed, and the gadget gets a new code in its next poll.
        assert (await bob.call("gadgets_remove", device_id="dev-2"))["result"] == "unclaimed"
        assert (await bob.call("gadgets_list_devices"))["devices"] == []
        polled = await talk_two({"type": "poll"})
        assert polled["claimed"] is False and len(polled["claim_code"]) == 9

        # Alice claims it with that code from her live sign-in.
        claimed = await alice.call("gadgets_claim", code=polled["claim_code"])
        assert claimed["role"] == "owner"
        assert error(await alice.call("gadgets_claim", code="ZZZZ-ZZZZ")) == "invalid_code"

        # A link code signs a second app in to Alice's account, with no recovery code.
        link = await alice.call("gadgets_share", action="link")
        second = await sign_in(stack, url, link["link_code"])
        names = [d["device_id"] for d in (await second.call("gadgets_list_devices"))["devices"]]
        assert names == ["dev-1", "dev-2"]

    lines = [json.loads(x) for x in (tmp_path / "requests.jsonl").read_text().splitlines()]
    events = {line["event"] for line in lines}
    assert {"share_created", "share_used", "share_revoked", "device_unclaimed"} <= events
    text = json.dumps(lines)
    for secret in (share["share_code"], view["share_code"], link["link_code"]):
        assert secret not in text


async def test_without_accounts_the_tools_are_unchanged(tmp_path):
    """Operator mode keeps the six tools and adds none."""
    from grok_gadgets_gateway.domain import Gateway
    from grok_gadgets_gateway.mcp_server import make_server

    names = {tool.name for tool in await make_server(Gateway()).list_tools()}
    assert names == {
        "gadgets_list_devices",
        "gadgets_get_state",
        "gadgets_command",
        "gadgets_command_status",
        "gadgets_read_events",
        "gadgets_diagnostics",
    }
