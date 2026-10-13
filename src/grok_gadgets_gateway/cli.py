"""Local entry points. MCP stdio reserves stdout for protocol messages."""

import argparse
import asyncio
import json
import os
import signal
import sys
from importlib.metadata import version

from .domain import Gateway
from .mcp_server import RateLimiter, RequestLog, make_server
from .operator import (
    OperatorError,
    config_dir,
    credentials_path,
    device_ids,
    enroll,
    init_config,
    issue_mcp_token,
    list_mcp_tokens,
    mcp_token_path,
    mcp_tokens_path,
    read_mcp_token,
    remote_enabled,
    remote_off_path,
    revoke,
    revoke_mcp_token,
    rotate_mcp_token,
    set_remote,
    write_token_file,
)
from .service import MCP_PATH, Limits, gateway_version, serve_gateway
from .simulator import Simulator
from .simulator_config import SimulatorConfigError, load_config
from .transport import CredentialError, Credentials, DevicePortInUse, DeviceServer

COMMANDS = (
    "init",
    "enroll",
    "revoke",
    "devices",
    "rotate-mcp-token",
    "mcp-token",
    "remote",
    "oauth",
    "usage",
    "serve",
    "stdio",
    "usb-bridge",
    "rehearse",
    "lan-cert",
    "lan-ca",
    "wifi-provision",
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
        help=(
            f"Private registry (default {credentials_path()} when that file exists; "
            "--simulator alone never reads it)"
        ),
    )


def _positive_int(text):
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a whole number") from None
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def _usage_hook_option(parser):
    parser.add_argument(
        "--usage-hook",
        metavar="CMD",
        help="start CMD once and write one JSON usage line per tool call to its stdin "
        "(no tokens or arguments; never delays a call). See docs/usage.md",
    )


def _start_usage_hook(parser, command):
    if not command:
        return None
    from .usage import UsageHook

    try:
        return UsageHook(command)
    except (OperatorError, ValueError) as exc:
        parser.error(f"--usage-hook: {exc}")


