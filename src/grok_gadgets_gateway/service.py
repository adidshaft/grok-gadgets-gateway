"""Loopback device listener, optional LAN TLS device listener and authenticated HTTP MCP."""

import asyncio
import hashlib
import hmac
import json
import re
import time
from collections import deque
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import uvicorn
from mcp.server.transport_security import TransportSecuritySettings

from .domain import DEFAULT_MAX_DEVICES, MAX_DEVICES_LIMIT, Gateway
from .mcp_server import RateLimiter, RequestLog, make_server
from .operator import (
    DEFAULT_TOKEN_NAME,
    OperatorError,
    match_named_token,
    read_mcp_token,
    read_named_tokens,
)
from .simulator import Simulator
from .simulator_config import SimulatorConfigError, load_config
from .transport import CredentialError, Credentials, DevicePortInUse, DeviceServer

_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]*(?::[0-9]{1,5})?$")
MCP_PATH = "/mcp"
# Set by BearerAuth after a token matches; any client-sent copy is removed first.
TOKEN_HEADER = b"x-grok-gadgets-token"
REMOTE_HEADER = b"x-grok-gadgets-remote"
# Accounts mode (hub ADR 011): the account of the OAuth sign-in that made this request.
ACCOUNT_HEADER = b"x-grok-gadgets-account"
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
# A tunnel or proxy adds one of these; a local client does not.
_FORWARDING_HEADERS = (b"forwarded", b"x-forwarded-for", b"x-forwarded-host", b"x-real-ip")


# OAuth discovery and sign-in paths answer without a bearer token (serve --oauth only).
OAUTH_PATHS = ("/authorize", "/token", "/register", "/revoke", "/oauth/approve")
# Requests a minute for each group of sign-in paths: (per caller, in total). Separate
# groups keep a registration flood from starving Grok Bot's hourly refresh (hub ADR 008).
OAUTH_BUDGETS = {
    "sign_up": (10, 30),  # /register and /authorize create state
    "token": (60, 120),  # /token and /revoke: refresh for a live sign-in
    "page": (60, 120),  # discovery metadata and the approval page, which reloads every 3 s
}
OAUTH_GROUPS = {
    "/register": "sign_up",
    "/authorize": "sign_up",
    "/token": "token",
    "/revoke": "token",
}
FORWARDED_FOR = (b"cf-connecting-ip", b"x-forwarded-for", b"x-real-ip")


def _host_name(headers):
    host = headers.get(b"host", b"").decode("latin-1").strip().lower()
    if host.startswith("["):
        return host[1 : host.find("]")] if "]" in host else host
    return host.partition(":")[0] if host.count(":") == 1 else host


def host_allowed(headers, extra_hosts):
    """The Host check for paths the MCP app does not cover: loopback or an --allowed-host."""
    host = headers.get(b"host", b"").decode("latin-1").strip().lower()
    names = {*_LOOPBACK_HOSTS, *(h.lower() for h in extra_hosts if ":" not in h)}
    exact = {h.lower() for h in extra_hosts if ":" in h}
    return host in exact or _host_name(headers) in names


def is_remote(headers):
    """True for a request through a tunnel: a non-loopback Host or a forwarding header."""
    if any(name in headers for name in _FORWARDING_HEADERS):
        return True
    return _host_name(headers) not in _LOOPBACK_HOSTS


def caller(scope, values):
    """Who sent a request: the tunnel's forwarding header, else the peer address.

    Behind a tunnel every peer is 127.0.0.1. Only a process on this computer can forge the
    header, and it can reach loopback anyway.
    """
    for header in FORWARDED_FOR:
        value = values.get(header, b"").decode("latin-1").split(",")[0].strip()
        if value:
            return value[:64]
    client = scope.get("client") or ("-",)
    return str(client[0])


