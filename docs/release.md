# Alpha release preparation

Proposed package: grok-gadgets-gateway 0.1.0a1; protocol 0.1.0. No release tag, package publication, remote push or deployment is authorized. Build locally with `uv build`; preserve wheel/sdist checksums and test against the exact component commits in the hub compatibility manifest before tagging an approved prerelease.

Original source is Apache-2.0, dependency licenses are recorded in NOTICE and installed dependency distributions. No restricted vendor logo/avatar assets are included. Owner/contact and private security reporting route remain pending; do not invent GitHub links or email addresses.

Prepared CI workflow runs locked installation, named gateway-checks job, lint, tests and package build. Proposed GitHub main protections: PR required; gateway-checks passing; conversation resolution; no force push/deletion; zero mandatory second-human reviews until another real maintainer exists. These settings are not active locally. Local planning/issues.json becomes a migration input, with stable GW IDs mapped to GitHub issues after authorization; GitHub then owns status.

Before publication: approve owner/remote, review secrets and notices, create issues/milestones, select tested alpha commits and compatibility map, arrange real security contact, and approve publishing. Hardware, Grok account/mobile and independent reproduction are open gates documented separately and must remain clearly pending in release claims.
