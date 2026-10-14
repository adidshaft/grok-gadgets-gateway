"""Send Wi-Fi settings, the device token and the pinned CA to a Wi-Fi build over USB serial.

The firmware console takes one JSON line per command (`status`, `provision`,
`factory_reset`) and answers one JSON line with an `ok` field. Its other lines are
status text. Nothing here prints, logs or stores the password or the token.
"""

import json
import time

MAX_SSID_BYTES = 32
MAX_PASSWORD_BYTES = 64
MAX_LINE = 4096


class ProvisionError(Exception):
    """An operator-facing failure whose message never contains a secret."""


def check_wifi(ssid, password):
    if not 1 <= len(ssid.encode()) <= MAX_SSID_BYTES:
        raise ProvisionError("Wi-Fi name must be 1-32 bytes")
    size = len(password.encode())
    if size > MAX_PASSWORD_BYTES or 0 < size < 8:
        raise ProvisionError("Wi-Fi password must be 8-64 bytes (or empty for an open network)")


def provision_frame(ssid, password, token, host, port, ca_pem):
    check_wifi(ssid, password)
    frame = {
        "type": "provision",
        "ssid": ssid,
        "password": password,
        "token": token,
        "host": host,
        "port": port,
        "ca": ca_pem,
    }
    line = json.dumps(frame, separators=(",", ":")) + "\n"
    if len(line.encode()) > MAX_LINE:
        raise ProvisionError("Provisioning line exceeds the 4096-byte console limit")
    return line.encode()


def factory_frame(token, host, port, ca_pem, passphrase, setup_code, replace=False):
    """The `factory_provision` console line (hub ADR 010): identity and label values, no Wi-Fi."""
    frame = {
        "type": "factory_provision",
        "token": token,
        "host": host,
        "port": port,
        "ca": ca_pem,
        "passphrase": passphrase,
        "setup_code": setup_code,
    }
    if replace:
        frame["replace"] = True
    line = json.dumps(frame, separators=(",", ":")) + "\n"
    if len(line.encode()) > MAX_LINE:
        raise ProvisionError("Factory line exceeds the 4096-byte console limit")
    return line.encode()


def factory_provision(port, device_id, frame):
    """Check the board's identity and that it supports setup, then send the factory line."""
    board = check_board(port, device_id)
    if "factory" not in board:
        raise ProvisionError(
            "This firmware has no factory identity (hub ADR 010). Flash a Wi-Fi build from "
            "ESP32 SDK 0.2.0-alpha.15 or later; nothing sent"
        )
    port.write(frame)
    reply = read_reply(port)
    if not reply.get("ok"):
        error = _error(reply)
        if error.startswith("already_provisioned"):
            raise ProvisionError(
                "This board already has a factory identity; pass --replace to write a new one"
            )
        raise ProvisionError("Factory provisioning failed: " + error)
    return reply


def read_reply(port, *, timeout=10.0, clock=time.monotonic):
    """Next console line that is a JSON object with an `ok` field; other lines are skipped."""
    deadline = clock() + timeout
    buffer = bytearray()
    while clock() < deadline:
        chunk = port.read(256)
        if not chunk:
            continue
        buffer.extend(chunk)
        while b"\n" in buffer:
            line, _, rest = bytes(buffer).partition(b"\n")
            buffer = bytearray(rest)
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict) and isinstance(value.get("ok"), bool):
                return value
        if len(buffer) > MAX_LINE:
            buffer.clear()
    raise ProvisionError("The device did not answer. Is the Wi-Fi build flashed on this port?")


def _error(reply):
    error = reply.get("error") if isinstance(reply.get("error"), dict) else {}
    code = error.get("code") if isinstance(error.get("code"), str) else "failed"
    message = error.get("message") if isinstance(error.get("message"), str) else ""
    return f"{code}: {message}".strip(": ")


def status(port, *, attempts=10):
    """Ask the console for its status, retrying while the board finishes booting."""
    for _ in range(attempts):
        port.write(b'{"type":"status"}\n')
        try:
            reply = read_reply(port, timeout=1.0)
        except ProvisionError:
            continue
        if reply.get("ok"):
            return reply
        raise ProvisionError("Status failed: " + _error(reply))
    raise ProvisionError("The device did not answer. Is the Wi-Fi build flashed on this port?")


def check_board(port, device_id):
    """Confirm the board on this port answers and is `device_id`, before anything is sent."""
    reply = status(port)
    if reply.get("device_id") != device_id:
        raise ProvisionError(
            f"This board reports {reply.get('device_id')!r}, not {device_id!r}; nothing sent"
        )
    return reply


def provision(port, device_id, frame):
    """Check the board's identity, then send one provisioning line. Returns its reply."""
    check_board(port, device_id)
    port.write(frame)
    reply = read_reply(port)
    if not reply.get("ok"):
        raise ProvisionError("Provisioning failed: " + _error(reply))
    return reply


def factory_reset(port):
    port.write(b'{"type":"factory_reset"}\n')
    reply = read_reply(port)
    if not reply.get("ok"):
        raise ProvisionError("Factory reset failed: " + _error(reply))
    return reply
