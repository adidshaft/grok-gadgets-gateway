"""A simulated fleet for measuring a relay (GW-SCALE-001, hub ADR 014 §1).

`fleet run` connects N software gadgets to a relay's device port the way the ESP32 does:
TLS with the relay CA pinned and the name checked, a protocol 0.1.0 hello, a poll every
100 ms and a state frame every 3 s. It ramps up within the relay's per-address limits and
never bypasses them. An optional driver sends `gadgets_command` through MCP and times each
call until the gadget's `executed` result. `fleet sample` runs on the relay host and records
the gateway's CPU share and memory.

The files hold counts and timings only: never a token, an address or a host name.
"""

import asyncio
import contextlib
import csv
import json
import multiprocessing
import os
import random
import secrets
import ssl
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .operator import OperatorError, read_device_token_file
from .protocol import MAX_TCP_FRAME
from .transport import FATAL_CODES

CAPABILITY = "display.text"
SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string", "maxLength": 32}},
    "required": ["text"],
    "additionalProperties": False,
    "description": "Show up to 32 characters on a simulated load-test gadget.",
}
REPLY_SECONDS = 15  # The relay's idle timeout: a reply later than this is a lost session.
RESERVOIR = 50_000


@dataclass
class FleetConfig:
    batch: Path | list  # One enroll --batch folder, or several.
    count: int
    host: str
    port: int
    ca: Path
    connect: str | None = None  # Address to dial instead of resolving `host` (the TLS name).
    poll_ms: int = 100
    state_ms: int = 3000
    ramp_per_minute: float = 18
    duration: float = 900.0
    workers: int = 1
    every: float = 5.0
    retry_seconds: float = 5.0
    mcp_url: str | None = None
    mcp_token: str | None = field(default=None, repr=False)
    commands_per_second: float = 1.0
    max_in_flight: int = 8


def load_tokens(batch, count):
    """The first `count` (device ID, token) pairs from one or more `enroll --batch` folders."""
    folders = [Path(batch)] if isinstance(batch, (str, Path)) else [Path(b) for b in batch]
    files = []
    for folder in folders:
        tokens = folder / "tokens"
        files += sorted(tokens.glob("*.token")) if tokens.is_dir() else []
    if len(files) < count:
        raise OperatorError(
            f"the batch folders hold only {len(files)} token files. A batch holds up to 500: "
            "grok-gadgets-gateway enroll --batch IDS.csv --out DIR --no-labels, then pass "
            "--batch once for each folder"
        )
    return [(path.stem, read_device_token_file(path)) for path in files[:count]]


def percentile(values, p):
    """Nearest-rank percentile; None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, -(-len(ordered) * p // 100))
    return ordered[int(rank) - 1]


def offered_rate(count, poll_ms):
    """Frames a second the fleet sends. A state frame takes a poll's place, as on the ESP32."""
    return count * 1000 / poll_ms


def _ms(values):
    return {
        f"p{p}": None if percentile(values, p) is None else round(percentile(values, p) * 1000, 1)
        for p in (50, 95, 99)
    }


class Reservoir:
    """A fixed-size uniform sample of a long stream of timings."""

    def __init__(self, size=RESERVOIR, seed=None):
        self.size = size
        self.values = []
        self.seen = 0
        self.random = random.Random(seed)

    def add(self, value):
        self.seen += 1
        if len(self.values) < self.size:
            self.values.append(value)
        else:
            slot = self.random.randrange(self.seen)
            if slot < self.size:
                self.values[slot] = value


class Stats:
    def __init__(self):
        self.online = 0
        self.reset()

    def reset(self):
        """Start the measured window: counters from zero, `online` kept."""
        self.hellos = self.sent = self.answered = self.acks = self.late = self.drops = 0
        self.errors = {}
        self.rtt = Reservoir()

    def error(self, code):
        self.errors[code] = self.errors.get(code, 0) + 1


class Pacer:
    """At most `per_minute` new connections from this process, evenly spaced."""

    def __init__(self, per_minute):
        self.gap = 60 / per_minute
        self.next = 0.0
        self.lock = asyncio.Lock()

    async def wait(self):
        async with self.lock:
            loop = asyncio.get_running_loop()
            delay = self.next - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            self.next = max(self.next, loop.time()) + self.gap


