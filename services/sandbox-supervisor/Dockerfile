# Expert Work sandbox-supervisor image — Stream I.1.
#
# Reuses the multi-stage uv build pattern (Mini-ADR F-15 / I-1), plus a
# third source stage: the supervisor shells out to the ``docker`` CLI
# (CliDockerClient) to launch sandbox containers, so the runtime stage
# copies in a static ``docker`` client binary.
#
# Docker-out-of-docker (Mini-ADR I-2): the supervisor talks to the host
# daemon via a mounted /var/run/docker.sock — sandboxes run as sibling
# containers on the host daemon. No daemon ships in this image.
#
# Build from the repo root:
#   docker build -f services/sandbox-supervisor/Dockerfile -t expert-work-sandbox-supervisor .

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY packages/ packages/
COPY services/ services/
RUN uv sync --frozen --no-dev --no-editable --package expert-work-sandbox-supervisor


FROM public.ecr.aws/docker/library/python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"
WORKDIR /app

# Non-root service account — image-hygiene baseline. The compose service
# runs as root for docker.sock access (Mini-ADR I-2): the mounted socket
# is the real trust boundary, so the in-container user is not a
# meaningful distinction there.
RUN useradd --uid 10003 --no-create-home --shell /usr/sbin/nologin expert_work

# Static ``docker`` client binary — the supervisor only needs the CLI
# client, not a daemon (docker-out-of-docker, Mini-ADR I-2).
COPY --from=docker:27-cli /usr/local/bin/docker /usr/local/bin/docker

COPY --from=builder --chown=expert_work:expert_work /app/.venv /app/.venv

USER expert_work
EXPOSE 8000
ENTRYPOINT ["uvicorn", "sandbox_supervisor.main:app", "--host", "0.0.0.0", "--port", "8000"]
