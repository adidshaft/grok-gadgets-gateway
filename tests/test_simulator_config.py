import copy
import json
import subprocess
import sys
import time
from importlib.resources import files

import pytest
from jsonschema import Draft202012Validator
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from grok_gadgets_gateway.domain import Gateway
from grok_gadgets_gateway.protocol import GatewayError
from grok_gadgets_gateway.simulator import Simulator
from grok_gadgets_gateway.simulator_config import (
    CONFIG_SCHEMA,
    DEFAULT_CONFIG,
    SimulatorConfigError,
    load_config,
    validate_config,
)


def test_packaged_schema_defaults_and_detached_settings():
    Draft202012Validator.check_schema(CONFIG_SCHEMA)
    resources = files("grok_gadgets_gateway")
    default = json.loads(resources.joinpath("simulator-config.json").read_text())
    assert validate_config({"schema_version": 1}) == default == DEFAULT_CONFIG
    assert Draft202012Validator(CONFIG_SCHEMA).is_valid(default)
    config = validate_config(default)
    config["initial_rgb"]["r"] = 200
    assert default["initial_rgb"]["r"] == DEFAULT_CONFIG["initial_rgb"]["r"] == 0


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"schema_version": 2},
        {"schema_version": True},
        {"schema_version": 1.0},
        {"device_id": "../bad"},
        {"device_id": ""},
        {"device_id": "a" * 65},
        {"device_id": "sim\n"},
        {"display_name": ""},
        {"display_name": " "},
        {"display_name": "x" * 81},
        {"display_name": "Lamp\n"},
        {"display_name": "Lamp\x1b[31m"},
        {"display_name": "Café"},
        {"response_delay_ms": -1},
        {"response_delay_ms": 2001},
        {"response_delay_ms": True},
        {"response_delay_ms": 1.0},
        {"response_delay_ms": float("nan")},
        {"response_delay_ms": float("inf")},
        {"start_disconnected": 1},
        {"initial_rgb": {"r": True, "g": 0, "b": 0, "on": True}},
        {"initial_rgb": {"r": 1.0, "g": 0, "b": 0, "on": True}},
        {"initial_rgb": {"r": -1, "g": 0, "b": 0, "on": True}},
        {"initial_rgb": {"r": 256, "g": 0, "b": 0, "on": True}},
        {"initial_rgb": {"r": 1, "g": 0, "b": 0, "on": 1}},
        {"initial_rgb": {"r": 0, "g": 0, "b": 0}},
        {"initial_rgb": {"r": 0, "g": 0, "b": 0, "on": False, "extra": 1}},
        {"command": "echo invalid"},
        {"code": "import os"},
        {"endpoint": "https://invalid.example"},
        {"credentials": "private"},
        {"test_controls": True},
    ],
)
def test_invalid_settings(fields):
    value = {"schema_version": 1, **fields} if fields else fields
    with pytest.raises(SimulatorConfigError):
        validate_config(value)


@pytest.mark.parametrize(
    "text",
    [
        "[]",
        "null",
        "{",
        '{"schema_version":1,"schema_version":1}',
        '{"schema_version":1,"response_delay_ms":NaN}',
        '{"schema_version":1,"response_delay_ms":Infinity}',
        '{"schema_version":1,"initial_rgb":{"r":1,"r":2,"g":0,"b":0,"on":true}}',
        " " * 4097,
    ],
)
def test_invalid_json_files(tmp_path, text):
    path = tmp_path / "config.json"
    path.write_text(text)
    with pytest.raises(SimulatorConfigError):
        load_config(path)


def test_file_errors_and_size_bound(tmp_path):
    path = tmp_path / "config.json"
    with pytest.raises(SimulatorConfigError):
        load_config(path)
    path.write_bytes(b"\xff")
    with pytest.raises(SimulatorConfigError):
        load_config(path)
    text = '{"schema_version":1}'
    path.write_text(text + " " * (4096 - len(text)))
    assert load_config(path) == DEFAULT_CONFIG
    path.write_text(text + " " * (4097 - len(text)))
    with pytest.raises(SimulatorConfigError):
        load_config(path)


