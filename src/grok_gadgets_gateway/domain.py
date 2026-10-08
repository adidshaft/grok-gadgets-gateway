"""Transport-independent single-user gateway state; no model backend or I/O."""

import copy
import json
import re
import time
import uuid
from collections import OrderedDict, deque
from datetime import UTC, datetime

from jsonschema import SchemaError

from .protocol import (
    MAX_DESCRIPTION,
    RGB_VALIDATOR,
    SCHEMA,
    GatewayError,
    StrictValidator,
    is_valid,
    validate_request,
    validate_state,
)

COMMAND_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
DEDUP_WINDOW_SECONDS = 600
ACK_TIMEOUT_SECONDS = 10
# Internal bookkeeping never returned to the assistant.
_PRIVATE_COMMAND_FIELDS = ("deadline", "ack_error", "requested_clock", "fingerprint")
_OPEN = ("accepted", "dispatched")
RGB_DESCRIPTION = "Set the light colour: r, g and b are 0-255; on switches it on or off."


def now_iso():
    return datetime.now(UTC).isoformat()


def canonical(value):
    """Type-strict JSON identity: 1, 1.0 and true are different arguments."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def check_capability_schema(schema):
    """Reject device-controlled regex before jsonschema compiles or evaluates it.

    Visit schema locations only: a property named 'pattern' or a literal object in
    enum/const is data, not a regex. Unknown annotation values are not evaluated.
    """
    pending = [schema]
    maps = {"properties", "$defs", "definitions", "dependentSchemas"}
    arrays = {"allOf", "anyOf", "oneOf", "prefixItems"}
    singles = {
        "additionalProperties",
        "unevaluatedProperties",
        "propertyNames",
        "items",
        "contains",
        "not",
        "if",
        "then",
        "else",
        "unevaluatedItems",
        "contentSchema",
    }
    while pending:
        current = pending.pop()
        if not isinstance(current, dict):
            continue
        if current.get("$schema", "https://json-schema.org/draft/2020-12/schema") != (
            "https://json-schema.org/draft/2020-12/schema"
        ):
            raise GatewayError("invalid_request", "Capability schemas require Draft 2020-12")
        if "$ref" in current or "$dynamicRef" in current:
            raise GatewayError("invalid_request", "Capability schemas must be inline")
        if "pattern" in current or "patternProperties" in current:
            raise GatewayError(
                "invalid_request",
                "Capability schemas cannot use pattern or patternProperties; "
                "use enum, lengths and explicit properties",
            )
        for key, value in current.items():
            if key in maps and isinstance(value, dict):
                pending.extend(value.values())
            elif key in arrays and isinstance(value, list):
                pending.extend(value)
            elif key in singles:
                pending.append(value)
    try:
        StrictValidator.check_schema(schema)
    except (SchemaError, RecursionError):
        raise GatewayError("invalid_request", "Invalid capability schema") from None


class Gateway:
    def __init__(
        self,
        *,
        event_limit=128,
        command_limit=128,
        clock=time.monotonic,
        dedup_window=DEDUP_WINDOW_SECONDS,
    ):
        self.devices = {}
        self.commands = OrderedDict()
        # Recently used command IDs, retained longer than full command records.
        self.used_command_ids = OrderedDict()
        self.dedup_window = dedup_window
        self.events = deque(maxlen=event_limit)
        # One bounded window for each registered device's current boot (at most 64).
        self.seen_events = {}
        self.event_limit = event_limit
        self.command_limit = command_limit
        self.clock = clock
        self.epoch = uuid.uuid4().hex
        self.sequence = 0

    def register(self, message, *, display_name=None):
        validate_request(message)
        descriptor = copy.deepcopy(message["device"])
        did = descriptor["device_id"]
        for name, schema in descriptor.get("capability_schemas", {}).items():
            if name not in descriptor["capabilities"] or name == "rgb.set":
                raise GatewayError("invalid_request", "Schema must match a custom capability")

            check_capability_schema(schema)
            description = schema.get("description")
            if description is not None and (
                not isinstance(description, str)
                or not description.strip()
                or len(description) > MAX_DESCRIPTION
            ):
                raise GatewayError(
                    "invalid_request",
                    f"Capability description must be 1-{MAX_DESCRIPTION} characters",
                )
        if did not in self.devices and len(self.devices) >= 64:
            raise GatewayError("busy", "Device registry is full")
        previous = self.devices.get(did)
        if previous:
            self.disconnect(did, previous["session_id"])
        if previous is None or previous["boot_id"] != descriptor["boot_id"]:
            self.seen_events[did] = OrderedDict()
        session = uuid.uuid4().hex
        self.devices[did] = {
            **descriptor,
            "session_id": session,
            "connected": True,
            "last_seen": self.clock(),
            "state_time": self.clock(),
            "received_at": now_iso(),
        }
        if display_name is not None:
            # Trusted local simulator metadata; this does not extend the device wire protocol.
            self.devices[did]["display_name"] = display_name
        return session

    def _device(self, did):
        if did not in self.devices:
            raise GatewayError("unknown_device", "Device not registered")
        return self.devices[did]

    def session(self, did, sid):
        dev = self._device(did)
        if not dev["connected"] or dev["session_id"] != sid:
            raise GatewayError("stale_session", "Device session is no longer current")
        dev["last_seen"] = self.clock()
        return dev

    def disconnect(self, did, sid):
        dev = self._device(did)
        if dev["session_id"] != sid:
            return
        dev["connected"] = False
        for command in self.commands.values():
            if command["device_id"] != did:
                continue
            if command["status"] == "accepted":
                command["status"] = "not_delivered"
                command["error"] = {
                    "code": "disconnected",
                    "message": "Session ended before delivery; the device never received it",
                }
            elif command["status"] == "dispatched":
                command["status"] = "unconfirmed"
                command["error"] = {
                    "code": "disconnected",
                    "message": "Session ended before result",
                }

    def state(self, did):
        dev = self._device(did)
        age = max(0, self.clock() - dev["state_time"])
        result = {
            k: copy.deepcopy(v)
            for k, v in dev.items()
            if k not in ("last_seen", "state_time", "connected")
        }
        result["command_capabilities"] = self.command_capabilities(dev)
        result["event_capabilities"] = self.event_capabilities(dev)
        result["capability_descriptions"] = self.capability_descriptions(dev)
        result["capability_contracts"] = {
            name: SCHEMA["$defs"]["rgb"]
            if name == "rgb.set"
            else copy.deepcopy(dev.get("capability_schemas", {}).get(name, {"type": "object"}))
            for name in result["command_capabilities"]
        }
        result.update(
            available=dev["connected"],
            state_age_seconds=round(age, 3),
            freshness="offline" if not dev["connected"] else "stale" if age > 10 else "fresh",
        )
        return result

    @staticmethod
    def event_capabilities(dev):
        """Names that are inputs, not commands.

        Reserved names are button and history_lost. Any other capability is an event when its
        inline schema sets ``x-grok-gadgets-kind`` to ``event`` (the Linux SDK custom-event shape).
        """
        schemas = dev.get("capability_schemas") or {}
        events = []
        for name in dev["capabilities"]:
            schema = schemas.get(name)
            kind = schema.get("x-grok-gadgets-kind") if isinstance(schema, dict) else None
            if name in ("button", "history_lost") or kind == "event":
                events.append(name)
        return events

    @staticmethod
    def capability_descriptions(dev):
        """What each capability does, from its schema's JSON Schema `description`.

        Device-supplied text: shown to the assistant as information, never as instructions.
        """
        schemas = dev.get("capability_schemas") or {}
        result = {}
        for name in dev["capabilities"]:
            schema = schemas.get(name)
            if isinstance(schema, dict) and isinstance(schema.get("description"), str):
                result[name] = schema["description"]
            elif name == "rgb.set":
                result[name] = RGB_DESCRIPTION
        return result

    def command_capabilities(self, dev):
        events = self.event_capabilities(dev)
        return [name for name in dev["capabilities"] if name not in events and name != "state"]

    def list_devices(self):
        return [self.state(did) for did in self.devices]

    def command(self, did, capability, arguments, command_id=None):
        if command_id is None:
            command_id = "gw-" + uuid.uuid4().hex
        elif not isinstance(command_id, str) or not COMMAND_ID.fullmatch(command_id):
            raise GatewayError("invalid_command_id", "Use a 1–64 character command ID or omit it")
        if not isinstance(arguments, dict):
            raise GatewayError("invalid_arguments", "Arguments must be an object")
        try:
            fingerprint = canonical([did, capability, arguments])
        except (TypeError, ValueError, RecursionError):
            raise GatewayError("invalid_arguments", "Arguments must be finite JSON") from None
        if capability == "rgb.set" and not is_valid(RGB_VALIDATOR, arguments):
            raise GatewayError("invalid_arguments", "RGB requires integer channels 0..255 and on")
        dev = self._device(did)
        if capability not in self.command_capabilities(dev):
            raise GatewayError("unsupported_capability", "Device does not declare this command")
        schema = dev.get("capability_schemas", {}).get(capability)
        if schema and not is_valid(StrictValidator(schema), arguments):
            raise GatewayError("invalid_arguments", "Arguments violate declared capability schema")
        existing = self.commands.get(command_id)
        if existing:
            if self.clock() - existing["requested_clock"] > self.dedup_window:
                raise GatewayError(
                    "stale_command_id",
                    "Retry window expired; inspect state before any further action",
                )
            if existing["fingerprint"] != fingerprint:
                raise GatewayError(
                    "duplicate_conflict", "Command ID reused with changed parameters"
                )
            return {**self.command_status(command_id), "duplicate": True}
        if command_id in self.used_command_ids:
            raise GatewayError(
                "stale_command_id", "Receipt is no longer retained; inspect state before acting"
            )
        if not dev["connected"]:
            raise GatewayError("unavailable", "Device is disconnected")
        # Ensure even arbitrary capability requests fit a poll frame.
        wire = {"command_id": command_id, "capability": capability, "arguments": arguments}
        if len(json.dumps({"ok": True, "commands": [wire]}).encode()) + 1 > 2048:
            raise GatewayError("invalid_arguments", "Command exceeds transport frame limit")
        if len(self.used_command_ids) >= 8 * self.command_limit:
            oldest_clock = next(iter(self.used_command_ids.values()))
            if self.clock() - oldest_clock <= self.dedup_window:
                raise GatewayError("busy", "Retry protection is full; try a new action later")
        self.expire_commands()
        if len(self.commands) >= self.command_limit:
            terminal = next((k for k, v in self.commands.items() if v["status"] not in _OPEN), None)
            if terminal is None:
                raise GatewayError("busy", "Command queue is full")
            del self.commands[terminal]
        self.commands[command_id] = {
            **copy.deepcopy(wire),
            "device_id": did,
            "status": "accepted",
            "session_id": dev["session_id"],
            "simulated": dev["simulated"],
            "physical_verified": False,
            "requested_at": now_iso(),
            "requested_clock": self.clock(),
            "fingerprint": fingerprint,
            "deadline": self.clock() + ACK_TIMEOUT_SECONDS,
        }
        self.used_command_ids[command_id] = self.clock()
        if len(self.used_command_ids) > 8 * self.command_limit:
            self.used_command_ids.popitem(last=False)
        return {**self.command_status(command_id), "duplicate": False}

    def expire_commands(self):
        for value in self.commands.values():
            if value["status"] in _OPEN and self.clock() > value["deadline"]:
                if value["status"] == "accepted":
                    value["status"] = "not_delivered"
                    value["error"] = {
                        "code": "timeout",
                        "message": "Device did not poll in time; it never received the command",
                    }
                else:
                    value["status"] = "timed_out"
                    value["error"] = {"code": "timeout", "message": "Execution unconfirmed"}

    def command_status(self, command_id):
        self.expire_commands()
        if command_id not in self.commands:
            raise GatewayError("unknown_command", "Command result not retained")
        return {
            k: copy.deepcopy(v)
            for k, v in self.commands[command_id].items()
            if k not in _PRIVATE_COMMAND_FIELDS
        }

    def handle(self, did, sid, message):
        validate_request(message)
        dev = self.session(did, sid)
        kind = message["type"]
        if kind == "hello":
            raise GatewayError("invalid_request", "Hello must begin a new connection")
        if kind == "poll":
            self.expire_commands()
            for value in self.commands.values():
                if value["device_id"] == did and value["status"] == "accepted":
                    value["status"] = "dispatched"
                    value["deadline"] = self.clock() + ACK_TIMEOUT_SECONDS
                    return {
                        "ok": True,
                        "commands": [
                            {
                                k: copy.deepcopy(value[k])
                                for k in ("command_id", "capability", "arguments")
                            }
                        ],
                    }
            return {"ok": True, "commands": []}
        if kind == "state":
            self._report(dev, message["state"])
        elif kind == "ack":
            cid = message["command_id"]
            value = self.commands.get(cid)
            if not value or value["device_id"] != did or value["session_id"] != sid:
                raise GatewayError("unknown_command", "Command is not owned by this session")
            if message["status"] == "failed" and "error" not in message:
                raise GatewayError("invalid_request", "Failed acknowledgement requires error")
            if message["status"] == "executed" and "error" in message:
                raise GatewayError("invalid_request", "Executed acknowledgement cannot have error")
            self.expire_commands()
            if value["status"] in ("executed", "failed"):
                same = (value["status"], value["reported_state"], value.get("ack_error")) == (
                    message["status"],
                    message["state"],
                    message.get("error"),
                )
                if not same:
                    raise GatewayError("duplicate_conflict", "Acknowledgement changed")
                return {"ok": True, "duplicate": True}
            if value["status"] in ("timed_out", "unconfirmed"):
                # Non-fatal: keep the session, keep the honest status, record what arrived.
                self._report(dev, message["state"])
                value["late_ack"] = {"status": message["status"], "received_at": now_iso()}
                raise GatewayError("late_ack", "Acknowledgement arrived after the command closed")
            if value["status"] != "dispatched":
                raise GatewayError("unknown_command", "Command was not delivered to this session")
            self._report(dev, message["state"])
            value.update(
                status=message["status"],
                reported_state=copy.deepcopy(message["state"]),
                executed_at=now_iso(),
            )
            value["ack_error"] = copy.deepcopy(message.get("error"))
            if "error" in message:
                # Never store untrusted device error text in diagnostics or assistant results.
                value["error"] = {"code": "device_failed", "message": "Device reported failure"}
        elif kind == "event":
            return self._event(dev, message)
        return {"ok": True}

    def _report(self, dev, state):
        validate_state(state)
        dev.update(state=copy.deepcopy(state), state_time=self.clock(), received_at=now_iso())

    def _event(self, dev, message):
        name = message["name"]
        if name in self.command_capabilities(dev):
            raise GatewayError("unsupported_capability", "Event name is a command capability")
        if name not in self.event_capabilities(dev):
            raise GatewayError("unsupported_capability", "Undeclared event capability")
        if message["name"] == "button":
            data = message["data"]
            if set(data) != {"pressed"} or type(data["pressed"]) is not bool:
                raise GatewayError("invalid_event", "Button event requires pressed boolean")
        window = self.seen_events[dev["device_id"]]
        key = message["event_id"]
        existing = window.get(key)
        content = (message["name"], message["data"], message.get("observed_at"))
        if existing:
            if existing != content:
                raise GatewayError(
                    "duplicate_conflict", "Event identifier reused with changed data"
                )
            return {"ok": True, "duplicate": True}
        window[key] = copy.deepcopy(content)
        if len(window) > 256:
            window.popitem(last=False)
        self.sequence += 1
        self.events.append(
            {
                "device_id": dev["device_id"],
                "boot_id": dev["boot_id"],
                "session_id": dev["session_id"],
                "sequence": self.sequence,
                "event_id": message["event_id"],
                "name": message["name"],
                "data": copy.deepcopy(message["data"]),
                "received_at": now_iso(),
                "observed_at": message.get("observed_at"),
                "simulated": dev["simulated"],
            }
        )
        return {"ok": True}

    def read_events(self, cursor=None, device_id=None, limit=32):
        if type(limit) is not int or not 1 <= limit <= 128:
            raise GatewayError("invalid_cursor", "Event limit must be 1..128")
        if device_id is not None:
            self._device(device_id)
        oldest = self.events[0]["sequence"] if self.events else self.sequence + 1
        position = oldest - 1
        history_lost = cursor is None and oldest > 1
        if cursor is not None:
            try:
                epoch, number = cursor.split(":")
                position = int(number)
            except (ValueError, AttributeError):
                raise GatewayError("invalid_cursor", "Malformed cursor") from None
            if epoch != self.epoch:
                raise GatewayError("cursor_reset", "Gateway restarted; read from null cursor")
            if position < oldest - 1:
                raise GatewayError("history_lost", "Events dropped; read from null cursor")
            if position < 0 or position > self.sequence:
                raise GatewayError("invalid_cursor", "Cursor position outside retained stream")
        result = []
        for event in self.events:
            if event["sequence"] <= position:
                continue
            position = event["sequence"]
            if device_id is None or event["device_id"] == device_id:
                result.append(copy.deepcopy(event))
                if len(result) >= limit:
                    break
        return {
            "events": result,
            "next_cursor": f"{self.epoch}:{position}",
            "history_lost": history_lost,
            "retention": self.event_limit,
        }

    def diagnostics(self):
        # Allowlist only; never serialize descriptors, states, events, args or errors.
        return {
            "protocol_version": "0.1.0",
            "gateway_epoch": self.epoch,
            "devices": [
                {"device_id": did, "available": dev["connected"], "simulated": dev["simulated"]}
                for did, dev in self.devices.items()
            ],
            "event_count": len(self.events),
            "command_count": len(self.commands),
            "credentials": "redacted",
            "physical_verified": False,
        }
