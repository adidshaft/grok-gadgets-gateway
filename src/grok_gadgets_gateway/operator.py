"""Private local config for device credentials and the MCP bearer token."""

import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from .transport import DEVICE_ID, CredentialError, digest, read_registry


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


def mcp_tokens_path():
    return config_dir() / "mcp-tokens.json"


def remote_off_path():
    """Kill switch: while this file exists, `serve` refuses every remote request."""
    return config_dir() / "remote-off"


def set_remote(enabled, path=None):
    path = Path(path or remote_off_path())
    if enabled:
        path.unlink(missing_ok=True)
    else:
        atomic_write(path, f"{datetime.now(UTC).isoformat()}\n".encode())


def remote_enabled(path=None):
    return not Path(path or remote_off_path()).exists()


TOKEN_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
# The single mcp-token file answers to this name in logs and limits.
DEFAULT_TOKEN_NAME = "default"


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


def migrate_registry(path):
    """Rewrite plain device tokens as SHA-256 hashes (GW-CLAIM-003). True if it changed.

    The old file is kept once as `credentials.json.pre-hash`, mode 0600, so the operator can
    roll back; it is never overwritten. Revocation flags are kept.
    """
    path = Path(path)
    devices = read_registry(path)
    if not any("token" in record for record in devices.values()):
        return False
    backup = path.with_name(path.name + ".pre-hash")
    if not backup.exists():
        atomic_write(backup, path.read_bytes())
    hashed = {
        device_id: (
            {"sha256": digest(record["token"]), "revoked": record.get("revoked", False)}
            if "token" in record
            else record
        )
        for device_id, record in devices.items()
    }
    atomic_write(path, _dump_devices(hashed))
    return True


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


def new_device_token():
    return secrets.token_urlsafe(32)


def enroll(device_id, path=None, *, rotate=False, token=None):
    """Issue a device token. rotate=True replaces an enrolled device's token, same identity.

    The registry keeps only the token's SHA-256 hash; the token is returned once. `token`
    stores a token made earlier, for example one a board has just confirmed.
    """
    if not isinstance(device_id, str) or not DEVICE_ID.fullmatch(device_id):
        raise OperatorError("Device ID must match [A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
    path = Path(path) if path else credentials_path()
    token = token or new_device_token()

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
        devices[device_id] = {"sha256": digest(token), "revoked": False}

    update_devices(path, add)
    return token


def read_device_token_file(path):
    """A device token from `enroll --token-file`: mode 0600, one 16-256 character token."""
    path = Path(path)
    try:
        mode = path.stat().st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise OperatorError(f"{path} must have mode 0600")
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise OperatorError(f"{path} does not exist") from None
    except (OSError, UnicodeError):
        raise OperatorError(f"{path} cannot be read") from None
    token = text.strip()
    if not 16 <= len(token) <= 256 or any(char.isspace() for char in token):
        raise OperatorError(f"{path} must contain one 16-256 character device token")
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


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _check_name(name):
    if not isinstance(name, str) or not TOKEN_NAME.fullmatch(name):
        raise OperatorError("Token name must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}")
    if name == DEFAULT_TOKEN_NAME:
        raise OperatorError("'default' is the mcp-token file; use rotate-mcp-token for it")


def read_named_tokens(path):
    """Named MCP tokens as {name: record}. A missing file means none; records hold hashes."""
    path = Path(path)
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        return {}
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise CredentialError("insecure", "MCP token list must have mode 0600")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise CredentialError("unreadable", "MCP token list cannot be read") from None
    tokens = value.get("tokens") if isinstance(value, dict) else None
    if not isinstance(tokens, dict) or not all(
        isinstance(name, str)
        and TOKEN_NAME.fullmatch(name)
        and isinstance(record, dict)
        and isinstance(record.get("sha256"), str)
        and len(record["sha256"]) == 64
        and isinstance(record.get("revoked"), bool)
        for name, record in tokens.items()
    ):
        raise CredentialError("invalid", "MCP token list has an invalid format")
    return tokens


def _write_named_tokens(path, tokens):
    payload = {"tokens": {name: tokens[name] for name in sorted(tokens)}}
    atomic_write(path, (json.dumps(payload, indent=2) + "\n").encode())


def issue_mcp_token(name, path=None):
    """Create a named MCP token. Only its hash is stored; the token is returned once."""
    _check_name(name)
    path = Path(path) if path else mcp_tokens_path()
    tokens = read_named_tokens(path)
    if name in tokens and not tokens[name]["revoked"]:
        raise OperatorError(f"{name} is already active. Revoke it first: mcp-token revoke {name}")
    token = secrets.token_urlsafe(32)
    tokens[name] = {
        "sha256": token_hash(token),
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
        "revoked": False,
    }
    _write_named_tokens(path, tokens)
    return token


def revoke_mcp_token(name, path=None):
    _check_name(name)
    path = Path(path) if path else mcp_tokens_path()
    tokens = read_named_tokens(path)
    if name not in tokens:
        raise OperatorError(f"{name} is not a named MCP token")
    tokens[name]["revoked"] = True
    _write_named_tokens(path, tokens)


def list_mcp_tokens(path=None):
    """(name, created, revoked) for each named token; never the token or its hash."""
    path = Path(path) if path else mcp_tokens_path()
    tokens = read_named_tokens(path)
    return [(name, tokens[name].get("created"), tokens[name]["revoked"]) for name in sorted(tokens)]


def match_named_token(token, tokens):
    """Name of the active token whose hash matches, compared in constant time per entry."""
    digest = token_hash(token)
    found = None
    for name, record in tokens.items():
        if hmac.compare_digest(digest, record["sha256"]) and not record["revoked"]:
            found = name
    return found
