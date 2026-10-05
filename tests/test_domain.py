import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from grok_gadgets_gateway.domain import Gateway
from grok_gadgets_gateway.protocol import GatewayError, SCHEMA, validate_request
from grok_gadgets_gateway.simulator import Simulator

FIXTURES = Path(__file__).parents[1] / "protocol/0.1.0/fixtures/device-transcript.json"


def hello(did="dev-1"):
    value = json.loads(FIXTURES.read_text())[0]
    value["device"]["device_id"] = did
    return value


def fails(code, action):
    with pytest.raises(GatewayError) as exc:
        action()
    assert exc.value.code == code


def test_canonical_fixtures_and_schema():
    Draft202012Validator.check_schema(SCHEMA)
    for message in json.loads(FIXTURES.read_text()):
        validate_request(message)
    bad = hello()
    bad["protocol_version"] = "99"
    fails("protocol_mismatch", lambda: validate_request(bad))
    bad = hello()
    bad["device"]["device_id"] = "../bad"
    fails("invalid_request", lambda: validate_request(bad))


@pytest.mark.parametrize("field", ["device_id", "boot_id", "capability"])
def test_ids_reject_trailing_newline(field):
    bad = hello()
    if field == "capability":
        bad["device"]["capabilities"].append("rgb.set\n")
    else:
        bad["device"][field] += "\n"
    fails("invalid_request", lambda: validate_request(bad))
    fails("invalid_request", lambda: Gateway().register(bad))


def test_integers_are_type_strict():
    g = Gateway()
    sid = g.register(hello())
    for value in (255.0, 0.0, True):
        args = {"r": value, "g": 0, "b": 0, "on": True}
        fails("invalid_arguments", lambda: g.command("dev-1", "rgb.set", args, "float"))
    fails(
        "invalid_request",
        lambda: g.handle(
            "dev-1", sid, {"type": "state", "state": {"rgb": {"r": 1.0, "g": 0, "b": 0, "on": 1}}}
        ),
    )
    floaty = hello("dev-2")
    floaty["device"]["state"] = {"rgb": {"r": 255.0, "g": 0, "b": 0, "on": True}}
    fails("invalid_request", lambda: g.register(floaty))
    custom = hello("dev-3")
    custom["device"]["capabilities"].append("level.set")
    custom["device"]["capability_schemas"] = {
        "level.set": {"type": "object", "properties": {"level": {"type": "integer"}}}
    }
    g.register(custom)
    fails("invalid_arguments", lambda: g.command("dev-3", "level.set", {"level": 1.0}, "lvl"))
    assert g.command("dev-3", "level.set", {"level": 1}, "lvl")["status"] == "accepted"


async def test_simulation_and_idempotency():
    g = Gateway()
    sim = Simulator(g)
    args = {"r": 0, "g": 255, "b": 0, "on": True}
    first = g.command(sim.device_id, "rgb.set", args, "cmd-1")
    assert first["status"] == "accepted" and first["duplicate"] is False
    await sim.execute()
    result = g.command(sim.device_id, "rgb.set", args, "cmd-1")
    assert result["status"] == "executed" and result["duplicate"] is True
    assert result["requested_at"] == first["requested_at"]
    assert result["simulated"] and not result["physical_verified"]
    assert g.state(sim.device_id)["state"]["rgb"] == args
    fails(
        "duplicate_conflict", lambda: g.command(sim.device_id, "rgb.set", {**args, "r": 1}, "cmd-1")
    )
    for args in (
        {"r": -1, "g": 0, "b": 0, "on": True},
        {"r": True, "g": 0, "b": 0, "on": True},
        {"r": 256},
    ):
        fails("invalid_arguments", lambda: g.command(sim.device_id, "rgb.set", args, "bad"))
    sim.control("button", True)
    sim.control("button", True)
    sim.control("button", False)
    events = g.read_events()["events"]
    assert [e["data"]["pressed"] for e in events] == [True, False]
    assert all(e["simulated"] for e in events)
    sim.control("disconnect")
    fails("unavailable", lambda: g.command(sim.device_id, "rgb.set", result["arguments"], "new"))
    sim.control("reconnect")
    assert g.state(sim.device_id)["available"]


