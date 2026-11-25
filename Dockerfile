# research-mcp for Edward's AI Hub (spec H-007, HD-007, HD-008): the claude.ai research
# connector on :8080 (behind the connector ALB), run as a Deployment in namespace `agents`.
# Build for the cluster's x86 nodes:
#   docker buildx build --platform linux/amd64 -t <ecr>/research-mcp:<tag> --push .
# No key, token or .env enters any layer: see .dockerignore.

ARG PYTHON_IMAGE=python:3.12-slim-trixie
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.10.2

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

FROM ${PYTHON_IMAGE}
# pip isn't used at run time and carries fixable CVEs (as in research-agent).
RUN apt-get update && apt-get -y upgrade && rm -rf /var/lib/apt/lists/* && \
    python -m pip uninstall -y pip && \
    groupadd -g 65532 app && useradd -u 65532 -g app -d /home/app -m -s /usr/sbin/nologin app
COPY --from=build --chown=0:0 /app /app
ENV PATH=/app/.venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/home/app
USER 65532:65532
WORKDIR /home/app
EXPOSE 8080
ENTRYPOINT ["/app/.venv/bin/research-mcp"]