def client_context(ca):
    context = ssl.create_default_context(cafile=str(ca))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def _hello(device_id, token, boot):
    return {
        "type": "hello",
        "protocol_version": "0.1.0",
        "token": token,
        "device": {
            "device_id": device_id,
            "model": "Grok Gadgets fleet (simulated)",
            "firmware_version": "fleet-1",
            "boot_id": boot,
            "simulated": True,
            "capabilities": [CAPABILITY],
            "capability_schemas": {CAPABILITY: SCHEMA},
            "state": {"display": {"text": ""}},
        },
    }


async def _exchange(reader, writer, message):
    writer.write((json.dumps(message, separators=(",", ":")) + "\n").encode())
    await writer.drain()
    line = await asyncio.wait_for(reader.readline(), REPLY_SECONDS)
    if not line:
        raise ConnectionError("closed")
    return json.loads(line)


async def _pause(stop, seconds):
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), max(0.0, seconds))


async def _session(reader, writer, cfg, stats, stop):
    """Poll on an open session until `stop`, a fatal error or a lost connection."""
    loop = asyncio.get_running_loop()
    interval, state_every = cfg.poll_ms / 1000, cfg.state_ms / 1000
    text = ""
    next_poll = loop.time()
    next_state = next_poll + state_every
    while not stop.is_set():
        if loop.time() >= next_state:
            frame = {"type": "state", "state": {"display": {"text": text}}}
            next_state += state_every
        else:
            frame = {"type": "poll"}
        started = loop.time()
        reply = await _exchange(reader, writer, frame)
        stats.sent += 1
        if not reply.get("ok"):
            stats.error(reply["error"]["code"])
            if reply["error"]["code"] in FATAL_CODES:
                return
        else:
            stats.answered += 1
            stats.rtt.add(loop.time() - started)
        for command in reply.get("commands") or []:
            text = str(command.get("arguments", {}).get("text", ""))[:32]
            ack = {
                "type": "ack",
                "command_id": command["command_id"],
                "status": "executed",
                "state": {"display": {"text": text}},
            }
            reply = await _exchange(reader, writer, ack)
            stats.sent += 1
            stats.acks += 1
            if reply.get("ok"):
                stats.answered += 1
            else:
                stats.error(reply["error"]["code"])
        next_poll += interval
        delay = next_poll - loop.time()
        if delay < 0:
            stats.late += 1  # No catch-up burst: the next poll goes now.
            next_poll = loop.time()
        await _pause(stop, delay)


async def gadget(device_id, token, cfg, stats, stop, pacer=None):
    """One simulated gadget: connect, hello, poll until `stop`; reconnect after a loss."""
    context = client_context(cfg.ca)
    boot = 0
    while not stop.is_set():
        if pacer is not None:
            await pacer.wait()
        if stop.is_set():
            break
        boot += 1
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    cfg.connect or cfg.host,
                    cfg.port,
                    ssl=context,
                    server_hostname=cfg.host,
                    limit=MAX_TCP_FRAME,
                ),
                10,
            )
        except ssl.SSLError:
            stats.error("tls")
            await _pause(stop, cfg.retry_seconds)
            continue
        except (OSError, TimeoutError):
            stats.error("connect")
            await _pause(stop, cfg.retry_seconds)
            continue
        online = False
        try:
            reply = await _exchange(
                reader, writer, _hello(device_id, token, f"fleet-{secrets.token_hex(4)}-{boot}")
            )
            if reply.get("ok"):
                online = True
                stats.online += 1
                stats.hellos += 1
                await _session(reader, writer, cfg, stats, stop)
            else:
                stats.error(reply["error"]["code"])
        except (ConnectionError, OSError, TimeoutError, ValueError, asyncio.IncompleteReadError):
            if online:
                stats.drops += 1
            else:
                stats.error("closed")
        finally:
            if online:
                stats.online -= 1
            writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), 2)
        if not stop.is_set():
            await _pause(stop, cfg.retry_seconds)


def _raise_file_limit(count):
    with contextlib.suppress(ImportError, ValueError, OSError):
        import resource

        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        wanted = count + 256
        if soft != resource.RLIM_INFINITY and soft < wanted:
            if hard != resource.RLIM_INFINITY:
                wanted = min(wanted, hard)
            resource.setrlimit(resource.RLIMIT_NOFILE, (wanted, hard))


