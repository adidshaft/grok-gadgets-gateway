"""Simulated fleet and the relay's device limit (GW-SCALE-001, hub ADR 014 §1)."""

import asyncio
import csv
import json
import os

import pytest
from test_domain import hello
from test_relay import NAME, free_port, pki  # noqa: F401 - pytest fixture

from grok_gadgets_gateway import fleet, lan
from grok_gadgets_gateway.batch import count_ids, make_batch
from grok_gadgets_gateway.cli import main
from grok_gadgets_gateway.domain import DEFAULT_MAX_DEVICES, Gateway, GatewayError
from grok_gadgets_gateway.mcp_server import RateLimiter
from grok_gadgets_gateway.operator import OperatorError
from grok_gadgets_gateway.service import serve_gateway
from grok_gadgets_gateway.transport import Credentials, DeviceServer

# The device limit


def test_the_registry_holds_64_devices_by_default():
    gateway = Gateway()
    assert DEFAULT_MAX_DEVICES == 64
    for n in range(64):
        gateway.register(hello(f"dev-{n}"))
    with pytest.raises(GatewayError) as exc:
        gateway.register(hello("dev-64"))
    assert exc.value.code == "busy"


def test_max_devices_raises_and_lowers_the_registry_limit():
    gateway = Gateway(max_devices=3)
    for n in range(3):
        gateway.register(hello(f"dev-{n}"))
    with pytest.raises(GatewayError):
        gateway.register(hello("dev-3"))
    # A known device may always come back with a new session.
    gateway.register(hello("dev-1"))
    big = Gateway(max_devices=200)
    for n in range(200):
        big.register(hello(f"dev-{n}"))


@pytest.mark.parametrize("value", [0, 2001, "64", True])
def test_max_devices_must_be_1_to_2000(value):
    with pytest.raises(ValueError):
        Gateway(max_devices=value)


@pytest.mark.parametrize("value", ["0", "2001", "many"])
def test_serve_refuses_a_bad_max_devices(value, capsys):
    with pytest.raises(SystemExit):
        main(["serve", "--max-devices", value])
    assert "--max-devices" in capsys.readouterr().err


# Tokens and numbers


def make_tokens(tmp_path, count):
    out = tmp_path / "batch"
    registry = tmp_path / "auth.json"
    if not registry.exists():
        registry.write_text('{"devices": {}}')
        registry.chmod(0o600)
    make_batch(count_ids(count, "fleet-"), out, credentials=tmp_path / "auth.json", labels=False)
    return out


def test_tokens_come_from_a_batch_folder(tmp_path, private_home):
    out = make_tokens(tmp_path, 3)
    pairs = fleet.load_tokens(out, 2)
    assert [device for device, _ in pairs] == ["fleet-01", "fleet-02"]
    assert all(len(token) >= 16 for _, token in pairs)
    with pytest.raises(OperatorError, match="only 3 token files"):
        fleet.load_tokens(out, 5)
    second = tmp_path / "second"
    make_batch(["more-1"], second, credentials=tmp_path / "auth.json", labels=False)
    assert [device for device, _ in fleet.load_tokens([out, second], 4)][-1] == "more-1"


def test_tokens_must_be_private(tmp_path, private_home):
    out = make_tokens(tmp_path, 1)
    os.chmod(out / "tokens" / "fleet-01.token", 0o644)
    with pytest.raises(OperatorError, match="0600"):
        fleet.load_tokens(out, 1)


def test_percentiles_use_the_nearest_rank():
    values = list(range(1, 101))
    assert fleet.percentile(values, 50) == 50
    assert fleet.percentile(values, 95) == 95
    assert fleet.percentile(values, 99) == 99
    assert fleet.percentile([7], 95) == 7
    assert fleet.percentile([], 95) is None


def test_the_reservoir_keeps_a_bounded_fair_sample():
    sample = fleet.Reservoir(100, seed=1)
    for value in range(10_000):
        sample.add(value)
    assert len(sample.values) == 100
    assert sample.seen == 10_000
    assert 2_000 < sum(sample.values) / 100 < 8_000


