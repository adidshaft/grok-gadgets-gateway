# Public-alpha documentation verification

LAUNCH-DOCS-GW-001 covers L2/L3/L6 preparation from `e3cd6f0`. Checks on
2026-10-05 used macOS 27 arm64, CPython 3.11.15. This is documentation/policy
preparation; runtime, tests, scripts, protocol, metadata and lock files are unchanged.

## Source and fresh installation checks

- `uv sync --locked --python 3.11`, `uv run pytest`: 73 passed.
- `uv run ruff check .`, `uv run python -m grok_gadgets_gateway.demo`, `uv build`: passed.
- Hub `python3 scripts/check.py`: passed; 35 labeled records and Python syntax verified.
- 37 relative links in changed Markdown resolved locally; the small Mermaid diagram
  was inspected against implemented local interfaces. External URLs are prepared
  destinations, not evidence that public repositories or reporting features exist.

The exact [README](../../README.md) block ran in an otherwise empty temporary folder
containing only the built wheel. `uv venv --python 3.11 --seed .venv` selected native
arm64 Python 3.11.15; pip installed the wheel and declared dependencies normally.
An isolated import confirmed temporary `site-packages`, with no editable installation
or sibling checkout. The official MCP demo passed discovery, negative command/RGB
validation, command/status/state readback, safe retry, events and offline/reconnect.
Its result was `led_status: executed`, `simulated: true`, `physical_verified: false`,
`grok_verified: false`.

Tested wheel SHA256: `dd0b71b0a9e124962184085c469c6a1bc0292a593d888cf219e4ce703598ac39`.
This names the tested working-source wheel, not a future published artifact. Final
clean-commit package hashes belong in the coordinated candidate manifest.

An exploratory generic `python3` install selected the host's x86_64 CPython 3.13.5
and failed compiling a transitive cryptography dependency with the older Rust toolchain.
The README now explicitly selects the tested Python 3.11 environment. Intel and other
Python/platform combinations remain unverified; this is not a new supported matrix.

## License, privacy and remaining gates

Apache-2.0 LICENSE and NOTICE are unchanged; the wheel includes both. The diagrams
are original Mermaid text and introduce no third-party asset or license dependency.
The bounded read-only scan covered all 10 pre-change reachable commits: no personal
machine path matching the checked pattern or limited token-signature match was found.
Commit author metadata retains `224602646+adidshaft@users.noreply.github.com`. History is preserved; approval
of that identity exposure remains a publication gate. Limited signature scans are not
comprehensive credential clearance.

Public repositories/downloads, private vulnerability reporting, hosted CI and owner
protection remain pending activation. Native Grok invocation, mobile, physical C124,
Windows/Intel and independent human reproduction remain open. GW-004 uses primary
M5 for native Grok evidence with M8 separately linked for physical acceptance.
