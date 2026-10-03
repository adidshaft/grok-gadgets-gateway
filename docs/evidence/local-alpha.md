# Local alpha validation — 2026-10-04

Historical engineering checkpoint. For the current installation journey and launch
preparation checks, use [README](../../README.md) and [launch verification](../verification/launch-docs.md).

Environment: macOS arm64, CPython 3.11.15, uv 0.12.3, official MCP Python SDK 1.26.0. Python 3.11 is the tested runtime; Python >=3.11 support declaration is not evidence that every later version passed.

At e9a887e plus b2ad132 USB recovery fix and demo/docs working tree:

- `uv sync --locked`: passed.
- `uv run pytest`: 13 tests passed (1.31 seconds on recorded run).
- `uv run ruff check .`: passed.
- `uv run python -m grok_gadgets_gateway.demo`: official ClientSession initializes real subprocess stdio server, discovers tools, asserts green LED state, idempotent retry, RGB bounds, simulated button press/release/cursor reads, offline error and reconnect. Output explicitly simulated=true, physical_verified=false, grok_verified=false.
- `uv build`: wheel and source distribution built.
- Fresh `/tmp/grok-gadgets-gateway-clean-venv` installation of the wheel: passed; running installed `python -m grok_gadgets_gateway.demo` passed without importing from the repository package. Packaged schema resource fallback exercised.
- TCP tests: authenticated descriptor, one-command poll and ACK, duplicate ACK, per-device isolation, revocation, replacement sessions, heartbeat idle timeout, 2048-byte frame bound and forbidden nonloopback bind.
- USB pseudo-terminal tests: firmware-side token-free hello, injected per-device token, command/ACK/button event, repeated hello new session, gateway shutdown error and restart recovery. This is software serial framing, not physical USB evidence.
- Domain tests: protocol mismatch, invalid IDs/RGB, custom capability schemas/discovery, bounded queues, stale/offline state, timeouts/late ACK rejection, disconnect/reconnect, event duplicate conflict, ordered history, retention loss, previous-epoch reset and future cursor errors, support-report allowlist.

The pinned SDK with current transitive pydantic-settings emits an `IncompleteFieldDefinitionWarning` on stderr concerning its lifespan settings annotation; all initialization, tool and shutdown assertions pass. It does not contaminate MCP stdout. This upstream non-failing warning is recorded rather than hidden.

No paid model call, cloud Bot invocation, physical C124 operation, mobile-client test, Linux service operation, public endpoint, remote publication or independent installer test occurred. SDK integration evidence is recorded by the component owners and hub. The local alpha can be tested now; those gates remain open.

Configurable simulator update: [52 installed-wheel configuration checks and 73 gateway tests](../verification/simulator-config.md) pass, including actual MCP custom identity/state, delay and offline/reconnect acceptance. These are software checks; the external gates above remain open.
