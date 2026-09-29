"""Canonical schema loading and strict wire validation."""

import json
from importlib.resources import files
from pathlib import Path

from jsonschema import Draft202012Validator

VERSION = "0.1.0"
MAX_FRAME = 2048
_root = Path(__file__).resolve().parents[2] / "protocol" / VERSION
if not _root.exists():
    _root = files("grok_gadgets_gateway").joinpath("protocol", VERSION)
SCHEMA = json.loads(_root.joinpath("device-request.schema.json").read_text())
VALIDATOR = Draft202012Validator(SCHEMA)
RGB_VALIDATOR = Draft202012Validator(SCHEMA["$defs"]["rgb"])
_state_schema = dict(SCHEMA)
_state_schema.pop("oneOf")
_state_schema["$ref"] = "#/$defs/state"
STATE_VALIDATOR = Draft202012Validator(_state_schema)


class GatewayError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)

    def response(self):
        return {"ok": False, "error": {"code": self.code, "message": self.message}}


def validate_request(value):
    if isinstance(value, dict) and value.get("type") == "hello":
        if value.get("protocol_version") != VERSION:
            raise GatewayError("protocol_mismatch", "Expected protocol 0.1.0")
    if not VALIDATOR.is_valid(value):
        raise GatewayError("invalid_request", "Request does not match protocol schema")


def validate_state(value):
    if not STATE_VALIDATOR.is_valid(value):
        raise GatewayError("invalid_state", "Reported state does not match protocol schema")
