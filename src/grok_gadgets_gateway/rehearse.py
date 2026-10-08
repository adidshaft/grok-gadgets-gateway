"""Rehearse a Grok Bot session against a running `serve`: the same six tools, called locally."""

import asyncio
import json

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

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


async def rehearse(port, token, device_id=None, out=print, capability=None, arguments=None):
    """Call the tools Grok Bot will call. Returns the final state of the commanded gadget."""
    try:
        return await _rehearse(port, token, device_id, out, capability, arguments)
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


async def _rehearse(port, token, device_id, out, capability, arguments):
    url = f"http://127.0.0.1:{port}{MCP_PATH}"
    headers = {"Authorization": "Bearer " + token}
    # The MCP SDK's default timeouts: 30 s per request, 300 s for the event stream.
    timeout = httpx2.Timeout(30.0, read=300.0)
    async with (
        httpx2.AsyncClient(headers=headers, timeout=timeout) as http,
        streamable_http_client(url, http_client=http) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        names = {tool.name for tool in (await session.list_tools()).tools}
        if not TOOLS <= names:
            raise RehearsalError("the gateway does not offer the six gadgets tools")
        out(f"ok  {url} offers the six tools Grok Bot will use")

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


def main(port, token_path_text, device_id, read_token, capability=None, arguments=None):
    token = read_token(token_path_text)
    try:
        asyncio.run(rehearse(port, token, device_id, print, capability, arguments))
    except Exception as exc:  # noqa: BLE001 - any failure becomes one plain message
        failure = _find(exc, RehearsalError)
        if failure is None:
            failure = f"no authenticated gateway answers on 127.0.0.1:{port}; start `serve` first"
        print(f"Rehearsal failed: {failure}")
        return 1
    print(
        "Rehearsal passed: these are the calls Grok Bot will make. It does not prove a Grok Bot "
        "connection or any physical effect."
    )
    return 0
