"""USB LF framing bridge; inject private host credential into firmware hello only."""

import argparse
import asyncio
import json
import os

import serial

from .protocol import MAX_FRAME

_SERIAL_BACKOFF_START = 0.05
_SERIAL_BACKOFF_MAX = 2.0


async def _wait(stop, delay):
    if stop is None:
        await asyncio.sleep(delay)
        return
    try:
        await asyncio.wait_for(stop.wait(), delay)
    except asyncio.TimeoutError:
        pass


async def bridge(serial_port, token, *, host="127.0.0.1", port=8765, baudrate=115200, stop=None):
    if host not in ("127.0.0.1", "::1"):
        raise ValueError("Local USB bridge connects only to loopback")
    if len(token) < 16:
        raise ValueError("Device token must contain at least 16 characters")
    reader = writer = None
    device = None
    frame = bytearray()
    discarding = False
    backoff = _SERIAL_BACKOFF_START

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

    def reply_serial(code, message):
        """Write one device reply. False means the serial port needs to be reopened."""
        try:
            reply_error(code, message)
        except (serial.SerialException, OSError):
            return False
        return True

    async def close_serial():
        nonlocal device, frame, discarding
        if device is not None:
            try:
                device.close()
            except (serial.SerialException, OSError):
                pass
        device = None
        frame.clear()
        discarding = False

    async def open_serial():
        return await asyncio.to_thread(
            serial.Serial, serial_port, baudrate, timeout=0.1, write_timeout=1
        )

    async def reopen():
        nonlocal backoff
        await close_serial()
        await _wait(stop, backoff)
        backoff = min(backoff * 2, _SERIAL_BACKOFF_MAX)

    async def handle_frame():
        nonlocal reader, writer
        try:
            message = json.loads(bytes(frame))
        except RecursionError:
            return reply_serial("invalid_request", "Malformed USB JSON")
        except (ValueError, UnicodeError):
            # Firmware log lines are not requests. Stay silent and keep the session.
            return True
        if not isinstance(message, dict):
            return reply_serial("invalid_request", "Malformed USB JSON")
        try:
            if message.get("type") == "hello":
                # A fresh firmware hello always begins a fresh authenticated TCP session.
                await reset()
                message["token"] = token
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port, limit=MAX_FRAME), timeout=2
                )
            if writer is None:
                return reply_serial("stale_session", "Send hello to establish device session")
            wire = (json.dumps(message, separators=(",", ":")) + "\n").encode()
            if len(wire) > MAX_FRAME:
                return reply_serial("invalid_request", "Authenticated frame exceeds limit")
            writer.write(wire)
            await writer.drain()
            reply = await asyncio.wait_for(reader.readline(), timeout=10)
            if not reply or len(reply) > MAX_FRAME:
                raise ConnectionError("Gateway connection ended")
            try:
                device.write(reply)
            except (serial.SerialException, OSError):
                await reset()
                return False
            response = json.loads(reply)
            if not response.get("ok") and response.get("error", {}).get("code") in (
                "unauthorized",
                "revoked",
                "stale_session",
            ):
                await reset()
        except RecursionError:
            await reset()
            return reply_serial("invalid_request", "Malformed USB JSON")
        except (OSError, ConnectionError, asyncio.TimeoutError, ValueError):
            await reset()
            return reply_serial("gateway_unavailable", "Reconnect with a fresh hello")
        return True

    async def consume(chunk):
        """Frame one read. False means the serial port failed and the rest is dropped."""
        nonlocal frame, discarding
        data = chunk
        while data:
            if discarding:
                newline = data.find(b"\n")
                if newline < 0:
                    return True
                data = data[newline + 1 :]
                discarding = False
                if not reply_serial("invalid_request", "USB frame exceeds limit"):
                    return False
                continue
            newline = data.find(b"\n")
            if newline < 0:
                frame.extend(data)
                if len(frame) > MAX_FRAME:
                    frame.clear()
                    discarding = True
                return True
            take = newline + 1
            if len(frame) + take > MAX_FRAME:
                frame.clear()
                data = data[take:]
                if not reply_serial("invalid_request", "USB frame exceeds limit"):
                    return False
                continue
            frame.extend(data[:take])
            data = data[take:]
            if not await handle_frame():
                return False
            frame.clear()
        return True

    try:
        while stop is None or not stop.is_set():
            if device is None:
                try:
                    device = await open_serial()
                    backoff = _SERIAL_BACKOFF_START
                except (serial.SerialException, OSError):
                    await _wait(stop, backoff)
                    backoff = min(backoff * 2, _SERIAL_BACKOFF_MAX)
                    continue
            try:
                chunk = await asyncio.to_thread(device.read, MAX_FRAME)
            except (serial.SerialException, OSError):
                await reopen()
                continue
            if not chunk:
                continue
            if not await consume(chunk):
                await reopen()
    finally:
        await reset()
        await close_serial()


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
