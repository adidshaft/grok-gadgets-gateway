# Gateway hardening evidence

Local software evidence only: actual Grok Bot, physical C124 and mobile testing remain pending under GW-004. Gateway owner is the bounded `hard_gateway` subagent; model setting inherited from the coordinator (no model override requested by this agent).

## 2026-10-04 — H1 / HARD-GW-001

- Source: `4cf42fffa32afa2e5ad022e3fa4797f474ba10a9`, branch `fix/gateway-hardening`; starting worktree only had coordinator-seeded `planning/issues.json` additions.
- Reproduction: `uv run python` constructed `Gateway`/`Simulator`, submitted `button` and `state` with `{r:1,g:2,b:3,on:true}`, then called `Simulator.execute()`. Both returned `executed` and changed the RGB state. This confirmed the audited failure before edits.
- Decision: reserve existing `button` and `state` names for event/read interfaces, preserve all other custom string-only command names and optional inline schemas. Discovery retains capabilities, adds callable/event lists, and exposes argument contracts only for commands. No wire schema or fixture changed. Simulator independently validates its dispatched command before reporting execution.
- Checks on corrected working source: `uv run pytest` exit 0 (17 tests); `uv run ruff check .` exit 0; hub `python3 scripts/check.py` exit 0. `uv run python -m grok_gadgets_gateway.demo` exit 0 through official MCP `ClientSession` over actual stdio subprocess: rejects button/state/unknown with `unsupported_capability`, each denied ID remains unknown, four malformed RGB forms fail, reported state is unchanged, RGB/events/disconnect/reconnect remain usable, controls absent in default test.
- Environment: macOS 27.0 arm64, Python 3.11.15, uv 0.12.3, mcp 1.26.0, pytest 9.0.2, Ruff 0.14.14. The MCP dependency emits a nonfatal Pydantic forward-reference warning; tests and tool results pass.
- Commit: pending at this checkpoint; subsequent entry records the committed source. No package built yet.

## 2026-10-04 — H1 / HARD-GW-002 baseline

- Same starting source: device A submitted `original`, device B submitted 256 different IDs, A retried `original`. Result was `{ok:true}` without `duplicate`; sequence increased to 258. This violates the promised isolated 256-ID window.
- Next: isolate current-boot windows per registered device, preserve same-boot reconnect dedupe, retire changed-boot windows, and test bounded lifecycle plus real transport/MCP observation.
