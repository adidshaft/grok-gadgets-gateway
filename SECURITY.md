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

- Device TCP is unencrypted, authenticated loopback only. Do not tunnel its port or MCP
  stdio. This is single-user local software, not a hosted tenant service.
- Keep per-device credentials outside Git with mode `0600`. Revocation is checked on
  subsequent requests; old reported state remains in memory.
- Simulator settings are bounded data, not code, endpoints, or credentials. Test controls
  require a separate explicit opt-in.
- State, command arguments, and events are visible to the requesting operator. Keep
  application payloads free of secrets; support diagnostics alone use an allowlist.
- Lost acknowledgements remain unconfirmed. Never blindly retry a physical action under
  a new command ID.

Read [local operation](docs/local-operation.md) before enrollment. Public exposure,
remote authentication, and physical operation need separate review and acceptance.
