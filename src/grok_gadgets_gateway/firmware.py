"""Signed firmware updates (GW-OTA-001, hub ADR 012).

The builder signs an update on their own computer: the image, a manifest that names the
board, the version, the size and the SHA-256 of the image, and an ECDSA P-256 signature over
the manifest. The relay stores updates, offers an assigned one to a gadget that asked for
updates in its hello, and serves the image in chunks. The gadget checks everything itself;
the relay never needs the private key.
"""

import base64
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from .operator import OperatorError, atomic_write, config_dir
from .protocol import VERSION, GatewayError

FORMAT = "grok-gadgets-update/1"
CHUNK = 1024  # 1,368 base64 characters: a chunk reply stays under 1,400 bytes.
MAX_IMAGE = 8 * 1024 * 1024
MAX_MANIFEST = 512
MAX_DOWNLOADS = 8  # Gadgets downloading at once on one relay.
ACTIVE_SECONDS = 30  # A download with no chunk request for this long no longer counts.
PROTOCOLS = (VERSION,)
STATUSES = ("started", "verified", "rejected", "failed", "applied", "reverted")
STOPPED = ("rejected", "failed", "reverted")  # Not offered again until assigned again.
REASONS = (
    "signature",
    "board",
    "version",
    "size",
    "protocol",
    "sha256",
    "write",
    "link",
    "not_online",
    "boot",
)
BOARD = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
FIRMWARE_VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,31}")
DEVICE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
FILES = ("image.bin", "manifest.json", "manifest.sig")


def firmware_dir():
    return config_dir() / "firmware"


def _now():
    return datetime.now(UTC).isoformat(timespec="seconds")


# --- keys and signing (the builder's computer) ---


def _public_der(public_key):
    return public_key.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def _load_public(path):
    try:
        key = serialization.load_pem_public_key(Path(path).read_bytes())
    except (OSError, ValueError) as exc:
        raise OperatorError(f"cannot read the public key {path}: {exc}") from None
    if not isinstance(key, ec.EllipticCurvePublicKey) or key.curve.name != "secp256r1":
        raise OperatorError(f"{path} is not an ECDSA P-256 public key")
    return key


def key_id(public_path):
    """The first 16 hex digits of SHA-256 over the public key (DER). A name, not trust."""
    return hashlib.sha256(_public_der(_load_public(public_path))).hexdigest()[:16]


def public_path_for(private_path):
    private_path = Path(private_path)
    stem = private_path.name.removesuffix(".key").removesuffix(".pem")
    return private_path.with_name(stem + ".pub.pem")


