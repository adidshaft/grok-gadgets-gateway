"""Local entry points. MCP stdio reserves stdout for protocol messages."""

import argparse
import asyncio
import json
import os
import signal
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
    write_token_file,
)
from .service import MCP_PATH, serve_gateway
from .simulator import Simulator
from .simulator_config import SimulatorConfigError, load_config
from .transport import CredentialError, Credentials, DeviceServer

COMMANDS = (
    "init",
    "enroll",
    "revoke",
    "devices",
    "rotate-mcp-token",
    "serve",
    "stdio",
    "usb-bridge",
    "rehearse",
)
DESCRIPTION = """Grok Gadgets gateway: the MCP server your Grok Bot will use to reach your gadgets.

Start here:
  grok-gadgets-gateway init                 create the config and print connector settings
  grok-gadgets-gateway serve --simulator    run HTTP MCP on 127.0.0.1:8766 with a simulated light
  grok-gadgets-gateway rehearse             call the six tools exactly as Grok Bot will

`stdio` runs the same server for a connector that starts the gateway itself."""


def resolve_credentials(explicit):
    """Use an explicit registry, otherwise the XDG file when it already exists."""
    if explicit:
        return explicit
    path = credentials_path()
    if path.is_file():
        return str(path)
    return None


def executable():
    """Absolute command that starts this gateway, for pasteable connector settings."""
    script = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if os.path.basename(script) == "grok-gadgets-gateway" and os.path.isfile(script):
        return script, []
    # Never resolve symlinks: a virtualenv python must stay inside its virtualenv.
    return os.path.abspath(sys.executable), ["-m", "grok_gadgets_gateway.cli"]


def stdio_settings():
    command, prefix = executable()
    entry = {"command": command, "args": [*prefix, "stdio", "--simulator"]}
    return {"mcpServers": {"grok-gadgets": entry}}


def http_settings(port, show_token):
    header = "Bearer <contents of " + str(mcp_token_path()) + ">"
    if show_token:
        header = "Bearer " + read_mcp_token(mcp_token_path())
    url = f"http://127.0.0.1:{port}{MCP_PATH}"
    return {"mcpServers": {"grok-gadgets": {"url": url, "headers": {"Authorization": header}}}}


def client_settings(port, show_token, client="both"):
    """Pasteable MCP connector JSON: one complete block per connection mode."""
    blocks = []
    if client in ("both", "stdio"):
        blocks.append(
            ("# The connector starts the gateway (stdio, simulated light):", stdio_settings())
        )
    if client in ("both", "http"):
        blocks.append(
            (
                "# The connector calls a running `grok-gadgets-gateway serve`:",
                http_settings(port, show_token),
            )
        )
    if client != "both":
        return json.dumps(blocks[0][1], indent=2) + "\n"
    return "\n".join(title + "\n" + json.dumps(value, indent=2) + "\n" for title, value in blocks)


def _credentials_option(parser):
    parser.add_argument(
        "--credentials",
        help=f"Private registry (default {credentials_path()} when that file exists)",
    )


