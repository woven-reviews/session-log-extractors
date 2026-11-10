#!/bin/sh
# Host transport only. Python in the pinned image owns interpretation and work.
set -eu
umask 077
action=${1:?expected pre, stop, notice, or doctor}
harness=${2:-claude}
trap 'status=$?; if [ "$action" = stop ] && [ "$status" -ne 0 ]; then printf "%s\n" "Capture failed; explain setup repair in chat. Saved signals remain in capture state." >&2; exit 0; fi' 0
fail() {
  printf '%s\n' "$*" 'Explain the setup problem in chat. The candidate must fix it outside the agent, then retry.' >&2
  if [ "$action" = stop ]; then exit 0; fi
  exit 2
}
safe_path() {
  case "$1" in *','*) fail 'Capture paths cannot contain commas.' ;; esac
  [ ! -L "$1" ] || fail 'Capture refuses symbolic-link sources.'
  if [ -d "$1" ]; then
    resolved=$(CDPATH= cd -P -- "$1" && pwd -P) || fail 'Cannot resolve capture directory.'
  else
    [ -f "$1" ] || fail 'The selected capture file is unavailable.'
    resolved=$(CDPATH= cd -P -- "$(dirname -- "$1")" && pwd -P)/$(basename -- "$1")
  fi
  case "$resolved" in /) fail 'Refusing to mount the home directory or an ancestor.' ;; esac
  case "$candidate_home/" in "$resolved/"*) fail 'Refusing to mount the home directory or an ancestor.' ;; esac
}
root=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
[ -f "$root/.session-log-capture" ] || exit 0
case "$harness" in claude|codex|copilot) ;; *) fail 'Unknown capture harness.' ;; esac
candidate_home=$(CDPATH= cd -P -- "$HOME" && pwd -P)
safe_path "$root"
root=$resolved
if [ "$action" = notice ]; then
  printf '%s\n' '{"systemMessage":"Assessment session logs are captured to the session-logs branch. Explain setup failures in chat; the candidate repairs setup outside agent tools."}'
  exit 0
fi
case "$action" in pre|stop|doctor) ;; *) fail 'Unknown capture action.' ;; esac
image=$(cat "$root/.session-log-capture")
digest=${image#ghcr.io/woven-reviews/session-log-extractors@}
case "$digest" in sha256:*) ;; *) fail 'Configure the approved capture image by SHA-256 digest.' ;; esac
hex=${digest#sha256:}
case "$hex" in *[!a-f0-9]*|'') fail 'Invalid capture image digest.' ;; esac
[ "${#hex}" -eq 64 ] || fail 'Invalid capture image digest.'
state=$(git -C "$root" rev-parse --path-format=absolute --git-path session-log-capture)
mkdir -p "$state"
safe_path "$state"
state=$resolved
signal=
if [ "$action" = stop ]; then
  mkdir -p "$state/incoming" "$state/requests"
  signal=$(mktemp "$state/incoming/signal.XXXXXXXX")
  cat > "$signal"
  mv "$signal" "$signal.json"
  signal=$signal.json
fi
engine=$(docker info --format '{{.OSType}}' 2>/dev/null) || fail 'Install or start Docker outside the agent.'
[ "$engine" = linux ] || fail 'Switch Docker to Linux containers outside the agent.'
docker image inspect "$image" >/dev/null 2>&1 || fail "Pull the configured image outside the agent: docker pull $image"
remote=$(git -C "$root" remote get-url origin) || fail 'Configure the assessment origin.'
case "$remote" in git@github.com:*) remote=https://github.com/${remote#git@github.com:} ;; esac
case "$remote" in https://github.com/*/*) ;; *) fail 'Capture requires a GitHub HTTPS origin.' ;; esac
case "${remote#https://github.com/}" in *'@'*|*'?'*|*'#'*|*' '*|*'//'*) fail 'Origin must not contain credentials or URL parameters.' ;; esac
repository=${remote#https://github.com/}
case "$repository" in *[!a-zA-Z0-9_./-]*) fail 'Invalid assessment origin.' ;; esac
case "${repository#*/}" in */*|'') fail 'Invalid assessment origin.' ;; esac
SESSION_LOG_GIT_USERNAME=x-access-token
SESSION_LOG_GIT_PASSWORD=${SESSION_LOG_TOKEN:-}
if [ -z "$SESSION_LOG_GIT_PASSWORD" ]; then
  credentials=$(printf 'url=%s\n\n' "$remote" | GIT_TERMINAL_PROMPT=0 GCM_INTERACTIVE=Never git -C "$root" credential fill 2>/dev/null) || fail 'Configure Git HTTPS authentication outside the agent.'
  while IFS= read -r line; do
    case "$line" in username=*) SESSION_LOG_GIT_USERNAME=${line#username=} ;; password=*) SESSION_LOG_GIT_PASSWORD=${line#password=} ;; esac
  done <<EOF
$credentials
EOF
  unset credentials
fi
[ -n "$SESSION_LOG_GIT_USERNAME" ] && [ -n "$SESSION_LOG_GIT_PASSWORD" ] || fail 'Git HTTPS credentials are unavailable.'
export SESSION_LOG_GIT_USERNAME SESSION_LOG_GIT_PASSWORD
if [ "$action" != stop ]; then exit 0; fi
copilot_home=${COPILOT_HOME:-$candidate_home/.copilot}
metadata=$signal.metadata
docker run --rm --pull=never --network=none \
  --mount "type=bind,src=$signal,dst=/hook.json,readonly" \
  --entrypoint python "$image" -m session_log_extractors.capture_hook describe /hook.json \
  --harness "$harness" --repo "$root" --remote "$remote" --copilot-home "$copilot_home" > "$metadata" \
  || fail 'Cannot decode capture input; signal retained.'
exec 3< "$metadata"
IFS= read -r session_id <&3
IFS= read -r transcript <&3
IFS= read -r state_log <&3
IFS= read -r descriptor <&3
exec 3<&-
safe_path "$transcript"
[ -f "$transcript" ] || fail 'Selected transcript must be a file.'
destination=/source/transcript
if [ "$harness" = copilot ]; then destination=/source/session-store.db; fi
set -- --mount "type=bind,src=$root,dst=/source/repo,readonly" \
  --mount "type=bind,src=$transcript,dst=$destination,readonly"
if [ "$harness" = copilot ]; then
  safe_path "$state_log"
  [ -f "$state_log" ] || fail 'Selected event log must be a file.'
  set -- "$@" --mount "type=bind,src=$state_log,dst=/source/events.jsonl,readonly"
  for suffix in -wal -shm; do
    if [ -e "$transcript$suffix" ]; then
      safe_path "$transcript$suffix"
      set -- "$@" --mount "type=bind,src=$transcript$suffix,dst=$destination$suffix,readonly"
    fi
  done
fi
request=$state/requests/$(basename -- "$signal")
printf '%s\n' "$descriptor" > "$request.tmp"
mv "$request.tmp" "$request"
docker run --detach --rm --pull=never --label "org.woven.capture.state=$state" \
  --mount "type=bind,src=$state,dst=/state" "$@" \
  --env SESSION_LOG_GIT_USERNAME --env SESSION_LOG_GIT_PASSWORD \
  --entrypoint python "$image" -m session_log_extractors.capture_hook run \
  "/state/requests/$(basename -- "$request")" --state /state \
  --harness "$harness" --repo "$root" --remote "$remote" --copilot-home "$copilot_home" >/dev/null \
  || fail 'Cannot start capture; request retained.'
rm -f "$metadata"
exit 0