def test_command_disconnect_timeout_stale_session_and_queue():
    clock = [0]
    g = Gateway(clock=lambda: clock[0], command_limit=1)
    sid = g.register(hello())
    args = {"r": 0, "g": 0, "b": 0, "on": False}
    g.command("dev-1", "rgb.set", args, "a")
    fails("busy", lambda: g.command("dev-1", "rgb.set", args, "b"))
    assert len(g.handle("dev-1", sid, {"type": "poll"})["commands"]) == 1
    clock[0] = 11
    assert g.command_status("a")["status"] == "timed_out"
    late = {"type": "ack", "command_id": "a", "status": "executed", "state": {"x": 1}}
    fails("late_ack", lambda: g.handle("dev-1", sid, late))
    assert g.command_status("a")["status"] == "timed_out"
    assert g.command_status("a")["late_ack"]["status"] == "executed"
    assert g.handle("dev-1", sid, {"type": "ping"}) == {"ok": True}
    assert g.state("dev-1")["freshness"] == "fresh"
    clock[0] = 22
    assert g.state("dev-1")["freshness"] == "stale"
    g.command("dev-1", "rgb.set", args, "b")
    g.disconnect("dev-1", sid)
    assert g.command_status("b")["status"] == "not_delivered"
    assert g.state("dev-1")["freshness"] == "offline"
    new = g.register(hello())
    fails("stale_session", lambda: g.handle("dev-1", sid, {"type": "ping"}))
    assert g.handle("dev-1", new, {"type": "poll"})["commands"] == []


def test_event_order_duplicates_retention_restart_filter_and_future():
    g = Gateway(event_limit=2)
    sid = g.register(hello())
    initial = g.read_events()["next_cursor"]
    event = {"type": "event", "name": "button", "event_id": "e1", "data": {"pressed": True}}
    assert g.handle("dev-1", sid, event) == {"ok": True}
    assert g.handle("dev-1", sid, event)["duplicate"]
    event["data"]["pressed"] = False
    fails("duplicate_conflict", lambda: g.handle("dev-1", sid, event))
    for n in (2, 3):
        event["event_id"] = f"e{n}"
        g.handle("dev-1", sid, event)
    fails("history_lost", lambda: g.read_events(initial))
    read = g.read_events()
    assert read["history_lost"] and len(read["events"]) == 2
    assert g.read_events(read["next_cursor"])["events"] == []
    assert g.read_events(device_id="dev-1", limit=1)["events"][0]["sequence"] == 2
    fails("invalid_cursor", lambda: g.read_events(f"{g.epoch}:99"))
    fails("cursor_reset", lambda: Gateway().read_events(read["next_cursor"]))
    fails("invalid_event", lambda: g.handle("dev-1", sid, {**event, "data": {"pressed": 1}}))


def test_ack_device_isolation_and_diagnostic_redaction():
    g = Gateway()
    a = g.register(hello("a"))
    b = g.register(hello("b"))
    g.command("a", "rgb.set", {"r": 1, "g": 2, "b": 3, "on": True}, "cmd")
    assert g.handle("b", b, {"type": "poll"})["commands"] == []
    fails(
        "unknown_command",
        lambda: g.handle(
            "b", b, {"type": "ack", "command_id": "cmd", "status": "executed", "state": {}}
        ),
    )
    g.handle("a", a, {"type": "poll"})
    ack = {"type": "ack", "command_id": "cmd", "status": "executed", "state": {"private": "secret"}}
    g.handle("a", a, ack)
    assert g.handle("a", a, ack)["duplicate"]
    assert "secret" not in json.dumps(g.diagnostics())
    assert "private" not in json.dumps(g.diagnostics())


def test_custom_capability_discovery_and_validation():
    g = Gateway()
    registration = hello()
    registration["device"]["capabilities"].append("relay.set")
    registration["device"]["capability_schemas"] = {
        "relay.set": {
            "type": "object",
            "properties": {"on": {"type": "boolean"}},
            "required": ["on"],
            "additionalProperties": False,
        }
    }
    g.register(registration)
    assert g.state("dev-1")["capability_contracts"]["relay.set"]["required"] == ["on"]
    assert g.command("dev-1", "relay.set", {"on": True}, "relay")["status"] == "accepted"
    fails("invalid_arguments", lambda: g.command("dev-1", "relay.set", {"on": 1}, "bad"))
    registration["device"]["capability_schemas"]["relay.set"] = {"$ref": "https://invalid.test"}
    fails("invalid_request", lambda: g.register(registration))


async def test_only_command_capabilities_are_callable_and_discovered():
    g = Gateway()
    sim = Simulator(g)
    initial = g.state(sim.device_id)["state"]
    discovery = g.state(sim.device_id)
    assert discovery["command_capabilities"] == ["rgb.set"]
    assert discovery["event_capabilities"] == ["button"]
    assert set(discovery["capability_contracts"]) == {"rgb.set"}
    args = {"r": 1, "g": 2, "b": 3, "on": True}
    for name in ("button", "state", "unknown"):
        fails("unsupported_capability", lambda: g.command(sim.device_id, name, args, name))
    for malformed in ({}, {**args, "r": True}, {**args, "extra": 1}):
        fails(
            "invalid_arguments",
            lambda: g.command(sim.device_id, "rgb.set", malformed, "malformed"),
        )
    await sim.execute()
    assert not g.commands
    assert g.state(sim.device_id)["state"] == initial
    sim.control("button", True)
    assert g.read_events()["events"][0]["data"] == {"pressed": True}
    assert g.state(sim.device_id)["state"]["button"] == {"pressed": True}


