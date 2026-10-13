"""Size-based rotation for the request log and the service log (hub ADR 008, gateway #142).

`requests.jsonl` rotates by rename: `requests.jsonl.1` is the newest old file and
`requests.jsonl.5` the oldest. The service log is opened in append mode by launchd or
systemd, so the gateway copies it to `gateway.log.1` and truncates it in place.
"""

import os
import shutil
import stat
from pathlib import Path

MAX_BYTES = 5 * 1024 * 1024
KEEP = 5


def rotated(path, keep=KEEP):
    """The log and its rotated files that exist, oldest first."""
    path = Path(path)
    older = [Path(f"{path}.{n}") for n in range(keep, 0, -1)]
    return [p for p in [*older, path] if p.is_file()]


def shift(path, keep=KEEP):
    """Move path.N to path.N+1, dropping the oldest, so path.1 is free."""
    Path(f"{path}.{keep}").unlink(missing_ok=True)
    for n in range(keep - 1, 0, -1):
        older = Path(f"{path}.{n}")
        if older.exists():
            older.rename(f"{path}.{n + 1}")


def copy_truncate(path, keep=KEEP):
    """Keep a copy of an append-mode log that another process holds open, then empty it."""
    shift(path, keep)
    shutil.copyfile(path, f"{path}.1")
    os.chmod(f"{path}.1", stat.S_IMODE(os.stat(path).st_mode))
    os.truncate(path, 0)


def service_log_of(stream):
    """The service log path when `stream` is that file (the gateway runs as the service)."""
    from .background import log_dir, platform_name

    try:
        mine = os.fstat(stream.fileno())
        path = log_dir(platform_name()) / "gateway.log"
        theirs = os.stat(path)
    except (AttributeError, ValueError, OSError):  # OperatorError is a ValueError
        return None
    same = (mine.st_dev, mine.st_ino) == (theirs.st_dev, theirs.st_ino)
    return path if same and stat.S_ISREG(mine.st_mode) else None