def test_offered_frames_follow_the_poll_rate():
    # The firmware sends a state frame in place of a poll (any request resets its timer).
    assert fleet.offered_rate(1000, 100) == pytest.approx(10_000)
    assert fleet.offered_rate(50, 500) == pytest.approx(100)


# A small fleet against a real relay listener on loopback


async def relay(tmp_path, pki, devices, **kwargs):  # noqa: F811 - fixture argument
    path = tmp_path / "auth.json"
    gateway = Gateway(**kwargs)
    server = DeviceServer(
        gateway,
        Credentials(path),
        port=0,
        tls=lan.device_context(pki),
        public=True,
        max_sessions=kwargs.get("max_devices", 64),
        connects_per_minute=20,
        pending_per_address=4,
        frames_per_second=30,
    )
    await server.start()
    return gateway, server


def config(tmp_path, pki, server, out, count, **extra):  # noqa: F811
    return fleet.FleetConfig(
        batch=out,
        count=count,
        host=NAME,
        port=server.port,
        connect="127.0.0.1",
        ca=pki / "ca.pem",
        poll_ms=100,
        ramp_per_minute=600,
        **extra,
    )


async def test_a_small_fleet_comes_online_and_acknowledges_commands(
    tmp_path,
    pki,  # noqa: F811
    private_home,
):
    out = make_tokens(tmp_path, 3)
    gateway, server = await relay(tmp_path, pki, 3)
    try:
        cfg = config(tmp_path, pki, server, out, 3)
        stats = fleet.Stats()
        stop = asyncio.Event()
        tasks = [
            asyncio.create_task(fleet.gadget(device, token, cfg, stats, stop))
            for device, token in fleet.load_tokens(out, 3)
        ]
        for _ in range(100):
            if stats.online == 3:
                break
            await asyncio.sleep(0.05)
        assert stats.online == 3
        command = gateway.command("fleet-02", "display.text", {"text": "load"}, None)
        for _ in range(100):
            if gateway.command_status(command["command_id"])["status"] == "executed":
                break
            await asyncio.sleep(0.05)
        assert gateway.command_status(command["command_id"])["status"] == "executed"
        await asyncio.sleep(0.3)
        stop.set()
        await asyncio.gather(*tasks)
        assert stats.answered >= 6
        assert stats.acks == 1
        assert stats.errors == {}
        assert fleet.percentile(stats.rtt.values, 95) < 1.0
    finally:
        await server.close()


async def test_a_full_relay_answers_busy_and_the_fleet_counts_it(
    tmp_path,
    pki,  # noqa: F811
    private_home,
):
    out = make_tokens(tmp_path, 3)
    _, server = await relay(tmp_path, pki, 3, max_devices=2)
    try:
        cfg = config(tmp_path, pki, server, out, 3, retry_seconds=30)
        stats = fleet.Stats()
        stop = asyncio.Event()
        tasks = [
            asyncio.create_task(fleet.gadget(device, token, cfg, stats, stop))
            for device, token in fleet.load_tokens(out, 3)
        ]
        for _ in range(100):
            if stats.online == 2 and stats.errors.get("busy"):
                break
            await asyncio.sleep(0.05)
        stop.set()
        await asyncio.gather(*tasks)
        assert stats.errors.get("busy", 0) >= 1
    finally:
        await server.close()


async def test_a_wrong_ca_is_refused_and_counted(tmp_path, pki, private_home):  # noqa: F811
    out = make_tokens(tmp_path, 1)
    _, server = await relay(tmp_path, pki, 1)
    other = tmp_path / "other"
    lan.create_ca(other)
    try:
        cfg = config(tmp_path, pki, server, out, 1, retry_seconds=30)
        cfg.ca = other / "ca.pem"
        stats = fleet.Stats()
        stop = asyncio.Event()
        task = asyncio.create_task(fleet.gadget(*fleet.load_tokens(out, 1)[0], cfg, stats, stop))
        for _ in range(100):
            if stats.errors.get("tls"):
                break
            await asyncio.sleep(0.05)
        stop.set()
        await task
        assert stats.errors.get("tls", 0) >= 1
        assert stats.online == 0
    finally:
        await server.close()


