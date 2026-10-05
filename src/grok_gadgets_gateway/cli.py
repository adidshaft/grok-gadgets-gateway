"""Local entry points. MCP stdio reserves stdout for protocol messages."""

import argparse
import asyncio
import json
import sys

from .domain import Gateway
from .mcp_server import make_server
from .operator import (
    OperatorError,
    config_dir,
    credentials_path,
    device_ids,
    enroll,
    init_config,
    mcp_token_path,
    read_mcp_token,
    revoke,
    rotate_mcp_token,
)
from .service import MCP_PATH, serve_gateway
from .simulator import Simulator
from .simulator_config import SimulatorConfigError, load_config
from .transport import CredentialError, Credentials, DeviceServer

COMMANDS = ("init", "enroll", "revoke", "devices", "rotate-mcp-token", "serve")


def resolve_credentials(explicit):
    """Use an explicit registry, otherwise the XDG file when it already exists."""
    if explicit:
        return explicit
    path = credentials_path()
    if path.is_file():
        return str(path)
    return None


def _fail(parser, message):
    parser.error(message)


def stdio_main(argv):
    parser = argparse.ArgumentParser(
        description=(
            "Grok Gadgets local MCP gateway. Commands: init, enroll, revoke, devices, "
            "rotate-mcp-token, serve. With no command, run the stdio MCP server."
        )
    )
    parser.add_argument("--simulator", action="store_true", help="Enable explicit software C124")
    parser.add_argument("--simulator-config", help="Local bounded simulator JSON settings")
    parser.add_argument(
        "--test-controls", action="store_true", help="Expose test-only MCP controls"
    )
    parser.add_argument(
        "--credentials", help="Private mode-0600 per-device JSON credential registry"
    )
    parser.add_argument("--device-port", type=int, default=8765)
    args = parser.parse_args(argv)
    if args.test_controls and not args.simulator:
        _fail(parser, "--test-controls requires --simulator")
    if args.simulator_config and not args.simulator:
        _fail(parser, "--simulator-config requires --simulator")
    try:
        config = load_config(args.simulator_config) if args.simulator_config else None
    except SimulatorConfigError as exc:
        _fail(parser, str(exc))
    credential_path = resolve_credentials(args.credentials)
    credentials = Credentials(credential_path) if credential_path else None
    if credentials:
        try:
            credentials.validate()
        except CredentialError as exc:
            _fail(parser, f"{exc}: {credential_path}")
    gateway = Gateway()
    simulator = Simulator(gateway, config=config) if args.simulator else None
    device_server = (
        DeviceServer(
            gateway,
            credentials,
            port=args.device_port,
            reserved_ids=[simulator.device_id] if simulator else [],
        )
        if credentials
        else None
    )
    server = make_server(gateway, simulator, device_server, args.test_controls)
    asyncio.run(server.run_stdio_async())


def _credential_argument(parser):
    parser.add_argument(
        "--credentials",
        help=f"Private registry (default {credentials_path()} when that file exists)",
    )


def client_settings(port, show_token):
    """Copy-paste MCP client JSON. The bearer token is included only with show_token."""
    stdio = {
        "mcpServers": {
            "grok-gadgets": {
                "command": "grok-gadgets-gateway",
                "args": ["--simulator"],
            }
        }
    }
    url = f"http://127.0.0.1:{port}{MCP_PATH}"
    header = "Bearer <mcp-token>"
    if show_token:
        header = "Bearer " + read_mcp_token(mcp_token_path())
    remote = {"mcpServers": {"grok-gadgets": {"url": url, "headers": {"Authorization": header}}}}
    body = json.dumps(stdio, indent=2) + "\n\n" + json.dumps(remote, indent=2) + "\n"
    return body


