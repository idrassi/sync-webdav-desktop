# Publishing a GitHub release

GitHub Actions automatically builds and publishes a release when a version tag
is pushed. The workflow is `.github/workflows/release.yml`.

## Prepare a release

1. Set the same public version in `settings.py` and `install.py`.
2. Add a matching section to `docs/CHANGELOG.md`, for example:

   ```markdown
   ## 1.0.0 — Initial public release

   ### Features

   - Guided setup and browser authentication.
   ```

   That section becomes the release's **Changes** description. Keep it specific
   to the release; subsequent `##` sections are not included. Empty, duplicate,
   or missing entries cause the workflow to fail before publishing.
3. Run the tests and packaging checks, then commit and push the source and
   workflow to GitHub.
4. Push a version tag pointing to that commit:

   ```bash
   git push origin master
   git tag -a v1.0.0 -m "Sync WebDAV Desktop 1.0.0"
   git push origin v1.0.0
   ```

Both `v1.0.0` and `1.0.0` formats are accepted. Choose one convention (preferably
`vMAJOR.MINOR.PATCH`) and use it consistently. The tag must exactly match both
source version constants. Non-version tags and prerelease/build-suffix tags are
rejected by validation; this workflow publishes stable MAJOR.MINOR.PATCH releases
only. Publishing another version requires updating the source versions and
changelog first.

**The tagged commit must contain the workflow.** Adding the workflow after a tag
was pushed does not automatically run a release for that earlier tag. Rerunning
the failed run also uses the original tagged commit, not fixes pushed to the
branch afterward.

## Workflow behavior

The read-only build job:

- Checks out the tagged source, with actions pinned to immutable commit SHAs.
- Validates version metadata and extracts changelog-based release notes offline.
- Installs distribution prerequisites on the disposable Ubuntu runner.
- Runs all unit tests and offline packaging verification.
- Builds source/Debian packages on Ubuntu 24.04 and one portable noarch RPM in
  a disposable Fedora container, using the tagged commit as `SOURCE_DATE_EPOCH`.
- Generates SHA-256 checksums and uploads validated files for the publishing job.

The compatibility matrix installs the **same** built `.deb` in Ubuntu 20.04,
22.04, 24.04, 26.04 and Debian Bullseye, Bookworm, Trixie containers. It validates
dependency resolution, runs unit tests with each stock Python, checks the installed
desktop entry and Rclone flags, and starts the GUI in an isolated Xvfb/session bus.
Bullseye's security packages come from the signed Debian archive.
A parallel RPM matrix installs one noarch RPM on Fedora 43/44, CentOS Stream
9/10 (with EPEL/CRB in test containers only), openSUSE Leap 16 and Tumbleweed.
CentOS Stream 10 uses headless Weston (Wayland) for its GTK smoke test because
it does not ship Xvfb. No compatibility job installs the vendor server,
authenticates, or mounts cloud files.

All configured Debian and RPM compatibility jobs must pass before publication.
The build/publish runner remains Ubuntu 24.04; it does not impose a compiled ABI
on this source-only package.

A separate job with `contents: write` downloads those files, checks that the
remote tag still resolves to the tested commit (including annotated tags), and
publishes using GitHub CLI. No source scripts or checkout run in the write-enabled
job. Configure repository rulesets to prevent moving/deleting release tags; the
remote check cannot make verification and publication a single atomic operation.

The release includes:

- `sync-webdav-desktop_<version>_all.deb`
- `sync-webdav-desktop-<version>-1.noarch.rpm`
- `sync-webdav-desktop-<version>.tar.gz`
- `SHA256SUMS`

The Debian package is `Architecture: all`: it contains the Python application,
not separate proprietary x86_64/ARM64 Sync binaries. Supported host architectures
still depend on distribution packages and the vendor server downloaded at setup.
The description includes the changelog section, download/install instructions,
platform requirements, and the independent/unofficial project disclaimer.

## GitHub configuration

Enable Actions for the repository. The standard repository `GITHUB_TOKEN` is used;
**no personal access token or extra repository secret is needed**. Repository or
organization policy must allow the publishing job's `contents: write` permission.
Do not disable tag restrictions or protected release environments if your project
uses them. Only trusted maintainers should be able to push release tags.

Publication happens only after successful tests/build/verification. If a release
for the tag already exists, the workflow fails rather than replacing published
assets. Existing releases remain untouched. For an accidental/partial release,
inspect it and clean it up deliberately before rerunning the failed workflow;
do not move an already published tag to different source code.

View progress and errors in the repository's **Actions → Publish release** page.
Local validation of the release notes is also available:

```bash
/usr/bin/python3 packaging/release_notes.py --tag v1.0.0 --output dist/release-notes.md
/usr/bin/python3 -m unittest discover -s tests -v
/usr/bin/python3 packaging/build.py
/usr/bin/python3 packaging/check_distribution.py
```

CI is not a substitute for the desktop, authentication, and target-platform
acceptance tests in `docs/ACCEPTANCE_TESTS.md`. No vendor sign-in, cloud access,
installed user services, or GUI recovery is exercised in the release workflow.
