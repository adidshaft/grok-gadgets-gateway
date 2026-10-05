# Grok Gadgets gateway

An MCP gateway and configurable software simulator for gadgets targeting Grok.
It discovers device capabilities, routes commands, and reports state and events.

**Experimental alpha.** Local MCP simulation and software transports are tested.
Native Grok invocation receipts, mobile clients, physical C124 operation, and a
reviewed authenticated route from a cloud Bot to local devices remain open.

```mermaid
flowchart LR
    C["Local MCP client"] --> G["Gateway"]
    G --> S["Software C124 simulator"]
    G --> D["Linux / ESP32 device application"]
    B["Grok Bot: invocation evidence pending"] -.-> G
```

Solid paths describe local software interfaces. An execution report is a device
acknowledgement; physical effects require a separate observation. The simulator
has no physical effects and makes no Grok API calls.

## Choose a first step

- **Try without hardware:** run the installed MCP demonstration below.
- **Customize:** use the [simulator guide](docs/simulator.md) and a separate `my-light.json`.
- **Connect your application:** read [local operation](docs/local-operation.md) and the
  [versioned protocol](protocol/0.1.0/README.md).
- **Contribute:** use [CONTRIBUTING](CONTRIBUTING.md), [support](SUPPORT.md), and
  [GitHub Issues](https://github.com/adidshaft/grok-gadgets-gateway/issues).

## Run the installed demonstration

Requirements: uv, Python 3.11, and the gateway wheel. Native Apple Silicon
Python 3.11.15 is the tested baseline. Installation may need network access;
the simulator needs no account, API key, hardware, or open device listener.

Build the wheel from this source checkout; package releases are not yet published. Use
`uv sync --locked` and `uv build`. Place
`grok_gadgets_gateway-0.1.0a1-py3-none-any.whl` in an otherwise empty working folder,
open a terminal there, and run:

```sh
uv venv --python 3.11 --seed .venv
.venv/bin/python -m pip install ./grok_gadgets_gateway-0.1.0a1-py3-none-any.whl
.venv/bin/python -m grok_gadgets_gateway.demo
```

The official local MCP client launches a subprocess gateway and asserts discovery,
green RGB, state readback, safe retry, invalid-command rejection, simulated button
edges, offline failure, and reconnect. The final JSON includes:

```json
{"led_status": "executed", "simulated": true, "physical_verified": false, "grok_verified": false}
```

That is a subset of the report, not a native Grok receipt. The demo deliberately
enables test controls in its child simulator only.

An ordinary local MCP client launches:

```sh
.venv/bin/grok-gadgets-gateway --simulator
```

Use the absolute executable path in the client's configuration. The client supervises
the process, which speaks MCP on stdio and waits for requests; it is not an interactive
terminal. Ordinary tools are `gadgets_list_devices`, `gadgets_get_state`,
`gadgets_command`, `gadgets_command_status`, `gadgets_read_events`, and
`gadgets_diagnostics`. `--test-controls` is a separate explicit opt-in.

## Compatibility and evidence

Package `0.1.0a1` and protocol `0.1.0` are separate version identifiers.
Declared Python `>=3.11` support is not evidence for every interpreter or platform.

| Path | Evidence | Remaining limit |
| --- | --- | --- |
| macOS arm64, CPython 3.11.15 | Source checks and fresh installed-wheel MCP demo | Independent human reproduction |
| Linux aarch64, CPython 3.11.17 container | Gateway domain/TCP used in recorded Linux SDK acceptance | Standalone gateway MCP/platform matrix |
| Simulator | Configured identity, RGB, delay, offline/reconnect | No physical device |
| Device TCP / USB framing | Authenticated loopback and software pseudo-terminal checks | Physical cable, board, and OS permissions |
| Windows / Intel Mac | Not verified | Clean installation and runtime tests |
| Grok / mobile / C124 | Native invocation/mobile/physical evidence pending | Supported client route and observed hardware acceptance |

[Launch verification](docs/verification/launch-docs.md) records the documented quick
start. [Simulator evidence](docs/verification/simulator-config.md) and
[historical alpha checks](docs/evidence/local-alpha.md) retain their actual scopes.

## Architecture and boundaries

This repository owns the gateway, simulator, and canonical schemas. Device libraries
belong in the [Linux SDK](https://github.com/adidshaft/grok-gadgets-linux-sdk) and
[ESP32 SDK](https://github.com/adidshaft/grok-gadgets-esp32-sdk). Shared architecture,
roadmap, and policies live in the [hub](https://github.com/adidshaft/grok-gadgets).
The demo needs no sibling checkout.

Device TCP binds to loopback only and requires per-device credentials outside Git.
It is separate from MCP stdio. A cloud Bot cannot execute a path on your computer.
Remote HTTPS/OAuth connectivity is not implemented by these transports. Read
[architecture](docs/architecture.md), [security](SECURITY.md), and
[release preparation](docs/release.md).

## Troubleshooting and support

| Symptom | Next step |
| --- | --- |
| Server waits silently | Launch through an MCP client, or run the demonstration instead. |
| No simulated device | Include `--simulator`; a config alone does not enable it. |
| Config rejected | Use strict v1 JSON and the [documented bounds](docs/simulator.md). |
| Dependency installation fails | Use the selected Python 3.11 environment above; another system interpreter/architecture is not the verified baseline. |
| Device unavailable | Inspect state; reconnect explicitly in a test session or diagnose the agent. |
| Unconfirmed / timed out | Inspect state and recover safely; never invent a new retry ID for an uncertain physical action. |

Use [SUPPORT](SUPPORT.md), [SECURITY](SECURITY.md), and
[CODE_OF_CONDUCT](CODE_OF_CONDUCT.md). Do not post credentials, household state,
private event bodies, or raw account captures in issues.

Original code is [Apache-2.0](LICENSE); retain [NOTICE](NOTICE) and installed dependency
licenses. This independent project is exclusively for Grok and is not affiliated with xAI.

## History note

Pre-publication commit dates were reconstructed across 29 September–5 October 2026 at the owner’s request. Verification records retain their actual execution dates. See the [history and privacy record](https://github.com/adidshaft/grok-gadgets/blob/main/docs/verification/publication-sanitization.md).
