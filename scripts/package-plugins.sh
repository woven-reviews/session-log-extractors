#!/bin/sh
# Maintainer/CI packaging only. Hooks are architecture-independent text.
set -eu
repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
destination=${1:?usage: package-plugins.sh OUTPUT_DIRECTORY}
mkdir -p "$destination"
destination=$(CDPATH= cd -- "$destination" && pwd)
for platform in posix windows; do
    bundle="$destination/session-log-plugins-$platform"
    mkdir -p "$bundle"
    cp -R "$repo/plugins/." "$bundle/"
    for harness in claude codex copilot; do
      cp "$repo/hooks/capture.sh" "$repo/hooks/capture.ps1" "$bundle/$harness/hooks/"
    done
    if [ "$platform" = windows ]; then
      cp "$bundle/claude/hooks/hooks.windows.json" "$bundle/claude/hooks/hooks.json"
    fi
    rm "$bundle/claude/hooks/hooks.windows.json"
    tar -czf "$destination/session-log-plugins-$platform.tar.gz" -C "$bundle" .
    rm -r "$bundle"
done
