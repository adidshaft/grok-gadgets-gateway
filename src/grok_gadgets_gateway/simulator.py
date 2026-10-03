"""Explicit simulator adapter and separate controls; never a physical device."""

import copy
import time
import uuid

from .protocol import GatewayError, RGB_VALIDATOR, VERSION
from .simulator_config import validate_config


class Simulator:
    def __init__(self, gateway, device_id=None, *, config=None):
        self.gateway = gateway
        self.config = validate_config(config if config is not None else {"schema_version": 1})
        if device_id is not None:
            self.config = validate_config({**self.config, "device_id": device_id})
        self.device_id = self.config["device_id"]
        self.pressed = False
        self.counter = 0
        self.connect()
        if self.config["start_disconnected"]:
            self.gateway.disconnect(self.device_id, self.sid)

    def connect(self):
        self.boot_id = uuid.uuid4().hex
        self.sid = self.gateway.register(
            {
                "type": "hello",
                "protocol_version": VERSION,
                "device": {
                    "device_id": self.device_id,
                    "model": "M5Stack AtomS3 Lite C124 (simulation)",
                    "firmware_version": "sim-0.1.0",
                    "boot_id": self.boot_id,
                    "simulated": True,
                    "capabilities": ["rgb.set", "button", "state"],
                    "state": {
                        "rgb": copy.deepcopy(self.config["initial_rgb"]),
                        "button": {"pressed": False},
                    },
                },
            },
            display_name=self.config["display_name"],
        )
        self.pressed = False

    def execute(self):
        sid = self.sid
        response = self.gateway.handle(self.device_id, sid, {"type": "poll"})
        for cmd in response["commands"]:
            if self.config["response_delay_ms"]:
                time.sleep(self.config["response_delay_ms"] / 1000)
            # A disconnect/reconnect during the delay must not acknowledge the retired session.
            current = self.gateway.state(self.device_id)
            if not current["available"] or current["session_id"] != sid:
                continue
            state = self.gateway.state(self.device_id)["state"]
            ack = {"type": "ack", "command_id": cmd["command_id"], "state": state}
            if cmd["capability"] != "rgb.set":
                ack.update(
                    status="failed",
                    error={"code": "unsupported_capability", "message": "Unsupported command"},
                )
            elif not RGB_VALIDATOR.is_valid(cmd["arguments"]):
                ack.update(
                    status="failed",
                    error={"code": "invalid_arguments", "message": "Invalid RGB arguments"},
                )
            else:
                state["rgb"] = cmd["arguments"]
                ack["status"] = "executed"
            self.gateway.handle(
                self.device_id,
                sid,
                ack,
            )

    def control(self, action, pressed=None):
        if action == "disconnect":
            self.gateway.disconnect(self.device_id, self.sid)
        elif action == "reconnect":
            self.connect()
        elif action == "button":
            if type(pressed) is not bool:
                raise GatewayError("invalid_event", "pressed must be boolean")
            self.gateway.session(self.device_id, self.sid)
            if pressed != self.pressed:
                self.counter += 1
                self.gateway.handle(
                    self.device_id,
                    self.sid,
                    {
                        "type": "event",
                        "name": "button",
                        "event_id": f"{self.boot_id}-{self.counter}",
                        "data": {"pressed": pressed},
                    },
                )
                self.pressed = pressed
                state = self.gateway.state(self.device_id)["state"]
                state["button"]["pressed"] = pressed
                self.gateway.handle(self.device_id, self.sid, {"type": "state", "state": state})
        else:
            raise GatewayError("invalid_request", "Unknown simulation action")
        return self.gateway.state(self.device_id)