def _simulator_options(parser):
    parser.add_argument("--simulator", action="store_true", help="Add the software C124 light")
    parser.add_argument("--simulator-config", help="Local bounded simulator JSON settings")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="grok-gadgets-gateway",
        description=DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    init = sub.add_parser("init", help="create the config and print connector settings")
    init.add_argument(
        "--client",
        choices=["both", "stdio", "http"],
        default="both",
        help="print settings for one connection mode only (plain JSON)",
    )
    init.add_argument(
        "--show-token",
        action="store_true",
        help="print the MCP bearer token inside the HTTP settings",
    )
    init.add_argument("--port", type=int, default=8766, help="HTTP port in the settings")

    enroll_cmd = sub.add_parser("enroll", help="issue a device token for a device ID")
    enroll_cmd.add_argument("device_id")
    enroll_cmd.add_argument(
        "--rotate",
        action="store_true",
        help="replace the token of an enrolled device; it keeps its identity",
    )
    enroll_cmd.add_argument(
        "--token-file",
        help="write the token to this mode-0600 file instead of printing it "
        "(pass the same file to the agent's --token-file)",
    )
    _credentials_option(enroll_cmd)
    revoke_cmd = sub.add_parser("revoke", help="revoke a device token")
    revoke_cmd.add_argument("device_id")
    _credentials_option(revoke_cmd)
    devices = sub.add_parser("devices", help="list enrolled device IDs")
    _credentials_option(devices)
    rotate = sub.add_parser("rotate-mcp-token", help="replace the MCP bearer token")
    rotate.add_argument("--mcp-token", help=f"Token file (default {mcp_token_path()})")

    serve = sub.add_parser("serve", help="run authenticated HTTP MCP and the device listener")
    _simulator_options(serve)
    _credentials_option(serve)
    serve.add_argument(
        "--device-port", type=int, default=8765, help="loopback device port (default 8765)"
    )
    serve.add_argument(
        "--port", type=int, default=8766, help="loopback MCP HTTP port (default 8766)"
    )
    serve.add_argument("--mcp-token", help=f"bearer token file (default {mcp_token_path()})")
    serve.add_argument(
        "--allowed-host",
        action="append",
        default=[],
        help="extra Host header allowed for a tunnel in front of 127.0.0.1",
    )
    serve.add_argument(
        "--request-log",
        metavar="PATH",
        help="append one JSON line per tool call to this mode-0600 file (no tokens or secrets)",
    )

    stdio = sub.add_parser("stdio", help="run MCP over stdin/stdout for a connector that starts it")
    _simulator_options(stdio)
    stdio.add_argument("--test-controls", action="store_true", help="expose test-only MCP controls")
    _credentials_option(stdio)
    stdio.add_argument("--device-port", type=int, default=8765)

    bridge = sub.add_parser("usb-bridge", help="connect a USB serial device to the gateway")
    bridge.add_argument("serial_port")
    bridge.add_argument("--port", type=int, default=8765, help="gateway device port")

    rehearse = sub.add_parser(
        "rehearse", help="call the six tools against a running serve, as Grok Bot will"
    )
    rehearse.add_argument("--port", type=int, default=8766, help="MCP HTTP port (default 8766)")
    rehearse.add_argument("--mcp-token", help=f"bearer token file (default {mcp_token_path()})")
    rehearse.add_argument("--device", help="gadget to command (default: first with the command)")
    rehearse.add_argument(
        "--command", dest="capability", help="command to call (default rgb.set, set to blue)"
    )
    rehearse.add_argument("--args", help="JSON arguments for --command, e.g. '{\"on\": true}'")
    return parser


def run_stdio(parser, args):
    if args.test_controls and not args.simulator:
        parser.error("--test-controls requires --simulator")
    if args.simulator_config and not args.simulator:
        parser.error("--simulator-config requires --simulator")
    try:
        config = load_config(args.simulator_config) if args.simulator_config else None
    except SimulatorConfigError as exc:
        parser.error(str(exc))
    credential_path = resolve_credentials(args.credentials)
    credentials = Credentials(credential_path) if credential_path else None
    if credentials:
        try:
            credentials.validate()
        except CredentialError as exc:
            parser.error(f"{exc}: {credential_path}")
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
    return 0


