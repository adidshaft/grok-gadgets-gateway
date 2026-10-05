# Grok Gadgets gateway

This guide uses ASD-STE100-inspired writing. It does not claim formal compliance. See the [project writing guide](https://github.com/adidshaft/grok-gadgets/blob/main/docs/contributing/writing-guide.md).

Use this gateway to connect local gadget applications through MCP. The project is exclusively for Grok.
The gateway lists device capabilities, sends commands, and reports state and events.
It includes a configurable software simulator.

**Experimental alpha.** Local MCP simulation and software transports are tested.
We have not verified native Grok calls, mobile clients, or physical C124 operation.
An authenticated connection from a cloud Bot to local devices is also pending.

```mermaid
flowchart LR
    C["Local MCP client"] --> G["Gateway"]
    G --> S["Software C124 simulator"]
    G --> D["Linux / ESP32 device application"]
    B["Grok Bot: invocation evidence pending"] -.-> G
```

Solid lines show local software interfaces. A device acknowledgement reports execution.
It does not prove a physical effect. Observe the device separately.
The simulator has no physical effects and makes no Grok API calls.

## Choose a first step

- **Try without hardware:** run the installed MCP demonstration below.
- **Customize:** use the [simulator guide](docs/simulator.md) and a separate `my-light.json`.
- **Connect your application:** read [local operation](docs/local-operation.md) and the
  [versioned protocol](protocol/0.1.0/README.md).
- **Contribute:** use [CONTRIBUTING](CONTRIBUTING.md), [support](SUPPORT.md), and
  [GitHub Issues](https://github.com/adidshaft/grok-gadgets-gateway/issues).

## Run the installed demonstration

You need uv, Python 3.11, and the gateway wheel. Tests used native Apple Silicon
Python 3.11.15. Installation can require network access.
The simulator needs no account, API key, hardware, or open device listener.

Package releases are not published. Build a wheel from this source checkout:

1. Run `uv sync --locked`.
2. Run `uv build`.
3. Copy `grok_gadgets_gateway-0.1.0a1-py3-none-any.whl` to an empty working folder.
4. Open a terminal in that folder.
5. Run these commands:

```sh
uv venv --python 3.11 --seed .venv
.venv/bin/python -m pip install ./grok_gadgets_gateway-0.1.0a1-py3-none-any.whl
.venv/bin/python -m grok_gadgets_gateway.demo
```

The official local MCP client starts the gateway as a child process.
The demo checks discovery, green RGB, state readback, retries, and invalid-command rejection.
It also checks simulated button transitions, offline failure, and reconnection.
The final JSON includes:

```json
{"led_status": "executed", "simulated": true, "physical_verified": false, "grok_verified": false}
```

This example shows part of the report. It is not a native Grok receipt.
The demo enables test controls only in its child simulator.

An ordinary local MCP client launches:

```sh
.venv/bin/grok-gadgets-gateway --simulator
```

Use the absolute executable path in the client configuration. The client manages
the process. The process uses MCP on stdio and waits for requests.
It does not provide an interactive terminal. The standard tools are `gadgets_list_devices`, `gadgets_get_state`,
`gadgets_command`, `gadgets_command_status`, `gadgets_read_events`, and
`gadgets_diagnostics`. `--test-controls` is a separate explicit opt-in.

## Compatibility and evidence

Package `0.1.0a1` and protocol `0.1.0` are separate version identifiers.
The package declares Python `>=3.11` support. We have not tested every interpreter or platform.

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
This device connection is separate from MCP stdio. A cloud Bot cannot execute a path on your computer.
These transports do not implement remote HTTPS/OAuth connections. Read
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
