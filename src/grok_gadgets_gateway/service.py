"""Loopback device listener plus authenticated Streamable HTTP MCP."""

import asyncio
import hashlib
import hmac
import re
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version

import uvicorn
from mcp.server.transport_security import TransportSecuritySettings

from .domain import Gateway
from .mcp_server import RequestLog, make_server
from .operator import (
    DEFAULT_TOKEN_NAME,
    OperatorError,
    match_named_token,
    read_mcp_token,
    read_named_tokens,
)
from .simulator import Simulator
from .simulator_config import SimulatorConfigError, load_config
from .transport import CredentialError, Credentials, DeviceServer

_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]*(?::[0-9]{1,5})?$")
MCP_PATH = "/mcp"
# Set by BearerAuth after a token matches; any client-sent copy is removed first.
TOKEN_HEADER = b"x-grok-gadgets-token"


class FileTokenVerifier:
    """Bearer check against the mcp-token file and the named token list.

    Both files are read on every request, so `mcp-token issue`, `mcp-token revoke` and
    `rotate-mcp-token` take effect without a restart. Returns the token's name or None.
    The token is never logged.
    """

    def __init__(self, path, named_path=None):
        self.path = path
        self.named_path = named_path

    async def verify_token(self, token):
        if not isinstance(token, str) or not token:
            return None
        if self.named_path is not None:
            try:
                name = match_named_token(token, read_named_tokens(self.named_path))
            except CredentialError:
                name = None  # A damaged list grants nothing; the default token still works.
            if name is not None:
                return name
        try:
            expected = read_mcp_token(self.path)
        except CredentialError:
            return None
        if hmac.compare_digest(
            hashlib.sha256(expected.encode()).digest(),
            hashlib.sha256(token.encode()).digest(),
        ):
            return DEFAULT_TOKEN_NAME
        return None


class BearerAuth:
    """Static bearer token for every HTTP request.

    This is not OAuth: a 401 carries a plain `WWW-Authenticate: Bearer` challenge and the
    server publishes no OAuth discovery metadata, so clients never look for an
    authorization server on loopback.
    """

    def __init__(self, app, verifier):
        self.app = app
        self.verifier = verifier

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = [(k, v) for k, v in scope.get("headers") or [] if k != TOKEN_HEADER]
            header = dict(headers).get(b"authorization", b"")
            scheme, _, token = header.decode("latin-1").partition(" ")
            name = await self.verifier.verify_token(token) if scheme.lower() == "bearer" else None
            if name is None:
                body = b'{"error":"invalid_token","error_description":"Bearer token required"}'
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode()),
                            (b"www-authenticate", b'Bearer error="invalid_token"'),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
            scope = {**scope, "headers": [*headers, (TOKEN_HEADER, name.encode())]}
        await self.app(scope, receive, send)


def transport_security(extra_hosts=()):
    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*", "127.0.0.1", "localhost", "[::1]"]
    origins = [
        "http://127.0.0.1:*",
        "http://localhost:*",
        "http://[::1]:*",
        "http://127.0.0.1",
        "http://localhost",
        "http://[::1]",
    ]
    for name in extra_hosts:
        if not isinstance(name, str) or not _HOST.fullmatch(name):
            raise ValueError("allowed host must be a hostname or hostname:port")
        hosts.append(name)
        origins.extend((f"http://{name}", f"https://{name}"))
        if ":" not in name:
            hosts.append(f"{name}:*")
            origins.extend((f"http://{name}:*", f"https://{name}:*"))
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=origins,
    )


class Running:
    def __init__(self, server, task, mcp_port, device_port):
        self.server = server
        self.task = task
        self.mcp_port = mcp_port
        self.device_port = device_port
        self.url = f"http://127.0.0.1:{mcp_port}{MCP_PATH}"

    async def wait(self):
        await self.task


async def _wait_started(server, task):
    for _ in range(200):
        if getattr(server, "started", False):
            return
        if task.done():
            raise RuntimeError("MCP HTTP server stopped during startup") from task.exception()
        await asyncio.sleep(0.01)
    raise RuntimeError("MCP HTTP server did not start")


@asynccontextmanager
async def serve_gateway(
    credentials,
    mcp_token,
    *,
    simulator=False,
    simulator_config=None,
    device_port=8765,
    port=8766,
    allowed_hosts=(),
    mcp_tokens=None,
    log_stream=None,
    request_log=None,
    host="127.0.0.1",
):
    """Run until the context exits. HTTP and the device listener bind 127.0.0.1 only."""
    if host != "127.0.0.1":
        raise ValueError("MCP HTTP binds 127.0.0.1 only")
    if simulator_config and not simulator:
        raise OperatorError("--simulator-config requires --simulator")
    try:
        config = load_config(simulator_config) if simulator_config else None
        security = transport_security(allowed_hosts)
        registry = Credentials(credentials)
        registry.validate()
        read_mcp_token(mcp_token)
        if mcp_tokens is not None:
            read_named_tokens(mcp_tokens)
    except (CredentialError, SimulatorConfigError, ValueError) as exc:
        raise OperatorError(str(exc)) from None
    try:
        log = RequestLog("streamable-http", stream=log_stream, path=request_log)
    except OSError as exc:
        raise OperatorError(f"cannot open request log {request_log}: {exc.strerror}") from None

    gateway = Gateway()
    sim = Simulator(gateway, config=config) if simulator else None
    device_server = DeviceServer(
        gateway,
        registry,
        host="127.0.0.1",
        port=device_port,
        reserved_ids=[sim.device_id] if sim else [],
    )
    try:
        await device_server.start()
        async with _serve_http(
            gateway,
            sim,
            device_server,
            mcp_token,
            port=port,
            security=security,
            log=log,
            mcp_tokens=mcp_tokens,
        ) as running:
            log.append(
                {
                    "event": "serve_started",
                    "gateway_version": gateway_version(),
                    "url": running.url,
                    "device_listener": f"127.0.0.1:{running.device_port}",
                    "simulator": sim.device_id if sim else None,
                }
            )
            yield running
    finally:
        await device_server.close()
        log.append({"event": "serve_stopped"})
        log.close()


def gateway_version():
    try:
        return version("grok-gadgets-gateway")
    except PackageNotFoundError:
        return None


@asynccontextmanager
async def _serve_http(
    gateway, sim, device_server, mcp_token, *, port, security, log, mcp_tokens=None
):
    mcp = make_server(
        gateway,
        sim,
        None,
        False,
        request_log=log,
        listener_status=device_server.status,
        http=True,
        log_level="WARNING",
    )
    app = mcp.streamable_http_app(
        streamable_http_path=MCP_PATH,
        json_response=True,
        transport_security=security,
        host="127.0.0.1",
    )
    config = uvicorn.Config(
        BearerAuth(app, FileTokenVerifier(mcp_token, mcp_tokens)),
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
        proxy_headers=False,
    )
    server = uvicorn.Server(config)
    # The CLI owns SIGTERM/SIGINT so the device listener closes too; see cli._serve.
    server.install_signal_handlers = lambda: None
    task = asyncio.create_task(server.serve())
    try:
        await _wait_started(server, task)
        bound = server.servers[0].sockets[0].getsockname()
        if bound[0] != "127.0.0.1":
            raise RuntimeError("MCP HTTP bound a non-loopback address")
        yield Running(server, task, bound[1], device_server.port)
    finally:
        server.should_exit = True
        if not task.done():
            try:
                await asyncio.wait_for(task, 5)
            except TimeoutError:
                task.cancel()
