# Build from the repository root:
# docker build -f scripts/Dockerfile -t session-log-extractor .
# The helpers run checkout scripts so repository scoping and output paths use
# the bind-mounted checkout. All three scripts delegate to the installed package;
# their thin scripts supply that checkout root. The image supplies Python and
# the package.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /extractor
COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

ENTRYPOINT ["python3"]
