"""Rehearse a Grok Bot session against a running `serve`: the same six tools, called locally."""

import asyncio
import json

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

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
    if result.isError:
        raise RehearsalError(f"{tool} returned an MCP error")
    value = result.structuredContent
    if value is None:
        value = json.loads(result.content[0].text)
    if not value.get("ok"):
        raise RehearsalError(f"{tool}: {value['error']['code']}: {value['error']['message']}")
    return value


async def rehearse(port, token, device_id=None, out=print):
    """Call the tools Grok Bot will call. Returns the final state of the commanded gadget."""
    try:
        return await _rehearse(port, token, device_id, out)
    except Exception as exc:  # The MCP client wraps step failures in exception groups.
        failure = _find(exc, RehearsalError)
        if failure is None or failure is exc:
            raise
        raise failure from None


async def _rehearse(port, token, device_id, out):
    url = f"http://127.0.0.1:{port}{MCP_PATH}"
    headers = {"Authorization": "Bearer " + token}
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
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
            if device_id is None:
                with_rgb = [d for d in devices if "rgb.set" in d["command_capabilities"]]
                if not with_rgb:
                    raise RehearsalError("no gadget offers rgb.set; start serve with --simulator")
                device_id = with_rgb[0]["device_id"]

            command = (
                await _call(
                    session,
                    "gadgets_command",
                    {"device_id": device_id, "capability": "rgb.set", "arguments": BLUE},
                )
            )["command"]
            out(f"ok  gadgets_command rgb.set blue -> {command['status']}")
            if command["status"] != "executed":
                raise RehearsalError(f"command status is {command['status']}, not executed")

            state = (await _call(session, "gadgets_get_state", {"device_id": device_id}))["device"]
            out(f"ok  gadgets_get_state {device_id}: {json.dumps(state['state'].get('rgb'))}")
            return state


def _find(exc, kind):
    if isinstance(exc, kind):
        return exc
    for inner in getattr(exc, "exceptions", ()):
        found = _find(inner, kind)
        if found is not None:
            return found
    return None


def main(port, token_path_text, device_id, read_token):
    token = read_token(token_path_text)
    try:
        asyncio.run(rehearse(port, token, device_id))
    except Exception as exc:
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
