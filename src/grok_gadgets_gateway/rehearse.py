"""Rehearse a Grok Bot session against a running `serve`: the same six tools, called locally.

Grok Bot's connector backend may speak either MCP era: the `initialize` handshake used on
13 October 2026 (legacy) or the 2026-07-28 revision, where every request is self-contained and
the session opens with `server/discover` (modern). `--protocol` picks one; `both` runs each.
"""

import asyncio
import json
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Implementation

from .service import MCP_PATH

TOOLS = {
    "gadgets_list_devices",
    "gadgets_get_state",
    "gadgets_command",
    "gadgets_command_status",
    "gadgets_read_events",
    "gadgets_diagnostics",
}
BLUE = {"r": 0, "g": 120, "b": 255, "on": True}
CLIENT_NAME = "grok-gadgets-rehearse"
PROTOCOLS = ("legacy", "modern")


def _client_info():
    """Name rehearsal calls so a request log can tell them from another client."""
    try:
        release = version("grok-gadgets-gateway")
    except PackageNotFoundError:
        release = "unknown"
    return Implementation(name=CLIENT_NAME, version=release)


class RehearsalError(Exception):
    """A step of the rehearsal did not give the expected result."""


async def _call(session, tool, arguments=None):
    result = await session.call_tool(tool, arguments or {})
    if result.is_error:
        raise RehearsalError(f"{tool} returned an MCP error")
    value = result.structured_content
    if value is None:
        value = json.loads(result.content[0].text)
    if not value.get("ok"):
        raise RehearsalError(f"{tool}: {value['error']['code']}: {value['error']['message']}")
    return value


async def rehearse(
    port, token, device_id=None, out=print, capability=None, arguments=None, protocol="legacy"
):
    """Call the tools Grok Bot will call. Returns the final state of the commanded gadget."""
    if protocol not in PROTOCOLS:
        raise ValueError(f"protocol must be one of {PROTOCOLS}")
    try:
        return await _rehearse(port, token, device_id, out, capability, arguments, protocol)
    except Exception as exc:  # The MCP client wraps step failures in exception groups.
        failure = _find(exc, RehearsalError)
        if failure is None or failure is exc:
            raise
        raise failure from None


def _pick(devices, device_id, capability):
    wanted = capability or "rgb.set"
    if device_id is None:
        offering = [d for d in devices if wanted in d["command_capabilities"]]
        if not offering:
            hint = "pass --device and --command" if capability else "start serve with --simulator"
            raise RehearsalError(f"no gadget offers {wanted}; {hint}")
        return offering[0], wanted
    for device in devices:
        if device["device_id"] == device_id:
            if capability is None and "rgb.set" not in device["command_capabilities"]:
                raise RehearsalError(f"{device_id} has no rgb.set; pass --command and --args")
            if wanted not in device["command_capabilities"]:
                raise RehearsalError(f"{device_id} does not offer {wanted}")
            return device, wanted
    raise RehearsalError(f"no gadget {device_id} is connected")


async def _open(session, protocol):
    """Start the session the way the chosen era does; return the negotiated version."""
    if protocol == "modern":
        await session.discover()
    else:
        await session.initialize()
    return session.protocol_version


async def _rehearse(port, token, device_id, out, capability, arguments, protocol):
    url = f"http://127.0.0.1:{port}{MCP_PATH}"
    headers = {"Authorization": "Bearer " + token}
    # The MCP SDK's default timeouts: 30 s per request, 300 s for the event stream.
    timeout = httpx2.Timeout(30.0, read=300.0)
    async with (
        httpx2.AsyncClient(headers=headers, timeout=timeout) as http,
        streamable_http_client(url, http_client=http) as (read, write),
        ClientSession(read, write, client_info=_client_info()) as session,
    ):
        negotiated = await _open(session, protocol)
        names = {tool.name for tool in (await session.list_tools()).tools}
        if not TOOLS <= names:
            raise RehearsalError("the gateway does not offer the six gadgets tools")
        out(f"ok  {url} offers the six tools Grok Bot will use ({protocol} MCP, {negotiated})")

        devices = (await _call(session, "gadgets_list_devices"))["devices"]
        for device in devices:
            label = "simulated" if device["simulated"] else "reported by the gadget"
            commands = ", ".join(device["command_capabilities"]) or "none"
            out(f"ok  gadget {device['device_id']} ({label}); commands: {commands}")
        device, capability = _pick(devices, device_id, capability)
        device_id = device["device_id"]
        if arguments is None:
            if capability != "rgb.set":
                raise RehearsalError(f"pass --args with JSON arguments for {capability}")
            arguments = BLUE

        command = (
            await _call(
                session,
                "gadgets_command",
                {"device_id": device_id, "capability": capability, "arguments": arguments},
            )
        )["command"]
        sent = json.dumps(arguments, separators=(",", ":"))
        out(f"ok  gadgets_command {capability} {sent} -> {command['status']}")
        if command["status"] != "executed":
            raise RehearsalError(f"command status is {command['status']}, not executed")

        state = (await _call(session, "gadgets_get_state", {"device_id": device_id}))["device"]
        out(f"ok  gadgets_get_state {device_id}: {json.dumps(state['state'])}")
        return state


