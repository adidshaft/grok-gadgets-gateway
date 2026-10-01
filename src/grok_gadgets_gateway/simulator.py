"""Explicit simulator adapter and separate controls; never a physical device."""

import uuid

from .protocol import GatewayError, RGB_VALIDATOR, VERSION


class Simulator:
    def __init__(self, gateway, device_id="sim-c124"):
        self.gateway = gateway
        self.device_id = device_id
        self.pressed = False
        self.counter = 0
        self.connect()

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
                        "rgb": {"r": 0, "g": 0, "b": 0, "on": False},
                        "button": {"pressed": False},
                    },
                },
            }
        )
        self.pressed = False

    def execute(self):
        response = self.gateway.handle(self.device_id, self.sid, {"type": "poll"})
        for cmd in response["commands"]:
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
                self.sid,
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
