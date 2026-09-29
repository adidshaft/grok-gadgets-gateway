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
    reader, writer = await asyncio.open_connection(host, port, limit=MAX_FRAME)
    frame = bytearray()
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
                device.write(
                    b'{"ok":false,"error":{"code":"invalid_request",'
                    b'"message":"Malformed USB JSON"}}\n'
                )
                frame.clear()
                continue
            frame.clear()
            if message.get("type") == "hello":
                message["token"] = token
            wire = (json.dumps(message, separators=(",", ":")) + "\n").encode()
            if len(wire) > MAX_FRAME:
                raise ValueError("Authenticated USB frame exceeds limit")
            writer.write(wire)
            await writer.drain()
            reply = await asyncio.wait_for(reader.readline(), timeout=10)
            if not reply or len(reply) > MAX_FRAME:
                raise ConnectionError("Gateway connection ended")
            device.write(reply)
    finally:
        writer.close()
        await writer.wait_closed()
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
