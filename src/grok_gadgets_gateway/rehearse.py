"""Rehearse a Grok Bot session against a running `serve`: the same six tools, called locally.

Grok Bot's connector backend may speak either MCP era: the `initialize` handshake used on
13 October 2026 (legacy) or the 2026-07-28 revision, where every request is self-contained and
the session opens with `server/discover` (modern). `--protocol` picks one; `both` runs each.
"""

import asyncio
import json
import os
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


# --- rehearse --update (GW-OTA-001, hub ADR 012) ---

UPDATE_GADGET = ("update-gadget", "rehearsal-token-update-gadget")
UPDATE_BOARD = "rehearsal-board"


class _UpdatingGadget:
    """A simulated gadget that checks and downloads signed updates as the firmware does."""

    def __init__(self, port, public_key, version):
        from cryptography.hazmat.primitives import serialization

        self.port = port
        self.key = serialization.load_pem_public_key(Path(public_key).read_bytes())
        self.version = version
        self.reader = self.writer = None
        self.offer = None

    async def connect(self):
        from .protocol import _root

        hello = json.loads(_root.joinpath("fixtures/device-transcript.json").read_text())[0]
        hello["device"]["device_id"], hello["token"] = UPDATE_GADGET
        hello["device"]["firmware_version"] = self.version
        hello["update"] = "ready"
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", self.port)
        reply = await self.send(hello)
        if not reply.get("ok"):
            raise RehearsalError(f"hello failed: {reply}")
        self.offer = reply.get("update")
        return self.offer

    async def send(self, message):
        self.writer.write((json.dumps(message) + "\n").encode())
        await self.writer.drain()
        line = await asyncio.wait_for(self.reader.readline(), 5)
        if len(line) > 2048:
            raise RehearsalError("a reply is longer than 2,048 bytes")
        return json.loads(line)

    async def report(self, status, version, reason=None):
        message = {"type": "update_status", "version": version, "status": status}
        if reason:
            message["reason"] = reason
        if not (await self.send(message)).get("ok"):
            raise RehearsalError(f"the relay refused the {status} report")

    def check(self):
        """The firmware's checks before any download: signature, then the manifest."""
        import base64

        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec

        raw = base64.b64decode(self.offer["manifest"])
        try:
            self.key.verify(
                base64.b64decode(self.offer["signature"]), raw, ec.ECDSA(hashes.SHA256())
            )
        except InvalidSignature:
            return None, "signature"
        manifest = json.loads(raw)
        if manifest["board"] != UPDATE_BOARD:
            return None, "board"
        if manifest["version"] in (self.version,) or manifest["size"] != self.offer["size"]:
            return None, "version"
        return manifest, None

    async def download(self, manifest):
        import base64
        import hashlib

        digest, size = hashlib.sha256(), 0
        for offset in range(0, manifest["size"], self.offer["chunk"]):
            reply = await self.send(
                {"type": "update_chunk", "version": manifest["version"], "offset": offset}
            )
            if not reply.get("ok"):
                raise RehearsalError(f"chunk {offset} refused: {reply['error']['code']}")
            data = base64.b64decode(reply["data"])
            digest.update(data)
            size += len(data)
        return size == manifest["size"] and digest.hexdigest() == manifest["sha256"]

    async def close(self):
        if self.writer is not None:
            self.writer.close()
            self.writer = None