class FileTokenVerifier:
    """Bearer check against the mcp-token file and the named token list.

    Both files are read on every request, so `mcp-token issue`, `mcp-token revoke` and
    `rotate-mcp-token` take effect without a restart. Returns the token's name or None.
    The token is never logged.
    """

    def __init__(self, path, named_path=None, oauth=None):
        self.path = path
        self.named_path = named_path
        # With serve --oauth, an access token from an owner-approved sign-in also matches.
        self.oauth = oauth

    async def account_of(self, token):
        """The account of an OAuth sign-in; None for the shared static tokens."""
        if self.oauth is None or not isinstance(token, str):
            return None
        return await self.oauth.verify_account(token)

    async def verify_token(self, token):
        if not isinstance(token, str) or not token:
            return None
        if self.oauth is not None:
            name = await self.oauth.verify_name(token)
            if name is not None:
                return name
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


class Limits:
    """HTTP caps applied after authentication, per token name."""

    def __init__(self, max_concurrent=8, max_body_bytes=65536, request_timeout=30.0):
        self.max_concurrent = max_concurrent
        self.max_body_bytes = max_body_bytes
        self.request_timeout = request_timeout


class _BodyTooLarge(Exception):
    pass


async def _send_json(send, status, error, description, extra=()):
    body = json.dumps({"error": error, "error_description": description}).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
        *extra,
    ]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class BearerAuth:
    """Static bearer token for every HTTP request, then per-token HTTP caps.

    This is not OAuth: a 401 carries a plain `WWW-Authenticate: Bearer` challenge and the
    server publishes no OAuth discovery metadata, so clients never look for an
    authorization server on loopback. Rejections are logged without the token or body.
    """

    def __init__(
        self,
        app,
        verifier,
        limits=None,
        log=None,
        remote_switch=None,
        oauth_app=None,
        resource_metadata=None,
        allowed_hosts=(),
        clock=time.monotonic,
        accounts=False,
    ):
        self.app = app
        # Accounts mode refuses the Bot's shared credential: only an account's sign-in works.
        self.accounts = accounts
        self.verifier = verifier
        self.limits = limits or Limits()
        self.log = log
        # Path of the kill-switch file; read on every remote request.
        self.remote_switch = remote_switch
        # serve --oauth: discovery and sign-in routes, and the metadata URL for 401s.
        self.oauth_app = oauth_app
        self.resource_metadata = resource_metadata
        self.allowed_hosts = tuple(allowed_hosts)
        self.clock = clock
        self.oauth_calls = {}  # (group, caller or "*") -> deque of request times
        self.flood_alerted_at = None
        self.active = {}

    def _oauth_path(self, path):
        return self.oauth_app is not None and (
            path.startswith("/.well-known/") or path in OAUTH_PATHS
        )

    async def _oauth(self, scope, receive, send, values):
        if not host_allowed(values, self.allowed_hosts):
            self._reject(421, "host_not_allowed", None)
            await _send_json(send, 421, "misdirected_request", "Host is not allowed")
            return
        now = self.clock()
        group = OAUTH_GROUPS.get(scope.get("path", ""), "page")
        # Accepted requests are capped by the totals, so this sweep stays small.
        for key, calls in list(self.oauth_calls.items()):
            while calls and now - calls[0] >= 60:
                calls.popleft()
            if not calls:
                del self.oauth_calls[key]
        windows = [
            (self.oauth_calls.setdefault((group, key), deque()), limit)
            for key, limit in zip((caller(scope, values), "*"), OAUTH_BUDGETS[group])
        ]
        if any(len(calls) >= limit for calls, limit in windows):
            self._reject(429, "oauth_rate_limited", None)
            if self.flood_alerted_at is None or now - self.flood_alerted_at >= 60:
                self.flood_alerted_at = now
                if self.log is not None:
                    self.log.write({"event": "oauth_flood", "reason": f"{group}_rate_limited"})
            await _send_json(
                send, 429, "slow_down", "Too many sign-in requests", [(b"retry-after", b"10")]
            )
            return
        for calls, _ in windows:
            calls.append(now)
        await self.oauth_app(scope, receive, send)

    def _reject(self, status, reason, name):
        if self.log is not None:
            self.log.write(
                {"event": "http_rejected", "status": status, "reason": reason, "token": name}
            )

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        internal = (TOKEN_HEADER, REMOTE_HEADER, ACCOUNT_HEADER)
        headers = [(k, v) for k, v in scope.get("headers") or [] if k not in internal]
        values = dict(headers)
        remote = is_remote(values)
        if remote and self.remote_switch is not None and Path(self.remote_switch).exists():
            self._reject(503, "remote_disabled", None)
            await _send_json(
                send, 503, "remote_disabled", "Remote access is turned off on this gateway"
            )
            return
        if self._oauth_path(scope.get("path", "")):
            await self._oauth(scope, receive, send, values)
            return
        scheme, _, token = values.get(b"authorization", b"").decode("latin-1").partition(" ")
        name = await self.verifier.verify_token(token) if scheme.lower() == "bearer" else None
        if name is None:
            self._reject(401, "invalid_token", None)
            challenge = 'Bearer error="invalid_token"'
            if self.resource_metadata:
                challenge += f', resource_metadata="{self.resource_metadata}"'
            await _send_json(
                send,
                401,
                "invalid_token",
                "Bearer token required",
                [(b"www-authenticate", challenge.encode())],
            )
            return
        account = await self.verifier.account_of(token) if self.accounts else None
        if self.accounts and account is None:
            self._reject(401, "shared_credential_refused", name)
            await _send_json(
                send,
                401,
                "shared_credential_refused",
                "This relay has accounts: sign in with your own Grok Bot sign-in",
            )
            return
        if remote and name == DEFAULT_TOKEN_NAME:
            # The local mcp-token never leaves this computer; remote clients get a named one.
            self._reject(403, "named_token_required", name)
            await _send_json(
                send, 403, "named_token_required", "Use a named token for remote access"
            )
            return
        limits = self.limits
        try:
            declared = int(values.get(b"content-length", b"0"))
        except ValueError:
            declared = 0
        if declared > limits.max_body_bytes:
            self._reject(413, "body_too_large", name)
            await _send_json(send, 413, "body_too_large", "Request body is too large")
            return
        if self.active.get(name, 0) >= limits.max_concurrent:
            self._reject(429, "too_many_concurrent_requests", name)
            await _send_json(
                send,
                429,
                "too_many_concurrent_requests",
                "Too many open requests for this token",
                [(b"retry-after", b"1")],
            )
            return

        received = 0
        started = False

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limits.max_body_bytes:
                    raise _BodyTooLarge
            return message

        async def tracked_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        added = [(TOKEN_HEADER, name.encode()), (REMOTE_HEADER, b"1" if remote else b"0")]
        if account is not None:
            added.append((ACCOUNT_HEADER, account.encode()))
        scope = {**scope, "headers": [*headers, *added]}
        self.active[name] = self.active.get(name, 0) + 1
        try:
            if scope.get("method") == "POST":
                async with asyncio.timeout(limits.request_timeout):
                    await self.app(scope, limited_receive, tracked_send)
            else:  # GET event streams stay open; they still count toward concurrency.
                await self.app(scope, limited_receive, tracked_send)
        except TimeoutError:
            self._reject(504, "request_timeout", name)
            if not started:
                await _send_json(send, 504, "request_timeout", "Request took too long")
        except _BodyTooLarge:
            self._reject(413, "body_too_large", name)
            if not started:
                await _send_json(send, 413, "body_too_large", "Request body is too large")
        finally:
            self.active[name] -= 1
            if not self.active[name]:
                del self.active[name]


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