# The whole run: workers, files and no secrets


async def test_run_writes_numbers_and_never_a_token(tmp_path, pki, private_home):  # noqa: F811
    out = make_tokens(tmp_path, 4)
    _, server = await relay(tmp_path, pki, 4)
    results = tmp_path / "results"
    try:
        cfg = config(tmp_path, pki, server, out, 4, duration=1.5, workers=1, every=0.5)
        summary = await fleet.run(cfg, results)
    finally:
        await server.close()
    assert summary["gadgets"] == 4
    assert summary["online_at_end"] == 4
    assert summary["frames_answered_per_second"] > 0
    assert summary["poll_rtt_ms"]["p95"] is not None
    assert summary["commands"]["sent"] == 0  # No driver without --mcp-token.
    with (results / "fleet.csv").open() as handle:
        rows = list(csv.DictReader(handle))
    assert rows and {"elapsed_s", "online", "frames_per_s"} <= set(rows[0])
    written = "".join(p.read_text() for p in results.iterdir())
    for _, token in fleet.load_tokens(out, 4):
        assert token not in written
    assert "127.0.0.1" not in written
    assert json.loads((results / "summary.json").read_text()) == summary


async def test_the_driver_times_commands_through_mcp(tmp_path, pki, private_home):  # noqa: F811
    out = make_tokens(tmp_path, 2)
    token = tmp_path / "mcp-token"
    token.write_text("t" * 32)
    token.chmod(0o600)
    port = free_port()
    async with serve_gateway(
        str(tmp_path / "auth.json"),
        str(token),
        device_port=0,
        port=0,
        device_listen=f"127.0.0.1:{port}",
        device_directory=pki,
        max_devices=2,
        rate_limiter=RateLimiter(600, 600),
    ) as running:
        results = tmp_path / "results"
        cfg = fleet.FleetConfig(
            batch=out,
            count=2,
            host=NAME,
            port=port,
            connect="127.0.0.1",
            ca=pki / "ca.pem",
            poll_ms=100,
            ramp_per_minute=600,
            duration=2.0,
            workers=1,
            every=0.5,
            mcp_url=running.url,
            mcp_token="t" * 32,
            commands_per_second=2,
        )
        summary = await fleet.run(cfg, results)
    assert summary["commands"]["sent"] >= 2
    assert summary["commands"]["outcomes"].get("executed", 0) >= 1
    assert summary["commands"]["latency_ms"]["p95"] is not None
    assert "t" * 32 not in "".join(p.read_text() for p in results.iterdir())


async def test_serve_max_devices_sets_the_relay_limit(tmp_path, pki, private_home):  # noqa: F811
    make_tokens(tmp_path, 3)
    token = tmp_path / "mcp-token"
    token.write_text("t" * 32)
    token.chmod(0o600)
    port = free_port()
    async with serve_gateway(
        str(tmp_path / "auth.json"),
        str(token),
        device_port=0,
        port=0,
        device_listen=f"127.0.0.1:{port}",
        device_directory=pki,
        max_devices=2,
    ) as running:
        assert running.relay_server.max_sessions == 2
        assert running.gateway.max_devices == 2
    with pytest.raises(OperatorError, match="max-devices"):
        async with serve_gateway(
            str(tmp_path / "auth.json"), str(token), device_port=0, port=0, max_devices=5000
        ):
            pass


# The sampler


def test_sample_reads_cpu_and_memory_of_a_process():
    first = fleet.read_process(os.getpid())
    sum(range(200_000))
    second = fleet.read_process(os.getpid())
    assert second.cpu_seconds >= first.cpu_seconds
    assert second.rss_bytes > 1_000_000


def test_sample_writes_rows_and_a_summary(tmp_path):
    path = tmp_path / "sample.csv"
    summary = fleet.sample(os.getpid(), path, every=0.05, duration=0.3)
    rows = list(csv.DictReader(path.open()))
    assert len(rows) >= 3
    assert {"elapsed_s", "cpu_pct", "rss_mb"} <= set(rows[0])
    assert summary["rss_mb_peak"] > 1
    assert summary["cpu_pct_mean"] >= 0