@pytest.mark.parametrize(
    "args,expected",
    [
        (["--simulator-config", "missing.json"], "--simulator-config requires --simulator"),
        (["--test-controls"], "--test-controls requires --simulator"),
        (["--simulator", "--simulator-config", "missing.json"], "Cannot read simulator"),
    ],
)
def test_cli_config_errors(args, expected):
    result = subprocess.run(
        [sys.executable, "-m", "grok_gadgets_gateway.cli", *args],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert expected in result.stderr
    assert result.stdout == ""
    assert "Traceback" not in result.stderr


def test_cli_rejects_config_content_without_logging_it(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"schema_version":1,"credentials":"never-log-this-value"}')
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "grok_gadgets_gateway.cli",
            "--simulator",
            "--simulator-config",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2 and result.stdout == ""
    assert "never-log-this-value" not in result.stderr


def test_configured_off_channels_delay_boundary_and_retired_session(monkeypatch):
    config = {
        **DEFAULT_CONFIG,
        "response_delay_ms": 2000,
        "initial_rgb": {"r": 1, "g": 2, "b": 3, "on": False},
    }
    gateway = Gateway()
    simulator = Simulator(gateway, config=config)
    assert gateway.state(simulator.device_id)["state"]["rgb"] == config["initial_rgb"]
    calls = []

    def disconnect_during_delay(seconds):
        calls.append(seconds)
        simulator.control("disconnect")
        simulator.control("reconnect")

    monkeypatch.setattr("grok_gadgets_gateway.simulator.time.sleep", disconnect_during_delay)
    gateway.command(
        simulator.device_id, "rgb.set", {"r": 200, "g": 0, "b": 0, "on": True}, "interrupted"
    )
    simulator.execute()
    assert calls == [2]
    assert gateway.command_status("interrupted")["status"] == "unconfirmed"
    assert gateway.state(simulator.device_id)["state"]["rgb"] == config["initial_rgb"]
    assert not gateway.command_status("interrupted")["physical_verified"]


@pytest.mark.parametrize("offline,controls", [(False, False), (True, True)])
async def test_actual_mcp_configured_simulator(tmp_path, offline, controls):
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(
        device_id="desk-42",
        display_name="My desk light",
        response_delay_ms=150,
        start_disconnected=offline,
        initial_rgb={"r": 11, "g": 22, "b": 33, "on": True},
    )
    path = tmp_path / "simulator-config.json"
    path.write_text(json.dumps(config))
    args = ["-m", "grok_gadgets_gateway.cli", "--simulator", "--simulator-config", str(path)]
    if controls:
        args.append("--test-controls")
    params = StdioServerParameters(command=sys.executable, args=args)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert ("test_simulator_control" in names) is controls

            async def call(name, args=None):
                result = await client.call_tool(name, args or {})
                assert not result.isError, result.content
                return result.structuredContent or json.loads(result.content[0].text)

            device = (await call("gadgets_list_devices"))["devices"][0]
            assert device["device_id"] == config["device_id"]
            assert device["display_name"] == config["display_name"]
            assert device["simulated"] and device["available"] is not offline
            assert device["state"]["rgb"] == config["initial_rgb"]
            request = {
                "device_id": config["device_id"],
                "capability": "rgb.set",
                "arguments": {"r": 255, "g": 0, "b": 80, "on": True},
                "command_id": "set",
            }
            if offline:
                assert device["freshness"] == "offline"
                assert (await call("gadgets_command", request))["error"]["code"] == "unavailable"
                assert (
                    await call("test_simulator_control", {"action": "button", "pressed": True})
                )["error"]["code"] == "stale_session"
                reconnected = (await call("test_simulator_control", {"action": "reconnect"}))[
                    "device"
                ]
                assert reconnected["available"] and reconnected["boot_id"] != device["boot_id"]
                assert reconnected["state"]["rgb"] == config["initial_rgb"]
            started = time.monotonic()
            command = (await call("gadgets_command", request))["command"]
            assert time.monotonic() - started >= 0.13
            assert command["status"] == "executed" and command["simulated"]
            assert command["physical_verified"] is False
            assert command["reported_state"]["rgb"] == request["arguments"]
            assert (await call("gadgets_command", request))["command"] == command
            state = (await call("gadgets_get_state", {"device_id": config["device_id"]}))["device"]
            assert state["state"]["rgb"] == request["arguments"]
            assert (await call("gadgets_get_state", {"device_id": "sim-c124"}))["error"][
                "code"
            ] == ("unknown_device")
            if controls:
                await call("test_simulator_control", {"action": "button", "pressed": True})
                await call("test_simulator_control", {"action": "button", "pressed": False})
                events = (await call("gadgets_read_events"))["events"]
                assert [event["data"]["pressed"] for event in events] == [True, False]
                assert all(event["device_id"] == config["device_id"] for event in events)
                await call("test_simulator_control", {"action": "disconnect"})
                await call("test_simulator_control", {"action": "reconnect"})
                state = (await call("gadgets_get_state", {"device_id": config["device_id"]}))[
                    "device"
                ]
                assert state["state"]["rgb"] == config["initial_rgb"]
            report = (await call("gadgets_diagnostics"))["report"]
            assert report["physical_verified"] is False
            assert "My desk light" not in json.dumps(report)


@pytest.mark.parametrize("pressed", [True, False])
def test_default_constructor_unchanged_and_offline_control_rejection(pressed):
    gateway = Gateway()
    simulator = Simulator(gateway)
    device = gateway.state("sim-c124")
    assert device["available"] and device["state"]["rgb"] == DEFAULT_CONFIG["initial_rgb"]
    assert simulator.config["response_delay_ms"] == 0
    simulator.control("disconnect")
    with pytest.raises(GatewayError, match="Device session is no longer current"):
        simulator.control("button", pressed)