def command_main(argv):
    parser = argparse.ArgumentParser(prog=f"grok-gadgets-gateway {argv[0]}")
    command = argv[0]
    if command == "init":
        parser.add_argument(
            "--show-token",
            action="store_true",
            help="Print the MCP bearer token once inside the HTTP client JSON",
        )
        parser.add_argument(
            "--port",
            type=int,
            default=8766,
            help="Port written into the HTTP client URL (default 8766)",
        )
        args = parser.parse_args(argv[1:])
        created = init_config()
        print(f"Config directory {config_dir()}", file=sys.stderr)
        if created:
            print("Created " + ", ".join(str(path) for path in created), file=sys.stderr)
        else:
            print("Existing files kept", file=sys.stderr)
        print(
            "Stdout is copy-paste stdio JSON, then HTTP JSON. "
            "The MCP token stays in the file unless you passed --show-token.",
            file=sys.stderr,
        )
        try:
            settings = client_settings(args.port, args.show_token)
        except CredentialError as exc:
            _fail(parser, str(exc))
        print(settings, end="")
        return 0
    if command == "enroll":
        parser.add_argument("device_id")
        _credential_argument(parser)
        args = parser.parse_args(argv[1:])
        try:
            token = enroll(args.device_id, args.credentials)
        except (OperatorError, CredentialError) as exc:
            _fail(parser, str(exc))
        print(f"GROK_GADGETS_DEVICE_TOKEN={token}")
        print(
            "Give this token to the device once. It is stored mode 0600 and will not be shown again.",
            file=sys.stderr,
        )
        return 0
    if command == "revoke":
        parser.add_argument("device_id")
        _credential_argument(parser)
        args = parser.parse_args(argv[1:])
        try:
            revoke(args.device_id, args.credentials)
        except (OperatorError, CredentialError) as exc:
            _fail(parser, str(exc))
        print(f"Revoked {args.device_id}", file=sys.stderr)
        return 0
    if command == "devices":
        _credential_argument(parser)
        args = parser.parse_args(argv[1:])
        try:
            ids = device_ids(args.credentials)
        except (OperatorError, CredentialError) as exc:
            _fail(parser, str(exc))
        for device_id in ids:
            print(device_id)
        return 0
    if command == "rotate-mcp-token":
        parser.add_argument("--mcp-token", help=f"Token file (default {mcp_token_path()})")
        args = parser.parse_args(argv[1:])
        token = rotate_mcp_token(args.mcp_token)
        print(f"GROK_GADGETS_MCP_TOKEN={token}")
        print(
            "Restart serve if it is running. Send this bearer token once; it will not be shown again.",
            file=sys.stderr,
        )
        return 0
    if command == "serve":
        parser.add_argument(
            "--simulator", action="store_true", help="Enable explicit software C124"
        )
        parser.add_argument("--simulator-config", help="Local bounded simulator JSON settings")
        _credential_argument(parser)
        parser.add_argument(
            "--device-port", type=int, default=8765, help="Loopback device port (default 8765)"
        )
        parser.add_argument(
            "--port", type=int, default=8766, help="Loopback MCP HTTP port (default 8766)"
        )
        parser.add_argument("--mcp-token", help=f"Bearer token file (default {mcp_token_path()})")
        parser.add_argument(
            "--allowed-host",
            action="append",
            default=[],
            help="Extra Host header allowed for a tunnel in front of 127.0.0.1",
        )
        args = parser.parse_args(argv[1:])
        credential_path = args.credentials or str(credentials_path())
        token_path = args.mcp_token or str(mcp_token_path())
        try:
            asyncio.run(
                _serve(
                    credential_path,
                    token_path,
                    simulator=args.simulator,
                    simulator_config=args.simulator_config,
                    device_port=args.device_port,
                    port=args.port,
                    allowed_hosts=args.allowed_host,
                )
            )
        except OperatorError as exc:
            _fail(parser, str(exc))
        except KeyboardInterrupt:
            return 0
        return 0
    _fail(parser, f"Unknown command {command}")
    return 2


async def _serve(credentials, mcp_token, **kwargs):
    async with serve_gateway(credentials, mcp_token, **kwargs) as running:
        print(
            f"MCP {running.url} on 127.0.0.1; device listener 127.0.0.1:{running.device_port}. "
            "Bearer token is the mcp-token file. This process does not prove Grok or hardware.",
            file=sys.stderr,
        )
        await running.wait()


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in COMMANDS:
        return command_main(args) or 0
    stdio_main(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