def test_sample_refuses_a_missing_process():
    with pytest.raises(OperatorError, match="no process"):
        fleet.read_process(2**22 + 12345)


# The command line


def test_service_install_passes_max_devices_through():
    from grok_gadgets_gateway.background import serve_arguments

    assert "--max-devices" not in serve_arguments(device_listen="0.0.0.0:8444")
    args = serve_arguments(device_listen="0.0.0.0:8444", max_devices=200)
    assert args[args.index("--max-devices") + 1] == "200"


def test_cli_fleet_sample_prints_a_summary(tmp_path, capsys):
    path = tmp_path / "sample.csv"
    code = main(
        ["fleet", "sample", "--pid", str(os.getpid()), "--out", str(path), "--every", "0.05"]
        + ["--duration", "0.2"]
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out)["samples"] >= 2
    assert path.with_suffix(".json").is_file()


def test_cli_fleet_run_needs_both_mcp_options(tmp_path, capsys, private_home):
    out = make_tokens(tmp_path, 1)
    with pytest.raises(SystemExit):
        main(
            ["fleet", "run", "--batch", str(out), "--count", "1"]
            + ["--gateway", NAME, "--ca", str(tmp_path / "ca.pem"), "--out", str(tmp_path / "r")]
            + ["--mcp-url", "http://127.0.0.1:8766/mcp"]
        )
    assert "go together" in capsys.readouterr().err


def test_cli_fleet_run_refuses_too_few_tokens(tmp_path, capsys, private_home):
    out = make_tokens(tmp_path, 1)
    with pytest.raises(SystemExit):
        main(
            ["fleet", "run", "--batch", str(out), "--count", "5"]
            + ["--gateway", NAME, "--ca", str(tmp_path / "ca.pem"), "--out", str(tmp_path / "r")]
        )
    assert "only 1 token files" in capsys.readouterr().err


# A bare poll skips the schema (GW-SCALE-001): it is most of a relay's traffic.


def test_a_bare_poll_is_valid_without_the_schema(monkeypatch):
    from grok_gadgets_gateway import protocol

    assert protocol.is_valid(protocol.VALIDATOR, {"type": "poll"})  # The fast path agrees.
    monkeypatch.setattr(protocol, "is_valid", lambda *_: pytest.fail("schema used"))
    protocol.validate_request({"type": "poll"})


@pytest.mark.parametrize(
    "frame",
    [{"type": "poll", "extra": 1}, {"type": "poll", "wait_ms": 100}, {"type": "Poll"}, ["poll"]],
)
def test_anything_else_still_meets_the_schema(frame):
    from grok_gadgets_gateway.protocol import validate_request

    with pytest.raises(GatewayError) as exc:
        validate_request(frame)
    assert exc.value.code == "invalid_request"


def test_a_worker_stops_when_its_parent_is_gone(tmp_path, monkeypatch, private_home):
    import multiprocessing

    out = make_tokens(tmp_path, 1)
    queue, measure, stop = multiprocessing.Queue(), multiprocessing.Event(), multiprocessing.Event()
    cfg = fleet.FleetConfig(
        batch=out,
        count=1,
        host=NAME,
        port=9,
        connect="127.0.0.1",
        ca=tmp_path / "none.pem",
        retry_seconds=0.05,
        every=0.05,
    )
    # The parent PID given is not this process's parent: the worker ends at once.
    fleet._worker(cfg, fleet.load_tokens(out, 1), 600, queue, measure, stop, parent=-1)
    kind, result = queue.get(timeout=5)
    assert kind == "done" and result["online_at_end"] == 0


async def test_a_lost_mcp_endpoint_is_recorded_not_fatal(tmp_path):
    cfg = fleet.FleetConfig(
        batch=tmp_path,
        count=1,
        host=NAME,
        port=9,
        ca=tmp_path / "ca.pem",
        mcp_url=f"http://127.0.0.1:{free_port()}/mcp",
        mcp_token="t" * 32,
    )
    result = await fleet.drive(cfg, ["dev-1"], 0.3)
    assert result["error"] and result["latency_ms"]["p95"] is None
