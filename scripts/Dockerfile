FROM python:3.12-slim

WORKDIR /extractor
COPY extract_claude_session_log.py extract_codex_session_log.py extract_copilot_session_log.py session_log_envelope.py skill_metadata.py ./

ENTRYPOINT ["python3"]
