"""Authenticated loopback NDJSON device transport; domain remains independent."""

import asyncio
import contextlib
import hmac
import json
import logging
import re
import stat
import time
from collections import OrderedDict
from pathlib import Path

from .protocol import MAX_TCP_FRAME, VERSION, GatewayError, validate_request

logger = logging.getLogger("grok_gadgets_gateway")
DEVICE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
MAX_REGISTRY_BYTES = 1024 * 1024
FATAL_CODES = ("unauthorized", "revoked", "stale_session")
INTERNAL_ERROR = (
    json.dumps({"ok": False, "error": {"code": "internal_error", "message": "Gateway error"}})
    + "\n"
).encode()


async def _close_writer(writer):
    writer.close()
    try:
        await writer.wait_closed()
    except (ConnectionResetError, BrokenPipeError):
        pass  # The peer has already gone; cleanup must not report another failure.


class CredentialError(ValueError):
    """A registry problem whose message never includes file contents."""

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CredentialError("invalid", "Credential file has a duplicate key")
        result[key] = value
    return result


def _no_constants(_value):
    raise CredentialError("invalid", "Credential file contains NaN or Infinity")


def parse_registry(data):
    """Strictly parse `{"devices": {id: {"token": str>=16, "revoked": bool}}}`."""
    try:
        value = json.loads(
            data.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_no_constants
        )
    except CredentialError:
        raise
    except (UnicodeError, ValueError, RecursionError):
        raise CredentialError("invalid", "Credential file is not valid UTF-8 JSON") from None
    devices = value.get("devices") if isinstance(value, dict) else None
    if not isinstance(devices, dict):
        raise CredentialError("invalid", 'Credential file needs a "devices" object')
    for device_id, record in devices.items():
        if not DEVICE_ID.fullmatch(device_id):
            raise CredentialError("invalid", "Credential file has an invalid device ID")
        if not isinstance(record, dict):
            raise CredentialError("invalid", "Each device entry must be an object")
        token = record.get("token")
        if not isinstance(token, str) or not 16 <= len(token) <= 256:
            raise CredentialError("invalid", "Each device token must be a 16-256 character string")
        if type(record.get("revoked", False)) is not bool:
            raise CredentialError("invalid", '"revoked" must be true or false')
    return devices


def read_registry(path):
    path = Path(path)
    try:
        mode = path.stat().st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise CredentialError("insecure", "Credential file must have mode 0600")
        with path.open("rb") as handle:
            data = handle.read(MAX_REGISTRY_BYTES + 1)
    except FileNotFoundError:
        raise CredentialError("missing", "Credential file does not exist") from None
    except OSError:
        raise CredentialError("unreadable", "Credential file cannot be read") from None
    if len(data) > MAX_REGISTRY_BYTES:
        raise CredentialError("invalid", "Credential file is too large")
    return parse_registry(data)


class Credentials:
    """Operator-managed registry, re-read on each request for prompt revocation.

    A transient read or parse failure (for example a non-atomic editor save) keeps serving the
    last good registry for `grace` seconds, then answers the retryable `unavailable`.
    `unauthorized` is reserved for a definite unknown device or token mismatch.
    """

    def __init__(self, path, *, grace=30, clock=time.monotonic):
        self.path = Path(path)
        self.grace = grace
        self.clock = clock
        self.status = "not_loaded"
        self._good = None
        self._good_at = None

    def validate(self):
        """Load once at startup; raises CredentialError with an operator-safe message."""
        self._good, self._good_at = read_registry(self.path), self.clock()
        self.status = "ok"
        return len(self._good)

    def enrolled_count(self):
        return None if self._good is None else len(self._good)

    def _current(self):
        try:
            devices = read_registry(self.path)
        except CredentialError as exc:
            if exc.code != self.status:
                logger.warning("Device credentials %s: %s (%s)", exc.code, exc, self.path)
            self.status = exc.code
            recent = self._good_at is not None and self.clock() - self._good_at <= self.grace
            if exc.code not in ("insecure", "missing") and recent:
                return self._good
            raise GatewayError("unavailable", "Credential configuration unavailable") from None
        if self.status != "ok" and self.status != "not_loaded":
            logger.warning("Device credentials ok again (%s)", self.path)
        self._good, self._good_at, self.status = devices, self.clock(), "ok"
        return devices

    def check(self, device_id, token):
        devices = self._current()
        record = devices.get(device_id)
        # Compare bytes so non-ASCII input cannot raise only for enrolled IDs.
        expected = record["token"].encode() if record else b"\0" * 32
        supplied = token.encode("utf-8", "surrogatepass") if isinstance(token, str) else b""
        if not hmac.compare_digest(expected, supplied) or record is None:
            raise GatewayError("unauthorized", "Device credentials rejected")
        if record.get("revoked", False):
            raise GatewayError("revoked", "Device credential revoked")


