# Simulator configuration evidence — 2026-10-04

Issue: `SIM-GW-001`. Local implementation branch: `feat/simulator-config`.
Environment: macOS arm64, CPython 3.11.15, uv 0.12.3; official MCP SDK 1.26.0.
Original code remains Apache-2.0; the gateway remains exclusively for Grok Gadgets.

## Validation

- `uv run pytest`: **73 passed**. This includes 52 simulator configuration checks,
  existing default simulator acceptance, authenticated device TCP, USB pseudo-terminal
  transport, command/capability isolation, event windows and canonical fixtures.
- `uv run ruff check .`: passed.
- Hub `python3 scripts/check.py`: passed, including Python source syntax.
- `uv build`: wheel and source distribution built successfully; wheel contains
  `grok_gadgets_gateway/simulator-config.json` and `simulator-config.schema.json`.
- Fresh wheel installation with dependencies from the local uv cache into
  `/tmp/grok-sim-config-install-20261004`: passed. Imports resolve to that environment's
  `site-packages`, not the checkout source.
- From `/tmp`, the installed interpreter ran the configuration test file with
  `--import-mode=importlib`: **52 passed**, including actual MCP subprocess tests.
- From `/tmp`, installed `python -m grok_gadgets_gateway.demo`: passed default MCP
  discovery, green RGB result, retry, malformed requests, button events and reconnect.
- Installed `python -m grok_gadgets_gateway.simulator_config`: printed the packaged
  default settings without requiring the source checkout.

## Behaviors exercised

Actual official `ClientSession` over stdio uses configured `desk-42` identity, `My desk
light` name, initial RGB 11/22/33/on, and 150 ms acknowledgement delay. The round trip
takes at least 130 ms. Discovery/get-state reflect the custom ID/name and RGB; the old
default ID is unknown. A subsequent command updates the reported RGB and keeps retry
results stable. Test tools stay absent unless `--test-controls` is supplied separately.

The offline-start variant reports offline state, rejects commands as `unavailable`, and
rejects simulated button injection until an explicit reconnect. Reconnect starts a new
boot/session and restores the configured initial state; emitted button events use the
configured ID. Direct tests also preserve RGB channels while off, prove the maximum
2000 ms setting requests a two-second delay, and prove a reconnect during that delay
retires the old command as `unconfirmed` without overwriting the new state.

Invalid ID/name bounds, control characters, non-ASCII names, RGB bounds, unknown fields,
command/code/network/credentials/test-tool fields, nonfinite numbers, boolean and float
integer fields, unsupported versions, duplicate keys, malformed/invalid UTF-8 files and
the 4096-byte read boundary are rejected. CLI errors exit 2 on stderr without input
contents or tracebacks; stdout remains empty on startup rejection.

The existing upstream lifespan `IncompleteFieldDefinitionWarning` remains on stderr;
all MCP initialization/tool/shutdown assertions pass.

## Limits and external gates

This is software simulation evidence. No physical C124 observation, actual Grok Bot tool
call, mobile-client verification, independent second-person reproduction, Linux run,
public release, deployment, cloud account or paid API call occurred. `GW-004` remains
blocked by the Grok account route and physical hardware. No device wire protocol schema
changed; `display_name` is trusted local simulator metadata in gateway state/discovery.
