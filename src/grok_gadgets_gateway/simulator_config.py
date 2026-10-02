"""Bounded local simulator settings, never executable instructions or network configuration."""

import argparse
import copy
import json
import re
from importlib.resources import files
from pathlib import Path

from jsonschema import Draft202012Validator

MAX_CONFIG_BYTES = 4096
_resources = files("grok_gadgets_gateway")
DEFAULT_CONFIG = json.loads(_resources.joinpath("simulator-config.json").read_text())
CONFIG_SCHEMA = json.loads(_resources.joinpath("simulator-config.schema.json").read_text())
_validator = Draft202012Validator(CONFIG_SCHEMA)


class SimulatorConfigError(ValueError):
    """A configuration error whose message never includes supplied file contents."""


def validate_config(value):
    """Validate supplied fields and return a detached configuration with defaults."""
    if not isinstance(value, dict) or not _validator.is_valid(value):
        raise SimulatorConfigError("Simulator configuration does not match schema version 1")
    for field in ("schema_version", "response_delay_ms"):
        if field in value and type(value[field]) is not int:
            raise SimulatorConfigError("Simulator integer fields require JSON integers")
    if "initial_rgb" in value and any(type(value["initial_rgb"][c]) is not int for c in "rgb"):
        raise SimulatorConfigError("Simulator RGB channels require JSON integers")
    # JSON Schema's '$' anchor can match before a final newline; require the whole label/ID.
    if "device_id" in value and not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}", value["device_id"]
    ):
        raise SimulatorConfigError("Simulator device ID must contain only safe ID characters")
    if "display_name" in value and not re.fullmatch(r"[ -~]*[!-~][ -~]*", value["display_name"]):
        raise SimulatorConfigError(
            "Simulator display name must be a nonblank printable ASCII label"
        )
    return {**copy.deepcopy(DEFAULT_CONFIG), **copy.deepcopy(value)}


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SimulatorConfigError("Duplicate simulator configuration fields are not allowed")
        result[key] = value
    return result


def _constant(value):
    raise SimulatorConfigError("Nonfinite simulator configuration values are not allowed")


def load_config(path):
    try:
        with Path(path).open("rb") as handle:
            data = handle.read(MAX_CONFIG_BYTES + 1)
        if len(data) > MAX_CONFIG_BYTES:
            raise SimulatorConfigError("Simulator configuration exceeds 4096 bytes")
        value = json.loads(
            data.decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant
        )
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        raise SimulatorConfigError("Cannot read simulator configuration as UTF-8 JSON") from None
    return validate_config(value)


def main():
    parser = argparse.ArgumentParser(description="Print packaged Grok Gadgets simulator settings")
    parser.add_argument("--schema", action="store_true", help="Print the JSON Schema instead")
    args = parser.parse_args()
    print(json.dumps(CONFIG_SCHEMA if args.schema else DEFAULT_CONFIG, indent=2))


if __name__ == "__main__":
    main()
