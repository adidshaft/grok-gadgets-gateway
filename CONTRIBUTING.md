# Contributing to the gateway

You can improve documentation, simulation, tests, and diagnostics without hardware
or a Grok account. The gateway owns canonical contracts and MCP routing; device SDK
implementations belong in their respective repositories.

Shared [contribution process](https://github.com/adidshaft/grok-gadgets/blob/main/CONTRIBUTING.md),
[governance](https://github.com/adidshaft/grok-gadgets/blob/main/GOVERNANCE.md), and
[roadmap](https://github.com/adidshaft/grok-gadgets/blob/main/ROADMAP.md) live in the hub.
These are planned destinations until approved publication; meanwhile use the supplied
source and [local issues](planning/issues.json). Small typo fixes need no issue ceremony.
Discuss substantial features, interfaces, and protocol changes first.

## Work from one checkout

Use Python 3.11+ and uv. After publication, fork the gateway repository and clone your
fork; for the local candidate use the supplied source. From its root:

```sh
git switch -c docs/clearer-simulator-guide
uv sync --locked --python 3.11
uv run pytest
uv run ruff check .
uv run python -m grok_gadgets_gateway.demo
uv build
```

No sibling checkout is required. Run pytest and Ruff before each commit, including
documentation commits. Run the demo when setup/tool guidance or simulator/MCP behavior
changes. Build and inspect packages when packaging or installation guidance changes.
Hardware checks need separately authorized hardware and remain unverified until observed.

## Make a focused pull request

1. Link the GitHub issue after migration, or the stable local ID while unpublished.
2. Explain the trigger and resulting behavior, with a before/after example when useful.
3. Add a focused behavioral regression for functional changes.
4. Give check commands, actual results, host/runtime, and the evidence level.
5. Update user guidance, respond to review, resolve conflicts on your branch, and rerun affected checks.

Keep small commits and short-lived branches from main. The maintainer reviews integration
and credits code, documentation, testing, and review contributions. Do not publish, deploy,
or operate a live device as part of an unapproved test.

Protocol changes need fixtures/schema review, refreshed SDK pins/hashes, and hub compatibility
acceptance before promotion. Every documentation contributor need not clone all components.
Separate assistant tools, domain logic, transport, and test controls. An acknowledgement
does not prove a physical effect.

You remain accountable for AI-assisted changes: review code, confirm licenses, and run
claimed checks. Do not include secrets, account captures, or fabricated evidence.
Contributions use the existing Apache-2.0 terms; no additional CLA or sign-off is required.

See [support](SUPPORT.md), [security](SECURITY.md), [conduct](CODE_OF_CONDUCT.md),
and [automation instructions](AGENTS.md).

## Ignore rules and publication privacy

Keep `.gitignore` current whenever a new tool produces caches, build output, local device configurations, execution logs or credentials. Preserve reviewed sample configuration files and the hub's verified public simulator download. Check new patterns with `git check-ignore`, then review the staged file list before committing. Ignore rules do not remove tracked files or past history; never merge the private pre-publication history back into a public branch. Use the sanitized public checkout and a public or GitHub noreply commit email.