def _find(exc, kind):
    if isinstance(exc, kind):
        return exc
    for inner in getattr(exc, "exceptions", ()):
        found = _find(inner, kind)
        if found is not None:
            return found
    return None


def main(
    port, token_path_text, device_id, read_token, capability=None, arguments=None, protocol="both"
):
    token = read_token(token_path_text)
    eras = PROTOCOLS if protocol == "both" else (protocol,)
    for era in eras:
        try:
            asyncio.run(rehearse(port, token, device_id, print, capability, arguments, era))
        except Exception as exc:  # noqa: BLE001 - any failure becomes one plain message
            failure = _find(exc, RehearsalError)
            if failure is None:
                failure = (
                    f"no authenticated gateway answers {era} MCP on 127.0.0.1:{port}; "
                    "start `serve` first"
                )
            print(f"Rehearsal failed: {failure}")
            return 1
    print(
        f"Rehearsal passed ({', '.join(eras)} MCP): these are the calls Grok Bot will make. "
        "It does not prove a Grok Bot connection or any physical effect."
    )
    return 0


# --- rehearse --accounts (GW-CLAIM-004, hub ADR 011) ---

ACCOUNT_GADGETS = {"gadget-a": "rehearsal-token-gadget-a", "gadget-b": "rehearsal-token-gadget-b"}


