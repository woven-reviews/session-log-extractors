#!/bin/sh
# Maintainer/CI build only. Candidates download these prebuilt bundles.
set -eu
repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
destination=${1:?usage: package-plugins.sh OUTPUT_DIRECTORY}
mkdir -p "$destination"
destination=$(CDPATH= cd -- "$destination" && pwd)
for platform in darwin linux windows; do
  for architecture in amd64 arm64; do
    bundle="$destination/session-log-plugins-$platform-$architecture"
    mkdir -p "$bundle"
    cp -R "$repo/plugins/." "$bundle/"
    for harness in claude codex copilot; do
      mkdir -p "$bundle/$harness/bin"
    done
    (cd "$repo/launcher" && CGO_ENABLED=0 GOOS="$platform" GOARCH="$architecture" go build -trimpath -o "$bundle/claude/bin/session-log-hook.exe" .)
    cp "$bundle/claude/bin/session-log-hook.exe" "$bundle/codex/bin/"
    cp "$bundle/claude/bin/session-log-hook.exe" "$bundle/copilot/bin/"
    tar -czf "$destination/session-log-plugins-$platform-$architecture.tar.gz" -C "$bundle" .
    rm -r "$bundle"
  done
done
