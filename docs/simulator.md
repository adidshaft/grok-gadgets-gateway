# Configurable Grok Gadgets simulator

This local software device exercises the Grok-facing MCP tools without hardware,
accounts or API keys. Original code is Apache-2.0. Results carry `simulated: true`;
command results and diagnostics keep `physical_verified: false`. Actual Grok Bot, mobile
clients and physical C124 acceptance remain pending.

## Install and start

Python 3.11+ is required; CPython 3.11 on macOS arm64 is the locally tested environment.
From an unpacked gateway source tree:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/python -m grok_gadgets_gateway.simulator_config > my-light.json
.venv/bin/grok-gadgets-gateway --simulator --simulator-config "$PWD/my-light.json"
```

The last command is a stdio MCP server: configure your local MCP client to launch it using
the absolute executable and config paths. It waits for a client speaking MCP. It does not
provide an interactive terminal or a conversation backend. Use a verified Grok client
route when one becomes available. With a built local wheel, replace `pip install .` with
`pip install /absolute/path/grok_gadgets_gateway-0.1.0a1-py3-none-any.whl`.

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

Unknown fields, duplicate keys, booleans in integer fields, integral floats (`1.0`),
nonfinite values, malformed/oversized files and unsupported schema versions are rejected.
The JSON Schema describes the fields and bounds; the loader additionally enforces JSON
integer tokens. This file contains settings only. It cannot configure commands, code,
network endpoints, credentials or test tools. Invalid-file errors go to stderr without
printing the contents; MCP stdout stays reserved for protocol messages.

`--simulator-config` requires `--simulator`. Configuring a file does not enable test
controls. To deliberately inject button edges or exercise reconnection through MCP, add
`--test-controls` separately:

```sh
.venv/bin/grok-gadgets-gateway --simulator --simulator-config "$PWD/my-light.json" --test-controls
```

The `test_simulator_control` tool accepts `disconnect`, `reconnect`, or `button` with a
`pressed` boolean. A reconnect creates a new boot/session, resets RGB to `initial_rgb`
and button state to false, and makes the device available regardless of the original
`start_disconnected` setting. Commands interrupted by a disconnection remain unconfirmed;
they are not replayed. Button injection while offline is rejected. Restarting the process
resets retained commands/events and reapplies the file. Edit settings before launch; no
hot reload is provided.

## Local evidence

`uv run pytest` covers the actual official MCP stdio client and subprocess gateway with
custom identity, display name, initial RGB, configured acknowledgement delay, offline
startup, reconnect, simulated button events and retry results. It also verifies controls
remain absent unless explicitly enabled, strict config and CLI rejection, packaged resource
loading, delayed-session retirement and the 2000 ms upper setting boundary.

This validates software simulation and MCP protocol behavior; it does not establish a real
Grok Bot route or physical effects. External gates remain tracked in `GW-004`.
