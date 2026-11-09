# Version 1 release and verification

This repository prepares the v1 package, public capture image, and host plugin
bundles. Tagging and publishing are separate maintainer actions. Publish and
verify the approved artifacts before the QUAL-2324 assessment canary.

## Build and publish

Run the checks in [AGENTS.md](../AGENTS.md). The tag-triggered release workflow
also requires the reusable Python checks and the native launcher matrix on
macOS, Linux, and Windows.

After approval, create the immutable `v1.0.0` tag on the approved commit and push
it. Do not move or reuse a published version tag. The workflow checks that the
tag matches `pyproject.toml`, builds the wheel and source distribution, compiles
host bundles for amd64/arm64 on all three operating systems, and publishes a
Linux amd64/arm64 capture image to GHCR. Go is a maintainer build dependency;
the bundles contain executables, so candidates need no Go installation.

The workflow creates a draft GitHub release with the distributions, six plugin
archives, `capture-image.txt`, and `SHA256SUMS`. The image reference includes its
manifest digest. Configure the GHCR package for public anonymous access before
publishing the draft release. Image registry and assessment Git authentication
are separate: the image must be downloadable without GitHub credentials.

## Verify published artifacts

1. In a clean disposable container, install
   `git+https://github.com/woven-reviews/session-log-extractors.git@v1.0.0`
   and run `session-log --help`. Do not mount host Git or registry credentials.
2. Build [the Git tag Dockerfile pattern](../examples/Dockerfile.git-tag) from an
   empty context and export a committed native fixture with `session-log`.
3. With an empty Docker CLI configuration and no registry login, pull the exact
   `capture-image.txt` reference. Measure this capture image's cold pull and
   cached startup; timings for a small probe image are not substitutes.
4. Verify the release checksums, extract each supported platform bundle, and
   install its harness plugin. Confirm the configured checkout is active and an
   unrelated checkout is inactive. Review the harness's normal hook trust UI.
5. Verify the live plugin path, including Docker unavailable then repaired,
   final-turn capture, a slow pull, a slow upload, concurrent turns, and recovery
   after a transient failure. Confirm source mounts are individual files and
   the candidate's branch, index, and working files remain unchanged.

All three host operating systems need the live plugin checks before the canary.
The native host CI matrix verifies the executable and command strings; it does
not replace testing the installed harness and Docker together. Paid native
harness sessions require separate explicit authorization.

## Local verification without publication

Maintainers can build the runtime locally; candidates use the published image:

```sh
docker build -t session-log-extractors:verify .
docker image inspect session-log-extractors:verify --format '{{.Id}}'
```

Use the returned immutable image ID for the opt-in integration tests:

```sh
SESSION_LOG_TEST_IMAGE=sha256:THE_LOCAL_IMAGE_ID \
  uv run --locked pytest scripts/test_capture_docker.py -q -s
```

These tests invoke the plugin's declared hook command with committed native
fixture copies, transfer synthetic runtime credentials, and upload to a
disposable local Git target. They verify the live container mounts and remove
their own labeled containers on exit. They run no model sessions or real GitHub
uploads. One probe delays Docker acquisition for 35 seconds; another delays
each Git upload for 35 seconds while another turn arrives. These controlled
delays establish handoff and queue behavior, not published-image download speed.

```sh
cd launcher
go test -race ./...
go vet ./...
```

Build all six plugin archives with `sh scripts/package-plugins.sh OUTPUT_DIR`.
Only the host binary is platform-specific. The capture image runs Linux Python
and Git on Docker Engine or Docker Desktop with Linux containers.