# LAN listener limits (hub ADR 005). Loopback keeps its own, larger limits.
LAN_SESSIONS = 8
LAN_PENDING = 4
LAN_CONNECTS_PER_MINUTE = 20
LAN_FRAMES_PER_SECOND = 30
LAN_HANDSHAKE_TIMEOUT = 5
# How often a --lan-listen NAME.local is resolved again, to follow an address change.
LAN_RECHECK_SECONDS = 30
# Relay listener limits (hub ADR 009): any peer, so per-address caps sit under global ones.
RELAY_PENDING = 32
RELAY_PENDING_PER_ADDRESS = 4
RELAY_CONNECTS_PER_MINUTE = 20
RELAY_FRAMES_PER_SECOND = 30
RELAY_HANDSHAKE_TIMEOUT = 5


class Running:
    def __init__(self, server, task, mcp_port, device_port):
        self.server = server
        self.task = task
        self.mcp_port = mcp_port
        self.device_port = device_port
        self.lan = None  # "address:port" when the LAN listener runs.
        self.relay = None  # "name on address:port" when the relay listener runs.
        self.gateway = None
        self.relay_server = None
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
    limits=None,
    rate_limiter=None,
    log_stream=None,
    request_log=None,
    host="127.0.0.1",
    lan_listen=None,
    lan_directory=None,
    lan_recheck=None,
    device_listen=None,
    device_directory=None,
    resolver=None,
    remote_switch=None,
    usage_hook=None,
    oauth=False,
    oauth_issuer=None,
    oauth_directory=None,
    accounts=False,
    firmware_directory=None,
    max_devices=DEFAULT_MAX_DEVICES,
    client_documents=None,
    credits=False,
):
    """Run until the context exits. HTTP and the device listener bind 127.0.0.1.

    `lan_listen` ("address[:port]") adds a TLS device listener on one private address.
    `device_listen` ("address[:port]") adds the relay device listener on any address.
    `credentials=None` (simulator only) reads no device registry and opens no device port.
    """
    if host != "127.0.0.1":
        raise ValueError("MCP HTTP binds 127.0.0.1 only")
    if type(max_devices) is not int or not 1 <= max_devices <= MAX_DEVICES_LIMIT:
        raise OperatorError(f"--max-devices must be 1 to {MAX_DEVICES_LIMIT}")
    if allowed_hosts and request_log is None:
        # A tunnel is only allowed when every call through it is recorded.
        raise OperatorError("--allowed-host requires --request-log")
    if oauth:
        if oauth_issuer is None:
            if len(allowed_hosts) != 1:
                raise OperatorError("--oauth needs exactly one --allowed-host: the tunnel name")
            oauth_issuer = f"https://{allowed_hosts[0]}"
        if request_log is None:
            raise OperatorError("--oauth requires --request-log")
    if accounts and not oauth:
        raise OperatorError("--accounts needs --oauth: accounts are created at sign-in")
    if credits and not accounts:
        raise OperatorError("--credits needs --accounts: credits are kept per account")
    if simulator_config and not simulator:
        raise OperatorError("--simulator-config requires --simulator")
    for flag, value in (("--lan-listen", lan_listen), ("--device-listen", device_listen)):
        if credentials is None and value is not None:
            raise OperatorError(
                f"{flag} needs device credentials: run without --simulator, "
                "or pass --credentials with --simulator"
            )
    try:
        config = load_config(simulator_config) if simulator_config else None
        security = transport_security(allowed_hosts)
        registry = None
        if credentials is not None:
            registry = Credentials(credentials)
            registry.validate()
        read_mcp_token(mcp_token)
        if mcp_tokens is not None:
            read_named_tokens(mcp_tokens)
        lan = None
        if lan_listen is not None:
            from .lan import is_local_name, parse_lan_address, resolve_lan_host, server_context

            lan_host, lan_port = parse_lan_address(lan_listen)
            lan = (lan_host, lan_port, server_context(lan_host, lan_directory))
            lan_bind = resolve_lan_host(lan_host, resolver)
        relay = None
        if device_listen is not None:
            from .lan import device_cert_name, device_context, parse_device_listen

            relay_host, relay_port = parse_device_listen(device_listen)
            relay = (relay_host, relay_port, device_context(device_directory))
            relay_name = device_cert_name(device_directory)
    except (CredentialError, SimulatorConfigError, ValueError) as exc:
        raise OperatorError(str(exc)) from None
    try:
        log = RequestLog("streamable-http", stream=log_stream, path=request_log)
    except OSError as exc:
        raise OperatorError(f"cannot open request log {request_log}: {exc.strerror}") from None

    gateway = Gateway(max_devices=max_devices)
    sim = Simulator(gateway, config=config) if simulator else None
    claims = None
    if accounts:
        from .accounts import Accounts
        from .oauth import oauth_dir
        from .transport import read_registry

        enrolled = list(read_registry(credentials)) if credentials is not None else []
        if sim is not None:
            enrolled.append(sim.device_id)
        claims = Accounts(Path(oauth_directory or oauth_dir()) / "accounts.json", log=log)
        claims.open(enrolled)
        if sim is not None and claims.owner(sim.device_id) is None:
            claims.assign(sim.device_id, claims.operator, via="operator")
    balances = None
    if credits:
        from .credits import Credits
        from .receipts import Receipts

        # Hub ADR 015 Stage 0: balances per account, beside accounts.json; signed receipts.
        receipts = Receipts(claims.path.parent, log=log).open()
        balances = Credits(claims.path.parent, claims, log=log, receipts=receipts).open()
    updates = None
    if registry is not None:
        from .firmware import Updates

        # Signed updates (hub ADR 012): offered only to gadgets that ask in their hello.
        updates = Updates(firmware_directory, log=log)
    device_server = None
    if registry is not None:
        device_server = DeviceServer(
            gateway,
            registry,
            host="127.0.0.1",
            port=device_port,
            reserved_ids=[sim.device_id] if sim else [],
            log=log,
            claims=claims,
            updates=updates,
        )

    def make_lan_server(address):
        return DeviceServer(
            gateway,
            registry,
            host=address,
            port=lan[1],
            reserved_ids=[sim.device_id] if sim else [],
            max_sessions=LAN_SESSIONS,
            max_pending=LAN_PENDING,
            tls=lan[2],
            log=log,
            handshake_timeout=LAN_HANDSHAKE_TIMEOUT,
            connects_per_minute=LAN_CONNECTS_PER_MINUTE,
            frames_per_second=LAN_FRAMES_PER_SECOND,
            claims=claims,
            updates=updates,
        )

    lan_server = make_lan_server(lan_bind) if lan is not None else None
    relay_server = None
    if relay is not None:
        relay_server = DeviceServer(
            gateway,
            registry,
            host=relay[0],
            port=relay[1],
            reserved_ids=[sim.device_id] if sim else [],
            # The relay holds as many sessions as the registry holds gadgets (hub ADR 014).
            max_sessions=max_devices,
            max_pending=RELAY_PENDING,
            tls=relay[2],
            log=log,
            handshake_timeout=RELAY_HANDSHAKE_TIMEOUT,
            connects_per_minute=RELAY_CONNECTS_PER_MINUTE,
            frames_per_second=RELAY_FRAMES_PER_SECOND,
            public=True,
            pending_per_address=RELAY_PENDING_PER_ADDRESS,
            claims=claims,
            updates=updates,
        )

    def listener_status():
        report = device_server.status() if device_server else {"enabled": False}
        if relay_server is not None:
            report["relay"] = {
                "name": relay_name,
                "port": relay_server.port,
                "connections": len(relay_server.connections),
            }
        return report

    def lan_label():
        bound = f"{lan_server.host}:{lan_server.port}"
        return f"{lan[0]} ({bound})" if lan[0] != lan_server.host else bound

    async def follow_address(running):
        # The name keeps the certificate and the boards valid; only the bound address moves.
        # These events go to stderr too, so `service logs` shows them.
        nonlocal lan_server
        while True:
            await asyncio.sleep(LAN_RECHECK_SECONDS if lan_recheck is None else lan_recheck)
            try:
                current = await asyncio.to_thread(resolve_lan_host, lan[0], resolver)
            except OperatorError as exc:
                log.write({"event": "lan_resolve_failed", "name": lan[0], "reason": str(exc)})
                continue
            if current == lan_server.host:
                continue
            moved = make_lan_server(current)
            try:
                await moved.start()
            except OSError as exc:
                log.write(
                    {"event": "lan_rebind_failed", "address": current, "reason": exc.strerror}
                )
                continue
            previous, lan_server = lan_server, moved
            running.lan = lan_label()
            log.write(
                {"event": "lan_rebound", "name": lan[0], "from": previous.host, "to": current}
            )
            await previous.close()

    try:
        try:
            if device_server is not None:
                await device_server.start()
        except DevicePortInUse as exc:
            raise OperatorError(str(exc)) from None
        if lan_server is not None:
            try:
                await lan_server.start()
            except OSError as exc:
                raise OperatorError(
                    f"cannot listen on {lan_server.host}:{lan[1]}: {exc.strerror}. "
                    "Use an address or mDNS name of this computer."
                ) from None
        if relay_server is not None:
            try:
                await relay_server.start()
            except OSError as exc:
                raise OperatorError(
                    f"cannot listen on {relay[0]}:{relay[1]}: {exc.strerror}. "
                    "Use an address of this host and a free port."
                ) from None
        async with _serve_http(
            gateway,
            sim,
            device_server,
            mcp_token,
            port=port,
            security=security,
            log=log,
            mcp_tokens=mcp_tokens,
            limits=limits,
            rate_limiter=rate_limiter if rate_limiter is not None else RateLimiter(),
            remote_switch=remote_switch,
            usage_hook=usage_hook,
            allowed_hosts=allowed_hosts,
            oauth_issuer=oauth_issuer if oauth else None,
            oauth_directory=oauth_directory,
            listener_status=listener_status if registry is not None else None,
            accounts=claims,
            client_documents=client_documents,
            credits=balances,
        ) as running:
            running.gateway = gateway
            running.relay_server = relay_server
            running.credits = balances
            follower = None
            admin = None
            metering = None
            if claims is not None:
                admin = asyncio.create_task(apply_admin(claims, claims.path.parent / "admin"))
            if balances is not None:
                metering = asyncio.create_task(meter_credits(balances, gateway))
            if lan_server is not None:
                running.lan = lan_label()
                if is_local_name(lan[0]):
                    follower = asyncio.create_task(follow_address(running))
            if relay_server is not None:
                bound = f"{relay_server.host}:{relay_server.port}"
                running.relay = f"{relay_name} on {bound}" if relay_name else bound
            log.append(
                {
                    "event": "serve_started",
                    "gateway_version": gateway_version(),
                    "url": running.url,
                    "device_listener": (
                        f"127.0.0.1:{running.device_port}" if running.device_port else None
                    ),
                    "lan_listener": running.lan,
                    "relay_listener": running.relay,
                    "simulator": sim.device_id if sim else None,
                }
            )
            try:
                yield running
            finally:
                if follower is not None:
                    follower.cancel()
                if admin is not None:
                    admin.cancel()
                if metering is not None:
                    metering.cancel()
    finally:
        if relay_server is not None:
            await relay_server.close()
        if lan_server is not None:
            await lan_server.close()
        if device_server is not None:
            await device_server.close()
        if balances is not None:
            balances.close()
        log.append({"event": "serve_stopped"})
        log.close()


