# Alpha release checks

The gateway has public alpha releases on [GitHub](https://github.com/adidshaft/grok-gadgets-gateway/releases).
The Python package is on [PyPI](https://pypi.org/project/grok-gadgets-gateway/) from v0.1.0-alpha.10. Protocol version is `0.1.0`.

## Prepare a release

1. Run the repository checks and `uv build`.
2. Review the source, dependency notices and package contents. Keep secrets and local logs out of the release.
3. Let the release workflow write and check `SHA256SUMS` (below).
4. Test the exact component commits in the hub compatibility manifest.
5. Update the release notes and evidence links. Separate simulation, physical tests and Grok Bot results.
6. Obtain the owner's approval for publication. Use `dev` for integration and `main` for releases.
7. Pin hub documentation snapshots to released component commits before a hub release.

## Publish a release

1. Run the repository checks and `uv build`.
2. Merge the release PR into `main`. Tag it, for example `v0.1.0-alpha.7`, and publish the
   GitHub release. The owner approves each step.
3. In **Actions → release packages → Run workflow**, choose `main` and enter the tag. The
   workflow checks that the tag matches the package version. It builds the wheel and the
   source archive and writes `SHA256SUMS` with plain file names. It checks that file in a
   fresh folder and attaches all three files to the release. It never replaces an asset.
4. To upload to PyPI, run it again with **Also upload to PyPI**. The upload uses trusted
   publishing: GitHub gives the job a short-lived identity, and no PyPI token is stored.
   The `pypi` environment waits for the owner's approval.

## Check a download

```sh
mkdir release-check
cd release-check
gh release download v0.1.0-alpha.7 --repo adidshaft/grok-gadgets-gateway
shasum -a 256 -c SHA256SUMS
```

Releases made before this workflow may list `dist/` paths in `SHA256SUMS`
([hub #151](https://github.com/adidshaft/grok-gadgets/issues/151)).

## One-time PyPI setup (owner)

1. Sign in to [pypi.org](https://pypi.org) with two-factor authentication on.
2. Open **Your projects → Publishing → Add a new pending publisher → GitHub**. Enter
   PyPI project `grok-gadgets-gateway`, owner `adidshaft`, repository `grok-gadgets-gateway`, workflow
   `release.yml` and environment `pypi`.
3. In GitHub, open **Settings → Environments → New environment**, name it `pypi`. Add
   `adidshaft` as a required reviewer. Under deployment branches, allow only `main`.

The first approved upload creates the PyPI project. After that, the pending publisher
becomes a normal trusted publisher for `grok-gadgets-gateway`.

Original source uses Apache-2.0. See [NOTICE](../NOTICE) for dependencies and [SECURITY](../SECURITY.md) for private reports.
GitHub issues track current work. Keep local issue IDs in commits so contributors can follow the evidence.

## Evidence limits

The owner tested one ESP32-S3 OLED kit over USB and Wi-Fi on 12 October 2026.
Grok Bot ran local shell commands through `rehearse`. On 13 October 2026 it also called the six tools natively from the Mac and the iPhone through `serve --oauth` behind an owner-run tunnel. An independent second installation remains unverified.
The Wi-Fi kit needs a running gateway. From 0.1.0-alpha.13 that can be an always-on Linux server instead of the Mac; the owner's kit passed this on 13 October 2026 ([record](https://github.com/adidshaft/grok-gadgets/blob/dev/docs/verification/mac-free-route-2026-10-13.md)).