def keygen(path):
    """Make the builder's key pair: the private key (mode 0600) and NAME.pub.pem beside it."""
    path = Path(path)
    public = public_path_for(path)
    for target in (path, public):
        if target.exists():
            raise OperatorError(f"{target} exists; a new key would end updates for its gadgets")
    private = ec.generate_private_key(ec.SECP256R1())
    atomic_write(
        path,
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )
    public.write_bytes(
        private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    os.chmod(public, 0o644)
    return {"private": path, "public": public, "key_id": key_id(public)}


def _check_fields(board, version, min_protocol):
    if not isinstance(board, str) or not BOARD.fullmatch(board):
        raise OperatorError("board must be 1-64 letters, digits, '.', '_' or '-'")
    if not isinstance(version, str) or not FIRMWARE_VERSION.fullmatch(version):
        raise OperatorError("version must be 1-32 letters, digits, '.', '+', '_' or '-'")
    if min_protocol not in PROTOCOLS:
        raise OperatorError(f"min_protocol must be one of {', '.join(PROTOCOLS)}")


def _hash_file(path):
    digest, size = hashlib.sha256(), 0
    with open(path, "rb") as handle:
        while block := handle.read(1 << 16):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def manifest_bytes(manifest):
    return json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()


def sign(image, key, *, board, version, out, min_protocol=VERSION):
    """Write image.bin, manifest.json and manifest.sig to `out`; return the manifest."""
    _check_fields(board, version, min_protocol)
    image, out = Path(image), Path(out)
    try:
        size = image.stat().st_size
        private = serialization.load_pem_private_key(Path(key).read_bytes(), password=None)
    except (OSError, ValueError, TypeError) as exc:
        raise OperatorError(f"cannot read the image or the key: {exc}") from None
    if not isinstance(private, ec.EllipticCurvePrivateKey) or private.curve.name != "secp256r1":
        raise OperatorError(f"{key} is not an ECDSA P-256 private key")
    if size == 0:
        raise OperatorError(f"{image} is empty")
    if size > MAX_IMAGE:
        raise OperatorError(f"{image} is larger than {MAX_IMAGE} bytes")
    sha256, size = _hash_file(image)
    manifest = {
        "board": board,
        "format": FORMAT,
        "key_id": hashlib.sha256(_public_der(private.public_key())).hexdigest()[:16],
        "min_protocol": min_protocol,
        "sha256": sha256,
        "size": size,
        "version": version,
    }
    raw = manifest_bytes(manifest)
    out.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(image, out / "image.bin")
    (out / "manifest.json").write_bytes(raw)
    (out / "manifest.sig").write_bytes(private.sign(raw, ec.ECDSA(hashes.SHA256())))
    return manifest


@dataclass
class Update:
    directory: Path
    manifest: dict
    raw: bytes
    signature: bytes

    @property
    def image(self):
        return self.directory / "image.bin"


def _parse_manifest(raw):
    if len(raw) > MAX_MANIFEST:
        raise OperatorError("manifest.json is larger than 512 bytes")
    try:
        manifest = json.loads(raw)
    except (ValueError, UnicodeError):
        raise OperatorError("manifest.json is not JSON") from None
    expected = {"board", "format", "key_id", "min_protocol", "sha256", "size", "version"}
    if not isinstance(manifest, dict) or set(manifest) != expected:
        raise OperatorError("manifest.json does not have the update fields")
    if manifest["format"] != FORMAT:
        raise OperatorError(f"manifest format is not {FORMAT}")
    _check_fields(manifest["board"], manifest["version"], manifest["min_protocol"])
    size = manifest["size"]
    if type(size) is not int or not 0 < size <= MAX_IMAGE:
        raise OperatorError("manifest size is not a valid image size")
    if not isinstance(manifest["sha256"], str) or not re.fullmatch(
        r"[0-9a-f]{64}", manifest["sha256"]
    ):
        raise OperatorError("manifest sha256 is not 64 hex digits")
    if not isinstance(manifest["key_id"], str) or not re.fullmatch(
        r"[0-9a-f]{16}", manifest["key_id"]
    ):
        raise OperatorError("manifest key_id is not 16 hex digits")
    if raw != manifest_bytes(manifest):
        raise OperatorError("manifest.json is not in the signed form (sorted, compact)")
    return manifest


def read_update(directory):
    """Read a signed update and check that the image matches its manifest."""
    directory = Path(directory)
    try:
        raw = (directory / "manifest.json").read_bytes()
        signature = (directory / "manifest.sig").read_bytes()
    except OSError as exc:
        raise OperatorError(f"{directory} is not a signed update: {exc.strerror}") from None
    manifest = _parse_manifest(raw)
    if not 8 <= len(signature) <= 72:
        raise OperatorError("manifest.sig is not an ECDSA P-256 signature")
    try:
        sha256, size = _hash_file(directory / "image.bin")
    except OSError as exc:
        raise OperatorError(f"cannot read {directory / 'image.bin'}: {exc.strerror}") from None
    if size != manifest["size"]:
        raise OperatorError("image.bin size differs from the manifest")
    if sha256 != manifest["sha256"]:
        raise OperatorError("image.bin SHA-256 differs from the manifest")
    return Update(directory, manifest, raw, signature)


def verify(update, public_path):
    """Raise unless the manifest signature verifies with this public key."""
    try:
        _load_public(public_path).verify(update.signature, update.raw, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        raise OperatorError("the manifest signature does not match that public key") from None


# --- the relay's store (operator CLI) ---


def _read_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        raise OperatorError(f"{path} is damaged: {exc}") from None


class Store:
    """`firmware/BOARD/VERSION/` updates, `assignments.json` (CLI) and `status.json` (serve)."""

    def __init__(self, root=None):
        self.root = Path(root or firmware_dir())

    @property
    def assignments_path(self):
        return self.root / "assignments.json"

    @property
    def status_path(self):
        return self.root / "status.json"

    def add(self, directory, *, public_key=None, unchecked=False):
        update = read_update(directory)
        if public_key is not None:
            verify(update, public_key)
        elif not unchecked:
            raise OperatorError(
                "pass the builder's public key with --public-key, or --unchecked for a test"
            )
        board, version = update.manifest["board"], update.manifest["version"]
        target = self.root / board / version
        if target.exists():
            raise OperatorError(f"{board} {version} is already stored; remove it first")
        target.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        staging = Path(tempfile.mkdtemp(prefix=".add-", dir=target.parent))
        try:
            for name in FILES:
                shutil.copyfile(update.directory / name, staging / name)
            read_update(staging)  # The copy, not the source, is what gadgets will get.
            os.replace(staging, target)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        return update.manifest

    def list(self):
        found = []
        if not self.root.is_dir():
            return found
        for board in sorted(
            p for p in self.root.iterdir() if p.is_dir() and BOARD.fullmatch(p.name)
        ):
            for version in sorted(p for p in board.iterdir() if p.is_dir()):
                try:
                    found.append(_parse_manifest((version / "manifest.json").read_bytes()))
                except (OSError, OperatorError):
                    continue
        return found

    def find(self, version, board=None):
        matches = [
            m for m in self.list() if m["version"] == version and board in (None, m["board"])
        ]
        if not matches:
            raise OperatorError(f"no stored update {version}" + (f" for {board}" if board else ""))
        if len(matches) > 1:
            raise OperatorError(f"{version} is stored for several boards; pass --board")
        return matches[0]

    def remove(self, board, version):
        target = self.root / board / version
        if not BOARD.fullmatch(board) or not (target / "manifest.json").exists():
            raise OperatorError(f"no stored update {board} {version}")
        self._write_assignments(
            {
                device: entry
                for device, entry in self.assignments().items()
                if (entry["board"], entry["version"]) != (board, version)
            }
        )
        shutil.rmtree(target)

    def assignments(self):
        return _read_json(self.assignments_path, {"devices": {}}).get("devices", {})

    def _write_assignments(self, devices):
        atomic_write(
            self.assignments_path, (json.dumps({"devices": devices}, indent=2) + "\n").encode()
        )

    def assign(self, device_id, version, board=None):
        if not DEVICE_ID.fullmatch(device_id or ""):
            raise OperatorError("not a valid device ID")
        manifest = self.find(version, board)
        devices = self.assignments()
        devices[device_id] = {
            "board": manifest["board"],
            "version": manifest["version"],
            "assigned_at": f"{_now()}#{os.urandom(4).hex()}",  # Unique: a new attempt.
        }
        self._write_assignments(devices)
        return devices[device_id]

    def withdraw(self, device_id):
        devices = self.assignments()
        if devices.pop(device_id, None) is None:
            raise OperatorError(f"{device_id} has no assigned update")
        self._write_assignments(devices)

    def status(self):
        return _read_json(self.status_path, {"devices": {}}).get("devices", {})


# --- delivery inside serve (hub ADR 012 §3) ---


def _b64(data):
    return base64.b64encode(data).decode("ascii")


class Updates:
    """Offers and chunks for gadgets that sent `"update": "ready"` in their hello."""

    def __init__(self, root=None, *, log=None, clock=time.monotonic):
        self.store = Store(root)
        self.log = log
        self.clock = clock
        self._stamp = None
        self._assigned = {}
        self._offers = {}  # (board, version) -> offer dict, without per-device state.
        self.sessions = {}  # device -> {"sid", "running", "sent"}
        self.active = {}  # device -> time of its last chunk request
        self.progress = {}  # device -> last logged quarter
        try:
            self.results = self.store.status()
        except OperatorError:
            self.results = {}

    # Assignments are re-read when the CLI replaces the file (a new inode).
    def _assignments(self):
        try:
            info = self.store.assignments_path.stat()
            stamp = (info.st_ino, info.st_mtime_ns, info.st_size)
        except OSError:
            stamp = None
        if stamp != self._stamp:
            try:
                self._assigned = self.store.assignments() if stamp else {}
            except OperatorError:
                return self._assigned  # Keep the last good file through a bad write.
            self._stamp = stamp
            self._offers.clear()
        return self._assigned

    def _log(self, event, device, version, **fields):
        if self.log is not None:
            record = {"event": event, "device": device, "version": version}
            record.update({k: v for k, v in fields.items() if v is not None})
            self.log.write(record)

    def _save(self, device, **fields):
        entry = dict(self.results.get(device, {}))
        entry.update(fields, at=_now())
        self.results[device] = entry
        try:
            atomic_write(
                self.store.status_path,
                (json.dumps({"devices": self.results}, indent=2) + "\n").encode(),
            )
        except OSError:
            pass  # Status is for the operator; a full disk never stops a gadget.

    def _offer_record(self, assigned):
        key = (assigned["board"], assigned["version"])
        if key not in self._offers:
            directory = self.store.root / key[0] / key[1]
            try:
                raw = (directory / "manifest.json").read_bytes()
                manifest = _parse_manifest(raw)
                signature = (directory / "manifest.sig").read_bytes()
            except (OSError, OperatorError):
                return None
            self._offers[key] = {
                "version": manifest["version"],
                "size": manifest["size"],
                "chunk": CHUNK,
                "manifest": _b64(raw),
                "signature": _b64(signature),
            }
        return self._offers[key]

    def _current(self, device, running):
        """The offer for this gadget now, or None."""
        assigned = self._assignments().get(device)
        if assigned is None or assigned["version"] == running:
            return None
        result = self.results.get(device, {})
        if (
            result.get("status") in STOPPED
            and result.get("version") == assigned["version"]
            and result.get("assigned_at") == assigned["assigned_at"]
        ):
            return None
        return self._offer_record(assigned)

    def _applied(self, device, version):
        result = self.results.get(device, {})
        return result.get("status") == "applied" and result.get("version") == version

    def hello(self, device, message, sid):
        running = message["device"]["firmware_version"]
        assigned = self._assignments().get(device)
        if (
            assigned is not None
            and assigned["version"] == running
            and not self._applied(device, running)
        ):
            self._log("update_applied", device, running, via="hello")
            self._save(
                device,
                version=running,
                status="applied",
                reason=None,
                assigned_at=assigned["assigned_at"],
            )
        if self.results.get(device, {}).get("reported") != running:
            self._save(device, reported=running)
        if message.get("update") != "ready":
            self.sessions.pop(device, None)
            return {}
        offer = self._current(device, running)
        self.sessions[device] = {"sid": sid, "running": running, "sent": offer}
        if offer is None:
            return {}
        self._log("update_offered", device, offer["version"], size=offer["size"])
        return {"update": offer}

    def poll(self, device, sid):
        session = self.sessions.get(device)
        if session is None or session["sid"] != sid:
            return {}
        offer = self._current(device, session["running"])
        if offer == session["sent"]:
            return {}
        before, session["sent"] = session["sent"], offer
        if offer is None:
            self._log("update_withdrawn", device, before["version"])
            self.active.pop(device, None)
        else:
            self._log("update_offered", device, offer["version"], size=offer["size"])
        return {"update": offer}

    def handle(self, device, sid, message):
        session = self.sessions.get(device)
        if session is None or session["sid"] != sid:
            raise GatewayError("invalid_request", "Updates need a hello with update ready")
        if message["type"] == "update_status":
            return self._status(device, session, message)
        return self._chunk(device, session, message)

    def _status(self, device, session, message):
        version, state = message["version"], message["status"]
        reason = message.get("reason")
        if state == "applied" and self._applied(device, version):
            return {"ok": True}  # Already logged from the hello.
        self._log(f"update_{state}", device, version, reason=reason)
        previous = self.results.get(device, {})
        if state == "rejected" and (previous.get("version"), previous.get("status")) == (
            version,
            "reverted",
        ):
            return {"ok": True}  # The gadget refuses a version it reverted; keep `reverted`.
        assigned = self._assignments().get(device) or {}
        self._save(
            device,
            version=version,
            status=state,
            reason=reason,
            assigned_at=assigned.get("assigned_at") if assigned.get("version") == version else None,
        )
        if state in STOPPED or state == "verified":
            self.active.pop(device, None)
        return {"ok": True}

    def _chunk(self, device, session, message):
        offer = self._current(device, session["running"])
        if offer is None or offer["version"] != message["version"]:
            raise GatewayError("update_unavailable", "That update is no longer offered")
        offset = message["offset"]
        if offset % offer["chunk"] or offset >= offer["size"]:
            raise GatewayError("invalid_request", "Offset is not a chunk of this update")
        now = self.clock()
        for other, seen in list(self.active.items()):
            if now - seen > ACTIVE_SECONDS:
                del self.active[other]
        if device not in self.active and len(self.active) >= MAX_DOWNLOADS:
            self._log("update_busy", device, offer["version"])
            raise GatewayError("busy", "Too many gadgets are updating; try again later")
        assigned = self._assignments()[device]
        path = self.store.root / assigned["board"] / assigned["version"] / "image.bin"
        try:
            with open(path, "rb") as handle:
                handle.seek(offset)
                data = handle.read(offer["chunk"])
        except OSError:
            raise GatewayError("update_unavailable", "That update is no longer stored") from None
        if len(data) != min(offer["chunk"], offer["size"] - offset):
            raise GatewayError("update_unavailable", "That update is no longer stored")
        self.active[device] = now
        if offset == 0:
            self.progress[device] = 0
        quarter = (offset + len(data)) * 4 // offer["size"]
        for step in range(self.progress.get(device, 0) + 1, min(quarter, 3) + 1):
            self._log("update_progress", device, offer["version"], percent=step * 25)
            self._save(device, version=offer["version"], status="downloading", progress=step * 25)
        self.progress[device] = max(self.progress.get(device, 0), min(quarter, 3))
        return {"ok": True, "data": _b64(data)}

    def disconnect(self, device, sid):
        session = self.sessions.get(device)
        if session is not None and session["sid"] == sid:
            del self.sessions[device]
            self.active.pop(device, None)