async def apply_admin(accounts, directory, every=1.0):
    """Apply operator commands queued by `accounts unclaim|assign|link` (hub ADR 011 §9)."""
    while True:
        accounts.apply_admin(directory)
        await asyncio.sleep(every)


async def meter_credits(credits, gateway, every=1.0, day_every=60.0):
    """Apply queued `credits` changes each second; each minute, charge online gadget-days
    and, after midnight UTC, write the day's receipts."""
    last = None
    while True:
        credits.apply_admin()
        now = time.monotonic()
        if last is None or now - last >= day_every:
            last = now
            online = {d["device_id"] for d in gateway.list_devices() if d.get("available")}
            credits.meter_days(online)
            credits.close_receipts()
        await asyncio.sleep(every)


def _oauth_routes(issuer, directory, log, accounts=None, documents=None):
    """The SDK's OAuth routes, protected-resource metadata and the owner approval page."""
    from mcp.server.auth.handlers.metadata import MetadataHandler
    from mcp.server.auth.routes import (
        build_metadata,
        build_resource_metadata_url,
        cors_middleware,
        create_auth_routes,
        create_protected_resource_routes,
    )
    from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
    from pydantic import AnyHttpUrl
    from starlette.applications import Starlette
    from starlette.routing import Route

    from .oauth import APPROVE_PATH, SCOPE, OwnerApprovedOAuth

    provider = OwnerApprovedOAuth(issuer, directory, log, accounts=accounts, documents=documents)
    issuer_url = AnyHttpUrl(provider.issuer)
    resource = AnyHttpUrl(provider.issuer + MCP_PATH)
    registration = ClientRegistrationOptions(
        enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
    )
    revocation = RevocationOptions(enabled=True)
    # Both registration methods: dynamic registration, and Client ID Metadata Documents (an
    # HTTPS URL as client_id, MCP 2026-07-28), fetched with limits (#176, hub ADR 014).
    metadata = build_metadata(issuer_url, None, registration, revocation)
    metadata.client_id_metadata_document_supported = True
    well_known = Route(
        "/.well-known/oauth-authorization-server",
        endpoint=cors_middleware(MetadataHandler(metadata).handle, ["GET", "OPTIONS"]),
        methods=["GET", "OPTIONS"],
    )
    sdk_routes = create_auth_routes(
        provider,
        issuer_url,
        client_registration_options=registration,
        revocation_options=revocation,
    )
    routes = [
        well_known,
        *(r for r in sdk_routes if r.path != well_known.path),
        *create_protected_resource_routes(
            resource, [issuer_url], scopes_supported=[SCOPE], resource_name="Grok Gadgets gateway"
        ),
        Route(APPROVE_PATH, provider.approve_page, methods=["GET"]),
        Route(APPROVE_PATH, provider.code_submitted, methods=["POST"]),
    ]
    return provider, Starlette(routes=routes), str(build_resource_metadata_url(resource))


