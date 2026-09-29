"""Grok-facing tool adapter implemented with the official MCP Python SDK."""

from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP

from .protocol import GatewayError


def make_server(gateway, simulator=None, device_server=None, test_controls=False):
    @asynccontextmanager
    async def lifespan(server):
        if device_server:
            await device_server.start()
        try:
            yield {}
        finally:
            if device_server:
                await device_server.close()

    mcp = FastMCP("Grok Gadgets local alpha", lifespan=lifespan)

    def result(action):
        try:
            return {"ok": True, **action()}
        except GatewayError as exc:
            return exc.response()

    @mcp.tool()
    def gadgets_list_devices() -> dict:
        """Discover devices, capability schemas, availability and simulation labels."""
        return {"ok": True, "devices": gateway.list_devices()}

    @mcp.tool()
    def gadgets_get_state(device_id: str) -> dict:
        """Read reported state and freshness; physical effect is not verified."""
        return result(lambda: {"device": gateway.state(device_id)})

    @mcp.tool()
    def gadgets_command(device_id: str, capability: str, arguments: dict, command_id: str) -> dict:
        """Request a device action with a stable retry ID; distinguish accepted from executed."""

        def invoke():
            value = gateway.command(device_id, capability, arguments, command_id)
            if simulator and device_id == simulator.device_id and value["status"] == "accepted":
                simulator.execute()
            return {"command": gateway.command_status(command_id)}

        return result(invoke)

    @mcp.tool()
    def gadgets_command_status(command_id: str) -> dict:
        """Read execution acknowledgement; timed_out/unconfirmed require human recovery."""
        return result(lambda: {"command": gateway.command_status(command_id)})

    @mcp.tool()
    def gadgets_read_events(
        cursor: str | None = None, device_id: str | None = None, limit: int = 32
    ) -> dict:
        """Read bounded ordered event history; null cursor recovers explicit history loss."""
        return result(lambda: gateway.read_events(cursor, device_id, limit))

    @mcp.tool()
    def gadgets_diagnostics() -> dict:
        """Get an allowlisted support report without tokens, state, arguments or raw errors."""
        return {"ok": True, "report": gateway.diagnostics()}

    if test_controls:
        if simulator is None:
            raise ValueError("Test controls require explicit simulator")

        @mcp.tool()
        def test_simulator_control(action: str, pressed: bool | None = None) -> dict:
            """TEST ONLY: inject a simulated button edge or disconnect/reconnect."""
            return result(lambda: {"device": simulator.control(action, pressed)})

    return mcp