async def _slice(cfg, pairs, ramp_per_minute, ready, measuring, stopping):
    """Run a share of the fleet. `ready()` once all are online; `measuring()` and
    `stopping()` are polled flags, so the same code runs in a worker process."""
    _raise_file_limit(len(pairs))
    stats = Stats()
    stop = asyncio.Event()
    pacer = Pacer(ramp_per_minute)
    tasks = [
        asyncio.create_task(gadget(device_id, token, cfg, stats, stop, pacer))
        for device_id, token in pairs
    ]
    loop = asyncio.get_running_loop()
    started = loop.time()
    rows, said_ready, measured_at = [], False, None
    last_answered, last_row = 0, started
    try:
        while not stopping():
            await asyncio.sleep(min(0.1, cfg.every))
            now = loop.time()
            if not said_ready and stats.online >= len(pairs):
                said_ready = True
                ready(now - started)
            if measured_at is None and measuring():
                measured_at = now
                stats.reset()
                last_answered = 0
            if now - last_row >= cfg.every:
                rows.append(
                    {
                        "online": stats.online,
                        "answered": stats.answered - last_answered,
                        "seconds": now - last_row,
                        "errors": sum(stats.errors.values()),
                        "drops": stats.drops,
                        "late": stats.late,
                        "phase": "measure" if measured_at is not None else "ramp",
                    }
                )
                last_answered, last_row = stats.answered, now
    finally:
        online_at_end = stats.online
        ended = loop.time()
        stop.set()
        await asyncio.gather(*tasks, return_exceptions=True)
    return {
        "gadgets": len(pairs),
        "online_at_end": online_at_end,
        "measured_seconds": 0.0 if measured_at is None else ended - measured_at,
        "hellos": stats.hellos,
        "sent": stats.sent,
        "answered": stats.answered,
        "acks": stats.acks,
        "late": stats.late,
        "drops": stats.drops,
        "errors": stats.errors,
        "rtt": stats.rtt.values,
        "rows": rows,
    }


def _worker(cfg, pairs, ramp_per_minute, queue, measure, stop):
    def ready(seconds):
        queue.put(("ready", seconds))

    result = asyncio.run(_slice(cfg, pairs, ramp_per_minute, ready, measure.is_set, stop.is_set))
    queue.put(("done", result))


async def drive(cfg, device_ids, duration):
    """Send `gadgets_command` at a fixed rate to random gadgets; time each call."""
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    from .rehearse import _client_info

    latencies, outcomes, sent = Reservoir(), Counter(), 0
    gate = asyncio.Semaphore(cfg.max_in_flight)
    headers = {"Authorization": "Bearer " + cfg.mcp_token}
    async with (
        httpx2.AsyncClient(headers=headers, timeout=httpx2.Timeout(30.0, read=300.0)) as http,
        streamable_http_client(cfg.mcp_url, http_client=http) as (read, write),
        ClientSession(read, write, client_info=_client_info()) as session,
    ):
        await session.initialize()

        async def one(device_id):
            async with gate:
                started = time.monotonic()
                try:
                    result = await session.call_tool(
                        "gadgets_command",
                        {
                            "device_id": device_id,
                            "capability": CAPABILITY,
                            "arguments": {"text": "load test"},
                        },
                    )
                    value = result.structured_content or json.loads(result.content[0].text)
                    status = (
                        value["command"]["status"] if value.get("ok") else value["error"]["code"]
                    )
                except Exception:  # noqa: BLE001 - count it; the run goes on
                    status = "client_error"
                outcomes[status] += 1
                if status == "executed":
                    latencies.add(time.monotonic() - started)

        tasks = []
        loop = asyncio.get_running_loop()
        end = loop.time() + duration
        next_call = loop.time()
        while loop.time() < end:
            tasks.append(asyncio.create_task(one(random.choice(device_ids))))
            sent += 1
            next_call += 1 / cfg.commands_per_second
            await asyncio.sleep(max(0.0, next_call - loop.time()))
        await asyncio.gather(*tasks)
    return {"sent": sent, "outcomes": dict(outcomes), "latency_ms": _ms(latencies.values)}