def test_custom_string_only_commands_remain_callable():
    g = Gateway()
    registration = hello()
    registration["device"]["capabilities"].append("counter.bump")
    sid = g.register(registration)
    assert g.state("dev-1")["capability_contracts"]["counter.bump"] == {"type": "object"}
    assert "counter.bump" in g.state("dev-1")["command_capabilities"]
    g.command("dev-1", "counter.bump", {"count": 2}, "custom")
    command = g.handle("dev-1", sid, {"type": "poll"})["commands"][0]
    assert command["capability"] == "counter.bump"
    g.handle(
        "dev-1",
        sid,
        {"type": "ack", "command_id": "custom", "status": "executed", "state": {"count": 2}},
    )
    assert g.command_status("custom")["status"] == "executed"


@pytest.mark.parametrize(
    "capability,arguments",
    [
        ("counter.bump", {"r": 1, "g": 2, "b": 3, "on": True}),
        ("rgb.set", {"r": -1, "g": 2, "b": 3, "on": True}),
    ],
)
async def test_simulator_rejects_unsupported_or_malformed_delivered_commands(capability, arguments):
    g = Gateway()
    sim = Simulator(g)
    initial = g.state(sim.device_id)["state"]
    g.command(sim.device_id, "rgb.set", initial["rgb"], "delivered")
    # Simulate a corrupted adapter delivery after domain validation to exercise dispatch defense.
    g.commands["delivered"].update(capability=capability, arguments=arguments)
    await sim.execute()
    assert g.command_status("delivered")["status"] == "failed"
    assert g.state(sim.device_id)["state"] == initial


def event(eid="original", pressed=True):
    return {"type": "event", "name": "button", "event_id": eid, "data": {"pressed": pressed}}


def test_event_windows_isolate_devices_and_retention_boundary():
    g = Gateway(event_limit=512)
    a, b = g.register(hello("a")), g.register(hello("b"))
    assert g.handle("a", a, event()) == {"ok": True}
    assert g.handle("b", b, event()) == {"ok": True}
    for index in range(1, 257):
        g.handle("b", b, event(f"b-{index}"))
    sequence = g.sequence
    history = f"{g.epoch}:{g.sequence}"
    assert g.handle("a", a, event()) == {"ok": True, "duplicate": True}
    assert g.sequence == sequence
    assert g.read_events(history)["events"] == []
    fails("duplicate_conflict", lambda: g.handle("a", a, event(pressed=False)))
    fails("duplicate_conflict", lambda: g.handle("a", a, {**event(), "observed_at": "changed"}))
    # At 256 entries b-1 is retained; identical retries do not extend retention.
    assert g.handle("b", b, event("b-1"))["duplicate"]
    assert len(g.seen_events["b"]) == 256
    g.handle("b", b, event("b-257"))
    assert g.handle("b", b, event("b-1")) == {"ok": True}
    assert len(g.seen_events["b"]) == 256
    assert g.handle("a", a, event())["duplicate"]


def test_event_windows_reconnect_boot_change_restart_and_stale_sessions():
    g = Gateway()
    registration = hello()
    old = g.register(registration)
    g.handle("dev-1", old, event())
    cursor = g.read_events()["next_cursor"]
    g.disconnect("dev-1", old)
    fails("stale_session", lambda: g.handle("dev-1", old, event("offline")))
    same_boot = g.register(registration)
    assert g.handle("dev-1", same_boot, event())["duplicate"]
    assert g.sequence == 1
    fails("stale_session", lambda: g.handle("dev-1", old, event("stale")))
    registration["device"]["boot_id"] = "new-boot"
    new_boot = g.register(registration)
    assert g.handle("dev-1", new_boot, event()) == {"ok": True}
    assert g.sequence == 2
    fails("stale_session", lambda: g.handle("dev-1", same_boot, event("retired")))
    assert len(g.seen_events) == 1 and len(g.seen_events["dev-1"]) == 1
    restarted = Gateway()
    new_session = restarted.register(registration)
    assert restarted.handle("dev-1", new_session, event()) == {"ok": True}
    fails("cursor_reset", lambda: restarted.read_events(cursor))
    fails("stale_session", lambda: restarted.handle("dev-1", new_boot, event("old-process")))


