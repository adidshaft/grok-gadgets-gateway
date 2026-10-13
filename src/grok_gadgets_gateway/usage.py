"""Usage records, the `usage` summary and the opt-in `--usage-hook` (gateway #132).

The gateway counts its own tool calls. It never reads a Grok Bot account; any credits
figure is an estimate from a rate that the developer sets or calibrates.
"""

import json
import os
import queue
import shlex
import subprocess
import threading
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from .operator import OperatorError, atomic_write, config_dir

# The only fields a hook ever receives: no token, arguments, argument hash, state or agent.
HOOK_FIELDS = (
    "ts",
    "request_id",
    "transport",
    "token",
    "remote",
    "client",
    "tool",
    "device_id",
    "capability",
    "outcome",
    "status",
    "simulated",
    "duration_ms",
    "bytes_in",
    "bytes_out",
)
GROUPS = ("token", "device", "capability", "day")
_GROUP_FIELD = {"token": "token", "device": "device_id", "capability": "capability"}
ESTIMATE_LABEL = "estimate"


def json_size(value):
    """Bytes of the compact JSON form, as a tool payload size."""
    return len(json.dumps(value, separators=(",", ":"), default=str).encode())


def hook_line(record):
    line = {"event": "usage"}
    line.update({key: record[key] for key in HOOK_FIELDS if record.get(key) is not None})
    return json.dumps(line, separators=(",", ":"), default=str) + "\n"


class UsageHook:
    """Feed one JSON line per tool call to a long-running process, never blocking a call.

    `send` only puts the line on a bounded queue. A writer thread starts the process and
    writes to its stdin. A full queue or a dead hook drops lines and counts them.
    """

    RESTART_SECONDS = 10.0

    def __init__(self, command, queue_size=1000, clock=time.monotonic):
        argv = shlex.split(command) if isinstance(command, str) else list(command)
        if not argv:
            raise OperatorError("--usage-hook needs a command")
        self.argv = argv
        self.clock = clock
        self.queue = queue.Queue(maxsize=queue_size)
        self.sent = self.dropped = self.starts = 0
        self.process = None
        self.last_start = None
        self.thread = threading.Thread(target=self._run, name="usage-hook", daemon=True)
        self.thread.start()

    def send(self, record):
        try:
            self.queue.put_nowait(hook_line(record))
        except queue.Full:
            self.dropped += 1

    def _environment(self):
        return {k: v for k, v in os.environ.items() if not k.startswith("GROK_GADGETS_")}

    def _ensure_process(self):
        if self.process is not None and self.process.poll() is None:
            return True
        now = self.clock()
        if self.last_start is not None and now - self.last_start < self.RESTART_SECONDS:
            return False
        self.last_start = now
        try:
            self.process = subprocess.Popen(
                self.argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=self._environment(),
                close_fds=True,
            )
        except OSError:
            self.process = None
            return False
        self.starts += 1
        return True

    def _run(self):
        while True:
            line = self.queue.get()
            if line is None:
                return
            if not self._ensure_process():
                self.dropped += 1
                continue
            try:
                self.process.stdin.write(line.encode())
                self.process.stdin.flush()
                self.sent += 1
            except (OSError, ValueError):
                self.dropped += 1

    def close(self, timeout=2.0):
        """Stop the writer, close the hook's stdin and wait briefly; returns the counters."""
        try:
            self.queue.put(None, timeout=timeout)
        except queue.Full:
            pass
        self.thread.join(timeout)
        process = self.process
        if process is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
            try:
                process.wait(timeout)
            except subprocess.TimeoutExpired:
                process.terminate()
        return {"sent": self.sent, "dropped": self.dropped, "starts": self.starts}


def default_log():
    """The always-on service's request log, if this computer has one."""
    from .background import log_dir, platform_name

    try:
        return log_dir(platform_name()) / "requests.jsonl"
    except OperatorError:
        return None


def parse_time(text, end=False):
    """ISO date or date-time; a plain date covers the whole UTC day. Naive means UTC."""
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        raise OperatorError(f"not an ISO date or time: {text}") from None
    if len(text) == 10 and end:
        value = value.replace(hour=23, minute=59, second=59, microsecond=999999)
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def last_record(paths, match, tail_bytes=256 * 1024):
    """The newest JSON line in the logs (oldest file first) for which `match` is true.

    Reads only the end of each file, newest file first, so a large log stays cheap.
    """
    for path in reversed([Path(p) for p in paths]):
        try:
            with path.open("rb") as file:
                file.seek(max(file.seek(0, os.SEEK_END) - tail_bytes, 0))
                lines = file.read().decode("utf-8", "replace").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and match(record):
                return record
    return None


