# Public prebuilt manual-export and capture runtime. No build credentials.
FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/session-log
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir .
ENTRYPOINT ["session-log"]
