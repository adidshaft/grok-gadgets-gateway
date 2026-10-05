"""Grok-facing tool adapter implemented with the official MCP Python SDK."""

import hashlib
import inspect
import json
import sys
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .protocol import GatewayError

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
ACTUATE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True
)

COMMAND_DESCRIPTION = """Ask a gadget to run one command capability with JSON arguments.

command_id is optional. For a new action, omit it: the gateway creates a unique ID and
returns it as command.command_id. Within 10 minutes and the same gateway process, repeating
that ID with identical arguments returns the original receipt with duplicate=true, or
stale_command_id if the receipt was evicted. It does not run again. Changed arguments return
duplicate_conflict. Never reuse an ID for a new action. Retry protection is not durable:
after a restart or outside that window, inspect state before acting. Do the same if a lost
reply left you without a command ID. Never create a new ID to retry an uncertain action.

command.status: accepted = queued; dispatched = delivered, waiting for the device;
executed / failed = the device reported a result (a report, not physical proof);
not_delivered = the device never received it, so sending it again as a new action is safe;
timed_out / unconfirmed = outcome unknown, so read state before acting again."""


class RequestLog:
    """Redacted structured JSON lines on stderr: never arguments, state, events or tokens."""

    def __init__(self, transport, stream=None):
        self.transport = transport
        self.stream = stream if stream is not None else sys.stderr

    def write(self, record):
        record = {"ts": datetime.now(timezone.utc).isoformat(), **record}
        print(json.dumps(record, separators=(",", ":")), file=self.stream, flush=True)


def argument_hash(arguments):
    data = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(data.encode()).hexdigest()[:16]


def make_server(
    gateway,
    simulator=None,
    device_server=None,
    test_controls=False,
    *,
    request_log=None,
    listener_status=None,
    http=False,
    **fastmcp_options,
):
    if http and (test_controls or device_server is not None):
        # HTTP sessions each run the MCP lifespan; the service owns the listener instead.
        raise ValueError("HTTP mode never exposes test controls or a per-session listener")

    @asynccontextmanager
    async def lifespan(server):
        if device_server:
            await device_server.start()
        try:
            yield {}
        finally:
            if device_server:
                await device_server.close()

    mcp = FastMCP("Grok Gadgets gateway", lifespan=lifespan, **fastmcp_options)

    def tool(annotations, description=None):
        def register(fn):
            text = inspect.cleandoc(description or fn.__doc__)
            return mcp.tool(annotations=annotations, description=text)(fn)

        return register

    def finish(tool, arguments, started, response):
        request_id = uuid.uuid4().hex[:16]
        response = {**response, "request_id": request_id}
        if request_log is not None:
            record = {
                "event": "mcp_tool_call",
                "request_id": request_id,
                "transport": request_log.transport,
                "tool": tool,
                "args_sha256": argument_hash(arguments),
                "outcome": "ok" if response["ok"] else response["error"]["code"],
                "duration_ms": round((time.monotonic() - started) * 1000, 1),
            }
            command = response.get("command")
            if command:
                record.update(
                    command_id=command["command_id"],
                    status=command["status"],
                    duplicate=command.get("duplicate", False),
                    simulated=command["simulated"],
                )
            request_log.write(record)
        return response

    def run(tool, arguments, action):
        started = time.monotonic()
        try:
            response = {"ok": True, **action()}
        except GatewayError as exc:
            response = exc.response()
        return finish(tool, arguments, started, response)

    @tool(READ_ONLY)
    async def gadgets_list_devices() -> dict:
        """List gadgets: callable command capabilities with argument contracts, event names,
        availability, freshness and simulated labels. Call this first."""
        return run("gadgets_list_devices", {}, lambda: {"devices": gateway.list_devices()})

    @tool(READ_ONLY)
    async def gadgets_get_state(device_id: str) -> dict:
        """Read a gadget's last reported state and freshness (fresh, stale or offline).
        Reported state is the device's own report; it does not prove a physical effect."""
        return run(
            "gadgets_get_state",
            {"device_id": device_id},
            lambda: {"device": gateway.state(device_id)},
        )

    @tool(ACTUATE, COMMAND_DESCRIPTION)
    async def gadgets_command(
        device_id: str, capability: str, arguments: dict, command_id: str | None = None
    ) -> dict:
        started = time.monotonic()
        request = {
            "device_id": device_id,
            "capability": capability,
            "arguments": arguments,
            "command_id": command_id,
        }
        try:
            value = gateway.command(device_id, capability, arguments, command_id)
        except GatewayError as exc:
            return finish("gadgets_command", request, started, exc.response())
        response = {"ok": True, "command": value}
        is_simulator = simulator is not None and device_id == simulator.device_id
        if is_simulator and not value["duplicate"] and value["status"] == "accepted":
            try:
                await simulator.execute()
            except GatewayError as exc:
                # The command is already accepted; report its real status, never a bare error.
                response["simulator_error"] = exc.response()["error"]
            response["command"] = {
                **gateway.command_status(value["command_id"]),
                "duplicate": False,
            }
        return finish("gadgets_command", request, started, response)

    @tool(READ_ONLY)
    async def gadgets_command_status(command_id: str) -> dict:
        """Read a command receipt. executed/failed are device reports, not physical proof;
        not_delivered is safe to send again as a new action; timed_out/unconfirmed need a
        state check before acting again."""
        return run(
            "gadgets_command_status",
            {"command_id": command_id},
            lambda: {"command": gateway.command_status(command_id)},
        )

    @tool(READ_ONLY)
    async def gadgets_read_events(
        cursor: str | None = None, device_id: str | None = None, limit: int = 32
    ) -> dict:
        """Read buffered device events (for example button presses) in order. Start with a
        null cursor, then pass next_cursor. On history_lost or cursor_reset, read again with a
        null cursor."""
        return run(
            "gadgets_read_events",
            {"cursor": cursor, "device_id": device_id, "limit": limit},
            lambda: gateway.read_events(cursor, device_id, limit),
        )

    @tool(READ_ONLY)
    async def gadgets_diagnostics() -> dict:
        """Get an allowlisted support report without tokens, state, arguments or raw errors."""

        def report():
            value = gateway.diagnostics()
            if listener_status is not None:
                value["device_listener"] = listener_status()
            return {"report": value}

        return run("gadgets_diagnostics", {}, report)

    if test_controls:
        if simulator is None:
            raise ValueError("Test controls require explicit simulator")

        @tool(ToolAnnotations(readOnlyHint=False, destructiveHint=False))
        async def test_simulator_control(action: str, pressed: bool | None = None) -> dict:
            """TEST ONLY: inject a simulated button edge or disconnect/reconnect."""
            return run(
                "test_simulator_control",
                {"action": action, "pressed": pressed},
                lambda: {"device": simulator.control(action, pressed)},
            )

    return mcp
