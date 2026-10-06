"""Private local config for device credentials and the MCP bearer token."""

import json
import os
import secrets
import stat
import tempfile
from pathlib import Path

from .transport import DEVICE_ID, CredentialError, read_registry


class OperatorError(ValueError):
    """An operator mistake whose message never includes a token."""


def config_dir():
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "grok-gadgets"


def credentials_path():
    return config_dir() / "credentials.json"


def mcp_token_path():
    return config_dir() / "mcp-token"


def atomic_write(path, data):
    """Write mode-0600 bytes via a temp file in the same directory, then os.replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, data)
        os.fsync(fd)
    except Exception:
        os.close(fd)
        os.unlink(name)
        raise
    os.close(fd)
    os.replace(name, path)
    os.chmod(path, 0o600)


def _dump_devices(devices):
    payload = {"devices": {key: devices[key] for key in sorted(devices)}}
    return (json.dumps(payload, indent=2) + "\n").encode()


def update_devices(path, mutate):
    path = Path(path)
    devices = read_registry(path) if path.exists() else {}
    mutate(devices)
    atomic_write(path, _dump_devices(devices))


def init_config():
    """Create the config directory, an empty registry, and an MCP token if missing."""
    directory = config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    created = []
    credentials = credentials_path()
    if not credentials.exists():
        atomic_write(credentials, b'{"devices": {}}\n')
        created.append(credentials)
    token_file = mcp_token_path()
    if not token_file.exists():
        atomic_write(token_file, (secrets.token_urlsafe(32) + "\n").encode())
        created.append(token_file)
    return created


def enroll(device_id, path=None, *, rotate=False):
    """Issue a device token. rotate=True replaces an enrolled device's token, same identity."""
    if not isinstance(device_id, str) or not DEVICE_ID.fullmatch(device_id):
        raise OperatorError("Device ID must match [A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
    path = Path(path) if path else credentials_path()
    token = secrets.token_urlsafe(32)

    def add(devices):
        if device_id in devices and not rotate:
            raise OperatorError(
                f"{device_id} is already enrolled. For a new token run: "
                f"grok-gadgets-gateway enroll {device_id} --rotate"
            )
        if device_id not in devices and rotate:
            raise OperatorError(
                f"{device_id} is not enrolled. Run: grok-gadgets-gateway enroll {device_id}"
            )
        devices[device_id] = {"token": token, "revoked": False}

    update_devices(path, add)
    return token


def write_token_file(path, token):
    """Store a device token for an agent's --token-file, mode 0600, never printed."""
    atomic_write(path, (token + "\n").encode())


def revoke(device_id, path=None):
    if not isinstance(device_id, str) or not DEVICE_ID.fullmatch(device_id):
        raise OperatorError("Device ID must match [A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
    path = Path(path) if path else credentials_path()
    if not path.exists():
        raise OperatorError("Credential file does not exist")

    def mark(devices):
        if device_id not in devices:
            raise OperatorError(f"{device_id} is not enrolled")
        devices[device_id]["revoked"] = True

    update_devices(path, mark)


def device_ids(path=None):
    path = Path(path) if path else credentials_path()
    if not path.exists():
        raise OperatorError("Credential file does not exist")
    return sorted(read_registry(path))


def rotate_mcp_token(path=None):
    path = Path(path) if path else mcp_token_path()
    token = secrets.token_urlsafe(32)
    atomic_write(path, (token + "\n").encode())
    return token


def read_mcp_token(path):
    path = Path(path)
    try:
        mode = path.stat().st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise CredentialError("insecure", "MCP token file must have mode 0600")
        text = path.read_text(encoding="utf-8")
    except CredentialError:
        raise
    except FileNotFoundError:
        raise CredentialError("missing", "MCP token file does not exist") from None
    except OSError:
        raise CredentialError("unreadable", "MCP token file cannot be read") from None
    token = text.strip()
    if not 16 <= len(token) <= 256 or any(char.isspace() for char in token):
        raise CredentialError("invalid", "MCP token file must contain one 16-256 character token")
    return token
