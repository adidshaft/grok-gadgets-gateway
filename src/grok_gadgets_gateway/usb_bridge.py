"""USB LF framing bridge; inject private host credential into firmware hello only."""

import argparse
import asyncio
import json
import os

import serial

from .protocol import MAX_FRAME


async def bridge(serial_port, token, *, host="127.0.0.1", port=8765, baudrate=115200, stop=None):
    if host not in ("127.0.0.1", "::1"):
        raise ValueError("Local USB bridge connects only to loopback")
    if len(token) < 16:
        raise ValueError("Device token must contain at least 16 characters")
    device = serial.Serial(serial_port, baudrate, timeout=0.1, write_timeout=1)
    reader = writer = None
    frame = bytearray()

    async def reset():
        nonlocal reader, writer
        if writer:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
        reader = writer = None

    def reply_error(code, message):
        device.write(
            (
                json.dumps(
                    {"ok": False, "error": {"code": code, "message": message}},
                    separators=(",", ":"),
                )
                + "\n"
            ).encode()
        )

    try:
        while stop is None or not stop.is_set():
            chunk = await asyncio.to_thread(device.read, 1)
            if not chunk:
                continue
            frame.extend(chunk)
            if len(frame) > MAX_FRAME:
                raise ValueError("USB frame exceeds limit")
            if chunk != b"\n":
                continue
            try:
                message = json.loads(frame)
                if not isinstance(message, dict):
                    raise ValueError("Expected object")
            except (ValueError, UnicodeError):
                reply_error("invalid_request", "Malformed USB JSON")
                frame.clear()
                continue
            frame.clear()
            try:
                if message.get("type") == "hello":
                    # A fresh firmware hello always begins a fresh authenticated TCP session.
                    await reset()
                    message["token"] = token
                    reader, writer = await asyncio.wait_for(
                        asyncio.open_connection(host, port, limit=MAX_FRAME), timeout=2
                    )
                if writer is None:
                    reply_error("stale_session", "Send hello to establish device session")
                    continue
                wire = (json.dumps(message, separators=(",", ":")) + "\n").encode()
                if len(wire) > MAX_FRAME:
                    reply_error("invalid_request", "Authenticated frame exceeds limit")
                    continue
                writer.write(wire)
                await writer.drain()
                reply = await asyncio.wait_for(reader.readline(), timeout=10)
                if not reply or len(reply) > MAX_FRAME:
                    raise ConnectionError("Gateway connection ended")
                device.write(reply)
                response = json.loads(reply)
                if not response.get("ok") and response.get("error", {}).get("code") in (
                    "unauthorized",
                    "revoked",
                    "stale_session",
                ):
                    await reset()
            except (OSError, ConnectionError, asyncio.TimeoutError, ValueError):
                await reset()
                reply_error("gateway_unavailable", "Reconnect with a fresh hello")
    finally:
        await reset()
        device.close()


def main():
    parser = argparse.ArgumentParser(description="Local Grok Gadgets USB NDJSON bridge")
    parser.add_argument("serial_port")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    token = os.environ.get("GROK_GADGETS_DEVICE_TOKEN")
    if not token:
        parser.error("Set GROK_GADGETS_DEVICE_TOKEN outside Git")
    asyncio.run(bridge(args.serial_port, token, port=args.port))


if __name__ == "__main__":
    main()