def test_event_lifecycle_bookkeeping_is_bounded():
    g = Gateway(event_limit=3)
    for index in range(64):
        registration = hello(f"dev-{index}")
        sid = g.register(registration)
        for count in range(257):
            g.handle(f"dev-{index}", sid, event(f"event-{count}"))
    assert len(g.devices) == len(g.seen_events) == 64
    assert sum(map(len, g.seen_events.values())) == 64 * 256
    assert len(g.events) == 3
    fails("busy", lambda: g.register(hello("overflow")))
    assert len(g.devices) == len(g.seen_events) == 64
    for boot in range(300):
        registration = hello("dev-0")
        registration["device"]["boot_id"] = f"boot-{boot}"
        sid = g.register(registration)
        g.handle("dev-0", sid, event())
    assert len(g.devices) == len(g.seen_events) == 64
    assert len(g.seen_events["dev-0"]) == 1
    assert sum(map(len, g.seen_events.values())) == 63 * 256 + 1
    assert len(g.events) == 3


def test_command_ids_generated_window_conflict_and_type_strict_retries():
    clock = [0]
    g = Gateway(clock=lambda: clock[0])
    registration = hello()
    registration["device"]["capabilities"].append("level.set")
    g.register(registration)
    generated = g.command("dev-1", "level.set", {"level": 1})
    other = g.command("dev-1", "level.set", {"level": 1})
    assert generated["command_id"] != other["command_id"]
    assert generated["command_id"].startswith("gw-") and not generated["duplicate"]
    g.command("dev-1", "level.set", {"level": 1}, "retry")
    for changed in ({"level": 1.0}, {"level": True}, {"level": 2}):
        fails("duplicate_conflict", lambda: g.command("dev-1", "level.set", changed, "retry"))
    clock[0] = 599
    assert g.command("dev-1", "level.set", {"level": 1}, "retry")["duplicate"]
    clock[0] = 601
    fails("stale_command_id", lambda: g.command("dev-1", "level.set", {"level": 1}, "retry"))
    fails("invalid_command_id", lambda: g.command("dev-1", "level.set", {}, "bad\n"))
    fails("invalid_arguments", lambda: g.command("dev-1", "level.set", {"x": float("nan")}))


def test_arguments_validated_before_duplicate_lookup():
    g = Gateway()
    registration = hello()
    registration["device"]["capabilities"].append("relay.set")
    registration["device"]["capability_schemas"] = {
        "relay.set": {"type": "object", "properties": {"on": {"type": "boolean"}}}
    }
    g.register(registration)
    g.command("dev-1", "relay.set", {"on": True}, "relay")
    fails("invalid_arguments", lambda: g.command("dev-1", "relay.set", {"on": 1}, "relay"))


def test_evicted_command_id_is_not_reused_silently():
    g = Gateway(command_limit=1)
    sid = g.register(hello())
    args = {"r": 1, "g": 2, "b": 3, "on": True}
    g.command("dev-1", "rgb.set", args, "first")
    g.handle("dev-1", sid, {"type": "poll"})
    ack = {"type": "ack", "command_id": "first", "status": "executed", "state": {}}
    g.handle("dev-1", sid, ack)
    g.command("dev-1", "rgb.set", args, "second")
    fails("unknown_command", lambda: g.command_status("first"))
    fails("stale_command_id", lambda: g.command("dev-1", "rgb.set", args, "first"))


def test_ack_semantics_and_not_delivered_queue_timeout():
    clock = [0]
    g = Gateway(clock=lambda: clock[0])
    sid = g.register(hello())
    args = {"r": 1, "g": 2, "b": 3, "on": True}
    g.command("dev-1", "rgb.set", args, "queued")
    clock[0] = 11
    assert g.command_status("queued")["status"] == "not_delivered"
    assert g.handle("dev-1", sid, {"type": "poll"})["commands"] == []
    g.command("dev-1", "rgb.set", args, "sent")
    clock[0] = 20
    g.handle("dev-1", sid, {"type": "poll"})
    clock[0] = 29  # The acknowledgement deadline starts at dispatch, not at acceptance.
    bad = {
        "type": "ack",
        "command_id": "sent",
        "status": "executed",
        "state": {},
        "error": {"code": "x", "message": "y"},
    }
    fails("invalid_request", lambda: g.handle("dev-1", sid, bad))
    del bad["error"]
    assert g.handle("dev-1", sid, bad) == {"ok": True}
    assert g.command_status("sent")["status"] == "executed"
    assert "error" not in g.command_status("sent")
