# Historical gateway hardening evidence

This file records the 2026-10-04 correction checkpoints and their original test counts.
Use the [README](../../README.md) and [launch verification](launch-docs.md) for current setup.

Local software evidence only: actual Grok Bot, physical C124 and mobile testing remain pending under GW-004.

## 2026-10-04 — H1 / HARD-GW-001

- Source: `46060230e52ea5dd3834a269ef44cebaf186490a`, branch `fix/gateway-hardening`; starting worktree only had coordinator-seeded `planning/issues.json` additions.
- Reproduction: `uv run python` constructed `Gateway`/`Simulator`, submitted `button` and `state` with `{r:1,g:2,b:3,on:true}`, then called `Simulator.execute()`. Both returned `executed` and changed the RGB state. This confirmed the audited failure before edits.
- Decision: reserve existing `button` and `state` names for event/read interfaces, preserve all other custom string-only command names and optional inline schemas. Discovery retains capabilities, adds callable/event lists, and exposes argument contracts only for commands. No wire schema or fixture changed. Simulator independently validates its dispatched command before reporting execution.
- Checks on corrected working source: `uv run pytest` exit 0 (17 tests); `uv run ruff check .` exit 0; hub `python3 scripts/check.py` exit 0. `uv run python -m grok_gadgets_gateway.demo` exit 0 through official MCP `ClientSession` over actual stdio subprocess: rejects button/state/unknown with `unsupported_capability`, each denied ID remains unknown, four malformed RGB forms fail, reported state is unchanged, RGB/events/disconnect/reconnect remain usable, controls absent in default test.
- Environment: macOS 27.0 arm64, Python 3.11.15, uv 0.12.3, mcp 1.26.0, pytest 9.0.2, Ruff 0.14.14. The MCP dependency emits a nonfatal Pydantic forward-reference warning; tests and tool results pass.
- Commit: pending at this checkpoint; subsequent entry records the committed source. No package built yet.

## 2026-10-04 — H1 / HARD-GW-002 baseline

- Same starting source: device A submitted `original`, device B submitted 256 different IDs, A retried `original`. Result was `{ok:true}` without `duplicate`; sequence increased to 258. This violates the promised isolated 256-ID window.
- Next: isolate current-boot windows per registered device, preserve same-boot reconnect dedupe, retire changed-boot windows, and test bounded lifecycle plus real transport/MCP observation.

## 2026-10-04 — H1 / HARD-GW-002 correction

- HARD-GW-001 source commit: `e0b7256`; required checks passed before accepting the subsequent change.
- Event windows now belong to registered device/current boot, capped at 256 newly accepted IDs each. Same-boot replacement retains dedupe; changed boot retires the previous window. Disconnected descriptors remain subject to the existing 64-device registry cap. No retired sessions/boot dictionaries are introduced; maximum duplicate IDs is 64 × 256. Global event history remains separate and bounded. Returning to a retired boot ID starts a fresh window and is documented explicitly.
- Initial new test run: `uv run pytest tests/test_domain.py` exit 1 (12 pass / 1 fail). The test incorrectly used the default 32-item read cursor as the stream end after 258 events. Corrected the test to use the known current epoch/sequence; no implementation change was needed for this test failure.
- Corrected working-source checks: `uv run pytest` exit 0 (21 tests), `uv run ruff check .` exit 0, hub `python3 scripts/check.py` exit 0. Direct regressions cover same event ID on two devices, B overflow isolation, oldest retained/beyond-window eviction, unchanged sequence/history, changed data/time conflicts, disconnect/same-boot reconnect/new boot/restart, stale session errors, 64-device cap, 257 events per device and 300 replacement boots.
- Actual MCP regression: official `ClientSession` launches the CLI stdio subprocess with a private temporary credential fixture and a loopback listener. Two authenticated TCP devices submit the same ID; B sends 256 further IDs. MCP reads sequence 258; A's retry returns `duplicate:true`, MCP reads no additional event, changed content conflicts. Replaced A session cannot emit; same-boot retry remains duplicate; fresh boot accepts the ID, and MCP reads exactly one new event at sequence 259 with its new boot identity. Fixture-only credential, no real device/account/paid API.
- Canonical schema/fixture bytes unchanged. Protocol README now documents callable built-ins and bounded current-boot lifecycle. SDK protocol schema pins require no update.
- Commit: pending at this checkpoint. Next: commit, integrate main, run the official demo and rebuild wheel/sdist from identified committed source.

## 2026-10-04 16:19 UTC — H1 complete / local package checkpoint

- Both tested commits integrated locally into `main` by fast-forward: command routing `e0b7256`, event isolation `7a4ba40b3e8535ced6dcd8c93d47ab0cdac15c07`. Worktree clean before artifact generation.
- `uv run python -m grok_gadgets_gateway.demo` exit 0 on integrated committed source; refreshed `docs/evidence/mcp-demo.json` includes actual negative MCP results. No real Grok or physical verification is claimed.
- `uv build` exit 0, wheel built from sdist from clean `7a4ba40`. Python 3.11.15/macOS27 arm64; uv0.12.3, hatchling1.28.0. Initial corrected wheel SHA256 `ad21e29244d3e11e4dbcca93c07d4039cc844b6455c77c34c7050a55e2e3ea95`; sdist `d7ffad2f9b30c487f3be60eadcf2d3672f9f35d0751620c42ded3cf3e52d20cb`.
- Installed that wheel into a fresh temporary venv with `uv venv --python .venv/bin/python` and `uv pip install --python <temporary-venv>/bin/python dist/grok_gadgets_gateway-0.1.0a1-py3-none-any.whl`. Changed cwd outside the checkout. `<temporary-venv>/bin/python -I -m grok_gadgets_gateway.demo` exit0; import confirmed temporary site-packages. An additional isolated installed-wheel event probe confirmed A retry after B256 is duplicate and sequence stays257. This install resolves declared dependencies normally (mcp1.26.0/jsonschema4.26.0/pyserial3.5); it is separate from the locked contributor environment.
- Canonical request schema SHA256 remains `e36d56ec7a6cee34e43a156927b494de5fcb2c100f64c05396219d1b62bfe55c`; response remains `e07ca120af819386fcc7912a3269cbc300645783338d9caa98421d115368c597`. Protocol README now `b1ae2a5c8ea0933eb9bcedce01e8c7bbed5215591229a78932637f2a76f07ac1`. Coordinator must refresh documentation/import/compatibility pins; no SDK wire schema copy change is required.
- After this documentation checkpoint commit, rebuild final artifacts from that clean HEAD and record exact source/environment/SHA256 in ignored `dist/build-provenance.json`. This avoids embedding a future self-referential commit hash in tracked evidence. Coordinator owns final multi-repository candidate manifest.
