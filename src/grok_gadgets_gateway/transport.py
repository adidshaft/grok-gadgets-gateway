"""Authenticated loopback NDJSON device transport; domain remains independent."""

import asyncio
import hmac
import json
import stat
from pathlib import Path

from .protocol import GatewayError, MAX_FRAME, VERSION, validate_request


class Credentials:
    """Small operator-managed credential registry, re-read for prompt revocation."""

    def __init__(self, path):
        self.path = Path(path)

    def check(self, device_id, token):
        try:
            if self.path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
                raise ValueError("insecure permissions")
            devices = json.loads(self.path.read_text())["devices"]
            record = devices.get(device_id)
            if (
                not record
                or not isinstance(token, str)
                or not hmac.compare_digest(record["token"], token)
            ):
                raise GatewayError("unauthorized", "Device credentials rejected")
            if record.get("revoked", False):
                raise GatewayError("revoked", "Device credential revoked")
        except GatewayError:
            raise
        except (OSError, KeyError, ValueError, TypeError):
            raise GatewayError("unauthorized", "Credential configuration unavailable") from None


class DeviceServer:
    def __init__(self, gateway, credentials, *, host="127.0.0.1", port=8765, idle_timeout=15):
        if host not in ("127.0.0.1", "::1"):
            raise ValueError("Local alpha binds loopback only")
        self.gateway = gateway
        self.credentials = credentials
        self.host, self.port = host, port
        self.idle_timeout = idle_timeout
        self.server = None
        self.connections = set()
        self.writers = set()

    async def start(self):
        self.server = await asyncio.start_server(self.client, self.host, self.port, limit=MAX_FRAME)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def close(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        for writer in list(self.writers):
            writer.close()
        if self.connections:
            await asyncio.gather(*list(self.connections), return_exceptions=True)

    async def client(self, reader, writer):
        task = asyncio.current_task()
        if len(self.connections) >= 64:
            writer.close()
            await writer.wait_closed()
            return
        self.connections.add(task)
        self.writers.add(writer)
        did = sid = token = None
        try:
            while True:
                try:
                    line = await asyncio.wait_for(reader.readline(), timeout=self.idle_timeout)
                    if not line:
                        break
                    if len(line) > MAX_FRAME or not line.endswith(b"\n"):
                        raise GatewayError("invalid_request", "Frame exceeds limit or lacks LF")
                    try:
                        message = json.loads(line)
                    except (ValueError, UnicodeError):
                        raise GatewayError("invalid_request", "Malformed JSON frame") from None
                    validate_request(message)
                    if did is None:
                        if message["type"] != "hello":
                            raise GatewayError("unauthorized", "Hello required first")
                        did = message["device"]["device_id"]
                        token = message.get("token")
                        self.credentials.check(did, token)
                        sid = self.gateway.register(message)
                        response = {"ok": True, "session_id": sid, "protocol_version": VERSION}
                    else:
                        self.credentials.check(did, token)
                        response = self.gateway.handle(did, sid, message)
                except GatewayError as exc:
                    response = exc.response()
                except (ValueError, asyncio.LimitOverrunError):
                    response = GatewayError("invalid_request", "Frame exceeds limit").response()
                    writer.write((json.dumps(response) + "\n").encode())
                    await writer.drain()
                    break
                writer.write((json.dumps(response, separators=(",", ":")) + "\n").encode())
                await writer.drain()
                if not response["ok"] and response["error"]["code"] in (
                    "unauthorized",
                    "revoked",
                    "stale_session",
                ):
                    break
        except (asyncio.TimeoutError, ConnectionError, OSError):
            pass  # Diagnostics expose status only, never raw input or credential errors.
        finally:
            if did is not None and sid is not None:
                self.gateway.disconnect(did, sid)
            writer.close()
            await writer.wait_closed()
            self.writers.discard(writer)
            self.connections.discard(task)
