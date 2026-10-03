# Alpha release preparation

Proposed package: grok-gadgets-gateway 0.1.0a1; protocol 0.1.0. No release tag, package publication, remote push or deployment is authorized. Build locally with `uv build`; preserve wheel/sdist checksums and test against the exact component commits in the hub compatibility manifest before tagging an approved prerelease.

Original source is Apache-2.0; dependency licenses are recorded in NOTICE and installed distributions. No vendor logo/avatar assets are included. Prepared owner: `adidshaft`; private fallback contact: `adidshaft@kyokasuigetsu.xyz`. GitHub private reporting must be enabled after activation; see [SECURITY](../SECURITY.md).

Prepared CI workflow runs locked installation, named gateway-checks job, lint, tests and package build. Proposed GitHub main protections: PR required; gateway-checks passing; conversation resolution; no force push/deletion; zero mandatory second-human reviews until another real maintainer exists. These settings are not active locally. Local planning/issues.json becomes a migration input, with stable GW IDs mapped to GitHub issues after authorization; GitHub then owns status.

Before publication: approve the exact remote and release payload, review current files and reachable history, create issues/milestones, select tested commits and compatibility map, activate private reporting, and verify hosted checks. Hardware, native Grok invocation/mobile and independent human reproduction remain open gates. Prepared destination: https://github.com/adidshaft/grok-gadgets-gateway; it is not yet an active release/support endpoint.
