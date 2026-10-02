# Grok Gadgets gateway

A usable local alpha connection point exclusively for Grok Gadgets. It provides Grok-facing MCP tools, canonical versioned device schemas, a software C124 simulator, authenticated loopback device TCP, and a USB NDJSON bridge. Original code is Apache-2.0; this independent project claims no xAI affiliation.

**Evidence:** simulated, software transport tested. Real Grok Bot, mobile clients, physical C124 and independent installation remain pending. Official MCP client acceptance demonstrates the protocol implementation; it does not establish compatibility with an existing Grok Bot.

## Quick start

Python 3.11+ and [uv](https://docs.astral.sh/uv/) are required. From this repository:

```sh
uv sync --locked
uv run python -m grok_gadgets_gateway.demo
uv run pytest
uv run ruff check .
```

The demo launches a real official MCP stdio client and subprocess gateway, then asserts discovery, green RGB command, retry safety, invalid RGB rejection, simulated button press/release, disconnect error and recovery. The output labels simulation and pending physical/Grok verification. No cloud account, API key or paid call is needed.

An MCP-capable local client can launch:

```sh
uv run grok-gadgets-gateway --simulator
```

Use this command's absolute working directory and uv executable in the client's configuration. The client's host must stay running. Normal tools are gadgets_list_devices, gadgets_get_state, gadgets_command, gadgets_command_status, gadgets_read_events and gadgets_diagnostics. Simulation controls are absent by default; `--simulator --test-controls` explicitly exposes the test_simulator_control tool for local acceptance only.

To customize the software device, generate settings with `uv run python -m grok_gadgets_gateway.simulator_config > simulator-config.json`, edit the bounded fields, and launch with `--simulator --simulator-config /absolute/path/simulator-config.json`. See [configurable simulator installation and settings](docs/simulator.md). Config files cannot enable test controls.

For a Linux SDK agent or USB bridge, prepare per-device credentials and start the same process with `--credentials /private/path/devices.json`. See [local operation](docs/local-operation.md). Device TCP is strictly loopback, default port 8765; it is separate from MCP stdio. Never expose it through a public tunnel. Hosted or remote Grok connection is an open prerequisite.

## Contracts and development

[Protocol 0.1.0](protocol/0.1.0/README.md) owns schemas, fixtures, capabilities, acknowledgements, cursors and retry semantics. [Architecture](docs/architecture.md) explains the reusable domain and transports. [Evidence](docs/evidence/local-alpha.md) records actual validation. [Release preparation](docs/release.md) describes publication gates.

Read [contribution instructions](CONTRIBUTING.md), [security limits](SECURITY.md) and [local issues](planning/issues.json). Shared policies live in the unpublished sibling [project hub](../grok-gadgets/README.md); the gateway is usable independently and does not require the hub to run. GitHub owner, public URLs and remote protections remain pending publication approval.
