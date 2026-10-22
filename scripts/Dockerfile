# Build from the repository root:
# docker build -f scripts/Dockerfile -t session-log-extractor .
# The helpers run checkout scripts so repository scoping and output paths use
# the bind-mounted checkout. Claude delegates to the installed package; its thin
# script supplies that checkout root. The image supplies Python and the package.
FROM python:3.12-slim

WORKDIR /extractor
COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir .
COPY scripts/extract_claude_session_log.py scripts/extract_codex_session_log.py scripts/extract_copilot_session_log.py ./scripts/

ENTRYPOINT ["python3"]
