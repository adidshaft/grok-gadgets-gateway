import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def acceptance(test_controls=True):
    args = ["-m", "grok_gadgets_gateway.cli", "--simulator"]
    if test_controls:
        args.append("--test-controls")
    params = StdioServerParameters(command=sys.executable, args=args)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            tools = (await client.list_tools()).tools
            names = {tool.name for tool in tools}
            assert ("test_simulator_control" in names) is test_controls
            assert {
                "gadgets_list_devices",
                "gadgets_get_state",
                "gadgets_command",
                "gadgets_read_events",
                "gadgets_diagnostics",
                "gadgets_command_status",
            } <= names

            async def call(name, arguments=None):
                result = await client.call_tool(name, arguments or {})
                assert not result.isError, result.content
                value = result.structuredContent
                if value is None:
                    value = json.loads(result.content[0].text)
                return value

            listing = await call("gadgets_list_devices")
            dev = listing["devices"][0]
            assert dev["simulated"] and dev["available"]
            assert dev["capability_contracts"]["rgb.set"]["required"] == ["r", "g", "b", "on"]
            assert dev["command_capabilities"] == ["rgb.set"]
            assert dev["event_capabilities"] == ["button"]
            assert set(dev["capability_contracts"]) == {"rgb.set"}
            request = {
                "device_id": "sim-c124",
                "capability": "rgb.set",
                "arguments": {"r": 0, "g": 255, "b": 0, "on": True},
                "command_id": "mcp-green",
            }
            result = await call("gadgets_command", request)
            assert result["command"]["status"] == "executed"
            assert result["command"]["simulated"] and not result["command"]["physical_verified"]
            assert (await call("gadgets_get_state", {"device_id": "sim-c124"}))["device"]["state"][
                "rgb"
            ]["g"] == 255
            duplicate = await call("gadgets_command", request)
            assert duplicate["command"] == result["command"]
            assert (
                await call(
                    "gadgets_command",
                    {
                        **request,
                        "command_id": "bad",
                        "arguments": {"r": 300, "g": 0, "b": 0, "on": True},
                    },
                )
            )["error"]["code"] == "invalid_arguments"
            rejected = []
            state_before = (await call("gadgets_get_state", {"device_id": "sim-c124"}))["device"][
                "state"
            ]
            for capability in ("button", "state", "unknown"):
                denial = await call(
                    "gadgets_command",
                    {
                        **request,
                        "capability": capability,
                        "command_id": "deny-" + capability,
                        "arguments": {"r": 1, "g": 2, "b": 3, "on": True},
                    },
                )
                assert denial["error"]["code"] == "unsupported_capability"
                status = await call("gadgets_command_status", {"command_id": "deny-" + capability})
                assert status["error"]["code"] == "unknown_command"
                rejected.append(capability)
            for index, arguments in enumerate(
                ({}, {"r": True, "g": 0, "b": 0, "on": True}, {**request["arguments"], "extra": 1})
            ):
                denial = await call(
                    "gadgets_command",
                    {
                        **request,
                        "arguments": arguments,
                        "command_id": f"malformed-{index}",
                    },
                )
                assert denial["error"]["code"] == "invalid_arguments"
            assert (await call("gadgets_get_state", {"device_id": "sim-c124"}))["device"][
                "state"
            ] == state_before
            if test_controls:
                await call("test_simulator_control", {"action": "button", "pressed": True})
                await call("test_simulator_control", {"action": "button", "pressed": False})
                events = await call("gadgets_read_events")
                assert [e["data"]["pressed"] for e in events["events"]] == [True, False]
                assert (await call("gadgets_read_events", {"cursor": events["next_cursor"]}))[
                    "events"
                ] == []
                await call("test_simulator_control", {"action": "disconnect"})
                offline = await call("gadgets_command", {**request, "command_id": "offline"})
                assert offline["error"]["code"] == "unavailable"
                await call("test_simulator_control", {"action": "reconnect"})
                recovered = await call("gadgets_command", {**request, "command_id": "recovered"})
                assert recovered["command"]["status"] == "executed"
            assert (await call("gadgets_diagnostics"))["report"]["credentials"] == "redacted"
            return {
                "tools": sorted(names),
                "rejected_command_capabilities": rejected,
                "malformed_rgb_rejections": 4,
                "led_status": result["command"]["status"],
                "simulated": True,
                "physical_verified": False,
                "grok_verified": False,
            }


def main():
    report = asyncio.run(acceptance())
    report["evidence"] = "Official MCP ClientSession over stdio; asserted simulated lifecycle"
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