class DeviceServer:
    def __init__(
        self,
        gateway,
        credentials,
        *,
        host="127.0.0.1",
        port=8765,
        idle_timeout=15,
        hello_timeout=2,
        reserved_ids=(),
        max_sessions=64,
        max_pending=16,
    ):
        if host not in ("127.0.0.1", "::1"):
            raise ValueError("Local alpha binds loopback only")
        self.gateway = gateway
        self.credentials = credentials
        self.host, self.port = host, port
        self.idle_timeout = idle_timeout
        self.hello_timeout = hello_timeout
        # IDs owned by in-process devices (the simulator); TCP clients can never take them.
        self.reserved_ids = frozenset(reserved_ids)
        self.max_sessions = max_sessions
        self.max_pending = max_pending
        self.server = None
        self.connections = set()
        self.writers = set()
        self.pending = OrderedDict()

    async def start(self):
        self.server = await asyncio.start_server(
            self.client, self.host, self.port, limit=MAX_TCP_FRAME
        )
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    def status(self):
        return {
            "enabled": self.server is not None,
            "host": self.host,
            "port": self.port,
            "credentials": self.credentials.status,
            "enrolled_devices": self.credentials.enrolled_count(),
            "connections": len(self.connections),
        }

    async def close(self, timeout=2):
        # Python >=3.12 Server.wait_closed() waits for every client connection, so clients
        # must be closed first and the wait must stay bounded.
        if self.server:
            self.server.close()
        for writer in list(self.writers):
            writer.close()
        tasks = [task for task in self.connections if task is not asyncio.current_task()]
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=timeout / 2)
            for writer in list(self.writers):
                writer.transport.abort()
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.wait(pending, timeout=timeout / 2)
        if self.server:
            try:
                async with asyncio.timeout(timeout):
                    await self.server.wait_closed()
            except TimeoutError:
                pass

    async def _send(self, writer, response):
        writer.write((json.dumps(response, separators=(",", ":")) + "\n").encode())
        # A peer that stops reading must not hold the session open forever.
        await asyncio.wait_for(writer.drain(), timeout=self.idle_timeout)

    async def client(self, reader, writer):
        task = asyncio.current_task()
        if len(self.pending) >= self.max_pending:
            _, oldest = self.pending.popitem(last=False)
            oldest.close()  # Evict the oldest unauthenticated socket, never a session.
        if len(self.connections) >= self.max_sessions + self.max_pending:
            await _close_writer(writer)
            return
        self.connections.add(task)
        self.writers.add(writer)
        self.pending[task] = writer
        loop = asyncio.get_running_loop()
        hello_deadline = loop.time() + self.hello_timeout
        did = sid = token = None
        try:
            while True:
                try:
                    if sid is None:
                        timeout = max(0, hello_deadline - loop.time())
                    else:
                        timeout = self.idle_timeout
                    line = await asyncio.wait_for(reader.readline(), timeout=timeout)
                    if not line:
                        break
                    if len(line) > MAX_TCP_FRAME or not line.endswith(b"\n"):
                        raise GatewayError("invalid_request", "Frame exceeds limit or lacks LF")
                    try:
                        message = json.loads(line)
                    except (ValueError, UnicodeError, RecursionError):
                        raise GatewayError("invalid_request", "Malformed JSON frame") from None
                    validate_request(message)
                    if sid is None:
                        if message["type"] != "hello":
                            raise GatewayError("unauthorized", "Hello required first")
                        hello_id = message["device"]["device_id"]
                        hello_token = message.get("token")
                        if hello_id in self.reserved_ids:
                            raise GatewayError("unauthorized", "Device credentials rejected")
                        self.credentials.check(hello_id, hello_token)
                        # Only a successful registration binds the connection to a device.
                        sid = self.gateway.register(message)
                        did, token = hello_id, hello_token
                        self.pending.pop(task, None)
                        response = {"ok": True, "session_id": sid, "protocol_version": VERSION}
                    else:
                        self.credentials.check(did, token)
                        response = self.gateway.handle(did, sid, message)
                except GatewayError as exc:
                    response = exc.response()
                except (ValueError, asyncio.LimitOverrunError):
                    response = GatewayError("invalid_request", "Frame exceeds limit").response()
                    await self._send(writer, response)
                    break
                await self._send(writer, response)
                if not response["ok"] and response["error"]["code"] in FATAL_CODES:
                    break
        except (TimeoutError, ConnectionError, OSError):
            pass  # Diagnostics expose status only, never raw input or credential errors.
        except Exception as exc:  # noqa: BLE001 - one bad connection must not stop the server
            # Last resort: one reply, then close. Never log frame contents.
            logger.error("Device connection error: %s", type(exc).__name__)
            with contextlib.suppress(Exception):  # the peer may already be gone
                writer.write(INTERNAL_ERROR)
        finally:
            self.pending.pop(task, None)
            try:
                if did is not None and sid is not None:
                    self.gateway.disconnect(did, sid)
            finally:
                try:
                    await _close_writer(writer)
                finally:
                    self.writers.discard(writer)
                    self.connections.discard(task)