def run(parser, args):
    command = args.command
    if command == "init":
        created = init_config()
        print(f"Config directory {config_dir()}", file=sys.stderr)
        if created:
            print("Created " + ", ".join(str(path) for path in created), file=sys.stderr)
        else:
            print("Existing files kept", file=sys.stderr)
        print(
            "Paste one block into your MCP connector. The MCP token stays in its file "
            "unless you pass --show-token. Next: grok-gadgets-gateway serve --simulator",
            file=sys.stderr,
        )
        try:
            settings = client_settings(args.port, args.show_token, args.client)
        except CredentialError as exc:
            parser.error(str(exc))
        print(settings, end="")
        return 0
    if command == "enroll":
        try:
            token = enroll(args.device_id, args.credentials, rotate=args.rotate)
            if args.token_file:
                write_token_file(args.token_file, token)
        except (OperatorError, CredentialError, OSError) as exc:
            parser.error(str(exc))
        if args.rotate:
            print(
                f"New token for {args.device_id}; its old token stops working now.",
                file=sys.stderr,
            )
        if args.token_file:
            print(f"Wrote the token to {args.token_file} (mode 0600).", file=sys.stderr)
            return 0
        print(f"GROK_GADGETS_DEVICE_TOKEN={token}")
        print(
            "Give this token to the device once. It is stored mode 0600 and will not be shown again.",
            file=sys.stderr,
        )
        return 0
    if command == "revoke":
        try:
            revoke(args.device_id, args.credentials)
        except (OperatorError, CredentialError) as exc:
            parser.error(str(exc))
        print(f"Revoked {args.device_id}", file=sys.stderr)
        return 0
    if command == "devices":
        try:
            ids = device_ids(args.credentials)
        except (OperatorError, CredentialError) as exc:
            parser.error(str(exc))
        for device_id in ids:
            print(device_id)
        return 0
    if command == "rotate-mcp-token":
        token = rotate_mcp_token(args.mcp_token)
        print(f"GROK_GADGETS_MCP_TOKEN={token}")
        print(
            "Update the client's bearer token and reconnect. No gateway restart is needed.",
            file=sys.stderr,
        )
        return 0
    if command == "serve":
        try:
            asyncio.run(
                _serve(
                    args.credentials or str(credentials_path()),
                    args.mcp_token or str(mcp_token_path()),
                    simulator=args.simulator,
                    simulator_config=args.simulator_config,
                    device_port=args.device_port,
                    port=args.port,
                    allowed_hosts=args.allowed_host,
                    request_log=args.request_log,
                )
            )
        except OperatorError as exc:
            parser.error(str(exc))
        except KeyboardInterrupt:
            return 0
        return 0
    if command == "usb-bridge":
        from .usb_bridge import bridge

        token = os.environ.get("GROK_GADGETS_DEVICE_TOKEN")
        if not token:
            parser.error("Set GROK_GADGETS_DEVICE_TOKEN (from enroll) in the environment")
        asyncio.run(bridge(args.serial_port, token, port=args.port))
        return 0
    if command == "rehearse":
        from .rehearse import main as rehearse_main

        def token(path):
            try:
                return read_mcp_token(path)
            except CredentialError as exc:
                parser.error(f"{exc}; run grok-gadgets-gateway init first")

        try:
            arguments = json.loads(args.args) if args.args is not None else None
        except ValueError:
            parser.error("--args must be a JSON object")
        if arguments is not None and not isinstance(arguments, dict):
            parser.error("--args must be a JSON object")
        return rehearse_main(
            args.port,
            args.mcp_token or mcp_token_path(),
            args.device,
            token,
            args.capability,
            arguments,
        )
    return run_stdio(parser, args)


async def _serve(credentials, mcp_token, **kwargs):
    # SIGTERM (launchd, systemd) and Ctrl+C both close devices and HTTP cleanly.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for number in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(number, stop.set)
        except (NotImplementedError, RuntimeError):  # Windows: Ctrl+C still raises
            pass
    async with serve_gateway(credentials, mcp_token, **kwargs) as running:
        print(
            f"MCP {running.url} on 127.0.0.1; device listener 127.0.0.1:{running.device_port}. "
            "Bearer token is the mcp-token file. This process does not prove Grok or hardware.",
            file=sys.stderr,
        )
        stopping = asyncio.create_task(stop.wait())
        await asyncio.wait({running.task, stopping}, return_when=asyncio.FIRST_COMPLETED)
        stopping.cancel()
    print("Gateway stopped; device sessions closed.", file=sys.stderr)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if not argv:
        if sys.stdin.isatty():
            # A person typed the bare command: show the way in instead of a silent server.
            parser.print_help(sys.stderr)
            return 2
        argv = ["stdio"]  # Spawned by a connector with no arguments: stdio, as before.
    elif argv[0].startswith("-") and argv[0] not in ("-h", "--help"):
        argv = ["stdio", *argv]  # Older client settings: grok-gadgets-gateway --simulator
    args = parser.parse_args(argv)
    if args.command == "stdio":
        return run_stdio(parser, args)
    return run(parser, args)


if __name__ == "__main__":
    raise SystemExit(main())
