# Configurable Grok Gadgets simulator

Use this software device to test the MCP tools intended for Grok.
You do not need hardware, an account, or an API key. Original code is Apache-2.0.
Results include `simulated: true`. Command results and diagnostics include `physical_verified: false`.
Actual Grok calls, mobile clients, and physical C124 operation remain unverified.

## Install and start

Python 3.11+ is required; CPython 3.11 on macOS arm64 is the locally tested environment.
From an unpacked gateway source tree:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/python -m grok_gadgets_gateway.simulator_config > my-light.json
.venv/bin/grok-gadgets-gateway --simulator --simulator-config "$PWD/my-light.json"
```

The last command starts a stdio MCP server. Configure your local MCP client to start it.
Use absolute executable and configuration paths. The server waits for an MCP client.
It does not provide an interactive terminal or a conversation backend.
Use a verified Grok connection when one becomes available.
To install a locally built wheel, replace `pip install .` with
`pip install /absolute/path/grok_gadgets_gateway-0.1.0a1-py3-none-any.whl`.

Local simulation needs no domain, public port, tunnel, or hosted service.
The simulator runs wherever its MCP client starts the process. A cloud client's executable
and configuration paths must exist in that cloud environment; they cannot refer to files only on your computer.
This gateway does not provide a remote HTTPS/OAuth endpoint. Read the
[hosting FAQ](https://github.com/adidshaft/grok-gadgets/blob/main/docs/getting-started/hosting.md)
before choosing a host or connection method. Actual Grok execution remains unverified.

The default and schema are included in the installed package. To inspect the schema,
run `.venv/bin/python -m grok_gadgets_gateway.simulator_config --schema`.
Development checkout users can substitute `uv run` for the `.venv/bin/` executables after
`uv sync --locked`.

## Settings

The file is strict UTF-8 JSON, at most 4096 bytes. `schema_version` must be the JSON integer
`1`. Other fields are optional and default to:

```json
{
  "schema_version": 1,
  "device_id": "sim-c124",
  "display_name": "Desk light",
  "initial_rgb": {"r": 0, "g": 0, "b": 0, "on": false},
  "response_delay_ms": 0,
  "start_disconnected": false
}
```

| Field | Accepted values and effect |
| --- | --- |
| `device_id` | 1–64 characters matching `[A-Za-z0-9][A-Za-z0-9._:-]*`; becomes the ID used in every MCP request and event. |
| `display_name` | A nonblank 1–80-character printable ASCII label; appears in discovery and state as local simulator metadata. |
| `initial_rgb` | Exactly `r`, `g`, `b` JSON integers 0–255 and `on` boolean; becomes the initial reported state. When off, channels are retained. |
| `response_delay_ms` | JSON integer 0–2000; waits this long before acknowledging a simulated command. Scheduling may add latency. Zero preserves immediate responses. |
| `start_disconnected` | Boolean; registers the initial device as offline so discovery can show it while commands return `unavailable`. |

The loader rejects unknown fields, duplicate keys, and booleans in integer fields.
It also rejects integral floats (`1.0`), nonfinite values, malformed or oversized files, and unsupported schema versions.
The JSON Schema defines the fields and limits. The loader also requires JSON integer tokens.

This file contains settings only. It cannot configure commands, code, network endpoints, credentials, or test tools.
Invalid-file errors go to stderr. They do not print the file contents. MCP stdout carries only protocol messages.

`--simulator-config` requires `--simulator`. Configuring a file does not enable test
controls. To deliberately inject button edges or exercise reconnection through MCP, add
`--test-controls` separately:

```sh
.venv/bin/grok-gadgets-gateway --simulator --simulator-config "$PWD/my-light.json" --test-controls
```

The `test_simulator_control` tool accepts `disconnect`, `reconnect`, or `button`.
For `button`, supply a `pressed` boolean.

Reconnection creates a new boot and session. It resets RGB to `initial_rgb` and button state to false.
It makes the device available, even if `start_disconnected` was true.
Commands interrupted by disconnection remain unconfirmed. The simulator does not replay them.
The simulator rejects button events while offline.

A process restart resets retained commands and events. It applies the configuration file again.
Edit settings before you start the process. There is no automatic reload.

## Local evidence

`uv run pytest` covers the actual official MCP stdio client and subprocess gateway with
custom identity, display name, initial RGB, configured acknowledgement delay, offline
startup, reconnect, simulated button events and retry results. It also verifies controls
remain absent unless explicitly enabled, strict config and CLI rejection, packaged resource
loading, delayed-session retirement and the 2000 ms upper setting boundary.

These checks verify software simulation and MCP protocol behavior.
They do not verify a real Grok Bot connection or physical effects.
`GW-004` tracks the remaining external checks.
