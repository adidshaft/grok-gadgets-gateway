# Alpha release checks

The gateway has public alpha releases on [GitHub](https://github.com/adidshaft/grok-gadgets-gateway/releases).
The Python package is not on PyPI. Protocol version is `0.1.0`.

## Prepare a release

1. Run the repository checks and `uv build`.
2. Review the source, dependency notices and package contents. Keep secrets and local logs out of the release.
3. Record wheel and source-archive checksums.
4. Test the exact component commits in the hub compatibility manifest.
5. Update the release notes and evidence links. Separate simulation, physical tests and Grok Bot results.
6. Obtain the owner's approval for publication. Use `dev` for integration and `main` for releases.
7. Pin hub documentation snapshots to released component commits before a hub release.

Original source uses Apache-2.0. See [NOTICE](../NOTICE) for dependencies and [SECURITY](../SECURITY.md) for private reports.
GitHub issues track current work. Keep local issue IDs in commits so contributors can follow the evidence.

## Evidence limits

The owner tested one ESP32-S3 OLED kit over USB and Wi-Fi on 12 October 2026.
Grok Bot ran local shell commands through `rehearse`. Native MCP access, mobile operation and an independent second installation remain unverified.
The Wi-Fi kit still needs a running gateway computer.
