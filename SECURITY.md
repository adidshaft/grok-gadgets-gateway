# Security policy

Supported scope: current experimental alpha (`0.1.0a1`, protocol `0.1.0`).
No security SLA or support for older snapshots is promised. Include an exact commit
or package version so the maintainer can reproduce the problem.

## Report privately

Use [GitHub private vulnerability reporting](https://github.com/adidshaft/grok-gadgets-gateway/security/advisories/new)
once enabled. That destination is pending repository activation. Until then, or if it
is unavailable, email **adidshaft@kyokasuigetsu.xyz**. Do not post exploit details or
secrets in issues or Reddit. The hub owns the shared
[disclosure process](https://github.com/adidshaft/grok-gadgets/blob/main/SECURITY.md).

Describe versions, reproduction, impact, and a minimal safe example. Remove tokens,
private paths, account identifiers, household state, event bodies, and raw transcripts.
Never attach a credential registry. The maintainer coordinates reproduction, mitigation,
and disclosure privately; do not assume a response SLA.

## Component limits

- Device TCP is unencrypted and accepts `127.0.0.1` only. Do not publish port 8765.
  `serve` adds Streamable HTTP MCP on `127.0.0.1:8766/mcp` with a bearer token. That is
  not an OAuth server. The token file is `mcp-token`, mode `0600`. Host and Origin are
  allow-listed; add a tunnel hostname with `--allowed-host`. The bind address stays
  loopback. This is single-user local software, not a hosted tenant service.
- Keep per-device credentials outside Git with mode `0600`. `enroll` writes them with a
  temp file and `os.replace`. Revocation is checked on subsequent requests; old reported
  state remains in memory. `devices` prints ids only.
- Simulator settings are bounded data, not code, endpoints, or credentials. Test controls
  require a separate explicit opt-in on stdio. HTTP mode cannot enable them.
- HTTP tool logs on stderr are redacted: argument hash, tool name, and outcome. They omit
  tokens, arguments, state, and event bodies.
- State, command arguments, and events are visible to the requesting operator. Keep
  application payloads free of secrets; support diagnostics alone use an allowlist.
- Lost acknowledgements remain unconfirmed. Never blindly retry a physical action under
  a new command ID.

Read [local operation](docs/local-operation.md) and [remote access](docs/remote-access.md)
before enrollment. Publishing your own HTTPS URL, Grok Bot acceptance, and physical
operation are not verified by the local tests.