async def rehearse_update(out=print, directory=None):
    """Sign updates and deliver them to a simulated gadget on a private relay.

    A good update is offered, downloaded in chunks, verified and applied after a restart.
    An update signed with another key and one with a changed byte are refused, and an image
    that never goes Online is reported as reverted and not offered again. It uses a temporary
    config directory and never this computer's gateway configuration or keys.
    """
    import io
    import tempfile

    from . import firmware
    from .operator import atomic_write
    from .service import serve_gateway
    from .transport import digest

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(directory or temporary)
        builder = firmware.keygen(root / "keys" / "builder.key")
        stranger = firmware.keygen(root / "keys" / "stranger.key")
        store = firmware.Store(root / "firmware")

        def publish(version, key, **add):
            image = root / f"{version}.bin"
            image.write_bytes(os.urandom(5 * firmware.CHUNK + 300))
            bundle = root / f"update-{version}"
            firmware.sign(image, key, board=UPDATE_BOARD, version=version, out=bundle)
            store.add(bundle, **add)
            return bundle

        credentials = root / "credentials.json"
        devices = {UPDATE_GADGET[0]: {"sha256": digest(UPDATE_GADGET[1]), "revoked": False}}
        atomic_write(credentials, json.dumps({"devices": devices}).encode())
        atomic_write(root / "mcp-token", b"rehearsal-only-mcp-token\n")
        async with serve_gateway(
            credentials,
            root / "mcp-token",
            port=_free_port(),
            device_port=(device_port := _free_port()),
            log_stream=io.StringIO(),
            request_log=root / "requests.jsonl",
            firmware_directory=store.root,
        ):
            gadget = _UpdatingGadget(device_port, builder["public"], "1.0.0")
            if await gadget.connect() is not None:
                raise RehearsalError("an update was offered before one was assigned")
            await gadget.close()

            publish("1.0.1", builder["private"], public_key=builder["public"])
            store.assign(UPDATE_GADGET[0], "1.0.1")
            if await gadget.connect() is None:
                raise RehearsalError("the assigned update was not offered")
            manifest, reason = gadget.check()
            if manifest is None:
                raise RehearsalError(f"the gadget refused a good update: {reason}")
            await gadget.report("started", "1.0.1")
            if not await gadget.download(manifest):
                raise RehearsalError("the downloaded image does not match its manifest")
            await gadget.report("verified", "1.0.1")
            await gadget.close()
            gadget.version = "1.0.1"  # The restart: the new image says hello.
            if await gadget.connect() is not None:
                raise RehearsalError("the running version was offered again")
            await gadget.report("applied", "1.0.1")
            if store.status()[UPDATE_GADGET[0]]["status"] != "applied":
                raise RehearsalError("the relay did not record the update as applied")
            out("ok  1.0.1 offered, downloaded in chunks, verified, restarted and applied")

            publish("1.0.2", stranger["private"], unchecked=True)
            store.assign(UPDATE_GADGET[0], "1.0.2")
            await gadget.close()
            await gadget.connect()
            manifest, reason = gadget.check()
            if manifest is not None or reason != "signature":
                raise RehearsalError("an update signed with another key was not refused")
            await gadget.report("rejected", "1.0.2", reason)
            if (await gadget.send({"type": "poll"})).get("update", False) is not None:
                raise RehearsalError("the refused update was not withdrawn")
            out("ok  1.0.2, signed with another key, refused before any download")

            publish("1.0.3", builder["private"], public_key=builder["public"])
            stored = store.root / UPDATE_BOARD / "1.0.3" / "image.bin"
            changed = bytearray(stored.read_bytes())
            changed[len(changed) // 2] ^= 0x01
            stored.write_bytes(bytes(changed))
            store.assign(UPDATE_GADGET[0], "1.0.3")
            await gadget.close()
            await gadget.connect()
            manifest, reason = gadget.check()
            if manifest is None or await gadget.download(manifest):
                raise RehearsalError("a changed byte was not caught by the SHA-256 check")
            await gadget.report("failed", "1.0.3", "sha256")
            out("ok  1.0.3 with one changed byte refused by the SHA-256 check before Update.end()")

            publish("1.0.4", builder["private"], public_key=builder["public"])
            store.assign(UPDATE_GADGET[0], "1.0.4")
            await gadget.close()
            await gadget.connect()
            manifest, _ = gadget.check()
            if manifest is None or not await gadget.download(manifest):
                raise RehearsalError("1.0.4 did not download")
            await gadget.report("verified", "1.0.4")
            await gadget.close()
            # 1.0.4 never goes Online; the previous image starts and reports the revert.
            await gadget.connect()
            if gadget.offer is None:
                raise RehearsalError("1.0.4 was not offered before the revert report")
            await gadget.report("reverted", "1.0.4", "not_online")
            await gadget.close()
            if await gadget.connect() is not None:
                raise RehearsalError("a reverted version was offered again")
            await gadget.close()
            out("ok  1.0.4 never went Online: reverted, reported and not offered again")

            names = [
                json.loads(line)["event"]
                for line in (root / "requests.jsonl").read_text().splitlines()
            ]
            for event in ("update_applied", "update_rejected", "update_failed", "update_reverted"):
                if event not in names:
                    raise RehearsalError(f"the request log has no {event} line")
            out("ok  the request log has update_applied, _rejected, _failed and _reverted lines")