def _merge_rows(results, every):
    """One fleet.csv row per interval: the workers' rows added up by index."""
    merged = []
    for index in range(max(len(r["rows"]) for r in results)):
        part = [r["rows"][index] for r in results if index < len(r["rows"])]
        seconds = max(row["seconds"] for row in part) or every
        merged.append(
            {
                "elapsed_s": round((index + 1) * every, 1),
                "phase": "measure" if any(r["phase"] == "measure" for r in part) else "ramp",
                "online": sum(row["online"] for row in part),
                "frames_per_s": round(sum(row["answered"] for row in part) / seconds, 1),
                "errors": sum(row["errors"] for row in part),
                "drops": sum(row["drops"] for row in part),
                "late": sum(row["late"] for row in part),
            }
        )
    return merged


async def run(cfg, out):
    """The whole test: ramp up, measure for `duration`, stop; write and return the summary."""
    if cfg.workers < 1 or cfg.count < cfg.workers:
        raise OperatorError("--workers must be 1 to --count")
    pairs = load_tokens(cfg.batch, cfg.count)
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    slices = [pairs[index :: cfg.workers] for index in range(cfg.workers)]
    rate = cfg.ramp_per_minute / cfg.workers
    # The ramp, plus a minute for the last handshakes and any refused first tries.
    ramp_limit = cfg.count / cfg.ramp_per_minute * 60 + 60
    loop = asyncio.get_running_loop()
    began = loop.time()
    ready_times = []
    if cfg.workers == 1:
        ready, measure, stop = asyncio.Event(), asyncio.Event(), asyncio.Event()

        def said_ready(seconds):
            ready_times.append(seconds)
            ready.set()

        task = asyncio.create_task(
            _slice(cfg, slices[0], rate, said_ready, measure.is_set, stop.is_set)
        )
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(ready.wait(), ramp_limit)
        measure.set()
        measure_began = loop.time()
        commands = await _measure(cfg, pairs)
        stop.set()
        results = [await task]
    else:
        context = multiprocessing.get_context("spawn")
        queue, measure, stop = context.Queue(), context.Event(), context.Event()
        processes = [
            context.Process(target=_worker, args=(cfg, part, rate, queue, measure, stop))
            for part in slices
        ]
        for process in processes:
            process.start()
        results = []
        deadline = loop.time() + ramp_limit
        while len(ready_times) < cfg.workers and loop.time() < deadline:
            with contextlib.suppress(Exception):
                kind, value = await asyncio.to_thread(queue.get, True, 1.0)
                (ready_times if kind == "ready" else results).append(value)
        measure.set()
        measure_began = loop.time()
        commands = await _measure(cfg, pairs)
        stop.set()
        while len(results) < cfg.workers:
            kind, value = await asyncio.to_thread(queue.get, True, 60)
            if kind == "done":
                results.append(value)
        for process in processes:
            await asyncio.to_thread(process.join, 10)
    measured = max(r["measured_seconds"] for r in results) or 1.0
    errors = Counter()
    for result in results:
        errors.update(result["errors"])
    rtt = [value for result in results for value in result["rtt"]]
    summary = {
        "gadgets": cfg.count,
        "workers": cfg.workers,
        "poll_ms": cfg.poll_ms,
        "state_ms": cfg.state_ms,
        "ramp_per_minute": cfg.ramp_per_minute,
        "ready_after_seconds": round(max(ready_times), 1)
        if len(ready_times) == cfg.workers
        else None,
        "ramp_seconds": round(measure_began - began, 1),
        "measured_seconds": round(measured, 1),
        "online_at_end": sum(r["online_at_end"] for r in results),
        "frames_offered_per_second": round(offered_rate(cfg.count, cfg.poll_ms), 1),
        "frames_answered_per_second": round(sum(r["answered"] for r in results) / measured, 1),
        "poll_rtt_ms": _ms(rtt),
        "late_polls": sum(r["late"] for r in results),
        "drops": sum(r["drops"] for r in results),
        "errors": dict(errors),
        "commands": commands,
    }
    rows = _merge_rows(results, cfg.every)
    with (out / "fleet.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle, ["elapsed_s", "phase", "online", "frames_per_s", "errors", "drops", "late"]
        )
        writer.writeheader()
        writer.writerows(rows)
    (out / "summary.json").write_text(json.dumps(summary, indent=1) + "\n")
    return summary


