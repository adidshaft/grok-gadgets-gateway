"""Backups of the config directory: one mode-0600 tgz plus its SHA-256.

The archive holds live secrets (device and MCP tokens, sign-ins, the receipt key), so it is
written owner-only, into a mode-0700 folder outside the config directory. Restore by
extracting it into an empty XDG_CONFIG_HOME; see docs/relay-host.md.
"""

import hashlib
import os
import re
import tarfile
from datetime import UTC, datetime
from pathlib import Path

from .operator import OperatorError, config_dir

PREFIX = "grok-gadgets-backup-"
NAME = re.compile(r"grok-gadgets-backup-\d{8}T\d{6}Z\.tgz")


def default_backup_dir():
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "grok-gadgets" / "backups"


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_temp(path):
    """A half-written atomic_write temp file (.NAME.xxxxxxxx) whose target sits beside it."""
    name = path.name
    if not name.startswith(".") or name.count(".") < 2:
        return False
    target = name[1:].rsplit(".", 1)[0]
    return (path.parent / target).exists()


def _members(source):
    """Regular files and folders under source, sorted; never symlinks, sockets or temp files."""
    for root, dirs, files in os.walk(source, followlinks=False):
        dirs.sort()
        here = Path(root)
        yield here
        for name in sorted(files):
            path = here / name
            if path.is_symlink() or not path.is_file() or _is_temp(path):
                continue
            yield path


def create_backup(out=None, *, source=None, keep=None, now=None):
    """Write the archive and its .sha256 file. Returns path, digest, files and pruned."""
    source = Path(source or config_dir())
    if not source.is_dir():
        raise OperatorError(f"no config directory at {source}; nothing to back up")
    out = Path(out or default_backup_dir()).expanduser()
    resolved_out, resolved_source = out.resolve(), source.resolve()
    if resolved_out == resolved_source or resolved_source in resolved_out.parents:
        raise OperatorError("the backup folder must be outside the config directory")
    if keep is not None and keep < 1:
        raise OperatorError("--keep must be 1 or more")
    out.mkdir(parents=True, exist_ok=True)
    os.chmod(out, 0o700)
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    path = out / f"{PREFIX}{stamp}.tgz"
    if path.exists():
        raise OperatorError(f"{path} already exists")
    partial = out / f".{path.name}.partial"
    files = 0
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as raw, tarfile.open(fileobj=raw, mode="w:gz") as archive:
            for member in _members(source):
                arcname = Path(source.name) / member.relative_to(source)
                archive.add(member, arcname=str(arcname), recursive=False)
                files += member.is_file()
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(partial, path)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    os.chmod(path, 0o600)
    digest = _sha256(path)
    checksum = out / f"{path.name}.sha256"
    checksum.write_text(f"{digest}  {path.name}\n")
    os.chmod(checksum, 0o600)
    pruned = prune(out, keep) if keep else []
    return {"path": path, "sha256": digest, "files": files, "pruned": pruned}


def prune(out, keep):
    """Delete the oldest backups (and their .sha256) beyond the newest `keep`."""
    found = sorted(p for p in Path(out).iterdir() if NAME.fullmatch(p.name))
    old = found[:-keep] if keep < len(found) else []
    for path in old:
        path.unlink()
        Path(f"{path}.sha256").unlink(missing_ok=True)
    return old


def verify_backup(path):
    """True when the archive matches the digest in its .sha256 file beside it."""
    path = Path(path)
    checksum = Path(f"{path}.sha256")
    if not path.is_file() or not checksum.is_file():
        raise OperatorError(f"need both {path.name} and {checksum.name}")
    expected = checksum.read_text().split()[0]
    return _sha256(path) == expected