def read_calls(paths, since=None, until=None, token=None):
    """`mcp_tool_call` records from request logs, in the window and for the token."""
    calls = []
    for path in paths:
        try:
            lines = Path(path).read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise OperatorError(f"cannot read {path}: {exc.strerror}") from None
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict) or record.get("event") != "mcp_tool_call":
                continue
            try:
                when = datetime.fromisoformat(record["ts"])
            except (KeyError, TypeError, ValueError):
                continue
            if (since and when < since) or (until and when > until):
                continue
            if token is not None and record.get("token") != token:
                continue
            calls.append(record)
    return calls


def _empty():
    return {"calls": 0, "errors": 0, "commands": 0, "duration_ms": 0.0, "bytes": 0}


def _add(row, call):
    row["calls"] += 1
    row["errors"] += call.get("outcome") != "ok"
    row["commands"] += call.get("tool") == "gadgets_command"
    row["duration_ms"] = round(row["duration_ms"] + float(call.get("duration_ms") or 0), 1)
    row["bytes"] += int(call.get("bytes_in") or 0) + int(call.get("bytes_out") or 0)


def summarize(calls, rate=None):
    """Totals and one table per group. With a rate, each row gets a labelled estimate."""
    total = _empty()
    groups = {name: defaultdict(_empty) for name in GROUPS}
    for call in calls:
        _add(total, call)
        for name in GROUPS:
            key = call.get("ts", "")[:10] if name == "day" else call.get(_GROUP_FIELD[name])
            _add(groups[name][key if key is not None else "-"], call)
    summary = {"total": total, **{k: dict(sorted(v.items())) for k, v in groups.items()}}
    if rate is not None:
        summary[ESTIMATE_LABEL] = {
            "label": "estimate, not a Grok Bot reading",
            "rate_per_call": rate["rate"],
            "unit": rate["unit"],
            "source": rate.get("source", "set by the developer"),
        }
        for row in [total, *(r for table in groups.values() for r in table.values())]:
            row[ESTIMATE_LABEL] = round(row["calls"] * rate["rate"], 6)
    return summary


def rate_path():
    return config_dir() / "usage-rate.json"


def calibrate(calls, used, unit, since, until, path=None):
    """Rate = owner-measured usage / gateway calls in the same window; saved as an estimate."""
    if not calls:
        raise OperatorError("no tool calls in that window; nothing to calibrate")
    if used < 0:
        raise OperatorError("--calibrate must be more than zero")
    if used == 0:
        # A page that rounds to whole percent often shows no change; a zero rate would then
        # report every later call as free. Nothing is saved.
        raise OperatorError(
            "--calibrate 0 would save a zero rate, so nothing was saved. Use a longer window "
            "until the usage page shows a change"
        )
    rate = {
        "rate": used / len(calls),
        "unit": unit,
        "source": (
            f"calibrated: {used} {unit} measured over {len(calls)} calls "
            f"from {since.isoformat()} to {until.isoformat()}"
        ),
        "calibrated_at": datetime.now(UTC).isoformat(),
    }
    atomic_write(Path(path or rate_path()), (json.dumps(rate, indent=2) + "\n").encode())
    return rate


def read_rate(path=None):
    path = Path(path or rate_path())
    try:
        rate = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise OperatorError(f"{path} is damaged; run usage --calibrate again") from None
    if not isinstance(rate.get("rate"), (int, float)) or not isinstance(rate.get("unit"), str):
        raise OperatorError(f"{path} is damaged; run usage --calibrate again")
    return rate


def format_summary(summary):
    estimate = summary.get(ESTIMATE_LABEL)
    columns = ["calls", "errors", "commands", "duration_ms", "bytes"]
    if estimate:
        columns.append(ESTIMATE_LABEL)
    lines = []

    def table(title, rows):
        lines.append(f"by {title}:")
        for key, row in rows.items():
            cells = "  ".join(f"{c}={row[c]}" for c in columns)
            lines.append(f"  {key}  {cells}")

    total = summary["total"]
    lines.append("total:  " + "  ".join(f"{c}={total[c]}" for c in columns))
    for name in GROUPS:
        table(name, summary[name])
    if estimate:
        lines.append(
            f"{ESTIMATE_LABEL}: {estimate['rate_per_call']:.6g} {estimate['unit']} per call "
            f"({estimate['source']}). This is an estimate, not a Grok Bot reading."
        )
    lines.append("Days are UTC. Counts come from this gateway's request log only.")
    return "\n".join(lines)
