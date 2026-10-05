"""Canonical schema loading and strict wire validation."""

import json
from importlib.resources import files
from pathlib import Path

from jsonschema import Draft202012Validator, validators

VERSION = "0.1.0"
MAX_FRAME = 2048
_root = Path(__file__).resolve().parents[2] / "protocol" / VERSION
if not _root.exists():
    _root = files("grok_gadgets_gateway").joinpath("protocol", VERSION)
SCHEMA = json.loads(_root.joinpath("device-request.schema.json").read_text())

# JSON Schema treats 255.0 as an integer; firmware JSON parsers do not, so require real ints.
StrictValidator = validators.extend(
    Draft202012Validator,
    type_checker=Draft202012Validator.TYPE_CHECKER.redefine(
        "integer", lambda _checker, value: type(value) is int
    ),
)
VALIDATOR = StrictValidator(SCHEMA)
RGB_VALIDATOR = StrictValidator(SCHEMA["$defs"]["rgb"])
_state_schema = dict(SCHEMA)
_state_schema.pop("oneOf")
_state_schema["$ref"] = "#/$defs/state"
STATE_VALIDATOR = StrictValidator(_state_schema)


class GatewayError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)

    def response(self):
        return {"ok": False, "error": {"code": self.code, "message": self.message}}


def is_valid(validator, value):
    """Validate without letting deeply nested input escape as RecursionError."""
    try:
        return validator.is_valid(value)
    except RecursionError:
        return False


def validate_request(value):
    if isinstance(value, dict) and value.get("type") == "hello":
        if value.get("protocol_version") != VERSION:
            raise GatewayError("protocol_mismatch", "Expected protocol 0.1.0")
    if not is_valid(VALIDATOR, value):
        raise GatewayError("invalid_request", "Request does not match protocol schema")


def validate_state(value):
    if not is_valid(STATE_VALIDATOR, value):
        raise GatewayError("invalid_state", "Reported state does not match protocol schema")
