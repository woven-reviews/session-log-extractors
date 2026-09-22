# Runs the session-log extractors (extract_claude_session_log.py,
# extract_codex_session_log.py, extract_copilot_session_log.py) without
# requiring a local Python install. Stdlib-only, so this needs no pip install.
#
# Built and run by extract-session-logs.sh / extract-session-logs.ps1 — see
# those for the bind-mount and cwd setup this image expects. Not meant to be
# run directly with plain `docker run`.
FROM python:3.12-slim

WORKDIR /extractor
COPY extract_claude_session_log.py extract_codex_session_log.py extract_copilot_session_log.py session_log_envelope.py skill_metadata.py ./

ENTRYPOINT ["python3"]
