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


def test_simulation_and_idempotency():
    g = Gateway()
    sim = Simulator(g)
    args = {"r": 0, "g": 255, "b": 0, "on": True}
    assert g.command(sim.device_id, "rgb.set", args, "cmd-1")["status"] == "accepted"
    sim.execute()
    result = g.command(sim.device_id, "rgb.set", args, "cmd-1")
    assert result["status"] == "executed"
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
    fails(
        "stale_session",
        lambda: g.handle(
            "dev-1", sid, {"type": "ack", "command_id": "a", "status": "executed", "state": {}}
        ),
    )
    assert g.state("dev-1")["freshness"] == "stale"
    g.command("dev-1", "rgb.set", args, "b")
    g.disconnect("dev-1", sid)
    assert g.command_status("b")["status"] == "unconfirmed"
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