def gateway_version():
    try:
        return version("grok-gadgets-gateway")
    except PackageNotFoundError:
        return None


@asynccontextmanager
async def _serve_http(
    gateway,
    sim,
    device_server,
    mcp_token,
    *,
    port,
    security,
    log,
    mcp_tokens=None,
    limits=None,
    rate_limiter=None,
    remote_switch=None,
    usage_hook=None,
    allowed_hosts=(),
    oauth_issuer=None,
    oauth_directory=None,
    listener_status=None,
    accounts=None,
    client_documents=None,
    credits=None,
):
    if listener_status is None and device_server is not None:
        listener_status = device_server.status
    mcp = make_server(
        gateway,
        sim,
        None,
        False,
        request_log=log,
        rate_limiter=rate_limiter,
        listener_status=listener_status,
        http=True,
        usage_hook=usage_hook,
        accounts=accounts,
        credits=credits,
        log_level="WARNING",
    )
    app = mcp.streamable_http_app(
        streamable_http_path=MCP_PATH,
        json_response=True,
        transport_security=security,
        host="127.0.0.1",
    )
    provider = oauth_app = resource_metadata = None
    if oauth_issuer is not None:
        provider, oauth_app, resource_metadata = _oauth_routes(
            oauth_issuer, oauth_directory, log, accounts, client_documents
        )
    config = uvicorn.Config(
        BearerAuth(
            app,
            FileTokenVerifier(mcp_token, mcp_tokens, provider),
            limits,
            log,
            remote_switch,
            oauth_app=oauth_app,
            resource_metadata=resource_metadata,
            allowed_hosts=allowed_hosts,
            accounts=accounts is not None,
        ),
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
        yield Running(server, task, bound[1], device_server.port if device_server else None)
    finally:
        server.should_exit = True
        if not task.done():
            try:
                await asyncio.wait_for(task, 5)
            except TimeoutError:
                task.cancel()
