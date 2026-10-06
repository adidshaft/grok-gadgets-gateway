# Contributing to the gateway

You can improve documentation, simulation, tests, and diagnostics without hardware
or a Grok account. The gateway owns canonical contracts and MCP routing; device SDK
implementations belong in their respective repositories.

Shared [contribution process](https://github.com/adidshaft/grok-gadgets/blob/main/CONTRIBUTING.md),
[governance](https://github.com/adidshaft/grok-gadgets/blob/main/GOVERNANCE.md), and
[roadmap](https://github.com/adidshaft/grok-gadgets/blob/main/ROADMAP.md) live in the hub.
Use [GitHub Issues](https://github.com/adidshaft/grok-gadgets-gateway/issues) to track current work.
The [local issue list](planning/issues.json) is a preparation record. Small typo fixes need no issue.
Discuss substantial features, interfaces, and protocol changes first.

## Work from one checkout

Use Python 3.11+ and uv. Fork the gateway repository. Clone your fork.
Run these commands from the repository root:

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

## Branches

Branch from `dev` and open your PR into `dev`; it is the default branch and is squash-merged when checks pass. `main` holds only tagged stable releases and changes through release or hotfix PRs. Name branches `<type>/<ISSUE-ID>-<short-slug>`, for example `fix/GW-021-short-name`. The shared [branch and release policy](https://github.com/adidshaft/grok-gadgets/blob/main/CONTRIBUTING.md#branches-and-releases) covers releases, hotfixes and cross-repository changes.

## Make a focused pull request

1. Link the GitHub issue. Include its local ID if one exists.
2. Explain what starts the behavior and what changes. Add a before-and-after example if useful.
3. Add a test that detects the changed behavior.
4. Record check commands, actual results, host, runtime, and evidence level.
5. Update the user guide. Respond to review. Resolve branch conflicts. Run the affected checks again.

Make small commits on short-lived branches from `dev`. The maintainer reviews changes
and credits code, documentation, testing, and reviews. Get approval before a test
that publishes files, deploys a service, or operates a live device.

Before a protocol release, review fixtures and schemas. Update SDK pins and hashes.
Run the hub compatibility checks. Documentation contributors do not need to clone every component.
Separate assistant tools, domain logic, transport, and test controls. An acknowledgement
does not prove a physical effect.

You are responsible for AI-assisted changes. Review the code. Check licenses. Run
each check that you report. Do not include secrets, account captures, or fabricated evidence.
Contributions use the existing Apache-2.0 terms; no additional CLA or sign-off is required.

See [support](SUPPORT.md), [security](SECURITY.md), [conduct](CODE_OF_CONDUCT.md),
and [automation instructions](AGENTS.md).

## Ignore rules and publication privacy

Update `.gitignore` when a new tool creates caches, build output, local configuration, logs, or credentials.
Keep reviewed sample configuration files and the hub's verified public simulator download.
Check new patterns with `git check-ignore`. Review staged files before each commit.
Ignore rules do not remove tracked files or Git history. Never merge private pre-publication history into a public branch.
Use the sanitized public checkout. Use a public or GitHub noreply commit email.