class _Gadget:
    """A simulated gadget on the relay's device port that shows claim codes (protocol 0.1.0)."""

    def __init__(self, port, device_id):
        self.port, self.device_id = port, device_id
        self.code = None
        self.task = None
        self.writer = None

    async def start(self, claim="ready"):
        from .protocol import _root

        hello = json.loads(_root.joinpath("fixtures/device-transcript.json").read_text())[0]
        hello["device"]["device_id"] = self.device_id
        hello["token"] = ACCOUNT_GADGETS[self.device_id]
        hello["claim"] = claim
        reader, self.writer = await asyncio.open_connection("127.0.0.1", self.port)
        reply = await self._exchange(reader, hello)
        if not reply.get("ok"):
            raise RehearsalError(f"{self.device_id} hello failed: {reply}")
        self._remember(reply)
        self.task = asyncio.create_task(self._serve(reader))

    async def _exchange(self, reader, message):
        self.writer.write((json.dumps(message) + "\n").encode())
        await self.writer.drain()
        return json.loads(await asyncio.wait_for(reader.readline(), 5))

    def _remember(self, reply):
        if "claimed" in reply:
            self.code = reply.get("claim_code")

    async def _serve(self, reader):
        while True:
            reply = await self._exchange(reader, {"type": "poll"})
            self._remember(reply)
            for command in reply.get("commands", []):
                ack = {
                    "type": "ack",
                    "command_id": command["command_id"],
                    "status": "executed",
                    "state": {"rgb": command["arguments"]},
                }
                await self._exchange(reader, ack)
            await asyncio.sleep(0.05)

    async def reset_and_rejoin(self):
        """A BOOT reset and a phone re-join: a new session whose hello says after_setup."""
        await self.stop()
        await self.start("after_setup")

    async def stop(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.writer is not None:
            self.writer.close()


class _Person:
    """One sign-in made with a code on the relay's sign-in page, and its MCP session."""

    def __init__(self, session):
        self.session = session

    async def call(self, tool, **arguments):
        result = await self.session.call_tool(tool, arguments)
        value = result.structured_content
        return value if value is not None else json.loads(result.content[0].text)

    async def ok(self, tool, **arguments):
        value = await self.call(tool, **arguments)
        if not value.get("ok"):
            raise RehearsalError(f"{tool}: {value['error']['code']}")
        return value

    async def refused(self, tool, expected, **arguments):
        value = await self.call(tool, **arguments)
        if value.get("ok") or value["error"]["code"] != expected:
            raise RehearsalError(f"{tool} should answer {expected}, got {value}")


async def _sign_in(stack, url, code):
    import re
    from urllib.parse import parse_qs, urlparse

    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import AuthorizationCodeResult, OAuthClientMetadata

    result = {}

    async def browser(authorization_url):
        async with httpx2.AsyncClient(follow_redirects=False) as web:
            page_url = (await web.get(authorization_url)).headers["location"]
            page = await web.get(page_url)
            request_id = re.search(r'name="request" value="([^"]+)"', page.text).group(1)
            done = await web.post(
                page_url.split("?")[0], data={"request": request_id, "code": code}
            )
            if done.status_code != 302:
                raise RehearsalError("the sign-in page refused the code")
            query = parse_qs(urlparse(done.headers["location"]).query)
            result.update({k: v[0] for k, v in query.items()})

    async def callback():
        return AuthorizationCodeResult(code=result["code"], state=result.get("state"))

    class Memory:
        tokens = client = None

        async def get_tokens(self):
            return self.tokens

        async def set_tokens(self, tokens):
            self.tokens = tokens

        async def get_client_info(self):
            return self.client

        async def set_client_info(self, client_info):
            self.client = client_info

    metadata = OAuthClientMetadata(
        client_name=CLIENT_NAME, redirect_uris=["http://localhost:9/callback"]
    )
    auth = OAuthClientProvider(url, metadata, Memory(), browser, callback)
    http = await stack.enter_async_context(httpx2.AsyncClient(auth=auth, timeout=30))
    read, write = await stack.enter_async_context(streamable_http_client(url, http_client=http))
    session = await stack.enter_async_context(
        ClientSession(read, write, client_info=_client_info())
    )
    await session.initialize()
    return _Person(session)


def _free_port():
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def rehearse_accounts(out=print, directory=None):
    """Two accounts on a private relay in accounts mode, with two simulated gadgets.

    The same steps as the Phase 11 acceptance, in a temporary config directory. It does not
    use or change this computer's gateway configuration.
    """
    import io
    import tempfile
    from contextlib import AsyncExitStack

    from .accounts import Accounts
    from .operator import atomic_write
    from .service import serve_gateway
    from .transport import digest

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(directory or temporary)
        credentials = root / "credentials.json"
        devices = {d: {"sha256": digest(t), "revoked": False} for d, t in ACCOUNT_GADGETS.items()}
        atomic_write(credentials, json.dumps({"devices": devices}).encode())
        atomic_write(root / "mcp-token", b"rehearsal-only-mcp-token\n")
        Accounts(root / "oauth" / "accounts.json").open([])  # gadgets enrolled after setup
        port, device_port = _free_port(), _free_port()
        blue = {"device_id": "gadget-b", "capability": "rgb.set", "arguments": BLUE}
        async with AsyncExitStack() as stack:
            running = await stack.enter_async_context(
                serve_gateway(
                    credentials,
                    root / "mcp-token",
                    port=port,
                    device_port=device_port,
                    log_stream=io.StringIO(),
                    request_log=root / "requests.jsonl",
                    oauth=True,
                    oauth_issuer=f"http://localhost:{port}",
                    oauth_directory=root / "oauth",
                    accounts=True,
                )
            )
            url = running.url.replace("127.0.0.1", "localhost")
            a_gadget, b_gadget = _Gadget(device_port, "gadget-a"), _Gadget(device_port, "gadget-b")
            for gadget in (a_gadget, b_gadget):
                await gadget.start()
                stack.push_async_callback(gadget.stop)
            alice = await _sign_in(stack, url, a_gadget.code)
            out("ok  A signed in with the code gadget-a showed")
            bob = await _sign_in(stack, url, b_gadget.code)
            out("ok  B signed in with the code gadget-b showed")

            listed = (await alice.ok("gadgets_list_devices"))["devices"]
            if [d["device_id"] for d in listed] != ["gadget-a"]:
                raise RehearsalError(f"A sees {listed}")
            out("ok  A lists only gadget-a")
            await alice.refused("gadgets_get_state", "unknown_device", device_id="gadget-b")
            await alice.refused("gadgets_command", "unknown_device", **blue)
            await alice.refused("gadgets_read_events", "unknown_device", device_id="gadget-b")
            out("ok  A cannot read, command or see events of gadget-b")
            sent = (await bob.ok("gadgets_command", **blue))["command"]
            await alice.refused(
                "gadgets_command_status", "unknown_command", command_id=sent["command_id"]
            )
            out("ok  A cannot read B's command status")
            await alice.refused("gadgets_claim", "invalid_code", code="ZZZZ-ZZZZ")
            out("ok  a wrong code is refused")

            share = await bob.ok("gadgets_share", device_id="gadget-b", action="create")
            await alice.ok("gadgets_claim", code=share["share_code"])
            await alice.ok("gadgets_command", **blue)
            out("ok  B shared gadget-b; A commanded it")
            shares = (await bob.ok("gadgets_share", device_id="gadget-b", action="list"))["shares"]
            await bob.ok(
                "gadgets_share",
                device_id="gadget-b",
                action="revoke",
                share_id=shares[0]["share_id"],
            )
            await alice.refused("gadgets_get_state", "unknown_device", device_id="gadget-b")
            out("ok  B revoked the share; A lost gadget-b")

            await b_gadget.reset_and_rejoin()
            if not b_gadget.code:
                raise RehearsalError("gadget-b showed no transfer code after the reset")
            claimed = await alice.ok("gadgets_claim", code=b_gadget.code)
            if claimed["role"] != "pending_transfer":
                raise RehearsalError(f"the transfer is not pending: {claimed}")
            await alice.refused("gadgets_command", "transfer_pending", **blue)
            out("ok  after a reset gadget-b showed a transfer code; A's claim is pending")
            removed = await bob.ok("gadgets_remove", device_id="gadget-b")
            if removed["result"] != "transferred":
                raise RehearsalError(f"B's remove gave {removed}")
            await alice.ok("gadgets_command", **blue)
            out("ok  B removed gadget-b; A owns it and commanded it")
