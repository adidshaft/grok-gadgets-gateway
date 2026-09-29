# Grok Gadgets gateway

Local alpha connection point exclusively for Grok Gadgets. Owns the canonical device protocol, reusable gateway domain, software C124 simulator and Grok-facing MCP tools. Real Grok Bot, mobile clients and physical C124 operation remain unverified.

Python 3.11+ and [uv](https://docs.astral.sh/uv/) are required. Run `uv sync --locked`, then `uv run pytest` and `uv run ruff check .`. No cloud account or paid API is required.

The first substantive slice establishes repository foundations; protocol and runnable commands follow in short-lived tested feature branches. Read [contribution instructions](CONTRIBUTING.md) and [local issues](planning/issues.json).

Apache-2.0 original code; independent project with no claimed xAI affiliation. Shared policies are owned by the sibling project hub; see [community](../grok-gadgets/community/README.md).