async def _measure(cfg, pairs):
    if cfg.mcp_url and cfg.mcp_token:
        return await drive(cfg, [device_id for device_id, _ in pairs], cfg.duration)
    await asyncio.sleep(cfg.duration)
    return {"sent": 0, "outcomes": {}, "latency_ms": _ms([])}


# --- The sampler, on the relay host. ---


@dataclass
class ProcessUse:
    cpu_seconds: float
    rss_bytes: int


def _clock_seconds(text):
    """`[[DD-]HH:]MM:SS[.ss]` from ps -o time= -> seconds."""
    days, _, rest = text.strip().rpartition("-")
    parts = [float(part) for part in rest.split(":")]
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + part
    return seconds + (int(days) * 86400 if days else 0)


def read_process(pid):
    """CPU seconds so far and resident memory of one process."""
    proc = Path(f"/proc/{pid}")
    if sys.platform.startswith("linux") and Path("/proc/self").exists():
        try:
            stat = (proc / "stat").read_text()
            status = (proc / "status").read_text()
        except FileNotFoundError:
            raise OperatorError(f"no process {pid}") from None
        fields = stat.rpartition(")")[2].split()
        ticks = os.sysconf("SC_CLK_TCK")
        cpu = (int(fields[11]) + int(fields[12])) / ticks
        rss = next(int(line.split()[1]) for line in status.splitlines() if line.startswith("VmRSS"))
        return ProcessUse(cpu, rss * 1024)
    result = subprocess.run(
        ["ps", "-o", "time=,rss=", "-p", str(int(pid))], capture_output=True, text=True, check=False
    )
    if result.returncode or not result.stdout.strip():
        raise OperatorError(f"no process {pid}")
    clock, rss = result.stdout.split()
    return ProcessUse(_clock_seconds(clock), int(rss) * 1024)


def sample(pid, path, every=5.0, duration=None, clock=time.monotonic):
    """Write `elapsed_s,cpu_pct,rss_mb` every `every` s until `duration`, Ctrl-C or the
    process ends; return and save (next to `path`, as .json) the summary."""
    path = Path(path)
    first = previous = read_process(pid)
    began = then = clock()
    cpu, peak = [], first.rss_bytes
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["elapsed_s", "cpu_pct", "rss_mb"])
        try:
            while duration is None or clock() - began < duration:
                time.sleep(every)
                try:
                    current = read_process(pid)
                except OperatorError:
                    break
                now = clock()
                share = (current.cpu_seconds - previous.cpu_seconds) / max(now - then, 1e-9) * 100
                cpu.append(share)
                peak = max(peak, current.rss_bytes)
                writer.writerow(
                    [round(now - began, 1), round(share, 1), round(current.rss_bytes / 2**20, 1)]
                )
                handle.flush()
                previous, then = current, now
        except KeyboardInterrupt:
            pass
    summary = {
        "samples": len(cpu),
        "every_s": every,
        "cpu_pct_mean": round(sum(cpu) / len(cpu), 1) if cpu else None,
        "cpu_pct_p95": None if not cpu else round(percentile(cpu, 95), 1),
        "rss_mb_start": round(first.rss_bytes / 2**20, 1),
        "rss_mb_peak": round(peak / 2**20, 1),
    }
    path.with_suffix(".json").write_text(json.dumps(summary, indent=1) + "\n")
    return summary


def describe(summary):
    """The summary as lines for a terminal."""
    rtt, commands = summary["poll_rtt_ms"], summary["commands"]
    lines = [
        (
            f"gadgets {summary['gadgets']} (online at end {summary['online_at_end']}), "
            f"poll {summary['poll_ms']} ms, measured {summary['measured_seconds']} s"
        ),
        (
            f"frames a second: offered {summary['frames_offered_per_second']}, "
            f"answered {summary['frames_answered_per_second']}"
        ),
        f"poll round trip ms: p50 {rtt['p50']}, p95 {rtt['p95']}, p99 {rtt['p99']}",
        (
            f"late polls {summary['late_polls']}, drops {summary['drops']}, "
            f"errors {summary['errors'] or 'none'}"
        ),
    ]
    if commands["sent"]:
        latency = commands["latency_ms"]
        lines.append(
            f"commands {commands['sent']}: {commands['outcomes']}; latency ms p50 "
            f"{latency['p50']}, p95 {latency['p95']}, p99 {latency['p99']}"
        )
    return lines