def _request_log_option(parser):
    parser.add_argument(
        "--request-log",
        metavar="PATH",
        help="append one JSON line per tool call to this mode-0600 file (no tokens or secrets)",
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
    parser.add_argument(
        "--version",
        action="version",
        version="grok-gadgets-gateway " + version("grok-gadgets-gateway"),
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

    tokens = sub.add_parser("mcp-token", help="issue, list or revoke named MCP bearer tokens")
    token_sub = tokens.add_subparsers(dest="token_command", metavar="ACTION", required=True)
    token_issue = token_sub.add_parser("issue", help="create a named token and show it once")
    token_issue.add_argument("name")
    token_issue.add_argument(
        "--token-file", help="write the token to this mode-0600 file instead of printing it"
    )
    token_sub.add_parser("list", help="list named tokens (never the tokens themselves)")
    token_revoke = token_sub.add_parser("revoke", help="revoke a named token at once")
    token_revoke.add_argument("name")
    for action in (token_issue, token_sub.choices["list"], token_revoke):
        action.add_argument("--mcp-tokens", help=f"token list (default {mcp_tokens_path()})")

    usage = sub.add_parser(
        "usage", help="summarize tool calls by token, device, capability and day"
    )
    usage.add_argument(
        "--log",
        action="append",
        metavar="PATH",
        help="request log to read, with its rotated files (repeatable; default: the service's)",
    )
    usage.add_argument("--since", metavar="ISO", help="from this UTC date or time")
    usage.add_argument("--until", metavar="ISO", help="up to this UTC date or time")
    usage.add_argument("--token", metavar="NAME", help="only calls with this token name")
    usage.add_argument("--json", action="store_true", help="print JSON")
    usage.add_argument(
        "--rate", type=float, metavar="N", help="estimate N units per call (needs --unit)"
    )
    usage.add_argument("--unit", metavar="TEXT", help='estimate unit, e.g. "%% weekly usage"')
    usage.add_argument(
        "--calibrate",
        type=float,
        metavar="USED",
        help="Grok Bot usage you measured over --since..--until (needs --unit); saves the rate",
    )

    oauth = sub.add_parser(
        "oauth", help="approve, list or revoke Grok Bot sign-ins for serve --oauth"
    )
    oauth_sub = oauth.add_subparsers(dest="oauth_command", required=True)
    oauth_sub.add_parser("pending", help="list sign-ins waiting for your approval")
    oauth_approve = oauth_sub.add_parser("approve", help="approve the sign-in with this code")
    oauth_approve.add_argument("code")
    oauth_approve.add_argument(
        "--name", default="grok-bot", help="name in logs and limits (default grok-bot)"
    )
    oauth_deny = oauth_sub.add_parser("deny", help="refuse the sign-in with this code")
    oauth_deny.add_argument("code")
    oauth_sub.add_parser("list", help="list live sign-ins (never the tokens)")
    oauth_revoke = oauth_sub.add_parser("revoke", help="end every sign-in with this name at once")
    oauth_revoke.add_argument("name")

    remote = sub.add_parser(
        "remote", help="kill switch: turn remote (tunnel) access off or on at once"
    )
    remote.add_argument("state", choices=["off", "on", "status"])

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
        "--mcp-tokens", help=f"named token list, read per request (default {mcp_tokens_path()})"
    )
    limits = serve.add_argument_group("per-token limits")
    limits.add_argument("--calls-per-minute", type=_positive_int, default=60, metavar="N")
    limits.add_argument("--commands-per-minute", type=_positive_int, default=10, metavar="N")
    limits.add_argument(
        "--max-concurrent",
        type=_positive_int,
        default=8,
        metavar="N",
        help="open HTTP requests per token, event streams included (default 8)",
    )
    limits.add_argument("--max-body-bytes", type=_positive_int, default=65536, metavar="N")
    limits.add_argument(
        "--request-timeout",
        type=_positive_int,
        default=30,
        metavar="SECONDS",
        help="limit for one POST request (default 30)",
    )
    serve.add_argument(
        "--allowed-host",
        action="append",
        default=[],
        help="extra Host header allowed for a tunnel in front of 127.0.0.1; "
        "needs --request-log, and remote requests need a named token",
    )
    serve.add_argument(
        "--lan-listen",
        metavar="ADDRESS[:PORT]",
        help="also run the TLS device listener on this private LAN address of this computer "
        "(port 8767 by default; off unless set; run lan-cert first)",
    )
    serve.add_argument(
        "--oauth",
        action="store_true",
        help="let Grok Bot sign in with OAuth at https://ALLOWED-HOST; you approve each sign-in "
        "with: grok-gadgets-gateway oauth approve CODE",
    )
    _request_log_option(serve)
    _usage_hook_option(serve)

    lan_cert = sub.add_parser(
        "lan-cert", help="issue the LAN listener certificate for one private address or NAME.local"
    )
    lan_cert.add_argument(
        "address",
        help="this computer's mDNS name or LAN address, e.g. my-mac.local or 192.168.1.20",
    )
    lan_ca = sub.add_parser("lan-ca", help="show or replace the local CA that devices pin")
    lan_ca.add_argument("action", choices=["show", "rotate"])

    provision = sub.add_parser(
        "wifi-provision",
        help="send Wi-Fi settings, the device token and the CA to a Wi-Fi build over USB",
    )
    provision.add_argument("serial_port")
    provision.add_argument("--device", required=True, help="enrolled device ID of this board")
    provision.add_argument(
        "--lan",
        metavar="ADDRESS[:PORT]",
        help="the address or NAME.local given to serve --lan-listen",
    )
    provision.add_argument(
        "--factory-reset", action="store_true", help="erase the board's Wi-Fi settings instead"
    )
    _credentials_option(provision)

    service = sub.add_parser(
        "service", help="run serve in the background at login: launchd (macOS) or systemd (Linux)"
    )
    service_sub = service.add_subparsers(dest="service_command", metavar="ACTION", required=True)
    service_install = service_sub.add_parser(
        "install", help="install and start the per-user service; it starts again at every login"
    )
    service_install.add_argument(
        "--lan-listen", metavar="ADDRESS[:PORT]", help="also run the Wi-Fi device listener"
    )
    service_install.add_argument("--port", type=int, default=8766, help="loopback MCP HTTP port")
    service_install.add_argument(
        "--allowed-host",
        action="append",
        default=[],
        help="tunnel Host header to allow (see serve --allowed-host)",
    )
    service_install.add_argument(
        "--oauth", action="store_true", help="let Grok Bot sign in (see serve --oauth)"
    )
    service_install.add_argument(
        "--device-port", type=int, default=8765, help="loopback device port"
    )
    _usage_hook_option(service_install)
    service_sub.add_parser("uninstall", help="stop the service and remove it; config stays")
    service_sub.add_parser("status", help="show whether the service is installed and running")
    service_logs = service_sub.add_parser("logs", help="print the end of the service log")
    service_logs.add_argument("--lines", type=int, default=40, help="lines to print")

    stdio = sub.add_parser("stdio", help="run MCP over stdin/stdout for a connector that starts it")
    _simulator_options(stdio)
    stdio.add_argument("--test-controls", action="store_true", help="expose test-only MCP controls")
    _credentials_option(stdio)
    stdio.add_argument("--device-port", type=int, default=8765)
    _request_log_option(stdio)
    _usage_hook_option(stdio)

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
    # --simulator alone is the simulated light only: it never reads the device registry.
    credential_path = args.credentials if args.simulator else resolve_credentials(args.credentials)
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
    request_log = None
    if args.request_log:
        try:
            request_log = RequestLog("stdio", path=args.request_log)
        except OSError as exc:
            parser.error(f"cannot open request log {args.request_log}: {exc.strerror}")
        request_log.append(
            {
                "event": "stdio_started",
                "gateway_version": gateway_version(),
                "simulator": simulator.device_id if simulator else None,
            }
        )
    usage_hook = _start_usage_hook(parser, args.usage_hook)
    server = make_server(
        gateway,
        simulator,
        device_server,
        args.test_controls,
        request_log=request_log,
        usage_hook=usage_hook,
    )
    try:
        asyncio.run(server.run_stdio_async())
    except BaseExceptionGroup as group:
        in_use = group.subgroup(DevicePortInUse)
        if in_use is None:
            raise
        while isinstance(in_use, BaseExceptionGroup):
            in_use = in_use.exceptions[0]
        parser.error(str(in_use))
    finally:
        hook = usage_hook.close() if usage_hook is not None else None
        if request_log is not None:
            request_log.append({"event": "stdio_stopped", "usage_hook": hook})
            request_log.close()
    return 0


def run(parser, args):
    command = args.command
    if command == "init":
        from .lan import create_ca

        created = init_config()
        ca = create_ca()
        if ca:
            created.append(ca)
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
    if command == "mcp-token":
        return run_mcp_token(parser, args)
    if command == "remote":
        return run_remote(parser, args)
    if command == "oauth":
        return run_oauth(parser, args)
    if command == "usage":
        return run_usage(parser, args)
    if command == "serve":
        usage_hook = _start_usage_hook(parser, args.usage_hook)
        try:
            asyncio.run(
                _serve(
                    # --simulator alone never reads the device registry or opens a device port.
                    args.credentials or (None if args.simulator else str(credentials_path())),
                    args.mcp_token or str(mcp_token_path()),
                    simulator=args.simulator,
                    simulator_config=args.simulator_config,
                    device_port=args.device_port,
                    port=args.port,
                    allowed_hosts=args.allowed_host,
                    mcp_tokens=args.mcp_tokens or str(mcp_tokens_path()),
                    limits=Limits(args.max_concurrent, args.max_body_bytes, args.request_timeout),
                    rate_limiter=RateLimiter(args.calls_per_minute, args.commands_per_minute),
                    request_log=args.request_log,
                    lan_listen=args.lan_listen,
                    remote_switch=str(remote_off_path()),
                    usage_hook=usage_hook,
                    oauth=args.oauth,
                )
            )
        except OperatorError as exc:
            parser.error(str(exc))
        except KeyboardInterrupt:
            return 0
        finally:
            if usage_hook is not None:
                counts = usage_hook.close()
                print(
                    f"Usage hook: {counts['sent']} lines sent, {counts['dropped']} dropped.",
                    file=sys.stderr,
                )
        return 0
    if command == "usb-bridge":
        from .usb_bridge import bridge

        token = os.environ.get("GROK_GADGETS_DEVICE_TOKEN")
        if not token:
            parser.error("Set GROK_GADGETS_DEVICE_TOKEN (from enroll) in the environment")
        asyncio.run(bridge(args.serial_port, token, port=args.port))
        return 0
    if command in ("lan-cert", "lan-ca"):
        return run_lan(parser, args)
    if command == "wifi-provision":
        return run_wifi_provision(parser, args)
    if command == "service":
        return run_service(parser, args)
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


def run_lan(parser, args):
    from .lan import ca_fingerprint, create_ca, issue_server_cert, lan_dir, parse_lan_address

    try:
        if args.command == "lan-cert":
            address, _ = parse_lan_address(args.address)
            path = issue_server_cert(address)
            print(
                f"Wrote {path} for {address}. Start: serve --lan-listen {address}", file=sys.stderr
            )
            return 0
        if args.action == "rotate":
            create_ca(rotate=True)
            print(
                "Replaced the local CA and removed the old LAN certificate. Run lan-cert, "
                "restart serve and provision every Wi-Fi device again.",
                file=sys.stderr,
            )
        print(f"{lan_dir() / 'ca.pem'}\nSHA-256 {ca_fingerprint()}")
    except OSError as exc:
        parser.error(f"cannot write the LAN files: {exc.strerror}")
    except OperatorError as exc:
        parser.error(str(exc))
    return 0


def run_wifi_provision(parser, args):
    import getpass

    import serial

    from .lan import ca_pem_text, parse_lan_address
    from .transport import read_registry
    from .wifi_provision import (
        ProvisionError,
        check_board,
        factory_reset,
        provision,
        provision_frame,
    )

    try:
        if not args.factory_reset:
            if not args.lan:
                parser.error("--lan ADDRESS[:PORT] is required to provision")
            host, port = parse_lan_address(args.lan)
            path = args.credentials or credentials_path()
            record = read_registry(path).get(args.device)
            if record is None:
                parser.error(f"{args.device} is not enrolled. Run: enroll {args.device}")
            if record.get("revoked"):
                parser.error(f"{args.device} is revoked. Run: enroll {args.device} --rotate")
            ca = ca_pem_text()
    except (OperatorError, CredentialError, ProvisionError) as exc:
        parser.error(str(exc))
    try:
        with serial.Serial(args.serial_port, 115200, timeout=0.1, write_timeout=2) as device:
            if args.factory_reset:
                factory_reset(device)
                print("Factory reset done; the board restarts unprovisioned.", file=sys.stderr)
                print(f"Also run: grok-gadgets-gateway revoke {args.device}", file=sys.stderr)
                return 0
            # Find the board before asking for the Wi-Fi name and password.
            check_board(device, args.device)
            try:
                ssid = input("Wi-Fi name (2.4 GHz): ")
                password = getpass.getpass("Wi-Fi password (not shown): ")
            except (EOFError, KeyboardInterrupt):
                print(file=sys.stderr)
                parser.error("Cancelled; nothing was sent to the board.")
            frame = provision_frame(ssid, password, record["token"], host, port, ca)
            del password
            provision(device, args.device, frame)
    except (serial.SerialException, OSError) as exc:
        parser.error(f"cannot use {args.serial_port}: {exc}")
    except ProvisionError as exc:
        parser.error(str(exc))
    print(
        f"Provisioned {args.device} for {host}:{port}. The board restarts and joins Wi-Fi; "
        "you can now power it from a USB charger.",
        file=sys.stderr,
    )
    return 0


def run_service(parser, args, service=None):
    from .background import Service, serve_arguments

    try:
        service = service or Service()
        action = args.service_command
        if action == "install":
            if not mcp_token_path().is_file():
                parser.error("the gateway is not set up. Run: grok-gadgets-gateway init")
            if args.oauth and len(args.allowed_host) != 1:
                parser.error("--oauth needs exactly one --allowed-host: the tunnel name")
            if args.usage_hook is not None:
                import shlex

                try:
                    hook = shlex.split(args.usage_hook)
                except ValueError as exc:
                    parser.error(f"--usage-hook: {exc}")
                if not hook or not os.path.isabs(hook[0]):
                    parser.error("--usage-hook: the service needs an absolute program path")
            if args.allowed_host:
                from .service import transport_security

                try:
                    transport_security(args.allowed_host)
                except ValueError as exc:
                    parser.error(str(exc))
            ports = [("127.0.0.1", args.port), ("127.0.0.1", args.device_port)]
            if args.lan_listen:
                from .lan import lan_dir, parse_lan_address, resolve_lan_host

                host, lan_port = parse_lan_address(args.lan_listen)
                if not (lan_dir() / "server.pem").is_file():
                    parser.error(f"no LAN certificate. Run: grok-gadgets-gateway lan-cert {host}")
                ports.append((resolve_lan_host(host), lan_port))
            program, prefix = executable()
            command_line = [
                program,
                *prefix,
                *serve_arguments(
                    args.lan_listen,
                    args.port,
                    args.device_port,
                    service.logs / "requests.jsonl",
                    args.allowed_host,
                    args.oauth,
                    args.usage_hook,
                ),
            ]
            environment = {
                key: os.environ[key] for key in ("XDG_CONFIG_HOME",) if os.environ.get(key)
            }
            path = service.install(command_line, environment, ports)
            print(f"Installed {path}", file=sys.stderr)
            print(
                "The gateway runs now and starts again at every login. "
                "Check it: grok-gadgets-gateway service status",
                file=sys.stderr,
            )
            if service.platform == "linux":
                print(
                    "To start it at boot without a login, run: loginctl enable-linger",
                    file=sys.stderr,
                )
            return 0
        if action == "uninstall":
            path = service.uninstall()
            print(
                f"Stopped the service and removed {path}. Config and tokens stay.", file=sys.stderr
            )
            return 0
        if action == "status":
            report = service.status()
            state = "running" if report["running"] else "stopped"
            if not report["installed"]:
                state = "not installed"
            pid = f" (pid {report['pid']})" if report["pid"] else ""
            print(f"service: {state}{pid}\nunit: {report['unit']}\nlog: {report['log']}")
            if report["installed"]:
                for line in route_status(service):
                    print(line)
            return 0 if report["running"] or not report["installed"] else 3
        lines = service.tail(args.lines)
        print(f"log: {service.logs / 'gateway.log'}", file=sys.stderr)
        for line in lines:
            print(line)
        return 0
    except OperatorError as exc:
        parser.error(str(exc))


def run_usage(parser, args):
    from .usage import (
        calibrate,
        default_log,
        format_summary,
        parse_time,
        read_calls,
        read_rate,
        summarize,
    )

    if (args.rate is not None or args.calibrate is not None) and not args.unit:
        parser.error("--rate and --calibrate need --unit")
    if args.rate is not None and args.calibrate is not None:
        parser.error("use --rate or --calibrate, not both")
    if args.calibrate is not None and not (args.since and args.until):
        parser.error("--calibrate needs --since and --until around your two readings")
    paths = args.log
    if not paths:
        found = default_log()
        if found is None or not found.is_file():
            parser.error("no service request log here; pass --log PATH")
        paths = [found]
    from .logfiles import rotated

    paths = [old for path in paths for old in rotated(path)] or paths
    try:
        since = parse_time(args.since) if args.since else None
        until = parse_time(args.until, end=True) if args.until else None
        calls = read_calls(paths, since, until, args.token)
        if args.calibrate is not None:
            rate = calibrate(calls, args.calibrate, args.unit, since, until)
            print(f"Saved the estimate rate: {rate['source']}.", file=sys.stderr)
        elif args.rate is not None:
            rate = {"rate": args.rate, "unit": args.unit}
        else:
            rate = read_rate()
    except OperatorError as exc:
        parser.error(str(exc))
    summary = summarize(calls, rate)
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(format_summary(summary))
    return 0


def run_oauth(parser, args):
    from datetime import UTC, datetime

    from .oauth import approve, pending_requests, revoke_name, sign_ins

    def when(seconds):
        return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%d %H:%M UTC")

    try:
        action = args.oauth_command
        if action == "pending":
            requests = pending_requests()
            for request in requests:
                print(
                    f"{request['code']}\t{request.get('client_name') or '-'}\t"
                    f"{request.get('redirect_host') or '-'}\texpires {when(request['expires_at'])}"
                )
            if not requests:
                print("No sign-in is waiting.", file=sys.stderr)
        elif action == "approve":
            code = approve(args.code, args.name)
            print(
                f"Approved {code} as {args.name}. The sign-in page continues within seconds.",
                file=sys.stderr,
            )
        elif action == "deny":
            code = approve(args.code, None, deny=True)
            print(f"Refused {code}.", file=sys.stderr)
        elif action == "list":
            for name, client, expires in sign_ins():
                print(f"{name}\t{client}\trefresh expires {when(expires)}")
        else:
            revoke_name(args.name)
            print(f"Ended every {args.name} sign-in; its tokens fail now.", file=sys.stderr)
    except (OperatorError, OSError) as exc:
        parser.error(str(exc))
    return 0


def route_status(service, now=None):
    """Lines for `service status` about the Grok Bot route; never a token or a code."""
    from datetime import UTC, datetime, timedelta

    from .background import option_values
    from .logfiles import rotated
    from .oauth import sign_ins
    from .usage import last_record

    command = service.unit_command()
    hosts = option_values(command, "--allowed-host")
    logs = option_values(command, "--request-log") or [service.logs / "requests.jsonl"]
    paths = [old for path in logs for old in rotated(path)]
    lines = [
        f"allowed host: {', '.join(hosts) if hosts else 'none (no tunnel route)'}",
        f"sign-in (--oauth): {'on' if '--oauth' in command else 'off'}",
        f"remote: {'on' if remote_enabled() else 'off (kill switch)'}",
    ]
    try:
        live = sign_ins()
    except OperatorError as exc:
        live, lines = [], [*lines, f"live sign-ins: unreadable ({exc})"]
    else:
        names = [
            f"{name} (until {datetime.fromtimestamp(expires, UTC):%Y-%m-%d})"
            for name, _, expires in live
        ]
        lines.append(f"live sign-ins: {', '.join(names) if names else 'none'}")
    call = last_record(
        paths, lambda r: r.get("event") == "mcp_tool_call" and r.get("remote") is True
    )
    if call:
        lines.append(
            f"last remote call: {call.get('ts', '-')} {call.get('tool', '-')} "
            f"{call.get('outcome', '-')} as {call.get('token') or '-'}"
        )
    else:
        lines.append("last remote call: none in the request log")
    flood = last_record(paths, lambda r: r.get("event") == "oauth_flood")
    now = now or datetime.now(UTC)
    try:
        recent = flood and now - datetime.fromisoformat(flood["ts"]) < timedelta(days=1)
    except (KeyError, TypeError, ValueError):
        recent = False
    if recent:
        lines.append(f"sign-in flood: {flood['ts']} {flood.get('reason', '-')}")
    else:
        lines.append("sign-in flood: none in the last 24 hours")
    return lines


def run_remote(parser, args):
    try:
        if args.state != "status":
            set_remote(args.state == "on")
    except OSError as exc:
        parser.error(f"cannot change {remote_off_path()}: {exc.strerror}")
    if remote_enabled():
        print("remote: on (requests through --allowed-host need a named token)")
    else:
        print("remote: off (every remote request gets 503; loopback and devices still work)")
    return 0


def run_mcp_token(parser, args):
    path = args.mcp_tokens or mcp_tokens_path()
    try:
        if args.token_command == "list":
            for name, created, revoked in list_mcp_tokens(path):
                print(f"{name}\t{created or '-'}\t{'revoked' if revoked else 'active'}")
            return 0
        if args.token_command == "revoke":
            revoke_mcp_token(args.name, path)
            print(f"Revoked {args.name}; requests with it fail now.", file=sys.stderr)
            return 0
        token = issue_mcp_token(args.name, path)
        if args.token_file:
            write_token_file(args.token_file, token)
    except (OperatorError, CredentialError, OSError) as exc:
        parser.error(str(exc))
    if args.token_file:
        print(f"Wrote the {args.name} token to {args.token_file} (mode 0600).", file=sys.stderr)
        return 0
    print(f"GROK_GADGETS_MCP_TOKEN={token}")
    print(
        f"Use it as 'Authorization: Bearer <token>' for {args.name}. It is stored only as a "
        "hash and will not be shown again. A running serve accepts it now.",
        file=sys.stderr,
    )
    return 0


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
        devices = (
            f"device listener 127.0.0.1:{running.device_port}"
            if running.device_port
            else "simulated light only, no device listener"
        )
        print(
            f"MCP {running.url} on 127.0.0.1; {devices}. "
            "Bearer token is the mcp-token file. This process does not prove Grok or hardware.",
            file=sys.stderr,
        )
        if running.lan:
            print(f"LAN TLS device listener {running.lan} (private peers only).", file=sys.stderr)
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
    elif argv[0].startswith("-") and argv[0] not in ("-h", "--help", "--version"):
        argv = ["stdio", *argv]  # Older client settings: grok-gadgets-gateway --simulator
    args = parser.parse_args(argv)
    if args.command == "stdio":
        return run_stdio(parser, args)
    return run(parser, args)


if __name__ == "__main__":
    raise SystemExit(main())
