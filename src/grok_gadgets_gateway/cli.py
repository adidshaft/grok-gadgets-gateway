"""Local entry points. MCP stdout is reserved exclusively for protocol messages."""

import argparse
import asyncio

from .domain import Gateway
from .mcp_server import make_server
from .simulator import Simulator
from .simulator_config import SimulatorConfigError, load_config
from .transport import Credentials, DeviceServer


def main():
    parser = argparse.ArgumentParser(description="Grok Gadgets local MCP gateway")
    parser.add_argument("--simulator", action="store_true", help="Enable explicit software C124")
    parser.add_argument("--simulator-config", help="Local bounded simulator JSON settings")
    parser.add_argument(
        "--test-controls", action="store_true", help="Expose test-only MCP controls"
    )
    parser.add_argument(
        "--credentials", help="Private mode-0600 per-device JSON credential registry"
    )
    parser.add_argument("--device-port", type=int, default=8765)
    args = parser.parse_args()
    if args.test_controls and not args.simulator:
        parser.error("--test-controls requires --simulator")
    if args.simulator_config and not args.simulator:
        parser.error("--simulator-config requires --simulator")
    try:
        config = load_config(args.simulator_config) if args.simulator_config else None
    except SimulatorConfigError as exc:
        parser.error(str(exc))
    gateway = Gateway()
    simulator = Simulator(gateway, config=config) if args.simulator else None
    device_server = (
        DeviceServer(gateway, Credentials(args.credentials), port=args.device_port)
        if args.credentials
        else None
    )
    server = make_server(gateway, simulator, device_server, args.test_controls)
    asyncio.run(server.run_stdio_async())


if __name__ == "__main__":
    main()
